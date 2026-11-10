"""活动事件与 AI 输出审计。"""
from __future__ import annotations

from .db import db, dump, fetch_all, iso


def log_event(
    *,
    user_id: str = "",
    username: str = "",
    tenant_id: str = "",
    event_type: str,
    detail: dict | None = None,
    ip: str = "",
    user_agent: str = "",
) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO activity_event (ts, user_id, username, tenant_id, event_type, detail, ip, user_agent)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (iso(), user_id, username, tenant_id, event_type, dump(detail or {}), ip[:64], user_agent[:240]),
            )
    except Exception:
        pass


def recent_events(limit: int = 100, since_id: int = 0) -> list[dict]:
    limit = max(1, min(300, int(limit or 100)))
    if since_id:
        rows = fetch_all("SELECT * FROM activity_event WHERE id>? ORDER BY id DESC LIMIT ?", (int(since_id), limit))
    else:
        rows = fetch_all("SELECT * FROM activity_event ORDER BY id DESC LIMIT ?", (limit,))
    from .db import load_json

    for row in rows:
        row["detail"] = load_json(row.get("detail"), {})
    return rows


def record_ai_output(
    *,
    kind: str,
    model: str,
    grounded: bool,
    fallback: bool = False,
    latency_ms: int = 0,
    user_id: str = "",
    document_id: str = "",
    session_id: str = "",
    detail: dict | None = None,
) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO ai_audit (ts, user_id, document_id, session_id, kind, model, grounded, fallback,"
                " latency_ms, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (iso(), user_id, document_id, session_id, kind, model, 1 if grounded else 0, 1 if fallback else 0,
                 int(latency_ms or 0), dump(detail or {})),
            )
    except Exception:
        pass
