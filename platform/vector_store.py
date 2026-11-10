"""pgvector 向量检索：本地确定性 384 维 embedding 基线。"""
from __future__ import annotations

import hashlib
import math

from ..app.config import DATABASE_URL, DB_BACKEND
from ..app.db import fetch_all, iso, new_id


def _vec(text: str, dims: int = 384) -> list[float]:
    """确定性哈希 embedding：可离线回归；生产可替换为模型 embedding。"""
    vec = [0.0] * dims
    text = (text or "").strip()
    for i in range(max(1, len(text) - 2)):
        gram = text[i:i + 3]
        h = hashlib.sha256(gram.encode("utf-8")).digest()
        idx = int.from_bytes(h[:4], "big") % dims
        sign = 1.0 if h[4] % 2 == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _conn():
    if DB_BACKEND != "postgres":
        raise RuntimeError("pgvector 仅在 ALUN_DB_BACKEND=postgres 时可用")
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)


def index_document(document_id: str, tenant_id: str) -> dict:
    chunks = fetch_all("SELECT chunk_id, node_id, text FROM chunk WHERE doc_id=? ORDER BY order_no", (document_id,))
    if not chunks:
        return {"document_id": document_id, "indexed": 0, "reason": "no_chunks"}
    conn = _conn()
    try:
        conn.execute("DELETE FROM document_embedding WHERE tenant_id=%s AND doc_id=%s", (tenant_id, document_id))
        for chunk in chunks:
            vec = _vec(chunk["text"])
            conn.execute(
                "INSERT INTO document_embedding (embedding_id, tenant_id, doc_id, node_id, chunk_id, content,"
                " embedding, metadata, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s,%s)",
                (new_id("emb"), tenant_id, document_id, chunk["node_id"], chunk["chunk_id"], chunk["text"],
                 "[" + ",".join(f"{x:.6f}" for x in vec) + "]", "{}", iso()),
            )
        conn.commit()
        return {"document_id": document_id, "indexed": len(chunks), "dimensions": 384}
    finally:
        conn.close()


def search(tenant_id: str, query: str, document_id: str | None = None, limit: int = 8) -> list[dict]:
    vec = _vec(query)
    literal = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
    sql = (
        "SELECT embedding_id, doc_id, node_id, chunk_id, content,"
        " 1 - (embedding <=> %s::vector) AS score"
        " FROM document_embedding WHERE tenant_id=%s"
    )
    params: list = [literal, tenant_id]
    if document_id:
        sql += " AND doc_id=%s"
        params.append(document_id)
    sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
    params += [literal, max(1, min(50, limit))]
    conn = _conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def stats(tenant_id: str) -> dict:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT count(*) AS embeddings, count(DISTINCT doc_id) AS documents FROM document_embedding WHERE tenant_id=%s",
            (tenant_id,),
        ).fetchone()
        return dict(row) if row else {"embeddings": 0, "documents": 0}
    finally:
        conn.close()
