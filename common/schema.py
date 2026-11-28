"""事件库表结构。SQLite 与 PostgreSQL 只在这里各写一份，调用方不再内嵌建表语句。"""

DEFAULT_CHECKS = '[{"kind":"compliance","parameters":{}}]'


def apply_sqlite(db) -> None:
    """创建或补齐本机 SQLite 的表和索引。调用方负责事务。"""
    db.execute(f"""CREATE TABLE IF NOT EXISTS events (
        event_id TEXT PRIMARY KEY, repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
        last_error TEXT, result TEXT, dead_lettered INTEGER NOT NULL DEFAULT 0,
        checks TEXT NOT NULL DEFAULT '{DEFAULT_CHECKS}'
    )""")
    event_columns = {row["name"] for row in db.execute("PRAGMA table_info(events)")}
    if "dead_lettered" not in event_columns:
        db.execute("ALTER TABLE events ADD COLUMN dead_lettered INTEGER NOT NULL DEFAULT 0")
    if "checks" not in event_columns:
        db.execute(f"""ALTER TABLE events ADD COLUMN checks TEXT NOT NULL DEFAULT '{DEFAULT_CHECKS}'""")
    db.execute("CREATE INDEX IF NOT EXISTS events_queue ON events(state, next_attempt)")
    db.execute("CREATE INDEX IF NOT EXISTS events_issue ON events(repository, issue_number, state)")
    # 消费者领取任务时写入的持有者标识；进程重启或消息重投后，旧持有者的完成写入会被拒绝。
    if "owner" not in event_columns:
        db.execute("ALTER TABLE events ADD COLUMN owner TEXT")
    # 同一次 running 只允许消息重投接管一次。新领取时回到 0。
    if "handoff" not in event_columns:
        db.execute("ALTER TABLE events ADD COLUMN handoff INTEGER NOT NULL DEFAULT 0")
    # 兜底接收端在覆盖旧任务与插入新任务之间崩溃的情况：同一 Issue 存在更新的行时，旧行标为已覆盖。
    # 只在启动时跑一次，由 BEGIN IMMEDIATE（SQLite）或 pg_advisory_lock（PostgreSQL）串行化。
    db.execute("""UPDATE events SET state='superseded'
                  WHERE state IN ('queued', 'running', 'failed', 'dispatching') AND EXISTS (
                      SELECT 1 FROM events newer WHERE newer.repository=events.repository
                        AND newer.issue_number=events.issue_number AND newer.rowid>events.rowid
                  )""")
    db.execute("CREATE TABLE IF NOT EXISTS api_cooldown (name TEXT PRIMARY KEY, until REAL NOT NULL)")
    db.execute("""CREATE TABLE IF NOT EXISTS issue_checks (
        repository TEXT NOT NULL, issue_number INTEGER NOT NULL, content_hash TEXT NOT NULL,
        PRIMARY KEY (repository, issue_number)
    )""")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(issue_checks)")}
    for name, kind in (("comment_marker", "TEXT"), ("issue_updated_at", "REAL"),
                       ("assessment_snapshot", "TEXT")):
        if name not in columns:
            db.execute(f"ALTER TABLE issue_checks ADD COLUMN {name} {kind}")
    db.execute("""CREATE TABLE IF NOT EXISTS pending_comments (
        repository TEXT NOT NULL, issue_number INTEGER NOT NULL, content_hash TEXT NOT NULL,
        marker TEXT NOT NULL, body TEXT NOT NULL, summary TEXT NOT NULL,
        PRIMARY KEY (repository, issue_number)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS maintainer_suggestions (
        id INTEGER PRIMARY KEY,
        repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
        created_at TEXT NOT NULL, reason TEXT NOT NULL, suggestion TEXT NOT NULL, mtr TEXT NOT NULL
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS maintainer_suggestions_date ON maintainer_suggestions(created_at)")
    db.execute("""CREATE TABLE IF NOT EXISTS reports (
        event_id TEXT PRIMARY KEY, repository TEXT NOT NULL, issue_number INTEGER NOT NULL,
        status TEXT, recommendation_status TEXT, comment_action TEXT,
        checks TEXT, result TEXT NOT NULL, created_at REAL NOT NULL
    )""")
    report_columns = {row["name"] for row in db.execute("PRAGMA table_info(reports)")}
    if "checks" not in report_columns:
        db.execute("ALTER TABLE reports ADD COLUMN checks TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS reports_issue ON reports(repository, issue_number)")


def apply_postgres(connection) -> None:
    """创建或补齐 PostgreSQL 的表和索引。可以重复调用。"""
    connection.execute(f"""CREATE TABLE IF NOT EXISTS events (
        id BIGSERIAL PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        repository TEXT NOT NULL,
        issue_number BIGINT NOT NULL,
        state TEXT NOT NULL DEFAULT 'queued',
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
        updated_at DOUBLE PRECISION NOT NULL,
        last_error TEXT,
        result TEXT,
        dead_lettered INTEGER NOT NULL DEFAULT 0,
        checks TEXT NOT NULL DEFAULT '{DEFAULT_CHECKS}'
    )""")
    connection.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS dead_lettered INTEGER NOT NULL DEFAULT 0")
    connection.execute(f"""ALTER TABLE events ADD COLUMN IF NOT EXISTS checks TEXT
                          NOT NULL DEFAULT '{DEFAULT_CHECKS}'""")
    # 消费者领取任务时写入的持有者标识，见 apply_sqlite 中的同名列。
    connection.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS owner TEXT")
    connection.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS handoff INTEGER NOT NULL DEFAULT 0")
    connection.execute("CREATE INDEX IF NOT EXISTS events_queue ON events(state, next_attempt)")
    connection.execute("CREATE INDEX IF NOT EXISTS events_issue ON events(repository, issue_number, state)")
    # 与 apply_sqlite 相同：启动时兜底一次，覆盖接收端在覆盖旧任务后崩溃的情况。
    connection.execute("""UPDATE events SET state='superseded'
                          WHERE state IN ('queued', 'running', 'failed', 'dispatching') AND EXISTS (
                              SELECT 1 FROM events newer WHERE newer.repository=events.repository
                                AND newer.issue_number=events.issue_number AND newer.id>events.id
                          )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS api_cooldown (
        name TEXT PRIMARY KEY, until DOUBLE PRECISION NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS issue_checks (
        repository TEXT NOT NULL, issue_number BIGINT NOT NULL, content_hash TEXT NOT NULL,
        comment_marker TEXT, issue_updated_at DOUBLE PRECISION,
        PRIMARY KEY (repository, issue_number)
    )""")
    connection.execute("ALTER TABLE issue_checks ADD COLUMN IF NOT EXISTS comment_marker TEXT")
    connection.execute("ALTER TABLE issue_checks ADD COLUMN IF NOT EXISTS issue_updated_at DOUBLE PRECISION")
    connection.execute("ALTER TABLE issue_checks ADD COLUMN IF NOT EXISTS assessment_snapshot TEXT")
    connection.execute("""CREATE TABLE IF NOT EXISTS pending_comments (
        repository TEXT NOT NULL, issue_number BIGINT NOT NULL, content_hash TEXT NOT NULL,
        marker TEXT NOT NULL, body TEXT NOT NULL, summary TEXT NOT NULL,
        PRIMARY KEY (repository, issue_number)
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS maintainer_suggestions (
        id BIGSERIAL PRIMARY KEY,
        repository TEXT NOT NULL, issue_number BIGINT NOT NULL,
        created_at TEXT NOT NULL, reason TEXT NOT NULL, suggestion TEXT NOT NULL, mtr TEXT NOT NULL
    )""")
    connection.execute("CREATE INDEX IF NOT EXISTS maintainer_suggestions_date ON maintainer_suggestions(created_at)")
    connection.execute("""CREATE TABLE IF NOT EXISTS reports (
        event_id TEXT PRIMARY KEY,
        repository TEXT NOT NULL,
        issue_number BIGINT NOT NULL,
        status TEXT,
        recommendation_status TEXT,
        comment_action TEXT,
        checks TEXT,
        result TEXT NOT NULL,
        created_at DOUBLE PRECISION NOT NULL
    )""")
    connection.execute("ALTER TABLE reports ADD COLUMN IF NOT EXISTS checks TEXT")
    connection.execute("CREATE INDEX IF NOT EXISTS reports_issue ON reports(repository, issue_number)")
