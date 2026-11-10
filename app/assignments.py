"""V1.2 教学闭环：题卷布置、学生作答与教师复盘。"""
# =============================================================================
# 模块：app/assignments.py —— 教学闭环中的“作业（题卷）编排”服务层
# -----------------------------------------------------------------------------
# 一、这个文件负责什么
#   把一份题卷（question_set）布置给一名学生（app_user），并维护作业状态机：
#       assigned（待开始） -> in_progress（进行中）
#       -> submitted（待复盘） -> reviewed（已复盘）
#   它不直接处理 HTTP，也不负责出题和判分：
#       · 出题由 generator.py / teaching_api.py 完成；
#       · 答题、评分、报告由 sessions.py 完成；
#       · 本文件只做“布置、开始、提交、复盘”的编排与权限校验。
#
# 二、在整个应用中的位置（调用关系）
#   FastAPI 路由 teaching_api.py
#       POST /api/v1/assignments             -> create_assignment()
#       GET  /api/v1/assignments             -> list_assignments()
#       GET  /api/v1/assignments/{id}        -> get_assignment()
#       POST /api/v1/assignments/{id}/start  -> start_assignment()
#       POST /api/v1/assignments/{id}/submit -> submit_assignment()
#       POST /api/v1/assignments/{id}/review -> review_assignment()
#       GET  /api/v1/assignments/by-session  -> _api() / _can_view()
#   本文件向下依赖：
#       db.py       —— 连接管理、参数化查询、JSON 序列化/反序列化、时间与 ID
#       sessions.py —— 创建/结束答题会话、读取会话、追问列表、复盘报告
#   反向回调：sessions._finish_internal() 在会话结束时调用
#       on_session_finished()，把作业状态自动同步为 submitted（见文件末尾）。
#
# 三、涉及的数据表
#   paper_assignment  作业主表（一行 = 一次布置）
#   question_set      题卷表（paper_id 对应 set_id）
#   document          被出题的论文（deleted_at 为软删除标记）
#   session / turn / report  学生的一次答题过程与复盘报告
#   app_user          用户表（学生、教师、管理员共用）
#
# 四、权限模型（Principal.role）
#   admin    —— 可管理、可查看所有作业
#   teacher  —— 只能管理自己布置的作业（teacher_id 匹配）
#   user     —— 学生，只能看到/开始/提交分配给自己的作业
#   权限不足统一抛 403；资源不存在抛 404（避免通过状态码探测他人资源）。
#
# 五、错误约定
#   所有业务错误都抛 AssignmentError(status, code, message, details)，
#   由 teaching_api._handle() 捕获并转换成 HTTPException，响应体形如
#   {“code”: “…”, “message”: “…”, …details}；前端应依据 code 做分支，
#   不要解析中文 message。
#
# 六、阅读提示
#   · 本文件只新增注释，没有改动任何可执行语句、变量名、签名或控制流；
#   · 字典字面量、多行 SQL、参数元组等多行结构，在结构上方整块解释；
#   · 注释里的“注意/为什么”解释设计取舍，“做什么”解释该行动作。
# =============================================================================
#
# 让类型注解“延迟求值”（PEP 563）：注解在运行时不会被真正计算，
# 因此 dict | None、float | None 这类新语法在旧版本 Python 上也能安全导入。
# 它必须位于模块文档字符串之后、其它所有语句之前。
from __future__ import annotations

# 仅用于校验 due_at 是否为合法的 ISO-8601 时间；
# 这里只做“能否解析”的检查，不保存 datetime 对象，数据库里仍存原字符串。
from datetime import datetime
# Any 表示“任意类型”。teacher/student/user 参数实际是 security.Principal
# 数据类实例，但本模块不导入该类（避免循环依赖），因此用鸭子类型 + Any。
from typing import Any

# 相对导入（点号表示当前 app 包）：复用答题会话引擎。
# 题目顺序、作答、评分、报告都由 sessions.py 维护，本文件只做作业层编排。
from . import sessions
# 相对导入数据层的六个工具：
#   db()        —— 上下文管理器：进入时建连接，正常退出 commit，异常 rollback
#   dump()      —— 把 Python 对象序列化成 JSON 字符串；本文件当前未直接调用
#                  （删除它会改变源文件的导入清单，因此原样保留）
#   fetch_all() —— 查询多行，返回 list[dict]
#   fetch_one() —— 查询单行，返回 dict 或 None
#   iso()       —— 生成带时区的 ISO 时间字符串，统一写入数据库
#   load_json() —— 把数据库里的 JSON 文本安全地转回 dict/list
#   new_id()    —— 生成带业务前缀的短唯一 ID
from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id

# =============================================================================
# 常量：作业状态 -> 中文展示名
# =============================================================================
# 数据库里存英文枚举，前端只负责展示；翻译集中在这里，改文案只需改一处。
# 状态正常只向前流转，不回退；submit/review 重复调用也不会重复写数据。
ASSIGNMENT_STATUS = {
    # 教师已布置，学生尚未点“开始作答”。
    "assigned": "待开始",
    # 学生已创建答题会话，正在做题。
    "in_progress": "进行中",
    # 学生已交卷，报告已生成，等待教师复盘。
    "submitted": "待复盘",
    # 教师已写评语/确认分数，流程结束。
    "reviewed": "已复盘",
}


# =============================================================================
# 业务异常
# =============================================================================
# 自定义异常：把“HTTP 状态码 + 机器可读错误码 + 中文提示 + 附加字段”打包。
# 服务层不依赖 FastAPI，所以不直接抛 HTTPException；由路由层统一翻译。
class AssignmentError(Exception):
    # 构造函数参数：
    #   status  —— HTTP 状态码，供路由层直接使用（404/403/409/422 等）
    #   code    —— 稳定错误码，前端据此分支判断（不要用 message 判断）
    #   message —— 给用户看的中文说明
    #   details —— 额外结构化信息（如 document_id），默认为 None
    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        # 保存状态码，路由层读取 exc.status 作为 HTTP status_code。
        self.status = status
        # 保存错误码，例如 ASSIGNMENT_NOT_FOUND。
        self.code = code
        # 保存可展示的中文消息。
        self.message = message
        # details or {}：None 时退化为空字典，保证后续 **exc.details 展开安全。
        self.details = details or {}
        # 调用父类 Exception 构造，让 str(exc) 等于 message，日志更友好。
        super().__init__(message)


# =============================================================================
# 内部查询与权限助手
# =============================================================================
# 按 user_id 取用户；取不到就抛 404。返回整行 dict（内部使用）。
def _user(user_id: str) -> dict:
    # 参数化查询：? 占位符由数据库驱动转义，避免 SQL 注入；params 用元组。
    row = fetch_one("SELECT * FROM app_user WHERE user_id=?", (user_id,))
    # fetch_one 找不到时返回 None，这里显式转成业务异常。
    if not row:
        # 错误码 USER_NOT_FOUND 供前端提示“用户不存在”。
        raise AssignmentError(404, "USER_NOT_FOUND", "用户不存在")
    # 返回数据库原始行（含 password_hash 等字段，仅内部使用，不直接外泄）。
    return row


# 按主键读取作业行，返回 paper_assignment 的整行。
# 所有状态流转都先经过这里，保证“作业不存在”只有一种报错方式。
def _assignment_row(assignment_id: str) -> dict:
    # assignment_id 是主键，最多返回一行。
    row = fetch_one("SELECT * FROM paper_assignment WHERE assignment_id=?", (assignment_id,))
    # 查不到说明 ID 不存在或已被清理。
    if not row:
        # 404 + 稳定错误码，路由层会原样带给前端。
        raise AssignmentError(404, "ASSIGNMENT_NOT_FOUND", "作业不存在")
    # 返回整行，后续用 row[“字段名”] 读取必填列。
    return row


# 按 set_id 读取题卷（question_set 表）。
# 表名虽然叫 question_set，业务上把它当作“题卷/试卷”。
def _paper(paper_id: str) -> dict:
    # set_id 是题卷主键。
    row = fetch_one("SELECT * FROM question_set WHERE set_id=?", (paper_id,))
    # 题卷被删除或 ID 写错时统一报 404。
    if not row:
        # 错误码与前端“题卷不存在”的提示对应。
        raise AssignmentError(404, "PAPER_NOT_FOUND", "题卷不存在")
    # 返回题卷整行（含 question_ids、question_count、tenant_id）。
    return row


# 判定“管理权”：管理员全量放行；教师仅限自己布置的作业。
# row 必须来自 paper_assignment，且包含 teacher_id 列。
def _can_manage(row: dict, user: Any) -> bool:
    # 短路求值：role 为 admin 时不再比较 teacher_id，减少一次访问。
    return user.role == "admin" or row["teacher_id"] == user.user_id


# 判定“查看权”：在管理权基础上，增加“被布置的学生本人”。
# 学生只能看自己的作业，不能看同班其他人的。
def _can_view(row: dict, user: Any) -> bool:
    # 复用 _can_manage，避免重复写 admin/teacher 判断。
    return _can_manage(row, user) or row["assignee_id"] == user.user_id


# =============================================================================
# 序列化：把数据库行拼成前端可直接使用的 JSON 结构
# =============================================================================
# _api() 是“作业详情”的统一出口：
#   row                —— paper_assignment 的一行
#   include_questions  —— 是否附带题卷中的题目（列表页不需要，详情页需要）
#   include_turns      —— 是否附带作答记录、追问与报告（仅教师/已交卷学生可见）
# 注意：星号让参数变成 keyword-only，调用时必须写 include_questions=True，
# 防止两个布尔值按位置传反而导致“该给的没给”。
def _api(row: dict, *, include_questions: bool = False, include_turns: bool = False) -> dict:
    # 查题卷；理论上一定存在，用 or {} 兜底，避免历史脏数据导致 KeyError。
    paper = fetch_one("SELECT * FROM question_set WHERE set_id=?", (row["paper_id"],)) or {}
    # 只取 title/page_count 两列；论文即使被软删除，也允许回看历史作业。
    doc = fetch_one("SELECT title, page_count FROM document WHERE doc_id=?", (row["doc_id"],)) or {}
    # 学生信息只暴露 username/display_name，绝不返回 password_hash 等敏感列。
    assignee = fetch_one("SELECT username, display_name FROM app_user WHERE user_id=?", (row["assignee_id"],)) or {}
    # 教师信息同理，仅用于展示“谁布置的”。
    teacher = fetch_one("SELECT username, display_name FROM app_user WHERE user_id=?", (row["teacher_id"],)) or {}
    # question_ids 在库里是 JSON 文本（如 [“q_1”,“q_2”]），解析失败回退空列表。
    question_ids = load_json(paper.get("question_ids"), [])
    # 组装对外的扁平结构；键名统一 snake_case，与接口文档保持一致。
    out = {
        # 作业主键（asg_ 前缀）。
        "assignment_id": row["assignment_id"],
        # 题卷主键（set_ 前缀）。
        "paper_id": row["paper_id"],
        # 标题优先级：作业自定义标题 > 题卷标题 > 兜底文案。
        "title": row.get("title") or paper.get("title") or "未命名题卷",
        # 关联论文 ID。
        "document_id": row["doc_id"],
        # 论文标题；论文缺失时给空串，前端显示“未知文档”。
        "document_title": doc.get("title") or "",
        # 页数，用于展示题卷规模；None 时归零。
        "page_count": doc.get("page_count") or 0,
        # 题目数量优先用题卷表的冗余字段；没有就数 question_ids 的长度。
        "question_count": int(paper.get("question_count") or len(question_ids)),
        # 学生对象：user_id 必填，username/display_name 多级兜底。
        "assignee": {"user_id": row["assignee_id"], "username": assignee.get("username", ""),
        # display_name 为空时退回 username，保证界面不出现空白。
                     "display_name": assignee.get("display_name") or assignee.get("username", "")},
        # 教师对象，结构与学生一致，前端可复用同一套渲染逻辑。
        "teacher": {"user_id": row["teacher_id"], "username": teacher.get("username", ""),
        # 教师显示名兜底。
                    "display_name": teacher.get("display_name") or teacher.get("username", "")},
        # 教师留言/作答说明，默认空串。
        "instructions": row.get("instructions") or "",
        # 截止时间（ISO 字符串或 None）。
        "due_at": row.get("due_at"),
        # 状态；历史数据可能为空，统一按 assigned 处理。
        "status": row.get("status") or "assigned",
        # 中文状态名；遇到未知状态时原样返回，方便排查。
        "status_label": ASSIGNMENT_STATUS.get(row.get("status") or "assigned", row.get("status")),
        # 关联的答题会话 ID；未开始为 None。
        "session_id": row.get("session_id"),
        # 得分；未评分/未生成报告为 None（0 分是合法值，不能用 or 兜底）。
        "score": row.get("score"),
        # 关联报告 ID。
        "report_id": row.get("report_id"),
        # 教师评语，默认空串。
        "teacher_comment": row.get("teacher_comment") or "",
        # 创建时间。
        "created_at": row.get("created_at"),
        # 开始作答时间。
        "started_at": row.get("started_at"),
        # 交卷时间。
        "submitted_at": row.get("submitted_at"),
        # 复盘完成时间。
        "reviewed_at": row.get("reviewed_at"),
    # 基础字段组装完成；下面按需追加可选字段。
    }
    # 可选：附带题目列表（详情页、开始作答时使用）。
    if include_questions:
        # 只有题卷里确实有题目才查库；空列表直接返回 []，少一次 SQL。
        if question_ids:
            # 动态生成与题目数量相同的 ? 占位符，例如 “?,?,?”。
            marks = ",".join("?" for _ in question_ids)
            # IN (...) 一次取回所有题目；marks 只由 ? 组成，不含用户输入，因此拼接安全。
            # 只 SELECT 列表展示需要的列，避免把大字段（答案、rubric）也读出来。
            rows = fetch_all(f"SELECT q_id, q_type, stem, difficulty, bloom_level, node_id FROM question WHERE q_id IN ({marks})", question_ids)
            # 以 q_id 建索引：IN 查询不保证返回顺序，需要按题卷原顺序重排。
            by_id = {q["q_id"]: q for q in rows}
            # 按 question_ids 顺序输出，并过滤掉已删除/不存在的题目。
            out["questions"] = [by_id[qid] for qid in question_ids if qid in by_id]
        # 题卷为空时的分支。
        else:
            # 返回空列表而不是省略字段，前端无需判空。
            out["questions"] = []
    # 可选：附带作答轨迹；必须已有会话，否则没有 turn 可查。
    if include_turns and row.get("session_id"):
        # 按 seq_no 升序，保证第 1 题、第 2 题……的展示顺序。
        turns = fetch_all("SELECT * FROM turn WHERE session_id=? ORDER BY seq_no", (row["session_id"],))
        # 取出本次会话涉及的所有题目 ID（可能重复，后面用 dict 去重）。
        qids = [t["q_id"] for t in turns]
        # 题目缓存：q_id -> 题目行，避免在循环里逐题查询（N+1 问题）。
        qmap = {}
        # 没有作答记录时保持空字典。
        if qids:
            # 同样动态生成占位符。
            marks = ",".join("?" for _ in qids)
            # 一次性批量查询题目，并用字典推导建索引。
            qmap = {q["q_id"]: q for q in fetch_all(f"SELECT * FROM question WHERE q_id IN ({marks})", qids)}
        # 最终返回给前端的 turn 列表。
        items = []
        # 逐条组装作答记录。
        for t in turns:
            # 题目可能已被删除，用 {} 兜底，下面统一 q.get(...) 安全取值。
            q = qmap.get(t["q_id"], {})
            # 单条记录 = 作答本身 + 评分 + 题目原文，方便复盘页左右对照。
            items.append({
                # turn 基础字段：主键、序号、状态（asking/answered/skipped）。
                "turn_id": t["turn_id"], "seq_no": t["seq_no"], "status": t["status"],
                # answer/evaluation 在库里是 JSON 文本，这里还原成对象。
                "score": t["score"], "answer": load_json(t.get("answer"), {}),
                # evaluation 含得分点、反馈等结构化评分结果。
                "evaluation": load_json(t.get("evaluation"), {}),
                # 嵌套题目信息：只暴露作答/复盘需要的字段。
                "question": {"q_id": q.get("q_id"), "q_type": q.get("q_type"), "stem": q.get("stem"),
                # 标准答案（JSON）；q 为空时给 {}。
                             "answer": load_json(q.get("answer"), {}) if q else {},
                # 评分量规（rubric），通常是列表。
                             "rubric": load_json(q.get("rubric"), []) if q else [],
                # 证据/原文定位，供“回看原文”使用。
                             "evidence": load_json(q.get("evidence"), []) if q else []},
            # 结束单条记录。
            })
        # 挂到主结构上；没有会话时该键完全不存在。
        out["turns"] = items
        # 追问记录由 sessions 模块统一序列化（内部会解析 hit/miss 两个 JSON 列）。
        out["follow_ups"] = sessions.list_follow_ups(row["session_id"])
        # 报告按 session_id 查询；一次会话最多一份报告。
        report = fetch_one("SELECT * FROM report WHERE session_id=?", (row["session_id"],))
        # 报告可能尚未生成（刚交卷/异步任务失败），此时不返回 report 键。
        if report:
            # 只挑前端展示需要的四个字段，避免泄露报告表的内部列。
            out["report"] = {
                # 报告主键。
                "report_id": report["report_id"],
                # 总评：分数、正确率、用时等汇总指标。
                "summary": load_json(report.get("summary"), {}),
                # 分章节掌握度列表。
                "section_mastery": load_json(report.get("section_mastery"), []),
                # 学习建议列表。
                "recommendations": load_json(report.get("recommendations"), []),
            # 报告字段组装完成。
            }
    # 返回组装好的字典（按需包含 questions/turns/follow_ups/report）。
    return out


# =============================================================================
# 写操作：布置作业
# =============================================================================
# 教师把一份题卷布置给一名学生。
# 入参：
#   teacher   —— 当前登录教师 Principal（路由层已做 require_teacher 鉴权）
#   tenant_id —— 租户 ID，用于多租户隔离
#   payload   —— 前端 JSON：paper_id/set_id、assignee_id/assignee_username、
#                title、instructions、due_at
# 返回：新作业的序列化详情（含题目列表）。
# 可能抛出：404 PAPER_NOT_FOUND / USER_NOT_FOUND / DOC_NOT_FOUND，
#          422 ASSIGNEE_REQUIRED / INVALID_DUE_AT，409 ASSIGNEE_DISABLED。
def create_assignment(teacher: Any, tenant_id: str, payload: dict) -> dict:
    # 兼容两种字段名：新前端传 paper_id，旧调用方可能传 set_id；
    # str(... or “”) 统一成字符串，None 也不会变成 “None”。
    paper_id = str(payload.get("paper_id") or payload.get("set_id") or "")
    # 题卷不存在会在这里直接抛 404 PAPER_NOT_FOUND。
    paper = _paper(paper_id)
    # 多租户隔离：A 租户不能给 B 租户的题卷布置作业。
    if paper.get("tenant_id") != tenant_id:
        # 故意返回 404 而不是 403，避免攻击者通过状态码探测其他租户的题卷。
        raise AssignmentError(404, "PAPER_NOT_FOUND", "题卷不存在")
    # 优先用 assignee_id；没有则尝试用用户名解析。
    assignee_id = str(payload.get("assignee_id") or "")
    # 兼容“按用户名布置”的表单；用户名可能带首尾空格。
    if not assignee_id and payload.get("assignee_username"):
        # 按用户名精确查找，找不到时 user 为 None。
        # 注意：源文件这里没有附加 tenant_id 条件，实际部署需保证用户名全局唯一。
        user = fetch_one("SELECT * FROM app_user WHERE username=?", (str(payload["assignee_username"]).strip(),))
        # 三元表达式：找到就取 user_id，否则保持空串，交给下面统一报错。
        assignee_id = user["user_id"] if user else ""
    # 两种方式都没定位到学生 -> 422（请求语义错误，而不是资源不存在）。
    if not assignee_id:
        # ASSIGNEE_REQUIRED 让前端知道要补选学生。
        raise AssignmentError(422, "ASSIGNEE_REQUIRED", "请选择学生")
    # 读取学生整行，顺便校验存在性（不存在抛 404 USER_NOT_FOUND）。
    assignee = _user(assignee_id)
    # 只有 active 账号才能接收新作业，禁用账号不能继续派题。
    if assignee.get("status") != "active":
        # 409 Conflict：资源当前状态不允许该操作。
        raise AssignmentError(409, "ASSIGNEE_DISABLED", "该学生账号已被禁用")
    # 校验论文未软删除：deleted_at IS NULL 表示仍有效。
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (paper["doc_id"],))
    # 论文被删除时不允许再布置，防止学生打开空白题卷。
    if not doc:
        # 错误码区分“题卷不存在”和“论文不存在”，方便教师排查。
        raise AssignmentError(404, "DOC_NOT_FOUND", "题卷关联论文不存在")
    # 截止时间：去空格；空串转 None（数据库用 NULL 表示未设置）。
    due_at = str(payload.get("due_at") or "").strip() or None
    # 只有传了截止时间才校验格式。
    if due_at:
        # try 包住解析：fromisoformat 对格式错误会抛 ValueError。
        try:
            # 前端常用 2026-01-01T10:00:00Z；Python 3.10 及更早不认结尾的 Z，
            # 先替换成 +00:00 再解析，兼容 UTC 写法。
            datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        # 捕获所有解析异常（ValueError/TypeError 等），统一转成 422。
        except Exception:
            # 这里没有 from exc，但 Python 会自动保留异常链，日志仍能看到根因。
            raise AssignmentError(422, "INVALID_DUE_AT", "截止时间格式不正确，请使用 ISO 时间")
    # 生成作业主键，形如 asg_1a2b3c4d5e（new_id 内部取 uuid4 前十位）。
    assignment_id = new_id("asg")
    # 标题优先级：自定义标题 > 题卷标题 > 默认“课堂题卷”。
    title = str(payload.get("title") or paper.get("title") or "课堂题卷")
    # 说明文字截断到 2000 字符，防止超长文本撑爆数据库/接口。
    instructions = str(payload.get("instructions") or "")[:2000]
    # with 打开连接：正常结束自动 commit，异常自动 rollback 并关闭。
    with db() as conn:
        # 显式列出列名，避免表结构新增字段时 INSERT 值错位。
        conn.execute(
            # SQL 文本分两段写（Python 相邻字符串自动拼接），仍是一条 INSERT。
            "INSERT INTO paper_assignment (assignment_id, paper_id, doc_id, teacher_id, tenant_id, title,"
            # 12 个 ? 对应 12 个列，顺序必须与下面的参数元组完全一致。
            " assignee_id, assignee_name, instructions, due_at, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            # 参数元组：teacher.user_id 来自登录态，tenant_id 来自路由层，均不信任前端。
            (assignment_id, paper_id, paper["doc_id"], teacher.user_id, tenant_id, title,
            # assignee_name 冗余保存显示名，列表页即使不 JOIN 也能显示学生姓名。
             assignee_id, assignee.get("display_name") or assignee.get("username") or "", instructions,
            # 初始状态固定 assigned；iso() 生成服务器当前时间（东八区）。
             due_at, "assigned", iso()),
        # 结束 execute 调用；离开 with 块时自动 commit。
        )
    # 重新从库里读一遍再序列化：确保返回数据库真实值（含默认值/时间格式）。
    return _api(_assignment_row(assignment_id), include_questions=True)


# =============================================================================
# 读操作：作业列表
# =============================================================================
# 按角色自动收窄可见范围：
#   · 学生（role=user）或 mine=True：只看分配给自己的
#   · 教师（role=teacher）：只看自己布置的
#   · 管理员：看全部
# 参数：
#   mine   —— 前端“我的作业”开关，对学生以外的角色也生效
#   status —— 可选状态过滤（assigned/in_progress/submitted/reviewed）
#   limit  —— 返回条数，最终夹在 1..200
# 返回：作业摘要列表（不含题目和作答明细，避免列表接口过重）。
def list_assignments(user: Any, *, mine: bool = False, status: str = "", limit: int = 50) -> list[dict]:
    # 用括号把多段 SQL 拼起来；先写基础查询。
    sql = (
        # JOIN document 只为过滤软删除论文：论文进回收站后，其作业不再出现。
        "SELECT a.* FROM paper_assignment a JOIN document d ON d.doc_id=a.doc_id"
        # d.deleted_at IS NULL 是软删除约定：NULL 表示未删除。
        " WHERE d.deleted_at IS NULL"
    )
    # 参数列表与 SQL 中的 ? 一一对应，顺序不能乱；
    # 明确标注 list，后面只会 append str/int。
    params: list = []
    # 学生天然只能看自己的；mine=True 时管理员/教师也切换成“只看自己”。
    if mine or user.role == "user":
        # 追加学生过滤条件。
        sql += " AND a.assignee_id=?"
        # 同步追加参数；用当前登录用户，不接受前端指定。
        params.append(user.user_id)
    # 教师默认只看自己布置的作业。
    elif user.role == "teacher":
        # teacher_id 来自登录态。
        sql += " AND a.teacher_id=?"
        # 同步追加参数。
        params.append(user.user_id)
    # 可选的状态过滤；空串表示不过滤。
    if status:
        # 状态值作为参数传入，不拼进 SQL 字符串。
        sql += " AND a.status=?"
        # 同步追加参数。
        params.append(status)
    # 统一按创建时间倒序，最新的作业排前面。
    sql += " ORDER BY a.created_at DESC LIMIT ?"
    # 限制条数：先 int 转换，再夹到 [1,200]，防止 0/负数/超大值。
    params.append(max(1, min(200, int(limit or 50))))
    # 执行查询，返回 list[dict]。
    rows = fetch_all(sql, params)
    # 逐行序列化；列表页不加载题目和作答，减少查询次数。
    return [_api(r) for r in rows]


# 读取单个作业详情。
# 权限：管理者或学生本人；其他人 403。
# include_turns 的控制很关键：
#   · 教师/管理员随时可以查看作答轨迹；
#   · 学生只有 submitted/reviewed 之后才能看，防止提前看到答案与评分。
def get_assignment(assignment_id: str, user: Any) -> dict:
    # 先取作业行（不存在会抛 404）。
    row = _assignment_row(assignment_id)
    # 查看权校验：未通过直接 403，不返回任何作业内容。
    if not _can_view(row, user):
        # 403：当前用户既不是管理者，也不是被布置的学生。
        raise AssignmentError(403, "FORBIDDEN", "无权查看该作业")
    # 管理者随时可看轨迹；
    # 学生只有 submitted/reviewed 之后才附带，防止提前看答案与评分。
    include_turns = _can_manage(row, user) or row.get("status") in ("submitted", "reviewed")
    # 详情页始终附带题目；轨迹按权限决定。
    return _api(row, include_questions=True, include_turns=include_turns)


# =============================================================================
# 状态流转：开始作答
# =============================================================================
# 学生点击“开始作答”时调用：
#   1. 校验只有被布置者（或管理员/教师）能开始；
#   2. 若已存在 session（重复点击），直接返回原会话，保证幂等；
#   3. 否则按题卷中的题目顺序创建 exam 模式的答题会话；
#   4. 把 session_id 写回作业，状态改为 in_progress。
# 返回：{“assignment”: 作业详情, “session”: 会话详情}。
def start_assignment(assignment_id: str, student: Any) -> dict:
    # 读取作业行。
    row = _assignment_row(assignment_id)
    # 允许被布置的学生本人；管理员/教师可代为开始（例如课堂统一开卷）。
    if row["assignee_id"] != student.user_id and student.role not in ("admin", "teacher"):
        # 403 而不是 404：作业存在，但当前用户无权操作。
        raise AssignmentError(403, "FORBIDDEN", "只能开始布置给自己的题卷")
    # 已有 session_id 说明之前开过卷，走幂等分支。
    if row.get("session_id"):
        # 再查一次会话，确认没有被清理/删除。
        session = fetch_one("SELECT * FROM session WHERE session_id=?", (row["session_id"],))
        # 会话还在：直接返回，不重复创建（避免重复记录/重复计时）。
        if session:
            # 返回结构与新建分支保持一致，前端无需区分首次/重复点击。
            return {"assignment": _api(_assignment_row(assignment_id)), "session": sessions.get_session(row["session_id"])}
    # 读取题卷，拿到题目 ID 列表。
    paper = _paper(row["paper_id"])
    # question_ids 是 JSON 文本，解析失败给空列表。
    question_ids = load_json(paper.get("question_ids"), [])
    # 空题卷无法开考。
    if not question_ids:
        # 409：题卷状态不满足开考条件。
        raise AssignmentError(409, "QUESTION_SET_EMPTY", "题卷没有可用题目")
    # 调用 sessions 服务创建会话；传 question_ids 表示严格按题卷顺序组卷。
    session = sessions.create_session(
        # doc_id 来自作业行（布置时已校验），user_id 用当前学生，保持租户隔离。
        doc_id=row["doc_id"], user_id=student.user_id, tenant_id=row["tenant_id"],
        # mode=exam 表示考试模式（与 guided 引导模式不同）；
        # question_count 直接取题卷长度，保证“布置多少题就考多少题”。
        mode="exam", question_count=len(question_ids), question_ids=question_ids,
    )
    # 事务：两条 UPDATE 必须一起成功，否则作业与会话状态会不一致。
    with db() as conn:
        # 反向关联：session.assignment_id 让“按会话查作业”的接口可用。
        conn.execute("UPDATE session SET assignment_id=? WHERE session_id=?", (assignment_id, session["session_id"]))
        # 更新作业：记录 session_id 与开始时间。
        conn.execute(
            # status 改为 in_progress，前端据此显示“进行中”。
            "UPDATE paper_assignment SET status='in_progress', session_id=?, started_at=? WHERE assignment_id=?",
            # 参数顺序与 SQL 占位符一致。
            (session["session_id"], iso(), assignment_id),
        # 结束 execute 调用；离开 with 块时自动 commit。
        )
    # create_session 返回值里没有 assignment_id，这里补上再返回。
    session["assignment_id"] = assignment_id
    # 重新读取作业，保证 status/started_at 是最新值。
    return {"assignment": _api(_assignment_row(assignment_id)), "session": session}


# =============================================================================
# 状态流转：提交作业
# =============================================================================
# 学生交卷；教师/管理员可代为提交。
# 幂等：已 submitted/reviewed 时直接返回当前详情，不重复生成报告。
# 内部动作：
#   1. sessions.finish_session 结算本次会话并生成报告；
#   2. 读取会话得分，写回作业表；
#   3. 状态改为 submitted（待复盘）。
# 返回：含题目、作答轨迹、报告的完整详情。
def submit_assignment(assignment_id: str, user: Any, *, reason: str = "student_submit") -> dict:
    # 读取作业行。
    row = _assignment_row(assignment_id)
    # 只有被布置者本人可以提交；管理员/教师可代交。
    if row["assignee_id"] != user.user_id and user.role not in ("admin", "teacher"):
        # 403：不是自己的作业。
        raise AssignmentError(403, "FORBIDDEN", "只能提交自己的题卷")
    # 幂等分支：重复点击“交卷”不应报错，也不应重复生成报告。
    if row["status"] in ("submitted", "reviewed"):
        # 直接返回最新详情；include_turns=True 让前端立即看到报告。
        return _api(row, include_questions=True, include_turns=True)
    # 没有 session 说明还没开始作答。
    if not row.get("session_id"):
        # 409：状态冲突，提示先开始。
        raise AssignmentError(409, "ASSIGNMENT_NOT_STARTED", "请先开始作答")
    # 结算会话：内部会生成 report、更新 session.state/score，
    # 并回调 on_session_finished（那里也会尝试写一次作业状态）。
    result = sessions.finish_session(row["session_id"], row["tenant_id"], reason=reason)
    # 单独事务更新作业表；即使回调失败，这里也能兜底写状态。
    with db() as conn:
        # 一条 UPDATE 同时写状态、交卷时间、分数、报告 ID。
        conn.execute(
            # score 用 _session_score 计算（优先 session.score，其次报告总分）。
            "UPDATE paper_assignment SET status='submitted', submitted_at=?, score=?, report_id=? WHERE assignment_id=?",
            # 参数顺序：时间、分数、报告 ID、作业 ID；
            # report_id 来自 finish_session 的返回值。
            (iso(), _session_score(row["session_id"]), result.get("report_id"), assignment_id),
        )
    # 离开 with 块时自动 commit；返回完整详情，前端可直接跳转报告页。
    return _api(_assignment_row(assignment_id), include_questions=True, include_turns=True)


# =============================================================================
# 状态流转：教师复盘
# =============================================================================
# 教师确认分数并写评语，把作业从 submitted 推到 reviewed。
# 注意：源码没有强制校验前置状态，教师端通常只在 submitted 时展示复盘入口。
# 分数优先级：payload.score > 作业表已有 score > 会话/报告得分。
# 评语字段兼容 comment 与 teacher_comment 两种命名。
# 返回：含题目、作答轨迹、报告的完整详情。
def review_assignment(assignment_id: str, teacher: Any, payload: dict) -> dict:
    # 读取作业行。
    row = _assignment_row(assignment_id)
    # 只有管理员或布置该作业的教师可以复盘。
    if not _can_manage(row, teacher):
        # 403：不能复盘别人布置的作业。
        raise AssignmentError(403, "FORBIDDEN", "只能复盘自己布置的题卷")
    # 兼容两种字段名；strip 去首尾空白；截断到 3000 字符。
    comment = str(payload.get("comment") or payload.get("teacher_comment") or "").strip()[:3000]
    # 教师可以不传分数，沿用系统自动评分。
    score = payload.get("score")
    # 未传分数时的兜底逻辑。
    if score is None:
        # 作业表已有分数（例如提交时写入）则沿用；用 is not None 而不是 or，
        # 避免 0 分被当成“没有分数”。连作业分数都没有，就回查会话/报告；
        # session_id 可能为 None，用空串兜底。
        score = row.get("score") if row.get("score") is not None else _session_score(row.get("session_id") or "")
    # try 包住 float 转换：score 可能来自 JSON 的字符串（如 “85”）。
    try:
        # 能转就转成 float；None 保持 None（表示不记分）。
        score = float(score) if score is not None else None
    # 捕获 ValueError/TypeError 等所有转换异常。
    except Exception:
        # 转换失败（如 “abc”）时不报错，降级为 None。
        score = None
    # 事务：状态、评语、分数、复盘时间一次写入。
    with db() as conn:
        # UPDATE 只影响一行（assignment_id 是主键）。
        conn.execute(
            # 状态固定 reviewed；reviewed_at 记录复盘时间。
            "UPDATE paper_assignment SET status='reviewed', teacher_comment=?, score=?, reviewed_at=? WHERE assignment_id=?",
            # 参数顺序与 SQL 一致。
            (comment, score, iso(), assignment_id),
        # 结束 execute 调用；离开 with 块时自动 commit。
        )
    # 返回最新详情（含报告），前端刷新复盘页。
    return _api(_assignment_row(assignment_id), include_questions=True, include_turns=True)


# =============================================================================
# 会话结束回调
# =============================================================================
# 由 sessions._finish_internal() 在报告生成后调用（见 sessions.py）。
# 作用：把“答题会话结束”同步成“作业已提交”，保证两条状态链一致。
# 为什么需要回调：学生也可能从“自由练习”入口结束会话，那条路径不经过
# submit_assignment()，只能靠会话层通知作业层。
# 注意：sessions 侧用 try/except 包住本函数，回调失败不影响报告生成。
def on_session_finished(session_id: str, report: dict) -> None:
    # 一个会话最多对应一份作业；普通练习会话查不到，返回 None。
    row = fetch_one("SELECT * FROM paper_assignment WHERE session_id=?", (session_id,))
    # 不是作业会话：什么也不做，直接返回（None 表示无返回值）。
    if not row:
        # 直接返回 None；调用方（sessions）会忽略这次回调。
        return
    # 事务更新作业状态。
    with db() as conn:
        # 执行 UPDATE；作业行一定存在，不需要检查受影响行数。
        conn.execute(
            # COALESCE(submitted_at, ?) 保留最早的提交时间，重复回调不会覆盖。
            "UPDATE paper_assignment SET status='submitted', submitted_at=COALESCE(submitted_at, ?),"
            # 第二段 SQL 接着写 score/report_id（相邻字符串自动拼接）。
            " score=?, report_id=? WHERE assignment_id=?",
            # 参数：当前时间、报告总分、报告 ID、作业 ID；summary 缺字段时取 None。
            (iso(), report.get("summary", {}).get("score"), report.get("report_id"), row["assignment_id"]),
        # 结束 execute；with 块退出时自动 commit。
        )


# 读取一次会话的最终得分，供提交/复盘兜底。
# 取值顺序：
#   1. session.score（会话结束时由 sessions._finish_internal 写入）
#   2. report.summary.score（历史数据或会话分数尚未回填时）
#   3. None（确实没有分数）
def _session_score(session_id: str) -> float | None:
    # 没有 session_id（未开始作答）直接返回 None，避免无意义查询。
    if not session_id:
        # 返回 None，表示没有可用的会话分数。
        return None
    # 只查 score 一列，减少 IO。
    row = fetch_one("SELECT score FROM session WHERE session_id=?", (session_id,))
    # 0.0 是合法分数，必须用 is not None 判断。
    if row and row.get("score") is not None:
        # 直接返回会话分数。
        return row["score"]
    # 回退到报告表。
    report = fetch_one("SELECT summary FROM report WHERE session_id=?", (session_id,))
    # 报告存在时解析 summary JSON。
    if report:
        # summary 里没有 score 时返回 None。
        return load_json(report.get("summary"), {}).get("score")
    # 两条路都没有结果，返回 None 表示“暂无分数”。
    return None
