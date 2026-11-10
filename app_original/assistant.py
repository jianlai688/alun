"""A论 AI 问答助手：文档 RAG、会话上下文、平台数据与权威资料检索。"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from . import audit, llm, settings
from .db import fetch_all, fetch_one

SYSTEM_PROMPT = """你是「A论 AI 问答助手」，服务于论文导读智能体 A论。

你的回答必须遵守：
1. 优先使用【文档上下文】中的真实内容回答；文档中的事实要标注原文页码，例如 [p.6]。
2. 文档没有的信息才参考【权威资料】，并用 [S1]、[S2] 标注来源；不得把外部资料伪装成论文原文。
3. 【平台数据】是后台真实统计，回答用户/用量/准确率/幻觉率问题时必须以此为准，不得编造数字。
4. 用户可以上传截图或图片；先描述你看到的图中文字、表格或界面，再结合上下文回答。看不清就明确说看不清。
5. 找不到依据时直接说“现有资料不足以回答”，并说明还缺什么；不要编造引文、页码或链接。
6. 用简洁的中文回答，先给结论，再给依据。公式、术语和数字保持原文含义。
"""


def _terms(query: str) -> set[str]:
    terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9._-]{1,}", query or ""))
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", query or ""):
        for i in range(len(seg) - 1):
            terms.add(seg[i : i + 2])
    stop = {"什么", "怎么", "如何", "这个", "那个", "一下", "可以", "是否", "以及", "相关", "全文", "文章", "论文"}
    return {t.lower() for t in terms if t not in stop}


def _page_of(node_id: str, nodes: dict[str, dict]) -> int:
    node = nodes.get(node_id) or {}
    return int(node.get("page_start") or 0)


def _document_context(document_id: str) -> tuple[str, list[dict], str]:
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (document_id,))
    if not doc:
        return "", [], ""
    nodes = {r["node_id"]: r for r in fetch_all("SELECT * FROM node WHERE doc_id=?", (document_id,))}
    kps = fetch_all("SELECT * FROM knowledge_point WHERE doc_id=? ORDER BY importance DESC", (document_id,))
    chunks = fetch_all("SELECT * FROM chunk WHERE doc_id=? ORDER BY order_no", (document_id,))
    query = ""  # 由调用方通过 question 重排；先保留结构，排序在 answer 里做
    lines = [
        f"【文档】《{doc.get('title') or ''}》 · {doc.get('page_count') or 0} 页 · {doc.get('doc_type') or ''}",
        f"【摘要】{(doc.get('abstract') or '')[:900]}",
        "【结构】",
    ]
    for n in list(nodes.values())[:30]:
        if n.get("node_type") == "section":
            lines.append(f"- {n.get('title')}（p.{n.get('page_start')}）")
    if kps:
        lines.append("【知识点】")
        for kp in kps[:14]:
            lines.append(f"- {kp.get('name')}：{(kp.get('definition') or '')[:220]}（p.{_page_of(kp.get('node_id'), nodes)}）")
    selected: list[dict] = []
    if chunks:
        lines.append("【原文片段】")
        for chunk in chunks[:8]:
            text = re.sub(r"\s+", " ", chunk.get("text") or "").strip()
            if not text:
                continue
            page = _page_of(chunk.get("node_id"), nodes)
            lines.append(f"[D{len(selected) + 1} | p.{page}] {text[:1100]}")
            selected.append({"type": "document", "page": page, "quote": text[:220],
                             "title": (nodes.get(chunk.get("node_id")) or {}).get("title") or doc.get("title")})
    return "\n".join(lines), selected, doc.get("title") or ""


def _rank_chunks(document_id: str, question: str) -> list[dict]:
    nodes = {r["node_id"]: r for r in fetch_all("SELECT * FROM node WHERE doc_id=?", (document_id,))}
    chunks = fetch_all("SELECT * FROM chunk WHERE doc_id=? ORDER BY order_no", (document_id,))
    terms = _terms(question)
    scored = []
    for chunk in chunks:
        text = chunk.get("text") or ""
        score = sum(1 for term in terms if term in text.lower())
        title = (nodes.get(chunk.get("node_id")) or {}).get("title") or ""
        score += sum(2 for term in terms if term in title.lower())
        scored.append((score, chunk))
    scored.sort(key=lambda x: (-x[0], x[1].get("order_no") or 0))
    picked = [c for score, c in scored if score > 0][:6]
    if len(picked) < 4:
        picked += [c for _, c in scored if c not in picked][: 4 - len(picked)]
    out = []
    for chunk in picked:
        text = re.sub(r"\s+", " ", chunk.get("text") or "").strip()
        if not text:
            continue
        page = _page_of(chunk.get("node_id"), nodes)
        out.append({"text": text, "page": page,
                    "title": (nodes.get(chunk.get("node_id")) or {}).get("title") or ""})
    return out


def _document_question_context(document_id: str, question: str) -> tuple[str, list[dict], str]:
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (document_id,))
    if not doc:
        return "", [], ""
    base, citations, title = _document_context(document_id)
    ranked = _rank_chunks(document_id, question)
    if ranked:
        # 把最相关的片段放在摘要/结构之后，避免全文 token 过大
        extra = ["【与问题最相关的原文】"]
        for i, chunk in enumerate(ranked, 1):
            extra.append(f"[R{i} | p.{chunk['page']}] {chunk['text'][:1200]}")
            citations.append({"type": "document", "page": chunk["page"], "quote": chunk["text"][:220],
                               "title": chunk["title"]})
        base += "\n" + "\n".join(extra)
    return base, citations, title


def _session_context(session_id: str) -> str:
    s = fetch_one("SELECT * FROM session WHERE session_id=?", (session_id,))
    if not s:
        return ""
    report = fetch_one("SELECT * FROM report WHERE session_id=?", (session_id,))
    turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no DESC LIMIT 5", (session_id,))
    lines = [f"【当前会话】{s.get('mode')} · {s.get('answered_count') or 0}/{s.get('total_questions') or 0} 题 · 能力 {round(float(s.get('theta') or 500), 1)}"]
    if report:
        rj = report.get("summary")
        lines.append(f"【本轮报告】{rj if rj else ''}")
    for t in turns:
        lines.append(f"- 第{t.get('seq_no')}题 {t.get('q_type') or ''}：{t.get('stem') or ''}；得分 {t.get('score') if t.get('score') is not None else '未答'}")
    return "\n".join(lines)


def _admin_context() -> str:
    users = fetch_one("SELECT COUNT(*) AS c FROM app_user WHERE status='active'")["c"]
    docs = fetch_one("SELECT COUNT(*) AS c FROM document WHERE deleted_at IS NULL")["c"]
    sessions = fetch_one("SELECT COUNT(*) AS c FROM session s JOIN document d ON d.doc_id=s.doc_id WHERE d.deleted_at IS NULL")["c"]
    ans = fetch_one(
        "SELECT COUNT(*) AS c, COALESCE(AVG(score),0) AS s, SUM(CASE WHEN score>=80 THEN 1 ELSE 0 END) AS good FROM turn WHERE status='answered'"
    ) or {}
    hall = fetch_one("SELECT COUNT(*) AS total, SUM(CASE WHEN grounded=0 THEN 1 ELSE 0 END) AS bad FROM ai_audit WHERE fallback=0") or {}
    hall_total = int(hall.get("total") or 0)
    hall_bad = int(hall.get("bad") or 0)
    hall_rate = round(hall_bad / hall_total, 4) if hall_total else 0.0
    today = time.strftime("%Y-%m-%d")
    active_today = fetch_one("SELECT COUNT(DISTINCT user_id) AS c FROM activity_event WHERE user_id<>'' AND substr(ts,1,10)=?", (today,))["c"]
    today_answers = fetch_one("SELECT COUNT(*) AS c FROM turn WHERE status='answered' AND substr(answered_at,1,10)=?", (today,))["c"]
    tok = fetch_one("SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_log")["t"]
    urows = fetch_all("SELECT user_id, username, display_name, role, last_login_at FROM app_user ORDER BY role DESC, created_at")
    lines = [
        "【平台数据】",
        f"- 活跃账号 {users}；今日活跃 {active_today}；文档 {docs}；会话 {sessions}；已判分作答 {ans.get('c') or 0} 次；今日作答 {today_answers} 次；平均分 {round(float(ans.get('s') or 0), 1)}",
        f"- 模型幻觉率：{round(hall_rate * 100, 1)}%（{hall_bad}/{hall_total} 次最终模型输出未通过证据/结构校验；模板兜底不计入分母）",
        f"- token 总量 {tok}；当前导读模型 {settings.get('llm.model')}；模型开关 {'开启' if settings.get_bool('llm.enabled') else '关闭'}",
        "【用户概览】",
    ]
    for u in urows[:10]:
        uid = u.get("user_id") or ""
        ua = fetch_one(
            "SELECT COUNT(*) AS c, COALESCE(AVG(t.score),0) AS s FROM turn t JOIN session s ON s.session_id=t.session_id"
            " WHERE s.user_id=? AND t.status='answered'", (uid,)
        ) or {}
        ud = fetch_one("SELECT COUNT(*) AS c FROM document WHERE user_id=? AND deleted_at IS NULL", (uid,))["c"]
        us = fetch_one("SELECT COUNT(*) AS c FROM session WHERE user_id=?", (uid,))["c"]
        lines.append(
            f"- {u.get('display_name') or u.get('username')}（{u.get('role')}）文档 {ud}、会话 {us}、"
            f"作答 {ua.get('c') or 0}、平均分 {round(float(ua.get('s') or 0), 1)}"
        )
    events = audit.recent_events(limit=8)
    if events:
        lines.append("【最近事件】")
        for e in events:
            lines.append(f"- {e.get('event_type')} · {e.get('username') or e.get('user_id') or ''} · {e.get('ts')}")
    return "\n".join(lines)


def _web_search(question: str, doc_title: str = "") -> tuple[list[dict], str]:
    """检索 OpenAlex / arXiv / PubMed 等公开学术来源，失败时静默降级。"""
    sources: list[dict] = []
    query = (doc_title + " " + question).strip() if doc_title else question.strip()
    if not query:
        return sources, ""
    latin_terms = list(dict.fromkeys(
        t for t in re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", question + " " + doc_title)
        if t.lower() not in {"the", "and", "for", "with", "from", "this", "that", "what", "how"}
    ))
    search_text = " ".join(latin_terms) if latin_terms else question

    # OpenAlex：开放学术元数据，适合中文/英文主题检索
    try:
        r = httpx.get(
            "https://api.openalex.org/works",
            params={"search": search_text, "per-page": 3},
            timeout=10,
            headers={"User-Agent": "A-Lun local assistant (mailto:local@example.com)"},
        )
        for item in ((r.json() or {}).get("results") or [])[:3]:
            title = item.get("display_name") or item.get("title") or ""
            if not title:
                continue
            abstract = ""
            inv = item.get("abstract_inverted_index") or {}
            if isinstance(inv, dict) and inv:
                positions = []
                for word, idxs in inv.items():
                    for idx in idxs or []:
                        positions.append((idx, word))
                abstract = " ".join(w for _, w in sorted(positions)[:70])
            url = item.get("doi") or ((item.get("primary_location") or {}).get("landing_page_url") or "")
            if url and str(url).startswith("10."):
                url = "https://doi.org/" + str(url)
            sources.append({"title": title, "url": url, "source": "OpenAlex",
                            "snippet": (abstract or f"{item.get('publication_year') or ''} · OpenAlex")[:500]})
    except Exception:
        pass

    # arXiv：技术类问题
    if re.search(r"模型|算法|网络|学习|语言|图像|检索|生成|Transformer|CLIP|Mamba|LLM|AI", question + " " + doc_title, re.I):
        try:
            queries = []
            if re.search(r"Transformer", question + " " + doc_title, re.I):
                queries.append('all:"Attention Is All You Need"')
            queries.append("all:" + (latin_terms[0] if latin_terms else question))
            for arxiv_q in dict.fromkeys(queries):
                r = httpx.get("https://export.arxiv.org/api/query",
                              params={"search_query": arxiv_q, "start": 0, "max_results": 3},
                              timeout=10, headers={"User-Agent": "A-Lun/1.0"})
                root = ET.fromstring(r.text)
                ns = {"a": "http://www.w3.org/2005/Atom"}
                for entry in root.findall("a:entry", ns)[:3]:
                    title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
                    summary = re.sub(r"\s+", " ", entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
                    url = entry.findtext("a:id", default="", namespaces=ns) or ""
                    if title:
                        sources.append({"title": title, "url": url, "source": "arXiv", "snippet": summary[:500]})
                if sources:
                    break
        except Exception:
            pass

    # Crossref 兜底：英文主题通常能返回原始论文元数据
    if len(sources) < 2 and latin_terms:
        try:
            r = httpx.get(
                "https://api.crossref.org/works",
                params={"query.bibliographic": search_text, "rows": 2,
                        "select": "title,DOI,URL,issued,container-title,abstract"},
                timeout=8,
            )
            for item in ((r.json().get("message") or {}).get("items") or [])[:2]:
                title = (item.get("title") or [""])[0]
                if not title:
                    continue
                snippet = re.sub(r"<[^>]+>", " ", item.get("abstract") or "").strip()
                sources.append({"title": title, "url": item.get("URL") or "",
                                "source": "Crossref", "snippet": snippet[:500]})
        except Exception:
            pass

    # PubMed：医学/临床问题
    if re.search(r"医学|临床|肿瘤|乳腺|超声|患者|诊断", question + " " + doc_title):
        try:
            r = httpx.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={"db": "pubmed", "term": search_text, "retmode": "json", "retmax": 2}, timeout=8,
            )
            ids = ((r.json().get("esearchresult") or {}).get("idlist") or [])[:2]
            if ids:
                d = httpx.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                    params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"}, timeout=8,
                )
                result = (d.json().get("result") or {})
                for uid in ids:
                    item = result.get(uid) or {}
                    if item.get("title"):
                        sources.append({"title": item["title"], "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                                        "source": "PubMed", "snippet": (item.get("fulljournalname") or "")[:500]})
        except Exception:
            pass

    seen = set()
    unique = []
    for s in sources:
        key = (s.get("title") or "")[:80]
        if key and key not in seen:
            seen.add(key)
            unique.append(s)
    return unique[:5], "\n".join(
        f"[S{i}] {s['source']} · {s['title']}：{s['snippet']}（{s['url']}）"
        for i, s in enumerate(unique[:5], 1)
    )


def _fallback_answer(citations: list[dict], sources: list[dict]) -> str:
    parts = ["模型网关暂时不可用，以下是已检索到的可核验资料，请先据此查看："]
    for i, c in enumerate(citations[:5], 1):
        parts.append(f"[p.{c.get('page')}] {c.get('quote')}")
    for i, s in enumerate(sources[:3], 1):
        parts.append(f"[S{i}] {s.get('title')}：{s.get('snippet')}")
    return "\n".join(parts)


def answer(
    *,
    question: str,
    images: list[str] | None = None,
    document_id: str = "",
    session_id: str = "",
    user_id: str = "",
    tenant_id: str = "",
    username: str = "",
    role: str = "user",
    history: list[dict] | None = None,
    use_web: bool = True,
) -> dict:
    started = time.time()
    images = images or []
    history = history or []
    doc_ctx, citations, doc_title = _document_question_context(document_id, question) if document_id else ("", [], "")
    session_ctx = _session_context(session_id) if session_id else ""
    admin_ctx = _admin_context() if role == "admin" and not document_id else ""
    sources: list[dict] = []
    web_ctx = ""
    should_search = bool(use_web) and (
        (not doc_ctx and role != "admin")
        or bool(re.search(r"权威|最新|外部|研究|论文|文献|指南|标准|定义|区别|为什么|是什么|背景|查一下|网站", question))
    )
    if should_search:
        sources, web_ctx = _web_search(question, doc_title)

    history_text = "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}：{str(m.get('content') or '')[:600]}"
        for m in history[-6:]
        if m.get("role") in ("user", "assistant")
    )
    prompt_parts = [
        "【文档上下文】\n" + (doc_ctx or "（本轮没有指定文档）"),
        "【会话上下文】\n" + (session_ctx or "（无）"),
        "【平台数据】\n" + (admin_ctx or "（无）"),
        "【权威资料】\n" + (web_ctx or "（未检索到外部资料）"),
        "【历史对话】\n" + (history_text or "（无）"),
        f"【用户问题】{question}",
        "请基于以上材料回答。引用文档时写页码，引用外部资料时写 [S1] 等编号；如果没有依据请直说。",
    ]
    text_prompt = "\n\n".join(prompt_parts)
    content: Any = text_prompt
    if images:
        content = [{"type": "text", "text": text_prompt}]
        for url in images:
            content.append({"type": "image_url", "image_url": {"url": url}})

    answer_text = ""
    model_name = settings.get("llm.model", "")
    try:
        answer_text = llm.chat(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
            kind="assistant_chat", json_mode=False, max_tokens=1700, temperature=0.25,
            document_id=document_id or None, session_id=session_id or None,
        ).strip()
    except Exception:
        answer_text = _fallback_answer(citations, sources)

    result = {
        "answer": answer_text,
        "citations": citations[:10],
        "sources": sources[:5],
        "used_web": should_search,
        "model": model_name,
        "latency_ms": int((time.time() - started) * 1000),
    }
    audit.log_event(
        user_id=user_id, username=username, tenant_id=tenant_id, event_type="assistant_ask",
        detail={"document_id": document_id, "session_id": session_id, "has_image": bool(images),
                "question": question[:120], "sources": len(sources), "used_web": should_search},
    )
    return result
