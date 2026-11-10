"""V1.1 并发压测：上传解析与 12 题全流程。

用法（服务需先启动）：
    python -m alun.scripts.quality_stress --mode parse --concurrency 10
    python -m alun.scripts.quality_stress --mode full --concurrency 3 --questions 12
    python -m alun.scripts.quality_stress --mode mixed --concurrency 10

说明：解析压测会把同一份 PDF 上传 N 次（追加不同尾部字节以绕过 SHA-256 去重），
每次都是独立的解析任务，避免并发重解析同一文档造成数据竞争。
"""
from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

DEFAULT_PDF = Path(__file__).resolve().parents[2] / "alun" / "data" / "uploads" / "doc_06f1cbf896.pdf"
OUT_DIR = Path(__file__).resolve().parents[2] / "alun" / "data" / "quality"
OUT_DIR.mkdir(parents=True, exist_ok=True)


class Client:
    def __init__(self, base: str, username: str, password: str):
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=180.0)
        r = self.http.post(f"{self.base}/api/v1/auth/login", json={"username": username, "password": password})
        r.raise_for_status()
        data = r.json()
        self.token = data["access_token"]
        self.user = data["user"]
        self.http.headers.update({"Authorization": "Bearer " + self.token, "X-Tenant-Id": self.user["tenant_id"]})

    def upload(self, path: Path, tag: str) -> dict:
        raw = path.read_bytes()
        # 追加注释字节，保证每次上传 SHA-256 不同但 PDF 结构仍可解析
        payload = raw + f"\n%ALUN-STRESS-{tag}\n".encode("utf-8")
        r = self.http.post(
            f"{self.base}/api/v1/documents",
            files={"file": (f"stress-{tag}.pdf", payload, "application/pdf")},
            data={"doc_type": "auto"},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"upload {r.status_code}: {r.text[:200]}")
        return r.json()

    def task(self, task_id: str) -> dict:
        r = self.http.get(f"{self.base}/api/v1/tasks/{task_id}")
        r.raise_for_status()
        return r.json()

    def wait_task(self, task_id: str, timeout: float = 600) -> dict:
        started = time.time()
        while time.time() - started < timeout:
            t = self.task(task_id)
            if t.get("status") in ("succeeded", "failed", "cancelled"):
                t["_wall_sec"] = round(time.time() - started, 2)
                return t
            time.sleep(0.8)
        return {"status": "timeout", "_wall_sec": round(time.time() - started, 2)}

    def session(self, doc_id: str, questions: int) -> dict:
        r = self.http.post(f"{self.base}/api/v1/sessions", json={
            "document_id": doc_id, "mode": "guided", "question_count": questions,
            "question_types": ["single_choice", "true_false", "fill_blank", "short_answer"],
        })
        r.raise_for_status()
        return r.json()

    def next_question(self, session_id: str) -> dict:
        r = self.http.post(f"{self.base}/api/v1/sessions/{session_id}/next-question")
        if r.status_code >= 400:
            raise RuntimeError(f"next {r.status_code}: {r.text[:200]}")
        return r.json()

    def answer(self, session_id: str, turn_id: str, q: dict) -> dict:
        qtype = q.get("q_type")
        if qtype == "single_choice":
            options = q.get("options") or []
            value = options[0]["key"] if options else "A"
        elif qtype == "multiple_choice":
            options = q.get("options") or []
            value = [o["key"] for o in options[:2]]
        elif qtype == "true_false":
            value = True
        else:
            value = "用于压测的占位答案，不保证正确。"
        r = self.http.post(f"{self.base}/api/v1/sessions/{session_id}/answers", json={
            "turn_id": turn_id, "answer": {"type": qtype, "value": value}, "hint_level": 0,
            "time_spent_ms": 1000,
        })
        if r.status_code >= 400:
            raise RuntimeError(f"answer {r.status_code}: {r.text[:200]}")
        return r.json()


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    idx = max(0, min(len(data) - 1, int(q * len(data) + 0.999999) - 1))
    return round(data[idx], 2)


def run_parse(client: Client, pdf: Path, n: int) -> dict:
    results: list[dict] = []
    lock = threading.Lock()

    def one(i: int) -> dict:
        started = time.time()
        try:
            up = client.upload(pdf, f"{int(time.time())}-{i}")
            if up.get("deduplicated"):
                return {"i": i, "ok": False, "error": "deduplicated"}
            task = client.wait_task(up["task_id"], timeout=600)
            return {
                "i": i, "ok": task.get("status") == "succeeded", "status": task.get("status"),
                "wall_sec": round(time.time() - started, 2), "task_sec": task.get("_wall_sec"),
                "document_id": up.get("document_id"), "error": task.get("error"),
            }
        except Exception as exc:  # noqa: BLE001
            return {"i": i, "ok": False, "error": str(exc)[:300], "wall_sec": round(time.time() - started, 2)}

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(one, i) for i in range(n)]
        for f in as_completed(futures):
            r = f.result()
            with lock:
                results.append(r)
            print(json.dumps(r, ensure_ascii=False), flush=True)
    walls = [r["wall_sec"] for r in results if r.get("ok")]
    return {
        "mode": "parse", "concurrency": n, "pdf": str(pdf), "total": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "wall_p50": _pct(walls, 0.50), "wall_p95": _pct(walls, 0.95), "wall_max": round(max(walls), 2) if walls else 0,
        "results": results,
    }


def run_full(client: Client, doc_id: str, n: int, questions: int) -> dict:
    results: list[dict] = []
    lock = threading.Lock()

    def one(i: int) -> dict:
        started = time.time()
        latencies: list[float] = []
        errors: list[str] = []
        try:
            s = client.session(doc_id, questions)
            for _ in range(questions):
                t0 = time.time()
                turn = client.next_question(s["session_id"])
                latencies.append(time.time() - t0)
                q = turn["question"]
                client.answer(s["session_id"], turn["turn_id"], q)
            return {"i": i, "ok": not errors, "session_id": s["session_id"],
                    "wall_sec": round(time.time() - started, 2), "questions": questions,
                    "question_latencies": [round(x, 2) for x in latencies], "errors": errors}
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc)[:300])
            return {"i": i, "ok": False, "wall_sec": round(time.time() - started, 2),
                    "questions_done": len(latencies), "errors": errors, "question_latencies": [round(x, 2) for x in latencies]}

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(one, i) for i in range(n)]
        for f in as_completed(futures):
            r = f.result()
            with lock:
                results.append(r)
            print(json.dumps({k: r[k] for k in ("i", "ok", "wall_sec", "questions") if k in r}, ensure_ascii=False), flush=True)
    all_lat = [x for r in results for x in r.get("question_latencies", [])]
    walls = [r["wall_sec"] for r in results if r.get("ok")]
    return {
        "mode": "full", "concurrency": n, "document_id": doc_id, "total": len(results),
        "ok": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
        "questions_done": len(all_lat),
        "question_p50": _pct(all_lat, 0.50), "question_p95": _pct(all_lat, 0.95), "question_max": round(max(all_lat), 2) if all_lat else 0,
        "wall_p50": _pct(walls, 0.50), "wall_p95": _pct(walls, 0.95),
        "results": results,
    }


def _markdown(report: dict) -> str:
    lines = [f"# A论 并发压测报告（{report['generated_at']}）", ""]
    lines.append(f"- 模式：`{report['mode']}`；并发：{report['concurrency']}；总请求：{report['total']}；成功：{report['ok']}；失败：{report['failed']}")
    env = report.get("environment") or {}
    db_env = env.get("database", {}).get("backend", "sqlite") if isinstance(env.get("database"), dict) else "sqlite"
    queue_env = env.get("queue", {}).get("backend", "inline") if isinstance(env.get("queue"), dict) else "inline"
    lines.append(f"- 环境：Windows / Python {report['python']} / {db_env} / queue={queue_env}；模型网关 127.0.0.1:7863")
    lines.append("")
    for section in report["sections"]:
        lines.append(f"## {section['mode']}")
        lines.append("")
        lines.append(f"- 成功 {section['ok']}/{section['total']}；P50 墙钟 {section.get('wall_p50')} 秒；P95 {section.get('wall_p95')} 秒；最大 {section.get('wall_max')} 秒")
        if section["mode"] == "full":
            lines.append(f"- 题目生成延迟：P50 {section.get('question_p50')} 秒；P95 {section.get('question_p95')} 秒；最大 {section.get('question_max')} 秒；完成 {section.get('questions_done')} 题")
        failures = [r for r in section["results"] if not r.get("ok")]
        if failures:
            lines.append("")
            lines.append("失败明细：")
            for f in failures[:10]:
                lines.append(f"- #{f.get('i')}：{f.get('error') or f.get('errors')}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--mode", default="parse", choices=["parse", "full", "mixed"])
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--questions", type=int, default=12)
    parser.add_argument("--pdf", default=str(DEFAULT_PDF))
    parser.add_argument("--document-id", default="")
    parser.add_argument("--keep-documents", action="store_true", help="保留压测上传的文档（默认清理）")
    args = parser.parse_args()

    client = Client(args.base_url, args.username, args.password)
    pdf = Path(args.pdf)
    doc_id = args.document_id
    if not doc_id:
        docs = client.http.get(f"{args.base_url}/api/v1/documents?limit=100").json().get("items", [])
        target = next((d for d in docs if d.get("page_count", 0) >= 30), docs[0] if docs else None)
        if not target:
            raise SystemExit("没有可用文档，请先上传")
        doc_id = target["document_id"]

    sections = []
    if args.mode in ("parse", "mixed"):
        sections.append(run_parse(client, pdf, args.concurrency))
    if args.mode in ("full", "mixed"):
        sections.append(run_full(client, doc_id, args.concurrency, args.questions))
    try:
        environment = client.http.get(f"{args.base_url.rstrip('/')}/api/v1/health").json().get("dependencies", {})
    except Exception:
        environment = {}
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": __import__("platform").python_version(),
        "environment": environment,
        "mode": args.mode,
        "concurrency": args.concurrency,
        "total": sum(s["total"] for s in sections),
        "ok": sum(s["ok"] for s in sections),
        "failed": sum(s["failed"] for s in sections),
        "sections": sections,
    }
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (OUT_DIR / f"stress-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT_DIR / f"stress-{stamp}.md").write_text(_markdown(report), encoding="utf-8")
    if not args.keep_documents:
        for section in sections:
            for r in section.get("results", []):
                doc_id_to_clean = r.get("document_id")
                if not doc_id_to_clean:
                    continue
                try:
                    client.http.delete(
                        f"{args.base_url.rstrip('/')}/api/v1/admin/documents/{doc_id_to_clean}?purge=true"
                    )
                except Exception:
                    pass
    print(f"report: {OUT_DIR / f'stress-{stamp}.md'}")
    return 0 if report["failed"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
