"""V1.2 结构版本：快照、列表与回滚。"""
from __future__ import annotations

from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id

VERSION_TABLES = {
    "node": (
        "node_id, doc_id, parent_id, level, node_type, order_no, title, text, page_start, page_end,"
        " bbox, confidence, path, char_count, order_index",
        ("node_id", "doc_id", "parent_id", "level", "node_type", "order_no", "title", "text",
         "page_start", "page_end", "bbox", "confidence", "path", "char_count", "order_index"),
    ),
    "chunk": ("chunk_id, doc_id, node_id, text, token_count, order_no",
              ("chunk_id", "doc_id", "node_id", "text", "token_count", "order_no")),
    "knowledge_point": (
        "kp_id, doc_id, node_id, name, kp_type, definition, importance, difficulty, evidence_ids, prerequisites, order_no",
        ("kp_id", "doc_id", "node_id", "name", "kp_type", "definition", "importance", "difficulty",
         "evidence_ids", "prerequisites", "order_no"),
    ),
    "evidence": (
        "evidence_id, doc_id, node_id, page_no, quote, char_start, char_end, strength, context",
        ("evidence_id", "doc_id", "node_id", "page_no", "quote", "char_start", "char_end", "strength", "context"),
    ),
}


def snapshot(doc_id: str, reason: str = "", created_by: str = "") -> dict | None:
    doc = fetch_one("SELECT * FROM document WHERE doc_id=?", (doc_id,))
    if not doc:
        return None
    data = {}
    for table, (cols, _) in VERSION_TABLES.items():
        data[table] = fetch_all(f"SELECT {cols} FROM {table} WHERE doc_id=?", (doc_id,))
    if not data["node"]:
        return None
    version_no = int(doc.get("doc_version") or 1)
    version_id = new_id("ver")
    with db() as conn:
        conn.execute(
            "INSERT INTO document_version (version_id, doc_id, version_no, snapshot, node_count, reason, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(doc_id, version_no) DO UPDATE SET"
            " snapshot=excluded.snapshot, node_count=excluded.node_count, reason=excluded.reason,"
            " created_by=excluded.created_by, created_at=excluded.created_at",
            (version_id, doc_id, version_no, dump(data), len(data["node"]), reason[:200], created_by, iso()),
        )
    return {"version_id": version_id, "version_no": version_no, "node_count": len(data["node"]), "reason": reason,
            "created_at": iso()}


def list_versions(doc_id: str) -> list[dict]:
    rows = fetch_all(
        "SELECT version_id, doc_id, version_no, node_count, reason, created_by, created_at"
        " FROM document_version WHERE doc_id=? ORDER BY version_no DESC", (doc_id,)
    )
    return rows


def get_version(doc_id: str, version_no: int) -> dict | None:
    row = fetch_one("SELECT * FROM document_version WHERE doc_id=? AND version_no=?", (doc_id, version_no))
    if not row:
        return None
    row["snapshot"] = load_json(row.get("snapshot"), {})
    return row


def restore(doc_id: str, version_no: int, created_by: str = "") -> dict:
    target = get_version(doc_id, version_no)
    if not target:
        raise ValueError("版本不存在")
    snapshot(doc_id, reason=f"回滚前自动快照（目标 v{version_no}）", created_by=created_by)
    data = target["snapshot"] or {}
    with db() as conn:
        for table in ("node", "chunk", "knowledge_point", "evidence"):
            conn.execute(f"DELETE FROM {table} WHERE doc_id=?", (doc_id,))
        for table, (cols, fields) in VERSION_TABLES.items():
            values = ", ".join("?" for _ in fields)
            for row in data.get(table, []):
                conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({values})", tuple(row.get(f) for f in fields))
        node_count = len(data.get("node", []))
        section_count = sum(1 for n in data.get("node", []) if n.get("node_type") == "section")
        kp_count = len(data.get("knowledge_point", []))
        chunk_count = len(data.get("chunk", []))
        doc = conn.execute("SELECT doc_version, stats FROM document WHERE doc_id=?", (doc_id,)).fetchone()
        stats = load_json(doc["stats"] if doc else "{}", {})
        stats.update({"node_count": node_count, "section_count": section_count, "kp_count": kp_count, "chunk_count": chunk_count})
        new_version = int(doc["doc_version"] if doc else 1) + 1
        conn.execute("UPDATE document SET doc_version=?, stats=?, updated_at=? WHERE doc_id=?",
                     (new_version, dump(stats), iso(), doc_id))
    snapshot(doc_id, reason=f"回滚到 v{version_no} 后快照", created_by=created_by)
    return {"document_id": doc_id, "restored_from": version_no, "version": new_version,
            "node_count": node_count, "section_count": section_count, "kp_count": kp_count}
