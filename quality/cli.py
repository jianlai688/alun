"""质量评测命令行。

用法（在 D:\\AI论文 下）：
    python -m alun.quality.cli run --suite all
    python -m alun.quality.cli run --suite parse --version golden-v1
    python -m alun.quality.cli list
    python -m alun.quality.cli golden
"""
from __future__ import annotations

import argparse
import json
import sys

from alun.quality import golden, report, runner


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alun-quality", description="A论质量评测与验收")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="运行一次评测")
    run_p.add_argument("--version", default="golden-v1")
    run_p.add_argument("--suite", default="all", choices=["all", "parse", "generation", "grading", "latency"])
    run_p.add_argument("--created-by", default="cli")
    run_p.add_argument("--since", default="", help="只统计该时间之后的时延样本，如 2026-11-06T10:45:00")
    run_p.add_argument("--report", action="store_true", help="同时生成 Markdown 报告")

    sub.add_parser("list", help="列出最近评测")
    sub.add_parser("golden", help="查看黄金集清单与校验")

    args = parser.parse_args(argv)
    if args.cmd == "golden":
        bundle = golden.load_version("golden-v1")
        out = {"version": bundle["version"], "documents": []}
        for case in bundle["cases"]:
            out["documents"].append({
                "doc_id": case["doc_id"],
                "title": case.get("title"),
                "headings": len(case.get("headings") or []),
                "problems": golden.validate_case(case),
            })
        _print(out)
        return 0
    if args.cmd == "list":
        _print(runner.list_runs(20))
        return 0
    run = runner.run_sync(args.version, args.suite, args.created_by, since=args.since or None)
    if args.report:
        text = report.render_markdown(run)
        path = runner.RUN_DIR / f"{run['run_id']}.md"
        path.write_text(text, encoding="utf-8")
        run["report_path"] = str(path)
    _print({
        "run_id": run["run_id"],
        "status": run["status"],
        "summary": run["summary"],
        "report_path": run.get("report_path"),
    })
    return 0 if run["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
