# =============================================================================
# 文件名：app/reports.py
# 所属项目：alun（AI 论文学习助手后端）
#
# 一、这个文件负责什么
#   把一次学习会话（session）中产生的答题记录（turns）汇总成一份“学习报告”：
#     · 总分、题目总数、已答题数、答对数、学习时长、能力值、模式；
#     · 各章节的掌握度（0~1 的小数）并按从弱到强排序；
#     · 布鲁姆认知层级（Bloom）的题目分布与得分率；
#     · 错题清单（题干、用户答案、参考要点、原文页码、所属章节）；
#     · 优势章节 / 薄弱章节；
#     · 下一步学习建议（由本文件的 _recommend 生成）。
#
# 二、它在整个应用中的位置
#   app/db.py      提供 load_json / new_id / now 等基础设施函数；
#   app/config.py  提供 BLOOM_LEVELS 常量（认知层级的固定顺序）；
#   本文件属于“领域/服务逻辑”层：不访问网络、不渲染页面、不直接读数据库，
#   只接收上层已经查好的普通 dict 数据，返回一个可直接 JSON 序列化的 dict。
#   典型调用链：路由层（会话结束接口）→ build_report(...) → 返回报告 JSON
#   或把报告写进 reports 表（对应设计文档 9.3 节、接口 S9）。
#
# 三、主要调用关系
#   build_report(session, turns, questions, nodes)
#       ├── 内部调用 _recommend(section_mastery, wrong, nodes) 生成建议
#       └── 调用 db.load_json / db.new_id / db.now
#   _recommend(...) 以下划线开头，表示“模块内部私有函数”，只被 build_report 调用。
#
# 四、阅读提示（初学者先看这里）
#   · 所有分数都是 0~100 的百分制：turn["score"] 是“单题得分”，
#     报告 summary["score"] 是“整场平均分”，section_mastery["score"] 是“0~1 的掌握度”。
#   · turns / questions / nodes 都是普通 Python 字典（或字典列表），不是 ORM 对象，
#     所以代码里大量使用 .get("字段") 来防止键不存在时抛 KeyError。
#   · 以 "_" 开头的字段（如 "_sum"）只是计算用的临时累加器，返回前会被删除。
# =============================================================================
"""会话报告聚合（设计文档 9.3、接口 S9）。"""
# ↑ 原有的模块文档字符串：说明本模块只做“报告聚合”，对应设计文档第 9.3 节与接口 S9。
# 延迟求值类型注解（PEP 563）：开启后，函数签名里的 list[dict]、dict[str, dict]
# 在运行时不会被真正求值，只作为字符串保存，因此兼容较老的 Python 解释器，
# 也能避免“先定义后引用”的循环导入问题导致的注解报错。
from __future__ import annotations

# defaultdict：带默认值的字典。当前版本实际未使用，但保留导入是为了兼容/备选分组写法；
# 它可以在访问不存在的键时自动创建默认值，省去“先判断键是否存在”的样板代码。
from collections import defaultdict
# datetime：用于把 ISO 格式的时间字符串（如 "2026-11-06T10:00:00"）解析成时间对象，
# 两个时间对象相减即可得到 timedelta，再取 total_seconds() 就得到学习时长。
from datetime import datetime

# BLOOM_LEVELS：布鲁姆认知层级的固定顺序（例如 记忆/理解/应用/分析/评价/创造）。
# 预先用它初始化统计字典，可以保证某个层级“一道题都没考”时仍然输出 0，
# 前端画柱状图时柱子数量才是固定的。
from .config import BLOOM_LEVELS
# load_json：把数据库里存的 JSON 文本安全解析成 Python 对象（解析失败时返回默认值，不抛异常）；
# new_id：生成带前缀的唯一 ID（例如 "rpt_ab12cd34"），用于报告主键；
# now：返回当前时间的 datetime 对象，封装成函数便于测试时统一替换时间源。
from .db import load_json, new_id, now

# 题型英文枚举 → 中文显示名的映射表。
# 为什么做成模块级常量：数据库里存英文代码，而报告/前端需要中文文案；
# 集中在一处维护可以避免每个函数里重复写 if/elif，后续改文案也只改这一处。
# 说明：本文件目前没有直接引用它，但它作为“题型文案字典”供其他模块导入复用。
TYPE_LABEL = {  # 字典：键 = 数据库中的题型代码，值 = 给用户看的中文名称
    "single_choice": "单选",  # 单选题：只有一个正确选项
    "multiple_choice": "多选",  # 多选题：两个及以上正确选项
    "true_false": "判断",  # 判断题：对 / 错
    "fill_blank": "填空",  # 填空题：补全关键词或公式
    "short_answer": "简答",  # 简答题：按评分要点（rubric）人工/模型打分
}


# 构建整份学习报告的主函数（本模块唯一对外暴露的核心入口）。
# 参数说明：
#   session   —— 会话记录，必须包含 "session_id"；可选 "total_questions"（总题数）、
#                "started_at"/"finished_at"（ISO 时间字符串）、"theta_after"（能力值）、"mode"（模式）；
#   turns     —— 答题记录列表，每条含 q_id（题目 ID）、status（状态）、score（得分）、answer（答案 JSON 文本）；
#   questions —— 题目字典，键是 q_id，值含 node_id（所属知识节点）、bloom_level、q_type、stem、rubric 等；
#   nodes     —— 知识树节点字典，键是 node_id，值含 parent_id（父节点）、title（标题）。
# 返回值：一个纯 dict，键固定为 report_id/session_id/summary/section_mastery/.../generated_at，
#         可以直接 json.dumps 后返回给前端，也可以整体存入 reports 表。
def build_report(session: dict, turns: list[dict], questions: dict[str, dict], nodes: dict[str, dict]) -> dict:
    # 函数文档字符串：解释入参、出参以及“只读不写库”的约定，方便调用方查阅。
    """把一次会话的答题明细聚合成报告字典（纯函数，不写数据库）。

    参数
    ----
    session:   会话行，至少含 session_id；可选 total_questions / started_at / finished_at / theta_after / mode
    turns:     答题记录列表，每条含 q_id / status / score / answer
    questions: q_id -> 题目字典（含 node_id / bloom_level / q_type / stem / rubric / evidence / answer）
    nodes:     node_id -> 知识节点字典（含 parent_id / title）

    返回
    ----
    dict: 含总分、章节掌握度、Bloom 分布、错题、优势/薄弱项与学习建议的报告结构
    """
    # 从会话里取“总题数”，or 0 处理 None/缺省，int() 再兜底转成整数，避免后续除法拿到字符串。
    total = int(session.get("total_questions") or 0)
    # 只保留“已作答”和“已跳过”的记录：跳过的题也算“处理过”，会以 0 分参与统计。
    answered = [t for t in turns if t.get("status") in ("answered", "skipped")]
    # 累加所有已处理题目的得分；float(... or 0) 兼容 score 为 None 或字符串 "85" 的情况。
    score_sum = sum(float(t.get("score") or 0) for t in answered)
    # 整场平均分：分母用“总题数”而不是“已答题数”，这样没作答的题相当于 0 分，能真实反映完成度；
    # max(1, total) 防止总题数为 0 时出现 ZeroDivisionError；round(..., 1) 保留一位小数。
    score = round(score_sum / max(1, total), 1)
    # 统计“答对”的题数：本项目约定单题得分 >= 80 分即为掌握（阈值可按业务调整）。
    correct = sum(1 for t in answered if float(t.get("score") or 0) >= 80)
    # 取会话开始时间（ISO 字符串）；若为空说明会话还没开始或数据缺失，下面会跳过时长计算。
    started = session.get("started_at")
    # 学习时长默认 0 秒，保证异常时返回值里始终有 duration_sec 字段，前端不用判空。
    duration = 0
    # 只有拿到开始时间才尝试计算时长，避免把 None 传进 fromisoformat 导致报错。
    if started:
        # 用 try 包住：时间字符串可能格式不合法（历史脏数据），不能让整份报告因此失败。
        try:
            # 计算逻辑：结束时间 - 开始时间。
            # session.get("finished_at") 为空时（会话仍在进行）用 now() 顶替，表示“算到此刻”；
            # 两端都用 datetime.fromisoformat 解析成时间对象，相减得到 timedelta，
            # .total_seconds() 取出总秒数（浮点），int() 截断成整数秒。
            duration = int((datetime.fromisoformat(session.get("finished_at") or now().isoformat()) - datetime.fromisoformat(started)).total_seconds())
        # 捕获所有异常（解析失败、类型错误等）：时长只是锦上添花的字段，坏数据不应影响报告主体。
        except Exception:
            # 兜底为 0 秒，让后续流程继续。
            duration = 0

    # 章节掌握度：按一级章节聚合
    # 内嵌函数：从任意知识节点出发，沿 parent_id 一路向上找到“根节点”（一级章节）。
    # 为什么要向上找：题目挂在叶子节点上（如“3.2.1 卷积核”），而报告希望按大章节（如“第三章 方法”）统计。
    # 参数 node_id 是题目所属节点的 ID；返回 (根节点 ID, 根节点标题) 二元组。
    def top_section(node_id: str) -> tuple[str, str]:
        # 先按 ID 取出节点对象；取不到就是 None，后面的 while 会直接跳过。
        node = nodes.get(node_id)
        # seen 记录已经访问过的节点 ID，用于防止脏数据造成 parent 环（A→B→A）时死循环。
        seen = set()
        # 循环条件：当前节点存在、还有父节点、且当前节点没被访问过。
        while node and node.get("parent_id") and node["node_id"] not in seen:
            # 把当前节点标记为“已访问”，下一轮再遇到就说明成环了。
            seen.add(node["node_id"])
            # 取出父节点对象；父节点不在字典里（数据被删/不完整）时为 None。
            parent = nodes.get(node["parent_id"])
            # 父节点不存在就跳出循环，否则 node 会变成 None，后面的 return 会走兜底分支。
            if not parent:
                break
            # 指针上移一层，继续向根节点走。
            node = parent
        # 三元表达式：node 存在就返回它的 ID 和标题（标题为空时给“未命名章节”），
        # node 为 None（题目没有挂节点 / 节点树断裂）时返回空 ID 和“未命名章节”。
        return (node["node_id"], node.get("title") or "未命名章节") if node else ("", "未命名章节")

    # 用字典做“分组累加器”：键是章节 ID（或标题），值是该章节的统计中间态。
    # 选 dict 而不是 list，是为了用 O(1) 的 setdefault 完成“不存在就建、存在就累加”。
    sections: dict[str, dict] = {}
    # 遍历每一条已处理（作答/跳过）的记录，把分数归到对应的一级章节。
    for turn in answered:
        # 用题目 ID 反查题目信息；查不到说明题目被删除，直接跳过这条记录。
        q = questions.get(turn["q_id"])
        # 题目不存在：无法知道它属于哪个章节，放弃这条统计（但不影响总分）。
        if not q:
            continue
        # 递归向上找到该题所属的一级章节，得到 (章节ID, 章节标题)。
        nid, title = top_section(q["node_id"])
        # setdefault：键不存在时插入默认统计结构，存在时直接返回已有结构。
        # 键用 nid or title：正常情况下用节点 ID；节点缺失时退化为用标题当键，避免所有孤儿题都挤到同一个空键。
        # 默认结构字段：node_id/title 给前端展示，score 先占位 0.0，
        # question_count 计题数，_sum 是本组得分总和（临时字段，最后会参与计算）。
        item = sections.setdefault(nid or title, {"node_id": nid, "title": title, "score": 0.0, "question_count": 0, "_sum": 0.0})
        # 把本题得分累加到该章节的临时总分里。
        item["_sum"] += float(turn.get("score") or 0)
        # 该章节的题目计数 +1，后面用来算平均分。
        item["question_count"] += 1
    # 最终对外输出的章节掌握度列表（会把内部临时字段 _sum 换成平均掌握度 score）。
    section_mastery = []
    # 遍历每个章节的统计中间态，转换成干净的输出结构。
    for item in sections.values():
        # 用 append 组装一条章节记录；这里用多行字典字面量，便于逐字段加注释。
        section_mastery.append({
            "node_id": item["node_id"],  # 章节节点 ID，前端点击可跳转到知识树
            "title": item["title"],  # 章节标题，用于图表标签
            # 掌握度 = 平均分 / 100，得到 0~1 的小数（例如 85 分 → 0.85）；
            # max(1, ...) 防止题数为 0 时除零；round(..., 2) 保留两位小数。
            "score": round(item["_sum"] / max(1, item["question_count"]) / 100, 2),
            "question_count": item["question_count"],  # 该章节本轮共做了多少题
        })
    # 按掌握度升序排序：最薄弱的章节排在最前面，方便前端直接取前几个作为“薄弱项”。
    section_mastery.sort(key=lambda x: x["score"])

    # Bloom 认知层级统计：先用固定层级初始化，保证每个层级都有一行（count=0 时也要输出）。
    # 每个值包含：count 题数、rate 得分率（0~1）、_sum 得分总和（临时字段，稍后删除）。
    bloom_stats: dict[str, dict] = {b: {"count": 0, "rate": 0.0, "_sum": 0.0} for b in BLOOM_LEVELS}
    # 再遍历一遍已处理记录，按题目的 bloom_level 归类累加。
    for turn in answered:
        # 反查题目；题目已被删除则跳过。
        q = questions.get(turn["q_id"])
        # 没有题目信息就没有认知层级，跳过。
        if not q:
            continue
        # 取题目的认知层级；数据缺失时默认归到“理解”（最常见的层级）。
        b = q.get("bloom_level") or "理解"
        # setdefault 兜底：若数据库里出现了 BLOOM_LEVELS 之外的层级（如自定义层级），
        # 也要为它补一个统计桶，而不是抛 KeyError。
        bloom_stats.setdefault(b, {"count": 0, "rate": 0.0, "_sum": 0.0})
        # 该层级题数 +1。
        bloom_stats[b]["count"] += 1
        # 该层级得分总和累加（同样用 float(... or 0) 兼容 None）。
        bloom_stats[b]["_sum"] += float(turn.get("score") or 0)
    # 遍历每个层级，把“得分总和”换算成“得分率”，并清掉临时字段。
    for b, item in bloom_stats.items():
        # rate = 平均分 / 100，得到 0~1 的得分率；题数为 0 时直接给 0.0（避免除零）。
        item["rate"] = round(item["_sum"] / max(1, item["count"]) / 100, 2) if item["count"] else 0.0
        # pop 删除内部临时字段 _sum，保证输出结构干净、不把实现细节泄露给前端。
        item.pop("_sum", None)

    # 错题清单：把“没掌握”的题目整理成结构化数据，供报告页展示与复习。
    wrong = []
    # 注意这里遍历的是全部 turns（不只是 answered），这样“未作答”的记录也会被算作错题。
    for turn in turns:
        # 反查题目；查不到就跳过（没有题干/要点，展示出来也没意义）。
        q = questions.get(turn["q_id"])
        # 题目不存在则跳过。
        if not q:
            continue
        # 过滤规则：得分 >= 60 且不是“跳过”状态的，说明基本掌握，不算错题，直接进入下一条。
        # 反过来说，错题 = 得分 < 60 的题，或者被标记为 skipped 的题。
        if float(turn.get("score") or 0) >= 60 and turn.get("status") != "skipped":
            continue
        # 解析用户答案：数据库里 answer 字段存的是 JSON 文本，load_json 解析失败时返回 {}，
        # 因此后面用 ans.get("value", "") 取值，永远不会因 None 报错。
        ans = load_json(turn.get("answer"), {})
        # 解析该题的评分要点（rubric）列表；解析失败时返回空列表 []。
        rubric = load_json(q.get("rubric"), [])
        # 解析证据列表（evidence），每条通常含 page（原文页码）等定位信息，用于指引学生回看原文。
        ev = load_json(q.get("evidence"), [])
        # 组装一条错题记录；字段设计目标是“学生不用再翻题库就能复习”。
        wrong.append({
            "q_id": q["q_id"],  # 题目 ID，前端可用于跳转题目详情
            "stem": q.get("stem", ""),  # 题干文本，缺省空串
            "q_type": q.get("q_type", ""),  # 题型代码（可配合 TYPE_LABEL 转中文）
            "user_answer": ans.get("value", ""),  # 用户当时填写的答案内容
            "score": turn.get("score", 0),  # 该题得分（0~100）
            # 参考要点：优先取 rubric 里每条的 point 字段，最多前 4 条；
            # 如果 rubric 为空，则退化为“标准答案里的解析文本”（q["answer"] 的 explanation 字段），
            # 保证错题卡片至少有一条可读的参考内容。
            "reference_points": [r.get("point", "") for r in rubric][:4] or [load_json(q.get("answer"), {}).get("explanation", "")],
            # 原文页码：取第一条证据的 page；没有证据时给 None（前端显示“原文对应位置”）。
            "page": (ev[0].get("page") if ev else None),
            # 所属知识节点标题：先按 node_id 找节点，再用 {} 兜底，避免节点缺失时 KeyError。
            "node_title": nodes.get(q.get("node_id", ""), {}).get("title", ""),
        })

    # 优势章节：按掌握度从高到低排序，筛出 >= 0.8 的章节，取前 3 个标题。
    strengths = [s["title"] for s in sorted(section_mastery, key=lambda x: -x["score"]) if s["score"] >= 0.8][:3]
    # 薄弱章节：section_mastery 本身已按升序排列，因此直接筛 <= 0.6 的取前 3 个即可（最弱的在前）。
    weaknesses = [s["title"] for s in section_mastery if s["score"] <= 0.6][:3]

    # 生成学习建议：把章节掌握度、错题、知识节点传给私有函数，返回 1~3 条中文建议字符串。
    recommendations = _recommend(section_mastery, wrong, nodes)
    # 组装最终报告：所有字段都是 JSON 可序列化的基本类型（str/int/float/list/dict/None）。
    return {
        "report_id": new_id("rpt"),  # 报告唯一 ID，前缀 rpt 便于日志中辨认
        "session_id": session["session_id"],  # 所属会话 ID（必填，缺失会抛 KeyError 属于调用方错误）
        "summary": {  # 概览区：报告顶部的几个关键数字
            "score": score,  # 整场平均分（百分制，一位小数）
            "total": total,  # 会话计划总题数
            "answered": len(answered),  # 实际处理（作答 + 跳过）的题数
            "correct": correct,  # 得分 >= 80 的题数
            "duration_sec": duration,  # 学习时长（秒），异常时为 0
            "ability": round(session.get("theta_after") or 500, 1),  # 能力值（IRT theta 换算分），缺省 500
            "mode": session.get("mode"),  # 会话模式（如练习/测验），可能为 None
        },
        "section_mastery": section_mastery,  # 章节掌握度列表（升序，最弱在前）
        "bloom_distribution": bloom_stats,  # 各认知层级的题数与得分率
        "wrong_questions": wrong,  # 错题明细列表
        "strengths": strengths,  # 优势章节标题（最多 3 个）
        "weaknesses": weaknesses,  # 薄弱章节标题（最多 3 个）
        "recommendations": recommendations,  # 学习建议（最多 3 条）
        # 生成时间：ISO 8601 字符串，timespec="seconds" 表示精确到秒（如 2026-11-06T17:52:07）
        "generated_at": now().isoformat(timespec="seconds"),
    }


# 私有函数：根据“章节掌握度 + 错题 + 知识节点”生成 1~3 条可执行的学习建议。
# 之所以拆成独立函数：建议文案的编排规则较长，放在 build_report 里会让主流程难以阅读；
# 同时独立出来后也便于单元测试（给定输入断言输出条数与内容）。
# 参数：
#   section_mastery —— build_report 算好的章节列表（已按掌握度升序）；
#   wrong           —— 错题列表（含 node_title / page 字段）；
#   nodes           —— 知识节点字典（当前版本保留该参数以兼容未来按节点层级生成建议）。
# 返回：去重后的建议字符串列表，最多 3 条。
def _recommend(section_mastery: list[dict], wrong: list[dict], nodes: dict[str, dict]) -> list[str]:
    # 函数文档字符串：说明建议生成的三条规则来源（薄弱章节、错题、兜底通用建议）。
    """按“薄弱章节 → 典型错题 → 通用建议”的顺序生成学习建议，去重后最多三条。"""
    # 结果容器：先收集候选建议，最后统一去重截断。
    recs: list[str] = []
    # 找出掌握度 < 0.7 的章节，取最弱的前 2 个（列表已升序，所以直接切片即可）。
    weak = [s for s in section_mastery if s["score"] < 0.7][:2]
    # 为每个薄弱章节生成一条“复述 + 对照原文”的建议。
    for s in weak:
        # f-string 把章节标题嵌入文案；强调“先复述再对照”，符合主动回忆的学习原理。
        recs.append(f"重读「{s['title']}」一节，先用自己的话复述该节的核心概念，再对照原文检查遗漏。")
    # 再挑前 2 道错题，生成“回到原文页码、抄写评分要点”的具体动作建议。
    for w in wrong[:2]:
        # 有页码就显示“第 N 页”，没有页码则用“原文对应位置”这种模糊但可操作的表述。
        page = f"第 {w['page']} 页" if w.get("page") else "原文对应位置"
        # node_title 为空时用“相关章节”兜底，保证句子通顺。
        recs.append(f"针对「{w['node_title'] or '相关章节'}」的错题，回到{page}，把评分要点逐条抄写并标注对应原句。")
    # 兜底分支：如果既没有薄弱章节也没有错题（说明学得很好或数据太少），也要给出可执行的建议。
    if not recs:
        # 有章节数据：挑掌握度最低的一节（min 按 score 比较），做“闭卷写要点”练习。
        if section_mastery:
            # min(..., key=...) 在升序列表里同样得到最弱的一节；get("score", 0) 兼容字段缺失。
            lowest = min(section_mastery, key=lambda x: x.get("score", 0))
            # 生成针对该章节的闭卷自测建议。
            recs.append(f"挑一个本轮得分最低的小节「{lowest['title']}」，不看原文写出三条要点，再回原文核对。")
        # 完全没有章节数据（例如一道题都没做）：给一条不依赖具体章节的通用建议。
        else:
            # 通用建议：任选一个未覆盖的小节做闭卷复述。
            recs.append("挑一个尚未覆盖的小节，尝试不看原文写出三条要点，再回原文核对。")
    # 固定建议 1：引导下一轮优先练“方法/实验”章节的简答题，并给出“结论—证据—页码”的答题模板。
    recs.append("下一轮优先选「方法」与「实验」章节做简答题，用“结论—证据—页码”的格式组织答案。")
    # 固定建议 2：把错题要点整理成“要点—页码”对照表，并用隔天自测强化长期记忆。
    recs.append("把本轮错题的参考要点整理成一张对照表：左侧写要点，右侧标页码，隔天再自测一次。")
    # 去重并限制三条
    # 用列表 out 保持原有顺序去重（dict.fromkeys 也能去重，但这里显式循环更直观、便于初学者理解）。
    out = []
    # 逐条检查候选建议。
    for r in recs:
        # 只有没出现过的建议才加入，避免“薄弱章节”和“错题”生成相同文案时重复展示。
        if r not in out:
            # 追加到结果列表尾部，保持优先级顺序（先章节、再错题、最后通用）。
            out.append(r)
    # 最多返回 3 条：报告页面空间有限，建议过多反而降低执行率。
    return out[:3]
