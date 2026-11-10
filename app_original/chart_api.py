"""图表数据提取 API：查询已解析图表，或触发文档重解析。"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException, Request

from . import pipeline
from .db import fetch_all, fetch_one
from .security import principal_from_request

router = APIRouter(prefix="/api/v1", tags=["charts"])


def _chart_payload(text: str) -> dict | None:
    m = re.search(r"```chart-data\s*(\{.*?\})\s*```", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


@router.get("/documents/{document_id}/charts")
def list_charts(document_id: str, request: Request):
    p = principal_from_request(request)
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    if not doc:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    if p.role != "admin" and doc.get("user_id") != p.user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权查看该文档"})
    rows = fetch_all(
        "SELECT node_id, title, page_start, page_end, text, confidence FROM node"
        " WHERE doc_id=? AND node_type='chart' ORDER BY order_index", (document_id,)
    )
    return {"items": [{
        "node_id": r["node_id"], "title": r["title"], "page": r["page_start"],
        "confidence": r["confidence"], "data": _chart_payload(r["text"]),
        "markdown": (r["text"] or "").split("```chart-data")[0].strip(),
    } for r in rows]}


@router.post("/documents/{document_id}/charts/extract", status_code=202)
def extract_charts(document_id: str, request: Request):
    p = principal_from_request(request)
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    if not doc:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    if p.role != "admin" and doc.get("user_id") != p.user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权重解析该文档"})
    path = pipeline.get_document_path(document_id)
    if not path:
        raise HTTPException(status_code=404, detail={"code": "DOC_FILE_MISSING", "message": "原始文件缺失"})
    task_id = pipeline.start_parse(document_id, path, doc.get("doc_type") or "auto", doc.get("title"))
    return {"document_id": document_id, "task_id": task_id, "status": "processing"}
