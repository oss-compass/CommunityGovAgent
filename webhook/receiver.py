"""AtomGit Issue Webhook 接收入口及共用的配置、数据库函数。

本文件提供函数，不提供命令行入口；日常操作统一使用 manage.py。
接收进程由 Gunicorn 加载 create_app()。
"""

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterator, Mapping

from flask import Flask, Response, jsonify, request
import yaml
from dotenv import dotenv_values


LOG = logging.getLogger("issue-gov-webhook")

CONFIG_KEYS = (
    "WEBHOOK_SECRET", "ATOMGIT_TOKEN", "AGENT_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "WEBHOOK_DB"
)


@dataclass(frozen=True)
class Config:
    """webhook服务端采用统一配置；凭据不出现在对象的字符串表示中。

    Attributes:
        secret: 各接入仓库 WebHook 页面使用的共同密码。
        token: 有权读取接入仓库、Issue 和模板，并发布评论的个人访问令牌。
        repositories: 允许接入的仓库路径列表。
        database: 本机 SQLite 队列文件路径，接收与任务处理进程必须使用同一文件。
        agent_api_key: 所有接入仓库共用的模型接口密钥，使用 Bearer 认证。
        llm_base_url: 模型服务的 Anthropic 兼容接口地址。
        llm_model: 模型服务支持的模型名称。
        maintainers: 接入仓库到维护者账号列表的映射，空列表表示不 @。
    """

    secret: str = field(repr=False) # 凭据不出现在配置对象的打印结果中，避免意外写入日志；字段本身仍可读取
    token: str = field(repr=False)
    repositories: tuple[str, ...]
    database: str
    agent_api_key: str = field(default="", repr=False)
    llm_base_url: str = ""
    llm_model: str = ""
    maintainers: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def from_file(cls) -> "Config":
        """读取当前运行配置，避免重启或重载时沿用 Supervisor 启动时的旧值。

        Args:
            无；配置文件固定为本模块同目录下的 runtime/.env。
        Returns:
            已校验的服务配置；配置文件缺失或内容无效时抛出 ValueError。
        """
        path = Path(__file__).resolve().parent / "runtime" / ".env"
        if not path.is_file():
            raise ValueError("请先运行 manage.py init 并填写 runtime/.env")
        values = dotenv_values(path, interpolate=False)
        return cls.from_env({key: value for key, value in values.items() if value is not None})

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Config":
        """读取环境配置，校验仓库与维护者名单。

        Args:
            environ: 配置映射；为 None 时使用进程环境变量。
        Returns:
            服务配置；缺少必填项或配置不合法时抛出 ValueError。
        """
        # 传入配置映射，未传入时读取当前进程的环境变量
        env = os.environ if environ is None else environ

        def required(name: str) -> str:
            """读取必填配置，拒绝空值和未替换的示例凭据。

            Args:
                name: 环境变量名称。
            Returns:
                去除首尾空白后的配置值；无效时抛出 ValueError。
            """
            value = env.get(name, "").strip()
            if not value or value.startswith("replace-with-"):
                raise ValueError(f"请配置 {name}")
            return value
        # 读取仓库列表和维护者配置
        repository_file = Path(__file__).resolve().parent / "runtime" / "repository.yml"
        try:
            configured = yaml.safe_load(repository_file.read_text(encoding="utf-8"))
            if not isinstance(configured, dict) or not configured:
                raise ValueError
            maintainers = {}
            for repository, accounts in configured.items():
                if (not isinstance(repository, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                        or any(part in {".", ".."} for part in repository.split("/"))
                        or not isinstance(accounts, list)
                        or any(not isinstance(account, str) for account in accounts)):
                    raise ValueError
                accounts = [account.strip().removeprefix("@") for account in accounts]
                maintainers[repository] = tuple(dict.fromkeys(accounts))
        except (OSError, UnicodeError):
            raise ValueError("无法读取 runtime/repository.yml，请先运行 manage.py init 并检查文件权限") from None
        except (ValueError, yaml.YAMLError):
            raise ValueError("仓库及维护者配置格式无效，请在 runtime/repository.yml 中至少填写一个 owner/repo；无需维护者时使用 []") from None

        # 完成校验后创建并返回配置对象
        return cls(
            secret=required("WEBHOOK_SECRET"),
            token=required("ATOMGIT_TOKEN"),
            repositories=tuple(maintainers),
            database=env.get("WEBHOOK_DB", "").strip() or "./runtime/data/events.sqlite3",
            agent_api_key=required("AGENT_API_KEY"),
            llm_base_url=required("LLM_BASE_URL"),
            llm_model=required("LLM_MODEL"),
            maintainers=maintainers,
        )


@contextmanager
def connect(config: Config) -> Iterator[sqlite3.Connection]:
    """创建一次数据库会话，退出时提交或回滚事务，并关闭连接。

    Args:
        config: 包含 SQLite 文件路径的服务配置。
    Yields:
        使用 sqlite3.Row 返回查询结果的连接，可按列名访问记录。
    Returns:
        供 with 语句使用的数据库连接上下文管理器；进入时提供下述连接，退出时关闭连接。
    """
    db = sqlite3.connect(config.database, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(config: Config) -> None:
    """准备 Webhook 数据库：创建目录、表和索引，并将每个 Issue 被更新请求替代的旧任务标为已覆盖。可以重复调用，让各个入口在使用数据库前确认它已经准备好。

    Args:
        config: 包含 SQLite 文件路径的服务配置。
    Returns:
        None；标记被后续请求覆盖的旧任务，保留全部历史记录；重复初始化不重新处理已覆盖记录。
    """

    # 创建数据库所在目录
    Path(config.database).parent.mkdir(parents=True, exist_ok=True)

    # 连接数据库并创建表和索引
    with connect(config) as db:
        db.execute("BEGIN IMMEDIATE")
        # 创建事件表
        db.execute("""CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY, repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
            last_error TEXT, result TEXT
        )""")
        # 创建索引
        db.execute("CREATE INDEX IF NOT EXISTS events_queue ON events(state, next_attempt)")
        db.execute("CREATE INDEX IF NOT EXISTS events_issue ON events(repository, issue_number, state)")
        # 兼容已有历史：按入库顺序将被新请求覆盖的旧任务终结，保留所有记录。
        db.execute("""UPDATE events SET state='superseded'
                      WHERE state IN ('queued', 'running', 'failed') AND EXISTS (
                          SELECT 1 FROM events newer WHERE newer.repository=events.repository
                            AND newer.issue_number=events.issue_number AND newer.rowid>events.rowid
                      )""")
        # 同一服务共用令牌；限流等待需跨任务及进程重启保留。
        db.execute("CREATE TABLE IF NOT EXISTS api_cooldown (name TEXT PRIMARY KEY, until REAL NOT NULL)")
        # 保存最近一次完成检查的内容和评论标记，用于去重及连续修改时更新评论。
        db.execute("""CREATE TABLE IF NOT EXISTS issue_checks (
            repository TEXT NOT NULL, issue_number INTEGER NOT NULL, content_hash TEXT NOT NULL,
            PRIMARY KEY (repository, issue_number)
        )""")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(issue_checks)")}
        for name, kind in (("comment_marker", "TEXT"), ("issue_updated_at", "REAL")):
            if name not in columns:
                db.execute(f"ALTER TABLE issue_checks ADD COLUMN {name} {kind}")

        # 在请求平台前保存发送意图，进程退出或响应丢失后可以核对同一条评论。
        db.execute("""CREATE TABLE IF NOT EXISTS pending_comments (
            repository TEXT NOT NULL, issue_number INTEGER NOT NULL, content_hash TEXT NOT NULL,
            marker TEXT NOT NULL, body TEXT NOT NULL, summary TEXT NOT NULL,
            PRIMARY KEY (repository, issue_number)
        )""")

        # 记录建议
        db.execute("""CREATE TABLE IF NOT EXISTS maintainer_suggestions (
            id INTEGER PRIMARY KEY,
            repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
            created_at TEXT NOT NULL, reason TEXT NOT NULL, suggestion TEXT NOT NULL, mtr TEXT NOT NULL
        )""")
        db.execute("CREATE INDEX IF NOT EXISTS maintainer_suggestions_date ON maintainer_suggestions(created_at)")


def select_issue(
    payload: dict[str, Any], repositories: tuple[str, ...],
) -> tuple[str, int] | None:
    """筛选目标仓库未关闭 Issue 的创建及标题、正文更新事件。

    Args:
        payload: AtomGit Webhook 的 JSON 请求体。
        repositories: 允许接入的仓库路径列表。
    Returns:
        来源仓库和仓库内 Issue 编号；无关事件或明确已关闭时返回 None。
        未提供状态时允许入队，由后台读取最新状态确认。
        目标 Issue 事件缺少必要字段或编号不合法时抛出 ValueError。
    """

    # 检查是否为 Issue 事件
    if payload.get("object_kind", payload.get("event_type")) != "issue":
        return None

    # 读取仓库信息和 Issue 信息
    project = payload.get("project")
    attrs = payload.get("object_attributes")
    if not isinstance(project, dict) or not isinstance(attrs, dict):
        raise ValueError("missing project/object_attributes")

    # 检查仓库是否接入
    repository = project.get("path_with_namespace")
    if not isinstance(repository, str) or repository not in repositories:
        return None
    
    # 检查事件类型和实际内容变化（只处理新建和更新）
    action = attrs.get("action")
    if action not in ("open", "update"):
        return None
    # 已关闭 Issue 的内容编辑不再入队；缺少状态时交给后台查询确认。
    if attrs.get("state") == "closed":
        return None
    
    # 检查更新事件是否实际修改了标题、描述或正文
    changes = payload.get("changes")
    if action == "update" and isinstance(changes, dict):
        content_changes = [changes[k] for k in ("title", "description", "body") if k in changes]
        if not content_changes:
            return None
        if all(isinstance(c, dict) and "previous" in c and "current" in c
               and c["previous"] == c["current"] for c in content_changes):
            return None

    # 检查Issue编号
    number = attrs.get("iid")
    if isinstance(number, bool) or not re.fullmatch(r"[1-9][0-9]*", str(number)):
        raise ValueError("invalid issue iid")
    number = int(number)
    if number > 2**63 - 1:
        raise ValueError("issue iid too large")
    
    return repository, number


def create_app(config: Config | None = None) -> Flask:
    """创建 Webhook HTTP 应用，只负责认证、筛选和入队，不在 HTTP 请求中执行治理检查。

    Args:
        config: 显式服务配置；为 None 时从 runtime/.env 读取。
    Returns:
        可由 Gunicorn 加载的 Flask 应用，包含健康检查和 Webhook 两个入口。
    """

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # 读取配置并创建数据库、表和索引
    config = config or Config.from_file()
    initialize(config)

    # 创建应用，限制请求体大小为 1MB
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024

    @app.get("/health")
    def health() -> Response:
        """返回接收进程的存活状态。

        Args:
            无；由 Flask 在收到 GET /health 请求时调用。
        Returns:
            包含 {"status": "ok"} 的 JSON 响应，仅表示接收进程存活。
        """
        return jsonify(status="ok")

    @app.post("/webhook")
    def webhook() -> tuple[Response, int]:
        """校验 AtomGit WebHook 密码，将需要检查的 Issue 事件持久化入队。

        Args:
            无显式参数；请求头和 JSON 请求体从 Flask 当前请求中读取。
        Returns:
            JSON 响应及 HTTP 状态码：新事件入队为 202，重复或忽略为 200；
            认证失败为 401，负载不合法为 400，队列写入失败为 503。
            请求体超过应用大小限制时，由 Flask 返回 413。
        """

        # 验证请求密码
        supplied = request.headers.get("X-GitCode-Token", "")
        if not hmac.compare_digest(supplied.encode(), config.secret.encode()):
            return jsonify(error="unauthorized"), 401

        # 读取请求中的 JSON 数据（AtomGit 发来的事件内容）
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="expected JSON object"), 400
        
        # 筛选需要处理的 Issue
        try:
            selected = select_issue(payload, config.repositories)
        except ValueError:
            return jsonify(error="invalid Issue event"), 400
        if selected is None:
            return jsonify(status="ignored"), 200
        repository, number = selected # owner/repo, number

        # 确定事件身份，生成唯一标识，用于去重
        identity = (payload.get("uuid") or payload.get("produce_random_id")
                    or request.headers.get("X-GitCode-Delivery"))
        if identity is not None and not isinstance(identity, str):
            return jsonify(error="invalid event identifier"), 400
        if not identity:
            identity = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        event_id = hashlib.sha256((repository + ":" + identity).encode()).hexdigest()

        # 将任务保存到数据库
        try:
            with connect(config) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT state FROM events WHERE event_id = ?", (event_id,)).fetchone()
                added = row is None
                if added:
                    previous = db.execute("""SELECT attempts, next_attempt FROM events
                                             WHERE repository = ? AND issue_number = ? AND state='queued'""",
                                          (repository, number)).fetchone()
                    # 保留旧请求的编号、结果和错误，仅结束其调度；新通知另存一条记录。
                    db.execute("""UPDATE events SET state='superseded'
                                  WHERE repository = ? AND issue_number = ?
                                    AND state IN ('queued', 'running', 'failed')""", (repository, number))
                    db.execute("""INSERT INTO events(event_id, repository, issue_number, updated_at,
                                                    attempts, next_attempt) VALUES (?, ?, ?, ?, ?, ?)""",
                               (event_id, repository, number, time.time(),
                                previous["attempts"] if previous else 0,
                                previous["next_attempt"] if previous else 0))
                    row = {"state": "queued"}
        except sqlite3.Error:
            LOG.error("通知保存失败：仓库=%s，Issue=#%s，数据库写入失败，请检查磁盘空间和目录权限",
                      repository, number)
            return jsonify(error="queue unavailable"), 503

        LOG.info("%s：仓库=%s，Issue=#%s，事件=%s",
                 "收到通知，已加入队列" if added else "收到重复通知，已去重，不再新增任务",
                 repository, number, event_id[:12])

        # 返回处理结果：202 表示新事件入队，200 表示重复或忽略
        return jsonify(status="queued" if added else "duplicate", state=row["state"]), 202 if added else 200

    return app


def queue_snapshot(config: Config) -> dict[str, Any]:
    """只读查询各仓库的任务统计和最近五条任务，不创建或修改数据库。

    Args:
        config: 包含任务数据库路径的服务配置。
    Returns:
        database 为绝对路径，exists 表示文件存在；counts 为按仓库及状态分组的
        数量，recent 为最近五条任务。文件尚不存在时两个列表为空。
    """
    path = Path(config.database).resolve()
    snapshot: dict[str, Any] = {"database": str(path), "exists": path.is_file(),
                                "counts": [], "recent": []}
    if not snapshot["exists"]:
        return snapshot
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute("BEGIN")
            snapshot["counts"] = [dict(row) for row in db.execute(
                "SELECT repository, state, COUNT(*) AS count FROM events "
                "GROUP BY repository, state ORDER BY repository, state")]
            snapshot["recent"] = [dict(row) for row in db.execute(
                "SELECT repository, issue_number, state, attempts, updated_at, last_error, result "
                "FROM events ORDER BY updated_at DESC, event_id LIMIT 5")]
    finally:
        db.close()
    return snapshot
