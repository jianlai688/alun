# ================================================================================
# 【文件导读】parser.py —— 文档解析流水线的核心模块
# --------------------------------------------------------------------------------
# 职责：
#   把用户上传的 PDF / DOCX / Markdown / 纯文本（以及图片）解析成统一的应用内部结构：
#   “页 -> 文本行(Line) -> 标题(Heading) -> 结构树节点(NodeDraft)”，同时提取题名、
#   作者、摘要、关键词、参考文献、表格、公式、图表，并生成质量报告(Quality Gate)。
# 在整个应用中的位置：
#   上传接口/后台任务先保存原始文件，再调用本文件的 parse_document()；解析结果由
#   pipeline 等模块写入数据库、切分知识块，并交给生成器/阅读器使用。
# 主要调用关系：
#   parse_document()
#     ├─ read_document()            按扩展名选择 PDF/DOCX/Markdown 读取器
#     ├─ ocr_pdf_pages()            文本层太薄时调用多模态模型补 OCR
#     ├─ extract_table_blocks()     PDF/DOCX 表格 -> Markdown
#     ├─ extract_chart_blocks()     视觉模型识别图表 -> JSON + Markdown
#     ├─ extract_formula_blocks()   公式字体/OMML -> 公式节点
#     ├─ detect_body_size()         估计正文字号
#     ├─ detect_title_layout()      猜题名所在行与起始页
#     └─ build_structure()          还原章节树与正文
#   本模块依赖 pymupdf（PDF）、python-docx（DOCX，惰性导入）、app.llm（视觉能力）、
#   app.settings（开关与模型参数）以及 app.db.new_id（生成稳定前缀的节点 ID）。
# 阅读建议：
#   先看 Line/NodeDraft 两个数据类，再看 read_document -> reading_order ->
#   heading_of -> build_structure 的主链路，最后看 quality_gate 的评分口径。
# ================================================================================
"""文档解析：PDF/DOCX/Markdown → 结构树 + 质量报告。

实现遵循设计文档第 5 章：
- 文本层优先，乱码/低覆盖触发质量门（首期不内置 OCR，标记 needs_review）
- 双栏阅读顺序还原（全宽元素分段，段内先左栏后右栏）
- 标题编号 + 字号字重 + 标签词识别
- 图表题注识别并挂到所属章节
- 结构树节点稳定 ID、页码、字符数、置信度
"""
# 启用“延迟解析类型注解”：让 `Line | None` 这类新语法在旧版 Python 中也能作为注解使用，且避免运行时求值开销。
from __future__ import annotations

# 标准库：把二进制图片编码成 base64，OCR/图表识别时拼成 data URL 发给视觉模型。
import base64
# 标准库：序列化图表数据，写入节点正文中的 ```chart-data 代码块。
import json
# 标准库：正则表达式，本文件大量用于标题、编号、页眉页脚和表格识别。
import re
# 标准库：Unicode 规范化（NFKC），统一全角/半角字符，减少“看似不同”的匹配失败。
import unicodedata
# 标准库：dataclass 自动生成初始化方法；field 用于给可变默认值（列表）提供工厂。
from dataclasses import dataclass, field
# 标准库：Path 统一处理跨平台路径，并提供后缀名、读写文本等便捷方法。
from pathlib import Path
# 类型提示：Iterable 表示“可迭代对象”，用于递归遍历节点树。
from typing import Iterable

# 第三方 PDF 库 PyMuPDF：读取页面、文字块、坐标、图片和表格。
import pymupdf

# 项目内相对导入：new_id 生成带业务前缀的随机 ID（如 node_xxx），保证节点主键唯一稳定。
from .db import new_id

# CJK 正则片段：匹配常用中日韩统一表意文字（此处主要判断中文）；后续用 f-string/拼接复用。
CJK = r"[\u4e00-\u9fff]"
# 数字字符类片段：同时覆盖半角 0-9 与全角 ０-９，供正则表达式复用。
NUM = r"0-9０-９"


# 把任意来源的文本清洗成统一形式，是后续所有匹配的“第一道闸门”。
# 为什么需要：PDF 提取出的文字常有全角空格、不间断空格、控制字符和重复空白；
# 不先归一化，标题编号、关键词分隔符等规则会大量漏判。
def norm(text: str) -> str:
    # None 安全：text 可能为 None；NFKC 会把全角字母/数字/标点转成半角兼容形式。
    text = unicodedata.normalize("NFKC", text or "")
    # 把不间断空格(U+00A0)和中文全角空格(U+3000)统一成普通空格。
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    # 删除除换行/制表符以外的 C0 控制字符，避免不可见字符污染数据库与前端展示。
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    # 把连续空格/制表符合并为一个空格，保留换行（换行对段落判断有意义）。
    text = re.sub(r"[ \t]+", " ", text)
    # 去掉首尾空白；返回新字符串，不修改调用方对象。
    return text.strip()


# Line 是解析阶段的“原子单位”：一个 PDF 文本行或 DOCX/OCR 的一段，统一承载文字、页码、字号、粗体和包围盒。
@dataclass
# dataclass 自动生成 __init__/__repr__，让下面这些字段可以按位置或关键字初始化。
class Line:
    # 行文本（已经过 norm 清洗；保留原始语义，不再含多余空白）。
    text: str
    # 该行所在的 1-based 页码，用于页码跨度与图表挂载。
    page: int
    # 该行最大字号（磅），是判断标题层级的重要信号。
    size: float
    # 是否粗体（来自 PDF flags 或 Word 样式）。
    bold: bool
    # 包围盒左上角 x 坐标（PDF 坐标原点在左上，单位通常为 pt）。
    x0: float
    # 包围盒左上角 y 坐标。
    y0: float
    # 包围盒右下角 x 坐标。
    x1: float
    # 包围盒右下角 y 坐标。
    y1: float
    # 行宽 = x1 - x0；默认 0，便于单栏/双栏判断。
    width: float = 0.0
    # 行的角色，默认 body；后续可扩展为 title/header 等，当前主要保留字段。
    role: str = "body"


# Heading 是 Line 的“升级版”：在行信息外增加标题层级、展示标题和特殊类型。
@dataclass
# 原始 Line，保留坐标/字号，供后续合并、切正文使用。
class Heading:
    # 标题层级：1 为一级，最大限制到 4。
    line: Line
    # 规范化后的标题文本（编号与文字之间可能补空格）。
    level: int
    # 特殊标题类型：abstract/keywords/reference/bio/section；普通章节为 None。
    title: str
    special: str | None = None


# NodeDraft 是准备写入数据库的结构树节点草稿；解析阶段先组树，最后统一转行。
@dataclass
# 节点唯一 ID（由 new_id 生成）。
class NodeDraft:
    # 父节点 ID；顶层节点为 None。
    node_id: str
    # 层级（1 起），与 Heading.level 对应。
    parent_id: str | None
    # 节点类型：title/author/abstract/keywords/section/figure/table/chart/formula/bio/reference。
    level: int
    # 同级排序号；后面 assign() 会按最终顶层顺序重排。
    node_type: str
    # 节点标题（章节名、图表题注等）。
    order_no: int
    # 节点正文，默认空字符串；章节正文由 _join_paragraph 填入。
    title: str
    # 起始页，默认 1。
    text: str = ""
    # 结束页，默认 1；父节点会递归扩展到子树最大页。
    page_start: int = 1
    # 置信度 0~1，用于前端/质量报告标识把握程度。
    page_end: int = 1
    # 树路径，形如 /1/2/；顶层默认 /，便于按前缀查询子树。
    confidence: float = 1.0
    # 子节点列表；default_factory 保证每个实例拿到独立列表，避免可变默认值共享。
    path: str = "/"
    # 正文分页跨度列表，元素为 (起始字符偏移, 结束字符偏移, 页码)，用于原文定位。
    children: list["NodeDraft"] = field(default_factory=list)
    page_spans: list[tuple[int, int, int]] = field(default_factory=list)

    # 把内存中的节点转换成数据库行（dict）；doc_id/order_index 由调用方补充。
    def to_row(self, doc_id: str, order_index: int) -> dict:
        # 返回的键名与数据库列名保持一致，便于直接 **row 入库。
        return {
            # 节点自身 ID。
            "node_id": self.node_id,
            # 所属文档 ID。
            "doc_id": doc_id,
            # 父节点 ID（可为 None）。
            "parent_id": self.parent_id,
            # 层级。
            "level": self.level,
            # 节点类型。
            "node_type": self.node_type,
            # 同级排序号。
            "order_no": self.order_no,
            # 标题。
            "title": self.title,
            # 正文。
            "text": self.text,
            # 起始页。
            "page_start": self.page_start,
            # 结束页。
            "page_end": self.page_end,
            # PDF 包围盒当前未按节点保存，统一写 None。
            "bbox": None,
            # 置信度。
            "confidence": self.confidence,
            # 树路径。
            "path": self.path,
            # 字符数：对空文本安全，len('')=0；数据库可用于统计。
            "char_count": len(self.text or ""),
            # 文档内全局顺序号（扁平遍历序号）。
            "order_index": order_index,
        }


# ParsedDoc 是 parse_document 的最终返回对象，聚合“元数据 + 结构树 + 质量报告”。
@dataclass
# 文档题名（可能来自版式识别或调用方提示）。
class ParsedDoc:
    # 文档类型：research_paper/review/policy/news/other 等。
    title: str
    # 总页数。
    doc_type: str
    # 作者与单位（已清理的展示字符串）。
    page_count: int
    # 摘要纯文本。
    authors: str
    # 关键词列表。
    abstract: str
    # 顶层节点列表；子节点通过 NodeDraft.children 递归挂载。
    keywords: list[str]
    # 统计字典：节点数、图表数、参考文献数、OCR 页数等。
    nodes: list[NodeDraft]
    # 质量门结果：score/level/issues/metrics/failed_pages。
    stats: dict
    # 去空白后的总字符数，用于展示与覆盖率参考。
    quality: dict
    # 逐页原始文本，供检索、质量检查和图表题注回退使用。
    text_chars: int
    page_texts: list[str] = field(default_factory=list)


# ========== 一、读取层：把不同格式统一成 pages / texts / page_count ==========
# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
# 把 PyMuPDF `get_text('dict')` 返回的一条 line 字典转换为内部 Line。
# 为什么要额外过滤：PDF 的视觉行经常被拆成多个 span，可能包含空白、零宽或无效坐标；
# 这里先合并 span，再做尺寸/宽度校验，避免后续把空行当标题。
def _line_from_dict(raw: dict, page_no: int, page_width: float) -> Line | None:
    # 只保留 strip 后非空的 span；span 是 PDF 中同一字体/字号的一段连续文字。
    spans = [s for s in raw.get("spans", []) if s.get("text", "").strip()]
    # 没有任何可见 span 说明该行是空白占位，直接丢弃。
    if not spans:
        # 没有可见内容时返回 None，调用方会 continue。
        return None
    # 把所有 span 文字拼接后统一清洗；span 之间不额外加空格，因为 PDF 已用坐标控制间距。
    text = norm("".join(s.get("text", "") for s in spans))
    # 清洗后仍为空（例如只有控制字符）也丢弃。
    if not text:
        return None
    # 取本行最大字号作为代表字号；float 转换失败会抛出 ValueError，由外层读取流程处理。
    size = max(float(s.get("size", 0)) for s in spans)
    # PyMuPDF 的 flags 是位掩码：bit 4（数值 16）表示粗体，任一 span 粗体即视为粗体行。
    # PyMuPDF flags: bit 4 (16) 为粗体
    bold = any(int(s.get("flags", 0)) & 16 for s in spans)
    # 读取包围盒；缺省用 (0,0,0,0)，避免解包失败。
    x0, y0, x1, y1 = raw.get("bbox", (0, 0, 0, 0))
    # 宽度小于等于 0.5pt 的行通常是竖排伪影或空框，跳过。
    if x1 - x0 <= 0.5:
        return None
    # 按 Line 的字段顺序构造对象；最后一个参数是行宽。
    return Line(text, page_no, size, bold, x0, y0, x1, y1, x1 - x0)


# 读取一页里的所有有效文本行，并做页眉/页脚和纯数字过滤。
def _page_lines(page: pymupdf.Page, page_no: int) -> list[Line]:
    # 页面宽度用于判断行宽比例（单栏/双栏）以及后续阅读顺序。
    width = float(page.rect.width)
    # 页面高度用于按相对位置过滤页眉页脚。
    height = float(page.rect.height)
    # 准备本页的行列表。
    lines: list[Line] = []
    # get_text('dict') 返回块结构；blocks 可能缺失，所以 get(..., []) 兜底。
    for block in page.get_text("dict").get("blocks", []):
        # type != 0 表示图片块而不是文字块，直接跳过。
        if block.get("type") != 0:
            # 一个 block 内可能有多行。
            continue
        # 逐行转换成内部 Line。
        for raw in block.get("lines", []):
            # 转换失败/空行时跳过。
            line = _line_from_dict(raw, page_no, width)
            # 空行不参与后续识别。
            if not line:
                continue
            # 页面顶部 3.5% 和底部 3.5% 视为页边区域，先粗过滤页眉页脚。
            if line.y1 < height * 0.035 or line.y0 > height * 0.965:
                continue
            # 原注释：纯数字行不再一律丢弃，章节号可能独占一行。
            # 纯数字行不再一律丢弃：不少期刊把章节号单独排成一行（如“1”下一行才是标题）。
            # 只过滤确实位于页眉/页脚区域的纯数字。
            # 只删除确实位于页眉/页脚区域的纯数字，正文中的章节号保留给标题合并步骤。
            # 匹配可带破折号/空格的 1~4 位纯数字行。
            if re.fullmatch(r"[-—–\s]*\d{1,4}[-—–\s]*", line.text):
                # 若该数字行在顶部 6% 或底部 6%，按页眉页脚丢弃。
                if line.y1 < height * 0.06 or line.y0 > height * 0.94:
                    continue
            # 保留正文中的数字行（可能是章节号）。
            lines.append(line)
    # 返回本页有效行。
    return lines


# 读取 PDF：返回（每页行列表、每页纯文本、总页数），随后再做跨页页眉页脚清理。
def _read_pdf(path: Path) -> tuple[list[list[Line]], list[str], int]:
    # 打开 PDF；调用方负责确保 path 存在且格式正确。
    doc = pymupdf.open(path)
    # 每页的 Line 列表。
    pages: list[list[Line]] = []
    # 每页的纯文本，作为质量检查/图表题注回退。
    texts: list[str] = []
    # 按文档顺序遍历页面；idx 从 0 开始，page_no 从 1 开始。
    for idx, page in enumerate(doc):
        # 抽取本页有效行。
        pages.append(_page_lines(page, idx + 1))
        # 同时保留 PyMuPDF 原生纯文本（未做行过滤），用于覆盖率和 OCR 判断。
        texts.append(page.get_text("text"))
    # 总页数。
    count = doc.page_count
    # 尽早关闭文件句柄，避免 Windows 上文件被占用。
    doc.close()

    # 原注释：页眉页脚只删除位于页面上下边缘且跨页重复的行。
    # 页眉页脚：仅删除位于页面上下边缘、且跨页重复的行（避免误删题名）
    # 统计每种“去数字后”的行文本出现页数；数字替换为 # 以容忍页码变化。
    freq: dict[str, int] = {}
    # 遍历每页。
    for lines in pages:
        # seen 保证同一页同一文本只计一次（防止页内重复行抬高频率）。
        seen = set()
        # 遍历该页文本行。
        for ln in lines:
            # 把数字替换成 #，例如“第 3 页”与“第 4 页”归为同一模式。
            key = re.sub(r"\d+", "#", ln.text)
            # 首次在本页出现才增加计数。
            if key not in seen:
                # 累计跨页出现次数。
                freq[key] = freq.get(key, 0) + 1
                # 标记本页已见。
                seen.add(key)
    # 阈值取 3 与“页数一半”的较大值：短文档至少重复 3 页才算页眉页脚。
    threshold = max(3, int(len(pages) * 0.5))
    # 重复且长度不超过 40 的模式才可能是页眉页脚，避免误删正文。
    repeated = {k for k, v in freq.items() if v >= threshold and len(k) <= 40}
    # 清理后的页列表。
    cleaned: list[list[Line]] = []
    # 逐页清理。
    for lines in pages:
        # 暂存本页保留的行。
        kept = []
        # 逐行判断。
        for ln in lines:
            # 同样把数字归一化后再匹配重复模式。
            key = re.sub(r"\d+", "#", ln.text)
            # y 坐标位于顶部 90pt 或底部 760pt，视为页面边缘；这是面向常见 A4 的启发式。
            at_edge = ln.y1 < 90 or ln.y0 > 760
            # 同时满足“重复模式 + 长度短 + 在边缘”才删除。
            if key in repeated and len(ln.text) <= 40 and at_edge:
                # 原注释：纯数字可能是章节号，不按页眉页脚删除。
                # 纯数字可能是章节号（如“1”单独成行），不按页眉页脚删除
                # 若整行是 1~4 位纯数字。
                if re.fullmatch(r"\d{1,4}", ln.text):
                    # 保留该行。
                    kept.append(ln)
                    # 继续下一行。
                    continue
                # 其余重复页眉页脚行丢弃。
                continue
            # 正常行保留。
            kept.append(ln)
        # 收集本页结果。
        cleaned.append(kept)
    # 返回清理后的页行、原始页文本、页数。
    return cleaned, texts, count


# 读取 DOCX：把段落和表格线性化为第 1 页上的 Line；DOCX 无固定分页信息，统一记为 1 页。
def _read_docx(path: Path) -> tuple[list[list[Line]], list[str], int]:
    # 惰性导入 python-docx：只有真正读取 DOCX 时才需要该依赖，降低模块导入成本。
    from docx import Document

    # 打开 Word 文档。
    document = Document(path)
    # 收集所有段落/表格生成的 Line。
    lines: list[Line] = []
    # 收集文本片段，最后拼成“第 1 页”的文本。
    texts: list[str] = []
    # 用递增 y 模拟纵向排版，让后续按 y 排序的算法可复用。
    y = 0.0
    # 遍历正文段落（不包含页眉页脚、文本框等）。
    for para in document.paragraphs:
        # 清洗段落文字。
        text = norm(para.text)
        # 空段落跳过。
        if not text:
            # 跳过空段后，不把空行计入文本。
            continue
        # 非空段落加入页面文本。
        texts.append(text)
        # 读取 Word 段落样式名并转小写；不同语言版本的样式名可能不同。
        style = (para.style.name or "").lower()
        # 默认正文 12pt。
        size = 12.0
        # 默认非粗体。
        bold = False
        # Heading 1 / 标题 1 映射为 16pt 粗体。
        if "heading 1" in style or style == "标题 1":
            # 同时兼容英文与中文样式名。
            size, bold = 16.0, True
        # Heading 2 / 标题 2。
        elif "heading 2" in style or style == "标题 2":
            # 14pt 粗体。
            size, bold = 14.0, True
        # Heading 3 / 标题 3。
        elif "heading 3" in style or style == "标题 3":
            # 12.5pt 粗体。
            size, bold = 12.5, True
        # Title 样式映射为最大字号，便于题名识别。
        elif "title" in style:
            # 20pt 粗体。
            size, bold = 20.0, True
        # 每段占 20pt 的虚拟高度。
        y += 20
        # 生成 Line；x 固定 40~555，宽度 515，模拟正文栏宽。
        lines.append(Line(text, 1, size, bold, 40, y, 555, y + 16, 515))
    # 再把表格转成 Markdown，作为额外文本行。
    for table in document.tables:
        # 逐表格、逐行、逐单元格清洗文本。
        rows = [[norm(cell.text) for cell in row.cells] for row in table.rows]
        # 转成 Markdown 表格；行列不齐时 _table_markdown 会补齐。
        md = _table_markdown(rows)
        # 无效表格（少于 2 行）跳过。
        if not md:
            # 表格 Markdown 加入页面文本，供检索和表格抽取。
            continue
        # 逐行生成 Line，让表格内容也能参与后续结构分析。
        texts.append(md)
        # 全空行跳过。
        for row in rows:
            # 空行没有可用内容。
            if not any(row):
                # 虚拟 y 递增。
                continue
            # 把单元格用 | 拼接成一行，字号 10、非粗体。
            y += 20
            # 生成表格行。
            lines.append(Line(" | ".join(row), 1, 10.0, False, 40, y, 555, y + 16, 515))
    # DOCX 永远只有 1 页；texts 用换行连接所有段落/表格。
    return [lines], ["\n".join(texts)], 1


# 读取 Markdown/纯文本：按行解析标题井号，其他行按正文处理。
def _read_markdown(path: Path) -> tuple[list[list[Line]], list[str], int]:
    # UTF-8 读取，errors='ignore' 容忍个别非法字节，避免整篇失败。
    text = path.read_text(encoding="utf-8", errors="ignore")
    # 收集 Line。
    lines: list[Line] = []
    # 虚拟 y 坐标。
    y = 0.0
    # 逐行遍历原始文本。
    for raw in text.splitlines():
        # 清洗当前行。
        t = norm(raw)
        # 空行跳过。
        if not t:
            # 默认正文 11pt、非粗体。
            continue
        # 识别 1~4 个 # 开头的 Markdown 标题。
        size, bold = 11.0, False
        # m 为匹配对象或 None。
        m = re.match(r"^(#{1,4})\s+(.*)$", t)
        # 匹配成功：按井号数量映射字号。
        if m:
            # 井号越多字号越小；4 级标题 11.5pt。
            size = {1: 18.0, 2: 15.0, 3: 13.0, 4: 11.5}[len(m.group(1))]
            # 标题加粗并去掉井号，只保留标题文字。
            bold, t = True, norm(m.group(2))
        # 虚拟 y 递增。
        y += 20
        # 生成 Line。
        lines.append(Line(t, 1, size, bold, 40, y, 555, y + 16, 515))
    # 返回单页结构；page_texts 保留原始 Markdown（含井号），便于检索原文。
    return [lines], [text], 1


# 统一读取入口：按扩展名分派，是 parse_document 的第一步。
def read_document(path: Path) -> tuple[list[list[Line]], list[str], int]:
    # 取小写后缀，兼容 .PDF/.Docx 等大小写混写。
    suffix = path.suffix.lower()
    # PDF 走 PyMuPDF 读取器。
    if suffix == ".pdf":
        # 直接返回三元组。
        return _read_pdf(path)
    # DOCX 走 python-docx 读取器。
    if suffix == ".docx":
        # 直接返回。
        return _read_docx(path)
    # Markdown/纯文本共用按行读取器。
    if suffix in (".md", ".markdown", ".txt"):
        # 直接返回。
        return _read_markdown(path)
    # 图片没有文本层，返回空页；后续 OCR 会补文本。
    if suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
        # 空行列表 + 空文本 + 1 页。
        return [[]], [""], 1
    # 未知后缀显式报错，错误信息直接给用户看。
    raise ValueError(f"暂不支持的文件类型：{suffix}")


# 把 OCR 得到的一整页纯文本拆成近似版式的 Line，供标题/段落算法复用。
def _ocr_lines(text: str, page_no: int) -> list[Line]:
    """把 OCR 纯文本还原成近似版式行，供标题/段落识别复用。"""
    # 输出行列表。
    out: list[Line] = []
    # 从页面上方开始模拟 y 坐标。
    y = 42.0
    # 按行遍历 OCR 文本；idx 用于判断首页第一行。
    for idx, raw in enumerate((text or "").splitlines()):
        # 清洗每一行。
        t = norm(raw)
        # 空行跳过。
        if not t:
            # 默认 OCR 正文字号。
            continue
        # 默认非粗体。
        size = 10.5
        # 识别“1.”“一、”“第 1 章”等编号开头，作为标题信号。
        bold = False
        # 若匹配，放大字号并加粗。
        if re.match(r"^\s*(第?[0-9一二三四五六七八九十]+([.．][0-9]+)*[、.．\s])", t):
            # 首页第一行通常是题名，单独放大。
            size, bold = 13.5, True
        # 首页首行按题名处理。
        elif idx == 0 and page_no == 1:
            # 非首行保持正文样式。
            size, bold = 16.0, True
        # 生成 Line；宽度固定 514，y 依字号递增。
        out.append(Line(t, page_no, size, bold, 42.0, y, 556.0, y + size * 1.5, 514.0))
        # 按字号增加行距，模拟真实排版。
        y += size * 1.8
    # 返回近似行列表。
    return out


# 调用视觉模型对单页 PNG 做 OCR，返回纯文本；本函数只负责拼请求。
def _ocr_page_png(png: bytes, page_no: int, page_count: int) -> str:
    # 惰性导入 llm（聊天模型客户端）和 settings（配置读取），避免循环依赖/启动开销。
    from . import llm, settings

    # 构造 OCR 提示词：明确阅读顺序、表格转 Markdown、禁止总结。
    prompt = (
        f"请逐字识别第 {page_no}/{page_count} 页图片中的全部文字，按阅读顺序输出纯文本。"
        "保留标题编号、段落换行、表格单元格和公式符号；不要总结、不要解释、不要加 Markdown 代码块。"
        "如果识别到表格，请用 Markdown 表格输出，第一行是表头，每个单元格一行一列，保留所有表头和行；"
        "如果是柱状图/折线图/流程图等图表，请输出图表标题、坐标轴名称、图例、节点或数据标签等可辨认文字；"
        "如果页面是纯图片，请按阅读顺序输出图中文字。"
    )
    # 把 PNG 字节编码为 data URL，视觉模型接口支持直接传 base64。
    data_url = "data:image/png;base64," + base64.b64encode(png).decode()
    # 调用统一聊天接口；返回模型生成的纯文本。
    return llm.chat(
        # messages 采用 OpenAI 兼容格式：system 约束角色，user 携带文字 + 图片。
        [
            # system 再次强调只抄录、不补充外部知识，降低幻觉。
            {"role": "system", "content": "你是严谨的文档 OCR 引擎，只输出从图片中看到的文字，不补充图片外知识。"},
            # user content 是分段列表，可同时包含文本和图片。
            {"role": "user", "content": [
                # 文本指令。
                {"type": "text", "text": prompt},
                # 图片以 image_url 形式传入。
                {"type": "image_url", "image_url": {"url": data_url}},
            # data URL 前缀 + base64 图片。
            ]},
        ],
        # kind 用于计费/日志分类。
        kind="ocr_page",
        # 优先使用配置的 OCR 模型；空字符串转 None 表示用客户端默认模型。
        model=settings.get("ocr.model") or None,
        # OCR 不需要 JSON 模式。
        json_mode=False,
        # 限制输出长度，防止超长页面拖垮上下文。
        max_tokens=3000,
        # 温度 0，尽量确定性抄录，减少自由发挥。
        temperature=0.0,
    )


# 对文本层很薄的 PDF/图片执行 OCR；失败时绝不破坏原数据，返回原 pages。
def ocr_pdf_pages(path: Path, pages: list[list[Line]], page_texts: list[str]) -> tuple[list[list[Line]], list[str], dict]:
    # 这是“增强”步骤，任何异常都应被吞掉并降级，不能阻断整篇解析。
    """对文本层很薄的 PDF/图片调用图片识别模型；失败时保持原文本层。"""
    # 尝试导入依赖并执行。
    try:
        # 惰性导入，便于测试时替换。
        from . import llm, settings

        # OCR 开关关闭或模型不可用时，直接返回原结果，并报告 0 页。
        if not settings.get_bool("ocr.enabled", True) or not llm.available():
            # 返回原页行/文本 + 统计。
            return pages, page_texts, {"ocr_pages": 0}
        # 判断文件类型。
        suffix = path.suffix.lower()
        # 图片类后缀走单页 OCR。
        is_image = suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
        # 既不是 PDF 也不是图片，直接跳过。
        if suffix != ".pdf" and not is_image:
            # 返回原结果。
            return pages, page_texts, {"ocr_pages": 0}
        # 没有页面文本可判断，跳过。
        if not page_texts:
            # 返回原结果。
            return pages, page_texts, {"ocr_pages": 0}
        # 统计每页去空白字符数，作为“文本层厚薄”的指标。
        lengths = [len(re.sub(r"\s", "", t or "")) for t in page_texts]
        # 页数。
        page_count = len(lengths)
        # PDF 才做整体覆盖率判断；图片必然需要 OCR。
        if not is_image:
            # 平均每页字符数；max(1, ...) 防止空列表除零。
            avg = sum(lengths) / max(1, page_count)
            # 从配置读取每页最低字符阈值，默认 450。
            min_chars = settings.get_int("ocr.min_chars_per_page", 450)
            # 统计“几乎空白”（少于 40 字）的页数。
            blank = sum(1 for n in lengths if n < 40)
            # 平均字符达标且空白页比例低，认为文本层可用，不做 OCR。
            if avg >= min_chars and blank < max(1, page_count // 3):
                # 返回原结果。
                return pages, page_texts, {"ocr_pages": 0}
        # 限制 OCR 页数：图片 1 页；PDF 默认最多 20 页，硬上限 60。
        max_pages = 1 if is_image else max(1, min(60, settings.get_int("ocr.max_pages", 20)))
        # DPI 限制在 90~180，兼顾识别率与耗时。
        dpi = max(90, min(180, settings.get_int("ocr.dpi", 120)))
        # 打开 PDF；图片也会被 PyMuPDF 当单页文档打开。
        doc = pymupdf.open(str(path))
        # 候选页：图片固定第 0 页；PDF 按字符数升序选最薄的页。
        candidates = [0] if is_image else sorted(range(page_count), key=lambda i: lengths[i])[:max_pages]
        # PDF 额外寻找“有大型嵌入图片”的页面。
        if not is_image:
            # 原注释：文本层正常但内嵌大图的页面，find_tables 可能找不到表格，需 OCR。
            # 文本层正常但页面里嵌有大图的 PDF，find_tables 往往找不到表格；
            # 记录需要补 OCR 的页索引。
            # 对这类页面补做 OCR，让视觉表格转成 Markdown 表格。
            # 逐页检查。
            image_pages: list[int] = []
            # 遍历文档页。
            for idx, page in enumerate(doc):
                # 尝试检测表格；异常时按无表格处理。
                try:
                    # find_tables 已有表格则不需要 OCR 补表格。
                    if page.find_tables().tables:
                        # 继续下一页。
                        continue
                    # 标记是否存在大型图片。
                    large = False
                    # 遍历页面图片对象。
                    for img in page.get_images(full=True):
                        # 一张图片可能有多个显示区域。
                        for rect in page.get_image_rects(img[0]):
                            # 图片宽超过页宽 45% 且高超过页高 10%，视为大图。
                            if rect.width > page.rect.width * 0.45 and rect.height > page.rect.height * 0.10:
                                # 标记为大图。
                                large = True
                                # 跳出内层图片区域循环。
                                break
                        # 已发现大图，跳出图片对象循环。
                        if large:
                            # 大图且文本不丰富（<1400 字）时，OCR 可能补充图表/表格信息。
                            break
                    # 加入候选页。
                    if large and lengths[idx] < 1400:
                        # 表格检测异常不应中断整页扫描。
                        image_pages.append(idx)
                # 继续下一页。
                except Exception:
                    # 无表格/无大图的异常页直接忽略。
                    continue
            # 合并候选页并去重，按字符数升序取前 max_pages 页。
            candidates = sorted(set(candidates + image_pages), key=lambda i: lengths[i])[:max_pages]
        # 记录成功替换的页数。
        replaced = 0
        # 读取 OCR 模型名，仅用于统计。
        model = settings.get("ocr.model") or ""
        # 逐页 OCR。
        for idx in candidates:
            # 防止候选索引越界（例如页数统计与文档不一致）。
            if idx >= len(doc):
                # 跳过越界页。
                continue
            # 单页异常只跳过该页。
            try:
                # 取当前页。
                page = doc[idx]
                # 渲染为 PNG；alpha=False 去掉透明通道，减小体积。
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                # 调用视觉 OCR 得到纯文本。
                text = _ocr_page_png(pix.tobytes("png"), idx + 1, page_count)
                # 清理模型可能加的开头 Markdown 代码围栏。
                text = re.sub(r"^```[a-zA-Z]*\s*", "", text.strip())
                # 清理结尾的 ``` 围栏和空白。
                text = re.sub(r"\s*```$", "", text).strip()
                # 清理“以下是识别结果：”这类寒暄前缀。
                text = re.sub(r"^(以下(是|为)[^：:\n]*[：:]\s*)", "", text)
                # 识别结果去空白后少于 30 字，视为失败/空白页，不替换。
                if len(re.sub(r"\s", "", text)) < 30:
                    # 跳过该页。
                    continue
                # 原注释：OCR 文本明显更完整才替换。
                # OCR 文本明显更完整时才替换，避免把正常文本层覆盖成更差的识别结果
                # 至少 30 字，且不少于原字符数的 60%，避免越 OCR 越差。
                if len(re.sub(r"\s", "", text)) >= max(30, int(lengths[idx] * 0.6)):
                    # 替换页面纯文本。
                    page_texts[idx] = text
                    # 用 OCR 文本重建近似版式行。
                    pages[idx] = _ocr_lines(text, idx + 1)
                    # 成功页计数 +1。
                    replaced += 1
            # 任何单页异常都降级，不阻断其他页。
            except Exception:
                # 继续下一页。
                continue
        # 所有页处理完后关闭文档。
        doc.close()
        # 返回增强后的页行、页文本和 OCR 统计。
        return pages, page_texts, {"ocr_pages": replaced, "ocr_model": model, "ocr_dpi": dpi}
    # 外层兜底：任何未预期异常都不影响原解析结果。
    except Exception:
        # 返回原始结果 + 0 页 OCR。
        return pages, page_texts, {"ocr_pages": 0}


# ========== 二、阅读顺序：把 PDF 的绝对坐标还原成人类阅读顺序 ==========
# ---------------------------------------------------------------------------
# 阅读顺序
# ---------------------------------------------------------------------------
# 还原一页内文本行的阅读顺序，尤其处理双栏论文。
def reading_order(lines: list[Line], page_width: float) -> list[Line]:
    # 单栏直接按 y（再按 x）排序；双栏则按“分段 + 左栏 + 右栏”输出。
    """还原阅读顺序：单栏按 y 排序；双栏按栏读取，全宽元素作为分段边界。"""
    # 空页直接返回空列表。
    if not lines:
        # 提前返回，避免后续除零/索引问题。
        return []
    # 以页宽 45% 作为左右栏分界线的近似值。
    mid = page_width * 0.45
    # 窄行：宽度小于页宽 45%，双栏正文通常都是窄行。
    narrow = [ln for ln in lines if ln.width < page_width * 0.45]
    # 窄行中起点在分界线左侧的，归为左栏。
    left_narrow = [ln for ln in narrow if ln.x0 < mid]
    # 窄行中起点在分界线右侧的，归为右栏。
    right_narrow = [ln for ln in narrow if ln.x0 >= mid]
    # 双栏判定：左右栏各有至少 5 行，且窄行占全页 60% 以上。
    two_column = (
        # 左栏行数达到 5。
        len(left_narrow) >= 5
        # 右栏行数达到 5。
        and len(right_narrow) >= 5
        # 窄行占比超过 60%。
        and len(narrow) / max(1, len(lines)) > 0.6
    # 结束条件。
    )
    # 不是双栏：按 y 从上到下、同一水平线再按 x 从左到右排序。
    if not two_column:
        # 返回单栏阅读顺序。
        return sorted(lines, key=lambda l: (l.y0, l.x0))

    # 全宽元素（标题、跨栏图、表题）宽度 >= 页宽 66%。
    full = [ln for ln in lines if ln.width >= page_width * 0.66]
    # 其余非全宽元素。
    others = [ln for ln in lines if ln.width < page_width * 0.66]

    # 判断两个 Line 在垂直方向是否重叠，用于识别“全宽分隔元素”。
    def overlaps(a: Line, b: Line) -> bool:
        # 取重叠区间的上下边界。
        top, bottom = max(a.y0, b.y0), min(a.y1, b.y1)
        # 重叠高度超过较矮行高的 40%，就算明显重叠。
        return (bottom - top) > 0.4 * min(a.y1 - a.y0, b.y1 - b.y0)

    # 分隔行：全宽且不与任何其他行重叠，才能作为栏间分段边界。
    separators = [f for f in full if not any(overlaps(f, o) for o in others)]
    # 剩余行 = 所有行去掉分隔行；后续只在剩余行中分栏。
    rest = [ln for ln in lines if ln not in separators]
    # 没有任何分隔行时，退化为简单“左栏全部 + 右栏全部”。
    if not separators:
        # 左栏按 y/x 排序。
        left = sorted([ln for ln in rest if ln.x0 < mid], key=lambda l: (l.y0, l.x0))
        # 右栏按 y/x 排序。
        right = sorted([ln for ln in rest if ln.x0 >= mid], key=lambda l: (l.y0, l.x0))
        # 先读左栏再读右栏。
        return left + right
    # 有分隔行时，按分隔行的 y 中心切成多个水平带。
    sep_sorted = sorted(separators, key=lambda l: l.y0)
    # 边界列表：页顶 0、每个分隔行中心、页底 10000。
    boundaries = [0.0] + [(f.y0 + f.y1) / 2 for f in sep_sorted] + [10_000.0]
    # 最终输出顺序。
    out: list[Line] = []
    # 逐个水平带处理。
    for idx in range(len(boundaries) - 1):
        # 当前带的上下边界。
        lo, hi = boundaries[idx], boundaries[idx + 1]
        # 取中心落在 [lo, hi) 内的行。
        band = [ln for ln in rest if lo <= (ln.y0 + ln.y1) / 2 < hi]
        # 带内非空才需要排序。
        if band:
            # 带内左栏行。
            left = sorted([ln for ln in band if ln.x0 < mid], key=lambda l: (l.y0, l.x0))
            # 带内右栏行。
            right = sorted([ln for ln in band if ln.x0 >= mid], key=lambda l: (l.y0, l.x0))
            # 先输出本带左栏。
            out.extend(left)
            # 再输出本带右栏。
            out.extend(right)
        # 每个带之后追加该带下方的全宽分隔行。
        if idx < len(sep_sorted):
            # 最后一行分隔行也要输出。
            out.append(sep_sorted[idx])
    # 返回跨栏阅读顺序。
    return out


# ========== 三、标题识别：把版式信号和文字规则结合 ==========
# ---------------------------------------------------------------------------
# 标题识别
# ---------------------------------------------------------------------------
# 特殊章节标签字典：把中英文写法统一映射为内部类型。
SPECIAL_LABELS = {
    # 摘要。
    "摘要": "abstract",
    # 英文摘要。
    "abstract": "abstract",
    # 关键词。
    "关键词": "keywords",
    # 关键词的同义写法。
    "关键字": "keywords",
    # 英文关键词。
    "keywords": "keywords",
    # 参考文献。
    "参考文献": "reference",
    # 英文参考文献。
    "references": "reference",
    # 作者简介。
    "作者简介": "bio",
    # 致谢按普通 section 处理。
    "致谢": "section",
    # 附录按普通 section 处理。
    "附录": "section",
# 字典结束。
}


# 判断一行是否恰好是特殊标签（去掉空白、冒号、转小写后精确比较）。
def special_of(text: str) -> str | None:
    # 压缩空白、转小写并去掉首尾冒号。
    t = re.sub(r"\s+", "", text).lower().strip("：:")
    # 遍历标签字典。
    for key, value in SPECIAL_LABELS.items():
        # 完全相等才算特殊标签，避免“摘要方法”误判。
        if t == key:
            # 返回内部类型。
            return value
    # 附录允许带编号/字母，例如“附录 A”。
    if re.match(r"^附录[a-z0-9一二三四五六七八九十]*$", t):
        # 统一按 section 处理。
        return "section"
    # 不是特殊标签返回 None。
    return None


# 规范化标题展示文本：合并中文间空格，并在数字编号后补一个空格。
def _heading_title(text: str) -> str:
    # 先压缩任意空白为单空格并去首尾。
    """统一标题展示：压缩中文之间的空格，并在编号后补一个空格。"""
    # 删除两个中文字符之间的空格（中文排版不应有词间空格）。
    t = re.sub(r"\s+", " ", text or "").strip()
    # 匹配“数字编号 + 紧跟中文/英文/引号/书名号”的写法。
    t = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", t)
    # 分组 1 是编号，分组 2 是标题首字符。
    m = re.match(r"^(\d{1,2}(?:\.\d{1,2}){0,3})([\u4e00-\u9fffA-Za-z“”‘’《])", t)
    # 在编号后插入一个空格，提升可读性。
    if m:
        # 返回规范化标题。
        t = m.group(1) + " " + t[len(m.group(1)):]
    return t


# 判断一行是否是标题，并推断层级；这是结构树质量的关键启发式函数。
def heading_of(line: Line, body_size: float) -> Heading | None:
    # 先规范化标题文本。
    text = _heading_title(line.text.strip())
    # 空标题直接放弃。
    if not text:
        # 空字符串返回 None。
        return None
    # 拆分“标签 + 行内正文”，例如“摘要：本文……”。
    kind, inline = _label_parts(text)
    # 特殊标签若满足长度/位置/字号条件，直接作为一级特殊标题。
    if kind and (len(text) <= 24 or line.y0 < 340 or line.size >= body_size + 0.5):
        # 标签展示名映射。
        label = {"abstract": "摘要", "keywords": "关键词", "reference": "参考文献", "bio": "作者简介"}.get(kind, "附录")
        # 构造 Heading：层级 1、label 为展示名、special 为内部类型。
        return Heading(line, 1, label, kind)
    # 超过 60 字的行几乎不可能是标题。
    if len(text) > 60:
        # 超长行返回 None。
        return None
    # 私有区字符通常来自公式字体，包含则不是普通标题。
    if re.search(r"[\ue000-\uf8ff]", text):
        # 私有区字符返回 None。
        return None
    # 标题通常不含句号。
    if "。" in text:
        # 含句号返回 None。
        return None
    # 以 [1] 等文献编号开头的行是参考文献条目，不是标题。
    if re.match(r"^\[\d+\]", text):
        # 匹配则返回 None。
        return None
    # 以句号/分号/逗号结尾的行更像正文。
    if re.search(r"[。；，,]$", text):
        # 匹配则返回 None。
        return None

    # 原注释：阿拉伯数字编号的标题识别。
    # 阿拉伯数字编号：1 / 1.2 / 1.2.3 / 2.2.3.1（允许“1引言”这种无空格写法）
    # 匹配 1 / 1.2 / 1.2.3 / 2.2.3.1，编号后可有分隔符。
    m = re.match(r"^(\d{1,2}(?:\.\d{1,2}){0,3})[.、．]?\s*(\S.*)$", text)
    # m.group(1) 是编号，m.group(2) 是标题正文。
    if m:
        # 按点号数量推断层级：无点=1 级，1 个点=2 级……
        num, rest = m.group(1), m.group(2)
        # 标题应当较短（<=46）且正文至少 2 字。
        depth = num.count(".") + 1
        # 原注释：字号/字重是主信号，正文同字号编号行仅在二级及以下放行。
        short = len(text) <= 46 and len(rest) >= 2
        # 标题通常字号更大或加粗。
        # 字号/字重是标题的主信号；正文同字号的编号行仅在二级及以下才放行，
        # styled 或二级以上才继续，避免正文编号误判。
        # 避免把“2 幅边缘复杂……”这类换行后的正文误判成一级标题。
        # 形如“1）”的编号可能是列表项。
        styled = line.size >= body_size + 0.5 or line.bold
        # 列表项不按标题处理。
        if short and (styled or depth >= 2):
            # 原注释：编号标题一般不用句读。
            if re.match(r"^\d{1,2}[）)]", text):
                # 含逗号/分号说明更像正文。
                return None
            # 返回 None。
            # 编号标题一般不使用句读；带逗号的整行更像正文续行
            # “2 个/年/月……”等量词开头是正文数量表达。
            if re.search(r"[，,；;]", text):
                # 返回 None。
                return None
            # “2 多……”数量短语误判保护。
            if re.match(r"^\d{1,2}\s*(个|年|月|日|页|条|项|种|类|倍|%|名|位|幅|张|组|篇)", text):
                # 返回 None。
                return None
            # 原注释：不能误伤“多维度……”等标题。
            # “2 多……”只有在“多个/多年”这类数量短语里才是正文；不能误伤“多维度……”
            # 若正文以“多个/多年”等开头则排除。
            if re.match(r"^(个|百|千|万|亿|余|多|以|倍|次|百分点)(个|年|月|日|页|条|项|种|类|倍|%|名|位|次|余)", rest):
                # 返回 None。
                return None
            # 以“个百分点/多个/多年”等开头的正文短语黑名单。
            if rest.startswith(("个百分点", "个百", "个千", "个万", "多年", "多种", "多项", "多个", "多次", "余名", "余项", "余种")):
                # 命中则返回 None。
                return None
            # 原注释：正文同字号二级编号行若缩进在栏中间，多半是表格行/续行。
            # 正文同字号的二级编号行若缩进在栏中间，多半是表格行或正文续行
            # 未加粗且缩进处在 100~280pt 之间。
            if not styled and depth >= 2 and 100 < line.x0 < 280:
                # 排除。
                return None
            # 标题正文不应以标点/符号开头。
            if re.match(r"^[%％,，、;；。.．:：]", rest):
                # 命中则排除。
                return None
            # 标题正文应以中文、字母或引号/书名号开头。
            if not re.match(r"[\u4e00-\u9fffA-Za-z“”‘’《]", rest):
                # 不满足则排除。
                return None
            # 正文首字符小写或数字更像句子/数据。
            if rest[0].islower() or rest[0].isdigit():
                # 排除。
                return None
            # 一级标题若全为 ASCII 且不含中文，多半是英文正文/缩写行。
            if depth == 1 and rest.isascii() and not re.search(CJK, rest):
                # 排除。
                return None
            # 一级标题含方括号时通常是引用/数据行。
            if depth == 1 and ("[" in text or "]" in text):
                # 排除。
                return None
            # 原注释：纯数字/小数行不当作标题。
            # 纯数字/小数行（表格数据、公式片段）不当作标题
            # 整行只由数字、运算符、空白组成则排除。
            if re.fullmatch(r"[\d\s.．%+×/=<>~\-]+", text):
                # 命中则排除。
                return None
            # 所有检查通过：层级限制在 1~4，构造 Heading。
            return Heading(line, min(depth, 4), text)
        # 不满足短标题条件则返回 None。
        return None

    # 原注释：中文编号“一、”“（一）”。
    # 中文编号：一、 / （一）——政策文本常与正文同字号，按编号直接接受
    # 匹配“一、标题”形式。
    m = re.match(r"^([一二三四五六七八九十]{1,3})[、.．]\s*(\S.*)$", text)
    # 长度合理则直接作为一级标题。
    if m and len(text) <= 46:
        # 匹配“（一）标题”形式，层级 2。
        return Heading(line, 1, text)
    # 长度合理则作为二级标题。
    m = re.match(r"^（([一二三四五六七八九十]{1,3})）\s*(\S.*)$", text)
    # 原注释：“第X章/节/部分”的识别规则。
    if m and len(text) <= 46:
        # 避免把“第二部分注重于……”这类正文误判。
        return Heading(line, 2, text)
    # 前提注释。
    # “第X章/第X节/第X部分”：只有短标题或字号/字重明显时才接受，
    # 匹配“第X章/节/部分 + 标题”。
    # 避免把“第二部分注重于……，该分支通过注”这类正文识别成标题。
    # 长度限制。
    m = re.match(r"^(第[一二三四五六七八九十百\d]{1,4}[章节部分])\s*(\S.*)$", text)
    # 长度合格才继续。
    if m and len(text) <= 46:
        # 判断字号/字重是否明显。
        styled = line.size >= body_size + 0.5 or line.bold
        # 有样式，或很短且无逗号/分号，才接受。
        if styled or (len(text) <= 16 and not re.search(r"[，,；;]", text)):
            # “章/部”为一级，其余为二级。
            return Heading(line, 1 if "章" in m.group(1) or "部" in m.group(1) else 2, text)
    # 所有规则都不匹配，返回 None。
    return None


# 估计正文字号：按中文字符数加权取中位数，抵抗表格/题注干扰。
def detect_body_size(pages: list[list[Line]]) -> float:
    # 按字符数加权，而不是按行数，避免大量短表格行拉偏。
    """按字符数加权的字号中位数，避免表格/题注把正文字号带偏。"""
    # 字号 -> 字符权重。
    weights: dict[float, int] = {}
    # 遍历每页。
    for lines in pages:
        # 遍历每行。
        for ln in lines:
            # 只统计含中文且字号在合理正文范围内的行。
            if re.search(CJK, ln.text) and 6.0 <= ln.size <= 16.0:
                # 字号四舍五入到 1 位小数后累加字符数。
                weights[round(ln.size, 1)] = weights.get(round(ln.size, 1), 0) + len(ln.text)
    # 没有统计到任何行时，使用常见默认值 10.5。
    if not weights:
        # 返回兜底字号。
        return 10.5
    # 总字符数。
    total = sum(weights.values())
    # 累计权重。
    acc = 0
    # 按字号从小到大遍历。
    for size in sorted(weights):
        # 累加当前字号权重。
        acc += weights[size]
        # 累计达到一半，当前字号即加权中位数。
        if acc >= total / 2:
            # 返回该字号。
            return size
    # 理论上不可达；兜底返回权重最大的字号。
    return max(weights.items(), key=lambda kv: kv[1])[0]


# ========== 四、元数据：题名、作者、文档类型 ==========
# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------
# 根据版式猜题名、题名所在页和题名行；优先在摘要上方找最大字号行。
def detect_title_layout(pages: list[list[Line]], body_size: float) -> tuple[str, int, Line | None]:
    # 返回 (题名, 起始页, 题名 Line 或 None)。
    """返回题名、正文起始页、题名行。"""
    # 摘要所在页，初始 None。
    abstract_page = None
    # 摘要的 y 坐标，初始为很大值（表示不限制）。
    abstract_y = 10_000.0
    # 只扫描前三页，避免全文开销；start=1 让页码从 1 开始。
    for idx, lines in enumerate(pages[:3], start=1):
        # 先按阅读顺序排列，减少双栏干扰；595 是 A4 宽度近似值。
        ordered = reading_order(lines, 595)
        # 逐行找摘要标签。
        for ln in ordered:
            # 拆分标签，取得类型。
            kind, _ = _label_parts(ln.text)
            # 找到摘要标签。
            if kind == "abstract":
                # 记录摘要页。
                abstract_page = idx
                # 记录摘要 y 坐标，作为题名搜索的上边界。
                abstract_y = ln.y0
                # 跳出内层循环。
                break
        # 找到摘要页后不再扫描后续页。
        if abstract_page:
            # 题名候选页：有摘要则只搜摘要页；否则搜前两页。
            break
    # 摘要页优先；无摘要则 1~2 页。
    candidate_pages = [abstract_page] if abstract_page else list(range(1, min(2, len(pages)) + 1))
    # 候选页为空时至少尝试第 1 页。
    if not candidate_pages:
        # 兜底为第 1 页。
        candidate_pages = [1]
    # best 保存 (得分, Line, 页码)。
    best: tuple[float, Line, int] | None = None
    # 遍历候选页。
    for pno in candidate_pages:
        # 越界页跳过。
        if pno < 1 or pno > len(pages):
            # 跳过无效页。
            continue
        # 按阅读顺序取该页行。
        lines = reading_order(pages[pno - 1], 595)
        # 题名不应位于摘要下方；摘要页用摘要 y 作截止线。
        cutoff = abstract_y if pno == abstract_page else 10_000
        # 非摘要页不限制。
        for ln in lines:
            # 遍历候选行。
            if ln.y0 > cutoff:
                # 超过截止线则跳过（后续行更靠下，可直接 continue）。
                continue
            # 题名长度应在 5~120 字之间。
            if len(ln.text) < 5 or len(ln.text) > 120:
                # 不符合则跳过。
                continue
            # 排除特殊标签行（摘要/关键词等）。
            kind, _ = _label_parts(ln.text)
            # 排除纯数字/符号行以及特殊标签。
            if kind or re.match(r"^[\d\s\.\-—*]+$", ln.text):
                # 排除页眉页脚类文本。
                continue
            # 命中期刊页眉/收稿日期/引用格式等噪声则跳过。
            if re.search(r"(网络首发|ISSN|CN\s*\d|收稿日期|引用格式|题目[:：]|作者[:：])", ln.text):
                # 计算得分：字号为主，含中文加 2 分，长文本轻微减分。
                continue
            # 得分更高则更新最优题名。
            score = ln.size + (2 if re.search(CJK, ln.text) else 0) - (0.01 * len(ln.text))
            # 更新 best。
            if best is None or score > best[0]:
                # 记录得分/行/页。
                best = (score, ln, pno)
    # 没有候选时返回占位题名。
    if not best:
        # 题名未识别，返回第 1 页且无题名行。
        return "未命名文档", 1, None
    # 取出最优题名行。
    line = best[1]
    # 按 y 排序该页，便于合并上下相邻的折行题名。
    page_lines = sorted(reading_order(pages[best[2] - 1], 595), key=lambda l: l.y0)
    # 原注释：同字号折行题名需要合并。
    # 同字号的折行题名：合并题名行上下相邻、字号接近的行
    # 找到题名行在排序列表中的索引；找不到用 -1。
    idx = next((i for i, l in enumerate(page_lines) if l is line), -1)
    # 题名行组，先放入最佳行。
    group = [line]
    # 向上下两个方向扩展。
    for direction in (-1, 1):
        # 从题名行索引出发。
        i = idx
        # 不断向该方向移动。
        while True:
            # 先移动到相邻行。
            i += direction
            # 越界停止。
            if i < 0 or i >= len(page_lines):
                # 越界退出 while。
                break
            # 取候选相邻行。
            cand = page_lines[i]
            # 向上合并时锚点是组首，向下时是组尾。
            anchor = group[0] if direction < 0 else group[-1]
            # 计算垂直间隙：向上用锚点 y0 减候选 y1，向下用候选 y0 减锚点 y1。
            gap = (anchor.y0 - cand.y1) if direction < 0 else (cand.y0 - anchor.y1)
            # 字号差超过 1pt 或间隙超过 2.4 倍字号则停止。
            if abs(cand.size - line.size) > 1.0 or gap > line.size * 2.4:
                # 停止扩展。
                break
            # 遇到特殊标签行（摘要等）停止，避免把摘要并入题名。
            if _label_parts(cand.text)[0]:
                # 停止。
                break
            # 把相邻行纳入题名组。
            group.append(cand)
    # 按 y 排序题名组。
    group.sort(key=lambda l: l.y0)
    # 直接拼接各行文字（中文题名通常不需要空格）。
    title = "".join(l.text for l in group)
    # 去掉末尾星号/＊（常见通讯作者标记）并去空白。
    title = re.sub(r"[*＊]\s*$", "", title).strip()
    # 返回题名、页码、原始题名行。
    return title, best[2], line


# 从题名下方、摘要上方提取作者与单位；规则以排除噪声为主。
def _authors_and_affiliations(lines: list[Line], title_line: Line | None, abstract_y: float) -> str:
    # 没有题名行时无法定位作者区域，返回空串。
    if not title_line:
        # 返回空作者。
        return ""
    # 收集候选作者行。
    found: list[str] = []
    # 遍历传入的行。
    for ln in lines:
        # 题名行及其上方不可能是作者。
        if ln.y0 <= title_line.y1:
            # 跳过。
            continue
        # 摘要及以下不取。
        if ln.y0 >= abstract_y:
            # 跳过。
            continue
        # 题名下方 48pt 之外不再视为作者区。
        if ln.y0 > title_line.y1 + 48:
            # 跳过。
            continue
        # 取文本。
        t = ln.text
        # 过长或字号过小（通常脚注）跳过。
        if len(t) > 100 or ln.size < 7.0:
            # 跳过。
            continue
        # 排除收稿日期/摘要/DOI 等元数据噪声。
        if re.search(r"(收稿|网络首发|引用格式|摘要|关键词|ISSN|CN\s*\d|DOI|题目[:：])", t):
            # 跳过。
            continue
        # 纯数字行（页码/编号）跳过。
        if re.match(r"^\d+$", t):
            # 跳过。
            continue
        # 含“大学/学院……路/号/市”等地址特征的行视为单位，不放入作者名。
        if re.search(r"(大学|学院|公司|研究院|研究所|中心|实验室|系|集团).*(路|号|市|省|区|县|\d{6})", t):
            # 跳过。
            continue
        # 只含数字/括号/逗号的行跳过。
        if re.match(r"^[\d\s()（）,，]+$", t):
            # 跳过。
            continue
        # 其余加入候选。
        found.append(t)
    # 原注释：去掉“作者：”标签和姓名后的上标数字。
    # 去掉“作者：”标签本身，保留其后的内容；清理姓名后的上标数字
    # 清洗后的作者列表。
    cleaned = []
    # 逐条清洗。
    for t in found:
        # 去掉“作者：”前缀。
        t = re.sub(r"^作者[:：]\s*", "", t)
        # 去掉开头数字/空白（上标编号）。
        t = re.sub(r"^[\d\s]+", "", t)
        # 去掉中文姓名后的数字/逗号/空白（上标单位编号）。
        t = re.sub(r"(?<=[\u4e00-\u9fff])[\d,，\s]+$", "", t)
        # 非空且未重复才加入。
        if t and t not in cleaned:
            # 去重追加。
            cleaned.append(t)
    # 原注释：合并同一行多个姓名（PDF 常拆成多个文本框）。
    # 合并同一行的多个姓名（PDF 常把姓名按文本框拆行）
    # 最多取前 4 个作者，用中文分号连接。
    return "；".join(cleaned[:4])


# 根据题名、摘要、章节标题和页数猜文档类型；用于后续策略（如新闻导语合并）。
def detect_doc_type(title: str, abstract: str, section_titles: list[str], page_count: int) -> str:
    # 把可用文本拼成一个 blob，供关键词规则匹配。
    blob = title + " " + abstract + " " + " ".join(section_titles)
    # 题名含综述类词 -> review。
    if re.search(r"(综述|研究进展|进展与展望|研究现状|文献综述)", title):
        # 返回综述类型。
        return "review"
    # 题名含政策/治理，且全文也含相关词 -> policy。
    if re.search(r"(政策|政府|治理|制度|框架及其生成逻辑)", title) and re.search(r"(政策|政府|治理)", blob):
        # 返回政策类型。
        return "policy"
    # 全文含实验/方法/模型等词，且有摘要/关键词 -> 研究论文。
    if re.search(r"(实验|结果|方法|模型|算法)", blob) and re.search(r"(摘要|关键词)", blob):
        # 返回研究论文。
        return "research_paper"
    # 短文档、无摘要关键词、章节少 -> 新闻/报道。
    if page_count <= 5 and not re.search(r"(摘要|关键词)", blob) and len(section_titles) <= 3:
        # 返回新闻类型。
        return "news"
    # 含实验/数据集/基线/消融等强信号 -> 研究论文。
    if re.search(r"(实验|数据集|基线|消融)", blob):
        # 返回研究论文。
        return "research_paper"
    # 其他情况归为 other。
    return "other"


# ========== 五、结构树：把行序列还原为可检索的章节树 ==========
# ---------------------------------------------------------------------------
# 结构树
# ---------------------------------------------------------------------------
# 把一段文本追加到 state['text']，并记录字符区间对应的页码，供原文定位。
def _append(state: dict, text: str, page: int) -> None:
    # 空文本不追加，避免产生空 span。
    if not text:
        # 直接返回。
        return
    # 若已有文本且末尾不是换行/空格，检查是否需要补空格。
    if state["text"] and not state["text"].endswith(("\n", " ")):
        # 前一个字符。
        prev = state["text"][-1]
        # 后一个字符。
        nxt = text[0]
        # 两侧都是英文字母/数字时补空格，避免英文单词粘连；中文不补。
        if re.match(r"[A-Za-z0-9]", prev) and re.match(r"[A-Za-z0-9]", nxt):
            # 追加一个空格。
            state["text"] += " "
    # 记录追加前长度，作为 span 起点。
    start = len(state["text"])
    # 追加正文。
    state["text"] += text
    # 记录 (start, end, page) 三元组。
    state["spans"].append((start, len(state["text"]), page))


# 把一组 Line 合并成章节正文，并在句末标点后换行；返回文本和页码跨度。
def _join_paragraph(lines: list[Line]) -> tuple[str, list[tuple[int, int, int]]]:
    # state 同时保存文本和 spans；用 dict 便于 _append 原地修改。
    state = {"text": "", "spans": []}
    # 遍历行。
    for ln in lines:
        # 去首尾空白。
        t = ln.text.strip()
        # 空行跳过。
        if not t:
            # 跳过空行。
            continue
        # 若已有文本且以句末标点/右引号/右括号结尾，则另起一行。
        if state["text"] and state["text"].endswith(("。", "！", "？", "；", "：", "”", "’", "）", ")", "】")):
            # 在换行后追加当前行；span 页码仍记录当前行。
            _append(state, "\n" + t, ln.page)
        # 否则直接接在当前文本后。
        else:
            # 普通续写，不换行。
            _append(state, t, ln.page)
    # 去掉整体首尾空白后返回；spans 保持原字符偏移。
    return state["text"].strip(), state["spans"]


# 识别“图 1/表 2”类题注；同时排除正文中的“如图 1 所示”等引用。
def _caption_of(text: str) -> str | None:
    # 题注通常短于 110 字。
    """识别图表题注；排除“如图 1 所示”“表 2 中”等正文引用。"""
    # 超长直接放弃。
    if len(text) > 110:
        # 返回 None。
        return None
    # 若前 16 字出现“所示/如下/如图”等引用语，则不是题注。
    if re.search(r"(所示|如下|可知|中给出|展示了|见图|见表|如图|如表)", text[:16]):
        # 返回 None。
        return None
    # 匹配“图/表 + 编号 + 可选子编号 + 题注正文”。
    m = re.match(r"^(图|表)\s*(\d+)([-—.．]\d+)?\s*(.*)$", text)
    # 必须有第 4 组（题注正文），否则只是“图 1”占位。
    if m and m.group(4):
        # “图”返回 figure，“表”返回 table。
        return "figure" if m.group(1) == "图" else "table"
    # 英文题注 Figure/Fig./Table 匹配。
    if re.match(r"^(Figure|Fig\.?|Table)\s*\d+", text, re.I):
        # 根据前缀判断图表类型。
        return "figure" if re.match(r"^(Figure|Fig)", text, re.I) else "table"
    # 不匹配返回 None。
    return None


# 识别“摘要：xxx”“关键词：xxx”这类标签行，返回（类型，行内正文）。
def _label_parts(text: str) -> tuple[str | None, str]:
    # 去掉所有空白后再匹配，兼容“摘 要”。
    """识别“摘要：xxx”这类标签行，返回（类型，行内正文）。"""
    # 匹配标签 + 可选冒号 + 行内正文。
    compact = re.sub(r"\s+", "", text or "")
    # 标签部分允许中英文、空格。
    m = re.match(
        # 使用 re.I 忽略英文大小写。
        r"^(摘\s*要|abstract|关键词|关键字|key\s*words?|参考文献|references|作者简介)\s*([:：]?)\s*(.*)$",
        # 组合标志。
        compact,
        # 闭合匹配。
        re.I,
    # 无匹配返回 (None, '')。
    )
    # 返回空结果。
    if not m:
        # 无匹配。
        return None, ""
    # 解包：标签原文、分隔符、剩余正文。
    raw_label, delim, rest = m.group(1), m.group(2), m.group(3)
    # 标准化标签：去空白、转小写。
    label = re.sub(r"\s+", "", raw_label).lower()
    # 查表得到内部类型。
    kind = SPECIAL_LABELS.get(label)
    # 未知标签返回空。
    if not kind:
        # 返回空结果。
        return None, ""
    # 原注释：关键词/参考文献/作者简介必须带分隔符。
    # 关键词/参考文献/作者简介必须带分隔符，避免“关键词检索采用……”被当成标签
    # 这三类标签容易出现在正文句子中，需要更严格。
    if kind in ("keywords", "reference", "bio") and not delim:
        # 判断是否英文关键词变体。
        english_kw = label.startswith("keyword") or label.startswith("key") and "word" in label
        # 若整行不是恰好等于标签本身（即后面还有内容），继续检查。
        if compact.lower() not in ("关键词", "关键字", "keywords", "参考文献", "references", "作者简介"):
            # 英文“keywords xxx”且后面有正文时允许无冒号。
            if not (english_kw and re.match(r"^(key\s*words?|keywords)\s+\S", text, re.I)):
                # 否则拒绝。
                return None, ""
    # 返回类型和行内正文。
    return kind, rest.strip()


# 无编号文档的小标题兜底识别（报纸/评论）。
def detect_short_headings(ordered: list[Line], body_size: float, start_index: int = 0) -> list[tuple[int, Heading]]:
    # 判据：短、字号接近正文、整行较宽、不以句末标点结尾、下一行是长正文、上一行已收句。
    """无编号文档（如报纸深度报道）的小标题兜底识别。
# 这些条件组合可降低把普通正文行误判成小标题的概率。

    # 返回值列表。
    判据：短行、字号接近正文、整行宽度较大、不以句末标点结尾、下一行是长正文、
    # 从 start_index+1 开始，跳过题名行；到倒数第二行为止。
    上一行已收句。用于没有编号体系的报道/评论类文本。
    # 逐行检查。
    """
    # 当前行。
    found: list[tuple[int, Heading]] = []
    # 前一行和后一行。
    for idx in range(max(1, start_index + 1), len(ordered) - 1):
        # 取当前行文本。
        ln = ordered[idx]
        # 长度必须在 4~28 字。
        prev, nxt = ordered[idx - 1], ordered[idx + 1]
        # 不满足则跳过。
        t = ln.text.strip()
        # 必须含中文，排除纯英文/数字。
        if not (4 <= len(t) <= 28):
            # 不满足则跳过。
            continue
        # 字号必须接近正文（差 <=0.7）。
        if not re.search(CJK, t):
            # 不满足则跳过。
            continue
        # 不能以句末标点开头/结尾，不能以引号/图表注等开头。
        if abs(ln.size - body_size) > 0.7:
            # 命中则跳过。
            continue
        # 纯编号/符号行跳过。
        if re.search(r"[。；：！？、?!]$", t) or t.startswith(("“", "‘", "图", "表", "注", "（", "(", "[")):
            # 匹配则跳过。
            continue
        # 逗号太多说明是正文句子。
        if re.match(r"^[\dA-Za-z.．、\-—#*]+$", t):
            # 超过 1 个逗号跳过。
            continue
        # 整行宽度至少 80，且左缩进不超过 220（应靠左）。
        if t.count("，") > 1:
            # 不满足则跳过。
            continue
        # 上一行必须以句末标点结尾，说明小标题另起一段。
        if ln.width < 80 or ln.x0 > 220:
            # 不满足则跳过。
            continue
        # 下一行必须足够长，或字号明显更大/有句末标点，才像正文开头。
        if not re.search(r"[。！？；”’）)]$", prev.text.strip()):
            # 下一行太短且无标点，可能是另一个标题/表格。
            continue
        # 跳过。
        if len(nxt.text) < 24 and nxt.size <= body_size + 0.5 and not re.search(r"[。！？；]", nxt.text):
            # 全部条件通过，记录索引和 Heading。
            continue
        # 追加候选。
        found.append((idx, Heading(ln, 1, t)))
    # 原注释：小标题通常 2—8 个；过多说明是表格/诗歌。
    # 小标题通常 2—8 个；过多说明是表格或诗歌，放弃兜底
    # 数量在合理范围才返回。
    if 2 <= len(found) <= 8:
        # 返回候选列表。
        return found
    # 数量异常，放弃兜底。
    return []


# 合并“编号独占一行”和“标题折行”，避免标题漏级或只截半句。
def _merge_heading_lines(ordered: list[Line], body_size: float, protected: set[int] | None = None) -> list[Line]:
    # 中文期刊常见两种版式：编号与标题分行（可能跨页）、长标题在右栏折行。
    """合并“编号独占一行”与“标题折行”的版式。
# 不合并会导致后续 heading_of 识别不到完整标题。

    # 这些注释解释为什么要做合并。
    中文期刊常见两种排版：
    # 原注释：中文期刊常见两种排版。
    1. “1” / “相关工作” 分两行（甚至跨页）；
    # 情形 1：编号与标题分两行，甚至跨页。
    2. 长标题在右栏折成两行。
    # 情形 2：长标题在栏内折成两行。
    不合并会让标题识别漏掉一整级，或只截到半句。
    # 后果说明。
    """
    # protected 是不得参与合并的行索引集合（如题名行）。
    protected = protected or set()
    # 没有传入时用空集合。
    out: list[Line] = []
    # 输出行列表。
    i = 0
    # 手动控制 i，因为可能一次消费两行。
    while i < len(ordered):
        # 从头遍历。
        a = ordered[i]
        # 当前行 a。
        if id(a) in protected:
            # 受保护行直接保留。
            out.append(a)
            # 加入输出。
            i += 1
            # 前进一行。
            continue
        # 跳过后续合并逻辑。
        # 版式里“标题文字在前、编号在后”的情况：4.2 与 M3UCD… 同 baseline，
        # 原注释：标题文字在前、编号在后的版式。
        # 阅读顺序可能先排标题再排编号。
        # 若 a 是纯编号（如 4.2），可能应把前一行标题文字拼到它前面。
        if re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){0,3}", a.text.strip()) and out:
            # 有前一行才处理。
            prev = out[-1]
            # 前一行。
            tb = prev.text.strip()
            # 前一行文本。
            if (prev.page == a.page and abs(prev.y0 - a.y0) <= 3.5
                    # 同一页、基线接近（<=3.5pt）、编号在文字左侧 0~40pt。
                    and 0 < prev.x0 - a.x0 <= 40 and 2 <= len(tb) <= 60
                    # 长度合理。
                    and not re.search(r"[。；，,：:]$", tb)
                    # 前一行不以句读结尾。
                    and re.match(r"[\u4e00-\u9fffA-Za-z“”‘’《]", tb)):
                # 前一行以中文/字母/引号开头。
                parts = [int(x) for x in a.text.strip().split(".")]
                # 解析编号为整数列表。
                if parts and parts[0] <= 20 and all(p <= 30 for p in parts):
                    # 限制编号范围，避免把数据行当标题。
                    merged = Line(f"{a.text.strip()} {tb}", a.page, max(a.size, prev.size),
                                  # 构造合并后的 Line：编号 + 空格 + 前一行文字。
                                  a.bold or prev.bold, prev.x0, prev.y0, prev.x1, prev.y1, prev.x1 - prev.x0)
                    # 用编号行和前一行中较大的字号/粗体。
                    out[-1] = merged
                    # 坐标取前一行的范围。
                    i += 1
                    # 替换输出中的前一行。
                    continue
        # 消费 a。
        if i + 1 >= len(ordered):
            # 继续下一轮。
            out.append(a)
            # 若已是最后一行，直接输出。
            i += 1
            # 加入输出。
            continue
        # 前进。
        b = ordered[i + 1]
        # 继续。
        merged = _merge_two_lines(a, b, body_size)
        # 取下一行 b。
        if merged is not None:
            # 尝试合并 a 与 b；不能合并返回 None。
            out.append(merged)
            # 成功则输出合并行。
            i += 2
        # 加入输出。
        else:
            # 一次消费两行。
            out.append(a)
            # 失败则只输出 a。
            i += 1
    # 加入输出。
    return out
# 下一轮从 b 继续。

# 返回合并后的行序列。

# 尝试合并相邻两行；成功返回新 Line，失败返回 None。
def _merge_two_lines(a: Line, b: Line, body_size: float) -> Line | None:
    # 取两行去空白文本。
    ta, tb = a.text.strip(), b.text.strip()
    # 任一侧为空则不合并。
    if not ta or not tb:
        # 返回 None。
        return None
    # 情形一：第一行是纯编号。
    # 编号独占一行：1 / 1.2 / 2.3.1
    # 匹配 1 / 1.2 / 2.3.1。
    if re.fullmatch(r"\d{1,2}(?:\.\d{1,2}){0,3}", ta):
        # 拆成整数列表。
        parts = [int(x) for x in ta.split(".")]
        # 编号各段范围校验（首段 <=20，其余 <=30）。
        if not parts or parts[0] > 20 or any(p > 30 for p in parts):
            # 越界返回 None。
            return None
        # 只允许同页或下一页，防止跨多页误合并。
        if b.page - a.page not in (0, 1):
            # 跨页超过 1 页不合并。
            return None
        # 原注释：只合并同一栏、紧邻下方的标题。
        # 只合并“同一栏、紧邻下方”的标题；表格单元格里的数字通常不在栏首
        # 编号行必须位于栏首附近（左 95pt 内或 270~345pt 的右栏起点）。
        if not (a.x0 <= 95 or 270 <= a.x0 <= 345):
            # 不满足则不合并。
            return None
        # 两行左边界差不超过 35pt，确保同栏对齐。
        if abs(b.x0 - a.x0) > 35:
            # 不满足则不合并。
            return None
        # 同页时检查垂直间距。
        if b.page == a.page:
            # 0~42pt 内才可能紧邻。
            if not (0 <= b.y0 - a.y0 <= 42):
                # 超出则不合并。
                return None
        # 跨页时检查页面位置。
        else:
            # 编号行应在页面底部（>=640），下一行在页顶（<=150）。
            if a.y0 < 640 or b.y0 > 150:
                # 否则不合并。
                return None
        # 标题文字长度 2~60。
        if not 2 <= len(tb) <= 60:
            # 不满足则不合并。
            return None
        # 原注释：标题行一般没有句读。
        # 标题行一般没有句读；带逗号/句号的“编号+文字”多半是正文续行
        # 带句读的“编号+文字”更像正文。
        if re.search(r"[。.；;，,：:]", tb):
            # 不合并。
            return None
        # 标题文字必须以中文/字母/引号开头。
        if not re.match(r"[\u4e00-\u9fffA-Za-z“”‘’《]", tb):
            # 不满足则不合并。
            return None
        # 下一行字号不能太小，且左边界不能超过右栏范围。
        if b.size < body_size - 1.5 or b.x0 > 345:
            # 不满足则不合并。
            return None
        # 构造合并行：文本“编号 标题”，页码取 b，字号取较大者。
        return Line(f"{ta} {tb}", b.page, max(a.size, b.size), a.bold or b.bold,
                    # 坐标取 b 的包围盒，宽度按 b。
                    b.x0, b.y0, b.x1, b.y1, b.x1 - b.x0)
    # 原注释：标题折行：同栏、同字号、同字重、上一行接近右边界。
    # 标题折行：同一栏、同字号、同字重，上一行接近右边界
    # 第一行必须本身像标题（字号更大或粗体）。
    if not (a.size >= body_size + 0.5 or a.bold):
        # 否则不合并。
        return None
    # 必须同页、字号差 <=0.35。
    if a.page != b.page or abs(a.size - b.size) > 0.35:
        # 不满足则不合并。
        return None
    # 字重必须一致且左边界对齐（差 <=10）。
    if a.bold != b.bold or abs(a.x0 - b.x0) > 10:
        # 不满足则不合并。
        return None
    # 上一行必须接近右边界（>=470），且两行总长受限。
    if a.x1 < 470 or len(tb) > 40 or len(ta) + len(tb) > 80:
        # 不满足则不合并。
        return None
    # 下一行不应以句读结尾或以数字开头。
    if re.search(r"[。；，,：:]$", tb) or re.match(r"^\d", tb):
        # 不满足则不合并。
        return None
    # 下一行必须以中文/字母/括号/引号开头。
    if not re.match(r"[\u4e00-\u9fffA-Za-z（(“”‘’《]", tb):
        # 不满足则不合并。
        return None
    # 合并文本并保留第一行的样式/起点、第二行的右/下边界。
    return Line(ta + tb, a.page, a.size, a.bold, a.x0, a.y0, b.x1, b.y1, b.x1 - a.x0)


# 结构树构建主函数：输入页行与题名信息，输出 NodeDraft 列表和元数据。
def build_structure(
    # 参数 pages 是每页 Line 列表；title_page/title_line 用于定位题名。
    pages: list[list[Line]],
    # body_size 是正文字号基准；doc_type 影响新闻导语处理。
    title: str,
    # 返回 (节点列表, 元数据字典)。
    title_page: int,
    # 顶层节点顺序：题名、作者、摘要、关键词、正文章节、参考文献。
    title_line: Line | None,
    # 空列表存放所有节点（包括子节点，后续扁平遍历）。
    body_size: float,
    # 先创建题名节点，类型 title、层级 1、order_no 1。
    doc_type: str,
# 题名节点加入列表。
) -> tuple[list[NodeDraft], dict]:
    # 把所有页的行按阅读顺序汇总成全局 ordered 序列。
    """把行序列还原为结构树。
# 逐页遍历，pno 从 1 开始。

    # 每页先还原阅读顺序；595 为 A4 宽度近似。
    顶层节点顺序：题名、作者、摘要、关键词、正文章节、参考文献。
    # 回填页码，防止上游 Line 页码缺失。
    """
    # 追加到全局序列。
    nodes: list[NodeDraft] = []
    # 合并编号独占行/折行标题；题名行保护起来不参与合并。
    title_node = NodeDraft(new_id("node"), None, 1, "title", 1, title or "未命名文档", "", title_page, title_page)
    # 建立 Line 对象 id -> 全局索引的映射，后续按对象身份定位。
    nodes.append(title_node)
# 标题候选列表，元素为 (索引, Heading)。

    # 遍历全局行。
    ordered: list[Line] = []
    # 识别当前行是否标题。
    for pno, lines in enumerate(pages, start=1):
        # 识别成功。
        for ln in reading_order(lines, 595):
            # 记录索引和 Heading。
            ln.page = pno
            # 原注释：只从摘要/题名之后识别章节，避免封面信息被当标题。
            ordered.append(ln)
    # 找摘要标题在 headings 中的位置；找不到为 None。
    ordered = _merge_heading_lines(ordered, body_size, {id(title_line)} if title_line else set())
    # 记录摘要 y 坐标，用于过滤题名之前的标题。
    index_of = {id(ln): i for i, ln in enumerate(ordered)}
# 如果找到摘要。

    # 摘要对应的全局行索引。
    headings: list[tuple[int, Heading]] = []
    # 只保留摘要及其之后的标题候选。
    for idx, ln in enumerate(ordered):
        # 如果有摘要 y 坐标。
        h = heading_of(ln, body_size)
        # 进一步过滤同一页摘要上方的标题。
        if h:
            # 保留不满足“同页且在摘要上方”的标题。
            headings.append((idx, h))
    # 过滤条件：同页且 y 小于摘要 y-1。
    # 只从摘要/题名之后开始识别章节，避免封面、作者、栏目信息被当成标题
    # 如果没找到摘要但有题名行。
    abstract_idx = next((i for i, (_, h) in enumerate(headings) if h.special == "abstract"), None)
    # 找到题名行索引；找不到用 0。
    abstract_y = next((h.line.y0 for _, h in headings if h.special == "abstract"), None)
    # 只保留题名行之后的标题。
    if abstract_idx is not None:
        # 如果仍没有标题候选。
        first_content = headings[abstract_idx][0]
        # 从题名行之后开始找小标题；找不到题名则从 0 开始。
        headings = [(i, h) for i, h in headings if i >= first_content]
        # 调用无编号小标题兜底。
        if abstract_y is not None:
            # 提取所有标题索引，供切正文使用。
            headings = [
                # 内部函数：找 idx 之后的下一个标题索引。
                (i, h) for i, h in headings
                # 遍历标题索引。
                if not (h.line.page == title_page and h.line.y0 < abstract_y - 1)
            # 第一个大于 idx 的即下一个标题。
            ]
    # 返回该索引。
    elif title_line is not None:
        # 没有后续标题则到全文末尾。
        title_idx = next((i for i, ln in enumerate(ordered) if ln is title_line), 0)
        # 内部函数：切出 idx 到下一个标题之间的正文行。
        headings = [(i, h) for i, h in headings if i > title_idx]
    # 返回切片（不包含标题行）。
    if not headings:
        # 原注释：作者与单位。
        start_idx = next((i for i, ln in enumerate(ordered) if ln is title_line), 0)
        # front_end 是摘要标题索引；没有摘要则取全文长度。
        headings = detect_short_headings(ordered, body_size, start_idx)
    # 如果找不到摘要。
    heading_indices = [idx for idx, _ in headings]
# 只取题名所在页的行作为前置区。

    # 摘要 y 上限：题名下方 45pt，或很大值。
    def next_heading_index(idx: int) -> int:
        # 否则取摘要之前的行。
        for j in heading_indices:
            # 从这些行中找摘要标签的 y 坐标。
            if j > idx:
                # 调用作者提取。
                return j
        # 有作者才创建节点。
        return len(ordered)
# 作者节点类型 author、order_no 2、正文为作者字符串。

    # 摘要文本。
    def slice_body(idx: int) -> list[Line]:
        # 关键词列表。
        return ordered[idx + 1 : next_heading_index(idx)]
# 摘要节点，稍后创建。

    # 关键词节点。
    # 作者与单位
    # 参考文献节点。
    front_end = next((idx for idx, h in headings if h.special == "abstract"), len(ordered))
    # 参考文献条目计数。
    if front_end == len(ordered):
        # 原注释：特殊段只取第一次。
        front_lines = [ln for ln in ordered if ln.page == title_page]
        # 遍历所有标题候选。
        abstract_y = min(10_000, title_line.y1 + 45) if title_line else 10_000
    # 解析标签类型和行内正文。
    else:
        # 找同一水平行右侧的文本（PDF 常把“摘要：”和正文拆成不同行对象）。
        front_lines = ordered[:front_end]
        # 在 idx 前后 3 行窗口内找。
        abstract_y = next((ln.y0 for ln in front_lines if _label_parts(ln.text)[0] == "abstract"), 10_000)
    # 排除标题行本身。
    authors = _authors_and_affiliations(front_lines, title_line, abstract_y)
    # 同页。
    if authors:
        # y 坐标接近（<6pt）。
        nodes.append(NodeDraft(new_id("node"), None, 1, "author", 2, "作者与单位", authors, title_page, title_page))
# 位于标题右侧。

    # 如果这是摘要。
    abstract_text = ""
    # 切出摘要到下一个标题之间的正文。
    keywords: list[str] = []
    # 去掉混入的关键词/参考文献行。
    abstract_node: NodeDraft | None = None
    # 同排右侧行的 id 集合。
    keywords_node: NodeDraft | None = None
    # 把同排行放到正文最前面，并去重。
    reference_node: NodeDraft | None = None
    # 合并为段落文本和 span。
    ref_count = 0
# 摘要 = 行内正文 + 正文，必要时加空格。

    # 截断到“关键词：”之前，避免把关键词并进摘要。
    # 特殊段：摘要、关键词、参考文献、作者简介（同名段只取第一次）
    # 统计中文字符数，用于在重复摘要中选更完整的一份。
    for idx, h in headings:
        # 已有摘要且不比新候选短，跳过。
        kind, inline = _label_parts(h.line.text)
        # 继续下一标题。
        same_row = [
            # 保存更完整的摘要。
            ln for ln in ordered[max(0, idx - 3) : idx + 4]
            # 创建摘要节点。
            if ln is not h.line and ln.page == h.line.page and abs(ln.y0 - h.line.y0) < 6 and ln.x0 > h.line.x0
        # 节点父为空、层级 1、类型 abstract、order 2、标题“摘要”。
        ]
        # 正文、起始页、结束页（body 最后一页或标题页）、置信度 0.97。
        if kind == "abstract":
            # 设置摘要的页码跨度。
            body = slice_body(idx)
            # 如果这是关键词标题。
            body = [ln for ln in body if _label_parts(ln.text)[0] not in ("keywords", "reference")]
            # 切正文。
            same_ids = {id(ln) for ln in same_row}
            # 初始化关键词片段，先放行内内容。
            body = same_row + [ln for ln in body if id(ln) not in same_ids]
            # 加上同排右侧文本。
            text, spans = _join_paragraph(body)
            # 最多检查正文前两行。
            candidate = (inline + (" " if inline and text else "") + text).strip()
            # 遇到中图分类号/DOI/英文摘要等停止。
            candidate = re.split(r"(关键词|关键字)[:：]", candidate)[0].strip()
            # 停止。
            cjk_new = len(re.findall(r"[\u4e00-\u9fff]", candidate))
            # 英文占比过高，可能是英文摘要，不是关键词。
            if abstract_node is not None and len(re.findall(r"[\u4e00-\u9fff]", abstract_node.text)) >= cjk_new:
                # 停止。
                continue
            # 去重后加入。
            abstract_text = candidate
            # 合并为原始字符串。
            abstract_node = NodeDraft(
                # 按分号/逗号/空白切分，过滤长度，最多 12 个。
                new_id("node"), None, 1, "abstract", 2, "摘要",
                # 统计关键词中文字符数。
                abstract_text, h.line.page, body[-1].page if body else h.line.page, 0.97,
            # 已有更完整的关键词节点则跳过。
            )
            # 继续。
            abstract_node.page_spans = spans
        # 创建关键词节点。
        elif kind == "keywords":
            # 标题“关键词”，正文用中文分号连接。
            body = slice_body(idx)
            # 如果这是参考文献标题。
            parts = [inline] if inline else []
            # 已有参考文献则只取第一次。
            parts.extend(ln.text for ln in same_row)
            # 跳过重复。
            for ln in body[:2]:
                # 切正文。
                if re.search(r"(中图|文献标志|DOI|Abstract|Key\s*words)", ln.text, re.I):
                    # 正文片段，先放行内内容。
                    break
                # 逐行检查正文。
                if len(re.findall(r"[A-Za-z]", ln.text)) > len(ln.text) * 0.6:
                    # 以 [数字] 开头计一条参考文献。
                    break
                # 计数 +1。
                if ln.text not in parts:
                    # 所有正文行都加入文本。
                    parts.append(ln.text)
            # 如果没有方括号编号，尝试数字编号格式。
            raw = " ".join(p for p in parts if p)
            # 合并文本。
            keywords = [k.strip() for k in re.split(r"[;；,，\s]+", raw) if 1 < len(k.strip()) < 30][:12]
            # 按行首 [n] 或 n. 统计条目数。
            cjk_new = len(re.findall(CJK, "".join(keywords)))
            # 创建参考文献节点。
            if keywords_node is not None and len(re.findall(CJK, keywords_node.text)) >= cjk_new:
                # 父为空、层级 1、类型 reference、order 99、标题“参考文献”。
                continue
            # 正文换行连接，页码取标题页到 body 末页，置信度 0.95。
            keywords_node = NodeDraft(
                # 结束特殊段循环。
                new_id("node"), None, 1, "keywords", 2, "关键词",
                # 原注释：参考文献标题缺失或为图片时，用 [1] 开头条目兜底。
                "；".join(keywords), h.line.page, body[-1].page if body else h.line.page, 0.95,
            # 记录被判定为参考文献的行 id，正文章节要跳过它们。
            )
        # 如果没有识别到参考文献节点。
        elif kind == "reference":
            # 遍历所有行。
            if reference_node is not None:
                # 找以 [1] 开头的行。
                continue
            # 取后续 8 行窗口。
            body = slice_body(idx)
            # 窗口内至少 3 条 [n] 条目，才确认是参考文献区。
            text_parts = [inline] if inline else []
            # 从 [1] 到文末都视为参考文献。
            for ln in body:
                # 收集文本行。
                if re.match(r"^\[\d+\]", ln.text):
                    # 统计条目数。
                    ref_count += 1
                # 创建兜底参考文献节点。
                text_parts.append(ln.text)
            # 正文为所有行换行连接。
            if ref_count == 0:
                # 页码取第一条和最后一条，置信度 0.9。
                joined = "\n".join(text_parts)
                # 记录这些行的 id。
                ref_count = len(re.findall(r"(?:^|\n)\s*(?:\[\d+\]|\d+[.、])\s*\S", joined))
            # 找到一组即可停止。
            reference_node = NodeDraft(
                # 原注释：正文章节。
                new_id("node"), None, 1, "reference", 99, "参考文献",
                # 章节节点列表（顶层）。
                "\n".join(text_parts), h.line.page, body[-1].page if body else h.line.page, 0.95,
            # 层级栈：用于把新标题挂到正确的父节点下。
            )
# 已出现题注的去重集合，键为 (图/表, 编号)。

    # 这些类型不进入正文章节。
    # 部分 PDF 的“参考文献”标题缺失或为图片：以 [1] 起头的连续条目兜底
    # 遍历所有标题候选。
    ref_line_ids: set[int] = set()
    # 解析标签。
    if reference_node is None:
        # 跳过摘要/关键词/参考文献。
        for i, ln in enumerate(ordered):
            # 跳过。
            if re.match(r"^\[1\]", ln.text):
                # 作者简介单独建节点。
                window = ordered[i : i + 8]
                # 切正文。
                if sum(1 for x in window if re.match(r"^\[\d+\]", x.text)) >= 3:
                    # 合并正文。
                    ref_lines = ordered[i:]
                    # 创建 bio 节点。
                    text_parts = [x.text for x in ref_lines]
                    # 父为空、层级 1、类型 bio、order 98。
                    ref_count = sum(1 for x in ref_lines if re.match(r"^\[\d+\]", x.text))
                    # 正文为行内 + 后续文本。
                    reference_node = NodeDraft(
                        # 页码范围。
                        new_id("node"), None, 1, "reference", 99, "参考文献",
                        # 设置分页跨度。
                        "\n".join(text_parts), ln.page, ref_lines[-1].page, 0.9,
                    # 加入节点列表。
                    )
                    # 作者简介后清空层级栈，后续章节从顶层重新开始。
                    ref_line_ids = {id(x) for x in ref_lines}
                    # 继续。
                    break
# 限制标题层级在 1~4。

    # 弹出栈中层级 >= 当前层级的节点，找到真正父节点。
    # 正文章节
    # 弹栈。
    section_nodes: list[NodeDraft] = []
    # 栈顶即父节点；空栈则父为空。
    stack: list[NodeDraft] = []
    # 创建章节节点。
    caption_seen: set[tuple[str, str]] = set()
    # 节点 ID。
    skip_kinds = {"abstract", "keywords", "reference"}
    # 父节点 ID。
    for idx, h in headings:
        # 层级。
        kind, inline = _label_parts(h.line.text)
        # 类型 section。
        if kind in skip_kinds:
            # 同级顺序：父节点已有子节点数 +1；顶层先给 1，后面统一重排。
            continue
        # 标题。
        if kind == "bio":
            # 正文先空，稍后填。
            body = slice_body(idx)
            # 起始页。
            text, spans = _join_paragraph(body)
            # 结束页初始为标题页，后续按正文更新。
            node = NodeDraft(
                # 有编号的标题置信度 0.95，否则 0.82。
                new_id("node"), None, 1, "bio", 98, "作者简介",
                # 如果有父节点。
                (inline + text).strip(), h.line.page, body[-1].page if body else h.line.page, 0.9,
            # 挂到父节点 children。
            )
            # 否则作为顶层章节。
            node.page_spans = spans
            # 加入顶层列表。
            nodes.append(node)
            # 所有节点列表也加入该章节。
            stack = []
            # 当前节点入栈，成为后续标题的潜在父节点。
            continue
        # 原注释：本节正文到下一个任意标题为止。
        level = max(1, min(4, h.level))
        # 切正文。
        while stack and stack[-1].level >= level:
            # 收集正文行（排除题注和参考文献）。
            stack.pop()
        # 遍历正文行。
        parent = stack[-1] if stack else None
        # 跳过兜底参考文献行。
        node = NodeDraft(
            # 跳过。
            new_id("node"),
            # 跳过混入的摘要/关键词/参考文献标签行。
            parent.node_id if parent else None,
            # 跳过。
            level,
            # 判断是否图表题注。
            "section",
            # 题注且长度合理。
            (len(parent.children) + 1) if parent else 1,
            # 提取图/表编号。
            h.title,
            # 生成去重键。
            "",
            # 已出现过则跳过。
            h.line.page,
            # 跳过重复题注。
            h.line.page,
            # 登记题注键。
            0.95 if re.match(r"^[0-9一二三四五六七八九十]", h.title) else 0.82,
        # 创建题注节点。
        )
        # 节点 ID。
        if parent:
            # 父节点为当前章节。
            parent.children.append(node)
        # 层级为章节 +1。
        else:
            # 类型 figure/table。
            section_nodes.append(node)
        # 同级顺序接在已有子节点后。
        nodes.append(node)
        # 标题和正文都用题注原文。
        stack.append(node)
# 页码。

        # 挂到章节 children。
        # 本节正文（到下一个任意标题为止）
        # 加入全部节点。
        body = slice_body(idx)
        # 否则是普通正文。
        body_text: list[Line] = []
        # 加入正文行列表。
        for ln in body:
            # 合并正文并得到 spans。
            if id(ln) in ref_line_ids:
                # 写入章节正文。
                continue
            # 写入分页跨度。
            if _label_parts(ln.text)[0] in ("abstract", "keywords", "reference"):
                # 有正文时。
                continue
            # 章节结束页取正文最大页。
            cap = _caption_of(ln.text)
            # 否则。
            if cap and len(ln.text) < 120:
                # 没有正文时结束页保持标题页。
                m = re.match(r"^(图|表)\s*(\d+)", ln.text)
                # 循环结束。
                key = (m.group(1), m.group(2)) if m else (cap, ln.text[:12])
                # 如果有参考文献节点。
                if key in caption_seen:
                    # 追加到节点列表。
                    continue
                # 如果有摘要节点。
                caption_seen.add(key)
                # 追加。
                cap_node = NodeDraft(
                    # 如果有关键词节点。
                    new_id("node"), node.node_id, node.level + 1, cap,
                    # 追加。
                    len(node.children) + 1, ln.text, ln.text, ln.page, ln.page, 0.9,
                # 用最终节点正文重新切分关键词，保证与展示一致。
                )
                # 原注释：新闻导语不能丢。
                node.children.append(cap_node)
                # 旧实现会把第一个小标题前的导语丢掉。
                nodes.append(cap_node)
            # 把它并入第一节，保证全文可检索。
            else:
                # 仅新闻类且有章节和标题时处理。
                body_text.append(ln)
        # 第一个标题的全局索引。
        text, spans = _join_paragraph(body_text)
        # 收集导语行。
        node.text = text
        # 遍历第一个标题之前的行。
        node.page_spans = spans
        # 跳过题名行本身。
        if body_text:
            # 跳过。
            node.page_end = max(ln.page for ln in body_text)
        # 取文本。
        else:
            # 太短的行（栏目名/日期）跳过。
            node.page_end = h.line.page
# 跳过。

    # 排除报纸名称/版次/日期等页眉噪声。
    if reference_node:
        # 跳过。
        nodes.append(reference_node)
    # 加入导语。
    if abstract_node:
        # 有导语时才处理。
        nodes.append(abstract_node)
    # 合并导语。
    if keywords_node:
        # 第一节。
        nodes.append(keywords_node)
        # 把导语插到第一节正文前。
        keywords = [k for k in keywords_node.text.split("；") if k]
# 合并分页跨度。

    # 更新起始页为更早的导语页。
    # 报纸/报道类文档常在第一个小标题之前先写导语；这段文字没有标题可挂，
    # 原注释：父节点页码覆盖子树。
    # 旧实现会直接丢失。把它并入第一节，保证全文可检索、可出题。
    # 递归计算节点及其子树的页码范围。
    if doc_type == "news" and section_nodes and headings:
        # 初始范围取节点自身。
        first_idx = headings[0][0]
        # 遍历子节点。
        lead_lines = []
        # 递归得到子范围。
        for ln in ordered[:first_idx]:
            # 扩展到最小/最大页。
            if ln is title_line:
                # 写回节点。
                continue
            # 返回范围供父节点使用。
            t = ln.text.strip()
            # 对所有顶层节点执行。
            if len(t) < 20:
                # 只处理父为空的节点，避免重复递归。
                continue
            # 递归扩展。
            if re.search(r"(人民日报|第\d+版|网络世界|年/.*月/.*日)", t):
                # 原注释：统一排序与路径。
                continue
            # 顶层类型优先级：题名、作者、摘要、关键词、章节、作者简介、参考文献。
            lead_lines.append(ln)
        # 取所有顶层节点。
        if lead_lines:
            # 按优先级、起始页、原 order_no 排序。
            lead_text, lead_spans = _join_paragraph(lead_lines)
            # 递归分配最终顺序号和路径。
            first = section_nodes[0]
            # 设置同级顺序。
            first.text = (lead_text + "\n" + first.text).strip()
            # 路径 = 父路径 + 顺序 + “/”。
            first.page_spans = lead_spans + first.page_spans
            # 遍历子节点。
            first.page_start = min(first.page_start, min(ln.page for ln in lead_lines))
# 递归，子节点顺序从 1 开始。

    # 对每个顶层节点执行。
    # 父节点页码覆盖子树
    # 从 1 开始编号。
    def extend_pages(node: NodeDraft) -> tuple[int, int]:
        # 递归赋值。
        start, end = node.page_start, node.page_end
        # 递归生成器：深度优先遍历所有节点。
        for child in node.children:
            # 遍历输入列表。
            cs, ce = extend_pages(child)
            # 先产出当前节点。
            start, end = min(start, cs), max(end, ce)
        # 再递归产出其所有子节点。
        node.page_start, node.page_end = start, end
        # 统计 figure 节点数。
        return start, end
# 统计 table 节点数。

    # 原注释：题注按图/表编号去重，包含首节前的图表。
    for node in nodes:
        # 题注键集合。
        if node.parent_id is None:
            # 遍历所有行。
            extend_pages(node)
# 识别题注。

    # 提取图/表编号。
    # 统一排序与路径
    # 同时有题注和编号才计入。
    priority = {"title": 0, "author": 1, "abstract": 2, "keywords": 3, "section": 5, "bio": 8, "reference": 9}
    # 加入去重集合。
    top = [n for n in nodes if n.parent_id is None]
    # 如果全文存在题注。
    top.sort(key=lambda n: (priority.get(n.node_type, 6), n.page_start, n.order_no))
# 取节点统计与题注统计的较大值，避免遗漏前置图表。

    # 表格同理。
    def assign(node: NodeDraft, order: int, parent_path: str) -> None:
        # 如果有参考文献节点且有正文。
        node.order_no = order
        # 提取所有 [n] 编号。
        node.path = f"{parent_path}{order}/"
        # 如果有编号。
        for idx, child in enumerate(node.children, start=1):
            # 参考文献数取最大编号，容忍中间缺号。
            assign(child, idx, node.path)
# 否则。

    # 按行首 [n] 计数兜底。
    for idx, node in enumerate(top, start=1):
        # 统计章节节点数。
        assign(node, idx, "/")
# 构造统计字典。

    # 节点总数。
    def all_nodes(items: Iterable[NodeDraft]) -> Iterable[NodeDraft]:
        # 知识块数由后续流水线填写，这里先 0。
        for it in items:
            # 知识点数同理。
            yield it
            # 图数量。
            yield from all_nodes(it.children)
# 表数量。

    # 参考文献数量。
    figures = sum(1 for n in all_nodes(nodes) if n.node_type == "figure")
    # 章节数量。
    tables = sum(1 for n in all_nodes(nodes) if n.node_type == "table")
    # 返回节点列表和元数据。
    # 统计口径：全文题注按“图/表 + 编号”去重，包含首节之前的前置图表
    caption_keys: set[tuple[str, str]] = set()
    for ln in ordered:
        # 报告：统计数据 + 摘要/关键词/作者。
        cap = _caption_of(ln.text)
        m = re.match(r"^(图|表)\s*(\d+)", ln.text)
        if cap and m:
            # ========== 六、质量门：给解析结果打分并给出可执行告警 ==========
            caption_keys.add((cap, m.group(2)))
    # 统计页面文本质量，输出分数、等级、问题列表和指标；供人工复核/前端提示。
    if caption_keys:
        # NFKC 归一化，避免全角字符被误判为乱码。
        figures = max(figures, sum(1 for cap, _ in caption_keys if cap == "figure"))
        # 去掉所有空白，按可见字符计算比例。
        tables = max(tables, sum(1 for cap, _ in caption_keys if cap == "table"))
    # 总字符数；为 0 时用 1 避免除零。
    if reference_node and reference_node.text:
        # 真正的乱码：替换字符 U+FFFD 和无法显示的方块 U+25A1。
        nums = [int(n) for n in re.findall(r"\[(\d{1,3})\]", reference_node.text)]
        # 统计替换字符/方块数量。
        if nums:
            # 统计控制字符数量。
            ref_count = max(ref_count, max(nums))
        # 乱码总数。
        else:
            # 乱码率，封顶 1.0。
            ref_count = max(ref_count, len(re.findall(r"(?:^|\n)\s*\[\d+\]", reference_node.text)))
    # 私有区字符常来自公式字体（如 U+F03D 表示等号），不是 OCR 失败。
    sections = [n for n in nodes if n.node_type == "section"]
    # 公式字形数量。
    stats = {
        # 公式字形比例，保留 4 位小数。
        "node_count": len(nodes),
        # 页数。
        "chunk_count": 0,
        # OCR 页数，来自 stats。
        "kp_count": 0,
        # 预期字符数：未 OCR 页按 1100 字、OCR 页按 300 字估算。
        "figure_count": figures,
        # 覆盖率 = 实际/预期，封顶 1.0。
        "table_count": tables,
        # 章节数 >=4 时标题置信度高。
        "reference_count": ref_count,
        # 给 0.95。
        "section_count": len(sections),
    # 章节数 >=2 时中等。
    }
    # 给 0.8。
    return nodes, {"stats": stats, "abstract": abstract_text, "keywords": keywords, "authors": authors}
# 章节太少。

# 给 0.5。

# 完整度：题名、摘要、章节、参考文献四项各占 25%。
# ---------------------------------------------------------------------------
# 统计满足的项数并除以 4。
def quality_gate(page_texts: list[str], stats: dict, title: str, abstract: str) -> dict:
    # 有图表时图注关联率 0.95，否则 1.0。
    # NFKC 归一化后再统计：全角数字/标点不应被误判为乱码
    # 加权总分：覆盖 30%、标题 25%、乱码 20%、完整度 15%、图表 10%。
    normalized = unicodedata.normalize("NFKC", "\n".join(page_texts))
    # 限制在 0~1 并保留 3 位小数。
    compact = re.sub(r"\s", "", normalized)
    # >=0.85 pass，>=0.6 review，否则 fail。
    total = len(compact) or 1
    # 问题列表。
    # 真正的乱码：替换字符、无法显示的方块、控制字符
    # 覆盖率低于 98%，且不是“OCR 后达到 60%”的可接受情况。
    replacement = len(re.findall(r"[\ufffd\u25a1]", compact))
    # 追加 TEXT_COVERAGE_LOW 告警。
    control = len(re.findall(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", compact))
    # 乱码率超过 0.5%。
    garbled = replacement + control
    # 追加 GARBLED_TEXT 告警。
    garbled_rate = min(1.0, garbled / total)
    # 章节少于 3 个。
    # 私有区字符通常来自 PDF 的公式字体（如 \uf03d 表示等号），不是 OCR 失败
    # 追加 STRUCTURE_INCOMPLETE 告警。
    formula_glyphs = len(re.findall(r"[\ue000-\uf8ff]", compact))
    # 返回质量报告。
    formula_rate = round(formula_glyphs / total, 4)
    # 总分。
    page_count = len(page_texts)
    # 等级。
    ocr_pages = int(stats.get("ocr_pages") or 0)
    # 问题列表。
    expected = max(1, (page_count - ocr_pages) * 1100 + ocr_pages * 300)
    # 指标明细。
    coverage = min(1.0, total / expected)
    # 文本覆盖率。
    if stats.get("section_count", 0) >= 4:
        # 乱码率。
        heading = 0.95
    # 公式字形率。
    elif stats.get("section_count", 0) >= 2:
        # 标题置信度。
        heading = 0.8
    # 结构完整度。
    else:
        # 图表关联率。
        heading = 0.5
    # OCR 页数。
    completeness = sum(
        # 当前未记录失败页，保留空列表供扩展。
        1 for cond in [bool(title and len(title) >= 4), bool(abstract), stats.get("section_count", 0) >= 3, stats.get("reference_count", 0) > 0]
        if cond
    ) / 4
    # 把二维单元格转成 Markdown 表格；列数不齐时补齐。
    figure_rate = 0.95 if (stats.get("figure_count", 0) + stats.get("table_count", 0) + stats.get("chart_count", 0)) > 0 else 1.0
    # 每格压缩空白；空行丢弃。
    score = 0.30 * coverage + 0.25 * heading + 0.20 * (1 - min(1, garbled_rate * 20)) + 0.15 * completeness + 0.10 * figure_rate
    # 去掉全空行。
    score = round(max(0.0, min(1.0, score)), 3)
    # 少于两行无法构成表格（至少表头+一行数据）。
    level = "pass" if score >= 0.85 else ("review" if score >= 0.6 else "fail")
    # 返回空串表示无表格。
    issues = []
    # 取最大列数。
    if coverage < 0.98 and not (ocr_pages and coverage >= 0.6):
        # 每行补空单元格到等宽。
        issues.append({"code": "TEXT_COVERAGE_LOW", "message": f"文本覆盖率约 {coverage:.0%}，可能存在图片型页面"})
    # 表头行。
    if garbled_rate > 0.005:
        # 分隔行。
        issues.append({"code": "GARBLED_TEXT", "message": f"乱码率 {garbled_rate:.2%}，建议 OCR 或人工校正"})
    # 数据行。
    if stats.get("section_count", 0) < 3:
        # 用换行拼接。
        issues.append({"code": "STRUCTURE_INCOMPLETE", "message": "章节识别不足，结构树可能不完整"})
    return {
        "score": score,
        # 从文本中恢复 Markdown 表格块，跳过正文中的竖线。
        "level": level,
        # 按行切分。
        "issues": issues,
        # 表格块列表。
        "metrics": {
            # 手动扫描行索引。
            "text_coverage": round(coverage, 3),
            # 非竖线开头跳过。
            "garbled_rate": round(garbled_rate, 4),
            # 索引 +1。
            "formula_glyph_rate": formula_rate,
            # 继续。
            "heading_confidence": heading,
            # 记录表格起始行。
            "structure_completeness": round(completeness, 2),
            # 当前表格的行组。
            "figure_link_rate": figure_rate,
            # 连续收集以 | 开头且至少两个竖线的行。
            "ocr_pages": ocr_pages,
        # 加入。
        },
        # 前进。
        "failed_pages": [],
    # 少于两行不是有效表格。
    }
# 继续外层扫描。

# 解析每行单元格。

# 去掉首尾竖线后按 | 分割并 strip。
def _table_markdown(rows: list[list[str]]) -> str:
    # 跳过 Markdown 分隔行（|---|---|）。
    clean = [[" ".join((c or "").split()) for c in row] for row in rows if row]
    # 继续。
    clean = [r for r in clean if any(r)]
    # 加入数据行。
    if len(clean) < 2:
        # 至少两行且至少两列才算表格。
        return ""
    # 转成标准 Markdown。
    width = max(len(r) for r in clean)
    # 成功则记录。
    clean = [r + [""] * (width - len(r)) for r in clean]
    # 加入块列表。
    lines = ["| " + " | ".join(clean[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    # 返回所有表格块。
    lines += ["| " + " | ".join(r) + " |" for r in clean[1:]]
    return "\n".join(lines)

# 抽取 PDF/DOCX 表格单元格，返回页级 Markdown 块；失败时返回已收集部分。

# 结果列表。
def _markdown_tables(text: str) -> list[str]:
    # 整体兜底，表格抽取失败不影响主流程。
    """从 OCR 的 Markdown 输出中恢复表格，跳过普通正文里的竖线。"""
    # PDF 分支。
    lines = (text or "").splitlines()
    # 动态导入，避免非 PDF 环境依赖。
    blocks: list[str] = []
    # 打开 PDF。
    i = 0
    # 逐页处理。
    while i < len(lines):
        # 单页表格检测异常不影响其他页。
        if not lines[i].strip().startswith("|"):
            # find_tables 可能依赖可选组件。
            i += 1
            # 异常时按空表处理。
            continue
        # 遍历检测到的表格。
        start = i
        # 单元格提取也可能单表失败。
        group: list[str] = []
        # 提取二维单元格。
        while i < len(lines) and lines[i].strip().startswith("|") and lines[i].count("|") >= 2:
            # 失败跳过该表。
            group.append(lines[i].strip())
            # 转 Markdown。
            i += 1
        # 空表跳过。
        if len(group) < 2:
            # 记录表格块：页码、Markdown、行数、单元格数。
            continue
        # 同时把 Markdown 追加到该页文本，保证检索命中。
        rows: list[list[str]] = []
        # 关闭文档。
        for line in group:
            # DOCX 分支。
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # 惰性导入。
            if cells and all(re.fullmatch(r":?-{2,}:?", c or "") for c in cells):
                # 打开文档。
                continue
            # 遍历所有表格。
            rows.append(cells)
        # 取单元格文本。
        if len(rows) >= 2 and max(len(r) for r in rows) >= 2:
            # 转 Markdown。
            md = _table_markdown(rows)
            # 有效表格才记录。
            if md:
                # DOCX 统一记第 1 页。
                blocks.append(md)
    # 追加到页面文本。
    return blocks
# 收集已有 Markdown，避免与 OCR 文本中的表格重复。

# 扫描每页文本。

# 逐页提取内嵌 Markdown 表格。
def extract_table_blocks(path: Path, page_texts: list[str]) -> list[dict]:
    # 新表格才加入。
    """抽取 PDF/DOCX 表格单元格，返回页级 Markdown 表格块。"""
    # 登记。
    blocks: list[dict] = []
    # 记录块；行数为换行数 +1，单元格数为每行竖线数减 1。
    try:
        # 整体异常时返回已收集部分。
        if path.suffix.lower() == ".pdf":
            # 返回表格块列表。
            import pymupdf
            doc = pymupdf.open(str(path))
            for idx, page in enumerate(doc):
                # 提取 PDF 公式字体行 / DOCX OMML 公式文本，作为公式结构节点。
                try:
                    # 结果列表。
                    tables = page.find_tables()
                # 整体兜底，公式抽取失败不影响主流程。
                except Exception:
                    # PDF 分支。
                    tables = []
                # 动态导入。
                for table in tables:
                    # 打开文档。
                    try:
                        # 逐页处理。
                        data = table.extract()
                    # 本页已见文本集合，避免重复。
                    except Exception:
                        # 遍历文本块。
                        continue
                    # 遍历块内行。
                    md = _table_markdown(data or [])
                    # 取 spans。
                    if not md:
                        # 拼接 span 文本并去空白。
                        continue
                    # 太短或重复的行跳过。
                    blocks.append({"page": idx + 1, "markdown": md, "rows": len(data or []),
                                   # 跳过。
                                   "cells": sum(len(r) for r in (data or []))})
                    # 拼接字体名并转小写。
                    if idx < len(page_texts):
                        # 是否含私有区字符（常见于公式字体）。
                        page_texts[idx] = (page_texts[idx] or "") + "\n\n" + md
            # 是否使用数学字体（Cambria Math、Computer Modern 等）。
            doc.close()
        # 去掉私有区字符后的可读文本。
        elif path.suffix.lower() == ".docx":
            # 既无私有区字符也无数学字体，跳过。
            from docx import Document
            # 跳过。
            document = Document(path)
            # 长度 3~120，且不含替换字符。
            for table in document.tables:
                # 跳过。
                data = [[cell.text for cell in row.cells] for row in table.rows]
                # 必须包含至少一个数学符号/字母/数字。
                md = _table_markdown(data)
                # 跳过。
                if md:
                    # 统计数学字符数。
                    blocks.append({"page": 1, "markdown": md, "rows": len(data),
                                   # 至少 2 个数学字符，且不是纯数字/小数。
                                   "cells": sum(len(r) for r in data)})
                    # 跳过。
                    if page_texts:
                        # 中文字符远多于数学字符，说明是正文而非公式。
                        page_texts[0] = (page_texts[0] or "") + "\n\n" + md
        # 跳过。
        seen = {b.get("markdown") for b in blocks}
        # 登记本页已见。
        for page_no, text in enumerate(page_texts, 1):
            # 记录公式块：页码 + 文本。
            for md in _markdown_tables(text):
                # 所有页处理完。
                if md and md not in seen:
                    # 超过 80 条公式就停止，防止性能/存储膨胀。
                    seen.add(md)
                    # 跳出页循环。
                    blocks.append({"page": page_no, "markdown": md, "rows": md.count("\n") + 1,
                                   # 关闭文档。
                                   "cells": sum(line.count("|") - 1 for line in md.splitlines())})
    # DOCX 分支。
    except Exception:
        # 惰性导入 Document。
        return blocks
    # qn 用于构造 OMML 命名空间标签。
    return blocks
# 打开文档。

# 去重集合。

# 遍历文档 body 下所有 XML 元素。
def extract_formula_blocks(path: Path) -> list[dict]:
    # 只处理 OMML 数学公式元素。
    """提取 PDF 公式字体行 / DOCX OMML 公式文本，作为公式结构节点。"""
    # 跳过非公式元素。
    formulas: list[dict] = []
    # 收集公式内所有 m:t 文本节点。
    try:
        # 拼接并清洗。
        if path.suffix.lower() == ".pdf":
            # 非空且未重复才记录。
            import pymupdf
            # 登记。
            doc = pymupdf.open(str(path))
            # 公式统一记第 1 页。
            for idx, page in enumerate(doc):
                # 整体异常时返回已收集公式。
                seen = set()
                # 返回公式块列表。
                for block in page.get_text("dict").get("blocks", []):
                    for line in block.get("lines", []):
                        spans = line.get("spans", [])
                        # 允许的图表类型；模型输出越界时统一归为 other。
                        text = "".join(s.get("text", "") for s in spans).strip()
                        if not text or len(text) < 2 or text in seen:
                            continue
                        # 把视觉模型的 JSON 输出规范化为可入库的图表数据；拒绝没有数值的伪图表。
                        fonts = " ".join(str(s.get("font", "")) for s in spans).lower()
                        # 非 dict 或明确 is_chart=false 时拒绝。
                        has_private = bool(re.search(r"[\ue000-\uf8ff]", text))
                        # 读取图表类型，默认 other。
                        has_math_font = any(k in fonts for k in ("math", "cmmi", "cmsy", "symbol", "stix", "mt extra"))
                        # 不在白名单则归为 other。
                        clean = re.sub(r"[\ue000-\uf8ff]", "", text).strip()
                        # 类别列表：转字符串并去空。
                        if not (has_private or has_math_font):
                            # 读取 series，默认空列表。
                            continue
                        # 类型不是 list 时视为空。
                        if len(clean) < 3 or len(clean) > 120 or "\ufffd" in clean:
                            # 规范化后的系列列表。
                            continue
                        # 遍历每个系列。
                        if not re.search(r"[=+\-×÷∑∫√^_0-9A-Za-zα-ωΑ-Ω]", clean):
                            # 非 dict 系列跳过。
                            continue
                        # 系列名，缺省自动编号。
                        math_chars = re.findall(r"[=+\-×÷∑∫√^_0-9A-Za-zα-ωΑ-Ω]", clean)
                        # 值列表。
                        if len(math_chars) < 2 or re.fullmatch(r"[\d\s.,]+", clean):
                            # 遍历原始值。
                            continue
                        # 尝试转 float；空值转 None。
                        if len(re.findall(r"[\u4e00-\u9fff]", clean)) > len(math_chars) * 2:
                            # 转换失败也记 None，不中断整块。
                            continue
                        # 至少有一个有效数值才保留系列。
                        seen.add(text)
                        # 追加规范系列，名称截断到 80 字。
                        formulas.append({"page": idx + 1, "text": clean})
                # points 兜底列表。
                if len(formulas) >= 80:
                    # 遍历原始点。
                    break
            # 非 dict 点跳过。
            doc.close()
        # 转换 y 值为 float。
        elif path.suffix.lower() == ".docx":
            # 空值转 None。
            from docx import Document
            # 转换失败置 None。
            from docx.oxml.ns import qn
            # 有效点才保留，x/series 统一字符串。
            document = Document(path)
            # 如果模型只给 points 没给 series。
            seen = set()
            # 按系列名聚合 y 值。
            for elem in document.element.body.iter():
                # 遍历点。
                if elem.tag not in (qn("m:oMath"), qn("m:oMathPara")):
                    # 同名系列归并；无系列名归为“数值”。
                    continue
                # 转成 series 结构。
                parts = [t.text or "" for t in elem.iter() if t.tag == qn("m:t")]
                # 没有类别时用点的 x 填充。
                text = norm("".join(parts))
                # 仍无 series（没有数值）则拒绝。
                if text and text not in seen:
                    # 返回 None。
                    seen.add(text)
                    # 计算最大列宽：取系列长度与类别数的最大值。
                    formulas.append({"page": 1, "text": text})
    # 宽度为 0 无法成表。
    except Exception:
        # 返回 None。
        return formulas
    # 没有类别时用序号补齐。
    return formulas
# 补齐到 width。

# 构造表格行。

# 遍历类别。
CHART_TYPES = {"bar", "line", "pie", "scatter", "area", "mixed", "other"}
# 每行首列是类别。

# 遍历每个系列。

# 取对应值，缺失用 None。
def _normalise_chart(raw: dict, page: int, fallback_title: str) -> dict | None:
    # 数值格式化为紧凑字符串，空值留空。
    """把视觉模型输出规范化为可入库的图表数据；拒绝没有数值的伪图表。"""
    # 加入行。
    if not isinstance(raw, dict) or raw.get("is_chart") is False:
        # 表头：类别 + 系列名。
        return None
    # Markdown 表头。
    chart_type = str(raw.get("chart_type") or "other").lower()
    # Markdown 分隔行。
    if chart_type not in CHART_TYPES:
        # Markdown 数据行。
        chart_type = "other"
    # 标题：模型标题 > 题注回退 > 未命名图表，截断 120 字。
    categories = [str(x) for x in (raw.get("categories") or []) if str(x).strip()]
    # 返回规范化字典。
    series_in = raw.get("series") or []
    # 页码、类型、标题。
    if not isinstance(series_in, list):
        # x 轴名，截断 80。
        series_in = []
    # y 轴名，截断 80。
    series: list[dict] = []
    # 单位，截断 40。
    for item in series_in:
        # 类别去重/截断到宽度。
        if not isinstance(item, dict):
            # 系列数据。
            continue
        # 置信度限制 0~1，默认 0.75。
        name = str(item.get("name") or f"系列 {len(series)+1}")
        # Markdown 正文。
        values = []
        # data 字段给前端图表组件复用。
        for value in (item.get("values") or []):
            # 图表类型和标题。
            try:
                # 轴名和单位。
                values.append(float(value) if value is not None and str(value).strip() != "" else None)
            # 类别和系列。
            except Exception:
                # 结束 data。
                values.append(None)
        # 结束返回字典。
        if values and any(v is not None for v in values):
            series.append({"name": name[:80], "values": values})
    points = []
    # 构造图表提取提示词，要求只输出 JSON、无法读出的数值用 null。
    for item in (raw.get("points") or []):
        # 返回提示词。
        if not isinstance(item, dict):
            # 分析页码并限定不猜测。
            continue
        # 非图表返回 is_chart=false。
        try:
            # 要求输出单个 JSON 对象。
            y = float(item.get("y")) if item.get("y") is not None and str(item.get("y")).strip() != "" else None
        # 给出字段格式。
        except Exception:
            # 图表类型枚举。
            y = None
        # 标题与轴。
        if y is not None:
            # 类别与系列。
            points.append({"x": str(item.get("x") or ""), "y": y, "series": str(item.get("series") or "")})
    # 点数据兜底。
    if not series and points:
        # 置信度。
        by_series: dict[str, list[float]] = {}
        # 要求 values 与 categories 对齐。
        for point in points:
            # 无法读出用 null，禁止代码块/解释。
            by_series.setdefault(point["series"] or "数值", []).append(point["y"])
        # 结束提示词。
        series = [{"name": name, "values": values} for name, values in by_series.items()]
        if not categories:
            categories = [p["x"] for p in points]
    # 用视觉模型抽取图表数据；非图表返回空列表。
    if not series:
        # 惰性导入配置与模型客户端。
        return None
    # 开关关闭或模型不可用则不做图表识别。
    width = max([len(s["values"]) for s in series] + [len(categories)])
    # 返回空列表。
    if width == 0:
        # 整体兜底。
        return None
    # 后缀。
    if not categories:
        # 是否图片。
        categories = [str(i + 1) for i in range(width)]
    # 打开文档。
    rows = []
    # 最多处理 30 页，默认 6 页。
    for i, category in enumerate(categories[:width]):
        # DPI 限制 90~300。
        row = [category]
        # 图片只取第 0 页；PDF 先空列表再筛。
        for s in series:
            # PDF 筛选。
            value = s["values"][i] if i < len(s["values"]) else None
            # 遍历页。
            row.append("" if value is None else (f"{value:g}"))
        # 取对应页文本，越界用空串。
        rows.append(row)
    # 是否有“图 N”题注。
    header = ["类别"] + [s["name"] for s in series]
    # 是否含大图。
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    # 遍历页面图片。
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    # 图片显示区域。
    title = str(raw.get("title") or fallback_title or "未命名图表").strip()[:120]
    # 大图阈值：宽 >35%、高 >8%。
    return {
        # 标记大图。
        "page": page, "chart_type": chart_type, "title": title,
        # 跳出内层。
        "x_label": str(raw.get("x_label") or "")[:80],
        # 已大图，跳出。
        "y_label": str(raw.get("y_label") or "")[:80],
        # 有题注或大图都作为候选。
        "unit": str(raw.get("unit") or "")[:40],
        # 加入候选页。
        "categories": categories[:width],
        # 截断到最大页数。
        "series": series,
        # 选择图表模型，回退 OCR 模型，最后用默认。
        "confidence": min(1.0, max(0.0, float(raw.get("confidence") or 0.75))),
        # 结果列表。
        "markdown": "\n".join(lines),
        # 遍历候选页。
        "data": {"chart_type": chart_type, "title": title, "x_label": raw.get("x_label") or "",
                 # 越界跳过。
                 "y_label": raw.get("y_label") or "", "unit": raw.get("unit") or "",
                 # 单页异常跳过。
                 "categories": categories[:width], "series": series},
    # 取页。
    }
# 渲染 PNG。

# 编码 data URL。

# 调用 JSON 模式视觉接口。
def _chart_prompt(page_no: int) -> str:
    # system 约束只输出合法 JSON。
    return (
        # user 携带提示词和图片。
        f"你是严谨的图表数据提取引擎。分析第 {page_no} 页图片中的图表，只提取图中可读数据，不猜测。"
        # 图片 data URL。
        "如果没有图表（包括纯表格、纯文字），返回 {\"is_chart\": false}。"
        # kind/模型/json 模式参数。
        "如果有图表，只输出一个 JSON 对象，格式："
        # 限制 token 与低温度。
        '{"is_chart":true,"chart_type":"bar|line|pie|scatter|area|mixed|other",'
        # 题注回退默认“图表”。
        '"title":"图表标题","x_label":"X轴","y_label":"Y轴","unit":"单位",'
        # 如果该页有文本。
        '"categories":["类别1","类别2"],"series":[{"name":"系列名","values":[1,2]}],'
        # 找图/Figure/Fig 题注。
        '"points":[{"x":"类别","y":1,"series":"系列名"}],"confidence":0.0}'
        # 有题注则用题注作为标题回退。
        "。values 与 categories 对齐；无法读出的数值用 null；不要输出 Markdown 代码块、不要解释。"
    # 规范化模型输出。
    )
# 有效图表块才加入。

# 加入。

# 单页异常不影响其他页。
def extract_chart_blocks(path: Path, page_texts: list[str]) -> list[dict]:
    # 关闭文档。
    """用视觉模型提取图表数据；表格/正文返回 is_chart=false，不生成节点。"""
    # 返回图表块。
    from . import llm, settings
    # 整体异常返回空列表。
    if not settings.get_bool("chart.enabled", True) or not llm.available():
        return []
    try:
        # 把图表块挂到最近章节；没有题注的图表也生成 chart 节点。
        suffix = path.suffix.lower()
        # 内部递归函数：扁平化节点树。
        is_image = suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp")
        # 遍历节点列表。
        doc = pymupdf.open(str(path))
        # 加入当前节点。
        max_pages = max(1, min(30, settings.get_int("chart.max_pages", 6)))
        # 递归加入子节点。
        dpi = max(90, min(300, settings.get_int("chart.dpi", 140)))
        # 返回扁平列表。
        candidates: list[int] = [0] if is_image else []
        # 扁平化所有节点。
        if not is_image:
            # 只从章节节点中选父节点。
            for idx, page in enumerate(doc):
                # 遍历图表块。
                text = page_texts[idx] if idx < len(page_texts) else ""
                # 候选章节：页码覆盖图表页的章节；没有则退回全部章节。
                has_caption = bool(re.search(r"(图|Figure|Fig\.?)\s*\d+", text or "", re.I))
                # 选页码距离最近的章节作为父节点。
                large = False
                # 构造 chart 节点。
                for img in page.get_images(full=True):
                    # 节点 ID、父 ID。
                    for rect in page.get_image_rects(img[0]):
                        # 层级、类型 chart。
                        if rect.width > page.rect.width * 0.35 and rect.height > page.rect.height * 0.08:
                            # 顺序、标题（缺省“图表 N”）。
                            large = True
                            # 正文 = Markdown + chart-data JSON 代码块。
                            break
                    # 页码与置信度。
                    if large:
                        # 路径暂用父路径，后续可能重排。
                        break
                # 追加到顶层节点列表（注意：父路径已设但未加入父 children）。
                if has_caption or large:
                    # 返回图表统计。
                    candidates.append(idx)
        # 图表数量。
        candidates = candidates[:max_pages]
        # 系列总数。
        client_model = settings.get("chart.model") or settings.get("ocr.model") or None
        # 模型来源标记。
        blocks: list[dict] = []
        for idx in candidates:
            if idx >= len(doc):
                # 把表格单元格文本挂到表格节点，把公式文本挂成章节下的公式节点。
                continue
            # 递归扁平化。
            try:
                # 遍历。
                page = doc[idx]
                # 加入当前节点。
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                # 递归子节点。
                data_url = "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode()
                # 返回。
                raw = llm.chat_json(
                    # 扁平化所有节点。
                    [{"role": "system", "content": "你是图表数据提取引擎，只输出合法 JSON。"},
                     # 已有表格节点。
                     {"role": "user", "content": [
                         # 按顺序把表格块填入对应表格节点。
                         {"type": "text", "text": _chart_prompt(idx + 1)},
                         # 追加 Markdown 到节点正文。
                         {"type": "image_url", "image_url": {"url": data_url}},
                     # 置信度 +0.05，封顶 1。
                     ]}],
                    # 所有章节节点。
                    kind="chart_extract", model=client_model, json_mode=True,
                    # 原注释：没有题注的表格补建 table 节点。
                    max_tokens=2500, temperature=0.1,
                # 从已有表格节点之后的块开始，序号接着排。
                )
                # 候选章节。
                fallback = "图表"
                # 最近章节作为父节点。
                if idx < len(page_texts):
                    # 构造 table 节点。
                    m = re.search(r"((?:图|Figure|Fig\.?)\s*\d+[^\n]{0,100})", page_texts[idx] or "", re.I)
                    # ID、父 ID。
                    if m:
                        # 层级、类型。
                        fallback = m.group(1).strip()
                # 顺序、标题。
                block = _normalise_chart(raw, idx + 1, fallback)
                # 正文为 Markdown。
                if block:
                    # 页码与置信度。
                    blocks.append(block)
            # 路径。
            except Exception:
                # 追加到节点列表。
                continue
        # 同步到表格节点列表。
        doc.close()
        # 遍历公式块。
        return blocks
    # 候选章节。
    except Exception:
        # 最近章节。
        return []
# 构造 formula 节点。

# ID、父 ID。

# 层级、类型。
def attach_chart_nodes(nodes: list[NodeDraft], chart_blocks: list[dict]) -> dict:
    # 顺序、标题、正文、页码。
    """把图表数据挂到最近章节；无题注图表生成 chart 节点。"""
    # 置信度与路径。
    def walk(items):
        # 追加节点。
        out = []
        # 返回表格/公式统计。
        for item in items:
            # 表格块数。
            out.append(item)
            # 单元格数。
            out.extend(walk(item.children))
        # 公式数。
        return out

    all_nodes = walk(nodes)
    # ========== 七、对外入口：parse_document 的完整流水线 ==========
    sections = [n for n in all_nodes if n.node_type == "section"]
    # 解析一篇文档的对外主函数；title_hint/doc_type_hint 允许调用方覆盖自动识别。
    for idx, block in enumerate(chart_blocks, 1):
        # 第 1 步：按扩展名读取页行、页文本、页数。
        candidates = [s for s in sections if s.page_start <= block["page"] <= s.page_end] or sections
        # 第 2 步：文本层薄时 OCR 增强。
        parent = min(candidates, key=lambda s: abs(s.page_start - block["page"])) if candidates else None
        # 第 3 步：抽取表格。
        ChartNode = NodeDraft(
            # 第 4 步：视觉模型抽取图表。
            node_id=new_id("nd"), parent_id=parent.node_id if parent else None,
            # 第 5 步：抽取公式。
            level=(parent.level + 1) if parent else 1, node_type="chart",
            # 第 6 步：估计正文字号。
            order_no=idx, title=block.get("title") or f"图表 {idx}",
            # 第 7 步：识别题名。
            text=(block.get("markdown", "") + "\n\n```chart-data\n" + json.dumps(block.get("data", {}), ensure_ascii=False) + "\n```"),
            # 第 8 步：调用方给了题名提示则覆盖自动识别。
            page_start=block["page"], page_end=block["page"], confidence=block.get("confidence", 0.75),
            # 覆盖题名。
            path=(parent.path if parent else "/"),
        # 第 9 步：先按 other 构建结构树（新闻导语需要二次构建）。
        )
        # 提取章节标题，供文档类型判断。
        nodes.append(ChartNode)
    # 第 10 步：自动判断文档类型；hint 非 auto 时直接采用。
    return {"chart_count": len(chart_blocks), "chart_data_count": sum(len(b.get("series", [])) for b in chart_blocks),
            # 自动/覆盖判断。
            "chart_model": "vision"}
# 原注释：报道类导语处理依赖 doc_type，需要重建。

# 仅新闻类重建。

# 用 doc_type='news' 重建结构树，让第一节吸收导语。
def attach_table_and_formula_nodes(nodes: list[NodeDraft], table_blocks: list[dict], formula_blocks: list[dict]) -> dict:
    # 第 11 步：挂载表格和公式节点。
    """把表格单元格文本挂到表格节点，把公式文本挂成章节下的公式节点。"""
    # 第 12 步：挂载图表节点。
    def walk(items):
        # 取基础统计。
        out = []
        # 合并 OCR 统计。
        for item in items:
            # 合并表格/公式统计。
            out.append(item)
            # 合并图表统计。
            out.extend(walk(item.children))
        # 表格数取结构统计与块数的较大值。
        return out
# 图表数同理。

    # 第 13 步：质量门评分。
    all_nodes = walk(nodes)
    # 第 14 步：统计去空白总字符数。
    table_nodes = [n for n in all_nodes if n.node_type == "table"]
    # 组装最终 ParsedDoc。
    for block, node in zip(table_blocks, table_nodes):
        # 题名。
        node.text = (node.text + "\n\n" + block["markdown"]).strip()
        # 文档类型。
        node.confidence = min(1.0, node.confidence + 0.05)
    # 页数。
    sections = [n for n in all_nodes if n.node_type == "section"]
    # 作者。
    # OCR/图片表格没有“表 x”题注时，补建 table 节点，避免表格文本只停留在页面文本层。
    # 摘要。
    for idx, block in enumerate(table_blocks[len(table_nodes):], start=len(table_nodes) + 1):
        # 关键词。
        candidates = [s for s in sections if s.page_start <= block["page"] <= s.page_end] or sections
        # 结构树节点。
        parent = min(candidates, key=lambda s: abs(s.page_start - block["page"])) if candidates else None
        # 统计。
        TableNode = NodeDraft(
            # 质量报告。
            node_id=new_id("nd"), parent_id=parent.node_id if parent else None,
            # 总字符数。
            level=(parent.level + 1) if parent else 1, node_type="table",
            # 逐页文本。
            order_no=idx, title=f"表格 {idx}", text=block["markdown"],
            # 结束。
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
