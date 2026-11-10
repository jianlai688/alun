"""A论 · REST API v1（对齐《DGQA 接口文档》）。"""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import assistant as assistant_api
from . import llm, pipeline, sessions, settings, users, audit
from . import url_import
from . import admin as admin_api
from . import quality_api
from . import teaching_api
from ..platform import observability, queue, redis_client
from ..platform import storage as storage_mod
from . import reader_api
from . import version_api
from . import vector_api
from . import platform_api
from . import chart_api
from . import versions
from .config import (
    APP_NAME,
    APP_SUBTITLE,
    APP_VERSION,
    AUTH_MODE,
    AUTH_SECRET,
    CORS_ORIGINS,
    DB_BACKEND,
    LOG_JSON,
    SECURITY_HEADERS,
    DATA_DIR,
    DEFAULT_SCOPES,
    DOC_TYPE_LABELS,
    GATEWAY,
    MAX_UPLOAD_BYTES,
    WEB_DIR,
)
from .db import db, dump, fetch_all, fetch_one, init_db, iso, load_json, new_id, now
from .security import Principal, issue_token, principal_from_request, require, require_admin, validate_runtime_secret
from .sessions import SessionError

validate_runtime_secret(DB_BACKEND)
init_db()
if DB_BACKEND == "postgres":
    from ..platform import migrations
    migrations.migrate()
users.ensure_seed_users()
settings.ensure_defaults()
observability.setup_logging(LOG_JSON)

app = FastAPI(title=f"{APP_NAME} · {APP_SUBTITLE}", version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["http://127.0.0.1:8010", "http://localhost:8010"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Tenant-Id", "X-Request-Id"],
)
observability.instrument(app)
app.include_router(admin_api.router)
app.include_router(quality_api.router)
app.include_router(teaching_api.router)
app.include_router(reader_api.router)
app.include_router(version_api.router)
app.include_router(vector_api.router)
app.include_router(platform_api.router)
app.include_router(chart_api.router)

EXPORT_DIR = DATA_DIR / "reports"
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
if SECURITY_HEADERS:
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:12]}"
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(SessionError)
async def session_error_handler(request: Request, exc: SessionError):
    return _error(exc.status, exc.code, exc.message, exc.details, request)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        return _error(exc.status_code, detail.get("code", "HTTP_ERROR"), detail.get("message", str(detail)), detail.get("details"), request)
    return _error(exc.status_code, "HTTP_ERROR", str(detail), None, request)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    return _error(422, "VALIDATION_ERROR", "请求参数不合法", {"errors": errors}, request)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    import traceback

    traceback.print_exc()
    return _error(500, "INTERNAL_ERROR", "服务内部错误，请稍后重试", {"error": str(exc)[:200]}, request)


def _error(status: int, code: str, message: str, details=None, request: Request | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": getattr(request.state, "request_id", "") if request else "",
            "timestamp": iso(),
        },
    )


def principal(request: Request) -> Principal:
    return principal_from_request(request)


def _paginate(items: list, limit: int = 20, cursor: str | None = None) -> tuple[list, str | None]:
    limit = max(1, min(100, int(limit or 20)))
    offset = 0
    if cursor:
        try:
            offset = int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        except Exception:
            offset = 0
    page = items[offset : offset + limit]
    next_cursor = None
    if offset + limit < len(items):
        next_cursor = base64.urlsafe_b64encode(str(offset + limit).encode()).decode().rstrip("=")
    return page, next_cursor


def _idem_lookup(key: str | None, endpoint: str) -> dict | None:
    if not key:
        return None
    row = fetch_one("SELECT response FROM idempotency WHERE key=? AND endpoint=?", (key, endpoint))
    return load_json(row["response"], None) if row else None


def _idem_save(key: str | None, endpoint: str, response: dict) -> None:
    if not key:
        return
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO idempotency (key, endpoint, response, created_at) VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET endpoint=excluded.endpoint, response=excluded.response, created_at=excluded.created_at",
                (key, endpoint, dump(response), iso()),
            )
    except Exception:
        pass


@app.get("/api/v1/health")
def health():
    deps = observability.refresh_dependencies()
    gateway_ok, gateway_msg = llm.health()
    deps["model_gateway"] = "ok" if gateway_ok else (gateway_msg if llm.available() else "not_configured")
    ok = deps["database"].get("ok") and deps["redis"].get("ok") and deps["storage"].get("ok")
    return {
        "status": "ok" if ok else "degraded",
        "name": APP_NAME,
        "version": APP_VERSION,
        "time": iso(),
        "dependencies": deps,
    }


@app.get("/api/v1/ready")
def ready():
    deps = observability.refresh_dependencies()
    ok = deps["database"].get("ok") and deps["redis"].get("ok") and deps["storage"].get("ok")
    return JSONResponse({"status": "ready" if ok else "not_ready", "dependencies": deps},
                        status_code=200 if ok else 503)


@app.get("/metrics")
def metrics():
    return observability.metrics()


@app.get("/api/v1/meta")
def meta():
    cached = redis_client.get_json("meta:v2")
    if cached:
        return cached
    payload = {
        "name": APP_NAME,
        "subtitle": APP_SUBTITLE,
        "version": APP_VERSION,
        "auth_mode": AUTH_MODE,
        "llm": {"enabled": llm.available(), "model": settings.get("llm.model") if llm.available() else None},
        "doc_types": DOC_TYPE_LABELS,
        "modes": {"guided": "导读模式", "exam": "测评模式", "defense": "答辩模式"},
    }
    redis_client.set_json("meta:v2", payload, ttl=300)
    return payload


def _request_meta(request: Request) -> tuple[str, str]:
    ip = request.client.host if request.client else ""
    ua = request.headers.get("User-Agent", "")
    return ip, ua


def _login_response(user: dict) -> dict:
    scope = users.scopes_for(user["role"])
    token, ttl = issue_token(
        user["user_id"], user["tenant_id"], scope,
        role=user["role"], username=user["username"],
    )
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": ttl,
        "scope": scope,
        "user": users.public_user(user),
    }


@app.post("/api/v1/auth/login")
async def login(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    ip, ua = _request_meta(request)
    allowed, remaining = redis_client.rate_limit("login", ip or username or "unknown", limit=10, window=60)
    if not allowed:
        raise SessionError(429, "RATE_LIMITED", "登录尝试过于频繁，请稍后再试", {"retry_after": 60})
    user = users.authenticate(username, password)
    if not user:
        audit.log_event(username=username, event_type="login_failed", detail={"reason": "invalid_credentials"}, ip=ip, user_agent=ua)
        raise SessionError(401, "INVALID_CREDENTIALS", "用户名或密码错误")
    audit.log_event(
        user_id=user["user_id"], username=user["username"], tenant_id=user["tenant_id"],
        event_type="login", detail={"role": user["role"]}, ip=ip, user_agent=ua,
    )
    return _login_response(user)


@app.post("/api/v1/auth/register", status_code=201)
async def register(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        user = users.create_user(
            str(body.get("username") or ""),
            str(body.get("password") or ""),
            str(body.get("display_name") or ""),
            email=str(body.get("email") or ""),
            phone=str(body.get("phone") or ""),
        )
    except ValueError as exc:
        raise SessionError(422, "REGISTER_INVALID", str(exc))
    ip, ua = _request_meta(request)
    audit.log_event(
        user_id=user["user_id"], username=user["username"], tenant_id=user["tenant_id"],
        event_type="register", detail={"role": "user"}, ip=ip, user_agent=ua,
    )
    return _login_response(user)


@app.get("/api/v1/auth/me")
def me(p: Principal = Depends(principal)):
    row = fetch_one("SELECT * FROM app_user WHERE user_id=?", (p.user_id,))
    if not row:
        raise SessionError(401, "UNAUTHORIZED", "账号不存在")
    return users.public_user(row)


@app.post("/api/v1/auth/logout", status_code=204)
def logout(request: Request, p: Principal = Depends(principal)):
    ip, ua = _request_meta(request)
    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id,
                    event_type="logout", ip=ip, user_agent=ua)
    return Response(status_code=204)


@app.post("/api/v1/auth/token")
async def auth_token(request: Request):
    """兼容接口文档的 client_credentials 调用，同时支持 username/password。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    username = str(body.get("username") or body.get("client_id") or "").strip()
    password = str(body.get("password") or body.get("client_secret") or "")
    user = users.authenticate(username, password)
    if not user:
        raise SessionError(401, "INVALID_CREDENTIALS", "用户名或密码错误")
    ip, ua = _request_meta(request)
    audit.log_event(user_id=user["user_id"], username=user["username"], tenant_id=user["tenant_id"],
                    event_type="login", detail={"via": "token"}, ip=ip, user_agent=ua)
    out = _login_response(user)
    return {
        "access_token": out["access_token"],
        "token_type": "Bearer",
        "expires_in": out["expires_in"],
        "scope": out["scope"],
    }


# ---------------------------------------------------------------------------
# AI 问答助手
# ---------------------------------------------------------------------------
@app.post("/api/v1/assistant/chat")
async def assistant_chat(
    message: str = Form(""),
    document_id: str | None = Form(None),
    session_id: str | None = Form(None),
    history: str | None = Form(None),
    use_web: bool = Form(True),
    images: list[UploadFile] | None = File(None),
    p: Principal = Depends(principal),
):
    message = (message or "").strip()
    files = images or []
    if not message and not files:
        raise SessionError(422, "ASSISTANT_EMPTY", "请输入问题或上传图片")
    if len(files) > 3:
        raise SessionError(422, "ASSISTANT_IMAGE_LIMIT", "最多上传 3 张图片")
    if document_id:
        _get_owned_document(document_id, p)
    if session_id:
        session = _session_owned(session_id, p)
        document_id = document_id or session.get("document_id") or ""
    hist = []
    if history:
        try:
            raw_hist = json.loads(history)
            if isinstance(raw_hist, list):
                hist = [x for x in raw_hist if isinstance(x, dict)][-8:]
        except Exception:
            hist = []
    data_urls = []
    total_bytes = 0
    for item in files:
        mime = (item.content_type or "").lower()
        if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            raise SessionError(422, "ASSISTANT_IMAGE_TYPE", "只支持 PNG、JPEG、WEBP、GIF 图片")
        content = await item.read()
        total_bytes += len(content)
        if len(content) > 8 * 1024 * 1024 or total_bytes > 16 * 1024 * 1024:
            raise SessionError(413, "ASSISTANT_IMAGE_TOO_LARGE", "单张图片不超过 8 MB，合计不超过 16 MB")
        data_urls.append(f"data:{mime};base64,{base64.b64encode(content).decode()}")
    if not message and data_urls:
        message = "请描述这张图片，并结合文档或权威资料回答我能从中得到什么信息。"
    result = await asyncio.to_thread(
        assistant_api.answer,
        question=message,
        images=data_urls,
        document_id=document_id or "",
        session_id=session_id or "",
        user_id=p.user_id,
        tenant_id=p.tenant_id,
        username=p.username,
        role=p.role,
        history=hist,
        use_web=use_web,
    )
    result["document_id"] = document_id or None
    result["session_id"] = session_id or None
    return result


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------
def _doc_owner_clause(p: Principal) -> tuple[str, tuple]:
    if p.role == "admin":
        return "", ()
    return " AND user_id=?", (p.user_id,)


def _get_owned_document(document_id: str, p: Principal) -> dict:
    clause, params = _doc_owner_clause(p)
    row = fetch_one(
        "SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL" + clause,
        tuple([document_id, p.tenant_id] + list(params)),
    )
    if not row:
        raise SessionError(404, "DOC_NOT_FOUND", "文档不存在或无权访问")
    return row


def _session_owned(session_id: str, p: Principal) -> dict:
    session = sessions.get_session(session_id, p.tenant_id)
    if p.role != "admin" and session.get("user_id") != p.user_id:
        raise SessionError(404, "SESSION_NOT_FOUND", "会话不存在")
    return session


def _doc_api(row: dict) -> dict:
    return {
        "document_id": row["doc_id"],
        "tenant_id": row["tenant_id"],
        "title": row["title"],
        "doc_type": row["doc_type"],
        "source_uri": row.get("source_uri") or "",
        "page_count": row["page_count"],
        "source": {
            "file_name": row["file_name"],
            "mime_type": row["mime_type"],
            "size_bytes": row["size_bytes"],
            "page_count": row["page_count"],
            "sha256": row["sha256"],
        },
        "status": row["status"],
        "doc_version": row["doc_version"],
        "language": row["language"],
        "stats": load_json(row["stats"], {}),
        "quality": {
            "score": row["quality_score"],
            "level": row["quality_level"],
            "issues": load_json(row["quality_issues"], []),
        },
        "error": load_json(row["error"], None),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.post("/api/v1/documents", status_code=202)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    doc_type: str = Form("auto"),
    language: str = Form("zh-CN"),
    title: str | None = Form(None),
    idem: str | None = Header(None, alias="Idempotency-Key"),
    p: Principal = Depends(principal),
):
    require(p, "documents:write")
    cached = _idem_lookup(idem, "POST /documents")
    if cached:
        return JSONResponse(status_code=202, content=cached)
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise SessionError(413, "DOC_TOO_LARGE", "文件超过 100 MB")
    if not content:
        raise SessionError(400, "DOC_UNSUPPORTED", "空文件")
    suffix = Path(file.filename or "").suffix.lower()
    if suffix == ".pdf" and not content[:5].startswith(b"%PDF"):
        raise SessionError(400, "DOC_UNSUPPORTED", "文件不是有效的 PDF")
    try:
        doc, dedup = pipeline.create_document(
            tenant_id=p.tenant_id,
            user_id=p.user_id,
            file_name=file.filename or f"document{suffix}",
            mime_type=file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream",
            content=content,
            doc_type=doc_type,
            language=language,
            title=title,
        )
    except ValueError:
        raise SessionError(400, "DOC_UNSUPPORTED", "暂不支持该文件类型，请上传 PDF、DOCX、Markdown 或图片")
    if dedup:
        payload = {
            "document_id": doc["doc_id"],
            "task_id": None,
            "status": doc["status"],
            "deduplicated": True,
            "file_name": doc["file_name"],
            "page_count": doc["page_count"],
            "created_at": doc["created_at"],
        }
        _idem_save(idem, "POST /documents", payload)
        audit.log_event(
            user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="document_upload",
            detail={"document_id": doc["doc_id"], "file_name": doc["file_name"], "deduplicated": True},
            ip=_request_meta(request)[0], user_agent=_request_meta(request)[1],
        )
        return payload
    task_id = pipeline.start_parse(doc["doc_id"], Path(doc["file_path"]), doc_type, title)
    payload = {
        "document_id": doc["doc_id"],
        "task_id": task_id,
        "status": "processing",
        "deduplicated": False,
        "file_name": file.filename,
        "page_count": 0,
        "created_at": iso(),
    }
    _idem_save(idem, "POST /documents", payload)
    audit.log_event(
        user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="document_upload",
        detail={"document_id": doc["doc_id"], "file_name": file.filename, "deduplicated": False},
        ip=_request_meta(request)[0], user_agent=_request_meta(request)[1],
    )
    return JSONResponse(
        status_code=202,
        content=payload,
        headers={"Location": f"/api/v1/documents/{doc['doc_id']}"},
    )


@app.post("/api/v1/documents/from-url", status_code=202)
async def import_document_url(payload: dict, p: Principal = Depends(principal)):
    require(p, "documents:write")
    url = str(payload.get("url") or "").strip()
    if not url:
        raise SessionError(422, "URL_REQUIRED", "请填写网页链接")
    try:
        fetched = await asyncio.to_thread(url_import.fetch_url, url)
    except ValueError as exc:
        raise SessionError(422, "URL_IMPORT_FAILED", str(exc))
    try:
        doc, dedup = pipeline.create_document(
            tenant_id=p.tenant_id, user_id=p.user_id, file_name=fetched["file_name"],
            mime_type=fetched["mime_type"], content=fetched["content"],
            doc_type=payload.get("doc_type") or "auto", language="zh-CN",
            title=fetched.get("title"), source_uri=fetched.get("source_uri", ""),
        )
    except ValueError:
        raise SessionError(400, "DOC_UNSUPPORTED", "网页内容暂不支持")
    if dedup:
        audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id,
                        event_type="document_import", detail={"url": url, "document_id": doc["doc_id"], "deduplicated": True})
        return {"document_id": doc["doc_id"], "task_id": None, "status": doc["status"], "deduplicated": True,
                "title": doc.get("title"), "source_uri": doc.get("source_uri") or url}
    task_id = pipeline.start_parse(doc["doc_id"], Path(doc["file_path"]), payload.get("doc_type") or "auto", fetched.get("title"))
    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id,
                    event_type="document_import", detail={"url": url, "document_id": doc["doc_id"],
                                                         "file_name": fetched["file_name"]})
    return {"document_id": doc["doc_id"], "task_id": task_id, "status": "processing",
            "deduplicated": False, "title": fetched.get("title"), "source_uri": fetched.get("source_uri", ""),
            "file_name": fetched["file_name"]}


@app.get("/api/v1/documents")
def list_documents(
    status: str | None = None,
    doc_type: str | None = None,
    q: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
    sort: str = "created_at_desc",
    p: Principal = Depends(principal),
):
    clause, owner_params = _doc_owner_clause(p)
    rows = fetch_all(
        "SELECT * FROM document WHERE tenant_id=? AND deleted_at IS NULL" + clause + " ORDER BY created_at DESC",
        tuple([p.tenant_id] + list(owner_params)),
    )
    if status:
        rows = [r for r in rows if r["status"] == status]
    if doc_type:
        rows = [r for r in rows if r["doc_type"] == doc_type]
    if q:
        rows = [r for r in rows if q.lower() in (r["title"] or "").lower()]
    page, next_cursor = _paginate(rows, limit, cursor)
    items = []
    for r in page:
        abstract_row = fetch_one(
            "SELECT text FROM node WHERE doc_id=? AND node_type='abstract' ORDER BY order_index LIMIT 1",
            (r["doc_id"],),
        ) or {}
        items.append({
            "document_id": r["doc_id"],
            "title": r["title"],
            "doc_type": r["doc_type"],
            "status": r["status"],
            "page_count": r["page_count"],
            "stats": load_json(r["stats"], {}),
            "abstract": (abstract_row.get("text") or "")[:220],
            "quality": {"score": r["quality_score"], "level": r["quality_level"]},
            "created_at": r["created_at"],
        })
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
    }


@app.get("/api/v1/documents/{document_id}")
def get_document(document_id: str, p: Principal = Depends(principal)):
    row = _get_owned_document(document_id, p)
    return _doc_api(row)


@app.delete("/api/v1/documents/{document_id}", status_code=204)
def delete_document(document_id: str, purge: bool = False, p: Principal = Depends(principal)):
    require(p, "documents:write")
    _get_owned_document(document_id, p)
    with db() as conn:
        conn.execute("UPDATE document SET deleted_at=?, status='deleted', updated_at=? WHERE doc_id=?", (iso(), iso(), document_id))
    if purge:
        for suffix in (".pdf", ".docx", ".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            try:
                storage_mod.delete(f"{document_id}{suffix}")
            except Exception:
                pass
        for path in (DATA_DIR / "uploads").glob(f"{document_id}.*"):
            try:
                path.unlink()
            except Exception:
                pass
    return Response(status_code=204)


@app.post("/api/v1/documents/{document_id}/reparse", status_code=202)
def reparse_document(document_id: str, payload: dict | None = None, p: Principal = Depends(principal)):
    require(p, "documents:write")
    row = _get_owned_document(document_id, p)
    path = pipeline.get_document_path(document_id)
    if not path:
        raise SessionError(404, "DOC_NOT_FOUND", "原始文件缺失")
    payload = payload or {}
    task_id = pipeline.start_parse(document_id, path, payload.get("template") or row["doc_type"], None)
    with db() as conn:
        conn.execute("UPDATE document SET status='parsing', updated_at=? WHERE doc_id=?", (iso(), document_id))
    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="document_reparse",
                    detail={"document_id": document_id})
    return {"document_id": document_id, "task_id": task_id, "status": "queued"}


@app.get("/api/v1/documents/{document_id}/outline")
def get_outline(document_id: str, depth: int = 3, include_text: bool = False, node_types: str | None = None, p: Principal = Depends(principal)):
    row = _get_owned_document(document_id, p)
    nodes = fetch_all("SELECT * FROM node WHERE doc_id=? ORDER BY order_index", (document_id,))
    by_id = {n["node_id"]: n for n in nodes}
    allowed_types = set(node_types.split(",")) if node_types else None
    depth = max(1, min(6, int(depth or 3)))

    def build(parent_id):
        out = []
        for n in sorted([x for x in nodes if x["parent_id"] == parent_id], key=lambda x: (x["order_no"], x["order_index"])):
            if n["level"] > depth:
                continue
            if allowed_types and n["node_type"] not in allowed_types:
                continue
            item = {
                "node_id": n["node_id"],
                "parent_id": n["parent_id"],
                "level": n["level"],
                "node_type": n["node_type"],
                "order_no": n["order_no"],
                "title": n["title"],
                "page_start": n["page_start"],
                "page_end": n["page_end"],
                "char_count": n["char_count"],
                "confidence": n["confidence"],
                "children": build(n["node_id"]),
            }
            if include_text:
                item["text"] = n["text"]
            out.append(item)
        return out

    return {"document_id": document_id, "version": row["doc_version"], "nodes": build(None)}


@app.put("/api/v1/documents/{document_id}/outline")
def update_outline(document_id: str, payload: dict, p: Principal = Depends(principal)):
    require(p, "documents:write")
    row = _get_owned_document(document_id, p)
    ops = payload.get("operations") or []
    versions.snapshot(document_id, reason="结构编辑前自动快照", created_by=p.username or p.user_id)
    applied = 0
    with db() as conn:
        for op in ops:
            if not isinstance(op, dict):
                continue
            node_id = op.get("node_id")
            if not node_id or not conn.execute("SELECT 1 FROM node WHERE node_id=? AND doc_id=?", (node_id, document_id)).fetchone():
                raise SessionError(422, "OUTLINE_INVALID", "结构节点不存在", {"node_id": node_id})
            if op.get("op") == "update":
                sets, vals = [], []
                for k in ("title", "level"):
                    if k in op:
                        sets.append(f"{k}=?")
                        vals.append(op[k])
                if sets:
                    vals.append(node_id)
                    conn.execute(f"UPDATE node SET {', '.join(sets)} WHERE node_id=?", tuple(vals))
            elif op.get("op") == "move":
                conn.execute(
                    "UPDATE node SET parent_id=?, order_no=? WHERE node_id=?",
                    (op.get("parent_id"), int(op.get("order_no") or 1), node_id),
                )
            elif op.get("op") == "delete":
                conn.execute("DELETE FROM node WHERE node_id=? OR parent_id=?", (node_id, node_id))
            elif op.get("op") == "merge":
                target_id = op.get("target_id")
                if not target_id:
                    sibling = conn.execute(
                        "SELECT node_id FROM node WHERE doc_id=? AND parent_id IS ? AND order_no>?"
                        " ORDER BY order_no LIMIT 1",
                        (document_id, op.get("parent_id"), int(op.get("order_no") or 0)),
                    ).fetchone()
                    target_id = sibling["node_id"] if sibling else None
                if not target_id or not conn.execute("SELECT 1 FROM node WHERE node_id=? AND doc_id=?", (target_id, document_id)).fetchone():
                    raise SessionError(422, "OUTLINE_INVALID", "合并目标不存在", {"node_id": node_id})
                source = conn.execute("SELECT * FROM node WHERE node_id=?", (node_id,)).fetchone()
                target = conn.execute("SELECT * FROM node WHERE node_id=?", (target_id,)).fetchone()
                merged = (target["text"] or "") + "\n\n" + (source["text"] or "")
                conn.execute("UPDATE node SET text=?, char_count=? WHERE node_id=?",
                             (merged.strip(), len(merged.strip()), target_id))
                conn.execute("UPDATE node SET parent_id=? WHERE parent_id=? AND node_id<>?",
                             (target_id, node_id, target_id))
                conn.execute("DELETE FROM node WHERE node_id=?", (node_id,))
            applied += 1
        new_version = row["doc_version"] + 1
        conn.execute("UPDATE document SET doc_version=?, updated_at=? WHERE doc_id=?", (new_version, iso(), document_id))
    return {"document_id": document_id, "version": new_version, "applied": applied, "updated_at": iso()}


@app.get("/api/v1/documents/{document_id}/quality")
def get_quality(document_id: str, p: Principal = Depends(principal)):
    row = _get_owned_document(document_id, p)
    issues = load_json(row["quality_issues"], [])
    stats = load_json(row["stats"], {})
    qm = stats.get("quality_metrics") or {}
    coverage = qm.get("text_coverage", 1.0 if stats.get("node_count") else 0)
    heading = qm.get("heading_confidence", 0.95 if stats.get("section_count", 0) >= 3 else 0.6)
    garbled = qm.get("garbled_rate", 0.0)
    figure = qm.get("figure_link_rate", 0.95 if stats.get("figure_count", 0) else 1.0)
    ocr_pages = int(stats.get("ocr_pages") or 0)
    return {
        "document_id": document_id,
        "score": row["quality_score"],
        "level": row["quality_level"],
        "metrics": [
            {"name": "text_coverage", "value": coverage, "threshold": 0.98,
             "passed": coverage >= 0.98 or (ocr_pages > 0 and coverage >= 0.6)},
            {"name": "heading_f1", "value": heading, "threshold": 0.95, "passed": heading >= 0.95},
            {"name": "garbled_rate", "value": garbled, "threshold": 0.005, "passed": garbled <= 0.005},
            {"name": "figure_link_rate", "value": figure, "threshold": 0.90, "passed": figure >= 0.90},
            {"name": "ocr_pages", "value": stats.get("ocr_pages", 0), "threshold": None, "passed": True},
        ],
        "ocr": {"pages": stats.get("ocr_pages", 0), "model": stats.get("ocr_model", "")},
        "failed_pages": [],
        "suggestions": [i.get("message", "") for i in issues],
        "issues": issues,
    }


@app.get("/api/v1/documents/{document_id}/knowledge-points")
def list_kps(document_id: str, node_id: str | None = None, kp_type: str | None = None, limit: int = 100, cursor: str | None = None, p: Principal = Depends(principal)):
    _ensure_doc(document_id, p)
    sql = "SELECT * FROM knowledge_point WHERE doc_id=?"
    params: list = [document_id]
    if node_id:
        sql += " AND node_id=?"
        params.append(node_id)
    if kp_type:
        sql += " AND kp_type=?"
        params.append(kp_type)
    sql += " ORDER BY importance DESC, order_no"
    rows = fetch_all(sql, params)
    page, next_cursor = _paginate(rows, limit, cursor)
    return {
        "items": [
            {
                "kp_id": r["kp_id"],
                "node_id": r["node_id"],
                "name": r["name"],
                "kp_type": r["kp_type"],
                "definition": r["definition"],
                "importance": r["importance"],
                "difficulty": r["difficulty"],
                "evidence_count": len(load_json(r["evidence_ids"], [])),
                "prerequisites": load_json(r["prerequisites"], []),
            }
            for r in page
        ],
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
    }


@app.get("/api/v1/documents/{document_id}/evidence")
def list_evidence(document_id: str, node_id: str | None = None, strength: str | None = None, limit: int = 100, cursor: str | None = None, p: Principal = Depends(principal)):
    _ensure_doc(document_id, p)
    sql = "SELECT * FROM evidence WHERE doc_id=?"
    params: list = [document_id]
    if node_id:
        sql += " AND node_id=?"
        params.append(node_id)
    if strength:
        sql += " AND strength=?"
        params.append(strength)
    sql += " ORDER BY page_no, char_start"
    rows = fetch_all(sql, params)
    page, next_cursor = _paginate(rows, limit, cursor)
    return {
        "items": [
            {
                "evidence_id": r["evidence_id"],
                "node_id": r["node_id"],
                "page_no": r["page_no"],
                "quote": r["quote"],
                "char_start": r["char_start"],
                "char_end": r["char_end"],
                "strength": r["strength"],
            }
            for r in page
        ],
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
    }


@app.get("/api/v1/documents/{document_id}/file")
def get_document_file(document_id: str, p: Principal = Depends(principal)):
    row = _get_owned_document(document_id, p)
    path = pipeline.get_document_path(document_id)
    if not path:
        raise SessionError(404, "DOC_NOT_FOUND", "原始文件缺失")
    return FileResponse(path, media_type=row["mime_type"], filename=row["file_name"])


def _ensure_doc(document_id: str, p: Principal) -> dict:
    return _get_owned_document(document_id, p)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
def _task_api(row: dict) -> dict:
    return {
        "task_id": row["task_id"],
        "type": row["type"],
        "status": row["status"],
        "stage": row["stage"],
        "progress": row["progress"],
        "document_id": row["document_id"],
        "result": load_json(row["result"], None),
        "error": load_json(row["error"], None),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str, p: Principal = Depends(principal)):
    row = fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
    if not row:
        raise SessionError(404, "TASK_NOT_FOUND", "任务不存在")
    return _task_api(row)


@app.get("/api/v1/tasks/{task_id}/events")
async def task_events(task_id: str, request: Request):
    row = fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
    if not row:
        raise SessionError(404, "TASK_NOT_FOUND", "任务不存在")

    async def stream():
        last = None
        sent = 0
        while sent < 1800:
            row = fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
            if not row:
                break
            data = _task_api(row)
            if data != last:
                if last is None:
                    yield f"event: snapshot\nid: {sent}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                elif row["status"] in ("succeeded", "failed", "canceled"):
                    event = "completed" if row["status"] == "succeeded" else "failed"
                    payload = {"task_id": task_id, "status": row["status"], "result": data["result"], "error": data["error"]}
                    yield f"event: {event}\nid: {sent}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    break
                else:
                    payload = {"task_id": task_id, "stage": row["stage"], "progress": row["progress"], "message": row["stage"]}
                    yield f"event: progress\nid: {sent}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                last = data
            if await request.is_disconnected():
                break
            await asyncio.sleep(0.5)
            sent += 1
            if sent % 20 == 0:
                yield f"event: heartbeat\ndata: {json.dumps({'timestamp': iso()})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/v1/tasks/{task_id}/cancel")
def cancel_task(task_id: str, payload: dict | None = None):
    row = fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
    if not row:
        raise SessionError(404, "TASK_NOT_FOUND", "任务不存在")
    if row["status"] in ("succeeded", "failed", "canceled"):
        return {"task_id": task_id, "status": row["status"], "canceled_at": row["updated_at"]}
    with db() as conn:
        conn.execute("UPDATE task SET status='canceled', updated_at=? WHERE task_id=?", (iso(), task_id))
    return {"task_id": task_id, "status": "canceled", "canceled_at": iso()}


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
@app.post("/api/v1/sessions", status_code=201)
def create_session(payload: dict, idem: str | None = Header(None, alias="Idempotency-Key"), p: Principal = Depends(principal)):
    require(p, "sessions:write")
    cached = _idem_lookup(idem, "POST /sessions")
    if cached:
        return JSONResponse(status_code=201, content=cached)
    session = sessions.create_session(
        doc_id=payload.get("document_id"),
        user_id=p.user_id,
        tenant_id=p.tenant_id,
        mode=payload.get("mode", "guided"),
        question_count=int(payload.get("question_count", 12)),
        difficulty=payload.get("difficulty", "auto"),
        focus_sections=payload.get("focus_sections"),
        exclude_node_ids=payload.get("exclude_node_ids"),
        question_types=payload.get("question_types"),
    )
    _idem_save(idem, "POST /sessions", session)
    return session


@app.get("/api/v1/sessions")
def list_sessions(
    document_id: str | None = None,
    state: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
    p: Principal = Depends(principal),
):
    sql = "SELECT s.* FROM session s JOIN document d ON d.doc_id=s.doc_id WHERE s.tenant_id=? AND d.deleted_at IS NULL"
    params: list = [p.tenant_id]
    if p.role != "admin":
        sql += " AND s.user_id=?"
        params.append(p.user_id)
    sql += " ORDER BY s.started_at DESC"
    rows = fetch_all(sql, params)
    if document_id:
        rows = [r for r in rows if r["doc_id"] == document_id]
    if state:
        rows = [r for r in rows if r["state"] == state]
    page, next_cursor = _paginate(rows, limit, cursor)
    return {
        "items": [
            {
                "session_id": r["session_id"],
                "document_id": r["doc_id"],
                "mode": r["mode"],
                "state": r["state"],
                "score": r["score"],
                "total_questions": r["total_questions"],
                "created_at": r["started_at"],
            }
            for r in page
        ],
        "next_cursor": next_cursor,
        "has_more": bool(next_cursor),
    }


@app.get("/api/v1/sessions/{session_id}")
def get_session(session_id: str, p: Principal = Depends(principal)):
    return _session_owned(session_id, p)


@app.post("/api/v1/sessions/{session_id}/next-question")
def next_question(session_id: str, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return sessions.next_question(session_id, p.tenant_id)


@app.post("/api/v1/sessions/{session_id}/answers")
def submit_answer(session_id: str, payload: dict, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return sessions.submit_answer(session_id, payload, p.tenant_id)


@app.post("/api/v1/sessions/{session_id}/hint")
def hint(session_id: str, payload: dict, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return sessions.get_hint(session_id, payload.get("turn_id"), int(payload.get("level") or 1), p.tenant_id)


@app.post("/api/v1/sessions/{session_id}/follow-up")
def follow_up(session_id: str, payload: dict, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return sessions.create_follow_up(session_id, payload.get("turn_id"), payload.get("kind", "clarify"), p.tenant_id)


@app.get("/api/v1/sessions/{session_id}/follow-ups")
def list_follow_ups(session_id: str, turn_id: str | None = None, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return {"items": sessions.list_follow_ups(session_id, turn_id, p.tenant_id)}


@app.post("/api/v1/sessions/{session_id}/follow-up/answer")
def follow_up_answer(session_id: str, payload: dict, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return sessions.answer_follow_up(session_id, payload.get("turn_id"), payload.get("kind", "clarify"),
                                     payload.get("answer", ""), p.tenant_id, payload.get("follow_up_id"))


@app.post("/api/v1/sessions/{session_id}/skip")
def skip(session_id: str, payload: dict | None = None, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    payload = payload or {}
    return sessions.skip_question(session_id, payload.get("turn_id"), payload.get("reason", ""), p.tenant_id)


@app.post("/api/v1/sessions/{session_id}/finish", status_code=202)
def finish(session_id: str, payload: dict | None = None, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return sessions.finish_session(session_id, p.tenant_id, (payload or {}).get("reason", "user_finished"))


@app.get("/api/v1/sessions/{session_id}/report")
def get_report(session_id: str, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return sessions.get_report(session_id, p.tenant_id)


@app.get("/api/v1/sessions/{session_id}/turns/{seq_no}")
def get_turn_review(session_id: str, seq_no: int, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return sessions.get_turn_review(session_id, seq_no, p.tenant_id)


@app.get("/api/v1/sessions/{session_id}/turns")
def get_turns(session_id: str, include_answer: bool = False, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    return {"items": sessions.list_turns(session_id, p.tenant_id), "next_cursor": None, "has_more": False}


@app.delete("/api/v1/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, purge_report: bool = False, p: Principal = Depends(principal)):
    _session_owned(session_id, p)
    with db() as conn:
        conn.execute("DELETE FROM turn WHERE session_id=?", (session_id,))
        conn.execute("DELETE FROM session WHERE session_id=?", (session_id,))
        if purge_report:
            conn.execute("DELETE FROM report WHERE session_id=?", (session_id,))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 题库与报告
# ---------------------------------------------------------------------------
def _question_api(row: dict, include_answer: bool = False) -> dict:
    out = {
        "q_id": row["q_id"],
        "document_id": row["doc_id"],
        "node_id": row["node_id"],
        "kp_id": row["kp_id"],
        "q_type": row["q_type"],
        "bloom_level": row["bloom_level"],
        "difficulty": row["difficulty"],
        "stem": row["stem"],
        "options": load_json(row["options"], []),
        "time_limit_sec": row["time_limit_sec"],
        "hint_levels": row["hint_levels"],
        "status": row["status"],
        "version": row["version"],
        "source": row["source"],
    }
    if include_answer:
        out["answer"] = load_json(row["answer"], {})
        out["rubric"] = load_json(row["rubric"], [])
        out["evidence"] = load_json(row["evidence"], [])
        out["hints"] = load_json(row["hints"], [])
    return out


@app.get("/api/v1/questions")
def list_questions(
    document_id: str | None = None,
    node_id: str | None = None,
    q_type: str | None = None,
    bloom_level: str | None = None,
    difficulty_min: int | None = None,
    difficulty_max: int | None = None,
    status: str = "active",
    include_answer: bool = False,
    limit: int = 20,
    cursor: str | None = None,
    p: Principal = Depends(principal),
):
    require_admin(p)
    sql = "SELECT * FROM question WHERE 1=1"
    params: list = []
    if document_id:
        sql += " AND doc_id=?"
        params.append(document_id)
    if node_id:
        sql += " AND node_id=?"
        params.append(node_id)
    if q_type:
        sql += " AND q_type=?"
        params.append(q_type)
    if bloom_level:
        sql += " AND bloom_level=?"
        params.append(bloom_level)
    if difficulty_min is not None:
        sql += " AND difficulty>=?"
        params.append(int(difficulty_min))
    if difficulty_max is not None:
        sql += " AND difficulty<=?"
        params.append(int(difficulty_max))
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    rows = fetch_all(sql, params)
    page, next_cursor = _paginate(rows, limit, cursor)
    return {"items": [_question_api(r, include_answer) for r in page], "next_cursor": next_cursor, "has_more": bool(next_cursor)}


@app.get("/api/v1/questions/{q_id}")
def get_question(q_id: str, include_answer: bool = True, p: Principal = Depends(principal)):
    require_admin(p)
    row = fetch_one("SELECT * FROM question WHERE q_id=?", (q_id,))
    if not row:
        raise SessionError(404, "QUESTION_NOT_FOUND", "题目不存在")
    return _question_api(row, include_answer)


@app.patch("/api/v1/questions/{q_id}")
def patch_question(q_id: str, payload: dict, if_match: str | None = Header(None, alias="If-Match"),
                  p: Principal = Depends(principal)):
    require_admin(p)
    row = fetch_one("SELECT * FROM question WHERE q_id=?", (q_id,))
    if not row:
        raise SessionError(404, "QUESTION_NOT_FOUND", "题目不存在")
    if if_match and str(row["version"]) != str(if_match):
        raise SessionError(409, "VERSION_CONFLICT", "题目版本已变化，请刷新后重试")
    fields = {}
    for k in ("stem", "difficulty"):
        if k in payload:
            fields[k] = payload[k]
    if "options" in payload:
        fields["options"] = dump(payload["options"])
    if "answer" in payload:
        fields["answer"] = dump(payload["answer"])
    if "rubric" in payload:
        fields["rubric"] = dump(payload["rubric"])
    fields["version"] = int(row["version"]) + 1
    fields["status"] = "pending_review"
    with db() as conn:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE question SET {sets} WHERE q_id=?", tuple(fields.values()) + (q_id,))
    return {"q_id": q_id, "version": fields["version"], "status": "pending_review"}


@app.post("/api/v1/questions/{q_id}/review")
def review_question(q_id: str, payload: dict, p: Principal = Depends(principal)):
    require_admin(p)
    row = fetch_one("SELECT * FROM question WHERE q_id=?", (q_id,))
    if not row:
        raise SessionError(404, "QUESTION_NOT_FOUND", "题目不存在")
    decision = payload.get("decision", "approve")
    status = "active" if decision == "approve" else "retired" if decision == "retire" else "pending_review"
    with db() as conn:
        conn.execute("UPDATE question SET status=? WHERE q_id=?", (status, q_id))
    return {"q_id": q_id, "status": status, "reviewed_at": iso()}


@app.get("/api/v1/question-sets")
def list_question_sets(document_id: str | None = None, p: Principal = Depends(principal)):
    require_admin(p)
    sql = "SELECT * FROM question_set"
    params = []
    if document_id:
        sql += " WHERE doc_id=?"
        params.append(document_id)
    sql += " ORDER BY created_at DESC LIMIT 100"
    rows = fetch_all(sql, params)
    for row in rows:
        row["question_ids"] = load_json(row.get("question_ids"), [])
    return {"items": rows}


@app.post("/api/v1/question-sets", status_code=201)
def create_question_set(payload: dict, p: Principal = Depends(principal)):
    require_admin(p)
    doc_id = str(payload.get("document_id") or "")
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (doc_id,))
    if not doc:
        raise SessionError(404, "DOC_NOT_FOUND", "文档不存在")
    difficulty = str(payload.get("difficulty") or "mixed")
    ranges = {"easy": (1, 2), "medium": (3, 3), "hard": (4, 5), "mixed": (1, 5)}
    lo, hi = ranges.get(difficulty, (1, 5))
    limit = max(1, min(30, int(payload.get("count") or 12)))
    rows = fetch_all(
        "SELECT * FROM question WHERE doc_id=? AND status='active' AND difficulty BETWEEN ? AND ?"
        " ORDER BY RANDOM() LIMIT ?", (doc_id, lo, hi, limit),
    )
    if not rows:
        raise SessionError(409, "QUESTION_SET_EMPTY", "该难度暂无可用题目，请先完成几轮会话或复核题目")
    title = str(payload.get("title") or f"{doc['title']} · {difficulty} 题卷")
    set_id = new_id("qset")
    ids = [r["q_id"] for r in rows]
    with db() as conn:
        conn.execute(
            "INSERT INTO question_set (set_id, doc_id, tenant_id, title, difficulty, question_ids, question_count,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (set_id, doc_id, p.tenant_id, title, difficulty, dump(ids), len(ids), p.user_id, iso()),
        )
    return {"set_id": set_id, "title": title, "difficulty": difficulty, "question_count": len(ids),
            "requested": limit, "question_ids": ids,
            "warning": None if len(ids) >= limit else f"可用题目不足，已生成 {len(ids)} 题"}


@app.delete("/api/v1/question-sets/{set_id}", status_code=204)
def delete_question_set(set_id: str, p: Principal = Depends(principal)):
    require_admin(p)
    with db() as conn:
        conn.execute("DELETE FROM question_set WHERE set_id=?", (set_id,))
    return Response(status_code=204)


def _report_owned(report_id: str, p: Principal) -> dict:
    row = fetch_one("SELECT * FROM report WHERE report_id=?", (report_id,))
    if not row:
        raise SessionError(404, "REPORT_NOT_FOUND", "报告不存在")
    if p.role != "admin":
        session = fetch_one("SELECT user_id FROM session WHERE session_id=?", (row["session_id"],))
        if not session or session.get("user_id") != p.user_id:
            raise SessionError(404, "REPORT_NOT_FOUND", "报告不存在")
    return row


@app.get("/api/v1/reports/{report_id}")
def get_report_by_id(report_id: str, p: Principal = Depends(principal)):
    row = _report_owned(report_id, p)
    return sessions._report_api(row)


@app.get("/api/v1/reports/{report_id}/export")
def export_report(report_id: str, format: str = "md", p: Principal = Depends(principal)):
    row = _report_owned(report_id, p)
    report = sessions._report_api(row)
    text = _report_markdown(report)
    suffix = ".md"
    path = EXPORT_DIR / f"{report_id}{suffix}"
    path.write_text(text, encoding="utf-8")
    return {
        "download_url": f"/api/v1/reports/{report_id}/download",
        "expires_at": iso(now() + timedelta(hours=1)),
        "size_bytes": path.stat().st_size,
        "format": "md",
    }


@app.get("/api/v1/reports/{report_id}/download")
def download_report(report_id: str, p: Principal = Depends(principal)):
    _report_owned(report_id, p)
    path = EXPORT_DIR / f"{report_id}.md"
    if not path.exists():
        raise SessionError(404, "REPORT_NOT_FOUND", "报告文件不存在")
    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=f"{report_id}.md")


def _report_markdown(report: dict) -> str:
    s = report["summary"]
    lines = [
        f"# A论 · 学习报告",
        "",
        f"- 总分：{s.get('score')} / 100",
        f"- 题量：{s.get('total')}（已答 {s.get('answered', 0)}）",
        f"- 正确题数：{s.get('correct', 0)}",
        f"- 用时：{s.get('duration_sec', 0)} 秒",
        f"- 能力估计：{s.get('ability', 500)}",
        "",
        "## 章节掌握度",
        "",
    ]
    for item in report["section_mastery"]:
        lines.append(f"- {item['title']}：{int(item['score']*100)}%（{item['question_count']} 题）")
    lines += ["", "## 错题清单", ""]
    for w in report["wrong_questions"]:
        lines.append(f"- {w['stem']}（第 {w.get('page')} 页）：{w.get('user_answer')}")
    lines += ["", "## 改进建议", ""]
    for r in report["recommendations"]:
        lines.append(f"1. {r}")
    return "\n".join(lines)


@app.get("/api/v1/usage")
def usage(from_: str | None = Query(None, alias="from"), to: str | None = None, group_by: str = "day",
          p: Principal = Depends(principal)):
    require_admin(p)
    rows = fetch_all("SELECT * FROM usage_log ORDER BY ts DESC LIMIT 5000")
    total_requests = len(rows)
    total_credits = round(sum(float(r["credits"] or 0) for r in rows), 2)
    return {
        "items": [
            {"date": iso()[:10], "requests": total_requests, "documents": fetch_one("SELECT COUNT(*) AS c FROM document")["c"],
             "input_tokens": sum(int(r["prompt_tokens"] or 0) for r in rows),
             "output_tokens": sum(int(r["completion_tokens"] or 0) for r in rows),
             "cost_cny": total_credits}
        ],
        "total": {"requests": total_requests, "cost_cny": total_credits},
    }


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------
if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")


@app.get("/")
def index():
    path = WEB_DIR / "index.html"
    if not path.exists():
        return JSONResponse({"name": APP_NAME, "message": "前端尚未构建"})
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/teacher")
@app.get("/teacher/")
def teacher_page():
    return FileResponse(WEB_DIR / "admin.html")


@app.get("/admin")
@app.get("/admin/")
def admin_page():
    path = WEB_DIR / "admin.html"
    if not path.exists():
        return JSONResponse({"name": APP_NAME, "message": "管理端尚未构建"})
    return FileResponse(path, media_type="text/html; charset=utf-8")


@app.get("/{path:path}")
def spa_fallback(path: str):
    if path.startswith("api/"):
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "接口不存在"})
    target = WEB_DIR / path
    if target.exists() and target.is_file():
        return FileResponse(target)
    index_path = WEB_DIR / "index.html"
    if index_path.exists() and "." not in path:
        return FileResponse(index_path, media_type="text/html; charset=utf-8")
    raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "资源不存在"})
