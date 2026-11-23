"""接收筛选、持久化队列、重试和治理执行互斥测试。"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import receiver
import worker
import atomgit
from errors import IssueProcessingError

RESULT = {"status": "pass", "comment_action": "created"}


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        """创建临时数据库、Flask 测试应用和通知样例，并禁止真实网络访问。

        Args:
            无。
        Returns:
            None；测试环境保存到实例属性并注册自动清理。
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runtime = Path(tmp.name) / "runtime"
        runtime.mkdir()
        (runtime / "repository.yml").write_text("example/project: []\nexample/another: []\n")
        receiver_path = patch.object(receiver, "__file__", str(Path(tmp.name) / "receiver.py"))
        receiver_path.start()
        self.addCleanup(receiver_path.stop)
        self.config = receiver.Config(
            "test-webhook-secret", "test-api-token",
            ("example/project",), str(Path(tmp.name) / "queue.db"),
        )
        self.api_client = atomgit.AtomGit(self.config.token, request_interval=0)
        self.app = receiver.create_app(self.config)
        self.app.testing = True
        self.payload = {"object_kind": "issue", "uuid": "event-1",
                        "project": {"path_with_namespace": "example/project"},
                        "object_attributes": {"action": "open", "iid": 12, "id": 9876}}
        self.network = patch.object(atomgit, "build_opener", side_effect=AssertionError("No live network in tests"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def post(self, payload=None, secret="test-webhook-secret", **headers):
        """通过 Flask 测试客户端发送一条 Webhook 请求。

        Args:
            payload: 请求体；为 None 时使用实例中的通知样例。
            secret: 请求头中的 Webhook 密码。
            headers: 额外请求头，可覆盖同名默认请求头。
        Returns:
            Flask 测试响应，可读取 HTTP 状态码和 JSON 内容。
        """
        with self.app.test_client() as client:
            return client.post("/webhook", json=self.payload if payload is None else payload,
                               headers={"X-GitCode-Token": secret, **headers})

    def rows(self):
        """读取测试数据库中的全部任务记录。

        Args:
            无；使用实例中的测试配置。
        Returns:
            任务字典列表，每个字典对应 events 表的一条记录。
        """
        with receiver.connect(self.config) as db:
            return [dict(r) for r in db.execute("SELECT * FROM events")]

    def test_authentication_required_and_never_governs_inline(self):
        """验证 Webhook 拒绝错误密码，合法通知仅入队，不在接收入口执行处理。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        for secret in ("", "wrong", "错误密码"):
            self.assertEqual(self.post(secret=secret).status_code, 401)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(self.rows()[0]["issue_number"], 12)  # iid, not global id

    def test_comment_other_repository_and_lifecycle_events_are_ignored(self):
        """验证评论、未接入仓库及关闭和重新开启 Issue 的事件被忽略。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        for kind, repo, action in [("note", "example/project", "open"),
                                  ("issue", "other/project", "open"),
                                  ("issue", "example/project", "close"),
                                  ("issue", "example/project", "reopen")]:
            with self.subTest(kind=kind, repo=repo, action=action):
                payload = deepcopy(self.payload)
                payload["object_kind"] = kind
                payload["project"]["path_with_namespace"] = repo
                payload["object_attributes"]["action"] = action
                self.assertEqual(self.post(payload).json["status"], "ignored")
        self.assertEqual(self.rows(), [])

    def test_closed_issue_content_events_are_ignored(self):
        """验证明确已关闭的 Issue 不入队，未关闭或状态缺失时仍可处理。

        Args:
            无。
        Returns:
            None；核对接收状态、任务数量，并确认接收入口不查询平台。
        """
        with patch.object(atomgit.AtomGit, "request") as api:
            for action in ("open", "update"):
                for field in ("title", "description", "body"):
                    payload = deepcopy(self.payload)
                    payload["object_attributes"].update(action=action, state="closed")
                    payload["changes"] = {field: {"previous": "old", "current": "new"}}
                    response = self.post(payload)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json["status"], "ignored")
            self.assertEqual(self.rows(), [])
            for state in ("opened", "open", None):
                payload = deepcopy(self.payload)
                payload["uuid"] = str(state)
                payload["object_attributes"]["action"] = "update"
                if state is not None:
                    payload["object_attributes"]["state"] = state
                payload["changes"] = {"description": {"previous": "old", "current": "new"}}
                self.assertEqual(self.post(payload).status_code, 202)
            self.assertEqual(len(self.rows()), 3)
            api.assert_not_called()

    def test_title_and_description_updates_are_selected(self):
        """验证标题和正文修改事件会保存为待处理任务。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        for key in ("title", "description"):
            payload = deepcopy(self.payload)
            payload["uuid"] = key
            payload["object_attributes"]["action"] = "update"
            payload["changes"] = {key: {"previous": "old", "current": "new"}}
            self.assertEqual(self.post(payload).status_code, 202)
        self.assertEqual(len(self.rows()), 2)

    def test_noncontent_and_unchanged_updates_are_ignored(self):
        """验证非内容修改和内容未实际变化的更新事件被忽略。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        for changes in ({}, {"labels": ["bug"]}, {"description": {"previous": "same", "current": "same"}}):
            payload = deepcopy(self.payload)
            payload["object_attributes"]["action"] = "update"
            payload["changes"] = changes
            self.assertEqual(self.post(payload).json["status"], "ignored")

    def test_update_without_changes_checks_conservatively(self):
        """验证未提供变化详情的更新事件仍然入队，避免漏检。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.payload["object_attributes"]["action"] = "update"
        self.assertEqual(self.post().status_code, 202)

    def test_bad_payload_and_oversize_are_rejected(self):
        """验证无效请求体、非法 Issue 编号和超大请求被拒绝。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.assertEqual(self.post([1]).status_code, 400)
        for iid in (None, True, 0, -1, "#12", "abc", 2**63):
            self.payload["object_attributes"]["iid"] = iid
            self.assertEqual(self.post().status_code, 400)
        with self.app.test_client() as client:
            result = client.post("/webhook", data=b"x" * (1024 * 1024 + 1),
                                 headers={"X-GitCode-Token": self.config.secret}, content_type="application/json")
            self.assertEqual(result.status_code, 413)

    def test_duplicate_delivery_and_restart_keep_one_record(self):
        """验证重复投递和接收应用重建后仍只保存并处理一条任务。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.assertEqual(self.post(**{"X-GitCode-Delivery": "delivery-1"}).status_code, 202)
        self.app = receiver.create_app(self.config)
        self.assertEqual(self.post(**{"X-GitCode-Delivery": "delivery-2"}).json["status"], "duplicate")
        send = Mock(return_value=RESULT)
        with patch.object(worker, "_process_issue", send):
            self.assertTrue(worker.process_one(self.config, client=self.api_client))
            self.assertFalse(worker.process_one(self.config, client=self.api_client))
            self.assertEqual(self.post().json["state"], "completed")
            send.assert_called_once_with(self.config, "example/project", 12, client=self.api_client)

    def test_simultaneous_deliveries_are_deduplicated(self):
        """验证并发通知只入队一次，多个后台调用不会重复处理同一任务。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.post().status_code, range(8)))
        self.assertEqual(results.count(202), 1)
        self.assertEqual(results.count(200), 7)
        send = Mock(return_value=RESULT)
        with patch.object(worker, "_process_issue", send):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _: worker.process_one(self.config, client=self.api_client), range(2)))
            self.assertEqual(send.call_count, 1)

    def test_fallback_identifier_is_stable(self):
        """验证缺少事件 ID 时使用请求内容去重，重复通知不新增任务。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        del self.payload["uuid"]
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(self.post().json["status"], "duplicate")

    def test_network_failure_is_retried_after_delay(self):
        """验证网络失败的任务按延迟重试，并保存下一次成功处理的结果。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.post()
        send = Mock(side_effect=[IssueProcessingError("network error"), RESULT])
        with patch.object(worker, "_process_issue", send):
            worker.process_one(self.config, now=100, client=self.api_client)
            self.assertEqual(self.rows()[0]["state"], "queued")
            self.assertFalse(worker.process_one(self.config, now=129, client=self.api_client))
            self.assertTrue(worker.process_one(self.config, now=130, client=self.api_client))
            self.assertEqual(json.loads(self.rows()[0]["result"]), RESULT)

    def test_forbidden_is_failed_without_automatic_retry(self):
        """验证权限错误直接标记失败，不再自动重试。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.post()
        send = Mock(side_effect=IssueProcessingError("AtomGit HTTP 403", retryable=False))
        with patch.object(worker, "_process_issue", send):
            worker.process_one(self.config, client=self.api_client)
            self.assertEqual(self.rows()[0]["state"], "failed")
            self.assertFalse(worker.process_one(self.config, client=self.api_client))

    def test_retries_are_bounded(self):
        """验证重试次数达到上限后任务失败，且不再被领取。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.post()
        send = Mock(side_effect=IssueProcessingError("AtomGit HTTP 503"))
        with patch.object(worker, "_process_issue", send):
            for attempt in range(worker.MAX_ATTEMPTS):
                self.assertTrue(worker.process_one(self.config, now=attempt * 1000, client=self.api_client))
            self.assertEqual(self.rows()[0]["state"], "failed")
            self.assertFalse(worker.process_one(self.config, now=99999, client=self.api_client))

    def test_worker_crash_recovers_immediately_without_spending_attempt(self):
        """验证取得文件锁后立即恢复中断任务，不消耗中断尝试的次数。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.post()
        with receiver.connect(self.config) as db:
            db.execute("UPDATE events SET state='running', attempts=1, updated_at=100")
        send = Mock(return_value=RESULT)
        with patch.object(worker, "_process_issue", send):
            self.assertTrue(worker.process_one(self.config, now=101, client=self.api_client))
            self.assertEqual(self.rows()[0]["attempts"], 1)
            self.assertEqual(self.rows()[0]["state"], "completed")

    def test_missing_credentials_fail_at_startup(self):
        """验证缺少必需配置时配置加载失败。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        with patch.dict(receiver.os.environ, {}, clear=True), self.assertRaises(ValueError):
            receiver.Config.from_env()

    def test_two_repositories_keep_same_issue_and_event_ids_separate(self):
        """验证不同仓库的同号 Issue 和同 ID 事件独立保存、分别处理。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.config = replace(self.config, repositories=("example/project", "example/another"))
        self.app = receiver.create_app(self.config)
        other = deepcopy(self.payload)
        other["project"]["path_with_namespace"] = "example/another"
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(self.post(other).status_code, 202)
        self.assertEqual(self.post().json["status"], "duplicate")
        self.assertEqual(self.post(other).json["status"], "duplicate")
        send = Mock(return_value=RESULT)
        with patch.object(worker, "_process_issue", send):
            worker.process_one(self.config, client=self.api_client)
            # 重启后仍根据各任务保存的仓库处理，不依赖配置列表顺序。
            self.app = receiver.create_app(self.config)
            worker.process_one(self.config, client=self.api_client)
            self.assertEqual({call.args[1:] for call in send.call_args_list},
                             {("example/project", 12), ("example/another", 12)})
            self.assertTrue(all(row["state"] == "completed" for row in self.rows()))

    def test_removed_repository_is_not_processed(self):
        """验证已从接入列表移除的仓库任务被标记失败。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.post()
        config = replace(self.config, repositories=("example/another",))
        worker.process_one(config, client=self.api_client)
        self.assertEqual(self.rows()[0]["state"], "failed")
        self.assertEqual(self.rows()[0]["last_error"], "仓库已不在允许接入列表中")

    def test_long_task_is_not_reclaimed_by_second_worker(self):
        """验证长任务持锁期间，另一个后台调用无法重新领取该任务。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.post()
        def slow_send(config, repository, number, *, client):
            """模拟长时间处理期间另一个后台调用尝试领取任务，验证文件锁阻止重复处理。

            Args:
                config: 指向同一个测试数据库的配置。
                repository: 模拟处理调用的仓库路径，本函数不使用该参数。
                number: 模拟处理调用的 Issue 编号，本函数不使用该参数。
                client: 从领取任务函数传入的共用测试客户端。
            Returns:
                预设的成功处理结果 RESULT；互斥行为不符合预期时断言失败。
            """
            other = Mock(return_value=RESULT)
            # 模拟模型耗时已超过任务租期，第二个 worker 仍不能领取任务。
            with patch.object(worker, "_process_issue", other):
                self.assertFalse(worker.process_one(config, now=10000, client=client))
            other.assert_not_called()
            return RESULT
        with patch.object(worker, "_process_issue", side_effect=slow_send):
            self.assertTrue(worker.process_one(self.config, now=100, client=self.api_client))
        self.assertEqual(self.rows()[0]["attempts"], 1)

    def test_unexpected_failure_is_redacted_and_retried(self):
        """验证未预期错误会有限重试，日志和任务记录均不泄露令牌。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.post()
        send = Mock(side_effect=RuntimeError(self.config.token))
        with patch.object(worker, "_process_issue", send):
            with self.assertLogs(worker.LOG, level="WARNING") as logs:
                worker.process_one(self.config, now=100, client=self.api_client)
            self.assertEqual(self.rows()[0]["state"], "queued")
            self.assertNotIn(self.config.token, str(logs.output))
            self.assertNotIn(self.config.token, self.rows()[0]["last_error"])

    def test_legacy_tasks_are_superseded_once_without_losing_history(self):
        """旧库按入库顺序标记已覆盖的旧请求，保留全部历史记录。

        Args:
            无。
        Returns:
            None；验证全部历史保留、重复初始化和旧通知重投。
        """
        self.post()
        old_id = self.rows()[0]['event_id']
        with receiver.connect(self.config) as db:
            db.execute("UPDATE events SET state='failed', updated_at=1")
            db.executemany("""INSERT INTO events(event_id, repository, issue_number, state, updated_at)
                              VALUES (?, 'example/project', ?, ?, ?)""", [
                ('first-success', 12, 'completed', 0), ('new-success', 12, 'completed', 2),
                ('old-queued', 13, 'queued', 1), ('new-failed', 13, 'failed', 2),
                ('old-success', 14, 'completed', 1), ('latest-failed', 14, 'failed', 2),
                ('interrupted', 15, 'running', 1), ('new-edit', 15, 'queued', 2)])
        original = self.rows()
        completed = [r for r in original if r['state'] == 'completed']
        receiver.initialize(self.config)
        self.assertEqual([r for r in self.rows() if r['state'] == 'completed'], completed)
        self.assertEqual(len(self.rows()), 9)
        self.assertEqual([{k: v for k, v in r.items() if k != 'state'} for r in self.rows()],
                         [{k: v for k, v in r.items() if k != 'state'} for r in original])
        expected = {13: 'failed', 14: 'failed', 15: 'queued'}
        self.assertEqual({r['issue_number']: r['state'] for r in self.rows()
                          if r['state'] in ('queued', 'running', 'failed')}, expected)
        before = self.rows()
        receiver.initialize(self.config)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.rows(), before)
        with receiver.connect(self.config) as db:
            self.assertEqual(db.execute('SELECT state FROM events WHERE event_id=?', (old_id,)).fetchone()[0], 'superseded')

    def test_failed_new_insert_rolls_back_superseding_previous_request(self):
        """新请求写入失败时，不能让旧请求失去继续处理的资格。

        Args:
            无。
        Returns:
            None；验证覆盖标记和新记录在同一事务中提交或回滚。
        """
        self.post()
        before = self.rows()
        with receiver.connect(self.config) as db:
            db.execute("""CREATE TRIGGER reject_insert BEFORE INSERT ON events
                          BEGIN SELECT RAISE(ABORT, 'write failure'); END""")
        payload = deepcopy(self.payload)
        payload['uuid'] = 'new-edit'
        with self.assertLogs(receiver.LOG, level='ERROR'):
            self.assertEqual(self.post(payload).status_code, 503)
        self.assertEqual(self.rows(), before)
        with receiver.connect(self.config) as db:
            db.execute('DROP TRIGGER reject_insert')
        self.assertEqual(self.post(payload).status_code, 202)
        self.assertEqual([r['state'] for r in self.rows()], ['superseded', 'queued'])

    def test_shared_model_configuration_and_validation(self):
        """验证共享模型配置、默认数据库路径、凭据隐藏及仓库路径校验。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        env = {"WEBHOOK_SECRET": "secret", "ATOMGIT_TOKEN": "token",
               "REPOSITORIES": "ignored/legacy", "AGENT_API_KEY": "model-secret",
               "LLM_BASE_URL": "https://models.example", "LLM_MODEL": "custom"}
        config = receiver.Config.from_env(env)
        self.assertEqual(config.repositories, ("example/project", "example/another"))
        self.assertEqual(config.database, "./runtime/data/events.sqlite3")
        self.assertEqual(config.llm_base_url, "https://models.example")
        self.assertEqual(config.llm_model, "custom")
        self.assertNotIn("model-secret", repr(config))
        self.assertEqual(config.maintainers, {"example/project": (), "example/another": ()})
        path = Path(receiver.__file__).parent / "runtime" / "repository.yml"
        for invalid in ("../project", "example/..", "example", "example/repo/extra", "example/repo?query"):
            path.write_text(f'"{invalid}": []\n')
            with self.subTest(repository=invalid), self.assertRaises(ValueError):
                receiver.Config.from_env(env)


    def test_queue_write_failure_returns_503_without_accepting_event(self):
        """验证数据库写入失败返回 503，恢复后同一通知仍能正常入队。

        Args:
            无。
        Returns:
            None；丢失通知或错误信息泄漏凭据时测试失败。
        """
        with patch.object(receiver, "connect", side_effect=receiver.sqlite3.OperationalError(self.config.token)), self.assertLogs(receiver.LOG) as logs:
            response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(self.config.token, response.get_data(as_text=True))
        self.assertNotIn(self.config.token, str(logs.output))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.post().status_code, 202)

    def test_alternative_event_identifiers_deduplicate_deliveries(self):
        """验证备用事件标识和投递请求头都能去重，无效标识不入队。

        Args:
            无。
        Returns:
            None；备用标识去重失败或无效标识被接受时测试失败。
        """
        del self.payload["uuid"]
        self.payload["produce_random_id"] = "producer-id"
        self.assertEqual(self.post().status_code, 202)
        self.assertEqual(self.post().json["status"], "duplicate")
        del self.payload["produce_random_id"]
        self.assertEqual(self.post(**{"X-GitCode-Delivery": "delivery-id"}).status_code, 202)
        self.assertEqual(self.post(**{"X-GitCode-Delivery": "delivery-id"}).json["status"], "duplicate")
        self.payload["uuid"] = 123
        self.assertEqual(self.post().status_code, 400)
        self.assertEqual(len(self.rows()), 2)

    def test_repeated_interruptions_do_not_exhaust_attempts(self):
        """反复进程中断不耗尽业务重试次数，最终仍能处理成功。

        Args:
            无。
        Returns:
            None；验证超过原尝试上限的多次中断仍可恢复。
        """
        self.post()
        with patch.object(worker, "_process_issue", side_effect=SystemExit(0)):
            for attempt in range(worker.MAX_ATTEMPTS + 2):
                with self.assertRaises(SystemExit):
                    worker.process_one(self.config, now=100 + attempt, client=self.api_client)
                self.assertEqual(self.rows()[0]["state"], "running")
                self.assertEqual(self.rows()[0]["attempts"], 1)
        with patch.object(worker, "_process_issue", return_value=RESULT):
            self.assertTrue(worker.process_one(self.config, now=108, client=self.api_client))
        self.assertEqual(self.rows()[0]["state"], "completed")
        self.assertEqual(self.rows()[0]["attempts"], 1)

    def test_legacy_interrupted_failure_is_recovered(self):
        """升级后恢复旧版本因中断次数耗尽而标为失败的任务。

        Args:
            无。
        Returns:
            None；业务失败仍由既有失败重试规则处理。
        """
        self.post()
        with receiver.connect(self.config) as db:
            db.execute("UPDATE events SET state='failed', attempts=5, last_error='后台处理超时或中断'")
        with patch.object(worker, "_process_issue", return_value=RESULT):
            self.assertTrue(worker.process_one(self.config, now=100, client=self.api_client))
        self.assertEqual(self.rows()[0]["state"], "completed")
        self.assertEqual(self.rows()[0]["attempts"], 5)

    def test_health_and_invalid_json_do_not_enqueue_tasks(self):
        """验证健康检查可访问，损坏 JSON 和缺少必要结构的通知均不入队。

        Args:
            无。
        Returns:
            None；接口状态或队列内容不符时测试失败。
        """
        with self.app.test_client() as client:
            self.assertEqual(client.get("/health").json, {"status": "ok"})
            response = client.post("/webhook", data='{"broken":', content_type="application/json",
                                   headers={"X-GitCode-Token": self.config.secret})
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post({"object_kind": "issue"}).status_code, 400)
        self.assertEqual(self.rows(), [])



    def test_rate_limit_pauses_all_tasks_but_new_webhooks_are_saved(self):
        """验证限流暂停全部任务，重启后仍等待，新通知可正常入队。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        self.post()
        self.payload["uuid"] = "event-2"
        self.payload["object_attributes"]["iid"] = 13
        self.post()
        limited = IssueProcessingError("AtomGit 请求被限流，HTTP 429", status_code=429, retry_after=120)
        with patch.object(worker, "_process_issue", side_effect=limited) as send:
            self.assertTrue(worker.process_one(self.config, now=100, client=self.api_client))
            self.assertFalse(worker.process_one(self.config, now=101, client=self.api_client))
            self.assertEqual(send.call_count, 1)
        # 重建接收应用模拟重启，冷却时间保留，新通知仍可入队。
        self.app = receiver.create_app(self.config)
        self.payload["uuid"] = "event-3"
        self.payload["object_attributes"]["iid"] = 35
        self.assertEqual(self.post().status_code, 202)
        with patch.object(worker, "_process_issue", return_value=RESULT) as send:
            self.assertFalse(worker.process_one(self.config, now=219, client=self.api_client))
            send.assert_not_called()
            self.assertEqual(sum(row["attempts"] for row in self.rows()), 1)
            for _ in range(3):
                self.assertTrue(worker.process_one(self.config, now=220, client=self.api_client))
            self.assertTrue(all(row["state"] == "completed" for row in self.rows()))

    def test_last_attempt_rate_limit_still_pauses_other_tasks_and_requeue(self):
        """验证最后一次尝试遇到限流时，其他任务及手动重试仍需等待。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        self.post()
        with receiver.connect(self.config) as db:
            db.execute("UPDATE events SET attempts = ?", (worker.MAX_ATTEMPTS - 1,))
        limited = IssueProcessingError("HTTP 429", status_code=429, retry_after=900)
        with patch.object(worker, "_process_issue", side_effect=limited):
            self.assertTrue(worker.process_one(self.config, now=100, client=self.api_client))
        self.assertEqual(self.rows()[0]["state"], "failed")
        self.assertEqual(worker.retry_failed(self.config), 1)
        with patch.object(worker, "_process_issue", return_value=RESULT) as send:
            self.assertFalse(worker.process_one(self.config, now=999, client=self.api_client))
            send.assert_not_called()
            self.assertTrue(worker.process_one(self.config, now=1000, client=self.api_client))

    def test_existing_database_upgrade_keeps_events(self):
        """验证已有数据库补建限流表时保留任务记录。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        self.post()
        before = self.rows()
        with receiver.connect(self.config) as db:
            db.execute("DROP TABLE api_cooldown")
            db.execute("DROP TABLE pending_comments")
            db.execute("INSERT INTO issue_checks (repository, issue_number, content_hash) VALUES ('example/project', 12, 'already-checked')")
        receiver.initialize(self.config)
        self.assertEqual(self.rows(), before)
        with receiver.connect(self.config) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM api_cooldown").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pending_comments").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT content_hash FROM issue_checks").fetchone()[0], 'already-checked')


if __name__ == "__main__":
    unittest.main()
