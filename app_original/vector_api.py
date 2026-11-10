"""V2.0 pgvector 语义检索 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from ..app.db import fetch_one
from ..app.security import principal_from_request
from ..platform import vector_store

router = APIRouter(prefix="/api/v1", tags=["vector"])


@router.post("/documents/{document_id}/embeddings")
def index_embeddings(document_id: str, request: Request):
    p = principal_from_request(request)
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    if not doc:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    if p.role != "admin" and doc.get("user_id") != p.user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权索引该文档"})
    try:
        return vector_store.index_document(document_id, p.tenant_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail={"code": "VECTOR_UNAVAILABLE", "message": str(exc)})


@router.get("/documents/{document_id}/embeddings/status")
def embedding_status(document_id: str, request: Request):
    p = principal_from_request(request)
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    if not doc:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    conn_status = vector_store.stats(p.tenant_id)
    return {"document_id": document_id, **conn_status}


@router.get("/search/semantic")
def semantic_search(request: Request, q: str, document_id: str | None = None, limit: int = 8):
    p = principal_from_request(request)
    if not q.strip():
        raise HTTPException(status_code=422, detail={"code": "QUERY_EMPTY", "message": "请输入检索问题"})
    try:
        return {"query": q, "items": vector_store.search(p.tenant_id, q, document_id, limit)}
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail={"code": "VECTOR_UNAVAILABLE", "message": str(exc)})
