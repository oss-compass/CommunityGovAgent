"""后台任务与治理流程：领取任务、组织治理功能、写回评论、保存结果及重试。"""

from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import signal
import sys
import threading
import time

from common.atomgit import AtomGit
from common.errors import IssueProcessingError
from common.store import connect, initialize
from issue_gov.analysis import run_checks as analyze_issue
from issue_gov.queues import ISSUE_ANALYSIS
from issue_gov.settings import Config

LOG = logging.getLogger("issue-gov-webhook")
MAX_ATTEMPTS = 5  # 每个事件最多完成 5 次处理尝试；进程中断不消耗次数
COMMENT_EDIT_SECONDS = 3 * 60
CHECK_PASSED_LABEL = "issue-check-passed"
CHECK_FAILED_LABEL = "issue-check-failed"


def _issue_edit_time(issue: dict) -> float | None:
    """使用平台的 Issue 修改时间，避免处理耗时改变三分钟评论更新窗口。

    Args:
        issue: 平台返回的 Issue 对象，updated_at 应为带时区的 ISO 日期时间。
    Returns:
        修改时间的 Unix 时间戳（秒）；时间缺失、格式无效、无时区或溢出时返回 None。
    """
    try:
        value = datetime.fromisoformat(issue.get("updated_at"))
        return value.timestamp() if value.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None  # 缺少可靠时间时不合并评论。


def issue_content_hash(issue: dict) -> str:
    """校验并计算标题、正文的内容标识，不把评论数或更新时间算作正文修改。

    Args:
        issue: AtomGit 返回的 Issue 对象，正文允许为 None。
    Returns:
        标题和正文的 SHA-256；字段无效时抛出可重试的 IssueProcessingError。
    """
    title, body = issue.get("title"), issue.get("body")
    if not isinstance(title, str) or (body is not None and not isinstance(body, str)):
        raise IssueProcessingError("Issue 标题或正文格式不正确")
    return hashlib.sha256(json.dumps([title, body or ""], ensure_ascii=False).encode()).hexdigest()


def _save_maintainer_suggestion(db, repository: str, number: int, suggestion: dict) -> int:
    """在调用方事务内保存维护者建议，不单独提交事务。

    Args:
        db: 当前事务使用的 SQLite 数据库连接。
        repository: 建议关联的仓库路径，格式为 owner/repo。
        number: 建议关联的 Issue 编号。
        suggestion: 包含 reason、suggestion 和 mtr 的字典；前两项为原因和建议文本，
            mtr 为配置中的维护者用户名列表序列化后的 JSON 字符串。
    Returns:
        新增维护者建议记录的整数 ID。
    Raises:
        sqlite3.Error: 数据库写入失败，由调用方回滚事务并安排重试。
    """
    return db.execute("""INSERT INTO maintainer_suggestions
                         (repository, issue_number, created_at, reason, suggestion, mtr) VALUES (?, ?, ?, ?, ?, ?)""",
                      (repository, number, datetime.now(timezone.utc).isoformat(),
                       suggestion["reason"], suggestion["suggestion"], suggestion["mtr"])).lastrowid


def _verdict_labels(summary: dict) -> tuple[str, str] | None:
    """按检查结论选择要写入的标签，以及需要去掉的相反标签。

    摘要里的 label_verdict 优先。没有该字段时，通过、以及未匹配但推荐认为不适用，
    使用 issue-check-passed。部分匹配和其余未匹配使用 issue-check-failed。
    模板故障、关闭和未变化没有结论时不改标签。

    Args:
        summary: 分析摘要。可包含 label_verdict、status 和 recommendation_status。
    Returns:
        (要添加的标签, 要移除的相反标签)；没有结论时返回 None。
    """
    verdict = summary.get("label_verdict")
    if verdict == "passed":
        return CHECK_PASSED_LABEL, CHECK_FAILED_LABEL
    if verdict == "failed":
        return CHECK_FAILED_LABEL, CHECK_PASSED_LABEL
    if verdict == "unchanged":
        return None
    status = summary.get("status")
    passed = status == "pass" or (
        status == "no_match" and summary.get("recommendation_status") == "not_applicable")
    if passed:
        return CHECK_PASSED_LABEL, CHECK_FAILED_LABEL
    if status in {"partial_match", "no_match"}:
        return CHECK_FAILED_LABEL, CHECK_PASSED_LABEL
    return None


def _assign_check_label(client: AtomGit, repository: str, number: int, summary: dict) -> dict:
    """把检查结论写成 Issue 标签。仓库没有该标签或平台拒绝时只记录日志。

    先添加本次结论。添加成功后再移除相反标签，避免仓库缺少新标签时把原标签清掉。
    移除时平台返回 404 表示 Issue 上本来没有那个标签。任一失败都不抛出，不影响评论和任务完成。

    Args:
        client: 当前任务使用的 AtomGit 客户端。
        repository: 目标仓库路径，格式为 owner/repo。
        number: 仓库内的 Issue 编号。
        summary: 分析摘要。
    Returns:
        label_action 为 assigned、rejected 或 skipped。有结论时还包含 label，即本次要写入的标签名。
    """
    labels = _verdict_labels(summary)
    if labels is None:
        return {"label_action": "skipped"}
    wanted, other = labels
    path = f"/repos/{repository}/issues/{number}/labels"
    record = {"label": wanted, "label_action": "rejected"}
    try:
        client.request("POST", path, [wanted])
    except IssueProcessingError as exc:
        LOG.warning("检查标签未写入，任务继续：仓库=%s，Issue=#%s，标签=%s，原因=%s",
                    repository, number, wanted, exc)
        return record
    record["label_action"] = "assigned"
    try:
        client.request("DELETE", f"{path}/{other}")
    except IssueProcessingError as exc:
        if exc.status_code != 404:
            LOG.warning("旧检查标签未移除，任务继续：仓库=%s，Issue=#%s，标签=%s，原因=%s",
                        repository, number, other, exc)
    return record


def _process_issue(config: Config, repository: str, number: int, *, client: AtomGit,
                   checks=None) -> dict:
    """读取 Issue 和模板，按分析决定静默记录或新增、更新评论。

    Args:
        config: 服务配置。
        repository: 目标仓库路径，格式为 owner/repo，必须在接入列表中。
        number: 仓库内的正整数 Issue 编号。
        client: 由 run_worker 创建并复用的客户端，用于保留缓存和请求间隔。
    Returns:
        处理结果字典。status 为 pass、partial_match、no_match、template_error、skipped_closed
        或 skipped_unchanged；comment_action 为 created、updated、recovered、suppressed、skipped_closed 或 unchanged。
        执行过分析时还包含 recommendation_status，为模型推荐状态或 None；
        首次读取即发现 Issue 已关闭时不包含该字段。
        label_action 为 assigned、rejected 或 skipped；分配过标签时包含 label。
    """
    if repository not in config.repositories:
        raise IssueProcessingError("仓库已不在允许接入列表中", retryable=False)
    if not re.fullmatch(r"[1-9][0-9]*", str(number)):
        raise IssueProcessingError("Issue 编号必须是正整数", retryable=False)
    
    # 获取issue
    path = f"/repos/{repository}/issues/{number}"
    issue = client.request("GET", path)
    if not isinstance(issue, dict) or not isinstance(issue.get("title"), str):
        raise IssueProcessingError("Issue 响应格式不正确")
    if "pull_request" in issue:
        raise IssueProcessingError("该编号对应 Pull Request，不能执行 Issue 检查", retryable=False)
    if issue.get("state") == "closed":
        return {"status": "skipped_closed", "comment_action": "skipped_closed", "label_action": "skipped"}
    digest = issue_content_hash(issue)
    with connect(config) as db:
        checked = db.execute("SELECT * FROM issue_checks WHERE repository = ? AND issue_number = ?",
                             (repository, number)).fetchone()
        pending = db.execute("SELECT * FROM pending_comments WHERE repository = ? AND issue_number = ?",
                             (repository, number)).fetchone()
    # 有未确认发送时，旧的已检查记录不足以证明当前最后一条评论对应最新内容。
    if checked is not None and checked["content_hash"] == digest and pending is None:
        return {"status": "skipped_unchanged", "comment_action": "unchanged", "label_action": "skipped"}
    pending_summary = json.loads(pending["summary"]) if pending is not None else {}
    delivery = pending_summary.pop("delivery", {})
    edit_time = _issue_edit_time(issue)
    comment_id = None
    extra = {}
    suggestion = None
    publish_comment = pending_summary.pop("publish_comment", False)
    resume = (pending is not None and pending["content_hash"] == digest
              and publish_comment)
    if resume:
        # 使用已保存的发布决定；旧记录缺少决定时重新分析。
        summary = pending_summary
        comment_id = delivery.get("comment_id")
        suggestion = delivery.get("maintainer_suggestion")
        edit_time = delivery.get("issue_updated_at", edit_time)
        body, marker = pending["body"], pending["marker"]
        published = client.has_comment(repository, number, marker)
    else:
        # 检查是否有已完成的评论或者待确认发送的评论记录
        has_checked_comment = checked is not None and bool(checked["comment_marker"])
        has_pending_comment = pending is not None and bool(publish_comment)
        previously_commented = has_checked_comment or has_pending_comment

        template_files = client.templates(repository)
        recommend_with = {}
        if config.llm_failover:
            recommend_with = {
                "agent_api_keys": config.agent_api_keys or ((config.agent_api_key,) if config.agent_api_key else ()),
                "llm_key_retries": config.llm_key_retries,
                "llm_key_retry_interval": config.llm_key_retry_interval,
            }
        analysis_result = analyze_issue(
            issue["title"], issue.get("body") or "", template_files,
            config.agent_api_key, config.llm_model, config.llm_base_url,
            repository=repository, number=number, maintainers=config.maintainers.get(repository, ()),
            previously_commented=previously_commented,
            checks=checks,
            **recommend_with)
        summary = analysis_result["summary"]
        publish_comment = analysis_result["publish_comment"]
        suggestion = analysis_result["maintainer_suggestion"]
        if pending is not None and pending["content_hash"] == digest and publish_comment:
            # 旧记录重新分析后仍需评论时，先核对原标记，避免响应丢失导致重复发布。
            marker = pending["marker"]
            published = client.has_comment(repository, number, marker)
        else:
            marker = f"<!-- issue-gov:{secrets.token_hex(24)} -->"
            published = False
        body = analysis_result["comment"] + "\n\n" + marker

    if not resume and publish_comment and not published and edit_time is not None:
        # 未完成的上一版可能已写入平台，先找它，再找最近完成检查的评论。
        candidates = []
        if pending is not None:
            candidates.append((pending["marker"], delivery.get("issue_updated_at")))
        if checked is not None:
            previous_time = (delivery.get("issue_updated_at") if delivery.get("comment_id") is not None
                             else checked["issue_updated_at"])
            candidates.append((checked["comment_marker"], previous_time))
        for previous_marker, previous_time in candidates:
            if (previous_marker and previous_time is not None
                    and 0 <= edit_time - previous_time <= COMMENT_EDIT_SECONDS):
                comment = client.find_comment(repository, number, previous_marker)
                if comment is not None:
                    comment_id = comment.get("id")
                    if type(comment_id) is not int or comment_id <= 0:
                        raise IssueProcessingError("评论 ID 格式不正确")
                    break

    # 写库或发布前复查，避免对已关闭或已变化的 Issue 保存过时结果。
    latest = client.request("GET", path)
    if latest.get("state") == "closed":
        action = "skipped_closed"
        extra["label_action"] = "skipped"
    elif issue_content_hash(latest) != digest:
        raise IssueProcessingError("检查期间 Issue 内容已修改，将重新读取并检查")
    else:
        if not publish_comment:
            action = "suppressed"
        elif published:
            action = "recovered"
        else:
            with connect(config) as db:
                db.execute("""INSERT INTO pending_comments VALUES (?, ?, ?, ?, ?, ?)
                              ON CONFLICT(repository, issue_number) DO UPDATE SET
                              content_hash=excluded.content_hash, marker=excluded.marker,
                              body=excluded.body, summary=excluded.summary""",
                           (repository, number, digest, marker, body,
                            json.dumps({**summary, "publish_comment": publish_comment,
                                        "delivery": {"comment_id": comment_id, "issue_updated_at": edit_time,
                                                     "maintainer_suggestion": suggestion}}, ensure_ascii=False)))
            action = client.update_comment(repository, comment_id, body) if comment_id is not None else "missing"
            if action == "missing":
                action = client.create_comment(repository, number, body) if publish_comment else "suppressed"
        saved_marker = marker if action != "suppressed" else checked["comment_marker"] if checked is not None else None
        saved_time = checked["issue_updated_at"] if action == "suppressed" and checked is not None else edit_time
        with connect(config) as db:
            if suggestion is not None:
                extra["maintainer_suggestion_id"] = _save_maintainer_suggestion(
                    db, repository, number, suggestion)
            db.execute("""INSERT INTO issue_checks
                          (repository, issue_number, content_hash, comment_marker, issue_updated_at)
                          VALUES (?, ?, ?, ?, ?) ON CONFLICT(repository, issue_number)
                          DO UPDATE SET content_hash=excluded.content_hash, comment_marker=excluded.comment_marker,
                                        issue_updated_at=excluded.issue_updated_at""",
                       (repository, number, digest, saved_marker, saved_time))
            db.execute("DELETE FROM pending_comments WHERE repository = ? AND issue_number = ?",
                       (repository, number))
        extra.update(_assign_check_label(client, repository, number, summary))
    return {**summary, **extra, "comment_action": action}


def process_one(
    config: Config, now: float | None = None, *, client: AtomGit,
) -> bool:
    """领取并处理一个到期事件，保存成功结果或安排失败重试。

    文件锁覆盖整个处理过程，同一数据库只允许一个治理任务执行，防止长任务被重复领取。
    锁文件与数据库一起放在本地磁盘，文件关闭或进程退出时由系统释放锁。

    Args:
        config: 接收端与后台进程共用的服务配置。
        now: 本轮处理使用的 Unix 时间戳；为 None 时读取当前时间，可注入以验证重试行为。
        client: 由 run_worker 创建的共用客户端，原样传给 _process_issue。
    Returns:
        本轮领取并尝试处理了一个事件时返回 True；无到期任务、仍在限流等待期
        或未取得文件锁时返回 False。
        返回 True 不保证处理成功，具体结果以队列状态和日志为准。
    """

    # 领取事件前先尝试获取文件锁，防止多个进程同时处理同一数据库
    fd = os.open(str(Path(config.database).resolve()) + ".worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fixed_now = now

        now = time.time() if now is None else now

        # 从数据库领取一条任务，并标记为“正在处理”
        with connect(config) as db:
            db.execute("BEGIN IMMEDIATE")
            # 已取得覆盖整个处理过程的文件锁，遗留 running 任务不可能仍由其他进程执行。
            # 退回未完成的这一次尝试；同时恢复旧版本因中断次数耗尽而标记失败的任务。
            db.execute("""UPDATE events SET state = 'queued',
                          attempts = CASE WHEN events.attempts - 1 > 0 THEN events.attempts - 1 ELSE 0 END,
                          next_attempt = 0, last_error = '后台处理中断，已重新排队'
                          WHERE state = 'running'
                             OR (state = 'failed' AND last_error = '后台处理超时或中断')""")
            cooldown = db.execute("SELECT until FROM api_cooldown WHERE name = 'atomgit'").fetchone()
            if cooldown is not None and cooldown["until"] > now:
                return False
            # 领取一条待处理事件
            row = db.execute("""SELECT e.* FROM events e WHERE e.state = 'queued' AND e.next_attempt <= ?
                                AND NOT EXISTS (SELECT 1 FROM events waiting
                                    WHERE waiting.repository = e.repository AND waiting.issue_number = e.issue_number
                                      AND waiting.state = 'queued' AND waiting.next_attempt > ?)
                                ORDER BY e.updated_at LIMIT 1""", (now, now)).fetchone()
            if row is None:
                return False
            # 领取尚需处理的请求，沿用已有重试预算。
            attempt = db.execute("""SELECT MAX(attempts) FROM events WHERE repository = ?
                                    AND issue_number = ? AND state = 'queued'""",
                                 (row["repository"], row["issue_number"])).fetchone()[0] + 1
            db.execute("""UPDATE events SET state = 'running', attempts = ?, updated_at = ?, owner = NULL,
                          handoff = 0
                          WHERE repository = ? AND issue_number = ? AND state = 'queued'""",
                       (attempt, now, row["repository"], row["issue_number"]))

        # 执行处理和评论
        try:
            # sqlite3.Row 和 PostgreSQL Row 都支持 keys()；老库可能没有 checks 列。
            stored_checks = row["checks"] if "checks" in row.keys() else None  # noqa: SIM118
            result = _process_issue(config, row["repository"], row["issue_number"], client=client,
                                    checks=stored_checks)
            if not isinstance(result, dict) or not result.get("comment_action"):
                raise IssueProcessingError("治理程序未返回有效处理结果")
        except Exception as error:
            # 未预期错误也需要有限重试，不能因反复崩溃跳过失败上限；不记录异常原文。
            exc = error if isinstance(error, IssueProcessingError) else IssueProcessingError("治理程序发生内部错误，请检查代码和配置")
            now = time.time() if fixed_now is None else fixed_now
            state, retry_delay = save_failure(config, row["event_id"], attempt, exc, now)
            _log_failure(row["repository"], row["issue_number"], attempt, exc, state, retry_delay,
                         row["event_id"])
        else:
            now = time.time() if fixed_now is None else fixed_now
            save_success(config, row["event_id"], attempt, result, now)
            _log_success(row["repository"], row["issue_number"], result, row["event_id"])
        return True


def run_worker(config: Config) -> None:
    """持续处理已保存的任务，由 Supervisor 在独立后台进程中调用。

    Args:
        config: 接收进程与任务处理进程共用的配置。
    Returns:
        正常情况下持续循环、不返回；收到进程终止信号或发生未处理异常时结束运行。
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    def stop_worker(signum, frame) -> None:
        """响应停止信号，退出处理并执行模型子进程清理。

        Args:
            signum: 操作系统信号编号。
            frame: 收到信号时的执行栈帧。
        Returns:
            不返回；抛出 SystemExit，遗留任务由下一进程取得锁后恢复。
        """
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)

    # 确保数据库和表已创建
    initialize(config)
    # 创建客户端，复用缓存和请求间隔记录
    client = AtomGit(config.token)
    LOG.info("后台处理服务已启动，正在等待任务：接入仓库=%s", ",".join(config.repositories))

    # 只处理已收到的 Webhook 通知，不主动扫描仓库历史 Issue。
    while True:
        if not process_one(config, client=client):
            time.sleep(1)


def retry_failed(config: Config) -> int:
    """将所有失败任务重新安排为待处理；由 manage.py 在修复问题后调用。

    Args:
        config: 指向服务任务数据库的配置。
    Returns:
        重新安排的任务数量；不会自行执行检查，需要后台进程继续处理。
    """
    initialize(config)
    with connect(config) as db:
        count = db.execute("""UPDATE events SET state = 'queued', attempts = 0, dead_lettered = 0,
                              next_attempt = 0, updated_at = ?, owner = NULL WHERE state = 'failed'""",
                           (time.time(),)).rowcount
    if count and config.rabbitmq_url:
        # 上次失败时进入死信队列的消息已对应到被重排的任务；不清掉会在下次失败时重复堆积。
        purge_dead_letters(config.rabbitmq_url, ISSUE_ANALYSIS)
        republish_due(config)
    return count


def purge_dead_letters(url: str, topology) -> int:
    """清空死信队列并返回清掉的消息数。队列不可达时记录日志并返回 0，不中断重排。"""
    from common.queueing import QueuePublishError, purge_queue

    try:
        return purge_queue(url, topology)
    except QueuePublishError:
        LOG.error("死信队列不可达，未能清空；不影响失败任务重新入队")
        return 0


def _holder_clause(owner: str | None) -> tuple[str, tuple]:
    """队列路径只允许当前持有者写回。不传 owner 时是 SQLite 路径，由文件锁保证唯一。

    持有者为空、或标识与当前行不一致时，更新匹配 0 行。已被新通知覆盖的行仍可由原持有者写回结果，
    状态保持 superseded。
    """
    if owner is None:
        return "", ()
    return " AND owner = ?", (owner,)


def save_success(config: Config, event_id: str, attempt: int, result: dict, now: float,
                 *, owner: str | None = None) -> bool:
    """保存处理结果，并写入报告表。已被新请求覆盖的任务保留 superseded。

    owner 是消费者领取任务时写入的持有者标识；给出时只有仍是持有者的写入才生效，
    防止失联的旧消费者在任务被接管后覆盖结果。SQLite 路径由文件锁保证唯一，不传。

    Returns:
        结果已写入为 True；任务已被其他消费者接管、本次写入被拒绝时为 False。
    """
    payload = json.dumps(result, ensure_ascii=False)
    guard, extra = _holder_clause(owner)
    with connect(config) as db:
        updated = db.execute(f"""UPDATE events SET state = CASE WHEN state='superseded' THEN state ELSE 'completed' END,
                      result = ?, last_error = NULL, updated_at = ?, owner = NULL
                      WHERE event_id = ? AND attempts = ? AND state IN ('running', 'superseded'){guard}""",
                   (payload, now, event_id, attempt, *extra))
        if updated.rowcount == 0:
            # 任务已被其他消费者接管，不写报告，避免覆盖接管者的结果。
            return False
        db.execute("""INSERT INTO reports (event_id, repository, issue_number, status,
                      recommendation_status, comment_action, checks, result, created_at)
                      SELECT event_id, repository, issue_number, ?, ?, ?, checks, ?, ?
                      FROM events WHERE event_id = ?
                      ON CONFLICT(event_id) DO UPDATE SET
                        status=excluded.status,
                        recommendation_status=excluded.recommendation_status,
                        comment_action=excluded.comment_action,
                        checks=excluded.checks,
                        result=excluded.result,
                        created_at=excluded.created_at,
                        repository=excluded.repository,
                        issue_number=excluded.issue_number""",
                   (result.get("status"), result.get("recommendation_status"),
                    result.get("comment_action"), payload, now, event_id))
    return True


def save_failure(config: Config, event_id: str, attempt: int, exc: IssueProcessingError, now: float,
                 *, owner: str | None = None) -> tuple[str, float]:
    """记录失败、安排退避或标记最终失败。全部密钥失败时同时标上死信。

    owner 是消费者领取任务时写入的持有者标识；给出时只有仍是持有者的写入才生效。
    标识为空、与当前行不一致，或更新落空时返回 taken_over，不再安排重试或写报告。
    """
    mark_dead = bool(exc.keys_exhausted)
    state = "queued" if exc.retryable and not mark_dead and attempt < MAX_ATTEMPTS else "failed"
    retry_delay = min(30 * 2 ** (attempt - 1), 300)
    if exc.status_code == 429:
        retry_delay = max(retry_delay, math.ceil(exc.retry_after))
    with connect(config) as db:
        current = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if current is None:
            return "missing", retry_delay
        # 给出持有者时，标识必须与当前行一致。空标识和他人标识都不能写回，已覆盖的行也一样。
        holder = current["owner"] if "owner" in current.keys() else None  # noqa: SIM118
        if owner is not None and holder != owner:
            return "taken_over", retry_delay
        guard, extra = _holder_clause(owner)
        if exc.status_code == 429:
            # PostgreSQL 的 MAX() 只是聚合函数；跨方言用 CASE 比较取较大值。
            db.execute("""INSERT INTO api_cooldown(name, until) VALUES ('atomgit', ?)
                          ON CONFLICT(name) DO UPDATE SET until =
                              CASE WHEN excluded.until > api_cooldown.until
                                   THEN excluded.until ELSE api_cooldown.until END""",
                       (now + retry_delay,))
        updated = db.execute(f"""UPDATE events SET
                        state = CASE WHEN state='superseded' THEN state ELSE ? END,
                        next_attempt = ?, last_error = ?, updated_at = ?, owner = NULL,
                        dead_lettered = CASE WHEN state='superseded' THEN dead_lettered
                                             WHEN ? = 1 THEN 1 ELSE dead_lettered END
                      WHERE event_id = ? AND attempts = ? AND state IN ('running', 'superseded'){guard}""",
                   (state, now + retry_delay, str(exc), now, 1 if mark_dead else 0, event_id, attempt, *extra))
        if owner is not None and updated.rowcount == 0:
            return "taken_over", retry_delay
        current = db.execute("SELECT state FROM events WHERE event_id=?", (event_id,)).fetchone()[0]
        if current == "failed":
            db.execute("""INSERT INTO reports (event_id, repository, issue_number, status,
                          recommendation_status, comment_action, checks, result, created_at)
                          SELECT event_id, repository, issue_number, 'failed', NULL, NULL, checks, ?, ?
                          FROM events WHERE event_id = ?
                          ON CONFLICT(event_id) DO UPDATE SET
                            status=excluded.status,
                            recommendation_status=excluded.recommendation_status,
                            comment_action=excluded.comment_action,
                            checks=excluded.checks,
                            result=excluded.result, created_at=excluded.created_at""",
                       (json.dumps({"error": str(exc), "dead_lettered": 1 if mark_dead else 0},
                                   ensure_ascii=False), now, event_id))
    return current, retry_delay


def _log_failure(repository: str, number: int, attempt: int, exc: IssueProcessingError,
                 state: str, retry_delay: float, event_id: str) -> None:
    """沿用原来的失败日志，不记录令牌或响应原文。"""
    if exc.status_code == 429:
        LOG.warning("AtomGit API 已限流，全部治理任务暂停请求 %s 秒；Webhook 继续接收并保存通知", retry_delay)
    if state == "superseded":
        LOG.info("旧请求已被新请求覆盖：仓库=%s，Issue=#%s，停止本次处理，由新请求接替，原因=%s，事件=%s",
                 repository, number, exc, event_id[:12])
        return
    outcome = (f"将在 {retry_delay} 秒后自动重试" if state == "queued"
               else "任务已失败，停止自动重试，请排查原因")
    if exc.keys_exhausted:
        outcome = "全部模型密钥均失败，任务已进入死信队列"
    LOG.warning("治理处理失败：仓库=%s，Issue=#%s，第 %s/%s 次尝试，%s，原因=%s，事件=%s",
                repository, number, attempt, MAX_ATTEMPTS, outcome, exc, event_id[:12])


def _log_success(repository: str, number: int, result: dict, event_id: str) -> None:
    """沿用原来的完成日志。"""
    statuses = {"pass": "基础格式通过", "partial_match": "格式待补充", "no_match": "未匹配模板",
                "template_error": "模板配置异常，维护者建议已入库", "skipped_closed": "Issue 已关闭",
                "skipped_unchanged": "标题和正文已检查，跳过重复处理"}
    actions = {"created": "已发布评论", "recovered": "已找到先前发布的评论，恢复完成状态", "unchanged": "未重复发布评论",
               "updated": "已更新检查评论",
               "suppressed": "静默处理，未发表评论",
               "skipped_closed": "跳过已关闭的 Issue"}
    label_actions = {"assigned": "已写入", "rejected": "未写入", "skipped": "未分配"}
    if result.get("maintainer_suggestion_id") is not None:
        statuses["no_match"] = "检查通过，模板覆盖建议已入库"
    label_text = label_actions.get(result.get("label_action"), "未分配")
    if result.get("label"):
        label_text += " " + result["label"]
    LOG.info("治理处理完成：仓库=%s，Issue=#%s，检查=%s，评论=%s，标签=%s，事件=%s",
             repository, number, statuses.get(result.get("status"), "已处理"),
             actions.get(result["comment_action"], "已处理"), label_text, event_id[:12])


def handle_delivery(config: Config, payload: dict, redelivered: bool, *, client: AtomGit) -> str:
    """处理一条队列消息。返回 ack、nack 或 requeue。

    nack 只用于全部模型密钥失败，消息由交换机转入死信队列。
    可重试错误写回数据库后确认当前消息，由重试线程按到期时间重新投递。
    """
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        LOG.warning("丢弃缺少事件编号的队列消息")
        return "ack"
    now = time.time()
    with connect(config) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            return "ack" if redelivered else "requeue"
        state = row["state"]
        repository, number = row["repository"], row["issue_number"]
        if state in ("completed", "superseded"):
            return "ack"
        if state == "failed":
            return "nack" if row["dead_lettered"] else "ack"
        cooldown = db.execute("SELECT until FROM api_cooldown WHERE name = 'atomgit'").fetchone()
        if cooldown is not None and cooldown["until"] > now and state in ("queued", "dispatching"):
            # 只推迟尚未开始处理的任务；running 行由正在执行的消费者持有，改回 queued 会让它的完成写入落空。
            db.execute("""UPDATE events SET state='queued', next_attempt=?
                          WHERE event_id=? AND state IN ('queued', 'dispatching')""",
                       (max(cooldown["until"], row["next_attempt"]), event_id))
            return "ack"
        if state == "queued" and row["next_attempt"] > now:
            return "ack"
        if state == "running" and not redelivered:
            return "ack"
        owner = secrets.token_hex(8)
        if state == "running" and redelivered:
            # 同一次 running 只接管一次。不按刚读到的 owner 交换：先提交的接管会写下新标识，
            # 后到的消费者再读一次就能再次匹配。handoff 从 0 改为 1 后，其余重投都落空。
            attempt = int(row["attempts"])
            updated = db.execute("""UPDATE events SET updated_at=?, owner=?, handoff=1
                                    WHERE event_id=? AND state='running' AND handoff=0""",
                                 (now, owner, event_id))
            if updated.rowcount == 0:
                return "ack"
        else:
            attempt = int(row["attempts"]) + 1
            updated = db.execute("""UPDATE events SET state='running', attempts=?, updated_at=?, owner=?, handoff=0
                                    WHERE event_id=? AND state IN ('queued', 'dispatching')""",
                                 (attempt, now, owner, event_id))
            if updated.rowcount == 0:
                return "ack"

    try:
        result = _process_issue(config, repository, number, client=client, checks=payload.get("checks"))
        if not isinstance(result, dict) or not result.get("comment_action"):
            raise IssueProcessingError("治理程序未返回有效处理结果")
    except Exception as error:
        exc = error if isinstance(error, IssueProcessingError) else IssueProcessingError("治理程序发生内部错误，请检查代码和配置")
        final, retry_delay = save_failure(config, event_id, attempt, exc, time.time(), owner=owner)
        if final == "taken_over":
            # 任务已被其他消费者接管：本次失败未写入，不按失败记录日志。
            LOG.info("任务已被其他消费者接管，本次失败不再记录：仓库=%s，Issue=#%s，事件=%s",
                     repository, number, event_id[:12])
            return "ack"
        _log_failure(repository, number, attempt, exc, final, retry_delay, event_id)
        if final == "superseded":
            return "ack"
        if exc.keys_exhausted and final == "failed":
            return "nack"
        return "ack"
    if not save_success(config, event_id, attempt, result, time.time(), owner=owner):
        LOG.info("任务已被其他消费者接管，本次结果不写入：仓库=%s，Issue=#%s，事件=%s",
                 repository, number, event_id[:12])
        return "ack"
    _log_success(repository, number, result, event_id)
    return "ack"


def republish_due(config: Config) -> int:
    """把到期的重试任务重新投递到分析队列。仅服务模式使用。"""
    if not config.database_url or not config.rabbitmq_url:
        return 0
    from common.pgstore import claim_due
    from common.queueing import QueuePublishError, publish_analysis_task
    from issue_gov.checks.base import default_checks, parse_checks

    try:
        rows = claim_due(config.database_url, time.time())
    except Exception:
        LOG.exception("读取到期任务失败")
        return 0
    published = 0
    for row in rows:
        try:
            try:
                tasks = [item.to_dict() for item in parse_checks(row.get("checks"))]
            except ValueError:
                tasks = default_checks()
            publish_analysis_task(
                config.rabbitmq_url, row["event_id"], row["repository"], int(row["issue_number"]),
                tasks, ISSUE_ANALYSIS,
            )
            published += 1
        except QueuePublishError:
            with connect(config) as db:
                db.execute("UPDATE events SET state='queued' WHERE event_id=? AND state='dispatching'",
                           (row["event_id"],))
            LOG.error("重试任务投递失败：事件=%s", str(row["event_id"])[:12])
            break
    return published


def _serve_health(stop: threading.Event) -> None:
    """提供消费者存活检查。端口被占用时只记录日志，不阻止处理任务。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port_text = os.environ.get("CONSUMER_HEALTH_PORT", "7001").strip() or "7001"
    if not port_text.isascii() or not port_text.isdecimal():
        return
    port = int(port_text)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            return

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError:
        LOG.warning("消费者健康检查端口 %s 不可用，跳过", port)
        return
    server.timeout = 1
    while not stop.is_set():
        server.handle_request()
    server.server_close()


def run_queue_consumer(config: Config) -> None:
    """按竞争消费者处理队列中的分析任务，直到收到停止信号。"""
    from common.queueing import consume

    keys = tuple(key for key in config.agent_api_keys if key)
    if not keys and config.agent_api_key:
        keys = (config.agent_api_key,)
    config = replace(config, llm_failover=True, agent_api_keys=keys)
    initialize(config)
    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        LOG.info("收到停止信号，当前任务结束后退出")
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    threading.Thread(target=_serve_health, args=(stop,), name="issue-gov-health", daemon=True).start()
    threading.Thread(target=_pump, args=(config, stop), name="issue-gov-retry", daemon=True).start()
    client = AtomGit(config.token)
    LOG.info("队列消费者已启动，正在等待任务：接入仓库=%s", ",".join(config.repositories))

    def handler(payload: dict, redelivered: bool) -> str:
        return handle_delivery(config, payload, redelivered, client=client)

    while not stop.is_set():
        try:
            consume(config.rabbitmq_url, handler, stop, ISSUE_ANALYSIS)
        except Exception:
            if stop.is_set():
                break
            LOG.exception("队列连接中断，5 秒后重连")
            stop.wait(5)


def _pump(config: Config, stop: threading.Event) -> None:
    """周期性投递到期重试任务。"""
    while not stop.is_set():
        republish_due(config)
        stop.wait(1)


def main() -> None:
    """容器入口。服务模式消费队列；否则沿用原来的数据库轮询循环。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    config = Config.from_file()
    if len(sys.argv) > 1 and sys.argv[1] == "--retry-failed":
        print(json.dumps({"requeued": retry_failed(config)}, ensure_ascii=False))
        return
    if config.rabbitmq_url:
        run_queue_consumer(config)
    else:
        run_worker(config)


if __name__ == "__main__":
    main()
