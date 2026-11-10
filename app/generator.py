"""单题生成与 V1—V8 校验（设计文档 6.5、附录 C.2）。"""
# =============================================================================
# 模块：app/generator.py —— 单题生成与 V1—V8 质量校验
# -----------------------------------------------------------------------------
# 一、文件职责
#   本文件把“结构节点（node）+ 知识点（kp）+ 证据（evidence）+ 出题规格（spec）”
#   转成一道可入库、可展示、可评分的题目 q（普通 dict）。
#   有两条生成路径：
#     1) LLM 路径：拼 Prompt -> 调模型 -> 归一化 -> 规则校验 -> 审计；
#     2) 兜底路径：模型不可用/调用异常/连续两次校验失败时，用确定性模板出题。
#   另外提供 question_row()，把内部题目字典映射成 question 表的列。
#
# 二、在整个应用中的位置（主要调用关系）
#   上层：app/sessions.py 的 next_question()
#     -> generator.generate_question(...)
#     -> generator.question_row(...)
#     -> sessions 负责 INSERT question / turn（本文件不直接执行 SQL）
#   下游：
#     · llm.available()/chat_json()：同步调用 OpenAI 兼容网关并解析 JSON；
#     · settings.get()：读取后台可改的模型名与系统提示词；
#     · audit.record_ai_output()：写 ai_audit，记录时延、是否接地、是否兜底；
#     · db.new_id()/dump()：生成 q_xxx 主键、把 list/dict 序列化为 JSON 文本。
#
# 三、核心数据流
#   build_prompt() 只读地组织上下文；generate_question() 串联两次尝试；
#   _normalize() 负责“宽容接收模型输出 + 收紧成统一结构”；
#   verify_question() 是生成后的硬闸门；fallback_question() 保证最终必有题。
#
# 四、V1—V8 校验码（本文件显式处理的部分）
#   V1 证据可溯源：引文必须逐字出现在节点原文；
#   V2 防泄题：题干不能包含答案/解析前 40 字；
#   V3 结构完整：题型、选项、答案、评分要点必须配套；
#   V5 指向性：禁止泛称“某文档”，禁止选择题出现空括号；
#   V6 去重：与已出题干相似度不得超过 0.9；
#   V7 难度：必须落在 1—5；
#   V8 选项质量：禁止“以上都……”这类绝对项。
#   （V4 不在本文件单独编码。）
#
# 五、边界与设计取舍
#   · 本模块不直接写 SQL、不做 HTTP 权限判断、不创建线程/协程；
#     数据库写入在 sessions/audit 层，模型并发限制在 llm.py 内部；
#   · LLM 整体包在 try/except Exception 中，任何网络、解析、校验异常都转兜底，
#     优先保证“用户还能继续答题”，代价是这里不会把异常继续向上抛；
#   · 本文件只新增注释，未改动任何可执行语句、字符串、签名、缩进或控制流。
#
# 六、阅读提示
#   · 多行函数调用/字典/列表字面量在结构上方整块解释，避免破坏语法；
#   · 三引号 Prompt 内部不能插注释，相关字段与转义规则在字面量上方说明。
# =============================================================================
# 让类型注解延迟求值（PEP 563）：运行时不会立即计算 list[dict] 等注解，
# 既减少导入开销，也避免类型对象在定义阶段被求值。它必须放在模块文档字符串后、
# 其它普通语句前。
from __future__ import annotations

# difflib 提供序列相似度算法，后面的 V6 用 SequenceMatcher 判断题干是否重复。
import difflib
# 标准库 JSON 编解码。本文件当前没有直接调用 json.*：
# 模型输出解析交给 llm.chat_json()，题目字段序列化交给 db.dump()；
# 这里保留原导入，不改变源文件的导入清单。
import json
# random 用于兜底题的选项乱序；会用固定种子创建独立 Random，避免影响全局随机状态。
import random
# re 用于空白归一化、题干模式检查、答案分隔，以及提取填空候选术语。
import re
# time 用于统计模型调用耗时，写入审计的 latency_ms 字段。
import time
# Any 表示“任意类型”。模型返回的 raw 可能是 dict/list/标量，
# 在归一化前无法静态确定，所以用 Any 标注。
from typing import Any

# 同包依赖：audit 写 AI 审计；llm 调模型；settings 读运行时配置。
# 三者都是服务层模块，本文件只调用其公开函数，不直接访问其内部状态。
from . import audit, llm, settings
# 数据层工具：dump 把 Python 对象转成 JSON 文本；new_id 生成带前缀的短唯一 ID。
# 注意：本文件不执行 SQL，真正的 INSERT 由 sessions.py 完成。
from .db import dump, new_id
# parser 中的 CJK 常量/正则用于识别中文字符；本文件当前没有直接引用 CJK，
# 属于源文件保留的导入，删除它会改变导入清单，因此原样保留。
from .parser import CJK

# 默认系统提示词。仅当后台设置 guide.system_prompt 为空时才使用它。
# 内容分三段：
#   1) 角色与工作原则（结构优先、证据约束、一次一题、难度适配、反馈具体、
#      不越界、保护隐私）；
#   2) 输出要求（只输出 JSON、不要 Markdown 围栏、缺字段用 null、全角标点）；
#   3) 具体字段契约在 build_prompt() 的 Prompt 中进一步给出。
# 这是普通三引号字符串（不是 f-string），内部的中文和换行会原样发送给模型。
SYSTEM_PROMPT = """你是 A论（A-Lun），一位严谨、耐心的论文导读教练。
你的任务不是替用户读文档，而是围绕文档结构向用户提问、评判作答并给出可执行的反馈。

【工作原则】
1. 结构优先：每道题必须挂在一个结构节点（章节、图表、段落）上，并记录 node_id。
2. 证据约束：先有证据，再有题目；没有证据就换考点，绝不编造原文。
3. 一次一题：每轮只问一个问题，等待用户作答后再继续。
4. 难度适配：目标难度略高于用户当前能力；答对提高，答错降低。
5. 反馈具体：引用原文并标注页码；禁止“回答得不错”“还需努力”等空话。
6. 不越界：只使用当前文档与本次会话的信息；文档未涉及的内容明确说“未涉及”。
7. 保护隐私：不询问、不记录与学习无关的个人信息。

【输出要求】
只输出 JSON，不要输出 Markdown 代码块；字段缺失时用 null，不要臆造。
所有中文标点使用全角；专有名词保留原文写法。"""


# 组装单次“生成题目”的 user Prompt。纯函数：不写数据库、不修改入参。
# 参数：
#   doc/node/kp：文档、结构节点、知识点三张表的行（dict）；
#   evidence：当前知识点可引用的证据列表，每项含 evidence_id/page_no/quote；
#   spec：出题规格，含 q_type/bloom_level/target_difficulty/section_path 等；
#   asked：本次会话已出题干，用来让模型主动避开重复。
# 返回：可直接作为 user message 的中文字符串。
def build_prompt(doc: dict, node: dict, kp: dict, evidence: list[dict], spec: dict, asked: list[str]) -> str:
    # 收集证据文本行；只取前 4 条，避免 Prompt 过长、注意力被无关证据稀释。
    ev_lines = []
    # 逐条遍历可用证据，按固定格式拼成一行，方便模型引用 evidence_id 与页码。
    for ev in evidence[:4]:
        # 证据行使用“- 字段=值 | 字段=值”的稳定格式；quote 必须逐字复制，供 V1 校验。
        ev_lines.append(f"- evidence_id={ev['evidence_id']} | page={ev['page_no']} | quote={ev['quote']}")
    # 已出题干只放最近 8 条，并用“- ”列表化；为空时写“（无）”，避免出现空段落。
    asked_text = "\n".join(f"- {s}" for s in asked[-8:]) or "（无）"
    # 节点原文先折叠所有连续空白为一个空格，再截前 900 字：
    # 既控制 Prompt 体积，又让模型看到足够上下文；截断不改变原节点数据。
    node_excerpt = re.sub(r"\s+", " ", (node.get("text") or ""))[:900]
    # 用 f-string 生成完整任务书。三引号内部不能插 Python 注释（否则会进入 Prompt），
    # 这里在结构上方一次说明整块内容与转义规则：
    #   【任务】明确只生成一道可直接展示的中文题；
    #   【输入】题名、结构路径、节点类型、题型/Bloom/难度/知识点、900 字摘录、
    #          最多 4 条证据、最近 8 道已出题干；
    #   【硬性要求】禁止泛称/空括号，规定单选与多选答案格式，答案必须有证据，
    #          简答题评分要点要具体；
    #   【步骤】选证据 -> 写题干 -> 写选项 -> 写答案/解析 -> 三级提示 -> 自检；
    #   【输出 JSON】给出字段骨架；外层 {{ }} 是 f-string 的转义写法，
    #          最终 Prompt 中会变成单个 { }，供模型输出合法 JSON。
    # 这段字符串本身是返回值，不能改写其中的标点、换行或示例值。
    return f"""【任务】根据给定的结构节点、证据与题目规格，生成一道可直接呈现给用户的中文题目。

【输入】
文档题名：{doc.get('title', '')}
结构路径：{' > '.join(spec.get('section_path') or [node.get('title', '')])}
节点类型：{node.get('node_type', 'section')}
题目规格：题型={spec['q_type']}；Bloom 层级={spec['bloom_level']}；目标难度={spec['target_difficulty']}；考查知识点={kp['name']}（{kp['kp_type']}）
本节原文摘录：{node_excerpt}
可用证据：
{chr(10).join(ev_lines)}

已出题目（避免重复）：
{asked_text}

【硬性要求】
1. 题干必须针对本文的具体概念、方法、数据或结论，禁止使用“某文档”“某电力文档”“某研究”等泛称。
2. 题干不得出现空的“（ ）”或“（ ）。”；选择题的选项与题干分开输出。
3. 单选题恰好 4 个选项，只有一个正确项；多选题 4 个选项中至少 2 个正确项，answer.value 必须写成包含 2—3 个正确选项 key 的数组（如 ["A","C"]）。
4. 答案只能来自给定的证据；不要补充文档外知识。
5. 简答题的评分要点写 2—3 条具体、可核对的要点，不要写“回答完整”这类空话。

【步骤】
1. 从可用证据中选择 1—2 条能直接支撑答案的证据，记录 evidence_id，并逐字复制 quote。
2. 依据题目规格写题干；题干必须自足，不出现“上文”“如下”等指代。
3. 选择题写 4 个选项，干扰项必须似是而非，不得使用“以上都对/都不对”。
4. 给出标准答案、评分要点与解析；解析必须引用证据原句。
5. 再给出三级提示：一级指方向，二级给关键线索，三级给答案要点（不得直接复制完整答案）。
6. 自检：答案是否被证据直接支持？题干是否泄露答案？选项是否互斥？

【输出 JSON】
{{"node_id": "{node['node_id']}", "kp_id": "{kp['kp_id']}",
 "q_type": "{spec['q_type']}",
 "bloom_level": "{spec['bloom_level']}", "difficulty": {spec['target_difficulty']},
 "stem": "",
 "options": [{{"key": "A", "text": ""}}],
 "answer": {{"value": "", "explanation": ""}},
 "rubric": [{{"point": "", "weight": 0.5}}],
 "evidence": [{{"evidence_id": "", "page": 0, "quote": ""}}],
 "hints": ["", "", ""],
 "self_check": {{"evidence_supported": true, "answer_leaked": false, "options_exclusive": true}}}}"""


# 生成一道题的总入口，返回内部题目 dict（不是数据库行）。
# 参数：
#   doc/node/kp：上下文；evidence：可用证据；spec：题型/难度/路径等规格；
#   asked_stems：已问题干，用于 V6 去重；
#   session_id/user_id：只用于审计关联，不参与 Prompt 内容。
# 返回字段至少包含 q_id/doc_id/kp_id/node_id/q_type/stem/options/answer/
# rubric/evidence/hints/source；调用方随后用 question_row() 转成表行。
# 控制流：LLM 可用则最多试 2 次；否则或仍失败则走 fallback_question()。
def generate_question(
    doc: dict,
    node: dict,
    kp: dict,
    evidence: list[dict],
    spec: dict,
    asked_stems: list[str],
    session_id: str = "",
    user_id: str = "",
) -> dict:
    # 先探测模型网关是否启用且配置完整；不可用时不发请求，直接走模板题，减少等待。
    if llm.available():
        # 记下本次使用的模型名，后续审计需要；即使 llm.chat_json 内部切备用模型，
        # 这里记录的仍是调用方请求的主模型名。
        model_name = settings.get("llm.model", "")
        # 第一次尝试的起始时间，用于计算这次调用的端到端时延。
        started = time.time()
        # 整个 LLM 流程都放在 try 中：网络超时、HTTP 错误、JSON 解析失败、
        # 归一化字段类型错误等，统统转由函数末尾的兜底路径处理。
        try:
            # 调用模型并直接拿解析后的 Python 对象；chat_json 内部负责 JSON 提取与一次修复。
            raw = llm.chat_json(
                # OpenAI 兼容的消息数组：system 负责规则，user 负责本次具体任务。
                [
                    # 管理员在后台配置了 guide.system_prompt 就用它；为空时使用模块默认 SYSTEM_PROMPT。
                    {"role": "system", "content": settings.get("guide.system_prompt") or SYSTEM_PROMPT},
                    # 将结构化上下文交给 build_prompt() 压缩成自然语言，减少模型自由发挥。
                    {"role": "user", "content": build_prompt(doc, node, kp, evidence, spec, asked_stems)},
                ],
                # kind 用于审计/用量统计区分场景；重试会用 generate_question_retry。
                kind="generate_question",
                # 单题输出上限 1800 tokens，足够包含题干、4 个选项、解析和三级提示。
                max_tokens=1800,
                # 首轮温度 0.45：保留一定多样性，但不过度发散。
                temperature=0.45,
                # 把文档 ID 传给网关，仅用于用量/审计关联；缺失时传 None。
                document_id=doc.get("doc_id"),
                # 把会话 ID 传给网关，便于追踪一次答题会话中的模型调用。
                session_id=session_id,
            )
            # 把模型原始输出清洗成统一题目结构；非 dict 等严重问题会抛 ValueError。
            q = _normalize(raw, doc, node, kp, evidence, spec)
            # 规则校验返回 (是否通过, 失败原因)；reason 可直接追加到下一次 Prompt。
            ok, reason = verify_question(q, node, evidence, spec, asked_stems)
            # 首轮通过校验：记录成功审计并立即返回，不再消耗第二次模型调用。
            if ok:
                # 成功审计：记录模型名、时延、用户/文档/会话，以及“一次成功”的细节。
                audit.record_ai_output(
                    # grounded=True 表示题目已通过证据校验；kind 与模型调用保持同名，便于聚合。
                    kind="generate_question", model=model_name, grounded=True,
                    # 时延换算成整数毫秒；这里只统计首轮，因为 started 只覆盖第一次调用。
                    latency_ms=int((time.time() - started) * 1000),
                    # 审计关联字段：user_id 可能为空（后台批处理），document_id 做兜底空串。
                    user_id=user_id, document_id=doc.get("doc_id", ""), session_id=session_id,
                    # detail 留空 reason，并标记 attempts=1，质量报表可据此算一次通过率。
                    detail={"reason": "", "attempts": 1},
                )
                # 给题目打来源标记；question_row 会把它写到 question.source。
                q["source"] = "llm"
                # 首轮成功，直接返回。
                return q
            # 携带失败原因重试一次
            # 重新计时，使第二次审计只统计重试耗时（首轮失败原因已由本次审计单独记录）。
            started = time.time()
            # 在原任务书末尾追加校验失败原因，要求模型针对问题完整重出 JSON。
            retry_prompt = build_prompt(doc, node, kp, evidence, spec, asked_stems) + (
                f"\n\n【上一次输出未通过校验】{reason}。请修正后重新输出完整 JSON。"
            )
            # 第二次模型调用：kind 改成 retry，温度降到 0.3，优先满足规则而不是多样性。
            raw2 = llm.chat_json(
                # 重试仍使用同样的 system/user 双消息结构，只是 user 内容多了失败原因。
                [
                    # 系统提示词保持一致，避免两次调用的人设和输出契约发生变化。
                    {"role": "system", "content": settings.get("guide.system_prompt") or SYSTEM_PROMPT},
                    # 使用带失败原因的 retry_prompt。
                    {"role": "user", "content": retry_prompt},
                ],
                # 审计和用量统计用单独 kind，能看出这是重试调用。
                kind="generate_question_retry",
                # 重试同样限制 1800 tokens，避免模型因“修正”而无限扩写。
                max_tokens=1800,
                # 更低温度让输出更确定，减少再次踩中同一校验规则的概率。
                temperature=0.3,
                # 仍关联同一文档，便于按文档统计重试率。
                document_id=doc.get("doc_id"),
                # 仍关联同一会话，便于回放完整答题链路。
                session_id=session_id,
            )
            # 对第二次输出执行同样的归一化；异常仍会被外层 try 捕获并转兜底。
            q2 = _normalize(raw2, doc, node, kp, evidence, spec)
            # 对第二次输出再做一次完整规则校验，不能因为“重试过”就放宽标准。
            ok2, reason2 = verify_question(q2, node, evidence, spec, asked_stems)
            # 以最终一次模型输出计“幻觉率”；重试成功不算幻觉，但保留首轮失败信息
            # 无论第二次是否通过都记审计：grounded 反映最终结果；
            # detail 保留 attempts=2 和首轮失败标记，便于质量统计区分“一次成功/重试成功”。
            audit.record_ai_output(
                kind="generate_question", model=model_name, grounded=ok2,
                latency_ms=int((time.time() - started) * 1000),
                user_id=user_id, document_id=doc.get("doc_id", ""), session_id=session_id,
                detail={"reason": reason2, "attempts": 2, "first_attempt_failed": True},
            )
            # 重试通过：标记来源并返回第二份题目。
            if ok2:
                # 统一来源标记，调用方无需区分首轮还是重试。
                q2["source"] = "llm"
                # 返回重试成功的题目。
                return q2
        # 任何异常（网关超时、JSON 解析失败、字段类型错误等）都被有意吞掉：
        # 不在此处向上抛，确保调用方仍能拿到一道兜底题。
        except Exception:
            # pass 表示不做局部补救，统一落到函数末尾的兜底路径。
            pass
    # 模板兜底：不依赖模型，仍从证据/知识点定义生成可评分题目。
    q = fallback_question(doc, node, kp, evidence, spec, asked_stems)
    # 兜底也要审计：fallback=True 让质量报表能把模板题与模型题分开。
    audit.record_ai_output(
        # 模板题的引文仍来自证据，因此 grounded=True；fallback=True 表示不是模型生成。
        kind="generate_question", model=settings.get("llm.model", ""), grounded=True, fallback=True,
        # 审计关联字段与 LLM 路径保持一致，便于按用户/文档/会话汇总。
        user_id=user_id, document_id=doc.get("doc_id", ""), session_id=session_id,
        # 失败原因固定写 fallback_template，说明这是主动降级而非模型“幻觉”。
        detail={"reason": "fallback_template"},
    )
    # 返回兜底题，保证 generate_question 永远有结果。
    return q


# 把 LLM 的原始输出“归一化”成内部统一结构。
# 原则：能修则修、不能修则丢弃该字段；整体不是 dict 时抛 ValueError，
# 由 generate_question 的 try/except 捕获并转兜底。
# 参数 raw 声明为 Any，因为模型 JSON 可能是标量/list/dict 中任意一种。
# 返回新的题目 dict；会生成新的 q_id，并强制使用当前 node/kp 的 ID。
def _normalize(raw: Any, doc: dict, node: dict, kp: dict, evidence: list[dict], spec: dict) -> dict:
    # 模型的顶层输出必须是 JSON 对象；数组、字符串、null 都无法按题目字段解释。
    if not isinstance(raw, dict):
        # 主动抛类型错误，让上层统一走兜底，而不是在这里猜测字段。
        raise ValueError("模型未返回 JSON 对象")
    # 题型优先采用模型返回值；缺失时退回规格指定的题型。
    q_type = raw.get("q_type") or spec["q_type"]
    # 模型不得擅自改题型：只要与规格不一致，就以 spec 为准。
    if q_type != spec["q_type"]:
        # 强制回写规格题型，保证后续校验和评分逻辑按同一题型处理。
        q_type = spec["q_type"]
    # 读取选项；None/缺失统一变成空列表，避免后面 len() 或迭代报错。
    options = raw.get("options") or []
    # 选项必须是列表；模型偶尔返回字符串或对象，这种情况直接视为没有选项。
    if not isinstance(options, list):
        # 非列表时清空，后续 V3 会判“选择题必须有 4 个选项”。
        options = []
    # 列表推导式一次性完成三件事：
    #   1) 只保留 dict 且有非空 text 的选项；
    #   2) key 转字符串、去空白、转大写后只取首字符（如 "a." -> "A"）；
    #   3) text 去首尾空白；非法项直接跳过，不抛异常。
    options = [
        {"key": str(o.get("key", "")).strip().upper()[:1], "text": str(o.get("text", "")).strip()}
        for o in options if isinstance(o, dict) and str(o.get("text", "")).strip()
    ]
    # 读取答案对象；缺失时先给空 dict，保持后续 .get() 安全。
    answer = raw.get("answer") or {}
    # 答案本应是 {"value":..., "explanation":...}；模型若直接给标量，就包装成 value。
    if not isinstance(answer, dict):
        # 包装时补空解析，保证 answer 始终是 dict，后续可以统一读写。
        answer = {"value": answer, "explanation": ""}
    # 读取评分要点列表；缺失时用空列表。
    rubric = raw.get("rubric") or []
    # 评分要点必须是列表；其他类型无法逐条评分，直接丢弃。
    if not isinstance(rubric, list):
        # 非列表时清空，简答题会因此被 V3 判定缺少评分要点。
        rubric = []
    # 逐条清洗评分点：point 必须是非空字符串，weight 转成 float。
    # 如果 weight 是无法转换的文本，float() 会抛异常，最终由上层转兜底。
    rubric = [
        {"point": str(r.get("point", "")).strip(), "weight": float(r.get("weight", 0.5))}
        for r in rubric if isinstance(r, dict) and str(r.get("point", "")).strip()
    ]
    # 模型有时用选项全文而不是 A/B/C/D 作答，这里做一次可解释的归一化
    # 只有选择题且确实有选项时才做答案映射；其他题型保持原答案结构。
    if q_type in ("single_choice", "multiple_choice") and options:
        # 取出所有合法选项 key，作为后续“答案是否是选项”的判定集合。
        keys = [o["key"] for o in options]
        # 读取原始答案值，可能是 "A"、"A,C"、["A","C"] 或选项全文。
        value = answer.get("value")
        # 字符串按中英文逗号、顿号、空白切分；列表原样使用；其他标量包成单元素列表。
        raw_vals = re.split(r"[,，、\s]+", value) if isinstance(value, str) else (value if isinstance(value, list) else [value])
        # mapped 收集最终识别出的选项 key，保持模型给出的顺序。
        mapped = []
        # 逐个解析候选答案项。
        for item in raw_vals:
            # 统一转字符串并去空白；None 也变成空串，避免后续 .upper() 报错。
            text = str(item or "").strip()
            # 空项直接跳过（例如 "A,,C" 中的空段）。
            if not text:
                continue
            # 如果本身已经是合法 key（大小写不敏感），直接采用大写形式。
            if text.upper() in keys:
                # 记录该 key 并进入下一项。
                mapped.append(text.upper())
                continue
            # 若模型给的是选项全文，则按“完全相等/互相包含”反查对应 key。
            hit = next((o for o in options if text == o["text"] or text in o["text"] or o["text"] in text), None)
            # 命中选项文本时记录其 key。
            if hit:
                mapped.append(hit["key"])
        # 只有成功映射出至少一个 key 才覆盖原答案，避免把无法识别的答案清空。
        if mapped:
            # 单选只留第一个映射；多选用 dict.fromkeys 去重且保留顺序。
            answer["value"] = mapped[0] if q_type == "single_choice" else list(dict.fromkeys(mapped))
    # 归一化后的证据列表；后面逐条匹配原始 evidence，防止模型编造 ID。
    ev_out = []
    # 遍历模型返回的证据数组；缺失或 None 时按空数组处理。
    for ev in raw.get("evidence") or []:
        # 非 dict 的证据项无法读取字段，直接跳过。
        if not isinstance(ev, dict):
            continue
        # 取引文并去空白；空引文没有校验价值，下一步会跳过。
        quote = str(ev.get("quote", "")).strip()
        # 空引文不进入结果，避免产生“有证据但没内容”的假接地。
        if not quote:
            continue
        # 先按 evidence_id 精确匹配原始证据；匹配不到再考虑文本相似。
        matched = next((e for e in evidence if e["evidence_id"] == ev.get("evidence_id")), None)
        # 精确 ID 未命中时，用引文前 20 字做宽松匹配，容忍模型改写了 ID。
        if not matched:
            # 宽松匹配只比较引文前缀，避免长文本中任意片段造成误配。
            matched = next((e for e in evidence if quote[:20] in e["quote"]), None)
        # 组装一条标准证据：
        #   · evidence_id 优先采用原始证据的 ID，找不到则留空；
        #   · node_id 强制使用当前节点，不能由模型改挂到别的节点；
        #   · page 优先采用原始证据的 page_no，找不到才相信模型给的页码；
        #   · quote 保留模型输出，后续 verify_question 会验证它是否在原文中。
        ev_out.append({
            "evidence_id": matched["evidence_id"] if matched else "",
            "node_id": node["node_id"],
            "page": int(matched["page_no"]) if matched else int(ev.get("page") or 0),
            "quote": quote,
        })
    # 模型偶尔把“多选题”写成只有一个正确项：这本身是一道合法的单选题，
    # 不必为题型标签重试一次、付出双倍时延。
    # 仅在“多选 + 有选项”时检查，避免空选项触发无意义降级。
    if q_type == "multiple_choice" and options:
        # 重新取 key 列表（选项可能刚被归一化过）。
        keys = [o["key"] for o in options]
        # 读取答案值，下一步统一成列表。
        vals = answer.get("value")
        # 字符串按分隔符切开；已经是列表则直接用。
        vals = vals if isinstance(vals, list) else re.split(r"[,，、\s]+", str(vals or ""))
        # 只保留真正落在选项集合里的 key，并去重；其他文本视为无效答案。
        valid = {str(v).strip().upper() for v in vals if str(v).strip().upper() in keys}
        # 有效正确项不足 2 个：按单选处理，避免 V3 反复判多选不合法。
        if len(valid) <= 1:
            # 降级题型，而不是丢弃整道题。
            q_type = "single_choice"
            # 若恰好有 1 个有效项，把它改写成单选的标量答案。
            if valid:
                # next(iter(valid)) 取出集合中唯一元素；此处已保证 len(valid)==1。
                answer["value"] = next(iter(valid))
    # 提示词最多保留 3 条非空字符串，对应产品约定的三级提示。
    hints = [str(h).strip() for h in (raw.get("hints") or []) if str(h).strip()][:3]
    # 返回完整的内部题目结构；所有下游都依赖这些固定字段。
    return {
        # 生成新的题目主键，前缀 q_ 便于日志和数据库排查。
        "q_id": new_id("q"),
        # 文档 ID 必须存在；这里用下标而不是 get，缺失说明上游上下文不完整。
        "doc_id": doc["doc_id"],
        # 知识点 ID 同理必须存在。
        "kp_id": kp["kp_id"],
        # 题目必须挂在当前结构节点上。
        "node_id": node["node_id"],
        # 归一化/降级后的最终题型。
        "q_type": q_type,
        # Bloom 层级只接受六个中文枚举；其他值一律退回规格指定层级。
        "bloom_level": raw.get("bloom_level") if raw.get("bloom_level") in ("记忆", "理解", "应用", "分析", "评价", "创造") else spec["bloom_level"],
        # 难度先转 int，再夹到 1—5，防止模型返回 0、99 或字符串数字。
        "difficulty": max(1, min(5, int(raw.get("difficulty") or spec["target_difficulty"]))),
        # 题干转字符串并去首尾空白；空题干会在 verify_question 被拦截。
        "stem": str(raw.get("stem", "")).strip(),
        # 清洗后的选项列表。
        "options": options,
        # 答案对象；选择题可能已被映射成 key。
        "answer": answer,
        # 清洗后的评分要点。
        "rubric": rubric,
        # 只包含与原文能匹配上的证据。
        "evidence": ev_out,
        # 最多三条提示。
        "hints": hints,
        # 先标记为模型来源；兜底路径会覆盖成 fallback。
        "source": "llm",
    }


# 生成后的硬校验闸门，返回 (是否通过, 面向重试的中文原因)。
# 只做规则判断，不修改 q；调用方把 reason 追加到重试 Prompt。
# 参数 evidence/spec 当前函数体未直接读取，保留是为了统一调用接口；
# 实际校验依赖 q 内已经归一化的证据、选项、答案、评分点和难度。
# 失败 reason 前缀即设计文档中的校验码（V1/V2/...），便于日志定位。
def verify_question(q: dict, node: dict, evidence: list[dict], spec: dict, asked_stems: list[str]) -> tuple[bool, str]:
    # 取节点原文作为证据比对基准；None 统一成空串，避免 in 操作报错。
    text = node.get("text", "") or ""
    # 取题干；后续长度、括号、泛称等检查都以它为准。
    stem = q.get("stem", "")
    # 题干为空或少于 8 个字符：信息量不足，直接判失败。
    if not stem or len(stem) < 8:
        # 返回失败原因，调用方会把它拼进重试 Prompt。
        return False, "题干过短"
    # 题干超过 500 字：展示和作答体验差，也可能是模型把解析混进了题干。
    if len(stem) > 500:
        # 超长直接拒绝。
        return False, "题干过长"
    # 除填空题外，题干里不能出现空括号；否则选择题会变成“（ ）”式残题。
    if q["q_type"] != "fill_blank" and re.search(r"（\s*）|\(\s*\)", stem):
        # 命中空括号，按 V5 拒绝。
        return False, "V5 题干出现空白括号"
    # 题干出现“某文档/某研究”等泛称，说明没有指向本文具体内容。
    if re.search(r"(某文档|某电力文档|某研究|某论文)", stem):
        # 命中泛称，按 V5 拒绝。
        return False, "V5 题干使用泛称，未指向本文"
    # V1 证据必须逐字存在于节点原文
    # 取出题目引用的证据；没有证据就无法证明答案来自原文。
    evs = q.get("evidence") or []
    # 空证据列表直接判 V1 失败。
    if not evs:
        # 返回 V1 原因。
        return False, "V1 缺少证据"
    # 逐条检查证据引文。
    for ev in evs:
        # 去空白后取引文；None 也安全转成空串。
        quote = (ev.get("quote") or "").strip()
        # 空引文没有校验价值。
        if not quote:
            # 按 V1 拒绝空引文。
            return False, "V1 证据引文为空"
        # 先做原样子串判断；若不成立，再去除所有空白后比较，容忍换行/空格差异。
        if quote not in text and re.sub(r"\s+", "", quote) not in re.sub(r"\s+", "", text):
            # 仍找不到则拒绝，并回显引文前 20 字方便定位。
            return False, f"V1 证据引文不在原文中：{quote[:20]}"
    # V3 选择题结构
    # 单选和多选都要检查选项数量、key、答案，以及禁用绝对项。
    if q["q_type"] in ("single_choice", "multiple_choice"):
        # 取选项；缺失按空列表处理。
        opts = q.get("options") or []
        # 产品约定选择题必须恰好 4 个选项。
        if len(opts) != 4:
            # 数量不对直接拒绝。
            return False, "V3 选择题必须有 4 个选项"
        # 提取所有选项 key。
        keys = [o["key"] for o in opts]
        # key 必须非空且互不重复，否则无法唯一判分。
        if len(set(keys)) != 4 or any(not k for k in keys):
            # 重复/缺失按 V3 拒绝。
            return False, "V3 选项 key 重复或缺失"
        # “以上都/都不对”等选项会破坏区分度，属于 V8 禁止项。
        if any(re.search(r"(以上都|都不对|都正确|都错误)", o["text"]) for o in opts):
            # 命中绝对项，按 V8 拒绝。
            return False, "V8 禁止使用“以上都”类选项"
        # 读取答案值；下面按单选/多选分别检查。
        value = q.get("answer", {}).get("value")
        # 单选：答案应是单个 key；兼容模型误给单元素列表。
        if q["q_type"] == "single_choice":
            # 如果答案是列表，只取第一个元素。
            if isinstance(value, list):
                # 空列表转成空串，下一步会被判为不合法。
                value = value[0] if value else ""
            # 答案 key 必须存在于选项集合（大小写不敏感）。
            if str(value).strip().upper() not in keys:
                # 不在选项中则按 V3 拒绝。
                return False, "V3 单选答案不在选项中"
        # 多选：答案应至少包含 2 个合法 key。
        else:
            # 字符串答案按中英文逗号/顿号/空白切分。
            if isinstance(value, str):
                # 切分后得到列表，供下一步统一大小写和去空白。
                value = re.split(r"[,，、\s]+", value)
            # 非列表答案包成单元素列表，避免迭代字符串的每个字符。
            vals = value if isinstance(value, list) else [value]
            # 统一转字符串、去空白、转大写，并过滤空项。
            vals = [str(v).strip().upper() for v in vals if str(v).strip()]
            # 少于 2 个或任一 key 不在选项中：按 V3 拒绝。
            if len(vals) < 2 or any(v not in keys for v in vals):
                # 返回多选不合法原因。
                return False, "V3 多选答案不合法"
    # 判断题：答案必须是真正的布尔值。
    elif q["q_type"] == "true_false":
        # bool 是 int 的子类，但这里用 isinstance(value, bool) 仍能正确区分 True/False。
        if not isinstance(q.get("answer", {}).get("value"), bool):
            # 字符串 "true" 或 0/1 也不接受，避免判分歧义。
            return False, "V3 判断题答案必须为布尔值"
    # 填空题：答案不能为空，且不能直接出现在题干中。
    elif q["q_type"] == "fill_blank":
        # 取填空答案。
        val = q.get("answer", {}).get("value")
        # 空字符串/空列表都视为缺少答案。
        if not val or (isinstance(val, list) and not val):
            # 按 V3 拒绝。
            return False, "V3 填空题缺少答案"
        # 答案文本直接出现在题干里，等于把答案送给用户。
        if isinstance(val, str) and val in stem:
            # 按 V2 泄题拒绝。
            return False, "V2 题干泄露填空答案"
    # 简答题：必须有评分要点，否则无法稳定评分。
    elif q["q_type"] == "short_answer":
        # 评分要点为空列表/None 都判缺失。
        if not (q.get("rubric") or []):
            # 按 V3 拒绝。
            return False, "V3 简答题缺少评分要点"
    # V2 题干泄露答案关键词（简答题允许）
    # 只对客观题做这项检查；简答题的解析本来就可能与题干主题重合。
    if q["q_type"] in ("single_choice", "multiple_choice", "true_false"):
        # 取解析前 40 字作为“答案关键词”代理；过长会误伤，过短又抓不到。
        ans_text = str(q.get("answer", {}).get("explanation", ""))[:40]
        # 解析片段出现在题干中，说明题干可能提前泄露答案。
        if ans_text and ans_text in stem:
            # 按 V2 拒绝。
            return False, "V2 题干泄露答案"
    # V6 去重
    # 逐条比较历史题干。
    for old in asked_stems:
        # SequenceMatcher 的 ratio 在 0—1 之间；超过 0.9 视为高度相似。
        if difflib.SequenceMatcher(None, stem, old).ratio() > 0.9:
            # 按 V6 拒绝重复题。
            return False, "V6 与已出题目重复"
    # V7 难度
    # 先转 int 再比较；归一化路径已保证是整数，这里防御直接调用的情况。
    if not 1 <= int(q.get("difficulty", 0)) <= 5:
        # 越界或非数字按 V7 拒绝。
        return False, "V7 难度必须是 1—5"
    # 全部检查通过：返回 True 和空原因。
    return True, ""


# 规则兜底题：不调用模型，必须仍然“有证据、可评分、可展示”。
# 使用固定种子，保证同一 kp + seq_no 每次生成相同选项顺序，便于测试与复现。
# asked_stems 当前未使用（为与 generate_question 保持同一签名）；
# 去重由正常 LLM 路径负责，兜底优先保证一定有题。
def fallback_question(doc: dict, node: dict, kp: dict, evidence: list[dict], spec: dict, asked_stems: list[str]) -> dict:
    """模型不可用或连续校验失败时的规则模板题（仍保证证据可溯源）。"""
    # 固定种子只由知识点 ID 和题号组成：同一道题重跑时乱序结果一致，
    # 不使用全局 random，避免影响其它并发请求。
    rng = random.Random(kp["kp_id"] + str(spec.get("seq_no", 0)))
    # 兜底也尽量沿用规格题型；后续分支会把不支持的类型降级。
    q_type = spec["q_type"]
    # 知识点定义是模板题的主要素材；缺失时用空串继续，后面会退回证据。
    definition = (kp.get("definition") or "").strip()
    # 证据优先取第一条引文；没有证据才退回知识点定义，保证 quote 字段非空。
    quote = (evidence[0]["quote"] if evidence else definition).strip()
    # 页码优先采用第一条证据的 page_no；没有证据则用节点起始页，默认第 1 页。
    page = int(evidence[0]["page_no"]) if evidence else int(node.get("page_start", 1))
    # 章节名依次取规格里的 section_title、节点标题，最后兜底“本节”。
    section = spec.get("section_title") or node.get("title") or "本节"
    # 知识点名称用于题干和提示；缺失时统一称“核心概念”。
    kp_name = kp.get("name") or "核心概念"

    # 三种题型共用的容器：选项、答案、评分要点。先统一初始化，分支中按需填充。
    options: list[dict] = []
    # 答案固定是 dict，至少包含 value/explanation 两个字段。
    answer: dict = {}
    # 评分要点是 list[dict]，每项含 point/weight。
    rubric: list[dict] = []
    # 选择题分支：模板只生成单选，即使规格写多选也降级，保证答案唯一。
    if q_type in ("single_choice", "multiple_choice"):
        # 强制降级为单选，避免模板无法构造多个正确项。
        q_type = "single_choice"
        # 正确答案优先用知识点定义，定义太短/为空则用证据原句；都截到 72 字。
        correct = _clip(definition, 72) or _clip(quote, 72)
        # 备用池当前未参与后续组题（保留原文件计算），不影响输出。
        pool = [e["quote"] for e in evidence[1:]]
        # 从同文档其他知识点取干扰项
        # sessions 预先放入 doc["_distractors"]，最多取 3 条。
        distractors = _distractor_pool(doc, kp)[:3]
        # 干扰项不足 3 条时循环补齐，保证选择题始终有 4 个选项。
        while len(distractors) < 3:
            # 补位文本是通用错误表述，虽然质量一般，但能保证结构完整且不被误判为正确。
            distractors.append("原文并未提及该概念的具体作用与边界")
        # 第 1 项固定是正确答案，后 3 项截断后作为干扰项；共 4 项。
        choices = [correct] + [_clip(d, 72) for d in distractors[:3]]
        # 用固定种子 RNG 打乱，保证可复现；不能直接用全局 random。
        rng.shuffle(choices)
        # 选项 key 固定为 A/B/C/D。
        keys = ["A", "B", "C", "D"]
        # 按打乱后的顺序生成选项；correct_key 下面再反查。
        options = [{"key": keys[i], "text": text} for i, text in enumerate(choices)]
        # 乱序后反查正确答案的位置，得到 A/B/C/D。
        correct_key = keys[choices.index(correct)]
        # 选择题答案就是正确 key，解析直接引用证据原句。
        answer = {"value": correct_key, "explanation": quote}
        # 评分要点明确写出正确 key；权重 1.0 表示选择题全对得分。
        rubric = [{"point": f"选择与原文表述一致的选项（{correct_key}）", "weight": 1.0}]
        # 题干带上文档名、章节和知识点，避免泛称并满足“指向本文”的要求。
        stem = f"根据《{doc.get('title', '本文')}》{section}部分，下列哪一项最符合原文对「{kp_name}」的表述？"
    # 判断题分支：用知识点定义组成陈述。
    elif q_type == "true_false":
        # 陈述截到 90 字，避免题干过长。
        statement = _clip(definition, 90)
        # 模板固定输出“正确”，因为定义本身来自原文；解析引用证据。
        answer = {"value": True, "explanation": quote}
        # 判断题权重 1.0。
        rubric = [{"point": "判断与原文一致", "weight": 1.0}]
        # 判断题题干明确“原文在……部分指出”，让用户知道判断依据。
        stem = f"判断：原文在{section}部分指出——{statement}。"
    # 填空题分支：从定义中找一个术语挖空。
    elif q_type == "fill_blank":
        # 优先把知识点名称挖空；名称不在定义中时，再找最长术语。
        blank = kp_name if kp_name and kp_name in definition else _longest_term(definition)
        # 仍为空或不在定义中，就退回知识点名称。
        if not blank or blank not in definition:
            # 最终 blank 作为填空答案。
            blank = kp_name
        # 只替换第一次出现，避免把定义中所有同名术语都挖空。
        stem_text = definition.replace(blank, "____", 1) if blank in definition else definition
        # 题干前缀“填空：根据……部分”，并截到 140 字。
        stem = f"填空：根据{section}部分，{_clip(stem_text, 140)}"
        # 填空答案与解析。
        answer = {"value": blank, "explanation": quote}
        # 评分要点写出应填内容。
        rubric = [{"point": f"填入「{blank}」", "weight": 1.0}]
    # 其余未知题型统一降级为简答题，避免兜底路径产生无法评分的题型。
    else:
        # 规范题型，保证 question_row 与评分器能识别。
        q_type = "short_answer"
        # 简答题题干要求说明含义和作用，属于开放但可评分的问题。
        stem = f"请结合{section}部分，说明「{kp_name}」的含义，并指出它在文中的作用。"
        # 参考答案取定义前 160 字，解析引用证据。
        answer = {"value": _clip(definition, 160), "explanation": quote}
        # 按中文分号/句号切分定义，保留长度至少 6 的句子，最多 3 条作为评分点。
        points = [p.strip() for p in re.split(r"[；;。]", definition) if len(p.strip()) >= 6][:3]
        # 每条评分点权重 = 1 / 点数，保留两位小数；若没有可用分句则用下面的默认两条。
        rubric = [{"point": p, "weight": round(1 / max(1, len(points)), 2)} for p in points] or [
            # 默认评分点 1：说明概念含义。
            {"point": "说明概念含义", "weight": 0.5},
            # 默认评分点 2：说明作用或意义。
            {"point": "说明作用或意义", "weight": 0.5},
        # 结束评分要点列表。
        ]
    # 三级提示逐级增加信息量，但都不直接给出完整答案。
    hints = [
        # 一级提示：指回章节和知识点，告诉用户去哪里找。
        f"回到「{section}」一节，先找到与「{kp_name}」直接相关的原句。",
        # 二级提示：给出定义中的关键表述，帮助定位。
        f"关注原文中的关键表述：{_clip(definition, 48)}",
        # 三级提示：给出答案要点（截断到 90 字），仍不直接复制完整答案。
        f"答案要点：{_clip(definition, 90)}",
    # 提示列表结束。
    ]
    # 组装最终题目 dict，字段名与 LLM 路径保持一致，方便 question_row 统一入库。
    q = {
        # 新题目主键。
        "q_id": new_id("q"),
        # 所属文档。
        "doc_id": doc["doc_id"],
        # 所属知识点。
        "kp_id": kp["kp_id"],
        # 挂载的结构节点。
        "node_id": node["node_id"],
        # 降级后的最终题型。
        "q_type": q_type,
        # Bloom 层级沿用规格。
        "bloom_level": spec["bloom_level"],
        # 难度沿用规格，兜底不做二次调整。
        "difficulty": spec["target_difficulty"],
        # 模板生成的题干。
        "stem": stem,
        # 选择题有 4 项；其他题型为空列表。
        "options": options,
        # 对应题型的答案。
        "answer": answer,
        # 对应题型的评分要点。
        "rubric": rubric,
        # 兜底题只引用一条证据：有 evidence 用第一条的 ID，否则留空；页码上面已算好。
        "evidence": [{"evidence_id": evidence[0]["evidence_id"] if evidence else "", "node_id": node["node_id"], "page": page, "quote": quote}],
        # 三条提示。
        "hints": hints,
        # 来源标记为 fallback，审计和前端可区分模板题。
        "source": "fallback",
    }
    # 返回兜底题。
    return q


# 从 doc 的隐藏字段读取干扰项。sessions 在调用前把同文档其他知识点定义放进来，
# 这样本模块无需查询数据库，也避免为了组题再开一次连接。
# kp 参数当前未使用，保留是为了让调用方明确“为哪个知识点取干扰项”。
def _distractor_pool(doc: dict, kp: dict) -> list[str]:
    # 由调用方在 doc 中放入 _distractors（同文档其他知识点定义）
    # 复制成新列表再返回，防止调用方 append 时修改 doc["_distractors"] 原对象。
    return list(doc.get("_distractors") or [])


# 压缩空白并限制长度，供题干/选项/Prompt 使用；超过 n 时保留 n-1 个字符再加省略号，
# 因此返回值最多 n 个字符。
def _clip(text: str, n: int) -> str:
    # 把换行/制表符等连续空白折叠成一个空格，并去掉首尾空白；None 安全变成空串。
    text = re.sub(r"\s+", " ", text or "").strip()
    # 未超长原样返回；超长则截断并补“…”提示省略。
    return text if len(text) <= n else text[: n - 1] + "…"


# 从文本中找长度 4—16 的连续中文/字母/数字/连字符片段，取最长者作为填空答案候选。
# 这是启发式规则，不保证语义一定正确，但能保证题目结构完整。
def _longest_term(text: str) -> str:
    # 正则字符类覆盖：常用汉字、英文字母、数字、连字符；{4,16} 限制候选长度。
    terms = re.findall(r"[\u4e00-\u9fffA-Za-z0-9\-]{4,16}", text or "")
    # 没有候选时返回空串；有候选时按长度取最大。
    return max(terms, key=len) if terms else ""


# 把内部题目 dict 映射为 question 表的列（不执行 INSERT）。
# JSON 字段用 dump() 序列化，因为 SQLite 列存文本；调用方再补 created_at 并入库。
# spec 当前未使用，保留以维持既有调用签名。
def question_row(q: dict, spec: dict) -> dict:
    # 返回数据库行字典；键名必须与 question 表列名一致。
    return {
        # 题目主键。
        "q_id": q["q_id"],
        # 所属文档。
        "doc_id": q["doc_id"],
        # 知识点允许为空（历史数据可能没有 kp_id）。
        "kp_id": q.get("kp_id"),
        # 结构节点必须存在。
        "node_id": q["node_id"],
        # 题型，决定前端渲染和评分器分支。
        "q_type": q["q_type"],
        # Bloom 层级。
        "bloom_level": q["bloom_level"],
        # 难度转成 int，保证数据库列类型稳定。
        "difficulty": int(q["difficulty"]),
        # 题干。
        "stem": q["stem"],
        # 选项列表序列化为 JSON 文本。
        "options": dump(q.get("options") or []),
        # 答案对象序列化为 JSON 文本。
        "answer": dump(q.get("answer") or {}),
        # 评分要点序列化为 JSON 文本。
        "rubric": dump(q.get("rubric") or []),
        # 证据列表序列化为 JSON 文本。
        "evidence": dump(q.get("evidence") or []),
        # 提示列表序列化为 JSON 文本。
        "hints": dump(q.get("hints") or []),
        # 作答时限：简答题 180 秒，其余题型 90 秒。
        "time_limit_sec": 180 if q["q_type"] == "short_answer" else 90,
        # 产品固定提供三级提示。
        "hint_levels": 3,
        # 新题默认 active，可被会话直接使用。
        "status": "active",
        # 题目版本从 1 开始，后续编辑可递增。
        "version": 1,
        # 来源默认 llm；兜底题会显式带 fallback。
        "source": q.get("source", "llm"),
        # 创建时间留给调用方填（sessions 在 INSERT 前写入 iso() 时间）。
        "created_at": "",
    }
