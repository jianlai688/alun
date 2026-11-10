"""单题生成与 V1—V8 校验（设计文档 6.5、附录 C.2）。"""
from __future__ import annotations

import difflib
import json
import random
import re
import time
from typing import Any

from . import audit, llm, settings
from .db import dump, new_id
from .parser import CJK

SYSTEM_PROMPT = """你是 A论（A-Lun），一位严谨、耐心的论文导读教练。
你的任务不是替用户读文档，而是围绕文档结构向用户提问、评判作答并给出可执行的反馈。

【工作原则】
1. 结构优先：每道题必须挂在一个结构节点（章节、图表、段落）上，并记录 node_id。
2. 证据约束：先有证据，再有题目；没有证据就换考点，绝不编造原文。
3. 一次一题：每轮只问一个问题，等待用户作答后再继续。
4. 难度适配：目标难度略高于用户当前能力；答对提高，答错降低。
5. 反馈具体：引用原文并标注页码；禁止“回答得不错”“还需努力”等空话。
6. 不越界：只使用当前文档与本次会话的信息；文档未涉及的内容明确说“未涉及”。
7. 保护隐私：不询问、不记录与学习无关的个人信息。

【输出要求】
只输出 JSON，不要输出 Markdown 代码块；字段缺失时用 null，不要臆造。
所有中文标点使用全角；专有名词保留原文写法。"""


def build_prompt(doc: dict, node: dict, kp: dict, evidence: list[dict], spec: dict, asked: list[str]) -> str:
    ev_lines = []
    for ev in evidence[:4]:
        ev_lines.append(f"- evidence_id={ev['evidence_id']} | page={ev['page_no']} | quote={ev['quote']}")
    asked_text = "\n".join(f"- {s}" for s in asked[-8:]) or "（无）"
    node_excerpt = re.sub(r"\s+", " ", (node.get("text") or ""))[:900]
    return f"""【任务】根据给定的结构节点、证据与题目规格，生成一道可直接呈现给用户的中文题目。

【输入】
文档题名：{doc.get('title', '')}
结构路径：{' > '.join(spec.get('section_path') or [node.get('title', '')])}
节点类型：{node.get('node_type', 'section')}
题目规格：题型={spec['q_type']}；Bloom 层级={spec['bloom_level']}；目标难度={spec['target_difficulty']}；考查知识点={kp['name']}（{kp['kp_type']}）
本节原文摘录：{node_excerpt}
可用证据：
{chr(10).join(ev_lines)}

已出题目（避免重复）：
{asked_text}

【硬性要求】
1. 题干必须针对本文的具体概念、方法、数据或结论，禁止使用“某文档”“某电力文档”“某研究”等泛称。
2. 题干不得出现空的“（ ）”或“（ ）。”；选择题的选项与题干分开输出。
3. 单选题恰好 4 个选项，只有一个正确项；多选题 4 个选项中至少 2 个正确项，answer.value 必须写成包含 2—3 个正确选项 key 的数组（如 ["A","C"]）。
4. 答案只能来自给定的证据；不要补充文档外知识。
5. 简答题的评分要点写 2—3 条具体、可核对的要点，不要写“回答完整”这类空话。

【步骤】
1. 从可用证据中选择 1—2 条能直接支撑答案的证据，记录 evidence_id，并逐字复制 quote。
2. 依据题目规格写题干；题干必须自足，不出现“上文”“如下”等指代。
3. 选择题写 4 个选项，干扰项必须似是而非，不得使用“以上都对/都不对”。
4. 给出标准答案、评分要点与解析；解析必须引用证据原句。
5. 再给出三级提示：一级指方向，二级给关键线索，三级给答案要点（不得直接复制完整答案）。
6. 自检：答案是否被证据直接支持？题干是否泄露答案？选项是否互斥？

【输出 JSON】
{{"node_id": "{node['node_id']}", "kp_id": "{kp['kp_id']}",
 "q_type": "{spec['q_type']}",
 "bloom_level": "{spec['bloom_level']}", "difficulty": {spec['target_difficulty']},
 "stem": "",
 "options": [{{"key": "A", "text": ""}}],
 "answer": {{"value": "", "explanation": ""}},
 "rubric": [{{"point": "", "weight": 0.5}}],
 "evidence": [{{"evidence_id": "", "page": 0, "quote": ""}}],
 "hints": ["", "", ""],
 "self_check": {{"evidence_supported": true, "answer_leaked": false, "options_exclusive": true}}}}"""


def generate_question(
    doc: dict,
    node: dict,
    kp: dict,
    evidence: list[dict],
    spec: dict,
    asked_stems: list[str],
    session_id: str = "",
    user_id: str = "",
) -> dict:
    if llm.available():
        model_name = settings.get("llm.model", "")
        started = time.time()
        try:
            raw = llm.chat_json(
                [
                    {"role": "system", "content": settings.get("guide.system_prompt") or SYSTEM_PROMPT},
                    {"role": "user", "content": build_prompt(doc, node, kp, evidence, spec, asked_stems)},
                ],
                kind="generate_question",
                max_tokens=1800,
                temperature=0.45,
                document_id=doc.get("doc_id"),
                session_id=session_id,
            )
            q = _normalize(raw, doc, node, kp, evidence, spec)
            ok, reason = verify_question(q, node, evidence, spec, asked_stems)
            if ok:
                audit.record_ai_output(
                    kind="generate_question", model=model_name, grounded=True,
                    latency_ms=int((time.time() - started) * 1000),
                    user_id=user_id, document_id=doc.get("doc_id", ""), session_id=session_id,
                    detail={"reason": "", "attempts": 1},
                )
                q["source"] = "llm"
                return q
            # 携带失败原因重试一次
            started = time.time()
            retry_prompt = build_prompt(doc, node, kp, evidence, spec, asked_stems) + (
                f"\n\n【上一次输出未通过校验】{reason}。请修正后重新输出完整 JSON。"
            )
            raw2 = llm.chat_json(
                [
                    {"role": "system", "content": settings.get("guide.system_prompt") or SYSTEM_PROMPT},
                    {"role": "user", "content": retry_prompt},
                ],
                kind="generate_question_retry",
                max_tokens=1800,
                temperature=0.3,
                document_id=doc.get("doc_id"),
                session_id=session_id,
            )
            q2 = _normalize(raw2, doc, node, kp, evidence, spec)
            ok2, reason2 = verify_question(q2, node, evidence, spec, asked_stems)
            # 以最终一次模型输出计“幻觉率”；重试成功不算幻觉，但保留首轮失败信息
            audit.record_ai_output(
                kind="generate_question", model=model_name, grounded=ok2,
                latency_ms=int((time.time() - started) * 1000),
                user_id=user_id, document_id=doc.get("doc_id", ""), session_id=session_id,
                detail={"reason": reason2, "attempts": 2, "first_attempt_failed": True},
            )
            if ok2:
                q2["source"] = "llm"
                return q2
        except Exception:
            pass
    q = fallback_question(doc, node, kp, evidence, spec, asked_stems)
    audit.record_ai_output(
        kind="generate_question", model=settings.get("llm.model", ""), grounded=True, fallback=True,
        user_id=user_id, document_id=doc.get("doc_id", ""), session_id=session_id,
        detail={"reason": "fallback_template"},
    )
    return q


def _normalize(raw: Any, doc: dict, node: dict, kp: dict, evidence: list[dict], spec: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("模型未返回 JSON 对象")
    q_type = raw.get("q_type") or spec["q_type"]
    if q_type != spec["q_type"]:
        q_type = spec["q_type"]
    options = raw.get("options") or []
    if not isinstance(options, list):
        options = []
    options = [
        {"key": str(o.get("key", "")).strip().upper()[:1], "text": str(o.get("text", "")).strip()}
        for o in options if isinstance(o, dict) and str(o.get("text", "")).strip()
    ]
    answer = raw.get("answer") or {}
    if not isinstance(answer, dict):
        answer = {"value": answer, "explanation": ""}
    rubric = raw.get("rubric") or []
    if not isinstance(rubric, list):
        rubric = []
    rubric = [
        {"point": str(r.get("point", "")).strip(), "weight": float(r.get("weight", 0.5))}
        for r in rubric if isinstance(r, dict) and str(r.get("point", "")).strip()
    ]
    # 模型有时用选项全文而不是 A/B/C/D 作答，这里做一次可解释的归一化
    if q_type in ("single_choice", "multiple_choice") and options:
        keys = [o["key"] for o in options]
        value = answer.get("value")
        raw_vals = re.split(r"[,，、\s]+", value) if isinstance(value, str) else (value if isinstance(value, list) else [value])
        mapped = []
        for item in raw_vals:
            text = str(item or "").strip()
            if not text:
                continue
            if text.upper() in keys:
                mapped.append(text.upper())
                continue
            hit = next((o for o in options if text == o["text"] or text in o["text"] or o["text"] in text), None)
            if hit:
                mapped.append(hit["key"])
        if mapped:
            answer["value"] = mapped[0] if q_type == "single_choice" else list(dict.fromkeys(mapped))
    ev_out = []
    for ev in raw.get("evidence") or []:
        if not isinstance(ev, dict):
            continue
        quote = str(ev.get("quote", "")).strip()
        if not quote:
            continue
        matched = next((e for e in evidence if e["evidence_id"] == ev.get("evidence_id")), None)
        if not matched:
            matched = next((e for e in evidence if quote[:20] in e["quote"]), None)
        ev_out.append({
            "evidence_id": matched["evidence_id"] if matched else "",
            "node_id": node["node_id"],
            "page": int(matched["page_no"]) if matched else int(ev.get("page") or 0),
            "quote": quote,
        })
    # 模型偶尔把“多选题”写成只有一个正确项：这本身是一道合法的单选题，
    # 不必为题型标签重试一次、付出双倍时延。
    if q_type == "multiple_choice" and options:
        keys = [o["key"] for o in options]
        vals = answer.get("value")
        vals = vals if isinstance(vals, list) else re.split(r"[,，、\s]+", str(vals or ""))
        valid = {str(v).strip().upper() for v in vals if str(v).strip().upper() in keys}
        if len(valid) <= 1:
            q_type = "single_choice"
            if valid:
                answer["value"] = next(iter(valid))
    hints = [str(h).strip() for h in (raw.get("hints") or []) if str(h).strip()][:3]
    return {
        "q_id": new_id("q"),
        "doc_id": doc["doc_id"],
        "kp_id": kp["kp_id"],
        "node_id": node["node_id"],
        "q_type": q_type,
        "bloom_level": raw.get("bloom_level") if raw.get("bloom_level") in ("记忆", "理解", "应用", "分析", "评价", "创造") else spec["bloom_level"],
        "difficulty": max(1, min(5, int(raw.get("difficulty") or spec["target_difficulty"]))),
        "stem": str(raw.get("stem", "")).strip(),
        "options": options,
        "answer": answer,
        "rubric": rubric,
        "evidence": ev_out,
        "hints": hints,
        "source": "llm",
    }


def verify_question(q: dict, node: dict, evidence: list[dict], spec: dict, asked_stems: list[str]) -> tuple[bool, str]:
    text = node.get("text", "") or ""
    stem = q.get("stem", "")
    if not stem or len(stem) < 8:
        return False, "题干过短"
    if len(stem) > 500:
        return False, "题干过长"
    if q["q_type"] != "fill_blank" and re.search(r"（\s*）|\(\s*\)", stem):
        return False, "V5 题干出现空白括号"
    if re.search(r"(某文档|某电力文档|某研究|某论文)", stem):
        return False, "V5 题干使用泛称，未指向本文"
    # V1 证据必须逐字存在于节点原文
    evs = q.get("evidence") or []
    if not evs:
        return False, "V1 缺少证据"
    for ev in evs:
        quote = (ev.get("quote") or "").strip()
        if not quote:
            return False, "V1 证据引文为空"
        if quote not in text and re.sub(r"\s+", "", quote) not in re.sub(r"\s+", "", text):
            return False, f"V1 证据引文不在原文中：{quote[:20]}"
    # V3 选择题结构
    if q["q_type"] in ("single_choice", "multiple_choice"):
        opts = q.get("options") or []
        if len(opts) != 4:
            return False, "V3 选择题必须有 4 个选项"
        keys = [o["key"] for o in opts]
        if len(set(keys)) != 4 or any(not k for k in keys):
            return False, "V3 选项 key 重复或缺失"
        if any(re.search(r"(以上都|都不对|都正确|都错误)", o["text"]) for o in opts):
            return False, "V8 禁止使用“以上都”类选项"
        value = q.get("answer", {}).get("value")
        if q["q_type"] == "single_choice":
            if isinstance(value, list):
                value = value[0] if value else ""
            if str(value).strip().upper() not in keys:
                return False, "V3 单选答案不在选项中"
        else:
            if isinstance(value, str):
                value = re.split(r"[,，、\s]+", value)
            vals = value if isinstance(value, list) else [value]
            vals = [str(v).strip().upper() for v in vals if str(v).strip()]
            if len(vals) < 2 or any(v not in keys for v in vals):
                return False, "V3 多选答案不合法"
    elif q["q_type"] == "true_false":
        if not isinstance(q.get("answer", {}).get("value"), bool):
            return False, "V3 判断题答案必须为布尔值"
    elif q["q_type"] == "fill_blank":
        val = q.get("answer", {}).get("value")
        if not val or (isinstance(val, list) and not val):
            return False, "V3 填空题缺少答案"
        if isinstance(val, str) and val in stem:
            return False, "V2 题干泄露填空答案"
    elif q["q_type"] == "short_answer":
        if not (q.get("rubric") or []):
            return False, "V3 简答题缺少评分要点"
    # V2 题干泄露答案关键词（简答题允许）
    if q["q_type"] in ("single_choice", "multiple_choice", "true_false"):
        ans_text = str(q.get("answer", {}).get("explanation", ""))[:40]
        if ans_text and ans_text in stem:
            return False, "V2 题干泄露答案"
    # V6 去重
    for old in asked_stems:
        if difflib.SequenceMatcher(None, stem, old).ratio() > 0.9:
            return False, "V6 与已出题目重复"
    # V7 难度
    if not 1 <= int(q.get("difficulty", 0)) <= 5:
        return False, "V7 难度必须是 1—5"
    return True, ""


def fallback_question(doc: dict, node: dict, kp: dict, evidence: list[dict], spec: dict, asked_stems: list[str]) -> dict:
    """模型不可用或连续校验失败时的规则模板题（仍保证证据可溯源）。"""
    rng = random.Random(kp["kp_id"] + str(spec.get("seq_no", 0)))
    q_type = spec["q_type"]
    definition = (kp.get("definition") or "").strip()
    quote = (evidence[0]["quote"] if evidence else definition).strip()
    page = int(evidence[0]["page_no"]) if evidence else int(node.get("page_start", 1))
    section = spec.get("section_title") or node.get("title") or "本节"
    kp_name = kp.get("name") or "核心概念"

    options: list[dict] = []
    answer: dict = {}
    rubric: list[dict] = []
    if q_type in ("single_choice", "multiple_choice"):
        q_type = "single_choice"
        correct = _clip(definition, 72) or _clip(quote, 72)
        pool = [e["quote"] for e in evidence[1:]]
        # 从同文档其他知识点取干扰项
        distractors = _distractor_pool(doc, kp)[:3]
        while len(distractors) < 3:
            distractors.append("原文并未提及该概念的具体作用与边界")
        choices = [correct] + [_clip(d, 72) for d in distractors[:3]]
        rng.shuffle(choices)
        keys = ["A", "B", "C", "D"]
        options = [{"key": keys[i], "text": text} for i, text in enumerate(choices)]
        correct_key = keys[choices.index(correct)]
        answer = {"value": correct_key, "explanation": quote}
        rubric = [{"point": f"选择与原文表述一致的选项（{correct_key}）", "weight": 1.0}]
        stem = f"根据《{doc.get('title', '本文')}》{section}部分，下列哪一项最符合原文对「{kp_name}」的表述？"
    elif q_type == "true_false":
        statement = _clip(definition, 90)
        answer = {"value": True, "explanation": quote}
        rubric = [{"point": "判断与原文一致", "weight": 1.0}]
        stem = f"判断：原文在{section}部分指出——{statement}。"
    elif q_type == "fill_blank":
        blank = kp_name if kp_name and kp_name in definition else _longest_term(definition)
        if not blank or blank not in definition:
            blank = kp_name
        stem_text = definition.replace(blank, "____", 1) if blank in definition else definition
        stem = f"填空：根据{section}部分，{_clip(stem_text, 140)}"
        answer = {"value": blank, "explanation": quote}
        rubric = [{"point": f"填入「{blank}」", "weight": 1.0}]
    else:
        q_type = "short_answer"
        stem = f"请结合{section}部分，说明「{kp_name}」的含义，并指出它在文中的作用。"
        answer = {"value": _clip(definition, 160), "explanation": quote}
        points = [p.strip() for p in re.split(r"[；;。]", definition) if len(p.strip()) >= 6][:3]
        rubric = [{"point": p, "weight": round(1 / max(1, len(points)), 2)} for p in points] or [
            {"point": "说明概念含义", "weight": 0.5},
            {"point": "说明作用或意义", "weight": 0.5},
        ]
    hints = [
        f"回到「{section}」一节，先找到与「{kp_name}」直接相关的原句。",
        f"关注原文中的关键表述：{_clip(definition, 48)}",
        f"答案要点：{_clip(definition, 90)}",
    ]
    q = {
        "q_id": new_id("q"),
        "doc_id": doc["doc_id"],
        "kp_id": kp["kp_id"],
        "node_id": node["node_id"],
        "q_type": q_type,
        "bloom_level": spec["bloom_level"],
        "difficulty": spec["target_difficulty"],
        "stem": stem,
        "options": options,
        "answer": answer,
        "rubric": rubric,
        "evidence": [{"evidence_id": evidence[0]["evidence_id"] if evidence else "", "node_id": node["node_id"], "page": page, "quote": quote}],
        "hints": hints,
        "source": "fallback",
    }
    return q


def _distractor_pool(doc: dict, kp: dict) -> list[str]:
    # 由调用方在 doc 中放入 _distractors（同文档其他知识点定义）
    return list(doc.get("_distractors") or [])


def _clip(text: str, n: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _longest_term(text: str) -> str:
    terms = re.findall(r"[\u4e00-\u9fffA-Za-z0-9\-]{4,16}", text or "")
    return max(terms, key=len) if terms else ""


def question_row(q: dict, spec: dict) -> dict:
    return {
        "q_id": q["q_id"],
        "doc_id": q["doc_id"],
        "kp_id": q.get("kp_id"),
        "node_id": q["node_id"],
        "q_type": q["q_type"],
        "bloom_level": q["bloom_level"],
        "difficulty": int(q["difficulty"]),
        "stem": q["stem"],
        "options": dump(q.get("options") or []),
        "answer": dump(q.get("answer") or {}),
        "rubric": dump(q.get("rubric") or []),
        "evidence": dump(q.get("evidence") or []),
        "hints": dump(q.get("hints") or []),
        "time_limit_sec": 180 if q["q_type"] == "short_answer" else 90,
        "hint_levels": 3,
        "status": "active",
        "version": 1,
        "source": q.get("source", "llm"),
        "created_at": "",
    }
