"""语义分块、知识点抽取与证据单元构建（设计文档 5.4—5.6）。"""

# ============================================================================
# 【文件职责】
# 本文件是“文档解析流水线”中的知识加工层，负责把解析器输出的结构化节点
# （标题、正文、摘要、表格、图表）加工成两类可供检索和教学使用的数据：
#   1. build_chunks：按句子边界把长文本切成带重叠的语义块（chunk），
#      供向量化、全文检索和 RAG 召回使用；
#   2. extract_knowledge：从正文段落中抽取“知识点”，并为每个知识点绑定
#      一条可回溯到原文的证据（evidence），供引用、复习和质检使用。
#
# 【算法定位】
# 这里使用的是可解释的规则/启发式算法，没有调用大模型，也不访问网络。
# 因此同一份输入通常得到稳定输出，执行速度快，但抽取质量受正则规则和
# 阈值影响；理解这些规则是修改抽取效果的关键。
#
# 【在整个应用中的位置】
# 上游：
#   parser.parse_document() 读取 PDF/DOCX/Markdown，产出 NodeDraft 节点列表；
#   pipeline.run_parse_task() 在后台解析任务中依次调用本文件的函数。
# 下游：
#   pipeline 把 build_chunks() 的结果写入 chunk 表；
#   把 extract_knowledge() 返回的 kps/evidences 分别写入 knowledge_point 和
#   evidence 表；之后 main.py、assistant.py、sessions.py 等模块再读取这些表，
#   用于文档问答、知识点列表、学习会话和引用展示。
#
# 【主要调用关系】
#   pipeline.run_parse_task
#     -> build_chunks(doc_id, parsed.nodes)
#     -> extract_knowledge(doc_id, parsed.nodes)
#          -> split_sentences / classify / _importance
#          -> extract_name / polish_name / _similar / _prerequisites
#   tests/test_smoke.py 也直接调用 extract_knowledge 做冒烟测试。
#
# 【重要边界】
# - 本文件不执行 SQL，也不做权限校验；数据库写入在 pipeline.py，鉴权在 API 层。
# - 本文件没有 async/await；它是同步的 CPU 计算代码，由流水线的工作线程调用。
# - 本文件不主动抛 HTTP 错误码；没有候选时会返回空列表或跳过该节点。
# - 所有 ID 都由 db.new_id(prefix) 生成，格式类似 "kp_1a2b3c4d5e"。
#
# 【核心数据结构】
# Sentence：一句原文 + 它在节点文本中的字符起止位置 + 所在页码。
# chunk：{"chunk_id", "doc_id", "node_id", "text", "token_count", "order_no"}。
# kp：知识点字典，包含名称、类型、定义、重要度、难度、证据 ID、前置知识等。
# evidence：证据字典，记录原文引文、页码、字符区间、强度和上下文。
#
# 【阅读提示】
# 带下划线前缀的函数（如 _clean_name、_importance）表示模块内部辅助函数，
# 不打算被其他模块直接导入；其余函数是对外的加工入口。
# ============================================================================

# 启用“延迟求值”的类型注解：允许在注解中写 list[Sentence] 等新式类型，
# 也让运行时不立即解析注解，避免前向引用和版本兼容问题。
from __future__ import annotations

# 标准库 re：本文件大量使用正则表达式做分句、分类和名称清洗。
import re

# dataclasses.dataclass：用装饰器自动生成 Sentence 的 __init__/__repr__/__eq__，
# 避免手写样板代码，同时让“一句文本 + 元数据”成为清晰的小对象。
from dataclasses import dataclass

# typing.Iterable 是类型注解工具；本文件当前没有直接使用它（可能是重构遗留），
# 保留导入不会影响运行，读者不必在本文件中寻找对应的使用点。
from typing import Iterable

# 从同包的 db 模块导入 ID 生成器。相对导入中的“.”表示 app 包自身；
# new_id 用 UUID 前 10 位生成短 ID，避免不同表之间的主键冲突。
from .db import new_id

# 从同包的 parser 模块导入两个符号：
# - NodeDraft：解析器产出的结构树节点，提供 node_id、parent_id、title、text、
#   page_spans 等字段，是本文件两个主函数的输入元素；
# - CJK：中文字符的正则片段；本文件当前没有直接用到，保留导入不影响运行。
from .parser import NodeDraft, CJK

# 分句正则：匹配“一段不含句末标点的文字 + 可选的句末标点”。
# 方括号里列出的中文/英文标点是分隔符；最后的“?”表示句末标点可以缺失，
# 这样文末最后一句即使没有句号也能被匹配。英文句点“.”故意没有列入，
# 以免把小数、缩写和网址误切成多句。
SENTENCE_RE = re.compile(r"[^。！？；!?;]+[。！？；!?;]?")

# 知识点分类规则表。每个元素是三元组：
#   (类型标识, 判定正则, 该类型的初始重要度)
# 列表顺序就是优先级：classify() 从上往下找到第一条命中规则就返回，
# 所以更具体的“limitation”放在“conclusion”等通用规则之前。
# 分数是后续计算重要度的起点，不是模型概率。
TYPE_RULES: list[tuple[str, str, float]] = [
    # “局限/不足/挑战”等词优先识别为研究局限；0.70 是初始重要度。
    ("limitation", r"(局限|不足|挑战|有待|尚未|瓶颈|未来方向|仍需|难以)", 0.70),
    # “表明/说明/因此/结论”等词识别为结论；结论通常较重要，初始 0.82。
    ("conclusion", r"(表明|说明|因此|结论|发现|可见|证明|结果显示|总体来看|由此可见)", 0.82),
    # 百分比、F1、准确率、数据集、样本、实验提升等识别为数据类；初始 0.80。
    ("data", r"(\d+(\.\d+)?%|F1|准确率|召回率|精确率|数据集|样本|参数|提升了|下降了|达到|实验结果)", 0.80),
    # 方法、模型、算法、框架、流程、训练、检索等识别为方法类；初始 0.76。
    ("method", r"(方法|模型|算法|框架|流程|机制|步骤|策略|训练|检索|生成|微调|架构)", 0.76),
    # “是指/定义为/称为/概念”等定义句式识别为概念类；初始 0.80。
    ("concept", r"(是指|指的是|定义为|称为|所谓|简称|概念|术语|是一种|是一个|即)", 0.80),
    # 观点、主张、建议、政策等识别为论点类；初始 0.66，作为较弱的兜底类型。
    ("claim", r"(认为|主张|应当|需要|建议|政策|取向|逻辑|观点|强调|指出)", 0.66),
]

# 知识点类型到“初始难度”的映射（整数，通常 1~5，数字越大越难）。
# 这里把概念设为 2，方法/数据/结论设为 3，观点/局限设为 4；
# 这是给学习路径排序用的启发式估计，不代表严格的认知难度测评。
TYPE_DIFFICULTY = {
    "concept": 2,       # 概念：只需理解定义，难度较低
    "method": 3,        # 方法：需要理解步骤和原理
    "data": 3,          # 数据：需要读懂指标和实验条件
    "conclusion": 3,    # 结论：需要结合前文判断适用范围
    "claim": 4,         # 论点：涉及立场和价值判断，较难
    "limitation": 4,    # 局限：需要识别边界与未解决问题，较难
}

# 章节角色加分表：如果章节标题包含某个关键词，就给该节知识点增加对应分数。
# 这体现“方法/实验/结果”等章节里的句子往往比引言中的句子更值得学习。
# 注意：_importance() 只取第一个命中的键，不会把多个关键词的分数相加。
ROLE_BOOST = {
    "方法": 0.08,   # 方法章节：核心技术描述
    "实验": 0.08,   # 实验章节：实验设计与结果
    "结果": 0.08,   # 结果章节：关键发现
    "结论": 0.06,   # 结论章节：总结性陈述
    "讨论": 0.05,   # 讨论章节：解释与边界
    "引言": 0.02,   # 引言章节：背景信息，轻微加分
    "综述": 0.04,   # 综述章节：文献归纳
}


# @dataclass 会为下面的类自动生成构造方法、打印格式和相等性比较；
# 这里只声明字段，相当于一张轻量的“句子记录表”，比普通字典更易读。
@dataclass
class Sentence:
    # 句子文本（已经去掉首尾空白，但保留原句内部格式）。
    text: str
    # 句子在所属节点 node.text 中的起始字符下标（从 0 开始，含该位置）。
    start: int
    # 句子在 node.text 中的结束字符下标（Python 切片语义，不含该位置）。
    end: int
    # 句子所在的 PDF 页码，用于证据回溯；找不到页码跨度时默认 1。
    page: int


# 把一段长文本切成若干 Sentence。spans 是节点记录的分页信息，
# 每一项是 (起始字符下标, 结束字符下标, 页码)，用来判断句子落在哪一页。
def split_sentences(text: str, spans: list[tuple[int, int, int]]) -> list[Sentence]:
    # 准备结果列表；类型标注帮助编辑器提示，也表明最终返回 Sentence 对象列表。
    out: list[Sentence] = []
    # 用 finditer 逐个扫描正则匹配；text or "" 兼容调用方传入 None 的情况。
    for m in SENTENCE_RE.finditer(text or ""):
        # group(0) 是整段匹配文本；strip 去掉首尾空白后再判断长度。
        raw = m.group(0).strip()
        # 过短的片段（少于 12 个字符）通常是页眉、编号或噪声，直接跳过。
        if len(raw) < 12:
            # continue 进入下一次循环，不把这个片段加入结果。
            continue
        # 记录匹配在原始文本中的起止位置；end 是开区间端点。
        start, end = m.start(), m.end()
        # 去掉前导空白偏移
        # 因为 raw 做过 strip，真实起点要向右移动“被去掉的前导空白”长度；
        # 这样 start 才能和原文下标、后续证据的 char_start 对齐。
        lead = len(m.group(0)) - len(m.group(0).lstrip())
        # 修正句子起点；末尾空白没有单独修正，end 仍覆盖原匹配范围。
        start += lead
        # 页码默认值：如果分页信息缺失或没有命中任何跨度，就记为第 1 页。
        page = 1
        # 遍历该节点的分页跨度，寻找与当前句子有交集的页。
        for s, e, p in spans:
            # 两种相交情况：
            # 1) 句子起点落在 [s, e) 内；
            # 2) 句子起点在页前，但句子终点落在 (s, e] 内。
            # 找到第一段相交的页就采用它；跨页句子只记录最先命中的页。
            if s <= start < e or s < end <= e:
                # 保存命中的页码。
                page = p
                # 已经确定页码，跳出内层 for，避免后面的页覆盖结果。
                break
        # 构造 Sentence 并追加到结果；位置使用原文下标，便于证据回溯。
        out.append(Sentence(raw, start, end, page))
    # 返回全部分句；没有有效句子时就是空列表。
    return out


# 内部辅助函数：清洗候选知识点名称，去掉编号、引导词、标点和过长部分。
# limit 控制最大长度，默认 22 个字符；调用方可以传更短的值。
def _clean_name(text: str, limit: int = 22) -> str:
    # 去掉开头的逗号、顿号、分号、冒号及空白，避免名称以标点开头。
    text = re.sub(r"^[，,、；;：:\s]+", "", text)
    # 去掉列表编号（i) / iii） / 1) 等）与常见引导词
    # 解释：可选左括号 + 罗马数字（i~iii、iv、v、vi~viii）或 1~2 位阿拉伯数字
    # + 右括号/句点/顿号 + 可选空白；flags=re.I 让罗马数字大小写都匹配。
    text = re.sub(r"^[（(]?(?:i{1,3}|iv|v|vi{0,3}|[0-9]{1,2})[)）.、]\s*", "", text, flags=re.I)
    # 去掉“所谓/其中/此外/本文/本研究”等常见引导词；只处理开头，避免误删中间词。
    text = re.sub(r"^(所谓|其中|此外|同时|因此|首先|其次|最后|文章|本文|本研究|该研究|作者)\s*", "", text)
    # 只取第一个分句：遇到逗号、顿号、分号或冒号就截断，让名称更短更聚焦。
    text = re.split(r"[，,、；;：:]", text)[0]
    # 去掉首尾的中英文引号、书名号、括号、方括号和空格等装饰性字符。
    text = text.strip("“”‘’《》()（）[]【】 ")
    # 未闭合的括号片段直接截断
    # 如果只有左中文括号没有右括号，说明括号内容不完整，取括号前的主体。
    if "（" in text and "）" not in text:
        # split 后取第 0 段，相当于删除未闭合的中文括号及其后内容。
        text = text.split("（")[0]
    # 对英文半角括号做同样的未闭合保护。
    if "(" in text and ")" not in text:
        # 删除未闭合的英文括号及其后内容。
        text = text.split("(")[0]
    # 超过长度上限时硬截断，防止名称在界面上过长或破坏布局。
    if len(text) > limit:
        # 切片只保留前 limit 个字符。
        text = text[:limit]
    # 最后再 strip 一次，清掉截断或前面清洗留下的尾部空白。
    return text.strip()


# 对 extract_name 得到的原始名称做二次润色：太长的句子片段不直接当标题，
# 而是根据类型改成“XX的方法/的结论/的局限”等更适合展示的短语。
def polish_name(name: str, kp_type: str, sentence: str, section_title: str) -> str:
    """把过长的句段名压成可展示的知识点标题。"""
    # 判断名称是否“不合格”：以下三个条件任意一个成立即可。
    # 这里把布尔表达式拆成多行，便于逐条阅读；括号保证逻辑整体成立。
    bad = (
        # 条件一：名称超过 16 个字符，展示时容易变成一整句话。
        len(name) > 16
        # 条件二：名称中含有虚词/动词/介词等，说明它更像句子而不是名词短语。
        # 末尾的“的$”专门拦截以“的”结尾的残缺短语。
        or bool(re.search(r"(则|若|当|进行|采用|具有|通过|以及|对于|较|在|是|的$)", name))
        # 条件三：以“的/和/与/或/逗号”结尾，通常表示成分不完整。
        or name.endswith(("的", "和", "与", "或", "，"))
    )
    # 名称看起来合格时，只需处理少数“短语型但语义太泛”的特例。
    if not bad:
        # 短语型“有效提升/极大提升”等动词短语不适合作为知识点名
        # 数据类名称太短，或以“有效/显著/大幅”等副词开头时，改成通用标题。
        if kp_type == "data" and (len(name) < 6 or re.search(r"^(有效|极大|显著|明显|大幅|进一步|不断|持续)", name)):
            # 返回稳定的通用名称，避免出现“提升了”这类无主语标题。
            return "关键实验结果"
        # 结论类名称太短，或者直接照搬章节标题时，补上“的结论”。
        if kp_type == "conclusion" and (len(name) < 6 or name == section_title):
            # 去掉章节标题开头的“1”“2.3”“3.1.4”等编号及空白。
            section = re.sub(r"^\d+(\.\d+)*\s*", "", section_title or "").strip()
            # 有章节名就取前 10 个字符加“的结论”，否则用通用兜底名称。
            return (section[:10] + "的结论") if section else "核心结论"
        # 其他合格名称原样返回，尽量保留原文信息。
        return name
    # 走到这里说明名称不合格，需要根据类型和句子内容重新构造标题。
    # 先统一清洗章节标题：section_title or "" 兼容空值，再去编号和空白。
    section = re.sub(r"^\d+(\.\d+)*\s*", "", section_title or "").strip()
    # 数据类名称按句子中的线索细分，优先给出更有信息量的类别名。
    if kp_type == "data":
        # 出现超参数、学习率、dropout 等词，归为“训练超参数”。
        if re.search(r"(超参数|学习率|批量|训练轮数|dropout|温度|KL)", sentence, re.I):
            # 返回超参数类标题。
            return "训练超参数"
        # 出现数据集、样本、标注等词，归为“数据集与样本”。
        if re.search(r"(数据集|样本|标注)", sentence):
            # 返回数据集合类标题。
            return "数据集与样本"
        # 出现指标或升降幅度，归为“关键实验结果”。
        if re.search(r"(F1|精确率|召回率|准确率|时延|提升|下降|百分点|%)", sentence):
            # 返回实验结果类标题。
            return "关键实验结果"
        # 没有明显线索时，用章节名加“的数据”；章节名也为空则用“关键数据”。
        return (section[:10] + "的数据") if section else "关键数据"
    # 非数据类使用“类型 -> 后缀”的映射，把章节名补成完整标题。
    suffix = {
        # 方法类标题形如“XX的方法”。
        "method": "的方法",
        # 结论类标题形如“XX的结论”。
        "conclusion": "的结论",
        # 论点类标题形如“XX的观点”。
        "claim": "的观点",
        # 局限类标题形如“XX的局限”。
        "limitation": "的局限",
        # 概念类标题形如“XX的概念”。
        "concept": "的概念",
    # get 的第二个参数是未知类型的兜底后缀，保证一定能得到字符串。
    }.get(kp_type, "的要点")
    # 章节名截取前 10 个字符；没有章节名就用“本节”作为主语。
    base = section[:10] if section else "本节"
    # 拼接主语和类型后缀，得到最终可展示标题。
    return base + suffix


# 从一句话中抽取知识点名称，优先使用“X 是指/X 方法/结果表明”等句式的主语部分。
def extract_name(sentence: str, kp_type: str, section_title: str) -> str:
    # 先去掉首尾空白，后续所有正则都基于这个干净文本。
    t = sentence.strip()
    # 概念类：优先匹配定义句“X 是指/指的是/定义为/称为/所谓/简称 …”。
    if kp_type == "concept":
        # 非贪婪匹配 2~30 个字符作为概念名，再跟一个定义提示词。
        m = re.search(r"^(.{2,30}?)(是指|指的是|定义为|称为|所谓|简称)", t)
        # 命中就把第一个捕获组（概念名）清洗后返回。
        if m:
            # group(1) 是概念名；_clean_name 去掉编号和标点。
            return _clean_name(m.group(1))
        # 再尝试“X 是一种/是一个/即 …”这种定义句式。
        m = re.search(r"^(.{2,24}?)(是一种|是一个|即)", t)
        # 命中则返回清洗后的概念名。
        if m:
            # 只取提示词之前的捕获组。
            return _clean_name(m.group(1))
    # 方法类：抓取“X 方法/模型/算法/框架/机制/策略/流程”中的“X + 类型词”。
    if kp_type == "method":
        # 2~30 个字符的非贪婪主体，后接一个方法类名词。
        m = re.search(r"^(.{2,30}?)(方法|模型|算法|框架|机制|策略|流程)", t)
        # 命中后把主体和类型词拼起来，避免标题只剩“方法”两个字。
        if m:
            # 例如“两阶段” + “方法” => “两阶段方法”。
            name = _clean_name(m.group(1) + m.group(2))
            # 至少 4 个字符才认为是有信息量的方法名。
            if len(name) >= 4:
                # 返回方法名称。
                return name
    # 数据类：寻找“实验/结果/表现/指标/对比/提升”等词前面的 2~16 字主体。
    if kp_type == "data":
        # 字符类允许中文、英文字母和连字符，后接一个数据类后缀词。
        m = re.search(r"([\u4e00-\u9fffA-Za-z\-]{2,16}(?:实验|结果|表现|指标|对比|提升))", t)
        # 命中就清洗并返回，例如“消融实验结果”。
        if m:
            # group(1) 包含主体和后缀，例如“对比实验”。
            return _clean_name(m.group(1))
    # 结论类：优先取“表明/说明/发现”等提示词之后的结论内容。
    if kp_type == "conclusion":
        # 提示词后可跟中文逗号/冒号，再捕获 4~26 个字符作为结论。
        m = re.search(r"(?:表明|说明|发现|结论是|可见)[，,：:]?\s*(.{4,26})", t)
        # 命中则清洗捕获到的结论片段。
        if m:
            # group(1) 是结论正文，不含前面的提示词。
            return _clean_name(m.group(1))
    # 局限类：抓取“X 仍是/仍然/尚/有待/存在/面临/受制于”中的问题主体。
    if kp_type == "limitation":
        # 2~24 字的主体，后接表示未解决/受限制的提示词。
        m = re.search(r"(.{2,24}?)(?:仍是|仍然|尚|有待|存在|面临|受制于)", t)
        # 命中后给主体补上“的局限”，形成完整标题。
        if m:
            # 例如“跨文档检索” + “的局限”。
            return _clean_name(m.group(1)) + "的局限"
    # 兜底：取第一个分句，过短或含公式/编号则用章节名
    # 没有命中任何模板时，截取前 18 个字符作为候选名称。
    name = _clean_name(t, 18)
    # 名称太短、含有“=”/“%”等公式符号，或以数字开头时，不适合直接展示。
    if len(name) < 4 or re.search(r"[=%]", name) or re.match(r"^\d", name):
        # 优先使用章节标题的前 18 个字符；标题为空时用通用名称。
        name = section_title[:18] or "核心知识点"
    # 返回最终候选名称，后续还会经过 polish_name 二次润色。
    return name


# 根据句子中的关键词判断知识点类型，并返回该类型的初始重要度。
def classify(sentence: str) -> tuple[str, float]:
    # 按 TYPE_RULES 的声明顺序逐条尝试；顺序本身就是优先级。
    for kp_type, pattern, score in TYPE_RULES:
        # 只要句子中出现该规则的关键词，就采用这个类型。
        if re.search(pattern, sentence):
            # 返回类型标识和初始重要度，不再检查后面的规则。
            return kp_type, score
    # 所有规则都没命中时，保守地归为“论点”，给 0.55 的初始分。
    return "claim", 0.55


# 计算知识点的重要度（0~1 之间），综合类型基础分、章节角色、句子长度和标题层级。
def _importance(sentence: str, kp_type: str, section_title: str, section_level: int) -> float:
    # 从 TYPE_RULES 中查找该类型的基础分；找不到就用 0.55 兜底。
    # next(生成器, 默认值) 找到第一个匹配项后立即停止。
    base = next((s for t, _, s in TYPE_RULES if t == kp_type), 0.55)
    # 查找章节标题中第一个命中的角色关键词，拿到对应加分；没有命中则为 0。
    # 这里的 for 顺序受 ROLE_BOOST 字典插入顺序影响，不会累加多个关键词。
    boost = next((v for k, v in ROLE_BOOST.items() if k in section_title), 0.0)
    # 长度适中（30~120 字）的句子通常信息量较完整，加 0.04。
    length_bonus = 0.04 if 30 <= len(sentence) <= 120 else 0.0
    # 一级/二级标题下的内容更靠近核心结构，加 0.04。
    level_bonus = 0.04 if section_level <= 2 else 0.0
    # 先加总，再用 min/max 限制在 [0.35, 0.97]，避免分数越界；
    # 最后 round(..., 2) 保留两位小数，便于存储和展示。
    return round(min(0.97, max(0.35, base + boost + length_bonus + level_bonus)), 2)


# 为当前知识点挑选前置知识：只从同一节、且排在当前知识点之前的候选里找。
def _prerequisites(kps: list[dict], current: dict) -> list[str]:
    """同一节内先出现的概念/方法作为前置知识。"""
    # 保存前置知识点的 kp_id；返回给上层写入 prerequisites 字段。
    out = []
    # 按传入顺序遍历候选知识点。
    for kp in kps:
        # 跳过当前知识点自身，避免把“自己”当成自己的前置知识。
        # 这里用 is 比较对象身份，速度快且能处理内容相同的不同字典。
        if kp is current:
            # 当前项不参与，继续看下一个。
            continue
        # 只有“概念/方法”且重要度达到 0.75 的知识点才适合当先修内容。
        if kp["kp_type"] in ("concept", "method") and kp["importance"] >= 0.75:
            # 记录候选知识点的 ID。
            out.append(kp["kp_id"])
        # 最多保留 2 个前置知识，避免学习路径过于复杂。
        if len(out) >= 2:
            # 达到上限后提前结束循环。
            break
    # 返回前置知识 ID 列表；可能为空。
    return out


# 把结构节点切成用于检索的语义块。每个节点独立切分，不跨节点合并。
# min_len/max_len 以字符数近似控制块大小：过短信息不足，过长影响检索精度。
def build_chunks(doc_id: str, nodes: list[NodeDraft], min_len: int = 300, max_len: int = 650) -> list[dict]:
    # 保存所有节点产生的块，最终一次性返回给流水线写库。
    chunks: list[dict] = []
    # 逐个处理解析器给出的节点；nodes 通常是按文档顺序展开的扁平列表。
    for node in nodes:
        # 只处理正文类节点：章节、摘要、表格、图表；其他类型（如封面）跳过。
        # 同时要求文本至少 30 个字符，太短的节点没有切块价值。
        if node.node_type not in ("section", "abstract", "table", "chart") or len(node.text) < 30:
            # 不符合条件就处理下一个节点。
            continue
        # 取节点全文；后续分句和字符下标都以这段文本为坐标系。
        text = node.text
        # 以句子边界切块，保留 10% 重叠
        # 先把节点文本拆成句子字符串列表；这里不保留位置元数据，只用于拼块。
        sentences = [m.group(0) for m in SENTENCE_RE.finditer(text)]
        # buf 是当前正在累积的块；每个节点开始时必须清空，避免跨节点串块。
        buf = ""
        # order_no 是块在“当前节点内”的序号，也从 0 重新开始。
        order = 0
        # 按原文顺序逐句处理，保证块内语义连续。
        for sent in sentences:
            # 第一句直接放入缓冲区，避免后面做空字符串拼接判断。
            if not buf:
                # 保存第一句。
                buf = sent
                # 第一句已处理，进入下一句。
                continue
            # 如果当前块加上新句子仍未超过上限，就继续合并。
            if len(buf) + len(sent) <= max_len:
                # 追加句子；不在句子之间额外加空格，保持原文格式。
                buf += sent
            # 否则说明加入后会超长，需要考虑把当前块先输出。
            else:
                # 只有当前块达到最小长度，才值得单独成为一个检索块。
                if len(buf) >= min_len:
                    # 序号加一，表示新块在当前节点中的位置。
                    order += 1
                    # 构造块字典并追加到结果列表；字段名与 chunk 表列一一对应。
                    chunks.append({
                        # 用统一 ID 生成器创建块主键，前缀为 chk。
                        "chunk_id": new_id("chk"),
                        # 所属文档 ID，由调用方传入，用于多文档隔离。
                        "doc_id": doc_id,
                        # 来源节点 ID，便于从块反查章节/页码。
                        "node_id": node.node_id,
                        # 去掉首尾空白后作为真正入库的块文本。
                        "text": buf.strip(),
                        # 粗略估算 token 数：中文约每 2 个字符一个 token；
                        # max(1, ...) 保证空文本也不会出现 0。
                        "token_count": max(1, len(buf) // 2),
                        # 当前节点内的块序号，从 1 开始。
                        "order_no": order,
                    })
                    # 保留当前块末尾约 10% 作为重叠；最少保留 20 个字符。
                    # 重叠能防止关键句恰好被切在边界处，提升检索召回。
                    overlap = buf[-max(20, len(buf) // 10) :]
                    # 新块从重叠片段开始，再拼上刚刚放不下的句子。
                    buf = overlap + sent
                # 当前块还没达到最小长度时，即使超上限也继续合并，避免碎片块。
                else:
                    # 继续拼接；这可能导致块超过 max_len，是保证语义完整的取舍。
                    buf += sent
        # 循环结束后，把最后一个未输出的块补上；至少 40 个字符才保留。
        if len(buf.strip()) >= 40:
            # 序号加一。
            order += 1
            # 构造并追加最后一块，字段含义与上面的块完全相同。
            chunks.append({
                # 块主键。
                "chunk_id": new_id("chk"),
                # 文档 ID。
                "doc_id": doc_id,
                # 来源节点 ID。
                "node_id": node.node_id,
                # 清理首尾空白后的块文本。
                "text": buf.strip(),
                # 粗略 token 估算，至少为 1。
                "token_count": max(1, len(buf) // 2),
                # 节点内序号。
                "order_no": order,
            })
    # 返回全部块；调用方 pipeline 会逐条写入 chunk 表。
    return chunks


# 从结构节点中抽取知识点与对应证据。返回值是两个列表：
# kps 写入 knowledge_point 表，evidences 写入 evidence 表，二者通过 ID 关联。
# 该函数不写数据库，便于单元测试和复用。
def extract_knowledge(doc_id: str, nodes: list[NodeDraft]) -> tuple[list[dict], list[dict]]:
    """从结构节点中抽取知识点与证据单元。"""
    # 保存本次抽取出的知识点字典。
    kps: list[dict] = []
    # 保存与知识点一一对应的证据字典。
    evidences: list[dict] = []
    # 建立 node_id -> NodeDraft 的索引，供 path_of 沿 parent_id 向上查找父节点。
    # 如果 node_id 重复，后面的节点会覆盖前面的；正常情况下 ID 应唯一。
    by_id = {n.node_id: n for n in nodes}

    # 嵌套辅助函数：从当前节点向上回溯，构造“章/节/小节”的标题路径。
    # 返回值按从根到叶的顺序排列，例如 ["2 方法", "2.1 模型"]。
    def path_of(node: NodeDraft) -> list[str]:
        # 收集路径上的标题；只收非空标题。
        parts = []
        # 从当前节点开始向上遍历。
        cur = node
        # 一直走到没有父节点为止；None 表示到达根。
        while cur is not None:
            # 节点自身有标题才加入，空标题不污染路径。
            if cur.title:
                # 先加入当前标题，最后再整体反转。
                parts.append(cur.title)
            # 有 parent_id 才查父节点；查不到时 get 返回 None，循环自然结束。
            # 这种写法也能避免父节点缺失时抛 KeyError。
            cur = by_id.get(cur.parent_id) if cur.parent_id else None
        # 因为是从叶到根收集的，这里反转为根到叶，便于阅读和展示。
        return list(reversed(parts))

    # 主循环：遍历每个结构节点，逐节抽取。
    for node in nodes:
        # 当前只从“章节”和“摘要”抽知识点；表格/图表留给分块，不直接抽。
        if node.node_type not in ("section", "abstract"):
            # 非目标类型跳过。
            continue
        # 正文太短（少于 60 字）通常没有足够信息支撑一个知识点。
        if len(node.text) < 60:
            # 跳过过短节点。
            continue
        # 标题中出现“参考文献/作者简介/致谢”等，说明不是知识正文，跳过。
        # any + 生成器：只要任意一个关键词出现在标题中就成立。
        if any(k in node.title for k in ("参考文献", "作者简介", "致谢")):
            # 跳过这些辅助章节。
            continue
        # 按句切分，并传入 page_spans 以便给每句标注页码。
        sentences = split_sentences(node.text, node.page_spans)
        # 候选三元组：(重要度分数, Sentence 对象, 知识点类型)。
        # 先收集再排序，避免边遍历边修改顺序。
        candidates: list[tuple[float, Sentence, str]] = []
        # 逐句评估是否值得成为知识点。
        for sent in sentences:
            # 过滤图表引用和文献引用：如“图 3”“表2”“[1]”“[1,2]”。
            # 这些句子往往只是指向别处，不适合作为独立学习内容。
            if re.search(r"(图|表)\s*\d+|\[[0-9,，\-–]+\]", sent.text):
                # 命中引用格式就跳过。
                continue
            # 过长的句子（超过 260 字）可能是整段粘连或解析错误，跳过。
            if len(sent.text) > 260:
                # 避免把异常长文本塞进定义和证据字段。
                continue
            # 用关键词规则判断类型；这里只需要类型，初始分数稍后重新计算。
            kp_type, _ = classify(sent.text)
            # 综合类型、章节、长度、层级计算重要度。
            score = _importance(sent.text, kp_type, node.title, node.level)
            # 对“本文/文章/研究 提出/设计/构建…”这类作者自述句额外加 0.06。
            # 这类句子通常概括了论文的核心贡献，值得优先保留。
            if re.search(r"(本文|文章|研究)(提出|设计|构建|采用|使用)", sent.text):
                # 加分后仍限制不超过 0.97，保持分数量纲一致。
                score = min(0.97, score + 0.06)
            # 把候选加入列表；后续统一按分数排序。
            candidates.append((score, sent, kp_type))
        # 该节点没有任何候选句子时，直接处理下一个节点。
        if not candidates:
            # 跳过空候选节点。
            continue
        # 按重要度从高到低排序；分数相同时按句子在原文中的起点升序。
        # 负号把 Python 的升序排序变成降序；第二关键字保证同分时保持原文顺序。
        candidates.sort(key=lambda x: (-x[0], x[1].start))
        # 保存最终选中的知识点三元组，最多 3 个/节点。
        picked: list[tuple[float, Sentence, str]] = []
        # 统计每种类型已经选了多少个，防止整节都是同一种类型。
        seen_types: dict[str, int] = {}
        # 按分数从高到低考察候选。
        for score, sent, kp_type in candidates:
            # 同一类型最多取 2 个，保证类型多样性。
            if seen_types.get(kp_type, 0) >= 2:
                # 达到该类型上限，跳过这个候选。
                continue
            # 与已选句子做文本相似度比较；相似度超过 0.75 视为重复。
            # any 只要有一个已选项相似就返回 True。
            if any(_similar(sent.text, p[1].text) > 0.75 for p in picked):
                # 重复表达不重复建知识点。
                continue
            # 通过类型上限和去重检查，正式选中。
            picked.append((score, sent, kp_type))
            # 更新该类型的计数；get(..., 0) 处理第一次出现。
            seen_types[kp_type] = seen_types.get(kp_type, 0) + 1
            # 每个节点最多保留 3 个知识点，避免单节内容过载。
            if len(picked) >= 3:
                # 达到数量上限，结束挑选。
                break
        # 为每个被选中的句子构造知识点和证据，二者使用同一个 ev_id 关联。
        for score, sent, kp_type in picked:
            # 先生成证据 ID，知识点里的 evidence_ids 会引用它。
            ev_id = new_id("ev")
            # 从原句提取候选名称，例如“X 方法”“X 的局限”。
            name = extract_name(sent.text, kp_type, node.title)
            # 对名称做长度和语义润色，得到最终展示标题。
            name = polish_name(name, kp_type, sent.text, node.title)
            # 构造知识点字典；字段与 knowledge_point 表基本对应。
            kp = {
                # 知识点主键，前缀 kp。
                "kp_id": new_id("kp"),
                # 所属文档，保证跨文档隔离。
                "doc_id": doc_id,
                # 来源结构节点，能反查章节和页码。
                "node_id": node.node_id,
                # 最终展示名称。
                "name": name,
                # 类型：concept/method/data/conclusion/claim/limitation。
                "kp_type": kp_type,
                # 定义/解释：截取原句前 220 字，避免数据库字段过长。
                "definition": sent.text[:220],
                # 重要度分数，来自上面的启发式计算。
                "importance": score,
                # 初始难度，按类型查表；未知类型默认 3。
                "difficulty": TYPE_DIFFICULTY.get(kp_type, 3),
                # 证据 ID 列表；当前一条知识点对应一条证据。
                "evidence_ids": [ev_id],
                # 前置知识先留空，所有知识点生成完后再统一计算。
                "prerequisites": [],
                # 顺序号按全文档累加，从 1 开始，便于稳定展示。
                "order_no": len(kps) + 1,
                # 内部临时字段：保存章节路径，方便后续扩展或调试；
                # pipeline 写库时不会使用它（表里没有这一列）。
                "_section_path": path_of(node),
            }
            # 构造证据字典；它让每个知识点都能回到原文的具体位置。
            ev = {
                # 证据主键，与知识点中的 evidence_ids 对应。
                "evidence_id": ev_id,
                # 所属文档 ID。
                "doc_id": doc_id,
                # 来源节点 ID。
                "node_id": node.node_id,
                # 句子所在页码，来自 split_sentences 的分页判断。
                "page_no": sent.page,
                # 证据引文：最多 300 字，供界面高亮或引用展示。
                "quote": sent.text[:300],
                # 引文在节点文本中的起始字符下标。
                "char_start": sent.start,
                # 结束下标不能超过节点文本长度，min 起保护作用。
                "char_end": min(len(node.text), sent.end),
                # 证据强度：概念、数据、结论视为强证据；方法/观点/局限为弱证据。
                # 这是规则判断，不是统计置信度。
                "strength": "strong" if kp_type in ("concept", "data", "conclusion") else "weak",
                # 上下文：句子前后各扩 40 个字符，并裁剪到节点文本边界。
                # max(0, ...) 防止负数起点，min(len(...), ...) 防止越界。
                "context": node.text[max(0, sent.start - 40) : min(len(node.text), sent.end + 40)],
            }
            # 把知识点加入总结果。
            kps.append(kp)
            # 把证据加入总结果，两个列表通过 ev_id/kp_id 对应。
            evidences.append(ev)
    # 前置知识
    # 所有知识点生成完后，再按节点分组计算前置关系。
    for kp in kps:
        # 找出与当前知识点属于同一节点的所有知识点。
        section_kps = [k for k in kps if k["node_id"] == kp["node_id"]]
        # 只把 order_no 更小的（即本节中先出现的）知识点作为候选，
        # 再交给 _prerequisites 筛选概念/方法和重要度阈值。
        kp["prerequisites"] = _prerequisites([k for k in section_kps if k["order_no"] < kp["order_no"]], kp)
    # 返回知识点列表和证据列表；调用方负责序列化并写入数据库。
    return kps, evidences


# 计算两段文本的相似度，用于过滤同一节点内表达重复的候选知识点。
def _similar(a: str, b: str) -> float:
    # 在函数内部导入 difflib：只有真正需要去重时才加载，减少模块导入开销。
    import difflib

    # SequenceMatcher 基于最长连续匹配片段计算相似比例，返回值在 0~1 之间；
    # 1 表示完全相同，0 表示几乎没有共同片段。None 表示使用默认的字符比较。
    return difflib.SequenceMatcher(None, a, b).ratio()
