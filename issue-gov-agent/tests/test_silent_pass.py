"""通过检查静默、维护者建议持久化与旧版待发送记录迁移的离线测试。"""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import issue_gov  # 把仓库根目录放进导入路径，后续才能导入 common
import common.atomgit as atomgit
import issue_gov.checks.tools.compliance.assess as compliance_assess
import issue_gov.checks.tools.compliance.recommend as compliance_recommend
import issue_gov.receiver as receiver
import issue_gov.worker as worker
from common.errors import IssueProcessingError

TEMPLATE_FILE = {"filename": "bug.md", "source": "example/repo/.gitcode/ISSUE_TEMPLATE/bug.md",
                 "content": '---\nname: 缺陷反馈\ntitle: "[Bug] "\n---\n### 问题描述\n请填写'}
GAP = {"status": "not_applicable", "template": None,
       "reason": "Issue 请求加入维护团队，现有模板只覆盖缺陷报告。",
       "maintainer": "建议新增维护者申请模板，收集申请账号及负责范围、相关贡献链接。"}
ISSUE = {"title": "申请加入维护团队", "body": "希望维护文档，已有多项贡献。", "state": "open",
         "user": {"login": "contributor"}, "created_at": "2026-11-15T08:00:00+08:00",
         "updated_at": "2026-11-16T09:00:00+08:00"}


class SilentPassTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = receiver.Config("secret", "platform-key", ("example/repo",),
            str(Path(temp.name) / "events.db"), agent_api_key="model-key", llm_model="test-model",
            llm_base_url="https://model.example", maintainers={"example/repo": ("alice", "bob")})
        self.app = receiver.create_app(self.config)
        self.issue = deepcopy(ISSUE)
        self.client = atomgit.AtomGit(self.config.token, request_interval=0)
        self.request = self.enterContext(patch.object(self.client, "request", side_effect=lambda *a, **k: deepcopy(self.issue)))
        self.templates = self.enterContext(patch.object(self.client, "templates", return_value=[deepcopy(TEMPLATE_FILE)]))
        self.publish = self.enterContext(patch.object(self.client, "create_comment", side_effect=AssertionError("Passing checks must not post")))
        self.lookup = self.enterContext(patch.object(self.client, "has_comment", side_effect=AssertionError("No pending comment lookup")))
        self.recommend = self.enterContext(patch.object(compliance_recommend, "recommend_template", return_value=deepcopy(GAP)))
        self.consult = self.enterContext(patch.object(compliance_assess, "consult", side_effect=self.model_review))

    def model_review(self, payload, key, model, base_url):
        if payload.get("current_template"):
            return {"template_fit": "correct", "template": payload["current_template"], "reason": "当前模板适用",
                    "maintainer": None, "alignments": [], "fields": [], "conflicts": []}
        return {"template_fit": "not_applicable", "template": None, "reason": GAP["reason"],
                "maintainer": GAP["maintainer"], "alignments": [], "fields": [], "conflicts": []}
        self.enterContext(patch.object(atomgit, "build_opener", side_effect=AssertionError("No live network")))

    def rows(self, table):
        with receiver.connect(self.config) as db:
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def post(self, identity="event"):
        response = self.app.test_client().post("/webhook", headers={"X-GitCode-Token": "secret"}, json={
            "object_kind": "issue", "uuid": identity, "project": {"path_with_namespace": "example/repo"},
            "object_attributes": {"iid": 12, "action": "update"}})
        self.assertEqual(response.status_code, 202)

    def process(self):
        return worker._process_issue(self.config, "example/repo", 12, client=self.client)

    def test_pass_never_posts_and_is_deduplicated(self):
        self.issue.update(title="[Bug] 错误", body="### 问题描述\n程序退出")
        self.post()
        self.assertTrue(worker.process_one(self.config, client=self.client))
        self.assertEqual(json.loads(self.rows("events")[0]["result"])["comment_action"], "suppressed")
        self.assertEqual(self.rows("maintainer_suggestions"), [])
        self.assertEqual(len(self.rows("issue_checks")), 1)
        self.assertEqual(self.process()["status"], "skipped_unchanged")
        self.templates.assert_called_once()
        self.recommend.assert_not_called()
        self.publish.assert_not_called()

    def test_gap_records_general_suggestion_without_comment(self):
        self.post()
        self.assertTrue(worker.process_one(self.config, client=self.client))
        row = self.rows("maintainer_suggestions")[0]
        event = self.rows("events")[0]
        self.assertEqual(row["id"], json.loads(event["result"])["maintainer_suggestion_id"])
        self.assertEqual(row["repository"], "example/repo")
        self.assertEqual(row["issue_number"], 12)
        self.assertEqual(set(row), {"id", "repository", "issue_number", "created_at", "reason", "suggestion", "mtr"})
        self.assertEqual(datetime.fromisoformat(row["created_at"]).tzinfo, timezone.utc)
        self.assertEqual(row["reason"], GAP["reason"])
        self.assertEqual(row["suggestion"], GAP["maintainer"])
        self.assertEqual(json.loads(row["mtr"]), ["alice", "bob"])
        self.assertNotIn("platform-key", json.dumps(row))
        self.assertNotIn("model-key", json.dumps(row))
        self.publish.assert_not_called()
        self.lookup.assert_not_called()
        self.assertEqual(self.rows("pending_comments"), [])
        self.assertEqual(self.process()["status"], "skipped_unchanged")
        self.consult.assert_called()
        self.recommend.assert_not_called()

    def test_completed_check_is_not_repeated_and_new_checks_keep_suggestions(self):
        first = self.process()["maintainer_suggestion_id"]
        self.assertEqual(self.process()["status"], "skipped_unchanged")
        self.assertEqual(len(self.rows("maintainer_suggestions")), 1)
        self.issue["body"] += " 新的申请内容"
        second = self.process()["maintainer_suggestion_id"]
        self.assertNotEqual(first, second)
        self.issue = deepcopy(ISSUE)
        third = self.process()["maintainer_suggestion_id"]
        self.assertNotIn(third, (first, second))
        self.assertEqual(len(self.rows("maintainer_suggestions")), 3)

    def test_suggestions_for_other_functions_share_the_same_issue(self):
        notes = ["建议完善贡献指南中的权限申请流程。", "建议维护者确认文档链接是否过期。"]
        reasons = ["贡献指南没有说明权限申请流程。", "Issue 提到文档链接无法访问。"]
        with receiver.connect(self.config) as db:
            for reason, note in zip(reasons, notes):
                worker._save_maintainer_suggestion(db, "example/repo", 12,
                    {"reason": reason, "suggestion": note, "mtr": json.dumps(["alice"])})
        rows = self.rows("maintainer_suggestions")
        self.assertEqual([row["reason"] for row in rows], reasons)
        self.assertEqual([row["suggestion"] for row in rows], notes)

    def test_storage_failure_rolls_back_and_queue_retry_saves_once(self):
        self.post()
        save = worker._save_maintainer_suggestion
        def fail_after_insert(*args, **kwargs):
            save(*args, **kwargs)
            raise sqlite3.OperationalError("simulated disk failure")
        with patch.object(worker, "_save_maintainer_suggestion", side_effect=fail_after_insert):
            self.assertTrue(worker.process_one(self.config, now=100, client=self.client))
        self.assertEqual(self.rows("maintainer_suggestions"), [])
        self.assertEqual(self.rows("issue_checks"), [])
        self.assertEqual(self.rows("events")[0]["state"], "queued")
        self.assertTrue(worker.process_one(self.config, now=130, client=self.client))
        self.assertEqual(len(self.rows("maintainer_suggestions")), 1)
        self.assertEqual(len(self.rows("issue_checks")), 1)
        self.assertEqual(self.rows("events")[0]["state"], "completed")
        self.publish.assert_not_called()

    def test_changed_or_closed_issue_does_not_save_stale_suggestion(self):
        for change in ({"body": "新正文"}, {"state": "closed"}):
            with self.subTest(change=change):
                self.request.side_effect = [deepcopy(ISSUE), {**ISSUE, **change}]
                if "body" in change:
                    with self.assertRaises(IssueProcessingError):
                        self.process()
                else:
                    self.assertEqual(self.process()["comment_action"], "skipped_closed")
                self.assertEqual(self.rows("maintainer_suggestions"), [])
                self.assertEqual(self.rows("issue_checks"), [])
        self.publish.assert_not_called()

    def test_legacy_pending_passes_are_reanalyzed_and_never_published(self):
        for summary in ({"status": "pass", "recommendation_status": None},
                        {"status": "no_match", "recommendation_status": "not_applicable"}):
            with self.subTest(summary=summary):
                self.issue = deepcopy(ISSUE)
                if summary["status"] == "pass":
                    self.issue.update(title="[Bug] 错误", body="### 问题描述\n程序退出")
                with receiver.connect(self.config) as db:
                    db.execute("DELETE FROM issue_checks")
                    db.execute("INSERT INTO pending_comments VALUES (?, ?, ?, ?, ?, ?)",
                               ("example/repo", 12, worker.issue_content_hash(self.issue),
                                "old-marker", "旧版通过评论", json.dumps(summary)))
                self.assertEqual(self.process()["comment_action"], "suppressed")
                self.assertEqual(self.rows("pending_comments"), [])
        self.assertEqual(len(self.rows("maintainer_suggestions")), 1)
        self.publish.assert_not_called()
        self.lookup.assert_not_called()

    def test_initialization_adds_table_without_erasing_existing_checks_or_suggestions(self):
        self.process()
        saved = self.rows("maintainer_suggestions")
        checked = self.rows("issue_checks")
        receiver.initialize(self.config)
        self.assertEqual(self.rows("maintainer_suggestions"), saved)
        self.assertEqual(self.rows("issue_checks"), checked)
        with receiver.connect(self.config) as db:
            db.execute("DROP TABLE maintainer_suggestions")
        receiver.initialize(self.config)
        self.assertEqual(self.rows("maintainer_suggestions"), [])
        self.assertEqual(self.rows("issue_checks"), checked)

    def test_missing_maintenance_is_retried_instead_of_silently_lost(self):
        self.consult.side_effect = lambda payload, key, model, base_url: {
            "template_fit": "not_applicable", "template": None, "reason": GAP["reason"],
            "maintainer": None, "alignments": [], "fields": [], "conflicts": []}
        self.post()
        worker.process_one(self.config, now=100, client=self.client)
        self.assertEqual(self.rows("events")[0]["state"], "queued")
        self.assertEqual(self.rows("issue_checks"), [])
        self.assertEqual(self.rows("maintainer_suggestions"), [])
        self.publish.assert_not_called()


if __name__ == "__main__":
    unittest.main()
