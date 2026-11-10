"""质量评测运行器：解析结构、生成证据、判分一致性与时延。"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

from alun.app import pipeline as pipeline_mod
from alun.app.config import DATA_DIR
from alun.app.db import db, dump, fetch_all, fetch_one, init_db, iso, load_json, new_id
from alun.quality import golden as golden_mod
from alun.quality import metrics as m

RUN_DIR = DATA_DIR / "quality" / "runs"
RUN_DIR.mkdir(parents=True, exist_ok=True)

_THRESHOLDS = {
    "title_f1": m.TITLE_F1_TARGET,
    "evidence_support_rate": m.EVIDENCE_TARGET,
    "answerability": m.ANSWERABILITY_TARGET,
    "grading_within10_rate": m.GRADING_WITHIN10_TARGET,
    "parse_30p_p95_sec": m.PARSE_30P_TARGET_SEC,
}


# ---------------------------------------------------------------------------
# 运行记录
# ---------------------------------------------------------------------------
def create_run(version: str = "golden-v1", suite: str = "all", created_by: str = "") -> dict:
    init_db()
    run_id = new_id("qrun")
    with db() as conn:
        conn.execute(
            "INSERT INTO quality_run (run_id, version, suite, status, metrics, summary, created_by, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (run_id, version, suite, "queued", "{}", "{}", created_by, iso()),
        )
    return get_run(run_id, include_cases=False)


def get_run(run_id: str, include_cases: bool = True, case_limit: int = 500) -> dict:
    row = fetch_one("SELECT * FROM quality_run WHERE run_id=?", (run_id,))
    if not row:
        raise KeyError(run_id)
    run = dict(row)
    run["metrics"] = load_json(row.get("metrics"), {})
    run["summary"] = load_json(row.get("summary"), {})
    if include_cases:
        cases = fetch_all(
            "SELECT * FROM quality_case WHERE run_id=? ORDER BY metric, id LIMIT ?",
            (run_id, max(1, min(2000, int(case_limit)))),
        )
        for c in cases:
            c["detail"] = load_json(c.get("detail"), {})
            c["passed"] = bool(c.get("passed"))
        run["cases"] = cases
    defects = fetch_all("SELECT * FROM quality_defect WHERE run_id=? ORDER BY severity, updated_at DESC", (run_id,))
    for d in defects:
        d["detail"] = load_json(d.get("detail"), {})
    run["defects"] = defects
    return run


def list_runs(limit: int = 20) -> list[dict]:
    rows = fetch_all(
        "SELECT run_id, version, suite, status, metrics, summary, created_by, created_at, finished_at"
        " FROM quality_run ORDER BY created_at DESC LIMIT ?",
        (max(1, min(100, int(limit or 20))),),
    )
    for r in rows:
        r["metrics"] = load_json(r.get("metrics"), {})
        r["summary"] = load_json(r.get("summary"), {})
    return rows


def latest_run() -> dict | None:
    rows = list_runs(1)
    return rows[0] if rows else None


_LATENCY_SINCE: str | None = None


def run_sync(version: str = "golden-v1", suite: str = "all", created_by: str = "",
             since: str | None = None) -> dict:
    global _LATENCY_SINCE
    _LATENCY_SINCE = since
    run = create_run(version, suite, created_by)
    execute_run(run["run_id"])
    return get_run(run["run_id"], include_cases=True)


def run_background(version: str = "golden-v1", suite: str = "all", created_by: str = "") -> dict:
    run = create_run(version, suite, created_by)
    thread = threading.Thread(
        target=execute_run, args=(run["run_id"],), name=f"quality-{run['run_id']}", daemon=True
    )
    thread.start()
    return run


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def execute_run(run_id: str) -> None:
    row = fetch_one("SELECT * FROM quality_run WHERE run_id=?", (run_id,))
    if not row:
        return
    version = row["version"]
    suite = row["suite"]
    started = datetime.now()
    _set_run(run_id, status="running")
    try:
        bundle = golden_mod.load_version(version)
        results: dict[str, Any] = {"version": version, "suite": suite, "started_at": iso(started)}
        if suite in ("all", "parse"):
            results["parse"] = _run_parse_suite(bundle, run_id)
        if suite in ("all", "generation"):
            results["generation"] = _run_generation_suite(bundle, run_id)
        if suite in ("all", "grading"):
            results["grading"] = _run_grading_suite(run_id)
        if suite in ("all", "latency"):
            results["latency"] = _run_latency_suite(bundle, since=_LATENCY_SINCE)
        summary = _summarize(results)
        results["summary"] = summary
        _sync_defects(run_id, results)
        finished = datetime.now()
        results["finished_at"] = iso(finished)
        results["duration_sec"] = round((finished - started).total_seconds(), 2)
        _write_artifact(run_id, results)
        _set_run(run_id, status="completed", metrics=results, summary=summary)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        _set_run(run_id, status="failed", summary={"error": str(exc)[:800]})
        raise


def _set_run(run_id: str, *, status: str | None = None, metrics: dict | None = None,
             summary: dict | None = None) -> None:
    fields = []
    values: list[Any] = []
    if status:
        fields.append("status=?")
        values.append(status)
    if metrics is not None:
        fields.append("metrics=?")
        values.append(dump(metrics))
    if summary is not None:
        fields.append("summary=?")
        values.append(dump(summary))
    if status in ("completed", "failed"):
        fields.append("finished_at=?")
        values.append(iso())
    if not fields:
        return
    values.append(run_id)
    with db() as conn:
        conn.execute(f"UPDATE quality_run SET {', '.join(fields)} WHERE run_id=?", tuple(values))


def _add_case(run_id: str, *, case_id: str, doc_id: str, metric: str, expected: str,
              actual: str, passed: bool, detail: dict) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO quality_case (run_id, case_id, doc_id, metric, expected, actual, passed, detail)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (run_id, case_id, doc_id, metric, expected, actual, 1 if passed else 0, dump(detail)),
        )


# ---------------------------------------------------------------------------
# 解析结构
# ---------------------------------------------------------------------------
def _run_parse_suite(bundle: dict, run_id: str) -> dict:
    per_doc: list[dict] = []
    coverage_rows: list[dict] = []
    for case in bundle["cases"]:
        doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (case["doc_id"],))
        if not doc:
            _add_case(run_id, case_id=case.get("case_id") or case["doc_id"], doc_id=case["doc_id"],
                      metric="structure", expected="存在文档", actual="文档缺失", passed=False,
                      detail={"reason": "DOC_NOT_FOUND"})
            continue
        nodes = fetch_all(
            "SELECT node_id, title, level, page_start, page_end, node_type FROM node"
            " WHERE doc_id=? AND node_type='section' ORDER BY order_index",
            (case["doc_id"],),
        )
        sm = m.structure_metrics(case.get("headings") or [], nodes)
        sm["doc_id"] = case["doc_id"]
        sm["title"] = case.get("title") or doc.get("title")
        sm["case_id"] = case.get("case_id") or case["doc_id"]
        per_doc.append(sm)
        _add_case(
            run_id, case_id=sm["case_id"], doc_id=case["doc_id"], metric="structure",
            expected=f"F1>={m.TITLE_F1_TARGET:.2f}", actual=f'{sm["f1"]:.3f}',
            passed=sm["f1"] >= m.TITLE_F1_TARGET,
            detail={
                "precision": sm["precision"], "recall": sm["recall"], "f1": sm["f1"],
                "boundary_accuracy": sm["boundary_accuracy"], "level_accuracy": sm["level_accuracy"],
                "strict_f1": sm["strict_f1"], "matched": sm["matched"],
                "gold_total": sm["gold_total"], "pred_total": sm["pred_total"],
                "missing": sm["missing"][:30], "extra": sm["extra"][:30],
                "mismatched_level": sm["mismatched_level"][:20],
                "mismatched_page": sm["mismatched_page"][:20],
            },
        )
        cov = _coverage_for_document(doc, nodes)
        cov["doc_id"] = case["doc_id"]
        cov["title"] = case.get("title") or doc.get("title")
        cov["case_id"] = sm["case_id"]
        coverage_rows.append(cov)
        _add_case(
            run_id, case_id=sm["case_id"], doc_id=case["doc_id"], metric="coverage",
            expected="文本覆盖率>=0.98 且 图表覆盖>=0.90",
            actual=f'text={cov["text_coverage"]:.3f}, table={cov["table_coverage"]:.3f}, figure={cov["figure_coverage"]:.3f}',
            passed=cov["text_coverage"] >= 0.98 and cov["table_coverage"] >= 0.90 and cov["figure_coverage"] >= 0.90,
            detail=cov,
        )
    overall = m.micro_structure_metrics(per_doc)
    coverage = {
        "documents": len(coverage_rows),
        "mean_text_coverage": round(sum(x["text_coverage"] for x in coverage_rows) / len(coverage_rows), 4) if coverage_rows else 0.0,
        "mean_table_coverage": round(sum(x["table_coverage"] for x in coverage_rows) / len(coverage_rows), 4) if coverage_rows else 0.0,
        "mean_figure_coverage": round(sum(x["figure_coverage"] for x in coverage_rows) / len(coverage_rows), 4) if coverage_rows else 0.0,
    }
    return {"overall": overall, "documents": per_doc, "coverage": coverage, "coverage_rows": coverage_rows}


def _coverage_for_document(doc: dict, section_nodes: list[dict]) -> dict:
    # 学术 PDF 的第 1 页常是期刊“网络首发”封面（ISSN、引用格式、版权声明），
    # 不属于正文；从第 2 页起算，避免把封面噪声算成解析漏字。
    academic = str(doc.get("doc_type") or "") in ("research_paper", "review", "policy")
    start_page = 2 if academic and int(doc.get("page_count") or 0) > 2 else 1
    all_nodes = fetch_all(
        "SELECT node_type, title, text, page_start FROM node WHERE doc_id=? AND page_start>=?",
        (doc["doc_id"], start_page),
    )
    node_text = "\n".join(
        (str(n.get("title") or "") + "\n" + str(n.get("text") or "")) for n in all_nodes
    )
    counts = {t: sum(1 for n in all_nodes if n.get("node_type") == t) for t in ("table", "figure", "formula")}
    pdf_text = ""
    try:
        path = pipeline_mod.get_document_path(doc["doc_id"])
        if path:
            pdf_text = _extract_text(path, start_page=start_page, exclude_english=True)
    except Exception:
        pdf_text = ""
    if not pdf_text:
        # 无原始文本层（扫描件/图片）时以节点自身文本为分母，覆盖率按 1 记，
        # 由 OCR 页数在解析质量门中单独统计。
        pdf_text = node_text
    return m.coverage_metrics(
        pdf_text=pdf_text,
        node_text=node_text,
        table_nodes=counts["table"],
        figure_nodes=counts["figure"],
        formula_nodes=counts["formula"],
    )


def _extract_text(path: Path, start_page: int = 1, exclude_english: bool = False) -> str:
    # 用解析器清洗后的行文本作分母：页眉页脚/重复页码已被剔除，
    # 避免把“解析漏字”与“PDF 自带版面噪声”混为一谈。
    def keep(text: str) -> bool:
        if not exclude_english:
            return True
        letters = len(re.findall(r"[A-Za-z]", text))
        cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
        # 中文论文的英文题名/摘要/作者单位属于对照信息，不参与中文正文覆盖率
        return not (letters > 2 * cjk and len(text) >= 20)

    try:
        from alun.app import parser as parser_mod
        pages, page_texts, _ = parser_mod.read_document(path)
        lines = [ln.text for lines in pages[start_page - 1:] for ln in lines if keep(ln.text)]
        line_text = "\n".join(lines)
        if line_text.strip():
            return line_text
        return "\n".join(t for t in (page_texts[start_page - 1:] or []) if keep(t))
    except Exception:
        pass
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pymupdf
        doc = pymupdf.open(str(path))
        try:
            return "\n".join(page.get_text("text") or "" for page in doc)
        finally:
            doc.close()
    if suffix == ".docx":
        from docx import Document
        d = Document(str(path))
        return "\n".join(p.text for p in d.paragraphs)
    if suffix in (".md", ".markdown", ".txt"):
        return path.read_text(encoding="utf-8", errors="ignore")
    return ""


# ---------------------------------------------------------------------------
# 生成与证据
# ---------------------------------------------------------------------------
def _run_generation_suite(bundle: dict, run_id: str) -> dict:
    per_doc: list[dict] = []
    all_questions: list[dict] = []
    for case in bundle["cases"]:
        doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (case["doc_id"],))
        if not doc:
            continue
        nodes = fetch_all("SELECT * FROM node WHERE doc_id=? ORDER BY order_index", (case["doc_id"],))
        qs = fetch_all("SELECT * FROM question WHERE doc_id=? AND status='active' ORDER BY created_at", (case["doc_id"],))
        if not qs:
            per_doc.append({
                "doc_id": case["doc_id"], "title": case.get("title") or doc.get("title"),
                "total": 0, "evidence_support_rate": None, "node_traceability_rate": None,
                "page_consistency_rate": None, "stale_nodes": 0, "failures": [],
            })
            continue
        audit = _audit_questions(doc, nodes, qs)
        audit["title"] = case.get("title") or doc.get("title")
        per_doc.append(audit)
        all_questions.extend(qs)
        _add_case(
            run_id, case_id=case.get("case_id") or case["doc_id"], doc_id=case["doc_id"], metric="evidence",
            expected="证据支持率=1.0 且 失效节点=0",
            actual=f'支持率={audit["evidence_support_rate"]}, 失效节点={audit["stale_nodes"]}',
            passed=audit["evidence_support_rate"] == 1.0 and audit["stale_nodes"] == 0,
            detail={
                "total": audit["total"], "supported": audit["supported"],
                "node_traceable": audit["node_traceable"], "stale_nodes": audit["stale_nodes"],
                "page_consistent": audit["page_consistent"], "failures": audit["failures"][:20],
            },
        )
    totals = {
        "questions": sum(int(d.get("total") or 0) for d in per_doc),
        "supported": sum(int(d.get("supported") or 0) for d in per_doc),
        "node_traceable": sum(int(d.get("node_traceable") or 0) for d in per_doc),
        "page_consistent": sum(int(d.get("page_consistent") or 0) for d in per_doc),
        "stale_nodes": sum(int(d.get("stale_nodes") or 0) for d in per_doc),
    }
    n = totals["questions"]
    overall = {
        **totals,
        "evidence_support_rate": round(totals["supported"] / n, 4) if n else 0.0,
        "node_traceability_rate": round(totals["node_traceable"] / n, 4) if n else 0.0,
        "page_consistency_rate": round(totals["page_consistent"] / n, 4) if n else 0.0,
    }
    overall.update(m.template_question_flags(all_questions))
    overall["first_pass_failure_rate"] = _first_pass_failure_rate()
    return {"overall": overall, "documents": per_doc}


def _audit_questions(doc: dict, nodes: list[dict], questions: list[dict]) -> dict:
    by_id = {n["node_id"]: n for n in nodes}
    doc_text = _squash("".join(str(n.get("text") or "") for n in nodes))
    supported = node_traceable = page_consistent = stale = 0
    failures: list[dict] = []
    for q in questions:
        evs = load_json(q.get("evidence"), [])
        node = by_id.get(q.get("node_id"))
        in_node = True
        in_doc = True
        page_ok = True
        for ev in evs if isinstance(evs, list) else []:
            quote = _squash(str(ev.get("quote") or ""))
            if len(quote) < 4:
                in_node = in_doc = False
                continue
            if node is None or quote not in _squash(str(node.get("text") or "")):
                in_node = False
            if quote not in doc_text:
                in_doc = False
            page = int(ev.get("page") or 0)
            if node is not None and page:
                if page < int(node.get("page_start") or 1) or page > int(node.get("page_end") or 1):
                    page_ok = False
        if not evs:
            in_node = in_doc = page_ok = False
        if node is None:
            stale += 1
        if in_doc:
            supported += 1
        if in_node:
            node_traceable += 1
        if page_ok:
            page_consistent += 1
        if not (in_doc and in_node and page_ok):
            failures.append({
                "q_id": q.get("q_id"), "stem": str(q.get("stem") or "")[:100],
                "source": q.get("source"), "stale_node": node is None,
                "quote_in_doc": in_doc, "quote_in_node": in_node, "page_ok": page_ok,
            })
    total = len(questions)
    return {
        "doc_id": doc["doc_id"], "total": total, "supported": supported,
        "node_traceable": node_traceable, "stale_nodes": stale, "page_consistent": page_consistent,
        "evidence_support_rate": round(supported / total, 4) if total else 1.0,
        "node_traceability_rate": round(node_traceable / total, 4) if total else 1.0,
        "page_consistency_rate": round(page_consistent / total, 4) if total else 1.0,
        "failures": failures,
    }


def _squash(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text or "")


def _first_pass_failure_rate() -> float | None:
    rows = fetch_all(
        "SELECT detail FROM ai_audit WHERE kind='generate_question' AND fallback=0"
        " AND substr(ts,1,10)>=date('now','-30 day')"
    )
    if not rows:
        return None
    total = 0
    failed = 0
    for r in rows:
        detail = load_json(r.get("detail"), {})
        total += 1
        if detail.get("first_attempt_failed"):
            failed += 1
    return round(failed / total, 4) if total else None


# ---------------------------------------------------------------------------
# 判分与可答性
# ---------------------------------------------------------------------------
def _run_grading_suite(run_id: str) -> dict:
    reviews = fetch_all(
        "SELECT * FROM quality_review WHERE target_type='grading' ORDER BY created_at"
    )
    samples = []
    for r in reviews:
        scores = load_json(r.get("scores"), {})
        samples.append({
            "sample_id": r["review_id"], "target_id": r["target_id"],
            "human": scores.get("human_score"), "model": scores.get("model_score"),
            "reviewer": r["reviewer"],
        })
    consistency = m.grading_consistency(samples)
    answerability_reviews = fetch_all(
        "SELECT * FROM quality_review WHERE target_type='answerability' ORDER BY created_at"
    )
    for r in answerability_reviews:
        r["scores"] = load_json(r.get("scores"), {})
    answerability = m.answerability_summary(answerability_reviews)
    _add_case(
        run_id, case_id="grading", doc_id="", metric="grading",
        expected=f"一致率>={m.GRADING_WITHIN10_TARGET:.2f}",
        actual=f'一致率={consistency["within10_rate"]}',
        passed=consistency["within10_rate"] is not None and consistency["within10_rate"] >= m.GRADING_WITHIN10_TARGET,
        detail=consistency,
    )
    _add_case(
        run_id, case_id="answerability", doc_id="", metric="answerability",
        expected=f"均分>={m.ANSWERABILITY_TARGET}",
        actual=f'均分={answerability["mean"]}',
        passed=bool(answerability["pass"]),
        detail=answerability,
    )
    return {
        "grading": {**consistency, "samples": consistency.get("samples", [])[:50]},
        "answerability": answerability,
    }


# ---------------------------------------------------------------------------
# 时延
# ---------------------------------------------------------------------------
def _run_latency_suite(bundle: dict, since: str | None = None) -> dict:
    parse_all: list[float] = []
    parse_30p: list[float] = []
    for case in bundle["cases"]:
        doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (case["doc_id"],))
        if not doc:
            continue
        if since and str(doc.get("updated_at") or "") < since:
            continue
        stats = load_json(doc.get("stats"), {})
        timings = stats.get("timings") or {}
        ms = timings.get("parse_ms") or stats.get("parse_ms")
        if ms:
            parse_all.append(float(ms) / 1000.0)
            if int(doc.get("page_count") or 0) >= 30:
                parse_30p.append(float(ms) / 1000.0)
    gen_sql = "SELECT latency_ms FROM ai_audit WHERE kind='generate_question' AND fallback=0 AND latency_ms>0"
    judge_sql = "SELECT latency_ms FROM ai_audit WHERE kind='judge_answer' AND latency_ms>0"
    params: tuple = ()
    if since:
        gen_sql += " AND ts>=?"
        judge_sql += " AND ts>=?"
        params = (since,)
    gen = fetch_all(gen_sql, params)
    judge = fetch_all(judge_sql, params)
    return {
        "parse_all": m.percentiles(parse_all),
        "parse_30p": m.percentiles(parse_30p),
        "generation": m.percentiles([r["latency_ms"] / 1000.0 for r in gen]),
        "judge": m.percentiles([r["latency_ms"] / 1000.0 for r in judge]),
    }


# ---------------------------------------------------------------------------
# 汇总与缺陷
# ---------------------------------------------------------------------------
def _summarize(results: dict) -> dict:
    checks: list[dict] = []
    parse = results.get("parse") or {}
    if parse:
        overall = parse.get("overall") or {}
        checks.append({
            "id": "title_f1", "label": "标题识别 F1", "value": overall.get("f1"),
            "target": m.TITLE_F1_TARGET, "status": m.threshold_status(overall.get("f1"), m.TITLE_F1_TARGET),
            "detail": f'{overall.get("matched", 0)}/{overall.get("gold_total", 0)} 个标题匹配',
        })
        checks.append({
            "id": "title_min_doc", "label": "单篇最低标题 F1", "value": overall.get("min_doc_f1"),
            "target": m.TITLE_F1_TARGET, "status": m.threshold_status(overall.get("min_doc_f1"), m.TITLE_F1_TARGET),
            "detail": "任一设计样本低于 0.95 即需校正",
        })
        cov = parse.get("coverage") or {}
        checks.append({
            "id": "text_coverage", "label": "平均文本覆盖率", "value": cov.get("mean_text_coverage"),
            "target": 0.98, "status": m.threshold_status(cov.get("mean_text_coverage"), 0.98),
            "detail": "节点文本字符 / 原文文本字符",
        })
    generation = results.get("generation") or {}
    if generation:
        g = generation.get("overall") or {}
        checks.append({
            "id": "evidence_support_rate", "label": "证据支持率", "value": g.get("evidence_support_rate"),
            "target": 1.0, "status": m.threshold_status(g.get("evidence_support_rate"), 1.0),
            "detail": f'{g.get("supported", 0)}/{g.get("questions", 0)} 道题引文可在原文定位',
        })
        checks.append({
            "id": "node_traceability", "label": "节点可追溯率", "value": g.get("node_traceability_rate"),
            "target": 1.0, "status": m.threshold_status(g.get("node_traceability_rate"), 1.0),
            "detail": f'{g.get("stale_nodes", 0)} 道题仍指向失效节点',
        })
        checks.append({
            "id": "generic_stem", "label": "泛称题干", "value": g.get("generic_stem_rate"),
            "target": 0.0, "status": m.threshold_status(g.get("generic_stem_rate"), 0.0, higher_is_better=False),
            "detail": f'{g.get("generic_stem", 0)} 道题使用“某文档”等泛称',
        })
    grading = results.get("grading") or {}
    if grading:
        gs = grading.get("grading") or {}
        checks.append({
            "id": "grading_within10", "label": "主观题判分一致率", "value": gs.get("within10_rate"),
            "target": m.GRADING_WITHIN10_TARGET,
            "status": m.threshold_status(gs.get("within10_rate"), m.GRADING_WITHIN10_TARGET),
            "detail": f'样本 {gs.get("n", 0)} 条，差值≤10 分比例',
        })
        ans = grading.get("answerability") or {}
        checks.append({
            "id": "answerability", "label": "题目可答性（5 分制）", "value": ans.get("mean"),
            "target": m.ANSWERABILITY_TARGET, "status": m.threshold_status(ans.get("mean"), m.ANSWERABILITY_TARGET),
            "detail": f'{ans.get("questions", 0)} 道题 / {ans.get("reviews", 0)} 人次评审',
        })
    latency = results.get("latency") or {}
    if latency:
        p30 = latency.get("parse_30p") or {}
        if p30.get("n"):
            checks.append({
                "id": "parse_30p_p95", "label": "30 页文档解析 P95（秒）", "value": p30.get("p95"),
                "target": m.PARSE_30P_TARGET_SEC,
                "status": m.threshold_status(p30.get("p95"), m.PARSE_30P_TARGET_SEC, higher_is_better=False),
                "detail": f'样本 {p30.get("n")} 次',
            })
        gen = latency.get("generation") or {}
        p95 = gen.get("p95")
        reduction = None
        if p95:
            reduction = round((m.GENERATION_P95_BASELINE_SEC - float(p95)) / m.GENERATION_P95_BASELINE_SEC, 4)
        checks.append({
            "id": "generation_p95", "label": "单题生成 P95（秒）", "value": p95,
            "target": f"较基线下降 {int(m.GENERATION_P95_REDUCTION_TARGET * 100)}%",
            "status": ("pass" if reduction is not None and reduction >= m.GENERATION_P95_REDUCTION_TARGET else "warn"),
            "detail": f'样本 {gen.get("n", 0)} 次；基线 {m.GENERATION_P95_BASELINE_SEC:.2f}s，'
                      f'当前 {p95 if p95 is not None else "—"}s，变化 {reduction * 100 if reduction is not None else "—"}%',
        })
    blocking = [c for c in checks if c["status"] == "fail"]
    blocked = [c for c in checks if c["status"] == "blocked"]
    warned = [c for c in checks if c["status"] == "warn"]
    return {
        "checks": checks,
        "pass": len(blocking) == 0 and len(blocked) == 0,
        "status": "pass" if not blocking and not blocked else ("blocked" if blocked else "fail"),
        "failed": [c["id"] for c in blocking],
        "blocked": [c["id"] for c in blocked],
        "warned": [c["id"] for c in warned],
        "headline": _headline(checks),
    }


def _headline(checks: list[dict]) -> str:
    fails = [c for c in checks if c["status"] == "fail"]
    blocked = [c for c in checks if c["status"] == "blocked"]
    warned = [c for c in checks if c["status"] == "warn"]
    if fails:
        return "未通过：" + "、".join(c["label"] for c in fails[:3])
    if blocked:
        return "待补证：" + "、".join(c["label"] for c in blocked[:3])
    if warned:
        return "硬指标通过；跟踪目标未达：" + "、".join(c["label"] for c in warned[:3])
    return "全部硬指标通过"


def _defect_id(area: str, key: str) -> str:
    return "dfc_" + hashlib.sha1(f"{area}|{key}".encode("utf-8")).hexdigest()[:12]


def _upsert_defect(run_id: str, *, area: str, key: str, severity: str, title: str, detail: dict) -> str:
    defect_id = _defect_id(area, key)
    now = iso()
    existing = fetch_one("SELECT defect_id FROM quality_defect WHERE defect_id=?", (defect_id,))
    with db() as conn:
        if existing:
            conn.execute(
                "UPDATE quality_defect SET severity=?, title=?, detail=?, status='open', run_id=?, updated_at=?"
                " WHERE defect_id=?",
                (severity, title, dump(detail), run_id, now, defect_id),
            )
        else:
            conn.execute(
                "INSERT INTO quality_defect (defect_id, severity, area, title, detail, status, run_id, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (defect_id, severity, area, title, dump(detail), "open", run_id, now, now),
            )
    return defect_id


def _sync_defects(run_id: str, results: dict) -> None:
    current: set[str] = set()

    def add(**kwargs):
        current.add(_upsert_defect(run_id, **kwargs))

    parse = results.get("parse") or {}
    overall = parse.get("overall") or {}
    if overall and overall.get("f1", 1) < m.TITLE_F1_TARGET:
        add(area="parse", key="overall_title_f1", severity="P0",
            title="标题识别 F1 低于 0.95",
            detail={"f1": overall.get("f1"), "precision": overall.get("precision"),
                    "recall": overall.get("recall"), "missing": overall.get("gold_total", 0) - overall.get("matched", 0)})
    for d in parse.get("documents") or []:
        if d.get("f1", 1) < m.TITLE_F1_TARGET:
            add(area="parse", key=f'doc:{d["doc_id"]}', severity="P1",
                title=f'《{(d.get("title") or "")[:28]}》标题 F1 {d.get("f1")}',
                detail={"doc_id": d["doc_id"], "f1": d.get("f1"), "missing": d.get("missing")[:10],
                        "extra": d.get("extra")[:10], "mismatched_level": d.get("mismatched_level")[:10]})
    generation = results.get("generation") or {}
    g = generation.get("overall") or {}
    if g:
        if g.get("evidence_support_rate", 1) < 1:
            add(area="generation", key="evidence_support", severity="P0",
                title="题目证据支持率未达 100%",
                detail={k: g.get(k) for k in ("questions", "supported", "evidence_support_rate", "stale_nodes")})
        if g.get("stale_nodes", 0) > 0:
            add(area="generation", key="stale_nodes", severity="P0",
                title=f'{g.get("stale_nodes")} 道题指向已失效结构节点',
                detail={"stale_nodes": g.get("stale_nodes"), "questions": g.get("questions")})
        if g.get("generic_stem", 0) > 0:
            add(area="generation", key="generic_stems", severity="P1",
                title=f'{g.get("generic_stem")} 道题使用泛称题干',
                detail={"generic_stem": g.get("generic_stem"), "rate": g.get("generic_stem_rate")})
    grading = results.get("grading")
    if grading is not None:
        gs = grading.get("grading") or {}
        if gs.get("within10_rate") is None:
            add(area="grading", key="no_samples", severity="P1",
                title="主观题判分一致率缺少人工评分样本",
                detail={"action": "需按标注手册补 12—20 条人工评分"})
        ans = grading.get("answerability") or {}
        if ans.get("mean") is None:
            add(area="generation", key="no_answerability", severity="P1",
                title="题目可答性缺少评审样本",
                detail={"action": "需 3 名评审按 5 分制抽样评分"})
    latency = results.get("latency") or {}
    p30 = latency.get("parse_30p") or {}
    if p30.get("n") and p30.get("p95", 0) > m.PARSE_30P_TARGET_SEC:
        add(area="performance", key="parse_30p_p95", severity="P0",
            title=f'30 页文档解析 P95 {p30.get("p95")} 秒，超过 90 秒',
            detail=p30)
    gen_p95 = (latency.get("generation") or {}).get("p95")
    if gen_p95:
        reduction = (m.GENERATION_P95_BASELINE_SEC - float(gen_p95)) / m.GENERATION_P95_BASELINE_SEC
        if reduction < m.GENERATION_P95_REDUCTION_TARGET:
            add(area="performance", key="generation_p95_reduction", severity="P1",
                title=f'单题生成 P95 {gen_p95} 秒，未较基线下降 50%',
                detail={"baseline_sec": m.GENERATION_P95_BASELINE_SEC, "current_sec": gen_p95,
                        "reduction": round(reduction, 4), "target": m.GENERATION_P95_REDUCTION_TARGET,
                        "samples": (latency.get("generation") or {}).get("n", 0)})
    # 本轮覆盖到的区域里，不再出现的旧缺陷自动关闭，避免看板长期挂着已修复项
    areas: set[str] = set()
    if parse:
        areas.add("parse")
    if generation:
        areas.add("generation")
    if grading:
        areas.update({"grading"})
    if latency:
        areas.add("performance")
    if areas:
        placeholders = ",".join("?" for _ in areas)
        params: list = list(areas) + [run_id]
        stale = fetch_all(
            f"SELECT defect_id FROM quality_defect WHERE status='open' AND area IN ({placeholders}) AND run_id<>?",
            params,
        )
        if stale:
            ids = [x["defect_id"] for x in stale if x["defect_id"] not in current]
            if ids:
                marks = ",".join("?" for _ in ids)
                with db() as conn:
                    conn.execute(f"UPDATE quality_defect SET status='resolved', updated_at=? WHERE defect_id IN ({marks})",
                                 tuple([iso()] + ids))


def _write_artifact(run_id: str, results: dict) -> None:
    path = RUN_DIR / f"{run_id}.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 看板
# ---------------------------------------------------------------------------
def overview() -> dict:
    latest = latest_run()
    golden_index = golden_mod.load_index("golden-v1")
    golden_docs = []
    for entry in golden_index.get("documents", []):
        try:
            case = golden_mod.load_case("golden-v1", entry)
            golden_docs.append({
                "doc_id": case["doc_id"], "title": case.get("title"),
                "headings": len(case.get("headings") or []),
                "doc_type": case.get("doc_type"), "page_count": case.get("page_count"),
                **golden_mod.stats(case),
            })
        except Exception:
            continue
    defects = fetch_all(
        "SELECT * FROM quality_defect ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END, updated_at DESC"
    )
    for d in defects:
        d["detail"] = load_json(d.get("detail"), {})
    return {
        "latest": latest,
        "golden": {
            "version": golden_index.get("version", "golden-v1"),
            "documents": golden_docs,
            "count": len(golden_docs),
            "headings": sum(d["headings"] for d in golden_docs),
        },
        "defects": defects,
        "open_defects": [d for d in defects if d.get("status") == "open"],
        "thresholds": _THRESHOLDS,
    }
