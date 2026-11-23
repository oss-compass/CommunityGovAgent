"""后台启动和已入队任务处理测试；不访问真实平台。"""

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import receiver
import worker


class WorkerStartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.config = receiver.Config("secret", "token", ("example/repo",),
                                      str(Path(temporary.name) / "events.db"))
        receiver.initialize(self.config)
        self.client = Mock()
        self.client.request.side_effect = AssertionError("No platform calls without a Webhook")

    def rows(self):
        with receiver.connect(self.config) as db:
            return [dict(r) for r in db.execute("SELECT * FROM events ORDER BY rowid")]

    def run_until_idle(self, config=None):
        with patch.object(worker.signal, 'signal'), patch.object(worker, 'AtomGit', return_value=self.client), \
                patch.object(worker.time, 'sleep', side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit):
                worker.run_worker(config or self.config)

    def test_start_restart_and_readding_repository_do_not_scan(self):
        for repositories in (('example/repo',), ('example/other',), ('example/repo',)):
            self.run_until_idle(replace(self.config, repositories=repositories))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.client.method_calls, [])

    def test_normal_failed_and_interrupted_webhook_tasks_remain_recoverable(self):
        with receiver.connect(self.config) as db:
            for number, state in ((1, 'running'), (2, 'failed')):
                db.execute("""INSERT INTO events(event_id, repository, issue_number, state, attempts, updated_at)
                              VALUES (?, 'example/repo', ?, ?, 1, 100)""", (f'webhook-{number}', number, state))
        receiver.initialize(self.config)
        with patch.object(worker, '_process_issue', return_value={'status': 'pass', 'comment_action': 'suppressed'}):
            self.assertTrue(worker.process_one(self.config, client=self.client))
            self.assertEqual(worker.retry_failed(self.config), 1)
            self.assertTrue(worker.process_one(self.config, client=self.client))
        self.assertEqual([r['state'] for r in self.rows()], ['completed', 'completed'])


if __name__ == '__main__':
    unittest.main()
