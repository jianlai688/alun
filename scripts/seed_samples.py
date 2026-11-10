"""把工作目录中的 6 份样例 PDF 导入正在运行的 A论 服务。

用法（在 D:\\AI论文 下）：
    python alun/scripts/seed_samples.py --base http://127.0.0.1:8010/api/v1
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8010/api/v1")
    parser.add_argument("--username", default="demo")
    parser.add_argument("--password", default="demo123")
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parents[2]))
    args = parser.parse_args()
    login = httpx.post(args.base + "/auth/login", json={"username": args.username, "password": args.password}, timeout=20)
    if login.status_code != 200:
        print("登录失败：", login.status_code, login.text)
        return 1
    headers = {"Authorization": "Bearer " + login.json()["access_token"]}
    root = Path(args.dir)
    pdfs = sorted(root.glob("*.pdf"))
    if not pdfs:
        print("没有找到 PDF")
        return 1
    documents = []
    for path in pdfs:
        with path.open("rb") as fh:
            resp = httpx.post(
                args.base + "/documents",
                headers=headers,
                files={"file": (path.name, fh, "application/pdf")},
                data={"doc_type": "auto", "language": "zh-CN"},
                timeout=120,
            )
        print(path.name, resp.status_code)
        if resp.status_code in (200, 202):
            documents.append(resp.json()["document_id"])
    for doc_id in documents:
        for _ in range(120):
            time.sleep(1)
            data = httpx.get(f"{args.base}/documents/{doc_id}", headers=headers, timeout=20).json()
            if data["status"] in ("ready", "needs_review", "failed"):
                print(doc_id, data["status"], data.get("stats"))
                break
    return 0


if __name__ == "__main__":
    sys.exit(main())
