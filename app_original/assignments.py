"""V1.2 教学闭环：题卷布置、学生作答与教师复盘。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from . import sessions
from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id

ASSIGNMENT_STATUS = {
    "assigned": "待开始",
    "in_progress": "进行中",
    "submitted": "待复盘",
    "reviewed": "已复盘",
}


class AssignmentError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def _user(user_id: str) -> dict:
    row = fetch_one("SELECT * FROM app_user WHERE user_id=?", (user_id,))
    if not row:
        raise AssignmentError(404, "USER_NOT_FOUND", "用户不存在")
    return row


def _assignment_row(assignment_id: str) -> dict:
    row = fetch_one("SELECT * FROM paper_assignment WHERE assignment_id=?", (assignment_id,))
    if not row:
        raise AssignmentError(404, "ASSIGNMENT_NOT_FOUND", "作业不存在")
    return row


def _paper(paper_id: str) -> dict:
    row = fetch_one("SELECT * FROM question_set WHERE set_id=?", (paper_id,))
    if not row:
        raise AssignmentError(404, "PAPER_NOT_FOUND", "题卷不存在")
    return row


def _can_manage(row: dict, user: Any) -> bool:
    return user.role == "admin" or row["teacher_id"] == user.user_id


def _can_view(row: dict, user: Any) -> bool:
    return _can_manage(row, user) or row["assignee_id"] == user.user_id


def _api(row: dict, *, include_questions: bool = False, include_turns: bool = False) -> dict:
    paper = fetch_one("SELECT * FROM question_set WHERE set_id=?", (row["paper_id"],)) or {}
    doc = fetch_one("SELECT title, page_count FROM document WHERE doc_id=?", (row["doc_id"],)) or {}
    assignee = fetch_one("SELECT username, display_name FROM app_user WHERE user_id=?", (row["assignee_id"],)) or {}
    teacher = fetch_one("SELECT username, display_name FROM app_user WHERE user_id=?", (row["teacher_id"],)) or {}
    question_ids = load_json(paper.get("question_ids"), [])
    out = {
        "assignment_id": row["assignment_id"],
        "paper_id": row["paper_id"],
        "title": row.get("title") or paper.get("title") or "未命名题卷",
        "document_id": row["doc_id"],
        "document_title": doc.get("title") or "",
        "page_count": doc.get("page_count") or 0,
        "question_count": int(paper.get("question_count") or len(question_ids)),
        "assignee": {"user_id": row["assignee_id"], "username": assignee.get("username", ""),
                     "display_name": assignee.get("display_name") or assignee.get("username", "")},
        "teacher": {"user_id": row["teacher_id"], "username": teacher.get("username", ""),
                    "display_name": teacher.get("display_name") or teacher.get("username", "")},
        "instructions": row.get("instructions") or "",
        "due_at": row.get("due_at"),
        "status": row.get("status") or "assigned",
        "status_label": ASSIGNMENT_STATUS.get(row.get("status") or "assigned", row.get("status")),
        "session_id": row.get("session_id"),
        "score": row.get("score"),
        "report_id": row.get("report_id"),
        "teacher_comment": row.get("teacher_comment") or "",
        "created_at": row.get("created_at"),
        "started_at": row.get("started_at"),
        "submitted_at": row.get("submitted_at"),
        "reviewed_at": row.get("reviewed_at"),
    }
    if include_questions:
        if question_ids:
            marks = ",".join("?" for _ in question_ids)
            rows = fetch_all(f"SELECT q_id, q_type, stem, difficulty, bloom_level, node_id FROM question WHERE q_id IN ({marks})", question_ids)
            by_id = {q["q_id"]: q for q in rows}
            out["questions"] = [by_id[qid] for qid in question_ids if qid in by_id]
        else:
            out["questions"] = []
    if include_turns and row.get("session_id"):
        turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no", (row["session_id"],))
        qids = [t["q_id"] for t in turns]
        qmap = {}
        if qids:
            marks = ",".join("?" for _ in qids)
            qmap = {q["q_id"]: q for q in fetch_all(f"SELECT * FROM question WHERE q_id IN ({marks})", qids)}
        items = []
        for t in turns:
            q = qmap.get(t["q_id"], {})
            items.append({
                "turn_id": t["turn_id"], "seq_no": t["seq_no"], "status": t["status"],
                "score": t["score"], "answer": load_json(t.get("answer"), {}),
                "evaluation": load_json(t.get("evaluation"), {}),
                "question": {"q_id": q.get("q_id"), "q_type": q.get("q_type"), "stem": q.get("stem"),
                             "answer": load_json(q.get("answer"), {}) if q else {},
                             "rubric": load_json(q.get("rubric"), []) if q else [],
                             "evidence": load_json(q.get("evidence"), []) if q else []},
            })
        out["turns"] = items
        out["follow_ups"] = sessions.list_follow_ups(row["session_id"])
        report = fetch_one("SELECT * FROM report WHERE session_id=?", (row["session_id"],))
        if report:
            out["report"] = {
                "report_id": report["report_id"],
                "summary": load_json(report.get("summary"), {}),
                "section_mastery": load_json(report.get("section_mastery"), []),
                "recommendations": load_json(report.get("recommendations"), []),
            }
    return out


def create_assignment(teacher: Any, tenant_id: str, payload: dict) -> dict:
    paper_id = str(payload.get("paper_id") or payload.get("set_id") or "")
    paper = _paper(paper_id)
    if paper.get("tenant_id") != tenant_id:
        raise AssignmentError(404, "PAPER_NOT_FOUND", "题卷不存在")
    assignee_id = str(payload.get("assignee_id") or "")
    if not assignee_id and payload.get("assignee_username"):
        user = fetch_one("SELECT * FROM app_user WHERE username=?", (str(payload["assignee_username"]).strip(),))
        assignee_id = user["user_id"] if user else ""
    if not assignee_id:
        raise AssignmentError(422, "ASSIGNEE_REQUIRED", "请选择学生")
    assignee = _user(assignee_id)
    if assignee.get("status") != "active":
        raise AssignmentError(409, "ASSIGNEE_DISABLED", "该学生账号已被禁用")
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (paper["doc_id"],))
    if not doc:
        raise AssignmentError(404, "DOC_NOT_FOUND", "题卷关联论文不存在")
    due_at = str(payload.get("due_at") or "").strip() or None
    if due_at:
        try:
            datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        except Exception:
            raise AssignmentError(422, "INVALID_DUE_AT", "截止时间格式不正确，请使用 ISO 时间")
    assignment_id = new_id("asg")
    title = str(payload.get("title") or paper.get("title") or "课堂题卷")
    instructions = str(payload.get("instructions") or "")[:2000]
    with db() as conn:
        conn.execute(
            "INSERT INTO paper_assignment (assignment_id, paper_id, doc_id, teacher_id, tenant_id, title,"
            " assignee_id, assignee_name, instructions, due_at, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (assignment_id, paper_id, paper["doc_id"], teacher.user_id, tenant_id, title,
             assignee_id, assignee.get("display_name") or assignee.get("username") or "", instructions,
             due_at, "assigned", iso()),
        )
    return _api(_assignment_row(assignment_id), include_questions=True)


def list_assignments(user: Any, *, mine: bool = False, status: str = "", limit: int = 50) -> list[dict]:
    sql = (
        "SELECT a.* FROM paper_assignment a JOIN document d ON d.doc_id=a.doc_id"
        " WHERE d.deleted_at IS NULL"
    )
    params: list = []
    if mine or user.role == "user":
        sql += " AND a.assignee_id=?"
        params.append(user.user_id)
    elif user.role == "teacher":
        sql += " AND a.teacher_id=?"
        params.append(user.user_id)
    if status:
        sql += " AND a.status=?"
        params.append(status)
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    params.append(max(1, min(200, int(limit or 50))))
    rows = fetch_all(sql, params)
    return [_api(r) for r in rows]


def get_assignment(assignment_id: str, user: Any) -> dict:
    row = _assignment_row(assignment_id)
    if not _can_view(row, user):
        raise AssignmentError(403, "FORBIDDEN", "无权查看该作业")
    include_turns = _can_manage(row, user) or row.get("status") in ("submitted", "reviewed")
    return _api(row, include_questions=True, include_turns=include_turns)


def start_assignment(assignment_id: str, student: Any) -> dict:
    row = _assignment_row(assignment_id)
    if row["assignee_id"] != student.user_id and student.role not in ("admin", "teacher"):
        raise AssignmentError(403, "FORBIDDEN", "只能开始布置给自己的题卷")
    if row.get("session_id"):
        session = fetch_one("SELECT * FROM session WHERE session_id=?", (row["session_id"],))
        if session:
            return {"assignment": _api(_assignment_row(assignment_id)), "session": sessions.get_session(row["session_id"])}
    paper = _paper(row["paper_id"])
    question_ids = load_json(paper.get("question_ids"), [])
    if not question_ids:
        raise AssignmentError(409, "QUESTION_SET_EMPTY", "题卷没有可用题目")
    session = sessions.create_session(
        doc_id=row["doc_id"], user_id=student.user_id, tenant_id=row["tenant_id"],
        mode="exam", question_count=len(question_ids), question_ids=question_ids,
    )
    with db() as conn:
        conn.execute("UPDATE session SET assignment_id=? WHERE session_id=?", (assignment_id, session["session_id"]))
        conn.execute(
            "UPDATE paper_assignment SET status='in_progress', session_id=?, started_at=? WHERE assignment_id=?",
            (session["session_id"], iso(), assignment_id),
        )
    session["assignment_id"] = assignment_id
    return {"assignment": _api(_assignment_row(assignment_id)), "session": session}


def submit_assignment(assignment_id: str, user: Any, *, reason: str = "student_submit") -> dict:
    row = _assignment_row(assignment_id)
    if row["assignee_id"] != user.user_id and user.role not in ("admin", "teacher"):
        raise AssignmentError(403, "FORBIDDEN", "只能提交自己的题卷")
    if row["status"] in ("submitted", "reviewed"):
        return _api(row, include_questions=True, include_turns=True)
    if not row.get("session_id"):
        raise AssignmentError(409, "ASSIGNMENT_NOT_STARTED", "请先开始作答")
    result = sessions.finish_session(row["session_id"], row["tenant_id"], reason=reason)
    with db() as conn:
        conn.execute(
            "UPDATE paper_assignment SET status='submitted', submitted_at=?, score=?, report_id=? WHERE assignment_id=?",
            (iso(), _session_score(row["session_id"]), result.get("report_id"), assignment_id),
        )
    return _api(_assignment_row(assignment_id), include_questions=True, include_turns=True)


def review_assignment(assignment_id: str, teacher: Any, payload: dict) -> dict:
    row = _assignment_row(assignment_id)
    if not _can_manage(row, teacher):
        raise AssignmentError(403, "FORBIDDEN", "只能复盘自己布置的题卷")
    comment = str(payload.get("comment") or payload.get("teacher_comment") or "").strip()[:3000]
    score = payload.get("score")
    if score is None:
        score = row.get("score") if row.get("score") is not None else _session_score(row.get("session_id") or "")
    try:
        score = float(score) if score is not None else None
    except Exception:
        score = None
    with db() as conn:
        conn.execute(
            "UPDATE paper_assignment SET status='reviewed', teacher_comment=?, score=?, reviewed_at=? WHERE assignment_id=?",
            (comment, score, iso(), assignment_id),
        )
    return _api(_assignment_row(assignment_id), include_questions=True, include_turns=True)


def on_session_finished(session_id: str, report: dict) -> None:
    row = fetch_one("SELECT * FROM paper_assignment WHERE session_id=?", (session_id,))
    if not row:
        return
    with db() as conn:
        conn.execute(
            "UPDATE paper_assignment SET status='submitted', submitted_at=COALESCE(submitted_at, ?),"
            " score=?, report_id=? WHERE assignment_id=?",
            (iso(), report.get("summary", {}).get("score"), report.get("report_id"), row["assignment_id"]),
        )


def _session_score(session_id: str) -> float | None:
    if not session_id:
        return None
    row = fetch_one("SELECT score FROM session WHERE session_id=?", (session_id,))
    if row and row.get("score") is not None:
        return row["score"]
    report = fetch_one("SELECT summary FROM report WHERE session_id=?", (session_id,))
    if report:
        return load_json(report.get("summary"), {}).get("score")
    return None
