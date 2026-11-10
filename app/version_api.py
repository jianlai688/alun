# 原文件已有的模块级文档字符串，保留不动；它同时也是本模块的第一条语句。
"""V1.2 结构版本 API。"""
# =============================================================================
# 【模块职责】
#   app/version_api.py 是 A论 后端 FastAPI 应用中的“结构版本 API”路由模块。
#   它把 app/versions.py 里的版本快照能力包装成 HTTP 接口，本身不实现快照算法。
#   对外提供三个接口：
#     GET  /api/v1/documents/{document_id}/versions
#     GET  /api/v1/documents/{document_id}/versions/{version_no}
#     POST /api/v1/documents/{document_id}/versions/{version_no}/restore
#
# 【在应用中的位置】
#   app/main.py 通过 `from . import version_api` 导入本模块，并执行
#   `app.include_router(version_api.router)` 把它挂到主 FastAPI 应用。
#   router 上的 prefix="/api/v1" 会与各装饰器里的路径拼接成最终 URL；
#   tags=["versions"] 用于 Swagger/OpenAPI 文档分组。
#
# 【主要调用关系】
#   浏览器/客户端
#     -> FastAPI 路由函数（本文件）
#     -> _p() -> security.principal_from_request()：解析并校验请求者身份
#     -> _doc() -> db.fetch_one()：校验文档存在、租户归属、软删除状态和访问权限
#     -> versions.list_versions()/get_version()/restore()：读写版本与结构表
#     -> 回滚成功后 pipeline.repair_sessions_for_document() 和
#        pipeline.repair_questions_for_document()：修复会话与题库中的旧结构引用
#
# 【版本数据模型】
#   document.doc_version 表示文档当前版本号；document_version 表按
#   (doc_id, version_no) 保存版本快照，快照内容是 node/chunk/knowledge_point/
#   evidence 四类结构数据的 JSON。快照由 versions.snapshot() 创建；
#   回滚由 versions.restore() 完成：回滚前自动快照 -> 恢复四类结构表 ->
#   document.doc_version 加 1 -> 回滚后再创建一次快照。
#
# 【认证、租户与错误码】
#   _doc() 的 SQL 同时按 doc_id 和 tenant_id 过滤，避免跨租户越权；非 admin
#   只能访问 user_id 等于自己的文档。鉴权失败通常由 security 层抛 401；
#   本文件显式返回 404(DOC_NOT_FOUND)、403(FORBIDDEN)、404(VERSION_NOT_FOUND)。
#   HTTPException.detail 统一使用 {"code": ..., "message": ...} 结构，
#   前端可以按 code 做稳定分支，message 可直接展示给用户。
#
# 【同步/异步说明】
#   三个路由都使用普通 def 定义。FastAPI/Starlette 会把同步路由放到线程池执行，
#   所以这里直接调用阻塞的数据库函数不会卡住事件循环；本文件没有 async/await。
#
# 【给初学者的阅读提示】
#   SQL 里的 ? 是参数占位符，实际值通过元组单独传入，能防止 SQL 注入；
#   fetch_one 返回一行 dict，查不到时返回 None；Principal 是当前请求者身份对象；
#   401 表示未认证/令牌无效，403 表示身份有效但权限不足，404 表示资源不存在。
# =============================================================================

# 【导入 1/4｜延迟求值类型注解】
# 让函数返回注解不在定义时立即求值，而按字符串保存；可减少循环导入问题，
# 也让类型提示在较新/较旧 Python 版本之间更稳定。
from __future__ import annotations

# 【导入 2/4｜FastAPI 基础组件】
# APIRouter：把本文件的接口组织成可挂载的子路由；
# HTTPException：抛出带 HTTP 状态码和 JSON detail 的错误响应；
# Request：取得原始请求对象，再交给 security 层解析 Authorization 等请求头。
from fastapi import APIRouter, HTTPException, Request

# 【导入 3/4｜应用内业务模块】
# pipeline：提供回滚后的会话/题库结构修复函数；
# versions：提供版本快照的列表、详情读取和回滚实现。
from . import pipeline, versions
# 【导入 3/4 续｜数据库查询助手】
# fetch_one(sql, params)：执行查询并返回第一行 dict；无结果返回 None。
# 它内部通过上下文管理器自动提交事务并关闭连接。
from .db import fetch_one
# 【导入 3/4 续｜身份与权限对象】
# Principal：当前请求者的数据类（user_id、tenant_id、role、username 等）；
# principal_from_request：从 Bearer JWT 或开发模式请求头解析出 Principal。
from .security import Principal, principal_from_request

# 【模块级常量｜版本子路由】
# prefix 会加到本文件每个接口路径前；tags 决定 OpenAPI 文档中的分组名称。
# 这个 router 对象由 app/main.py 导入并注册到主应用。
router = APIRouter(prefix="/api/v1", tags=["versions"])


# 【工具函数 1｜获取当前请求者】
# 单独封装是为了让每个路由都用一行拿到 Principal，也方便测试时替换实现。
# 参数 request 由 FastAPI 自动注入，不需要客户端显式传递。
def _p(request: Request) -> Principal:
    # 调用 security 层校验 Bearer 令牌的签名和过期时间；strict 模式缺少令牌会抛 401。
    # 返回的 Principal 携带后续 SQL 过滤需要的 tenant_id、user_id 和 role。
    return principal_from_request(request)


# 【工具函数 2｜加载文档并做租户/权限校验】
# 三个版本接口在执行任何操作前都会先调用它，避免只凭 document_id 就访问数据。
# 参数 document_id 是 URL 中的文档 ID；p 是 _p() 解析出的当前请求者。
# 返回 document 表的完整行；校验不通过时直接抛出 HTTPException。
def _doc(document_id: str, p: Principal) -> dict:
    # 【SQL 说明】按主键 doc_id 查询，同时限制 tenant_id 必须与当前身份一致，
    # 并用 deleted_at IS NULL 排除软删除文档；参数化查询可防止 SQL 注入。
    # fetch_one 只取第一行；没有匹配记录时返回 None。
    row = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    # 【分支：文档不存在、不属于当前租户，或已经被软删除】
    if not row:
        # 统一返回 404，不区分“不存在”和“属于别的租户”，避免泄露跨租户信息。
        # detail 中的 code 供前端稳定判断，message 是可直接展示的中文提示。
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    # 【分支：非管理员且不是文档所有者】
    # admin 可以访问本租户内的任意文档；其他角色只能访问 user_id 等于自己的文档。
    # 使用 row.get 防止字段缺失导致 KeyError；p.user_id 来自已验签的身份信息。
    if p.role != "admin" and row.get("user_id") != p.user_id:
        # 403 表示“已经识别身份，但权限不足”，与 404 的资源不存在语义区分开。
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权访问该文档"})
    # 校验通过后把同一行返回给路由，路由无需再查一次文档。
    return row


# 【接口 1/3｜列出某个文档的所有版本快照】
# HTTP 方法为 GET，只读不修改数据；路径参数 document_id 由 FastAPI 从 URL 提取。
# 完整路径示例：GET /api/v1/documents/doc_123/versions
@router.get("/documents/{document_id}/versions")
def list_versions(document_id: str, request: Request):
    # 第一步：解析当前身份；没有有效令牌时 security 层会抛 401。
    p = _p(request)
    # 第二步：文档级鉴权；不存在/跨租户/已删除 -> 404，非本人且非 admin -> 403。
    # 这里丢弃返回值，只利用它“放行或抛错”的副作用。
    _doc(document_id, p)
    # 第三步：查询版本元数据列表；versions 内部按 version_no 倒序排列，
    # 且不返回体积较大的 snapshot 字段，避免列表接口响应过大。
    # 返回 list[dict]，每项含 version_id、version_no、node_count、reason、created_at 等。
    rows = versions.list_versions(document_id)
    # 第四步：查询文档当前版本号。document_version 中不一定存在与当前编辑状态
    # 完全同步的最新快照，因此不能简单用 rows[0] 代替，current_version 以
    # document 表为权威来源。此时 _doc 已确认文档存在且有权限，按主键查询即可。
    latest = fetch_one("SELECT doc_version FROM document WHERE doc_id=?", (document_id,))
    # 第五步：组装响应。latest 为空时返回 None，避免访问 None["doc_version"]。
    # 典型 JSON：{"items": [...], "current_version": 3}。
    return {"items": rows, "current_version": latest["doc_version"] if latest else None}


# 【接口 2/3｜读取某个指定版本的详情和节点快照】
# 路径包含 document_id（字符串）和 version_no（整数）两个参数。
# 如果 version_no 不是合法整数，FastAPI 会在进入函数前自动返回 422。
@router.get("/documents/{document_id}/versions/{version_no}")
def get_version(document_id: str, version_no: int, request: Request):
    # 第一步：解析当前请求者身份，得到租户和用户信息。
    p = _p(request)
    # 第二步：校验文档可见性和访问权限；不通过会直接抛 404 或 403。
    _doc(document_id, p)
    # 第三步：读取目标版本。versions.get_version 会把 snapshot 的 JSON 文本
    # 反序列化为 dict；如果 (doc_id, version_no) 不存在则返回 None。
    row = versions.get_version(document_id, version_no)
    # 【分支：目标版本不存在】
    if not row:
        # 返回 404 和版本专用业务码，提醒客户端“文档存在，但这个版本号不存在”。
        raise HTTPException(status_code=404, detail={"code": "VERSION_NOT_FOUND", "message": "版本不存在"})
    # 第四步：返回版本详情。下面是一个多行字典字面量，整体说明如下：
    # - 前六个键是版本元数据，直接取自 document_version 行；
    # - nodes 只取 snapshot 里的 node 数组，供前端预览结构树；
    # - snapshot 还可能包含 chunk/knowledge_point/evidence，本接口不对外暴露，
    #   以控制响应体积；
    # - 若旧快照没有 node 键，.get("node", []) 返回空列表，而不是抛 KeyError。
    return {
        "version_id": row["version_id"], "version_no": row["version_no"], "node_count": row["node_count"],
        "reason": row["reason"], "created_by": row["created_by"], "created_at": row["created_at"],
        "nodes": row["snapshot"].get("node", []),
    }


# 【接口 3/3｜把文档结构回滚到指定版本】
# 使用 POST 表示该接口会修改服务器状态；虽然当前没有请求体，也不应改成 GET。
# 完整路径示例：POST /api/v1/documents/doc_123/versions/2/restore
@router.post("/documents/{document_id}/versions/{version_no}/restore")
def restore_version(document_id: str, version_no: int, request: Request):
    # 第一步：解析身份，后续会把用户名/用户 ID 写入版本的 created_by 字段。
    p = _p(request)
    # 第二步：确认文档存在、属于当前租户，并且当前用户有权访问。
    _doc(document_id, p)
    # 第三步：调用业务层执行回滚。try 只包住 restore，便于把业务层的
    # “版本不存在”异常精确转换成 404；数据库等其它异常继续向上抛为 500。
    try:
        # created_by 优先记录可读的用户名；用户名为空时退回 user_id，
        # 保证审计字段始终有值。versions.restore 的主要动作是：
        # 回滚前自动快照 -> 删除并重建四类结构表 -> doc_version 加 1 ->
        # 回滚后再创建快照；返回值含 restored_from、version 和各项统计数。
        result = versions.restore(document_id, version_no, created_by=p.username or p.user_id)
    # 【异常处理】versions.restore 在目标版本不存在时抛 ValueError("版本不存在")。
    except ValueError as exc:
        # 转成 HTTP 404，detail 结构与其它接口保持一致；str(exc) 保留具体原因。
        raise HTTPException(status_code=404, detail={"code": "VERSION_NOT_FOUND", "message": str(exc)})
    # 第四步：回滚会重建 node_id/kp_id 等结构主键，旧的 session.plan 和 question
    # 可能仍指向回滚前的节点。下面两个修复函数负责修正这些引用：
    # - repair_sessions_for_document：按章节标题/知识点名称重挂未完成会话；
    # - repair_questions_for_document：按证据引文把题库题目重挂到新节点。
    # 返回值在这里不使用；若修复过程抛异常，整个 HTTP 请求会返回 500，
    # 但此前的回滚事务已经提交，这一点需要在运维/重试策略中注意。
    pipeline.repair_sessions_for_document(document_id)
    pipeline.repair_questions_for_document(document_id)
    # 第五步：所有步骤成功后，把 versions.restore 的结果原样序列化为 JSON 返回。
    # 典型字段：document_id、restored_from、version、node_count、section_count 等。
    return result
