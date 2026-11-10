# =============================================================================
# 文件定位：alun/app/reader_api.py
# 阅读器 API：负责“阅读位置”“页级笔记”和“PDF 原文页文本层”。
# 上游由 main.py 挂载 router，下游使用 db.py 的 SQL 封装、security.py 的
# Principal 鉴权，以及 pymupdf 读取 PDF 原文。
# =============================================================================
"""V1.2 阅读器增强：阅读位置与页级笔记。"""
# 让类型注解在运行时延迟求值，避免循环导入（本项目统一使用该写法）。
from __future__ import annotations

# base64 用于把 PDF 页面 PNG 编码成 data URL，直接返回给浏览器。
import base64

# FastAPI 的路由、依赖、异常和请求对象。
from fastapi import APIRouter, Depends, HTTPException, Request

# 数据层：db() 是上下文管理器；fetch_* 是带占位符转换的查询封装。
from .db import db, fetch_all, fetch_one, iso, new_id
# 鉴权层：Principal 表示当前 JWT 用户，principal_from_request 解析请求头。
from .security import Principal, principal_from_request

# 统一前缀 /api/v1；tags 只影响 OpenAPI 文档分组。
router = APIRouter(prefix="/api/v1", tags=["reader"])


def _p(request: Request) -> Principal:
    """从请求中解析当前用户，作为所有阅读器接口的第一道鉴权。"""
    return principal_from_request(request)


def _doc(document_id: str, p: Principal) -> dict:
    """读取文档并校验租户/所有者，防止越权访问别人的阅读数据。"""
    # deleted_at IS NULL：软删除的文档不能再被阅读接口访问。
    row = fetch_one(
        "SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL",
        (document_id, p.tenant_id),
    )
    # 查不到统一返回 404，避免泄露“文档存在但不属于你”的信息。
    if not row:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    # 管理员可以查看租户内所有文档；普通用户只能看自己的文档。
    if p.role != "admin" and row.get("user_id") != p.user_id:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权访问该文档"})
    return row


@router.get("/documents/{document_id}/pages/{page_no}/text-layer")
def page_text_layer(document_id: str, page_no: int, request: Request):
    """PDF 原文页的图片 + 透明可划选文本层，避免用解析后的正文替代原文。"""
    # 1) 先鉴权并确认文档存在，避免把文件路径暴露给未授权请求。
    p = _p(request)
    row = _doc(document_id, p)
    # 2) 只允许 PDF；页面图片和坐标文本层依赖 PDF 的版面信息。
    if not str(row.get("file_name") or row.get("source_file_name") or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail={"code": "NOT_PDF", "message": "仅 PDF 支持原文文本层"})
    # 3) 延迟导入 pipeline：避免 reader_api 与 pipeline 在模块加载时循环依赖。
    from . import pipeline
    # get_document_path 会先从本地 uploads 找，再回退到 S3 对象存储。
    path = pipeline.get_document_path(document_id)
    if not path:
        raise HTTPException(status_code=404, detail={"code": "DOC_FILE_MISSING", "message": "原始文件缺失"})
    try:
        # 4) 使用 PyMuPDF 打开 PDF，页码做边界保护（1-based 对外，0-based 对内）。
        import pymupdf
        doc = pymupdf.open(str(path))
        idx = max(1, min(len(doc), int(page_no))) - 1
        page = doc[idx]
        # 5) 以 144 DPI 渲染整页 PNG：这是用户看到的“原文”，保证公式/版式不变。
        pix = page.get_pixmap(dpi=144, alpha=False)
        # 6) 转成 data URL，前端可直接放进 img.src，不需要额外的图片下载接口。
        image = "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()
        # 7) 读取 PDF 的文本块和每行 bbox；透明文本层靠这些坐标覆盖在图片上。
        width, height = page.rect.width, page.rect.height
        lines = []
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                # 把同一行的多个 span 拼成完整文本（公式可能由多个 span 组成）。
                text = "".join(s.get("text", "") for s in spans)
                if not text.strip():
                    continue
                # 取该行最大字号，前端按比例缩放后定位；字号为空时兜底 9pt。
                sizes = [float(s.get("size") or 0) for s in spans if s.get("text", "").strip()]
                bbox = line.get("bbox") or [0, 0, 0, 0]
                lines.append({
                    "text": text,
                    "bbox": [round(float(v), 2) for v in bbox],
                    "size": round(max(sizes) if sizes else 9.0, 2),
                    "font": spans[0].get("font", "") if spans else "",
                })
        # 8) 关闭文档前先取页数；按 y 坐标、再按 x 坐标排序，保证 DOM 顺序接近阅读顺序。
        page_count = len(doc)
        doc.close()
        lines.sort(key=lambda x: (round(x["bbox"][1], 1), x["bbox"][0]))
        return {
            "page": idx + 1,
            "page_count": page_count,
            "width": width,
            "height": height,
            "image": image,
            "lines": lines,
        }
    except HTTPException:
        # 已经构造好的业务异常原样抛出，不要被下面的通用异常吞掉。
        raise
    except Exception as exc:
        # 文件损坏、PyMuPDF 解析失败等统一变成可读的 PAGE_LAYER_FAILED。
        raise HTTPException(status_code=500, detail={"code": "PAGE_LAYER_FAILED", "message": str(exc)}) from exc


@router.get("/documents/{document_id}/reading-state")
def get_state(document_id: str, request: Request):
    """读取当前用户在该文档上的上次阅读位置。"""
    p = _p(request)
    _doc(document_id, p)
    # reader_state 以 (user_id, doc_id) 为唯一键；没有记录时返回默认第 1 页。
    row = fetch_one("SELECT * FROM reader_state WHERE user_id=? AND doc_id=?", (p.user_id, document_id))
    return row or {
        "user_id": p.user_id,
        "doc_id": document_id,
        "page_no": 1,
        "scroll_ratio": 0.0,
        "zoom": 1.0,
        "updated_at": None,
    }


@router.put("/documents/{document_id}/reading-state")
def put_state(document_id: str, payload: dict, request: Request):
    """保存阅读位置；前端每次翻页/滚动后会做防抖调用。"""
    p = _p(request)
    _doc(document_id, p)
    # 对页码、滚动比例、缩放做服务端边界校验，避免前端传入异常值污染数据库。
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
        # SQLite 与 PostgreSQL 都支持 ON CONFLICT；有记录则更新，没有则插入。
        conn.execute(
            "INSERT INTO reader_state (user_id, doc_id, page_no, scroll_ratio, zoom, updated_at) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(user_id, doc_id) DO UPDATE SET page_no=excluded.page_no, scroll_ratio=excluded.scroll_ratio,"
            " zoom=excluded.zoom, updated_at=excluded.updated_at",
            (p.user_id, document_id, page_no, scroll_ratio, zoom, iso()),
        )
    return {"document_id": document_id, "page_no": page_no, "scroll_ratio": scroll_ratio, "zoom": zoom, "updated_at": iso()}


@router.get("/documents/{document_id}/notes")
def list_notes(document_id: str, request: Request, page_no: int | None = None):
    """列出当前用户的页级笔记，可按页码过滤。"""
    p = _p(request)
    _doc(document_id, p)
    # 只拼参数值，不把用户输入直接拼进 SQL，避免 SQL 注入。
    sql = "SELECT * FROM reader_note WHERE user_id=? AND doc_id=?"
    params: list = [p.user_id, document_id]
    if page_no:
        sql += " AND page_no=?"
        params.append(int(page_no))
    # 同一页按创建时间倒序，最新笔记排在前面。
    sql += " ORDER BY page_no, created_at DESC"
    return {"items": fetch_all(sql, params)}


@router.post("/documents/{document_id}/notes", status_code=201)
def add_note(document_id: str, payload: dict, request: Request):
    """新增一条页级笔记（正文和引用至少有一个）。"""
    p = _p(request)
    _doc(document_id, p)
    # 去除首尾空白；quote 是选中原文，note 是用户自己的批注。
    note = str(payload.get("note") or "").strip()
    quote = str(payload.get("quote") or "").strip()
    if not note and not quote:
        raise HTTPException(status_code=422, detail={"code": "NOTE_EMPTY", "message": "笔记内容不能为空"})
    try:
        page_no = max(1, int(payload.get("page_no") or 1))
    except Exception:
        page_no = 1
    # 生成可读前缀的 note_id，方便日志排查。
    note_id = new_id("note")
    with db() as conn:
        conn.execute(
            "INSERT INTO reader_note (note_id, user_id, doc_id, page_no, quote, note, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            # 对 quote/note 做长度截断，防止单条笔记过大拖垮数据库/接口。
            (note_id, p.user_id, document_id, page_no, quote[:1000], note[:4000], iso(), iso()),
        )
    return fetch_one("SELECT * FROM reader_note WHERE note_id=?", (note_id,))


@router.patch("/documents/{document_id}/notes/{note_id}")
def update_note(document_id: str, note_id: str, payload: dict, request: Request):
    """修改当前用户的一条笔记；未传字段保留原值。"""
    p = _p(request)
    _doc(document_id, p)
    # 查询时同时带 user_id，确保只能改自己的笔记（管理员也不越权改用户笔记）。
    row = fetch_one("SELECT * FROM reader_note WHERE note_id=? AND user_id=? AND doc_id=?", (note_id, p.user_id, document_id))
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOTE_NOT_FOUND", "message": "笔记不存在"})
    note = str(payload.get("note", row["note"]))[:4000]
    quote = str(payload.get("quote", row["quote"]))[:1000]
    page_no = int(payload.get("page_no", row["page_no"]) or row["page_no"])
    with db() as conn:
        conn.execute(
            "UPDATE reader_note SET note=?, quote=?, page_no=?, updated_at=? WHERE note_id=?",
            (note, quote, page_no, iso(), note_id),
        )
    return fetch_one("SELECT * FROM reader_note WHERE note_id=?", (note_id,))


@router.delete("/documents/{document_id}/notes/{note_id}", status_code=204)
def delete_note(document_id: str, note_id: str, request: Request):
    """删除当前用户的一条笔记；删除成功返回 204 空响应。"""
    p = _p(request)
    _doc(document_id, p)
    with db() as conn:
        # 删除条件同样带 user_id，避免猜测 note_id 删除别人的数据。
        conn.execute("DELETE FROM reader_note WHERE note_id=? AND user_id=? AND doc_id=?", (note_id, p.user_id, document_id))
    return None
