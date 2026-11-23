"""AtomGit API 契约测试，禁止真实网络请求。"""
import base64
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import atomgit
from errors import IssueProcessingError

TEMPLATE = {"name": "缺陷反馈", "title": "[Bug] ", "body": [
    {"type": "textarea", "attributes": {"label": "问题描述"}, "validations": {"required": True}}]}


def encoded(template):
    """将测试模板编码为平台文件接口返回的 Base64 数据。

    Args:
        template: 用于测试的 YAML 模板字典。
    Returns:
        包含 encoding 和 content 字段的模拟文件响应字典。
    """
    return {"encoding": "base64", "content": base64.b64encode(yaml.safe_dump(template).encode()).decode()}


def template_file(template, repository="example/repo", filename="bug.yml"):
    """构造模板下载接口应返回的原始文件记录。

    Args:
        template: 用于编码成 YAML 原文的测试对象。
        repository: 实际提供文件的仓库。
        filename: 模板文件名。
    Returns:
        包含文件名、来源路径和原文的字典。
    """
    return {"filename": filename, "source": repository + "/.gitcode/ISSUE_TEMPLATE/" + filename,
            "content": yaml.safe_dump(template)}


class AtomGitTests(unittest.TestCase):
    def setUp(self):
        """为每个接口测试创建客户端并禁止真实网络访问，注册自动清理。

        Args:
            无。
        Returns:
            None；测试环境保存到实例属性。
        """
        self.client = atomgit.AtomGit("private-token", request_interval=0)
        guard = patch.object(atomgit, "build_opener", side_effect=AssertionError("No live requests"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_bearer_auth_json_and_empty_patch_response(self):
        """验证平台请求使用 Bearer 认证、JSON 编码，并接受空的成功响应。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        for method, data, content in (("GET", None, b'{}'), ("POST", {"body": "中文"}, b'{}'),
                                      ("PATCH", {"body": "更新"}, b'')):
            response = io.BytesIO(content)
            response.status = 200
            opener = Mock()
            opener.open.return_value = response
            with patch.object(atomgit, "build_opener", return_value=opener):
                self.assertEqual(self.client.request(method, "/test", data), {})
            req = opener.open.call_args.args[0]
            self.assertEqual(req.get_header("Authorization"), "Bearer private-token")
            self.assertNotIn("private-token", req.full_url)
            self.assertEqual(req.get_method(), method)
            self.assertEqual(json.loads(req.data) if req.data else None, data)

    def test_errors_are_classified_and_redacted(self):
        """验证平台错误的重试分类、敏感信息隐藏和禁止重定向行为。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        for code in (302, 401, 403, 404, 408, 429, 500):
            opener = Mock()
            opener.open.side_effect = HTTPError("https://example/?private-token", code,
                                               "private-token", {}, io.BytesIO(b'private-token'))
            with patch.object(atomgit, "build_opener", return_value=opener), self.assertRaises(IssueProcessingError) as error:
                self.client.request("GET", "/test")
            self.assertNotIn("private-token", str(error.exception))
            self.assertEqual(error.exception.retryable, code in (408, 429, 500))
        opener.open.side_effect = URLError("private-token")
        with patch.object(atomgit, "build_opener", return_value=opener), self.assertRaises(IssueProcessingError) as error:
            self.client.request("GET", "/test")
        self.assertNotIn("private-token", str(error.exception))
        self.assertIsNone(atomgit.NoRedirect().redirect_request(None, None, 302, "", {}, "https://outside"))

    def test_unnamed_markdown_textarea_does_not_block_other_templates(self):
        """仓库读取接受真实空 label 模板，并继续加载同目录中的其他模板。

        Args:
            无。
        Returns:
            None；核对所有模板成功返回且原始 Markdown 得以保留。
        """
        path = Path(__file__).with_name('fixtures') / 'markdown_textarea.yml'
        embedded = yaml.safe_load(path.read_text())
        entries = [{'type': 'file', 'name': 'embedded.yml'}, {'type': 'file', 'name': 'bug.yml'}]
        with patch.object(self.client, 'request', side_effect=[
            {'default_branch': 'master'}, entries, encoded(embedded), encoded(TEMPLATE),
        ]) as api:
            templates = self.client.templates('example/repo')
        self.assertEqual(templates, [template_file(embedded, filename='embedded.yml'), template_file(TEMPLATE)])
        self.assertEqual(api.call_count, 4)

    def test_each_repository_uses_its_own_default_branch(self):
        """验证各仓库使用自己的默认分支读取模板，且不使用返回的下载链接。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        entries = [{"type": "file", "name": "bug.yml", "download_url": "https://untrusted"},
                   {"type": "file", "name": "config.yml"}, {"type": "file", "name": "README.txt"}]
        for repo, branch in (("example/one", "main"), ("example/two", "release/stable")):
            with patch.object(self.client, "request", side_effect=[{"default_branch": branch}, entries, encoded(TEMPLATE)]) as api:
                self.assertEqual(self.client.templates(repo), [template_file(TEMPLATE, repo)])
            for call in api.call_args_list[1:]:
                path = call.args[1]
                self.assertTrue(path.startswith(f"/repos/{repo}/contents/"))
                self.assertEqual(parse_qs(urlsplit(path).query), {"ref": [branch]})
                self.assertNotIn("untrusted", path)

    def test_template_contents_are_not_parsed_and_missing_templates_return_empty(self):
        """下载阶段不解析模板内容；模板缺失时返回空列表交给分析阶段。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        invalid = [None, {"name": " "}, {"name": "bad", "title": 123}]
        for template in invalid:
            self.client = atomgit.AtomGit("private-token", request_interval=0)
            with patch.object(self.client, "request", side_effect=[{"default_branch": "main"},
                     [{"type": "file", "name": "bug.yml"}], encoded(template)]):
                self.assertEqual(self.client.templates("example/repo"), [template_file(template)])
        self.client = atomgit.AtomGit("private-token", request_interval=0)
        with patch.object(self.client, "request", side_effect=[{"default_branch": "main", "namespace": {"type": "personal"}}, *[[] for _ in range(6)]]):
            self.assertEqual(self.client.templates("example/repo"), [])

    def test_each_check_creates_a_comment_even_with_identical_content(self):
        """每次发布均新增评论，不查询账号、查找旧评论或跳过相同内容。"""
        body = "### Issue 模板检查\n\n基础格式检查通过。"
        with patch.object(self.client, "request", return_value={}) as api:
            for _ in range(2):
                self.assertEqual(self.client.create_comment("example/repo", 12, body), "created")
        self.assertEqual(api.call_args_list, [
            call("POST", "/repos/example/repo/issues/12/comments", {"body": body}),
        ] * 2)

    def test_comment_creation_failure_propagates(self):
        with patch.object(self.client, "request", side_effect=IssueProcessingError("write failed")) as api:
            with self.assertRaises(IssueProcessingError):
                self.client.create_comment("example/repo", 12, "检查结果")
        api.assert_called_once_with("POST", "/repos/example/repo/issues/12/comments", {"body": "检查结果"})

    def test_invalid_and_oversized_responses_are_rejected(self):
        """验证损坏 JSON、错误响应类型及超限响应被拒绝，且不会泄漏响应正文。

        Args:
            无。
        Returns:
            None；返回格式或大小限制失效时测试失败。
        """
        for content, retryable in ((b'private-token', True), (b'\xff', True),
                                   (b'null', True), (b'123', True),
                                   (b'x' * (atomgit.MAX_RESPONSE_BYTES + 1), False)):
            with self.subTest(size=len(content), retryable=retryable):
                response = io.BytesIO(content)
                response.status = 200
                opener = Mock()
                opener.open.return_value = response
                with patch.object(atomgit, "build_opener", return_value=opener), self.assertRaises(IssueProcessingError) as error:
                    self.client.request("GET", "/test")
                self.assertEqual(error.exception.retryable, retryable)
                self.assertNotIn("private-token", str(error.exception))
                self.assertTrue(response.closed)

    def test_invalid_template_encoding_is_rejected(self):
        """验证错误编码、损坏 Base64 和非 UTF-8 内容在下载阶段被拒绝。

        Args:
            无。
        Returns:
            None；模板内容错误未转换为不可重试异常时测试失败。
        """
        for item in ([], {"encoding": "text", "content": "plain"},
                     {"encoding": "base64", "content": "!!!!"},
                     {"encoding": "base64", "content": base64.b64encode(b'\xff').decode()}):
            with self.subTest(item=item), patch.object(self.client, "request", side_effect=[
                {"default_branch": "main"}, [{"type": "file", "name": "bug.yml"}], item,
            ]), self.assertRaises(IssueProcessingError) as error:
                self.client.templates("example/repo")
            self.assertFalse(error.exception.retryable)

    def test_template_paths_and_count_are_bounded(self):
        """验证目录穿越文件名被拒绝，并允许 50 个模板而拒绝第 51 个下载。

        Args:
            无。
        Returns:
            None；非法路径被请求或模板数量限制失效时测试失败。
        """
        for name in ("../outside.yml", "sub/bug.yaml", "sub\\bug.yml"):
            with self.subTest(name=name), patch.object(self.client, "request", side_effect=[
                {"default_branch": "main"}, [{"type": "file", "name": name}],
            ]) as api, self.assertRaises(IssueProcessingError) as error:
                self.client.templates("example/repo")
            self.assertFalse(error.exception.retryable)
            self.assertEqual(api.call_count, 2)
        for count in (50, 51):
            self.client = atomgit.AtomGit("private-token", request_interval=0)
            entries = [{"type": "file", "name": f"template-{i}.yml"} for i in range(count)]
            with self.subTest(count=count), patch.object(self.client, "request", side_effect=[
                {"default_branch": "main"}, entries, *[encoded(TEMPLATE) for _ in range(50)],
            ]) as api:
                if count == 50:
                    self.assertEqual(len(self.client.templates("example/repo")), 50)
                else:
                    with self.assertRaises(IssueProcessingError) as error:
                        self.client.templates("example/repo")
                    self.assertFalse(error.exception.retryable)
                self.assertEqual(api.call_count, 52)

    def test_rate_limit_preserves_retry_after_without_response_secrets(self):
        """验证限流等待时间解析正确，异常不泄露响应中的敏感信息。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        for header, expected in (("120", 120), (None, 30), ("invalid", 30),
                                 ("0", 30), ("-5", 30), ("inf", 30),
                                 ("Thu, 01 Jan 1970 00:20:00 GMT", 200)):
            with self.subTest(header=header):
                opener = Mock()
                opener.open.side_effect = HTTPError("https://example/private-token", 429,
                    "private-token", {"Retry-After": header}, io.BytesIO(b'private-token'))
                with patch.object(atomgit, "build_opener", return_value=opener), \
                        patch.object(atomgit.time, "time", return_value=1000), \
                        self.assertRaises(IssueProcessingError) as error:
                    self.client.request("GET", "/test")
                self.assertEqual(error.exception.status_code, 429)
                self.assertEqual(error.exception.retry_after, expected)
                self.assertNotIn("private-token", str(error.exception))

    def test_requests_are_spaced_even_after_a_failed_request(self):
        """验证请求失败后仍遵守最小请求间隔。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        client = atomgit.AtomGit("private-token")
        opener = Mock()
        opener.open.side_effect = URLError("offline")
        with patch.object(atomgit, "build_opener", return_value=opener), \
                patch.object(atomgit.time, "monotonic", side_effect=[100, 100.25, 101]), \
                patch.object(atomgit.time, "sleep") as sleep:
            for _ in range(2):
                with self.assertRaises(IssueProcessingError):
                    client.request("GET", "/test")
        sleep.assert_called_once_with(0.75)

    def test_templates_cache_is_per_repository_expires_and_copies_values(self):
        """验证模板缓存按仓库隔离、到期刷新且返回副本。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        def responses(template):
            """构造一次模板读取的模拟响应。

            Args:
                template: 待编码的模板字典。
            Returns:
                默认分支、目录列表和模板文件响应组成的列表。
            """
            return [{"default_branch": "main"}, [{"type": "file", "name": "bug.yml"}], encoded(template)]
        changed = {**TEMPLATE, "title": "[Changed] "}
        with patch.object(atomgit.time, "monotonic", return_value=100) as clock, \
                patch.object(self.client, "request", side_effect=[
                    *responses(TEMPLATE), *responses(changed), *responses(changed)]) as api:
            initial = self.client.templates("example/one")
            initial[0]["content"] = "mutated"
            self.assertEqual(self.client.templates("example/one"), [template_file(TEMPLATE, "example/one")])
            self.assertEqual(api.call_count, 3)
            self.assertEqual(self.client.templates("example/two"), [template_file(changed, "example/two")])
            self.assertEqual(api.call_count, 6)
            clock.return_value = 100 + atomgit.TEMPLATE_CACHE_SECONDS
            self.assertEqual(self.client.templates("example/one"), [template_file(changed, "example/one")])
            self.assertEqual(api.call_count, 9)

    def test_invalid_file_response_reports_filename_and_is_not_cached(self):
        """验证文件响应无法解码时报告文件名且不写入缓存。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        invalid = {"encoding": "base64", "content": "!!!!"}
        responses = [{"default_branch": "main"}, [{"type": "file", "name": "broken.yml"}]]
        with patch.object(self.client, "request", side_effect=[
                *responses, invalid, *responses, encoded(TEMPLATE)]) as api:
            with self.assertRaises(IssueProcessingError) as error:
                self.client.templates("example/repo")
            self.assertIn("broken.yml", str(error.exception))
            self.assertFalse(error.exception.retryable)
            self.assertEqual(self.client.templates("example/repo"), [template_file(TEMPLATE, filename="broken.yml")])
            self.assertEqual(api.call_count, 6)


def encoded_text(text):
    """将模板原文编码为模拟的文件响应。

    Args:
        text: YAML 或 Markdown 模板原文。
    Returns:
        包含 encoding 和 Base64 content 的字典。
    """
    return {'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}


class TemplateDiscoveryTests(unittest.TestCase):
    def setUp(self):
        """创建模板读取测试客户端，并禁止真实网络访问。

        Args:
            无。
        Returns:
            None；客户端保存在实例中，网络拦截在测试结束后清理。
        """
        self.client = atomgit.AtomGit('test-token', request_interval=0)
        guard = patch.object(atomgit, 'build_opener', side_effect=AssertionError('No live API'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_mixed_yaml_markdown_and_config_files(self):
        """验证 YAML 与 Markdown 混合读取，并跳过配置文件。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        entries = [{'type': 'file', 'name': name} for name in ('bug.YAML', 'question.md', 'config.yml', 'config.yaml')]
        with patch.object(self.client, 'request', side_effect=[{'default_branch': 'main'}, entries,
                encoded_text('name: 缺陷\nbody: []'), encoded_text('### 描述')]) as api:
            templates = self.client.templates('example/repo')
        self.assertEqual([item['filename'] for item in templates], ['bug.YAML', 'question.md'])
        self.assertEqual([item['content'] for item in templates], ['name: 缺陷\nbody: []', '### 描述'])
        self.assertEqual(api.call_count, 4)

    def test_legacy_locations_and_github_fallback(self):
        """验证兼容模板目录和单文件模板的查找顺序。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        for target in ('.gitcode', '.github/ISSUE_TEMPLATE', '.github', 'docs', ''):
            with self.subTest(target=target):
                client = atomgit.AtomGit('test-token', request_interval=0)
                file = 'bug.md' if target.endswith('ISSUE_TEMPLATE') else 'ISSUE_TEMPLATE.md'
                full = target + '/' + file if target else file
                def request(method, path, data=None):
                    """模拟模板路径查找的 API 响应。

                    Args:
                        method: 请求方法。
                        path: 含默认分支参数的 API 路径。
                        data: 可选请求体，本测试不使用。
                    Returns:
                        仓库信息、目标目录列表或模板文件响应。
                    Raises:
                        IssueProcessingError: 非目标模板路径返回 404。
                    """
                    if path == '/repos/example/repo':
                        return {'default_branch': 'release/stable'}
                    self.assertIn('ref=release%2Fstable', path)
                    if path.split('?')[0] == '/repos/example/repo/contents/' + full:
                        return encoded_text('### 描述')
                    if path.split('?')[0] == '/repos/example/repo/contents/' + target:
                        return [{'type': 'file', 'name': file}]
                    raise IssueProcessingError('HTTP 404', retryable=False, status_code=404)
                with patch.object(client, 'request', side_effect=request):
                    self.assertEqual(client.templates('example/repo'), [
                        {'filename': file, 'source': 'example/repo/' + full, 'content': '### 描述'}])

    def test_permission_and_rate_limit_errors_do_not_fall_through_locations(self):
        """验证权限、限流及服务端错误不会被当作模板路径缺失。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        for code in (401, 403, 429, 500):
            with self.subTest(code=code), patch.object(self.client, 'request', side_effect=[
                    {'default_branch': 'main'}, IssueProcessingError('HTTP error', status_code=code)]) as api:
                with self.assertRaises(IssueProcessingError) as error:
                    self.client.templates('example/repo')
                self.assertEqual(error.exception.status_code, code)
                self.assertEqual(api.call_count, 2)

    def test_templates_expire_at_sixty_seconds_and_refresh_failure_never_uses_stale_data(self):
        """验证模板恰在 60 秒时到期，刷新失败不使用旧缓存。

        Args:
            无。
        Returns:
            None；通过断言核对预期结果。
        """
        entries = [{'type': 'file', 'name': 'bug.yml'}]
        with patch.object(atomgit.time, 'monotonic', return_value=100) as clock, patch.object(
                self.client, 'request', side_effect=[{'default_branch': 'main'}, entries, encoded_text('name: old\nbody: []'),
                    IssueProcessingError('HTTP 429', status_code=429),
                    {'default_branch': 'main'}, entries, encoded_text('name: new\nbody: []')]) as api:
            self.assertEqual(self.client.templates('example/repo')[0]['content'], 'name: old\nbody: []')
            clock.return_value = 159
            self.assertEqual(self.client.templates('example/repo')[0]['content'], 'name: old\nbody: []')
            self.assertEqual(api.call_count, 3)
            clock.return_value = 160
            with self.assertRaises(IssueProcessingError):
                self.client.templates('example/repo')
            self.assertEqual(self.client.templates('example/repo')[0]['content'], 'name: new\nbody: []')


class SharedTemplateTests(unittest.TestCase):
    def test_shared_templates_use_organization_branch_and_target_cache(self):
        """本地无模板时读取组织公共模板，使用公共仓库分支并按目标仓库缓存。

        Args:
            无。
        Returns:
            None；验证不同分支、404 路径、缓存副本及过期后本地模板优先。
        """
        client = atomgit.AtomGit('test-token', request_interval=0)
        missing = IssueProcessingError('missing', status_code=404)
        entries = [{'type': 'file', 'name': 'bug.yml'}]
        local = {**TEMPLATE, 'name': '本地模板'}
        with patch.object(atomgit.time, 'monotonic', return_value=100) as clock, patch.object(
            client, 'request', side_effect=[
                {'default_branch': 'release/local', 'namespace': {'type': 'group'}},
                missing, *[[] for _ in range(5)], {'default_branch': 'release/shared'}, entries, encoded(TEMPLATE),
                {'default_branch': 'main'}, entries, encoded(local),
            ]) as api:
            result = client.templates('example/repo')
            self.assertEqual(result, [template_file(TEMPLATE, "example/.gitcode")])
            result[0]['content'] = '改动'
            self.assertEqual(client.templates('example/repo'), [template_file(TEMPLATE, 'example/.gitcode')])
            self.assertEqual(api.call_count, 10)
            self.assertIn(call('GET', '/repos/example/.gitcode/contents/.gitcode/ISSUE_TEMPLATE?ref=release%2Fshared'),
                          api.call_args_list)
            self.assertTrue(all(c.args[0] == 'GET' for c in api.call_args_list))
            clock.return_value = 160
            self.assertEqual(client.templates('example/repo'), [template_file(local)])
            self.assertEqual(api.call_count, 13)

    def test_unknown_namespace_checks_organization_before_fallback(self):
        """接口未带命名空间时先确认组织身份，再读取公共模板。

        Args:
            无。
        Returns:
            None；组织接口返回 404 时停止，确认存在时读取公共仓库。
        """
        for exists in (False, True):
            client = atomgit.AtomGit('test-token', request_interval=0)
            responses = [{'default_branch': 'main'}, *[[] for _ in range(6)]]
            if exists:
                responses += [{'login': 'example'}, {'default_branch': 'master'},
                              [{'type': 'file', 'name': 'bug.yml'}], encoded(TEMPLATE)]
            else:
                responses += [IssueProcessingError('missing org', status_code=404)]
            with patch.object(client, 'request', side_effect=responses) as api:
                if exists:
                    self.assertEqual(client.templates('example/repo'), [template_file(TEMPLATE, 'example/.gitcode')])
                else:
                    self.assertEqual(client.templates('example/repo'), [])
                self.assertEqual(api.call_args_list[7], call('GET', '/orgs/example'))
                self.assertEqual(api.call_count, 11 if exists else 8)

    def test_personal_and_shared_repositories_do_not_recurse(self):
        """个人仓库不继承组织模板，公共仓库自身不循环回退。

        Args:
            无。
        Returns:
            None；两种情况均只查一次仓库及其六个模板位置。
        """
        for repo, kind in (('example/repo', 'personal'), ('example/.gitcode', 'group')):
            client = atomgit.AtomGit('test-token', request_interval=0)
            with patch.object(client, 'request', side_effect=[
                    {'default_branch': 'main', 'namespace': {'type': kind}}, *[[] for _ in range(6)]]) as api:
                self.assertEqual(client.templates(repo), [])
                self.assertEqual(api.call_count, 7)

    def test_empty_shared_templates_are_not_cached(self):
        """本地和公共仓库均无模板时返回空列表，新增模板后下一次读取可发现。

        Args:
            无。
        Returns:
            None；验证空公共模板不会触发缓存错误或遮蔽后续新增文件。
        """
        client = atomgit.AtomGit('test-token', request_interval=0)
        missing = [{'default_branch': 'main', 'namespace': {'type': 'group'}}, *[[] for _ in range(6)],
                   {'default_branch': 'master'}, *[[] for _ in range(6)]]
        added = [{'default_branch': 'main'}, [{'type': 'file', 'name': 'bug.yml'}], encoded(TEMPLATE)]
        with patch.object(client, 'request', side_effect=missing + added) as api:
            self.assertEqual(client.templates('example/repo'), [])
            self.assertEqual(client._templates, {})
            self.assertEqual(client.templates('example/repo'), [template_file(TEMPLATE)])
        self.assertEqual(api.call_count, 17)

    def test_shared_request_and_decoding_errors_are_not_hidden(self):
        """公共模板缺失返回空列表，权限或解码失败保留错误且不缓存。

        Args:
            无。
        Returns:
            None；404、权限、限流和解码失败都不会生成可用文件。
        """
        for code in (404, 401, 403, 429, 500):
            client = atomgit.AtomGit('test-token', request_interval=0)
            with patch.object(client, 'request', side_effect=[
                    {'default_branch': 'main', 'namespace': {'type': 'group'}}, *[[] for _ in range(6)],
                    IssueProcessingError('shared error', status_code=code)]) as api:
                if code == 404:
                    self.assertEqual(client.templates('example/repo'), [])
                else:
                    with self.assertRaises(IssueProcessingError) as error:
                        client.templates('example/repo')
                    self.assertEqual(error.exception.status_code, code)
                self.assertEqual(api.call_count, 8)
                self.assertNotIn('example/repo', client._templates)
        client = atomgit.AtomGit('test-token', request_interval=0)
        with patch.object(client, 'request', side_effect=[
                {'default_branch': 'main', 'namespace': {'type': 'group'}}, *[[] for _ in range(6)],
                {'default_branch': 'master'}, [{'type': 'file', 'name': 'broken.yml'}],
                {'encoding': 'base64', 'content': '!!!!'}]):
            with self.assertRaises(IssueProcessingError) as error:
                client.templates('example/repo')
            self.assertIn('example/.gitcode', str(error.exception))
            self.assertNotIn('example/repo', client._templates)
