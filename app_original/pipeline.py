"""文档入库流水线与解析任务执行。"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import traceback
from pathlib import Path

from . import parser as parser_mod
from ..platform import storage
from . import audit
from .config import UPLOAD_DIR
from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id, now
from .knowledge import build_chunks, extract_knowledge

_stage_labels = {
    "validating": "校验文件",
    "text_extract": "抽取文本层",
    "layout": "版面与阅读顺序",
    "structure": "识别标题与结构树",
    "chunk": "语义分块",
    "knowledge": "抽取知识点与证据",
    "indexing": "建立索引",
    "quality": "解析质量门",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create_task(task_type: str, document_id: str | None = None, session_id: str | None = None) -> str:
    task_id = new_id("task")
    with db() as conn:
        conn.execute(
            "INSERT INTO task (task_id, type, status, stage, progress, document_id, session_id, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, task_type, "queued", "queued", 0, document_id, session_id, iso(), iso()),
        )
    return task_id


def update_task(task_id: str, *, status: str | None = None, stage: str | None = None,
                progress: int | None = None, result: dict | None = None, error: dict | None = None) -> None:
    fields = ["updated_at=?"]
    values: list = [iso()]
    if status is not None:
        fields.append("status=?")
        values.append(status)
    if stage is not None:
        fields.append("stage=?")
        values.append(stage)
    if progress is not None:
        fields.append("progress=?")
        values.append(progress)
    if result is not None:
        fields.append("result=?")
        values.append(dump(result))
    if error is not None:
        fields.append("error=?")
        values.append(dump(error))
    values.append(task_id)
    with db() as conn:
        conn.execute(f"UPDATE task SET {', '.join(fields)} WHERE task_id=?", tuple(values))


def start_parse(document_id: str, path: Path, doc_type: str = "auto", title: str | None = None) -> str:
    task_id = create_task("document_parse", document_id)
    try:
        from ..platform import queue
        if queue.enabled() and queue.enqueue("parse", {
            "task_id": task_id, "document_id": document_id, "path": str(path),
            "doc_type": doc_type, "title": title,
        }):
            return task_id
    except Exception:
        pass
    thread = threading.Thread(
        target=run_parse_task,
        args=(task_id, document_id, path, doc_type, title),
        name=f"parse-{document_id}",
        daemon=True,
    )
    thread.start()
    return task_id


def run_parse_task(task_id: str, document_id: str, path: Path, doc_type: str, title: str | None) -> None:
    try:
        timings: dict[str, int] = {}
        started = time.perf_counter()

        def mark(name: str) -> None:
            timings[name] = int((time.perf_counter() - started) * 1000)

        update_task(task_id, status="running", stage="validating", progress=3)
        _set_document(document_id, status="validating")
        time.sleep(0.2)

        update_task(task_id, stage="text_extract", progress=12)
        _set_document(document_id, status="parsing")
        prior_doc = fetch_one("SELECT doc_version FROM document WHERE doc_id=?", (document_id,)) or {"doc_version": 1}
        had_nodes = bool(fetch_one("SELECT 1 FROM node WHERE doc_id=? LIMIT 1", (document_id,)))
        parsed = parser_mod.parse_document(path, doc_type_hint=doc_type, title_hint=title)
        mark("parse_ms")

        update_task(task_id, stage="structure", progress=45)
        time.sleep(0.1)
        from . import versions as versions_mod
        if had_nodes:
            versions_mod.snapshot(document_id, reason="重新解析前自动快照")
        with db() as conn:
            conn.execute("DELETE FROM node WHERE doc_id=?", (document_id,))
            conn.execute("DELETE FROM chunk WHERE doc_id=?", (document_id,))
            conn.execute("DELETE FROM knowledge_point WHERE doc_id=?", (document_id,))
            conn.execute("DELETE FROM evidence WHERE doc_id=?", (document_id,))
            order = 0
            for node in _flatten(parsed.nodes):
                order += 1
                conn.execute(
                    "INSERT INTO node (node_id, doc_id, parent_id, level, node_type, order_no, title, text,"
                    " page_start, page_end, bbox, confidence, path, char_count, order_index)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        node.node_id, document_id, node.parent_id, node.level, node.node_type, node.order_no,
                        node.title, node.text, node.page_start, node.page_end, None, node.confidence,
                        node.path, len(node.text), order,
                    ),
                )
        mark("structure_ms")
        update_task(task_id, stage="chunk", progress=58)
        chunks = build_chunks(document_id, parsed.nodes)
        with db() as conn:
            for ch in chunks:
                conn.execute(
                    "INSERT INTO chunk (chunk_id, doc_id, node_id, text, token_count, order_no)"
                    " VALUES (?,?,?,?,?,?)",
                    (ch["chunk_id"], document_id, ch["node_id"], ch["text"], ch["token_count"], ch["order_no"]),
                )
        mark("chunk_ms")

        update_task(task_id, stage="knowledge", progress=72)
        kps, evidences = extract_knowledge(document_id, parsed.nodes)
        mark("knowledge_ms")
        with db() as conn:
            for kp in kps:
                conn.execute(
                    "INSERT INTO knowledge_point (kp_id, doc_id, node_id, name, kp_type, definition,"
                    " importance, difficulty, evidence_ids, prerequisites, order_no)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        kp["kp_id"], document_id, kp["node_id"], kp["name"], kp["kp_type"],
                        kp["definition"], kp["importance"], kp["difficulty"],
                        dump(kp["evidence_ids"]), dump(kp["prerequisites"]), kp["order_no"],
                    ),
                )
            for ev in evidences:
                conn.execute(
                    "INSERT INTO evidence (evidence_id, doc_id, node_id, page_no, quote, char_start,"
                    " char_end, strength, context) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ev["evidence_id"], document_id, ev["node_id"], ev["page_no"], ev["quote"],
                        ev["char_start"], ev["char_end"], ev["strength"], ev["context"],
                    ),
                )

        update_task(task_id, stage="quality", progress=92)
        mark("quality_ms")
        timings["total_ms"] = int((time.perf_counter() - started) * 1000)
        stats = dict(parsed.stats)
        stats["node_count"] = len(parsed.nodes)
        stats["chunk_count"] = len(chunks)
        stats["kp_count"] = len(kps)
        stats["timings"] = timings
        quality = parsed.quality
        stats["quality_metrics"] = quality.get("metrics", {})
        status = "ready" if quality["level"] in ("pass", "review") else "needs_review"
        if not parsed.nodes or parsed.text_chars < 50:
            status = "failed"
        _set_document(
            document_id,
            status=status,
            title=parsed.title,
            doc_type=parsed.doc_type,
            page_count=parsed.page_count,
            doc_version=int(prior_doc.get("doc_version") or 1) + (1 if had_nodes else 0),
            quality_score=quality["score"],
            quality_level=quality["level"],
            quality_issues=dump(quality["issues"]),
            stats=dump(stats),
            error=None,
        )
        repair_sessions_for_document(document_id)
        repair_questions_for_document(document_id)
        docrow = fetch_one("SELECT user_id, tenant_id, title FROM document WHERE doc_id=?", (document_id,)) or {}
        userrow = fetch_one("SELECT username FROM app_user WHERE user_id=?", (docrow.get("user_id", ""),)) or {}
        audit.log_event(
            user_id=docrow.get("user_id", ""), username=userrow.get("username", ""),
            tenant_id=docrow.get("tenant_id", ""),
            event_type="document_ready",
            detail={"document_id": document_id, "status": status, "quality": quality.get("score"),
                    "stats": stats, "title": docrow.get("title")},
        )
        update_task(
            task_id, status="succeeded", stage="done", progress=100,
            result={"document_id": document_id, "status": status, "quality": quality, "timings": timings},
        )
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        if isinstance(exc, ModuleNotFoundError):
            code = "DEPENDENCY_MISSING"
            message = f"服务器缺少解析依赖 {exc.name}，请安装 requirements.txt 后重试"
        else:
            code = "PARSE_FAILED"
            message = str(exc)
        _set_document(document_id, status="failed", error=dump({"code": code, "message": message, "stage": "parse"}))
        update_task(
            task_id, status="failed", stage="failed", progress=100,
            error={"code": code, "message": message, "retryable": True},
        )


def repair_sessions_for_document(document_id: str) -> int:
    """重新解析后，按章节标题/知识点名称把未完成会话的 plan 重新挂到新节点。

    D7/重新解析会重建 node_id、kp_id；这个修复让正在进行的会话可以继续，
    已出过的题目仍保留原题干与证据引文。
    """
    nodes = fetch_all("SELECT * FROM node WHERE doc_id=?", (document_id,))
    kps = fetch_all("SELECT * FROM knowledge_point WHERE doc_id=?", (document_id,))
    if not nodes:
        return 0

    def compact(s: str) -> str:
        return re.sub(r"\s+", "", s or "")

    node_by_title: dict[str, list[dict]] = {}
    for n in nodes:
        node_by_title.setdefault(compact(n.get("title") or ""), []).append(n)
    kp_by_node: dict[str, list[dict]] = {}
    for kp in kps:
        kp_by_node.setdefault(kp["node_id"], []).append(kp)
    for items in kp_by_node.values():
        items.sort(key=lambda x: -(x.get("importance") or 0))

    sessions = fetch_all("SELECT * FROM session WHERE doc_id=? AND state<>'finished'", (document_id,))
    repaired = 0
    for session in sessions:
        plan = load_json(session.get("plan"), {})
        changed = False
        for item in plan.get("items", []):
            title = item.get("section_title") or ""
            candidates = node_by_title.get(compact(title)) or []
            if not candidates:
                candidates = [
                    n for key, vals in node_by_title.items()
                    if compact(title) and (compact(title) in key or key in compact(title))
                    for n in vals
                ]
            node = candidates[0] if candidates else None
            if node:
                if item.get("node_id") != node["node_id"]:
                    item["node_id"] = node["node_id"]
                    changed = True
                item["section_title"] = node.get("title") or title
            pool = kp_by_node.get(item.get("node_id", ""), [])
            if pool:
                exact = next((k for k in pool if compact(k["name"]) == compact(item.get("kp_name", ""))), None)
                kp = exact or pool[0]
                if item.get("kp_id") != kp["kp_id"]:
                    item["kp_id"] = kp["kp_id"]
                    item["kp_name"] = kp["name"]
                    changed = True
        if changed:
            # 已生成题目的 node_id/kp_id 也同步到新结构，保证报告能聚合章节
            turns = fetch_all("SELECT * FROM turn WHERE session_id=?", (session["session_id"],))
            for turn in turns:
                item = next((i for i in plan.get("items", []) if i.get("seq_no") == turn.get("seq_no")), None)
                if not item:
                    continue
                q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],))
                if q:
                    with db() as conn:
                        conn.execute(
                            "UPDATE question SET node_id=?, kp_id=? WHERE q_id=?",
                            (item["node_id"], item["kp_id"], q["q_id"]),
                        )
            with db() as conn:
                conn.execute("UPDATE session SET plan=? WHERE session_id=?", (dump(plan), session["session_id"]))
            repaired += 1
    return repaired


def repair_questions_for_document(document_id: str) -> dict:
    """重新解析后把题库里的旧题重新挂到新结构节点。

    旧实现只修复会话 plan，题库中的题目仍指向已删除的 node_id，导致
    “证据支持率”统计失败、题目详情无法回原文。这里按证据引文反查节点：
    引文能在某个节点中完整找到即重挂；找不到则转为待复核，不静默保留假引用。
    """
    nodes = fetch_all("SELECT * FROM node WHERE doc_id=? ORDER BY order_index", (document_id,))
    kps = fetch_all("SELECT * FROM knowledge_point WHERE doc_id=? ORDER BY order_no", (document_id,))
    if not nodes:
        return {"rebound": 0, "pending": 0}
    kp_by_node: dict[str, list[dict]] = {}
    for kp in kps:
        kp_by_node.setdefault(kp["node_id"], []).append(kp)
    norm = lambda s: re.sub(r"\s+", "", s or "")
    node_text = {n["node_id"]: norm(n.get("text") or "") for n in nodes}
    rebound = pending = 0
    for q in fetch_all("SELECT * FROM question WHERE doc_id=? AND status IN ('active','pending_review')", (document_id,)):
        evs = load_json(q.get("evidence"), [])
        quotes = [norm(str(e.get("quote") or "")) for e in evs if str(e.get("quote") or "").strip()]
        if not quotes:
            with db() as conn:
                conn.execute("UPDATE question SET status='pending_review' WHERE q_id=?", (q["q_id"],))
            pending += 1
            continue
        candidates = [
            n for n in nodes
            if all(quote in node_text[n["node_id"]] for quote in quotes)
        ]
        if not candidates:
            # 旧题的引文可能横跨重新切分后的两个节点。用最长公共子串找
            # 最接近的节点，覆盖度 ≥55% 才接受，避免把题目随便挂到无关章节。
            from difflib import SequenceMatcher
            best_node, best_score = None, 0.0
            for n in nodes:
                text = node_text[n["node_id"]]
                if not text:
                    continue
                score = 0.0
                for quote in quotes:
                    match = SequenceMatcher(None, quote, text).find_longest_match(0, len(quote), 0, len(text))
                    score += match.size / max(1, len(quote))
                score /= max(1, len(quotes))
                if score > best_score:
                    best_node, best_score = n, score
            if best_node is not None and best_score >= 0.55:
                pool = kp_by_node.get(best_node["node_id"], [])
                kp = pool[0] if pool else None
                with db() as conn:
                    conn.execute(
                        "UPDATE question SET node_id=?, kp_id=?, status='pending_review' WHERE q_id=?",
                        (best_node["node_id"], kp["kp_id"] if kp else q.get("kp_id"), q["q_id"]),
                    )
                pending += 1
                continue
        if not candidates:
            with db() as conn:
                conn.execute("UPDATE question SET status='pending_review' WHERE q_id=?", (q["q_id"],))
            pending += 1
            continue
        # 优先章节节点，其次页码范围能覆盖证据页码的节点
        def score(n):
            ev_pages = [int(e.get("page") or 0) for e in evs if e.get("page")]
            page_ok = all(n["page_start"] <= p <= n["page_end"] for p in ev_pages) if ev_pages else True
            return (0 if n["node_type"] == "section" else 1, 0 if page_ok else 1, n["order_index"])
        node = sorted(candidates, key=score)[0]
        pool = kp_by_node.get(node["node_id"], [])
        kp = next((k for k in pool if k["kp_id"] == q.get("kp_id")), None) or (pool[0] if pool else None)
        if q.get("node_id") != node["node_id"] or (kp and q.get("kp_id") != kp["kp_id"]):
            with db() as conn:
                conn.execute(
                    "UPDATE question SET node_id=?, kp_id=? WHERE q_id=?",
                    (node["node_id"], kp["kp_id"] if kp else q.get("kp_id"), q["q_id"]),
                )
            rebound += 1
    if rebound or pending:
        audit.log_event(
            event_type="question_rebound", tenant_id="",
            detail={"document_id": document_id, "rebound": rebound, "pending_review": pending},
        )
    return {"rebound": rebound, "pending": pending}


def _flatten(nodes: list[parser_mod.NodeDraft]):
    for node in nodes:
        yield node


def _set_document(document_id: str, **fields) -> None:
    fields["updated_at"] = iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    with db() as conn:
        conn.execute(f"UPDATE document SET {sets} WHERE doc_id=?", tuple(fields.values()) + (document_id,))


def create_document(
    *,
    tenant_id: str,
    user_id: str,
    file_name: str,
    mime_type: str,
    content: bytes,
    doc_type: str = "auto",
    language: str = "zh-CN",
    title: str | None = None,
    source_uri: str = "",
) -> tuple[dict, bool]:
    suffix = Path(file_name).suffix.lower()
    if suffix not in (".pdf", ".docx", ".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        raise ValueError("DOC_UNSUPPORTED")
    digest = hashlib.sha256(content).hexdigest()
    with db() as conn:
        # 去重必须限定到当前用户：同一租户下其他用户的同名/同哈希文档
        # 不应该阻止本次上传，否则会把无权访问的 doc_id 返回给前端。
        existing = conn.execute(
            "SELECT * FROM document WHERE tenant_id=? AND user_id=? AND sha256=?"
            " AND status IN ('ready','needs_review') AND deleted_at IS NULL"
            " ORDER BY created_at DESC LIMIT 1",
            (tenant_id, user_id, digest),
        ).fetchone()
    if existing:
        return dict(existing), True

    doc_id = new_id("doc")
    key = f"{doc_id}{suffix}"
    storage.put(key, content, mime_type)
    path = UPLOAD_DIR / key
    with db() as conn:
        conn.execute(
            "INSERT INTO document (doc_id, tenant_id, user_id, title, doc_type, source_uri, file_name, mime_type,"
            " size_bytes, page_count, sha256, status, doc_version, language, quality_score, quality_level,"
            " quality_issues, stats, error, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc_id, tenant_id, user_id, title or Path(file_name).stem, doc_type, source_uri, file_name, mime_type,
                len(content), 0, digest, "uploaded", 1, language, 0, "review", "[]", "{}", None, iso(), iso(),
            ),
        )
    return {"doc_id": doc_id, "file_path": str(path)}, False


def get_document_path(document_id: str) -> Path | None:
    with db() as conn:
        row = conn.execute("SELECT file_name FROM document WHERE doc_id=?", (document_id,)).fetchone()
    if not row:
        return None
    for p in UPLOAD_DIR.glob(f"{document_id}.*"):
        return p
    for suffix in (".pdf", ".docx", ".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        key = f"{document_id}{suffix}"
        try:
            return storage.get(key)
        except FileNotFoundError:
            continue
    return None
