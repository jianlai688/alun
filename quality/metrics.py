"""纯函数指标：结构 F1、证据支持率、生成/判分质量与时延分位。

本模块不依赖 FastAPI，可被 CLI、后台任务与单元测试直接调用。
所有指标都保留分子/分母，报告可追溯到样本清单。
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Iterable

TITLE_F1_TARGET = 0.95
EVIDENCE_TARGET = 1.0
ANSWERABILITY_TARGET = 4.3
GRADING_WITHIN10_TARGET = 0.85
GRADING_RANGE_TARGET = 5.0
PARSE_30P_TARGET_SEC = 90.0
GENERATION_P95_REDUCTION_TARGET = 0.5
GENERATION_P95_BASELINE_SEC = 23.34

_NUM_HEAD = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})[.、．]?\s*")
_CN_NUM_HEAD = re.compile(r"^\s*[一二三四五六七八九十]{1,3}[、.．]\s*")
_CN_PAREN_HEAD = re.compile(r"^\s*[（(][一二三四五六七八九十]{1,3}[）)]\s*")
_PUNCT = re.compile(r"[\s\u3000，。；：、,.!?！？;:'\"“”‘’（）()\[\]【】《》<>—\-_/\\|]+")
_GENERIC_STEM = re.compile(r"(某文档|某论文|某研究|某文献|某电力文档|某行业|上文|如下所述|文中提到)")


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def squash(text: str) -> str:
    """去空白与全部标点的比较键。"""
    return _PUNCT.sub("", nfkc(text)).lower()


def heading_core(title: str) -> str:
    """去编号后的标题主体，用于容忍编号写法差异。"""
    t = nfkc(title).strip()
    for pattern in (_NUM_HEAD, _CN_NUM_HEAD, _CN_PAREN_HEAD):
        t = pattern.sub("", t)
    return t.strip()


def heading_key(title: str) -> str:
    return squash(heading_core(title))


def title_similarity(a: str, b: str) -> float:
    ka, kb = heading_key(a), heading_key(b)
    if not ka or not kb:
        return 0.0
    if ka == kb:
        return 1.0
    shorter, longer = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if len(shorter) >= 6 and longer.startswith(shorter):
        return 0.94 * (len(shorter) / len(longer)) ** 0.15
    return SequenceMatcher(None, ka, kb).ratio()


def _page_gap(a: int, b: int) -> int:
    return abs(int(a or 1) - int(b or 1))


def match_headings(
    gold: list[dict],
    predicted: list[dict],
    *,
    page_tolerance: int = 1,
    min_score: float = 0.80,
) -> dict:
    """一一匹配黄金标题与解析标题。

    匹配分 = 标题相似度 - 页码惩罚；同标题跨页仍可匹配，但页码惩罚会让
    更好的同页候选优先。返回 matched / missing / extra 与逐条明细。
    """
    gold_items = [
        {"title": str(g.get("title") or ""), "level": int(g.get("level") or 1), "page": int(g.get("page") or 1)}
        for g in gold
    ]
    pred_items = [
        {
            "title": str(p.get("title") or ""),
            "level": int(p.get("level") or 1),
            "page": int(p.get("page_start") or p.get("page") or 1),
            "node_id": p.get("node_id") or "",
        }
        for p in predicted
    ]
    pairs: list[tuple[float, int, int]] = []
    for gi, g in enumerate(gold_items):
        for pi, p in enumerate(pred_items):
            sim = title_similarity(g["title"], p["title"])
            if sim < min_score:
                continue
            gap = _page_gap(g["page"], p["page"])
            penalty = 0.0 if gap <= page_tolerance else (0.06 if gap <= 2 else 0.18)
            score = sim - penalty
            if score >= min_score:
                pairs.append((score, gi, pi))
    pairs.sort(reverse=True)
    used_g: set[int] = set()
    used_p: set[int] = set()
    matches: list[dict] = []
    for score, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        g, p = gold_items[gi], pred_items[pi]
        gap = _page_gap(g["page"], p["page"])
        matches.append({
            "gold_index": gi,
            "pred_index": pi,
            "title": g["title"],
            "predicted_title": p["title"],
            "gold_level": g["level"],
            "predicted_level": p["level"],
            "gold_page": g["page"],
            "predicted_page": p["page"],
            "page_gap": gap,
            "boundary_ok": gap <= page_tolerance,
            "level_ok": g["level"] == p["level"],
            "similarity": round(min(1.0, score), 4),
            "node_id": p["node_id"],
        })
    matched_g = {m["gold_index"] for m in matches}
    matched_p = {m["pred_index"] for m in matches}
    missing = [g for i, g in enumerate(gold_items) if i not in matched_g]
    extra = [p for i, p in enumerate(pred_items) if i not in matched_p]
    return {
        "matches": matches,
        "missing": missing,
        "extra": extra,
        "gold_total": len(gold_items),
        "pred_total": len(pred_items),
        "matched": len(matches),
    }


def structure_metrics(
    gold_headings: list[dict],
    predicted_nodes: list[dict],
    *,
    page_tolerance: int = 1,
) -> dict:
    """标题识别 P/R/F1、边界准确率与层级准确率。"""
    result = match_headings(gold_headings, predicted_nodes, page_tolerance=page_tolerance)
    gold_n = result["gold_total"]
    pred_n = result["pred_total"]
    matched = result["matched"]
    precision = matched / pred_n if pred_n else (1.0 if gold_n == 0 else 0.0)
    recall = matched / gold_n if gold_n else (1.0 if pred_n == 0 else 0.0)
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    boundary_ok = sum(1 for m in result["matches"] if m["boundary_ok"])
    level_ok = sum(1 for m in result["matches"] if m["level_ok"])
    strict = sum(1 for m in result["matches"] if m["boundary_ok"] and m["level_ok"])
    return {
        "gold_total": gold_n,
        "pred_total": pred_n,
        "matched": matched,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "boundary_accuracy": round(boundary_ok / matched, 4) if matched else 0.0,
        "level_accuracy": round(level_ok / matched, 4) if matched else 0.0,
        "strict_f1": round(2 * strict / (gold_n + pred_n), 4) if (gold_n + pred_n) else 1.0,
        "missing": result["missing"],
        "extra": result["extra"],
        "mismatched_level": [m for m in result["matches"] if not m["level_ok"]],
        "mismatched_page": [m for m in result["matches"] if not m["boundary_ok"]],
    }


def micro_structure_metrics(per_doc: list[dict]) -> dict:
    """按样本汇总（micro）结构指标。"""
    gold_n = sum(int(d.get("gold_total") or 0) for d in per_doc)
    pred_n = sum(int(d.get("pred_total") or 0) for d in per_doc)
    matched = sum(int(d.get("matched") or 0) for d in per_doc)
    boundary_ok = sum(int(round(float(d.get("boundary_accuracy") or 0) * int(d.get("matched") or 0))) for d in per_doc)
    level_ok = sum(int(round(float(d.get("level_accuracy") or 0) * int(d.get("matched") or 0))) for d in per_doc)
    precision = matched / pred_n if pred_n else 0.0
    recall = matched / gold_n if gold_n else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    doc_f1 = [float(d.get("f1") or 0) for d in per_doc if d.get("gold_total")]
    return {
        "documents": len(per_doc),
        "gold_total": gold_n,
        "pred_total": pred_n,
        "matched": matched,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "macro_f1": round(sum(doc_f1) / len(doc_f1), 4) if doc_f1 else 0.0,
        "min_doc_f1": round(min(doc_f1), 4) if doc_f1 else 0.0,
        "boundary_accuracy": round(boundary_ok / matched, 4) if matched else 0.0,
        "level_accuracy": round(level_ok / matched, 4) if matched else 0.0,
    }


def percentiles(values: Iterable[float]) -> dict:
    data = sorted(float(v) for v in values if v is not None and float(v) >= 0)
    n = len(data)
    if not n:
        return {"n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}

    def pick(q: float) -> float:
        idx = max(0, min(n - 1, math.ceil(q * n) - 1))
        return data[idx]

    return {
        "n": n,
        "mean": round(sum(data) / n, 2),
        "p50": round(pick(0.50), 2),
        "p95": round(pick(0.95), 2),
        "p99": round(pick(0.99), 2),
        "max": round(data[-1], 2),
    }


def text_chars(text: str) -> int:
    return len(re.sub(r"\s", "", nfkc(text or "")))


def caption_counts(text: str) -> dict:
    """按“图 N / 表 N”题注估计图表数量。"""
    t = nfkc(text or "")
    figures = set(re.findall(r"(?:^|\n)\s*图\s*(\d{1,3})", t))
    tables = set(re.findall(r"(?:^|\n)\s*表\s*(\d{1,3})", t))
    return {"figures": len(figures), "tables": len(tables)}


def coverage_metrics(
    *,
    pdf_text: str,
    node_text: str,
    table_nodes: int,
    figure_nodes: int,
    formula_nodes: int = 0,
) -> dict:
    pdf_chars = text_chars(pdf_text)
    node_chars = text_chars(node_text)
    raw = node_chars / pdf_chars if pdf_chars else 0.0
    caps = caption_counts(pdf_text)
    table_coverage = min(1.0, table_nodes / caps["tables"]) if caps["tables"] else (1.0 if not table_nodes else 1.0)
    figure_coverage = min(1.0, figure_nodes / caps["figures"]) if caps["figures"] else (1.0 if not figure_nodes else 1.0)
    return {
        "pdf_chars": pdf_chars,
        "node_chars": node_chars,
        "text_coverage": round(min(1.0, raw), 4),
        "text_coverage_raw": round(raw, 4),
        "table_captions": caps["tables"],
        "table_nodes": table_nodes,
        "table_coverage": round(table_coverage, 4),
        "figure_captions": caps["figures"],
        "figure_nodes": figure_nodes,
        "figure_coverage": round(figure_coverage, 4),
        "formula_nodes": formula_nodes,
    }


def template_question_flags(questions: list[dict]) -> dict:
    """题干泛称、重复与来源分布。"""
    stems = [str(q.get("stem") or "") for q in questions]
    keys = [squash(s) for s in stems]
    counts = Counter(keys)
    duplicates = sum(1 for k in keys if k and counts[k] > 1)
    generic = sum(1 for s in stems if _GENERIC_STEM.search(s))
    sources = Counter(str(q.get("source") or "unknown") for q in questions)
    n = len(questions)
    return {
        "total": n,
        "generic_stem": generic,
        "generic_stem_rate": round(generic / n, 4) if n else 0.0,
        "duplicate_stems": duplicates,
        "duplicate_rate": round(duplicates / n, 4) if n else 0.0,
        "sources": dict(sources),
        "fallback_rate": round(sources.get("fallback", 0) / n, 4) if n else 0.0,
    }


def grading_consistency(samples: list[dict]) -> dict:
    """人工分 vs 模型分：差值 ≤10 分的比例与极差。"""
    rows = []
    for s in samples:
        try:
            human = float(s.get("human"))
            model = float(s.get("model"))
        except (TypeError, ValueError):
            continue
        rows.append((human, model, abs(human - model)))
    if not rows:
        return {"n": 0, "within10_rate": None, "mae": None, "max_diff": None,
                "range_ok": None, "samples": []}
    n = len(rows)
    within = sum(1 for _, _, d in rows if d <= 10)
    diffs = [d for _, _, d in rows]
    return {
        "n": n,
        "within10_rate": round(within / n, 4),
        "mae": round(sum(diffs) / n, 2),
        "max_diff": round(max(diffs), 2),
        "range_ok": max(diffs) <= 10,
        "samples": rows,
    }


def answerability_summary(reviews: list[dict]) -> dict:
    """按题目聚合 1—5 分评审，输出均分与样本量。"""
    by_q: dict[str, list[float]] = {}
    for r in reviews:
        try:
            score = float((r.get("scores") or {}).get("answerability"))
        except (TypeError, ValueError):
            continue
        if 1 <= score <= 5:
            by_q.setdefault(str(r.get("target_id") or ""), []).append(score)
    if not by_q:
        return {"questions": 0, "reviews": 0, "mean": None, "pass": None}
    per_q = [sum(v) / len(v) for v in by_q.values()]
    mean = sum(per_q) / len(per_q)
    return {
        "questions": len(per_q),
        "reviews": sum(len(v) for v in by_q.values()),
        "mean": round(mean, 2),
        "pass": mean >= ANSWERABILITY_TARGET,
    }


def threshold_status(value: float | None, target: float, *, higher_is_better: bool = True,
                     tolerance: float = 1e-9) -> str:
    if value is None:
        return "blocked"
    ok = value >= target - tolerance if higher_is_better else value <= target + tolerance
    return "pass" if ok else "fail"
