"""V1.2 阅读器增强：阅读位置与页级笔记。"""
from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, HTTPException, Request

from .db import db, fetch_all, fetch_one, iso, new_id
from .security import Principal, principal_from_request

router = APIRouter(prefix="/api/v1", tags=["reader"])


def _p(request: Request) -> Principal:
    return principal_from_request(request)


def _doc(document_id: str, p: Principal) -> dict:
    row = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    if not row:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    if p.role != "admin" and row.get("user_id") != p.user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权访问该文档"})
    return row


@router.get("/documents/{document_id}/pages/{page_no}/text-layer")
def page_text_layer(document_id: str, page_no: int, request: Request):
    """PDF 原文页的图片 + 透明可划选文本层，避免用解析后的正文替代原文。"""
    p = _p(request)
    row = _doc(document_id, p)
    if not str(row.get("file_name") or row.get("source_file_name") or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail={"code": "NOT_PDF", "message": "仅 PDF 支持原文文本层"})
    from . import pipeline
    path = pipeline.get_document_path(document_id)
    if not path:
        raise HTTPException(status_code=404, detail={"code": "DOC_FILE_MISSING", "message": "原始文件缺失"})
    try:
        import pymupdf
        doc = pymupdf.open(str(path))
        idx = max(1, min(len(doc), int(page_no))) - 1
        page = doc[idx]
        pix = page.get_pixmap(dpi=144, alpha=False)
        image = "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()
        width, height = page.rect.width, page.rect.height
        lines = []
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(s.get("text", "") for s in spans)
                if not text.strip():
                    continue
                sizes = [float(s.get("size") or 0) for s in spans if s.get("text", "").strip()]
                bbox = line.get("bbox") or [0, 0, 0, 0]
                lines.append({
                    "text": text,
                    "bbox": [round(float(v), 2) for v in bbox],
                    "size": round(max(sizes) if sizes else 9.0, 2),
                    "font": spans[0].get("font", "") if spans else "",
                })
        page_count = len(doc)
        doc.close()
        lines.sort(key=lambda x: (round(x["bbox"][1], 1), x["bbox"][0]))
        return {"page": idx + 1, "page_count": page_count, "width": width, "height": height,
                "image": image, "lines": lines}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail={"code": "PAGE_LAYER_FAILED", "message": str(exc)}) from exc


@router.get("/documents/{document_id}/reading-state")
def get_state(document_id: str, request: Request):
    p = _p(request)
    _doc(document_id, p)
    row = fetch_one("SELECT * FROM reader_state WHERE user_id=? AND doc_id=?", (p.user_id, document_id))
    return row or {"user_id": p.user_id, "doc_id": document_id, "page_no": 1, "scroll_ratio": 0.0, "zoom": 1.0, "updated_at": None}


@router.put("/documents/{document_id}/reading-state")
def put_state(document_id: str, payload: dict, request: Request):
    p = _p(request)
    _doc(document_id, p)
    try:
        page_no = max(1, min(10000, int(payload.get("page_no") or 1)))
    except Exception:
        page_no = 1
    try:
        scroll_ratio = max(0.0, min(1.0, float(payload.get("scroll_ratio") or 0)))
    except Exception:
        scroll_ratio = 0.0
    try:
        zoom = max(0.5, min(3.0, float(payload.get("zoom") or 1)))
    except Exception:
        zoom = 1.0
    with db() as conn:
        conn.execute(
            "INSERT INTO reader_state (user_id, doc_id, page_no, scroll_ratio, zoom, updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(user_id, doc_id) DO UPDATE SET page_no=excluded.page_no, scroll_ratio=excluded.scroll_ratio,"
            " zoom=excluded.zoom, updated_at=excluded.updated_at",
            (p.user_id, document_id, page_no, scroll_ratio, zoom, iso()),
        )
    return {"document_id": document_id, "page_no": page_no, "scroll_ratio": scroll_ratio, "zoom": zoom, "updated_at": iso()}


@router.get("/documents/{document_id}/notes")
def list_notes(document_id: str, request: Request, page_no: int | None = None):
    p = _p(request)
    _doc(document_id, p)
    sql = "SELECT * FROM reader_note WHERE user_id=? AND doc_id=?"
    params: list = [p.user_id, document_id]
    if page_no:
        sql += " AND page_no=?"
        params.append(int(page_no))
    sql += " ORDER BY page_no, created_at DESC"
    return {"items": fetch_all(sql, params)}


@router.post("/documents/{document_id}/notes", status_code=201)
def add_note(document_id: str, payload: dict, request: Request):
    p = _p(request)
    _doc(document_id, p)
    note = str(payload.get("note") or "").strip()
    quote = str(payload.get("quote") or "").strip()
    if not note and not quote:
        raise HTTPException(status_code=422, detail={"code": "NOTE_EMPTY", "message": "笔记内容不能为空"})
    try:
        page_no = max(1, int(payload.get("page_no") or 1))
    except Exception:
        page_no = 1
    note_id = new_id("note")
    with db() as conn:
        conn.execute(
            "INSERT INTO reader_note (note_id, user_id, doc_id, page_no, quote, note, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (note_id, p.user_id, document_id, page_no, quote[:1000], note[:4000], iso(), iso()),
        )
    return fetch_one("SELECT * FROM reader_note WHERE note_id=?", (note_id,))


@router.patch("/documents/{document_id}/notes/{note_id}")
def update_note(document_id: str, note_id: str, payload: dict, request: Request):
    p = _p(request)
    _doc(document_id, p)
    row = fetch_one("SELECT * FROM reader_note WHERE note_id=? AND user_id=? AND doc_id=?", (note_id, p.user_id, document_id))
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOTE_NOT_FOUND", "message": "笔记不存在"})
    note = str(payload.get("note", row["note"]))[:4000]
    quote = str(payload.get("quote", row["quote"]))[:1000]
    page_no = int(payload.get("page_no", row["page_no"]) or row["page_no"])
    with db() as conn:
        conn.execute("UPDATE reader_note SET note=?, quote=?, page_no=?, updated_at=? WHERE note_id=?", (note, quote, page_no, iso(), note_id))
    return fetch_one("SELECT * FROM reader_note WHERE note_id=?", (note_id,))


@router.delete("/documents/{document_id}/notes/{note_id}", status_code=204)
def delete_note(document_id: str, note_id: str, request: Request):
    p = _p(request)
    _doc(document_id, p)
    with db() as conn:
        conn.execute("DELETE FROM reader_note WHERE note_id=? AND user_id=? AND doc_id=?", (note_id, p.user_id, document_id))
    return None
