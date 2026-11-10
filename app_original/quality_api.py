"""质量看板 API：评测运行、黄金集、缺陷台账与人工复核。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id
from .security import Principal, principal_from_request, require_admin
from alun.quality import golden as golden_mod
from alun.quality import runner as quality_runner
from alun.quality import metrics as quality_metrics
from alun.quality import report as quality_report

router = APIRouter(prefix="/api/v1/admin/quality", tags=["quality"])


def quality_admin(request: Request) -> Principal:
    p = principal_from_request(request)
    require_admin(p)
    return p


@router.get("/overview")
def overview(p: Principal = Depends(quality_admin)):
    data = quality_runner.overview()
    latest = data.get("latest")
    if latest and latest.get("run_id"):
        latest = quality_runner.get_run(latest["run_id"], include_cases=False)
        data["latest"] = latest
    return data


@router.get("/runs")
def runs(limit: int = 20, p: Principal = Depends(quality_admin)):
    return {"items": quality_runner.list_runs(limit)}


@router.get("/runs/{run_id}")
def run_detail(run_id: str, p: Principal = Depends(quality_admin)):
    try:
        return quality_runner.get_run(run_id, include_cases=True, case_limit=800)
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "评测运行不存在"})


@router.get("/runs/{run_id}/report")
def run_report(run_id: str, p: Principal = Depends(quality_admin)):
    try:
        run = quality_runner.get_run(run_id, include_cases=True, case_limit=2000)
    except KeyError:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "评测运行不存在"})
    text = quality_report.render_markdown(run)
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'},
    )


@router.post("/runs", status_code=202)
def start_run(payload: dict | None = None, p: Principal = Depends(quality_admin)):
    payload = payload or {}
    suite = str(payload.get("suite") or "all")
    if suite not in ("all", "parse", "generation", "grading", "latency"):
        raise HTTPException(status_code=422, detail={"code": "INVALID_SUITE", "message": "suite 不合法"})
    version = str(payload.get("version") or "golden-v1")
    try:
        golden_mod.load_version(version)
    except Exception as exc:
        raise HTTPException(status_code=422, detail={"code": "GOLDEN_NOT_FOUND", "message": str(exc)})
    return quality_runner.run_background(version, suite, p.username or p.user_id)


@router.get("/golden")
def golden(version: str = "golden-v1", p: Principal = Depends(quality_admin)):
    try:
        bundle = golden_mod.load_version(version)
    except Exception as exc:
        raise HTTPException(status_code=404, detail={"code": "GOLDEN_NOT_FOUND", "message": str(exc)})
    items = []
    for case in bundle["cases"]:
        items.append({
            "doc_id": case["doc_id"],
            "title": case.get("title"),
            "doc_type": case.get("doc_type"),
            "page_count": case.get("page_count"),
            "headings": len(case.get("headings") or []),
            "annotation": case.get("annotation") or {},
            "problems": golden_mod.validate_case(case),
            "stats": golden_mod.stats(case),
        })
    return {
        "version": bundle["version"],
        "index": bundle["index"],
        "items": items,
        "total": len(items),
        "headings": sum(len(c.get("headings") or []) for c in bundle["cases"]),
    }


@router.get("/defects")
def defects(status: str = "", severity: str = "", p: Principal = Depends(quality_admin)):
    sql = "SELECT * FROM quality_defect WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if severity:
        sql += " AND severity=?"
        params.append(severity)
    sql += " ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END, updated_at DESC"
    rows = fetch_all(sql, params)
    for r in rows:
        r["detail"] = load_json(r.get("detail"), {})
    return {"items": rows, "total": len(rows)}


@router.patch("/defects/{defect_id}")
def update_defect(defect_id: str, payload: dict, p: Principal = Depends(quality_admin)):
    status = str(payload.get("status") or "open")
    if status not in ("open", "fixed", "wontfix", "resolved"):
        raise HTTPException(status_code=422, detail={"code": "INVALID_STATUS", "message": "状态不合法"})
    with db() as conn:
        conn.execute("UPDATE quality_defect SET status=?, updated_at=? WHERE defect_id=?", (status, iso(), defect_id))
    return {"defect_id": defect_id, "status": status}


@router.get("/reviews")
def reviews(target_type: str = "", target_id: str = "", limit: int = 100, p: Principal = Depends(quality_admin)):
    sql = "SELECT * FROM quality_review WHERE 1=1"
    params: list = []
    if target_type:
        sql += " AND target_type=?"
        params.append(target_type)
    if target_id:
        sql += " AND target_id=?"
        params.append(target_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(max(1, min(500, int(limit or 100))))
    rows = fetch_all(sql, params)
    for r in rows:
        r["scores"] = load_json(r.get("scores"), {})
    return {"items": rows, "total": len(rows)}


@router.post("/reviews", status_code=201)
def add_review(payload: dict, p: Principal = Depends(quality_admin)):
    target_type = str(payload.get("target_type") or "")
    target_id = str(payload.get("target_id") or "")
    if target_type not in ("grading", "answerability"):
        raise HTTPException(status_code=422, detail={"code": "INVALID_TARGET", "message": "target_type 只能是 grading / answerability"})
    if not target_id:
        raise HTTPException(status_code=422, detail={"code": "INVALID_TARGET", "message": "target_id 不能为空"})
    scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    if target_type == "grading":
        for key in ("human_score", "model_score"):
            try:
                scores[key] = max(0.0, min(100.0, float(scores.get(key))))
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail={"code": "INVALID_SCORE", "message": "grading 需要 0—100 的 human_score / model_score"})
    else:
        try:
            scores["answerability"] = max(1.0, min(5.0, float(scores.get("answerability"))))
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail={"code": "INVALID_SCORE", "message": "answerability 需要 1—5 分"})
    review_id = new_id("qrev")
    with db() as conn:
        conn.execute(
            "INSERT INTO quality_review (review_id, target_type, target_id, reviewer, scores, notes, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (review_id, target_type, target_id, p.username or p.user_id, dump(scores),
             str(payload.get("notes") or "")[:500], iso()),
        )
    return {"review_id": review_id, "target_type": target_type, "target_id": target_id, "scores": scores}


@router.get("/thresholds")
def thresholds(p: Principal = Depends(quality_admin)):
    return {
        "title_f1": quality_metrics.TITLE_F1_TARGET,
        "evidence_support_rate": quality_metrics.EVIDENCE_TARGET,
        "answerability": quality_metrics.ANSWERABILITY_TARGET,
        "grading_within10_rate": quality_metrics.GRADING_WITHIN10_TARGET,
        "parse_30p_p95_sec": quality_metrics.PARSE_30P_TARGET_SEC,
    }
