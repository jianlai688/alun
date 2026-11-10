# =============================================================================
# 文件：app/admin.py —— 后台管理 API
# =============================================================================
# 【这个文件做什么】
#   它定义了 A论（论文导读智能体）管理后台所需的全部管理接口，包括：
#     1) 首页总览：用户数、文档数、会话数、答题数、准确率、token 消耗、14 天趋势；
#     2) 文档管理：列表查询（关键词/状态过滤）与删除（软删除 + 物理清理）；
#     3) 用户管理：用户列表与用量统计、启用/禁用账号；
#     4) 运营观测：活动事件流、最近 5 分钟在线用户、AI 调用审计日志；
#     5) 系统设置：读取/保存运行时配置、测试模型网关连通性、token/积分用量报表。
#
# 【在整个应用中的位置】
#   - 后端是 FastAPI 应用：app/main.py 创建 app，并通过
#     `app.include_router(admin_api.router)` 把本文件的路由挂载进去；
#   - 本文件里的 router 声明了统一前缀 /api/v1/admin，因此下面每个 @router.xxx
#     最终对应的真实 URL 都是 /api/v1/admin/...；
#   - 浏览器端由 web/admin.html（main.py 的 /admin 路由负责返回）调用这些 JSON 接口；
#   - 本文件不直接操作 SQL 连接，而是复用 db.py 的 fetch_one/fetch_all/db()，
#     因此同一套代码既能跑 SQLite（默认），也能跑 PostgreSQL（平台模式）。
#
# 【主要调用关系（一次请求的旅程）】
#   管理台 fetch("/api/v1/admin/overview", {Authorization: "Bearer ..."})
#     -> FastAPI 匹配到 overview()
#     -> 先执行依赖 admin_principal(request)
#          -> security.principal_from_request() 解析 JWT / 请求头，得到 Principal
#          -> security.require_admin() 校验 role == "admin"，否则抛 403
#     -> 进入 overview() 函数体
#          -> fetch_one/fetch_all 查询 document、app_user、session、turn、usage_log 等表
#          -> audit.recent_events() 取最近活动
#          -> settings.get()/get_bool() 取当前模型配置
#     -> FastAPI 把返回的 dict 序列化成 JSON 响应
#
# 【依赖的兄弟模块】
#   audit.py    —— 活动事件与 AI 输出审计（activity_event / ai_audit 两张表）
#   llm.py      —— 大模型网关客户端，本文件只用它的 health() 做连通性探测
#   settings.py —— 运行时可改配置（app_setting 表 + 代码内 DEFAULTS 兜底）
#   config.py   —— 全局配置常量（这里只用 UPLOAD_DIR 上传目录）
#   db.py       —— 数据库连接与查询助手（含 SQLite/PostgreSQL 兼容层）
#   security.py —— 身份模型 Principal、请求解析、管理员权限校验
#
# 【涉及的数据表】
#   app_user        用户账号（user_id/username/role/status/tenant_id...）
#   document        文档主表（软删除字段 deleted_at；quality_* 为质量评估结果）
#   session         导读会话（通过 doc_id 关联文档）
#   turn            问答轮次（status='answered' 表示完成；score 为百分制得分）
#   usage_log       每次模型调用的 token/积分流水（prompt_tokens/completion_tokens/credits）
#   activity_event  用户行为事件（登录、上传、删除等，ts 为 ISO 时间字符串）
#   ai_audit        AI 输出审计（grounded 表示是否有依据，fallback 表示是否降级兜底）
#   app_setting     键值型运行时配置
#
# 【错误与返回约定】
#   - 业务错误统一抛 HTTPException，detail 形如 {"code": "XXX", "message": "中文提示"}，
#     前端可以按 code 分支处理，不必解析中文；
#   - 成功时直接返回 dict，FastAPI 自动转 JSON；列表接口统一包一层 {"items": [...]}；
#   - 权限失败由依赖统一处理：401（未登录/令牌无效）或 403（非管理员）。
#
# 【阅读提示】
#   - 本文件是“逐行学习注释版”：所有 # 注释均为后加，代码本身与原文件逐字一致；
#   - 所有接口都用同步 def 定义；FastAPI 会把同步端点放进线程池执行，
#     因此这里可以直接调用同步数据库驱动，不会阻塞事件循环；
#   - SQL 中的 ? 是参数占位符，db.py 会在 PostgreSQL 模式下自动翻译成 %s；
#   - 时间在库中以 ISO 字符串保存，substr(ts,1,10) 是跨数据库取日期部分的常用技巧；
#   - 统计接口大量使用 COALESCE(...,0)，是为了让“没有数据”时也返回 0 而不是 NULL。
# =============================================================================
"""后台管理 API：总览、用户、实时事件、模型设置与 token 用量。"""
# 导入“延迟注解”特性：类型注解以字符串形式保存、不在运行时求值。
# 好处：可以使用 tuple[int, float] 这类新语法，也能避免类型引用造成的循环导入。
from __future__ import annotations

# 从标准库导入 defaultdict（访问不存在的键时自动创建默认值的字典）。
# 说明：本文件后续并未使用它，属于历史遗留导入；按“只加注释、不改代码”的要求原样保留。
from collections import defaultdict
# datetime：日期时间类型（本文件未直接使用，保留原导入）。
# timedelta：时间间隔，用来计算“5 分钟前”“13 天前”等相对时间。
from datetime import datetime, timedelta

# 从 FastAPI 导入本文件需要的构件：
#   APIRouter     —— 路由分组器，统一挂前缀与文档标签；
#   Depends       —— 依赖注入，让框架在进入接口前先执行鉴权函数；
#   HTTPException —— 抛出带状态码与 JSON detail 的 HTTP 错误；
#   Query         —— 声明查询参数（本文件未使用，原样保留）；
#   Request       —— 当前请求对象，用于读取请求头中的身份信息。
from fastapi import APIRouter, Depends, HTTPException, Query, Request
# pathlib.Path：面向对象的路径操作；删除文档时用来判断并删除磁盘上的物理文件。
from pathlib import Path

# 导入同包内的三个业务模块：
#   audit    —— 写活动事件表与 AI 审计表；
#   llm      —— 模型网关封装，model_test 接口调用其 health()；
#   settings —— 运行时配置读写（数据库 + 默认值）。
from . import audit, llm, settings
# 上传文件根目录（默认 data/uploads），删除文档时用于清理以文档 ID 命名的衍生文件。
from .config import UPLOAD_DIR
# 导入数据访问层助手，业务代码不直接写 connect/commit：
#   db()       —— 上下文管理器，正常提交、异常回滚、最后关闭连接；
#   fetch_all  —— 查询多行，返回 list[dict]；
#   fetch_one  —— 查询单行，返回 dict 或 None；
#   iso()      —— 生成 ISO 8601 时间字符串（秒级）；
#   load_json  —— 把数据库里的 JSON 文本安全解析为 Python 对象；
#   now()      —— 返回东八区当前时间（datetime）。
from .db import db, fetch_all, fetch_one, iso, load_json, now
# 导入安全相关的能力：
#   Principal              —— 当前请求者身份（user_id/tenant_id/role/scopes/username）；
#   principal_from_request —— 从请求头解析身份（Bearer JWT 或 demo 模式默认用户）；
#   require_admin          —— 角色不是 admin 时抛 403。
from .security import Principal, principal_from_request, require_admin

# 创建本文件专属路由器：
#   prefix="/api/v1/admin" 会自动拼到每个路由前面；
#   tags=["admin"] 只影响 OpenAPI 文档的分组展示，不影响业务逻辑。
router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# 自定义 FastAPI 依赖函数：所有管理接口都写成 Depends(admin_principal)。
# 为什么单独抽出来：避免每个接口重复写“解析身份 + 校验管理员”的两步逻辑。
# 参数 request 由 FastAPI 自动注入，调用方不需要传。
def admin_principal(request: Request) -> Principal:
    # 解析请求头得到 Principal；令牌无效会在这里抛 401。
    p = principal_from_request(request)
    # 权限闸门：role != "admin" 时抛 403（错误码 FORBIDDEN）。
    require_admin(p)
    # 返回身份对象，FastAPI 会把它注入到接口参数 p 上。
    return p


# 内部辅助函数（下划线前缀表示仅供本模块使用）：计算答题准确率。
# 返回值：(已作答总数, 准确率 0~1)，例如 (200, 0.875)。
def _accuracy() -> tuple[int, float]:
    # 一条 SQL 同时算出总数与“达标数”（score >= 80 视为达标）：
    #   COUNT(*) 统计所有已作答轮次；
    #   SUM(CASE WHEN 条件 THEN 1 ELSE 0 END) 是标准的“条件计数”写法；
    #   WHERE status='answered' 排除失败/进行中的轮次，保证分母口径正确。
    # fetch_one 无结果时返回 None，末尾 `or {}` 兜底为空字典，后面 .get 才不会报错。
    row = fetch_one(
        # SQL 第一段：总数 + 达标数（相邻字符串字面量会自动拼接）。
        "SELECT COUNT(*) AS total, SUM(CASE WHEN score>=80 THEN 1 ELSE 0 END) AS good"
        # SQL 第二段：过滤条件；两段之间已预留空格，拼接后语法正确。
        " FROM turn WHERE status='answered'"
    ) or {}  # 兜底：把 None 变成 {}，避免下一行 row.get 触发 AttributeError。
    # 取 total；COUNT 不会为 NULL，但 get 仍可能返回 None，统一 `or 0` 再 int。
    total = int(row.get("total") or 0)
    # 取 good；没有达标记录时 SUM 返回 NULL，`or 0` 兜底。
    good = int(row.get("good") or 0)
    # 三元表达式做除零保护；round(x, 4) 保留 4 位小数。
    return total, round(good / total, 4) if total else 0.0


# 内部辅助函数：统计 AI 输出的“无依据（幻觉）”比例。
# 口径：只统计 ai_audit 中 fallback=0 的记录（排除模型失败后降级生成的兜底内容），
#       其中 grounded=0 表示回答没有落到原文证据上，记为 invalid。
# 返回：{"total": 样本数, "invalid": 无依据数, "rate": 比例}。
def _hallucination() -> dict:
    # 同样用一条聚合 SQL 同时取总数与“无依据数”。
    row = fetch_one(
        # SQL 第一段：COUNT(*) 总数 + 条件计数 bad。
        "SELECT COUNT(*) AS total, SUM(CASE WHEN grounded=0 THEN 1 ELSE 0 END) AS bad"
        # SQL 第二段：只看非降级输出（fallback=0）。
        " FROM ai_audit WHERE fallback=0"
    ) or {}  # 无结果兜底。
    # 样本总数。
    total = int(row.get("total") or 0)
    # 无依据条数。
    bad = int(row.get("bad") or 0)
    # 组装结果；rate 同样做除零保护并保留 4 位小数。
    return {"total": total, "invalid": bad, "rate": round(bad / total, 4) if total else 0.0}


# 注册 GET /api/v1/admin/overview：管理台首页总览（指标卡 + 趋势图）。
# p 不是普通入参，而是依赖注入结果：FastAPI 先执行 admin_principal(request)，
# 校验通过后把 Principal 作为 p 传入；校验失败则在进入函数前就返回 401/403。
@router.get("/overview")
def overview(p: Principal = Depends(admin_principal)):
    # “今天”的日期字符串（YYYY-MM-DD），用于和库中 ISO 时间字符串的前 10 位比较。
    today = now().strftime("%Y-%m-%d")
    # 指标卡：启用中的用户数；COUNT 查询必定返回一行，可直接取 ["c"]。
    users = fetch_one("SELECT COUNT(*) AS c FROM app_user WHERE status='active'")["c"]  # 指标卡：启用中的用户数
    # 指标卡：今天活跃过的去重用户数。
    # 说明：user_id<>'' 排除匿名事件；substr(ts,1,10)=? 取出日期部分与今天比较。
    active_today = fetch_one(
        # 参数用 ? 占位符传入，由数据库驱动转义，避免 SQL 注入。
        "SELECT COUNT(DISTINCT user_id) AS c FROM activity_event WHERE user_id<>'' AND substr(ts,1,10)=?", (today,)
    )["c"]  # 取计数列 c
    # 指标卡：未软删除的文档数（deleted_at IS NULL 表示“有效”）。
    docs = fetch_one("SELECT COUNT(*) AS c FROM document WHERE deleted_at IS NULL")["c"]  # 指标卡：有效文档数
    # 指标卡：有效文档下的会话总数；JOIN 让已删除文档的会话自然被排除。
    # 注意：这些统计是全局口径，没有按 tenant_id 过滤，适用于当前单租户管理台。
    sessions = fetch_one(
        # 通过 doc_id 关联，确保会话所属文档未被删除。
        "SELECT COUNT(*) AS c FROM session s JOIN document d ON d.doc_id=s.doc_id WHERE d.deleted_at IS NULL"
    )["c"]  # 取计数列 c
    # 复用辅助函数：answers=已作答数，accuracy=准确率。
    answers, accuracy = _accuracy()  # 复用辅助函数
    # 幻觉统计 dict，下面只取其中三个字段。
    hall = _hallucination()  # 幻觉统计
    # 指标卡：全站累计 token 与积分。
    # COALESCE(SUM(x),0) 把“没有数据”时的 NULL 变成 0，省去 Python 端判断。
    token_row = fetch_one(
        # p=输入 token，c=输出 token。
        "SELECT COALESCE(SUM(prompt_tokens),0) AS p, COALESCE(SUM(completion_tokens),0) AS c,"
        # credit=计费积分（可能是小数）。
        " COALESCE(SUM(credits),0) AS credit FROM usage_log"
    ) or {}  # 兜底：理论上聚合必有一行，仍防御性处理。
    # 指标卡：今天的 token 消耗（输入 + 输出），同样按日期前 10 位过滤。
    today_tokens = fetch_one(
        # SUM 里直接相加两列，减少一次 Python 运算。
        "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_log WHERE substr(ts,1,10)=?",
        # 参数元组：今天的日期字符串。
        (today,),
    )["t"]  # 取单列 t
    # 指标卡：今天完成的回答数；answered_at 是回答完成时间，可能与创建时间不同。
    today_answers = fetch_one(
        # 只统计 status='answered' 的轮次。
        "SELECT COUNT(*) AS c FROM turn WHERE status='answered' AND substr(answered_at,1,10)=?", (today,)
    )["c"]  # 取计数列 c
    # 趋势数据容器：14 个点，每天一个 dict。
    trend = []
    # range(13,-1,-1) 依次产生 13,12,...,0，覆盖“13 天前”到“今天”共 14 天。
    for i in range(13, -1, -1):
        # 用当前时间减去 i 天，格式化成 YYYY-MM-DD。
        day = (now() - timedelta(days=i)).strftime("%Y-%m-%d")
        # 当天答题数 + 平均分。AVG(score) 是百分制平均分，空天由 COALESCE 变成 0。
        # 性能提示：循环里每天查 3 次，14 天共 42 次查询（N+1 模式）；
        #           数据量大时应改成一条 GROUP BY 查询，这里保持原实现便于逐行理解。
        a = fetch_one(
            # 过滤当天已作答的轮次。
            "SELECT COUNT(*) AS c, COALESCE(AVG(score),0) AS s FROM turn WHERE status='answered' AND substr(answered_at,1,10)=?",
            # 参数：当天日期。
            (day,),
        ) or {}  # 兜底空结果。
        # 当天 token 消耗（输入 + 输出）。
        t = fetch_one(
            # 同样按日期前 10 位过滤。
            "SELECT COALESCE(SUM(prompt_tokens+completion_tokens),0) AS n FROM usage_log WHERE substr(ts,1,10)=?",
            # 参数：当天日期。
            (day,),
        ) or {}  # 兜底空结果。
        # 当天活动事件条数（用于趋势图的“活跃度”曲线）。
        e = fetch_one("SELECT COUNT(*) AS c FROM activity_event WHERE substr(ts,1,10)=?", (day,))["c"]  # 当天事件数
        # 组装一天的数据点：
        #   date     —— 日期字符串；
        #   answers  —— 当天完成答题数；
        #   accuracy —— 平均分 / 100，转成 0~1 的小数；
        #   tokens   —— 当天 token 总数；
        #   events   —— 当天活动事件数。
        # 字典跨行写在括号内是合法语法，不需要反斜杠续行。
        trend.append({"date": day, "answers": int(a.get("c") or 0), "accuracy": round(float(a.get("s") or 0) / 100, 4),
                      "tokens": int(t.get("n") or 0), "events": int(e)})
    # 最近 8 条审计事件，用于首页“实时动态”；audit 内部会限制 1~300 条。
    recent = audit.recent_events(limit=8)  # 最近 8 条审计事件
    # 返回总览 JSON，共四块：
    #   cards         —— 顶部指标卡；
    #   trend         —— 14 天趋势折线图数据；
    #   recent_events —— 最近动态列表；
    #   model         —— 当前模型名与启用状态。
    return {
        "cards": {  # 指标卡区域
            "users": users,  # 启用中的用户数
            "active_today": active_today,  # 今天活跃用户数
            "documents": docs,  # 有效文档数
            "sessions": sessions,  # 有效会话数
            "answers": answers,  # 已作答总数
            "accuracy": accuracy,  # 准确率（0~1）
            "hallucination_rate": hall["rate"],  # 幻觉率（0~1）
            "hallucination_total": hall["total"],  # 审计样本总数
            "hallucination_invalid": hall["invalid"],  # 无依据条数
            "tokens": int(token_row.get("p") or 0) + int(token_row.get("c") or 0),  # 累计输入+输出 token
            "today_tokens": int(today_tokens or 0),  # 今日 token
            "today_answers": int(today_answers or 0),  # 今日回答数
            "credits": round(float(token_row.get("credit") or 0), 2),  # 累计积分
        },
        "trend": trend,  # 14 天趋势数据
        "recent_events": recent,  # 最近动态
        "model": {"model": settings.get("llm.model"), "enabled": settings.get_bool("llm.enabled")},  # 当前模型与启用开关，供页面显示“已启用/已停用”
    }


# 注册 GET /api/v1/admin/documents：文档列表，支持关键词 q 与状态 status 过滤。
# q / status 是查询参数（如 ?q=论文&status=ready），FastAPI 自动从 URL 解析；
# 默认空串表示“不过滤”。p 仍是管理员依赖。
@router.get("/documents")
def documents(q: str = "", status: str = "", p: Principal = Depends(admin_principal)):
    # 用“列表 + 追加”的方式动态拼 SQL：先写基础查询，再按传入条件追加 WHERE。
    # 所有用户输入都通过 ? 参数传入，绝不拼进 SQL 文本，避免 SQL 注入。
    sql = (
        # 第一段：文档主表全部字段 + 上传者用户名/显示名。
        "SELECT d.*, u.username, u.display_name FROM document d"  # 文档主表 + 上传者信息
        # 左连接：即使用户记录缺失，文档仍然返回（用户列为 NULL）。
        " LEFT JOIN app_user u ON u.user_id=d.user_id"  # 左连接：无匹配用户时用户列为 NULL
        # 软删除过滤：只展示未删除文档。
        " WHERE d.deleted_at IS NULL"  # 只看未删除文档
    )  # 结束 SQL 基础部分（三行字符串会自动拼接）。
    # 参数列表，顺序必须与 SQL 中 ? 的出现顺序一致。
    params: list = []  # 动态参数列表
    # 传了 status 才追加状态条件。
    if status:
        # 追加过滤片段；前导空格很重要，避免与上一段粘连成 "...NULLAND"。
        sql += " AND d.status=?"  # 前导空格不可少
        # 对应参数。
        params.append(status)
    # 传了关键词才追加模糊搜索。
    if q:
        # 标题或文件名任一匹配；LIKE 的通配符放在参数值里，不放 SQL 里。
        sql += " AND (d.title LIKE ? OR d.file_name LIKE ?)"
        # 两个 ? 对应两个 %q%。
        params.extend([f"%{q}%", f"%{q}%"])
    # 统一排序：最新创建的文档在前。
    sql += " ORDER BY d.created_at DESC"
    # 执行查询，得到 list[dict]。
    rows = fetch_all(sql, params)  # 执行查询
    # 逐行转换成前端直接可用的 DTO。
    items = []  # DTO 列表
    # 遍历数据库行。
    for d in rows:
        # stats / quality_issues 在库中是 JSON 文本，需反序列化；解析失败回退默认值。
        stats = load_json(d.get("stats"), {})
        # 默认空列表，保证前端可以直接遍历。
        issues = load_json(d.get("quality_issues"), [])
        # 组装单条文档对象（字段名对前端更友好，避免直接暴露数据库列名）。
        items.append({
            "document_id": d["doc_id"],  # 文档主键
            "title": d.get("title") or d.get("file_name") or "未命名文档",  # 标题为空时依次回退到文件名、“未命名文档”
            "file_name": d.get("file_name"),  # 原始文件名
            "doc_type": d.get("doc_type"),  # 文档类型（research_paper/review/...）
            "page_count": d.get("page_count") or 0,  # 页数，NULL 视为 0
            "size_bytes": d.get("size_bytes") or 0,  # 文件字节数
            "status": d.get("status"),  # 处理状态（uploaded/parsed/ready/...）
            "version": d.get("doc_version"),  # 文档版本号
            "owner": {"user_id": d.get("user_id"), "username": d.get("username") or "",  # 上传者信息
                      "display_name": d.get("display_name") or d.get("username") or ""},  # 显示名回退：display_name -> username -> 空串
            "quality": {"score": d.get("quality_score") or 0, "level": d.get("quality_level") or "review",  # 质量评估结果
                        "issues": issues},  # 质量问题列表（JSON 数组）
            "stats": stats,  # 解析统计（页数、字符数、节点数等，结构由解析器决定）
            "created_at": d.get("created_at"),  # 创建时间（ISO 字符串）
        })
    # total 为本次返回条数；本接口没有分页，一次性返回所有匹配文档。
    return {"items": items, "total": len(items)}  # 总条数


# 注册 DELETE /api/v1/admin/documents/{document_id}：删除文档，默认同时清理磁盘文件。
# document_id 来自 URL 路径；purge 是查询参数（默认 True，即物理清理）；
# p 为管理员身份（删除是高危操作，必须 admin）。
@router.delete("/documents/{document_id}")
def delete_document(document_id: str, purge: bool = True, p: Principal = Depends(admin_principal)):
    # 先查文档是否存在且未删除，避免对不存在的 ID 做无意义更新。
    row = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (document_id,))  # 查询有效文档
    # 查不到 -> 404；错误体统一为 {code, message}。
    if not row:
        # detail 用 dict 便于前端按 code 分支处理。
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    # 开启事务：with 块正常结束自动 commit，异常自动 rollback。
    with db() as conn:  # 开启事务
        # 软删除：写入 deleted_at 并把 status 改为 'deleted'，同时刷新 updated_at。
        # 数据行仍保留在表中，后续可恢复，也方便审计追溯。
        conn.execute(
            # deleted_at / updated_at 都使用当前时间（iso() 默认取 now()）。
            "UPDATE document SET deleted_at=?, status='deleted', updated_at=? WHERE doc_id=?",
            # 参数顺序与 SQL 占位符一一对应：删除时间、更新时间、文档 ID。
            (iso(), iso(), document_id),
        )  # 结束 execute 调用（提交由 with 语句负责）。
    # purge 为真时才删除物理文件；失败也不影响已完成的软删除。
    if purge:
        # 文件操作可能因权限、占用、文件不存在而失败，用 try 包住做 best-effort 清理。
        try:
            # file_path 可能为 None/空串；Path("") 指向当前目录且 is_file() 为 False，因此安全。
            path = Path(row.get("file_path") or "")
            # 只删除普通文件，避免把目录当成文件删。
            if path.is_file():
                # unlink() 删除文件；没有 missing_ok 参数，所以必须捕获异常。
                path.unlink()
        # 捕获所有异常：清理失败不应让整个删除请求报错。
        except Exception:
            # pass：忽略异常，保持接口幂等。
            pass  # 忽略异常
        # 再按命名约定清理上传目录中的衍生文件（如 {doc_id}.pdf / 预览图）。
        for path in UPLOAD_DIR.glob(f"{document_id}.*"):
            # 每个文件单独 try，一个失败不影响其余文件。
            try:
                # 删除匹配到的文件。
                path.unlink()
            # 同样吞掉异常。
            except Exception:
                # 忽略。
                pass  # 忽略异常
    # 写审计事件：记录操作者、租户、动作类型与目标文档；audit 内部失败也不会抛出。
    audit.log_event(
        # 操作者身份来自已鉴权的 Principal。
        user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="document_delete",
        # detail 保留文档标题与是否物理清理，便于事后追溯。
        detail={"document_id": document_id, "title": row.get("title"), "purged": bool(purge)},
    )  # 结束 audit.log_event 调用。
    # 返回结果；bool(purge) 统一成 JSON 布尔值。
    return {"document_id": document_id, "deleted": True, "purged": bool(purge)}  # 返回删除结果


# 注册 GET /api/v1/admin/users：用户列表 + 每个用户的文档/会话/答题/token 统计。
# 管理员依赖；本接口没有分页参数，用户量很大时需要前端分页或后端补分页。
@router.get("/users")
def users(p: Principal = Depends(admin_principal)):
    # 取出全部用户，先按 role 字符串倒序、再按 created_at 正序。
    # 注意：字符串倒序是字典序（user > teacher > admin），并不等于权限从高到低；
    #       若希望管理员排在最前，应改用 CASE WHEN role='admin' THEN 0 ... 的写法。
    rows = fetch_all("SELECT * FROM app_user ORDER BY role DESC, created_at")  # 全部用户
    # 结果容器。
    items = []  # 结果容器
    # 逐个用户做聚合查询（N+1 模式）；用户多时应改为 GROUP BY 子查询。
    for u in rows:
        # 用户主键，后续所有统计都用它作为查询参数。
        uid = u["user_id"]
        # 该用户未删除的文档数。
        docs = fetch_one("SELECT COUNT(*) AS c FROM document WHERE user_id=? AND deleted_at IS NULL", (uid,))["c"]  # 文档数
        # 该用户的有效会话数：session 通过 doc_id 关联 document，排除已删除文档。
        # 同时用 s.user_id 过滤，避免把同一文档下别人的会话算进来。
        sessions = fetch_one(
            # 关联文档以判断删除状态。
            "SELECT COUNT(*) AS c FROM session s JOIN document d ON d.doc_id=s.doc_id"
            # 过滤条件：本人 + 文档未删除。
            " WHERE s.user_id=? AND d.deleted_at IS NULL", (uid,)
        )["c"]  # 会话数
        # 该用户的答题统计：总数 c、平均分 s、达标数 good。
        # JOIN session 是为了把 turn 归属到用户；只统计已作答轮次。
        answer_row = fetch_one(
            # 条件计数 good 的写法同上。
            "SELECT COUNT(*) AS c, COALESCE(AVG(t.score),0) AS s, SUM(CASE WHEN t.score>=80 THEN 1 ELSE 0 END) AS good"
            # 过滤本人且已作答。
            " FROM turn t JOIN session s ON s.session_id=t.session_id WHERE s.user_id=? AND t.status='answered'",
            # 参数：用户 ID。
            (uid,),
        ) or {}  # 兜底空结果。
        # 最近活动时间：MAX(ts)，没有记录时返回 NULL；`or {}` 兜底。
        last_event = fetch_one("SELECT MAX(ts) AS ts FROM activity_event WHERE user_id=?", (uid,)) or {}  # 最近活动
        # 该用户消耗的 token 总量。
        # 口径：usage_log 通过 session_id 或 document_id 关联到用户，满足其一即可（OR）。
        # LEFT JOIN 让已删除会话/文档的用量也能统计到；COALESCE 把空结果变成 0。
        tokens = fetch_one(
            # 输入 + 输出 token。
            "SELECT COALESCE(SUM(u.prompt_tokens+u.completion_tokens),0) AS n FROM usage_log u"
            # 通过会话关联用户。
            " LEFT JOIN session s ON s.session_id=u.session_id"
            # 通过文档关联用户（上传者）。
            " LEFT JOIN document d ON d.doc_id=u.document_id"
            # 两种归属满足其一。
            " WHERE s.user_id=? OR d.user_id=?",
            # 两个参数都是用户 ID。
            (uid, uid),
        )["n"]  # token 总数
        # 组装用户 DTO：账号信息 + 文档/会话/答题/token 统计。
        items.append({
            "user_id": uid,  # 用户主键
            "username": u["username"],  # 登录名
            "display_name": u["display_name"] or u["username"],  # 显示名回退到登录名
            "email": u.get("email") or "",  # 邮箱，NULL 转空串
            "phone": u.get("phone") or "",  # 手机号
            "role": u["role"],  # 角色（admin/teacher/user）
            "status": u["status"],  # 状态（active/disabled）
            "tenant_id": u["tenant_id"],  # 所属租户
            "documents": int(docs),  # 文档数
            "sessions": int(sessions),  # 会话数
            "answers": int(answer_row.get("c") or 0),  # 答题数
            "accuracy": round(float(answer_row.get("s") or 0) / 100, 4),  # 准确率：平均分 / 100
            "tokens": int(tokens or 0),  # token 总消耗
            "last_login_at": u["last_login_at"],  # 最后登录时间（可能为 None）
            "last_active_at": last_event.get("ts"),  # 最后活跃时间：activity_event 中最大的 ts
        })
    # 返回 {"items": [...]}；本接口无 total 字段，前端可直接用数组长度。
    return {"items": items}  # 返回列表


# 注册 POST /api/v1/admin/users/{user_id}/status：启用或禁用账号。
# payload 是请求体 JSON（FastAPI 自动解析成 dict）；
# 动作型接口用 POST 是项目约定，语义上相当于“对用户执行启用/禁用操作”。
@router.post("/users/{user_id}/status")
def set_user_status(user_id: str, payload: dict, p: Principal = Depends(admin_principal)):
    # 取 status，缺省为 active（宽松默认，避免漏传字段直接报错）。
    status = payload.get("status", "active")  # 默认 active
    # 白名单校验：只允许 active / disabled，防止写入任意状态值。
    if status not in ("active", "disabled"):
        # 422 表示参数语义不合法；错误码 INVALID_STATUS 供前端识别。
        raise HTTPException(status_code=422, detail={"code": "INVALID_STATUS", "message": "状态只能是 active / disabled"})
    # 先确认用户存在；fetch_one 返回 None 表示查无此人。
    if fetch_one("SELECT user_id FROM app_user WHERE user_id=?", (user_id,)) is None:
        # 404 + USER_NOT_FOUND。
        raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"})
    # 事务写库。
    with db() as conn:
        # 参数化 UPDATE，status 来自白名单，user_id 来自路径。
        conn.execute("UPDATE app_user SET status=? WHERE user_id=?", (status, user_id))  # 更新状态
    # 审计：谁（p）把哪个用户（target）改成了什么状态（status）。
    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="user_status",
                    # detail 记录被操作用户与新状态。
                    detail={"target": user_id, "status": status})
    # 返回新状态，前端可据此就地更新列表。
    return {"user_id": user_id, "status": status}  # 返回新状态


# 注册 GET /api/v1/admin/events：活动事件流，支持 since_id 增量拉取。
# limit：本次最多返回多少条（默认 100）；
# since_id：只取 id 比它大的新事件（默认 0，表示取最新一批）。
@router.get("/events")
def events(limit: int = 100, since_id: int = 0, p: Principal = Depends(admin_principal)):
    # 复用 audit.recent_events：内部会限制 limit 在 1~300，并按 id 倒序。
    items = audit.recent_events(limit=limit, since_id=since_id)  # 查询事件
    # 计算本轮最大 id 作为下一次轮询的游标；列表为空时退回 since_id，保证游标不回退。
    max_id = max((x["id"] for x in items), default=since_id)  # 增量游标
    # 返回 items 与 max_id；前端保存 max_id，下次带 ?since_id=... 只拿增量。
    return {"items": items, "max_id": max_id}  # 返回


# 注册 GET /api/v1/admin/online：最近 5 分钟内有活动事件的“在线用户”。
# 这里用“最近有行为”近似在线，不是长连接/WebSocket 的真实在线状态。
@router.get("/online")
def online(p: Principal = Depends(admin_principal)):
    # 起点 = 当前时间 - 5 分钟，转成 ISO 字符串；ISO 格式可直接按字典序比较大小。
    since = iso(now() - timedelta(minutes=5))  # 5 分钟窗口起点
    # 按用户聚合最近 5 分钟的活动：
    #   MAX(NULLIF(username,'')) 跳过空用户名，取一个非空名字用于展示；
    #   COUNT(*) 是事件条数，可粗略反映活跃度；
    #   MAX(ts) 是最后活跃时间，用于排序；
    #   GROUP BY user_id 去重，ORDER BY last_ts DESC 让最活跃的排前面。
    rows = fetch_all(
        # NULLIF(username,'') 把空串变成 NULL，MAX 会忽略 NULL。
        "SELECT user_id, MAX(NULLIF(username,'')) AS username, COUNT(*) AS events, MAX(ts) AS last_ts"
        # 只统计窗口内的活动。
        " FROM activity_event WHERE ts>=? GROUP BY user_id ORDER BY last_ts DESC",
        # 参数：窗口起点。
        (since,),
    )  # 结束查询。
    # 同时返回统计窗口起点，前端可显示“最近 5 分钟”。
    return {"since": since, "items": rows}  # 返回窗口与列表


# 注册 GET /api/v1/admin/settings：读取全部运行时配置。
# 管理员依赖。
@router.get("/settings")
def get_settings(p: Principal = Depends(admin_principal)):
    # all_settings() 已合并代码默认值与数据库值，返回 {values, updated_at}。
    return settings.all_settings()  # 读取全部设置


# 注册 PUT /api/v1/admin/settings：批量保存设置（部分更新语义）。
# payload 为请求体；兼容 {"values": {...}} 与直接 {...} 两种格式。
@router.put("/settings")
def put_settings(payload: dict, p: Principal = Depends(admin_principal)):
    # 若 payload["values"] 是 dict 就用它，否则把整个 payload 当键值表；
    # isinstance 检查可避免 values 是字符串/列表时调用 .items() 报错。
    values = payload.get("values") if isinstance(payload.get("values"), dict) else payload  # 兼容两种请求体
    # 过滤掉 dict/list 等嵌套值（设置只支持标量），统一 str 后交给 settings.set_many；
    # set_many 内部还会做键白名单、数值范围收敛，并返回保存后的全量设置。
    result = settings.set_many({k: str(v) for k, v in values.items() if not isinstance(v, (dict, list))})  # 保存设置
    # 审计只记录提交了哪些键，不记录具体值，避免把密钥类配置写进日志。
    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="settings_update",
                    # detail 中的 keys 仅用于追溯“改过哪些项”。
                    detail={"keys": list(values.keys())})
    # 返回保存后的全量设置，前端可直接刷新表单。
    return result  # 返回最新设置


# 注册 POST /api/v1/admin/model/test：测试模型网关是否可用。
# 管理员依赖。
@router.post("/model/test")
def model_test(p: Principal = Depends(admin_principal)):
    # llm.health() 返回 (ok, message)：内部请求 /v1/models，5 秒超时，异常也会转成 (False, 原因)。
    ok, msg = llm.health()  # 探活
    # 一并返回当前模型名与启用开关，方便管理台展示状态。
    return {"ok": ok, "message": msg, "model": settings.get("llm.model"), "enabled": settings.get_bool("llm.enabled")}  # 返回结果


# 注册 GET /api/v1/admin/usage：token 与积分用量报表。
# days：统计最近多少天（默认 14）。
@router.get("/usage")
def usage(days: int = 14, p: Principal = Depends(admin_principal)):
    # 参数收敛：None/0 先兜底为 14，再转 int，最后夹到 1~90 天，
    # 防止 days=0 造成空窗口，也防止一次查询过大范围拖垮数据库。
    days = max(1, min(90, int(days or 14)))  # 收敛天数
    # 起始日期（含）：往前推 days-1 天，正好覆盖连续 days 天。
    start = (now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")  # 起始日期
    # 按天聚合：请求次数、输入/输出 token、积分。
    # substr(ts,1,10) 取日期部分；GROUP BY 同一表达式；ORDER BY date 保证时间升序。
    # 别名 date/requests/... 会直接成为 JSON 字段名。
    daily = fetch_all(
        # 日期 + 请求次数。
        "SELECT substr(ts,1,10) AS date, COUNT(*) AS requests,"
        # 输入/输出 token 汇总。
        " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
        # 积分汇总；过滤窗口起点。
        " COALESCE(SUM(credits),0) AS credits FROM usage_log WHERE substr(ts,1,10)>=?"
        # 按天分组并按日期升序。
        " GROUP BY substr(ts,1,10) ORDER BY date",
        # 参数：起始日期。
        (start,),
    )  # 结束查询。
    # 按调用类型（kind）聚合，例如 guide/ocr/chart 等，用于分类占比图。
    by_kind = fetch_all(
        # 类型 + 请求次数。
        "SELECT kind, COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        # token 汇总。
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens, COALESCE(SUM(credits),0) AS credits"
        # 过滤窗口 + 按类型分组，请求多的排前面。
        " FROM usage_log WHERE substr(ts,1,10)>=? GROUP BY kind ORDER BY requests DESC",
        # 参数：起始日期。
        (start,),
    )  # 结束查询。
    # 按模型（model）聚合，用于比较不同模型的调用量与成本。
    by_model = fetch_all(
        # 模型名 + 请求次数。
        "SELECT model, COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        # token 汇总。
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens, COALESCE(SUM(credits),0) AS credits"
        # 过滤 + 分组 + 按请求量倒序。
        " FROM usage_log WHERE substr(ts,1,10)>=? GROUP BY model ORDER BY requests DESC",
        # 参数：起始日期。
        (start,),
    )  # 结束查询。
    # 窗口总计：请求数、输入/输出 token、积分。
    total = fetch_one(
        # 一行聚合出所有汇总列。
        "SELECT COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
        # 输入/输出 token 与积分。
        " COALESCE(SUM(completion_tokens),0) AS completion_tokens, COALESCE(SUM(credits),0) AS credits"
        # 过滤窗口起点。
        " FROM usage_log WHERE substr(ts,1,10)>=?",
        # 参数：起始日期。
        (start,),
    ) or {}  # 兜底
    # 返回报表数据：days、daily（折线）、by_kind/by_model（分组）、total（汇总卡）。
    return {
        "days": days,  # 统计窗口天数
        "daily": daily,  # 按天序列
        "by_kind": by_kind,  # 按调用类型
        "by_model": by_model,  # 按模型
        "total": {  # 汇总卡
            "requests": total.get("requests", 0),  # 请求总数
            "prompt_tokens": total.get("prompt_tokens", 0),  # 输入 token
            "completion_tokens": total.get("completion_tokens", 0),  # 输出 token
            "tokens": int(total.get("prompt_tokens") or 0) + int(total.get("completion_tokens") or 0),  # 输入+输出合计
            "credits": round(float(total.get("credits") or 0), 2),  # 积分保留 2 位小数
        },
    }


# 注册 GET /api/v1/admin/model-logs：最近的 AI 调用审计日志。
# limit：返回条数，默认 30。
@router.get("/model-logs")
def model_logs(limit: int = 30, p: Principal = Depends(admin_principal)):
    # 取 ai_audit 最新 N 条；max(1, min(200, limit)) 防止负数或一次拉取过多。
    # LIMIT 同样用 ? 占位符传参（SQLite 与 PostgreSQL 都支持）。
    rows = fetch_all("SELECT * FROM ai_audit ORDER BY id DESC LIMIT ?", (max(1, min(200, limit)),))  # 查询最近日志
    # 逐行把 detail 从 JSON 文本解析成对象，前端可直接展开查看。
    for r in rows:  # 遍历解析
        # 解析失败回退空字典，保证每行结构稳定。
        r["detail"] = load_json(r.get("detail"), {})  # 解析 detail
    # 返回日志列表。
    return {"items": rows}  # 返回列表
