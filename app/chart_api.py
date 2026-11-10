# =============================================================================
# 文件：app/chart_api.py
# 项目：A论 · 论文导读智能体（上传 PDF / DOCX / Markdown，解析结构并辅助学习）
#
# 一、这个文件负责什么
#   本文件是 FastAPI 的“图表路由层”（Controller），只做三件事：
#     1. 解析并校验当前请求的身份，确认调用者有权访问目标文档；
#     2. 从数据库 node 表中读取 node_type='chart' 的图表节点，整理成前端需要的 JSON；
#     3. 提供一个“重新提取图表”的入口，把耗时的文档解析工作交给后台任务。
#   它本身不实现 PDF 解析、也不直接读写文件，具体工作都委托给 pipeline 模块。
#
# 二、在应用中的位置（主要调用关系）
#   app/main.py
#     └─ app.include_router(chart_api.router)        # 应用启动时挂载本路由
#   web/app.js
#     ├─ GET  /api/v1/documents/{id}/charts          # 文档详情页加载图表列表
#     └─ POST /api/v1/documents/{id}/charts/extract  # （可选）重新提取图表
#   app/parser.py 的 attach_chart_nodes()
#     └─ 解析文档时把图表写成 node 表的一行：node_type='chart'，
#        text = 图表说明 Markdown + "\n\n```chart-data\n" + JSON + "\n```"
#   app/pipeline.py
#     ├─ get_document_path(document_id)              # 定位原始文件（本地或对象存储）
#     └─ start_parse(...)                            # 创建任务并异步解析（队列或线程）
#
# 三、数据流
#   上传/解析 -> node 表（chart 节点） -> 本文件 GET 接口 -> 前端渲染图表
#   前端点“重新提取” -> 本文件 POST 接口 -> pipeline.start_parse -> task 表/后台线程
#
# 四、接口与错误码
#   GET  /api/v1/documents/{document_id}/charts
#       200 成功；401 UNAUTHORIZED（strict 模式缺少/无效令牌）；404 DOC_NOT_FOUND；403 FORBIDDEN
#   POST /api/v1/documents/{document_id}/charts/extract
#       202 已受理；401 UNAUTHORIZED；404 DOC_NOT_FOUND / DOC_FILE_MISSING（原始文件缺失）；403 FORBIDDEN
#
# 五、阅读提示
#   - 路由函数使用同步 def，FastAPI 会把它们放到线程池执行，避免阻塞事件循环；
#   - 所有 SQL 都使用 ? 占位符参数化，不要把用户输入拼进 SQL 字符串；
#   - 数据库查询结果已被 app/db.py 转成普通 dict，因此可以直接用 r["字段名"] 读取。
# =============================================================================
"""图表数据提取 API：查询已解析图表，或触发文档重解析。"""
# ↑ 上一行是源文件原有的模块文档字符串（docstring），此处原样保留。
# 启用“延迟注解”（PEP 563）：函数签名里的 dict | None 等注解在运行时不会真正求值，
# 因此本模块在较旧的 Python 版本上也能被正常导入。该语句必须位于模块 docstring 之后、
# 其它所有可执行语句之前。
from __future__ import annotations

# 标准库 json：把 ```chart-data``` 代码块中的 JSON 文本反序列化为 Python 字典。
import json
# 标准库 re：用正则从图表节点的 Markdown 文本中截取 JSON 片段。
import re

# FastAPI 组件：
#   APIRouter     —— 路由分组容器，最后在 main.py 中挂到主应用上；
#   HTTPException —— 抛出带 HTTP 状态码和结构化 detail 的错误响应；
#   Request       —— 当前 HTTP 请求对象，用来读取认证相关的请求头。
from fastapi import APIRouter, HTTPException, Request

# 项目内的解析流水线：负责定位原始文件、创建解析任务并异步启动解析。
from . import pipeline
# 数据库帮助函数：fetch_all 返回多行（list[dict]），fetch_one 返回单行（dict 或 None）。
from .db import fetch_all, fetch_one
# 身份解析：从 Authorization / X-Tenant-Id / X-User-Id 等请求头构造 Principal 对象。
from .security import principal_from_request

# 创建本模块的路由器：
#   prefix="/api/v1" —— 所有路由路径都会自动加上该前缀；
#   tags=["charts"]  —— 在 OpenAPI/Swagger 文档中归入 charts 分组。
# 该 router 在 app/main.py 中通过 app.include_router(chart_api.router) 挂载。
router = APIRouter(prefix="/api/v1", tags=["charts"])


# 私有辅助函数：从图表节点的 text 字段里提取 ```chart-data JSON 代码块。
# 参数 text：数据库中 node.text 的原始内容（Markdown 说明 + 围栏代码块）。
# 返回值：解析成功 -> dict；没有代码块或 JSON 非法 -> None。
# 设计要点：本函数不向外抛异常，避免单条脏数据导致整个图表列表接口返回 500。
def _chart_payload(text: str) -> dict | None:
    # 正则表达式逐段说明：
    #   ```chart-data  固定的围栏起始标记；
    #   \s*            允许标记后出现空格或换行；
    #   (\{.*?\})      捕获第一个大括号包裹的内容，.*? 为非贪婪匹配，防止吞掉后面的代码块；
    #   \s*```         结尾围栏，允许前面有空白。
    # re.S（DOTALL）让 . 也能匹配换行符，因为 JSON 通常跨多行；
    # text or "" 用于兼容 text 为 None 的情况，避免 re.search 抛 TypeError。
    m = re.search(r"```chart-data\s*(\{.*?\})\s*```", text or "", re.S)
    # 没有匹配到代码块（例如普通正文节点或旧版数据），直接返回 None。
    if not m:
        # None 表示“该图表没有可用的结构化数据”，前端仍可展示 markdown 说明。
        return None
    # m.group(1) 是正则第一个捕获组，即大括号内的 JSON 文本。
    try:
        # json.loads 把 JSON 文本转成 Python 对象（通常是 dict，内部可能嵌套 list/dict）。
        return json.loads(m.group(1))
    # 捕获所有异常（JSON 语法错误、类型错误等）：坏数据不应让整个接口失败。
    except Exception:
        # 解析失败同样返回 None，属于“尽力而为”的容错策略。
        return None


# =============================================================================
# 接口一：GET /api/v1/documents/{document_id}/charts
# 作用：返回指定文档下所有图表节点的列表，包含结构化 data 和 markdown 说明。
# 权限：先解析身份；普通用户只能查看自己创建的文档，admin 可查看本租户内任意文档。
# 响应：{"items": [{...}, ...]}；文档不存在返回 404，越权返回 403。
# =============================================================================
# 注册 GET 路由；{document_id} 是路径参数，FastAPI 会自动从 URL 中提取并传入函数。
@router.get("/documents/{document_id}/charts")
# document_id：文档主键；request：FastAPI 注入的请求对象，用于读取认证请求头。
def list_charts(document_id: str, request: Request):
    # 解析当前请求的身份，得到 p.user_id / p.tenant_id / p.role 等字段。
    # demo 模式下未携带 token 会回退为默认开发用户；strict 模式下 security 模块会直接抛 401。
    p = principal_from_request(request)
    # 按“文档 ID + 租户 ID + 未删除”三个条件查询文档：
    #   deleted_at IS NULL 过滤软删除记录；
    #   tenant_id 用于多租户隔离，防止跨租户读取；
    #   ? 占位符由数据库驱动转义，避免 SQL 注入；fetch_one 返回 dict 或 None。
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    # 查不到文档：可能是不存在、属于其它租户、或已被软删除。
    # 统一返回 404，避免向调用者泄露“其它租户中是否存在该 ID”。
    if not doc:
        # detail 使用结构化字典，前端可根据 code 字段做提示或国际化；404 表示资源不存在。
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    # 授权判断：admin 直接放行；普通用户必须是文档的创建者（document.user_id）。
    # doc 是 dict，用 .get 读取可避免字段缺失时抛 KeyError；p.user_id 来自登录身份。
    if p.role != "admin" and doc.get("user_id") != p.user_id:
        # 403 表示“身份已确认，但没有访问权限”，与 404 的语义区分开。
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权查看该文档"})
    # 查询该文档下所有图表节点：
    #   node_type='chart' 由解析器 parser.attach_chart_nodes 写入；
    #   ORDER BY order_index 保证返回顺序与原文出现顺序一致；
    #   只取必要列，避免把 bbox、path 等大字段带到接口响应里。
    # 下面两条相邻字符串字面量会被 Python 自动拼接成一条 SQL，第一行末尾没有逗号。
    rows = fetch_all(
        "SELECT node_id, title, page_start, page_end, text, confidence FROM node"
        " WHERE doc_id=? AND node_type='chart' ORDER BY order_index", (document_id,)
    )
    # 组装响应：列表推导式把每个数据库行转换成前端需要的结构。
    # 字段含义：
    #   node_id    图表节点唯一 ID，可用于定位或跳转；
    #   title      图表标题（解析器生成，例如“图表 1”）；
    #   page       起始页码（page_start）；page_end 虽已查询，但当前接口只暴露起始页；
    #   confidence 解析置信度（0~1），前端可据此提示低置信度图表；
    #   data       _chart_payload 解析出的结构化数据（dict 或 None）；
    #   markdown   图表说明文字，取 text 中 ```chart-data 之前的部分并去掉首尾空白。
    # 注意：这里没有捕获数据库异常，异常会交给 FastAPI/全局异常处理并返回 500。
    return {"items": [{
        "node_id": r["node_id"], "title": r["title"], "page": r["page_start"],  # 节点 ID、标题、起始页码
        "confidence": r["confidence"], "data": _chart_payload(r["text"]),  # 置信度、结构化图表数据
        "markdown": (r["text"] or "").split("```chart-data")[0].strip(),  # 围栏前的说明文字
    } for r in rows]}


# =============================================================================
# 接口二：POST /api/v1/documents/{document_id}/charts/extract
# 作用：对已有文档重新触发解析，重新识别其中的图表，属于异步任务。
# 返回 202 Accepted：请求已受理，解析在后台进行，前端应轮询任务/文档状态。
# =============================================================================
# 注册 POST 路由；status_code=202 明确告诉客户端“已接受、尚未完成”。
@router.post("/documents/{document_id}/charts/extract", status_code=202)
# document_id：要重新解析的文档；request：用于解析调用者身份。
def extract_charts(document_id: str, request: Request):
    # 解析当前请求的身份（user_id / tenant_id / role 等）。
    p = principal_from_request(request)
    # 与列表接口相同：先按文档 ID + 租户 ID + 未删除条件查找文档，确保文档存在且属于本租户。
    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL", (document_id, p.tenant_id))
    # 文档不存在、已删除或跨租户访问：统一返回 404。
    if not doc:
        # 使用结构化错误码 DOC_NOT_FOUND，便于前端精确处理。
        raise HTTPException(status_code=404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    # 授权判断：admin 可重解析租户内任意文档；普通用户只能重解析自己的文档。
    if p.role != "admin" and doc.get("user_id") != p.user_id:
        # 403 FORBIDDEN：身份合法但权限不足。
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "无权重解析该文档"})
    # 定位原始文件：pipeline 会先查 document 表，再在本地 UPLOAD_DIR 或对象存储中查找。
    # 返回值是 pathlib.Path；文件不存在时返回 None。
    path = pipeline.get_document_path(document_id)
    # 数据库里有记录但磁盘/对象存储里找不到文件，无法重新解析。
    if not path:
        # DOC_FILE_MISSING 与 DOC_NOT_FOUND 区分开，方便运维定位“文件丢失”问题。
        raise HTTPException(status_code=404, detail={"code": "DOC_FILE_MISSING", "message": "原始文件缺失"})
    # 启动解析任务：
    #   start_parse 先创建 task 记录（状态 queued），再尝试投递到平台队列；
    #   队列不可用时回退为后台 daemon 线程执行 run_parse_task；
    #   doc_type 为空时回退为 "auto"，让解析器自动判断 PDF/Word/图片等类型；
    #   title 传给解析器，用于生成文档/章节标题。
    # 返回值为 task_id，前端可用它查询解析进度。
    task_id = pipeline.start_parse(document_id, path, doc.get("doc_type") or "auto", doc.get("title"))
    # 立即返回，不等待解析完成；status="processing" 只是给前端的语义提示，
    # 真正的任务状态以 task 表或任务查询接口为准。
    return {"document_id": document_id, "task_id": task_id, "status": "processing"}
