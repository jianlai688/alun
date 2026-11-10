"""Redis Streams worker：消费解析任务。"""
from __future__ import annotations

import signal
import sys
import time
from pathlib import Path

from ..app import pipeline
from ..app.db import init_db
from . import queue


def handle(job: dict) -> None:
    if job.get("kind") != "parse":
        return
    p = job.get("payload") or {}
    path = Path(p["path"])
    if not path.exists():
        path = pipeline.get_document_path(p["document_id"]) or path
    pipeline.run_parse_task(
        p["task_id"], p["document_id"], path,
        p.get("doc_type") or "auto", p.get("title"),
    )


def main() -> None:
    init_db()
    stop = False

    def _stop(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    consumer = sys.argv[1] if len(sys.argv) > 1 else "worker-1"
    print(f"[alun-worker] started consumer={consumer}", flush=True)
    while not stop:
        try:
            job = queue.consume(consumer)
            if not job:
                continue
            print(f"[alun-worker] job={job['id']} kind={job['kind']}", flush=True)
            handle(job)
            queue.ack(job["id"])
        except Exception as exc:
            print(f"[alun-worker] error: {exc}", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    main()
