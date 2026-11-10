"""黄金集：加载、校验与版本清单。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GOLDEN_ROOT = Path(__file__).resolve().parent / "goldens"


class GoldenError(ValueError):
    pass


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GoldenError(f"无法读取黄金集文件 {path.name}: {exc}") from exc


def versions() -> list[str]:
    if not GOLDEN_ROOT.exists():
        return []
    return sorted(
        p.name for p in GOLDEN_ROOT.iterdir()
        if p.is_dir() and (p / "index.json").is_file()
    )


def load_index(version: str = "golden-v1") -> dict:
    path = GOLDEN_ROOT / version / "index.json"
    if not path.is_file():
        raise GoldenError(f"黄金集版本不存在：{version}")
    index = _read_json(path)
    index.setdefault("version", version)
    index.setdefault("documents", [])
    return index


def load_case(version: str, entry: dict) -> dict:
    path = GOLDEN_ROOT / version / str(entry.get("file") or "")
    if not path.is_file():
        # 允许 index 直接内联标注
        if entry.get("headings"):
            return dict(entry)
        raise GoldenError(f"黄金集样本文件不存在：{entry.get('file')}")
    data = _read_json(path)
    for key in ("doc_id", "title", "headings"):
        if key not in data:
            raise GoldenError(f"{path.name} 缺少字段 {key}")
    return data


def load_version(version: str = "golden-v1") -> dict:
    index = load_index(version)
    cases: list[dict] = []
    for entry in index.get("documents", []):
        case = load_case(version, entry)
        case.setdefault("doc_type", entry.get("doc_type", "other"))
        case.setdefault("case_id", entry.get("case_id") or case.get("doc_id"))
        cases.append(case)
    return {"version": version, "index": index, "cases": cases}


def validate_case(case: dict) -> list[str]:
    problems: list[str] = []
    headings = case.get("headings")
    if not isinstance(headings, list) or not headings:
        problems.append("headings 为空")
        return problems
    seen: set[str] = set()
    for i, h in enumerate(headings):
        if not isinstance(h, dict):
            problems.append(f"headings[{i}] 不是对象")
            continue
        if not str(h.get("title") or "").strip():
            problems.append(f"headings[{i}] 缺少 title")
        try:
            level = int(h.get("level"))
            page = int(h.get("page"))
        except (TypeError, ValueError):
            problems.append(f"headings[{i}] level/page 不是整数")
            continue
        if not 1 <= level <= 4:
            problems.append(f"headings[{i}] level={level} 超出 1—4")
        if page < 1:
            problems.append(f"headings[{i}] page={page} 非法")
        key = (str(h.get("title") or "").strip(), level, page)
        if key in seen:
            problems.append(f"headings[{i}] 重复：{key}")
        seen.add(key)
    return problems


def stats(case: dict) -> dict:
    headings = case.get("headings") or []
    by_level: dict[str, int] = {}
    for h in headings:
        by_level[str(h.get("level"))] = by_level.get(str(h.get("level")), 0) + 1
    return {
        "headings": len(headings),
        "by_level": by_level,
        "pages": int(case.get("page_count") or 0),
        "doc_type": case.get("doc_type") or "other",
        "annotation_method": (case.get("annotation") or {}).get("method", ""),
    }
