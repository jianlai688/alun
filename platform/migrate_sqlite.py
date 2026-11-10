"""SQLite → PostgreSQL 数据迁移与校验。

用法：
    python -m alun.platform.migrate_sqlite --source data/alun.db --target postgresql://...
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

TABLES = [
    "app_user", "app_setting", "document", "node", "chunk", "knowledge_point", "evidence",
    "question", "question_set", "session", "turn", "report", "task", "usage_log", "idempotency",
    "activity_event", "ai_audit", "quality_run", "quality_case", "quality_review", "quality_defect",
    "follow_up", "paper_assignment", "reader_state", "reader_note", "document_version",
]


def _table_columns(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def migrate(source: str, target: str) -> dict:
    from ..app import db
    db.DB_BACKEND = "postgres"
    db.DATABASE_URL = target
    db.DB_PATH = Path(source)
    db.init_db()

    sconn = sqlite3.connect(source)
    sconn.row_factory = sqlite3.Row
    import psycopg
    from psycopg.rows import dict_row
    tconn = psycopg.connect(target, row_factory=dict_row)

    result = {"source": source, "target": target, "tables": {}, "total_rows": 0, "checksum": ""}
    digest = hashlib.sha256()
    try:
        for table in TABLES:
            cols = _table_columns(sconn, table)
            if not cols:
                continue
            rows = sconn.execute(f"SELECT * FROM {table}").fetchall()
            for row in rows:
                placeholders = ",".join(["%s"] * len(cols))
                tconn.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    tuple(row[c] for c in cols),
                )
            if "id" in cols:
                try:
                    tconn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}','id'),"
                        f" COALESCE((SELECT MAX(id) FROM {table}), 1))"
                    )
                except Exception:
                    tconn.rollback()
            tconn.commit()
            target_count = tconn.execute(f"SELECT count(*) AS c FROM {table}").fetchone()["c"]
            result["tables"][table] = {"source": len(rows), "target": target_count}
            result["total_rows"] += len(rows)
            digest.update(f"{table}:{len(rows)}:{target_count}".encode())
        result["checksum"] = digest.hexdigest()[:16]
        return result
    finally:
        sconn.close()
        tconn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="alun/data/alun.db")
    ap.add_argument("--target", required=True)
    ap.add_argument("--out", default="alun/data/platform/migration-report.json")
    args = ap.parse_args()
    report = migrate(args.source, args.target)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
