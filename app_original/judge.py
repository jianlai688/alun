"""判分、反馈与 Elo 能力估计（设计文档 6.7、6.9、附录 C.4）。"""
from __future__ import annotations

import json
import re
import time
import unicodedata

from . import audit, llm, settings
from .db import load_json

JUDGE_SYSTEM = """你是 A论 的判分器。依据评分细则评判用户答案，输出维度分、总分与反馈。
只输出 JSON，不要输出 Markdown 代码块。
反馈必须包含：得分、原文依据（引句＋页码）、一条改进建议；禁止空泛评价。
评分维度：要点覆盖 50%、准确性 25%、证据引用 15%、表达结构 10%。
若证据不足以判断，confidence < 0.7，并在 feedback 中说明。"""


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"[\s，。；：、,.!?！？;:'\"“”‘’（）()\[\]【】]", "", text)
    return text


def judge(question: dict, user_answer: dict, *, hint_level: int = 0) -> dict:
    q_type = question["q_type"]
    answer_key = load_json(question.get("answer"), {})
    evidence = load_json(question.get("evidence"), [])
    rubric = load_json(question.get("rubric"), [])
    value = user_answer.get("value")

    if q_type in ("single_choice", "multiple_choice", "true_false", "fill_blank"):
        result = _judge_objective(q_type, answer_key, value, evidence)
    else:
        result = _judge_subjective(question, user_answer, answer_key, rubric, evidence)
    penalty = {0: 0, 1: 10, 2: 20, 3: 40}.get(int(hint_level or 0), 0)
    raw_score = float(result["score"])
    if penalty:
        result["score"] = max(0.0, round(raw_score * (1 - penalty / 100), 1))
        result["feedback"] = re.sub(
            r"得分[：:]\s*\d+(?:\.\d+)?",
            f"得分：{result['score']:.1f}".rstrip("0").rstrip("."),
            result["feedback"],
            count=1,
        )
        result["feedback"] += f"（已使用 {hint_level} 级提示，按规则扣减 {penalty}%。）"
    else:
        result["score"] = round(raw_score, 1)
    result["hint_penalty"] = penalty
    return result


def _judge_objective(q_type: str, answer: dict, value, evidence: list[dict]) -> dict:
    correct_value = answer.get("value")
    explanation = answer.get("explanation") or ""
    quote = evidence[0]["quote"] if evidence else ""
    page = evidence[0].get("page") if evidence else None
    ref = f"原文（第 {page} 页）：{quote}" if quote else ""
    if q_type == "single_choice":
        ok = str(value).strip().upper() == str(correct_value).strip().upper()
        score = 100 if ok else 0
        feedback = (
            f"正确。{explanation or '与原文表述一致。'}"
            if ok
            else f"不正确。正确答案是 {correct_value}。{explanation or ''}"
        )
    elif q_type == "multiple_choice":
        vals = value if isinstance(value, list) else re.split(r"[,，、\s]+", str(value or ""))
        chosen = {str(v).strip().upper() for v in vals if str(v).strip()}
        raw_correct = correct_value if isinstance(correct_value, list) else re.split(r"[,，、\s]+", str(correct_value or ""))
        correct = {str(v).strip().upper() for v in raw_correct if str(v).strip()}
        if not chosen:
            score = 0
            ok = False
        elif not chosen.issubset(correct):
            score = 0
            ok = False
        elif chosen == correct:
            score = 100
            ok = True
        else:
            score = round(len(chosen) / max(1, len(correct)) * 70, 1)
            ok = False
        feedback = (
            f"完全正确。{explanation}"
            if ok
            else f"正确答案是 {'、'.join(sorted(correct))}。漏选得部分分，错选不得分。{explanation}"
        )
    elif q_type == "true_false":
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "对", "正确", "是", "t", "1")
        ok = bool(value) == bool(correct_value)
        score = 100 if ok else 0
        feedback = f"判断正确。{explanation}" if ok else f"判断错误。原文表述为：{explanation or quote}"
    else:
        answers = correct_value if isinstance(correct_value, list) else [correct_value]
        norm_user = normalize_text(str(value))
        ok = any(norm_user == normalize_text(str(a)) for a in answers)
        score = 100 if ok else 0
        if not ok:
            import difflib
            best = max((difflib.SequenceMatcher(None, norm_user, normalize_text(str(a))).ratio() for a in answers), default=0)
            if best >= 0.85:
                score, ok = 80, False
                feedback = f"与标准答案接近，但表述不够准确。参考答案：{answers[0]}。{explanation}"
            else:
                feedback = f"答案不匹配。参考答案：{answers[0]}。{explanation}"
        else:
            feedback = f"正确。{explanation}"
    if ref:
        feedback = f"{feedback}\n{ref}"
    return {
        "score": score,
        "correct": ok,
        "dimension_scores": {"知识准确性": score},
        "feedback": feedback,
        "evidence": [{"page": e.get("page"), "node_id": e.get("node_id", ""), "quote": e.get("quote", "")} for e in evidence[:2]],
        "confidence": 0.99,
        "needs_review": False,
        "covered_points": [],
        "missing_points": [],
    }


def _judge_subjective(question: dict, user_answer: dict, answer: dict, rubric: list[dict], evidence: list[dict]) -> dict:
    user_text = str(user_answer.get("value") or "").strip()
    if not user_text:
        return _empty_subjective(question, evidence)
    if llm.available():
        started = time.time()
        try:
            prompt = _judge_prompt(question, user_text, answer, rubric, evidence)
            raw = llm.chat_json(
                [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": prompt}],
                kind="judge_answer",
                max_tokens=1200,
                temperature=0.2,
            )
            result = _parse_judge(raw, question, evidence)
            if result:
                audit.record_ai_output(
                    kind="judge_answer", model=settings.get("llm.model", ""), grounded=True,
                    latency_ms=int((time.time() - started) * 1000),
                    detail={"confidence": result.get("confidence"), "needs_review": result.get("needs_review")},
                )
                return result
        except Exception:
            pass
    return _heuristic_subjective(question, user_text, answer, rubric, evidence)


def _judge_prompt(question: dict, user_text: str, answer: dict, rubric: list[dict], evidence: list[dict]) -> str:
    rubric_text = "\n".join(f"- {r.get('point')}（权重 {r.get('weight', 0.5)}）" for r in rubric) or "- 依据参考答案判断"
    ev_text = "\n".join(f"- 第 {e.get('page')} 页：{e.get('quote')}" for e in evidence[:3])
    return f"""【任务】依据评分细则评判用户答案，输出维度分、总分与反馈。只输出 JSON。

【题目】
{question.get('stem')}

【标准答案与评分要点】
{answer.get('value', '')}
{rubric_text}

【证据原文】
{ev_text}

【用户答案】
{user_text}

【要求】
1. 逐条对照评分要点，对每个要点给出 status：covered（完整覆盖）/ partial（部分覆盖）/ missing（未覆盖）。
2. 不得因为表达冗长而加分；不得因为与标准答案措辞不同而扣分。
3. accuracy 表示已作答内容的正确程度（0—1）；evidence_used 表示答案是否引用了原文关键表述；structure 表示表达是否有条理（0—1）。
4. 反馈必须包含：原文依据（引句＋页码）、一条改进建议；不要在 feedback 里写总分。
5. 若证据不足以判断，confidence < 0.7，并在 feedback 中说明。

【输出】
{{"point_verdicts": [{{"point": "评分要点原文", "status": "covered|partial|missing"}}],
 "covered_points": [], "missing_points": [],
 "accuracy": 0.0, "evidence_used": true, "structure": 0.0,
 "feedback": "", "evidence": [{{"page": 0, "quote": ""}}], "confidence": 0.0}}"""


def _parse_judge(raw, question: dict, evidence: list[dict]) -> dict | None:
    if not isinstance(raw, dict):
        return None
    rubric = load_json(question.get("rubric"), []) or []
    points = [str(r.get("point") or "").strip() for r in rubric if str(r.get("point") or "").strip()]
    covered_raw = [str(x).strip() for x in (raw.get("covered_points") or []) if str(x).strip()]
    verdicts = raw.get("point_verdicts") or []
    matched_points: list[str] = []
    partial_count = 0.0
    if points and isinstance(verdicts, list) and verdicts:
        import difflib
        for point in points:
            key = normalize_text(point)
            best_status, best_score = "", 0.0
            for v in verdicts:
                if not isinstance(v, dict):
                    continue
                label = str(v.get("point") or "").strip()
                lk = normalize_text(label)
                if not lk:
                    continue
                score = difflib.SequenceMatcher(None, key, lk).ratio() if key and lk else 0.0
                if key and (key in lk or lk in key):
                    score = 1.0
                if score > best_score:
                    best_score, best_status = score, str(v.get("status") or "").lower()
            if best_score >= 0.45:
                if best_status == "covered":
                    matched_points.append(point)
                elif best_status == "partial":
                    partial_count += 1
            # 模型没给逐点 verdict 的要点按未覆盖处理
        coverage = (len(matched_points) + 0.5 * partial_count) / len(points)
    else:
        for point in points:
            key = normalize_text(point)
            hit = False
            for c in covered_raw:
                ck = normalize_text(c)
                if not ck:
                    continue
                if key and (key in ck or ck in key):
                    hit = True
                    break
                if len(key) >= 8 and len(ck) >= 8:
                    import difflib
                    if difflib.SequenceMatcher(None, key, ck).ratio() >= 0.45:
                        hit = True
                        break
            if hit:
                matched_points.append(point)
        coverage = len(matched_points) / len(points) if points else 1.0
    if points:
        try:
            accuracy = max(0.0, min(1.0, float(raw.get("accuracy", coverage))))
        except Exception:
            accuracy = coverage
        evidence_used = 1.0 if raw.get("evidence_used") else 0.0
        try:
            structure = max(0.0, min(1.0, float(raw.get("structure", 0.5))))
        except Exception:
            structure = 0.5
        score = 50 * coverage + 25 * accuracy + 15 * evidence_used + 10 * structure
        dims_num = {
            "要点覆盖": round(50 * coverage, 1),
            "准确性": round(25 * accuracy, 1),
            "证据引用": round(15 * evidence_used, 1),
            "表达结构": round(10 * structure, 1),
        }
        missing_points = [p for p in points if p not in matched_points]
        covered_points = matched_points
    else:
        # 没有 rubric 时退回模型总分（兼容旧题）
        try:
            score = max(0.0, min(100.0, float(raw.get("score", 0))))
        except Exception:
            return None
        if score <= 1.0:
            score *= 100
        dims_num = {}
        missing_points = raw.get("missing_points") or []
        covered_points = covered_raw
    score = round(score, 1)
    confidence = float(raw.get("confidence", 0.8) or 0.8)
    ev = raw.get("evidence") or []
    ev_out = []
    for e in ev:
        if isinstance(e, dict) and str(e.get("quote", "")).strip():
            quote = str(e.get("quote")).strip()
            matched = next(
                (x for x in evidence if x.get("quote") and (x["quote"][:24] in quote or quote[:24] in x["quote"])),
                None,
            )
            ev_out.append({
                "page": e.get("page") or (matched.get("page") if matched else None),
                "node_id": (matched.get("node_id") if matched else ""),
                "quote": quote,
            })
    if not ev_out:
        ev_out = [{"page": e.get("page"), "node_id": e.get("node_id", ""), "quote": e.get("quote", "")} for e in evidence[:2]]
    feedback = str(raw.get("feedback") or "").strip()
    if not feedback:
        feedback = "请对照原文补充关键要点。"
    # 统一反馈中的分数写法
    m = re.search(r"得分[：:]\s*(\d+(?:\.\d+)?)", feedback)
    if m and float(m.group(1)) <= 1.0 and score >= 50:
        feedback = feedback.replace(m.group(0), f"得分：{int(round(score))}")
    return {
        "score": round(score, 1),
        "correct": score >= 80,
        "dimension_scores": dims_num,
        "feedback": feedback,
        "evidence": ev_out,
        "confidence": max(0.0, min(1.0, confidence)),
        "needs_review": confidence < 0.7,
        "covered_points": covered_points,
        "missing_points": missing_points,
    }


def _heuristic_subjective(question: dict, user_text: str, answer: dict, rubric: list[dict], evidence: list[dict]) -> dict:
    norm_user = normalize_text(user_text)
    points = [r.get("point", "") for r in rubric] or [answer.get("value", "")]
    covered = []
    missing = []
    for p in points:
        terms = [t for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", p) if len(t) >= 2]
        if not terms:
            continue
        hit = sum(1 for t in terms if normalize_text(t) in norm_user)
        if hit / len(terms) >= 0.35:
            covered.append(p)
        else:
            missing.append(p)
    coverage = len(covered) / max(1, len(points))
    evidence_hit = 0.0
    for e in evidence[:2]:
        quote = e.get("quote", "")
        key_terms = [t for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{3,}", quote)][:6]
        if key_terms and sum(1 for t in key_terms if normalize_text(t) in norm_user) / len(key_terms) >= 0.25:
            evidence_hit = 1.0
            break
    structure = 1.0 if len(user_text) >= 60 else 0.5
    score = 50 * coverage + 25 * coverage + 15 * evidence_hit + 10 * structure
    quote = evidence[0]["quote"] if evidence else ""
    page = evidence[0].get("page") if evidence else ""
    feedback = (
        f"得分 {round(score, 1)}。已覆盖 {len(covered)}/{len(points)} 个要点。"
        f"\n原文依据（第 {page} 页）：{quote}"
        "\n改进建议：对照评分要点逐条作答，并引用原文中的关键表述。"
    )
    return {
        "score": round(score, 1),
        "correct": score >= 80,
        "dimension_scores": {
            "要点覆盖": round(50 * coverage, 1),
            "准确性": round(25 * coverage, 1),
            "证据引用": round(15 * evidence_hit, 1),
            "表达结构": round(10 * structure, 1),
        },
        "feedback": feedback,
        "evidence": [{"page": e.get("page"), "node_id": e.get("node_id", ""), "quote": e.get("quote", "")} for e in evidence[:2]],
        "confidence": 0.65,
        "needs_review": True,
        "covered_points": covered,
        "missing_points": missing,
    }


def _empty_subjective(question: dict, evidence: list[dict]) -> dict:
    rubric = load_json(question.get("rubric"), []) or []
    points = [r.get("point", "") for r in rubric if isinstance(r, dict)]
    return {
        "score": 0,
        "correct": False,
        "dimension_scores": {"要点覆盖": 0, "准确性": 0, "证据引用": 0, "表达结构": 0},
        "feedback": "未作答。参考要点：" + ("；".join(points[:3]) if points else "请回原文对应章节重新梳理。"),
        "evidence": [{"page": e.get("page"), "node_id": e.get("node_id", ""), "quote": e.get("quote", "")} for e in evidence[:2]],
        "confidence": 0.99,
        "needs_review": False,
        "covered_points": [],
        "missing_points": points,
    }


def elo_update(theta: float, difficulty: int, score: float, answered_count: int) -> float:
    b = 500 + (int(difficulty) - 3) * 100
    expected = 1 / (1 + 10 ** ((b - theta) / 400))
    s = 1.0 if score >= 85 else (0.6 if score >= 50 else 0.0)
    k = 32 if answered_count < 5 else 16
    return max(0.0, min(1000.0, theta + k * (s - expected)))


def update_chapter_ability(chapter: dict, node_id: str, theta: float, score: float) -> dict:
    chapter = dict(chapter or {})
    item = chapter.get(node_id) or {"theta": 500.0, "n": 0}
    n = int(item.get("n", 0)) + 1
    prev = float(item.get("theta", 500))
    # 新章节向总能力收缩，3 题后采用自身估计
    alpha = min(1.0, n / 3)
    theta_new = prev + (theta - prev) * 0.35
    chapter[node_id] = {"theta": round(theta_new if alpha >= 1 else (500 * (1 - alpha) + theta_new * alpha), 1), "n": n}
    return chapter
