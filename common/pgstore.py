"""PostgreSQL 存储。SQLite 与这里的切换在 store.py。"""

from contextlib import contextmanager
from typing import Iterator

# 固定锁编号，避免多个接收进程同时建表。
SCHEMA_LOCK = 8675309
# 重试线程领走一批 queued 后、代理确认前，只推后 next_attempt，不改成 dispatching。
PUBLISH_LEASE = 30


class DatabaseError(Exception):
    """数据库不可用或写入失败。调用方据此返回 503，不把连接串写入日志。"""


class Row(dict):
    """同时支持列名和下标，兼容现有 sqlite3.Row 用法。"""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


class Result:
    """模拟 sqlite 游标上实际用到的 fetchone、rowcount 和 lastrowid。"""

    def __init__(self, rows: list[Row], rowcount: int, lastrowid: int | None = None) -> None:
        self._rows = rows
        self._index = 0
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self) -> Row | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self) -> list[Row]:
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows


def _placeholders(sql: str) -> str:
    """把 SQLite 占位符换成 psycopg 占位符，不改写字符串字面量。"""
    output: list[str] = []
    quoted = False
    for char in sql:
        if char == "'":
            quoted = not quoted
            output.append(char)
        elif char == "?" and not quoted:
            output.append("%s")
        else:
            output.append(char)
    return "".join(output)


class PgConnection:
    """在 PostgreSQL 上执行与 SQLite 相同的语句。"""

    def __init__(self, connection) -> None:
        self._connection = connection

    def lock_issue(self, repository: str, number: int) -> None:
        """按 Issue 加事务级咨询锁，见模块级 lock_issue。"""
        lock_issue(self, repository, number)

    def execute(self, sql: str, params: tuple | list = ()) -> Result:
        statement = sql.strip()
        if statement.upper().startswith("BEGIN"):
            return Result([], 0)
        translated = _placeholders(sql)
        returning = False
        if "INTO maintainer_suggestions" in translated and "RETURNING" not in translated.upper():
            translated = translated.rstrip().rstrip(";") + " RETURNING id"
            returning = True
        try:
            cursor = self._connection.execute(translated, params)
        except Exception as exc:
            if type(exc).__module__.startswith("psycopg"):
                raise DatabaseError("数据库写入失败") from None
            raise
        lastrowid = None
        rows: list[Row] = []
        if returning:
            fetched = cursor.fetchone()
            if fetched:
                lastrowid = fetched["id"] if isinstance(fetched, dict) else fetched[0]
        elif cursor.description:
            rows = [Row(row) for row in cursor.fetchall()]
        return Result(rows, cursor.rowcount, lastrowid)


@contextmanager
def connect(database_url: str) -> Iterator[PgConnection]:
    """打开一次 PostgreSQL 事务，退出时提交或回滚。

    会话使用 READ COMMITTED：业务语句都靠 ``UPDATE ... WHERE state = ...`` 做原子状态迁移，
    接收端入队时用按 Issue 的事务级咨询锁（lock_issue）代替 SQLite 的 BEGIN IMMEDIATE。
    """
    import psycopg
    from psycopg.rows import dict_row

    try:
        raw = psycopg.connect(database_url, row_factory=dict_row, connect_timeout=5)
    except psycopg.Error:
        raise DatabaseError("数据库不可用") from None
    wrapper = PgConnection(raw)
    try:
        yield wrapper
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def lock_issue(db, repository: str, number: int) -> None:
    """按 Issue 加事务级咨询锁，使同一 Issue 的入队串行执行；调用方在同一事务内继续写入。

    PostgreSQL 没有 BEGIN IMMEDIATE。行锁在第一条通知到来时无行可锁，
    因此用 (repository, issue_number) 派生的键做事务级咨询锁：事务结束自动释放，
    不同 Issue 之间互不阻塞。SQLite 路径不需要这一步，由 BEGIN IMMEDIATE 覆盖。
    """
    key = f"{repository}#{number}"
    db.execute("SELECT pg_advisory_xact_lock(hashtextextended(?, 0))", (key,))


def initialize(database_url: str) -> None:
    """创建服务化部署使用的表和索引。可以重复调用。"""
    import psycopg

    try:
        raw = psycopg.connect(database_url, connect_timeout=5, autocommit=True)
    except psycopg.Error:
        raise DatabaseError("数据库不可用") from None
    try:
        raw.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK,))
        try:
            _schema(raw)
        finally:
            raw.execute("SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK,))
    except psycopg.Error:
        raise DatabaseError("数据库结构初始化失败") from None
    finally:
        raw.close()


def _schema(connection) -> None:
    """写入当前版本的表结构。语句集中在 schema 模块。"""
    from .schema import apply_postgres
    apply_postgres(connection)


def claim_due(database_url: str, now: float, *, limit: int = 20) -> list[dict]:
    """领取到期的 queued 任务，准备发布。

    只领取 state 为 queued 且 next_attempt 已到的行。领取时把 next_attempt 推后一个发布租约，
    状态保持 queued。dispatching 要等代理确认之后才写，这样发布失败或进程退出都还能再领。
    正在 running 或已经 dispatching 的任务不在这里。

    Args:
        database_url: PostgreSQL 连接串。
        now: 当前 Unix 时间戳。
        limit: 单次最多领取的任务数。
    Returns:
        已加上发布租约、等待发布的任务。next_attempt 是这次租约的到期时间。
    """
    import psycopg
    from psycopg.rows import dict_row

    lease = now + PUBLISH_LEASE
    claim = """WITH due AS (
                   SELECT event_id FROM events
                   WHERE state = 'queued' AND next_attempt <= %s
                   ORDER BY updated_at
                   FOR UPDATE SKIP LOCKED
                   LIMIT %s
               )
               UPDATE events SET next_attempt = %s, updated_at = %s
               FROM due WHERE events.event_id = due.event_id
               RETURNING events.event_id, events.repository, events.issue_number,
                         events.checks, events.next_attempt"""
    try:
        with psycopg.connect(database_url, row_factory=dict_row, connect_timeout=5) as raw:
            with raw.transaction():
                rows = raw.execute(claim, (now, limit, lease, now)).fetchall()
    except psycopg.Error:
        raise DatabaseError("数据库不可用") from None
    return [dict(row) for row in rows]
