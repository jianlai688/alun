"""V1.2 结构版本 API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from . import pipeline, versions
from .db import fetch_one
from .security import Principal, principal_from_request

router = APIRouter(prefix="/api/v1", tags=["versions"])


def _p(request: Request) -> Principal:
    return principal_from_request(request)


def _doc(document_id: str, p: Principal) -> dict:
    row = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    if not row:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    if p.role != "admin" and row.get("user_id") != p.user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权访问该文档"})
    return row


@router.get("/documents/{document_id}/versions")
def list_versions(document_id: str, request: Request):
    p = _p(request)
    _doc(document_id, p)
    rows = versions.list_versions(document_id)
    latest = fetch_one("SELECT doc_version FROM document WHERE doc_id=?", (document_id,))
    return {"items": rows, "current_version": latest["doc_version"] if latest else None}


@router.get("/documents/{document_id}/versions/{version_no}")
def get_version(document_id: str, version_no: int, request: Request):
    p = _p(request)
    _doc(document_id, p)
    row = versions.get_version(document_id, version_no)
    if not row:
        raise HTTPException(status_code=404, detail={"code": "VERSION_NOT_FOUND", "message": "版本不存在"})
    return {
        "version_id": row["version_id"], "version_no": row["version_no"], "node_count": row["node_count"],
        "reason": row["reason"], "created_by": row["created_by"], "created_at": row["created_at"],
        "nodes": row["snapshot"].get("node", []),
    }


@router.post("/documents/{document_id}/versions/{version_no}/restore")
def restore_version(document_id: str, version_no: int, request: Request):
    p = _p(request)
    _doc(document_id, p)
    try:
        result = versions.restore(document_id, version_no, created_by=p.username or p.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail={"code": "VERSION_NOT_FOUND", "message": str(exc)})
    pipeline.repair_sessions_for_document(document_id)
    pipeline.repair_questions_for_document(document_id)
    return result
