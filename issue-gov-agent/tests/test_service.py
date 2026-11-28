"""服务化队列、多密钥轮换和竞争消费测试。不访问真实平台、数据库或消息代理。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import issue_gov.analysis as analysis
import issue_gov.checks.tools.compliance.assess as compliance_assess
import issue_gov.checks.tools.compliance.check as compliance_check
import issue_gov.checks.tools.compliance.comment as compliance_comment
import issue_gov.checks.tools.compliance.parse as compliance_parse
import issue_gov.checks.tools.compliance.recommend as compliance_recommend
import issue_gov.checks.tools.compliance.run as compliance_tool
import issue_gov.receiver as receiver
import issue_gov.worker as worker
from common.errors import IssueProcessingError

RESULT = {"status": "pass", "comment_action": "suppressed"}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "repository.yml").write_text("example/project: []\n", encoding="utf-8")
        patched = patch("issue_gov.paths.service_root", return_value=root)
        patched.start()
        self.addCleanup(patched.stop)
        self.root = root
        self.base = {
            "WEBHOOK_SECRET": "secret", "ATOMGIT_TOKEN": "token",
            "LLM_API_KEY": "single-key", "LLM_BASE_URL": "https://models.example",
            "LLM_MODEL": "model",
        }

    def test_single_key_keeps_task_retry_and_ignores_service_urls(self):
        env = {**self.base, "DATABASE_URL": "postgresql://user:secret@db/app",
               "RABBITMQ_URL": "amqp://user:secret@mq/%2F", "LLM_API_KEYS": ""}
        config = receiver.Config.from_env(env)
        self.assertEqual(config.agent_api_keys, ("single-key",))
        self.assertFalse(config.llm_failover)
        self.assertEqual(config.database_url, "")
        self.assertEqual(config.rabbitmq_url, "")
        self.assertNotIn("secret", repr(config))
        self.assertNotIn("single-key", repr(config))

    def test_multiple_keys_and_service_mode_are_explicit(self):
        env = {**self.base, "LLM_API_KEYS": " key-a, key-b, key-a ",
               "SERVICE_MODE": "queue",
               "DATABASE_URL": "postgresql://user:secret@db/app",
               "RABBITMQ_URL": "amqp://user:secret@mq/%2F",
               "LLM_KEY_RETRIES": "2", "LLM_KEY_RETRY_INTERVAL": "10"}
        config = receiver.Config.from_env(env)
        self.assertEqual(config.agent_api_keys, ("key-a", "key-b"))
        self.assertEqual(config.agent_api_key, "key-a")
        self.assertTrue(config.llm_failover)
        self.assertEqual(config.llm_key_retries, 2)
        self.assertEqual(config.llm_key_retry_interval, 10)
        self.assertNotIn("key-a", repr(config))
        self.assertNotIn("secret", repr(config))
        env["DATABASE_URL"] = ""
        env["RABBITMQ_URL"] = ""
        with self.assertRaisesRegex(ValueError, "POSTGRES_USER"):
            receiver.Config.from_env(env)

    def test_service_urls_are_built_from_passwords(self):
        env = {**self.base, "SERVICE_MODE": "queue",
               "POSTGRES_USER": "issue", "POSTGRES_PASSWORD": "dbpass", "POSTGRES_DB": "issue_gov",
               "RABBITMQ_USER": "issue", "RABBITMQ_PASSWORD": "qpass"}
        config = receiver.Config.from_env(env)
        self.assertEqual(config.database_url, "postgresql://issue:dbpass@postgres:5432/issue_gov")
        self.assertEqual(config.rabbitmq_url, "amqp://issue:qpass@rabbitmq:5672/%2F")
        self.assertNotIn("dbpass", repr(config))
        self.assertNotIn("qpass", repr(config))

    def test_file_urls_do_not_enable_service_mode_without_process_flag(self):
        (self.root / ".env").write_text(
            "WEBHOOK_SECRET=secret\nATOMGIT_TOKEN=token\nLLM_API_KEY=single-key\n"
            "LLM_BASE_URL=https://models.example\nLLM_MODEL=model\n"
            "DATABASE_URL=postgresql://user:secret@db/app\n"
            "RABBITMQ_URL=amqp://user:secret@mq/%2F\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            config = receiver.Config.from_file()
        self.assertEqual(config.database_url, "")
        with patch.dict(os.environ, {"SERVICE_MODE": "queue"}, clear=True):
            from_file = receiver.Config.from_file()
        self.assertEqual(from_file.database_url, "postgresql://user:secret@db/app")
        self.assertEqual(from_file.rabbitmq_url, "amqp://user:secret@mq/%2F")
        with patch.dict(os.environ, {"SERVICE_MODE": "queue",
                                     "DATABASE_URL": "postgresql://user:other@postgres/app",
                                     "RABBITMQ_URL": "amqp://user:other@rabbitmq/%2F"}, clear=True):
            enabled = receiver.Config.from_file()
        self.assertEqual(enabled.database_url, "postgresql://user:other@postgres/app")
        self.assertNotIn("other", repr(enabled))


class KeyFailoverTests(unittest.TestCase):
    def test_each_key_retries_twice_then_switches_and_dead_letters(self):
        calls = []
        sleeps = []

        def fail(title, body, templates, key, model, base_url):
            calls.append(key)
            raise IssueProcessingError("模型响应超时")

        with patch.object(compliance_recommend, "recommend_template", side_effect=fail), patch.object(
                compliance_tool.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(IssueProcessingError) as raised:
                compliance_tool._recommend("未知", "正文", [], "ignored", "model", "https://models.example",
                                    ("key-a", "key-b"), 2, 10)
        self.assertTrue(raised.exception.keys_exhausted)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(calls, ["key-a", "key-a", "key-a", "key-b", "key-b", "key-b"])
        self.assertEqual(sleeps, [10, 10, 10, 10])
        self.assertNotIn("key-a", str(raised.exception))

    def test_success_on_next_key_stops_rotation(self):
        calls = []

        def consult(payload, key, model, base_url):
            calls.append(key)
            if key == "key-a":
                raise IssueProcessingError("模型响应超时")
            return {"template_fit": "wrong", "template": "缺陷", "reason": "匹配", "maintainer": None,
                    "alignments": [], "fields": [], "conflicts": []}

        template = {"name": "缺陷", "title": "[Bug] ", "body": [
            {"type": "textarea", "attributes": {"label": "问题描述"}, "validations": {"required": True}}]}
        with patch.object(compliance_parse, "parse_template", return_value=template), patch.object(
                compliance_check, "check_issue", return_value={"status": "no_match", "matched_templates": [], "checks": []}), \
                patch.object(compliance_assess, "consult", side_effect=consult), patch.object(
                compliance_tool.time, "sleep") as pause:
            result = analysis.analyze_issue(
                "未知", "正文", [{"filename": "a.yml", "source": "s", "content": "x"}],
                "ignored", "model", "https://models.example", repository="example/project", number=2,
                agent_api_keys=("key-a", "key-b"), llm_key_retries=2, llm_key_retry_interval=10)
        self.assertEqual(calls, ["key-a", "key-a", "key-a", "key-b"])
        self.assertEqual(pause.call_count, 2)
        self.assertEqual(result["summary"]["recommendation_status"], "recommended")

    def test_auth_failure_switches_key_without_retrying_it(self):
        calls = []

        def fail(title, body, templates, key, model, base_url):
            calls.append(key)
            raise IssueProcessingError("模型鉴权失败，未能完成模板推荐。", failure="auth")

        with patch.object(compliance_recommend, "recommend_template", side_effect=fail), patch.object(
                compliance_tool.time, "sleep") as pause:
            with self.assertRaises(IssueProcessingError) as raised:
                compliance_tool._recommend("未知", "正文", [], "ignored", "model", "https://models.example",
                                    ("key-a", "key-b"), 2, 10)
        self.assertTrue(raised.exception.keys_exhausted)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(calls, ["key-a", "key-b"])
        pause.assert_not_called()

    def test_invalid_output_retries_call_without_exhausting_keys(self):
        calls = []
        sleeps = []

        def fail(title, body, templates, key, model, base_url):
            calls.append(key)
            raise IssueProcessingError("模型未返回有效的推荐结果，未能完成模板推荐。", failure="invalid_output")

        with patch.object(compliance_recommend, "recommend_template", side_effect=fail), patch.object(
                compliance_tool.time, "sleep", side_effect=sleeps.append):
            with self.assertRaises(IssueProcessingError) as raised:
                compliance_tool._recommend("未知", "正文", [], "ignored", "model", "https://models.example",
                                    ("key-a", "key-b"), 2, 10)
        self.assertFalse(raised.exception.keys_exhausted)
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(calls, ["key-a", "key-a", "key-a"])
        self.assertEqual(sleeps, [10, 10])
        self.assertIn("模型未返回有效的推荐结果", str(raised.exception))

    def test_configuration_error_does_not_sleep_or_rotate(self):
        with patch.object(compliance_recommend, "recommend_template",
                          side_effect=IssueProcessingError("未配置 LLM_BASE_URL 或 LLM_MODEL")) as recommend, \
                patch.object(compliance_tool.time, "sleep") as pause:
            with self.assertRaises(IssueProcessingError) as raised:
                compliance_tool._recommend("未知", "正文", [], "key-a", "", "", ("key-a", "key-b"), 2, 10)
        self.assertFalse(raised.exception.keys_exhausted)
        self.assertEqual(recommend.call_count, 1)
        pause.assert_not_called()


class QueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "repository.yml").write_text("example/project: []\n", encoding="utf-8")
        patched = patch("issue_gov.paths.service_root", return_value=root)
        patched.start()
        self.addCleanup(patched.stop)
        self.config = receiver.Config(
            "secret", "token", ("example/project",), str(Path(temporary.name) / "queue.db"),
            agent_api_key="model-key", llm_base_url="https://models.example", llm_model="model",
            rabbitmq_url="amqp://issue:issue@localhost/%2F")
        self.app = receiver.create_app(self.config)
        self.payload = {"object_kind": "issue", "uuid": "event-1",
                        "project": {"path_with_namespace": "example/project"},
                        "object_attributes": {"action": "open", "iid": 12}}

    def post(self, payload=None):
        with self.app.test_client() as client:
            return client.post("/webhook", json=self.payload if payload is None else payload,
                               headers={"X-GitCode-Token": "secret"})

    def rows(self):
        with receiver.connect(self.config) as db:
            return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY rowid")]

    def test_publish_marks_dispatching_and_failure_releases_without_losing_task(self):
        with patch.object(receiver, "publish_analysis_task") as publish:
            response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json["state"], "queued")
        self.assertEqual(self.rows()[0]["state"], "dispatching")
        publish.assert_called_once()
        payload = {**self.payload, "uuid": "event-2", "object_attributes": {"action": "open", "iid": 13}}
        with patch.object(receiver, "publish_analysis_task", side_effect=receiver.QueuePublishError("消息队列不可用")):
            failed = self.post(payload)
        self.assertEqual(failed.status_code, 503)
        released = [row for row in self.rows() if row["issue_number"] == 13][0]
        self.assertEqual(released["state"], "queued")
        with patch.object(receiver, "publish_analysis_task") as again:
            retried = self.post(payload)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json["status"], "duplicate")
        again.assert_called_once()
        self.assertEqual([row for row in self.rows() if row["issue_number"] == 13][0]["state"], "dispatching")

    def test_delivered_message_runs_before_next_attempt(self):
        """已经在队列里的消息必须开始分析，不能因为宽限时间还没到就确认丢掉。"""
        now = time.time()
        with receiver.connect(self.config) as db:
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state,
                          attempts, next_attempt, updated_at)
                          VALUES ('early', 'example/project', 21, 'queued', 0, ?, ?)""",
                       (now + 120, now))
        with patch.object(worker, "_process_issue", return_value=dict(RESULT)) as process:
            action = worker.handle_delivery(self.config, {"event_id": "early"}, False, client=object())
        self.assertEqual(action, "ack")
        process.assert_called_once()
        row = [item for item in self.rows() if item["event_id"] == "early"][0]
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["attempts"], 1)

    def test_confirmed_publish_does_not_cover_a_rescheduled_retry(self):
        """消费者已经把任务排回重试后，晚到的确认不能再把它改成 dispatching。"""
        now = time.time()
        with receiver.connect(self.config) as db:
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state,
                          attempts, next_attempt, updated_at)
                          VALUES ('moved', 'example/project', 22, 'queued', 1, ?, ?)""",
                       (now + worker.RETRY_DELAY, now))
        receiver._mark_dispatching(self.config, "moved", now + 30, now)
        row = [item for item in self.rows() if item["event_id"] == "moved"][0]
        self.assertEqual(row["state"], "queued")
        receiver._mark_dispatching(self.config, "moved", now + worker.RETRY_DELAY, now)
        row = [item for item in self.rows() if item["event_id"] == "moved"][0]
        self.assertEqual(row["state"], "dispatching")

    def test_cooldown_ack_is_not_marked_dispatched_afterwards(self):
        """限流期间先确认消息时，晚到的投递确认不能再把任务写成 dispatching。"""
        now = time.time()
        lease = now + 30
        with receiver.connect(self.config) as db:
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state,
                          attempts, next_attempt, updated_at)
                          VALUES ('leased', 'example/project', 23, 'queued', 0, ?, ?)""",
                       (lease, now))
            db.execute("INSERT INTO api_cooldown(name, until) VALUES ('atomgit', ?)", (now + 1,))
        action = worker.handle_delivery(self.config, {"event_id": "leased"}, False, client=object())
        self.assertEqual(action, "ack")
        receiver._mark_dispatching(self.config, "leased", lease, now)
        row = [item for item in self.rows() if item["event_id"] == "leased"][0]
        self.assertEqual(row["state"], "queued")
        self.assertNotEqual(row["updated_at"], now)

    def test_related_delivery_races_do_not_lose_or_cover_the_task(self):
        """限流、完成、覆盖和重试之后，晚到的投递确认都停在原来的结论上。"""
        now = time.time()
        with receiver.connect(self.config) as db:
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state,
                          attempts, next_attempt, updated_at)
                          VALUES ('done', 'example/project', 31, 'completed', 1, ?, ?)""",
                       (now + 5, now))
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state,
                          attempts, next_attempt, updated_at)
                          VALUES ('old', 'example/project', 32, 'superseded', 0, ?, ?)""",
                       (now + 5, now))
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state,
                          attempts, next_attempt, updated_at)
                          VALUES ('held', 'example/project', 33, 'dispatching', 0, ?, ?)""",
                       (now + 30, now))
            db.execute("INSERT INTO api_cooldown(name, until) VALUES ('atomgit', ?)", (now + 60,))
        receiver._mark_dispatching(self.config, "done", now + 5, now)
        receiver._mark_dispatching(self.config, "old", now + 5, now)
        action = worker.handle_delivery(self.config, {"event_id": "held"}, False, client=object())
        self.assertEqual(action, "ack")
        receiver._mark_dispatching(self.config, "held", now + 30, now)
        rows = {row["event_id"]: row for row in self.rows()}
        self.assertEqual(rows["done"]["state"], "completed")
        self.assertEqual(rows["old"]["state"], "superseded")
        self.assertEqual(rows["held"]["state"], "queued")
        self.assertEqual(rows["held"]["attempts"], 0)
        self.assertGreaterEqual(rows["held"]["next_attempt"], now + 59)

    def test_in_flight_message_is_not_marked_dispatched_after_it_was_handled(self):
        """消息在投递确认之前就被处理时，确认不能把已完成或已推迟的任务盖回去。"""
        event_id = hashlib.sha256(b"example/project:event-1").hexdigest()

        def finish_before_confirm(*_args, **_kwargs):
            with patch.object(worker, "_process_issue", return_value=dict(RESULT)):
                self.assertEqual(
                    worker.handle_delivery(self.config, {"event_id": event_id}, False, client=object()),
                    "ack")

        with patch.object(receiver, "publish_analysis_task", side_effect=finish_before_confirm):
            response = self.post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.rows()[0]["state"], "completed")

        payload = {**self.payload, "uuid": "event-cooldown", "object_attributes": {"action": "open", "iid": 41}}
        cooldown_id = hashlib.sha256(b"example/project:event-cooldown").hexdigest()
        with receiver.connect(self.config) as db:
            db.execute("INSERT INTO api_cooldown(name, until) VALUES ('atomgit', ?)", (time.time() + 60,))

        def defer_before_confirm(*_args, **_kwargs):
            self.assertEqual(
                worker.handle_delivery(self.config, {"event_id": cooldown_id}, False, client=object()),
                "ack")

        with patch.object(receiver, "publish_analysis_task", side_effect=defer_before_confirm):
            deferred = self.post(payload)
        self.assertEqual(deferred.status_code, 202)
        held = [row for row in self.rows() if row["event_id"] == cooldown_id][0]
        self.assertEqual(held["state"], "queued")
        self.assertEqual(held["attempts"], 0)
        with receiver.connect(self.config) as db:
            db.execute("DELETE FROM api_cooldown")

        payload = {**self.payload, "uuid": "event-retry", "object_attributes": {"action": "open", "iid": 42}}
        retry_id = hashlib.sha256(b"example/project:event-retry").hexdigest()

        def fail_before_confirm(*_args, **_kwargs):
            with patch.object(worker, "_process_issue", side_effect=IssueProcessingError("暂时失败")):
                self.assertEqual(
                    worker.handle_delivery(self.config, {"event_id": retry_id}, False, client=object()),
                    "ack")

        with patch.object(receiver, "publish_analysis_task", side_effect=fail_before_confirm):
            retried = self.post(payload)
        self.assertEqual(retried.status_code, 202)
        retry = [row for row in self.rows() if row["event_id"] == retry_id][0]
        self.assertEqual(retry["state"], "queued")
        self.assertEqual(retry["attempts"], 1)
        self.assertGreater(retry["next_attempt"], time.time())

    def test_duplicate_dispatching_event_is_not_published_again(self):
        with patch.object(receiver, "publish_analysis_task") as publish:
            self.assertEqual(self.post().status_code, 202)
            self.assertEqual(self.post().status_code, 200)
        publish.assert_called_once()

    def test_competing_handlers_process_one_event_once_and_store_report(self):
        with receiver.connect(self.config) as db:
            for number in range(1, 21):
                db.execute("""INSERT INTO events(event_id, repository, issue_number, state, updated_at)
                              VALUES (?, 'example/project', ?, 'dispatching', ?)""",
                           (f"event-{number}", number, time.time()))
        calls = []

        def process(config, repository, number, *, client, checks=None):
            time.sleep(0.01)
            calls.append(number)
            return dict(RESULT)

        client = object()
        with patch.object(worker, "_process_issue", side_effect=process):
            with ThreadPoolExecutor(max_workers=8) as pool:
                actions = list(pool.map(
                    lambda number: worker.handle_delivery(
                        self.config, {"event_id": f"event-{number}"}, False, client=client),
                    range(1, 21)))
        self.assertEqual(actions, ["ack"] * 20)
        self.assertEqual(sorted(calls), list(range(1, 21)))
        with receiver.connect(self.config) as db:
            states = [row["state"] for row in db.execute("SELECT state FROM events")]
            reports = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        self.assertEqual(states, ["completed"] * 20)
        self.assertEqual(reports, 20)

    def test_same_event_is_not_processed_twice(self):
        with receiver.connect(self.config) as db:
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state, updated_at)
                          VALUES ('same', 'example/project', 7, 'dispatching', ?)""", (time.time(),))
        calls = []

        def process(config, repository, number, *, client, checks=None):
            time.sleep(0.05)
            calls.append(number)
            return dict(RESULT)

        with patch.object(worker, "_process_issue", side_effect=process):
            with ThreadPoolExecutor(max_workers=2) as pool:
                actions = list(pool.map(
                    lambda _: worker.handle_delivery(self.config, {"event_id": "same"}, False, client=object()),
                    range(2)))
        self.assertEqual(sorted(actions), ["ack", "ack"])
        self.assertEqual(calls, [7])

    def test_exhausted_keys_mark_failed_and_request_dead_letter(self):
        with receiver.connect(self.config) as db:
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state, updated_at)
                          VALUES ('dead', 'example/project', 8, 'dispatching', ?)""", (time.time(),))
        error = IssueProcessingError("全部模型密钥均调用失败", retryable=False, keys_exhausted=True)
        with patch.object(worker, "_process_issue", side_effect=error):
            action = worker.handle_delivery(self.config, {"event_id": "dead"}, False, client=object())
        self.assertEqual(action, "nack")
        row = self.rows()[0]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["dead_lettered"], 1)
        self.assertIn("全部模型密钥均调用失败", row["last_error"])
        with receiver.connect(self.config) as db:
            report = db.execute("SELECT status, result FROM reports WHERE event_id='dead'").fetchone()
        self.assertEqual(report["status"], "failed")
        self.assertIn("全部模型密钥均调用失败", report["result"])

    def test_retryable_failure_waits_instead_of_dead_letter(self):
        with receiver.connect(self.config) as db:
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state, updated_at)
                          VALUES ('later', 'example/project', 9, 'dispatching', ?)""", (100,))
        with patch.object(worker, "_process_issue", side_effect=IssueProcessingError("暂时失败")):
            action = worker.handle_delivery(
                replace(self.config, database_url=""), {"event_id": "later"}, False, client=object())
        self.assertEqual(action, "ack")
        row = self.rows()[0]
        self.assertEqual(row["state"], "queued")
        self.assertGreater(row["next_attempt"], 100)
        self.assertEqual(row["dead_lettered"], 0)


if __name__ == "__main__":
    unittest.main()
