# =============================================================================
# 文件：app/platform_api.py —— V2.0 平台状态 API（管理员）
# =============================================================================
# 【这个文件做什么】
#   整个文件只提供一个只读接口：GET /api/v1/admin/platform/status。
#   它把“平台各组件现在是否正常”汇总成一个 JSON，供管理后台 / 运维人员一眼查看：
#     health     —— 依赖健康检查：数据库、Redis、对象存储、任务队列；
#     migrations —— 数据库迁移台账：哪些版本已执行、哪些还没执行；
#     vectors    —— 当前租户在 pgvector 里的向量条数与覆盖文档数；
#     documents  —— 全平台尚未软删除的文档总数。
#   本文件不写库、不改配置、不触发迁移，只做“读状态 + 汇总”，可以安全地反复调用。
#
# 【在整个应用中的位置】
#   - 本文件属于后端包 app/，与 admin.py、quality_api.py 等平级，都是 FastAPI 的路由模块；
#   - app/main.py 启动时执行 `from . import platform_api`，随后
#     `app.include_router(platform_api.router)` 把这里的 router 挂到全局 app；
#   - router 自带 prefix="/api/v1/admin/platform"，装饰器里只写 "/status"，
#     所以最终对外 URL 是 /api/v1/admin/platform/status；
#   - tags=["platform"] 只影响 OpenAPI/Swagger 文档的分组，不影响鉴权与业务逻辑；
#   - 它依赖平台层包 alun.platform（migrations/observability/vector_store）以及
#     app 内部的 db/security，本身不直接持有数据库连接。
#
# 【主要调用关系（一次请求的旅程）】
#   浏览器 / 运维脚本
#     └─ GET /api/v1/admin/platform/status（请求头 Authorization: Bearer <JWT>）
#          └─ FastAPI 匹配到 platform_status()
#               ├─ security.principal_from_request(request)
#               │    解析 JWT（HS256 验签 + exp 过期校验），必要时查 app_user 确认账号未禁用，
#               │    返回 Principal(user_id, tenant_id, scopes, role, username)
#               ├─ security.require_admin(p)
#               │    role != "admin" → 403 {"code": "FORBIDDEN", ...}
#               ├─ observability.refresh_dependencies()
#               │    逐个探测 database / redis / storage / queue，并刷新 Prometheus 指标
#               ├─ migrations.status()      失败只记录 error，不抛出
#               ├─ vector_store.stats(...)  失败只记录 error，不抛出
#               ├─ db.fetch_one("SELECT count(*) ...")
#               └─ return dict → FastAPI 用 jsonable_encoder 转成 JSON 响应
#
# 【成功响应示例（SQLite 本地开发环境）】
#   {
#     "health": {
#       "database": {"backend": "sqlite", "ok": true},
#       "redis": {"backend": "memory", "ok": true},
#       "storage": {"backend": "local", "ok": true},
#       "queue": {"backend": "inline", "ok": true, "depth": 0}
#     },
#     "migrations": {"backend": "sqlite", "applied": [], "pending": [1, 2]},
#     "vectors": {"error": "pgvector 仅在 ALUN_DB_BACKEND=postgres 时可用"},
#     "documents": 42
#   }
#
# 【错误码约定】
#   200 —— 成功；即使 migrations/vectors 探测失败也仍是 200，失败信息放在 error 字段里；
#   401 —— 未登录 / JWT 无效 / 已过期 / 账号被禁用（由 principal_from_request 抛出）；
#   403 —— 已登录但不是管理员（由 require_admin 抛出，code=FORBIDDEN）；
#   404 —— 路径写错；405 —— 请求方法不是 GET（这两类由 FastAPI 框架层返回）；
#   500 —— 文档计数 SQL 执行失败等“未兜底”的异常（本文件只在两处做了降级捕获）。
#
# 【设计取舍与阅读提示】
#   - 只有两个 try/except，且都宽泛捕获 Exception：这是状态接口的刻意降级设计——
#     迁移库连不上、pgvector 未启用时，仍要把其他健康信息返回给运维，而不是整页 500；
#   - 代价是异常被吞掉，访问日志里看不到 5xx，排查时要看响应中的 migrations.error / vectors.error；
#   - 错误信息统一截断到 200 个字符，避免把完整 DSN、堆栈或超长驱动报错直接暴露给前端；
#   - 接口用同步 def：FastAPI 会把同步端点放进线程池执行，阻塞的数据库/网络探测不会卡住事件循环；
#   - 文档计数是“全平台”口径（没有 tenant_id 条件），因为这是平台管理员的总览接口；
#   - 本文件没有自定义常量、类或循环，全部逻辑集中在一个函数里。
# =============================================================================
# 模块文档字符串（原文件保留）：一句话说明模块用途。
"""V2.0 平台状态 API（管理员）。"""
# 启用“延迟求值”的类型注解（PEP 563）：注解不在运行时求值，而是保存为字符串。
# 好处：可以使用 X | Y 这类新语法，也能避免模块间类型引用造成循环导入。
# 注意：它必须是模块 docstring 之后的第一个语句，所以只能紧跟在这里。
from __future__ import annotations

# 从 FastAPI 导入两个构件：
#   APIRouter —— 路由分组器，本文件用它统一前缀与文档标签；
#   Request   —— 当前 HTTP 请求对象，FastAPI 会按类型自动注入，用来读取请求头中的身份信息。
from fastapi import APIRouter, Request

# 从项目根包 alun 下的 platform 子包导入三个“平台能力”模块（.. 表示从 app 上跳一级）：
#   migrations    —— V2.0 数据库迁移（pgvector、platform_job 等）与版本台账；
#   observability —— 可观测性：依赖健康探测 refresh_dependencies()、Prometheus 指标、日志；
#   vector_store  —— pgvector 向量索引的统计/检索，本文件只调用 stats()。
from ..platform import migrations, observability, vector_store
# 从同包 db.py 导入单行查询助手：内部用 with db() 打开连接，执行完自动提交并关闭。
# 返回 dict（sqlite3.Row 已转字典）；查不到时返回 None；SQL 占位符写 ?，PostgreSQL 模式会自动转 %s。
from .db import fetch_one
# 从同包 security.py 导入身份与权限工具：
#   principal_from_request —— 解析 Authorization: Bearer JWT（或 demo 模式兜底），得到 Principal；
#   require_admin          —— 角色不是 admin 时抛 403。
from .security import principal_from_request, require_admin

# 创建本文件专属路由器（模块级常量，导入本模块时就会执行）。
#   prefix="/api/v1/admin/platform" —— 下面所有路由的公共前缀；
#   tags=["platform"]               —— 仅用于 Swagger/OpenAPI 分组展示。
# 这里没有写 dependencies=[...]，鉴权是在处理函数内部显式调用的（见下）。
router = APIRouter(prefix="/api/v1/admin/platform", tags=["platform"])


# 注册 GET 路由。为什么用 GET：这是纯读取、幂等、无副作用的查询，符合 HTTP 语义，
# 也方便浏览器、curl、监控系统直接调用（可安全重试）。
# "/status" 与上面的 prefix 拼接后，真实路径为 /api/v1/admin/platform/status。
# 装饰器在“导入模块”时执行，把这条路由登记进 router.routes；没有声明 response_model，
# 因此响应是自由格式 JSON（OpenAPI 里 schema 为空），由 FastAPI 的 jsonable_encoder 序列化。
@router.get("/status")
# 处理函数。函数名 platform_status 会参与生成 OpenAPI 的 operationId。
# 参数 request: Request 由 FastAPI 自动注入（客户端不需要传）；本接口没有路径/查询/请求体参数。
# 用同步 def 而不是 async def：函数里全是阻塞的数据库/网络探测，
# FastAPI 会把同步函数放到线程池执行，避免阻塞事件循环。
def platform_status(request: Request):
    # 第一步：解析调用者身份。principal_from_request 内部会：
    #   1) 读取 Authorization 头；以 "Bearer " 开头则按 HS256 校验签名与 exp；
    #   2) 用 JWT 的 sub 查 app_user.status，账号被禁用则抛 401 ACCOUNT_DISABLED；
    #   3) 没有令牌时：strict 模式抛 401 UNAUTHORIZED；demo 模式回退为本地演示用户（role="user"）。
    # 返回值 p 是 security.Principal 数据类：user_id/tenant_id/scopes/role/username。
    p = principal_from_request(request)
    # 第二步：权限闸门。只有 role == "admin" 才放行；
    # 否则抛 HTTPException(403, detail={"code": "FORBIDDEN", "message": "需要管理员权限"})。
    # 注意：demo 模式默认角色是 user，所以没有真实管理员 JWT 时这里一定 403。
    require_admin(p)
    # 第三步：先放入健康检查结果，再逐个补充其他板块。
    # 先构造字典可以保证响应键顺序稳定（health → migrations → vectors → documents），
    # 即使后面某个探测失败，已经拿到的部分也不会丢。
    # observability.refresh_dependencies() 内部会：
    #   - 执行 SELECT 1 探测数据库（失败写入 database.error，不抛出）；
    #   - 探测 Redis、对象存储、队列深度（各自内部已兜底）；
    #   - 若安装了 prometheus_client，顺便更新依赖健康/队列深度指标。
    out = {"health": observability.refresh_dependencies()}
    # 第四步：查询数据库迁移状态。单独包 try 的原因：
    # migrations.status() 在 PostgreSQL 模式下要新建连接、读 platform_schema 表，
    # 数据库没起、账号密码错、网络不通都会抛异常；状态接口不应因此整体 500。
    try:
        # 正常返回形如 {"backend": "postgres", "applied": [1, 2], "pending": []}；
        # SQLite 模式不会连库，直接返回 {"backend": "sqlite", "applied": [], "pending": [1, 2]}。
        out["migrations"] = migrations.status()
    # 宽泛捕获 Exception 是这里的设计选择：把“迁移信息不可用”降级成响应里的一个字段。
    # 它不会捕获 KeyboardInterrupt / SystemExit 这类 BaseException。
    except Exception as exc:
        # 只保留前 200 个字符：避免把完整连接串、堆栈或超长驱动报错暴露给前端；
        # 响应仍是 200，前端通过 migrations 里有没有 error 键来判断该板块是否可用。
        out["migrations"] = {"error": str(exc)[:200]}
    # 第五步：统计当前租户的向量索引。与迁移同样的降级策略：失败只影响 vectors 板块。
    try:
        # p.tenant_id 来自 JWT 的 tenant_id 声明（无令牌 demo 模式下来自 X-Tenant-Id 头），
        # 作为租户隔离条件，保证只统计本租户的 document_embedding。
        # vector_store.stats() 只支持 PostgreSQL + pgvector；SQLite 模式会抛 RuntimeError，
        # 因此本地开发时这里通常返回 {"error": "pgvector 仅在 ALUN_DB_BACKEND=postgres 时可用"}，
        # 这是“功能未启用”的正常提示，不是接口故障。
        out["vectors"] = vector_store.stats(p.tenant_id)
    except Exception as exc:
        # 同上：错误信息截断后放入 error 字段，接口继续返回其他板块。
        out["vectors"] = {"error": str(exc)[:200]}
    # 第六步：统计全平台文档数（平台管理员视角，因此没有 tenant_id 过滤）。
    # SQL 说明：
    #   count(*)           —— 统计行数；即使一条都没有也会返回一行 0，不会返回 NULL；
    #   AS c               —— 给结果列起别名，下面用 ["c"] 取值；
    #   deleted_at IS NULL —— 只算未软删除的文档（软删除只写 deleted_at 时间，不物理删行）。
    # fetch_one 返回 dict（如 {"c": 42}）；这里没有用户输入，所以不需要参数化占位符。
    # 注意：这一行不在 try 里，数据库真的不可用时异常会向上抛，最终由 FastAPI 变成 500。
    out["documents"] = fetch_one("SELECT count(*) AS c FROM document WHERE deleted_at IS NULL")["c"]
    # 第七步：返回汇总字典。FastAPI 会用 jsonable_encoder 递归转换，
    # 再序列化为 JSON 响应（默认状态码 200，Content-Type: application/json）。
    # 前端拿到的四个键：health / migrations / vectors / documents。
    return out
