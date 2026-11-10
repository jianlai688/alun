"""V1.2 教学闭环演示：教师布置 → 学生作答 → 教师复盘。

用法（服务已启动）：
    python -m alun.scripts.teaching_demo --doc-id doc_37b9cbf889
"""
from __future__ import annotations

import argparse
import json

import httpx

BASE = "http://127.0.0.1:8010"


def login(username: str, password: str) -> tuple[str, dict]:
    r = httpx.post(BASE + "/api/v1/auth/login", json={"username": username, "password": password}, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data["access_token"], data["user"]


def headers(token: str) -> dict:
    return {"Authorization": "Bearer " + token, "X-Tenant-Id": "tnt_local"}


def answer_value(q: dict) -> object:
    kind = q.get("q_type")
    options = q.get("options") or []
    if kind == "single_choice":
        return options[0]["key"] if options else "A"
    if kind == "multiple_choice":
        return [o["key"] for o in options[:2]] or ["A", "B"]
    if kind == "true_false":
        return True
    return "基于原文证据的课堂回答。"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-id", default="doc_37b9cbf889")
    ap.add_argument("--limit", type=int, default=6)
    ap.add_argument("--title", default="教学闭环演示题卷")
    args = ap.parse_args()

    teacher_token, teacher = login("teacher", "teacher123")
    th = headers(teacher_token)
    paper = httpx.post(BASE + "/api/v1/teaching/papers", headers=th, json={
        "document_id": args.doc_id, "title": args.title, "limit": args.limit,
    }, timeout=60)
    paper.raise_for_status()
    paper = paper.json()
    print(f"题卷：{paper['set_id']} / {paper['question_count']} 题")

    assignment = httpx.post(BASE + "/api/v1/assignments", headers=th, json={
        "paper_id": paper["set_id"], "assignee_username": "demo",
        "instructions": "读完指定章节后完成题卷，并写一句反思。",
    }, timeout=30)
    assignment.raise_for_status()
    assignment = assignment.json()
    print(f"布置：{assignment['assignment_id']} → {assignment['assignee']['display_name']}")

    student_token, _ = login("demo", "demo123")
    sh = headers(student_token)
    started = httpx.post(BASE + f"/api/v1/assignments/{assignment['assignment_id']}/start", headers=sh, json={}, timeout=30)
    started.raise_for_status()
    session = started.json()["session"]
    sid = session["session_id"]
    print(f"开始作答：{sid} / {session['total_questions']} 题")

    for i in range(session["total_questions"]):
        q = httpx.post(BASE + f"/api/v1/sessions/{sid}/next-question", headers=sh, timeout=60)
        q.raise_for_status()
        turn = q.json()
        payload = {"turn_id": turn["turn_id"], "answer": {"type": turn["question"]["q_type"], "value": answer_value(turn["question"])}}
        a = httpx.post(BASE + f"/api/v1/sessions/{sid}/answers", headers=sh, json=payload, timeout=60)
        a.raise_for_status()
        print(f"  第 {i+1} 题：{a.json().get('score')} 分")

    detail = httpx.get(BASE + f"/api/v1/assignments/{assignment['assignment_id']}", headers=th, timeout=30)
    detail.raise_for_status()
    detail = detail.json()
    review = httpx.post(BASE + f"/api/v1/assignments/{assignment['assignment_id']}/review", headers=th, json={
        "comment": "演示复盘：先肯定证据定位，再指出需要回读的章节，下一次课做针对性讲解。",
    }, timeout=30)
    review.raise_for_status()
    review = review.json()
    print(f"复盘：{review['status']} / 得分 {review['score']} / 评语 {review['teacher_comment']}")
    print(json.dumps({
        "paper_id": paper["set_id"], "assignment_id": assignment["assignment_id"], "session_id": sid,
        "status": review["status"], "score": review["score"], "turns": len(detail.get("turns") or []),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
