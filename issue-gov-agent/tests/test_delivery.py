"""最新内容合并与评论发送恢复：临时数据库、真实处理流程、模拟平台。"""

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import issue_gov  # 把仓库根目录放进导入路径，后续才能导入 common
import common.atomgit as atomgit
import issue_gov.receiver as receiver
import issue_gov.worker as worker
from common.errors import IssueProcessingError


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        """模拟平台评论存储和故障，禁止真实网络及模型调用。"""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.config = receiver.Config('secret', 'token', ('example/repo',), str(Path(temp.name) / 'events.db'),
                                      agent_api_key='model-key', llm_base_url='https://models.example', llm_model='model')
        self.app = receiver.create_app(self.config)
        self.issue = {'title': '最新标题', 'body': '最新正文', 'state': 'open'}
        self.comments = []
        self.posts = []
        self.post_error = None
        self.accept_before_error = False
        self.lookup_error = None
        self.during_post = None
        self.client = atomgit.AtomGit('token', request_interval=0)
        api = patch.object(atomgit.AtomGit, 'request', side_effect=self.request)
        self.api = api.start()
        self.addCleanup(api.stop)
        templates = patch.object(atomgit.AtomGit, 'templates', return_value=[])
        templates.start()
        self.addCleanup(templates.stop)
        analysis = patch.object(worker, 'analyze_issue', return_value={
            'summary': {'status': 'no_match', 'recommendation_status': 'recommended'}, 'comment': '检查结果',
            'publish_comment': True, 'maintainer_suggestion': None})
        self.analysis = analysis.start()
        self.addCleanup(analysis.stop)
        network = patch.object(atomgit, 'build_opener', side_effect=AssertionError('No live network'))
        network.start()
        self.addCleanup(network.stop)

    def request(self, method, path, data=None):
        """平台成功保存与客户端收到响应可分别模拟。"""
        if '/labels' in path:
            return {}
        if method == 'GET' and '/comments?' in path:
            if self.lookup_error:
                raise self.lookup_error
            return deepcopy(self.comments)
        if method == 'GET':
            return deepcopy(self.issue)
        if method == 'PATCH' and '/comments/' in path:
            comment_id = int(path.rsplit('/', 1)[-1])
            matched = next((item for item in self.comments if item['id'] == comment_id), None)
            if matched is None:
                raise IssueProcessingError('评论不存在', status_code=404)
            if not self.post_error or self.accept_before_error:
                matched['body'] = data['body']
            if self.during_post:
                self.during_post()
            if self.post_error:
                raise self.post_error
            return matched
        self.assertEqual(method, 'POST')
        self.posts.append(deepcopy(data))
        if not self.post_error or self.accept_before_error:
            self.comments.append({'id': len(self.comments) + 1, 'body': data['body']})
        if self.during_post:
            self.during_post()
        if self.post_error:
            raise self.post_error
        return {'id': len(self.comments)}

    def post(self, identity, number=12):
        """保留每次投递记录，后台按 Issue 合并。"""
        response = self.app.test_client().post('/webhook', headers={'X-GitCode-Token': 'secret'}, json={
            'object_kind': 'issue', 'uuid': identity, 'project': {'path_with_namespace': 'example/repo'},
            'object_attributes': {'iid': number, 'action': 'update'}})
        self.assertEqual(response.status_code, 202)

    def rows(self, table='events'):
        """读取临时库中的任务或发送意图。"""
        with receiver.connect(self.config) as db:
            return [dict(row) for row in db.execute(f'SELECT * FROM {table}')]

    def process(self, now=100):
        """使用注入时间验证重试，不等待真实时间流逝。"""
        return worker.process_one(self.config, now=now, client=self.client)

    def test_queued_edits_share_one_latest_analysis_and_comment(self):
        for identity in ('first', 'second', 'third'):
            self.post(identity)
        self.assertTrue(self.process())
        self.assertFalse(self.process())
        self.analysis.assert_called_once()
        self.assertEqual(self.analysis.call_args.args[:2], ('最新标题', '最新正文'))
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(len(self.rows()), 3)
        self.assertEqual([row['state'] for row in self.rows()], ['superseded', 'superseded', 'completed'])

    def test_different_issues_are_not_merged(self):
        self.post('one', 12)
        self.post('two', 13)
        self.assertTrue(self.process())
        self.assertTrue(self.process())
        self.assertEqual(self.analysis.call_count, 2)
        self.assertEqual(len(self.posts), 2)

    def test_edit_arriving_during_publication_is_processed_next(self):
        self.post('first')
        def edit():
            self.issue['body'] = '新到达的修改'
            self.post('during-post')
        self.during_post = edit
        self.process()
        self.assertEqual([row['state'] for row in self.rows()], ['superseded', 'queued'])
        self.assertEqual(json.loads(self.rows()[0]['result'])['comment_action'], 'created')
        self.during_post = None
        self.process(101)
        self.assertEqual(self.analysis.call_count, 2)
        self.assertEqual(self.analysis.call_args.args[1], '新到达的修改')
        self.assertEqual(len(self.comments), 1)

    def test_edit_during_analysis_never_publishes_stale_result(self):
        self.post('first')
        result = self.analysis.return_value
        def edit(*args, **kwargs):
            self.issue['body'] = '分析期间的修改'
            self.post('during-analysis')
            return result
        self.analysis.side_effect = edit
        self.process()
        self.assertEqual(self.posts, [])
        self.assertEqual([row['state'] for row in self.rows()], ['superseded', 'queued'])
        self.assertIn('内容已修改', self.rows()[0]['last_error'])
        self.analysis.side_effect = None
        self.assertTrue(self.process(101))
        self.assertFalse(self.process(131))
        self.assertEqual(len(self.comments), 1)
        self.assertEqual([row['state'] for row in self.rows()], ['superseded', 'completed'])

    def test_lost_response_is_reconciled_without_second_post_or_analysis(self):
        self.analysis.return_value['summary'] = {'status': 'additional_check_complete'}
        self.post('first')
        self.post_error = IssueProcessingError('响应丢失')
        self.accept_before_error = True
        self.process()
        self.assertEqual(self.rows()[0]['state'], 'queued')
        self.assertEqual(len(self.rows('pending_comments')), 1)
        self.post_error = None
        # 新客户端模拟进程重启，恢复只依赖持久化记录和平台评论。
        self.client = atomgit.AtomGit('token', request_interval=0)
        receiver.initialize(self.config)
        self.process(130)
        self.analysis.assert_called_once()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.rows('pending_comments'), [])
        self.assertEqual(json.loads(self.rows()[0]['result'])['comment_action'], 'recovered')

    def test_legacy_pending_comment_is_reanalyzed_without_duplicate_publication(self):
        self.post('first')
        self.post_error = IssueProcessingError('响应丢失')
        self.accept_before_error = True
        self.process()
        with receiver.connect(self.config) as db:
            summary = json.loads(self.rows('pending_comments')[0]['summary'])
            del summary['publish_comment']
            db.execute('UPDATE pending_comments SET summary=?', (json.dumps(summary),))
        self.post_error = None
        self.process(130)
        self.assertEqual(self.analysis.call_count, 2)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.rows('pending_comments'), [])
        self.assertEqual(json.loads(self.rows()[0]['result'])['comment_action'], 'recovered')

    def test_crash_after_remote_acceptance_recovers_running_batch(self):
        self.post('first')
        self.post('second')
        self.post_error = SystemExit(0)
        self.accept_before_error = True
        with self.assertRaises(SystemExit):
            self.process()
        self.assertEqual([row['state'] for row in self.rows()], ['superseded', 'running'])
        self.post_error = None
        self.process(101)
        self.assertEqual(len(self.posts), 1)
        self.analysis.assert_called_once()
        self.assertEqual([row['state'] for row in self.rows()], ['superseded', 'completed'])
        self.assertEqual(self.rows()[-1]['attempts'], 1)

    def test_unsent_comment_reuses_persisted_body_and_marker(self):
        self.post('first')
        self.post_error = IssueProcessingError('请求未送达')
        self.process()
        saved = self.rows('pending_comments')[0]
        self.assertIs(json.loads(saved['summary'])['publish_comment'], True)
        self.assertEqual(self.comments, [])
        self.post_error = None
        self.process(130)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.posts[0], self.posts[1])
        self.assertEqual(self.posts[1]['body'], saved['body'])
        self.analysis.assert_called_once()

    def test_lookup_failure_never_causes_a_blind_resend(self):
        self.post('first')
        self.post_error = IssueProcessingError('响应丢失')
        self.accept_before_error = True
        self.process()
        self.post_error = None
        self.lookup_error = IssueProcessingError('查询失败')
        self.process(130)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.rows()[0]['state'], 'queued')
        self.lookup_error = None
        self.process(190)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.rows()[0]['state'], 'completed')

    def test_new_content_does_not_reuse_old_pending_result(self):
        self.post('first')
        self.post_error = IssueProcessingError('请求未送达')
        self.process()
        self.issue['body'] = '已经修改的正文'
        self.post('second')
        self.post_error = None
        self.assertFalse(self.process(100 + worker.RETRY_DELAY - 1))
        self.process(100 + worker.RETRY_DELAY)
        self.assertEqual(self.analysis.call_count, 2)
        self.assertEqual(len(self.comments), 1)
        self.assertNotEqual(self.posts[0], self.posts[1])
        self.assertEqual([row['state'] for row in self.rows()], ['superseded', 'completed'])

    def test_reverting_after_uncertain_publication_still_checks_latest_content(self):
        self.post('version-a')
        self.process()
        self.issue['body'] = '版本 B'
        self.post('version-b')
        self.post_error = IssueProcessingError('响应丢失')
        self.accept_before_error = True
        self.process(101)
        self.issue['body'] = '最新正文'
        self.post('back-to-a')
        self.post_error = None
        self.process(131)
        self.assertEqual(self.analysis.call_count, 3)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.analysis.call_args.args[1], '最新正文')
        self.assertEqual(self.rows('pending_comments'), [])
        self.assertEqual([row['state'] for row in self.rows()], ['completed', 'superseded', 'completed'])

    def test_failed_delivery_waits_for_manual_retry(self):
        self.post('version-a')
        self.process()
        self.issue['body'] = '版本 B'
        self.post('version-b')
        self.post_error = IssueProcessingError('发送结果不确定且停止自动重试', retryable=False)
        self.accept_before_error = True
        self.process(101)
        self.issue['body'] = '最新正文'
        self.post_error = None
        # 没有新通知时，终止失败状态保持不变，等待人工重试。
        self.assertFalse(self.process(200))
        self.assertEqual(worker.retry_failed(self.config), 1)
        self.assertTrue(self.process(200))
        self.assertEqual(self.analysis.call_count, 3)
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.rows('pending_comments'), [])

    def test_database_failure_after_publication_recovers_without_duplicate(self):
        self.post('first')
        real_connect = worker.connect

        class FailingConnection:
            def __init__(self, db):
                self.db = db

            def execute(self, sql, *args):
                if 'INSERT INTO issue_checks' in sql:
                    raise sqlite3.OperationalError('模拟保存完成记录失败')
                return self.db.execute(sql, *args)

        @contextmanager
        def failing_connect(config):
            with real_connect(config) as db:
                yield FailingConnection(db)

        with patch.object(worker, 'connect', failing_connect):
            self.process()
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.rows('issue_checks'), [])
        self.assertEqual(len(self.rows('pending_comments')), 1)
        self.process(130)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.rows()[0]['state'], 'completed')
        self.assertEqual(self.rows('pending_comments'), [])

    def test_merged_failures_share_retry_budget_and_wait(self):
        self.post('first')
        self.post('second')
        self.analysis.side_effect = IssueProcessingError('模型暂时不可用')
        self.process()
        self.post('late')
        self.assertFalse(self.process(100 + worker.RETRY_DELAY - 1))
        for now in (100 + worker.RETRY_DELAY, 190, 310, 550):
            self.assertTrue(self.process(now))
        self.assertFalse(self.process(1000))
        self.assertEqual(self.analysis.call_count, 5)
        self.assertEqual([row['state'] for row in self.rows()], ['superseded', 'superseded', 'failed'])
        self.assertEqual(self.rows()[-1]['attempts'], 5)


    def test_failed_issue_is_superseded_by_new_edit_and_old_notifications_stay_duplicate(self):
        """失败后新修改保留并覆盖旧任务，旧通知重投不再触发检查。

        Args:
            无。
        Returns:
            None；验证记录数量、状态、重试预算、历史通知去重及评论数量。
        """
        self.post('first')
        self.post_error = IssueProcessingError('无权限', retryable=False)
        self.process()
        self.assertEqual(self.rows()[0]['state'], 'failed')
        self.issue['body'] = '修复权限后修改正文'
        self.post('second')
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[0]['last_error'], '无权限')
        self.assertEqual(self.rows()[0]['state'], 'superseded')
        self.assertEqual(self.rows()[-1]['attempts'], 0)
        self.post_error = None
        self.process(101)
        before = self.rows()
        response = self.app.test_client().post('/webhook', headers={'X-GitCode-Token': 'secret'}, json={
            'object_kind': 'issue', 'uuid': 'first', 'project': {'path_with_namespace': 'example/repo'},
            'object_attributes': {'iid': 12, 'action': 'update'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['state'], 'superseded')
        self.assertEqual(self.rows(), before)
        self.assertEqual(worker.retry_failed(self.config), 0)
        self.assertFalse(self.process(102))
        self.assertEqual(len(self.comments), 1)

    def test_completed_history_is_preserved_while_unfinished_task_is_superseded(self):
        """多次成功保留历史记录，失败后的新修改只标记旧任务已覆盖。

        Args:
            无。
        Returns:
            None；验证历史记录不被覆盖、未完成任务唯一及旧通知不影响新任务。
        """
        for version in ('a', 'b'):
            self.issue['body'] = version
            self.post(version)
            self.process()
        history = self.rows()
        self.assertEqual(len(history), 2)
        self.issue['body'] = 'c'
        self.post('c')
        self.post_error = IssueProcessingError('无权限', retryable=False)
        self.process()
        self.assertEqual([r['state'] for r in self.rows()], ['completed', 'completed', 'failed'])
        self.issue['body'] = 'd'
        self.post('d')
        self.assertEqual(self.rows()[:2], history)
        self.assertEqual([r['state'] for r in self.rows()], ['completed', 'completed', 'superseded', 'queued'])
        before = self.rows()
        response = self.app.test_client().post('/webhook', headers={'X-GitCode-Token': 'secret'}, json={
            'object_kind': 'issue', 'uuid': 'a', 'project': {'path_with_namespace': 'example/repo'},
            'object_attributes': {'iid': 12, 'action': 'update'}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['state'], 'completed')
        self.assertEqual(self.rows(), before)
        self.post_error = None
        self.process(101)
        self.assertEqual(self.rows()[:2], history)
        self.assertEqual([r['state'] for r in self.rows()], ['completed', 'completed', 'superseded', 'completed'])
        self.assertEqual(len(self.comments), 1)

    def test_old_failure_cannot_fail_new_edit_and_global_cooldown_is_preserved(self):
        """旧检查报错时，新修改仍待处理；限流等待仍由全局记录约束。

        Args:
            无。
        Returns:
            None；验证新任务不继承旧失败，且不能绕过 429 等待。
        """
        self.post('first')
        result = self.analysis.return_value
        def edit_and_fail(*args, **kwargs):
            """模拟分析期间的新编辑和随后发生的限流。

            Args:
                args: 分析入口的位置参数。
                kwargs: 分析入口的命名参数。
            Returns:
                不返回；抛出可重试的限流错误。
            """
            self.issue['body'] = '较新的正文'
            self.post('second')
            raise IssueProcessingError('限流', status_code=429, retry_after=60)
        self.analysis.side_effect = edit_and_fail
        self.process(100)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual((self.rows()[-1]['state'], self.rows()[-1]['attempts']), ('queued', 0))
        self.analysis.side_effect = None
        self.analysis.return_value = result
        self.assertFalse(self.process(159))
        self.assertTrue(self.process(160))
        self.assertEqual(self.rows()[-1]['state'], 'completed')
        self.assertEqual(len(self.comments), 1)

    def test_new_edit_survives_crash_after_old_comment_was_published(self):
        """旧版本发布后进程退出，也不能丢失已收到的新编辑。

        Args:
            无。
        Returns:
            None；验证重启后处理最新版本，并在没有后续评论时修改上一条评估评论。
        """
        self.post('first')
        def edit():
            """在旧评论发布期间接收新编辑。

            Args:
                无。
            Returns:
                None；修改模拟 Issue 并投递通知。
            """
            self.issue['body'] = '进程退出前收到的新正文'
            self.post('second')
        self.during_post = edit
        self.post_error = SystemExit(0)
        self.accept_before_error = True
        with self.assertRaises(SystemExit):
            self.process(100)
        self.assertEqual(self.rows()[-1]['state'], 'queued')
        receiver.initialize(self.config)
        self.during_post = None
        self.post_error = None
        self.process(101)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.rows()[-1]['state'], 'completed')
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(self.analysis.call_args.args[1], self.issue['body'])


class CommentLookupTests(unittest.TestCase):
    def test_pagination_and_exact_marker_line(self):
        client = atomgit.AtomGit('token', request_interval=0)
        marker = '<!-- issue-gov:unique -->'
        first = [{'body': '普通评论 ' + marker} for _ in range(100)]
        with patch.object(client, 'request', side_effect=[first, [{'body': '检查结果\n\n' + marker}]]) as api:
            self.assertTrue(client.has_comment('example/repo', 12, marker))
        self.assertEqual(api.call_count, 2)
        self.assertIn('page=2&per_page=100&order=desc', api.call_args.args[1])
        with patch.object(client, 'request', return_value=[]):
            self.assertFalse(client.has_comment('example/repo', 12, marker))

    def test_invalid_pages_and_query_errors_do_not_mean_absence(self):
        client = atomgit.AtomGit('token', request_interval=0)
        for value in ({}, [None], [{'body': None}], [{'id': 12}]):
            with self.subTest(value=value), patch.object(client, 'request', return_value=value):
                with self.assertRaises(IssueProcessingError):
                    client.has_comment('example/repo', 12, 'marker')
        with patch.object(client, 'request', side_effect=IssueProcessingError('限流', status_code=429)):
            with self.assertRaises(IssueProcessingError):
                client.has_comment('example/repo', 12, 'marker')
