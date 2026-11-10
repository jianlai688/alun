# 模块级导读（以下均为新增注释，仅供学习，不影响程序运行）
# 一、文件职责
# 本文件是「A论」后端的 AI 问答编排器，对外只暴露一个函数 answer()：把“用户问题 + 论文文档 + 会话进度 +
# 平台统计 + 外部权威资料 + 历史对话”组装成一次大模型请求，拿到回答后再补上引用、来源、耗时等元数据，
# 并写一条审计日志。它不负责 HTTP 路由，也不关心模型网关的底层细节。
# 二、在整个应用中的位置
# 项目是一个 FastAPI 应用，包路径为 alun/app/。main.py 通过 `from . import assistant as assistant_api` 引入本模块，
# 并在问答路由里用 `await asyncio.to_thread(assistant_api.answer, ...)` 调用它——因为本文件内部全是同步阻塞调用
# （SQLite 查询、httpx 同步请求、llm.chat 同步请求），必须放进线程池执行，才不会卡住 FastAPI 的事件循环。
# 三、主要调用关系（一次问答的数据流）
# main.py 路由 → answer()
# ├─ _document_question_context() → _document_context() + _rank_chunks() → _terms()
# ├─ _session_context()     读 session / report / turn 表
# ├─ _admin_context()       读 app_user / document / session / turn / ai_audit / usage_log / activity_event 表，
# │                        并调用 audit.recent_events()
# ├─ _web_search()          调 OpenAlex、arXiv、Crossref、PubMed 四个公开学术接口
# ├─ llm.chat()             OpenAI 兼容网关；失败时用 _fallback_answer() 兜底
# └─ audit.log_event()      往 activity_event 表写审计事件
# 四、阅读建议
# 1) 先看 answer()（源文件第 303 行起）把握整体流程，再回头看各个以下划线开头的私有辅助函数；
# 2) 所有 SQL 都使用 ? 参数占位符，属于参数化查询，可防止 SQL 注入；
# 3) 代码里大量的 [:900]、[:14]、[:6] 等截断，都是为了控制送进模型的 token 数量与成本；
# 4) 本文件不定义任何类，结构是“模块常量 SYSTEM_PROMPT + 若干私有函数 + 唯一公开入口 answer()”；
# 5) 本文件新增的注释全部以 # 单独成行，原有代码行保持逐字不变，方便与源文件对照学习。
"""A论 AI 问答助手：文档 RAG、会话上下文、平台数据与权威资料检索。"""
# `from __future__ import annotations`：让 Python 推迟解析类型注解（PEP 563）。
# 作用：像 `list[str] | None` 这种写法在旧版本解释器里也不会在定义时报错，注解只在需要时才求值。
# 注意：它必须是模块文档字符串之后的第一个语句，所以只能放在这里；它不改变任何运行逻辑。
from __future__ import annotations

# 导入标准库 json：用于 JSON 的序列化/反序列化。
# 说明：本文件当前没有直接调用 json 的函数（审计模块 audit 内部自己做了 JSON 处理），
# 这行属于历史遗留/为后续扩展预留；按任务要求原样保留，不删除导入。
import json
# 导入标准库 re（正则表达式）：本文件用它做三件事——
# 1) _terms() 里切分中英文检索词；
# 2) _web_search() 里判断问题是否涉及技术/医学领域，决定要不要查 arXiv 或 PubMed；
# 3) 清洗文本中的连续空白、去掉 Crossref 摘要里的 HTML 标签。
import re
# 导入标准库 time：用于计时与取日期。
# `time.time()` 记录开始时间、最后算 latency_ms；`time.strftime("%Y-%m-%d")` 生成“今天”的日期字符串，供 SQL 的 substr 比较。
import time
# 导入 xml.etree.ElementTree 并简写为 ET：标准库的 XML 解析器。
# arXiv 的 API 返回的是 Atom 格式 XML（不是 JSON），所以必须用 ET.fromstring() 解析后再按命名空间取节点。
import xml.etree.ElementTree as ET
# 从 typing 导入 Any：表示“任意类型”。
# 本文件只在 `content: Any` 处使用：它有时是字符串（纯文本提问），有时是列表（带图片的多模态消息），
# 用 Any 可以避免类型检查器误报。
from typing import Any

# 导入第三方库 httpx：同步版 HTTP 客户端（requests 的现代替代品）。
# 本文件用它访问 OpenAlex、arXiv、Crossref、PubMed 四个公开接口；每个请求都显式设置了 timeout，
# 避免外网卡死拖垮整个问答接口。因为它是同步阻塞的，所以 answer() 必须由上层放进线程池执行。
import httpx

# 从同包（alun/app）导入三个内部模块：
# audit    —— 活动事件审计（log_event 写库、recent_events 读最近事件）；
# llm      —— OpenAI 兼容模型网关客户端（chat 负责真正调用大模型、记录 token 用量）；
# settings —— 后台可配置项（模型名、模型开关等），读取时带默认值。
# 使用相对导入（点号）是因为这些文件同属一个 Python 包。
from . import audit, llm, settings
# 从同包的 db 模块导入两个只读查询助手：
# fetch_one(sql, params) —— 返回第一行（dict）或 None；
# fetch_all(sql, params) —— 返回全部行（list[dict]）。
# 它们内部用 `with db() as conn` 管理连接：正常结束自动 commit，异常自动 rollback，调用方无需手动关闭连接。
from .db import fetch_all, fetch_one

# SYSTEM_PROMPT：系统提示词（system message），是整个问答的“行为准则”，每次请求都会原样发给模型。
# 下面这个三引号字符串整体是一段自然语言，不能在其中插入 # 注释（否则会改变字符串内容、破坏提示词），
# 因此把六条规则的设计意图集中写在这里：
# 规则 1：优先用【文档上下文】回答，并要求标注原文页码 [p.6]——页码是“可核验引用”的最低要求；
# 规则 2：文档里没有的才用【权威资料】，且必须用 [S1]、[S2] 标注——把外部资料与论文原文严格区分，防止伪装成原文；
# 规则 3：【平台数据】来自数据库实时统计，用户/用量/准确率/幻觉率类问题必须以它为准，禁止编造数字；
# 规则 4：支持截图/图片输入——先描述看到的文字、表格或界面，再结合上下文回答，看不清要明确说看不清；
# 规则 5：找不到依据时直接说“现有资料不足以回答”并说明缺什么——这是防幻觉的兜底话术；
# 规则 6：输出风格——简洁中文、先结论后依据，公式/术语/数字保持原意。
# 这段常量属于固定 token 开销，修改它会直接改变所有回答的风格与引用格式。
SYSTEM_PROMPT = """你是「A论 AI 问答助手」，服务于论文导读智能体 A论。

你的回答必须遵守：
1. 优先使用【文档上下文】中的真实内容回答；文档中的事实要标注原文页码，例如 [p.6]。
2. 文档没有的信息才参考【权威资料】，并用 [S1]、[S2] 标注来源；不得把外部资料伪装成论文原文。
3. 【平台数据】是后台真实统计，回答用户/用量/准确率/幻觉率问题时必须以此为准，不得编造数字。
4. 用户可以上传截图或图片；先描述你看到的图中文字、表格或界面，再结合上下文回答。看不清就明确说看不清。
5. 找不到依据时直接说“现有资料不足以回答”，并说明还缺什么；不要编造引文、页码或链接。
6. 用简洁的中文回答，先给结论，再给依据。公式、术语和数字保持原文含义。
"""


# _terms(query)：把一句自然语言问题切成“检索词集合”，供 _rank_chunks() 打分使用。
# 为什么需要它：中文句子没有空格，不能像英文那样直接 split；这里用“连续汉字串 + 相邻两字滑窗”的朴素办法近似分词，
# 既能保留较长的专有名词，又能用二元词提高召回率。返回值是 set，后续用 `in` 判断是否命中是 O(1) 复杂度。
# 参数 query 可能为 None（前端漏传），函数内用 `query or ""` 兜底。
def _terms(query: str) -> set[str]:
    # 用一条正则同时抽取两类词：
    # [\u4e00-\u9fff]{2,}                 —— 连续 2 个及以上的汉字（一个中文词/短语）；
    # [A-Za-z0-9][A-Za-z0-9._-]{1,}       —— 以字母或数字开头、总长至少 2 的英文/数字串，允许 . _ -（如 GPT-4o、BERT-base）。
    # re.findall 返回匹配到的字符串列表，set(...) 去重；`query or ""` 防止 None 让正则报错。
    terms = set(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9][A-Za-z0-9._-]{1,}", query or ""))
    # 再遍历每个较长的中文串，准备做“二元切分”。
    # 为什么要二次处理：像“知识图谱”这种长词，用户可能只记得“图谱”，切成“知识/识图/图谱”能提高命中率。
    for seg in re.findall(r"[\u4e00-\u9fff]{2,}", query or ""):
        # 对长度为 len(seg) 的中文串做滑窗，i 从 0 到 len(seg)-2。
        # `len(seg) - 1` 是为了保证 seg[i:i+2] 始终取到两个字符，不会越界。
        for i in range(len(seg) - 1):
            # 取出相邻的两个字（如 i=0 时是 seg[0:2]），加入词集合。
            # 集合自动去重，所以同一个二元词出现多次也只会保留一份。
            terms.add(seg[i : i + 2])
    # stop：中文停用词集合。这些词几乎每句话都有，却不携带主题信息，如果参与打分会让检索结果失真。
    # 用 set 而不是 list，是为了后面 `t not in stop` 的判断更快（哈希查找）。
    stop = {"什么", "怎么", "如何", "这个", "那个", "一下", "可以", "是否", "以及", "相关", "全文", "文章", "论文"}
    # 返回最终词集合：
    # t.lower()          —— 统一转小写，后面与原文小写文本比较时才能命中英文词；
    # if t not in stop   —— 过滤停用词；
    # 最外层 {...} 是集合推导式，结果仍然是 set[str]。
    return {t.lower() for t in terms if t not in stop}


# _page_of(node_id, nodes)：根据节点 ID 反查它所在的起始页码。
# 为什么单独抽一个函数：chunk（原文块）和 knowledge_point（知识点）表里只存 node_id，页码存在 node 表；
# 多个地方都要用，抽出来避免重复代码。返回 int，保证拼到 f-string 里不会出现 None。
def _page_of(node_id: str, nodes: dict[str, dict]) -> int:
    # 从 nodes 字典里取节点；`or {}` 处理“取不到（None）”的情况，这样下一行还能安全调用 .get()，不会抛 AttributeError。
    node = nodes.get(node_id) or {}
    # 返回页码：
    # node.get("page_start") or 0 —— 字段为 None 或 0 时统一当 0；
    # 外层 int(...)               —— 兼容数据库里可能是字符串的情况，保证返回整数。
    return int(node.get("page_start") or 0)


# _document_context(document_id)：构造“文档上下文”的基础版本，返回三元组：
# (拼好的多行文本, 引用列表 citations, 文档标题)
# 这是 RAG（检索增强生成）里“把资料塞进提示词”的第一步。所有数量都做了硬性截断，
# 目的是控制 token：摘要 900 字、节点 30 个、知识点 14 条、原文片段 8 块、每块 1100 字。
def _document_context(document_id: str) -> tuple[str, list[dict], str]:
    # 查文档主记录：
    # `deleted_at IS NULL` 是软删除过滤——项目删除文档只打时间戳、不物理删除，所以查询必须带上这个条件；
    # (document_id,) 是参数元组，? 占位符由驱动转义，避免 SQL 注入；
    # fetch_one 查不到会返回 None（不会抛异常）。
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (document_id,))
    # 文档不存在或已被软删除：直接返回三个空值。
    # 这样调用方（answer）会退化成“没有文档上下文”的普通问答，而不是让整个接口报 500。
    if not doc:
        # 返回空三元组：空文本、空引用列表、空标题。类型与正常返回保持一致，调用方无需额外判空。
        return "", [], ""
    # 一次性查出该文档的所有节点，并重组成 {node_id: 行字典} 的映射。
    # 为什么这样做：后面要按 chunk.node_id / kp.node_id 频繁查标题和页码，
    # 先建字典可以把“每查一次就发一条 SQL”的 N+1 查询降为 O(1) 的内存查找。
    # `for r in fetch_all(...)` 是字典推导式，r["node_id"] 一定存在（主键）。
    nodes = {r["node_id"]: r for r in fetch_all("SELECT * FROM node WHERE doc_id=?", (document_id,))}
    # 查知识点，按 importance 降序：importance 是抽取时给出的重要度，重要的排前面，后面只取前 14 条。
    kps = fetch_all("SELECT * FROM knowledge_point WHERE doc_id=? ORDER BY importance DESC", (document_id,))
    # 查原文块（chunk），按 order_no 升序：order_no 是块在原文中的阅读顺序，
    # 保证拼进提示词的片段顺序与论文一致，模型不容易“跳读”。
    chunks = fetch_all("SELECT * FROM chunk WHERE doc_id=? ORDER BY order_no", (document_id,))
    # 这一行是原作者留下的占位变量：query 目前恒为空串。
    # 原作者注释说明“由调用方通过 question 重排；先保留结构，排序在 answer 里做”——
    # 真正按问题相关性排序的逻辑在 _rank_chunks() / _document_question_context() 中实现。
    # 按要求保留原注释与语句，不做删除。
    query = ""  # 由调用方通过 question 重排；先保留结构，排序在 answer 里做
    # lines：存放最终要拼接的文本行。这里是一个多行列表字面量，先放“文档头、摘要、结构”三部分。
    # 列表元素都是 f-string；f-string 里的 `or ''`、`or 0` 用于把 None 转成空串/0，避免打印出 "None"。
    lines = [
        # 文档信息行：书名号里放标题，后面依次是页数与文档类型（如 pdf/word）。
        # `doc.get('title') or ''` 的写法表示：字段不存在或值为 None 时用空串，保证 f-string 不出现 None。
        f"【文档】《{doc.get('title') or ''}》 · {doc.get('page_count') or 0} 页 · {doc.get('doc_type') or ''}",
        # 摘要行：只取前 900 个字符。摘要通常最能概括全文，但太长会挤占原文片段的位置，所以做长度上限。
        f"【摘要】{(doc.get('abstract') or '')[:900]}",
        # “【结构】”是一个段落标记。模型靠这些【】标题区分不同来源的材料（文档/会话/平台/权威资料），
        # 这也是 SYSTEM_PROMPT 里要求“优先使用【文档上下文】”能够生效的前提。
        "【结构】",
    # 多行列表字面量结束：上面三项分别是文档头、摘要与【结构】段落标记，这里用 ] 收尾。
    ]
    # 只取前 30 个节点来列结构：
    # list(nodes.values()) —— dict_values 不支持切片，先转成列表；
    # [:30]                —— 超长文档（几百个节点）也只列 30 个，防止提示词爆长。
    for n in list(nodes.values())[:30]:
        # 只把 node_type == "section" 的节点当作“章节”列出来，跳过段落、表格、图注等更细的节点，
        # 否则结构会非常冗长。用 .get() 取值，字段缺失时返回 None，不会等于 "section"，自然被过滤。
        if n.get("node_type") == "section":
            # 追加一行章节：标题 + 起始页码（p.xx）。
            # `page_start` 直接用 .get 取值，没做 int 转换，因为这里只是展示；若为 None 会显示 None，属于源文件既有行为。
            lines.append(f"- {n.get('title')}（p.{n.get('page_start')}）")
    # 只有存在知识点时才追加“【知识点】”小标题，避免出现一个空段落让模型误以为“没有知识点”。
    if kps:
        # 追加“【知识点】”段落标题。
        lines.append("【知识点】")
        # 最多取 14 个知识点（已按 importance 降序），控制 token 用量。
        for kp in kps[:14]:
            # 每条知识点一行：名称 + 定义（截 220 字）+ 页码。
            # 页码通过 _page_of(kp.get("node_id"), nodes) 从节点表反查；kp 可能没有 node_id，_page_of 内部已做兜底。
            lines.append(f"- {kp.get('name')}：{(kp.get('definition') or '')[:220]}（p.{_page_of(kp.get('node_id'), nodes)}）")
    # selected：引用列表，最终会返回给前端渲染“原文依据”卡片。
    # 每个元素是一个 dict，字段为 type/page/quote/title；这里先声明为空列表，并在下面的片段循环里逐条追加。
    selected: list[dict] = []
    # 只有存在原文块时才追加“【原文片段】”段落，避免空段落浪费 token。
    if chunks:
        # 追加“【原文片段】”段落标题。
        lines.append("【原文片段】")
        # 只取前 8 个原文块。注意：这里按 order_no（原文顺序）取，不做相关性排序；
        # 按问题相关性挑选片段的逻辑在 _rank_chunks() 中单独实现，供 _document_question_context() 使用。
        for chunk in chunks[:8]:
            # 把块文本中的连续空白（空格、换行、制表符）压缩成一个空格，并去掉首尾空白。
            # 为什么：PDF 抽取出来的文本经常有大量换行和多余空格，直接拼进提示词会浪费 token、破坏排版。
            text = re.sub(r"\s+", " ", chunk.get("text") or "").strip()
            # 归一化后如果是空串（扫描页、图片页常见），就跳过这个块。
            if not text:
                # continue 跳过本次循环，处理下一个 chunk；不会污染上下文，也不会产生空的 [D] 编号。
                continue
            # 反查该块所属节点的起始页码，后面用于 [D1 | p.3] 这样的引用标记。
            page = _page_of(chunk.get("node_id"), nodes)
            # 拼接片段行：[D编号 | p.页码] 正文（最多 1100 字）。
            # D 编号按“实际收录的片段数 + 1”生成，所以即使中间跳过了空块，编号仍然连续。
            lines.append(f"[D{len(selected) + 1} | p.{page}] {text[:1100]}")
            # 同时记录一条引用：type=document 表示来自文档，page 页码，quote 摘录前 220 字（前端卡片展示用），
            # title 稍后在下一行补上。字典字面量跨两行，属于同一个 append 调用。
            selected.append({"type": "document", "page": page, "quote": text[:220],
                             # 引用标题：优先用块所属节点的标题；节点没有标题时回退到文档标题。
                             # `(nodes.get(...) or {})` 防止 node_id 查不到时对 None 调用 .get()。
                             "title": (nodes.get(chunk.get("node_id")) or {}).get("title") or doc.get("title")})
    # 返回三元组：
    # "\n".join(lines)      —— 把多行列表拼成一段文本，作为【文档上下文】；
    # selected              —— 引用列表，answer() 会截取前 10 条返回给前端；
    # doc.get("title") or "" —— 文档标题，传给 _web_search() 作为检索的领域提示词。
    return "\n".join(lines), selected, doc.get("title") or ""


# _rank_chunks(document_id, question)：一个“极简版关键词检索器”，返回与问题最相关的若干原文块。
# 为什么不用向量检索：这里追求零外部依赖、毫秒级、结果可解释（能说清为什么选中某块）。
# 打分规则很朴素：问题词在正文中出现一次 +1 分，在所属章节标题中出现一次 +2 分（标题更能代表主题）。
def _rank_chunks(document_id: str, question: str) -> list[dict]:
    # 再次建立 node_id → 节点 的映射（与 _document_context 中相同的查询）。
    # 虽然可以复用，但两个函数各自独立、互不依赖，读起来更直观；这两条 SQL 都走 doc_id 索引，代价很小。
    nodes = {r["node_id"]: r for r in fetch_all("SELECT * FROM node WHERE doc_id=?", (document_id,))}
    # 取出该文档全部原文块，按原文顺序排列，作为候选集。
    chunks = fetch_all("SELECT * FROM chunk WHERE doc_id=? ORDER BY order_no", (document_id,))
    # 把问题切成检索词集合（已小写、已去停用词），供下面逐块打分。
    terms = _terms(question)
    # scored：存放 (分数, chunk 行) 二元组的列表，稍后统一排序。
    scored = []
    # 逐个原文块计算相关度分数。复杂度是 O(块数 × 词数)，对单篇论文（几十到几百块）足够快。
    for chunk in chunks:
        # 取块正文；字段缺失时用空串兜底，避免后面 .lower() 报错。
        text = chunk.get("text") or ""
        # 基础分：每个出现在正文里的检索词记 1 分。
        # `text.lower()` 是因为 _terms 返回的词都已小写；两边统一小写才能做大小写不敏感匹配。
        # sum(1 for ...) 是生成器求和，只统计命中个数，不关心出现次数（出现多次不重复加分）。
        score = sum(1 for term in terms if term in text.lower())
        # 取该块所属节点的标题（可能为空串），标题命中会获得更高权重。
        title = (nodes.get(chunk.get("node_id")) or {}).get("title") or ""
        # 标题加权：每个出现在标题里的词再加 2 分。
        # 这样“标题就写着这个主题”的章节会排在只是正文偶尔提到的地方前面。
        score += sum(2 for term in terms if term in title.lower())
        # 把 (分数, 块) 存入列表；保留原始块是为了后面还能拿到 order_no、node_id 等字段。
        scored.append((score, chunk))
    # 排序：分数从高到低（-x[0] 实现降序），同分时按 order_no 升序（原文顺序）。
    # 显式写 order_no 作为第二关键字，保证每次运行结果稳定、可复现，不会因字典/数据库返回顺序不同而漂移。
    scored.sort(key=lambda x: (-x[0], x[1].get("order_no") or 0))
    # 只保留分数大于 0（至少命中一个词）的块，并截取前 6 个。
    picked = [c for score, c in scored if score > 0][:6]
    # 如果命中的块不足 4 个，就进入兜底逻辑：保证模型至少能看到 4 块原文，避免“检索不到就完全不看文档”。
    if len(picked) < 4:
        # 从所有块里按原顺序补足到 4 个：
        # `c not in picked` 用字典内容相等来判断是否已入选（chunk 是普通 dict，内容相同即视为同一块）；
        # [: 4 - len(picked)] 只补缺口数量。
        # 这条语句同时用 += 扩展列表，等价于 picked.extend(...)。
        picked += [c for _, c in scored if c not in picked][: 4 - len(picked)]
    # out：最终返回的片段列表，元素只保留 text/page/title 三个展示必需字段。
    out = []
    # 遍历选中的块，做最后的清洗与结构转换。
    for chunk in picked:
        # 再次压缩空白并去首尾空格：候选块可能来自兜底列表，仍有可能是空文本。
        text = re.sub(r"\s+", " ", chunk.get("text") or "").strip()
        # 空文本块直接丢弃。
        if not text:
            # 跳过当前块，继续处理下一个。
            continue
        # 反查页码（node 表里的 page_start）。
        page = _page_of(chunk.get("node_id"), nodes)
        # 组装返回项：text 供拼提示词，page 供引用，title 供前端显示来源章节。
        # 字典跨两行书写，第二行是同一个字典字面量的延续。
        out.append({"text": text, "page": page,
                    # title 字段：节点标题；节点不存在或没有标题时用空串。
                    "title": (nodes.get(chunk.get("node_id")) or {}).get("title") or ""})
    # 返回按相关度排序后的片段列表（最多 6 条）。
    return out


# _document_question_context(document_id, question)：面向“具体问题”的文档上下文，返回结构同为
# (文本, 引用列表, 文档标题)。它 = 基础上下文（摘要/结构/知识点/前 8 块）+ 按问题检索出的最相关片段。
# 这是 RAG 中“检索 → 增强 → 生成”里的检索与增强两步。
def _document_question_context(document_id: str, question: str) -> tuple[str, list[dict], str]:
    # 先校验文档是否存在且未删除。虽然 _document_context 内部也会查一次，但这里先判空可以避免
    # 为一个不存在的文档白跑后面的检索与拼接。
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (document_id,))
    # 文档不存在或已删除。
    if not doc:
        # 返回空三元组，行为与“没有文档”一致。
        return "", [], ""
    # 先拿基础上下文：base 是文本，citations 是引用列表（会被原地追加），title 是文档标题。
    base, citations, title = _document_context(document_id)
    # 按问题做关键词检索，得到最相关的原文块（最多 6 条）。
    ranked = _rank_chunks(document_id, question)
    # 只有检索到内容才追加“最相关原文”段落；否则直接返回基础上下文。
    if ranked:
        # 下面这行是原作者写的注释，按要求原样保留；它说明了为什么把“最相关片段”放在摘要/结构之后。
        # 换句话说：先给模型“全局地图”，再给“问题附近的地图”，比一上来就塞片段更省 token、也更符合阅读习惯。
        # 把最相关的片段放在摘要/结构之后，避免全文 token 过大
        # extra：新增的段落行列表，第一行是段落标题【与问题最相关的原文】。
        extra = ["【与问题最相关的原文】"]
        # enumerate(ranked, 1) 让编号从 1 开始，生成 [R1]、[R2]……
        # R 前缀表示“按问题排序的相关片段”，与基础上下文里的 [D1]（按原文顺序的片段）区分开。
        for i, chunk in enumerate(ranked, 1):
            # 每条相关片段一行：[R序号 | p.页码] 正文（截 1200 字）。
            # 比基础片段（1100 字）略长，因为这是最相关的内容，值得多给一点预算。
            extra.append(f"[R{i} | p.{chunk['page']}] {chunk['text'][:1200]}")
            # 同步追加引用：type=document、page、quote（前 220 字）、title。
            # 引用列表会与基础上下文的引用合并，answer() 最终截取前 10 条。
            citations.append({"type": "document", "page": chunk["page"], "quote": chunk["text"][:220],
                               # 引用字典的第二个字段行：title 直接取 chunk 字典里已整理好的标题。
                               "title": chunk["title"]})
        # 把 extra 段落拼到 base 后面：先用一个换行与前面的内容隔开，再用 \n 连接段落内部各行。
        # `base += ...` 是字符串原地拼接（生成新字符串再赋值）。
        base += "\n" + "\n".join(extra)
    # 返回增强后的上下文、合并后的引用列表、文档标题。
    return base, citations, title


# _session_context(session_id)：构造【会话上下文】——告诉模型“这轮练习进行到哪了”。
# 包含：练习模式、已答题数/总题数、IRT 能力值 theta、本轮报告摘要、最近 5 道题的作答情况。
# 没有会话时返回空串，answer() 会用“（无）”占位。
def _session_context(session_id: str) -> str:
    # 查会话主记录；查不到返回 None。
    s = fetch_one("SELECT * FROM session WHERE session_id=?", (session_id,))
    # 会话不存在（可能已被删除或前端传了过期 ID）。
    if not s:
        # 返回空串，调用方据此显示“（无会话上下文）”。
        return ""
    # 查本轮报告（report 表）。报告可能还没生成，所以允许返回 None，后面会判空。
    report = fetch_one("SELECT * FROM report WHERE session_id=?", (session_id,))
    # 查最近 5 道题：
    # ORDER BY seq_no DESC —— 题号倒序，最新的题在前；
    # LIMIT 5              —— 只取 5 条，避免历史越长提示词越大。
    turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no DESC LIMIT 5", (session_id,))
    # 第一行汇总：模式 · 已答/总题 · 能力值。
    # answered_count/total_questions 用 `or 0` 防 None；
    # theta 是 IRT（项目反应理论）能力值，默认 500，float + round(...,1) 保留一位小数；
    # f-string 里可以直接写表达式，可读性好。
    lines = [f"【当前会话】{s.get('mode')} · {s.get('answered_count') or 0}/{s.get('total_questions') or 0} 题 · 能力 {round(float(s.get('theta') or 500), 1)}"]
    # 只有存在报告时才追加“【本轮报告】”，避免空标题。
    if report:
        # 取报告摘要字段（可能为 None 或空串）。
        rj = report.get("summary")
        # 追加报告摘要：`rj if rj else ''` 保证 None 不会以字符串 "None" 的形式出现在提示词里。
        lines.append(f"【本轮报告】{rj if rj else ''}")
    # 遍历最近 5 题（已是倒序，最近的在最前）。
    for t in turns:
        # 每题一行：题号、题型、题干、得分。
        # 得分用条件表达式：`t.get('score') if ... is not None else '未答'`——
        # 这样能区分“0 分（答错）”和“None（还没答）”，模型不会把未作答误判为答错。
        lines.append(f"- 第{t.get('seq_no')}题 {t.get('q_type') or ''}：{t.get('stem') or ''}；得分 {t.get('score') if t.get('score') is not None else '未答'}")
    # 把多行拼成一段文本返回；answer() 会把它放进【会话上下文】段落。
    return "\n".join(lines)


# _admin_context()：构造【平台数据】——全站用户、文档、会话、作答、幻觉率、token 用量与最近事件。
# 权限说明：这是管理员专属数据，answer() 里只有 `role == "admin"` 且“没有绑定具体文档”时才会调用它，
# 普通用户的问题不会看到全站统计，避免越权泄露。
# 性能说明：本函数会执行十余条 COUNT/AVG 查询，并逐个用户再查 3 条；因为只在管理员问答时触发，量级可控。
def _admin_context() -> str:
    # 活跃账号数：status='active' 的用户。COUNT(*) 永远返回一行，所以 fetch_one(...) 不会为 None，可直接 ["c"]。
    users = fetch_one("SELECT COUNT(*) AS c FROM app_user WHERE status='active'")["c"]
    # 未删除文档数：同样带 `deleted_at IS NULL` 软删除过滤。
    docs = fetch_one("SELECT COUNT(*) AS c FROM document WHERE deleted_at IS NULL")["c"]
    # “有效文档下的会话数”：session 表通过 doc_id 关联 document，并过滤掉已删除文档的会话。
    # JOIN 保证只统计仍然“看得见”的文档所对应的会话。
    sessions = fetch_one("SELECT COUNT(*) AS c FROM session s JOIN document d ON d.doc_id=s.doc_id WHERE d.deleted_at IS NULL")["c"]
    # 统计作答质量，SQL 一次算出三个指标：
    # COUNT(*)                                   —— 已判分作答次数；
    # COALESCE(AVG(score),0)                     —— 平均分，没有任何作答时返回 0 而不是 NULL；
    # SUM(CASE WHEN score>=80 THEN 1 ELSE 0 END) —— 优秀次数（≥80 分），用 CASE 把布尔条件转成 0/1 再求和。
    # SQL 字符串跨行书写，Python 会自动把相邻字符串字面量拼接起来。
    # 最后的 `or {}` 是防御式写法：正常情况 fetch_one 一定返回一行，这里只是防止极端情况下 None 导致后面 .get 报错。
    ans = fetch_one(
        "SELECT COUNT(*) AS c, COALESCE(AVG(score),0) AS s, SUM(CASE WHEN score>=80 THEN 1 ELSE 0 END) AS good FROM turn WHERE status='answered'"
    ) or {}
    # 幻觉率统计（ai_audit 表）：
    # total —— 参与统计的模型输出总数；
    # bad   —— grounded=0（未通过证据/结构校验）的数量；
    # `fallback=0` 表示排除“模板兜底”的输出——兜底文案本来就不是模型生成的，不应算作模型幻觉。
    hall = fetch_one("SELECT COUNT(*) AS total, SUM(CASE WHEN grounded=0 THEN 1 ELSE 0 END) AS bad FROM ai_audit WHERE fallback=0") or {}
    # 把总数转成 int 并兜底 None：SUM(...) 在没有匹配行时会返回 NULL。
    hall_total = int(hall.get("total") or 0)
    # 把“未通过校验”的数量转成 int 并兜底 None。
    hall_bad = int(hall.get("bad") or 0)
    # 计算幻觉率：bad / total，保留 4 位小数（后面展示时再乘 100 变成百分比）。
    # `if hall_total else 0.0` 防止分母为 0 触发 ZeroDivisionError。
    hall_rate = round(hall_bad / hall_total, 4) if hall_total else 0.0
    # 取“今天”的日期字符串，格式 YYYY-MM-DD。
    # 数据库里的时间戳是 ISO 格式（如 2026-11-06T17:30:00），前 10 位正好是日期，所以 SQL 里用 substr(ts,1,10) 与之比较。
    # 注意：这里用的是服务器本地时区，不是 UTC。
    today = time.strftime("%Y-%m-%d")
    # 今日活跃用户数：
    # COUNT(DISTINCT user_id) —— 同一用户今天多条事件只算一次；
    # user_id<>''            —— 排除匿名/系统事件；
    # substr(ts,1,10)=?      —— 只取日期部分与今天比较，参数化传值。
    active_today = fetch_one("SELECT COUNT(DISTINCT user_id) AS c FROM activity_event WHERE user_id<>'' AND substr(ts,1,10)=?", (today,))["c"]
    # 今日已判分作答数：turn 表中 status='answered' 且 answered_at 的日期部分等于今天。
    today_answers = fetch_one("SELECT COUNT(*) AS c FROM turn WHERE status='answered' AND substr(answered_at,1,10)=?", (today,))["c"]
    # 累计 token 用量：usage_log 表里 prompt_tokens 与 completion_tokens 之和。
    # COALESCE(...,0) 保证表为空时返回 0，而不是 None。
    tok = fetch_one("SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_log")["t"]
    # 取出用户列表，供下面的“用户概览”使用。
    # 排序：role DESC 让 admin 等角色排在前面，同角色按 created_at 升序（先注册的在前）。
    urows = fetch_all("SELECT user_id, username, display_name, role, last_login_at FROM app_user ORDER BY role DESC, created_at")
    # lines：平台数据的多行文本。先放标题和三条汇总，再放用户概览标题。
    # 这些【】标记与 SYSTEM_PROMPT 规则 3 对应，模型据此识别“这是真实统计数据”。
    lines = [
        # 段落标题“【平台数据】”。
        "【平台数据】",
        # 汇总行 1：活跃账号、今日活跃、文档、会话、已判分作答、今日作答、平均分。
        # `ans.get('c') or 0` 与 `float(ans.get('s') or 0)` 都是防 None 的写法；round(...,1) 保留一位小数。
        f"- 活跃账号 {users}；今日活跃 {active_today}；文档 {docs}；会话 {sessions}；已判分作答 {ans.get('c') or 0} 次；今日作答 {today_answers} 次；平均分 {round(float(ans.get('s') or 0), 1)}",
        # 汇总行 2：幻觉率百分比。hall_rate 是 0~1 的小数，乘 100 后保留一位小数；
        # 括号里同时给出分子/分母，并说明统计口径（模板兜底不计入分母），避免管理员误解数字含义。
        f"- 模型幻觉率：{round(hall_rate * 100, 1)}%（{hall_bad}/{hall_total} 次最终模型输出未通过证据/结构校验；模板兜底不计入分母）",
        # 汇总行 3：累计 token、当前导读模型名、模型总开关状态。
        # settings.get(...) 读后台配置；settings.get_bool(...) 把配置值解析成布尔；
        # `'开启' if ... else '关闭'` 把布尔转成中文，便于模型直接引用。
        f"- token 总量 {tok}；当前导读模型 {settings.get('llm.model')}；模型开关 {'开启' if settings.get_bool('llm.enabled') else '关闭'}",
        # 用户概览的段落标题。
        "【用户概览】",
    # 多行列表字面量结束。
    ]
    # 只展示前 10 个用户：用户多时避免提示词无限膨胀（每个用户后面还要查 3 条 SQL）。
    for u in urows[:10]:
        # 取用户 ID；`or ""` 防止 None 被拼进 SQL 参数（虽然主键理论上不会为空）。
        uid = u.get("user_id") or ""
        # 查该用户的作答次数与平均分：
        # turn t JOIN session s ON s.session_id=t.session_id —— 通过会话把作答关联到用户；
        # WHERE s.user_id=? AND t.status='answered'          —— 只看该用户已判分的作答；
        # 跨两行书写的 SQL 同样是相邻字符串自动拼接。
        # 结尾的 `or {}` 兜底：即使没有作答记录，聚合查询也会返回一行，这里只是防御性写法。
        ua = fetch_one(
            "SELECT COUNT(*) AS c, COALESCE(AVG(t.score),0) AS s FROM turn t JOIN session s ON s.session_id=t.session_id"
            " WHERE s.user_id=? AND t.status='answered'", (uid,)
        ) or {}
        # 该用户的文档数（软删除过滤）。COUNT 必有一行，可直接取 ["c"]。
        ud = fetch_one("SELECT COUNT(*) AS c FROM document WHERE user_id=? AND deleted_at IS NULL", (uid,))["c"]
        # 该用户的会话数。
        us = fetch_one("SELECT COUNT(*) AS c FROM session WHERE user_id=?", (uid,))["c"]
        # 追加一行用户概览。f-string 跨三行书写（下面两行是它的延续），
        # Python 会把括号内的相邻字符串字面量自动拼接成一个字符串。
        lines.append(
            # 显示名优先，没有显示名就回退用户名；后面括号里是角色（admin/user 等），再跟文档数与会话数。
            f"- {u.get('display_name') or u.get('username')}（{u.get('role')}）文档 {ud}、会话 {us}、"
            # 作答次数与平均分；平均分保留一位小数，`or 0` 防止 None 参与 float() 报错。
            f"作答 {ua.get('c') or 0}、平均分 {round(float(ua.get('s') or 0), 1)}"
        # append 调用结束（右括号与参数分行书写，属于同一条语句）。
        )
    # 最近 8 条活动事件：audit.recent_events 内部会限制 limit 范围、按 id 倒序查询，并把 detail 字段从 JSON 还原成 dict；
    # 它自身吞掉数据库异常，所以这里不需要 try。
    events = audit.recent_events(limit=8)
    # 只有存在事件时才追加“最近事件”段落。
    if events:
        # 段落标题。
        lines.append("【最近事件】")
        # 遍历事件，展示“事件类型 · 用户 · 时间”。
        for e in events:
            # username 为空时回退 user_id，再为空就显示空串；ts 是事件时间戳。
            lines.append(f"- {e.get('event_type')} · {e.get('username') or e.get('user_id') or ''} · {e.get('ts')}")
    # 把所有行拼成一段文本返回，answer() 会把它放进【平台数据】段落。
    return "\n".join(lines)


# _web_search(question, doc_title)：检索公开学术来源，返回 (sources, 拼好的 [S1] 文本)。
# 设计原则：四个来源各自独立 try/except，任何一个超时或返回格式变化都“静默降级”，
# 绝不让外网故障导致整个问答接口失败；最后统一去重、截取 5 条。
def _web_search(question: str, doc_title: str = "") -> tuple[list[dict], str]:
    # 原作者的文档字符串（保留）：说明检索 OpenAlex / arXiv / PubMed 等公开学术来源，失败时静默降级。
    # 注意它是函数体的第一条语句，属于 docstring；新增注释只能放在它上方或之后，不能插进字符串里。
    """检索 OpenAlex / arXiv / PubMed 等公开学术来源，失败时静默降级。"""
    # sources：收集各来源的结果，每个元素是 {title, url, source, snippet} 的字典。
    sources: list[dict] = []
    # 组合检索词：有文档标题时把“标题 + 问题”拼起来（标题能提供领域词，显著提升召回）；没有标题就只用问题。
    # .strip() 去掉首尾空格，避免拼出多余空白。
    query = (doc_title + " " + question).strip() if doc_title else question.strip()
    # 如果问题与标题都为空，就没有可检索的内容。
    if not query:
        # 直接返回空结果与空文本，调用方会显示“（未检索到外部资料）”。
        return sources, ""
    # 抽取“拉丁字母术语”，专门用于英文数据库检索：
    # re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", ...) —— 以字母开头、总长至少 3，允许 . + -（如 GPT-4o、BERT-base、ResNet-50）；
    # if t.lower() not in {...}                      —— 过滤 the/and/for 等英文停用词；
    # dict.fromkeys(...)                             —— 去重且保持首次出现顺序（dict 键有序）；
    # list(...)                                      —— 转成列表，后面可以按位置取 latin_terms[0]。
    latin_terms = list(dict.fromkeys(
        t for t in re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", question + " " + doc_title)
        if t.lower() not in {"the", "and", "for", "with", "from", "this", "that", "what", "how"}
    ))
    # 检索串：有英文术语就用空格连接（英文库对英文关键词更敏感），否则退回原始问题（中文）。
    search_text = " ".join(latin_terms) if latin_terms else question

    # 下面一行是原作者注释，按要求原样保留；它说明了为什么首选 OpenAlex——开放学术元数据，中英文主题都能检索。
    # OpenAlex：开放学术元数据，适合中文/英文主题检索
    # try 包住整个 OpenAlex 分支：网络超时、JSON 解析失败、字段结构变化都在这里被吞掉，不影响其它来源。
    try:
        # 调用 OpenAlex works 接口：
        # params 里 search 是检索词、per-page=3 只要 3 条；
        # timeout=10 表示连接/读取合计最多 10 秒；
        # User-Agent 里带 mailto 是 OpenAlex 的“礼貌池”要求，能获得更稳定的配额。
        # 请求参数与方法分多行书写，属于同一个调用。
        r = httpx.get(
            "https://api.openalex.org/works",
            params={"search": search_text, "per-page": 3},
            timeout=10,
            headers={"User-Agent": "A-Lun local assistant (mailto:local@example.com)"},
        )
        # 遍历返回结果的前 3 条：
        # `(r.json() or {})`          —— 响应体可能为 null；
        # `.get("results") or []`     —— results 字段可能缺失；
        # [:3]                        —— 即使服务端多返回也只取 3 条。
        for item in ((r.json() or {}).get("results") or [])[:3]:
            # 标题：优先 display_name，其次 title，都没有就空串。
            title = item.get("display_name") or item.get("title") or ""
            # 没有标题的记录无法展示，直接跳过。
            if not title:
                # continue 进入下一条结果。
                continue
            # 摘要初始化为空串；下面尝试从倒排索引还原。
            abstract = ""
            # OpenAlex 的摘要以 abstract_inverted_index 形式给出：{单词: [出现位置, ...]}。
            # 没有摘要时该字段为 None，所以用 `or {}` 兜底。
            inv = item.get("abstract_inverted_index") or {}
            # 类型与空值双重校验：既要保证是 dict（防止接口返回列表/字符串），也要保证非空。
            if isinstance(inv, dict) and inv:
                # positions：收集 (位置, 单词) 二元组，稍后按位置排序还原句子。
                positions = []
                # 遍历倒排索引：word 是单词，idxs 是它出现的位置列表。
                for word, idxs in inv.items():
                    # 位置列表可能为 None，`or []` 保证可迭代。
                    for idx in idxs or []:
                        # 把 (位置, 单词) 存入列表；元组排序时先比位置，正好实现“按原文顺序还原”。
                        positions.append((idx, word))
                # 还原摘要：按位置排序后取前 70 个词，用空格连接。
                # 只取 70 个词是近似摘要（完整摘要可能几百词），既够模型判断相关性，又省 token。
                abstract = " ".join(w for _, w in sorted(positions)[:70])
            # 取原文链接：优先 DOI 字段，其次 primary_location.landing_page_url；
            # `(item.get("primary_location") or {})` 防止该字段为 None 时继续 .get 报错。
            url = item.get("doi") or ((item.get("primary_location") or {}).get("landing_page_url") or "")
            # DOI 常见形式是裸的 "10.xxxx/yyy"，不是可点击 URL。
            if url and str(url).startswith("10."):
                # 补上 https://doi.org/ 前缀，前端才能直接跳转。
                url = "https://doi.org/" + str(url)
            # 追加一条来源，统一字段：
            # title/url/source 用于展示与引用；
            # snippet 优先用摘要，摘要为空时退化成“年份 · OpenAlex”占位，并统一截断 500 字。
            # 字典跨两行书写，下一行是同一字面量的延续。
            sources.append({"title": title, "url": url, "source": "OpenAlex",
                            "snippet": (abstract or f"{item.get('publication_year') or ''} · OpenAlex")[:500]})
    # 捕获所有常规异常（Exception 的子类：网络错误、超时、JSON 解析错误、字段类型变化等）。
    # 这里故意捕获宽泛的 Exception，因为外部数据源不可控，任何一条都不应影响主流程。
    except Exception:
        # pass：静默降级——不记录、不抛出，继续尝试下一个来源。
        pass

    # 下面一行是原作者注释，按要求原样保留；它说明 arXiv 只用于技术类问题。
    # arXiv：技术类问题
    # 只有问题或标题里出现“模型/算法/网络/学习/语言/图像/检索/生成/Transformer/CLIP/Mamba/LLM/AI”等技术词时才查 arXiv。
    # `re.I` 忽略大小写；这样能避免为法律、历史等非技术问题发无谓请求。
    if re.search(r"模型|算法|网络|学习|语言|图像|检索|生成|Transformer|CLIP|Mamba|LLM|AI", question + " " + doc_title, re.I):
        # try 包住整个 arXiv 分支，任何失败都静默跳过。
        try:
            # queries：待执行的 arXiv 查询列表，可能一条或多条。
            queries = []
            # 如果提到 Transformer，额外加一条“精确标题”查询，命中经典论文《Attention Is All You Need》的概率很高。
            if re.search(r"Transformer", question + " " + doc_title, re.I):
                # arXiv 查询语法 all:"标题" 表示在全部字段中精确匹配该短语；引号必须保留在字符串里。
                queries.append('all:"Attention Is All You Need"')
            # 再追加一条通用查询：用第一个英文术语（若有）或原问题作为关键词。
            # `"all:" + ...` 是普通字符串拼接，拼出 arXiv 的字段限定语法。
            queries.append("all:" + (latin_terms[0] if latin_terms else question))
            # dict.fromkeys(queries) 去重且保持顺序，避免重复请求同一个查询。
            for arxiv_q in dict.fromkeys(queries):
                # 调用 arXiv Atom API：
                # search_query 查询表达式、start=0 从第一条开始、max_results=3 只要 3 条；
                # timeout=10 秒；User-Agent 标识客户端（arXiv 建议带上）。
                r = httpx.get("https://export.arxiv.org/api/query",
                              params={"search_query": arxiv_q, "start": 0, "max_results": 3},
                              timeout=10, headers={"User-Agent": "A-Lun/1.0"})
                # 用 ET.fromstring 解析返回的 XML 文本，得到根节点 <feed>。
                # 如果返回的是错误页/HTML，这里会抛异常，被下面的 except 捕获。
                root = ET.fromstring(r.text)
                # Atom 命名空间映射：XML 里的标签实际是 {http://www.w3.org/2005/Atom}entry，
                # 用前缀 a 映射后就可以写 "a:entry"。
                ns = {"a": "http://www.w3.org/2005/Atom"}
                # 取前 3 个 <entry>（每篇论文一个），逐个解析。
                for entry in root.findall("a:entry", ns)[:3]:
                    # 标题：findtext 找不到时返回 default=""，再 strip 去掉首尾空白（arXiv 标题常带换行）。
                    title = (entry.findtext("a:title", default="", namespaces=ns) or "").strip()
                    # 摘要：先把连续空白压成一个空格（arXiv 摘要常含换行），再 strip。
                    summary = re.sub(r"\s+", " ", entry.findtext("a:summary", default="", namespaces=ns) or "").strip()
                    # 论文链接：Atom 的 <id> 字段通常是 https://arxiv.org/abs/xxxx 形式。
                    url = entry.findtext("a:id", default="", namespaces=ns) or ""
                    # 有标题才收录，避免推送空记录。
                    if title:
                        # 追加 arXiv 来源，snippet 用摘要前 500 字。
                        sources.append({"title": title, "url": url, "source": "arXiv", "snippet": summary[:500]})
                # 如果此时 sources 已有内容（可能来自前面的 OpenAlex），就不再执行后续查询，直接跳出循环，减少请求。
                if sources:
                    # break 跳出 `for arxiv_q` 查询循环。
                    break
        # 捕获 arXiv 分支的所有常规异常（XML 解析失败、网络错误等）。
        except Exception:
            # 静默降级。
            pass

    # 下面一行是原作者注释，按要求原样保留；它说明 Crossref 是英文主题的兜底来源。
    # Crossref 兜底：英文主题通常能返回原始论文元数据
    # 只有当前结果不足 2 条、且确实抽到了英文术语时才查 Crossref：
    # 结果已经够多就不必再发请求；没有英文词时 Crossref 的中文召回也很差。
    if len(sources) < 2 and latin_terms:
        # try 包住 Crossref 分支。
        try:
            # 调用 Crossref works 接口：
            # query.bibliographic 是“书目检索”，适合用标题/关键词找论文；
            # rows=2 只要 2 条；
            # select 限定返回字段（title/DOI/URL/issued/container-title/abstract），减少响应体积；
            # timeout=8 秒。
            r = httpx.get(
                "https://api.crossref.org/works",
                params={"query.bibliographic": search_text, "rows": 2,
                        "select": "title,DOI,URL,issued,container-title,abstract"},
                timeout=8,
            )
            # 遍历返回条目：`r.json().get("message")` 可能为 None，层层 `or {}` / `or []` 做防御。
            for item in ((r.json().get("message") or {}).get("items") or [])[:2]:
                # Crossref 的 title 是数组（多语言标题），取第一个；`or [""]` 防止空数组导致索引越界。
                title = (item.get("title") or [""])[0]
                # 没有标题就跳过。
                if not title:
                    # continue 处理下一条。
                    continue
                # 摘要清洗：Crossref 摘要常含 JATS/HTML 标签（如 <jats:p>），用正则 `<[^>]+>` 全部替换成空格，再压缩空白。
                snippet = re.sub(r"<[^>]+>", " ", item.get("abstract") or "").strip()
                # 追加 Crossref 来源；URL 用接口返回的 URL 字段，缺失时为空串。
                sources.append({"title": title, "url": item.get("URL") or "",
                                "source": "Crossref", "snippet": snippet[:500]})
        # 捕获 Crossref 分支异常。
        except Exception:
            # 静默降级。
            pass

    # 下面一行是原作者注释，按要求原样保留；它说明 PubMed 只用于医学/临床问题。
    # PubMed：医学/临床问题
    # 只有问题或标题包含“医学/临床/肿瘤/乳腺/超声/患者/诊断”等医学词时才查 PubMed。
    # 这是一个很粗的关键词路由，但足以避免对非医学问题做无效请求。
    if re.search(r"医学|临床|肿瘤|乳腺|超声|患者|诊断", question + " " + doc_title):
        # try 包住 PubMed 分支（它需要两次请求：先 esearch 拿 ID，再 esummary 拿详情）。
        try:
            # 第一步：esearch.fcgi 按关键词检索，retmode=json 要 JSON，retmax=2 最多 2 条。
            r = httpx.get(
                "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                params={"db": "pubmed", "term": search_text, "retmode": "json", "retmax": 2}, timeout=8,
            )
            # 从 esearchresult.idlist 取 PMID 列表；两层 `or {}` / `or []` 防止字段缺失。
            ids = ((r.json().get("esearchresult") or {}).get("idlist") or [])[:2]
            # 只有拿到 ID 才继续查详情。
            if ids:
                # 第二步：esummary.fcgi 批量取摘要信息；多个 ID 用逗号连接（PubMed 的接口约定）。
                d = httpx.get(
                    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
                    params={"db": "pubmed", "id": ",".join(ids), "retmode": "json"}, timeout=8,
                )
                # 响应里的 result 是一个字典：键是 PMID，另外还有一个 "uids" 键（ID 列表），所以下面按 ids 逐个取，避免把 uids 当论文。
                result = (d.json().get("result") or {})
                # 按 esearch 返回的顺序遍历 PMID。
                for uid in ids:
                    # 取单条记录；缺失时用空字典，避免 .get 报错。
                    item = result.get(uid) or {}
                    # 有标题才收录。
                    if item.get("title"):
                        # 追加 PubMed 来源：
                        # url 用 PMID 拼出可访问的详情页；
                        # snippet 用期刊全名（fulljournalname），截 500 字。
                        sources.append({"title": item["title"], "url": f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                                        "source": "PubMed", "snippet": (item.get("fulljournalname") or "")[:500]})
        # 捕获 PubMed 分支异常。
        except Exception:
            # 静默降级。
            pass

    # seen：去重用的集合，存放“标题前 80 个字符”。
    # 同一篇论文可能同时出现在 OpenAlex、Crossref、arXiv，标题相同即可近似判重。
    seen = set()
    # unique：去重后的结果列表，保持首次出现的顺序。
    unique = []
    # 按收集顺序遍历所有来源（OpenAlex → arXiv → Crossref → PubMed）。
    for s in sources:
        # 判重键：标题前 80 个字符。截断是为了容忍不同来源标题尾部的小差异（如副标题、标点）。
        key = (s.get("title") or "")[:80]
        # key 非空且没出现过，才收录。
        if key and key not in seen:
            # 登记该标题，防止后续重复。
            seen.add(key)
            # 加入去重结果。
            unique.append(s)
    # 返回两个值：
    # 1) unique[:5] —— 最多 5 条来源，answer() 原样放进 result["sources"]；
    # 2) 拼好的文本 —— 每条一行，格式 [S1] 来源 · 标题：摘要（URL），供 SYSTEM_PROMPT 规则 2 引用。
    # 这里用生成器表达式 + enumerate(..., 1) 让编号从 1 开始。
    return unique[:5], "\n".join(
        # 单行格式：编号、来源站名、标题、摘要、URL。
        # f-string 中的 s['source'] 等键在构造 sources 时都已保证存在，不会 KeyError。
        f"[S{i}] {s['source']} · {s['title']}：{s['snippet']}（{s['url']}）"
        # 生成器表达式：遍历 unique 的前 5 条，i 从 1 开始编号，与 [S1]、[S2] 的引用格式对应。
        for i, s in enumerate(unique[:5], 1)
    # "\n".join(...) 把每行用换行连接；外层 return 同时返回 sources 列表与这段文本。
    )


# _fallback_answer(citations, sources)：模型网关不可用时的兜底回答。
# 设计目标：接口仍然返回 200 且有信息量——把已经检索到的文档片段与外部来源列给用户看，
# 同时明确告知“模型暂时不可用”，绝不编造答案。
def _fallback_answer(citations: list[dict], sources: list[dict]) -> str:
    # 第一句固定说明：模型网关暂时不可用，以下是已检索到的可核验资料。
    # 这样用户知道当前不是正常回答，而是降级结果。
    parts = ["模型网关暂时不可用，以下是已检索到的可核验资料，请先据此查看："]
    # 最多列 5 条文档引用。enumerate(..., 1) 的 i 在这里没有直接使用，保留它是为了与编号语义一致（可按需改成 [D1]）。
    for i, c in enumerate(citations[:5], 1):
        # 每条写成 [p.页码] 原文摘录；c.get 防字段缺失。
        parts.append(f"[p.{c.get('page')}] {c.get('quote')}")
    # 最多列 3 条外部来源，编号与 [S1] 格式保持一致。
    for i, s in enumerate(sources[:3], 1):
        # 写成 [S编号] 标题：摘要。
        parts.append(f"[S{i}] {s.get('title')}：{s.get('snippet')}")
    # 用换行拼接所有段落返回。即使 citations 和 sources 都为空，也至少会返回第一句说明。
    return "\n".join(parts)


# answer(...)：本模块唯一的公开入口（其它函数都是下划线开头的内部实现）。
# 调用方：main.py 的问答路由通过 `asyncio.to_thread(assistant_api.answer, ...)` 调用。
# 线程说明：本函数是普通同步函数，内部不会 await；因为 SQLite、httpx、llm.chat 都是阻塞调用，
# 上层用线程池执行它，才能避免阻塞 FastAPI 的事件循环。
# 参数列表里的 `*` 表示“之后全是仅关键字参数”（keyword-only）：调用时必须写 answer(question=..., role=...)，
# 这样可以防止位置参数顺序写错，也让调用处更易读。
# 返回值：一个 dict（见函数末尾 result），FastAPI 会把它序列化成 JSON 返回给前端。
# 错误处理约定：本函数不向外抛业务错误码；联网检索失败会静默忽略，模型调用失败会降级成兜底文案，
# 因此调用方通常始终拿到 HTTP 200，只需根据 answer 内容与 used_web/sources 判断是否为降级结果。
def answer(
    # `*`：星号之后的参数全部变成“仅关键字参数”（keyword-only）。
    # 好处：调用时必须写参数名，杜绝位置传参顺序错位；本函数参数多、又有多个空串默认值，这个限制尤其重要。
    *,
    # question：用户问题原文，必填；后续会用于检索、拼提示词和审计。
    question: str,
    # images：图片列表，元素通常是 data:image/...;base64,... 的 Data URL（前端上传后转换），也可以是 http(s) 图片地址。
    # 默认 None 表示没有图片；函数开头会转成空列表。
    images: list[str] | None = None,
    # document_id：绑定的论文文档 ID。空串表示“不针对具体文档”，此时会走通用问答/联网检索。
    document_id: str = "",
    # session_id：练习会话 ID。空串表示独立问答，不注入会话进度。
    session_id: str = "",
    # user_id：当前用户 ID，仅用于审计日志（谁问了什么），不参与数据查询过滤。
    user_id: str = "",
    # tenant_id：租户 ID，多租户环境下的归属标识；当前实现只写进审计，不参与业务查询。
    tenant_id: str = "",
    # username：用户名，写审计日志时便于后台直接展示。
    username: str = "",
    # role：用户角色，默认 "user"。
    # 这是本文件的权限控制点：只有 role == "admin" 且未绑定文档时，才会注入全站【平台数据】。
    role: str = "user",
    # history：前端传来的历史对话，形如 [{"role": "user", "content": "..."}, ...]。
    # 只取最近 6 条、每条截 600 字，防止上下文无限增长。
    history: list[dict] | None = None,
    # use_web：是否允许联网检索外部权威资料，来自前端开关。False 时完全不出网。
    use_web: bool = True,
    # 参数列表结束，返回类型标注为 dict（实际的键见函数末尾）。
) -> dict:
    # 记录开始时间（秒级浮点），函数返回前用它计算总耗时 latency_ms（包含检索 + 模型生成）。
    started = time.time()
    # 把 None 归一成空列表：后面可以放心地 `if images:`、`for url in images:`，不用反复判空。
    # `images or []` 在 images 为 None 或空列表时都会得到新的空列表。
    images = images or []
    # history 同理归一成空列表，避免后续切片 history[-6:] 对 None 报错。
    history = history or []
    # 构造文档上下文（条件表达式）：
    # 有 document_id → 调用 _document_question_context()，返回 (文本, 引用列表, 文档标题)；
    # 没有 document_id → 直接给三个空值，省掉所有文档相关 SQL 与检索。
    # 这是“是否绑定论文”的总开关。
    doc_ctx, citations, doc_title = _document_question_context(document_id, question) if document_id else ("", [], "")
    # 构造会话上下文；没有 session_id 就不查库，直接空串。
    session_ctx = _session_context(session_id) if session_id else ""
    # 构造平台数据，条件是“管理员 且 没有绑定文档”：
    # - 普通用户看不到全站统计（权限隔离）；
    # - 管理员一旦针对某篇文档提问，就优先给文档上下文，避免平台数据把论文内容“带偏”。
    # `and` 的短路特性保证 role 不是 admin 时不会执行 _admin_context()。
    admin_ctx = _admin_context() if role == "admin" and not document_id else ""
    # sources：外部来源列表，初始为空；只有 should_search 为真时才会被 _web_search 填充。
    sources: list[dict] = []
    # web_ctx：外部资料拼好的文本，初始为空串，作用同上。
    web_ctx = ""
    # 计算“是否需要联网检索”，由两部分用 and 连接：
    # bool(use_web) —— 前端开关打开；
    # 后面括号内的 or 条件满足其一即可：
    # a) 没有文档上下文且不是管理员 —— 纯闲聊/通用问题，需要外部知识；
    # b) 问题里出现“权威/最新/外部/研究/论文/文献/指南/标准/定义/区别/为什么/是什么/背景/查一下/网站”等词
    # —— 用户明确在要外部依据或背景知识。
    # 布尔运算有短路特性：use_web 为 False 时，后面的正则根本不会执行。
    should_search = bool(use_web) and (
        (not doc_ctx and role != "admin")
        or bool(re.search(r"权威|最新|外部|研究|论文|文献|指南|标准|定义|区别|为什么|是什么|背景|查一下|网站", question))
    )
    # 只有确实需要才发外部请求；否则 sources 为空、web_ctx 为空，提示词里显示“（未检索到外部资料）”。
    if should_search:
        # 执行检索：把文档标题作为领域提示词一起传入，能显著提高英文数据库的命中率。
        # 返回值解包为 sources（列表）与 web_ctx（拼好的 [S1] 文本）。
        sources, web_ctx = _web_search(question, doc_title)

    # 构造历史对话文本：
    # history[-6:]              —— 只取最近 6 条，越早的越不重要；
    # if m.get("role") in (...) —— 过滤掉 system/tool 等角色，只保留用户与助手；
    # '用户' if ... else '助手'  —— 把英文 role 转成中文标签；
    # str(m.get('content') or '')[:600] —— 内容转字符串、防 None、每条截 600 字。
    # 生成器表达式跨五行书写，最后 join 成一个字符串。
    history_text = "\n".join(
        f"{'用户' if m.get('role') == 'user' else '助手'}：{str(m.get('content') or '')[:600]}"
        for m in history[-6:]
        if m.get("role") in ("user", "assistant")
    )
    # prompt_parts：提示词的七个段落，顺序是刻意安排的：
    # 文档 → 会话 → 平台 → 权威资料 → 历史 → 用户问题 → 行为指令。
    # 先给“事实材料”，再给“对话历史”，最后给“问题与要求”，符合大模型的注意力习惯，也方便排查问题。
    # 每个段落都用【】标题包裹；没有内容时给“（无）”之类的占位，避免模型把空段落误读成“没有限制”。
    prompt_parts = [
        # 第 1 段：文档上下文。没有文档时写“（本轮没有指定文档）”，明确告诉模型不要假装看过论文。
        "【文档上下文】\n" + (doc_ctx or "（本轮没有指定文档）"),
        # 第 2 段：会话上下文（练习进度/报告/最近作答）。没有会话时写“（无）”。
        "【会话上下文】\n" + (session_ctx or "（无）"),
        # 第 3 段：平台数据。只有管理员问答才有内容，普通用户这里是“（无）”。
        "【平台数据】\n" + (admin_ctx or "（无）"),
        # 第 4 段：权威资料（外部检索结果）。没检索到时写“（未检索到外部资料）”，配合 SYSTEM_PROMPT 规则 2/5。
        "【权威资料】\n" + (web_ctx or "（未检索到外部资料）"),
        # 第 5 段：历史对话。没有历史时写“（无）”。
        "【历史对话】\n" + (history_text or "（无）"),
        # 第 6 段：用户当前问题，原样放入（不截断），保证模型看到完整诉求。
        f"【用户问题】{question}",
        # 第 7 段：最终行为指令，重申引用格式（文档页码、[S1] 编号）以及“没有依据就直说”。
        # 放在最后是因为它离模型生成位置最近，约束效果最好。
        "请基于以上材料回答。引用文档时写页码，引用外部资料时写 [S1] 等编号；如果没有依据请直说。",
    # 多行列表字面量结束。
    ]
    # 用两个换行（空行）连接七个段落：段落之间留白能让模型更清晰地区分材料边界。
    text_prompt = "\n\n".join(prompt_parts)
    # content：发给模型的 user 消息内容，默认就是这段纯文本。
    # 类型标注 Any 是因为有图片时它会被改成列表（多模态格式）。
    content: Any = text_prompt
    # 有图片时，必须改成 OpenAI 兼容的“多模态内容数组”，不能再用纯字符串。
    if images:
        # 多模态数组的第一项是文本：{"type": "text", "text": ...}；
        # 后续每张图片追加一个 {"type": "image_url", ...} 项。
        content = [{"type": "text", "text": text_prompt}]
        # 逐张处理用户上传的图片（可能是多张截图）。
        for url in images:
            # 图片项格式：image_url.url 可以是 Data URL（base64 内嵌）或公网 URL，由前端决定。
            content.append({"type": "image_url", "image_url": {"url": url}})

    # answer_text：模型回答文本，先初始化为空串；异常分支会把它覆盖成兜底文案。
    answer_text = ""
    # 读取当前配置的模型名（后台可改），用于返回给前端展示“本次由哪个模型回答”。
    # 第二个参数是默认值：配置缺失时返回空串，不会抛异常。
    model_name = settings.get("llm.model", "")
    # try：模型调用可能因为网关未配置、超时、HTTP 4xx/5xx、返回空内容等失败，
    # 这里统一兜底，保证问答接口不因模型故障而 500。
    try:
        # 调用模型网关 llm.chat：
        # messages 只有两条：system 放行为准则，user 放七段材料 + 问题；
        # kind="assistant_chat" —— 用量记账的分类标签，会写进 usage_log；
        # json_mode=False       —— 本场景要自然语言回答，不要 JSON 结构；
        # max_tokens=1700       —— 限制回答长度，控制成本与延迟；
        # temperature=0.25      —— 偏低温度，减少发挥、降低幻觉；
        # document_id/session_id —— 传给 llm 记录 token 消耗，便于按文档/会话统计；
        # 空串转 None，表示“不归属任何文档/会话”。
        # 最后 .strip() 去掉模型输出首尾空白。
        answer_text = llm.chat(
            [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}],
            kind="assistant_chat", json_mode=False, max_tokens=1700, temperature=0.25,
            document_id=document_id or None, session_id=session_id or None,
        ).strip()
    # 捕获所有常规异常（LLMError、httpx 超时、JSON 解析失败等，均为 Exception 的子类）。
    # 这里故意不区分错误码：对用户而言都是“模型暂时不可用”，统一走降级。
    except Exception:
        # 降级回答：把已检索到的文档引用和外部来源列出来，并说明模型网关不可用；
        # 这样用户至少能看到可核验的资料，体验上优于直接报错。
        answer_text = _fallback_answer(citations, sources)

    # result：最终返回给前端的字典，字段名是前后端约定的接口契约。
    result = {
        # answer：模型回答或兜底文案。
        "answer": answer_text,
        # citations：文档引用，最多 10 条（_document_context 与 _rank_chunks 可能合计超过 10 条）。
        "citations": citations[:10],
        # sources：外部来源，最多 5 条（_web_search 已限制过一次，这里再兜底截断）。
        "sources": sources[:5],
        # used_web：本次是否真的执行了联网检索（注意不是“是否检索到结果”），前端可据此显示“已联网”标识。
        "used_web": should_search,
        # model：本次使用的模型名，来自后台配置。
        "model": model_name,
        # latency_ms：总耗时（毫秒）。`time.time() - started` 是秒，乘 1000 后 int 取整。
        # 包含文档检索、会话查询、可能的联网检索以及模型生成的全部时间。
        "latency_ms": int((time.time() - started) * 1000),
    # 字典字面量结束。
    }
    # 写审计日志 audit.log_event：记录“谁在什么时候问了什么”。
    # 审计写入失败不会影响回答——audit.log_event 内部用 try/except 吞掉了数据库异常。
    audit.log_event(
        # 审计字段：
        # user_id / username / tenant_id —— 回答者身份与租户；
        # event_type="assistant_ask"     —— 事件类型，后台“最近事件”按它筛选/展示。
        user_id=user_id, username=username, tenant_id=tenant_id, event_type="assistant_ask",
        # detail：事件详情（会以 JSON 字符串存进 activity_event.detail）。
        # document_id / session_id —— 关联对象，便于回溯；
        # has_image                —— 是否带图提问，用于分析多模态使用率；
        # question[:120]           —— 只存前 120 字，避免审计表膨胀、也减少敏感信息留存；
        # sources / used_web       —— 外部来源数量与是否联网，用于评估检索效果。
        detail={"document_id": document_id, "session_id": session_id, "has_image": bool(images),
                # detail 字典的后续字段行（与上一行同属一个字典字面量）。
                "question": question[:120], "sources": len(sources), "used_web": should_search},
    # log_event 调用结束。
    )
    # 返回结果字典。调用方 main.py 拿到后还会补上 document_id 等字段，再序列化成 HTTP 响应。
    return result
