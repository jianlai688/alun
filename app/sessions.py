"""会话编排：创建计划、逐题生成、作答判分、提示、跳过、报告。"""

#
# ============================================================================
# 【模块定位】
# 本文件是后端“练习会话”领域服务（service 层）的核心实现，串联起：
#   文档/节点/知识点/证据（只读） → 计划 planner → 出题 generator
#   → 作答判分 judge → 追问（LLM + 兜底模板） → 报告 reports
# 上层（HTTP 路由 / 前端）只与本文件暴露的函数交互，不直接拼 SQL。
#
# 【数据模型与状态机】
#   session（一次练习会话）
#     state: ready（已建计划，未出题） → waiting（有题待答 / 等待下一题） → finished（已出报告）
#   turn（会话中的“一题一轮次”，是 session 的子实体）
#     status: asking（题目已下发、等待作答） → answered（已作答）/ skipped（已跳过）
#   同一 session 同时最多只有一个 status='asking' 的 turn，
#   由 session.current_turn_id 指向它（出题时写入、作答/跳过时清空）。
#   plan（JSON，存于 session.plan）：预先排好的题目清单，
#   每项含 seq_no / node_id / kp_id / q_type / bloom_level / target_difficulty / status。
#
# 【通用约定】
#   1. 所有对外函数都接收 tenant_id，用于多租户隔离（SQL 追加 tenant_id=?）。
#   2. 对外返回“API 视图”（dict），内部使用数据库原始行（raw row），
#      通过 _raw_session / _session_api / _turn_payload / _answer_payload 显式转换，
#      避免把答案、内部 id 等敏感字段泄露给前端。
#   3. 写库统一走 db() 上下文管理器：with 块结束自动提交/回滚，
#      块内多条 SQL 构成一个事务。
#   4. 幂等：next_question / submit_answer / skip_question / _finish_internal
#      都做了“重复调用返回同一结果”的处理，防止前端重试导致重复扣分或重复建报告。
# ============================================================================
from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from datetime import timedelta

from . import generator, judge as judge_mod, llm, planner, audit
from .config import HINT_PENALTIES
from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id, now
from .reports import build_report

# ---------------------------------------------------------------------------
# 【导入说明】
#   json / re        ：JSON 序列化、清洗 LLM 输出里的 Markdown 代码围栏
#   random           ：给 planner 一个随机种子，让每次组卷不完全相同
#   timedelta        ：计算会话 24 小时有效期
#   defaultdict      ：把“知识点 → 证据列表”建成倒排索引
#   generator        ：根据知识点 + 原文证据生成题目（内部可能调用 LLM）
#   judge as judge_mod：判分、Elo 能力值更新、分章节能力更新
#   planner          ：按题型 / Bloom 层级 / 难度分布生成“出题计划”
#   audit            ：审计日志（谁在什么时候做了什么）
#   llm              ：大模型调用封装；llm.available() 判断是否配置了模型
#   config.HINT_PENALTIES：每一级提示对应的扣分比例表
#   db 模块：db() 事务、dump/load_json（JSON 列序列化/反序列化）、
#            fetch_one/fetch_all（参数化查询，天然防 SQL 注入）、
#            iso/now（时间格式化）、new_id（业务主键，如 ses_xxx / turn_xxx）
#   build_report     ：根据 turns/questions/nodes 汇总出学习报告
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 【异常类型】SessionError —— 领域异常，自带 HTTP 语义
# 设计要点：service 层不直接依赖 Web 框架，而是抛出带 status/code 的异常，
# 由路由层统一捕获转成 {"code": ..., "message": ..., "details": {...}}。
# 好处：业务逻辑可被单元测试直接断言，前端也能按 code 做分支引导。
# ---------------------------------------------------------------------------
class SessionError(Exception):
    """会话领域的业务异常。

    status  : 建议返回给客户端的 HTTP 状态码（404 / 409 / 422 等）
    code    : 机器可读错误码，前端据此分支处理
              （如 QUESTION_SET_EMPTY → 提示“请重新生成题卷”）
    message : 面向用户的中文提示
    details : 附加上下文（文档 id、当前状态等），便于排查问题
    """
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        """保存错误上下文，并让 Exception.args 与 message 保持一致（便于日志打印）。"""
        # 四个字段就是异常对象的全部契约，路由层按需读取（status 决定响应码）
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        # 调用父类构造，保证 str(e) / 异常追踪里能直接看到中文提示
        super().__init__(message)


# ---------------------------------------------------------------------------
# 【内部工具】审计日志：把业务动作写入 audit 表，供教师端 / 合规审计追溯
# ---------------------------------------------------------------------------
def _log_event(session: dict, event_type: str, detail: dict | None = None) -> None:
    """记录一次会话内的事件（开始、出题、作答、提示、跳过、追问、报告）。

    参数
    ----
    session    : session 原始行（dict），用于取出 user_id / tenant_id
    event_type : 事件类型，如 "session_start" / "answer_submitted"
    detail     : 事件的结构化细节（会被 JSON 序列化后存库）

    注意：这里刻意“反查用户名”而不让调用方传入，
    好处是调用点简单；代价是每次写日志多一次 SELECT
    （日志写入属于低频操作，这个代价可以接受）。
    """
    # 1) 查用户昵称；fetch_one 查不到时返回 None，用 `or {}` 兜底成空字典
    user = fetch_one("SELECT username FROM app_user WHERE user_id=?", (session.get("user_id"),)) or {}
    # 2) 统一写审计表：即使 user 查不到，也会记下 user_id / tenant_id，保证可追溯
    audit.log_event(
        user_id=session.get("user_id", ""), username=user.get("username", ""),
        tenant_id=session.get("tenant_id", ""), event_type=event_type, detail=detail or {},
    )


# ---------------------------------------------------------------------------
# 【对外主流程 1】创建会话
# 入口场景：用户在某个文档上点“开始练习”，或教师按题卷布置作业。
# 产出：session 行（state='ready'）+ 预先排好的出题计划 plan（JSON）。
#
# 注意：此时并不生成任何题目。题目是在 next_question() 里按计划“懒生成”的，
#       这样既能缩短“点开始”的响应时间，也能把单题生成失败的影响限制在一题内。
# ---------------------------------------------------------------------------
def create_session(
    *,
    doc_id: str,
    user_id: str,
    tenant_id: str,
    mode: str = "guided",
    question_count: int = 12,
    difficulty: str = "auto",
    focus_sections: list[str] | None = None,
    exclude_node_ids: list[str] | None = None,
    question_types: list[str] | None = None,
    question_ids: list[str] | None = None,
) -> dict:
    """创建一次练习会话。

    参数（全部为关键字参数，避免长参数列表按位置传错）
    ----
    doc_id           : 文档 id（必须属于当前租户且未被软删除）
    user_id          : 发起练习的用户 id
    tenant_id        : 租户 id，用于数据隔离
    mode             : 练习模式 guided（引导）/ exam（考试）/ defense（答辩）
    question_count   : 期望题量，默认 12；常规模式 5—30，题卷模式最少 1
    difficulty       : 难度偏好，默认 auto（交给 planner 决策）
    focus_sections   : 只在这些章节出题（None 表示不限）
    exclude_node_ids : 需要排除的节点（已掌握、或不想考的章节）
    question_types   : 允许的题型，非法值会被静默过滤
    question_ids     : 若提供，则进入“题卷模式”，严格按给定顺序组卷

    返回
    ----
    dict：session 的 API 视图（见 _session_api）

    异常
    ----
    SessionError(404, DOC_NOT_FOUND)              文档不存在 / 不属于该租户
    SessionError(409, DOC_NOT_READY)              文档还没解析完，不能出题
    SessionError(422, INVALID_MODE)               mode 取值非法
    SessionError(422, INVALID_QUESTION_COUNT)     题量越界
    SessionError(409, QUESTION_SET_EMPTY)         题卷里有失效 / 跨文档题目
    SessionError(422, QUESTION_POOL_INSUFFICIENT) 可用知识点不足以组卷
    """
    # ---- 步骤 1：校验文档 ----
    # 三个条件缺一不可：id 匹配、租户匹配（防越权）、deleted_at 为空（未被软删）
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (doc_id, tenant_id))
    # 查不到时统一报 404，避免通过错误信息探测“该 doc 是否存在”
    if not doc:
        raise SessionError(404, "DOC_NOT_FOUND", "文档不存在或无权访问")
    # ---- 步骤 2：校验文档解析状态 ----
    if doc["status"] not in ("ready", "needs_review"):
        # ready = 可出题；needs_review = 解析完成但结构待人工确认，也放行；其余状态一律拒绝
        raise SessionError(409, "DOC_NOT_READY", "文档尚未完成解析", {"document_id": doc_id, "status": doc["status"]})
    # ---- 步骤 3：校验练习模式 ----
    if mode not in ("guided", "exam", "defense"):
        raise SessionError(422, "INVALID_MODE", "mode 只能是 guided / exam / defense")
    # ---- 步骤 4：校验题量 ----
    # 题卷模式下题目是教师挑好的，允许少到 1 题；普通模式至少 5 题才构成一次有效练习。
    min_count = 1 if question_ids else 5
    # 上限统一 30 题：单次练习过长会显著增加 LLM 成本，也容易让用户疲劳
    if not min_count <= int(question_count) <= 30:
        raise SessionError(422, "INVALID_QUESTION_COUNT", "题量必须在 5—30 之间")
    # ---- 步骤 5：清洗题型 ----
    # 白名单过滤：前端可能传脏数据或旧枚举值，这里“静默丢弃”而不是报错（对用户更友好）
    allowed_types = {"choice", "fill_blank", "true_false", "short_answer"}
    question_types = [x for x in (question_types or []) if x in allowed_types]
    # 全部非法 / 压根没传 → 退化为“全部题型”，由 planner 自行配比
    if not question_types:
        question_types = list(allowed_types)

    # ---- 步骤 6：加载编排所需的原始素材 ----
    # nodes 章节节点、kps 知识点、evidences 原文证据，
    # 都按 doc 维度一次性取回，避免在循环里逐条查库（批量取数 + 内存组装）。
    nodes = fetch_all("SELECT * FROM node WHERE doc_id=? ORDER BY order_index", (doc_id,))
    kps = fetch_all("SELECT * FROM knowledge_point WHERE doc_id=? ORDER BY order_no", (doc_id,))
    evidences = fetch_all("SELECT * FROM evidence WHERE doc_id=?", (doc_id,))
    # 用 defaultdict 建“知识点 id → 证据列表”的倒排索引。
    # 复杂度 O(证据数 × 知识点数)，单文档规模下可接受；
    # 数据量大时可改成先把 kp.evidence_ids 展开成 (kp_id, evidence_id) 再 join。
    by_kp: dict[str, list[dict]] = defaultdict(list)
    for ev in evidences:
        for kp in kps:
            # 一个知识点可能引用多条证据；用 in 判断当前证据是否被该知识点引用
            if ev["evidence_id"] in load_json(kp["evidence_ids"], []):
                by_kp[kp["kp_id"]].append(ev)
    # ---- 步骤 7：生成计划（两条互斥路径）----
    if question_ids:
        # 题卷布置：严格按题卷题目顺序组卷，不再按知识点重新生成。
        # 路径 A：题卷模式 —— 教师已定好题目，这里只把它们按顺序落成计划
        # 过滤空串并统一转字符串，防止前端传数字 id 导致 SQL 参数类型不一致
        clean_ids = [str(x) for x in question_ids if str(x).strip()]
        # 动态占位符 IN (?,?,?)，参数以列表传出，杜绝 SQL 注入
        marks = ",".join("?" for _ in clean_ids)
        rows = fetch_all(f"SELECT * FROM question WHERE q_id IN ({marks})", clean_ids) if clean_ids else []
        by_id = {q["q_id"]: q for q in rows}
        # 按 clean_ids 的顺序重建列表：SQL 的 IN 不保证返回顺序，必须自己排
        picked = [by_id[qid] for qid in clean_ids if qid in by_id]
        # 一致性校验：数量必须完全一致（不一致说明有 id 查不到），
        # 且每题都必须 ① 处于 active（未下架）② 属于当前文档（防跨文档串题）。
        if len(picked) != len(clean_ids) or any(q["status"] != "active" or q["doc_id"] != doc_id for q in picked):
            raise SessionError(409, "QUESTION_SET_EMPTY", "题卷中有题目已下架、跨文档或不存在，请重新生成题卷")
        # 节点表做成字典，便于按 node_id 反查章节标题
        node_by_id = {n["node_id"]: n for n in nodes}
        items = []
        # 逐题构造 plan item；seq_no 从 1 开始递增，是 turn.seq_no 的对齐依据
        for i, q in enumerate(picked, start=1):
            # 节点可能已被删除，用 {} 兜底，标题退化为空串
            node = node_by_id.get(q["node_id"], {})
            # 注意：题卷模式的 item 直接带 question_id，
            # next_question 见到它就跳过“生成”步骤，直接取题库里的现成题。
            items.append({
                "seq_no": i, "node_id": q["node_id"], "kp_id": q["kp_id"],
                "section_title": node.get("title", ""), "section_path": [node.get("title", "")],
                "q_type": q["q_type"], "bloom_level": q["bloom_level"],
                "target_difficulty": q["difficulty"], "question_id": q["q_id"], "status": "pending",
            })
        # 题卷模式不做 Bloom / 章节配比（题目是固定的），因此 by_bloom、by_section 留空
        plan = {"total": len(items), "requested": len(items), "items": items,
                "by_bloom": {}, "by_section": [], "question_types": [q["q_type"] for q in picked]}
    # 路径 B：自动组卷 —— 把素材交给 planner，按模式 / 难度 / 题型配比生成计划
    else:
        # seed 用随机数，保证每次组卷不完全相同；排查问题时可以改成固定值复现
        plan = planner.build_plan(
            nodes=nodes,
            kps=kps,
            evidence_by_kp=by_kp,
            mode=mode,
            question_count=int(question_count),
            difficulty=difficulty,
            focus_sections=focus_sections,
            exclude_node_ids=exclude_node_ids,
            question_types=question_types,
            seed=random.randint(1, 1_000_000),
        )
    # ---- 步骤 8：计划兜底校验 ----
    # 题卷模式在步骤 7 已做过 409 校验，这里只约束“自动组卷”的结果。
    if plan["total"] < 5 and not question_ids:
        raise SessionError(422, "QUESTION_POOL_INSUFFICIENT", "该文档可用知识点不足，请先校正结构或减少题量")
    # ---- 步骤 9：落库 ----
    session_id = new_id("ses")
    # started 只取一次时间，保证 started_at 与 expires_at 基于同一时间基准
    started = now()
    # 组装行字典：字段名 → 值。后面用 keys() 动态拼 INSERT，新增字段无需改 SQL。
    row = {
        "session_id": session_id,
        "doc_id": doc_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "mode": mode,
        # state='ready'：计划已就绪，但还没下发第一题
        "state": "ready",
        "total_questions": plan["total"],
        "plan": dump(plan),
        "current_turn_id": None,
        "score": None,
        # theta 是 Elo 能力值，初始 500；chapter_ability 是“节点 → 能力值”的 JSON 缓存
        "theta_before": 500,
        "theta_after": 500,
        "chapter_ability": "{}",
        # 两个计数器记录学习行为：提示次数会影响得分（见 get_hint 的 penalty_percent）
        "skipped_count": 0,
        "hint_count": 0,
        "started_at": iso(started),
        "finished_at": None,
        # 会话 24 小时后过期，防止长期挂起占用资源（也便于前端提示“已超时”）
        "expires_at": iso(started + timedelta(hours=24)),
    }
    # 动态 INSERT：列名取自 row 的键，占位符数量与列数一致；
    # 值用 tuple(row.values()) 传入 —— Python 3.7+ 字典保序，顺序与 cols 完全对应。
    with db() as conn:
        cols = ", ".join(row.keys())
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO session ({cols}) VALUES ({marks})", tuple(row.values()))
    # 写审计日志；注意传的是 row（含 user_id / tenant_id），而不是 API 视图
    _log_event(row, "session_start", {"session_id": session_id, "mode": mode, "total": plan["total"]})
    # 返回前重新查一次库，保证返回结构与 get_session 完全一致（单一数据来源）
    return get_session(session_id, tenant_id)


# ---------------------------------------------------------------------------
# 【对外主流程 2】读取会话（API 视图）
# get_session 返回“给前端看的投影”，_raw_session 返回“给内部逻辑用的原始行”。
# 两者分离，既能防止内部字段外泄，也让“改内部结构不动接口”成为可能。
# ---------------------------------------------------------------------------
def get_session(session_id: str, tenant_id: str | None = None) -> dict:
    """按 id 读取会话，可选按租户过滤。

    tenant_id 传 None 时不加租户条件（供内部任务 / 后台使用）；
    传值时强制隔离，越权访问会得到与“不存在”相同的 404（不泄露存在性）。
    """
    # 动态拼接 WHERE：只有传了 tenant_id 才追加租户条件
    sql = "SELECT * FROM session WHERE session_id=?"
    params: tuple = (session_id,)
    # 注意：拼接进去的只是“固定 SQL 片段”，用户数据一律走 params 占位符
    if tenant_id:
        sql += " AND tenant_id=?"
        params += (tenant_id,)
    row = fetch_one(sql, params)
    if not row:
        raise SessionError(404, "SESSION_NOT_FOUND", "会话不存在")
    # 统一转换成对外 API 视图（字段改名 + 结构重组 + 白名单裁剪）
    return _session_api(row)


# ---------------------------------------------------------------------------
# 【视图转换】原始 session 行 → API 视图
# 本函数是对外契约的唯一出口：
#   * 字段改名（doc_id → document_id）；
#   * 结构重组（theta_before/after → ability 嵌套对象）；
#   * 内容裁剪（plan.items 只保留前端渲染需要的 6 个键）。
# ---------------------------------------------------------------------------
def _session_api(row: dict) -> dict:
    """把 session 数据库行转换成前端使用的结构。

    关键点：
    * 列名 doc_id 在这里改名为 document_id，前端只认后者；
    * plan 是 JSON 字符串，先 load_json 再重新组装，不直接把内部字段透传；
    * plan.items 用字典推导做“白名单裁剪”，隐藏 question_id / node_id / kp_id。
    """
    # plan 存的是 JSON 字符串；解析失败或为空时退化为 {}，保证后续 .get 不报错
    plan = load_json(row.get("plan"), {})
    return {
        "session_id": row["session_id"],
        "document_id": row["doc_id"],
        "user_id": row["user_id"],
        "assignment_id": row.get("assignment_id") or "",
        "mode": row["mode"],
        "state": row["state"],
        "total_questions": row["total_questions"],
        # requested 是用户最初请求的题量；plan.total 可能因素材不足而小于请求值
        "requested_questions": plan.get("requested", row["total_questions"]),
        "question_types": plan.get("question_types", []),
        "plan": {
            "total": row["total_questions"],
            # 【注意】这里统计的是 item["answered"]，而全文件写入计划状态的键是 "status"
            # （见 _mark_plan）。因此该值实际上恒为 0，属于历史遗留字段；
            # 前端应以 items[].status 为准。学习时留意这类“读写键不一致”的隐患，
            # 真实项目里很容易造成进度条永远不动。
            "answered": sum(1 for i in plan.get("items", []) if i.get("answered")),
            "by_bloom": plan.get("by_bloom", {}),
            "by_section": plan.get("by_section", []),
            "items": [
                # 白名单裁剪：只暴露展示字段，隐藏 question_id / node_id / kp_id 等内部标识
                {k: v for k, v in item.items() if k in ("seq_no", "section_title", "q_type", "bloom_level", "target_difficulty", "status")}
                for item in plan.get("items", [])
            ],
        },
        # 当前正在作答的轮次（没有则为 None）
        "current_turn_id": row.get("current_turn_id"),
        "score": row.get("score"),
        "ability": {"theta_before": row.get("theta_before", 500), "theta_after": row.get("theta_after", 500)},
        "created_at": row.get("started_at"),
        "expires_at": row.get("expires_at"),
    }


# ---------------------------------------------------------------------------
# 【对外主流程 3】取下一题（本文件最核心的编排函数）
# 流程：校验会话状态 → 幂等返回未提交轮次 → 找第一个未作答的计划项
#       → 题卷模式直接取题库 / 自动模式调用 generator 现场生成
#       → 同一事务内写 question + turn，并把 session 置为 waiting
# ---------------------------------------------------------------------------
def next_question(session_id: str, tenant_id: str | None = None) -> dict:
    """下发下一题，返回题面 + 进度。

    幂等性：若 session.current_turn_id 指向的 turn 仍是 asking，直接返回该题，
    避免用户刷新页面时重复生成题目（省钱，也避免计划错位）。

    分支
    ----
    * item.question_id 存在 → 题卷模式，从 question 表取现成题目；
    * 否则                  → 调用 generator.generate_question 现场生成并落库。

    异常
    ----
    * 409 NO_MORE_QUESTIONS   ：会话已结束，或计划已全部答完（此时会顺手生成报告）
    * 409 QUESTION_SET_EMPTY  ：题卷中的题目被下架
    * 409 SESSION_STATE_CONFLICT：知识点 / 证据缺失（文档结构被改动过）
    """
    # 内部逻辑用原始行：需要 plan / current_turn_id / theta 等未暴露给前端的字段
    session = _raw_session(session_id, tenant_id)
    # 终态直接拒绝，避免在已经出过报告的会话上继续出题
    if session["state"] == "finished":
        raise SessionError(409, "NO_MORE_QUESTIONS", "会话已结束，请查询报告")
    # 幂等：已有未提交轮次
    # ---- 未提交轮次的判定 ----
    # 不仅要 current_turn_id 非空，还要 turn 仍处于 asking：
    # 若 turn 已 answered/skipped 但 session 没来得及更新，则继续走正常流程。
    if session.get("current_turn_id"):
        turn = fetch_one("SELECT * FROM turn WHERE turn_id=?", (session["current_turn_id"],))
        if turn and turn["status"] == "asking":
            return _turn_payload(turn, session)
    # ---- 读取计划，计算“哪些题已完成” ----
    plan = load_json(session["plan"], {})
    items = plan.get("items", [])
    # 以 turn 表为准（而不是只看 plan.status），因为 turn 是真实发生过的作答记录
    turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no", (session_id,))
    # answered + skipped 都算“已完成”：跳过的题不再重复下发
    answered_seq = {t["seq_no"] for t in turns if t["status"] in ("answered", "skipped")}
    # 取第一个未完成的计划项；next(..., None) 表示找不到就返回 None
    item = next((i for i in items if i["seq_no"] not in answered_seq), None)
    # 计划已耗尽：先补生成报告（幂等，重复调用不会重复建），
    # 再抛 409 告知前端“没有更多题目”。用异常而不是返回空对象，
    # 是为了让前端用统一错误处理弹出“查看报告”的引导。
    if not item:
        _finish_internal(session)
        raise SessionError(409, "NO_MORE_QUESTIONS", "题目已全部完成，请结束会话")
    # ---- 分支 A：题卷模式（计划项里已绑定 question_id）----
    if item.get("question_id"):
        qrow = fetch_one("SELECT * FROM question WHERE q_id=?", (item["question_id"],))
        # 二次校验题目状态：从创建会话到真正出题之间，教师可能已把题目下架
        if not qrow or qrow["status"] != "active":
            raise SessionError(409, "QUESTION_SET_EMPTY", "题卷中的题目已下架，请联系教师重新布置")
        # 创建轮次：turn 是“会话 × 题目”的一次作答记录
        turn_id = new_id("turn")
        # 一个事务内完成两件事：插入 turn、把 session 指向它。
        # 中途失败时 with 块会回滚，不会留下“孤儿 turn”。
        with db() as conn:
            conn.execute(
                "INSERT INTO turn (turn_id, session_id, q_id, seq_no, answer, evaluation, score, status, hint_level,"
                " created_at, answered_at, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (turn_id, session_id, qrow["q_id"], item["seq_no"], "{}", "{}", 0, "asking", 0, iso(), None, 0),
            )
            conn.execute(
                "UPDATE session SET state='waiting', current_turn_id=?, plan=? WHERE session_id=?",
                (turn_id, dump(_mark_plan(plan, item["seq_no"], "asking")), session_id),
            )
        # 审计：记录“出了哪道题”；stem 只截前 80 字，避免日志表膨胀
        _log_event(session, "question_asked", {
            "session_id": session_id, "seq_no": item["seq_no"], "q_type": qrow["q_type"],
            "source": "paper", "stem": qrow["stem"][:80],
        })
        turn = fetch_one("SELECT * FROM turn WHERE turn_id=?", (turn_id,))
        # 重新读一次 session（state/plan 已变化），保证返回的进度是最新的
        return _turn_payload(turn, _raw_session(session_id, tenant_id))
    # ---- 分支 B：自动模式，现场生成题目 ----
    # 生成题目
    # 取出该计划项对应的文档、节点、知识点：这是“生成一道题”所需的最小上下文
    doc = fetch_one("SELECT * FROM document WHERE doc_id=?", (session["doc_id"],))
    node = fetch_one("SELECT * FROM node WHERE node_id=?", (item["node_id"],))
    kp = fetch_one("SELECT * FROM knowledge_point WHERE kp_id=?", (item["kp_id"],))
    evidence = _kp_evidence(kp, session["doc_id"])
    # 证据是硬性依赖：没有原文证据就无法保证题目有据可依，
    # 因此直接报状态冲突，让用户重建会话（比生成一道胡编的题安全得多）。
    if not node or not kp or not evidence:
        raise SessionError(409, "SESSION_STATE_CONFLICT", "当前考点缺少证据，请重新创建会话")
    # 本会话已出过的题干：传给生成器做去重，避免反复问相似问题。
    # 这里用 JOIN 只取 stem，避免 SELECT *。
    asked = [q["stem"] for q in fetch_all(
        "SELECT q.stem FROM question q JOIN turn t ON t.q_id=q.q_id WHERE t.session_id=?", (session_id,)
    )]
    # 同文档下其他知识点的定义，作为“干扰项素材”：
    # 选择题的错误选项由此而来，且必须是同文档内的概念，才有迷惑性。
    other_kps = fetch_all(
        "SELECT name, definition FROM knowledge_point WHERE doc_id=? AND kp_id<>? ORDER BY importance DESC LIMIT 20",
        (session["doc_id"], kp["kp_id"]),
    )
    # 复制一份 dict 再挂私有字段：避免污染 doc 原始行，也避免修改数据库取回的对象
    doc_ctx = dict(doc)
    # 干扰项筛选规则：
    #   1) 长度 20—120 字（太短无信息量，太长不适合做选项）；
    #   2) 前 30 字与正确知识点不同（用 normalize_text 归一化后比较，
    #      避免仅因标点 / 空格 / 大小写差异被判成“相同”）；
    # 最后只取 5 条，够一道选择题用即可。
    doc_ctx["_distractors"] = [
        k["definition"][:80] for k in other_kps
        if 20 <= len(k["definition"]) <= 120 and judge_mod.normalize_text(k["definition"][:30]) not in judge_mod.normalize_text(kp["definition"][:30])
    ][:5]
    # 调用出题器：完整上下文 + 计划项（含题型 / Bloom / 目标难度）+ 去重列表
    q = generator.generate_question(doc_ctx, node, kp, evidence, item, asked,
                                    session_id=session_id, user_id=session["user_id"])
    # 把生成结果规范化成 question 表的行结构（补 q_id、默认字段等）
    qrow = generator.question_row(q, item)
    # 出题时间由调用方补齐（生成器不关心持久化细节）
    qrow["created_at"] = iso()
    # 关键：生成题目与创建 turn 必须在同一事务里完成。
    # 若先提交题目再建 turn，失败时会留下“孤儿题目”，污染题库统计。
    with db() as conn:
        cols = ", ".join(qrow.keys())
        marks = ", ".join("?" for _ in qrow)
        conn.execute(f"INSERT INTO question ({cols}) VALUES ({marks})", tuple(qrow.values()))
        turn_id = new_id("turn")
        conn.execute(
            "INSERT INTO turn (turn_id, session_id, q_id, seq_no, answer, evaluation, score, status, hint_level,"
            " created_at, answered_at, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (turn_id, session_id, q["q_id"], item["seq_no"], "{}", "{}", 0, "asking", 0, iso(), None, 0),
        )
        conn.execute(
            "UPDATE session SET state='waiting', current_turn_id=?, plan=? WHERE session_id=?",
            (turn_id, dump(_mark_plan(plan, item["seq_no"], "asking")), session_id),
        )
    # 审计：source 标明题目来源（"paper" = 题卷 / 其他 = 生成器返回的来源标签）
    _log_event(session, "question_asked", {
        "session_id": session_id, "seq_no": item["seq_no"], "q_type": q["q_type"],
        "source": q.get("source"), "stem": q["stem"][:80],
    })
    # 重新读取 turn，确保返回的是数据库里的最终状态
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=?", (turn_id,))
    return _turn_payload(turn, _raw_session(session_id, tenant_id))


# ---------------------------------------------------------------------------
# 【视图转换】turn + question → 下发给前端的题面
# 只返回作答必需的字段：题干、选项、限时、提示层数。
# 特别注意：绝不返回 answer / correct_answer / rubric，否则前端可直接看到答案。
# ---------------------------------------------------------------------------
def _turn_payload(turn: dict, session: dict) -> dict:
    """组装“当前题目”的响应体。

    options 在库里是 JSON 字符串，这里解码成数组；
    progress 用 plan.items 中 answered/skipped 的数量计算，
    total 用 session.total_questions（即计划题量）。
    """
    # 题目正文单独查：turn 只存 q_id，题目内容在 question 表
    q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],))
    plan = load_json(session["plan"], {})
    # 已答 + 已跳过 = 已完成，用于前端进度条
    answered = sum(1 for i in plan.get("items", []) if i.get("status") in ("answered", "skipped"))
    return {
        "turn_id": turn["turn_id"],
        "seq_no": turn["seq_no"],
        "question": {
            "q_id": q["q_id"],
            "q_type": q["q_type"],
            "bloom_level": q["bloom_level"],
            "difficulty": q["difficulty"],
            "stem": q["stem"],
            "options": load_json(q["options"], []),
            "time_limit_sec": q["time_limit_sec"],
            "hint_levels": q["hint_levels"],
        },
        "progress": {"answered": answered, "total": session["total_questions"]},
        "expires_at": session["expires_at"],
    }


# ---------------------------------------------------------------------------
# 【对外主流程 4】提交作答（判分 + 能力更新）
# 本文件最长的事务性函数，顺序很关键：
#   幂等检查 → 答案校验 → 判分 → Elo / 章节能力更新 → 计划状态 → 落库 → 自动收尾
# 顺序不能随意调整：比如必须“先判分再更新能力值”，也必须“先算剩余题数再决定 state”。
# ---------------------------------------------------------------------------
def submit_answer(session_id: str, payload: dict, tenant_id: str | None = None) -> dict:
    """提交一道题的答案并返回判分结果。

    payload 结构（前端传入）：
        {"turn_id": "...", "answer": {"type": "choice", "value": "A"},
         "hint_level": 0, "time_spent_ms": 12345}

    幂等：turn 状态不是 asking 时（已答过 / 已跳过），直接返回首次判分结果，
    这样网络抖动导致的重试不会重复扣分，也不会重复更新能力值。
    """
    # 原始行：需要 theta_after / chapter_ability / plan 等内部状态
    session = _raw_session(session_id, tenant_id)
    # turn_id 允许前端不传，此时退化为“当前轮次”，
    # 适配“单题页面只有一个作答入口”的简单场景。
    turn_id = payload.get("turn_id")
    if not turn_id:
        turn_id = session.get("current_turn_id")
    # 必须同时匹配 session_id：防止拿 A 会话的 turn_id 去提交 B 会话的答案
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id, session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    # 幂等：重复提交返回首次判分
    # 直接回放历史判分，不重新调用 judge（判分可能调用 LLM，代价高且结果可能漂移）。
    if turn["status"] != "asking":
        return _answer_payload(turn, session)
    # 取出题目：判分需要 q_type / answer / rubric / difficulty / node_id
    q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],))
    # answer 必须是 {"type": ..., "value": ...} 的结构；
    # 这里只强校验 value 存在，type 缺失或错误会在下面按题目类型补全。
    answer = payload.get("answer") or {}
    if not isinstance(answer, dict) or "value" not in answer:
        raise SessionError(422, "ANSWER_INVALID", "answer 必须包含 type 与 value")
    # 容错：前端声明的 type 与题目实际类型不一致时，以题目为准。
    # （例如把 true_false 传成 choice，会让判分逻辑走错分支。）
    if answer.get("type") and answer["type"] != q["q_type"]:
        answer["type"] = q["q_type"]
    # 提示等级：优先取“提交时携带”的值，其次用 turn 上记录的（用户点过提示后刷新了页面），
    # 最后兜底 0。int(...) 兼容前端传字符串 "2"。
    hint_level = int(payload.get("hint_level") or turn.get("hint_level") or 0)
    # 判分入口：内部按题型分流（客观题精确匹配 / 主观题 LLM 评分）
    result = judge_mod.judge(q, answer, hint_level=hint_level)
    # ---- Elo 能力值的递推与章节拆分 ----
    # theta 是连续能力估计值（初始 500）：答对难题涨得多，答错简单题掉得多。
    # 这里以“上一次的 theta_after”作为本次的 theta_before，形成递推。
    # 能力更新
    theta_before = float(session.get("theta_after") or 500)
    # 已完成题数作为 Elo 的 K 因子调节量：做得越多，单题波动越小（估计更稳定）
    answered_count = len(fetch_all("SELECT turn_id FROM turn WHERE session_id=? AND status IN ('answered','skipped')", (session_id,)))
    theta_after = judge_mod.elo_update(theta_before, q["difficulty"], result["score"], answered_count)
    # 章节能力是“node_id → theta”的字典，用于报告里的章节掌握度
    chapter = load_json(session.get("chapter_ability"), {})
    chapter = judge_mod.update_chapter_ability(chapter, q["node_id"], theta_after, result["score"])
    # 把 judge 的返回规范化成 evaluation 结构再落库。
    # 统一在这里做“字段补全 + 默认值”，后续 _answer_payload / _report_api
    # 只需依赖这一个结构，避免各处再写 result.get(...) 的兜底逻辑。
    evaluation = {
        "score": result["score"],
        "correct": result["correct"],
        "dimension_scores": result.get("dimension_scores", {}),
        "feedback": result["feedback"],
        "evidence": result.get("evidence", []),
        "confidence": result.get("confidence", 0.9),
        "needs_review": result.get("needs_review", False),
        "covered_points": result.get("covered_points", []),
        "missing_points": result.get("missing_points", []),
    }
    # 更新计划项状态：置为 answered 后，该项不会再被 next_question 选中
    plan = load_json(session["plan"], {})
    plan = _mark_plan(plan, turn["seq_no"], "answered")
    # 剩余未完成题数：为 0 说明本次练习结束，需要自动生成报告
    remaining = sum(1 for i in plan.get("items", []) if i.get("status") not in ("answered", "skipped"))
    # 两条 UPDATE 放在同一事务：
    #   ① 写入本轮判分结果（含作答耗时，用于统计答题速度）；
    #   ② 清空 current_turn_id，并更新能力值 / 章节能力 / 计划 / 会话状态。
    with db() as conn:
        conn.execute(
            "UPDATE turn SET answer=?, evaluation=?, score=?, status='answered', hint_level=?, answered_at=?, latency_ms=? WHERE turn_id=?",
            (dump(answer), dump(evaluation), result["score"], hint_level, iso(), int(payload.get("time_spent_ms") or 0), turn_id),
        )
        conn.execute(
            "UPDATE session SET current_turn_id=NULL, theta_after=?, chapter_ability=?, plan=?, state=? WHERE session_id=?",
            (theta_after, dump(chapter), dump(plan), "finished" if remaining == 0 else "waiting", session_id),
        )
    _log_event(session, "answer_submitted", {
        "session_id": session_id, "seq_no": turn["seq_no"], "q_id": q["q_id"], "q_type": q["q_type"],
        "score": result["score"], "correct": result["correct"], "hint_level": hint_level,
    })
    # 重新读 session：上面的 UPDATE 已改了 state / plan / theta，旧对象已过期
    session = _raw_session(session_id, tenant_id)
    # 最后一题：立即生成报告（内部幂等）；再刷新一次 session，
    # 保证返回体里的 ability.theta_after 与报告里的分数口径一致。
    if remaining == 0:
        _finish_internal(session)
        session = _raw_session(session_id, tenant_id)
    # 重新读 turn，拿到带 evaluation 的最新行
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=?", (turn_id,))
    return _answer_payload(turn, session)


# ---------------------------------------------------------------------------
# 【视图转换】判分结果 → 前端响应
# 数据全部来自已落库的 turn.evaluation，因此“重复提交”与“首次提交”返回完全一致。
# ---------------------------------------------------------------------------
def _answer_payload(turn: dict, session: dict) -> dict:
    """组装作答响应体。

    next.action 是前端的关键驱动字段：
      ask_next → 继续调用 next_question；
      finish   → 跳转报告页。
    """
    # evaluation 是 JSON 字符串；解析失败时给 {}，下面所有字段都有默认值
    ev = load_json(turn.get("evaluation"), {})
    plan = load_json(session.get("plan"), {})
    # 剩余题数：与 submit_answer 中的计算口径保持一致（answered + skipped 都算完成）
    remaining = sum(1 for i in plan.get("items", []) if i.get("status") not in ("answered", "skipped"))
    return {
        "turn_id": turn["turn_id"],
        "score": turn.get("score", 0),
        "correct": ev.get("correct", False),
        "dimension_scores": ev.get("dimension_scores", {}),
        "feedback": ev.get("feedback", ""),
        "evidence": ev.get("evidence", []),
        "confidence": ev.get("confidence", 0.9),
        "needs_review": ev.get("needs_review", False),
        "covered_points": ev.get("covered_points", []),
        "missing_points": ev.get("missing_points", []),
        # 由 remaining 推导前端下一步动作，避免前端自己算（口径唯一，才好维护）
        "next": {"action": "finish" if remaining == 0 else "ask_next", "remaining": remaining},
        "ability": {"theta_before": session.get("theta_before", 500), "theta_after": session.get("theta_after", 500)},
    }


# ---------------------------------------------------------------------------
# 【对外主流程 5】请求提示（分层提示 + 扣分系数）
# 设计：提示必须逐级解锁（1 → 2 → 3），不能跳级；
#       每级提示对应一个扣分比例（HINT_PENALTIES，定义在 config 中）。
# ---------------------------------------------------------------------------
def get_hint(session_id: str, turn_id: str, level: int, tenant_id: str | None = None) -> dict:
    """获取当前题目的第 level 级提示。

    规则
    ----
    * 只有 asking 状态可以要提示（已作答返回 409）；
    * level 会被夹到 [1, 3]，且不允许跳级（最多比当前 hint_level 大 1）；
    * 提示不足 3 条时用通用引导语补齐，保证永远能返回内容。
    """
    # 取原始行（要用 current_turn_id 做缺省兜底）
    session = _raw_session(session_id, tenant_id)
    # turn_id 允许缺省：前端只传 level 时，默认作用于当前正在作答的轮次
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id or session.get("current_turn_id"), session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    # 已作答 / 已跳过：提示没有意义，直接拒绝，避免“先看提示再改答案”
    if turn["status"] != "asking":
        raise SessionError(409, "SESSION_STATE_CONFLICT", "当前轮次已作答")
    # 先把 level 夹到 1—3（兼容前端传 0、负数或超大值），
    # 再与“当前已解锁等级 + 1”比较，实现“必须逐级解锁”。
    level = max(1, min(3, int(level or 1)))
    # 跳级请求被“降级”到下一级，而不是直接报错 —— 对用户更友好
    if level > int(turn.get("hint_level") or 0) + 1:
        level = int(turn.get("hint_level") or 0) + 1
    # 降级后仍 > 3，说明三级已经用完
    if level > 3:
        raise SessionError(409, "HINT_LIMIT_REACHED", "同一题最多请求 3 次提示")
    # 提示文本存在 question.hints（JSON 数组），按 level-1 取下标
    q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],))
    hints = load_json(q.get("hints"), [])
    # 兜底：历史数据 / 生成的题目可能不足 3 条提示，
    # 用一句通用引导语补齐，保证 hints[level-1] 永远取得到。
    while len(hints) < 3:
        hints.append("回到对应章节，找出与该知识点直接相关的原句。")
    hint = hints[level - 1]
    # 原子更新：记录本题已解锁到的等级 + 会话级提示计数（用于行为统计与扣分）
    with db() as conn:
        conn.execute("UPDATE turn SET hint_level=? WHERE turn_id=?", (level, turn["turn_id"]))
        conn.execute("UPDATE session SET hint_count=hint_count+1 WHERE session_id=?", (session_id,))
    _log_event(session, "hint", {"session_id": session_id, "turn_id": turn["turn_id"], "level": level})
    # 返回扣分比例与剩余次数，前端可直接展示“本提示将扣 X%”
    return {
        "turn_id": turn["turn_id"],
        "level": level,
        "hint": hint,
        "penalty_percent": HINT_PENALTIES[level],
        "remaining_hints": 3 - level,
    }


# ---------------------------------------------------------------------------
# 【对外主流程 6】跳过题目
# 跳过的语义：记 0 分、写入一条“跳过型” evaluation、推进计划；
# 与 submit_answer 共用同一套“剩余题数 → 自动收尾”逻辑。
# ---------------------------------------------------------------------------
def skip_question(session_id: str, turn_id: str, reason: str = "", tenant_id: str | None = None) -> dict:
    """跳过当前题目。

    幂等：turn 已不是 asking 时，直接返回带分数的最小响应，不重复推进计划。
    跳过会在 evaluation 中标记 skipped=True 并保留 reason，
    报告据此把该题归入“未掌握 / 待复习”。
    """
    # 取原始行（同样要用 current_turn_id 做缺省兜底）
    session = _raw_session(session_id, tenant_id)
    # turn 必须属于当前会话，防止跨会话操作
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id or session.get("current_turn_id"), session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    # 已经作答 / 已跳过：不再重复处理，直接返回现状（幂等）
    if turn["status"] != "asking":
        return {"turn_id": turn["turn_id"], "score": turn.get("score", 0), "next": {"action": "ask_next"}}
    # 构造一条“跳过型”判分结果：分数 0、置信度 1.0（跳过是确定发生的事实）
    evaluation = {
        "score": 0, "correct": False, "dimension_scores": {}, "feedback": "本题已跳过，记 0 分。",
        "evidence": [], "confidence": 1.0, "needs_review": False, "covered_points": [], "missing_points": [],
        "skipped": True, "reason": reason,
    }
    # 计划项状态改为 skipped，使其不再被 next_question 选中
    plan = _mark_plan(load_json(session["plan"], {}), turn["seq_no"], "skipped")
    remaining = sum(1 for i in plan.get("items", []) if i.get("status") not in ("answered", "skipped"))
    # 事务：更新 turn 为 skipped + 推进 session（plan / 计数 / 状态）
    with db() as conn:
        conn.execute(
            "UPDATE turn SET status='skipped', score=0, answer=?, evaluation=?, answered_at=? WHERE turn_id=?",
            (dump({"type": "skip", "value": reason}), dump(evaluation), iso(), turn["turn_id"]),
        )
        conn.execute(
            "UPDATE session SET current_turn_id=NULL, plan=?, skipped_count=skipped_count+1, state=? WHERE session_id=?",
            (dump(plan), "finished" if remaining == 0 else "waiting", session_id),
        )
    _log_event(session, "skip", {"session_id": session_id, "turn_id": turn["turn_id"], "reason": reason})
    # 最后一题被跳过：同样要收尾出报告
    if remaining == 0:
        _finish_internal(_raw_session(session_id, tenant_id))
    return {"turn_id": turn["turn_id"], "score": 0, "next": {"action": "finish" if remaining == 0 else "ask_next", "remaining": remaining}}


# ---------------------------------------------------------------------------
# 【追问模块】
# 目的：在一道题作答后，用“澄清 / 证据 / 迁移”三类追问检验是否真正理解，
#       而不是靠选项蒙对。优先调用 LLM 生成，失败时回退到固定模板。
# ---------------------------------------------------------------------------
# 追问类型枚举：kind（机器用的 code） → 中文标签（给人看的说明）。
# 三类分别对应“解释概念 / 引用证据 / 迁移应用”，覆盖 Bloom 的领会—分析—应用层。
FOLLOWUP_KINDS = {"clarify": "澄清型", "evidence": "证据型", "transfer": "迁移型"}


def _followup_fallback(kind: str, question: dict, evaluation: dict) -> dict:
    """按追问类型生成模板化的问题（无 LLM 时的兜底）。

    三类追问的教学意图：
      clarify  —— 用自己的话解释概念，检验“是否理解”；
      evidence —— 回到原文找原句 / 页码，检验“是否有据”；
      transfer —— 迁移到新场景，检验“是否会应用”。
    """
    # 知识点名可能缺失（题目未关联 kp），用占位语避免出现“「None」”
    kp = question.get("kp_name") or "这个知识点"
    # 证据型：要求引用原文并给出页码 —— 这是论文阅读最核心的能力
    if kind == "evidence":
        return {"question": f"请回原文找出一句能直接支持「{kp}」的原句，并说明它所在页码；再解释这句话为什么能支撑你的判断。",
                "rationale": "检查你能否把答案落到可核验的原文证据，而不是只复述结论。"}
    # 迁移型：要求说明“迁移后仍成立的条件”，即适用边界
    if kind == "transfer":
        return {"question": f"如果把「{kp}」背后的方法迁移到一个新的材料或场景，你会怎样调整？请说明迁移后仍然成立的条件。",
                "rationale": "检查你是否理解了方法的适用边界，而不只是记住原文表述。"}
    # 默认（clarify）：解释它解决什么问题、依赖什么条件、与相邻概念的区别
    return {"question": f"请用自己的话解释「{kp}」：它解决什么问题、依赖什么条件、和相邻概念的区别是什么？",
            "rationale": "检查你是否真正理解概念，而不是依赖选项或原文短语。"}


# ---------------------------------------------------------------------------
# 【对外主流程 7】创建追问
# 只有题目“已作答”后才允许追问；追问的回答质量会并入报告统计（但不进总分）。
# ---------------------------------------------------------------------------
def create_follow_up(session_id: str, turn_id: str, kind: str, tenant_id: str | None = None) -> dict:
    """为某个已作答的轮次创建一个追问。

    流程：校验 kind → 取题目与判分结果 → 生成兜底问题 →
          LLM 可用则尝试覆盖 → 落库 → 返回。
    注意：LLM 调用失败被完全吞掉（pass），保证追问功能不会因模型故障而不可用。
    """
    # kind 容错：非法值一律按“澄清型”处理，避免前端枚举不一致导致 500
    kind = kind if kind in FOLLOWUP_KINDS else "clarify"
    session = _raw_session(session_id, tenant_id)
    # 必须同时匹配 session_id，防止跨会话创建追问
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id, session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    # 只有 answered 才能追问；asking（还没答）/ skipped（跳过）都不允许
    if turn["status"] != "answered":
        raise SessionError(409, "FOLLOWUP_NOT_READY", "先完成本题作答，再进入追问")
    # 取题目详情用于填充模板；`or {}` 兜底，避免题目被删导致 KeyError
    question = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],)) or {}
    evaluation = load_json(turn.get("evaluation"), {})
    # 先拿兜底结果：即便下面 LLM 全部失败，也能返回一个可用的追问
    fallback = _followup_fallback(kind, question, evaluation)
    result = dict(fallback)
    # 只有配置了 LLM 才走“智能生成”分支
    if llm.available():
        # 证据最多取 2 条：prompt 里放太多会稀释重点，也增加 token 成本
        evidence = load_json(question.get("evidence"), [])
        # Prompt 设计要点：
        #   1) 角色设定（论文导读教练）+ 明确的 JSON 输出格式；
        #   2) 三类追问分别给出“必须满足的要求”；
        #   3) 明确禁止“重复原题”“直接给答案”，避免追问变成第二次讲解；
        #   4) 把原题、知识点、用户答案、评分反馈、原文证据一并给出，
        #      让模型针对“这道题的这个答案”追问，而不是泛泛而谈。
        prompt = (
            "你是论文导读教练。基于当前题目和用户作答，生成一个简短的追问。"
            f"追问类型：{FOLLOWUP_KINDS[kind]}。只输出 JSON："
            '{"question":"追问问题","rationale":"为什么追问"}。'
            "澄清型要求用户用自己的话解释概念或区别；证据型要求引用原文并说明页码；"
            "迁移型要求把方法应用到新条件并说明边界。不要重复原题，不要直接给答案。"
            f"\n原题：{question.get('stem','')}\n知识点：{question.get('kp_name','')}\n"
            f"用户答案：{json.dumps(load_json(turn.get('answer'), {}), ensure_ascii=False)}\n"
            f"评分反馈：{evaluation.get('feedback','')}\n"
            f"原文证据：{json.dumps(evidence[:2], ensure_ascii=False)}"
        )
        # 调用失败 / 返回非 JSON 都不影响主流程，因此整段包在 try 里
        try:
            # json_mode 让模型尽量只输出 JSON；temperature 0.4 兼顾多样性与稳定性
            raw = llm.chat([{"role": "user", "content": prompt}], kind="followup", json_mode=True,
                           temperature=0.4, max_tokens=500)
            # 先剥掉 Markdown 代码围栏（```json ... ```）再解析：
            # 即便开了 json_mode，部分模型仍会加围栏，这里做防御式处理。
            parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
            # 只有解析出非空 question 才覆盖兜底值，否则保留模板
            if isinstance(parsed, dict) and parsed.get("question"):
                result["question"] = str(parsed["question"])
                result["rationale"] = str(parsed.get("rationale") or result["rationale"])
        # 静默降级：这是“增强功能”，不应让主流程失败
        except Exception:
            pass
    # 追问与“当前题目轮次”绑定，因此用 turn_id 关联
    follow_up_id = new_id("fu")
    # 落库时 status='created'：表示问题已生成、等待用户回答
    with db() as conn:
        conn.execute(
            "INSERT INTO follow_up (follow_up_id, session_id, turn_id, q_id, kind, question, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (follow_up_id, session_id, turn_id, turn.get("q_id"), kind, result["question"], "created", iso()),
        )
    _log_event(session, "followup_created", {"session_id": session_id, "turn_id": turn_id,
                                             "kind": kind, "follow_up_id": follow_up_id})
    # 返回时附带 evidence，前端可在追问旁展示原文出处，帮助用户作答
    return {"follow_up_id": follow_up_id, "turn_id": turn_id, "kind": kind, "kind_label": FOLLOWUP_KINDS[kind],
            "question": result["question"], "rationale": result["rationale"],
            "evidence": load_json(question.get("evidence"), [])[:2]}


# ---------------------------------------------------------------------------
# 【对外主流程 8】回答追问（评分）
# 与判分主流程不同：追问评分“不参与”Elo 能力值与总成绩，
# 只作为理解深度的补充证据进入报告。
# ---------------------------------------------------------------------------
def answer_follow_up(session_id: str, turn_id: str, kind: str, answer: str, tenant_id: str | None = None,
                     follow_up_id: str | None = None) -> dict:
    """提交追问回答并返回评分。

    follow_up_id 可选：
    * 传了   → 精确更新那条追问；
    * 没传   → 按 (session, turn, kind) 找最近一条 status='created' 的追问；
    * 仍没有 → 新建一条记录（兼容旧前端不先调用 create_follow_up 的情况）。
    """
    # 先做非空校验：空回答没有评分意义，也容易让 LLM 输出无意义内容
    answer = (answer or "").strip()
    if not answer:
        raise SessionError(422, "FOLLOWUP_EMPTY", "请先写下你的追问回答")
    session = _raw_session(session_id, tenant_id)
    # 追问依附于 turn，因此先确认 turn 属于当前会话
    turn = fetch_one("SELECT * FROM turn WHERE turn_id=? AND session_id=?", (turn_id, session_id))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    question = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],)) or {}
    # 与 create_follow_up 保持一致的类型兜底
    kind = kind if kind in FOLLOWUP_KINDS else "clarify"
    # Prompt 要求模型输出结构化评分：score(0-100) + 反馈 + 命中点 + 遗漏点。
    # “不要编造原文”是硬约束：防止模型为了给分而虚构引用。
    prompt = (
        "你是论文导读教练，请评价用户对追问的回答。只输出 JSON："
        '{"score":0到100的整数,"feedback":"两句话以内反馈","hit":["答对的要点"],"miss":["遗漏的要点"]}。'
        "按是否理解概念、是否引用原文证据、是否能迁移应用来评分；不要编造原文。"
        f"\n追问类型：{FOLLOWUP_KINDS[kind]}\n原题：{question.get('stem','')}\n"
        f"知识点：{question.get('kp_name','')}\n追问回答：{answer}\n"
        f"原文证据：{json.dumps(load_json(question.get('evidence'), [])[:2], ensure_ascii=False)}"
    )
    # 默认值：LLM 不可用或解析失败时使用。
    # 给 60 分而不是 0 分，是因为“写了回答”本身就比不写好，避免打击积极性。
    score, feedback, hit, miss = 60, "已记录你的追问回答。建议再回到原文核对关键句与页码。", [], []
    # 有模型才做智能评分
    if llm.available():
        try:
            raw = llm.chat([{"role": "user", "content": prompt}], kind="followup_judge", json_mode=True,
                           temperature=0.2, max_tokens=500)
            # 同样先剥掉 Markdown 代码围栏
            parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
            if isinstance(parsed, dict):
                # 分数夹到 [0,100]，防止模型返回 120 或负数污染统计
                score = max(0, min(100, int(parsed.get("score", 60))))
                feedback = str(parsed.get("feedback") or feedback)
                # hit / miss 各取前 4 条：前端展示空间有限，也避免模型长篇大论
                hit = [str(x) for x in (parsed.get("hit") or [])][:4]
                miss = [str(x) for x in (parsed.get("miss") or [])][:4]
        # 任何异常都退回默认值（60 分 + 提示语），保证接口始终可用
        except Exception:
            pass
    # ---- 定位要更新的追问记录 ----
    row = None
    # 优先用显式 follow_up_id（且必须属于本会话，防越权）
    if follow_up_id:
        row = fetch_one("SELECT * FROM follow_up WHERE follow_up_id=? AND session_id=?", (follow_up_id, session_id))
    # 没传 id 或 id 无效时，找“最近一条尚未回答的同类型追问”：
    # 这是为了兼容“先 create_follow_up、再 answer_follow_up”的正常链路。
    if not row:
        row = fetch_one(
            "SELECT * FROM follow_up WHERE session_id=? AND turn_id=? AND kind=? AND status='created'"
            " ORDER BY created_at DESC LIMIT 1", (session_id, turn_id, kind),
        )
    # 找到 → UPDATE；找不到 → INSERT 一条完整记录（兼容旧客户端）
    if row:
        follow_up_id = row["follow_up_id"]
        with db() as conn:
            conn.execute(
                "UPDATE follow_up SET answer=?, score=?, feedback=?, hit=?, miss=?, status='answered', answered_at=?"
                " WHERE follow_up_id=?",
                (answer, score, feedback, dump(hit), dump(miss), iso(), follow_up_id),
            )
    else:
        follow_up_id = new_id("fu")
        with db() as conn:
            conn.execute(
                "INSERT INTO follow_up (follow_up_id, session_id, turn_id, q_id, kind, question, answer, score,"
                " feedback, hit, miss, status, created_at, answered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (follow_up_id, session_id, turn_id, turn.get("q_id"), kind,
                 str(question.get("stem") or ""), answer, score, feedback, dump(hit), dump(miss),
                 "answered", iso(), iso()),
            )
    # 审计：追问评分不写入 turn，因此审计日志是唯一的行为追溯来源
    _log_event(session, "followup_answered", {"session_id": session_id, "turn_id": turn_id, "kind": kind,
                                               "score": score, "follow_up_id": follow_up_id})
    return {"follow_up_id": follow_up_id, "turn_id": turn_id, "kind": kind, "score": score,
            "feedback": feedback, "hit": hit, "miss": miss}


# ---------------------------------------------------------------------------
# 【查询】追问列表
# ---------------------------------------------------------------------------
def list_follow_ups(session_id: str, turn_id: str | None = None, tenant_id: str | None = None) -> list[dict]:
    """列出某个会话（可限定某个轮次）的全部追问。

    hit / miss 在库里是 JSON 字符串，这里统一解码成数组再返回，
    避免各前端各自解析、格式不一致。
    """
    # 先校验会话存在 / 租户匹配（越权时抛 404），再查子表
    _raw_session(session_id, tenant_id)
    # 动态条件：turn_id 可选，参数用 list 动态追加
    sql = "SELECT * FROM follow_up WHERE session_id=?"
    params: list = [session_id]
    if turn_id:
        sql += " AND turn_id=?"
        params.append(turn_id)
    sql += " ORDER BY created_at"
    rows = fetch_all(sql, params)
    # 就地解码 JSON 字段（直接修改行对象后返回给上层）
    for row in rows:
        row["hit"] = load_json(row.get("hit"), [])
        row["miss"] = load_json(row.get("miss"), [])
    return rows


# ---------------------------------------------------------------------------
# 【对外主流程 9】结束会话 / 生成报告
# 与 _finish_internal 的分工：
#   finish_session   → 对外入口，额外写一条 task 记录（前端据此显示“报告已生成”）
#   _finish_internal → 幂等内核，任何“最后一题完成 / 跳过 / 无题可出”的路径都会调用
# ---------------------------------------------------------------------------
def finish_session(session_id: str, tenant_id: str | None = None, reason: str = "user_finished") -> dict:
    """主动结束会话并生成报告。

    返回 report_task_id：前端可用它去任务中心查进度。
    本实现是同步生成报告，但保留 task 记录是为了与异步报告生成的历史接口保持兼容。

    参数 reason 目前仅作语义标记、并未落库（历史遗留，保留以免破坏调用方）。
    """
    # 幂等：若报告已存在，_finish_internal 会直接返回旧报告
    session = _raw_session(session_id, tenant_id)
    report = _finish_internal(session)
    # 写一条“已成功”的任务记录，让前端能在任务列表里看到这次报告生成
    task_id = new_id("task")
    with db() as conn:
        conn.execute(
            "INSERT INTO task (task_id, type, status, stage, progress, document_id, session_id, result, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (task_id, "report_generate", "succeeded", "done", 100, session["doc_id"], session_id,
             dump({"session_id": session_id, "report_id": report["report_id"]}), iso(), iso()),
        )
    # 返回 report_id，前端可直接跳转报告页
    return {"session_id": session_id, "state": "finished", "report_task_id": task_id, "report_id": report["report_id"]}


# ---------------------------------------------------------------------------
# 【内部核心】收尾：生成并持久化报告（幂等）
# 所有“练习结束”路径的公共出口：
#   * next_question 发现计划耗尽
#   * submit_answer 最后一题作答完成
#   * skip_question 最后一题被跳过
#   * finish_session 用户主动结束
# ---------------------------------------------------------------------------
def _finish_internal(session: dict) -> dict:
    """生成报告并结束会话；若报告已存在则直接返回（幂等）。

    步骤
    ----
    1) 查重：同一 session 只允许一份报告；
    2) 汇总数据：turns + questions + nodes；
    3) 交给 reports.build_report 计算分数 / 掌握度 / 薄弱点；
    4) 合并追问统计（追问不计入总分，只作补充指标）；
    5) 单事务写入 report + 把 session 置为 finished；
    6) 触发作业模块回调（失败不影响主流程）。
    """
    # ① 幂等：并发 / 重复调用时，第二次直接命中已生成的报告
    existing = fetch_one("SELECT * FROM report WHERE session_id=?", (session["session_id"],))
    if existing:
        return _report_api(existing)
    # ② 取本会话所有轮次（按 seq_no 排序，保证报告里的题目顺序稳定）
    turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no", (session["session_id"],))
    # 批量取题目，避免在报告构建里逐题查库（典型的 N+1 规避）
    qids = [t["q_id"] for t in turns]
    questions = {}
    # 动态 IN 查询：qids 为空时不能拼出 "IN ()"，因此加 if 保护
    if qids:
        marks = ",".join("?" for _ in qids)
        for q in fetch_all(f"SELECT * FROM question WHERE q_id IN ({marks})", qids):
            questions[q["q_id"]] = q
    # 节点表：报告需要 node_id → 标题，用于“章节掌握度”展示
    nodes = {n["node_id"]: n for n in fetch_all("SELECT * FROM node WHERE doc_id=?", (session["doc_id"],))}
    # 真正的统计逻辑在 reports 模块，sessions 只负责取数与落库（职责分离）
    report = build_report(session, turns, questions, nodes)
    # 追问单独统计：它是“理解深度”的辅助指标，不混入主分数
    follow_ups = list_follow_ups(session["session_id"])
    answered_fu = [x for x in follow_ups if x.get("status") == "answered"]
    # 把追问统计挂到 summary 上。
    # report 是 build_report 产出的 dict，summary 一定存在，故直接下标赋值。
    report["summary"]["follow_ups"] = {
        "total": len(follow_ups),
        "answered": len(answered_fu),
        "avg_score": round(sum(float(x.get("score") or 0) for x in answered_fu) / len(answered_fu), 1) if answered_fu else None,
    }
    # ⑤ 单事务：插入报告 + 把会话置为 finished，二者必须同时成功
    with db() as conn:
        conn.execute(
            "INSERT INTO report (report_id, session_id, summary, section_mastery, bloom_distribution, wrong_questions,"
            " strengths, weaknesses, recommendations, generated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                report["report_id"], report["session_id"], dump(report["summary"]), dump(report["section_mastery"]),
                dump(report["bloom_distribution"]), dump(report["wrong_questions"]), dump(report["strengths"]),
                dump(report["weaknesses"]), dump(report["recommendations"]), report["generated_at"],
            ),
        )
        conn.execute(
            "UPDATE session SET state='finished', finished_at=?, score=? WHERE session_id=?",
            (iso(), report["summary"]["score"], session["session_id"]),
        )
    # 审计：报告生成事件，附带分数便于运营统计
    _log_event(session, "report_ready", {"session_id": session["session_id"], "report_id": report["report_id"],
                                          "score": report["summary"]["score"]})
    # ⑥ 回调作业模块（作业 / 班级场景需要把成绩回写到 assignment）。
    # 用 try/except 包住 + 函数内延迟 import：
    #   * 避免 sessions 与 assignments 循环导入；
    #   * 回写失败不应让报告接口 500（报告本身已经生成好了）。
    try:
        from . import assignments as assignment_mod
        assignment_mod.on_session_finished(session["session_id"], report)
    except Exception:
        pass
    return report


# ---------------------------------------------------------------------------
# 【对外主流程 10】读取报告
# 懒生成策略：会话已 finished 但没有报告行（历史数据 / 异常中断）时，
# 现场补生成一次，保证“已结束就一定有报告可看”。
# ---------------------------------------------------------------------------
def get_report(session_id: str, tenant_id: str | None = None) -> dict:
    """读取会话报告。

    * 有报告行            → 直接转 API 视图；
    * 无报告行但会话已结束 → 现场补生成（_finish_internal 幂等）；
    * 会话还没结束        → 409 REPORT_NOT_READY。
    """
    # 先校验会话归属（越权直接 404）
    session = _raw_session(session_id, tenant_id)
    # 一个会话最多一份报告
    row = fetch_one("SELECT * FROM report WHERE session_id=?", (session_id,))
    # 没有报告行：只有“已结束”的会话才允许补生成
    if not row:
        # 还没结束就查报告，属于时序错误（前端应先答完题）
        if session["state"] != "finished":
            # 用 409 而不是 404：资源存在，但当前状态不允许该操作
            raise SessionError(409, "REPORT_NOT_READY", "报告尚未生成")
        # 补生成：_finish_internal 内部会重新取数并落库
        return _finish_internal(session)
    return _report_api(row)


# ---------------------------------------------------------------------------
# 【视图转换】report 行 → API 视图
# 报告各列都是 JSON，统一在这里解码；同时“实时刷新”追问统计，
# 避免报告生成后用户又答了追问、但统计仍是旧值。
# ---------------------------------------------------------------------------
def _report_api(row: dict) -> dict:
    """把 report 表行转换成前端结构，并实时刷新追问统计。"""
    # summary 是 JSON；报告核心指标（总分 / 正确率 / 用时等）都在里面
    summary = load_json(row["summary"], {})
    # 追问可能是在报告生成之后才回答的，因此这里重新统计一次
    fus = fetch_all("SELECT * FROM follow_up WHERE session_id=?", (row["session_id"],))
    answered = [x for x in fus if x.get("status") == "answered"]
    summary["follow_ups"] = {
        "total": len(fus), "answered": len(answered),
        "avg_score": round(sum(float(x.get("score") or 0) for x in answered) / len(answered), 1) if answered else None,
    }
    # 其余分析维度各自独立成列（JSON 数组 / 对象），按需解码
    return {
        "report_id": row["report_id"],
        "session_id": row["session_id"],
        "summary": summary,
        "section_mastery": load_json(row["section_mastery"], []),
        "bloom_distribution": load_json(row["bloom_distribution"], {}),
        "wrong_questions": load_json(row["wrong_questions"], []),
        "strengths": load_json(row["strengths"], []),
        "weaknesses": load_json(row["weaknesses"], []),
        "recommendations": load_json(row["recommendations"], []),
        # 报告生成时间（ISO 字符串）
        "generated_at": row["generated_at"],
    }


# ---------------------------------------------------------------------------
# 【查询】单题复盘
# 与 _turn_payload 的区别：复盘发生在“作答之后”，因此可以展示
# 正确答案、解析、评分要点与原文证据 —— 但用 can_view 严格控制展示时机。
# ---------------------------------------------------------------------------
def get_turn_review(session_id: str, seq_no: int, tenant_id: str | None = None) -> dict:
    """返回一题的完整复盘：题干、作答、正确答案、评分要点与原文证据。"""
    # 校验会话归属（越权直接 404）
    _raw_session(session_id, tenant_id)
    # seq_no 统一转 int，兼容前端传字符串
    turn = fetch_one("SELECT * FROM turn WHERE session_id=? AND seq_no=?", (session_id, int(seq_no)))
    if not turn:
        raise SessionError(404, "TURN_NOT_FOUND", "轮次不存在")
    # 题目可能已被删除，用 {} 兜底
    q = fetch_one("SELECT * FROM question WHERE q_id=?", (turn["q_id"],)) or {}
    evaluation = load_json(turn.get("evaluation"), {})
    # 标准答案与解析存放在 question.answer 这个 JSON 列里
    q_answer = load_json(q.get("answer"), {})
    # 可见性规则：只有 answered / skipped 的轮次才展示答案与解析，
    # 防止用户在“正在作答”时通过复盘接口拿到正确答案。
    # 关键开关：未作答 / 正在作答时一律隐藏答案类字段
    can_view = turn.get("status") in ("answered", "skipped")
    return {
        "turn_id": turn["turn_id"],
        "seq_no": turn["seq_no"],
        "status": turn.get("status"),
        "q_id": turn["q_id"],
        "question": {
            "q_type": q.get("q_type"),
            "bloom_level": q.get("bloom_level"),
            "difficulty": q.get("difficulty"),
            "stem": q.get("stem"),
            "options": load_json(q.get("options"), []),
            "hints": load_json(q.get("hints"), []),
        },
        "answer": load_json(turn.get("answer"), {}),
        "evaluation": evaluation,
        "score": turn.get("score", 0),
        # 下面四个字段都受 can_view 控制：未完成时返回 None / "" / []
        "correct_answer": q_answer.get("value") if can_view else None,
        "explanation": (q_answer.get("explanation") or evaluation.get("feedback") or "") if can_view else "",
        # rubric 优先取题目自带的评分要点，其次用判分时写入的 rubric
        "rubric": (load_json(q.get("rubric"), []) or evaluation.get("rubric") or []) if can_view else [],
        "reference_evidence": load_json(q.get("evidence"), []) if can_view else [],
        "can_view_answer": can_view,
    }


# ---------------------------------------------------------------------------
# 【查询】轮次列表（会话内的作答流水）
# ---------------------------------------------------------------------------
def list_turns(session_id: str, tenant_id: str | None = None) -> list[dict]:
    """列出会话的所有轮次（含题干摘要、分数、状态与判分详情）。

    注意：这里对每个 turn 单独查一次 question，属于 N+1 查询；
    单会话 ≤30 题的规模下可接受，若要优化可改成一次 IN 批量查询。
    """
    _raw_session(session_id, tenant_id)
    # 按 seq_no 排序，保证与出题顺序一致
    turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no", (session_id,))
    out = []
    for t in turns:
        # 逐题补题干信息；or {} 兜底，避免题目缺失导致 KeyError
        q = fetch_one("SELECT stem, q_type, bloom_level FROM question WHERE q_id=?", (t["q_id"],)) or {}
        out.append({
            "turn_id": t["turn_id"],
            "seq_no": t["seq_no"],
            "q_id": t["q_id"],
            "stem": q.get("stem", ""),
            "q_type": q.get("q_type", ""),
            "bloom_level": q.get("bloom_level", ""),
            "score": t.get("score", 0),
            "status": t.get("status"),
            "evaluation": load_json(t.get("evaluation"), {}),
            "created_at": t.get("created_at"),
        })
    return out


# ---------------------------------------------------------------------------
# 【内部工具】按知识点取原文证据
# evidence_ids 是 JSON 数组；这里保持“强证据优先”的排序，
# 让生成器优先使用 strong 证据，降低题目失真的概率。
# ---------------------------------------------------------------------------
def _kp_evidence(kp: dict, doc_id: str) -> list[dict]:
    """取出某知识点关联的全部证据，强证据排在前面。"""
    # 知识点可能没有证据（自动抽取失败），直接返回空列表
    ids = load_json(kp.get("evidence_ids"), [])
    if not ids:
        return []
    # 参数顺序必须与 SQL 占位符一致：doc_id 在最前，其后是各 evidence_id
    marks = ",".join("?" for _ in ids)
    rows = fetch_all(f"SELECT * FROM evidence WHERE doc_id=? AND evidence_id IN ({marks})", tuple([doc_id] + ids))
    # strength == "strong" 记 0（排前），其余记 1；sort 稳定，同组内保持原顺序
    rows.sort(key=lambda r: 0 if r["strength"] == "strong" else 1)
    return rows


# ---------------------------------------------------------------------------
# 【内部工具】读取 session 原始行
# 与 get_session 的区别：不做字段投影、不做 JSON 解码，
# 供内部逻辑直接使用（需要 plan / theta / current_turn_id 等字段）。
# ---------------------------------------------------------------------------
def _raw_session(session_id: str, tenant_id: str | None = None) -> dict:
    """读取 session 原始数据库行（内部使用），不存在时抛 404。

    tenant_id 传值时强制租户隔离；越权与不存在返回同样的错误，避免信息泄露。
    """
    # 与 get_session 相同的动态租户条件写法（两处逻辑一致，便于理解）
    sql = "SELECT * FROM session WHERE session_id=?"
    params: tuple = (session_id,)
    # 只有显式传了租户才追加，保证后台任务可以不传
    if tenant_id:
        sql += " AND tenant_id=?"
        params += (tenant_id,)
    # 查不到一律 404：不区分“不存在”和“无权访问”
    row = fetch_one(sql, params)
    if not row:
        raise SessionError(404, "SESSION_NOT_FOUND", "会话不存在")
    return row


# ---------------------------------------------------------------------------
# 【内部工具】更新计划项状态
# plan 是内存中的 dict，就地修改，落库由调用方负责 dump。
# 返回同一个对象，方便写成 _mark_plan(plan, ...) 的链式调用。
# ---------------------------------------------------------------------------
def _mark_plan(plan: dict, seq_no: int, status: str) -> dict:
    """把指定 seq_no 的计划项状态改为 status（pending / asking / answered / skipped）。"""
    # item["seq_no"] 用下标而非 .get：计划项必须有序号，缺失说明计划已损坏，应尽早暴露
    for item in plan.get("items", []):
        if item["seq_no"] == seq_no:
            # 就地修改并返回同一对象（原地修改，调用方无需接收返回值也能生效）
            item["status"] = status
    return plan

#
# ============================================================================
# 【阅读小结】
# 1) 本文件是“会话”聚合根的应用服务：所有状态流转都集中在此，便于审计与测试。
# 2) 读写分离：*_api 负责输出投影，_raw_* 负责内部读取，边界清晰。
# 3) 幂等贯穿始终：next_question / submit_answer / skip_question / finish 都可安全重试。
# 4) 降级策略：LLM 不可用时追问有模板兜底，报告仍能正常生成。
# 5) 可继续优化的小点：
#      * _session_api 里统计用的 "answered" 键，与 _mark_plan 写入的 "status" 不一致；
#      * list_turns 存在 N+1 查询；
#      * finish_session 的 reason 参数未落库。
# ============================================================================
