"""V2.0 平台数据库迁移：pgvector 向量表与迁移版本台账。"""
from __future__ import annotations

from dataclasses import dataclass

from ..app.config import DATABASE_URL, DB_BACKEND
from ..app.db import iso


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    up: tuple[str, ...]
    down: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        name="pgvector_document_embedding",
        up=(
            "CREATE EXTENSION IF NOT EXISTS vector",
            """CREATE TABLE IF NOT EXISTS document_embedding (
                embedding_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                node_id TEXT NOT NULL DEFAULT '',
                chunk_id TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL DEFAULT '',
                embedding vector(384) NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_embedding_tenant_doc ON document_embedding(tenant_id, doc_id)",
            "CREATE INDEX IF NOT EXISTS idx_embedding_vector ON document_embedding USING hnsw (embedding vector_cosine_ops)",
        ),
        down=(
            "DROP TABLE IF EXISTS document_embedding",
        ),
    ),
    Migration(
        version=2,
        name="platform_job_audit",
        up=(
            """CREATE TABLE IF NOT EXISTS platform_job (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                payload TEXT NOT NULL DEFAULT '{}',
                result TEXT NOT NULL DEFAULT '{}',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            "CREATE INDEX IF NOT EXISTS idx_platform_job_status ON platform_job(status, created_at)",
        ),
        down=(
            "DROP TABLE IF EXISTS platform_job",
        ),
    ),
)


def _connect(url: str):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10)


def _ensure_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS platform_schema (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    conn.commit()


def status(url: str | None = None) -> dict:
    explicit_url = url is not None
    url = url or DATABASE_URL
    if DB_BACKEND != "postgres" and not explicit_url:
        return {"backend": "sqlite", "applied": [], "pending": [m.version for m in MIGRATIONS]}
    conn = _connect(url)
    try:
        _ensure_table(conn)
        applied = [r["version"] for r in conn.execute("SELECT version FROM platform_schema ORDER BY version").fetchall()]
        return {"backend": "postgres", "applied": applied,
                "pending": [m.version for m in MIGRATIONS if m.version not in applied]}
    finally:
        conn.close()


def migrate(url: str | None = None) -> dict:
    url = url or DATABASE_URL
    conn = _connect(url)
    applied = []
    try:
        _ensure_table(conn)
        done = {r["version"] for r in conn.execute("SELECT version FROM platform_schema").fetchall()}
        for migration in MIGRATIONS:
            if migration.version in done:
                continue
            for statement in migration.up:
                conn.execute(statement)
            conn.execute("INSERT INTO platform_schema (version, name, applied_at) VALUES (%s,%s,%s)",
                         (migration.version, migration.name, iso()))
            conn.commit()
            applied.append(migration.version)
        return {"applied": applied, "current": status(url)}
    finally:
        conn.close()


def rollback(target: int = 0, url: str | None = None) -> dict:
    url = url or DATABASE_URL
    conn = _connect(url)
    rolled = []
    try:
        _ensure_table(conn)
        done = {r["version"] for r in conn.execute("SELECT version FROM platform_schema").fetchall()}
        for migration in sorted(MIGRATIONS, key=lambda m: m.version, reverse=True):
            if migration.version <= target or migration.version not in done:
                continue
            for statement in migration.down:
                conn.execute(statement)
            conn.execute("DELETE FROM platform_schema WHERE version=%s", (migration.version,))
            conn.commit()
            rolled.append(migration.version)
        return {"rolled_back": rolled, "current": status(url)}
    finally:
        conn.close()


def ping(url: str | None = None) -> bool:
    conn = _connect(url or DATABASE_URL)
    try:
        conn.execute("SELECT 1")
        return True
    finally:
        conn.close()
