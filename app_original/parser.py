"""文档解析：PDF/DOCX/Markdown → 结构树 + 质量报告。

实现遵循设计文档第 5 章：
- 文本层优先，乱码/低覆盖触发质量门（首期不内置 OCR，标记 needs_review）
- 双栏阅读顺序还原（全宽元素分段，段内先左栏后右栏）
- 标题编号 + 字号字重 + 标签词识别
- 图表题注识别并挂到所属章节
- 结构树节点稳定 ID、页码、字符数、置信度
"""
from __future__ import annotations

import base64
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pymupdf

from .db import new_id

CJK = r"[\u4e00-\u9fff]"
NUM = r"0-9０-９"


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


@dataclass
class Line:
    text: str
    page: int
    size: float
    bold: bool
    x0: float
    y0: float
    x1: float
    y1: float
    width: float = 0.0
    role: str = "body"


@dataclass
class Heading:
    line: Line
    level: int
    title: str
    special: str | None = None


@dataclass
class NodeDraft:
    node_id: str
    parent_id: str | None
    level: int
    node_type: str
    order_no: int
    title: str
    text: str = ""
    page_start: int = 1
    page_end: int = 1
    confidence: float = 1.0
    path: str = "/"
    children: list["NodeDraft"] = field(default_factory=list)
    page_spans: list[tuple[int, int, int]] = field(default_factory=list)

    def to_row(self, doc_id: str, order_index: int) -> dict:
        return {
            "node_id": self.node_id,
            "doc_id": doc_id,
            "parent_id": self.parent_id,
            "level": self.level,
            "node_type": self.node_type,
            "order_no": self.order_no,
            "title": self.title,
            "text": self.text,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "bbox": None,
            "confidence": self.confidence,
            "path": self.path,
            "char_count": len(self.text or ""),
            "order_index": order_index,
        }


@dataclass
class ParsedDoc:
    title: str
    doc_type: str
    page_count: int
    authors: str
    abstract: str
    keywords: list[str]
    nodes: list[NodeDraft]
    stats: dict
    quality: dict
    text_chars: int
    page_texts: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
def _line_from_dict(raw: dict, page_no: int, page_width: float) -> Line | None:
    spans = [s for s in raw.get("spans", []) if s.get("text", "").strip()]
    if not spans:
        return None
    text = norm("".join(s.get("text", "") for s in spans))
    if not text:
        return None
    size = max(float(s.get("size", 0)) for s in spans)
    # PyMuPDF flags: bit 4 (16) 为粗体
    bold = any(int(s.get("flags", 0)) & 16 for s in spans)
    x0, y0, x1, y1 = raw.get("bbox", (0, 0, 0, 0))
    if x1 - x0 <= 0.5:
        return None
    return Line(text, page_no, size, bold, x0, y0, x1, y1, x1 - x0)


def _page_lines(page: pymupdf.Page, page_no: int) -> list[Line]:
    width = float(page.rect.width)
    height = float(page.rect.height)
    lines: list[Line] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for raw in block.get("lines", []):
            line = _line_from_dict(raw, page_no, width)
            if not line:
                continue
            if line.y1 < height * 0.035 or line.y0 > height * 0.965:
                continue
            # 纯数字行不再一律丢弃：不少期刊把章节号单独排成一行（如“1”下一行才是标题）。
            # 只删除确实位于页眉/页脚区域的纯数字，正文中的章节号保留给标题合并步骤。
            if re.fullmatch(r"[-—–\s]*\d{1,4}[-—–\s]*", line.text):
                if line.y1 < height * 0.06 or line.y0 > height * 0.94:
                    continue
            lines.append(line)
    return lines


def _read_pdf(path: Path) -> tuple[list[list[Line]], list[str], int]:
    doc = pymupdf.open(path)
    pages: list[list[Line]] = []
    texts: list[str] = []
    for idx, page in enumerate(doc):
        pages.append(_page_lines(page, idx + 1))
        texts.append(page.get_text("text"))
    count = doc.page_count
    doc.close()

    # 页眉页脚：仅删除位于页面上下边缘、且跨页重复的行（避免误删题名）
    freq: dict[str, int] = {}
    for lines in pages:
        seen = set()
        for ln in lines:
            key = re.sub(r"\d+", "#", ln.text)
            if key not in seen:
                freq[key] = freq.get(key, 0) + 1
                seen.add(key)
    threshold = max(3, int(len(pages) * 0.5))
    repeated = {k for k, v in freq.items() if v >= threshold and len(k) <= 40}
    cleaned: list[list[Line]] = []
    for lines in pages:
        kept = []
        for ln in lines:
            key = re.sub(r"\d+", "#", ln.text)
            at_edge = ln.y1 < 90 or ln.y0 > 760
            if key in repeated and len(ln.text) <= 40 and at_edge:
                # 纯数字可能是章节号（如“1”单独成行），不按页眉页脚删除
                if re.fullmatch(r"\d{1,4}", ln.text):
                    kept.append(ln)
                    continue
                continue
            kept.append(ln)
        cleaned.append(kept)
    return cleaned, texts, count


def _read_docx(path: Path) -> tuple[list[list[Line]], list[str], int]:
    from docx import Document

    document = Document(path)
    lines: list[Line] = []
    texts: list[str] = []
    y = 0.0
    for para in document.paragraphs:
        text = norm(para.text)
        if not text:
            continue
        texts.append(text)
        style = (para.style.name or "").lower()
        size = 12.0
        bold = False
        if "heading 1" in style or style == "标题 1":
            size, bold = 16.0, True
        elif "heading 2" in style or style == "标题 2":
            size, bold = 14.0, True
        elif "heading 3" in style or style == "标题 3":
            size, bold = 12.5, True
        elif "title" in style:
            size, bold = 20.0, True
        y += 20
        lines.append(Line(text, 1, size, bold, 40, y, 555, y + 16, 515))
    for table in document.tables:
        rows = [[norm(cell.text) for cell in row.cells] for row in table.rows]
        md = _table_markdown(rows)
        if not md:
            continue
        texts.append(md)
        for row in rows:
            if not any(row):
                continue
            y += 20
            lines.append(Line(" | ".join(row), 1, 10.0, False, 40, y, 555, y + 16, 515))
    return [lines], ["\n".join(texts)], 1


def _read_markdown(path: Path) -> tuple[list[list[Line]], list[str], int]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines: list[Line] = []
    y = 0.0
    for raw in text.splitlines():
        t = norm(raw)
        if not t:
            continue
        size, bold = 11.0, False
        m = re.match(r"^(#{1,4})\s+(.*)$", t)
        if m:
            size = {1: 18.0, 2: 15.0, 3: 13.0, 4: 11.5}[len(m.group(1))]
            bold, t = True, norm(m.group(2))
        y += 20
        lines.append(Line(t, 1, size, bold, 40, y, 555, y + 16, 515))
    return [lines], [text], 1


def read_document(path: Path) -> tuple[list[list[Line]], list[str], int]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix == ".docx":
        return _read_docx(path)
    if suffix in (".md", ".markdown", ".txt"):
        return _read_markdown(path)
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        return [[]], [""], 1
    raise ValueError(f"暂不支持的文件类型：{suffix}")


def _ocr_lines(text: str, page_no: int) -> list[Line]:
    """把 OCR 纯文本还原成近似版式行，供标题/段落识别复用。"""
    out: list[Line] = []
    y = 42.0
    for idx, raw in enumerate((text or "").splitlines()):
        t = norm(raw)
        if not t:
            continue
        size = 10.5
        bold = False
        if re.match(r"^\s*(第?[0-9一二三四五六七八九十]+([.．][0-9]+)*[、.．\s])", t):
            size, bold = 13.5, True
        elif idx == 0 and page_no == 1:
            size, bold = 16.0, True
        out.append(Line(t, page_no, size, bold, 42.0, y, 556.0, y + size * 1.5, 514.0))
        y += size * 1.8
    return out


def _ocr_page_png(png: bytes, page_no: int, page_count: int) -> str:
    from . import llm, settings

    prompt = (
        f"请逐字识别第 {page_no}/{page_count} 页图片中的全部文字，按阅读顺序输出纯文本。"
        "保留标题编号、段落换行、表格单元格和公式符号；不要总结、不要解释、不要加 Markdown 代码块。"
        "如果识别到表格，请用 Markdown 表格输出，第一行是表头，每个单元格一行一列，保留所有表头和行；"
        "如果是柱状图/折线图/流程图等图表，请输出图表标题、坐标轴名称、图例、节点或数据标签等可辨认文字；"
        "如果页面是纯图片，请按阅读顺序输出图中文字。"
    )
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    return llm.chat(
        [
            {"role": "system", "content": "你是严谨的文档 OCR 引擎，只输出从图片中看到的文字，不补充图片外知识。"},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ],
        kind="ocr_page",
        model=settings.get("ocr.model") or None,
        json_mode=False,
        max_tokens=3000,
        temperature=0.0,
    )


def ocr_pdf_pages(path: Path, pages: list[list[Line]], page_texts: list[str]) -> tuple[list[list[Line]], list[str], dict]:
    """对文本层很薄的 PDF/图片调用图片识别模型；失败时保持原文本层。"""
    try:
        from . import llm, settings

        if not settings.get_bool("ocr.enabled", True) or not llm.available():
            return pages, page_texts, {"ocr_pages": 0}
        suffix = path.suffix.lower()
        is_image = suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
        if suffix != ".pdf" and not is_image:
            return pages, page_texts, {"ocr_pages": 0}
        if not page_texts:
            return pages, page_texts, {"ocr_pages": 0}
        lengths = [len(re.sub(r"\s", "", t or "")) for t in page_texts]
        page_count = len(lengths)
        if not is_image:
            avg = sum(lengths) / max(1, page_count)
            min_chars = settings.get_int("ocr.min_chars_per_page", 450)
            blank = sum(1 for n in lengths if n < 40)
            if avg >= min_chars and blank < max(1, page_count // 3):
                return pages, page_texts, {"ocr_pages": 0}
        max_pages = 1 if is_image else max(1, min(60, settings.get_int("ocr.max_pages", 20)))
        dpi = max(90, min(180, settings.get_int("ocr.dpi", 120)))
        doc = pymupdf.open(str(path))
        candidates = [0] if is_image else sorted(range(page_count), key=lambda i: lengths[i])[:max_pages]
        if not is_image:
            # 文本层正常但页面里嵌有大图的 PDF，find_tables 往往找不到表格；
            # 对这类页面补做 OCR，让视觉表格转成 Markdown 表格。
            image_pages: list[int] = []
            for idx, page in enumerate(doc):
                try:
                    if page.find_tables().tables:
                        continue
                    large = False
                    for img in page.get_images(full=True):
                        for rect in page.get_image_rects(img[0]):
                            if rect.width > page.rect.width * 0.45 and rect.height > page.rect.height * 0.10:
                                large = True
                                break
                        if large:
                            break
                    if large and lengths[idx] < 1400:
                        image_pages.append(idx)
                except Exception:
                    continue
            candidates = sorted(set(candidates + image_pages), key=lambda i: lengths[i])[:max_pages]
        replaced = 0
        model = settings.get("ocr.model") or ""
        for idx in candidates:
            if idx >= len(doc):
                continue
            try:
                page = doc[idx]
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                text = _ocr_page_png(pix.tobytes("png"), idx + 1, page_count)
                text = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
                text = re.sub(r"\s*```$", "", text).strip()
                text = re.sub(r"^(以下(是|为)[^：:\n]*[：:]\s*)", "", text)
                if len(re.sub(r"\s", "", text)) < 30:
                    continue
                # OCR 文本明显更完整时才替换，避免把正常文本层覆盖成更差的识别结果
                if len(re.sub(r"\s", "", text)) >= max(30, int(lengths[idx] * 0.6)):
                    page_texts[idx] = text
                    pages[idx] = _ocr_lines(text, idx + 1)
                    replaced += 1
            except Exception:
                continue
        doc.close()
        return pages, page_texts, {"ocr_pages": replaced, "ocr_model": model, "ocr_dpi": dpi}
    except Exception:
        return pages, page_texts, {"ocr_pages": 0}


# ---------------------------------------------------------------------------
# 阅读顺序
# ---------------------------------------------------------------------------
def reading_order(lines: list[Line], page_width: float) -> list[Line]:
    """还原阅读顺序：单栏按 y 排序；双栏按栏读取，全宽元素作为分段边界。"""
    if not lines:
        return []
    mid = page_width * 0.45
    narrow = [ln for ln in lines if ln.width < page_width * 0.45]
    left_narrow = [ln for ln in narrow if ln.x0 < mid]
    right_narrow = [ln for ln in narrow if ln.x0 >= mid]
    two_column = (
        len(left_narrow) >= 5
        and len(right_narrow) >= 5
        and len(narrow) / max(1, len(lines)) > 0.6
    )
    if not two_column:
        return sorted(lines, key=lambda l: (l.y0, l.x0))

    full = [ln for ln in lines if ln.width >= page_width * 0.66]
    others = [ln for ln in lines if ln.width < page_width * 0.66]

    def overlaps(a: Line, b: Line) -> bool:
        top, bottom = max(a.y0, b.y0), min(a.y1, b.y1)
        return (bottom - top) > 0.4 * min(a.y1 - a.y0, b.y1 - b.y0)

    separators = [f for f in full if not any(overlaps(f, o) for o in others)]
    rest = [ln for ln in lines if ln not in separators]
    if not separators:
        left = sorted([ln for ln in rest if ln.x0 < mid], key=lambda l: (l.y0, l.x0))
        right = sorted([ln for ln in rest if ln.x0 >= mid], key=lambda l: (l.y0, l.x0))
        return left + right
    sep_sorted = sorted(separators, key=lambda l: l.y0)
    boundaries = [0.0] + [(f.y0 + f.y1) / 2 for f in sep_sorted] + [10_000.0]
    out: list[Line] = []
    for idx in range(len(boundaries) - 1):
        lo, hi = boundaries[idx], boundaries[idx + 1]
        band = [ln for ln in rest if lo <= (ln.y0 + ln.y1) / 2 < hi]
        if band:
            left = sorted([ln for ln in band if ln.x0 < mid], key=lambda l: (l.y0, l.x0))
            right = sorted([ln for ln in band if ln.x0 >= mid], key=lambda l: (l.y0, l.x0))
            out.extend(left)
            out.extend(right)
        if idx < len(sep_sorted):
            out.append(sep_sorted[idx])
    return out


# ---------------------------------------------------------------------------
# 标题识别
# ---------------------------------------------------------------------------
SPECIAL_LABELS = {
    "摘要": "abstract",
    "abstract": "abstract",
    "关键词": "keywords",
    "关键字": "keywords",
    "keywords": "keywords",
    "参考文献": "reference",
    "references": "reference",
    "作者简介": "bio",
    "致谢": "section",
    "附录": "section",
}


def special_of(text: str) -> str | None:
    t = re.sub(r"\s+", "", text).lower().strip("：:")
    for key, value in SPECIAL_LABELS.items():
        if t == key:
            return value
    if re.match(r"^附录[a-z0-9一二三四五六七八九十]*$", t):
        return "section"
    return None


def _heading_title(text: str) -> str:
    """统一标题展示：压缩中文之间的空格，并在编号后补一个空格。"""
    t = re.sub(r"\s+", " ", text or "").strip()
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t)
    m = re.match(r"^(\d{1,2}(?:\.\d{1,2}){0,3})([\u4e00-\u9fffA-Za-z“”‘’《])", t)
    if m:
        t = m.group(1) + " " + t[len(m.group(1)):]
    return t


def heading_of(line: Line, body_size: float) -> Heading | None:
    text = _heading_title(line.text.strip())
    if not text:
        return None
    kind, inline = _label_parts(text)
    if kind and (len(text) <= 24 or line.y0 < 340 or line.size >= body_size + 0.5):
        label = {"abstract": "摘要", "keywords": "关键词", "reference": "参考文献", "bio": "作者简介"}.get(kind, "附录")
        return Heading(line, 1, label, kind)
    if len(text) > 60:
        return None
    if re.search(r"[\ue000-\uf8ff]", text):
        return None
    if "。" in text:
        return None
    if re.match(r"^\[\d+\]", text):
        return None
    if re.search(r"[。；，,]$", text):
        return None

    # 阿拉伯数字编号：1 / 1.2 / 1.2.3 / 2.2.3.1（允许“1引言”这种无空格写法）
    m = re.match(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[.、．]?\s*(\S.*)$", text)
    if m:
        num, rest = m.group(1), m.group(2)
        depth = num.count(".") + 1
        short = len(text) <= 46 and len(rest) >= 2
        # 字号/字重是标题的主信号；正文同字号的编号行仅在二级及以下才放行，
        # 避免把“2 幅边缘复杂……”这类换行后的正文误判成一级标题。
        styled = line.size >= body_size + 0.5 or line.bold
        if short and (styled or depth >= 2):
            if re.match(r"^\d{1,2}[）)]", text):
                return None
            # 编号标题一般不使用句读；带逗号的整行更像正文续行
            if re.search(r"[，,；;]", text):
                return None
            if re.match(r"^\d{1,2}\s*(个|年|月|日|页|条|项|种|类|倍|%|名|位|幅|张|组|篇)", text):
                return None
            # “2 多……”只有在“多个/多年”这类数量短语里才是正文；不能误伤“多维度……”
            if re.match(r"^(个|百|千|万|亿|余|多|以|倍|次|百分点)(个|年|月|日|页|条|项|种|类|倍|%|名|位|次|余)", rest):
                return None
            if rest.startswith(("个百分点", "个百", "个千", "个万", "多年", "多种", "多项", "多个", "多次", "余名", "余项", "余种")):
                return None
            # 正文同字号的二级编号行若缩进在栏中间，多半是表格行或正文续行
            if not styled and depth >= 2 and 100 < line.x0 < 280:
                return None
            if re.match(r"^[%％,，、;；。.．:：]", rest):
                return None
            if not re.match(r"[\u4e00-\u9fffA-Za-z“”‘’《]", rest):
                return None
            if rest[0].islower() or rest[0].isdigit():
                return None
            if depth == 1 and rest.isascii() and not re.search(CJK, rest):
                return None
            if depth == 1 and ("[" in text or "]" in text):
                return None
            # 纯数字/小数行（表格数据、公式片段）不当作标题
            if re.fullmatch(r"[\d\s.．%+×/=<>~\-]+", text):
                return None
            return Heading(line, min(depth, 4), text)
        return None

    # 中文编号：一、 / （一）——政策文本常与正文同字号，按编号直接接受
    m = re.match(r"^([一二三四五六七八九十]{1,3})[、.．]\s*(\S.*)$", text)
    if m and len(text) <= 46:
        return Heading(line, 1, text)
    m = re.match(r"^（([一二三四五六七八九十]{1,3})）\s*(\S.*)$", text)
    if m and len(text) <= 46:
        return Heading(line, 2, text)
    # “第X章/第X节/第X部分”：只有短标题或字号/字重明显时才接受，
    # 避免把“第二部分注重于……，该分支通过注”这类正文识别成标题。
    m = re.match(r"^(第[一二三四五六七八九十百\d]{1,4}[章节部分])\s*(\S.*)$", text)
    if m and len(text) <= 46:
        styled = line.size >= body_size + 0.5 or line.bold
        if styled or (len(text) <= 16 and not re.search(r"[，,；;]", text)):
            return Heading(line, 1 if "章" in m.group(1) or "部" in m.group(1) else 2, text)
    return None


def detect_body_size(pages: list[list[Line]]) -> float:
    """按字符数加权的字号中位数，避免表格/题注把正文字号带偏。"""
    weights: dict[float, int] = {}
    for lines in pages:
        for ln in lines:
            if re.search(CJK, ln.text) and 6.0 <= ln.size <= 16.0:
                weights[round(ln.size, 1)] = weights.get(round(ln.size, 1), 0) + len(ln.text)
    if not weights:
        return 10.5
    total = sum(weights.values())
    acc = 0
    for size in sorted(weights):
        acc += weights[size]
        if acc >= total / 2:
            return size
    return max(weights.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------
def detect_title_layout(pages: list[list[Line]], body_size: float) -> tuple[str, int, Line | None]:
    """返回题名、正文起始页、题名行。"""
    abstract_page = None
    abstract_y = 10_000.0
    for idx, lines in enumerate(pages[:3], start=1):
        ordered = reading_order(lines, 595)
        for ln in ordered:
            kind, _ = _label_parts(ln.text)
            if kind == "abstract":
                abstract_page = idx
                abstract_y = ln.y0
                break
        if abstract_page:
            break
    candidate_pages = [abstract_page] if abstract_page else list(range(1, min(2, len(pages)) + 1))
    if not candidate_pages:
        candidate_pages = [1]
    best: tuple[float, Line, int] | None = None
    for pno in candidate_pages:
        if pno < 1 or pno > len(pages):
            continue
        lines = reading_order(pages[pno - 1], 595)
        cutoff = abstract_y if pno == abstract_page else 10_000
        for ln in lines:
            if ln.y0 > cutoff:
                continue
            if len(ln.text) < 5 or len(ln.text) > 120:
                continue
            kind, _ = _label_parts(ln.text)
            if kind or re.match(r"^[\d\s\.\-—*]+$", ln.text):
                continue
            if re.search(r"(网络首发|ISSN|CN\s*\d|收稿日期|引用格式|题目[:：]|作者[:：])", ln.text):
                continue
            score = ln.size + (2 if re.search(CJK, ln.text) else 0) - (0.01 * len(ln.text))
            if best is None or score > best[0]:
                best = (score, ln, pno)
    if not best:
        return "未命名文档", 1, None
    line = best[1]
    page_lines = sorted(reading_order(pages[best[2] - 1], 595), key=lambda l: l.y0)
    # 同字号的折行题名：合并题名行上下相邻、字号接近的行
    idx = next((i for i, l in enumerate(page_lines) if l is line), -1)
    group = [line]
    for direction in (-1, 1):
        i = idx
        while True:
            i += direction
            if i < 0 or i >= len(page_lines):
                break
            cand = page_lines[i]
            anchor = group[0] if direction < 0 else group[-1]
            gap = (anchor.y0 - cand.y1) if direction < 0 else (cand.y0 - anchor.y1)
            if abs(cand.size - line.size) > 1.0 or gap > line.size * 2.4:
                break
            if _label_parts(cand.text)[0]:
                break
            group.append(cand)
    group.sort(key=lambda l: l.y0)
    title = "".join(l.text for l in group)
    title = re.sub(r"[*＊]\s*$", "", title).strip()
    return title, best[2], line


def _authors_and_affiliations(lines: list[Line], title_line: Line | None, abstract_y: float) -> str:
    if not title_line:
        return ""
    found: list[str] = []
    for ln in lines:
        if ln.y0 <= title_line.y1:
            continue
        if ln.y0 >= abstract_y:
            continue
        if ln.y0 > title_line.y1 + 48:
            continue
        t = ln.text
        if len(t) > 100 or ln.size < 7.0:
            continue
        if re.search(r"(收稿|网络首发|引用格式|摘要|关键词|ISSN|CN\s*\d|DOI|题目[:：])", t):
            continue
        if re.match(r"^\d+$", t):
            continue
        if re.search(r"(大学|学院|公司|研究院|研究所|中心|实验室|系|集团).*(路|号|市|省|区|县|\d{6})", t):
            continue
        if re.match(r"^[\d\s()（）,，]+$", t):
            continue
        found.append(t)
    # 去掉“作者：”标签本身，保留其后的内容；清理姓名后的上标数字
    cleaned = []
    for t in found:
        t = re.sub(r"^作者[:：]\s*", "", t)
        t = re.sub(r"^[\d\s]+", "", t)
        t = re.sub(r"(?<=[\u4e00-\u9fff])[\d,，\s]+$", "", t)
        if t and t not in cleaned:
            cleaned.append(t)
    # 合并同一行的多个姓名（PDF 常把姓名按文本框拆行）
    return "；".join(cleaned[:4])


def detect_doc_type(title: str, abstract: str, section_titles: list[str], page_count: int) -> str:
    blob = title + " " + abstract + " " + " ".join(section_titles)
    if re.search(r"(综述|研究进展|进展与展望|研究现状|文献综述)", title):
        return "review"
    if re.search(r"(政策|政府|治理|制度|框架及其生成逻辑)", title) and re.search(r"(政策|政府|治理)", blob):
        return "policy"
    if re.search(r"(实验|结果|方法|模型|算法)", blob) and re.search(r"(摘要|关键词)", blob):
        return "research_paper"
    if page_count <= 5 and not re.search(r"(摘要|关键词)", blob) and len(section_titles) <= 3:
        return "news"
    if re.search(r"(实验|数据集|基线|消融)", blob):
        return "research_paper"
    return "other"


# ---------------------------------------------------------------------------
# 结构树
# ---------------------------------------------------------------------------
def _append(state: dict, text: str, page: int) -> None:
    if not text:
        return
    if state["text"] and not state["text"].endswith(("\n", " ")):
        prev = state["text"][-1]
        nxt = text[0]
        if re.match(r"[A-Za-z0-9]", prev) and re.match(r"[A-Za-z0-9]", nxt):
            state["text"] += " "
    start = len(state["text"])
    state["text"] += text
    state["spans"].append((start, len(state["text"]), page))


def _join_paragraph(lines: list[Line]) -> tuple[str, list[tuple[int, int, int]]]:
    state = {"text": "", "spans": []}
    for ln in lines:
        t = ln.text.strip()
        if not t:
            continue
        if state["text"] and state["text"].endswith(("。", "！", "？", "；", "：", "”", "’", "）", ")", "】")):
            _append(state, "\n" + t, ln.page)
        else:
            _append(state, t, ln.page)
    return state["text"].strip(), state["spans"]


def _caption_of(text: str) -> str | None:
    """识别图表题注；排除“如图 1 所示”“表 2 中”等正文引用。"""
    if len(text) > 110:
        return None
    if re.search(r"(所示|如下|可知|中给出|展示了|见图|见表|如图|如表)", text[:16]):
        return None
    m = re.match(r"^(图|表)\s*(\d+)([-—.．]\d+)?\s*(.*)$", text)
    if m and m.group(4):
        return "figure" if m.group(1) == "图" else "table"
    if re.match(r"^(Figure|Fig\.?|Table)\s*\d+", text, re.I):
        return "figure" if re.match(r"^(Figure|Fig)", text, re.I) else "table"
    return None


def _label_parts(text: str) -> tuple[str | None, str]:
    """识别“摘要：xxx”这类标签行，返回（类型，行内正文）。"""
    compact = re.sub(r"\s+", "", text or "")
    m = re.match(
        r"^(摘\s*要|abstract|关键词|关键字|key\s*words?|参考文献|references|作者简介)\s*([:：]?)\s*(.*)$",
        compact,
        re.I,
    )
    if not m:
        return None, ""
    raw_label, delim, rest = m.group(1), m.group(2), m.group(3)
    label = re.sub(r"\s+", "", raw_label).lower()
    kind = SPECIAL_LABELS.get(label)
    if not kind:
        return None, ""
    # 关键词/参考文献/作者简介必须带分隔符，避免“关键词检索采用……”被当成标签
    if kind in ("keywords", "reference", "bio") and not delim:
        english_kw = label.startswith("keyword") or label.startswith("key") and "word" in label
        if compact.lower() not in ("关键词", "关键字", "keywords", "参考文献", "references", "作者简介"):
            if not (english_kw and re.match(r"^(key\s*words?|keywords)\s+\S", text, re.I)):
                return None, ""
    return kind, rest.strip()


def detect_short_headings(ordered: list[Line], body_size: float, start_index: int = 0) -> list[tuple[int, Heading]]:
    """无编号文档（如报纸深度报道）的小标题兜底识别。

    判据：短行、字号接近正文、整行宽度较大、不以句末标点结尾、下一行是长正文、
    上一行已收句。用于没有编号体系的报道/评论类文本。
    """
    found: list[tuple[int, Heading]] = []
    for idx in range(max(1, start_index + 1), len(ordered) - 1):
        ln = ordered[idx]
        prev, nxt = ordered[idx - 1], ordered[idx + 1]
        t = ln.text.strip()
        if not (4 <= len(t) <= 28):
            continue
        if not re.search(CJK, t):
            continue
        if abs(ln.size - body_size) > 0.7:
            continue
        if re.search(r"[。；：！？、?!]$", t) or t.startswith(("“", "‘", "图", "表", "注", "（", "(", "[")):
            continue
        if re.match(r"^[\dA-Za-z.．、\-—#*]+$", t):
            continue
        if t.count("，") > 1:
            continue
        if ln.width < 80 or ln.x0 > 220:
            continue
        if not re.search(r"[。！？；”’）)]$", prev.text.strip()):
            continue
        if len(nxt.text) < 24 and nxt.size <= body_size + 0.5 and not re.search(r"[。！？；]", nxt.text):
            continue
        found.append((idx, Heading(ln, 1, t)))
    # 小标题通常 2—8 个；过多说明是表格或诗歌，放弃兜底
    if 2 <= len(found) <= 8:
        return found
    return []


def _merge_heading_lines(ordered: list[Line], body_size: float, protected: set[int] | None = None) -> list[Line]:
    """合并“编号独占一行”与“标题折行”的版式。

    中文期刊常见两种排版：
    1. “1” / “相关工作” 分两行（甚至跨页）；
    2. 长标题在右栏折成两行。
    不合并会让标题识别漏掉一整级，或只截到半句。
    """
    protected = protected or set()
    out: list[Line] = []
    i = 0
    while i < len(ordered):
        a = ordered[i]
        if id(a) in protected:
            out.append(a)
            i += 1
            continue
        # 版式里“标题文字在前、编号在后”的情况：4.2 与 M3UCD… 同 baseline，
        # 阅读顺序可能先排标题再排编号。
        if re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){0,3}", a.text.strip()) and out:
            prev = out[-1]
            tb = prev.text.strip()
            if (prev.page == a.page and abs(prev.y0 - a.y0) <= 3.5
                    and 0 < prev.x0 - a.x0 <= 40 and 2 <= len(tb) <= 60
                    and not re.search(r"[。；，,：:]$", tb)
                    and re.match(r"[\u4e00-\u9fffA-Za-z“”‘’《]", tb)):
                parts = [int(x) for x in a.text.strip().split(".")]
                if parts and parts[0] <= 20 and all(p <= 30 for p in parts):
                    merged = Line(f"{a.text.strip()} {tb}", a.page, max(a.size, prev.size),
                                  a.bold or prev.bold, prev.x0, prev.y0, prev.x1, prev.y1, prev.x1 - prev.x0)
                    out[-1] = merged
                    i += 1
                    continue
        if i + 1 >= len(ordered):
            out.append(a)
            i += 1
            continue
        b = ordered[i + 1]
        merged = _merge_two_lines(a, b, body_size)
        if merged is not None:
            out.append(merged)
            i += 2
        else:
            out.append(a)
            i += 1
    return out


def _merge_two_lines(a: Line, b: Line, body_size: float) -> Line | None:
    ta, tb = a.text.strip(), b.text.strip()
    if not ta or not tb:
        return None
    # 编号独占一行：1 / 1.2 / 2.3.1
    if re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){0,3}", ta):
        parts = [int(x) for x in ta.split(".")]
        if not parts or parts[0] > 20 or any(p > 30 for p in parts):
            return None
        if b.page - a.page not in (0, 1):
            return None
        # 只合并“同一栏、紧邻下方”的标题；表格单元格里的数字通常不在栏首
        if not (a.x0 <= 95 or 270 <= a.x0 <= 345):
            return None
        if abs(b.x0 - a.x0) > 35:
            return None
        if b.page == a.page:
            if not (0 <= b.y0 - a.y0 <= 42):
                return None
        else:
            if a.y0 < 640 or b.y0 > 150:
                return None
        if not 2 <= len(tb) <= 60:
            return None
        # 标题行一般没有句读；带逗号/句号的“编号+文字”多半是正文续行
        if re.search(r"[。.；;，,：:]", tb):
            return None
        if not re.match(r"[\u4e00-\u9fffA-Za-z“”‘’《]", tb):
            return None
        if b.size < body_size - 1.5 or b.x0 > 345:
            return None
        return Line(f"{ta} {tb}", b.page, max(a.size, b.size), a.bold or b.bold,
                    b.x0, b.y0, b.x1, b.y1, b.x1 - b.x0)
    # 标题折行：同一栏、同字号、同字重，上一行接近右边界
    if not (a.size >= body_size + 0.5 or a.bold):
        return None
    if a.page != b.page or abs(a.size - b.size) > 0.35:
        return None
    if a.bold != b.bold or abs(a.x0 - b.x0) > 10:
        return None
    if a.x1 < 470 or len(tb) > 40 or len(ta) + len(tb) > 80:
        return None
    if re.search(r"[。；，,：:]$", tb) or re.match(r"^\d", tb):
        return None
    if not re.match(r"[\u4e00-\u9fffA-Za-z（(“”‘’《]", tb):
        return None
    return Line(ta + tb, a.page, a.size, a.bold, a.x0, a.y0, b.x1, b.y1, b.x1 - a.x0)


def build_structure(
    pages: list[list[Line]],
    title: str,
    title_page: int,
    title_line: Line | None,
    body_size: float,
    doc_type: str,
) -> tuple[list[NodeDraft], dict]:
    """把行序列还原为结构树。

    顶层节点顺序：题名、作者、摘要、关键词、正文章节、参考文献。
    """
    nodes: list[NodeDraft] = []
    title_node = NodeDraft(new_id("node"), None, 1, "title", 1, title or "未命名文档", "", title_page, title_page)
    nodes.append(title_node)

    ordered: list[Line] = []
    for pno, lines in enumerate(pages, start=1):
        for ln in reading_order(lines, 595):
            ln.page = pno
            ordered.append(ln)
    ordered = _merge_heading_lines(ordered, body_size, {id(title_line)} if title_line else set())
    index_of = {id(ln): i for i, ln in enumerate(ordered)}

    headings: list[tuple[int, Heading]] = []
    for idx, ln in enumerate(ordered):
        h = heading_of(ln, body_size)
        if h:
            headings.append((idx, h))
    # 只从摘要/题名之后开始识别章节，避免封面、作者、栏目信息被当成标题
    abstract_idx = next((i for i, (_, h) in enumerate(headings) if h.special == "abstract"), None)
    abstract_y = next((h.line.y0 for _, h in headings if h.special == "abstract"), None)
    if abstract_idx is not None:
        first_content = headings[abstract_idx][0]
        headings = [(i, h) for i, h in headings if i >= first_content]
        if abstract_y is not None:
            headings = [
                (i, h) for i, h in headings
                if not (h.line.page == title_page and h.line.y0 < abstract_y - 1)
            ]
    elif title_line is not None:
        title_idx = next((i for i, ln in enumerate(ordered) if ln is title_line), 0)
        headings = [(i, h) for i, h in headings if i > title_idx]
    if not headings:
        start_idx = next((i for i, ln in enumerate(ordered) if ln is title_line), 0)
        headings = detect_short_headings(ordered, body_size, start_idx)
    heading_indices = [idx for idx, _ in headings]

    def next_heading_index(idx: int) -> int:
        for j in heading_indices:
            if j > idx:
                return j
        return len(ordered)

    def slice_body(idx: int) -> list[Line]:
        return ordered[idx + 1 : next_heading_index(idx)]

    # 作者与单位
    front_end = next((idx for idx, h in headings if h.special == "abstract"), len(ordered))
    if front_end == len(ordered):
        front_lines = [ln for ln in ordered if ln.page == title_page]
        abstract_y = min(10_000, title_line.y1 + 45) if title_line else 10_000
    else:
        front_lines = ordered[:front_end]
        abstract_y = next((ln.y0 for ln in front_lines if _label_parts(ln.text)[0] == "abstract"), 10_000)
    authors = _authors_and_affiliations(front_lines, title_line, abstract_y)
    if authors:
        nodes.append(NodeDraft(new_id("node"), None, 1, "author", 2, "作者与单位", authors, title_page, title_page))

    abstract_text = ""
    keywords: list[str] = []
    abstract_node: NodeDraft | None = None
    keywords_node: NodeDraft | None = None
    reference_node: NodeDraft | None = None
    ref_count = 0

    # 特殊段：摘要、关键词、参考文献、作者简介（同名段只取第一次）
    for idx, h in headings:
        kind, inline = _label_parts(h.line.text)
        same_row = [
            ln for ln in ordered[max(0, idx - 3) : idx + 4]
            if ln is not h.line and ln.page == h.line.page and abs(ln.y0 - h.line.y0) < 6 and ln.x0 > h.line.x0
        ]
        if kind == "abstract":
            body = slice_body(idx)
            body = [ln for ln in body if _label_parts(ln.text)[0] not in ("keywords", "reference")]
            same_ids = {id(ln) for ln in same_row}
            body = same_row + [ln for ln in body if id(ln) not in same_ids]
            text, spans = _join_paragraph(body)
            candidate = (inline + (" " if inline and text else "") + text).strip()
            candidate = re.split(r"(关键词|关键字)[:：]", candidate)[0].strip()
            cjk_new = len(re.findall(r"[\u4e00-\u9fff]", candidate))
            if abstract_node is not None and len(re.findall(r"[\u4e00-\u9fff]", abstract_node.text)) >= cjk_new:
                continue
            abstract_text = candidate
            abstract_node = NodeDraft(
                new_id("node"), None, 1, "abstract", 2, "摘要",
                abstract_text, h.line.page, body[-1].page if body else h.line.page, 0.97,
            )
            abstract_node.page_spans = spans
        elif kind == "keywords":
            body = slice_body(idx)
            parts = [inline] if inline else []
            parts.extend(ln.text for ln in same_row)
            for ln in body[:2]:
                if re.search(r"(中图|文献标志|DOI|Abstract|Key\s*words)", ln.text, re.I):
                    break
                if len(re.findall(r"[A-Za-z]", ln.text)) > len(ln.text) * 0.6:
                    break
                if ln.text not in parts:
                    parts.append(ln.text)
            raw = " ".join(p for p in parts if p)
            keywords = [k.strip() for k in re.split(r"[;；,，\s]+", raw) if 1 < len(k.strip()) < 30][:12]
            cjk_new = len(re.findall(CJK, "".join(keywords)))
            if keywords_node is not None and len(re.findall(CJK, keywords_node.text)) >= cjk_new:
                continue
            keywords_node = NodeDraft(
                new_id("node"), None, 1, "keywords", 2, "关键词",
                "；".join(keywords), h.line.page, body[-1].page if body else h.line.page, 0.95,
            )
        elif kind == "reference":
            if reference_node is not None:
                continue
            body = slice_body(idx)
            text_parts = [inline] if inline else []
            for ln in body:
                if re.match(r"^\[\d+\]", ln.text):
                    ref_count += 1
                text_parts.append(ln.text)
            if ref_count == 0:
                joined = "\n".join(text_parts)
                ref_count = len(re.findall(r"(?:^|\n)\s*(?:\[\d+\]|\d+[.、])\s*\S", joined))
            reference_node = NodeDraft(
                new_id("node"), None, 1, "reference", 99, "参考文献",
                "\n".join(text_parts), h.line.page, body[-1].page if body else h.line.page, 0.95,
            )

    # 部分 PDF 的“参考文献”标题缺失或为图片：以 [1] 起头的连续条目兜底
    ref_line_ids: set[int] = set()
    if reference_node is None:
        for i, ln in enumerate(ordered):
            if re.match(r"^\[1\]", ln.text):
                window = ordered[i : i + 8]
                if sum(1 for x in window if re.match(r"^\[\d+\]", x.text)) >= 3:
                    ref_lines = ordered[i:]
                    text_parts = [x.text for x in ref_lines]
                    ref_count = sum(1 for x in ref_lines if re.match(r"^\[\d+\]", x.text))
                    reference_node = NodeDraft(
                        new_id("node"), None, 1, "reference", 99, "参考文献",
                        "\n".join(text_parts), ln.page, ref_lines[-1].page, 0.9,
                    )
                    ref_line_ids = {id(x) for x in ref_lines}
                    break

    # 正文章节
    section_nodes: list[NodeDraft] = []
    stack: list[NodeDraft] = []
    caption_seen: set[tuple[str, str]] = set()
    skip_kinds = {"abstract", "keywords", "reference"}
    for idx, h in headings:
        kind, inline = _label_parts(h.line.text)
        if kind in skip_kinds:
            continue
        if kind == "bio":
            body = slice_body(idx)
            text, spans = _join_paragraph(body)
            node = NodeDraft(
                new_id("node"), None, 1, "bio", 98, "作者简介",
                (inline + text).strip(), h.line.page, body[-1].page if body else h.line.page, 0.9,
            )
            node.page_spans = spans
            nodes.append(node)
            stack = []
            continue
        level = max(1, min(4, h.level))
        while stack and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1] if stack else None
        node = NodeDraft(
            new_id("node"),
            parent.node_id if parent else None,
            level,
            "section",
            (len(parent.children) + 1) if parent else 1,
            h.title,
            "",
            h.line.page,
            h.line.page,
            0.95 if re.match(r"^[0-9一二三四五六七八九十]", h.title) else 0.82,
        )
        if parent:
            parent.children.append(node)
        else:
            section_nodes.append(node)
        nodes.append(node)
        stack.append(node)

        # 本节正文（到下一个任意标题为止）
        body = slice_body(idx)
        body_text: list[Line] = []
        for ln in body:
            if id(ln) in ref_line_ids:
                continue
            if _label_parts(ln.text)[0] in ("abstract", "keywords", "reference"):
                continue
            cap = _caption_of(ln.text)
            if cap and len(ln.text) < 120:
                m = re.match(r"^(图|表)\s*(\d+)", ln.text)
                key = (m.group(1), m.group(2)) if m else (cap, ln.text[:12])
                if key in caption_seen:
                    continue
                caption_seen.add(key)
                cap_node = NodeDraft(
                    new_id("node"), node.node_id, node.level + 1, cap,
                    len(node.children) + 1, ln.text, ln.text, ln.page, ln.page, 0.9,
                )
                node.children.append(cap_node)
                nodes.append(cap_node)
            else:
                body_text.append(ln)
        text, spans = _join_paragraph(body_text)
        node.text = text
        node.page_spans = spans
        if body_text:
            node.page_end = max(ln.page for ln in body_text)
        else:
            node.page_end = h.line.page

    if reference_node:
        nodes.append(reference_node)
    if abstract_node:
        nodes.append(abstract_node)
    if keywords_node:
        nodes.append(keywords_node)
        keywords = [k for k in keywords_node.text.split("；") if k]

    # 报纸/报道类文档常在第一个小标题之前先写导语；这段文字没有标题可挂，
    # 旧实现会直接丢失。把它并入第一节，保证全文可检索、可出题。
    if doc_type == "news" and section_nodes and headings:
        first_idx = headings[0][0]
        lead_lines = []
        for ln in ordered[:first_idx]:
            if ln is title_line:
                continue
            t = ln.text.strip()
            if len(t) < 20:
                continue
            if re.search(r"(人民日报|第\d+版|网络世界|年/.*月/.*日)", t):
                continue
            lead_lines.append(ln)
        if lead_lines:
            lead_text, lead_spans = _join_paragraph(lead_lines)
            first = section_nodes[0]
            first.text = (lead_text + "\n" + first.text).strip()
            first.page_spans = lead_spans + first.page_spans
            first.page_start = min(first.page_start, min(ln.page for ln in lead_lines))

    # 父节点页码覆盖子树
    def extend_pages(node: NodeDraft) -> tuple[int, int]:
        start, end = node.page_start, node.page_end
        for child in node.children:
            cs, ce = extend_pages(child)
            start, end = min(start, cs), max(end, ce)
        node.page_start, node.page_end = start, end
        return start, end

    for node in nodes:
        if node.parent_id is None:
            extend_pages(node)

    # 统一排序与路径
    priority = {"title": 0, "author": 1, "abstract": 2, "keywords": 3, "section": 5, "bio": 8, "reference": 9}
    top = [n for n in nodes if n.parent_id is None]
    top.sort(key=lambda n: (priority.get(n.node_type, 6), n.page_start, n.order_no))

    def assign(node: NodeDraft, order: int, parent_path: str) -> None:
        node.order_no = order
        node.path = f"{parent_path}{order}/"
        for idx, child in enumerate(node.children, start=1):
            assign(child, idx, node.path)

    for idx, node in enumerate(top, start=1):
        assign(node, idx, "/")

    def all_nodes(items: Iterable[NodeDraft]) -> Iterable[NodeDraft]:
        for it in items:
            yield it
            yield from all_nodes(it.children)

    figures = sum(1 for n in all_nodes(nodes) if n.node_type == "figure")
    tables = sum(1 for n in all_nodes(nodes) if n.node_type == "table")
    # 统计口径：全文题注按“图/表 + 编号”去重，包含首节之前的前置图表
    caption_keys: set[tuple[str, str]] = set()
    for ln in ordered:
        cap = _caption_of(ln.text)
        m = re.match(r"^(图|表)\s*(\d+)", ln.text)
        if cap and m:
            caption_keys.add((cap, m.group(2)))
    if caption_keys:
        figures = max(figures, sum(1 for cap, _ in caption_keys if cap == "figure"))
        tables = max(tables, sum(1 for cap, _ in caption_keys if cap == "table"))
    if reference_node and reference_node.text:
        nums = [int(n) for n in re.findall(r"\[(\d{1,3})\]", reference_node.text)]
        if nums:
            ref_count = max(ref_count, max(nums))
        else:
            ref_count = max(ref_count, len(re.findall(r"(?:^|\n)\s*\[\d+\]", reference_node.text)))
    sections = [n for n in nodes if n.node_type == "section"]
    stats = {
        "node_count": len(nodes),
        "chunk_count": 0,
        "kp_count": 0,
        "figure_count": figures,
        "table_count": tables,
        "reference_count": ref_count,
        "section_count": len(sections),
    }
    return nodes, {"stats": stats, "abstract": abstract_text, "keywords": keywords, "authors": authors}


# ---------------------------------------------------------------------------
def quality_gate(page_texts: list[str], stats: dict, title: str, abstract: str) -> dict:
    # NFKC 归一化后再统计：全角数字/标点不应被误判为乱码
    normalized = unicodedata.normalize("NFKC", "\n".join(page_texts))
    compact = re.sub(r"\s", "", normalized)
    total = len(compact) or 1
    # 真正的乱码：替换字符、无法显示的方块、控制字符
    replacement = len(re.findall(r"[\ufffd\u25a1]", compact))
    control = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", compact))
    garbled = replacement + control
    garbled_rate = min(1.0, garbled / total)
    # 私有区字符通常来自 PDF 的公式字体（如 \uf03d 表示等号），不是 OCR 失败
    formula_glyphs = len(re.findall(r"[\ue000-\uf8ff]", compact))
    formula_rate = round(formula_glyphs / total, 4)
    page_count = len(page_texts)
    ocr_pages = int(stats.get("ocr_pages") or 0)
    expected = max(1, (page_count - ocr_pages) * 1100 + ocr_pages * 300)
    coverage = min(1.0, total / expected)
    if stats.get("section_count", 0) >= 4:
        heading = 0.95
    elif stats.get("section_count", 0) >= 2:
        heading = 0.8
    else:
        heading = 0.5
    completeness = sum(
        1 for cond in [bool(title and len(title) >= 4), bool(abstract), stats.get("section_count", 0) >= 3, stats.get("reference_count", 0) > 0]
        if cond
    ) / 4
    figure_rate = 0.95 if (stats.get("figure_count", 0) + stats.get("table_count", 0) + stats.get("chart_count", 0)) > 0 else 1.0
    score = 0.30 * coverage + 0.25 * heading + 0.20 * (1 - min(1, garbled_rate * 20)) + 0.15 * completeness + 0.10 * figure_rate
    score = round(max(0.0, min(1.0, score)), 3)
    level = "pass" if score >= 0.85 else ("review" if score >= 0.6 else "fail")
    issues = []
    if coverage < 0.98 and not (ocr_pages and coverage >= 0.6):
        issues.append({"code": "TEXT_COVERAGE_LOW", "message": f"文本覆盖率约 {coverage:.0%}，可能存在图片型页面"})
    if garbled_rate > 0.005:
        issues.append({"code": "GARBLED_TEXT", "message": f"乱码率 {garbled_rate:.2%}，建议 OCR 或人工校正"})
    if stats.get("section_count", 0) < 3:
        issues.append({"code": "STRUCTURE_INCOMPLETE", "message": "章节识别不足，结构树可能不完整"})
    return {
        "score": score,
        "level": level,
        "issues": issues,
        "metrics": {
            "text_coverage": round(coverage, 3),
            "garbled_rate": round(garbled_rate, 4),
            "formula_glyph_rate": formula_rate,
            "heading_confidence": heading,
            "structure_completeness": round(completeness, 2),
            "figure_link_rate": figure_rate,
            "ocr_pages": ocr_pages,
        },
        "failed_pages": [],
    }


def _table_markdown(rows: list[list[str]]) -> str:
    clean = [[" ".join((c or "").split()) for c in row] for row in rows if row]
    clean = [r for r in clean if any(r)]
    if len(clean) < 2:
        return ""
    width = max(len(r) for r in clean)
    clean = [r + [""] * (width - len(r)) for r in clean]
    lines = ["| " + " | ".join(clean[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in clean[1:]]
    return "\n".join(lines)


def _markdown_tables(text: str) -> list[str]:
    """从 OCR 的 Markdown 输出中恢复表格，跳过普通正文里的竖线。"""
    lines = (text or "").splitlines()
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        if not lines[i].strip().startswith("|"):
            i += 1
            continue
        start = i
        group: list[str] = []
        while i < len(lines) and lines[i].strip().startswith("|") and lines[i].count("|") >= 2:
            group.append(lines[i].strip())
            i += 1
        if len(group) < 2:
            continue
        rows: list[list[str]] = []
        for line in group:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
                continue
            rows.append(cells)
        if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
            md = _table_markdown(rows)
            if md:
                blocks.append(md)
    return blocks


def extract_table_blocks(path: Path, page_texts: list[str]) -> list[dict]:
    """抽取 PDF/DOCX 表格单元格，返回页级 Markdown 表格块。"""
    blocks: list[dict] = []
    try:
        if path.suffix.lower() == ".pdf":
            import pymupdf
            doc = pymupdf.open(str(path))
            for idx, page in enumerate(doc):
                try:
                    tables = page.find_tables()
                except Exception:
                    tables = []
                for table in tables:
                    try:
                        data = table.extract()
                    except Exception:
                        continue
                    md = _table_markdown(data or [])
                    if not md:
                        continue
                    blocks.append({"page": idx + 1, "markdown": md, "rows": len(data or []),
                                   "cells": sum(len(r) for r in (data or []))})
                    if idx < len(page_texts):
                        page_texts[idx] = (page_texts[idx] or "") + "\n\n" + md
            doc.close()
        elif path.suffix.lower() == ".docx":
            from docx import Document
            document = Document(path)
            for table in document.tables:
                data = [[cell.text for cell in row.cells] for row in table.rows]
                md = _table_markdown(data)
                if md:
                    blocks.append({"page": 1, "markdown": md, "rows": len(data),
                                   "cells": sum(len(r) for r in data)})
                    if page_texts:
                        page_texts[0] = (page_texts[0] or "") + "\n\n" + md
        seen = {b.get("markdown") for b in blocks}
        for page_no, text in enumerate(page_texts, 1):
            for md in _markdown_tables(text):
                if md and md not in seen:
                    seen.add(md)
                    blocks.append({"page": page_no, "markdown": md, "rows": md.count("\n") + 1,
                                   "cells": sum(line.count("|") - 1 for line in md.splitlines())})
    except Exception:
        return blocks
    return blocks


def extract_formula_blocks(path: Path) -> list[dict]:
    """提取 PDF 公式字体行 / DOCX OMML 公式文本，作为公式结构节点。"""
    formulas: list[dict] = []
    try:
        if path.suffix.lower() == ".pdf":
            import pymupdf
            doc = pymupdf.open(str(path))
            for idx, page in enumerate(doc):
                seen = set()
                for block in page.get_text("dict").get("blocks", []):
                    for line in block.get("lines", []):
                        spans = line.get("spans", [])
                        text = "".join(s.get("text", "") for s in spans).strip()
                        if not text or len(text) < 2 or text in seen:
                            continue
                        fonts = " ".join(str(s.get("font", "")) for s in spans).lower()
                        has_private = bool(re.search(r"[\ue000-\uf8ff]", text))
                        has_math_font = any(k in fonts for k in ("math", "cmmi", "cmsy", "symbol", "stix", "mt extra"))
                        clean = re.sub(r"[\ue000-\uf8ff]", "", text).strip()
                        if not (has_private or has_math_font):
                            continue
                        if len(clean) < 3 or len(clean) > 120 or "\ufffd" in clean:
                            continue
                        if not re.search(r"[=+\-×÷∑∫√^_0-9A-Za-zα-ωΑ-Ω]", clean):
                            continue
                        math_chars = re.findall(r"[=+\-×÷∑∫√^_0-9A-Za-zα-ωΑ-Ω]", clean)
                        if len(math_chars) < 2 or re.fullmatch(r"[\d\s.,]+", clean):
                            continue
                        if len(re.findall(r"[\u4e00-\u9fff]", clean)) > len(math_chars) * 2:
                            continue
                        seen.add(text)
                        formulas.append({"page": idx + 1, "text": clean})
                if len(formulas) >= 80:
                    break
            doc.close()
        elif path.suffix.lower() == ".docx":
            from docx import Document
            from docx.oxml.ns import qn
            document = Document(path)
            seen = set()
            for elem in document.element.body.iter():
                if elem.tag not in (qn("m:oMath"), qn("m:oMathPara")):
                    continue
                parts = [t.text or "" for t in elem.iter() if t.tag == qn("m:t")]
                text = norm("".join(parts))
                if text and text not in seen:
                    seen.add(text)
                    formulas.append({"page": 1, "text": text})
    except Exception:
        return formulas
    return formulas


CHART_TYPES = {"bar", "line", "pie", "scatter", "area", "mixed", "other"}


def _normalise_chart(raw: dict, page: int, fallback_title: str) -> dict | None:
    """把视觉模型输出规范化为可入库的图表数据；拒绝没有数值的伪图表。"""
    if not isinstance(raw, dict) or raw.get("is_chart") is False:
        return None
    chart_type = str(raw.get("chart_type") or "other").lower()
    if chart_type not in CHART_TYPES:
        chart_type = "other"
    categories = [str(x) for x in (raw.get("categories") or []) if str(x).strip()]
    series_in = raw.get("series") or []
    if not isinstance(series_in, list):
        series_in = []
    series: list[dict] = []
    for item in series_in:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"系列 {len(series)+1}")
        values = []
        for value in (item.get("values") or []):
            try:
                values.append(float(value) if value is not None and str(value).strip() != "" else None)
            except Exception:
                values.append(None)
        if values and any(v is not None for v in values):
            series.append({"name": name[:80], "values": values})
    points = []
    for item in (raw.get("points") or []):
        if not isinstance(item, dict):
            continue
        try:
            y = float(item.get("y")) if item.get("y") is not None and str(item.get("y")).strip() != "" else None
        except Exception:
            y = None
        if y is not None:
            points.append({"x": str(item.get("x") or ""), "y": y, "series": str(item.get("series") or "")})
    if not series and points:
        by_series: dict[str, list[float]] = {}
        for point in points:
            by_series.setdefault(point["series"] or "数值", []).append(point["y"])
        series = [{"name": name, "values": values} for name, values in by_series.items()]
        if not categories:
            categories = [p["x"] for p in points]
    if not series:
        return None
    width = max([len(s["values"]) for s in series] + [len(categories)])
    if width == 0:
        return None
    if not categories:
        categories = [str(i + 1) for i in range(width)]
    rows = []
    for i, category in enumerate(categories[:width]):
        row = [category]
        for s in series:
            value = s["values"][i] if i < len(s["values"]) else None
            row.append("" if value is None else (f"{value:g}"))
        rows.append(row)
    header = ["类别"] + [s["name"] for s in series]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    title = str(raw.get("title") or fallback_title or "未命名图表").strip()[:120]
    return {
        "page": page, "chart_type": chart_type, "title": title,
        "x_label": str(raw.get("x_label") or "")[:80],
        "y_label": str(raw.get("y_label") or "")[:80],
        "unit": str(raw.get("unit") or "")[:40],
        "categories": categories[:width],
        "series": series,
        "confidence": min(1.0, max(0.0, float(raw.get("confidence") or 0.75))),
        "markdown": "\n".join(lines),
        "data": {"chart_type": chart_type, "title": title, "x_label": raw.get("x_label") or "",
                 "y_label": raw.get("y_label") or "", "unit": raw.get("unit") or "",
                 "categories": categories[:width], "series": series},
    }


def _chart_prompt(page_no: int) -> str:
    return (
        f"你是严谨的图表数据提取引擎。分析第 {page_no} 页图片中的图表，只提取图中可读数据，不猜测。"
        "如果没有图表（包括纯表格、纯文字），返回 {\"is_chart\": false}。"
        "如果有图表，只输出一个 JSON 对象，格式："
        '{"is_chart":true,"chart_type":"bar|line|pie|scatter|area|mixed|other",'
        '"title":"图表标题","x_label":"X轴","y_label":"Y轴","unit":"单位",'
        '"categories":["类别1","类别2"],"series":[{"name":"系列名","values":[1,2]}],'
        '"points":[{"x":"类别","y":1,"series":"系列名"}],"confidence":0.0}'
        "。values 与 categories 对齐；无法读出的数值用 null；不要输出 Markdown 代码块、不要解释。"
    )


def extract_chart_blocks(path: Path, page_texts: list[str]) -> list[dict]:
    """用视觉模型提取图表数据；表格/正文返回 is_chart=false，不生成节点。"""
    from . import llm, settings
    if not settings.get_bool("chart.enabled", True) or not llm.available():
        return []
    try:
        suffix = path.suffix.lower()
        is_image = suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
        doc = pymupdf.open(str(path))
        max_pages = max(1, min(30, settings.get_int("chart.max_pages", 6)))
        dpi = max(90, min(300, settings.get_int("chart.dpi", 140)))
        candidates: list[int] = [0] if is_image else []
        if not is_image:
            for idx, page in enumerate(doc):
                text = page_texts[idx] if idx < len(page_texts) else ""
                has_caption = bool(re.search(r"(图|Figure|Fig\.?)\s*\d+", text or "", re.I))
                large = False
                for img in page.get_images(full=True):
                    for rect in page.get_image_rects(img[0]):
                        if rect.width > page.rect.width * 0.35 and rect.height > page.rect.height * 0.08:
                            large = True
                            break
                    if large:
                        break
                if has_caption or large:
                    candidates.append(idx)
        candidates = candidates[:max_pages]
        client_model = settings.get("chart.model") or settings.get("ocr.model") or None
        blocks: list[dict] = []
        for idx in candidates:
            if idx >= len(doc):
                continue
            try:
                page = doc[idx]
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                data_url = "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()
                raw = llm.chat_json(
                    [{"role": "system", "content": "你是图表数据提取引擎，只输出合法 JSON。"},
                     {"role": "user", "content": [
                         {"type": "text", "text": _chart_prompt(idx + 1)},
                         {"type": "image_url", "image_url": {"url": data_url}},
                     ]}],
                    kind="chart_extract", model=client_model, json_mode=True,
                    max_tokens=2500, temperature=0.1,
                )
                fallback = "图表"
                if idx < len(page_texts):
                    m = re.search(r"((?:图|Figure|Fig\.?)\s*\d+[^\n]{0,100})", page_texts[idx] or "", re.I)
                    if m:
                        fallback = m.group(1).strip()
                block = _normalise_chart(raw, idx + 1, fallback)
                if block:
                    blocks.append(block)
            except Exception:
                continue
        doc.close()
        return blocks
    except Exception:
        return []


def attach_chart_nodes(nodes: list[NodeDraft], chart_blocks: list[dict]) -> dict:
    """把图表数据挂到最近章节；无题注图表生成 chart 节点。"""
    def walk(items):
        out = []
        for item in items:
            out.append(item)
            out.extend(walk(item.children))
        return out

    all_nodes = walk(nodes)
    sections = [n for n in all_nodes if n.node_type == "section"]
    for idx, block in enumerate(chart_blocks, 1):
        candidates = [s for s in sections if s.page_start <= block["page"] <= s.page_end] or sections
        parent = min(candidates, key=lambda s: abs(s.page_start - block["page"])) if candidates else None
        ChartNode = NodeDraft(
            node_id=new_id("nd"), parent_id=parent.node_id if parent else None,
            level=(parent.level + 1) if parent else 1, node_type="chart",
            order_no=idx, title=block.get("title") or f"图表 {idx}",
            text=(block.get("markdown", "") + "\n\n```chart-data\n" + json.dumps(block.get("data", {}), ensure_ascii=False) + "\n```"),
            page_start=block["page"], page_end=block["page"], confidence=block.get("confidence", 0.75),
            path=(parent.path if parent else "/"),
        )
        nodes.append(ChartNode)
    return {"chart_count": len(chart_blocks), "chart_data_count": sum(len(b.get("series", [])) for b in chart_blocks),
            "chart_model": "vision"}


def attach_table_and_formula_nodes(nodes: list[NodeDraft], table_blocks: list[dict], formula_blocks: list[dict]) -> dict:
    """把表格单元格文本挂到表格节点，把公式文本挂成章节下的公式节点。"""
    def walk(items):
        out = []
        for item in items:
            out.append(item)
            out.extend(walk(item.children))
        return out

    all_nodes = walk(nodes)
    table_nodes = [n for n in all_nodes if n.node_type == "table"]
    for block, node in zip(table_blocks, table_nodes):
        node.text = (node.text + "\n\n" + block["markdown"]).strip()
        node.confidence = min(1.0, node.confidence + 0.05)
    sections = [n for n in all_nodes if n.node_type == "section"]
    # OCR/图片表格没有“表 x”题注时，补建 table 节点，避免表格文本只停留在页面文本层。
    for idx, block in enumerate(table_blocks[len(table_nodes):], start=len(table_nodes) + 1):
        candidates = [s for s in sections if s.page_start <= block["page"] <= s.page_end] or sections
        parent = min(candidates, key=lambda s: abs(s.page_start - block["page"])) if candidates else None
        TableNode = NodeDraft(
            node_id=new_id("nd"), parent_id=parent.node_id if parent else None,
            level=(parent.level + 1) if parent else 1, node_type="table",
            order_no=idx, title=f"表格 {idx}", text=block["markdown"],
            page_start=block["page"], page_end=block["page"], confidence=0.86,
            path=(parent.path if parent else "/"),
        )
        nodes.append(TableNode)
        table_nodes.append(TableNode)
    for idx, block in enumerate(formula_blocks, 1):
        candidates = [s for s in sections if s.page_start <= block["page"] <= s.page_end] or sections
        parent = min(candidates, key=lambda s: abs(s.page_start - block["page"])) if candidates else None
        FormulaNode = NodeDraft(
            node_id=new_id("nd"), parent_id=parent.node_id if parent else None,
            level=(parent.level + 1) if parent else 1, node_type="formula",
            order_no=idx, title=f"公式 {idx}", text=block["text"], page_start=block["page"],
            page_end=block["page"], confidence=0.82, path=(parent.path if parent else "/"),
        )
        nodes.append(FormulaNode)
    return {"table_blocks": len(table_blocks), "table_cells": sum(b.get("cells", 0) for b in table_blocks),
            "formula_count": len(formula_blocks)}


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------
def parse_document(path: Path, doc_type_hint: str = "auto", title_hint: str | None = None) -> ParsedDoc:
    pages, page_texts, page_count = read_document(path)
    pages, page_texts, ocr_info = ocr_pdf_pages(path, pages, page_texts)
    table_blocks = extract_table_blocks(path, page_texts)
    chart_blocks = extract_chart_blocks(path, page_texts)
    formula_blocks = extract_formula_blocks(path)
    body_size = detect_body_size(pages)
    title, title_page, title_line = detect_title_layout(pages, body_size)
    if title_hint:
        title = title_hint
    nodes, meta = build_structure(pages, title, title_page, title_line, body_size, "other")
    section_titles = [n.title for n in nodes if n.node_type == "section"]
    doc_type = detect_doc_type(title, meta["abstract"], section_titles, page_count) if doc_type_hint in ("", "auto", None) else doc_type_hint
    # 报道类的导语处理依赖 doc_type；文体重建一次，保证首节吸收导语。
    if doc_type == "news":
        nodes, meta = build_structure(pages, title, title_page, title_line, body_size, doc_type)
    structure_stats = attach_table_and_formula_nodes(nodes, table_blocks, formula_blocks)
    chart_stats = attach_chart_nodes(nodes, chart_blocks)
    stats = meta["stats"]
    stats.update(ocr_info)
    stats.update(structure_stats)
    stats.update(chart_stats)
    stats["table_count"] = max(stats.get("table_count", 0), len(table_blocks))
    stats["chart_count"] = max(stats.get("chart_count", 0), len(chart_blocks))
    quality = quality_gate(page_texts, stats, title, meta["abstract"])
    text_chars = sum(len(re.sub(r"\s", "", t)) for t in page_texts)
    return ParsedDoc(
        title=title,
        doc_type=doc_type,
        page_count=page_count,
        authors=meta["authors"],
        abstract=meta["abstract"],
        keywords=meta["keywords"],
        nodes=nodes,
        stats=stats,
        quality=quality,
        text_chars=text_chars,
        page_texts=page_texts,
    )
