"""会话编排：创建计划、逐题生成、作答判分、提示、跳过、报告。"""
from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from datetime import timedelta

from . import generator, judge as judge_mod, llm, planner, audit
from .config import HINT_PENALTIES
from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id, now
from .reports import build_report


class SessionError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def _log_event(session: dict, event_type: str, detail: dict | None = None) -> None:
    user = fetch_one("SELECT username FROM app_user WHERE user_id=?", (session.get("user_id"),)) or {}
    audit.log_event(
        user_id=session.get("user_id", ""), username=user.get("username", ""),
        tenant_id=session.get("tenant_id", ""), event_type=event_type, detail=detail or {},
    )


def create_session(
    *,
    doc_id: str,
    user_id: str,
    tenant_id: str,
    mode: str = "guided",
    question_count: int = 12,
    difficulty: str = "auto",
    focus_sections: list[str] | None = None,
    exclude_node_ids: list[str] | None = None,
    question_types: list[str] | None = None,
    question_ids: list[str] | None = None,
) -> dict:
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (doc_id, tenant_id))
    if not doc:
        raise SessionError(404, "DOC_NOT_FOUND", "文档不存在或无权访问")
    if doc["status"] not in ("ready", "needs_review"):
        raise SessionError(409, "DOC_NOT_READY", "文档尚未完成解析", {"document_id": doc_id, "status": doc["status"]})
    if mode not in ("guided", "exam", "defense"):
        raise SessionError(422, "INVALID_MODE", "mode 只能是 guided / exam / defense")
    min_count = 1 if question_ids else 5
    if not min_count <= int(question_count) <= 30:
        raise SessionError(422, "INVALID_QUESTION_COUNT", "题量必须在 5—30 之间")
    allowed_types = {"choice", "fill_blank", "true_false", "short_answer"}
    question_types = [x for x in (question_types or []) if x in allowed_types]
    if not question_types:
        question_types = list(allowed_types)

    nodes = fetch_all("SELECT * FROM node WHERE doc_id=? ORDER BY order_index", (doc_id,))
    kps = fetch_all("SELECT * FROM knowledge_point WHERE doc_id=? ORDER BY order_no", (doc_id,))
    evidences = fetch_all("SELECT * FROM evidence WHERE doc_id=?", (doc_id,))
    by_kp: dict[str, list[dict]] = defaultdict(list)
    for ev in evidences:
        for kp in kps:
            if ev["evidence_id"] in load_json(kp["evidence_ids"], []):
                by_kp[kp["kp_id"]].append(ev)
    if question_ids:
        # 题卷布置：严格按题卷题目顺序组卷，不再按知识点重新生成。
        clean_ids = [str(x) for x in question_ids if str(x).strip()]
        marks = ",".join("?" for _ in clean_ids)
        rows = fetch_all(f"SELECT * FROM question WHERE q_id IN ({marks})", clean_ids) if clean_ids else []
        by_id = {q["q_id"]: q for q in rows}
        picked = [by_id[qid] for qid in clean_ids if qid in by_id]
        if len(picked) != len(clean_ids) or any(q["status"] != "active" or q["doc_id"] != doc_id for q in picked):
            raise SessionError(409, "QUESTION_SET_EMPTY", "题卷中有题目已下架、跨文档或不存在，请重新生成题卷")
        node_by_id = {n["node_id"]: n for n in nodes}
        items = []
        for i, q in enumerate(picked, start=1):
            node = node_by_id.get(q["node_id"], {})
            items.append({
                "seq_no": i, "node_id": q["node_id"], "kp_id": q["kp_id"],
                "section_title": node.get("title", ""), "section_path": [node.get("title", "")],
                "q_type": q["q_type"], "bloom_level": q["bloom_level"],
                "target_difficulty": q["difficulty"], "question_id": q["q_id"], "status": "pending",
            })
        plan = {"total": len(items), "requested": len(items), "items": items,
                "by_bloom": {}, "by_section": [], "question_types": [q["q_type"] for q in picked]}
    else:
        plan = planner.build_plan(
            nodes=nodes,
            kps=kps,
            evidence_by_kp=by_kp,
            mode=mode,
            question_count=int(question_count),
            difficulty=difficulty,
            focus_sections=focus_sections,
            exclude_node_ids=exclude_node_ids,
            question_types=question_types,
            seed=random.randint(1, 1_000_000),
        )
    if plan["total"] < 5 and not question_ids:
        raise SessionError(422, "QUESTION_POOL_INSUFFICIENT", "该文档可用知识点不足，请先校正结构或减少题量")
    session_id = new_id("ses")
    started = now()
    row = {
        "session_id": session_id,
        "doc_id": doc_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "mode": mode,
        "state": "ready",
        "total_questions": plan["total"],
        "plan": dump(plan),
        "current_turn_id": None,
        "score": None,
        "theta_before": 500,
        "theta_after": 500,
        "chapter_ability": "{}",
        "skipped_count": 0,
        "hint_count": 0,
        "started_at": iso(started),
        "finished_at": None,
        "expires_at": iso(started + timedelta(hours=24)),
    }
    with db() as conn:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO session ({cols}) VALUES ({marks})", tuple(row.values()))
    _log_event(row, "session_start", {"session_id": session_id, "mode": mode, "total": plan["total"]})
    return get_session(session_id, tenant_id)


def get_session(session_id: str, tenant_id: str | None = None) -> dict:
    sql = "SELECT * FROM session WHERE session_id=?"
    params: tuple = (session_id,)
    if tenant_id:
        sql += " AND tenant_id=?"
        params += (tenant_id,)
    row = fetch_one(sql, params)
    if not row:
        raise SessionError(404, "SESSION_NOT_FOUND", "会话不存在")
    return _session_api(row)


def _session_api(row: dict) -> dict:
    plan = load_json(row.get("plan"), {})
    return {
        "session_id": row["session_id"],
        "document_id": row["doc_id"],
        "user_id": row["user_id"],
        "assignment_id": row.get("assignment_id") or "",
        "mode": row["mode"],
        "state": row["state"],
        "total_questions": row["total_questions"],
        "requested_questions": plan.get("requested", row["total_questions"]),
        "question_types": plan.get("question_types", []),
        "plan": {
            "total": row["total_questions"],
            "answered": sum(1 for i in plan.get("items", []) if i.get("answered")),
            "by_bloom": plan.get("by_bloom", {}),
            "by_section": plan.get("by_section", []),
            "items": [
                {k: v for k, v in item.items() if k in ("seq_no", "section_title", "q_type", "bloom_level", "target_difficulty", "status")}
                for item in plan.get("items", [])
            ],
        },
        "current_turn_id": row.get("current_turn_id"),
        "score": row.get("score"),
        "ability": {"theta_before": row.get("theta_before", 500), "theta_after": row.get("theta_after", 500)},
        "created_at": row.get("started_at"),
        "expires_at": row.get("expires_at"),
    }


def next_question(session_id: str, tenant_id: str | None = None) -> dict:
    session = _raw_session(session_id, tenant_id)
    if session["state"] == "finished":
        raise SessionError(409, "NO_MORE_QUESTIONS", "会话已结束，请查询报告")
    # 幂等：已有未提交轮次
    if session.get("current_turn_id"):
        turn = fetch_one("SELECT * FROM turn WHERE turn_id=?", (session["current_turn_id"],))
        if turn and turn["status"] == "asking":
            return _turn_payload(turn, session)
    plan = load_json(session["plan"], {})
    items = plan.get("items", [])
    turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no", (session_id,))
    answered_seq = {t["seq_no"] for t in turns if t["status"] in ("answered", "skipped")}
    item = next((i for i in items if i["seq_no"] not in answered_seq), None)
    if not item:
        _finish_internal(session)
        raise SessionError(409, "NO_MORE_QUESTIONS", "题目已全部完成，请结束会话")
    if item.get("question_id"):
        qrow = fetch_one("SELECT * FROM question WHERE q_id=?", (item["question_id"],))
        if not qrow or qrow["status"] != "active":
            raise SessionError(409, "QUESTION_SET_EMPTY", "题卷中的题目已下架，请联系教师重新布置")
        turn_id = new_id("turn")
        with db() as conn:
            conn.execute(
                "INSERT INTO turn (turn_id, session_id, q_id, seq_no, answer, evaluation, score, status, hint_level,"
                " created_at, answered_at, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (turn_id, session_id, qrow["q_id"], item["seq_no"], "{}", "{}", 0, "asking", 0, iso(), None, 0),
            )
            conn.execute(
                "UPDATE session SET state='waiting', current_turn_id=?, plan=? WHERE session_id=?",
                (turn_id, dump(_mark_plan(plan, item["seq_no"], "asking")), session_id),
            )
        _log_event(session, "question_asked", {
            "session_id": session_id, "seq_no": item["seq_no"], "q_type": qrow["q_type"],
            "source": "paper", "stem": qrow["stem"][:80],
        })
        turn = fetch_one("SELECT * FROM turn WHERE turn_id=?", (turn_id,))
        return _turn_payload(turn, _raw_session(session_id, tenant_id))
    # 生成题目
    doc = fetch_one("SELECT * FROM document WHERE doc_id=?", (session["doc_id"],))
    node = fetch_one("SELECT * FROM node WHERE node_id=?", (item["node_id"],))
    kp = fetch_one("SELECT * FROM knowledge_point WHERE kp_id=?", (item["kp_id"],))
    evidence = _kp_evidence(kp, session["doc_id"])
    if not node or not kp or not evidence:
        raise SessionError(409, "SESSION_STATE_CONFLICT", "当前考点缺少证据，请重新创建会话")
    asked = [q["stem"] for q in fetch_all(
        "SELECT q.stem FROM question q JOIN turn t ON t.q_id=q.q_id WHERE t.session_id=?", (session_id,)
    )]
    other_kps = fetch_all(
        "SELECT name, definition FROM knowledge_point WHERE doc_id=? AND kp_id<>? ORDER BY importance DESC LIMIT 20",
        (session["doc_id"], kp["kp_id"]),
    )
    doc_ctx = dict(doc)
    doc_ctx["_distractors"] = [
        k["definition"][:80] for k in other_kps
        if 20 <= len(k["definition"]) <= 120 and judge_mod.normalize_text(k["definition"][:30]) not in judge_mod.normalize_text(kp["definition"][:30])
    ][:5]
    q = generator.generate_question(doc_ctx, node, kp, evidence, item, asked,
                                    session_id=session_id, user_id=session["user_id"])
    qrow = generator.question_row(q, item)
    qrow["created_at"] = iso()
    with db() as conn:
        cols = ", ".join(qrow.keys())
        marks = ", ".join("?" for _ in qrow)
        conn.execute(f"INSERT INTO question ({cols}) VALUES ({marks})", tuple(qrow.values()))
        turn_id = new_id("turn")
        conn.execute(
            "INSERT INTO turn (turn_id, session_id, q_id, seq_no, answer, evaluation, score, status, hint_level,"
            " created_at, answered_at, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (turn_id, session_id, q["q_id"], item["seq_no"], "{}", "{}", 0, "asking", 0, iso(), None, 0),
        )
        conn.execute(
            "UPDATE session SET state='waiting', current_turn_id=?, plan=? WHERE session_id=?",
            (turn_id, dump(_mark_plan(plan, item["seq_no"], "asking")), session_id),
        )
    _log_event(session, "question_asked", {
        "session_id": session_id, "seq_no": item["seq_no"], "q_type": q["q_type"],
        "source": q.get("source"), "stem": q["stem"][:80],
    })
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=?", (turn_id,))
    return _turn_payload(turn, _raw_session(session_id, tenant_id))


def _turn_payload(turn: dict, session: dict) -> dict:
    q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],))
    plan = load_json(session["plan"], {})
    answered = sum(1 for i in plan.get("items", []) if i.get("status") in ("answered", "skipped"))
    return {
        "turn_id": turn["turn_id"],
        "seq_no": turn["seq_no"],
        "question": {
            "q_id": q["q_id"],
            "q_type": q["q_type"],
            "bloom_level": q["bloom_level"],
            "difficulty": q["difficulty"],
            "stem": q["stem"],
            "options": load_json(q["options"], []),
            "time_limit_sec": q["time_limit_sec"],
            "hint_levels": q["hint_levels"],
        },
        "progress": {"answered": answered, "total": session["total_questions"]},
        "expires_at": session["expires_at"],
    }


def submit_answer(session_id: str, payload: dict, tenant_id: str | None = None) -> dict:
    session = _raw_session(session_id, tenant_id)
    turn_id = payload.get("turn_id")
    if not turn_id:
        turn_id = session.get("current_turn_id")
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id, session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    # 幂等：重复提交返回首次判分
    if turn["status"] != "asking":
        return _answer_payload(turn, session)
    q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],))
    answer = payload.get("answer") or {}
    if not isinstance(answer, dict) or "value" not in answer:
        raise SessionError(422, "ANSWER_INVALID", "answer 必须包含 type 与 value")
    if answer.get("type") and answer["type"] != q["q_type"]:
        answer["type"] = q["q_type"]
    hint_level = int(payload.get("hint_level") or turn.get("hint_level") or 0)
    result = judge_mod.judge(q, answer, hint_level=hint_level)
    # 能力更新
    theta_before = float(session.get("theta_after") or 500)
    answered_count = len(fetch_all("SELECT turn_id FROM turn WHERE session_id=? AND status IN ('answered','skipped')", (session_id,)))
    theta_after = judge_mod.elo_update(theta_before, q["difficulty"], result["score"], answered_count)
    chapter = load_json(session.get("chapter_ability"), {})
    chapter = judge_mod.update_chapter_ability(chapter, q["node_id"], theta_after, result["score"])
    evaluation = {
        "score": result["score"],
        "correct": result["correct"],
        "dimension_scores": result.get("dimension_scores", {}),
        "feedback": result["feedback"],
        "evidence": result.get("evidence", []),
        "confidence": result.get("confidence", 0.9),
        "needs_review": result.get("needs_review", False),
        "covered_points": result.get("covered_points", []),
        "missing_points": result.get("missing_points", []),
    }
    plan = load_json(session["plan"], {})
    plan = _mark_plan(plan, turn["seq_no"], "answered")
    remaining = sum(1 for i in plan.get("items", []) if i.get("status") not in ("answered", "skipped"))
    with db() as conn:
        conn.execute(
            "UPDATE turn SET answer=?, evaluation=?, score=?, status='answered', hint_level=?, answered_at=?, latency_ms=? WHERE turn_id=?",
            (dump(answer), dump(evaluation), result["score"], hint_level, iso(), int(payload.get("time_spent_ms") or 0), turn_id),
        )
        conn.execute(
            "UPDATE session SET current_turn_id=NULL, theta_after=?, chapter_ability=?, plan=?, state=? WHERE session_id=?",
            (theta_after, dump(chapter), dump(plan), "finished" if remaining == 0 else "waiting", session_id),
        )
    _log_event(session, "answer_submitted", {
        "session_id": session_id, "seq_no": turn["seq_no"], "q_id": q["q_id"], "q_type": q["q_type"],
        "score": result["score"], "correct": result["correct"], "hint_level": hint_level,
    })
    session = _raw_session(session_id, tenant_id)
    if remaining == 0:
        _finish_internal(session)
        session = _raw_session(session_id, tenant_id)
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=?", (turn_id,))
    return _answer_payload(turn, session)


def _answer_payload(turn: dict, session: dict) -> dict:
    ev = load_json(turn.get("evaluation"), {})
    plan = load_json(session.get("plan"), {})
    remaining = sum(1 for i in plan.get("items", []) if i.get("status") not in ("answered", "skipped"))
    return {
        "turn_id": turn["turn_id"],
        "score": turn.get("score", 0),
        "correct": ev.get("correct", False),
        "dimension_scores": ev.get("dimension_scores", {}),
        "feedback": ev.get("feedback", ""),
        "evidence": ev.get("evidence", []),
        "confidence": ev.get("confidence", 0.9),
        "needs_review": ev.get("needs_review", False),
        "covered_points": ev.get("covered_points", []),
        "missing_points": ev.get("missing_points", []),
        "next": {"action": "finish" if remaining == 0 else "ask_next", "remaining": remaining},
        "ability": {"theta_before": session.get("theta_before", 500), "theta_after": session.get("theta_after", 500)},
    }


def get_hint(session_id: str, turn_id: str, level: int, tenant_id: str | None = None) -> dict:
    session = _raw_session(session_id, tenant_id)
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id or session.get("current_turn_id"), session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    if turn["status"] != "asking":
        raise SessionError(409, "SESSION_STATE_CONFLICT", "当前轮次已作答")
    level = max(1, min(3, int(level or 1)))
    if level > int(turn.get("hint_level") or 0) + 1:
        level = int(turn.get("hint_level") or 0) + 1
    if level > 3:
        raise SessionError(409, "HINT_LIMIT_REACHED", "同一题最多请求 3 次提示")
    q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],))
    hints = load_json(q.get("hints"), [])
    while len(hints) < 3:
        hints.append("回到对应章节，找出与该知识点直接相关的原句。")
    hint = hints[level - 1]
    with db() as conn:
        conn.execute("UPDATE turn SET hint_level=? WHERE turn_id=?", (level, turn["turn_id"]))
        conn.execute("UPDATE session SET hint_count=hint_count+1 WHERE session_id=?", (session_id,))
    _log_event(session, "hint", {"session_id": session_id, "turn_id": turn["turn_id"], "level": level})
    return {
        "turn_id": turn["turn_id"],
        "level": level,
        "hint": hint,
        "penalty_percent": HINT_PENALTIES[level],
        "remaining_hints": 3 - level,
    }


def skip_question(session_id: str, turn_id: str, reason: str = "", tenant_id: str | None = None) -> dict:
    session = _raw_session(session_id, tenant_id)
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id or session.get("current_turn_id"), session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    if turn["status"] != "asking":
        return {"turn_id": turn["turn_id"], "score": turn.get("score", 0), "next": {"action": "ask_next"}}
    evaluation = {
        "score": 0, "correct": False, "dimension_scores": {}, "feedback": "本题已跳过，记 0 分。",
        "evidence": [], "confidence": 1.0, "needs_review": False, "covered_points": [], "missing_points": [],
        "skipped": True, "reason": reason,
    }
    plan = _mark_plan(load_json(session["plan"], {}), turn["seq_no"], "skipped")
    remaining = sum(1 for i in plan.get("items", []) if i.get("status") not in ("answered", "skipped"))
    with db() as conn:
        conn.execute(
            "UPDATE turn SET status='skipped', score=0, answer=?, evaluation=?, answered_at=? WHERE turn_id=?",
            (dump({"type": "skip", "value": reason}), dump(evaluation), iso(), turn["turn_id"]),
        )
        conn.execute(
            "UPDATE session SET current_turn_id=NULL, plan=?, skipped_count=skipped_count+1, state=? WHERE session_id=?",
            (dump(plan), "finished" if remaining == 0 else "waiting", session_id),
        )
    _log_event(session, "skip", {"session_id": session_id, "turn_id": turn["turn_id"], "reason": reason})
    if remaining == 0:
        _finish_internal(_raw_session(session_id, tenant_id))
    return {"turn_id": turn["turn_id"], "score": 0, "next": {"action": "finish" if remaining == 0 else "ask_next", "remaining": remaining}}


FOLLOWUP_KINDS = {"clarify": "澄清型", "evidence": "证据型", "transfer": "迁移型"}


def _followup_fallback(kind: str, question: dict, evaluation: dict) -> dict:
    kp = question.get("kp_name") or "这个知识点"
    if kind == "evidence":
        return {"question": f"请回原文找出一句能直接支持「{kp}」的原句，并说明它所在页码；再解释这句话为什么能支撑你的判断。",
                "rationale": "检查你能否把答案落到可核验的原文证据，而不是只复述结论。"}
    if kind == "transfer":
        return {"question": f"如果把「{kp}」背后的方法迁移到一个新的材料或场景，你会怎样调整？请说明迁移后仍然成立的条件。",
                "rationale": "检查你是否理解了方法的适用边界，而不只是记住原文表述。"}
    return {"question": f"请用自己的话解释「{kp}」：它解决什么问题、依赖什么条件、和相邻概念的区别是什么？",
            "rationale": "检查你是否真正理解概念，而不是依赖选项或原文短语。"}


def create_follow_up(session_id: str, turn_id: str, kind: str, tenant_id: str | None = None) -> dict:
    kind = kind if kind in FOLLOWUP_KINDS else "clarify"
    session = _raw_session(session_id, tenant_id)
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id, session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    if turn["status"] != "answered":
        raise SessionError(409, "FOLLOWUP_NOT_READY", "先完成本题作答，再进入追问")
    question = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],)) or {}
    evaluation = load_json(turn.get("evaluation"), {})
    fallback = _followup_fallback(kind, question, evaluation)
    result = dict(fallback)
    if llm.available():
        evidence = load_json(question.get("evidence"), [])
        prompt = (
            "你是论文导读教练。基于当前题目和用户作答，生成一个简短的追问。"
            f"追问类型：{FOLLOWUP_KINDS[kind]}。只输出 JSON："
            '{"question":"追问问题","rationale":"为什么追问"}。'
            "澄清型要求用户用自己的话解释概念或区别；证据型要求引用原文并说明页码；"
            "迁移型要求把方法应用到新条件并说明边界。不要重复原题，不要直接给答案。"
            f"\n原题：{question.get('stem','')}\n知识点：{question.get('kp_name','')}\n"
            f"用户答案：{json.dumps(load_json(turn.get('answer'), {}), ensure_ascii=False)}\n"
            f"评分反馈：{evaluation.get('feedback','')}\n"
            f"原文证据：{json.dumps(evidence[:2], ensure_ascii=False)}"
        )
        try:
            raw = llm.chat([{"role": "user", "content": prompt}], kind="followup", json_mode=True,
                           temperature=0.4, max_tokens=500)
            parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
            if isinstance(parsed, dict) and parsed.get("question"):
                result["question"] = str(parsed["question"])
                result["rationale"] = str(parsed.get("rationale") or result["rationale"])
        except Exception:
            pass
    follow_up_id = new_id("fu")
    with db() as conn:
        conn.execute(
            "INSERT INTO follow_up (follow_up_id, session_id, turn_id, q_id, kind, question, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (follow_up_id, session_id, turn_id, turn.get("q_id"), kind, result["question"], "created", iso()),
        )
    _log_event(session, "followup_created", {"session_id": session_id, "turn_id": turn_id,
                                             "kind": kind, "follow_up_id": follow_up_id})
    return {"follow_up_id": follow_up_id, "turn_id": turn_id, "kind": kind, "kind_label": FOLLOWUP_KINDS[kind],
            "question": result["question"], "rationale": result["rationale"],
            "evidence": load_json(question.get("evidence"), [])[:2]}


def answer_follow_up(session_id: str, turn_id: str, kind: str, answer: str, tenant_id: str | None = None,
                     follow_up_id: str | None = None) -> dict:
    answer = (answer or "").strip()
    if not answer:
        raise SessionError(422, "FOLLOWUP_EMPTY", "请先写下你的追问回答")
    session = _raw_session(session_id, tenant_id)
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id, session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    question = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],)) or {}
    kind = kind if kind in FOLLOWUP_KINDS else "clarify"
    prompt = (
        "你是论文导读教练，请评价用户对追问的回答。只输出 JSON："
        '{"score":0到100的整数,"feedback":"两句话以内反馈","hit":["答对的要点"],"miss":["遗漏的要点"]}。'
        "按是否理解概念、是否引用原文证据、是否能迁移应用来评分；不要编造原文。"
        f"\n追问类型：{FOLLOWUP_KINDS[kind]}\n原题：{question.get('stem','')}\n"
        f"知识点：{question.get('kp_name','')}\n追问回答：{answer}\n"
        f"原文证据：{json.dumps(load_json(question.get('evidence'), [])[:2], ensure_ascii=False)}"
    )
    score, feedback, hit, miss = 60, "已记录你的追问回答。建议再回到原文核对关键句与页码。", [], []
    if llm.available():
        try:
            raw = llm.chat([{"role": "user", "content": prompt}], kind="followup_judge", json_mode=True,
                           temperature=0.2, max_tokens=500)
            parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
            if isinstance(parsed, dict):
                score = max(0, min(100, int(parsed.get("score", 60))))
                feedback = str(parsed.get("feedback") or feedback)
                hit = [str(x) for x in (parsed.get("hit") or [])][:4]
                miss = [str(x) for x in (parsed.get("miss") or [])][:4]
        except Exception:
            pass
    row = None
    if follow_up_id:
        row = fetch_one("SELECT * FROM follow_up WHERE follow_up_id=? AND session_id=?", (follow_up_id, session_id))
    if not row:
        row = fetch_one(
            "SELECT * FROM follow_up WHERE session_id=? AND turn_id=? AND kind=? AND status='created'"
            " ORDER BY created_at DESC LIMIT 1", (session_id, turn_id, kind),
        )
    if row:
        follow_up_id = row["follow_up_id"]
        with db() as conn:
            conn.execute(
                "UPDATE follow_up SET answer=?, score=?, feedback=?, hit=?, miss=?, status='answered', answered_at=?"
                " WHERE follow_up_id=?",
                (answer, score, feedback, dump(hit), dump(miss), iso(), follow_up_id),
            )
    else:
        follow_up_id = new_id("fu")
        with db() as conn:
            conn.execute(
                "INSERT INTO follow_up (follow_up_id, session_id, turn_id, q_id, kind, question, answer, score,"
                " feedback, hit, miss, status, created_at, answered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (follow_up_id, session_id, turn_id, turn.get("q_id"), kind,
                 str(question.get("stem") or ""), answer, score, feedback, dump(hit), dump(miss),
                 "answered", iso(), iso()),
            )
    _log_event(session, "followup_answered", {"session_id": session_id, "turn_id": turn_id, "kind": kind,
                                               "score": score, "follow_up_id": follow_up_id})
    return {"follow_up_id": follow_up_id, "turn_id": turn_id, "kind": kind, "score": score,
            "feedback": feedback, "hit": hit, "miss": miss}


def list_follow_ups(session_id: str, turn_id: str | None = None, tenant_id: str | None = None) -> list[dict]:
    _raw_session(session_id, tenant_id)
    sql = "SELECT * FROM follow_up WHERE session_id=?"
    params: list = [session_id]
    if turn_id:
        sql += " AND turn_id=?"
        params.append(turn_id)
    sql += " ORDER BY created_at"
    rows = fetch_all(sql, params)
    for row in rows:
        row["hit"] = load_json(row.get("hit"), [])
        row["miss"] = load_json(row.get("miss"), [])
    return rows


def finish_session(session_id: str, tenant_id: str | None = None, reason: str = "user_finished") -> dict:
    session = _raw_session(session_id, tenant_id)
    report = _finish_internal(session)
    task_id = new_id("task")
    with db() as conn:
        conn.execute(
            "INSERT INTO task (task_id, type, status, stage, progress, document_id, session_id, result, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, "report_generate", "succeeded", "done", 100, session["doc_id"], session_id,
             dump({"session_id": session_id, "report_id": report["report_id"]}), iso(), iso()),
        )
    return {"session_id": session_id, "state": "finished", "report_task_id": task_id, "report_id": report["report_id"]}


def _finish_internal(session: dict) -> dict:
    existing = fetch_one("SELECT * FROM report WHERE session_id=?", (session["session_id"],))
    if existing:
        return _report_api(existing)
    turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no", (session["session_id"],))
    qids = [t["q_id"] for t in turns]
    questions = {}
    if qids:
        marks = ",".join("?" for _ in qids)
        for q in fetch_all(f"SELECT * FROM question WHERE q_id IN ({marks})", qids):
            questions[q["q_id"]] = q
    nodes = {n["node_id"]: n for n in fetch_all("SELECT * FROM node WHERE doc_id=?", (session["doc_id"],))}
    report = build_report(session, turns, questions, nodes)
    follow_ups = list_follow_ups(session["session_id"])
    answered_fu = [x for x in follow_ups if x.get("status") == "answered"]
    report["summary"]["follow_ups"] = {
        "total": len(follow_ups),
        "answered": len(answered_fu),
        "avg_score": round(sum(float(x.get("score") or 0) for x in answered_fu) / len(answered_fu), 1) if answered_fu else None,
    }
    with db() as conn:
        conn.execute(
            "INSERT INTO report (report_id, session_id, summary, section_mastery, bloom_distribution, wrong_questions,"
            " strengths, weaknesses, recommendations, generated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                report["report_id"], report["session_id"], dump(report["summary"]), dump(report["section_mastery"]),
                dump(report["bloom_distribution"]), dump(report["wrong_questions"]), dump(report["strengths"]),
                dump(report["weaknesses"]), dump(report["recommendations"]), report["generated_at"],
            ),
        )
        conn.execute(
            "UPDATE session SET state='finished', finished_at=?, score=? WHERE session_id=?",
            (iso(), report["summary"]["score"], session["session_id"]),
        )
    _log_event(session, "report_ready", {"session_id": session["session_id"], "report_id": report["report_id"],
                                          "score": report["summary"]["score"]})
    try:
        from . import assignments as assignment_mod
        assignment_mod.on_session_finished(session["session_id"], report)
    except Exception:
        pass
    return report


def get_report(session_id: str, tenant_id: str | None = None) -> dict:
    session = _raw_session(session_id, tenant_id)
    row = fetch_one("SELECT * FROM report WHERE session_id=?", (session_id,))
    if not row:
        if session["state"] != "finished":
            raise SessionError(409, "REPORT_NOT_READY", "报告尚未生成")
        return _finish_internal(session)
    return _report_api(row)


def _report_api(row: dict) -> dict:
    summary = load_json(row["summary"], {})
    fus = fetch_all("SELECT * FROM follow_up WHERE session_id=?", (row["session_id"],))
    answered = [x for x in fus if x.get("status") == "answered"]
    summary["follow_ups"] = {
        "total": len(fus), "answered": len(answered),
        "avg_score": round(sum(float(x.get("score") or 0) for x in answered) / len(answered), 1) if answered else None,
    }
    return {
        "report_id": row["report_id"],
        "session_id": row["session_id"],
        "summary": summary,
        "section_mastery": load_json(row["section_mastery"], []),
        "bloom_distribution": load_json(row["bloom_distribution"], {}),
        "wrong_questions": load_json(row["wrong_questions"], []),
        "strengths": load_json(row["strengths"], []),
        "weaknesses": load_json(row["weaknesses"], []),
        "recommendations": load_json(row["recommendations"], []),
        "generated_at": row["generated_at"],
    }


def get_turn_review(session_id: str, seq_no: int, tenant_id: str | None = None) -> dict:
    """返回一题的完整复盘：题干、作答、正确答案、评分要点与原文证据。"""
    _raw_session(session_id, tenant_id)
    turn = fetch_one("SELECT * FROM turn WHERE session_id=? AND seq_no=?", (session_id, int(seq_no)))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],)) or {}
    evaluation = load_json(turn.get("evaluation"), {})
    q_answer = load_json(q.get("answer"), {})
    can_view = turn.get("status") in ("answered", "skipped")
    return {
        "turn_id": turn["turn_id"],
        "seq_no": turn["seq_no"],
        "status": turn.get("status"),
        "q_id": turn["q_id"],
        "question": {
            "q_type": q.get("q_type"),
            "bloom_level": q.get("bloom_level"),
            "difficulty": q.get("difficulty"),
            "stem": q.get("stem"),
            "options": load_json(q.get("options"), []),
            "hints": load_json(q.get("hints"), []),
        },
        "answer": load_json(turn.get("answer"), {}),
        "evaluation": evaluation,
        "score": turn.get("score", 0),
        "correct_answer": q_answer.get("value") if can_view else None,
        "explanation": (q_answer.get("explanation") or evaluation.get("feedback") or "") if can_view else "",
        "rubric": (load_json(q.get("rubric"), []) or evaluation.get("rubric") or []) if can_view else [],
        "reference_evidence": load_json(q.get("evidence"), []) if can_view else [],
        "can_view_answer": can_view,
    }


def list_turns(session_id: str, tenant_id: str | None = None) -> list[dict]:
    _raw_session(session_id, tenant_id)
    turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no", (session_id,))
    out = []
    for t in turns:
        q = fetch_one("SELECT stem, q_type, bloom_level FROM question WHERE q_id=?", (t["q_id"],)) or {}
        out.append({
            "turn_id": t["turn_id"],
            "seq_no": t["seq_no"],
            "q_id": t["q_id"],
            "stem": q.get("stem", ""),
            "q_type": q.get("q_type", ""),
            "bloom_level": q.get("bloom_level", ""),
            "score": t.get("score", 0),
            "status": t.get("status"),
            "evaluation": load_json(t.get("evaluation"), {}),
            "created_at": t.get("created_at"),
        })
    return out


def _kp_evidence(kp: dict, doc_id: str) -> list[dict]:
    ids = load_json(kp.get("evidence_ids"), [])
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = fetch_all(f"SELECT * FROM evidence WHERE doc_id=? AND evidence_id IN ({marks})", tuple([doc_id] + ids))
    rows.sort(key=lambda r: 0 if r["strength"] == "strong" else 1)
    return rows


def _raw_session(session_id: str, tenant_id: str | None = None) -> dict:
    sql = "SELECT * FROM session WHERE session_id=?"
    params: tuple = (session_id,)
    if tenant_id:
        sql += " AND tenant_id=?"
        params += (tenant_id,)
    row = fetch_one(sql, params)
    if not row:
        raise SessionError(404, "SESSION_NOT_FOUND", "会话不存在")
    return row


def _mark_plan(plan: dict, seq_no: int, status: str) -> dict:
    for item in plan.get("items", []):
        if item["seq_no"] == seq_no:
            item["status"] = status
    return plan
