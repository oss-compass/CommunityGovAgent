"""AtomGit Issue Webhook 接收入口。数据库会话在 common.store。

主机上的日常操作仍使用 manage.py，由 Supervisor 加载 create_app()。
容器入口是本文件的 main()，直接启动 Gunicorn，不经过 Supervisor。
配置了 SERVICE_MODE=queue 时，校验通过的任务写入 PostgreSQL 并投递到消息队列。
"""

import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any

from flask import Flask, Response, jsonify, request
from dotenv import dotenv_values

from common.pgstore import DatabaseError
from common.queueing import QueuePublishError, publish_analysis_task
from common.store import connect, initialize, lock_issue
# queue_snapshot 供 manage.py 和测试从本模块转取；ruff 的 F401 可忽略。
from common.store import queue_snapshot  # noqa: F401
import issue_gov.paths as paths
from issue_gov.checks.base import default_checks, dump_checks
from issue_gov.queues import ISSUE_ANALYSIS
from issue_gov.settings import Config


LOG = logging.getLogger("issue-gov-webhook")
# 新通知入库后留给本次请求自己发布。重试线程在这之前不会再发一份。
# 进程若在发布前退出，到期后由重试线程补投。一次代理确认通常远短于这个间隔。
PUBLISH_GRACE = 5


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
        config: 显式服务配置；为 None 时从服务根目录的 .env 读取。
    Returns:
        可由 Gunicorn 加载的 Flask 应用，包含健康检查和 Webhook 两个入口。
    """

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # 读取配置；先同步建表，数据库暂时不可达时转到后台重试，就绪前 /ready 和 /webhook 返回 503
    config = config or Config.from_file()
    ready_event = threading.Event()
    try:
        initialize(config)
    except (sqlite3.Error, DatabaseError, OSError):
        LOG.error("数据库尚未就绪，转到后台重试初始化")
        _retry_initialize_in_background(config, ready_event)
    else:
        ready_event.set()

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

    @app.get("/ready")
    def ready() -> tuple[Response, int]:
        """确认数据库已初始化且可读写；服务模式下同时确认消息队列可连接。"""
        if not ready_event.is_set():
            return jsonify(status="initializing"), 503
        try:
            with connect(config) as db:
                db.execute("SELECT 1")
            if config.rabbitmq_url:
                from common.queueing import check
                check(config.rabbitmq_url, ISSUE_ANALYSIS)
        except (sqlite3.Error, DatabaseError, QueuePublishError):
            return jsonify(status="unavailable"), 503
        return jsonify(status="ok"), 200

    @app.post("/webhook")
    def webhook() -> tuple[Response, int]:
        """校验 AtomGit WebHook 密码，将需要检查的 Issue 事件持久化入队。

        Args:
            无显式参数；请求头和 JSON 请求体从 Flask 当前请求中读取。
        Returns:
            JSON 响应及 HTTP 状态码：新事件入队为 202，重复或忽略为 200；
            认证失败为 401，负载不合法为 400，数据库未就绪或队列写入失败为 503。
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

        # 表尚未建好时让通知方稍后重试，不在请求线程里等待初始化
        if not ready_event.is_set():
            return jsonify(error="initializing"), 503

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

        # 将任务保存到数据库。服务模式下先记为 queued，并给投递留出宽限，避免重试线程同时再发一条。
        # 代理确认后再改为 dispatching，且只改仍停在这次宽限上的行。投递前进程退出时任务仍是 queued。
        added = False
        stored_state = ""
        publish_stamp = None
        try:
            with connect(config) as db:
                db.execute("BEGIN IMMEDIATE")
                # PostgreSQL 下 BEGIN 是空操作，用按 Issue 的咨询锁让同一 Issue 的入队串行执行
                lock_issue(db, repository, number)
                row = db.execute("SELECT state, checks, next_attempt FROM events WHERE event_id = ?",
                                 (event_id,)).fetchone()
                added = row is None
                stored_state = "" if row is None else row["state"]
                checks = default_checks() if row is None or not row["checks"] else json.loads(row["checks"])
                if added:
                    previous = db.execute("""SELECT attempts, next_attempt FROM events
                                             WHERE repository = ? AND issue_number = ?
                                               AND state IN ('queued', 'dispatching')""",
                                          (repository, number)).fetchone()
                    # 保留旧请求的编号、结果和错误，仅结束其调度；新通知另存一条记录。
                    db.execute("""UPDATE events SET state='superseded'
                                  WHERE repository = ? AND issue_number = ?
                                    AND state IN ('queued', 'running', 'failed', 'dispatching')""",
                               (repository, number))
                    now = time.time()
                    # 宽限内由本次请求投递。失败或进程退出后 next_attempt 到期，重试线程再投。
                    next_attempt = now + PUBLISH_GRACE if config.rabbitmq_url else (
                        previous["next_attempt"] if previous else 0)
                    publish_stamp = next_attempt
                    db.execute("""INSERT INTO events(event_id, repository, issue_number, updated_at,
                                                    attempts, next_attempt, state, checks)
                                  VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                               (event_id, repository, number, now,
                                previous["attempts"] if previous else 0,
                                next_attempt, "queued",
                                dump_checks(default_checks())))
                elif stored_state == "queued":
                    publish_stamp = row["next_attempt"]
        except (sqlite3.Error, DatabaseError):
            LOG.error("通知保存失败：仓库=%s，Issue=#%s，数据库写入失败，请检查磁盘空间和目录权限",
                      repository, number)
            return jsonify(error="queue unavailable"), 503

        # 新任务，或上次投递失败后仍停在 queued 的重复通知，才发布到队列。
        # 已经是 dispatching 的重复通知不重复发布，队列里那一份会由消费者领取。
        if config.rabbitmq_url and (added or stored_state == "queued"):
            try:
                publish_analysis_task(config.rabbitmq_url, event_id, repository, number, checks, ISSUE_ANALYSIS)
            except QueuePublishError:
                if added:
                    _release_dispatch(config, event_id)
                LOG.error("通知已保存但未能投递到消息队列：仓库=%s，Issue=#%s，事件=%s",
                          repository, number, event_id[:12])
                return jsonify(error="queue unavailable"), 503
            _mark_dispatching(config, event_id, publish_stamp)

        LOG.info("%s：仓库=%s，Issue=#%s，事件=%s",
                 "收到通知，已加入队列" if added else "收到重复通知，已去重，不再新增任务",
                 repository, number, event_id[:12])

        # 返回处理结果：202 表示新事件入队，200 表示重复或忽略
        return jsonify(status="queued" if added else "duplicate",
                       state="queued" if added else stored_state), 202 if added else 200

    return app


def _retry_initialize_in_background(config: Config, done: threading.Event) -> None:
    """数据库暂不可达时在守护线程里每 2 秒重试建表，成功后置位 done。

    就绪前 /ready 和 /webhook 返回 503，容器编排据此等待，
    而不是让工作进程退出后反复重启。

    Args:
        config: 服务配置。
        done: 初始化成功后被 set 的 threading.Event。
    """
    def run() -> None:
        while True:
            time.sleep(2)
            try:
                initialize(config)
            except (sqlite3.Error, DatabaseError, OSError):
                LOG.error("数据库尚未就绪，2 秒后重试初始化")
                continue
            done.set()
            return

    threading.Thread(target=run, name="issue-gov-init", daemon=True).start()


def _release_dispatch(config: Config, event_id: str) -> None:
    """投递失败时把刚入库的任务退回 queued，等待通知方或重试线程再次投递。"""
    try:
        with connect(config) as db:
            db.execute("""UPDATE events SET state='queued', next_attempt=0
                          WHERE event_id=? AND state IN ('queued', 'dispatching')""", (event_id,))
    except (sqlite3.Error, DatabaseError):
        LOG.error("任务未能退回等待投递：事件=%s", event_id[:12])


def _mark_dispatching(config: Config, event_id: str, next_attempt: float) -> None:
    """代理确认后标记为已投递。消费者若已改过下次投递时间，说明任务已进入重试，不能再盖回。"""
    try:
        with connect(config) as db:
            db.execute("""UPDATE events SET state='dispatching'
                          WHERE event_id=? AND state='queued' AND next_attempt=?""",
                       (event_id, next_attempt))
    except (sqlite3.Error, DatabaseError):
        LOG.error("任务已投递但状态更新失败：事件=%s", event_id[:12])


def listen_port() -> int:
    """读取容器或进程环境中的端口，未设置时再读服务根目录的 .env，最后默认 7000。"""
    port = os.environ.get("WEBHOOK_PORT", "").strip()
    if not port:
        path = paths.env_file()
        if path.is_file():
            port = (dotenv_values(path, interpolate=False).get("WEBHOOK_PORT") or "").strip()
    port = port or "7000"
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise ValueError("WEBHOOK_PORT 必须是 1 到 65535 之间的整数。")
    return int(port)


def main() -> None:
    """容器和直接启动入口：用 Gunicorn 加载接收应用，不经过 Supervisor。"""
    from gunicorn.app.base import BaseApplication

    os.umask(0o077)
    port = listen_port()
    workers = os.environ.get("WEB_CONCURRENCY", "2").strip() or "2"
    if not workers.isascii() or not workers.isdecimal() or not 1 <= int(workers) <= 32:
        raise ValueError("WEB_CONCURRENCY 必须是 1 到 32 之间的整数。")

    class ReceiverApplication(BaseApplication):
        def __init__(self) -> None:
            self.options = {
                "bind": f"0.0.0.0:{port}",
                "workers": int(workers),
                "timeout": 30,
                "errorlog": "-",
                "graceful_timeout": 20,
            }
            super().__init__()

        def load_config(self) -> None:
            for key, value in self.options.items():
                self.cfg.set(key.lower(), value)

        def load(self):
            return create_app()

    ReceiverApplication().run()


if __name__ == "__main__":
    main()
