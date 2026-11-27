"""PostgreSQL 路径的关键语句回归。SQLite 单测无法暴露方言差异（如 MAX 双参）。

无 PostgreSQL 时整组跳过：设置 PG_TEST_DATABASE_URL，
例如 postgresql://issue:issue@127.0.0.1:54329/issue_gov_test。
CI 中由 service 容器提供。本地先启动 postgres；连接串里的库不存在且账号可以建库时，这里会创建它。
"""

import os
from urllib.parse import urlsplit, urlunsplit
import time
import unittest

import issue_gov  # noqa: F401  # 把仓库根目录放进导入路径，后续才能导入 common
from common.errors import IssueProcessingError

TEST_URL = os.environ.get("PG_TEST_DATABASE_URL", "").strip()

# SQLite 单测复用同一断言；PG 环境注入连接串后走真实 PostgreSQL。
if not TEST_URL:
    raise unittest.SkipTest("未设置 PG_TEST_DATABASE_URL，跳过 PostgreSQL 路径测试")


def _ensure_database(url: str) -> str:
    """目标库不存在时，用同一账号在维护库 postgres 里创建它。库已存在时直接返回。"""
    import re

    import psycopg
    from psycopg import sql

    try:
        with psycopg.connect(url, connect_timeout=5):
            return url
    except psycopg.OperationalError as exc:
        if getattr(exc, "sqlstate", None) != "3D000" and "does not exist" not in str(exc):
            raise
    parts = urlsplit(url)
    name = parts.path.lstrip("/").split("?")[0]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
        raise RuntimeError(f"无法为测试创建数据库：{name}") from None
    admin = urlunsplit((parts.scheme, parts.netloc, "/postgres", parts.query, ""))
    with psycopg.connect(admin, connect_timeout=5, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    return url


def _config(database_url: str):
    """构造最小服务配置，只用于数据库层语句验证。"""
    from issue_gov.settings import Config
    return Config(
        secret="test-secret", token="test-token",
        repositories=("example/repo",), database=":memory:",
        database_url=database_url, rabbitmq_url="",
    )


class PostgresPathTests(unittest.TestCase):
    """覆盖队列模式下消费者真正执行的数据库写入路径。"""

    @classmethod
    def setUpClass(cls):
        from common.pgstore import initialize
        from common.store import connect
        cls.url = _ensure_database(TEST_URL)
        initialize(cls.url)
        cls.config = _config(cls.url)
        cls.connect = staticmethod(lambda: connect(cls.config))

    def _reset(self, event_id="pg-evt-1", state="running", attempts=1):
        with self.connect() as db:
            db.execute("DELETE FROM events")
            db.execute("DELETE FROM reports")
            db.execute("DELETE FROM api_cooldown")
            db.execute("""INSERT INTO events(event_id, repository, issue_number, updated_at,
                            attempts, next_attempt, state, checks)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                       (event_id, "example/repo", 1, time.time(), attempts, 0, state,
                        '[{"kind":"compliance","parameters":{}}]'))

    def test_save_failure_429_writes_cooldown_and_schedules_retry(self):
        """429 限流必须写入 api_cooldown 且任务回到 queued —— 回归：MAX(a,b) 在 PG 上非法。"""
        from issue_gov.worker import save_failure
        self._reset()
        config = _config(self.url)
        exc = IssueProcessingError("AtomGit 请求被限流，HTTP 429", True,
                                   status_code=429, retry_after=30)
        state, delay = save_failure(config, "pg-evt-1", 1, exc, time.time())
        self.assertEqual(state, "queued")
        self.assertGreaterEqual(delay, 30)
        with self.connect() as db:
            until = db.execute("SELECT until FROM api_cooldown WHERE name='atomgit'").fetchone()
            self.assertIsNotNone(until, "429 后必须写入 api_cooldown，否则限流冷却失效")

    def test_save_failure_429_keeps_longer_cooldown(self):
        """已有更长冷却时，新的 429 不得缩短它 —— 回归：MAX(until, excluded.until)。"""
        from issue_gov.worker import save_failure
        self._reset()
        config = _config(self.url)
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO api_cooldown(name, until) VALUES ('atomgit', ?)", (now + 300,))
        exc = IssueProcessingError("AtomGit 请求被限流，HTTP 429", True,
                                   status_code=429, retry_after=30)
        save_failure(config, "pg-evt-1", 1, exc, now)
        with self.connect() as db:
            until = db.execute("SELECT until FROM api_cooldown WHERE name='atomgit'").fetchone()[0]
        self.assertGreaterEqual(until, now + 299, "重复 429 不能把冷却时间改短")

    def test_save_failure_dead_letter_marks_failed(self):
        """全部模型密钥失败时任务标记 failed 且 dead_lettered=1。"""
        from issue_gov.worker import save_failure
        self._reset(attempts=5)
        config = _config(self.url)
        exc = IssueProcessingError("全部模型密钥均调用失败", retryable=False, keys_exhausted=True)
        state, _ = save_failure(config, "pg-evt-1", 5, exc, time.time())
        self.assertEqual(state, "failed")
        with self.connect() as db:
            row = db.execute("SELECT state, dead_lettered FROM events").fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["dead_lettered"], 1)

    def test_save_success_completes_and_reports(self):
        """成功路径写 completed 和 reports。"""
        from issue_gov.worker import save_success
        self._reset()
        config = _config(self.url)
        save_success(config, "pg-evt-1", 1,
                     {"status": "pass", "comment_action": "suppressed"}, time.time())
        with self.connect() as db:
            state = db.execute("SELECT state FROM events").fetchone()[0]
            count = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        self.assertEqual(state, "completed")
        self.assertEqual(count, 1)

    def test_claim_due_leases_without_marking_dispatching(self):
        """到期任务先加上发布租约，确认之前仍是 queued，且不会被领第二次。"""
        from common.pgstore import claim_due
        self._reset(state="queued")
        now = time.time()
        first = claim_due(self.url, now)
        second = claim_due(self.url, now)
        self.assertEqual([row["event_id"] for row in first], ["pg-evt-1"])
        self.assertGreater(first[0]["next_attempt"], now)
        self.assertEqual(second, [], "租约内的任务不能被领取两次")
        with self.connect() as db:
            row = db.execute("SELECT state, next_attempt FROM events").fetchone()
        self.assertEqual(row["state"], "queued")
        self.assertGreater(row["next_attempt"], now)

    def test_claim_due_does_not_republish_a_message_still_dispatching(self):
        """已经在队列中的任务停留再久也不再投一份。"""
        from common.pgstore import claim_due
        now = time.time()
        self._reset(state="dispatching")
        with self.connect() as db:
            db.execute("UPDATE events SET updated_at = ?", (now - 120,))
        self.assertEqual(claim_due(self.url, now), [])
        with self.connect() as db:
            state = db.execute("SELECT state FROM events").fetchone()[0]
        self.assertEqual(state, "dispatching")

    def test_handle_delivery_uses_shared_locking_claim(self):
        """handle_delivery 竞争领取：同 event 并发调用只处理一次（借线程池模拟）。"""
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import issue_gov.worker as worker
        self._reset(state="dispatching", attempts=0)
        config = _config(self.url)
        calls = []

        def process(config_, repository, number, *, client, checks=None):
            time.sleep(0.02)
            calls.append(number)
            return {"status": "pass", "comment_action": "suppressed"}

        with patch.object(worker, "_process_issue", side_effect=process):
            with ThreadPoolExecutor(max_workers=4) as pool:
                actions = list(pool.map(
                    lambda _: worker.handle_delivery(
                        config, {"event_id": "pg-evt-1"}, False, client=object()),
                    range(4)))
        self.assertEqual(actions, ["ack"] * 4)
        self.assertEqual(calls, [1], "并发领取后只应执行一次处理")

    def test_concurrent_webhooks_for_one_issue_leave_single_active_task(self):
        """回归：PostgreSQL 下 BEGIN IMMEDIATE 是空操作，同一 Issue 的并发入队必须仍然只留一条活跃任务。

        修复前 8 条并发通知会得到 6 条 queued；现在用按 Issue 的咨询锁串行化后应为 1 条 queued。
        """
        from concurrent.futures import ThreadPoolExecutor
        import issue_gov.receiver as receiver
        self._reset()
        with self.connect() as db:
            db.execute("DELETE FROM events")
        config = _config(self.url)
        app = receiver.create_app(config)
        payload = {"object_kind": "issue", "project": {"path_with_namespace": "example/repo"},
                   "object_attributes": {"action": "open", "iid": 77, "state": "open"}}

        def post(index):
            body = {**payload, "uuid": f"concurrent-{index}"}
            return app.test_client().post("/webhook", headers={"X-GitCode-Token": "test-secret"}, json=body).status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(post, range(8)))
        self.assertEqual(statuses, [202] * 8)
        with self.connect() as db:
            states = [row["state"] for row in db.execute(
                "SELECT state FROM events WHERE issue_number = 77").fetchall()]
        self.assertEqual(len(states), 8)
        self.assertEqual(states.count("queued"), 1, f"只能有一条活跃任务，实际状态：{states}")
        self.assertEqual(states.count("superseded"), 7)

    def test_cooldown_does_not_requeue_running_task(self):
        """回归：限流冷却只能推迟尚未开始的任务，不能把 running 改回 queued，否则持有者的完成写入会落空。"""
        from unittest.mock import patch
        import issue_gov.worker as worker
        self._reset(state="running", attempts=1)
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO api_cooldown(name, until) VALUES ('atomgit', ?)", (now + 60,))
        config = _config(self.url)

        def process(config_, repository, number, *, client, checks=None):
            # 冷却不改 running 时，消息走接管分支并真正执行处理；这里让它成功完成。
            return {"status": "pass", "comment_action": "suppressed"}

        with patch.object(worker, "_process_issue", side_effect=process):
            action = worker.handle_delivery(config, {"event_id": "pg-evt-1"}, True, client=object())
        self.assertEqual(action, "ack")
        with self.connect() as db:
            row = db.execute("SELECT state, attempts FROM events").fetchone()
        self.assertEqual((row["state"], row["attempts"]), ("completed", 1),
                         "running 任务不得被冷却分支改回 queued，应正常完成")
        # 对照：queued 任务在冷却期内被推迟，等待到期重投
        self._reset(state="queued", attempts=0)
        with self.connect() as db:
            db.execute("INSERT INTO api_cooldown(name, until) VALUES ('atomgit', ?)", (now + 60,))
        action = worker.handle_delivery(config, {"event_id": "pg-evt-1"}, False, client=object())
        self.assertEqual(action, "ack")
        with self.connect() as db:
            row = db.execute("SELECT state, next_attempt FROM events").fetchone()
        self.assertEqual(row["state"], "queued")
        self.assertGreaterEqual(row["next_attempt"], now + 59)

    def test_stale_owner_cannot_overwrite_takeover_result(self):
        """回归：消息重投后任务被新消费者接管，旧持有者的完成写入必须被拒绝。"""
        import issue_gov.worker as worker
        self._reset(state="running", attempts=1)
        with self.connect() as db:
            db.execute("UPDATE events SET owner = 'new-owner'")
        config = _config(self.url)
        written = worker.save_success(config, "pg-evt-1", 1, {"status": "pass", "comment_action": "created"},
                                      time.time(), owner="stale-owner")
        self.assertFalse(written, "旧持有者的写入应被拒绝")
        with self.connect() as db:
            row = db.execute("SELECT state, result, owner FROM events").fetchone()
            reports = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        self.assertEqual(row["state"], "running", "旧持有者不能把任务标为 completed")
        self.assertIsNone(row["result"])
        self.assertEqual(row["owner"], "new-owner")
        self.assertEqual(reports, 0, "旧持有者的结果不应写入 reports")
        # 新持有者自己的写入照常生效
        written = worker.save_success(config, "pg-evt-1", 1, {"status": "pass", "comment_action": "created"},
                                      time.time(), owner="new-owner")
        self.assertTrue(written)
        with self.connect() as db:
            row = db.execute("SELECT state, owner FROM events").fetchone()
        self.assertEqual((row["state"], row["owner"]), ("completed", None))

    def test_failure_by_stale_owner_keeps_takeover_state(self):
        """回归：旧持有者的失败写入同样不得覆盖接管者的状态或安排重试。"""
        import issue_gov.worker as worker
        self._reset(state="running", attempts=1)
        with self.connect() as db:
            db.execute("UPDATE events SET owner = 'new-owner'")
        config = _config(self.url)
        exc = IssueProcessingError("临时错误", retryable=True)
        state, _ = worker.save_failure(config, "pg-evt-1", 1, exc, time.time(), owner="stale-owner")
        self.assertEqual(state, "taken_over")
        with self.connect() as db:
            row = db.execute("SELECT state, attempts, next_attempt, owner FROM events").fetchone()
        self.assertEqual((row["state"], row["attempts"], row["owner"]), ("running", 1, "new-owner"))

    def test_null_owner_and_other_holder_cannot_write(self):
        """持有者为空，或已被覆盖且标识不是当前持有者时，不能写结果或报告。"""
        import issue_gov.worker as worker
        self._reset(state="running", attempts=1)
        config = _config(self.url)
        written = worker.save_success(config, "pg-evt-1", 1, {"status": "pass", "comment_action": "created"},
                                      time.time(), owner="intruder")
        self.assertFalse(written)
        with self.connect() as db:
            row = db.execute("SELECT state, result FROM events").fetchone()
            reports = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        self.assertEqual(row["state"], "running")
        self.assertIsNone(row["result"])
        self.assertEqual(reports, 0)
        with self.connect() as db:
            db.execute("UPDATE events SET state = 'superseded', owner = 'holder'")
        written = worker.save_success(config, "pg-evt-1", 1, {"status": "pass", "comment_action": "created"},
                                      time.time(), owner="intruder")
        self.assertFalse(written)
        with self.connect() as db:
            row = db.execute("SELECT state, result, owner FROM events").fetchone()
            reports = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        self.assertEqual((row["state"], row["owner"]), ("superseded", "holder"))
        self.assertIsNone(row["result"])
        self.assertEqual(reports, 0)

    def test_current_holder_can_record_superseded_result(self):
        """原持有者在任务被新通知覆盖后仍可记下结果，但状态保持 superseded。"""
        import issue_gov.worker as worker
        self._reset(state="running", attempts=1)
        with self.connect() as db:
            db.execute("UPDATE events SET state = 'superseded', owner = 'holder'")
        config = _config(self.url)
        written = worker.save_success(config, "pg-evt-1", 1, {"status": "pass", "comment_action": "suppressed"},
                                      time.time(), owner="holder")
        self.assertTrue(written)
        with self.connect() as db:
            row = db.execute("SELECT state FROM events").fetchone()
            reports = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        self.assertEqual(row["state"], "superseded")
        self.assertEqual(reports, 1)

    def test_redelivery_takeover_runs_once(self):
        """回归：先接管的消费者已经在检查时，后到的重投不能再执行一次。"""
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch
        import issue_gov.worker as worker
        self._reset(state="running", attempts=1)
        config = _config(self.url)
        calls = []
        started = threading.Event()
        release = threading.Event()

        def process(config_, repository, number, *, client, checks=None):
            calls.append(number)
            started.set()
            self.assertTrue(release.wait(2))
            return {"status": "pass", "comment_action": "suppressed"}

        def deliver(_index):
            return worker.handle_delivery(config, {"event_id": "pg-evt-1"}, True, client=object())

        with patch.object(worker, "_process_issue", side_effect=process):
            with ThreadPoolExecutor(max_workers=4) as pool:
                first = pool.submit(deliver, 0)
                self.assertTrue(started.wait(2), "第一个消费者应进入检查")
                rest = [pool.submit(deliver, index) for index in range(1, 4)]
                release.set()
                actions = [first.result(), *(item.result() for item in rest)]
        self.assertEqual(sorted(actions), ["ack"] * 4)
        self.assertEqual(calls, [1], "重投接管后只能有一个消费者执行检查")
        with self.connect() as db:
            row = db.execute("SELECT state, attempts, handoff FROM events").fetchone()
        self.assertEqual((row["state"], row["attempts"]), ("completed", 1))

    def test_redelivery_after_holder_exit_runs_again(self):
        """持有进程已经退出时，handoff 用过的 running 任务仍可由下一次重投接管。"""
        from unittest.mock import patch
        import issue_gov.worker as worker
        self._reset(state="running", attempts=1)
        with self.connect() as db:
            db.execute("UPDATE events SET handoff = 1, owner = 'gone'")
        config = _config(self.url)
        with patch.object(worker, "_process_issue",
                          return_value={"status": "pass", "comment_action": "suppressed"}) as process:
            action = worker.handle_delivery(config, {"event_id": "pg-evt-1"}, True, client=object())
        self.assertEqual(action, "ack")
        process.assert_called_once()
        with self.connect() as db:
            row = db.execute("SELECT state, owner FROM events").fetchone()
        self.assertEqual(row["state"], "completed")
        self.assertIsNone(row["owner"])

    def test_republish_marks_dispatching_only_after_confirm_and_releases_the_rest(self):
        """确认之后才写成 dispatching；中途失败时，还没发出去的租约全部退回。"""
        from dataclasses import replace
        from unittest.mock import patch
        from common.queueing import QueuePublishError
        import issue_gov.worker as worker
        self._reset(state="queued", attempts=0)
        now = time.time()
        with self.connect() as db:
            for number, event_id in ((2, "pg-evt-2"), (3, "pg-evt-3")):
                db.execute("""INSERT INTO events(event_id, repository, issue_number, updated_at,
                                attempts, next_attempt, state, checks)
                              VALUES (?, 'example/repo', ?, ?, 0, 0, 'queued', '[]')""",
                           (event_id, number, now))
        config = replace(_config(self.url), rabbitmq_url="amqp://issue:issue@localhost/%2F")

        def fail_second(*_args, **_kwargs):
            if fail_second.calls:
                raise QueuePublishError("消息队列不可用")
            fail_second.calls += 1

        fail_second.calls = 0
        with patch("common.queueing.publish_analysis_task", side_effect=fail_second):
            published = worker.republish_due(config)
        self.assertEqual(published, 1)
        with self.connect() as db:
            rows = {row["event_id"]: row for row in db.execute(
                "SELECT event_id, state, next_attempt FROM events").fetchall()}
        states = {event_id: row["state"] for event_id, row in rows.items()}
        self.assertIn("dispatching", states.values())
        self.assertEqual(list(states.values()).count("queued"), 2)
        queued = [row for row in rows.values() if row["state"] == "queued"]
        self.assertTrue(all(row["next_attempt"] == 0 for row in queued))


if __name__ == "__main__":
    unittest.main()
