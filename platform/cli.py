"""平台化运维 CLI。"""
from __future__ import annotations

import argparse
import json
import sys

from . import migrations


def main() -> int:
    ap = argparse.ArgumentParser(prog="alun-platform")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("status", "migrate"):
        p = sub.add_parser(name)
        p.add_argument("--url", default=None)
    p = sub.add_parser("rollback")
    p.add_argument("--url", default=None)
    p.add_argument("--target", type=int, default=0)
    p = sub.add_parser("migrate-sqlite")
    p.add_argument("--source", default="alun/data/alun.db")
    p.add_argument("--target", required=True)
    p.add_argument("--out", default="alun/data/platform/migration-report.json")
    p = sub.add_parser("index-doc")
    p.add_argument("--document-id", required=True)
    p.add_argument("--tenant-id", default="tnt_local")
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--tenant-id", default="tnt_local")
    p.add_argument("--document-id", default=None)

    args = ap.parse_args()
    if args.cmd == "status":
        print(json.dumps(migrations.status(args.url), ensure_ascii=False, indent=2))
    elif args.cmd == "migrate":
        print(json.dumps(migrations.migrate(args.url), ensure_ascii=False, indent=2))
    elif args.cmd == "rollback":
        print(json.dumps(migrations.rollback(args.target, args.url), ensure_ascii=False, indent=2))
    elif args.cmd == "migrate-sqlite":
        from .migrate_sqlite import migrate
        report = migrate(args.source, args.target)
        from pathlib import Path
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    elif args.cmd == "index-doc":
        from .vector_store import index_document
        print(json.dumps(index_document(args.document_id, args.tenant_id), ensure_ascii=False, indent=2))
    elif args.cmd == "search":
        from .vector_store import search
        print(json.dumps(search(args.tenant_id, args.query, args.document_id), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
