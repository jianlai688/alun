"""语义分块、知识点抽取与证据单元构建（设计文档 5.4—5.6）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .db import new_id
from .parser import NodeDraft, CJK

SENTENCE_RE = re.compile(r"[^。！？；!?;]+[。！？；!?;]?")

TYPE_RULES: list[tuple[str, str, float]] = [
    ("limitation", r"(局限|不足|挑战|有待|尚未|瓶颈|未来方向|仍需|难以)", 0.70),
    ("conclusion", r"(表明|说明|因此|结论|发现|可见|证明|结果显示|总体来看|由此可见)", 0.82),
    ("data", r"(\d+(\.\d+)?%|F1|准确率|召回率|精确率|数据集|样本|参数|提升了|下降了|达到|实验结果)", 0.80),
    ("method", r"(方法|模型|算法|框架|流程|机制|步骤|策略|训练|检索|生成|微调|架构)", 0.76),
    ("concept", r"(是指|指的是|定义为|称为|所谓|简称|概念|术语|是一种|是一个|即)", 0.80),
    ("claim", r"(认为|主张|应当|需要|建议|政策|取向|逻辑|观点|强调|指出)", 0.66),
]

TYPE_DIFFICULTY = {
    "concept": 2,
    "method": 3,
    "data": 3,
    "conclusion": 3,
    "claim": 4,
    "limitation": 4,
}

ROLE_BOOST = {
    "方法": 0.08,
    "实验": 0.08,
    "结果": 0.08,
    "结论": 0.06,
    "讨论": 0.05,
    "引言": 0.02,
    "综述": 0.04,
}


@dataclass
class Sentence:
    text: str
    start: int
    end: int
    page: int


def split_sentences(text: str, spans: list[tuple[int, int, int]]) -> list[Sentence]:
    out: list[Sentence] = []
    for m in SENTENCE_RE.finditer(text or ""):
        raw = m.group(0).strip()
        if len(raw) < 12:
            continue
        start, end = m.start(), m.end()
        # 去掉前导空白偏移
        lead = len(m.group(0)) - len(m.group(0).lstrip())
        start += lead
        page = 1
        for s, e, p in spans:
            if s <= start < e or s < end <= e:
                page = p
                break
        out.append(Sentence(raw, start, end, page))
    return out


def _clean_name(text: str, limit: int = 22) -> str:
    text = re.sub(r"^[，,、；;：:\s]+", "", text)
    # 去掉列表编号（i) / iii） / 1) 等）与常见引导词
    text = re.sub(r"^[（(]?(?:i{1,3}|iv|v|vi{0,3}|[0-9]{1,2})[)）.、]\s*", "", text, flags=re.I)
    text = re.sub(r"^(所谓|其中|此外|同时|因此|首先|其次|最后|文章|本文|本研究|该研究|作者)\s*", "", text)
    text = re.split(r"[，,、；;：:]", text)[0]
    text = text.strip("“”‘’《》()（）[]【】 ")
    # 未闭合的括号片段直接截断
    if "（" in text and "）" not in text:
        text = text.split("（")[0]
    if "(" in text and ")" not in text:
        text = text.split("(")[0]
    if len(text) > limit:
        text = text[:limit]
    return text.strip()


def polish_name(name: str, kp_type: str, sentence: str, section_title: str) -> str:
    """把过长的句段名压成可展示的知识点标题。"""
    bad = (
        len(name) > 16
        or bool(re.search(r"(则|若|当|进行|采用|具有|通过|以及|对于|较|在|是|的$)", name))
        or name.endswith(("的", "和", "与", "或", "，"))
    )
    if not bad:
        # 短语型“有效提升/极大提升”等动词短语不适合作为知识点名
        if kp_type == "data" and (len(name) < 6 or re.search(r"^(有效|极大|显著|明显|大幅|进一步|不断|持续)", name)):
            return "关键实验结果"
        if kp_type == "conclusion" and (len(name) < 6 or name == section_title):
            section = re.sub(r"^\d+(\.\d+)*\s*", "", section_title or "").strip()
            return (section[:10] + "的结论") if section else "核心结论"
        return name
    section = re.sub(r"^\d+(\.\d+)*\s*", "", section_title or "").strip()
    if kp_type == "data":
        if re.search(r"(超参数|学习率|批量|训练轮数|dropout|温度|KL)", sentence, re.I):
            return "训练超参数"
        if re.search(r"(数据集|样本|标注)", sentence):
            return "数据集与样本"
        if re.search(r"(F1|精确率|召回率|准确率|时延|提升|下降|百分点|%)", sentence):
            return "关键实验结果"
        return (section[:10] + "的数据") if section else "关键数据"
    suffix = {
        "method": "的方法",
        "conclusion": "的结论",
        "claim": "的观点",
        "limitation": "的局限",
        "concept": "的概念",
    }.get(kp_type, "的要点")
    base = section[:10] if section else "本节"
    return base + suffix


def extract_name(sentence: str, kp_type: str, section_title: str) -> str:
    t = sentence.strip()
    if kp_type == "concept":
        m = re.search(r"^(.{2,30}?)(是指|指的是|定义为|称为|所谓|简称)", t)
        if m:
            return _clean_name(m.group(1))
        m = re.search(r"^(.{2,24}?)(是一种|是一个|即)", t)
        if m:
            return _clean_name(m.group(1))
    if kp_type == "method":
        m = re.search(r"^(.{2,30}?)(方法|模型|算法|框架|机制|策略|流程)", t)
        if m:
            name = _clean_name(m.group(1) + m.group(2))
            if len(name) >= 4:
                return name
    if kp_type == "data":
        m = re.search(r"([\u4e00-\u9fffA-Za-z\-]{2,16}(?:实验|结果|表现|指标|对比|提升))", t)
        if m:
            return _clean_name(m.group(1))
    if kp_type == "conclusion":
        m = re.search(r"(?:表明|说明|发现|结论是|可见)[，,：:]?\s*(.{4,26})", t)
        if m:
            return _clean_name(m.group(1))
    if kp_type == "limitation":
        m = re.search(r"(.{2,24}?)(?:仍是|仍然|尚|有待|存在|面临|受制于)", t)
        if m:
            return _clean_name(m.group(1)) + "的局限"
    # 兜底：取第一个分句，过短或含公式/编号则用章节名
    name = _clean_name(t, 18)
    if len(name) < 4 or re.search(r"[=%]", name) or re.match(r"^\d", name):
        name = section_title[:18] or "核心知识点"
    return name


def classify(sentence: str) -> tuple[str, float]:
    for kp_type, pattern, score in TYPE_RULES:
        if re.search(pattern, sentence):
            return kp_type, score
    return "claim", 0.55


def _importance(sentence: str, kp_type: str, section_title: str, section_level: int) -> float:
    base = next((s for t, _, s in TYPE_RULES if t == kp_type), 0.55)
    boost = next((v for k, v in ROLE_BOOST.items() if k in section_title), 0.0)
    length_bonus = 0.04 if 30 <= len(sentence) <= 120 else 0.0
    level_bonus = 0.04 if section_level <= 2 else 0.0
    return round(min(0.97, max(0.35, base + boost + length_bonus + level_bonus)), 2)


def _prerequisites(kps: list[dict], current: dict) -> list[str]:
    """同一节内先出现的概念/方法作为前置知识。"""
    out = []
    for kp in kps:
        if kp is current:
            continue
        if kp["kp_type"] in ("concept", "method") and kp["importance"] >= 0.75:
            out.append(kp["kp_id"])
        if len(out) >= 2:
            break
    return out


def build_chunks(doc_id: str, nodes: list[NodeDraft], min_len: int = 300, max_len: int = 650) -> list[dict]:
    chunks: list[dict] = []
    for node in nodes:
        if node.node_type not in ("section", "abstract", "table", "chart") or len(node.text) < 30:
            continue
        text = node.text
        # 以句子边界切块，保留 10% 重叠
        sentences = [m.group(0) for m in SENTENCE_RE.finditer(text)]
        buf = ""
        order = 0
        for sent in sentences:
            if not buf:
                buf = sent
                continue
            if len(buf) + len(sent) <= max_len:
                buf += sent
            else:
                if len(buf) >= min_len:
                    order += 1
                    chunks.append({
                        "chunk_id": new_id("chk"),
                        "doc_id": doc_id,
                        "node_id": node.node_id,
                        "text": buf.strip(),
                        "token_count": max(1, len(buf) // 2),
                        "order_no": order,
                    })
                    overlap = buf[-max(20, len(buf) // 10) :]
                    buf = overlap + sent
                else:
                    buf += sent
        if len(buf.strip()) >= 40:
            order += 1
            chunks.append({
                "chunk_id": new_id("chk"),
                "doc_id": doc_id,
                "node_id": node.node_id,
                "text": buf.strip(),
                "token_count": max(1, len(buf) // 2),
                "order_no": order,
            })
    return chunks


def extract_knowledge(doc_id: str, nodes: list[NodeDraft]) -> tuple[list[dict], list[dict]]:
    """从结构节点中抽取知识点与证据单元。"""
    kps: list[dict] = []
    evidences: list[dict] = []
    by_id = {n.node_id: n for n in nodes}

    def path_of(node: NodeDraft) -> list[str]:
        parts = []
        cur = node
        while cur is not None:
            if cur.title:
                parts.append(cur.title)
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(parts))

    for node in nodes:
        if node.node_type not in ("section", "abstract"):
            continue
        if len(node.text) < 60:
            continue
        if any(k in node.title for k in ("参考文献", "作者简介", "致谢")):
            continue
        sentences = split_sentences(node.text, node.page_spans)
        candidates: list[tuple[float, Sentence, str]] = []
        for sent in sentences:
            if re.search(r"(图|表)\s*\d+|\[[0-9,，\-–]+\]", sent.text):
                continue
            if len(sent.text) > 260:
                continue
            kp_type, _ = classify(sent.text)
            score = _importance(sent.text, kp_type, node.title, node.level)
            if re.search(r"(本文|文章|研究)(提出|设计|构建|采用|使用)", sent.text):
                score = min(0.97, score + 0.06)
            candidates.append((score, sent, kp_type))
        if not candidates:
            continue
        candidates.sort(key=lambda x: (-x[0], x[1].start))
        picked: list[tuple[float, Sentence, str]] = []
        seen_types: dict[str, int] = {}
        for score, sent, kp_type in candidates:
            if seen_types.get(kp_type, 0) >= 2:
                continue
            if any(_similar(sent.text, p[1].text) > 0.75 for p in picked):
                continue
            picked.append((score, sent, kp_type))
            seen_types[kp_type] = seen_types.get(kp_type, 0) + 1
            if len(picked) >= 3:
                break
        for score, sent, kp_type in picked:
            ev_id = new_id("ev")
            name = extract_name(sent.text, kp_type, node.title)
            name = polish_name(name, kp_type, sent.text, node.title)
            kp = {
                "kp_id": new_id("kp"),
                "doc_id": doc_id,
                "node_id": node.node_id,
                "name": name,
                "kp_type": kp_type,
                "definition": sent.text[:220],
                "importance": score,
                "difficulty": TYPE_DIFFICULTY.get(kp_type, 3),
                "evidence_ids": [ev_id],
                "prerequisites": [],
                "order_no": len(kps) + 1,
                "_section_path": path_of(node),
            }
            ev = {
                "evidence_id": ev_id,
                "doc_id": doc_id,
                "node_id": node.node_id,
                "page_no": sent.page,
                "quote": sent.text[:300],
                "char_start": sent.start,
                "char_end": min(len(node.text), sent.end),
                "strength": "strong" if kp_type in ("concept", "data", "conclusion") else "weak",
                "context": node.text[max(0, sent.start - 40) : min(len(node.text), sent.end + 40)],
            }
            kps.append(kp)
            evidences.append(ev)
    # 前置知识
    for kp in kps:
        section_kps = [k for k in kps if k["node_id"] == kp["node_id"]]
        kp["prerequisites"] = _prerequisites([k for k in section_kps if k["order_no"] < kp["order_no"]], kp)
    return kps, evidences


def _similar(a: str, b: str) -> float:
    import difflib

    return difflib.SequenceMatcher(None, a, b).ratio()
