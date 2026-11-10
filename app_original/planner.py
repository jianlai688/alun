"""提问规划器：结构配额 + 加权采样（设计文档 6.3、6.4）。"""
from __future__ import annotations

import math
import random
import re
from collections import defaultdict

from .db import load_json

ROLE_PATTERNS = [
    ("abstract", r"摘要|abstract"),
    ("keywords", r"关键词|关键字|keywords"),
    ("intro", r"引言|前言|绪论|导论"),
    ("related", r"相关工作|文献|综述|研究现状|理论基础|研究进展"),
    ("method", r"方法|模型|算法|框架|技术路线|机制|架构|流程|设计"),
    ("experiment", r"实验|结果|实证|评估|数据分析|对比|消融|案例"),
    ("conclusion", r"结论|讨论|展望|结束语|结语|总结"),
    ("reference", r"参考文献|references"),
]

ROLE_QUOTA = {
    "guided": {"abstract": 1, "intro": 1, "related": 1, "method": 2, "experiment": 2, "conclusion": 1, "keywords": 1},
    "exam": {"abstract": 1, "intro": 1, "related": 1, "method": 2, "experiment": 2, "conclusion": 1},
    "defense": {"abstract": 1, "intro": 1, "related": 1, "method": 2, "experiment": 2, "conclusion": 2},
}

QTYPE_BY_ROLE = {
    "abstract": ["single_choice", "short_answer", "fill_blank"],
    "keywords": ["single_choice", "fill_blank"],
    "intro": ["single_choice", "short_answer"],
    "related": ["multiple_choice", "single_choice", "short_answer"],
    "method": ["single_choice", "short_answer", "fill_blank"],
    "experiment": ["multiple_choice", "single_choice", "fill_blank", "short_answer"],
    "conclusion": ["short_answer", "multiple_choice", "single_choice"],
    "other": ["single_choice", "short_answer", "multiple_choice"],
}

QTYPE_GROUPS = {
    "choice": ["single_choice", "multiple_choice"],
    "fill_blank": ["fill_blank"],
    "true_false": ["true_false"],
    "short_answer": ["short_answer"],
}
ALL_QTYPES = [q for items in QTYPE_GROUPS.values() for q in items]

BLOOM_BY_ROLE = {
    "abstract": ["理解", "记忆"],
    "keywords": ["理解", "记忆"],
    "intro": ["分析", "理解"],
    "related": ["分析", "理解"],
    "method": ["理解", "应用", "分析"],
    "experiment": ["分析", "应用", "评价"],
    "conclusion": ["评价", "分析", "创造"],
    "other": ["理解", "分析"],
}

MODE_TARGET = {"guided": 1, "exam": 0, "defense": 2}


def role_of(node: dict) -> str:
    if node["node_type"] == "abstract":
        return "abstract"
    for role, pattern in ROLE_PATTERNS:
        if re.search(pattern, node["title"] or "", re.I):
            return role
    return "other"


def difficulty_target(mode: str, item_index: int) -> int:
    base = {"easy": 2, "medium": 3, "hard": 4}.get(mode, 3)
    return base


def build_plan(
    *,
    nodes: list[dict],
    kps: list[dict],
    evidence_by_kp: dict[str, list[dict]],
    mode: str = "guided",
    question_count: int = 12,
    difficulty: str = "auto",
    focus_sections: list[str] | None = None,
    exclude_node_ids: list[str] | None = None,
    question_types: list[str] | None = None,
    seed: int | None = None,
) -> dict:
    rng = random.Random(seed)
    selected_groups = [g for g in (question_types or []) if g in QTYPE_GROUPS]
    allowed_qtypes = {q for g in selected_groups for q in QTYPE_GROUPS[g]} if selected_groups else set(ALL_QTYPES)
    node_by_id = {n["node_id"]: n for n in nodes}
    excluded = set(exclude_node_ids or [])
    focus = set(focus_sections or [])

    # 只保留有证据的知识点
    usable = [kp for kp in kps if evidence_by_kp.get(kp["kp_id"]) and kp["node_id"] not in excluded]
    if focus:
        allowed = set()
        for nid in focus:
            allowed.add(nid)
            for n in nodes:
                if n.get("parent_id") == nid:
                    allowed.add(n["node_id"])
        usable = [kp for kp in usable if kp["node_id"] in allowed]
    if not usable:
        return {"total": 0, "items": [], "by_bloom": {}, "by_section": [], "message": "没有可用知识点"}

    # 按章节角色分组
    grouped: dict[str, list[dict]] = defaultdict(list)
    for kp in usable:
        node = node_by_id.get(kp["node_id"])
        if not node:
            continue
        grouped[role_of(node)].append(kp)
    # 其他章节并入 other
    for role in list(grouped):
        if role in ("reference",):
            grouped.pop(role, None)
    all_pool = [kp for role in grouped for kp in grouped[role]]
    if not all_pool:
        return {"total": 0, "items": [], "by_bloom": {}, "by_section": [], "message": "没有可用知识点"}

    quota = dict(ROLE_QUOTA.get(mode, ROLE_QUOTA["guided"]))
    if mode == "exam":
        quota = {k: max(1, v) for k, v in quota.items()}
    target_total = max(5, min(30, question_count))

    chosen: list[dict] = []
    used_kp: set[str] = set()
    section_count: dict[str, int] = defaultdict(int)
    type_count: dict[str, int] = defaultdict(int)
    qtype_count: dict[str, int] = defaultdict(int)

    def weighted_pick(pool: list[dict], target_diff: int) -> dict | None:
        candidates = []
        for kp in pool:
            if kp["kp_id"] in used_kp:
                continue
            if section_count[kp["node_id"]] >= 2:
                continue
            ev = evidence_by_kp[kp["kp_id"]]
            strength = 1.0 if any(e["strength"] == "strong" for e in ev) else 0.6
            diff_fit = 1 - abs(int(kp["difficulty"]) - target_diff) / 4
            diversity = 1.0 if type_count[kp["kp_type"]] < 2 else 0.4
            score = (
                0.30 * float(kp["importance"])
                + 0.20 * (1 - min(1, section_count[kp["node_id"]] / 2))
                + 0.20 * diff_fit
                + 0.15 * diversity
                + 0.15 * strength
            )
            candidates.append((score, kp))
        if not candidates:
            return None
        # softmax 采样
        top = sorted(candidates, key=lambda x: -x[0])[:10]
        tau = 0.35
        exps = [math.exp((score - top[0][0]) / tau) for score, _ in top]
        total = sum(exps)
        r = rng.random() * total
        acc = 0
        for (score, kp), w in zip(top, exps):
            acc += w
            if acc >= r:
                return kp
        return top[-1][1]

    # 先按角色配额出题
    for role, count in quota.items():
        pool = grouped.get(role, [])
        for _ in range(count):
            if len(chosen) >= target_total:
                break
            target_diff = difficulty_target(difficulty, len(chosen))
            kp = weighted_pick(pool, target_diff)
            if not kp:
                break
            chosen.append(_make_item(kp, node_by_id, role, mode, len(chosen), target_diff, allowed_qtypes))
            used_kp.add(kp["kp_id"])
            section_count[kp["node_id"]] += 1
            type_count[kp["kp_type"]] += 1
            qtype_count[chosen[-1]["q_type"]] += 1

    # 配额不足则从全池补齐；同节最多 2 题
    while len(chosen) < target_total:
        kp = weighted_pick(all_pool, difficulty_target(difficulty, len(chosen)))
        if not kp:
            break
        node = node_by_id.get(kp["node_id"], {})
        role = role_of(node)
        chosen.append(_make_item(kp, node_by_id, role, mode, len(chosen), difficulty_target(difficulty, len(chosen)), allowed_qtypes))
        used_kp.add(kp["kp_id"])
        section_count[kp["node_id"]] += 1

    # 题序：按结构路径排序，摘要/引言在前，结论在后，避免同一节连出
    chosen.sort(key=lambda x: (x.get("section_order", 99), x["seq_no"]))
    for idx, item in enumerate(chosen, start=1):
        item["seq_no"] = idx

    by_bloom: dict[str, int] = defaultdict(int)
    by_section: dict[str, dict] = {}
    for item in chosen:
        by_bloom[item["bloom_level"]] += 1
        key = item["node_id"]
        by_section.setdefault(key, {
            "node_id": key,
            "title": item["section_title"],
            "question_count": 0,
        })["question_count"] += 1
    return {
        "requested": target_total,
        "total": len(chosen),
        "question_types": sorted(selected_groups) if selected_groups else ["choice", "fill_blank", "true_false", "short_answer"],
        "items": chosen,
        "by_bloom": dict(by_bloom),
        "by_section": list(by_section.values()),
        "message": "",
    }


def _make_item(kp: dict, node_by_id: dict, role: str, mode: str, index: int, target_diff: int,
               allowed_qtypes: set[str] | None = None) -> dict:
    node = node_by_id.get(kp["node_id"], {})
    bloom_options = BLOOM_BY_ROLE.get(role, BLOOM_BY_ROLE["other"])
    bloom = bloom_options[index % len(bloom_options)]
    qtypes = QTYPE_BY_ROLE.get(role, QTYPE_BY_ROLE["other"])
    # 答辩模式提高简答比例
    if mode == "defense":
        qtypes = ["short_answer"] * 2 + qtypes
    allowed = allowed_qtypes or set(ALL_QTYPES)
    filtered = [q for q in qtypes if q in allowed]
    if filtered:
        qtypes = filtered
    else:
        qtypes = [q for q in ALL_QTYPES if q in allowed]
    q_type = qtypes[index % len(qtypes)]
    if mode == "exam" and q_type == "short_answer" and "multiple_choice" in allowed:
        q_type = "multiple_choice"
    return {
        "seq_no": index + 1,
        "kp_id": kp["kp_id"],
        "kp_name": kp["name"],
        "node_id": kp["node_id"],
        "section_title": node.get("title") or "",
        "section_order": node.get("order_index", 999),
        "section_path": _path(node, node_by_id),
        "q_type": q_type,
        "bloom_level": bloom,
        "target_difficulty": max(1, min(5, target_diff)),
    }


def _path(node: dict, node_by_id: dict) -> list[str]:
    parts = []
    cur = node
    seen = set()
    while cur and cur["node_id"] not in seen:
        seen.add(cur["node_id"])
        if cur.get("title"):
            parts.append(cur["title"])
        cur = node_by_id.get(cur.get("parent_id")) if cur.get("parent_id") else None
    return list(reversed(parts))
