"""判分、反馈与 Elo 能力估计（设计文档 6.7、6.9、附录 C.4）。"""
# =============================================================================
# 【文件定位】
# 本文件是 A论后端“答题判分与能力估计”的纯业务模块，负责四件事：
#   1. 客观题判分：单选、多选、判断、填空；
#   2. 主观题判分：优先调用大模型，失败或模型不可用时退回确定性规则；
#   3. 统一反馈：分数、维度分、证据引用、是否需人工复核、覆盖/缺失要点；
#   4. 能力估计：用简化 Elo 更新整场能力 theta，并维护章节能力。
#
# 【在整个应用中的位置】
#   FastAPI 路由 / 会话服务
#     └─ app/sessions.py::submit_answer()
#          ├─ judge.judge(question, user_answer, hint_level=...)
#          │    ├─ _judge_objective()       客观题直接比对标准答案
#          │    └─ _judge_subjective()      主观题：LLM → 规则兜底
#          │         ├─ _judge_prompt()     组装评判提示词
#          │         ├─ llm.chat_json()     同步调用 OpenAI 兼容模型网关
#          │         ├─ _parse_judge()      校验/归一化模型 JSON，计算加权分
#          │         └─ _heuristic_subjective() / _empty_subjective()
#          ├─ judge.elo_update()            更新会话总能力 theta
#          └─ judge.update_chapter_ability() 更新章节能力
#   判分结果最后由 sessions.py 写入 turn.evaluation / turn.score，供报告与
#   后续出题使用；本文件只计算并返回普通 dict，不持久化、不处理 HTTP。
#
# 【核心数据】
#   question：question 表的一行（dict），关键字段有 q_type、answer、evidence、
#             rubric、stem、difficulty、node_id 等；JSON 字段可能是字符串。
#   user_answer：前端提交的答案字典，通常形如 {"type": "single_choice",
#                "value": "B"}；本模块主要读取 value。
#   answer：标准答案字典，value 是正确值，explanation 是解析。
#   evidence：证据列表，每项含 page、node_id、quote；用于反馈引用原文。
#   rubric：评分细则列表，每项含 point（要点）与 weight（权重）。
#   result：统一判分结果，字段见各 return 前注释。
#
# 【判分口径】
#   客观题：答对 100 分，答错 0 分；“知识准确性”是唯一维度。
#   主观题：要点覆盖 50% + 准确性 25% + 证据引用 15% + 表达结构 10%。
#   提示扣分：0/1/2/3 级提示分别扣 0%/10%/20%/40%，扣在最终得分上。
#   主观题正确线：score >= 80。
#
# 【异常与降级】
#   · 本文件不抛业务异常、不定义 HTTP 错误码。上层 sessions.py 负责
#     SessionError(404, "TURN_NOT_FOUND", ...) / ANSWER_INVALID(422) 等错误码；
#     judge 只保证“只要进入这里，就尽量返回一个可展示的结果”。
#   · LLM 调用、JSON 解析、审计写入中的异常都在此处被兜底；任何异常最多使
#     主观题退回 _heuristic_subjective()，不会让答题接口 500。
#   · 静默兜底的代价是可观测性较弱，排障时要结合 ai_audit、usage_log 与
#     confidence/needs_review 字段判断模型是否真正参与了判分。
#
# 【边界说明】
#   · 本文件没有 SQL：读取 JSON 用 load_json()，写入审计用 audit 层，落库在
#     sessions.py 中完成，且使用参数化 SQL。
#   · 本文件没有异步/线程代码：调用是同步的；llm.py 内部用 BoundedSemaphore
#     限制最多 3 个并发模型请求，但不会把当前请求变成协程。
#   · 本文件不做权限/租户校验：question 与 session 的可访问性由上层按
#     tenant_id 查询并校验，判分函数拿到的已是获准使用的数据。
#
# 【阅读提示】
#   · 常规语句逐行在上一行注释；多行函数调用、字典/列表字面量在整块上方
#     解释，避免把 # 插进字符串或破坏括号结构。
#   · 三引号提示词内部不能插注释，因此在其上方统一说明用途与字段契约。
#   · 本文件只增加注释，未改动任何可执行语句、字符串、签名、缩进或控制流。
# =============================================================================
# PEP 563：让类型注解延迟求值。这样 dict | None、list[dict] 等注解不会在
# 模块导入时真正计算，也兼容较老的 Python 解释器；它必须是文档字符串后的
# 第一条普通语句，注释可以放在它前面。
from __future__ import annotations

# 标准库 JSON 编解码。当前版本本文件没有直接调用 json.*：题目 JSON 由
# db.load_json() 解析、模型输出由 llm.chat_json() 解析；保留该导入是为了
# 不改变源文件的导入清单，阅读时不必误以为这里有隐藏的 JSON 逻辑。
import json
# 正则表达式：用于文本归一化、多选答案拆分、反馈中的分数格式统一，
# 以及规则兜底时提取中文/字母/数字关键词。
import re
# 计时器：只用于测量主观题 LLM 判分耗时，并写入 ai_audit.latency_ms。
import time
# Unicode 工具：normalize_text() 用 NFKC 把全角字符统一成半角，
# 避免“Ａ”和“A”、“（”和“(”因编码形式不同而误判。
import unicodedata

# 同包基础设施：
#   audit    —— 记录 AI 输出审计（模型、耗时、置信度、是否需复核）；
#   llm      —— 模型网关客户端，提供 available()/chat_json()；
#   settings —— 读取运行时可修改的配置（这里读取实际模型名）。
from . import audit, llm, settings
# 数据层 JSON 读取器：兼容 None/空串、已反序列化的 dict/list、JSON 字符串；
# 解析失败时返回调用方给的默认值，避免脏数据直接把判分流程打断。
from .db import load_json

# 主观题系统提示词：约束模型只输出 JSON，并明确评分维度、证据要求和低置信
# 规则。它是普通三引号字符串（不是 f-string），发送给模型时保持原样。
# 注意：修改这段文字会直接影响模型判分行为，属于产品/评分口径变更，不能
# 把它当成普通注释随意编辑。
JUDGE_SYSTEM = """你是 A论 的判分器。依据评分细则评判用户答案，输出维度分、总分与反馈。
只输出 JSON，不要输出 Markdown 代码块。
反馈必须包含：得分、原文依据（引句＋页码）、一条改进建议；禁止空泛评价。
评分维度：要点覆盖 50%、准确性 25%、证据引用 15%、表达结构 10%。
若证据不足以判断，confidence < 0.7，并在 feedback 中说明。"""


# -----------------------------------------------------------------------------
# 文本归一化：客观题填空、要点匹配、证据匹配都依赖同一套“宽松比较”规则，
# 集中在这里可以保证各处口径一致。
# 参数 text：任意文本；None 会被当作空串。
# 返回：去掉空白和常见中英文标点、统一全半角并转小写后的字符串。
# -----------------------------------------------------------------------------
def normalize_text(text: str) -> str:
    # 先做 NFKC 兼容分解/组合，把全角字母数字、全角括号等转成半角；
    # text or "" 兼容 None 和空值，lower() 让英文比较不区分大小写。
    text = unicodedata.normalize("NFKC", text or "").lower()
    # 删除所有空白以及中英文标点（逗号、句号、分号、冒号、顿号、问号、感叹号、
    # 引号、括号、方括号、书名号等）。这样“答案：A。”与“答案 A”会得到同样
    # 的归一化结果；字符类里的 \s 同时覆盖空格、制表符和换行。
    text = re.sub(r"[\s，。；：、,.!?！？;:'\"“”‘’（）()\[\]【】]", "", text)
    # 返回归一化文本；调用方只做相等/包含/相似度比较，不再二次处理。
    return text


# -----------------------------------------------------------------------------
# 判分总入口：根据题型选择客观题或主观题判分，再统一应用提示扣分。
# 参数：
#   question    —— question 表行（dict），至少包含 q_type/answer/evidence；
#   user_answer —— 用户答案 dict，通常包含 value；
#   hint_level  —— 仅关键字参数，表示使用了几级提示，默认 0。
# 返回：统一判分结果 dict（score/correct/dimension_scores/feedback/...）。
# 调用关系：sessions.submit_answer() 的唯一判分入口。
# -----------------------------------------------------------------------------
def judge(question: dict, user_answer: dict, *, hint_level: int = 0) -> dict:
    # 取出题型，决定后续走客观题还是主观题分支；q_type 是题目的判别字段。
    q_type = question["q_type"]
    # answer 可能是 JSON 字符串，也可能是已经解析好的 dict；默认 {} 防止空值。
    answer_key = load_json(question.get("answer"), {})
    # 证据同样可能是 JSON 文本，统一解析成 list；缺省为空列表。
    evidence = load_json(question.get("evidence"), [])
    # 评分细则解析成 list[dict]；客观题一般不用，主观题用于逐点覆盖计算。
    rubric = load_json(question.get("rubric"), [])
    # 从用户提交中取实际答案；不同题型 value 可能是字符串、布尔值或列表。
    value = user_answer.get("value")

    # 四种题型可以直接按标准答案确定对错，无需模型。
    if q_type in ("single_choice", "multiple_choice", "true_false", "fill_blank"):
        # 调用客观题判分：传入题型、标准答案、用户值和证据，返回 0/100 分结果。
        result = _judge_objective(q_type, answer_key, value, evidence)
    # 其余题型（如简答、论述、名词解释）都按主观题处理。
    else:
        # 主观题需要完整题目上下文（题干、评分细则）、用户答案和证据。
        result = _judge_subjective(question, user_answer, answer_key, rubric, evidence)
    # 提示扣分表：0 级不扣，1 级扣 10%，2 级扣 20%，3 级扣 40%。
    # 若 hint_level 是未知值（如 4），get 返回默认 0，避免异常但也不扣分。
    penalty = {0: 0, 1: 10, 2: 20, 3: 40}.get(int(hint_level or 0), 0)
    # 先把判分器给出的原始分转成 float，便于后续乘法与舍入。
    raw_score = float(result["score"])
    # 只有确实使用了提示才进入扣分与反馈改写逻辑。
    if penalty:
        # 按“原始分 × (1 - 扣减比例)”计算；max(0.0, ...) 保证不出现负分，
        # round(..., 1) 统一保留一位小数。
        result["score"] = max(0.0, round(raw_score * (1 - penalty / 100), 1))
        # 下面用正则找到反馈中第一次出现的“得分：80”或“得分：80.0”并替换。
        # 三行参数分别是：匹配模式、替换值、最多替换次数；re.sub 返回新字符串。
        # 正则 \d+(?:\.\d+)? 只匹配整数或小数，不碰后面的单位/文字。
        # 替换值由 lambda 之外的 f-string 生成；先格式化为 1 位小数，再去掉
        # 末尾多余的 0 和小数点，使 90.0 显示为 90、80.5 仍为 80.5。
        result["feedback"] = re.sub(
            r"得分[：:]\s*\d+(?:\.\d+)?",
            f"得分：{result['score']:.1f}".rstrip("0").rstrip("."),
            result["feedback"],
            count=1,
        )
        # 在反馈末尾补一句扣分说明，让用户知道分数为什么低于原始判分。
        result["feedback"] += f"（已使用 {hint_level} 级提示，按规则扣减 {penalty}%。）"
    # 未使用提示时只做一位小数归一化，不改变分数含义。
    else:
        # round 返回 float；即使原来是整数分数也会变成 80.0 这样的形式。
        result["score"] = round(raw_score, 1)
    # 把本次实际扣减的百分比写入结果，方便前端展示和审计追踪。
    result["hint_penalty"] = penalty
    # 返回统一结果；此后的调用方可以覆盖存储，但不应再改动判分口径。
    return result


# -----------------------------------------------------------------------------
# 客观题判分：所有判断都在本地完成，不调用模型，结果确定、可复现。
# 参数：
#   q_type  —— 四种客观题型之一；
#   answer  —— 标准答案 dict（value/explanation）；
#   value   —— 用户提交的原始值；
#   evidence—— 原文证据列表，用于在反馈中附上出处。
# 返回：与主观题相同的统一结果 dict，但 objective 的维度分只有“知识准确性”。
# -----------------------------------------------------------------------------
def _judge_objective(q_type: str, answer: dict, value, evidence: list[dict]) -> dict:
    # 标准答案值；选择题可能是 "A"，多选可能是 ["A", "C"]，判断可能是布尔值。
    correct_value = answer.get("value")
    # 标准解析文本；None 时用空串，后面可直接拼接。
    explanation = answer.get("explanation") or ""
    # 取第一条证据的原文；没有证据时用空串，避免下标越界。
    quote = evidence[0]["quote"] if evidence else ""
    # 取第一条证据的页码；没有证据时为 None；get 允许证据项缺少 page 字段。
    page = evidence[0].get("page") if evidence else None
    # 生成“原文（第 X 页）：引文”的引用；没有引文时保持空串，不输出空出处。
    ref = f"原文（第 {page} 页）：{quote}" if quote else ""
    # 单选题：把用户值和标准值都转成字符串、去空白、统一大写后比较，
    # 因此 "b" 与 " B " 都视为 B。
    if q_type == "single_choice":
        # 两边都做 str().strip().upper()，避免类型/大小写/空白造成误判。
        ok = str(value).strip().upper() == str(correct_value).strip().upper()
        # 客观题只有全对 100、全错 0 两档。
        score = 100 if ok else 0
        # 反馈根据对错二选一：答对给解析；答错明确给出正确答案再附解析。
        # 这里用括号包住条件表达式，跨行书写只为可读性，逻辑仍是单个赋值。
        feedback = (
            f"正确。{explanation or '与原文表述一致。'}"
            if ok
            else f"不正确。正确答案是 {correct_value}。{explanation or ''}"
        )
    # 多选题：支持前端直接传 list，也兼容 "A,C" 或 "A C" 这类字符串。
    elif q_type == "multiple_choice":
        # value 是列表就原样使用；否则按中英文逗号、顿号或空白拆分字符串。
        # str(value or "") 兼容 None；re.split 会保留空片段，下一步再过滤。
        vals = value if isinstance(value, list) else re.split(r"[,，、\s]+", str(value or ""))
        # 转成集合：自动去重、忽略空项，并统一成大写去空白，便于集合比较。
        chosen = {str(v).strip().upper() for v in vals if str(v).strip()}
        # 标准答案也可能是列表或分隔字符串，按同样规则归一化。
        raw_correct = correct_value if isinstance(correct_value, list) else re.split(r"[,，、\s]+", str(correct_value or ""))
        # 标准答案集合；后续用子集判断是否“所有选择都在正确项内”。
        correct = {str(v).strip().upper() for v in raw_correct if str(v).strip()}
        # 一个都没选：直接 0 分，避免空集被当成任何集合的子集而“蒙对”。
        if not chosen:
            # 空选择不得分。
            score = 0
            # 也不标记为正确。
            ok = False
        # 只要选了任何错误项，整题 0 分（错选不得分）。
        elif not chosen.issubset(correct):
            # 错选/多选均判 0。
            score = 0
            # 标记错误。
            ok = False
        # 选择集合与正确集合完全相等：满分。
        elif chosen == correct:
            # 完全正确得 100。
            score = 100
            # 标记正确。
            ok = True
        # 否则是“只漏选、没有错选”：按选中的正确项比例给部分分，上限 70。
        else:
            # len(chosen)/len(correct) 是覆盖率；max(1, ...) 防止标准答案为空时除零。
            score = round(len(chosen) / max(1, len(correct)) * 70, 1)
            # 部分分不算“正确”，因为 correct 表示是否完全答对。
            ok = False
        # 反馈：全对显示“完全正确”；否则列出排序后的标准答案并说明计分规则。
        feedback = (
            f"完全正确。{explanation}"
            if ok
            else f"正确答案是 {'、'.join(sorted(correct))}。漏选得部分分，错选不得分。{explanation}"
        )
    # 判断题：标准值通常是布尔值；用户值可能是布尔值或中文/英文文本。
    elif q_type == "true_false":
        # 只有字符串需要转换；布尔值直接进入下一行比较。
        if isinstance(value, str):
            # 识别常见“真”写法：true/对/正确/是/t/1；其余字符串一律视为 False。
            # 这是宽松输入约定，前端最好仍提交真正的布尔值。
            value = value.strip().lower() in ("true", "对", "正确", "是", "t", "1")
        # 都转 bool 后比较，避免 True 与 1 之类的类型差异。
        ok = bool(value) == bool(correct_value)
        # 判断正确 100，错误 0。
        score = 100 if ok else 0
        # 正确给解析；错误时优先给解析，解析为空则退回原文引句。
        feedback = f"判断正确。{explanation}" if ok else f"判断错误。原文表述为：{explanation or quote}"
    # 剩下的客观题型是填空；支持多个等价标准答案（如中英文/简称全称）。
    else:
        # 标准值若是列表就作为多个可接受答案；否则包装成单元素列表。
        answers = correct_value if isinstance(correct_value, list) else [correct_value]
        # 用户答案归一化（去标点空白、统一大小写），用于精确或模糊比较。
        norm_user = normalize_text(str(value))
        # 任意一个标准答案与用户答案归一化后完全相等，即判正确。
        ok = any(norm_user == normalize_text(str(a)) for a in answers)
        # 精确匹配得 100，否则先记 0，后续再看是否接近。
        score = 100 if ok else 0
        # 只有不精确相等时才计算相似度，省去不必要的工作。
        if not ok:
            # 延迟导入 difflib：只有填空题需要相似度算法，避免模块导入时加载。
            import difflib
            # 计算用户答案与每个标准答案的序列相似度（0—1），取最大值；
            # default=0 保证 answers 为空时 max() 不会抛 ValueError。
            best = max((difflib.SequenceMatcher(None, norm_user, normalize_text(str(a))).ratio() for a in answers), default=0)
            # 相似度达到 0.85 视为“接近但表述不准确”，给 80 分。
            if best >= 0.85:
                # 80 分但 correct 仍为 False，因为并未精确命中标准答案。
                score, ok = 80, False
                # 反馈说明接近程度，并展示第一个参考答案。
                feedback = f"与标准答案接近，但表述不够准确。参考答案：{answers[0]}。{explanation}"
            # 相似度不足 0.85：0 分，并给出参考答案与解析。
            else:
                # 明确不匹配，避免用户误以为系统没有识别答案。
                feedback = f"答案不匹配。参考答案：{answers[0]}。{explanation}"
        # 精确匹配：给正确反馈并附解析。
        else:
            # 这里不再重复标准答案，因为用户已经答对。
            feedback = f"正确。{explanation}"
    # 有原文引句时，把出处追加到反馈末尾；没有引文则保持原反馈不变。
    if ref:
        # 用换行分隔主体反馈与原文依据，前端按纯文本展示即可。
        feedback = f"{feedback}\n{ref}"
    # 统一结果结构；客观题 confidence 固定 0.99、无需人工复核。
    # 字段含义：
    #   score             0/80/100 的最终分；
    #   correct           是否完全正确；
    #   dimension_scores  只含知识准确性；
    #   feedback          给用户看的文字；
    #   evidence          最多两条证据的投影；
    #   confidence        规则判分可信度高；
    #   needs_review      客观题无需复核；
    #   covered/missing   客观题不使用评分要点，保持空列表。
    return {
        # 最终分数。
        "score": score,
        # 布尔对错标记。
        "correct": ok,
        # 客观题只有一个维度。
        "dimension_scores": {"知识准确性": score},
        # 完整反馈文本。
        "feedback": feedback,
        # 对每条证据只保留 page/node_id/quote 三个前端需要的字段，并截取前两条。
        "evidence": [{"page": e.get("page"), "node_id": e.get("node_id", ""), "quote": e.get("quote", "")} for e in evidence[:2]],
        # 规则判分的置信度固定为 0.99。
        "confidence": 0.99,
        # 无需人工复核。
        "needs_review": False,
        # 客观题没有“要点覆盖”概念。
        "covered_points": [],
        # 也没有缺失要点。
        "missing_points": [],
    }


# -----------------------------------------------------------------------------
# 主观题判分调度器：优先走 LLM（更懂语义），任何失败再走关键词规则。
# 参数：
#   question    —— 完整题目（需要 stem、rubric 等）；
#   user_answer —— 用户答案 dict；
#   answer      —— 标准答案 dict；
#   rubric      —— 评分要点列表；
#   evidence    —— 原文证据列表。
# 返回：统一判分结果 dict。
# -----------------------------------------------------------------------------
def _judge_subjective(question: dict, user_answer: dict, answer: dict, rubric: list[dict], evidence: list[dict]) -> dict:
    # 统一把答案提取成字符串并去首尾空白；None/数字等异常值也能安全转换。
    user_text = str(user_answer.get("value") or "").strip()
    # 空答案无需调用模型，直接返回 0 分与参考要点，省时省钱。
    if not user_text:
        # 空答案专用构造器，仍会返回完整统一结构。
        return _empty_subjective(question, evidence)
    # 只有模型网关已开启且配置可用时才尝试 LLM；否则直接走规则兜底。
    if llm.available():
        # 记录开始时间（秒），用于在成功调用后计算毫秒级耗时。
        started = time.time()
        # 模型调用/解析/审计都可能失败，因此整体包在 try 中；任何异常都降级。
        try:
            # 组装提示词：题目、标准答案、评分要点、最多三条证据、用户答案。
            prompt = _judge_prompt(question, user_text, answer, rubric, evidence)
            # 调用模型并直接得到解析后的 JSON（chat_json 内部还会处理 Markdown
            # 围栏和一次 JSON 修复）。参数说明：
            #   messages：system 约束输出格式与评分标准，user 提供本次材料；
            #   kind="judge_answer"：写入 usage_log/ai_audit 的调用类型；
            #   max_tokens=1200：限制输出长度，避免冗长反馈拖慢接口；
            #   temperature=0.2：低温度让判分更稳定、更少随机性。
            raw = llm.chat_json(
                [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": prompt}],
                kind="judge_answer",
                max_tokens=1200,
                temperature=0.2,
            )
            # 将模型返回的原始 JSON 校验、归一化，并重新计算加权分；
            # 返回 None 表示格式不可用，此时继续走下方的规则兜底。
            result = _parse_judge(raw, question, evidence)
            # 只有解析成功才记录审计并返回，避免把无效模型输出当成结果。
            if result:
                # 写 AI 审计：记录场景、实际模型、是否接地、耗时和关键质量字段。
                # 这里记录失败不会抛出（audit 内部自行吞异常），不影响判分返回。
                audit.record_ai_output(
                    kind="judge_answer", model=settings.get("llm.model", ""), grounded=True,
                    latency_ms=int((time.time() - started) * 1000),
                    detail={"confidence": result.get("confidence"), "needs_review": result.get("needs_review")},
                )
                # 模型判分成功，直接返回，不再执行规则兜底。
                return result
        # 捕获网络超时、网关 4xx/5xx、JSON 解析失败、字段类型错误等所有异常。
        except Exception:
            # 故意静默：判分接口必须可用，失败细节通过模型网关日志/审计排查。
            pass
    # 模型不可用、调用失败或解析失败时的确定性兜底：关键词覆盖 + 结构启发式。
    return _heuristic_subjective(question, user_text, answer, rubric, evidence)


# -----------------------------------------------------------------------------
# 构造主观题提示词。提示词是“评分契约”：既要给模型足够上下文，也要明确
# 输出 JSON 的字段，减少后处理失败。
# 参数：question/user_text/answer/rubric/evidence，含义同 _judge_subjective。
# 返回：可直接作为 user 消息发送的字符串。
# -----------------------------------------------------------------------------
def _judge_prompt(question: dict, user_text: str, answer: dict, rubric: list[dict], evidence: list[dict]) -> str:
    # 把评分细则拼成逐行文本：“- 要点（权重 0.5）”；weight 缺省为 0.5。
    # 若 rubric 为空，join 得到空串，or 后使用通用兜底说明。
    rubric_text = "\n".join(f"- {r.get('point')}（权重 {r.get('weight', 0.5)}）" for r in rubric) or "- 依据参考答案判断"
    # 只取前三条证据，避免提示词过长；每行带页码与原文引句。
    ev_text = "\n".join(f"- 第 {e.get('page')} 页：{e.get('quote')}" for e in evidence[:3])
    # f-string 大提示词：{} 中的变量会被替换；需要输出字面量花括号时写成 {{ }}。
    # 末尾【输出】里的双花括号就是为了让模型看到普通 JSON 的 { 和 }。
    # 该字符串跨越很多行，不能在内部插入 # 注释，否则会改变发给模型的文本。
    return f"""【任务】依据评分细则评判用户答案，输出维度分、总分与反馈。只输出 JSON。

【题目】
{question.get('stem')}

【标准答案与评分要点】
{answer.get('value', '')}
{rubric_text}

【证据原文】
{ev_text}

【用户答案】
{user_text}

【要求】
1. 逐条对照评分要点，对每个要点给出 status：covered（完整覆盖）/ partial（部分覆盖）/ missing（未覆盖）。
2. 不得因为表达冗长而加分；不得因为与标准答案措辞不同而扣分。
3. accuracy 表示已作答内容的正确程度（0—1）；evidence_used 表示答案是否引用了原文关键表述；structure 表示表达是否有条理（0—1）。
4. 反馈必须包含：原文依据（引句＋页码）、一条改进建议；不要在 feedback 里写总分。
5. 若证据不足以判断，confidence < 0.7，并在 feedback 中说明。

【输出】
{{"point_verdicts": [{{"point": "评分要点原文", "status": "covered|partial|missing"}}],
 "covered_points": [], "missing_points": [],
 "accuracy": 0.0, "evidence_used": true, "structure": 0.0,
 "feedback": "", "evidence": [{{"page": 0, "quote": ""}}], "confidence": 0.0}}"""


# -----------------------------------------------------------------------------
# 解析并校验模型判分 JSON，再按固定权重算出最终分。
# 参数：
#   raw      —— llm.chat_json 的返回值，理想情况是 dict；
#   question —— 用于重新读取 rubric；
#   evidence —— 原文证据，用于校验/匹配模型引用的出处。
# 返回：统一结果 dict；raw 不是 dict 或旧格式分数非法时返回 None，触发规则兜底。
# -----------------------------------------------------------------------------
def _parse_judge(raw, question: dict, evidence: list[dict]) -> dict | None:
    # 模型可能返回 list/字符串/None；只有对象才符合判分契约。
    if not isinstance(raw, dict):
        # 返回 None 是“解析失败”的信号，调用方会走启发式兜底。
        return None
    # 重新读取题目 rubric，保证该函数可独立调用；空值归一成 []。
    rubric = load_json(question.get("rubric"), []) or []
    # 提取非空要点文本，保持原顺序；str(...) 兼容数字等异常类型。
    points = [str(r.get("point") or "").strip() for r in rubric if str(r.get("point") or "").strip()]
    # 模型给的总覆盖要点文本，去空白、过滤空项，作为没有逐点 verdict 时的退化依据。
    covered_raw = [str(x).strip() for x in (raw.get("covered_points") or []) if str(x).strip()]
    # 逐点判定数组；模型未提供时用空列表。
    verdicts = raw.get("point_verdicts") or []
    # 已覆盖要点（保存原始要点文本，不是模型改写后的文本）。
    matched_points: list[str] = []
    # partial 只算半覆盖，所以用 float 累计 0.5 的倍数。
    partial_count = 0.0
    # 优先使用结构化的逐点 verdict；points 与 verdicts 都非空才进入此分支。
    if points and isinstance(verdicts, list) and verdicts:
        # 延迟导入相似度算法，只在需要做要点模糊对齐时加载。
        import difflib
        # 逐个标准要点去找最相似的模型 verdict。
        for point in points:
            # 归一化标准要点，减少标点/全半角差异。
            key = normalize_text(point)
            # 当前最佳匹配状态与相似度；初始为空/0。
            best_status, best_score = "", 0.0
            # 遍历模型返回的每个逐点判定。
            for v in verdicts:
                # 跳过非字典项，防止模型输出字符串导致 get() 报错。
                if not isinstance(v, dict):
                    # 当前项格式不对，继续看下一个。
                    continue
                # 模型给出的要点文本（去空白）。
                label = str(v.get("point") or "").strip()
                # 归一化模型要点文本。
                lk = normalize_text(label)
                # 空标签无法匹配。
                if not lk:
                    # 跳过空标签。
                    continue
                # 序列相似度作为基础分数；key/lk 都非空才计算，否则为 0。
                score = difflib.SequenceMatcher(None, key, lk).ratio() if key and lk else 0.0
                # 若一方是另一方的子串（例如模型加了前后缀），直接视为完全匹配。
                if key and (key in lk or lk in key):
                    # 子串关系比局部相似度更可靠，提升到 1.0。
                    score = 1.0
                # 只保留相似度更高的那个判定。
                if score > best_score:
                    # 同时记录状态（统一小写，兼容 COVERED 等写法）。
                    best_score, best_status = score, str(v.get("status") or "").lower()
            # 相似度至少 0.45 才认为模型在说同一个要点，避免张冠李戴。
            if best_score >= 0.45:
                # covered 计入完整覆盖。
                if best_status == "covered":
                    # 保存标准要点文本。
                    matched_points.append(point)
                # partial 只累计半个覆盖。
                elif best_status == "partial":
                    # partial_count 是浮点数，最终覆盖率为 (完整 + 0.5*partial)/总数。
                    partial_count += 1
            # 模型没给逐点 verdict 的要点按未覆盖处理
        # 覆盖率公式；points 非空由外层 if 保证，不会除零。
        coverage = (len(matched_points) + 0.5 * partial_count) / len(points)
    # 没有逐点 verdict 时，退回“模型总覆盖要点”与标准要点的文本匹配。
    else:
        # 每个标准要点尝试在模型 covered_points 中找到对应项。
        for point in points:
            # 归一化标准要点。
            key = normalize_text(point)
            # 是否命中标记。
            hit = False
            # 遍历模型声称已覆盖的要点文本。
            for c in covered_raw:
                # 归一化模型覆盖文本。
                ck = normalize_text(c)
                # 空文本不能作为命中。
                if not ck:
                    # 跳过空项。
                    continue
                # 子串包含即命中（标准要点是模型文本的一部分，或反过来）。
                if key and (key in ck or ck in key):
                    # 标记命中。
                    hit = True
                    # 已经命中，无需继续比较其他文本。
                    break
                # 只有双方都足够长（>=8 字）时才做模糊相似度，避免短词误判。
                if len(key) >= 8 and len(ck) >= 8:
                    # 延迟导入 difflib。
                    import difflib
                    # 相似度达到 0.45 视为同一要点。
                    if difflib.SequenceMatcher(None, key, ck).ratio() >= 0.45:
                        # 标记命中。
                        hit = True
                        # 跳出当前 covered_raw 循环。
                        break
            # 命中则加入已覆盖列表。
            if hit:
                # 保存标准要点原文，便于报告展示。
                matched_points.append(point)
        # 无要点时覆盖率定义为 1.0（没有可扣的要点），否则按命中比例算。
        coverage = len(matched_points) / len(points) if points else 1.0
    # 有 rubric 才按四维加权；没有 rubric 时走下面的旧格式兼容分支。
    if points:
        # accuracy 由模型给出；缺失时退回 coverage。先转 float，再夹到 [0,1]。
        try:
            # max/min 双重夹取，防止模型输出 -1 或 2 导致总分越界。
            accuracy = max(0.0, min(1.0, float(raw.get("accuracy", coverage))))
        # 模型可能给 "高"、None 等无法转 float 的值。
        except Exception:
            # 用覆盖率作为准确性兜底，至少保持 0—1 范围。
            accuracy = coverage
        # evidence_used 是布尔语义：真给 1.0，假给 0.0，参与 15% 权重。
        evidence_used = 1.0 if raw.get("evidence_used") else 0.0
        # structure 同样限制在 [0,1]；模型没给时用中性的 0.5。
        try:
            # 转 float 并夹取，防止异常值破坏 10% 维度。
            structure = max(0.0, min(1.0, float(raw.get("structure", 0.5))))
        # 非数字结构分退回 0.5。
        except Exception:
            # 0.5 表示“结构一般”，既不加分也不重罚。
            structure = 0.5
        # 总分 = 50% 覆盖 + 25% 准确 + 15% 证据 + 10% 结构，满分为 100。
        score = 50 * coverage + 25 * accuracy + 15 * evidence_used + 10 * structure
        # 四个维度分分别换算成百分制权重后的分值，保留一位小数；总和约等于总分。
        dims_num = {
            # 要点覆盖维度，满分 50。
            "要点覆盖": round(50 * coverage, 1),
            # 准确性维度，满分 25。
            "准确性": round(25 * accuracy, 1),
            # 证据引用维度，满分 15。
            "证据引用": round(15 * evidence_used, 1),
            # 表达结构维度，满分 10。
            "表达结构": round(10 * structure, 1),
        }
        # 缺失要点 = 所有标准要点中未进入 matched_points 的项（保持原顺序）。
        missing_points = [p for p in points if p not in matched_points]
        # 已覆盖要点直接复用匹配结果。
        covered_points = matched_points
    # 没有 rubric 的旧题：模型直接给总分，本函数只做范围校验。
    else:
        # 没有 rubric 时退回模型总分（兼容旧题）
        try:
            # 转 float 并夹到 [0,100]；缺失或非法由 except 处理。
            score = max(0.0, min(100.0, float(raw.get("score", 0))))
        # 非数字分数意味着模型输出不可用。
        except Exception:
            # 返回 None，让调用方使用规则兜底。
            return None
        # 兼容模型把百分制写成 0—1 小数（如 0.85）；<=1.0 时放大 100 倍。
        if score <= 1.0:
            # 0.85 -> 85 分。
            score *= 100
        # 旧题没有四维明细，返回空字典。
        dims_num = {}
        # 旧题可用模型自己的缺失要点；缺省为空列表。
        missing_points = raw.get("missing_points") or []
        # 已覆盖要点使用前面提取的 covered_raw。
        covered_points = covered_raw
    # 最终总分统一保留一位小数，避免浮点误差造成 84.999999。
    score = round(score, 1)
    # 置信度默认 0.8；模型给 0 时 `or 0.8` 会把它也当缺失，这是源码既有行为。
    confidence = float(raw.get("confidence", 0.8) or 0.8)
    # 模型返回的证据数组；缺失时用空列表。
    ev = raw.get("evidence") or []
    # 校验后的证据输出列表。
    ev_out = []
    # 逐条检查模型引用的证据，过滤空引文/非字典项。
    for e in ev:
        # 只接受含非空 quote 的字典；否则跳过。
        if isinstance(e, dict) and str(e.get("quote", "")).strip():
            # 去空白后的引文文本。
            quote = str(e.get("quote")).strip()
            # 在题目原始 evidence 中寻找同一引文：比较双方前 24 个字符，
            # 任意一方包含另一方就视为同一条，以便补齐可信的 page/node_id。
            # next(..., None) 表示找不到时返回 None，而不是抛 StopIteration。
            matched = next(
                (x for x in evidence if x.get("quote") and (x["quote"][:24] in quote or quote[:24] in x["quote"])),
                None,
            )
            # 追加一条清洗后的证据：页码优先用模型给的，缺失时用原始证据匹配到的；
            # node_id 只信任原始证据，模型不负责生成数据库节点 ID。
            ev_out.append({
                "page": e.get("page") or (matched.get("page") if matched else None),
                "node_id": (matched.get("node_id") if matched else ""),
                "quote": quote,
            })
    # 模型没给有效证据时，用题目自带的前两条证据兜底，保证反馈有出处。
    if not ev_out:
        # 与客观题相同的字段投影，截取前两条。
        ev_out = [{"page": e.get("page"), "node_id": e.get("node_id", ""), "quote": e.get("quote", "")} for e in evidence[:2]]
    # 模型反馈去首尾空白；非字符串先 str 转换。
    feedback = str(raw.get("feedback") or "").strip()
    # 空反馈会让用户困惑，给一个可执行的通用提示。
    if not feedback:
        # 引导用户回看原文并补齐关键要点。
        feedback = "请对照原文补充关键要点。"
    # 统一反馈中的分数写法
    # 匹配“得分：0.8”这类 0—1 小数；只在总分 >= 50 时改写，避免低分误判。
    m = re.search(r"得分[：:]\s*(\d+(?:\.\d+)?)", feedback)
    # m 存在、模型写的是 <=1 的小数、且加权总分已经达到 50 分以上才替换。
    if m and float(m.group(1)) <= 1.0 and score >= 50:
        # 把“得分：0.8”替换成百分制整数，如“得分：85”；group(0) 是完整匹配。
        feedback = feedback.replace(m.group(0), f"得分：{int(round(score))}")
    # 返回统一结构；字段与 _judge_objective/_heuristic_subjective 对齐。
    return {
        # 归一化后的最终分。
        "score": round(score, 1),
        # 80 分及以上视为正确。
        "correct": score >= 80,
        # 四维分；无 rubric 时为 {}。
        "dimension_scores": dims_num,
        # 模型反馈（可能经过分数写法修正）。
        "feedback": feedback,
        # 校验/兜底后的证据列表。
        "evidence": ev_out,
        # 置信度夹到 [0,1]。
        "confidence": max(0.0, min(1.0, confidence)),
        # 低置信度（<0.7）标记人工复核。
        "needs_review": confidence < 0.7,
        # 已覆盖要点。
        "covered_points": covered_points,
        # 缺失要点。
        "missing_points": missing_points,
    }


# -----------------------------------------------------------------------------
# 确定性主观题兜底：不依赖模型，用“关键词命中 + 答案长度”估算四维分。
# 适合模型不可用/超时/输出坏 JSON 时保底，但语义理解能力弱，置信度固定较低。
# 参数：同 _judge_subjective。
# 返回：统一判分结果 dict。
# -----------------------------------------------------------------------------
def _heuristic_subjective(question: dict, user_text: str, answer: dict, rubric: list[dict], evidence: list[dict]) -> dict:
    # 先归一化整段用户答案，后续用关键词包含关系判断覆盖。
    norm_user = normalize_text(user_text)
    # 评分要点：优先用 rubric；没有 rubric 时把标准答案当作唯一要点。
    # 注意这里没有过滤空要点，保持源码行为。
    points = [r.get("point", "") for r in rubric] or [answer.get("value", "")]
    # 命中的要点。
    covered = []
    # 未命中的要点。
    missing = []
    # 逐条评估每个评分要点。
    for p in points:
        # 提取长度至少 2 的连续中文/英文字母/数字片段。
        # 注意：这是一个“近似关键词”方案，不做分词，长句会成为一个长词；
        # 标点会把长句切成多个片段，因此对表达自然的答案更友好。
        terms = [t for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", p) if len(t) >= 2]
        # 该要点没有任何可用关键词（例如纯标点），跳过，不记入覆盖或缺失。
        if not terms:
            # 跳过当前要点，继续下一个。
            continue
        # 统计有多少关键词出现在归一化用户答案中（子串包含）。
        hit = sum(1 for t in terms if normalize_text(t) in norm_user)
        # 关键词命中率达到 35% 就认为该要点覆盖；阈值低是为了容忍同义改写。
        if hit / len(terms) >= 0.35:
            # 记录覆盖的原始要点文本。
            covered.append(p)
        # 未达阈值则记为缺失。
        else:
            # 记录缺失要点。
            missing.append(p)
    # 覆盖率 = 已覆盖数 / 要点总数；max(1,...) 防止空列表除零。
    coverage = len(covered) / max(1, len(points))
    # 证据引用分的初始值：没有命中证据时为 0。
    evidence_hit = 0.0
    # 只看前两条证据，控制计算量。
    for e in evidence[:2]:
        # 取证据原文；缺失时为空串。
        quote = e.get("quote", "")
        # 从引文中提取长度至少 3 的连续中文/字母/数字片段，最多取前 6 个。
        key_terms = [t for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{3,}", quote)][:6]
        # 至少有一个关键词，且 25% 的证据关键词出现在用户答案中，就算引用了证据。
        if key_terms and sum(1 for t in key_terms if normalize_text(t) in norm_user) / len(key_terms) >= 0.25:
            # 证据引用维度拿满分 1.0。
            evidence_hit = 1.0
            # 任一证据命中即可，不再检查下一条。
            break
    # 结构分只用长度粗略估算：60 字以上视为 1.0，否则 0.5。
    structure = 1.0 if len(user_text) >= 60 else 0.5
    # 总分公式与 LLM 路径一致，但“准确性”这里也用覆盖率代替；
    # 因为无模型时无法判断语义正确性，只能假设“覆盖即准确”。
    score = 50 * coverage + 25 * coverage + 15 * evidence_hit + 10 * structure
    # 反馈引用第一条证据的原文；没有证据时为空串。
    quote = evidence[0]["quote"] if evidence else ""
    # 反馈引用第一条证据的页码；没有证据时为空字符串（便于 f-string 展示）。
    page = evidence[0].get("page") if evidence else ""
    # 反馈由三个相邻字符串字面量隐式拼接而成；第一个是得分与覆盖数，
    # 第二个是原文出处，第三个是固定的改进建议，中间用 \n 控制换行。
    feedback = (
        f"得分 {round(score, 1)}。已覆盖 {len(covered)}/{len(points)} 个要点。"
        f"\n原文依据（第 {page} 页）：{quote}"
        "\n改进建议：对照评分要点逐条作答，并引用原文中的关键表述。"
    )
    # 返回与正常路径相同结构的结果，便于上层无差别渲染。
    return {
        # 规则估算分，保留一位小数。
        "score": round(score, 1),
        # 80 分及以上标为正确。
        "correct": score >= 80,
        # 四维分仍是加权后的分值，方便前端统一展示。
        "dimension_scores": {
            # 覆盖维度，满分 50。
            "要点覆盖": round(50 * coverage, 1),
            # 准确性用 coverage 近似，满分 25。
            "准确性": round(25 * coverage, 1),
            # 证据引用维度，满分 15。
            "证据引用": round(15 * evidence_hit, 1),
            # 表达结构维度，满分 10。
            "表达结构": round(10 * structure, 1),
        },
        # 用户可见反馈。
        "feedback": feedback,
        # 最多两条证据，字段统一。
        "evidence": [{"page": e.get("page"), "node_id": e.get("node_id", ""), "quote": e.get("quote", "")} for e in evidence[:2]],
        # 规则兜底置信度固定 0.65，明确低于 0.7 的复核线。
        "confidence": 0.65,
        # 强制标记人工复核，提醒这是降级结果。
        "needs_review": True,
        # 已覆盖要点。
        "covered_points": covered,
        # 缺失要点。
        "missing_points": missing,
    }


# -----------------------------------------------------------------------------
# 空主观题答案的专用结果：不调用模型，直接 0 分并展示前三个参考要点。
# 参数：question 用于读取 rubric；evidence 用于附原文。
# 返回：统一判分结果 dict。
# -----------------------------------------------------------------------------
def _empty_subjective(question: dict, evidence: list[dict]) -> dict:
    # 读取评分细则；空值或坏 JSON 归一成 []。
    rubric = load_json(question.get("rubric"), []) or []
    # 只接受字典项，取出 point 文本；保持原顺序。
    points = [r.get("point", "") for r in rubric if isinstance(r, dict)]
    # 返回固定 0 分结构；confidence 高是因为“未作答”这个事实确定，不是答案质量高。
    return {
        # 未作答得 0 分。
        "score": 0,
        # 未作答不算正确。
        "correct": False,
        # 四个维度全部 0 分。
        "dimension_scores": {"要点覆盖": 0, "准确性": 0, "证据引用": 0, "表达结构": 0},
        # 反馈说明未作答；有要点时拼接前三个，否则给出回原文复习的建议。
        "feedback": "未作答。参考要点：" + ("；".join(points[:3]) if points else "请回原文对应章节重新梳理。"),
        # 最多附两条原文证据，便于用户立刻定位阅读位置。
        "evidence": [{"page": e.get("page"), "node_id": e.get("node_id", ""), "quote": e.get("quote", "")} for e in evidence[:2]],
        # 空答案的判定非常确定，置信度 0.99。
        "confidence": 0.99,
        # 不需要人工复核。
        "needs_review": False,
        # 没有任何已覆盖要点。
        "covered_points": [],
        # 所有参考要点都算缺失。
        "missing_points": points,
    }


# -----------------------------------------------------------------------------
# 简化 Elo 能力更新：把一次答题的 0—100 分映射成胜/平/负，再更新 theta。
# 模型说明：
#   theta       —— 当前能力值，约 0—1000，初始/中心值 500；
#   difficulty  —— 题目难度 1—5，映射为 b = 300/400/500/600/700；
#   score       —— 本题得分 0—100；
#   answered_count —— 已答题数，决定学习率 K（前 5 题 32，之后 16）。
# 返回：夹在 [0,1000] 的新 theta；本函数不写数据库。
# -----------------------------------------------------------------------------
def elo_update(theta: float, difficulty: int, score: float, answered_count: int) -> float:
    # 把 1—5 的题目难度转成 Elo 难度参数 b：难度 3 对应 500，
    # 每高一级 b 加 100，表示需要更高能力才有 50% 期望胜率。
    b = 500 + (int(difficulty) - 3) * 100
    # 标准 Elo 期望胜率：1 / (1 + 10^((对手分-自己分)/400))。
    # 这里 b 是题目难度，theta 是用户能力，两者单位都是“分”。
    expected = 1 / (1 + 10 ** ((b - theta) / 400))
    # 把百分制得分映射为三档结果：>=85 记 1.0（胜），>=50 记 0.6（部分胜），
    # 其余记 0.0（负）。0.6 是自定义的“及格但未优秀”权重。
    s = 1.0 if score >= 85 else (0.6 if score >= 50 else 0.0)
    # 动态学习率：前 5 题用 32 快速收敛，之后用 16 减少波动。
    k = 32 if answered_count < 5 else 16
    # 用实际结果 s 减期望 expected 得到 surprise，再乘 K 加到旧 theta；
    # max/min 把结果限制在 0—1000，防止极端题目把能力值拉出边界。
    return max(0.0, min(1000.0, theta + k * (s - expected)))


# -----------------------------------------------------------------------------
# 章节能力更新：在整场 Elo theta 的基础上维护每个知识节点的局部能力。
# 参数：
#   chapter —— {node_id: {"theta": float, "n": int}} 的章节能力字典；
#   node_id —— 本次题目所属知识节点；
#   theta   —— 本次 update 后的会话总能力；
#   score   —— 本次得分（保留参数以兼容调用接口，本函数当前未使用）。
# 返回：新的 chapter 字典（先复制，避免原地修改调用方对象）。
# -----------------------------------------------------------------------------
def update_chapter_ability(chapter: dict, node_id: str, theta: float, score: float) -> dict:
    # 浅拷贝最外层字典：允许调用方继续持有旧对象，同时避免修改原引用。
    chapter = dict(chapter or {})
    # 取出该节点旧状态；首次出现时默认 theta=500、n=0。
    item = chapter.get(node_id) or {"theta": 500.0, "n": 0}
    # 该节点答题次数加 1；int() 兼容数据库/JSON 中存成字符串的计数。
    n = int(item.get("n", 0)) + 1
    # 旧章节能力值；缺失时用 500 作为中性起点。
    prev = float(item.get("theta", 500))
    # 新章节向总能力收缩，3 题后采用自身估计
    # alpha 是“相信本次总能力”的权重：第 1 题 1/3，第 2 题 2/3，第 3 题起 1。
    alpha = min(1.0, n / 3)
    # 先让旧章节值以 35% 步长靠近本次总能力，得到 theta_new。
    theta_new = prev + (theta - prev) * 0.35
    # 前两题再把 theta_new 与 500 按 alpha 混合（样本少时向全局中心收缩，
    # 防止一两道题就把章节能力拉得过高/过低）；第 3 题起 alpha=1，直接用 theta_new。
    chapter[node_id] = {"theta": round(theta_new if alpha >= 1 else (500 * (1 - alpha) + theta_new * alpha), 1), "n": n}
    # 返回更新后的章节字典，由 sessions.py 序列化后写回会话。
    return chapter
