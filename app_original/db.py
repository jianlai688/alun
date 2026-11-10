"""数据层：SQLite（默认）/ PostgreSQL（平台模式）双后端与通用查询助手。"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .config import DATABASE_URL, DB_BACKEND, DB_PATH

TZ = timezone(timedelta(hours=8))


def now() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS document (
    doc_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL DEFAULT 'other',
    source_uri TEXT DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT 'application/pdf',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'uploaded',
    doc_version INTEGER NOT NULL DEFAULT 1,
    language TEXT NOT NULL DEFAULT 'zh-CN',
    quality_score REAL NOT NULL DEFAULT 0,
    quality_level TEXT NOT NULL DEFAULT 'review',
    quality_issues TEXT NOT NULL DEFAULT '[]',
    stats TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_document_tenant ON document(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS node (
    node_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    parent_id TEXT,
    level INTEGER NOT NULL,
    node_type TEXT NOT NULL,
    order_no INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    page_start INTEGER NOT NULL DEFAULT 1,
    page_end INTEGER NOT NULL DEFAULT 1,
    bbox TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    path TEXT NOT NULL DEFAULT '/',
    char_count INTEGER NOT NULL DEFAULT 0,
    order_index INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_node_doc ON node(doc_id, parent_id, order_no);
CREATE INDEX IF NOT EXISTS idx_node_path ON node(doc_id, path);

CREATE TABLE IF NOT EXISTS chunk (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    order_no INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunk_node ON chunk(node_id);

CREATE TABLE IF NOT EXISTS knowledge_point (
    kp_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kp_type TEXT NOT NULL,
    definition TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,
    difficulty INTEGER NOT NULL DEFAULT 3,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    prerequisites TEXT NOT NULL DEFAULT '[]',
    order_no INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kp_doc ON knowledge_point(doc_id, node_id);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    page_no INTEGER NOT NULL DEFAULT 1,
    quote TEXT NOT NULL,
    char_start INTEGER NOT NULL DEFAULT 0,
    char_end INTEGER NOT NULL DEFAULT 0,
    strength TEXT NOT NULL DEFAULT 'strong',
    context TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_evidence_node ON evidence(node_id, page_no);

CREATE TABLE IF NOT EXISTS question (
    q_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    kp_id TEXT,
    node_id TEXT NOT NULL,
    q_type TEXT NOT NULL,
    bloom_level TEXT NOT NULL,
    difficulty INTEGER NOT NULL DEFAULT 3,
    stem TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]',
    answer TEXT NOT NULL DEFAULT '{}',
    rubric TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]',
    hints TEXT NOT NULL DEFAULT '[]',
    time_limit_sec INTEGER NOT NULL DEFAULT 90,
    hint_levels INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'llm',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_question_doc ON question(doc_id, status);

CREATE TABLE IF NOT EXISTS question_set (
    set_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'tnt_local',
    title TEXT NOT NULL,
    difficulty TEXT NOT NULL DEFAULT 'mixed',
    question_ids TEXT NOT NULL DEFAULT '[]',
    question_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_question_set_doc ON question_set(doc_id, created_at DESC);

CREATE TABLE IF NOT EXISTS session (
    session_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready',
    total_questions INTEGER NOT NULL DEFAULT 12,
    plan TEXT NOT NULL DEFAULT '{}',
    current_turn_id TEXT,
    score REAL,
    theta_before REAL NOT NULL DEFAULT 500,
    theta_after REAL NOT NULL DEFAULT 500,
    chapter_ability TEXT NOT NULL DEFAULT '{}',
    skipped_count INTEGER NOT NULL DEFAULT 0,
    hint_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turn (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    q_id TEXT NOT NULL,
    seq_no INTEGER NOT NULL,
    answer TEXT NOT NULL DEFAULT '{}',
    evaluation TEXT NOT NULL DEFAULT '{}',
    score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'asking',
    hint_level INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    latency_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turn_session ON turn(session_id, seq_no);

CREATE TABLE IF NOT EXISTS report (
    report_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}',
    section_mastery TEXT NOT NULL DEFAULT '[]',
    bloom_distribution TEXT NOT NULL DEFAULT '{}',
    wrong_questions TEXT NOT NULL DEFAULT '[]',
    strengths TEXT NOT NULL DEFAULT '[]',
    weaknesses TEXT NOT NULL DEFAULT '[]',
    recommendations TEXT NOT NULL DEFAULT '[]',
    generated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_session ON report(session_id);

CREATE TABLE IF NOT EXISTS task (
    task_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    document_id TEXT,
    session_id TEXT,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    credits REAL NOT NULL DEFAULT 0,
    document_id TEXT,
    session_id TEXT
);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_user (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    display_name TEXT NOT NULL DEFAULT '',
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT 'tnt_local',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS app_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_event(ts DESC);
CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_event(user_id, ts DESC);

CREATE TABLE IF NOT EXISTS ai_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    grounded INTEGER NOT NULL DEFAULT 1,
    fallback INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ai_audit_ts ON ai_audit(ts DESC);

-- V1.1 质量与验收：评测运行、用例结果、人工复核与缺陷台账
CREATE TABLE IF NOT EXISTS quality_run (
    run_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    suite TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    metrics TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_quality_run_created ON quality_run(created_at DESC);

CREATE TABLE IF NOT EXISTS quality_case (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    doc_id TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL,
    expected TEXT NOT NULL DEFAULT '',
    actual TEXT NOT NULL DEFAULT '',
    passed INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_quality_case_run ON quality_case(run_id, metric);

CREATE TABLE IF NOT EXISTS quality_review (
    review_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    scores TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_review_target ON quality_review(target_type, target_id);

CREATE TABLE IF NOT EXISTS quality_defect (
    defect_id TEXT PRIMARY KEY,
    severity TEXT NOT NULL DEFAULT 'P2',
    area TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_defect_status ON quality_defect(status, severity);

-- V1.2 教学闭环：追问持久化、题卷布置、阅读器状态/笔记、结构版本
CREATE TABLE IF NOT EXISTS follow_up (
    follow_up_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    q_id TEXT,
    kind TEXT NOT NULL DEFAULT 'clarify',
    question TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    score REAL,
    feedback TEXT NOT NULL DEFAULT '',
    hit TEXT NOT NULL DEFAULT '[]',
    miss TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    answered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_follow_up_session ON follow_up(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_follow_up_turn ON follow_up(turn_id, kind);

CREATE TABLE IF NOT EXISTS paper_assignment (
    assignment_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    teacher_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'tnt_local',
    title TEXT NOT NULL DEFAULT '',
    assignee_id TEXT NOT NULL,
    assignee_name TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'assigned',
    session_id TEXT,
    score REAL,
    report_id TEXT,
    teacher_comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    submitted_at TEXT,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignment_teacher ON paper_assignment(teacher_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_assignment_assignee ON paper_assignment(assignee_id, status);

CREATE TABLE IF NOT EXISTS reader_state (
    user_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    page_no INTEGER NOT NULL DEFAULT 1,
    scroll_ratio REAL NOT NULL DEFAULT 0,
    zoom REAL NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, doc_id)
);

CREATE TABLE IF NOT EXISTS reader_note (
    note_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    page_no INTEGER NOT NULL DEFAULT 1,
    quote TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reader_note_doc ON reader_note(user_id, doc_id, page_no);

CREATE TABLE IF NOT EXISTS document_version (
    version_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    snapshot TEXT NOT NULL DEFAULT '[]',
    node_count INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_document_version_no ON document_version(doc_id, version_no);
CREATE INDEX IF NOT EXISTS idx_document_version_doc ON document_version(doc_id, version_no DESC);
"""


def _pg_sql(sql: str) -> str:
    """把 sqlite 风格 ? 占位符转成 psycopg 的 %s，跳过字符串字面量。"""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(sql):
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < len(sql) and sql[i + 1] == quote:
                    out.append(sql[i + 1])
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _pg_schema() -> str:
    schema = SCHEMA.replace("PRAGMA journal_mode=WAL;", "")
    return schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")


class PgConnection:
    """psycopg 连接的轻量 sqlite 兼容包装，业务层继续使用 ? 占位符。"""

    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import dict_row
        self.raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)

    def execute(self, sql: str, params: Iterable[Any] = ()):
        return self.raw.execute(_pg_sql(sql), tuple(params))

    def executescript(self, script: str) -> None:
        for statement in script.split(";"):
            if statement.strip():
                self.raw.execute(_pg_sql(statement))

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def connect():
    if DB_BACKEND == "postgres":
        return PgConnection(DATABASE_URL)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        if DB_BACKEND == "postgres":
            conn.execute("SELECT pg_advisory_xact_lock(724242)")
            conn.executescript(_pg_schema())
            for table, column, ddl in (
                ("app_user", "email", "TEXT DEFAULT ''"),
                ("app_user", "phone", "TEXT DEFAULT ''"),
                ("document", "source_uri", "TEXT DEFAULT ''"),
                ("session", "assignment_id", "TEXT DEFAULT ''"),
            ):
                exists = conn.execute(
                    "SELECT 1 FROM information_schema.columns WHERE table_name=? AND column_name=?",
                    (table, column),
                ).fetchone()
                if not exists:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            conn.commit()
            return
        conn.executescript(SCHEMA)
        # 轻量迁移：给旧库补注册资料字段
        columns = {r[1] for r in conn.execute("PRAGMA table_info(app_user)").fetchall()}
        for name in ("email", "phone"):
            if name not in columns:
                conn.execute(f"ALTER TABLE app_user ADD COLUMN {name} TEXT DEFAULT ''")
        doc_columns = {r[1] for r in conn.execute("PRAGMA table_info(document)").fetchall()}
        if "source_uri" not in doc_columns:
            conn.execute("ALTER TABLE document ADD COLUMN source_uri TEXT DEFAULT ''")
        session_columns = {r[1] for r in conn.execute("PRAGMA table_info(session)").fetchall()}
        if "assignment_id" not in session_columns:
            conn.execute("ALTER TABLE session ADD COLUMN assignment_id TEXT DEFAULT ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_app_user_email ON app_user(email) WHERE email IS NOT NULL AND email<>''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_app_user_phone ON app_user(phone) WHERE phone IS NOT NULL AND phone<>''")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with db() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [dict(r) for r in rows]


def fetch_one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with db() as conn:
        row = conn.execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def execute(sql: str, params: Iterable[Any] = ()) -> None:
    with db() as conn:
        conn.execute(sql, tuple(params))


def insert(table: str, data: dict) -> None:
    cols = ", ".join(data.keys())
    marks = ", ".join("?" for _ in data)
    with db() as conn:
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))


def update(table: str, key_field: str, key_value: str, data: dict) -> None:
    if not data:
        return
    sets = ", ".join(f"{k}=?" for k in data)
    with db() as conn:
        conn.execute(
            f"UPDATE {table} SET {sets} WHERE {key_field}=?",
            tuple(data.values()) + (key_value,),
        )


def load_json(value: Any, default: Any):
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
