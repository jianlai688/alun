"""会话报告聚合（设计文档 9.3、接口 S9）。"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from .config import BLOOM_LEVELS
from .db import load_json, new_id, now

TYPE_LABEL = {
    "single_choice": "单选",
    "multiple_choice": "多选",
    "true_false": "判断",
    "fill_blank": "填空",
    "short_answer": "简答",
}


def build_report(session: dict, turns: list[dict], questions: dict[str, dict], nodes: dict[str, dict]) -> dict:
    total = int(session.get("total_questions") or 0)
    answered = [t for t in turns if t.get("status") in ("answered", "skipped")]
    score_sum = sum(float(t.get("score") or 0) for t in answered)
    score = round(score_sum / max(1, total), 1)
    correct = sum(1 for t in answered if float(t.get("score") or 0) >= 80)
    started = session.get("started_at")
    duration = 0
    if started:
        try:
            duration = int((datetime.fromisoformat(session.get("finished_at") or now().isoformat()) - datetime.fromisoformat(started)).total_seconds())
        except Exception:
            duration = 0

    # 章节掌握度：按一级章节聚合
    def top_section(node_id: str) -> tuple[str, str]:
        node = nodes.get(node_id)
        seen = set()
        while node and node.get("parent_id") and node["node_id"] not in seen:
            seen.add(node["node_id"])
            parent = nodes.get(node["parent_id"])
            if not parent:
                break
            node = parent
        return (node["node_id"], node.get("title") or "未命名章节") if node else ("", "未命名章节")

    sections: dict[str, dict] = {}
    for turn in answered:
        q = questions.get(turn["q_id"])
        if not q:
            continue
        nid, title = top_section(q["node_id"])
        item = sections.setdefault(nid or title, {"node_id": nid, "title": title, "score": 0.0, "question_count": 0, "_sum": 0.0})
        item["_sum"] += float(turn.get("score") or 0)
        item["question_count"] += 1
    section_mastery = []
    for item in sections.values():
        section_mastery.append({
            "node_id": item["node_id"],
            "title": item["title"],
            "score": round(item["_sum"] / max(1, item["question_count"]) / 100, 2),
            "question_count": item["question_count"],
        })
    section_mastery.sort(key=lambda x: x["score"])

    bloom_stats: dict[str, dict] = {b: {"count": 0, "rate": 0.0, "_sum": 0.0} for b in BLOOM_LEVELS}
    for turn in answered:
        q = questions.get(turn["q_id"])
        if not q:
            continue
        b = q.get("bloom_level") or "理解"
        bloom_stats.setdefault(b, {"count": 0, "rate": 0.0, "_sum": 0.0})
        bloom_stats[b]["count"] += 1
        bloom_stats[b]["_sum"] += float(turn.get("score") or 0)
    for b, item in bloom_stats.items():
        item["rate"] = round(item["_sum"] / max(1, item["count"]) / 100, 2) if item["count"] else 0.0
        item.pop("_sum", None)

    wrong = []
    for turn in turns:
        q = questions.get(turn["q_id"])
        if not q:
            continue
        if float(turn.get("score") or 0) >= 60 and turn.get("status") != "skipped":
            continue
        ans = load_json(turn.get("answer"), {})
        rubric = load_json(q.get("rubric"), [])
        ev = load_json(q.get("evidence"), [])
        wrong.append({
            "q_id": q["q_id"],
            "stem": q.get("stem", ""),
            "q_type": q.get("q_type", ""),
            "user_answer": ans.get("value", ""),
            "score": turn.get("score", 0),
            "reference_points": [r.get("point", "") for r in rubric][:4] or [load_json(q.get("answer"), {}).get("explanation", "")],
            "page": (ev[0].get("page") if ev else None),
            "node_title": nodes.get(q.get("node_id", ""), {}).get("title", ""),
        })

    strengths = [s["title"] for s in sorted(section_mastery, key=lambda x: -x["score"]) if s["score"] >= 0.8][:3]
    weaknesses = [s["title"] for s in section_mastery if s["score"] <= 0.6][:3]

    recommendations = _recommend(section_mastery, wrong, nodes)
    return {
        "report_id": new_id("rpt"),
        "session_id": session["session_id"],
        "summary": {
            "score": score,
            "total": total,
            "answered": len(answered),
            "correct": correct,
            "duration_sec": duration,
            "ability": round(session.get("theta_after") or 500, 1),
            "mode": session.get("mode"),
        },
        "section_mastery": section_mastery,
        "bloom_distribution": bloom_stats,
        "wrong_questions": wrong,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "recommendations": recommendations,
        "generated_at": now().isoformat(timespec="seconds"),
    }


def _recommend(section_mastery: list[dict], wrong: list[dict], nodes: dict[str, dict]) -> list[str]:
    recs: list[str] = []
    weak = [s for s in section_mastery if s["score"] < 0.7][:2]
    for s in weak:
        recs.append(f"重读「{s['title']}」一节，先用自己的话复述该节的核心概念，再对照原文检查遗漏。")
    for w in wrong[:2]:
        page = f"第 {w['page']} 页" if w.get("page") else "原文对应位置"
        recs.append(f"针对「{w['node_title'] or '相关章节'}」的错题，回到{page}，把评分要点逐条抄写并标注对应原句。")
    if not recs:
        if section_mastery:
            lowest = min(section_mastery, key=lambda x: x.get("score", 0))
            recs.append(f"挑一个本轮得分最低的小节「{lowest['title']}」，不看原文写出三条要点，再回原文核对。")
        else:
            recs.append("挑一个尚未覆盖的小节，尝试不看原文写出三条要点，再回原文核对。")
    recs.append("下一轮优先选「方法」与「实验」章节做简答题，用“结论—证据—页码”的格式组织答案。")
    recs.append("把本轮错题的参考要点整理成一张对照表：左侧写要点，右侧标页码，隔天再自测一次。")
    # 去重并限制三条
    out = []
    for r in recs:
        if r not in out:
            out.append(r)
    return out[:3]
