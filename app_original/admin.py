"""后台管理 API：总览、用户、实时事件、模型设置与 token 用量。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pathlib import Path

from . import audit, llm, settings
from .config import UPLOAD_DIR
from .db import db, fetch_all, fetch_one, iso, load_json, now
from .security import Principal, principal_from_request, require_admin

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def admin_principal(request: Request) -> Principal:
    p = principal_from_request(request)
    require_admin(p)
    return p


def _accuracy() -> tuple[int, float]:
    row = fetch_one(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN score>=80 THEN 1 ELSE 0 END) AS good"
        " FROM turn WHERE status='answered'"
    ) or {}
    total = int(row.get("total") or 0)
    good = int(row.get("good") or 0)
    return total, round(good / total, 4) if total else 0.0


def _hallucination() -> dict:
    row = fetch_one(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN grounded=0 THEN 1 ELSE 0 END) AS bad"
        " FROM ai_audit WHERE fallback=0"
    ) or {}
    total = int(row.get("total") or 0)
    bad = int(row.get("bad") or 0)
    return {"total": total, "invalid": bad, "rate": round(bad / total, 4) if total else 0.0}


@router.get("/overview")
def overview(p: Principal = Depends(admin_principal)):
    today = now().strftime("%Y-%m-%d")
    users = fetch_one("SELECT COUNT(*) AS c FROM app_user WHERE status='active'")["c"]
    active_today = fetch_one(
        "SELECT COUNT(DISTINCT user_id) AS c FROM activity_event WHERE user_id<>'' AND substr(ts,1,10)=?", (today,)
    )["c"]
    docs = fetch_one("SELECT COUNT(*) AS c FROM document WHERE deleted_at IS NULL")["c"]
    sessions = fetch_one(
        "SELECT COUNT(*) AS c FROM session s JOIN document d ON d.doc_id=s.doc_id WHERE d.deleted_at IS NULL"
    )["c"]
    answers, accuracy = _accuracy()
    hall = _hallucination()
    token_row = fetch_one(
        "SELECT COALESCE(SUM(prompt_tokens),0) AS p, COALESCE(SUM(completion_tokens),0) AS c,"
        " COALESCE(SUM(credits),0) AS credit FROM usage_log"
    ) or {}
    today_tokens = fetch_one(
        "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_log WHERE substr(ts,1,10)=?",
        (today,),
    )["t"]
    today_answers = fetch_one(
        "SELECT COUNT(*) AS c FROM turn WHERE status='answered' AND substr(answered_at,1,10)=?", (today,)
    )["c"]
    trend = []
    for i in range(13, -1, -1):
        day = (now() - timedelta(days=i)).strftime("%Y-%m-%d")
        a = fetch_one(
            "SELECT COUNT(*) AS c, COALESCE(AVG(score),0) AS s FROM turn WHERE status='answered' AND substr(answered_at,1,10)=?",
            (day,),
        ) or {}
        t = fetch_one(
            "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS n FROM usage_log WHERE substr(ts,1,10)=?",
            (day,),
        ) or {}
        e = fetch_one("SELECT COUNT(*) AS c FROM activity_event WHERE substr(ts,1,10)=?", (day,))["c"]
        trend.append({"date": day, "answers": int(a.get("c") or 0), "accuracy": round(float(a.get("s") or 0) / 100, 4),
                      "tokens": int(t.get("n") or 0), "events": int(e)})
    recent = audit.recent_events(limit=8)
    return {
        "cards": {
            "users": users,
            "active_today": active_today,
            "documents": docs,
            "sessions": sessions,
            "answers": answers,
            "accuracy": accuracy,
            "hallucination_rate": hall["rate"],
            "hallucination_total": hall["total"],
            "hallucination_invalid": hall["invalid"],
            "tokens": int(token_row.get("p") or 0) + int(token_row.get("c") or 0),
            "today_tokens": int(today_tokens or 0),
            "today_answers": int(today_answers or 0),
            "credits": round(float(token_row.get("credit") or 0), 2),
        },
        "trend": trend,
        "recent_events": recent,
        "model": {"model": settings.get("llm.model"), "enabled": settings.get_bool("llm.enabled")},
    }


@router.get("/documents")
def documents(q: str = "", status: str = "", p: Principal = Depends(admin_principal)):
    sql = (
        "SELECT d.*, u.username, u.display_name FROM document d"
        " LEFT JOIN app_user u ON u.user_id=d.user_id"
        " WHERE d.deleted_at IS NULL"
    )
    params: list = []
    if status:
        sql += " AND d.status=?"
        params.append(status)
    if q:
        sql += " AND (d.title LIKE ? OR d.file_name LIKE ?)"
        params.extend([f"%{q}%", f"%{q}%"])
    sql += " ORDER BY d.created_at DESC"
    rows = fetch_all(sql, params)
    items = []
    for d in rows:
        stats = load_json(d.get("stats"), {})
        issues = load_json(d.get("quality_issues"), [])
        items.append({
            "document_id": d["doc_id"],
            "title": d.get("title") or d.get("file_name") or "未命名文档",
            "file_name": d.get("file_name"),
            "doc_type": d.get("doc_type"),
            "page_count": d.get("page_count") or 0,
            "size_bytes": d.get("size_bytes") or 0,
            "status": d.get("status"),
            "version": d.get("doc_version"),
            "owner": {"user_id": d.get("user_id"), "username": d.get("username") or "",
                      "display_name": d.get("display_name") or d.get("username") or ""},
            "quality": {"score": d.get("quality_score") or 0, "level": d.get("quality_level") or "review",
                        "issues": issues},
            "stats": stats,
            "created_at": d.get("created_at"),
        })
    return {"items": items, "total": len(items)}


@router.delete("/documents/{document_id}")
def delete_document(document_id: str, purge: bool = True, p: Principal = Depends(admin_principal)):
    row = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (document_id,))
    if not row:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    with db() as conn:
        conn.execute(
            "UPDATE document SET deleted_at=?, status='deleted', updated_at=? WHERE doc_id=?",
            (iso(), iso(), document_id),
        )
    if purge:
        try:
            path = Path(row.get("file_path") or "")
            if path.is_file():
                path.unlink()
        except Exception:
            pass
        for path in UPLOAD_DIR.glob(f"{document_id}.*"):
            try:
                path.unlink()
            except Exception:
                pass
    audit.log_event(
        user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="document_delete",
        detail={"document_id": document_id, "title": row.get("title"), "purged": bool(purge)},
    )
    return {"document_id": document_id, "deleted": True, "purged": bool(purge)}


@router.get("/users")
def users(p: Principal = Depends(admin_principal)):
    rows = fetch_all("SELECT * FROM app_user ORDER BY role DESC, created_at")
    items = []
    for u in rows:
        uid = u["user_id"]
        docs = fetch_one("SELECT COUNT(*) AS c FROM document WHERE user_id=? AND deleted_at IS NULL", (uid,))["c"]
        sessions = fetch_one(
            "SELECT COUNT(*) AS c FROM session s JOIN document d ON d.doc_id=s.doc_id"
            " WHERE s.user_id=? AND d.deleted_at IS NULL", (uid,)
        )["c"]
        answer_row = fetch_one(
            "SELECT COUNT(*) AS c, COALESCE(AVG(t.score),0) AS s, SUM(CASE WHEN t.score>=80 THEN 1 ELSE 0 END) AS good"
            " FROM turn t JOIN session s ON s.session_id=t.session_id WHERE s.user_id=? AND t.status='answered'",
            (uid,),
        ) or {}
        last_event = fetch_one("SELECT MAX(ts) AS ts FROM activity_event WHERE user_id=?", (uid,)) or {}
        tokens = fetch_one(
            "SELECT COALESCE(SUM(u.prompt_tokens+u.completion_tokens),0) AS n FROM usage_log u"
            " LEFT JOIN session s ON s.session_id=u.session_id"
            " LEFT JOIN document d ON d.doc_id=u.document_id"
            " WHERE s.user_id=? OR d.user_id=?",
            (uid, uid),
        )["n"]
        items.append({
            "user_id": uid,
            "username": u["username"],
            "display_name": u["display_name"] or u["username"],
            "email": u.get("email") or "",
            "phone": u.get("phone") or "",
            "role": u["role"],
            "status": u["status"],
            "tenant_id": u["tenant_id"],
            "documents": int(docs),
            "sessions": int(sessions),
            "answers": int(answer_row.get("c") or 0),
            "accuracy": round(float(answer_row.get("s") or 0) / 100, 4),
            "tokens": int(tokens or 0),
            "last_login_at": u["last_login_at"],
            "last_active_at": last_event.get("ts"),
        })
    return {"items": items}


@router.post("/users/{user_id}/status")
def set_user_status(user_id: str, payload: dict, p: Principal = Depends(admin_principal)):
    status = payload.get("status", "active")
    if status not in ("active", "disabled"):
        raise HTTPException(status_code=422, detail={"code": "INVALID_STATUS", "message": "状态只能是 active / disabled"})
    if fetch_one("SELECT user_id FROM app_user WHERE user_id=?", (user_id,)) is None:
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"})
    with db() as conn:
        conn.execute("UPDATE app_user SET status=? WHERE user_id=?", (status, user_id))
    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="user_status",
                    detail={"target": user_id, "status": status})
    return {"user_id": user_id, "status": status}


@router.get("/events")
def events(limit: int = 100, since_id: int = 0, p: Principal = Depends(admin_principal)):
    items = audit.recent_events(limit=limit, since_id=since_id)
    max_id = max((x["id"] for x in items), default=since_id)
    return {"items": items, "max_id": max_id}


@router.get("/online")
def online(p: Principal = Depends(admin_principal)):
    since = iso(now() - timedelta(minutes=5))
    rows = fetch_all(
        "SELECT user_id, MAX(NULLIF(username,'')) AS username, COUNT(*) AS events, MAX(ts) AS last_ts"
        " FROM activity_event WHERE ts>=? GROUP BY user_id ORDER BY last_ts DESC",
        (since,),
    )
    return {"since": since, "items": rows}


@router.get("/settings")
def get_settings(p: Principal = Depends(admin_principal)):
    return settings.all_settings()


@router.put("/settings")
def put_settings(payload: dict, p: Principal = Depends(admin_principal)):
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload
    result = settings.set_many({k: str(v) for k, v in values.items() if not isinstance(v, (dict, list))})
    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="settings_update",
                    detail={"keys": list(values.keys())})
    return result


@router.post("/model/test")
def model_test(p: Principal = Depends(admin_principal)):
    ok, msg = llm.health()
    return {"ok": ok, "message": msg, "model": settings.get("llm.model"), "enabled": settings.get_bool("llm.enabled")}


@router.get("/usage")
def usage(days: int = 14, p: Principal = Depends(admin_principal)):
    days = max(1, min(90, int(days or 14)))
    start = (now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    daily = fetch_all(
        "SELECT substr(ts,1,10) AS date, COUNT(*) AS requests,"
        " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        " COALESCE(SUM(credits),0) AS credits FROM usage_log WHERE substr(ts,1,10)>=?"
        " GROUP BY substr(ts,1,10) ORDER BY date",
        (start,),
    )
    by_kind = fetch_all(
        "SELECT kind, COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens, COALESCE(SUM(credits),0) AS credits"
        " FROM usage_log WHERE substr(ts,1,10)>=? GROUP BY kind ORDER BY requests DESC",
        (start,),
    )
    by_model = fetch_all(
        "SELECT model, COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens, COALESCE(SUM(credits),0) AS credits"
        " FROM usage_log WHERE substr(ts,1,10)>=? GROUP BY model ORDER BY requests DESC",
        (start,),
    )
    total = fetch_one(
        "SELECT COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens, COALESCE(SUM(credits),0) AS credits"
        " FROM usage_log WHERE substr(ts,1,10)>=?",
        (start,),
    ) or {}
    return {
        "days": days,
        "daily": daily,
        "by_kind": by_kind,
        "by_model": by_model,
        "total": {
            "requests": total.get("requests", 0),
            "prompt_tokens": total.get("prompt_tokens", 0),
            "completion_tokens": total.get("completion_tokens", 0),
            "tokens": int(total.get("prompt_tokens") or 0) + int(total.get("completion_tokens") or 0),
            "credits": round(float(total.get("credits") or 0), 2),
        },
    }


@router.get("/model-logs")
def model_logs(limit: int = 30, p: Principal = Depends(admin_principal)):
    rows = fetch_all("SELECT * FROM ai_audit ORDER BY id DESC LIMIT ?", (max(1, min(200, limit)),))
    for r in rows:
        r["detail"] = load_json(r.get("detail"), {})
    return {"items": rows}
