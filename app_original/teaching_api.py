"""V1.2 教学闭环 API：作业布置、学生作答、教师复盘。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from . import assignments, sessions
from .db import db, dump, fetch_all, fetch_one, iso, new_id
from .security import Principal, principal_from_request, require_teacher

router = APIRouter(prefix="/api/v1", tags=["teaching"])


def _p(request: Request) -> Principal:
    return principal_from_request(request)


def _handle(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except assignments.AssignmentError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message, **exc.details})
    except sessions.SessionError as exc:
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message, **exc.details})


@router.get("/teaching/students")
def students(request: Request):
    p = _p(request)
    require_teacher(p)
    rows = fetch_all(
        "SELECT user_id, username, display_name, status, created_at FROM app_user"
        " WHERE role='user' AND status='active' AND tenant_id=? ORDER BY created_at", (p.tenant_id,)
    )
    return {"items": rows, "total": len(rows)}


@router.get("/teaching/documents")
def teaching_documents(request: Request):
    p = _p(request)
    require_teacher(p)
    rows = fetch_all(
        "SELECT doc_id, title, doc_type, page_count, status, created_at FROM document"
        " WHERE tenant_id=? AND deleted_at IS NULL AND status IN ('ready','needs_review') AND page_count>=3"
        " AND EXISTS (SELECT 1 FROM question q WHERE q.doc_id=document.doc_id AND q.status='active')"
        " ORDER BY page_count DESC, created_at DESC LIMIT 200", (p.tenant_id,)
    )
    return {"items": rows, "total": len(rows)}


@router.post("/teaching/papers", status_code=201)
def create_paper(payload: dict, request: Request):
    p = _p(request)
    require_teacher(p)
    doc_id = str(payload.get("document_id") or "")
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (doc_id, p.tenant_id))
    if not doc:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "论文不存在"})
    difficulty = str(payload.get("difficulty") or "mixed")
    try:
        limit = max(1, min(30, int(payload.get("limit") or 10)))
    except Exception:
        limit = 10
    sql = "SELECT * FROM question WHERE doc_id=? AND status='active'"
    params: list = [doc_id]
    if difficulty in ("easy", "medium", "hard"):
        ranges = {"easy": (1, 2), "medium": (3, 3), "hard": (4, 5)}[difficulty]
        sql += " AND difficulty BETWEEN ? AND ?"
        params += [ranges[0], ranges[1]]
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = fetch_all(sql, params)
    if not rows:
        raise HTTPException(status_code=409, detail={"code": "QUESTION_SET_EMPTY", "message": "该论文暂无可用的 active 题目，请先生成或复核题目"})
    ids = [q["q_id"] for q in rows]
    set_id = new_id("set")
    title = str(payload.get("title") or f"{doc['title']} · 课堂题卷")[:120]
    with db() as conn:
        conn.execute(
            "INSERT INTO question_set (set_id, doc_id, tenant_id, title, difficulty, question_ids, question_count,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (set_id, doc_id, p.tenant_id, title, difficulty, dump(ids), len(ids), p.user_id, iso()),
        )
    return {"set_id": set_id, "title": title, "document_id": doc_id, "difficulty": difficulty,
            "question_count": len(ids), "question_ids": ids}


@router.get("/teaching/papers")
def papers(request: Request):
    p = _p(request)
    require_teacher(p)
    rows = fetch_all(
        "SELECT s.*, d.title AS document_title FROM question_set s"
        " LEFT JOIN document d ON d.doc_id=s.doc_id WHERE d.deleted_at IS NULL AND s.tenant_id=?"
        " ORDER BY s.created_at DESC LIMIT 100", (p.tenant_id,)
    )
    return {"items": rows, "total": len(rows)}


@router.get("/assignments")
def list_assignments(request: Request, mine: bool = False, status: str = "", limit: int = 50):
    p = _p(request)
    return {"items": assignments.list_assignments(p, mine=mine, status=status, limit=limit)}


@router.post("/assignments", status_code=201)
def create_assignment(payload: dict, request: Request):
    p = _p(request)
    require_teacher(p)
    return _handle(assignments.create_assignment, p, p.tenant_id, payload)


@router.get("/assignments/by-session/{session_id}")
def assignment_by_session(session_id: str, request: Request):
    p = _p(request)
    row = fetch_one("SELECT * FROM paper_assignment WHERE session_id=?", (session_id,))
    if not row:
        raise HTTPException(status_code=404, detail={"code": "ASSIGNMENT_NOT_FOUND", "message": "该会话不是题卷作业"})
    if not assignments._can_view(row, p):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权查看该作业"})
    return assignments._api(row, include_questions=False, include_turns=False)


@router.get("/assignments/{assignment_id}")
def get_assignment(assignment_id: str, request: Request):
    p = _p(request)
    return _handle(assignments.get_assignment, assignment_id, p)


@router.post("/assignments/{assignment_id}/start")
def start_assignment(assignment_id: str, request: Request):
    p = _p(request)
    return _handle(assignments.start_assignment, assignment_id, p)


@router.post("/assignments/{assignment_id}/submit")
def submit_assignment(assignment_id: str, request: Request, payload: dict | None = None):
    p = _p(request)
    payload = payload or {}
    return _handle(assignments.submit_assignment, assignment_id, p, reason=str(payload.get("reason") or "student_submit"))


@router.post("/assignments/{assignment_id}/review")
def review_assignment(assignment_id: str, payload: dict, request: Request):
    p = _p(request)
    require_teacher(p)
    return _handle(assignments.review_assignment, assignment_id, p, payload)


@router.get("/assignments/{assignment_id}/report")
def assignment_report(assignment_id: str, request: Request):
    p = _p(request)
    row = assignments._assignment_row(assignment_id)
    if not assignments._can_view(row, p):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权查看该作业报告"})
    if not row.get("report_id"):
        raise HTTPException(status_code=409, detail={"code": "REPORT_NOT_READY", "message": "作业尚未生成报告"})
    return sessions.get_report(row["session_id"], row["tenant_id"])
