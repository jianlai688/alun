# =============================================================================
# 文件：app/vector_api.py
# 主题：V2.0 pgvector 语义检索 API（FastAPI 路由层）
#
# 一、本文件的职责
# 1. 向客户端暴露“文档向量索引”“索引状态”“语义检索”三个 HTTP 接口。
# 2. 在进入向量存储之前完成身份解析、文档存在性检查、租户隔离，并在索引接口上检查归属权限。
# 3. 把“索引/检索”调用中可识别的 RuntimeError 转换成统一的 HTTP 409 错误响应。
# 4. 本文件不负责生成 embedding、不直接执行 pgvector SQL；这些工作委托给
#    platform/vector_store.py。它只做“HTTP 请求 -> 业务调用 -> HTTP 响应”的编排。
#
# 二、在整个应用中的位置
# - 本文件位于 Python 包 alun.app 中，完整模块名为 alun.app.vector_api。
# - app/main.py 通过 `from . import vector_api` 导入本模块，并在创建 FastAPI 应用后
#   通过 `app.include_router(vector_api.router)` 把这里定义的路由挂载到总应用。
# - 这里创建的是 APIRouter 而不是 FastAPI 实例：APIRouter 只负责收集路由，
#   最终由 main.py 中的 FastAPI 应用统一提供服务。
#
# 三、主要调用关系（请求自上而下流动）
# 客户端 / 浏览器
#   -> app/main.py 中的 FastAPI 应用（负责找到匹配的路由）
#   -> 本文件的路由函数（负责鉴权、校验、调用和错误码转换）
#   -> app/security.py 的 principal_from_request（解析 Authorization 头，得到 Principal）
#   -> app/db.py 的 fetch_one（查询 document 表，兼容 SQLite/PostgreSQL）
#   -> platform/vector_store.py（index_document / stats / search，访问 pgvector）
#   -> 返回 Python dict/list，由 FastAPI 自动序列化为 JSON 响应。
#
# 四、接口一览
# 1. POST /api/v1/documents/{document_id}/embeddings
#    函数：index_embeddings。为指定文档建立/重建向量索引。
#    权限：管理员（role == "admin"）或该文档的所属用户。
#    主要错误：404 文档不存在；403 无权操作；409 向量后端不可用。
# 2. GET /api/v1/documents/{document_id}/embeddings/status
#    函数：embedding_status。查询向量索引统计信息。
#    权限：只校验文档属于当前租户且未删除，不额外校验文档所有者或管理员角色。
#    注意：底层 stats() 返回的是“整个租户”的统计，不是该 document_id 的单独统计。
# 3. GET /api/v1/search/semantic
#    函数：semantic_search。在当前租户范围内做语义检索。
#    参数：q 必填；document_id 可选；limit 默认 8（底层会限制在 1~50）。
#    主要错误：422 查询为空；409 向量后端不可用。
#
# 五、权限与多租户说明
# - 所有接口都先调用 principal_from_request(request) 得到当前请求主体 p。
# - p.tenant_id 来自 JWT 载荷（或演示模式的 X-Tenant-Id 头），后续查询都带 tenant_id，
#   避免一个租户读到另一个租户的文档或向量。
# - 文档查询还要求 deleted_at IS NULL，因此“已软删除”的文档会被当作不存在。
# - index_embeddings 额外判断“管理员或文档所有者”，status 和 search 不做这层额外判断。
#
# 六、错误响应约定
# - HTTPException 的 detail 统一是字典：{"code": "...", "message": "..."}。
# - 404 + DOC_NOT_FOUND：当前租户下找不到未删除的文档。
# - 403 + FORBIDDEN：当前用户既不是管理员，也不是该文档的所有者。
# - 409 + VECTOR_UNAVAILABLE：pgvector 未启用或向量后端运行失败（仅索引/检索接口做了转换）。
# - 422 + QUERY_EMPTY：q 去掉首尾空白后为空。
# - 401 由 principal_from_request / decode_token 在更早阶段抛出（如令牌无效）。
# - 索引/检索接口只捕获 RuntimeError；状态接口没有 try/except，其他异常通常成为 500。
#
# 七、异步与线程模型
# - 下面三个处理函数都使用同步 `def`，不是 `async def`。FastAPI 会把同步路由放到
#   线程池执行，因此其中的同步数据库/psycopg 调用不会直接阻塞事件循环。
# - 这并不意味着可以无限并发：线程池大小、数据库连接数和 pgvector 查询耗时仍是瓶颈。
#
# 八、阅读提示
# - decorator（@router.post / @router.get）在模块导入时执行，负责登记“路径 + HTTP 方法
#   -> 函数”的映射；真正处理请求时才会调用被装饰的函数。
# - 函数参数中的 document_id 来自 URL 路径；request 由 FastAPI 注入；q/document_id/limit
#   来自查询字符串，FastAPI 会根据类型注解完成基础类型转换和校验。
# =============================================================================
"""V2.0 pgvector 语义检索 API。"""
# 上面这一行是模块级文档字符串（docstring），也是本模块的简短说明。
# 注释版保留其原文和位置，使模块的 __doc__ 与源文件保持一致。
# 下面这行是“未来导入”：让所有类型注解延迟求值，避免运行时立刻解析注解。
# 这样 `str | None` 这类写法在较老的 Python 版本中也不会因为求值而报错。
# 它必须位于模块 docstring 之后、其他可执行导入之前，因此不能随意移动。
from __future__ import annotations

# 从 FastAPI 导入路由与异常相关对象：
# - APIRouter：路由收集器，用来声明一组同前缀、同标签的接口；
# - HTTPException：主动中断请求并返回指定 HTTP 状态码/响应体的标准异常；
# - Request：封装当前 HTTP 请求，可读取请求头并交给身份解析函数使用。
# 这里只导入实际用到的三个名字，避免把整个 fastapi 命名空间引入模块。
from fastapi import APIRouter, HTTPException, Request

# 相对导入：当前模块是 alun.app.vector_api，`..app.db` 表示回到 alun 包后
# 再定位 app.db 模块。fetch_one 执行“查询一行并返回字典”的通用数据库操作。
# 使用相对导入可以让整个 alun 包被移动或改名时，内部依赖仍然成立。
from ..app.db import fetch_one
# 从安全模块导入请求身份解析器：它读取 Authorization: Bearer <JWT>，
# 校验签名/过期时间并返回 Principal（至少包含 user_id、tenant_id、role）。
# 本文件的权限判断全部基于这个 Principal，而不是直接信任客户端传来的用户字段。
from ..app.security import principal_from_request
# 从平台层导入向量存储模块。这里导入的是“模块对象”，后面通过
# vector_store.index_document / stats / search 调用具体函数，便于测试时替换模块实现。
from ..platform import vector_store

# 创建一个 APIRouter 实例，统一加两个元数据：
# - prefix="/api/v1"：本文件所有路径都会自动加上这个前缀；
# - tags=["vector"]：在自动生成的 OpenAPI/Swagger 文档中归入 vector 分组。
# 该 router 之后由 app/main.py 通过 app.include_router(...) 挂到总应用上。
# 注意：这一步是模块导入时执行的普通语句，不是请求处理逻辑。
router = APIRouter(prefix="/api/v1", tags=["vector"])


# =============================================================================
# 接口 1：为文档建立/重建向量索引
# 最终完整路径：POST /api/v1/documents/{document_id}/embeddings
# 设计意图：先确认“文档真实存在且属于当前租户”，再确认“当前用户有权操作”，
# 最后才调用可能较慢、可能失败的向量索引逻辑。
# =============================================================================
# 用 router.post 把下面的函数注册为 POST 接口。
# `{document_id}` 是路径参数占位符，FastAPI 会把它传给同名函数参数。
# 装饰器写在函数上方，模块导入时完成注册，但只有收到请求时才执行函数体。
@router.post("/documents/{document_id}/embeddings")
# 定义同步处理函数：
# - document_id: str：从 URL 路径取得的文档 ID；
# - request: Request：由 FastAPI 自动注入的请求对象，用于解析当前身份。
# 使用同步 def 的原因见文件头“异步与线程模型”：FastAPI 会把它放进线程池执行。
# 函数没有显式返回类型注解，实际成功时返回 vector_store.index_document 的 dict。
def index_embeddings(document_id: str, request: Request):
    # 解析当前请求主体，得到 p（Principal）。
    # 这里可能抛出 401：例如 Bearer 令牌签名错误、过期，或 strict 模式缺少令牌。
    # 后续所有租户/用户判断都只使用服务端解析出的 p，不直接信任请求头中的身份。
    p = principal_from_request(request)
    # 查询当前租户下、指定 ID 且未被软删除的文档。
    # SQL 逐段解释：
    # - SELECT *：本函数只关心文档是否存在、属于哪个用户，读取整行最直接；
    # - FROM document：文档元数据表；
    # - doc_id=?：第一个占位符由参数元组中的 document_id 填充；
    # - tenant_id=?：第二个占位符由 p.tenant_id 填充，实现租户隔离；
    # - deleted_at IS NULL：过滤软删除记录，已删除的文档视为不存在。
    # 这里使用 ? 占位符而不是字符串拼接，既能防止 SQL 注入，也由 db 层自动适配
    # PostgreSQL 的 %s 占位符。参数顺序必须与 SQL 中的占位符顺序完全一致。
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    # fetch_one 找不到记录时返回 None，因此这里同时覆盖“ID 不存在”“属于别的租户”
    # “文档已软删除”三种情况。对外统一返回 404，避免泄露其他租户文档是否存在。
    if not doc:
        # 主动抛出 404。detail 使用结构化字典，方便前端根据 code 做稳定判断，
        # message 则用于直接展示给用户。raise 后本函数立即结束，不会继续索引。
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    # 权限判断：管理员可以操作本租户内任意文档；非管理员必须是文档所有者。
    # `and` 的短路特性：如果当前用户是管理员，右侧的所有者比较不会执行；
    # 如果角色不是 admin，才比较 document 表中的 user_id 与当前用户 ID。
    # doc 是字典，使用 .get("user_id") 可在字段缺失时返回 None，避免 KeyError。
    if p.role != "admin" and doc.get("user_id") != p.user_id:
        # 已确认文档存在但当前主体无权操作，因此返回 403（禁止访问）。
        # 错误码 FORBIDDEN 与项目其他接口保持一致。
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权索引该文档"})
    # 用 try 包住向量存储调用。index_document 在 _conn() 中发现后端不是 postgres 时，
    # 会主动抛出 RuntimeError；本块把这种“可识别的后端不可用”转成 HTTP 409。
    # 注意：数据库连接失败、SQL 执行错误等可能不是 RuntimeError，本 except 不会捕获。
    try:
        # 真正执行索引：vector_store 会读取该文档的所有 chunk，重新计算 384 维向量，
        # 先删除旧向量再批量写入新向量，最后返回统计字典。
        # 传入 p.tenant_id 而不是客户端传值，确保写入的向量归属于当前租户。
        # 成功时常见返回：{"document_id": ..., "indexed": 数量, "dimensions": 384}；
        # 若文档没有 chunk，底层返回 {"indexed": 0, "reason": "no_chunks"}。
        return vector_store.index_document(document_id, p.tenant_id)
    # 只捕获 RuntimeError，并把原始错误文本放进 message，便于前端/运维定位。
    # 例如数据库后端不是 postgres 时，vector_store 会抛出“pgvector 仅在 ... 可用”。
    # 注意：这里没有使用 `raise ... from exc`，但 Python 会把当前异常记录为
    # 新异常的 __context__（隐式异常链）；底层异常类型不会按 500 暴露，而是转成 409。
    except RuntimeError as exc:
        # 409 Conflict 在此表示“当前服务状态无法完成索引”，业务错误码为 VECTOR_UNAVAILABLE。
        # str(exc) 保留底层可读原因；生产环境如需隐藏内部细节，可在这里做脱敏。
        raise HTTPException(status_code=409, detail={"code": "VECTOR_UNAVAILABLE", "message": str(exc)})


# =============================================================================
# 接口 2：查询向量索引状态
# 最终完整路径：GET /api/v1/documents/{document_id}/embeddings/status
# 该接口同样先确认文档在当前租户存在，然后返回向量统计信息。
# 注意：它不检查管理员/所有者，且 stats() 统计的是租户级总量。
# =============================================================================
# 注册 GET 路由。路径中的 {document_id} 与函数参数同名，FastAPI 自动完成绑定。
@router.get("/documents/{document_id}/embeddings/status")
# 同步处理函数，参数含义与上一个接口相同：
# document_id 来自 URL，request 用于解析身份；成功时返回 JSON 对象。
def embedding_status(document_id: str, request: Request):
    # 解析身份。可能因令牌无效/缺失（strict 模式）在此抛出 401。
    # 后续查询使用 p.tenant_id，而不是客户端可随意传入的租户参数。
    p = principal_from_request(request)
    # 与索引接口完全相同的文档可见性检查：
    # 只查当前租户、未软删除的文档，确保不会通过 ID 探测其他租户的数据。
    # `SELECT *` 这里只用于确认存在性；实际没有读取某个具体字段。
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    # 查不到就返回 404；这与索引接口保持一致的“文档不存在”语义。
    # 由于过滤了 tenant_id，其他租户的文档也会走到这里，避免跨租户信息泄露。
    if not doc:
        # 结构化 404 响应：code 供程序判断，message 供用户阅读。
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    # 调用 vector_store.stats(p.tenant_id) 获取统计信息。
    # 重要：这个函数只接收 tenant_id，没有接收 document_id，所以返回的是
    # 当前租户所有文档的 embeddings 总数和 documents 总数，而不是本路径文档的计数。
    # 变量名 conn_status 表示“连接/索引状态”，但实际内容是统计字典。
    # 若没有记录，底层返回 {"embeddings": 0, "documents": 0}。
    # 注意：与本文件另外两个接口不同，这一行没有 try/except；如果后端不是 postgres，
    # stats 内部的 RuntimeError 会继续向上冒泡，通常由全局处理成 500，而不是 409。
    conn_status = vector_store.stats(p.tenant_id)
    # 组装响应：先放入本次请求的 document_id，再用 `**conn_status` 展开统计字典。
    # 结果是 {"document_id": ..., "embeddings": ..., "documents": ...}。
    # 若 stats 返回的键与 document_id 重名，后面的展开会覆盖前者；当前实现不会冲突。
    # FastAPI 会自动把 dict 序列化成 JSON。
    return {"document_id": document_id, **conn_status}


# =============================================================================
# 接口 3：语义检索
# 最终完整路径：GET /api/v1/search/semantic?q=...&document_id=...&limit=8
# 设计意图：校验查询文本非空后，交给向量存储做租户内的相似度检索。
# =============================================================================
# 注册 GET 路由。该路径不含路径参数，所有输入来自查询字符串。
# 由于 router 有 prefix="/api/v1"，最终地址是 /api/v1/search/semantic。
@router.get("/search/semantic")
# 同步处理函数。参数来源与类型：
# - request: Request：FastAPI 注入，读取身份；
# - q: str：必填查询字符串参数；缺失或类型不符时 FastAPI 会在进入函数前返回 422；
# - document_id: str | None：可选，默认 None；用于把检索限制到单个文档；
# - limit: int：可选，默认 8；FastAPI 会尝试把查询串转换为整数。
# 返回：{"query": 原查询文本, "items": 检索结果列表}。
def semantic_search(request: Request, q: str, document_id: str | None = None, limit: int = 8):
    # 解析当前身份，取得租户 ID；检索范围由 p.tenant_id 决定，客户端不能越租户查询。
    # 令牌无效时会在这一行之前/此处抛出 401。
    p = principal_from_request(request)
    # 校验查询文本：strip() 去掉首尾空白（空格、换行、制表符等），
    # 若结果为空字符串，说明用户只输入了空白，没有实际检索内容。
    # 注意这里只做检查，不会把 q 改成 strip 后的值；后续返回和检索仍使用原始 q。
    if not q.strip():
        # 422 Unprocessable Entity：请求格式没问题，但语义上无法处理。
        # QUERY_EMPTY 是业务错误码，message 提示用户输入检索问题。
        # 这与 FastAPI 对缺失 q 的默认 422 保持同一状态码体系。
        raise HTTPException(status_code=422, detail={"code": "QUERY_EMPTY", "message": "请输入检索问题"})
    # 用 try 包住向量检索调用，统一把后端不可用转成 409。
    # search 内部会先做 embedding，再执行带 tenant_id 条件的 pgvector SQL；
    # 若传入 document_id，则额外增加 doc_id 条件；limit 会被限制在 1~50。
    # 注意：本函数没有单独查库确认 document_id 是否存在/属于谁；由于 SQL 同时限定
    # tenant_id，传入其他租户的文档 ID 不会越权，只会得到空列表（不存在的 ID 同理）。
    try:
        # 调用 vector_store.search 并立即组装响应：
        # - p.tenant_id：限定租户，防止跨租户检索；
        # - q：原始查询文本；
        # - document_id：可选文档过滤条件（None 表示全租户范围）；
        # - limit：返回条数上限，底层会做 max(1, min(50, limit)) 的兜底。
        # 成功时 items 是若干字典，通常包含 embedding_id、doc_id、node_id、
        # chunk_id、content 和 score（余弦相似度分数，越大越相似）。
        # 这里不对结果做二次排序或裁剪，直接信任 vector_store 的 ORDER BY / LIMIT。
        return {"query": q, "items": vector_store.search(p.tenant_id, q, document_id, limit)}
    # 只捕获 RuntimeError：例如未启用 postgres 后端、连接/查询阶段主动抛出的运行时错误。
    # 未被捕获的异常（如部分驱动异常）会继续向上冒泡，由全局异常处理形成 500。
    except RuntimeError as exc:
        # 返回 409 和 VECTOR_UNAVAILABLE，沿用与索引接口一致的错误契约。
        # message 直接使用异常文本；如果底层信息包含敏感连接串，应在部署层另行脱敏。
        raise HTTPException(status_code=409, detail={"code": "VECTOR_UNAVAILABLE", "message": str(exc)})
# =============================================================================
# 文件结束。
# 再次回顾三个接口的返回结构：
# - index_embeddings：{"document_id", "indexed", "dimensions"}，无 chunk 时可能带 reason；
# - embedding_status：{"document_id", "embeddings", "documents"}；
# - semantic_search：{"query", "items"}，其中 items 为相似 chunk 列表。
# 所有错误响应经 HTTPException 包装后形如：
# {"detail": {"code": "...", "message": "..."}}。
# =============================================================================
