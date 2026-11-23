"""AtomGit API：仅访问固定 API 域名，错误信息不包含凭据或响应原文。"""

import base64
import binascii
from copy import deepcopy
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import math
import time
from http.client import HTTPMessage
from pathlib import PurePosixPath
from typing import BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from errors import IssueProcessingError

API_URL = "https://api.atomgit.com/api/v5"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
TEMPLATE_CACHE_SECONDS = 60


def retry_after_seconds(value: str | None) -> float:
    """将 Retry-After 转成等待秒数。

    Args:
        value: 响应头中的秒数、HTTP 日期或 None。
    Returns:
        至少 30 秒的等待时间；值缺失、过期或无效时返回 30。
    """
    try:
        if value is None:
            return 30
        if value.strip().isdigit():
            seconds = float(value.strip())
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - time.time()
        return max(30, seconds) if math.isfinite(seconds) else 30
    except (ValueError, TypeError, OverflowError):
        return 30


class NoRedirect(HTTPRedirectHandler):
    """禁止API 请求自动跳转，避免包含令牌的请求被转发到其他地址。"""

    def redirect_request(
        self, req: Request, fp: BinaryIO, code: int, msg: str,
        headers: HTTPMessage, newurl: str,
    ) -> None:
        """拒绝 urllib 发起的新跳转请求，让调用方处理原始 HTTP 错误。

        Args:
            req: 原始API 请求。
            fp: 重定向响应的可读对象。
            code: 重定向 HTTP 状态码。
            msg: 响应状态说明。
            headers: 重定向响应头。
            newurl: 服务端要求跳转的地址。
        Returns:
            None，表示不创建后续请求。
        """
        return None


class AtomGit:
    def __init__(self, token: str, request_interval: float = 1):
        """保存访问令牌，创建 AtomGit API 客户端，用于与AtomGit平台交互；初始化时不发送请求。

        Args:
            token: 服务账号的 AtomGit 访问令牌，请求时使用 Bearer 认证。
            request_interval: 同一客户端两次请求开始时间的最小间隔，默认 1 秒。
        Returns:
            None。
        """
        self._token = token
        self._request_interval = request_interval
        self._last_request: float | None = None
        self._templates: dict[str, tuple[float, list[dict]]] = {} # 缓存模板原文，格式为[仓库路径，(过期时间戳, 文件列表)]

    def request(self, method: str, path: str, data=None):
        """向固定 AtomGit API 地址发送请求并解析响应，将请求失败转换为可安全记录的异常。

        请求禁止自动重定向，等待超时为 30 秒，响应大小限制为 2 MiB。

        Args:
            method: HTTP 方法，例如 GET、POST 或 PATCH。
            path: 相对于 API_URL 的接口路径，以 / 开头，可包含查询参数。
            data: 可序列化为 JSON 的请求数据；为 None 时不发送请求体。
        Returns:
            响应 JSON 对象或数组；成功响应的正文为空时返回空字典。
        Raises:
            IssueProcessingError: 网络或 HTTP 错误、响应过大、JSON 无效或响应类型不受支持；
                retryable 属性标明是否允许自动重试。
        """
        headers = {"Authorization": "Bearer " + self._token,
                   "Accept": "application/json", "Content-Type": "application/json"}
        payload = None if data is None else json.dumps(data).encode()
        req = Request(API_URL + path, headers=headers, data=payload, method=method)
        # 限制请求频率，避免触发平台的速率限制；如果上次请求时间未知或间隔足够长，则立即发送请求
        if self._last_request is not None:
            delay = self._last_request + self._request_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        self._last_request = time.monotonic()
        try:
            with build_opener(NoRedirect()).open(req, timeout=30) as response:
                if not 200 <= response.status < 300:
                    raise IssueProcessingError(f"AtomGit 返回异常状态 HTTP {response.status}")
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise IssueProcessingError("AtomGit 响应过大", retryable=False)
                result = json.loads(content) if content.strip() else {}
        except HTTPError as exc:
            code = exc.code
            # 429 限流时，尝试从响应头中解析 Retry-After，缺失或无效时默认 30 秒
            retry_after = retry_after_seconds((exc.headers or {}).get("Retry-After")) if code == 429 else 0
            exc.close()
            message = ("AtomGit 请求被限流，HTTP 429" if code == 429
                       else f"AtomGit 请求失败，HTTP {code}")
            raise IssueProcessingError(message, code in {408, 429} or code >= 500,
                                       status_code=code, retry_after=retry_after) from None
        except (URLError, TimeoutError, OSError):
            raise IssueProcessingError("AtomGit 网络连接失败或超时") from None
        except (ValueError, UnicodeError):
            raise IssueProcessingError("AtomGit 返回无效 JSON") from None
        if not isinstance(result, (dict, list)):
            raise IssueProcessingError("AtomGit 返回的数据格式不正确")
        return result

    def templates(self, repository: str) -> list[dict[str, str]]:
        """读取模板文件原文，缺失时回退到组织公共模板，按目标仓库缓存 60 秒。

        Args:
            repository: 目标仓库路径，格式为 owner/repo。
        Returns:
            仓库首个模板位置中的文件列表，排除 config.yml/config.yaml；
            每项含 filename（文件名）、source（来源仓库及文件路径）、content（UTF-8 原文）；
            无模板时读取所属组织 .gitcode 仓库的默认分支，不与本地模板混合；
            缓存命中时返回副本，过期后重新读取；不解析或校验 YAML/Markdown 内容。
            缓存仅表示文件已读取成功；格式错误由分析阶段报告，修正文件后需等待缓存到期刷新。
            仓库及可用的组织公共位置均无模板时返回空列表，由分析阶段通知维护者；不缓存空列表。
        Raises:
            IssueProcessingError: 平台请求失败、默认分支无效、文件响应或编码无效。
                查找路径时只有 404 会继续尝试其他位置，其他请求错误直接返回。
        """
        # 如果缓存未过期，直接返回副本
        cached = self._templates.get(repository)
        if cached is not None and time.monotonic() < cached[0]:
            return deepcopy(cached[1])
        
        info = self.request("GET", f"/repos/{repository}")
        branch = info.get("default_branch") if isinstance(info, dict) else None
        if not isinstance(branch, str) or not branch.strip():
            raise IssueProcessingError("仓库未返回默认分支", retryable=False)
        query = "?" + urlencode({"ref": branch})
        prefix = f"/repos/{repository}/contents/"
        locations = ((".gitcode/ISSUE_TEMPLATE", False), (".gitcode", True),
                     (".github/ISSUE_TEMPLATE", False), (".github", True),
                     ("docs", True), ("", True))
        for directory, single_file in locations:
            try:
                entries = self.request("GET", prefix + quote(directory, safe="/") + query)
            except IssueProcessingError as exc:
                if exc.status_code == 404:
                    continue
                raise
            if not isinstance(entries, list):
                raise IssueProcessingError("模板路径不是目录", retryable=False)
            # 下载并解码每个模板文件
            templates = []
            for entry in entries:
                if not isinstance(entry, dict) or entry.get("type") != "file":
                    continue
                name = entry.get("name", "")
                if not isinstance(name, str):
                    continue
                suffix = PurePosixPath(name).suffix.lower()
                if single_file:
                    if name.lower() != "issue_template.md":
                        continue
                elif suffix not in {".yml", ".yaml", ".md"}:
                    continue
                if name.lower() in {"config.yml", "config.yaml"}:
                    continue
                if "/" in name or "\\" in name:
                    raise IssueProcessingError("模板文件名不合法", retryable=False)
                if len(templates) >= 50:
                    raise IssueProcessingError("模板数量超过 50 个", retryable=False)
                path = directory + "/" + name if directory else name
                item = self.request("GET", prefix + quote(path, safe="/") + query)
                try:
                    if not isinstance(item, dict) or item.get("encoding") != "base64":
                        raise ValueError("encoding")
                    raw = base64.b64decode("".join(item["content"].split()), validate=True)
                    content = raw.decode("utf-8")
                except (ValueError, TypeError, KeyError, AttributeError, binascii.Error):
                    raise IssueProcessingError(
                        f"仓库模板文件读取失败：{json.dumps(repository + '/' + path, ensure_ascii=False)}；"
                        "请检查文件响应及 Base64/UTF-8 编码",
                        retryable=False) from None
                templates.append({"filename": name, "source": repository + "/" + path, "content": content})
            # 成功读取模板后，保存缓存并返回结果
            if templates:
                self._templates[repository] = (time.monotonic() + TEMPLATE_CACHE_SECONDS, deepcopy(templates))
                return templates
        # 本地无模板时复用同一读取流程；公共仓库自身不再回退。
        owner = repository.split("/", 1)[0]
        shared = f"{owner}/.gitcode"
        namespace = info.get("namespace") or {}
        if repository != shared and namespace.get("type") in {None, "group"}:
            try:
                if not namespace.get("type"):
                    self.request("GET", f"/orgs/{owner}")
                templates = self.templates(shared)
            except IssueProcessingError as exc:
                if exc.status_code != 404:
                    raise
            else:
                if templates:
                    self._templates[repository] = deepcopy(self._templates[shared])
                return templates
        return []

    def has_comment(self, repository: str, number: int, marker: str) -> bool:
        """分页核对持久化的随机评论标记；查询失败时抛错，不能当作尚未发布。

        Args:
            repository: 目标仓库路径。
            number: Issue 编号。
            marker: 本次发送前生成并持久化的完整隐藏标记。
        Returns:
            找到对应评论时为 True；全部分页查完仍未找到时为 False。
        """
        return self.find_comment(repository, number, marker) is not None

    def find_comment(self, repository: str, number: int, marker: str) -> dict | None:
        """分页按持久化的完整标记查找本服务评论。

        Args:
            repository: 目标仓库路径，格式为 owner/repo。
            number: 仓库内的 Issue 编号。
            marker: 评论正文中独占一行的完整隐藏标记。
        Returns:
            首个包含该标记的评论对象；全部分页查完仍未找到时返回 None。
        Raises:
            IssueProcessingError: 查询请求失败或评论响应格式无效。
        """
        page = 1
        while True:
            query = urlencode({"page": page, "per_page": 100, "order": "desc"})
            comments = self.request("GET", f"/repos/{repository}/issues/{number}/comments?{query}")
            if not isinstance(comments, list):
                raise IssueProcessingError("评论查询响应格式不正确")
            for comment in comments:
                if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
                    raise IssueProcessingError("评论查询内容格式不正确")
                if marker in comment["body"].splitlines():
                    return comment
            if len(comments) < 100:
                return None
            page += 1

    def create_comment(self, repository: str, number: int, body: str) -> str:
        """发布一条新的检查评论。

        Args:
            repository: 目标仓库路径，格式为 owner/repo。
            number: 仓库内的 Issue 编号。
            body: 要发布的完整 Markdown 评论。
        Returns:
            created 表示创建评论；平台请求失败时抛出 IssueProcessingError。
        """
        self.request("POST", f"/repos/{repository}/issues/{number}/comments", {"body": body})
        return "created"

    def update_comment(self, repository: str, comment_id: int, body: str) -> str:
        """更新已定位的检查评论，评论不存在时交由调用方处理。

        Args:
            repository: 目标仓库路径，格式为 owner/repo。
            comment_id: 要更新的评论 ID。
            body: 替换旧正文的完整 Markdown 评论。
        Returns:
            成功时返回 updated；平台返回 HTTP 404 时返回 missing。
        Raises:
            IssueProcessingError: 除 HTTP 404 外的请求错误，由任务队列处理。
        """
        try:
            self.request("PATCH", f"/repos/{repository}/issues/comments/{comment_id}", {"body": body})
        except IssueProcessingError as exc:
            if exc.status_code != 404:
                raise
            return "missing"
        return "updated"
