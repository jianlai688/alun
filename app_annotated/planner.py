# =============================================================================
# 【模块导读】app/planner.py —— 提问规划器（组卷规划：结构配额 + 加权采样）
# =============================================================================
# 一、这个文件负责什么
#   输入：一篇文档的结构树 nodes、知识点列表 kps、以及“知识点 → 证据”的映射
#        evidence_by_kp；输出：一份“题目规格清单”plan（纯 dict，可直接 JSON 序列化）。
#   它只做“规划”，不调用大模型、不读写数据库、不生成题干。具体职责：
#   1) 用 ROLE_PATTERNS 把章节标题识别成 8 类“结构角色”（摘要/关键词/引言/相关工作/
#      方法/实验/结论/参考文献），未命中的标题落到 "other" 兜底；
#   2) 用 ROLE_QUOTA 按模式（guided 引导练习 / exam 考试 / defense 答辩）分配
#      “每种角色出几题”的结构配额，保证题卷覆盖论文骨架，而不是堆在某一章；
#   3) 用 weighted_pick() 做“重要性 + 本节均衡 + 难度匹配 + 类型多样性 + 证据强度”
#      五维加权打分，再对 top-10 做 softmax 轮盘赌采样：既偏向优质知识点，又保留随机性；
#   4) 配额出不满时从全池补齐（同一节最多 2 题），最后按章节顺序排序并重排题号；
#   5) 汇总 by_bloom（Bloom 认知层级分布）与 by_section（每节题数），供前端/报表展示。
#
# 二、它在整个应用中的位置（调用链）
#   HTTP 路由（main.py：POST /sessions 等）
#        │
#        ▼
#   sessions.py: create_session()
#        ├─ 用 SQL 读出 node / knowledge_point / evidence 三张表
#        ├─ 把 evidence 按 kp_id 归并成 evidence_by_kp
#        └─ 调用 planner.build_plan(...)   ←【本文件】
#              │  返回 plan（纯 dict）
#              ▼
#        session.plan 字段（TEXT，JSON）落库
#              │
#              ▼
#   generator.py: build_prompt() / generate_question() 按 plan["items"][i] 逐题生成题干
#        └─ 再把 spec["q_type"] / ["bloom_level"] / ["target_difficulty"] 写入 question 表
#   judge.py、reports.py 读取题目与计划做判分、统计。
#   依赖方向：planner.py 只依赖标准库（math/random/re/collections）和 app.db.load_json，
#   不反向导入 sessions/generator，属于“纯函数式领域服务”，非常容易单元测试。
#
# 三、核心算法：一次 build_plan 的五步
#   ① 过滤：只保留“有证据 + 未被排除 + 在 focus 范围内”的知识点；
#   ② 分组：按 role_of(node) 把知识点分到各角色桶，并剔除参考文献；
#   ③ 配额：按 mode 取 ROLE_QUOTA，逐角色调用 weighted_pick() 抽题；
#   ④ 补齐：不足 target_total 时从全池继续抽（同节 ≤2 题、同知识点不重复）；
#   ⑤ 排序/统计：按 section_order + seq_no 排序，重排题号，汇总 by_bloom/by_section。
#
# 四、关键数据结构（读代码时对照看）
#   node（章节节点）：node_id、parent_id、node_type（section/abstract/table/chart…）、
#       title、order_index（同级顺序）、level（层级深度）。
#   kp（知识点）：kp_id、node_id、name、kp_type（concept/method/data/conclusion/limitation）、
#       importance（0~1，越大越值得考）、difficulty（1~5 的整数）。
#   evidence（证据）：evidence_id、node_id、quote、strength（strong/weak）。
#   plan item（本文件输出的一题规格）：
#       seq_no / kp_id / kp_name / node_id / section_title / section_order /
#       section_path（根→叶的标题链）/ q_type / bloom_level / target_difficulty。
#
# 五、贯穿全文的约定与“坑”
#   - 随机性：所有随机都走 rng = random.Random(seed)，seed 由 sessions.py 传入
#     random.randint(1, 1_000_000)。同一 seed 可复现同一份题卷，便于排查和回放。
#   - 无业务异常：本文件不 raise；没有可用知识点时返回 total=0 + message，
#     由 sessions.py 判断 total<5 后抛 422 QUESTION_POOL_INSUFFICIENT。
#   - 无 IO：不查库、不调模型、不写文件；虽然 import 了 load_json，但当前未使用（历史遗留）。
#   - 同步：全部是同步纯函数，没有 async/await、没有线程与锁，可在任意线程调用。
#   - 权限：不做登录/租户/文档权限校验，调用方必须先完成鉴权（security.py + 路由层）。
#   - 配额不是硬上限：quota 只决定“优先抽多少”，最终题量由 target_total（夹在 5~30）封顶；
#     某角色桶抽空会 break，缺口由“全池补齐”阶段填上。
#   - difficulty_target() 的入参其实来自 difficulty（easy/medium/hard/auto），不是会话模式；
#     传 "auto" 时走默认值 3。MODE_TARGET 常量目前未被任何代码使用。
#   - true_false 题型：QTYPE_GROUPS 里有，但 QTYPE_BY_ROLE 的候选列表里都没有，
#     只有“角色候选被用户筛选全部过滤光”时才会从全局候选里兜底选中。
#
# 六、建议阅读顺序
#   ROLE_PATTERNS → role_of() → ROLE_QUOTA / QTYPE_BY_ROLE / BLOOM_BY_ROLE →
#   build_plan()（重点看内嵌函数 weighted_pick）→ _make_item() → _path()。
#
# 七、关于本注释版
#   仅新增中文 # 注释，未改动任何可执行语句、字符串、缩进、导入与原有空行；
#   多行表达式/长字典字面量无法安全插行尾注释的，统一在结构上方整块说明。
# =============================================================================
"""提问规划器：结构配额 + 加权采样（设计文档 6.3、6.4）。"""
# from __future__ 必须是模块 docstring 之后的第一条语句：它让所有类型注解延迟求值，
# 这样 list[dict]、set[str] | None 这类写法在 Python 3.8/3.9 上也能正常导入。
from __future__ import annotations

# math：weighted_pick() 里的 softmax 采样要用 math.exp() 计算 e 的幂。
import math
# random：可复现的伪随机数；用 random.Random(seed) 独立实例而不是全局 random，
# 避免并发请求互相“抢”随机数，保证同一 seed 必然得到同一份题卷。
import random
# re：role_of() 用正则匹配中英文标题关键词；re.I 让 Abstract/ABSTRACT 也能命中。
import re
# defaultdict：分组 grouped、各类计数器都用它，访问不存在的 key 时自动给默认值，
# 省掉大量 "if key not in d" 的判断，代码更短。
from collections import defaultdict

# load_json：db.py 提供的“JSON 文本 → Python 对象”工具（带默认值兜底）。
# 【注意】本文件当前并未调用它（历史遗留/预留扩展）；保留导入是为了不改变原文件结构。
from .db import load_json

# 结构角色识别规则表：[(角色名, 正则表达式), ...]
# 匹配对象是章节标题 node["title"]，按列表顺序“先匹配先返回”，所以顺序 = 优先级：
# 例如“实验设计与结果分析”会先命中 experiment 而不是 method。
ROLE_PATTERNS = [
    ("abstract", r"摘要|abstract"),  # 摘要：单独成节时优先识别
    ("keywords", r"关键词|关键字|keywords"),  # 关键词：通常只有一两行，出记忆/理解题
    ("intro", r"引言|前言|绪论|导论"),  # 引言/绪论：背景与研究问题
    ("related", r"相关工作|文献|综述|研究现状|理论基础|研究进展"),  # 相关工作/综述
    ("method", r"方法|模型|算法|框架|技术路线|机制|架构|流程|设计"),  # 方法与技术路线（核心章节）
    ("experiment", r"实验|结果|实证|评估|数据分析|对比|消融|案例"),  # 实验与结果分析
    ("conclusion", r"结论|讨论|展望|结束语|结语|总结"),  # 结论/讨论/展望
    ("reference", r"参考文献|references"),  # 参考文献：识别出来是为了后面排除（不拿来出题）
]

# 每种模式的“结构配额”：角色 → 该角色优先出几题。
# 注意：这是“优先尝试的数量”，不是硬性上限；角色池不够时会跳过，最后由全池补齐。
ROLE_QUOTA = {
    # guided 引导练习：覆盖最全，额外给“关键词”1 题，合计 9 题（1+1+1+2+2+1+1）
    "guided": {"abstract": 1, "intro": 1, "related": 1, "method": 2, "experiment": 2, "conclusion": 1, "keywords": 1},
    # exam 考试：不考关键词，重方法与实验，合计 8 题；更适合标准化闭卷
    "exam": {"abstract": 1, "intro": 1, "related": 1, "method": 2, "experiment": 2, "conclusion": 1},
    # defense 答辩：结论多给 1 题（答辩常追问结论与局限），合计 9 题
    "defense": {"abstract": 1, "intro": 1, "related": 1, "method": 2, "experiment": 2, "conclusion": 2},
}

# 每种“结构角色”适合的题型候选列表。
# 列表顺序就是偏好顺序：_make_item() 用 index % len(qtypes) 轮转，靠前的位置更容易被选中。
QTYPE_BY_ROLE = {
    "abstract": ["single_choice", "short_answer", "fill_blank"],  # 摘要：单选/简答/填空
    "keywords": ["single_choice", "fill_blank"],  # 关键词：单选/填空（术语类）
    "intro": ["single_choice", "short_answer"],  # 引言：单选/简答
    "related": ["multiple_choice", "single_choice", "short_answer"],  # 相关工作：多选最适合比较
    "method": ["single_choice", "short_answer", "fill_blank"],  # 方法：理解与应用类题目
    "experiment": ["multiple_choice", "single_choice", "fill_blank", "short_answer"],  # 实验：多选/单选/填空/简答
    "conclusion": ["short_answer", "multiple_choice", "single_choice"],  # 结论：简答优先（评价/创造）
    "other": ["single_choice", "short_answer", "multiple_choice"],  # 兜底角色
}

# 题型“分组”：前端勾选的是粗粒度分组，规划器再展开成具体题型。
QTYPE_GROUPS = {
    "choice": ["single_choice", "multiple_choice"],  # 选择题组
    "fill_blank": ["fill_blank"],  # 填空题组
    "true_false": ["true_false"],  # 判断题组
    "short_answer": ["short_answer"],  # 简答题组
}
# 把上面所有分组摊平成一个“全题型”列表（按分组声明顺序）：
# ["single_choice", "multiple_choice", "fill_blank", "true_false", "short_answer"]
# 用途：① 用户没勾任何分组时的默认全集；② 角色候选被筛空后的兜底候选。
ALL_QTYPES = [q for items in QTYPE_GROUPS.values() for q in items]

# Bloom 认知层级（记忆→理解→应用→分析→评价→创造）的“角色 → 候选层级”映射。
# _make_item() 用 index % len(...) 轮转取用，所以列表第一项是该角色最典型的层级。
BLOOM_BY_ROLE = {
    "abstract": ["理解", "记忆"],  # 摘要：先理解，再记忆
    "keywords": ["理解", "记忆"],  # 关键词：术语理解/记忆
    "intro": ["分析", "理解"],  # 引言：分析研究背景
    "related": ["分析", "理解"],  # 相关工作：分析对比
    "method": ["理解", "应用", "分析"],  # 方法：理解→应用→分析
    "experiment": ["分析", "应用", "评价"],  # 实验：分析结果、应用方法、评价有效性
    "conclusion": ["评价", "分析", "创造"],  # 结论：评价贡献、分析局限、创造新问题
    "other": ["理解", "分析"],  # 兜底
}

# 各模式的“目标难度”常量。当前文件内没有引用它（difficulty_target 用的是自己的映射），
# 推测是早期设计或留给后续自适应难度使用；保留即可，不要误以为它生效。
MODE_TARGET = {"guided": 1, "exam": 0, "defense": 2}


# 判断一个章节节点属于哪种结构角色。
# 入参 node：数据库 node 表的一行（dict），至少包含 node_type 与 title。
# 返回：ROLE_PATTERNS 里的角色名，或 "other"。
# 注意：函数直接索引 node["node_type"]，传空 dict 会 KeyError；调用方保证传入真实节点。
def role_of(node: dict) -> str:
    # 摘要节点在解析阶段有专门的 node_type，优先级高于标题正则，先直接返回。
    if node["node_type"] == "abstract":
        return "abstract"
    # 依次尝试每条正则；title 为 None 时用 "" 兜底，避免 re.search 报错。
    for role, pattern in ROLE_PATTERNS:
        if re.search(pattern, node["title"] or "", re.I):  # re.I：忽略英文大小写
            return role  # 第一个命中的角色即最终角色（顺序即优先级）
    return "other"  # 所有规则都没命中：归入兜底角色


# 计算“目标难度”（1~5 的整数），供 weighted_pick 的难度匹配项使用。
# 入参 mode：名义上是模式，实际接收的是 difficulty 字符串（easy/medium/hard/auto）；
#      item_index：预留给“随题号递增难度”的自适应策略，当前未使用。
# 返回：easy=2、medium=3、hard=4，其他值（如 auto）默认 3。
def difficulty_target(mode: str, item_index: int) -> int:
    # 用字典做“枚举 → 数值”映射，.get 的第二个参数是默认值 3（中等难度）。
    base = {"easy": 2, "medium": 3, "hard": 4}.get(mode, 3)
    return base  # 目前难度是常数；item_index 预留给未来的阶梯式难度


# 组卷主函数：根据结构配额 + 加权采样，产出一份可落库的题目规格计划。
# 关键字参数（* 之后只能按名字传），逐个说明：
#   nodes：node 表全部章节节点（含 parent_id/order_index/title/node_type）；
#   kps：knowledge_point 表全部知识点；
#   evidence_by_kp：{kp_id: [evidence, ...]}，sessions.py 预先归并好；
#   mode：guided / exam / defense，决定配额与题型偏好；
#   question_count：期望题量，最终夹在 5~30；
#   difficulty：easy / medium / hard / auto，决定目标难度；
#   focus_sections：只考这些章节（含其直接子节），None 表示全书；
#   exclude_node_ids：排除的章节（如“致谢/附录”），None 表示不排除；
#   question_types：用户勾选的题型分组（choice/fill_blank/true_false/short_answer）；
#   seed：随机种子，固定后结果可复现。
# 返回：dict，含 requested/total/question_types/items/by_bloom/by_section/message。
def build_plan(
    *,
    nodes: list[dict],  # 章节树（扁平列表，靠 parent_id 关联）
    kps: list[dict],  # 知识点列表
    evidence_by_kp: dict[str, list[dict]],  # 知识点 → 证据列表
    mode: str = "guided",  # 组卷模式，默认引导练习
    question_count: int = 12,  # 期望题量
    difficulty: str = "auto",  # 难度偏好
    focus_sections: list[str] | None = None,  # 限定章节（node_id 列表）
    exclude_node_ids: list[str] | None = None,  # 排除章节（node_id 列表）
    question_types: list[str] | None = None,  # 题型分组筛选
    seed: int | None = None,  # 随机种子
) -> dict:
    # 独立随机源：seed=None 时用系统熵（每次不同）；测试/重放时传固定整数即可复现。
    rng = random.Random(seed)
    # 只保留合法的题型分组名（防止前端传错值），并保持用户勾选顺序。
    selected_groups = [g for g in (question_types or []) if g in QTYPE_GROUPS]
    # 把分组展开成具体题型集合；用户一个都没勾（或全非法）时视为“不限”，放开全部题型。
    allowed_qtypes = {q for g in selected_groups for q in QTYPE_GROUPS[g]} if selected_groups else set(ALL_QTYPES)
    # 建立 node_id → node 的索引：_path() 查父节点、_make_item() 查标题都靠它，避免 O(n) 扫描。
    node_by_id = {n["node_id"]: n for n in nodes}
    # 排除章节集合：用 set 做 O(1) 判断，None 时退化为空集合。
    excluded = set(exclude_node_ids or [])
    # 限定章节集合：同样转 set，方便后面判断“是否在范围内”。
    focus = set(focus_sections or [])

    # 只保留有证据的知识点
    # 三个条件：① evidence_by_kp 中该 kp 的证据列表非空（无证据的题容易胡编、无法溯源）；
    # ② 知识点所在章节没有被用户排除。focus 的过滤放在下一段单独做。
    usable = [kp for kp in kps if evidence_by_kp.get(kp["kp_id"]) and kp["node_id"] not in excluded]
    if focus:  # 只有用户指定了限定章节才需要计算 allowed
        # allowed = 选中的章节 + 它们的“直接子节”（只下钻一层，避免把整章无限展开）
        allowed = set()
        for nid in focus:
            allowed.add(nid)  # 选中的节点本身
            for n in nodes:  # 线性扫描找直接子节点；节点数是百级，成本可接受
                if n.get("parent_id") == nid:
                    allowed.add(n["node_id"])
        # 二次过滤：只保留落在 allowed 里的知识点。
        usable = [kp for kp in usable if kp["node_id"] in allowed]
    if not usable:  # 没有任何可用知识点：提前返回空计划，由上层转成 422 错误
        return {"total": 0, "items": [], "by_bloom": {}, "by_section": [], "message": "没有可用知识点"}

    # 按章节角色分组
    # role → [kp, ...]；defaultdict(list) 让 grouped[role].append 无需先判断 key 是否存在。
    grouped: dict[str, list[dict]] = defaultdict(list)
    for kp in usable:
        node = node_by_id.get(kp["node_id"])  # 找不到节点的知识点无法判断角色
        if not node:
            continue  # 防御：数据不一致（节点被删）时跳过，而不是抛 KeyError
        grouped[role_of(node)].append(kp)  # 按标题正则归类到对应角色桶
    # 其他章节并入 other
    # 实际动作：把“参考文献”桶整个删掉（reference 不参与出题）；未识别的角色在 role_of 里已是 other。
    for role in list(grouped):  # 先 list(...) 复制 key 列表：遍历时不能改 dict 大小
        if role in ("reference",):
            grouped.pop(role, None)  # pop 带默认值：桶不存在也不会报错
    # 摊平成“全池”：补齐阶段从中挑，不区分角色。
    all_pool = [kp for role in grouped for kp in grouped[role]]
    if not all_pool:  # 极端情况：所有知识点都在参考文献等被剔除的章节里
        return {"total": 0, "items": [], "by_bloom": {}, "by_section": [], "message": "没有可用知识点"}

    # 取当前模式的结构配额；dict(...) 复制一份，避免后续修改污染模块级常量。
    quota = dict(ROLE_QUOTA.get(mode, ROLE_QUOTA["guided"]))
    if mode == "exam":
        # 考试模式：确保每个角色至少 1 题（现有配额都 ≥1，因此当前是防御性写法）。
        quota = {k: max(1, v) for k, v in quota.items()}
    # 最终题量夹在 5~30：太少没有练习价值，太多会拖垮生成耗时与 token 成本。
    target_total = max(5, min(30, question_count))

    # 已选题目规格（每个元素由 _make_item 生成）
    chosen: list[dict] = []
    # 已用过的知识点 id，保证一个知识点只出一题
    used_kp: set[str] = set()
    # 每个章节已出题数，用来限制“同节最多 2 题”
    section_count: dict[str, int] = defaultdict(int)
    # 每种知识点类型已出题数，用来计算多样性权重
    type_count: dict[str, int] = defaultdict(int)
    # 每种题型已出题数；当前只写不读（预留统计），注意补齐阶段并不会更新它
    qtype_count: dict[str, int] = defaultdict(int)

    # 内嵌函数：从 pool 里按加权随机挑一个知识点；挑不到返回 None。
    # 它是闭包，直接读取外层 used_kp/section_count/type_count/evidence_by_kp/rng，
    # 但自身不修改状态（无副作用），状态更新由调用方负责。
    def weighted_pick(pool: list[dict], target_diff: int) -> dict | None:
        candidates = []  # 候选列表：[(综合得分, kp), ...]
        for kp in pool:
            if kp["kp_id"] in used_kp:
                continue  # 已出过题的知识点直接跳过
            if section_count[kp["node_id"]] >= 2:
                continue  # 同一节最多 2 题，防止整卷集中在一章
            ev = evidence_by_kp[kp["kp_id"]]  # 前面的过滤保证这里非空
            # 证据强度：只要有一条 strong 就给满分，否则 0.6（弱证据仍可用，但降权）
            strength = 1.0 if any(e["strength"] == "strong" for e in ev) else 0.6
            # 难度匹配度：与目标难度相差 0 → 1.0；相差 4 → 0.0（线性衰减，分母 4 对应 1~5 的跨度）
            diff_fit = 1 - abs(int(kp["difficulty"]) - target_diff) / 4
            # 类型多样性：同一 kp_type 前 2 题不惩罚，从第 3 题起把权重压到 0.4
            diversity = 1.0 if type_count[kp["kp_type"]] < 2 else 0.4
            # 五维加权求和，权重合计 1.0；注释里的数值是各项的理论最大贡献
            score = (
                0.30 * float(kp["importance"])  # 重要性 0.30：最看重知识点本身的价值
                + 0.20 * (1 - min(1, section_count[kp["node_id"]] / 2))  # 本节均衡 0.20：本节 0/1/2 题 → 1.0/0.5/0.0
                + 0.20 * diff_fit  # 难度匹配 0.20
                + 0.15 * diversity  # 类型多样性 0.15
                + 0.15 * strength  # 证据强度 0.15
            )
            candidates.append((score, kp))  # 记录“得分 + 知识点”
        if not candidates:
            return None  # 池子抽干（全用过/全被同节上限挡住）→ 让调用方 break
        # softmax 采样
        # 只取分数最高的 10 个：既降低计算量，也避免明显差的知识点被随机选中。
        top = sorted(candidates, key=lambda x: -x[0])[:10]  # 按得分降序取前 10
        tau = 0.35  # 温度系数：越小越“只选第一名”，越大分布越平均
        # 每个候选的权重 = exp((score - 最高分) / tau)；减去最高分是为了防止 exp 溢出。
        exps = [math.exp((score - top[0][0]) / tau) for score, _ in top]
        total = sum(exps)  # 归一化分母（轮盘赌总宽度）
        r = rng.random() * total  # 在 [0, total) 上取一个随机落点
        acc = 0  # 累加权重，用来判断落点落在哪个候选区间
        for (score, kp), w in zip(top, exps):
            acc += w  # 当前候选的区间右边界
            if acc >= r:
                return kp  # 落点进入该区间 → 选中这个知识点
        return top[-1][1]  # 浮点误差兜底：返回得分最高的那个

    # 先按角色配额出题
    # dict 保持插入顺序，所以出题顺序是 abstract→intro→related→method→experiment→conclusion(→keywords)。
    for role, count in quota.items():
        pool = grouped.get(role, [])  # 该角色没有知识点时用空列表，weighted_pick 会返回 None
        for _ in range(count):  # 该角色最多尝试 count 次
            if len(chosen) >= target_total:
                break  # 已达总量上限，提前结束
            # 目标难度：当前只由 difficulty 决定（item_index 未参与计算）
            target_diff = difficulty_target(difficulty, len(chosen))
            kp = weighted_pick(pool, target_diff)  # 在该角色桶内加权抽样
            if not kp:
                break  # 该角色池子抽干，剩余名额留给“全池补齐”
            # 生成一题规格并追加；len(chosen) 作为临时序号（排序后会重排）
            chosen.append(_make_item(kp, node_by_id, role, mode, len(chosen), target_diff, allowed_qtypes))
            used_kp.add(kp["kp_id"])  # 标记知识点已用
            section_count[kp["node_id"]] += 1  # 本节题数 +1
            type_count[kp["kp_type"]] += 1  # 该类型题数 +1
            qtype_count[chosen[-1]["q_type"]] += 1  # 该题型题数 +1（取刚追加的那题）

    # 配额不足则从全池补齐；同节最多 2 题
    # 循环条件：没到目标题量就继续；weighted_pick 会在无候选时返回 None，从而正常退出。
    while len(chosen) < target_total:
        kp = weighted_pick(all_pool, difficulty_target(difficulty, len(chosen)))
        if not kp:
            break  # 全池也没有可用的了（题量受知识点总数/同节上限限制）
        node = node_by_id.get(kp["node_id"], {})  # 取节点：理论上一定存在（分组时已过滤）
        role = role_of(node)  # 重新判断角色，决定题型与 Bloom 候选
        # 追加一题；注意这里重新调用了一次 difficulty_target，结果与上一行相同
        chosen.append(_make_item(kp, node_by_id, role, mode, len(chosen), difficulty_target(difficulty, len(chosen)), allowed_qtypes))
        used_kp.add(kp["kp_id"])  # 标记已用
        section_count[kp["node_id"]] += 1  # 本节题数 +1
        # 【注意】补齐阶段没有更新 type_count/qtype_count，与配额阶段不对称；
        # 只影响后续 diversity 权重与题型统计口径，不会造成错误。

    # 题序：按结构路径排序，摘要/引言在前，结论在后，避免同一节连出
    # section_order 是章节的 order_index；第二关键字用临时 seq_no 保证同节内顺序稳定。
    chosen.sort(key=lambda x: (x.get("section_order", 99), x["seq_no"]))
    for idx, item in enumerate(chosen, start=1):
        item["seq_no"] = idx  # 重排题号 1..N，覆盖 _make_item 里的临时序号

    # Bloom 层级 → 题数（defaultdict 让不存在的层级自动从 0 开始）
    by_bloom: dict[str, int] = defaultdict(int)
    # 章节 node_id → {node_id, title, question_count}；用 dict 去重并保持首次出现顺序
    by_section: dict[str, dict] = {}
    for item in chosen:
        by_bloom[item["bloom_level"]] += 1  # 按认知层级计数
        key = item["node_id"]  # 以章节为统计单位
        # setdefault：key 不存在时先放一个模板 dict，然后返回它；存在则直接返回旧的。
        # 这里内联一个多行字典字面量，无法安全插入行尾注释，故整体说明在此。
        by_section.setdefault(key, {
            "node_id": key,  # 章节 id
            "title": item["section_title"],  # 章节标题（首次出现时取）
            "question_count": 0,  # 计数器初值
        })["question_count"] += 1  # 该节题数 +1
    # 返回给 sessions.py 的完整计划；所有字段都必须是 JSON 可序列化类型。
    return {
        "requested": target_total,  # 夹取后的期望题量
        "total": len(chosen),  # 实际出题数；可能小于 requested（知识点不够）
        # 有效题型分组列表；用户没勾时返回四个分组的全集
        "question_types": sorted(selected_groups) if selected_groups else ["choice", "fill_blank", "true_false", "short_answer"],
        "items": chosen,  # 题目规格列表（已排序、已重编号）
        "by_bloom": dict(by_bloom),  # 转成普通 dict，避免 JSON 序列化时遇到 defaultdict
        "by_section": list(by_section.values()),  # 转成列表，前端按数组消费
        "message": "",  # 成功时为空串；失败路径在上面提前返回了具体提示
    }


# 生成“一道题的规格”（不是题干）。参数：
#   kp：选中的知识点；node_by_id：章节索引；role：结构角色；mode：组卷模式；
#   index：该题在本题卷中的临时下标（从 0 开始，决定 Bloom 与题型的轮转位置）；
#   target_diff：目标难度；allowed_qtypes：用户允许的题型集合（None = 不限）。
# 返回：可直接 JSON 序列化的 dict，字段会被 generator.py 读取。
def _make_item(kp: dict, node_by_id: dict, role: str, mode: str, index: int, target_diff: int,
               allowed_qtypes: set[str] | None = None) -> dict:
    node = node_by_id.get(kp["node_id"], {})  # 章节缺失时用空 dict，后续 .get 不会崩
    bloom_options = BLOOM_BY_ROLE.get(role, BLOOM_BY_ROLE["other"])  # 角色 → Bloom 候选
    bloom = bloom_options[index % len(bloom_options)]  # 取模轮转：同一角色多题时层级有变化
    qtypes = QTYPE_BY_ROLE.get(role, QTYPE_BY_ROLE["other"])  # 角色 → 题型候选
    # 答辩模式提高简答比例
    if mode == "defense":
        # 在候选前插两个 short_answer：列表变长后，取模更容易落到简答上
        qtypes = ["short_answer"] * 2 + qtypes
    allowed = allowed_qtypes or set(ALL_QTYPES)  # None 或空集合都视为“不限题型”
    filtered = [q for q in qtypes if q in allowed]  # 只保留用户勾选的题型
    if filtered:
        qtypes = filtered  # 正常情况：用过滤后的角色候选
    else:
        # 角色候选被用户筛光：退化为“全局候选 ∩ 允许集合”，保证一定有题型可选
        qtypes = [q for q in ALL_QTYPES if q in allowed]
    q_type = qtypes[index % len(qtypes)]  # 取模轮转型题型（列表首项优先）
    # 考试模式避免简答：主观题判分成本高、标准答案难统一，自动换成多选。
    if mode == "exam" and q_type == "short_answer" and "multiple_choice" in allowed:
        q_type = "multiple_choice"
    # 返回一题规格；字段名与 generator.py 的 spec 约定一一对应。
    return {
        "seq_no": index + 1,  # 临时题号（build_plan 排序后会重排）
        "kp_id": kp["kp_id"],  # 知识点主键，generator 用它回查定义/证据
        "kp_name": kp["name"],  # 知识点名称，写进 prompt 便于模型理解考点
        "node_id": kp["node_id"],  # 所属章节 id
        "section_title": node.get("title") or "",  # 标题兜底空串，前端可直接展示
        "section_order": node.get("order_index", 999),  # 章节顺序，用于最终排序
        "section_path": _path(node, node_by_id),  # 根→叶标题链，写进 prompt 提供上下文
        "q_type": q_type,  # 最终题型（已过用户筛选与模式修正）
        "bloom_level": bloom,  # 认知层级（记忆/理解/应用/分析/评价/创造）
        "target_difficulty": max(1, min(5, target_diff)),  # 夹到 1~5，与题目表约束一致
    }


# 计算章节的“结构路径”：从根章节一路到当前节点，返回标题列表。
# 例：["第 3 章 方法", "3.2 模型设计"]；无标题的中间节点会被跳过。
def _path(node: dict, node_by_id: dict) -> list[str]:
    parts = []  # 收集标题（从当前节点往上，顺序是反的）
    cur = node  # 游标：从当前节点开始
    seen = set()  # 已访问节点 id，防止数据异常形成环导致死循环
    while cur and cur["node_id"] not in seen:  # cur 为空/已访问过就停止
        seen.add(cur["node_id"])
        if cur.get("title"):
            parts.append(cur["title"])  # 只收集有标题的节点
        # 有 parent_id 才继续向上；父节点不存在时 get 返回 None，循环自然结束
        cur = node_by_id.get(cur.get("parent_id")) if cur.get("parent_id") else None
    return list(reversed(parts))  # 反转成“根 → 叶”的自然阅读顺序
