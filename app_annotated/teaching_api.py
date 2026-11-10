# =============================================================================
# 文件：app/teaching_api.py —— V1.2 教学闭环 API（作业布置 / 学生作答 / 教师复盘）
# =============================================================================
# 【这个文件做什么】
#   这是“教学闭环”功能的后端路由层（HTTP 接口层），只负责四件事：
#     1) 把 HTTP 请求翻译成函数参数（路径参数、查询参数、JSON 请求体）；
#     2) 判断“谁能调用”（require_teacher 校验角色，assignments._can_view 校验资源归属）；
#     3) 调用下层业务模块 assignments / sessions 完成真正的业务；
#     4) 把业务结果包成 JSON 返回，把业务异常翻译成带状态码的 HTTP 错误。
#   它自己只在“创建题卷”这一处直接写库（question_set 表），其余写操作全部委托给
#   assignments.py / sessions.py，避免同一套规则在两个地方各写一遍。
#
#   本文件共 12 条路由，按出现顺序：
#     GET  /api/v1/teaching/students                     教师：可选学生列表
#     GET  /api/v1/teaching/documents                    教师：可出题的论文列表
#     POST /api/v1/teaching/papers                       教师：从论文抽题生成题卷（201）
#     GET  /api/v1/teaching/papers                       教师：题卷列表
#     GET  /api/v1/assignments                           所有人：作业列表（按角色过滤）
#     POST /api/v1/assignments                           教师：布置作业（201）
#     GET  /api/v1/assignments/by-session/{session_id}   按答题会话反查作业
#     GET  /api/v1/assignments/{assignment_id}           作业详情
#     POST /api/v1/assignments/{assignment_id}/start     学生：开始作答
#     POST /api/v1/assignments/{assignment_id}/submit    学生：交卷
#     POST /api/v1/assignments/{assignment_id}/review    教师：复盘打分
#     GET  /api/v1/assignments/{assignment_id}/report    查看作业报告
#
# 【在整个应用中的位置】
#   浏览器教学页（fetch 带 Authorization: Bearer <JWT>）
#     └─ app/main.py：`from . import teaching_api` + `app.include_router(teaching_api.router)`
#          └─ 本文件的 router（prefix="/api/v1"）
#               └─ 具体端点函数
#                    ├─ security.principal_from_request(request) → Principal
#                    ├─ security.require_teacher(p) → 403 或放行
#                    ├─ db.fetch_all / fetch_one / db() → SQLite 或 PostgreSQL
#                    └─ assignments.* / sessions.* → 业务规则与落库
#
#   - 本文件属于后端包 app/，与 admin.py、quality_api.py、reader_api.py 等平级，
#     都是 FastAPI 的“路由模块”，由 main.py 逐一挂到全局 app；
#   - router 自带 prefix="/api/v1"，所以装饰器里写 "/teaching/students"，
#     最终对外 URL 就是 /api/v1/teaching/students；
#   - tags=["teaching"] 只影响 OpenAPI/Swagger 文档分组，不影响鉴权与业务逻辑；
#   - 全部使用相对导入（from . import ...），说明它是包内模块，
#     不能当作脚本直接 `python teaching_api.py` 运行。
#
# 【涉及的数据表（字段名均来自 db.py 的建表语句）】
#   app_user         用户账号（role: admin/teacher/user；status: active/disabled）
#   document         论文主表（deleted_at 软删除；page_count 页数；status 解析状态）
#   question         题目（doc_id 关联论文；difficulty 1~5；status='active' 才可用）
#   question_set     题卷（set_id 主键；question_ids 存 JSON 数组文本；created_by 教师）
#   paper_assignment 作业（assignment_id；paper_id；assignee_id 学生；teacher_id 教师；
#                    status: assigned/in_progress/submitted/reviewed；session_id 答题会话）
#   session          答题会话（本文件不直接查，交给 sessions.py 处理）
#   report           会话报告（sessions.get_report 读取）
#
# 【权限模型（两层）】
#   第一层·认证：security.principal_from_request(request)
#       * 请求头 "Authorization: Bearer <JWT>" → 验签 + 校验 exp → 解析 user_id/tenant_id/role/scopes；
#       * AUTH_MODE=strict 且没有令牌 → 401 UNAUTHORIZED；
#       * demo 模式没有令牌 → 退化为请求头 X-User-Id / X-Tenant-Id（默认 demo 用户），
#         只为本地调试方便，绝不能在公网这样放开；
#       * 令牌有效但账号被禁用 → 401 ACCOUNT_DISABLED。
#   第二层·授权：
#       * 角色级：require_teacher(p) 要求 role ∈ {"admin", "teacher"}，否则 403 FORBIDDEN；
#       * 资源级：assignments._can_view(row, p) 判断“管理员 / 布置者 / 被布置的学生”，
#         作业详情、开始、提交、复盘、报告都靠它兜底；
#       * 租户级：绝大多数 SQL 显式带 tenant_id=?；按主键取单行的接口则先取再校验归属。
#   读代码时要把“SQL 条件”和“函数里的 if 判断”合起来看，两层都成立才是完整权限。
#
# 【错误码约定】
#   200 成功；201 创建成功；400 请求不合法；401 未登录/令牌无效；
#   403 已登录但无权限（FORBIDDEN）；404 资源不存在（DOC_NOT_FOUND、ASSIGNMENT_NOT_FOUND）；
#   409 业务状态冲突（QUESTION_SET_EMPTY 没有可用题目、REPORT_NOT_READY 报告未生成、
#       ASSIGNMENT_NOT_STARTED 尚未开始作答、ASSIGNEE_DISABLED 学生账号被禁用）；
#   422 参数不合法（框架校验失败为 VALIDATION_ERROR，业务层如 ASSIGNEE_REQUIRED）；
#   500 未捕获异常（main.py 的兜底处理器返回 INTERNAL_ERROR）。
#   统一错误体（main.py 的 _error）：{"code", "message", "details", "request_id"}。
#
# 【异步 / 线程模型】
#   本文件所有端点都用同步 def，而不是 async def。
#   FastAPI 对同步端点的处理是：把整个函数丢进线程池（anyio worker thread）执行，
#   结束后把返回值交回事件循环序列化。因此这里可以直接调用阻塞的 sqlite3/psycopg，
#   不会卡住事件循环；代价是并发量受线程池大小限制（默认约 40 个线程）。
#   反过来，如果写成 async def 却仍调用同步数据库驱动，才会真正阻塞事件循环——常见坑。
#
# 【阅读提示】
#   - 本文件是“逐行学习注释版”：所有 # 注释都是后加的，代码本身与原文件逐字一致
#     （去掉注释后语法树完全相同，可用 ast.dump 对比验证）；
#   - SQL 里的 ? 是参数占位符而不是字符串拼接：值由数据库驱动单独传入，能防 SQL 注入；
#     db.py 在 PostgreSQL 模式下会自动把 ? 翻译成 %s；
#   - 时间字段统一存 ISO 字符串（db.iso()，东八区、精确到秒），不是数据库原生时间类型；
#   - JSON 字段（question_ids 等）在库里存文本：写入用 db.dump()，读出用 db.load_json()；
#   - 路由按定义顺序注册，具体路径建议写在模糊路径之前，避免被 {参数} 抢先匹配。
# =============================================================================
# 模块文档字符串（原文件保留，一字未改）：一句话说明本模块职责。
"""V1.2 教学闭环 API：作业布置、学生作答、教师复盘。"""
# 启用 PEP 563“延迟注解”：函数签名里的类型注解不在运行时求值，而是保存成字符串。
# 好处：1) 可以使用 dict | None 这类新语法；2) 避免模块间类型引用造成循环导入。
from __future__ import annotations

# 从 FastAPI 导入本文件需要的四个构件：
#   APIRouter     —— 路由分组器：把一组接口打包，统一加 URL 前缀与文档标签；
#   Depends       —— 依赖注入声明（本文件实际未使用，属历史遗留导入，按“不改代码”原则原样保留）；
#   HTTPException —— 抛出带 HTTP 状态码和 JSON detail 的错误，交给 main.py 的处理器统一格式化；
#   Request       —— 封装本次 HTTP 请求（请求头等），鉴权时要靠它读 Authorization。
from fastapi import APIRouter, Depends, HTTPException, Request

# 相对导入同包内的两个业务模块（from . 表示“当前包 app”）：
#   assignments —— 作业领域逻辑：布置、开始、提交、复盘、可见性判断（_can_view/_api 等）；
#   sessions    —— 答题会话逻辑：建会话、出题、判分、生成报告（本文件主要用 SessionError 与 get_report）。
# 分层意图：HTTP 层只管协议与权限，业务规则集中在 assignments/sessions，便于复用与单元测试。
from . import assignments, sessions
# 从同包 db.py 导入数据库工具（db.py 内部兼容 SQLite 与 PostgreSQL）：
#   db        —— @contextmanager 包装的连接工厂：with db() as conn 正常结束自动 commit，
#                出异常自动 rollback，最后一定 close；用于“多条语句同一事务”的写场景；
#   dump      —— 把 Python 对象序列化成 JSON 文本（ensure_ascii=False，中文不转义），写库前用；
#   fetch_all —— 执行 SELECT，把所有行转成 list[dict]，只读多行查询用；
#   fetch_one —— 执行 SELECT，返回第一行 dict，没有行则返回 None；
#   iso       —— 生成 ISO 时间字符串（东八区、精确到秒），写 created_at 等字段用；
#   new_id    —— 生成带前缀的短随机 ID，如 new_id("set") → "set_1a2b3c4d5e"。
from .db import db, dump, fetch_all, fetch_one, iso, new_id
# 从同包 security.py 导入身份与权限工具：
#   Principal              —— 当前登录者的身份数据类：user_id / tenant_id / scopes / role / username；
#   principal_from_request —— 从请求头解析 JWT（或 demo 模式的 X-User-Id）得到 Principal，失败抛 401；
#   require_teacher        —— 校验角色是 teacher 或 admin，否则抛 403 {"code": "FORBIDDEN"}。
from .security import Principal, principal_from_request, require_teacher

# 创建本模块的路由器：
#   prefix="/api/v1"  —— 本文件所有路径自动拼上此前缀（版本化 API 的常见做法）；
#   tags=["teaching"] —— Swagger/OpenAPI 文档里的分组名，纯展示用途。
# 注意：prefix 末尾不要加斜杠，装饰器里的路径要以 "/" 开头，FastAPI 才能正确拼接。
router = APIRouter(prefix="/api/v1", tags=["teaching"])


# 内部小工具：从请求里解析出当前用户身份。
# 为什么单独包一层？
#   1) 每个端点都写 principal_from_request(request) 太啰嗦，抽出来更整齐；
#   2) 以后要统一加缓存、加日志或换鉴权实现，只改这一处；
#   3) 本文件没有用 FastAPI 的 Depends 依赖注入，而是显式调用，
#      这样端点内部随时能拿到 p 去判断 tenant_id、user_id。
# 参数：request —— FastAPI 注入的 Request 对象（包含全部请求头）。
# 返回：Principal（user_id、tenant_id、role、scopes、username）。
# 异常：令牌无效/过期/账号被禁用 → security 层抛 401 HTTPException。
def _p(request: Request) -> Principal:
    # 真正干活的一行：读 Authorization: Bearer <JWT> → 验签 → 查账号状态 → 组装 Principal。
    # demo 模式（AUTH_MODE 非 strict）下没有令牌也不报错，而是用 X-User-Id/X-Tenant-Id 兜底，
    # 这正是本地开发时不用登录也能调接口的原因。
    return principal_from_request(request)


# 业务异常 → HTTP 异常的“翻译器”。
# 背景：assignments.py 和 sessions.py 是纯业务模块，不 import FastAPI，
#       出错时抛自己的 AssignmentError / SessionError（字段：status/code/message/details）。
#       路由层负责把它们转换成 HTTPException，FastAPI 才能返回正确状态码和 JSON。
# 用法：return _handle(assignments.create_assignment, p, p.tenant_id, payload)
#       —— 第一个参数是要调用的函数，后面的位置参数/关键字参数原样透传。
# 为什么不用装饰器？因为有些端点要在调用前后做额外检查（如先 require_teacher），
#       显式调用更直观，也更容易在异常处下断点。
# 返回值：被调函数的返回值原样返回（通常是 dict）。
def _handle(fn, *args, **kwargs):
    # try 包住整个业务调用：只有业务层主动抛出的两类“可预期异常”会被翻译，
    # 其它异常（数据库连接失败、KeyError 等）继续向上冒泡，由 main.py 兜底成 500。
    try:
        # 真正执行：把调用方给的参数原样传给业务函数，并立刻返回结果。
        # 这里是 return，所以成功路径不会走到下面的 except。
        return fn(*args, **kwargs)
    # 分支一：作业领域的业务异常（如 404 作业不存在、403 无权、409 未开始作答、422 缺学生）。
    # as exc 把异常对象绑定到 exc，方便读取它的四个属性：status / code / message / details。
    except assignments.AssignmentError as exc:
        # 转成 HTTPException：
        #   status_code=exc.status —— 业务层已经定好的 HTTP 状态码（404/403/409/422...）；
        #   detail 是 dict，main.py 的 http_error_handler 会读取其中的 code/message；
        #   **exc.details 把附加上下文字典展开合并进来（例如 {"assignment_id": ...}）。
        # 小坑：若 details 里也有 "code"/"message" 键，会覆盖前面的同名键（字典后面的优先）。
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message, **exc.details})
    # 分支二：会话领域的业务异常（如 409 REPORT_NOT_READY、404 SESSION_NOT_FOUND）。
    # 结构与上面完全一致，因为两个异常类的字段约定相同（status/code/message/details）。
    except sessions.SessionError as exc:
        # 同样翻译成 HTTPException；这里的 raise 发生在 except 块内，
        # Python 会自动把原异常挂到 __context__ 上，日志里仍能看到原始堆栈，便于排查。
        raise HTTPException(status_code=exc.status, detail={"code": exc.code, "message": exc.message, **exc.details})


# -----------------------------------------------------------------------------
# 接口 1：GET /api/v1/teaching/students —— 教师端“选择学生”下拉框的数据源
# -----------------------------------------------------------------------------
# 装饰器说明：
#   @router.get(...) 表示只接受 GET（读取操作，幂等，不应修改数据）；
#   路径拼接 prefix 后是 /api/v1/teaching/students；
#   没有声明 response_model，FastAPI 直接把返回的 dict 序列化成 JSON。
@router.get("/teaching/students")
def students(request: Request):
    # 第一步：解析身份（未登录或令牌失效会在这里抛 401）。
    p = _p(request)
    # 第二步：权限校验——只有教师或管理员能看学生名单，学生调用得到 403 FORBIDDEN。
    # 注意 require_teacher 没有返回值，校验通过就继续往下执行。
    require_teacher(p)
    # 第三步：查询本租户下“正常状态”的学生账号。
    # 这里用相邻字符串字面量自动拼接，最终是一条完整 SQL：
    #   SELECT user_id, username, display_name, status, created_at FROM app_user
    #    WHERE role='user' AND status='active' AND tenant_id=? ORDER BY created_at
    # 条件逐条解释：
    #   role='user'         —— 只要学生，排除 teacher/admin；
    #   status='active'     —— 排除被禁用的账号（禁用后不应再被布置作业）；
    #   tenant_id=?         —— 租户隔离，? 由第二个参数 (p.tenant_id,) 传入，防 SQL 注入；
    #   ORDER BY created_at —— 按注册时间正序，前端下拉框顺序稳定；
    # 没有 LIMIT：一个租户的学生通常有限；若学生很多，应补分页。
    rows = fetch_all(
        "SELECT user_id, username, display_name, status, created_at FROM app_user"
        # 只查学生、只查启用、只查本租户；参数是单元素元组，逗号不能省——(x) 只是加括号，不是元组。
        " WHERE role='user' AND status='active' AND tenant_id=? ORDER BY created_at", (p.tenant_id,)
    )
    # 第四步：统一包成 {"items": [...], "total": n}。
    # total 用 len(rows) 而不是再查一次 COUNT(*)：行都已经取回来了，省一次数据库往返。
    # rows 是 list[dict]，FastAPI 会递归地转成 JSON 数组。
    return {"items": rows, "total": len(rows)}


# -----------------------------------------------------------------------------
# 接口 2：GET /api/v1/teaching/documents —— 可用来出题的论文列表
# -----------------------------------------------------------------------------
# 业务含义：不是所有文档都能出题。只有同时满足下面四个条件的文档才出现在列表里，
# 这样前端“新建题卷”页面就不会让老师选到无法出题的文档。
@router.get("/teaching/documents")
def teaching_documents(request: Request):
    # 解析身份。
    p = _p(request)
    # 仅教师/管理员可用。
    require_teacher(p)
    # 查询满足“可出题”条件的文档，按页数从多到少排序（页数多通常内容更丰富，更适合出题）。
    # 完整 SQL（由下面几行相邻字符串拼成）：
    #   SELECT doc_id, title, doc_type, page_count, status, created_at FROM document
    #    WHERE tenant_id=? AND deleted_at IS NULL
    #      AND status IN ('ready','needs_review') AND page_count>=3
    #      AND EXISTS (SELECT 1 FROM question q WHERE q.doc_id=document.doc_id AND q.status='active')
    #    ORDER BY page_count DESC, created_at DESC LIMIT 200
    # 条件逐条解释：
    #   tenant_id=?                        —— 只看本租户的文档；
    #   deleted_at IS NULL                 —— 软删除：删除时只写时间戳，查询要显式排除；
    #   status IN ('ready','needs_review') —— 解析完成或待复核的文档才可能有题目；
    #   page_count>=3                      —— 太短的文档出不了像样的题，直接过滤；
    #   EXISTS 子查询                      —— 至少有一道 status='active' 的题目；
    #                                         EXISTS 只判断“有没有”，找到一行就短路，比 JOIN+DISTINCT 高效；
    #   document.doc_id                    —— 子查询引用外层表，形成“相关子查询”；
    #   ORDER BY ... LIMIT 200             —— 最多 200 条，防止一次拉爆内存/响应体。
    # 参数 (p.tenant_id,) 是单元素元组，逗号不能省。
    rows = fetch_all(
        "SELECT doc_id, title, doc_type, page_count, status, created_at FROM document"
        # 租户隔离 + 软删除过滤 + 状态白名单 + 页数下限。
        " WHERE tenant_id=? AND deleted_at IS NULL AND status IN ('ready','needs_review') AND page_count>=3"
        # 相关子查询：该文档至少有一道 active 题目，才允许被选来出题。
        " AND EXISTS (SELECT 1 FROM question q WHERE q.doc_id=document.doc_id AND q.status='active')"
        # 页数多的排前面；同页数时新建的排前面；最多 200 条。
        " ORDER BY page_count DESC, created_at DESC LIMIT 200", (p.tenant_id,)
    )
    # 统一返回 {"items", "total"}；total 为本次返回条数（不是全库总数）。
    return {"items": rows, "total": len(rows)}


# -----------------------------------------------------------------------------
# 接口 3：POST /api/v1/teaching/papers —— 从一篇论文里抽题，生成一份“题卷”
# -----------------------------------------------------------------------------
# 请求体（JSON）示例：
#   {"document_id": "doc_xxx", "difficulty": "medium", "limit": 10, "title": "第三章随堂测"}
# 字段说明：
#   document_id —— 必填，论文 ID；
#   difficulty  —— 可选，easy/medium/hard/mixed，默认 mixed（不按难度过滤）；
#   limit       —— 可选，抽题数量，默认 10，会被夹到 1~30；
#   title       —— 可选，题卷标题，默认“论文标题 · 课堂题卷”，最长 120 字。
# 成功：201 Created + 新题卷的 set_id / question_ids 等。
# 失败：401 未登录、403 非教师、404 论文不存在、409 没有可用题目。
@router.post("/teaching/papers", status_code=201)
# status_code=201 表示“资源创建成功”（REST 约定），而不是默认的 200。
# 参数说明：
#   payload: dict   —— FastAPI 把请求体 JSON 解析成 dict；因为没有声明 Pydantic 模型，
#                      字段校验全靠函数体自己写，缺字段不会自动 422；
#   request: Request —— 需要它来解析身份。
# 两个参数都没有默认值，因此顺序随意。
def create_paper(payload: dict, request: Request):
    # 解析当前用户。
    p = _p(request)
    # 只有教师/管理员能建题卷。
    require_teacher(p)
    # 取 document_id：payload.get("document_id") 缺失时返回 None，
    # `or ""` 把 None 变成空串，str() 再保证一定是字符串——避免 None 传进 SQL。
    doc_id = str(payload.get("document_id") or "")
    # 查论文是否存在，且属于当前租户、未被软删除。
    # 这一行同时完成“存在性校验”和“越权校验”：
    #   论文属于别的租户时 WHERE 不匹配 → 查不到 → 下面返回 404，
    #   而不是 403，避免泄露“这个 ID 确实存在”的信息。
    # SELECT * 在这里可以接受：只取一行，后面要用到 doc["title"]。
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (doc_id, p.tenant_id))
    # 查不到 → 404。detail 使用统一的 {"code", "message"} 结构，前端按 code 提示用户。
    if not doc:
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "论文不存在"})
    # 取难度：没传或传空串时默认 "mixed"（混合难度）。
    # 注意这里不校验合法值：传 "abc" 也能通过，只是后面不会加难度过滤条件。
    difficulty = str(payload.get("difficulty") or "mixed")
    # 解析数量 limit，并做“防御式”处理：
    #   int(...) 可能因传入 "abc" 抛 ValueError → except 兜底成 10；
    #   max(1, min(30, n)) 把结果夹在 1~30：防止 0/负数（空题卷）或 10000（一次查太多）。
    try:
        limit = max(1, min(30, int(payload.get("limit") or 10)))
    # 捕获所有异常（ValueError、TypeError 等）：任何一种都说明 limit 不合法，退回默认 10。
    # 这里宽泛捕获是刻意的——数量限制不是关键业务，不值得为它返回 422。
    except Exception:
        limit = 10
    # 开始拼 SQL：先写固定的 WHERE 部分。
    # 用“先拼字符串、再统一执行”的写法，是因为难度条件是可选的、需要动态追加。
    # 安全性说明：拼进去的只有代码里写死的 SQL 片段，用户输入永远走 ? 参数，所以没有注入风险。
    sql = "SELECT * FROM question WHERE doc_id=? AND status='active'"
    # params 收集与 SQL 中 ? 一一对应的参数，顺序必须完全一致：
    #   第 1 个 ? ← doc_id（下面 if 里可能再追加两个）
    # 类型标注 list 只是给人看的，不影响运行。
    params: list = [doc_id]
    # 只有明确传了 easy/medium/hard 才按难度过滤；mixed 或其它值表示“不筛难度”。
    if difficulty in ("easy", "medium", "hard"):
        # 题目表里 difficulty 是 1~5 的整数，这里把三档映射成区间：
        #   easy → 1~2 题； medium → 3 题； hard → 4~5 题。
        # 用 BETWEEN 写闭区间，SQLite 与 PostgreSQL 都支持。
        ranges = {"easy": (1, 2), "medium": (3, 3), "hard": (4, 5)}[difficulty]
        # 追加难度条件；前面留一个空格，否则会和上一段 SQL 粘成 "...status='active'AND..."。
        sql += " AND difficulty BETWEEN ? AND ?"
        # 按 SQL 中 ? 的顺序追加两个边界值（列表 += 等价于 extend）。
        params += [ranges[0], ranges[1]]
    # 追加排序与数量限制。
    # ORDER BY created_at DESC：优先取最新生成的题（通常更贴合当前版本）；
    # LIMIT ? 用占位符而不是 f-string 拼数字，保持参数化风格统一。
    sql += " ORDER BY created_at DESC LIMIT ?"
    # 追加 limit，此时 params 与 SQL 里的 ? 完全对应：doc_id[, low, high], limit。
    params.append(limit)
    # 执行查询，拿到题目行（每行是 dict，含 q_id/stem/difficulty/...）。
    rows = fetch_all(sql, params)
    # 一道题都没抽到 → 409 Conflict（不是 404：论文存在，只是“当前没有可用题目”这种状态冲突）。
    # 提示语告诉老师下一步该做什么：先生成题目或复核题目。
    if not rows:
        raise HTTPException(status_code=409, detail={"code": "QUESTION_SET_EMPTY", "message": "该论文暂无可用的 active 题目，请先生成或复核题目"})
    # 只把题目 ID 抽出来组成列表：题卷保存“ID 列表”，题目内容仍留在 question 表，
    # 这样题目后续被修订时，题卷拿到的是最新内容（而不是快照）。
    # 列表推导式 [q["q_id"] for q in rows] 等价于 for 循环逐个 append。
    ids = [q["q_id"] for q in rows]
    # 生成题卷主键，形如 "set_1a2b3c4d5e"（前缀 + 10 位十六进制）。
    set_id = new_id("set")
    # 题卷标题：优先用请求里的 title；为空则用“论文标题 · 课堂题卷”兜底。
    # [:120] 截断到 120 个字符，防止超长标题撑爆列表页或数据库字段。
    title = str(payload.get("title") or f"{doc['title']} · 课堂题卷")[:120]
    # 用 with db() as conn 开启事务（db.py 的上下文管理器）：
    #   正常结束 → 自动 commit；抛异常 → 自动 rollback；无论如何 → 关闭连接。
    # 这里只有一条 INSERT，用 execute() 其实也行；用事务写法是为了与项目其它写操作保持一致，
    # 也为将来“插入题卷 + 写审计日志”这类多语句操作留好结构。
    with db() as conn:
        # 插入题卷记录。
        # 列与值一一对应（共 9 列 9 个 ?）：
        #   set_id         题卷主键
        #   doc_id         来源论文
        #   tenant_id      租户（后续所有查询都靠它隔离）
        #   title          标题
        #   difficulty     难度（可能是 mixed）
        #   question_ids   题目 ID 列表的 JSON 文本，如 '["q1","q2"]'
        #   question_count 题目数量（冗余字段，列表页不用解析 JSON 就能显示“共 N 题”）
        #   created_by     创建者 user_id
        #   created_at     ISO 时间字符串
        # SQL 分两段书写只是为了不超屏幕宽度，Python 会自动拼接成一条。
        conn.execute(
            "INSERT INTO question_set (set_id, doc_id, tenant_id, title, difficulty, question_ids, question_count,"
            " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            # 值元组，顺序必须与上面的列顺序完全一致：
            #   dump(ids) 把 Python 列表转成 JSON 文本（中文不转义）；
            #   len(ids) 是实际抽到的题数（可能小于请求的 limit）；
            #   p.user_id 是当前教师；iso() 是当前时间（东八区、秒级）。
            (set_id, doc_id, p.tenant_id, title, difficulty, dump(ids), len(ids), p.user_id, iso()),
        )
    # 返回新题卷的关键信息，前端可直接跳到“题卷详情/布置作业”页。
    # 这里返回 question_ids，是为了让前端立刻知道抽到了哪些题，不必再发一次请求。
    # 多行 dict 字面量的换行只是排版，Python 会自动合并成一个 dict。
    return {"set_id": set_id, "title": title, "document_id": doc_id, "difficulty": difficulty,
            "question_count": len(ids), "question_ids": ids}


# -----------------------------------------------------------------------------
# 接口 4：GET /api/v1/teaching/papers —— 教师已建的题卷列表
# -----------------------------------------------------------------------------
@router.get("/teaching/papers")
def papers(request: Request):
    # 解析身份。
    p = _p(request)
    # 仅教师/管理员。
    require_teacher(p)
    # 查题卷，并顺带把论文标题查出来（前端列表要显示“这份题卷来自哪篇论文”）。
    # 完整 SQL：
    #   SELECT s.*, d.title AS document_title FROM question_set s
    #    LEFT JOIN document d ON d.doc_id=s.doc_id
    #    WHERE d.deleted_at IS NULL AND s.tenant_id=?
    #    ORDER BY s.created_at DESC LIMIT 100
    # 逐条解释：
    #   s.*                        —— 题卷所有列；
    #   d.title AS document_title  —— 给文档标题起别名，避免和 s.title（题卷标题）重名；
    #   LEFT JOIN                  —— 语法上是左连接，即使文档不存在也保留题卷行；
    #   WHERE d.deleted_at IS NULL —— 但 WHERE 又要求文档未删除，
    #                                 两者结合后实际效果等于 INNER JOIN：文档已删的题卷不会出现；
    #                                 （这是 LEFT JOIN 常见的“语义陷阱”，读代码时要意识到）
    #   s.tenant_id=?              —— 按租户隔离；
    #   LIMIT 100                  —— 最多 100 条，按创建时间倒序（最新在前）。
    rows = fetch_all(
        "SELECT s.*, d.title AS document_title FROM question_set s"
        # 先按 doc_id 左连接文档表，再用 WHERE 过滤软删除与租户。
        " LEFT JOIN document d ON d.doc_id=s.doc_id WHERE d.deleted_at IS NULL AND s.tenant_id=?"
        # 最新创建的题卷排前面，最多 100 条。
        " ORDER BY s.created_at DESC LIMIT 100", (p.tenant_id,)
    )
    # 统一列表返回格式。
    return {"items": rows, "total": len(rows)}


# -----------------------------------------------------------------------------
# 接口 5：GET /api/v1/assignments —— 作业列表（教师看自己布置的，学生看被布置的）
# -----------------------------------------------------------------------------
# 与前面几个接口不同：这里没有 require_teacher，因为学生也要能看自己的作业。
# 权限过滤在业务层 assignments.list_assignments 里按角色完成：
#   role == "user" 或 mine=True → 只看 assignee_id = 自己；
#   role == "teacher"           → 只看 teacher_id = 自己；
#   role == "admin"             → 不加人员条件（可看全部）。
# 查询参数（FastAPI 自动从 ?mine=true&status=submitted&limit=20 解析并做类型转换）：
#   mine   —— 布尔，默认 False；想看“与我相关”的可置 True；
#   status —— 字符串，默认空；按 assigned/in_progress/submitted/reviewed 过滤；
#   limit  —— 整数，默认 50；业务层会夹到 1~200。
# 记住：写在函数签名里、又不是路径参数的，FastAPI 就按 query string 处理。
@router.get("/assignments")
def list_assignments(request: Request, mine: bool = False, status: str = "", limit: int = 50):
    # 解析身份（未登录直接 401）。
    p = _p(request)
    # 直接委托给业务层，结果也原样透传（业务层返回的是 list，这里包一层 items）。
    # 用关键字参数传 mine/status/limit，可读性更好，也不怕参数顺序变化。
    return {"items": assignments.list_assignments(p, mine=mine, status=status, limit=limit)}


# -----------------------------------------------------------------------------
# 接口 6：POST /api/v1/assignments —— 教师把题卷布置给某个学生
# -----------------------------------------------------------------------------
# 请求体由 assignments.create_assignment 自己解析（paper_id/set_id、assignee_id 或
# assignee_username、title、instructions、due_at 等），本层不做字段校验。
# 成功返回 201 + 作业详情（含题目列表）。
@router.post("/assignments", status_code=201)
def create_assignment(payload: dict, request: Request):
    # 解析身份。
    p = _p(request)
    # 只有教师/管理员能布置作业。
    require_teacher(p)
    # 交给业务层处理，并用 _handle 翻译可能抛出的 AssignmentError：
    #   p           —— 当前教师（业务层用 p.user_id 记 teacher_id）；
    #   p.tenant_id —— 租户，业务层会校验题卷属于同一租户；
    #   payload     —— 原始请求体。
    # 业务层可能的错误码：404 PAPER_NOT_FOUND / 422 ASSIGNEE_REQUIRED /
    #   409 ASSIGNEE_DISABLED / 404 DOC_NOT_FOUND / 422 INVALID_DUE_AT。
    return _handle(assignments.create_assignment, p, p.tenant_id, payload)


# -----------------------------------------------------------------------------
# 接口 7：GET /api/v1/assignments/by-session/{session_id} —— 用答题会话反查作业
# -----------------------------------------------------------------------------
# 典型场景：学生在答题页刷新后，手里只有 session_id，需要知道“这是哪次作业”。
# 路径参数 {session_id}：FastAPI 会把 URL 里那一段按 str 传给函数参数，并自动做 URL 解码。
# 路由顺序说明：本路由比 /assignments/{assignment_id} 多一段路径，
#   所以两者不会互相“吃掉”；但把更具体的路径写在前面仍是好习惯。
@router.get("/assignments/by-session/{session_id}")
def assignment_by_session(session_id: str, request: Request):
    # 解析身份。
    p = _p(request)
    # 按 session_id 查作业记录。
    # 只按 session_id 查、没带 tenant_id，是为了先拿到行再统一做权限判断；
    # session_id 是全局唯一主键，不会串到别的租户（真正的隔离在下一步 _can_view）。
    row = fetch_one("SELECT * FROM paper_assignment WHERE session_id=?", (session_id,))
    # 没有对应作业 → 404。
    # 如果 session 存在但不是“题卷作业”（例如自由导读会话），也会走到这里，
    # 提示语“该会话不是题卷作业”正是这个意思。
    if not row:
        raise HTTPException(status_code=404, detail={"code": "ASSIGNMENT_NOT_FOUND", "message": "该会话不是题卷作业"})
    # 资源级权限：只有管理员、布置者、被布置的学生能看，否则 403。
    # _can_view 定义在 assignments.py：role=="admin" or teacher_id==user_id or assignee_id==user_id。
    # 这里调用下划线开头的“私有函数”，说明本文件与 assignments 模块耦合较紧（同包内协作）。
    if not assignments._can_view(row, p):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权查看该作业"})
    # 转成对外的 API 结构；不返回题目和答题轮次，减少响应体（这里只需要作业元信息）。
    # include_questions=False / include_turns=False 是关键字参数（定义里用 * 强制了关键字传参）。
    return assignments._api(row, include_questions=False, include_turns=False)


# -----------------------------------------------------------------------------
# 接口 8：GET /api/v1/assignments/{assignment_id} —— 作业详情
# -----------------------------------------------------------------------------
# 权限与返回内容全部由 assignments.get_assignment 决定：
#   可见者：管理员 / 布置教师 / 被布置学生；否则 403 FORBIDDEN；
#   内容：含题目；教师或已提交的学生还能看到答题轮次与报告摘要；
#   不存在 → 404 ASSIGNMENT_NOT_FOUND（由 _handle 翻译）。
@router.get("/assignments/{assignment_id}")
def get_assignment(assignment_id: str, request: Request):
    # 解析身份（可能是学生，也可能是教师，权限判断在业务层）。
    p = _p(request)
    # 委托业务层：参数顺序是 (assignment_id, user)。
    # 复习：_handle 会把 AssignmentError 的 status/code/message/details 原样翻成 HTTP 响应。
    return _handle(assignments.get_assignment, assignment_id, p)


# -----------------------------------------------------------------------------
# 接口 9：POST /api/v1/assignments/{assignment_id}/start —— 学生开始作答
# -----------------------------------------------------------------------------
# 业务行为（assignments.start_assignment）：
#   1) 只有被布置的学生（或教师/管理员）能开始，否则 403；
#   2) 若已开始过（session_id 已存在），直接返回原会话，实现“幂等”；
#   3) 否则按题卷里的 question_ids 创建一个 mode="exam" 的答题会话，
#      并把作业状态改成 in_progress、记录 started_at。
# 成功返回 {"assignment": {...}, "session": {...}}，前端据此跳进答题页。
@router.post("/assignments/{assignment_id}/start")
def start_assignment(assignment_id: str, request: Request):
    # 解析身份。
    p = _p(request)
    # 交给业务层；可能的错误：403 FORBIDDEN、409 QUESTION_SET_EMPTY、404 ASSIGNMENT_NOT_FOUND。
    return _handle(assignments.start_assignment, assignment_id, p)


# -----------------------------------------------------------------------------
# 接口 10：POST /api/v1/assignments/{assignment_id}/submit —— 学生交卷
# -----------------------------------------------------------------------------
# 业务行为（assignments.submit_assignment）：
#   1) 只有本人（或教师/管理员）能交，否则 403；
#   2) 已提交/已复盘的重复提交是幂等的：直接返回当前作业，不会重复判分；
#   3) 未开始作答 → 409 ASSIGNMENT_NOT_STARTED；
#   4) 正常流程：sessions.finish_session 结束会话、生成报告，
#      再把作业状态改成 submitted，并回填 score / report_id / submitted_at。
@router.post("/assignments/{assignment_id}/submit")
# payload 声明为 `dict | None = None`：请求体可以完全省略（学生只点“交卷”按钮），
# 此时 FastAPI 传 None；如果传了，就必须是 JSON 对象。
def submit_assignment(assignment_id: str, request: Request, payload: dict | None = None):
    # 解析身份。
    p = _p(request)
    # 空请求体统一成空字典，后面就能安全地 payload.get(...)，不用写 if payload。
    payload = payload or {}
    # 交给业务层，并额外传 reason（交卷原因，写进会话事件日志，便于审计/统计）。
    # reason 取值：请求体里的 reason，缺省为 "student_submit"（学生主动交卷）；
    # 系统自动收卷时业务层会传别的值（如 timeout）。
    # str(... or "...") 保证一定是非空字符串，避免 None 落库。
    return _handle(assignments.submit_assignment, assignment_id, p, reason=str(payload.get("reason") or "student_submit"))


# -----------------------------------------------------------------------------
# 接口 11：POST /api/v1/assignments/{assignment_id}/review —— 教师复盘打分
# -----------------------------------------------------------------------------
# 业务行为（assignments.review_assignment）：
#   1) 只有管理员或布置该作业的教师能复盘，否则 403；
#   2) 请求体可带 comment/teacher_comment（评语，截断到 3000 字）与 score（分数）；
#   3) score 缺省时沿用会话得分；无法转成 float 时置 None，不报错；
#   4) 更新作业状态为 reviewed，记录 reviewed_at。
# 注意：payload 在这里是必填的（没有默认值），空请求体会被 FastAPI 判为 422。
@router.post("/assignments/{assignment_id}/review")
def review_assignment(assignment_id: str, payload: dict, request: Request):
    # 解析身份。
    p = _p(request)
    # 角色级权限：只有教师/管理员能复盘（资源级“是否自己的作业”由业务层再判一次）。
    require_teacher(p)
    # 交给业务层：payload 原样传入，由 review_assignment 解析 comment/score。
    return _handle(assignments.review_assignment, assignment_id, p, payload)


# -----------------------------------------------------------------------------
# 接口 12：GET /api/v1/assignments/{assignment_id}/report —— 查看作业报告
# -----------------------------------------------------------------------------
# 报告本身存在 report 表里（由 sessions.finish_session 生成），本接口只做三件事：
#   1) 取作业行；2) 校验可见性；3) 确认报告已生成后，读取并返回报告内容。
# 权限：管理员 / 布置教师 / 被布置学生（_can_view）。
# 错误：403 FORBIDDEN（无权）、409 REPORT_NOT_READY（还没交卷/没生成报告）。
@router.get("/assignments/{assignment_id}/report")
def assignment_report(assignment_id: str, request: Request):
    # 解析身份。
    p = _p(request)
    # 取作业行；不存在时 _assignment_row 会抛 AssignmentError(404, "ASSIGNMENT_NOT_FOUND")。
    # 注意：这一行没有走 _handle，所以“作业不存在”不会被翻译成 404，
    # 而会落到 main.py 的兜底异常处理器变成 500 INTERNAL_ERROR——
    # 这是本文件里唯一一处不一致，读代码/排查问题时值得留意。
    row = assignments._assignment_row(assignment_id)
    # 资源级权限校验：看不见就直接 403，不继续读报告。
    if not assignments._can_view(row, p):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权查看该作业报告"})
    # report_id 是交卷后回填的；为空说明还没生成报告（未交卷，或报告任务失败）。
    # 用 409 而不是 404：作业存在，只是“报告还没准备好”这个状态不允许现在取。
    if not row.get("report_id"):
        raise HTTPException(status_code=409, detail={"code": "REPORT_NOT_READY", "message": "作业尚未生成报告"})
    # 读取报告：sessions.get_report 会先按 session_id + tenant_id 校验会话归属，
    # 再把 report 表的 JSON 字段解析成对象返回；若报告缺失且会话未结束，
    # 它会抛 SessionError(409, "REPORT_NOT_READY")，由 main.py 的全局处理器转成 JSON 错误。
    return sessions.get_report(row["session_id"], row["tenant_id"])
