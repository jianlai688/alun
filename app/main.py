# =============================================================================
# 文件：app/main.py —— A论后端 REST API v1 的总入口与路由编排层
# =============================================================================
# 一、这个文件在整个应用中的位置
#   浏览器 / 桌面端 / 第三方调用方
#       -> FastAPI 应用（本文件创建 app）
#       -> 中间件（CORS、安全响应头、请求编号）
#       -> 本文件中的通用接口与各业务路由（documents/sessions/questions...）
#       -> app 包内的业务模块（pipeline、sessions、assistant、users 等）
#       -> platform 层（数据库、Redis、对象存储、可观测性）
#   本文件不是“某一个功能”的实现文件，而是整个 HTTP 服务的装配中心：
#   它负责启动检查、创建 FastAPI 实例、挂中间件、注册子路由、统一异常格式，
#   并直接实现认证、文档、任务、会话、题库、报告、用量和静态前端等接口。
#
# 二、建议的阅读顺序
#   1. 先看 import 和启动代码，理解应用启动时做了哪些“不能失败”的检查；
#   2. 再看中间件和异常处理器，理解每个请求/响应都要经过的公共通道；
#   3. 然后按“认证 -> 文档 -> 任务 -> 会话 -> 题库/报告”阅读业务接口；
#   4. 最后看静态文件兜底路由，理解前后端如何由同一个服务托管。
#
# 三、几个贯穿全文的约定
#   * 所有业务错误尽量转成统一 JSON：code / message / details / request_id / timestamp，
#     这样前端可以用 code 做程序判断，用 message 做用户提示。
#   * 认证使用 Bearer Token：security.principal_from_request 解析请求头，
#     Depends(principal) 把当前用户 Principal 注入到路由函数参数 p 中。
#   * 多租户隔离：绝大多数查询同时带 tenant_id，普通用户还要带 user_id；
#     admin 在同一租户内可以跨用户访问，但不能跨租户。
#   * 同步路由函数（def）由 FastAPI 放到线程池执行，适合直接调用同步数据库；
#     异步路由函数（async def）中若调用阻塞 SDK，会用 asyncio.to_thread 转移线程，
#     避免堵住事件循环。SSE 推送则使用 async 生成器边查边发。
#   * 注释一律使用 #，不改变任何原有可执行语句、字符串、缩进和空行；
#     多行字典/列表/函数调用在上方用整块说明，避免把注释插进不安全的位置。
# =============================================================================

"""A论 · REST API v1（对齐《DGQA 接口文档》）。"""
# 启用“延迟求值”的类型注解：函数签名里的 list[str]、str | None 等先保存成字符串，
# 不在导入时立即求值，可减少循环导入问题，也方便使用较新的类型写法。

from __future__ import annotations

# asyncio：把阻塞函数丢到线程（to_thread），以及在任务事件流中 sleep 让出事件循环。

import asyncio
# base64：图片二进制转 data URL；分页 cursor 也用它做 URL 安全的编码/解码。

import base64
# json：解析前端传来的 history，以及把 SSE 事件数据序列化成 JSON 文本。

import json
# mimetypes：上传文件没有 content_type 时，按扩展名猜测 MIME 类型。

import mimetypes
# os：标准库通用操作系统接口。本文件当前没有直接调用 os，保留导入不改变原行为。

import os
# time：标准库时间工具。本文件当前没有直接调用 time，时间统一走 db.iso()。

import time
# uuid：生成请求追踪号 request_id，便于把一次请求的日志串起来。

import uuid
# defaultdict：带默认值的字典。本文件当前未直接使用，保留原导入。
# （不要因为“看起来没用到”就删除，注释任务只做解释，不重构代码。）

from collections import defaultdict
# datetime / timedelta：报告下载链接的过期时间用 now() + timedelta(hours=1) 计算。

from datetime import datetime, timedelta
# pathlib.Path：跨平台拼接文件路径，例如 DATA_DIR / "reports"。

from pathlib import Path

# FastAPI 核心对象与参数声明：
#   Depends       —— 依赖注入，例如自动执行 principal() 做鉴权；
#   FastAPI       —— Web 应用类；
#   File/Form/Header/Query —— 分别从文件、表单、请求头、查询串提取参数；
#   HTTPException —— 抛出标准 HTTP 错误（静态资源兜底路由会用到）；
#   Request       —— 原始请求对象，可读请求体、请求头、client 和 state；
#   UploadFile    —— FastAPI 对上传文件的封装，read() 是异步方法。

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
# 请求体验证失败时 FastAPI 抛出的异常类型；下面会把它转换成项目统一错误结构。

from fastapi.exceptions import RequestValidationError
# 跨域中间件：允许浏览器前端从配置的源访问本 API。

from fastapi.middleware.cors import CORSMiddleware
# 各类响应对象：
#   FileResponse     —— 返回磁盘文件（原始文档、报告、前端页面）；
#   JSONResponse     —— 返回自定义状态码/响应头/统一错误 JSON；
#   Response         —— 只关心状态码（如 204 No Content）；
#   StreamingResponse—— 流式返回 SSE（服务器发送事件）。

from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
# 将前端构建产物目录挂载为静态资源服务。

from fastapi.staticfiles import StaticFiles

# AI 问答助手的业务实现（回答、图片理解、检索增强等），这里只做 HTTP 适配。

from . import assistant as assistant_api
# 其余核心业务模块：
#   llm       —— 模型网关健康检查与可用性判断；
#   pipeline  —— 文档入库、去重、异步解析流水线；
#   sessions  —— 学习会话、题目、报告的核心状态机；
#   settings  —— 系统配置读写；
#   users     —— 用户注册、认证、权限范围；
#   audit     —— 审计日志。

from . import llm, pipeline, sessions, settings, users, audit
# 网页链接导入：抓取 URL 内容后交给文档流水线。

from . import url_import
# 管理端路由模块（用户、系统配置、审计等），以 router 形式挂到主应用。

from . import admin as admin_api
# 文档质量检查路由模块。

from . import quality_api
# 教学相关路由模块。

from . import teaching_api
# platform 公共基础设施：
#   observability —— 日志、依赖健康检查、指标埋点；
#   queue         —— 任务队列抽象（本文件未直接使用）；
#   redis_client  —— 缓存、限流、元数据缓存。

from ..platform import observability, queue, redis_client
# 对象存储适配层；用 storage_mod 别名避免和局部变量 storage 重名。

from ..platform import storage as storage_mod
# 阅读器相关路由模块。

from . import reader_api
# 文档版本相关路由模块。

from . import version_api
# 向量检索相关路由模块。

from . import vector_api
# 平台管理相关路由模块。

from . import platform_api
# 图表/可视化相关路由模块。

from . import chart_api
# 文档版本快照服务：编辑大纲前自动留一份快照。

from . import versions
# 从项目统一配置模块集中导入常量。集中配置的好处是部署时只改环境变量/配置文件，
# 不需要在各个业务文件里找魔法数字。下面逐项说明用途。

from .config import (
    # APP_NAME：产品名，用于 OpenAPI 标题、健康检查和首页兜底 JSON。

    APP_NAME,
    # APP_SUBTITLE：产品副标题，与 APP_NAME 拼成 FastAPI 的 title。

    APP_SUBTITLE,
    # APP_VERSION：应用版本号，会出现在 /health、/meta 和 OpenAPI 文档中。

    APP_VERSION,
    # AUTH_MODE：认证模式标识，通过 /meta 告诉前端当前认证方式。

    AUTH_MODE,
    # AUTH_SECRET：JWT/令牌签名密钥。本文件没有直接读取，实际签名在 security 模块内完成。

    AUTH_SECRET,
    # CORS_ORIGINS：允许跨域的前端源列表；为空时下面会使用本地开发默认值。

    CORS_ORIGINS,
    # DB_BACKEND：数据库后端类型（如 sqlite / postgres），决定是否执行 PostgreSQL 迁移。

    DB_BACKEND,
    # LOG_JSON：日志是否输出 JSON 格式，传给 observability.setup_logging。

    LOG_JSON,
    # SECURITY_HEADERS：是否启用浏览器安全响应头中间件。

    SECURITY_HEADERS,
    # DATA_DIR：数据根目录，上传文件、报告等持久化内容都放在它下面。

    DATA_DIR,
    # DEFAULT_SCOPES：默认权限范围。本文件没有直接使用，权限实际由 users/scopes_for 计算。

    DEFAULT_SCOPES,
    # DOC_TYPE_LABELS：文档类型的中文标签，供 /meta 给前端展示。

    DOC_TYPE_LABELS,
    # GATEWAY：模型网关配置。本文件没有直接引用，模型调用由 llm 模块负责。

    GATEWAY,
    # MAX_UPLOAD_BYTES：上传文档大小上限（默认语义为 100 MB），在 upload_document 中校验。

    MAX_UPLOAD_BYTES,
    # WEB_DIR：前端静态文件目录；存在时挂载 /assets 并作为 SPA 回退目录。

    WEB_DIR,
)
# 数据访问层：
#   db        —— 数据库连接上下文管理器（with db() as conn）；
#   dump      —— Python 对象序列化为数据库里的 JSON 文本；
#   fetch_all —— 查询多行；
#   fetch_one —— 查询一行（无结果返回 None）；
#   init_db   —— 初始化表结构；
#   iso       —— 生成统一格式的当前时间字符串；
#   load_json —— 安全地把数据库 JSON 文本还原为对象；
#   new_id    —— 生成带业务前缀的短 ID；
#   now       —— 当前 datetime，供过期时间计算。

from .db import db, dump, fetch_all, fetch_one, init_db, iso, load_json, new_id, now
# 安全模块：
#   Principal             —— 当前请求主体的数据类（用户、租户、角色、权限）；
#   issue_token           —— 登录成功后签发访问令牌；
#   principal_from_request—— 从 Authorization 头还原 Principal；
#   require               —— 检查某个具体 scope；
#   require_admin         —— 要求管理员角色；
#   validate_runtime_secret—— 启动时检查密钥配置是否安全。

from .security import Principal, issue_token, principal_from_request, require, require_admin, validate_runtime_secret
# 会话业务层自定义异常：携带 status/code/message/details，便于统一转成业务错误响应。

from .sessions import SessionError

# 启动第 1 步：校验运行时密钥。配置不安全时应在启动阶段直接失败，而不是带病上线。

validate_runtime_secret(DB_BACKEND)
# 启动第 2 步：初始化数据库连接池/表结构（具体行为由 db 模块决定）。

init_db()
# 只有 PostgreSQL 后端才需要执行 platform.migrations 的版本化迁移。

if DB_BACKEND == "postgres":
    # 条件导入：SQLite 部署时不必加载 PostgreSQL 专用迁移代码。

    from ..platform import migrations
    # 执行数据库迁移，把 schema 升级到当前代码需要的版本。

    migrations.migrate()
# 启动第 3 步：确保内置/种子用户存在，便于首次部署后立即登录。

users.ensure_seed_users()
# 启动第 4 步：写入默认系统配置（已有配置不会被覆盖，具体由 settings 保证）。

settings.ensure_defaults()
# 启动第 5 步：初始化日志系统；LOG_JSON 控制输出为 JSON 还是普通文本。

observability.setup_logging(LOG_JSON)

# 创建 FastAPI 应用实例。title/version 会显示在自动生成的 /docs OpenAPI 页面上。

app = FastAPI(title=f"{APP_NAME} · {APP_SUBTITLE}", version=APP_VERSION)
# 注册 CORS 中间件；参数较多，用多行写更易读。

app.add_middleware(
    CORSMiddleware,
    # 允许的前端来源。配置为空时回退到本地开发地址，避免开发环境被浏览器拦截。

    allow_origins=CORS_ORIGINS or ["http://127.0.0.1:8010", "http://localhost:8010"],
    # 允许携带 Cookie/Authorization 等凭证；生产环境不能把 allow_origins 配成 "*"。

    allow_credentials=True,
    # 明确允许的 HTTP 方法，覆盖 REST 常用动作和预检 OPTIONS。

    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # 允许的请求头：认证头、JSON 内容类型、租户标识、请求追踪号。

    allow_headers=["Authorization", "Content-Type", "X-Tenant-Id", "X-Request-Id"],
)
# 接入可观测性：请求指标、耗时、日志上下文等由 platform.observability 统一处理。

observability.instrument(app)
# 以下 include_router 把各业务模块的 APIRouter 挂到主应用。
# 每个 router 内部已经定义了路径、依赖和响应模型；这里只负责汇总。
# 管理端路由（用户管理、系统设置、审计查询等）。

app.include_router(admin_api.router)
# 文档质量相关接口。

app.include_router(quality_api.router)
# 教学相关接口。

app.include_router(teaching_api.router)
# 阅读器相关接口。

app.include_router(reader_api.router)
# 文档版本相关接口。

app.include_router(version_api.router)
# 向量检索相关接口。

app.include_router(vector_api.router)
# 平台管理相关接口。

app.include_router(platform_api.router)
# 图表相关接口。

app.include_router(chart_api.router)

# 报告导出目录：所有生成的 Markdown 报告都写到这里。
# 使用 pathlib 的 / 运算符拼接，跨 Windows/Linux 都能工作。

EXPORT_DIR = DATA_DIR / "reports"
# 递归创建目录。parents=True 表示父目录不存在也一起建；
# exist_ok=True 表示目录已存在时不报错，保证应用可重复启动。

EXPORT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 通用基础设施：中间件、异常处理、错误响应、鉴权依赖、分页与幂等
# ---------------------------------------------------------------------------
# 这一部分被后面几乎所有业务接口复用，建议先理解这里的“公共约定”。

# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------
# 安全响应头是可选能力，由配置 SECURITY_HEADERS 控制，关闭时完全不注册该中间件。

if SECURITY_HEADERS:
    # 注册 HTTP 中间件。装饰器写法等价于 app.add_middleware(...)，函数会在每个请求前后执行。

    @app.middleware("http")
    # request 是当前请求；call_next 是“继续执行后续中间件/路由”的回调，必须 await。

    async def security_headers(request: Request, call_next):
        # 先让请求走完后续流程拿到响应，再统一补安全头。

        response = await call_next(request)
        # 禁止浏览器猜测 Content-Type，降低 MIME 混淆攻击风险；
        # setdefault 表示如果下游已经设置，则不覆盖下游的选择。

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # 只允许同源页面用 iframe 嵌入，防止点击劫持；同样不覆盖已有值。

        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        # 不向外部站点发送 Referer，避免 URL 中的敏感信息泄露。

        response.headers.setdefault("Referrer-Policy", "no-referrer")
        # 关闭摄像头、麦克风、定位等浏览器权限，减少前端被利用的空间。

        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        # 把加工后的响应交还给 FastAPI/服务器。

        return response


# 请求上下文中间件：为每个请求生成/沿用追踪号，并回写到响应头。

@app.middleware("http")
# 异步中间件函数，签名固定为 (request, call_next)。

async def request_context(request: Request, call_next):
    # 优先使用调用方传来的 X-Request-Id（便于全链路追踪）；没有就生成 req_ + 12 位十六进制。
    # uuid4().hex 是随机 UUID 的 32 位十六进制字符串，截取前 12 位足够区分请求。

    request_id = request.headers.get("X-Request-Id") or f"req_{uuid.uuid4().hex[:12]}"
    # 把 request_id 存进 request.state，后续异常处理器和日志可以读取。

    request.state.request_id = request_id
    # 继续执行请求；响应可能在路由或更内层中间件中产生。

    response = await call_next(request)
    # 把同一个 request_id 返回给客户端，方便用户报错时提供服务端排查线索。

    response.headers["X-Request-Id"] = request_id
    # 返回响应，中间件链继续向外传递。

    return response


# 注册 SessionError 的专用异常处理器：业务层抛出这种异常时，不再返回 FastAPI 默认格式。

@app.exception_handler(SessionError)
# 异常处理器可以是 async 函数；exc 是捕获到的 SessionError 实例。

async def session_error_handler(request: Request, exc: SessionError):
    # 交给 _error 统一生成 JSON；SessionError 自带 HTTP 状态码和业务错误码。

    return _error(exc.status, exc.code, exc.message, exc.details, request)


# 注册 HTTPException 处理器，统一处理 404、401 等标准 HTTP 异常。

@app.exception_handler(HTTPException)
# detail 可能是字符串，也可能是业务层传入的字典（包含 code/message/details）。

async def http_error_handler(request: Request, exc: HTTPException):
    # 先取出原始 detail，后面根据类型分别处理。

    detail = exc.detail
    # 字典形式：按项目约定提取业务错误码和用户可读消息。

    if isinstance(detail, dict):
        # 使用 dict.get 提供兜底：没有 code 就用 HTTP_ERROR，没有 message 就把整个字典转字符串。
        # details 原样传递，便于前端展示字段级错误。

        return _error(exc.status_code, detail.get("code", "HTTP_ERROR"), detail.get("message", str(detail)), detail.get("details"), request)
    # 字符串形式：把它当作 message，错误码固定为 HTTP_ERROR，没有额外 details。

    return _error(exc.status_code, "HTTP_ERROR", str(detail), None, request)


# 注册参数校验异常处理器：FastAPI/Pydantic 校验失败时默认返回 422，这里改成统一格式。

@app.exception_handler(RequestValidationError)
# exc.errors() 返回每个字段的错误列表，包含 loc（出错位置）、msg（原因）、type（错误类型）。

async def validation_error_handler(request: Request, exc: RequestValidationError):
    # 列表推导式把错误精简成前端需要的三个字段；loc 转成 list，避免元组被 JSON 序列化成奇怪结构。

    errors = [{"loc": list(e.get("loc", [])), "msg": e.get("msg", ""), "type": e.get("type", "")} for e in exc.errors()]
    # 返回 422，业务码 VALIDATION_ERROR，details.errors 带字段级明细。

    return _error(422, "VALIDATION_ERROR", "请求参数不合法", {"errors": errors}, request)


# 兜底异常处理器：任何没有被前面处理器接住的异常都会到这里。

@app.exception_handler(Exception)
# 捕获 Exception（不包括 SystemExit/KeyboardInterrupt 等 BaseException）。

async def unhandled_error_handler(request: Request, exc: Exception):
    # 在函数内部导入 traceback，减少模块顶层命名空间污染；异常路径才需要它。

    import traceback

    # 把完整堆栈打印到服务端日志/标准错误，方便开发排查；不要把这个堆栈直接返回给用户。

    traceback.print_exc()
    # 对外只返回通用 500 文案，details.error 截断到 200 字符，避免泄露内部路径或密钥。

    return _error(500, "INTERNAL_ERROR", "服务内部错误，请稍后重试", {"error": str(exc)[:200]}, request)


# 统一错误响应构造器。
# status: HTTP 状态码；code: 稳定业务码；message: 用户提示；
# details: 可选结构化细节；request: 可选，用来带出 request_id。

def _error(status: int, code: str, message: str, details=None, request: Request | None = None) -> JSONResponse:
    # 直接构造 JSONResponse，确保状态码和 JSON 内容一起返回。

    return JSONResponse(
        # HTTP 层状态码，例如 401/404/422/500。

        status_code=status,
        # 响应体固定包含五个字段，前端可以统一处理。

        content={
            # 机器可读的业务错误码，前端应优先判断它而不是匹配中文 message。

            "code": code,
            # 人类可读的错误说明，可直接展示给用户。

            "message": message,
            # 没有 details 时返回空对象 {}，保证字段类型稳定，前端不必判空。

            "details": details or {},
            # 只有 request 存在时才读 request.state；getattr 的默认值防止中间件尚未执行时抛错。

            "request_id": getattr(request.state, "request_id", "") if request else "",
            # 服务端生成时间，便于和日志对齐。

            "timestamp": iso(),
        },
    # 结束 JSONResponse 参数。

    )


# FastAPI 依赖函数：路由参数写 p: Principal = Depends(principal) 时，会先调用这里。
# 把“如何从请求还原当前用户”与具体业务路由解耦。

def principal(request: Request) -> Principal:
    # 实际解析逻辑在 security 模块；这里只是给依赖注入一个稳定的入口。

    return principal_from_request(request)


# 通用游标分页。items 是已经过滤好的完整列表，limit 是每页数量，cursor 是上一页返回的游标。
# 返回 (当前页列表, 下一页游标或 None)。
# 注意：这里在内存中切片，适合中小数据量；大数据量应改成数据库 LIMIT/OFFSET 或键集分页。

def _paginate(items: list, limit: int = 20, cursor: str | None = None) -> tuple[list, str | None]:
    # 限制每页 1~100 条；limit 可能是 None/0/字符串，所以先 or 20，再 int，再夹紧范围。

    limit = max(1, min(100, int(limit or 20)))
    # 游标解码失败时的默认偏移量。

    offset = 0
    # 只有传了 cursor 才尝试解码；第一页没有游标。

    if cursor:
        # 解码可能因为游标被篡改/截断而失败，因此用 try 包住。

        try:
            # base64 的 urlsafe 解码。URL 安全 base64 可以去掉末尾 "="，
            # 这里用 -len(cursor) % 4 补回正确数量的等号，再 decode 成字符串并转 int。

            offset = int(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode())
        # 任何解码/转换异常都视为非法游标。

        except Exception:
            # 非法游标退回第一页，避免 500；如果业务要求严格报错，可在这里改成抛 400。

            offset = 0
    # Python 切片是“左闭右开”，所以取 [offset, offset+limit)，不会多取一条。

    page = items[offset : offset + limit]
    # 默认没有下一页。

    next_cursor = None
    # 如果本页之后还有元素，就生成下一页游标。

    if offset + limit < len(items):
        # 下一页的 offset 编码成 URL 安全 base64，并去掉末尾 "="，让 URL 更短。

        next_cursor = base64.urlsafe_b64encode(str(offset + limit).encode()).decode().rstrip("=")
    # 返回当前页和下一页游标；调用方通常还会把 next_cursor 转成 has_more。

    return page, next_cursor


# 幂等查询：相同 Idempotency-Key + 相同 endpoint 的重复请求，直接返回第一次的结果。
# key 为空表示客户端没有要求幂等，返回 None 让业务继续执行。

def _idem_lookup(key: str | None, endpoint: str) -> dict | None:
    # 没有 key 就没有可查的幂等记录。

    if not key:
        # 明确返回 None，调用方用 if cached: 判断。

        return None
    # 按 (key, endpoint) 查询之前保存的响应 JSON。
    # endpoint 也参与条件，避免同一个 key 被不同接口误复用。

    row = fetch_one("SELECT response FROM idempotency WHERE key=? AND endpoint=?", (key, endpoint))
    # 查到记录就解析 response 字段；load_json 的第二个参数是解析失败时的默认值。

    return load_json(row["response"], None) if row else None


# 幂等结果保存：把本次成功响应写入 idempotency 表，供后续重复请求读取。

def _idem_save(key: str | None, endpoint: str, response: dict) -> None:
    # 没有 key 时无需保存。

    if not key:
        # 提前返回，保持函数返回 None。

        return
    # 幂等保存是“增强能力”，不应影响主业务成功；因此捕获所有异常并忽略。

    try:
        # 打开数据库连接/事务，with 结束时会提交或回滚（由 db 模块实现）。

        with db() as conn:
            # 执行 INSERT ... ON CONFLICT 的 UPSERT：key 冲突时更新 endpoint/response/created_at。
            # 占位符用 ?，具体驱动会在 db 层适配不同数据库。

            conn.execute(
                # 第一段 SQL：插入四个字段；第二段 SQL 在 key 冲突时覆盖旧值。
                # 两段字符串在括号内会自动拼接，不要在两个字符串之间插入非注释内容。

                "INSERT INTO idempotency (key, endpoint, response, created_at) VALUES (?,?,?,?)"
                # ON CONFLICT(key) 是 SQLite/PostgreSQL 都支持的 upsert 语法；
                # excluded.response 表示“本次准备插入的新值”。

                " ON CONFLICT(key) DO UPDATE SET endpoint=excluded.endpoint, response=excluded.response, created_at=excluded.created_at",
                # 参数顺序必须与 SQL 中的 ? 一一对应：key、endpoint、序列化后的响应、当前时间。

                (key, endpoint, dump(response), iso()),
            )
    # 保存失败（例如表不存在、并发冲突）不应该让用户看到 500。

    except Exception:
        # 静默忽略；生产环境可在这里加日志，但本文件保持原行为。

        pass


# 健康检查：返回依赖状态和版本，供负载均衡/监控探活。
# 与 /ready 的区别：/health 更偏“整体状态汇报”，通常始终 200；/ready 用 503 表示未就绪。

@app.get("/api/v1/health")
# 同步路由：FastAPI 会在线程池执行，内部同步 IO 不会阻塞事件循环。

def health():
    # 刷新数据库/Redis/存储等依赖的健康状态，返回字典。

    deps = observability.refresh_dependencies()
    # 模型网关单独检查；health() 返回 (是否可用, 说明文本)。

    gateway_ok, gateway_msg = llm.health()
    # 把模型网关状态并入依赖字典：
    # 可用 -> "ok"；不可用但配置了网关 -> 返回具体错误；根本没配置 -> "not_configured"。

    deps["model_gateway"] = "ok" if gateway_ok else (gateway_msg if llm.available() else "not_configured")
    # 只有数据库、Redis、存储三个核心依赖都 ok，整体才算健康；
    # 模型网关是可选依赖，不影响 ok 的判断。

    ok = deps["database"].get("ok") and deps["redis"].get("ok") and deps["storage"].get("ok")
    # 返回给监控/客户端的健康报告。

    return {
        # 核心依赖异常时用 degraded 而非直接报错，便于监控看到细节。

        "status": "ok" if ok else "degraded",
        # 产品名。

        "name": APP_NAME,
        # 当前部署版本。

        "version": APP_VERSION,
        # 服务器当前时间。

        "time": iso(),
        # 各依赖的详细状态。

        "dependencies": deps,
    # 结束响应字典。

    }


# 就绪探针：Kubernetes/负载均衡用它决定是否把流量切到本实例。

@app.get("/api/v1/ready")
# 同步路由。

def ready():
    # 重新检查依赖，不能使用缓存，否则探针可能误判。

    deps = observability.refresh_dependencies()
    # 与 /health 相同的三个核心依赖判断。

    ok = deps["database"].get("ok") and deps["redis"].get("ok") and deps["storage"].get("ok")
    # 状态码随结果变化：200 ready；503 not_ready。
    # 503 告诉网关“实例还在但暂时不能接流量”。

    return JSONResponse({"status": "ready" if ok else "not_ready", "dependencies": deps},
                        # 结束 JSONResponse 调用。

                        status_code=200 if ok else 503)


# Prometheus 风格指标端点；具体格式由 observability.metrics() 决定。

@app.get("/metrics")
# 不需要业务鉴权，通常由内网监控抓取；生产环境应在网关层限制访问。

def metrics():
    # 直接返回指标文本/响应对象。

    return observability.metrics()


# 前端启动时读取的元信息：产品名、版本、认证模式、模型状态、文档类型、学习模式。

@app.get("/api/v1/meta")
# 同步路由。

def meta():
    # 先查 Redis 缓存，key 带版本号 v2，便于结构升级时自然失效。

    cached = redis_client.get_json("meta:v2")
    # 缓存命中就直接返回，避免每次请求都读配置/探测模型。

    if cached:
        # 返回缓存中的字典，FastAPI 会自动序列化为 JSON。

        return cached
    # 缓存未命中时组装完整元信息。

    payload = {
        # 产品名。

        "name": APP_NAME,
        # 副标题。

        "subtitle": APP_SUBTITLE,
        # 版本号。

        "version": APP_VERSION,
        # 认证模式，前端据此决定登录页/Token 处理方式。

        "auth_mode": AUTH_MODE,
        # 模型信息：enabled 表示是否可用；只有可用时才暴露模型名，避免误导。
        # settings.get("llm.model") 读取系统配置中的模型标识。

        "llm": {"enabled": llm.available(), "model": settings.get("llm.model") if llm.available() else None},
        # 文档类型枚举及中文标签，供上传表单下拉框使用。

        "doc_types": DOC_TYPE_LABELS,
        # 三种学习模式的展示文案；key 是后端协议值，value 是中文 UI 文案。

        "modes": {"guided": "导读模式", "exam": "测评模式", "defense": "答辩模式"},
    }
    # 写入 Redis，ttl=300 秒（5 分钟）。即使配置变更，最多 5 分钟后前端能看到新值。

    redis_client.set_json("meta:v2", payload, ttl=300)
    # 返回刚组装的 payload。

    return payload


# 提取请求元信息，供审计日志记录来源。
# 返回 (客户端 IP, User-Agent)。

def _request_meta(request: Request) -> tuple[str, str]:
    # request.client 在 TestClient/某些代理场景可能为 None，因此做保护。

    ip = request.client.host if request.client else ""
    # 没有 User-Agent 时用空字符串，保证审计字段类型稳定。

    ua = request.headers.get("User-Agent", "")
    # 返回二元组，调用方可以解包成 ip, ua。

    return ip, ua


# 登录成功后的统一响应构造：签发 Token，并附带用户公开信息。
# 登录接口和注册接口都复用它，避免两处逻辑漂移。

def _login_response(user: dict) -> dict:
    # 根据角色计算权限范围（scope 列表），例如普通用户/管理员拥有不同能力。

    scope = users.scopes_for(user["role"])
    # 签发访问令牌；返回值包含 token 字符串和有效期（秒）。

    token, ttl = issue_token(
        # 把用户 ID、租户 ID、权限范围作为 Token 的核心 claims。

        user["user_id"], user["tenant_id"], scope,
        # 额外写入角色和用户名，便于审计/前端展示；这些字段不应当替代服务端权限检查。

        role=user["role"], username=user["username"],
    )
    # 按 OAuth2 风格返回令牌信息，前端可直接用于 Authorization: Bearer <token>。

    return {
        # 实际令牌字符串。

        "access_token": token,
        # 固定为 Bearer，表示 HTTP Authorization 头的认证方案。

        "token_type": "Bearer",
        # 有效期秒数，前端可据此提前刷新/重新登录。

        "expires_in": ttl,
        # 权限范围列表。

        "scope": scope,
        # 用户公开信息（密码哈希等敏感字段必须由 users.public_user 过滤掉）。

        "user": users.public_user(user),
    }


# 登录接口：接收 JSON 用户名/密码，做限流、认证、审计，然后签发 Token。
# 这里不使用 Pydantic 模型，而是手动解析 request.json()，以兼容多种客户端提交方式。

@app.post("/api/v1/auth/login")
# async 路由：读取请求体是异步操作。

async def login(request: Request):
    # 手动解析 JSON；格式错误不应直接 500，而是当作空对象走后续校验。

    try:
        # await 读取整个请求体并解析为 Python 对象。

        body = await request.json()
    # 捕获 JSON 解析错误/空请求体。

    except Exception:
        # 用空字典兜底，后面 username/password 都取不到，会走认证失败分支。

        body = {}
    # 取 username 并转字符串、去首尾空白；None 用空字符串兜底。

    username = str(body.get("username") or "").strip()
    # 密码不做 strip（空格可能是密码的一部分），只做 None 兜底和字符串转换。

    password = str(body.get("password") or "")
    # 获取 IP 和 User-Agent，用于限流键和审计日志。

    ip, ua = _request_meta(request)
    # 登录限流：同一 IP（无 IP 时退化为用户名）60 秒最多 10 次。
    # rate_limit 返回 (是否允许, 剩余次数)；Redis 实现通常用计数+过期时间。

    allowed, remaining = redis_client.rate_limit("login", ip or username or "unknown", limit=10, window=60)
    # 超过限制立即拒绝。

    if not allowed:
        # 429 Too Many Requests；details.retry_after 告诉客户端 60 秒后重试。

        raise SessionError(429, "RATE_LIMITED", "登录尝试过于频繁，请稍后再试", {"retry_after": 60})
    # 校验用户名/密码。users.authenticate 内部应使用恒定时间比较密码哈希。

    user = users.authenticate(username, password)
    # 认证失败时不区分“用户不存在”和“密码错误”，避免账号枚举。

    if not user:
        # 记录失败审计：只记用户名和失败原因，不记录明文密码。

        audit.log_event(username=username, event_type="login_failed", detail={"reason": "invalid_credentials"}, ip=ip, user_agent=ua)
        # 返回 401 和统一错误码。

        raise SessionError(401, "INVALID_CREDENTIALS", "用户名或密码错误")
    # 认证成功，记录登录审计（用户 ID、租户、角色、IP、UA）。

    audit.log_event(
        # 审计上下文：谁、属于哪个租户。

        user_id=user["user_id"], username=user["username"], tenant_id=user["tenant_id"],
        # 事件类型 login，detail 记录角色；ip/ua 用于安全分析。

        event_type="login", detail={"role": user["role"]}, ip=ip, user_agent=ua,
    )
    # 返回 Token 和用户信息。

    return _login_response(user)


# 注册接口：创建用户并直接登录；201 表示资源创建成功。

@app.post("/api/v1/auth/register", status_code=201)
# async 因为要读取请求体。

async def register(request: Request):
    # 同样容错解析 JSON。

    try:
        body = await request.json()
    except Exception:
        body = {}
    # 调用用户服务创建账号；ValueError 表示业务校验失败（用户名重复、密码太弱等）。

    try:
        # 开始调用 create_user，参数按位置/关键字混合传入。

        user = users.create_user(
            # 用户名：统一转字符串并去空白（具体长度/字符校验在 users 内）。

            str(body.get("username") or ""),
            # 密码：不在本层做 strip，交给用户服务校验强度。

            str(body.get("password") or ""),
            # 显示名，允许为空，由用户服务决定默认值。

            str(body.get("display_name") or ""),
            # 邮箱，转字符串；空值由用户服务处理。

            email=str(body.get("email") or ""),
            # 手机号，转字符串；空值由用户服务处理。

            phone=str(body.get("phone") or ""),
        # 结束 create_user 调用。

        )
    # 捕获用户服务抛出的业务校验异常。

    except ValueError as exc:
        # 转成 422 注册参数错误；str(exc) 作为用户可读原因。

        raise SessionError(422, "REGISTER_INVALID", str(exc))
    # 注册成功后记录来源信息。

    ip, ua = _request_meta(request)
    # 写注册审计。注意：这里知道新建用户的角色，但 detail 固定写 user，
    # 若允许管理员创建其他角色，需要改为 user["role"]（当前保持原逻辑）。

    audit.log_event(
        # 审计主体信息。

        user_id=user["user_id"], username=user["username"], tenant_id=user["tenant_id"],
        # 事件类型 register，记录角色。

        event_type="register", detail={"role": "user"}, ip=ip, user_agent=ua,
    )
    # 注册后直接签发 Token，减少一次登录往返。

    return _login_response(user)


# 当前用户信息接口：前端刷新页面后用它校验 Token 是否仍有效。

@app.get("/api/v1/auth/me")
# Depends(principal) 会自动解析 Bearer Token；解析失败时 security 层抛 401。

def me(p: Principal = Depends(principal)):
    # 从数据库重新读取用户，确保返回的是最新资料而不是 Token 里的旧快照。

    row = fetch_one("SELECT * FROM app_user WHERE user_id=?", (p.user_id,))
    # 用户可能已被删除/禁用，Token 仍在有效期内。

    if not row:
        # 返回 401，提示账号不存在；不泄露更多内部状态。

        raise SessionError(401, "UNAUTHORIZED", "账号不存在")
    # 转成公开字段后返回，过滤密码哈希等敏感信息。

    return users.public_user(row)


# 登出接口。JWT 是无状态的，服务端通常不维护黑名单，
# 因此这里主要是记录审计，并返回 204 让客户端删除本地 Token。

@app.post("/api/v1/auth/logout", status_code=204)
# 需要已登录用户；request 用于取 IP/UA。

def logout(request: Request, p: Principal = Depends(principal)):
    # 提取请求元信息。

    ip, ua = _request_meta(request)
    # 记录登出审计；即使用户不主动登出，Token 也会在过期时间后失效。

    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id,
                    # 审计事件类型 logout。

                    event_type="logout", ip=ip, user_agent=ua)
    # 204 No Content：没有响应体，符合“删除/结束”语义。

    return Response(status_code=204)


# OAuth2 client_credentials 兼容接口：有些调用方只会发 client_id/client_secret，
# 这里同时兼容 username/password 和 client_id/client_secret 两套字段。

@app.post("/api/v1/auth/token")
# async 读取请求体。

async def auth_token(request: Request):
    # 原有文档字符串保留，说明该接口的兼容目标。

    """兼容接口文档的 client_credentials 调用，同时支持 username/password。"""
    # 容错解析 JSON。

    try:
        body = await request.json()
    except Exception:
        body = {}
    # 用户名优先取 username，其次 client_id；两者都没有就是空字符串。

    username = str(body.get("username") or body.get("client_id") or "").strip()
    # 密码优先取 password，其次 client_secret。

    password = str(body.get("password") or body.get("client_secret") or "")
    # 复用同一套用户认证逻辑。

    user = users.authenticate(username, password)
    # 认证失败。

    if not user:
        # 返回 401。

        raise SessionError(401, "INVALID_CREDENTIALS", "用户名或密码错误")
    # 记录来源。

    ip, ua = _request_meta(request)
    # 登录审计，detail.via=token 区分这是 Token 接口登录。

    audit.log_event(user_id=user["user_id"], username=user["username"], tenant_id=user["tenant_id"],
                    # 结束审计调用。

                    event_type="login", detail={"via": "token"}, ip=ip, user_agent=ua)
    # 复用登录响应构造，先拿到完整 token 信息。

    out = _login_response(user)
    # OAuth2 Token 端点通常只返回令牌相关字段，不返回完整 user 对象。

    return {
        # 令牌字符串。

        "access_token": out["access_token"],
        # 认证类型。

        "token_type": "Bearer",
        # 过期秒数。

        "expires_in": out["expires_in"],
        # 权限范围。

        "scope": out["scope"],
    # 结束响应。

    }


# ---------------------------------------------------------------------------
# AI 问答助手
# ---------------------------------------------------------------------------
# 这个接口接收文本/图片/文档上下文，把阻塞的模型调用放到线程池，避免阻塞事件循环。

# ---------------------------------------------------------------------------
# AI 问答助手
# ---------------------------------------------------------------------------
# 聊天接口：multipart/form-data，支持文字、最多 3 张图片、可选文档和会话上下文。

@app.post("/api/v1/assistant/chat")
# 路由函数参数较多，逐个说明见下。

async def assistant_chat(
    # message：用户问题，默认空字符串；允许只发图片。

    message: str = Form(""),
    # document_id：可选，限定回答所依据的文档；None 表示不绑定文档。

    document_id: str | None = Form(None),
    # session_id：可选，绑定学习会话；会继承会话所属文档。

    session_id: str | None = Form(None),
    # history：可选的 JSON 字符串，前端可传最近对话，最多取最后 8 条。

    history: str | None = Form(None),
    # use_web：是否允许联网检索，默认 True。

    use_web: bool = Form(True),
    # images：可选的多文件上传；FastAPI 用 File 绑定 multipart 文件字段。

    images: list[UploadFile] | None = File(None),
    # 当前登录用户，由 Bearer Token 解析。

    p: Principal = Depends(principal),
# 参数列表结束。

):
    # 清理问题文本首尾空白；None 安全兜底。

    message = (message or "").strip()
    # 把 None 统一成空列表，后面可以直接 for 循环。

    files = images or []
    # 既没有文字也没有图片，无法回答。

    if not message and not files:
        # 422 参数错误。

        raise SessionError(422, "ASSISTANT_EMPTY", "请输入问题或上传图片")
    # 图片数量上限 3 张，避免一次请求占用过多模型视觉 token。

    if len(files) > 3:
        # 明确提示上限。

        raise SessionError(422, "ASSISTANT_IMAGE_LIMIT", "最多上传 3 张图片")
    # 如果指定了文档，必须先验证文档存在、属于当前租户且当前用户有访问权。

    if document_id:
        # _get_owned_document 不通过会抛出 404，防止越权读取。

        _get_owned_document(document_id, p)
    # 如果指定了会话，也要验证会话归属。

    if session_id:
        # 读取会话记录；内部会检查租户和用户。

        session = _session_owned(session_id, p)
        # 会话绑定文档时，允许前端不重复传 document_id；显式传入优先，否则继承会话。

        document_id = document_id or session.get("document_id") or ""
    # 初始化历史消息列表。

    hist = []
    # 只有前端传了 history 才解析。

    if history:
        # JSON 解析/结构校验都可能失败，统一兜底为空历史。

        try:
            # 把 JSON 字符串解析为 Python 对象。

            raw_hist = json.loads(history)
            # 只接受列表形式；字典/字符串等都不是合法历史。

            if isinstance(raw_hist, list):
                # 过滤掉非字典元素（防止后续访问字段报错），并只保留最后 8 条控制上下文长度。

                hist = [x for x in raw_hist if isinstance(x, dict)][-8:]
        # 捕获解析失败。

        except Exception:
            # 解析失败就当作没有历史，不阻断本次问答。

            hist = []
    # 初始化图片 data URL 列表。

    data_urls = []
    # 累计图片字节数，用于总量限制。

    total_bytes = 0
    # 逐张读取和校验图片。

    for item in files:
        # 统一转小写，兼容 image/PNG 这类大小写差异。

        mime = (item.content_type or "").lower()
        # 只允许常见浏览器图片格式，防止上传恶意/不支持的内容。

        if mime not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            # 422 表示格式不合法。

            raise SessionError(422, "ASSISTANT_IMAGE_TYPE", "只支持 PNG、JPEG、WEBP、GIF 图片")
        # 异步读取整个文件内容；UploadFile.read() 是 async 方法。

        content = await item.read()
        # 累加总大小，必须在解码/编码前检查，避免内存放大。

        total_bytes += len(content)
        # 单张超过 8 MB 或合计超过 16 MB 就拒绝；1 MB = 1024*1024 字节。

        if len(content) > 8 * 1024 * 1024 or total_bytes > 16 * 1024 * 1024:
            # 413 Payload Too Large。

            raise SessionError(413, "ASSISTANT_IMAGE_TOO_LARGE", "单张图片不超过 8 MB，合计不超过 16 MB")
        # 转成 data URL：data:<mime>;base64,<内容>，可直接传给支持视觉的模型 SDK。
        # base64 会把体积增大约 1/3，因此前面的字节限制是必要的。

        data_urls.append(f"data:{mime};base64,{base64.b64encode(content).decode()}")
    # 用户只发了图片、没有文字时，补一个默认问题，让模型知道要描述图片并回答。

    if not message and data_urls:
        # 默认提示词同时要求结合文档/权威资料，降低纯图片臆测。

        message = "请描述这张图片，并结合文档或权威资料回答我能从中得到什么信息。"
    # assistant_api.answer 是同步阻塞调用（可能访问模型/网络），放到线程池执行。
    # await 让当前事件循环可以继续处理其他请求。

    result = await asyncio.to_thread(
        # 被调用的业务函数：回答问题。

        assistant_api.answer,
        # 问题文本（可能是用户输入或上面的默认提示）。

        question=message,
        # 图片 data URL 列表，交给多模态模型。

        images=data_urls,
        # 文档上下文 ID；空字符串表示不绑定。

        document_id=document_id or "",
        # 会话 ID；空字符串表示一次性问答。

        session_id=session_id or "",
        # 用户 ID：用于读取用户可见的文档/做权限内检索。

        user_id=p.user_id,
        # 租户 ID：保证检索和模型调用不跨租户。

        tenant_id=p.tenant_id,
        # 用户名：用于审计/提示词上下文。

        username=p.username,
        # 角色：影响可访问的数据范围。

        role=p.role,
        # 最近对话历史。

        history=hist,
        # 是否联网检索。

        use_web=use_web,
    # 结束 to_thread 调用，等待结果。

    )
    # 回填实际使用的文档 ID，方便前端关联；没有则为 None。

    result["document_id"] = document_id or None
    # 回填会话 ID。

    result["session_id"] = session_id or None
    # 返回助手结果（通常包含 answer、citations 等字段，由业务模块决定）。

    return result


# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------
# 文档是系统的核心资源：上传 -> 去重/入库 -> 异步解析 -> 大纲/知识点/证据 -> 质量报告。

# ---------------------------------------------------------------------------
# 文档
# ---------------------------------------------------------------------------
# 根据当前用户角色生成“文档归属”SQL 片段和参数。
# admin 在同一租户内看全部文档；普通用户只能看 user_id 等于自己的文档。
# 返回的 clause 会拼接到 WHERE 后面，clause 本身是固定字符串，不包含用户输入，避免 SQL 注入。

def _doc_owner_clause(p: Principal) -> tuple[str, tuple]:
    # 管理员不追加用户过滤。

    if p.role == "admin":
        # 返回空片段和空参数元组。

        return "", ()
    # 普通用户追加参数化条件；? 的值由调用方放入 params。

    return " AND user_id=?", (p.user_id,)


# 读取当前用户有权访问的文档；不存在和无权访问都返回同一个 404，避免泄露文档是否存在。

def _get_owned_document(document_id: str, p: Principal) -> dict:
    # 取得角色对应的过滤片段和参数。

    clause, params = _doc_owner_clause(p)
    # 查询文档：必须匹配 doc_id、tenant_id，且未被软删除。
    # clause 追加 user_id 条件（普通用户）或为空（管理员）。

    row = fetch_one(
        # SQL 字符串拼接只拼接内部固定片段，用户输入全部走 ? 参数。

        "SELECT * FROM document WHERE doc_id=? AND tenant_id=? AND deleted_at IS NULL" + clause,
        # 参数顺序：document_id、tenant_id，再接 owner 过滤参数。
        # list(...)+... 再转 tuple，兼容 sqlite/postgres 驱动。

        tuple([document_id, p.tenant_id] + list(params)),
    # 查询结束。

    )
    # 没查到 = 不存在或无权访问。

    if not row:
        # 统一返回 404，消息不区分两种情况。

        raise SessionError(404, "DOC_NOT_FOUND", "文档不存在或无权访问")
    # 返回完整数据库行，供上层转 API 结构。

    return row


# 读取会话并校验归属。会话服务按租户查询，随后本层再检查用户。

def _session_owned(session_id: str, p: Principal) -> dict:
    # sessions.get_session 会在会话不存在时抛 SessionError（通常 404）。

    session = sessions.get_session(session_id, p.tenant_id)
    # 管理员可以查看同租户内任意用户的会话；普通用户只能看自己的。

    if p.role != "admin" and session.get("user_id") != p.user_id:
        # 普通用户访问他人会话时，同样用 404 隐藏存在性。

        raise SessionError(404, "SESSION_NOT_FOUND", "会话不存在")
    # 返回会话记录。

    return session


# 把数据库 document 行转换成稳定的 API 响应结构。
# 数据库字段名偏内部（doc_id），API 字段名对前端更友好（document_id）。

def _doc_api(row: dict) -> dict:
    # 开始组装字典。

    return {
        # 文档主键，对外叫 document_id。

        "document_id": row["doc_id"],
        # 所属租户，便于多租户前端排查。

        "tenant_id": row["tenant_id"],
        # 文档标题。

        "title": row["title"],
        # 文档类型（论文/教材/报告等）。

        "doc_type": row["doc_type"],
        # 来源 URL；数据库可能为 NULL，统一转空字符串。

        "source_uri": row.get("source_uri") or "",
        # 页数。

        "page_count": row["page_count"],
        # source 子对象：原始文件元信息。

        "source": {
            # 原始文件名。

            "file_name": row["file_name"],
            # MIME 类型。

            "mime_type": row["mime_type"],
            # 文件大小（字节）。

            "size_bytes": row["size_bytes"],
            # 页数在 source 内再给一次，方便前端文件卡片直接使用。

            "page_count": row["page_count"],
            # 内容 SHA-256，用于去重/完整性校验。

            "sha256": row["sha256"],
        # source 子对象结束。

        },
        # 解析状态（processing/parsed/failed 等）。

        "status": row["status"],
        # 文档版本号，结构编辑时递增。

        "doc_version": row["doc_version"],
        # 语言。

        "language": row["language"],
        # 统计信息 JSON（节点数、字数、OCR 页数等），解析失败时回退空字典。

        "stats": load_json(row["stats"], {}),
        # 质量信息子对象。

        "quality": {
            # 质量总分。

            "score": row["quality_score"],
            # 质量等级。

            "level": row["quality_level"],
            # 质量问题列表，数据库存 JSON 文本，这里还原为列表。

            "issues": load_json(row["quality_issues"], []),
        # 质量子对象结束。

        },
        # 解析错误详情；没有错误时为 None。

        "error": load_json(row["error"], None),
        # 创建时间。

        "created_at": row["created_at"],
        # 最后更新时间。

        "updated_at": row["updated_at"],
    # 结束 API 字典。

    }


# 上传文档接口。返回 202 Accepted 而不是 200/201：
# 文件已经接收并创建了文档记录，但解析是异步任务，尚未完成。

@app.post("/api/v1/documents", status_code=202)
# async 路由：需要异步读取上传文件。

async def upload_document(
    # request：用于审计来源 IP/UA。

    request: Request,
    # file：必填文件字段（multipart/form-data 中的 file）。

    file: UploadFile = File(...),
    # doc_type：文档类型，默认 auto 让流水线自动识别。

    doc_type: str = Form("auto"),
    # language：文档语言，默认简体中文。

    language: str = Form("zh-CN"),
    # title：可选标题；不传时由流水线从文件名/内容推断。

    title: str | None = Form(None),
    # Idempotency-Key 请求头：同一个 key 重复提交时返回第一次的结果，避免重复扣费/重复解析。
    # Header(alias=...) 让 Python 参数名 idem 与 HTTP 头名称解耦。

    idem: str | None = Header(None, alias="Idempotency-Key"),
    # 当前用户。

    p: Principal = Depends(principal),
# 参数列表结束。

):
    # 权限检查：只有拥有 documents:write 的角色才能上传。

    require(p, "documents:write")
    # 先查幂等缓存；endpoint 用固定字符串标识“上传文档”这个操作。

    cached = _idem_lookup(idem, "POST /documents")
    # 命中缓存说明是重复请求。

    if cached:
        # 直接返回 202 和第一次的响应体，不再读取/保存文件。

        return JSONResponse(status_code=202, content=cached)
    # 一次性把上传内容读入内存；大文件会占用较多内存，因此下面立即做大小限制。

    content = await file.read()
    # 与配置上限比较（默认 100 MB）。

    if len(content) > MAX_UPLOAD_BYTES:
        # 413 文件过大。注意提示文案写死 100 MB，若修改配置需同步改文案。

        raise SessionError(413, "DOC_TOO_LARGE", "文件超过 100 MB")
    # 空文件没有解析价值。

    if not content:
        # 400 参数/文件不合法。

        raise SessionError(400, "DOC_UNSUPPORTED", "空文件")
    # 取小写扩展名；filename 可能为 None。

    suffix = Path(file.filename or "").suffix.lower()
    # PDF 必须满足魔数 %PDF-；只看扩展名会被伪造文件绕过。
    # content[:5] 是 bytes，startswith(b"%PDF") 做字节级比较。

    if suffix == ".pdf" and not content[:5].startswith(b"%PDF"):
        # 400 文件不是有效 PDF。

        raise SessionError(400, "DOC_UNSUPPORTED", "文件不是有效的 PDF")
    # 创建文档记录可能因不支持的格式抛 ValueError，需要转成业务错误。

    try:
        # pipeline.create_document 负责：计算哈希、去重、落盘、写数据库。
        # 返回 (doc 行, dedup 布尔值)。

        doc, dedup = pipeline.create_document(
            # 租户隔离字段。

            tenant_id=p.tenant_id,
            # 上传者。

            user_id=p.user_id,
            # 原始文件名；没有文件名时用 document + 扩展名兜底。

            file_name=file.filename or f"document{suffix}",
            # MIME 优先用客户端声明，其次按文件名猜，最后用二进制流默认值。
            # 不盲目相信客户端，但这里主要用于存储/下载响应。

            mime_type=file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream",
            # 文件字节内容。

            content=content,
            # 文档类型。

            doc_type=doc_type,
            # 语言。

            language=language,
            # 标题，可为 None。

            title=title,
        # 结束 create_document 调用。

        )
    # 捕获不支持的格式。

    except ValueError:
        # 400 提示支持的格式。

        raise SessionError(400, "DOC_UNSUPPORTED", "暂不支持该文件类型，请上传 PDF、DOCX、Markdown 或图片")
    # dedup=True 说明同租户下已存在相同内容（哈希相同），直接复用旧文档。

    if dedup:
        # 组装去重响应：没有新任务，所以 task_id=None。

        payload = {
            # 复用已有文档 ID。

            "document_id": doc["doc_id"],
            # 没有解析任务。

            "task_id": None,
            # 已有文档的当前状态（可能已解析完成）。

            "status": doc["status"],
            # 明确告诉前端这是去重命中。

            "deduplicated": True,
            # 原文件名。

            "file_name": doc["file_name"],
            # 原页数。

            "page_count": doc["page_count"],
            # 原创建时间。

            "created_at": doc["created_at"],
        # 结束 payload。

        }
        # 把响应写入幂等表，后续相同 key 可直接返回。

        _idem_save(idem, "POST /documents", payload)
        # 审计上传行为；detail 标记 deduplicated=True。

        audit.log_event(
            # 谁在哪个租户执行了上传。

            user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="document_upload",
            # 记录文档 ID、文件名和去重标记。

            detail={"document_id": doc["doc_id"], "file_name": doc["file_name"], "deduplicated": True},
            # 请求来源 IP/UA；这里调用两次 _request_meta，结果相同但略有重复计算。
            # 保持原代码逻辑，不合并。

            ip=_request_meta(request)[0], user_agent=_request_meta(request)[1],
        # 结束审计调用。

        )
        # 返回去重结果（默认 200，函数签名没有指定 status_code；路由装饰器也没有 202）。
        # 这是原接口行为：去重命中返回 200，新建返回 202。

        return payload
    # 新文档：启动异步解析任务，返回 task_id。
    # 传入原始文件路径、文档类型和标题，供解析器使用。

    task_id = pipeline.start_parse(doc["doc_id"], Path(doc["file_path"]), doc_type, title)
    # 新任务的响应体。

    payload = {
        # 新文档 ID。

        "document_id": doc["doc_id"],
        # 任务 ID，前端用它轮询 /tasks/{task_id} 或订阅 SSE。

        "task_id": task_id,
        # 新文档处于处理中。

        "status": "processing",
        # 不是去重命中。

        "deduplicated": False,
        # 原始文件名（可能为 None；JSON 会输出 null）。

        "file_name": file.filename,
        # 解析前页数未知，先返回 0。

        "page_count": 0,
        # 创建时间。

        "created_at": iso(),
    # 结束 payload。

    }
    # 保存幂等响应。

    _idem_save(idem, "POST /documents", payload)
    # 审计新建上传；detail 标记 deduplicated=False。

    audit.log_event(
        # 上传主体。

        user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="document_upload",
        # 文档信息。

        detail={"document_id": doc["doc_id"], "file_name": file.filename, "deduplicated": False},
        # 来源信息。

        ip=_request_meta(request)[0], user_agent=_request_meta(request)[1],
    # 结束审计调用。

    )
    # 返回 202，并带 Location 头指向文档详情，符合 REST 创建语义。

    return JSONResponse(
        # 状态码 202。

        status_code=202,
        # 响应体。

        content=payload,
        # Location 让客户端知道后续该查哪个资源。

        headers={"Location": f"/api/v1/documents/{doc['doc_id']}"},
    # 结束 JSONResponse。

    )


# 网页链接导入：抓取网页/在线文件，转成文档后走同一套解析流程。

@app.post("/api/v1/documents/from-url", status_code=202)
# payload 直接声明为 dict，由 FastAPI 解析 JSON 请求体。

async def import_document_url(payload: dict, p: Principal = Depends(principal)):
    # 需要文档写权限。

    require(p, "documents:write")
    # 取 url 并去空白；None 安全。

    url = str(payload.get("url") or "").strip()
    # URL 必填。

    if not url:
        # 422 参数错误。

        raise SessionError(422, "URL_REQUIRED", "请填写网页链接")
    # 抓取网页可能涉及阻塞网络 IO，放到线程池执行。

    try:
        # url_import.fetch_url 返回 file_name/mime_type/content/title/source_uri 等字段。

        fetched = await asyncio.to_thread(url_import.fetch_url, url)
    # 抓取失败/URL 不安全/内容不支持时抛 ValueError。

    except ValueError as exc:
        # 转成 422 URL_IMPORT_FAILED。

        raise SessionError(422, "URL_IMPORT_FAILED", str(exc))
    # 抓取成功后创建文档记录。

    try:
        # 复用文档入库逻辑，返回 (doc, dedup)。

        doc, dedup = pipeline.create_document(
            # 租户和用户来自当前登录主体，不能由请求体伪造。

            tenant_id=p.tenant_id, user_id=p.user_id, file_name=fetched["file_name"],
            # 用抓取结果的 MIME 和内容。

            mime_type=fetched["mime_type"], content=fetched["content"],
            # 文档类型可由请求指定，默认自动识别；语言当前固定中文。

            doc_type=payload.get("doc_type") or "auto", language="zh-CN",
            # 标题和来源 URL 来自抓取结果，source_uri 缺失时用空字符串。

            title=fetched.get("title"), source_uri=fetched.get("source_uri", ""),
        # 结束 create_document 调用。

        )
    # 网页内容无法转成支持的文档格式。

    except ValueError:
        # 400 提示不支持。

        raise SessionError(400, "DOC_UNSUPPORTED", "网页内容暂不支持")
    # 去重命中：不新建解析任务。

    if dedup:
        # 审计导入行为。

        audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id,
                        # 记录 URL、文档 ID 和去重标记。

                        event_type="document_import", detail={"url": url, "document_id": doc["doc_id"], "deduplicated": True})
        # 返回已有文档；task_id=None。

        return {"document_id": doc["doc_id"], "task_id": None, "status": doc["status"], "deduplicated": True,
                # 来源 URL 缺失时回退到用户提交的 url。

                "title": doc.get("title"), "source_uri": doc.get("source_uri") or url}
    # 新文档：启动解析任务，使用请求指定的类型或 auto。

    task_id = pipeline.start_parse(doc["doc_id"], Path(doc["file_path"]), payload.get("doc_type") or "auto", fetched.get("title"))
    # 记录导入审计。

    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id,
                    # 记录 URL 和文档 ID。

                    event_type="document_import", detail={"url": url, "document_id": doc["doc_id"],
                                                         # 记录文件名。

                                                         "file_name": fetched["file_name"]})
    # 返回新任务信息；202 由路由装饰器保证。

    return {"document_id": doc["doc_id"], "task_id": task_id, "status": "processing",
            # 标题和来源 URL。

            "deduplicated": False, "title": fetched.get("title"), "source_uri": fetched.get("source_uri", ""),
            # 文件名。

            "file_name": fetched["file_name"]}


# 文档列表：支持按状态、类型、标题关键词过滤，游标分页。
# 注意这里先把租户/用户范围内的全部文档查出来，再在 Python 中过滤；
# 数据量大时应改成 SQL WHERE + LIMIT，当前实现保持原逻辑。

@app.get("/api/v1/documents")
# 查询参数：状态、类型、关键词 q、每页数量、游标、排序方式。

def list_documents(
    # status：可选，例如 parsed/processing/failed。

    status: str | None = None,
    # doc_type：可选，文档类型。

    doc_type: str | None = None,
    # q：标题模糊搜索关键词。

    q: str | None = None,
    # limit：每页数量，后面会被 _paginate 限制到 1~100。

    limit: int = 20,
    # cursor：上一页返回的游标。

    cursor: str | None = None,
    # sort：排序方式参数。当前实现固定按 created_at DESC，sort 未参与 SQL（保留兼容）。

    sort: str = "created_at_desc",
    # 当前用户。

    p: Principal = Depends(principal),
# 结束参数列表。

):
    # 生成文档归属过滤条件。

    clause, owner_params = _doc_owner_clause(p)
    # 查询当前租户下未删除的文档，并追加用户过滤。
    # ORDER BY created_at DESC 保证最新文档在前。

    rows = fetch_all(
        # SQL 拼接的位置：tenant 条件之后、ORDER BY 之前。clause 是内部固定片段。

        "SELECT * FROM document WHERE tenant_id=? AND deleted_at IS NULL" + clause + " ORDER BY created_at DESC",
        # 参数：tenant_id 在前，普通用户再追加 user_id。

        tuple([p.tenant_id] + list(owner_params)),
    # 查询结束。

    )
    # 状态过滤（内存中）。

    if status:
        # 列表推导式保留状态完全相等的行。

        rows = [r for r in rows if r["status"] == status]
    # 类型过滤。

    if doc_type:
        # 类型精确匹配。

        rows = [r for r in rows if r["doc_type"] == doc_type]
    # 关键词过滤。

    if q:
        # 标题统一转小写后做子串匹配；title 可能为 NULL，用空字符串兜底。

        rows = [r for r in rows if q.lower() in (r["title"] or "").lower()]
    # 对过滤后的列表做游标分页。

    page, next_cursor = _paginate(rows, limit, cursor)
    # 初始化返回条目列表。

    items = []
    # 逐条补充摘要和质量信息。

    for r in page:
        # 查询该文档的第一条 abstract 节点。不是所有文档都有摘要节点，所以用 or {} 兜底。

        abstract_row = fetch_one(
            # 按 node_type=abstract 过滤，按 order_index 取最靠前的一条。

            "SELECT text FROM node WHERE doc_id=? AND node_type='abstract' ORDER BY order_index LIMIT 1",
            # 只取一个字段，减少数据传输。

            (r["doc_id"],),
        # 查询结束；无结果时得到 None，or {} 变成空字典。

        ) or {}
        # 组装列表项。

        items.append({
            # 文档 ID。

            "document_id": r["doc_id"],
            # 标题。

            "title": r["title"],
            # 类型。

            "doc_type": r["doc_type"],
            # 状态。

            "status": r["status"],
            # 页数。

            "page_count": r["page_count"],
            # 统计信息。

            "stats": load_json(r["stats"], {}),
            # 摘要文本截断到 220 字符，避免列表响应过大。

            "abstract": (abstract_row.get("text") or "")[:220],
            # 质量分数和等级（不返回完整问题列表，减少体积）。

            "quality": {"score": r["quality_score"], "level": r["quality_level"]},
            # 创建时间。

            "created_at": r["created_at"],
        # 结束列表项。

        })
    # 标准分页响应。

    return {
        # 当前页条目。

        "items": items,
        # 下一页游标，可能为 None。

        "next_cursor": next_cursor,
        # 有游标就说明还有更多。

        "has_more": bool(next_cursor),
    # 结束响应。

    }


# 文档详情：复用 _doc_api 输出完整字段。

@app.get("/api/v1/documents/{document_id}")
# 路径参数 document_id 由 FastAPI 自动传入。

def get_document(document_id: str, p: Principal = Depends(principal)):
    # 内部会做租户/用户归属校验，不存在或无权访问统一 404。

    row = _get_owned_document(document_id, p)
    # 转 API 结构返回。

    return _doc_api(row)


# 删除文档：默认软删除，purge=True 时额外清理磁盘/对象存储中的原始文件。
# 204 表示成功且无响应体。

@app.delete("/api/v1/documents/{document_id}", status_code=204)
# purge 是查询参数（False/True），不是请求体。

def delete_document(document_id: str, purge: bool = False, p: Principal = Depends(principal)):
    # 需要写权限。

    require(p, "documents:write")
    # 先校验存在性和归属；不通过直接 404。

    _get_owned_document(document_id, p)
    # 开数据库事务执行软删除。

    with db() as conn:
        # 设置 deleted_at 和状态 deleted；保留数据库记录，便于审计/恢复。
        # updated_at 同步刷新。

        conn.execute("UPDATE document SET deleted_at=?, status='deleted', updated_at=? WHERE doc_id=?", (iso(), iso(), document_id))
    # 只有明确要求 purge 才清理物理文件；软删除默认保留文件，便于恢复。

    if purge:
        # 遍历可能的扩展名，逐个尝试删除对象存储中的文件。
        # 这里用固定后缀列表，覆盖上传支持的常见格式。

        for suffix in (".pdf", ".docx", ".md", ".markdown", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp"):
            # 删除失败（文件本来不存在/存储不可用）不应让删除接口失败。

            try:
                # storage_mod.delete 接受对象 key，具体实现由平台存储层决定。

                storage_mod.delete(f"{document_id}{suffix}")
            # 捕获所有存储异常。

            except Exception:
                # 忽略，继续尝试其他后缀。

                pass
        # 同时清理本地 uploads 目录下以 document_id 开头的文件；glob 的 . 需转义吗？
        # 这里 f"{document_id}.*" 中 document_id 是内部生成的 ID，不含通配符；点号匹配字面点。

        for path in (DATA_DIR / "uploads").glob(f"{document_id}.*"):
            # unlink 删除本地文件。

            try:
                # 文件可能已被删除或正被占用。

                path.unlink()
            # 忽略单个文件清理失败。

            except Exception:
                # 结束 purge 分支。

                pass
    # 返回 204。

    return Response(status_code=204)


# 重新解析文档：用于用户更换解析模板、修复解析失败或内容更新后重跑。
# 202 表示任务已排队。

@app.post("/api/v1/documents/{document_id}/reparse", status_code=202)
# payload 可选，允许不传请求体；template 可指定解析模板。

def reparse_document(document_id: str, payload: dict | None = None, p: Principal = Depends(principal)):
    # 需要写权限。

    require(p, "documents:write")
    # 校验文档归属。

    row = _get_owned_document(document_id, p)
    # 查找原始文件路径；解析任务需要重新读取源文件。

    path = pipeline.get_document_path(document_id)
    # 原始文件可能已被清理/丢失。

    if not path:
        # 404 提示原始文件缺失。

        raise SessionError(404, "DOC_NOT_FOUND", "原始文件缺失")
    # None 统一成空字典，后面可以安全 get。

    payload = payload or {}
    # 启动解析任务：模板优先用请求指定，否则沿用文档原类型；标题传 None 让解析器沿用。

    task_id = pipeline.start_parse(document_id, path, payload.get("template") or row["doc_type"], None)
    # 立即把文档状态改为 parsing，前端轮询时能马上看到。

    with db() as conn:
        # 更新状态和更新时间。

        conn.execute("UPDATE document SET status='parsing', updated_at=? WHERE doc_id=?", (iso(), document_id))
    # 记录重解析审计。

    audit.log_event(user_id=p.user_id, username=p.username, tenant_id=p.tenant_id, event_type="document_reparse",
                    # 只记录文档 ID 即可。

                    detail={"document_id": document_id})
    # 返回任务信息和 queued 状态。

    return {"document_id": document_id, "task_id": task_id, "status": "queued"}


# 获取文档大纲树：按 parent_id 递归组装，支持限制深度、过滤节点类型、可选返回全文。

@app.get("/api/v1/documents/{document_id}/outline")
# depth：最大层级；include_text：是否带正文；node_types：逗号分隔的类型白名单。

def get_outline(document_id: str, depth: int = 3, include_text: bool = False, node_types: str | None = None, p: Principal = Depends(principal)):
    # 校验文档归属。

    row = _get_owned_document(document_id, p)
    # 一次取出该文档全部节点，按 order_index 排序；后续在内存中建树。
    # 对单文档来说节点数量通常可控；超大文档可改为按层查询。

    nodes = fetch_all("SELECT * FROM node WHERE doc_id=? ORDER BY order_index", (document_id,))
    # 建立 node_id -> 节点 的索引。当前 build 函数没有用到它，保留原变量。

    by_id = {n["node_id"]: n for n in nodes}
    # 把 "section,paragraph" 解析成集合；未传则不限制类型。

    allowed_types = set(node_types.split(",")) if node_types else None
    # 深度限制在 1~6，防止前端传入异常值导致递归过深/响应过大。

    depth = max(1, min(6, int(depth or 3)))
# 空行保留，下面定义递归函数。


    # 递归构建某个父节点下的子节点列表。parent_id=None 表示从根开始。

    def build(parent_id):
        # 当前层的结果列表。

        out = []
        # 找出 parent_id 匹配的子节点，按 order_no（同级顺序）和 order_index（全局顺序）排序。
        # 排序键使用元组，先比 order_no，再比 order_index。

        for n in sorted([x for x in nodes if x["parent_id"] == parent_id], key=lambda x: (x["order_no"], x["order_index"])):
            # 超过请求深度的节点直接跳过；其子节点也不会被递归到。

            if n["level"] > depth:
                # continue 进入下一个节点。

                continue
            # 节点类型不在白名单中则跳过。

            if allowed_types and n["node_type"] not in allowed_types:
                # 继续下一个。

                continue
            # 组装节点输出。

            item = {
                # 节点 ID。

                "node_id": n["node_id"],
                # 父节点 ID，根节点为 None。

                "parent_id": n["parent_id"],
                # 层级。

                "level": n["level"],
                # 节点类型（section/paragraph/abstract/figure 等）。

                "node_type": n["node_type"],
                # 同级顺序号。

                "order_no": n["order_no"],
                # 标题。

                "title": n["title"],
                # 起始页。

                "page_start": n["page_start"],
                # 结束页。

                "page_end": n["page_end"],
                # 字符数。

                "char_count": n["char_count"],
                # 解析置信度。

                "confidence": n["confidence"],
                # 递归构建子节点；这一步可能较深，但 depth 已限制在 6 层。

                "children": build(n["node_id"]),
            # 结束节点字典。

            }
            # 只有显式要求才返回正文，避免大纲接口过大。

            if include_text:
                # 把正文挂到 item 上。

                item["text"] = n["text"]
            # 加入当前层结果。

            out.append(item)
        # 返回当前层的子节点列表。

        return out

    # 返回文档版本和根节点列表；version 可用于前端判断结构是否变化。

    return {"document_id": document_id, "version": row["doc_version"], "nodes": build(None)}


# 编辑大纲：批量执行 update/move/delete/merge 操作，并递增文档版本。
# 这是结构性写操作，必须先做版本快照，便于误操作后恢复。

@app.put("/api/v1/documents/{document_id}/outline")
# payload 必须包含 operations 列表。

def update_outline(document_id: str, payload: dict, p: Principal = Depends(principal)):
    # 需要文档写权限。

    require(p, "documents:write")
    # 校验文档归属。

    row = _get_owned_document(document_id, p)
    # 取操作列表；没有时为空列表，后面不会执行任何操作。
    ops = payload.get("operations") or []
    # 编辑前自动快照：记录旧结构和操作者，出问题时可回滚。
    # 快照失败会阻止继续编辑，避免“无备份改结构”。

    versions.snapshot(document_id, reason="结构编辑前自动快照", created_by=p.username or p.user_id)
    # 统计成功应用的操作数，用于响应。

    applied = 0
    # 所有操作放在同一个数据库事务中，要么整体成功，要么整体回滚。

    with db() as conn:
        # 逐条执行客户端提交的操作。

        for op in ops:
            # 非字典元素直接跳过，避免脏数据导致属性访问异常。

            if not isinstance(op, dict):
                # 继续处理下一条。

                continue
            # 每条操作必须指向一个节点。

            node_id = op.get("node_id")
            # 校验节点存在且属于当前文档；不存在立即 422，事务回滚。
            # 同时防止跨文档修改节点。

            if not node_id or not conn.execute("SELECT 1 FROM node WHERE node_id=? AND doc_id=?", (node_id, document_id)).fetchone():
                # 422 结构编辑参数错误，details 带出问题节点。

                raise SessionError(422, "OUTLINE_INVALID", "结构节点不存在", {"node_id": node_id})
            # update：只允许改标题和层级，不能借动态 SQL 改任意列。

            if op.get("op") == "update":
                # 动态收集 SET 片段和参数；片段来自固定白名单，值全部参数化。

                sets, vals = [], []
                # 只处理白名单字段。

                for k in ("title", "level"):
                    # 只有请求里真的带了这个字段才更新。

                    if k in op:
                        # 拼接 "title=?" 或 "level=?"，列名不是用户输入。

                        sets.append(f"{k}=?")
                        # 对应的新值。

                        vals.append(op[k])
                # 至少有一个字段要改。

                if sets:
                    # 最后追加 WHERE node_id=? 的参数。

                    vals.append(node_id)
                    # 执行动态 UPDATE；', '.join(sets) 只包含内部白名单列名，无注入风险。

                    conn.execute(f"UPDATE node SET {', '.join(sets)} WHERE node_id=?", tuple(vals))
            # move：调整父节点和同级顺序。

            elif op.get("op") == "move":
                # 更新 parent_id 和 order_no。

                conn.execute(
                    # SQL 只有三个占位符，顺序为 parent_id、order_no、node_id。

                    "UPDATE node SET parent_id=?, order_no=? WHERE node_id=?",
                    # order_no 缺省或 0 时用 1，并强制 int，防止字符串进入数据库。

                    (op.get("parent_id"), int(op.get("order_no") or 1), node_id),
                # 结束 move 分支。

                )
            # delete：删除节点及其直接子节点。
            # 注意：只删一层子节点，孙节点不会级联删除；当前代码保持此行为。

            elif op.get("op") == "delete":
                # 用 OR 条件删除自身和 parent_id 等于自身的直接子节点。

                conn.execute("DELETE FROM node WHERE node_id=? OR parent_id=?", (node_id, node_id))
            # merge：把当前节点正文合并到目标节点，并把子节点迁移过去，然后删除当前节点。

            elif op.get("op") == "merge":
                # 目标节点 ID 可由客户端显式指定。

                target_id = op.get("target_id")
                # 没指定目标时，自动找同级的下一个节点作为合并目标。

                if not target_id:
                    # 查询同一文档、同一父节点下 order_no 更大的第一个节点。

                    sibling = conn.execute(
                        # parent_id IS ? 兼容 NULL（根节点）；ORDER BY + LIMIT 1 取最近的下一个兄弟。

                        "SELECT node_id FROM node WHERE doc_id=? AND parent_id IS ? AND order_no>?"
                        # 结束子查询。

                        " ORDER BY order_no LIMIT 1",
                        # 参数：文档 ID、父节点 ID、当前 order_no。

                        (document_id, op.get("parent_id"), int(op.get("order_no") or 0)),
                    # 执行查询。

                    ).fetchone()
                    # 有兄弟节点就取它的 node_id，否则为 None。

                    target_id = sibling["node_id"] if sibling else None
                # 目标必须存在且属于当前文档，否则拒绝合并。

                if not target_id or not conn.execute("SELECT 1 FROM node WHERE node_id=? AND doc_id=?", (target_id, document_id)).fetchone():
                    # 422 合并目标不存在。

                    raise SessionError(422, "OUTLINE_INVALID", "合并目标不存在", {"node_id": node_id})
                # 读取源节点完整行，准备合并正文。

                source = conn.execute("SELECT * FROM node WHERE node_id=?", (node_id,)).fetchone()
                # 读取目标节点完整行。

                target = conn.execute("SELECT * FROM node WHERE node_id=?", (target_id,)).fetchone()
                # 用两个换行拼接正文；NULL 用空字符串兜底。

                merged = (target["text"] or "") + "\n\n" + (source["text"] or "")
                # 更新目标节点正文和字符数。

                conn.execute("UPDATE node SET text=?, char_count=? WHERE node_id=?",
                             # 去首尾空白后写入，len 与写入内容保持一致。

                             (merged.strip(), len(merged.strip()), target_id))
                # 把源节点的子节点改挂到目标节点下；排除目标自身，避免自引用。

                conn.execute("UPDATE node SET parent_id=? WHERE parent_id=? AND node_id<>?",
                             # 参数：新父节点=target_id，旧父节点=node_id，node_id<>target_id。

                             (target_id, node_id, target_id))
                # 删除被合并的源节点。

                conn.execute("DELETE FROM node WHERE node_id=?", (node_id,))
            # 该条操作成功计数 +1。

            applied += 1
        # 所有操作成功后，文档版本号 +1；客户端可用它做乐观并发判断。

        new_version = row["doc_version"] + 1
        # 更新版本和更新时间。

        conn.execute("UPDATE document SET doc_version=?, updated_at=? WHERE doc_id=?", (new_version, iso(), document_id))
    # 返回新版本、应用条数和更新时间。

    return {"document_id": document_id, "version": new_version, "applied": applied, "updated_at": iso()}


# 文档质量报告：把数据库里的质量指标转成前端可展示的结构。

@app.get("/api/v1/documents/{document_id}/quality")
# 校验文档归属。

def get_quality(document_id: str, p: Principal = Depends(principal)):
    # 质量问题列表。

    row = _get_owned_document(document_id, p)
    # 解析 issues JSON；损坏时回退空列表。

    issues = load_json(row["quality_issues"], [])
    # 解析统计 JSON。

    stats = load_json(row["stats"], {})
    # 取出质量指标子字典；没有则空字典。

    qm = stats.get("quality_metrics") or {}
    # 文本覆盖率：优先用显式指标，否则有节点就视为 1.0，没有节点为 0。

    coverage = qm.get("text_coverage", 1.0 if stats.get("node_count") else 0)
    # 标题识别置信度：有至少 3 个章节时默认 0.95，否则 0.6。

    heading = qm.get("heading_confidence", 0.95 if stats.get("section_count", 0) >= 3 else 0.6)
    # 乱码率，默认 0。

    garbled = qm.get("garbled_rate", 0.0)
    # 图表关联率：有图时默认 0.95，无图时视为 1.0。

    figure = qm.get("figure_link_rate", 0.95 if stats.get("figure_count", 0) else 1.0)
    # OCR 页数，转 int 并兜底 0。

    ocr_pages = int(stats.get("ocr_pages") or 0)
    # 开始组装质量响应。

    return {
        # 文档 ID。

        "document_id": document_id,
        # 总分。

        "score": row["quality_score"],
        # 等级。

        "level": row["quality_level"],
        # 指标数组：每项包含名称、值、阈值、是否通过。

        "metrics": [
            # 文本覆盖率：阈值 0.98。
            # 对 OCR 文档放宽到 0.6（OCR 本身会损失部分文本）。

            {"name": "text_coverage", "value": coverage, "threshold": 0.98,
             # 普通文档达到 0.98 通过；有 OCR 页时达到 0.6 也通过。

             "passed": coverage >= 0.98 or (ocr_pages > 0 and coverage >= 0.6)},
            # 标题识别 F1 阈值 0.95。

            {"name": "heading_f1", "value": heading, "threshold": 0.95, "passed": heading >= 0.95},
            # 乱码率阈值 0.5%，越低越好，因此用 <=。

            {"name": "garbled_rate", "value": garbled, "threshold": 0.005, "passed": garbled <= 0.005},
            # 图表关联率阈值 0.90。

            {"name": "figure_link_rate", "value": figure, "threshold": 0.90, "passed": figure >= 0.90},
            # OCR 页数只做信息展示，没有通过/不通过阈值。

            {"name": "ocr_pages", "value": stats.get("ocr_pages", 0), "threshold": None, "passed": True},
        # 指标数组结束。

        ],
        # OCR 子对象：页数和模型名。

        "ocr": {"pages": stats.get("ocr_pages", 0), "model": stats.get("ocr_model", "")},
        # 当前实现没有逐页失败信息，返回空列表保持接口字段稳定。

        "failed_pages": [],
        # 从 issues 中提取 suggestion/message，作为前端建议列表。

        "suggestions": [i.get("message", "") for i in issues],
        # 同时返回完整 issues，供高级页面展示。

        "issues": issues,
    # 结束响应。

    }


# 知识点列表：按重要度降序、同级顺序排列，支持按节点和类型过滤。

@app.get("/api/v1/documents/{document_id}/knowledge-points")
# 参数：node_id/kp_type 可选过滤，limit/cursor 分页。

def list_kps(document_id: str, node_id: str | None = None, kp_type: str | None = None, limit: int = 100, cursor: str | None = None, p: Principal = Depends(principal)):
    # 校验文档归属（这里用 _ensure_doc 别名，语义相同）。

    _ensure_doc(document_id, p)
    # 基础 SQL：只查当前文档的知识点。

    sql = "SELECT * FROM knowledge_point WHERE doc_id=?"
    # 参数列表与 SQL 占位符同步增长。

    params: list = [document_id]
    # 按节点过滤。

    if node_id:
        # 追加参数化条件。

        sql += " AND node_id=?"
        # 记录 node_id。

        params.append(node_id)
    # 按知识点类型过滤。

    if kp_type:
        # 追加类型条件。

        sql += " AND kp_type=?"
        # 记录类型。

        params.append(kp_type)
    # 排序：重要度高的在前，同重要度按 order_no。

    sql += " ORDER BY importance DESC, order_no"
    # 执行查询。

    rows = fetch_all(sql, params)
    # 内存分页。

    page, next_cursor = _paginate(rows, limit, cursor)
    # 返回分页结构。

    return {
        # 列表推导式逐行转 API 对象。

        "items": [
            # 开始一个知识点对象。

            {
                # 知识点 ID。

                "kp_id": r["kp_id"],
                # 所属节点。

                "node_id": r["node_id"],
                # 名称。

                "name": r["name"],
                # 类型（概念/公式/方法等）。

                "kp_type": r["kp_type"],
                # 定义。

                "definition": r["definition"],
                # 重要度。

                "importance": r["importance"],
                # 难度。

                "difficulty": r["difficulty"],
                # 证据数量：只返回数量，不返回完整证据 ID 列表，减少响应体积。

                "evidence_count": len(load_json(r["evidence_ids"], [])),
                # 前置知识 ID 列表。

                "prerequisites": load_json(r["prerequisites"], []),
            # 结束知识点对象。

            }
            # 对 page 中每一行执行上面的构造。

            for r in page
        # 列表推导式结束。

        ],
        # 下一页游标。

        "next_cursor": next_cursor,
        # 是否还有更多。

        "has_more": bool(next_cursor),
    # 结束响应。

    }


# 证据列表：引用原文片段，支持按节点和证据强度过滤。

@app.get("/api/v1/documents/{document_id}/evidence")
# 参数：node_id、strength、limit、cursor。

def list_evidence(document_id: str, node_id: str | None = None, strength: str | None = None, limit: int = 100, cursor: str | None = None, p: Principal = Depends(principal)):
    # 校验文档归属。

    _ensure_doc(document_id, p)
    # 基础查询。

    sql = "SELECT * FROM evidence WHERE doc_id=?"
    # 参数列表。

    params: list = [document_id]
    # 按节点过滤。

    if node_id:
        # 追加节点条件。

        sql += " AND node_id=?"
        # 记录 node_id。

        params.append(node_id)
    # 按证据强度过滤。

    if strength:
        # 追加强度条件。

        sql += " AND strength=?"
        # 记录强度。

        params.append(strength)
    # 按页码和字符起点排序，保证引用顺序稳定。

    sql += " ORDER BY page_no, char_start"
    # 执行查询。

    rows = fetch_all(sql, params)
    # 分页。

    page, next_cursor = _paginate(rows, limit, cursor)
    # 返回分页结构。

    return {
        # 逐条构造证据对象。

        "items": [
            # 开始证据字典。

            {
                # 证据 ID。

                "evidence_id": r["evidence_id"],
                # 所属节点。

                "node_id": r["node_id"],
                # 页码。

                "page_no": r["page_no"],
                # 原文引用。

                "quote": r["quote"],
                # 在页面文本中的起始字符位置。

                "char_start": r["char_start"],
                # 结束字符位置。

                "char_end": r["char_end"],
                # 证据强度。

                "strength": r["strength"],
            # 结束对象。

            }
            # 列表推导式。

            for r in page
        # 列表结束。

        ],
        # 下一页游标。

        "next_cursor": next_cursor,
        # 是否还有更多。

        "has_more": bool(next_cursor),
    # 结束响应。

    }


# 下载/预览原始文档文件。

@app.get("/api/v1/documents/{document_id}/file")
# 校验文档归属。

def get_document_file(document_id: str, p: Principal = Depends(principal)):
    # 查原始文件路径。

    row = _get_owned_document(document_id, p)
    # 文件可能已被清理。

    path = pipeline.get_document_path(document_id)
    # 没有路径就返回 404。

    if not path:
        # 明确“原始文件缺失”，区别于文档记录不存在。

        raise SessionError(404, "DOC_NOT_FOUND", "原始文件缺失")
    # FileResponse 会设置 Content-Disposition 等下载头；media_type 用数据库记录。

    return FileResponse(path, media_type=row["mime_type"], filename=row["file_name"])


# 兼容旧代码的别名：语义上就是“确保文档可访问”，内部复用 _get_owned_document。

def _ensure_doc(document_id: str, p: Principal) -> dict:
    # 直接返回文档行；不存在/无权访问时由内部抛 404。

    return _get_owned_document(document_id, p)


# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
# 文档解析等耗时工作会创建异步任务，前端通过任务状态/事件流获知进度。

# ---------------------------------------------------------------------------
# 任务
# ---------------------------------------------------------------------------
# 把 task 数据库行转成 API 结构。

def _task_api(row: dict) -> dict:
    # 开始组装。

    return {
        # 任务 ID。

        "task_id": row["task_id"],
        # 任务类型（parse/...）。

        "type": row["type"],
        # 状态（queued/running/succeeded/failed/canceled）。

        "status": row["status"],
        # 当前阶段（如 extracting/ocr/indexing）。

        "stage": row["stage"],
        # 进度，通常 0~100。

        "progress": row["progress"],
        # 关联文档。

        "document_id": row["document_id"],
        # 成功结果 JSON；没有则 None。

        "result": load_json(row["result"], None),
        # 失败错误 JSON；没有则 None。

        "error": load_json(row["error"], None),
        # 创建时间。

        "created_at": row["created_at"],
        # 更新时间。

        "updated_at": row["updated_at"],
    # 结束。

    }


# 查询任务状态。前端轮询此接口获取进度。

@app.get("/api/v1/tasks/{task_id}")
# 需要登录（Depends principal）。

def get_task(task_id: str, p: Principal = Depends(principal)):
    # 按 task_id 查任务。

    row = fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
    # 不存在。

    if not row:
        # 404。

        raise SessionError(404, "TASK_NOT_FOUND", "任务不存在")
    # 转 API 结构。

    return _task_api(row)


# 任务事件流（SSE）：持续推送 snapshot/progress/completed/failed/heartbeat。
# 适合前端实时进度条；比轮询更省请求，但连接会长期占用。

@app.get("/api/v1/tasks/{task_id}/events")
# 注意：此接口没有 Principal 依赖，仅凭 task_id 查询；当前代码未做租户/用户校验。

async def task_events(task_id: str, request: Request):
    # 先查任务，不存在直接 404。

    row = fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
    # 任务不存在。

    if not row:
        # 返回 404。

        raise SessionError(404, "TASK_NOT_FOUND", "任务不存在")

    # 定义异步生成器：每次 yield 一段 SSE 文本，StreamingResponse 会逐段发送。

    async def stream():
        # last 保存上一次任务数据，用于判断是否变化，避免重复推送。

        last = None
        # sent 同时作为事件 id 和循环计数。

        sent = 0
        # 最多循环 1800 次，每次约 0.5 秒，连接最长约 15 分钟。

        while sent < 1800:
            # 每轮重新查数据库，获取最新状态。

            row = fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
            # 任务被删除/不存在就结束流。

            if not row:
                # 转 API 结构。

                break
            # 数据与上次相同则不重复发进度事件（心跳仍会发）。

            data = _task_api(row)
            # 首次推送：发送完整 snapshot，让前端立即拿到当前状态。

            if data != last:
                # last is None 表示这是第一帧。

                if last is None:
                    # SSE 帧格式：event 行、id 行、data 行，最后空行表示消息结束。
                    # json.dumps(..., ensure_ascii=False) 保留中文，不转成 \uXXXX。

                    yield f"event: snapshot\nid: {sent}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                # 非首帧且任务已进入终态：发送 completed/failed 并结束。

                elif row["status"] in ("succeeded", "failed", "canceled"):
                    # succeeded 映射为 completed 事件；其他终态（failed/canceled）映射为 failed。

                    event = "completed" if row["status"] == "succeeded" else "failed"
                    # 终态 payload 带任务 ID、状态、结果和错误。

                    payload = {"task_id": task_id, "status": row["status"], "result": data["result"], "error": data["error"]}
                    # 发送终态事件。

                    yield f"event: {event}\nid: {sent}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    # 终态后退出循环，关闭 SSE。

                    break
                # 其他情况：任务仍在进行，发送进度事件。

                else:
                    # 进度 payload 带任务、阶段、进度和消息。

                    payload = {"task_id": task_id, "stage": row["stage"], "progress": row["progress"], "message": row["stage"]}
                    # 发送 progress 事件。

                    yield f"event: progress\nid: {sent}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                # 更新 last，供下一轮比较。

                last = data
            # 客户端断开连接就停止查询，避免后台空转。

            if await request.is_disconnected():
                # 退出生成器。

                break
            # 让出事件循环 0.5 秒，避免高频轮询数据库。

            await asyncio.sleep(0.5)
            # 计数 +1。

            sent += 1
            # 每 20 轮（约 10 秒）发一次心跳。

            if sent % 20 == 0:
                # 心跳没有 id，只有 timestamp；用于保持代理/浏览器连接活跃。

                yield f"event: heartbeat\ndata: {json.dumps({'timestamp': iso()})}\n\n"

    # 返回 SSE 响应；Cache-Control 禁止缓存，X-Accel-Buffering=no 关闭 Nginx 缓冲。

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# 取消任务：将未完成任务标记为 canceled。当前实现没有鉴权依赖，只按 task_id 操作。

@app.post("/api/v1/tasks/{task_id}/cancel")
# payload 可选，当前未使用；保留以兼容客户端传空对象/备注。

def cancel_task(task_id: str, payload: dict | None = None):
    # 查任务。

    row = fetch_one("SELECT * FROM task WHERE task_id=?", (task_id,))
    # 不存在。

    if not row:
        # 404。

        raise SessionError(404, "TASK_NOT_FOUND", "任务不存在")
    # 已经是终态时，取消是幂等操作，直接返回现状。

    if row["status"] in ("succeeded", "failed", "canceled"):
        # 返回当前状态和最后更新时间作为 canceled_at。

        return {"task_id": task_id, "status": row["status"], "canceled_at": row["updated_at"]}
    # 未完成任务：更新状态。

    with db() as conn:
        # 标记 canceled 并更新时间。

        conn.execute("UPDATE task SET status='canceled', updated_at=? WHERE task_id=?", (iso(), task_id))
    # 返回取消成功。

    return {"task_id": task_id, "status": "canceled", "canceled_at": iso()}


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
# 会话代表一次学习/测评/答辩过程，包含题目、作答、提示、追问和报告。

# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------
# 创建学习会话：绑定文档，选择模式、题量、难度、章节和题型。
# 201 表示新会话资源创建成功。

@app.post("/api/v1/sessions", status_code=201)
# payload 为 JSON；支持 Idempotency-Key 防止重复创建。

def create_session(payload: dict, idem: str | None = Header(None, alias="Idempotency-Key"), p: Principal = Depends(principal)):
    # 需要会话写权限。

    require(p, "sessions:write")
    # 查询幂等缓存。

    cached = _idem_lookup(idem, "POST /sessions")
    # 命中则直接返回旧会话。

    if cached:
        # 保持 201 状态。

        return JSONResponse(status_code=201, content=cached)
    # 交给 sessions 服务创建会话。

    session = sessions.create_session(
        # 文档 ID，必须存在且属于当前租户（服务层应校验）。

        doc_id=payload.get("document_id"),
        # 会话归属用户。

        user_id=p.user_id,
        # 租户隔离。

        tenant_id=p.tenant_id,
        # 模式：guided 导读 / exam 测评 / defense 答辩，默认 guided。

        mode=payload.get("mode", "guided"),
        # 题量：默认 12，强转 int；具体上下限由服务层限制。

        question_count=int(payload.get("question_count", 12)),
        # 难度：auto 或 easy/medium/hard。

        difficulty=payload.get("difficulty", "auto"),
        # 聚焦章节列表。

        focus_sections=payload.get("focus_sections"),
        # 排除节点列表（用户不想练的内容）。

        exclude_node_ids=payload.get("exclude_node_ids"),
        # 题型白名单。

        question_types=payload.get("question_types"),
    # 结束创建调用。

    )
    # 保存幂等结果。

    _idem_save(idem, "POST /sessions", session)
    # 返回会话对象。

    return session


# 会话列表：当前租户内、未删除文档关联的会话，普通用户仅自己的。

@app.get("/api/v1/sessions")
# 过滤参数：文档、状态、分页。

def list_sessions(
    # document_id 可选。

    document_id: str | None = None,
    # state 可选（active/finished 等）。

    state: str | None = None,
    # 每页数量。

    limit: int = 20,
    # 游标。

    cursor: str | None = None,
    # 当前用户。

    p: Principal = Depends(principal),
# 参数结束。

):
    # JOIN document 是为了排除已软删除文档的会话；租户条件先限定范围。

    sql = "SELECT s.* FROM session s JOIN document d ON d.doc_id=s.doc_id WHERE s.tenant_id=? AND d.deleted_at IS NULL"
    # 参数从租户开始。

    params: list = [p.tenant_id]
    # 普通用户再追加 user_id 条件。

    if p.role != "admin":
        # SQL 追加。

        sql += " AND s.user_id=?"
        # 记录用户参数。

        params.append(p.user_id)
    # 最近开始的会话优先。

    sql += " ORDER BY s.started_at DESC"
    # 执行查询。

    rows = fetch_all(sql, params)
    # 文档过滤在内存中完成。

    if document_id:
        # 精确匹配 doc_id。

        rows = [r for r in rows if r["doc_id"] == document_id]
    # 状态过滤。

    if state:
        # 精确匹配 state。

        rows = [r for r in rows if r["state"] == state]
    # 分页。

    page, next_cursor = _paginate(rows, limit, cursor)
    # 返回分页结构。

    return {
        # 逐条构造会话摘要。

        "items": [
            # 开始摘要对象。

            {
                # 会话 ID。

                "session_id": r["session_id"],
                # 文档 ID。

                "document_id": r["doc_id"],
                # 模式。

                "mode": r["mode"],
                # 状态。

                "state": r["state"],
                # 得分。

                "score": r["score"],
                # 总题数。

                "total_questions": r["total_questions"],
                # 创建时间（数据库字段叫 started_at，对外叫 created_at）。

                "created_at": r["started_at"],
            # 结束对象。

            }
            # 列表推导式结束。

            for r in page
        # 当前页。

        ],
        # 下一页游标。

        "next_cursor": next_cursor,
        # 是否还有更多。

        "has_more": bool(next_cursor),
    # 结束响应。

    }


# 会话详情：直接返回业务服务组装的完整对象。

@app.get("/api/v1/sessions/{session_id}")
# 参数：会话 ID 和当前用户。

def get_session(session_id: str, p: Principal = Depends(principal)):
    # _session_owned 内部校验租户和归属，失败抛 404。

    return _session_owned(session_id, p)


# 获取下一题：由会话状态机决定出题顺序、难度和题型。

@app.post("/api/v1/sessions/{session_id}/next-question")
# 路径参数 + 依赖注入。

def next_question(session_id: str, p: Principal = Depends(principal)):
    # 先校验会话归属。

    _session_owned(session_id, p)
    # 业务层生成/返回下一题；tenant_id 用于隔离。

    return sessions.next_question(session_id, p.tenant_id)


# 提交答案：保存作答、判分、更新会话进度。

@app.post("/api/v1/sessions/{session_id}/answers")
# payload 包含 turn_id、answer 等字段，结构由 sessions 模块解释。

def submit_answer(session_id: str, payload: dict, p: Principal = Depends(principal)):
    # 校验会话归属。

    _session_owned(session_id, p)
    # 交给业务层处理答案。

    return sessions.submit_answer(session_id, payload, p.tenant_id)


# 获取提示：按 turn（某一轮题目）和提示等级返回提示，避免一次性泄露答案。

@app.post("/api/v1/sessions/{session_id}/hint")
# payload 包含 turn_id 和 level。

def hint(session_id: str, payload: dict, p: Principal = Depends(principal)):
    # 校验会话归属。

    _session_owned(session_id, p)
    # level 默认 1，强转 int；具体提示内容由 sessions 生成。

    return sessions.get_hint(session_id, payload.get("turn_id"), int(payload.get("level") or 1), p.tenant_id)


# 创建追问：用户请求对某轮题目/答案进一步解释。

@app.post("/api/v1/sessions/{session_id}/follow-up")
# payload 包含 turn_id 和 kind。

def follow_up(session_id: str, payload: dict, p: Principal = Depends(principal)):
    # 校验会话归属。

    _session_owned(session_id, p)
    # kind 默认 clarify（澄清），也可以是其他追问类型。

    return sessions.create_follow_up(session_id, payload.get("turn_id"), payload.get("kind", "clarify"), p.tenant_id)


# 列出某会话的追问记录，可按 turn_id 过滤。

@app.get("/api/v1/sessions/{session_id}/follow-ups")
# turn_id 可选。

def list_follow_ups(session_id: str, turn_id: str | None = None, p: Principal = Depends(principal)):
    # 校验会话归属。

    _session_owned(session_id, p)
    # 统一包成 items 字段。

    return {"items": sessions.list_follow_ups(session_id, turn_id, p.tenant_id)}


# 回答追问：把用户对追问的回答写回会话。

@app.post("/api/v1/sessions/{session_id}/follow-up/answer")
# payload 包含 turn_id、kind、answer、follow_up_id。

def follow_up_answer(session_id: str, payload: dict, p: Principal = Depends(principal)):
    # 校验会话归属。

    _session_owned(session_id, p)
    # 回答追问；参数较多，分两行。

    return sessions.answer_follow_up(session_id, payload.get("turn_id"), payload.get("kind", "clarify"),
                                     # 继续传入回答文本、租户和追问 ID。

                                     payload.get("answer", ""), p.tenant_id, payload.get("follow_up_id"))


# 跳过当前题：记录跳过原因，可能不计分或稍后重试。

@app.post("/api/v1/sessions/{session_id}/skip")
# payload 可选，允许空请求体。

def skip(session_id: str, payload: dict | None = None, p: Principal = Depends(principal)):
    # 校验会话归属。

    _session_owned(session_id, p)
    # None 统一空字典。

    payload = payload or {}
    # reason 默认空字符串。

    return sessions.skip_question(session_id, payload.get("turn_id"), payload.get("reason", ""), p.tenant_id)


# 结束会话：生成报告/结算成绩，202 表示后台处理中。

@app.post("/api/v1/sessions/{session_id}/finish", status_code=202)
# payload 可选，reason 说明结束原因。

def finish(session_id: str, payload: dict | None = None, p: Principal = Depends(principal)):
    # 校验会话归属。

    _session_owned(session_id, p)
    # 默认 user_finished；payload 为 None 时也能工作。

    return sessions.finish_session(session_id, p.tenant_id, (payload or {}).get("reason", "user_finished"))


# 获取会话报告。

@app.get("/api/v1/sessions/{session_id}/report")
# 会话 ID + 当前用户。

def get_report(session_id: str, p: Principal = Depends(principal)):
    # 校验归属。

    _session_owned(session_id, p)
    # 返回报告数据。

    return sessions.get_report(session_id, p.tenant_id)


# 查看某一轮（seq_no）的逐题回顾。

@app.get("/api/v1/sessions/{session_id}/turns/{seq_no}")
# seq_no 是会话内的轮次序号，不是数据库主键。

def get_turn_review(session_id: str, seq_no: int, p: Principal = Depends(principal)):
    # 校验归属。

    _session_owned(session_id, p)
    # 业务层返回题目、答案、评分、反馈等。

    return sessions.get_turn_review(session_id, seq_no, p.tenant_id)


# 列出会话所有轮次。include_answer 当前未参与过滤，保留参数兼容。

@app.get("/api/v1/sessions/{session_id}/turns")
# include_answer 可选。

def get_turns(session_id: str, include_answer: bool = False, p: Principal = Depends(principal)):
    # 校验归属。

    _session_owned(session_id, p)
    # 统一返回 items + 空分页字段（当前不分页）。

    return {"items": sessions.list_turns(session_id, p.tenant_id), "next_cursor": None, "has_more": False}


# 删除会话：硬删除 turn/session，可选删除报告；204 无响应体。

@app.delete("/api/v1/sessions/{session_id}", status_code=204)
# purge_report=True 时连报告一起删。

def delete_session(session_id: str, purge_report: bool = False, p: Principal = Depends(principal)):
    # 校验会话归属。

    _session_owned(session_id, p)
    # 开事务。

    with db() as conn:
        # 先删轮次，避免外键约束阻止删除会话。

        conn.execute("DELETE FROM turn WHERE session_id=?", (session_id,))
        # 再删会话主记录。

        conn.execute("DELETE FROM session WHERE session_id=?", (session_id,))
        # 报告可选删除。

        if purge_report:
            # 删除报告记录。

            conn.execute("DELETE FROM report WHERE session_id=?", (session_id,))
    # 返回 204。

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# 题库与报告
# ---------------------------------------------------------------------------
# 题目支持生命周期：生成 -> 待复核 -> 活跃/退役；管理端可编辑、复核、组卷。

# ---------------------------------------------------------------------------
# 题库与报告
# ---------------------------------------------------------------------------
# 把 question 行转 API 结构；默认隐藏答案、评分标准、证据和提示，防止普通接口泄题。
# include_answer=True 时才返回敏感字段。

def _question_api(row: dict, include_answer: bool = False) -> dict:
    # 先组装公开字段。

    out = {
        # 题目 ID。

        "q_id": row["q_id"],
        # 所属文档。

        "document_id": row["doc_id"],
        # 所属节点。

        "node_id": row["node_id"],
        # 关联知识点。

        "kp_id": row["kp_id"],
        # 题型。

        "q_type": row["q_type"],
        # Bloom 认知层级。

        "bloom_level": row["bloom_level"],
        # 难度。

        "difficulty": row["difficulty"],
        # 题干。

        "stem": row["stem"],
        # 选项 JSON（选择题）。

        "options": load_json(row["options"], []),
        # 建议答题时限。

        "time_limit_sec": row["time_limit_sec"],
        # 提示等级数量/结构。

        "hint_levels": row["hint_levels"],
        # 题目状态。

        "status": row["status"],
        # 版本号，用于乐观并发控制。

        "version": row["version"],
        # 题目来源（人工/AI 等）。

        "source": row["source"],
    # 公开字段结束。

    }
    # 只有明确要求才附加答案相关内容。

    if include_answer:
        # 答案 JSON。

        out["answer"] = load_json(row["answer"], {})
        # 评分标准。

        out["rubric"] = load_json(row["rubric"], [])
        # 证据引用。

        out["evidence"] = load_json(row["evidence"], [])
        # 分级提示。

        out["hints"] = load_json(row["hints"], [])
    # 返回组装结果。

    return out


# 题目列表：管理员专用，支持多条件过滤。

@app.get("/api/v1/questions")
# 过滤参数：文档、节点、题型、Bloom、难度区间、状态、是否带答案、分页。

def list_questions(
    # document_id 可选。

    document_id: str | None = None,
    # node_id 可选。

    node_id: str | None = None,
    # q_type 可选。

    q_type: str | None = None,
    # bloom_level 可选。

    bloom_level: str | None = None,
    # difficulty_min 可选。

    difficulty_min: int | None = None,
    # difficulty_max 可选。

    difficulty_max: int | None = None,
    # 默认只列 active 题目。

    status: str = "active",
    # 默认不返回答案。

    include_answer: bool = False,
    # 每页数量。

    limit: int = 20,
    # 游标。

    cursor: str | None = None,
    # 当前用户。

    p: Principal = Depends(principal),
# 参数结束。

):
    # 题库管理仅管理员可用。

    require_admin(p)
    # 动态 SQL 的基础；1=1 方便后续统一追加 AND。

    sql = "SELECT * FROM question WHERE 1=1"
    # 参数列表。

    params: list = []
    # 按文档过滤。

    if document_id:
        # 追加占位符。

        sql += " AND doc_id=?"
        # 记录参数。

        params.append(document_id)
    # 按节点过滤。

    if node_id:
        # 追加条件。

        sql += " AND node_id=?"
        # 记录参数。

        params.append(node_id)
    # 按题型过滤。

    if q_type:
        # 追加条件。

        sql += " AND q_type=?"
        # 记录参数。

        params.append(q_type)
    # 按 Bloom 层级过滤。

    if bloom_level:
        # 追加条件。

        sql += " AND bloom_level=?"
        # 记录参数。

        params.append(bloom_level)
    # 难度下限，用 is not None 区分 0 和未传。

    if difficulty_min is not None:
        # 追加 >= 条件。

        sql += " AND difficulty>=?"
        # 强转 int 后再入库，减少类型问题。

        params.append(int(difficulty_min))
    # 难度上限。

    if difficulty_max is not None:
        # 追加 <= 条件。

        sql += " AND difficulty<=?"
        # 记录上限。

        params.append(int(difficulty_max))
    # 状态过滤（默认 active）。

    if status:
        # 追加状态条件。

        sql += " AND status=?"
        # 记录状态。

        params.append(status)
    # 最新题目优先。

    sql += " ORDER BY created_at DESC"
    # 执行查询。

    rows = fetch_all(sql, params)
    # 分页。

    page, next_cursor = _paginate(rows, limit, cursor)
    # 转 API 列表并返回分页信息。

    return {"items": [_question_api(r, include_answer) for r in page], "next_cursor": next_cursor, "has_more": bool(next_cursor)}


# 题目详情：默认包含答案，供管理员编辑/复核使用。

@app.get("/api/v1/questions/{q_id}")
# q_id + include_answer 默认 True。

def get_question(q_id: str, include_answer: bool = True, p: Principal = Depends(principal)):
    # 管理员权限。

    require_admin(p)
    # 查题目。

    row = fetch_one("SELECT * FROM question WHERE q_id=?", (q_id,))
    # 不存在。

    if not row:
        # 404。

        raise SessionError(404, "QUESTION_NOT_FOUND", "题目不存在")
    # 转 API 结构。

    return _question_api(row, include_answer)


# 编辑题目：白名单字段更新，版本号 +1，并回到 pending_review 等待复核。

@app.patch("/api/v1/questions/{q_id}")
# If-Match 请求头用于乐观锁：客户端应回传自己看到的版本号。

def patch_question(q_id: str, payload: dict, if_match: str | None = Header(None, alias="If-Match"),
                  # 当前用户参数延续到下一行。

                  p: Principal = Depends(principal)):
    # 管理员权限。

    require_admin(p)
    # 查当前题目。

    row = fetch_one("SELECT * FROM question WHERE q_id=?", (q_id,))
    # 不存在。

    if not row:
        # 404。

        raise SessionError(404, "QUESTION_NOT_FOUND", "题目不存在")
    # 客户端版本号与数据库不一致，说明别人已改过。

    if if_match and str(row["version"]) != str(if_match):
        # 409 冲突，提示刷新后重试。

        raise SessionError(409, "VERSION_CONFLICT", "题目版本已变化，请刷新后重试")
    # 收集要更新的列，值稍后放入参数。

    fields = {}
    # 只允许改题干和难度这两个直接字段。

    for k in ("stem", "difficulty"):
        # 请求里带了才更新。

        if k in payload:
            # 记录字段值。

            fields[k] = payload[k]
    # options 是 JSON 结构，需先 dump 成文本。

    if "options" in payload:
        # 序列化选项。

        fields["options"] = dump(payload["options"])
    # answer 同理。

    if "answer" in payload:
        # 序列化答案。

        fields["answer"] = dump(payload["answer"])
    # rubric 同理。

    if "rubric" in payload:
        # 序列化评分标准。

        fields["rubric"] = dump(payload["rubric"])
    # 任何编辑都让版本号 +1。

    fields["version"] = int(row["version"]) + 1
    # 编辑后必须重新复核，避免未审核内容直接进入练习。

    fields["status"] = "pending_review"
    # 开事务执行更新。

    with db() as conn:
        # 动态拼 SET 片段；字段名来自上面固定白名单，值仍参数化。

        sets = ", ".join(f"{k}=?" for k in fields)
        # 参数顺序：所有字段值，最后是 WHERE q_id。

        conn.execute(f"UPDATE question SET {sets} WHERE q_id=?", tuple(fields.values()) + (q_id,))
    # 返回新版本和待复核状态。

    return {"q_id": q_id, "version": fields["version"], "status": "pending_review"}


# 复核题目：approve 进入 active，retire 退役，其他决定保持 pending_review。

@app.post("/api/v1/questions/{q_id}/review")
# payload 包含 decision。

def review_question(q_id: str, payload: dict, p: Principal = Depends(principal)):
    # 管理员权限。

    require_admin(p)
    # 查题目。

    row = fetch_one("SELECT * FROM question WHERE q_id=?", (q_id,))
    # 不存在。

    if not row:
        # 404。

        raise SessionError(404, "QUESTION_NOT_FOUND", "题目不存在")
    # 默认 approve，兼容客户端不传 decision。

    decision = payload.get("decision", "approve")
    # 三元表达式链：approve -> active；retire -> retired；其他 -> pending_review。

    status = "active" if decision == "approve" else "retired" if decision == "retire" else "pending_review"
    # 开事务。

    with db() as conn:
        # 更新状态。

        conn.execute("UPDATE question SET status=? WHERE q_id=?", (status, q_id))
    # 返回复核结果和时间。

    return {"q_id": q_id, "status": status, "reviewed_at": iso()}


# 题卷列表：管理员查看已生成的题卷。

@app.get("/api/v1/question-sets")
# 可按文档过滤。

def list_question_sets(document_id: str | None = None, p: Principal = Depends(principal)):
    # 管理员权限。

    require_admin(p)
    # 基础查询。

    sql = "SELECT * FROM question_set"
    # 参数列表。

    params = []
    # 有文档过滤时加 WHERE。

    if document_id:
        # 追加参数。

        sql += " WHERE doc_id=?"
        # 记录文档 ID。

        params.append(document_id)
    # 最新题卷优先，最多 100 条，防止一次性返回过多。

    sql += " ORDER BY created_at DESC LIMIT 100"
    # 执行查询。

    rows = fetch_all(sql, params)
    # 把 question_ids 的 JSON 文本还原成列表。

    for row in rows:
        # 逐行处理；load_json 对损坏数据回退空列表。

        row["question_ids"] = load_json(row.get("question_ids"), [])
    # 返回 items。

    return {"items": rows}


# 创建题卷：按文档、难度、数量随机抽题；201 表示创建成功。

@app.post("/api/v1/question-sets", status_code=201)
# payload 包含 document_id/difficulty/count/title。

def create_question_set(payload: dict, p: Principal = Depends(principal)):
    # 管理员权限。

    require_admin(p)
    # 文档 ID 转字符串。

    doc_id = str(payload.get("document_id") or "")
    # 校验文档存在且未删除；这里没有额外租户条件，管理员权限下保持原逻辑。

    doc = fetch_one("SELECT * FROM document WHERE doc_id=? AND deleted_at IS NULL", (doc_id,))
    # 文档不存在。

    if not doc:
        # 404。

        raise SessionError(404, "DOC_NOT_FOUND", "文档不存在")
    # 难度，默认 mixed。

    difficulty = str(payload.get("difficulty") or "mixed")
    # 难度到数值区间的映射：easy 1-2，medium 3，hard 4-5，mixed 1-5。

    ranges = {"easy": (1, 2), "medium": (3, 3), "hard": (4, 5), "mixed": (1, 5)}
    # 未知难度回退 1-5，避免 KeyError。

    lo, hi = ranges.get(difficulty, (1, 5))
    # 题量限制 1~30，默认 12。

    limit = max(1, min(30, int(payload.get("count") or 12)))
    # 随机抽题：只抽 active，且难度在区间内。

    rows = fetch_all(
        # SQL 条件与占位符。

        "SELECT * FROM question WHERE doc_id=? AND status='active' AND difficulty BETWEEN ? AND ?"
        # RANDOM() 随机排序；LIMIT 控制数量。SQLite/PostgreSQL 都支持 RANDOM()。

        " ORDER BY RANDOM() LIMIT ?", (doc_id, lo, hi, limit),
    # 结束查询。

    )
    # 没有可用题目。

    if not rows:
        # 409 冲突，提示先完成会话生成题目或复核现有题目。

        raise SessionError(409, "QUESTION_SET_EMPTY", "该难度暂无可用题目，请先完成几轮会话或复核题目")
    # 标题默认由文档标题 + 难度组成。

    title = str(payload.get("title") or f"{doc['title']} · {difficulty} 题卷")
    # 题卷 ID，前缀 qset。

    set_id = new_id("qset")
    # 抽取出的题目 ID 列表。

    ids = [r["q_id"] for r in rows]
    # 开事务写题卷。

    with db() as conn:
        # 插入题卷记录。

        conn.execute(
            # 字段列表：set_id、doc_id、tenant_id、title、difficulty、question_ids、question_count。

            "INSERT INTO question_set (set_id, doc_id, tenant_id, title, difficulty, question_ids, question_count,"
            # 继续字段 created_by、created_at；共 9 个占位符。

            " created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            # 参数与占位符一一对应；question_ids 序列化为 JSON 文本。

            (set_id, doc_id, p.tenant_id, title, difficulty, dump(ids), len(ids), p.user_id, iso()),
        # 结束插入。

        )
    # 返回题卷信息。

    return {"set_id": set_id, "title": title, "difficulty": difficulty, "question_count": len(ids),
            # requested 是用户请求数量，可能大于实际可用数量。

            "requested": limit, "question_ids": ids,
            # 实际数量不足时给 warning；足够则为 None。

            "warning": None if len(ids) >= limit else f"可用题目不足，已生成 {len(ids)} 题"}
# 结束创建。



@app.delete("/api/v1/question-sets/{set_id}", status_code=204)
# 删除题卷：只删题卷记录，不删除题目本身；204 无响应体。

def delete_question_set(set_id: str, p: Principal = Depends(principal)):
    # 管理员权限。

    require_admin(p)
    # 开事务。

    with db() as conn:
        # 按 set_id 删除。

        conn.execute("DELETE FROM question_set WHERE set_id=?", (set_id,))
    # 返回 204。

    return Response(status_code=204)


# 报告归属校验：普通用户只能看自己会话生成的报告，管理员可看全部。

def _report_owned(report_id: str, p: Principal) -> dict:
    # 按 report_id 查报告。

    row = fetch_one("SELECT * FROM report WHERE report_id=?", (report_id,))
    # 报告不存在。

    if not row:
        # 404。

        raise SessionError(404, "REPORT_NOT_FOUND", "报告不存在")
    # 管理员跳过归属检查。

    if p.role != "admin":
        # 普通用户：通过报告关联的 session 找到 user_id。

        session = fetch_one("SELECT user_id FROM session WHERE session_id=?", (row["session_id"],))
        # 会话不存在或不是自己的，都按“报告不存在”处理，避免泄露信息。

        if not session or session.get("user_id") != p.user_id:
            # 统一 404。

            raise SessionError(404, "REPORT_NOT_FOUND", "报告不存在")
    # 返回报告行。

    return row


# 按 ID 获取报告。

@app.get("/api/v1/reports/{report_id}")
# 参数：report_id + 当前用户。

def get_report_by_id(report_id: str, p: Principal = Depends(principal)):
    # 先校验归属。

    row = _report_owned(report_id, p)
    # 复用 sessions 模块的报告序列化函数。

    return sessions._report_api(row)


# 导出报告：生成 Markdown 文件，返回下载地址和过期时间。
# 注意 format 参数当前只支持 md，代码固定后缀 .md；保留参数用于兼容旧客户端。

@app.get("/api/v1/reports/{report_id}/export")
# format 查询参数默认 md。

def export_report(report_id: str, format: str = "md", p: Principal = Depends(principal)):
    # 校验报告归属。

    row = _report_owned(report_id, p)
    # 组装报告数据。

    report = sessions._report_api(row)
    # 转成 Markdown 文本。

    text = _report_markdown(report)
    # 固定 .md 后缀。

    suffix = ".md"
    # 导出文件路径：同一报告重复导出会覆盖。

    path = EXPORT_DIR / f"{report_id}{suffix}"
    # 写 UTF-8 文件，保证中文正常。

    path.write_text(text, encoding="utf-8")
    # 返回下载信息。

    return {
        # 下载接口地址（需要再次鉴权）。

        "download_url": f"/api/v1/reports/{report_id}/download",
        # 过期时间：当前时间 + 1 小时；这里只是返回给前端的提示，文件本身没有定时删除。

        "expires_at": iso(now() + timedelta(hours=1)),
        # 文件实际字节数。

        "size_bytes": path.stat().st_size,
        # 实际格式固定 md。

        "format": "md",
    # 结束。

    }


# 下载报告文件：再次做归属校验，防止直接猜下载 URL。

@app.get("/api/v1/reports/{report_id}/download")
# 参数：报告 ID + 当前用户。

def download_report(report_id: str, p: Principal = Depends(principal)):
    # 不走缓存，每次重新校验权限。

    _report_owned(report_id, p)
    # 导出文件路径。

    path = EXPORT_DIR / f"{report_id}.md"
    # 文件还没导出或已被清理。

    if not path.exists():
        # 404。

        raise SessionError(404, "REPORT_NOT_FOUND", "报告文件不存在")
    # FileResponse 返回 Markdown，指定 charset，浏览器可正确显示中文。

    return FileResponse(path, media_type="text/markdown; charset=utf-8", filename=f"{report_id}.md")


# 把报告字典渲染为 Markdown 字符串。纯字符串拼接，无模板引擎依赖。

def _report_markdown(report: dict) -> str:
    # 汇总信息子字典。

    s = report["summary"]
    # 逐行构建 Markdown 列表。

    lines = [
        # 一级标题。

        f"# A论 · 学习报告",
        # 空行分隔。

        "",
        # 总分。

        f"- 总分：{s.get('score')} / 100",
        # 题量与已答数量。

        f"- 题量：{s.get('total')}（已答 {s.get('answered', 0)}）",
        # 正确题数。

        f"- 正确题数：{s.get('correct', 0)}",
        # 用时（秒）。

        f"- 用时：{s.get('duration_sec', 0)} 秒",
        # 能力估计值。

        f"- 能力估计：{s.get('ability', 500)}",
        # 空行。

        "",
        # 章节掌握度二级标题。

        "## 章节掌握度",
        # 空行。

        "",
    # 列表初始化结束。

    ]
    # 逐章节输出掌握度。

    for item in report["section_mastery"]:
        # score 是 0~1 小数，乘 100 转百分比并取整。

        lines.append(f"- {item['title']}：{int(item['score']*100)}%（{item['question_count']} 题）")
    # 追加错题清单标题。

    lines += ["", "## 错题清单", ""]
    # 逐道错题输出题干、页码和用户答案。

    for w in report["wrong_questions"]:
        # 页码可能缺失，用 get 给默认 None。

        lines.append(f"- {w['stem']}（第 {w.get('page')} 页）：{w.get('user_answer')}")
    # 追加改进建议标题。

    lines += ["", "## 改进建议", ""]
    # 逐条建议输出有序列表。

    for r in report["recommendations"]:
        # 统一用 "1." 前缀（Markdown 渲染器会自动编号）。

        lines.append(f"1. {r}")
    # 用换行拼接成最终 Markdown。

    return "\n".join(lines)


# 用量统计：管理员查看请求数、token、费用。当前实现只返回一个聚合行，且固定按当前日期。

@app.get("/api/v1/usage")
# from 参数用别名 from_ 接收，因为 from 是 Python 关键字；to/group_by 当前未参与计算。

def usage(from_: str | None = Query(None, alias="from"), to: str | None = None, group_by: str = "day",
          # 当前用户参数延续。

          p: Principal = Depends(principal)):
    # 管理员权限。

    require_admin(p)
    # 取最近 5000 条用量日志；数据量大时应做时间范围过滤和数据库聚合。

    rows = fetch_all("SELECT * FROM usage_log ORDER BY ts DESC LIMIT 5000")
    # 总请求数。

    total_requests = len(rows)
    # 总费用（credits），四舍五入到 2 位。

    total_credits = round(sum(float(r["credits"] or 0) for r in rows), 2)
    # 返回统计结构。

    return {
        # items 当前只有一行，日期取服务器当前日期。

        "items": [
            # 文档总数单独 COUNT；这里没有租户过滤，是全局统计。

            {"date": iso()[:10], "requests": total_requests, "documents": fetch_one("SELECT COUNT(*) AS c FROM document")["c"],
             # 输入 token 总数。

             "input_tokens": sum(int(r["prompt_tokens"] or 0) for r in rows),
             # 输出 token 总数。

             "output_tokens": sum(int(r["completion_tokens"] or 0) for r in rows),
             # 费用。

             "cost_cny": total_credits}
        # items 结束。

        ],
        # 总计。

        "total": {"requests": total_requests, "cost_cny": total_credits},
    # 响应结束。

    }


# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------
# 如果前端已构建，则由 FastAPI 同时托管静态资源和 SPA 页面，简化部署。

# ---------------------------------------------------------------------------
# 静态前端
# ---------------------------------------------------------------------------
# 只有前端构建目录存在时才挂载，避免开发环境启动报错。

if WEB_DIR.exists():
    # /assets 下的请求直接由 StaticFiles 返回；name 是路由内部标识。

    app.mount("/assets", StaticFiles(directory=str(WEB_DIR)), name="assets")


# 首页：返回前端入口 index.html。

@app.get("/")
# 同步路由。

def index():
    # 入口文件路径。

    path = WEB_DIR / "index.html"
    # 前端尚未构建时，返回一个最小 JSON 而不是 500。

    if not path.exists():
        # 提示信息。

        return JSONResponse({"name": APP_NAME, "message": "前端尚未构建"})
    # 返回 HTML，指定 charset 避免中文乱码。

    return FileResponse(path, media_type="text/html; charset=utf-8")


# 教师端页面路由，同时兼容带/不带末尾斜杠。

@app.get("/teacher")
# 第二个装饰器把 /teacher/ 也映射到同一函数。

@app.get("/teacher/")
# 返回 admin.html（教师端复用管理页面）。

def teacher_page():
    # 这里没有检查文件是否存在；文件缺失时 FileResponse 会报错，保持原逻辑。

    return FileResponse(WEB_DIR / "admin.html")


# 管理端页面路由，同时兼容 /admin 和 /admin/。

@app.get("/admin")
# 末尾斜杠别名。

@app.get("/admin/")
# 同步路由。

def admin_page():
    # 管理页面路径。

    path = WEB_DIR / "admin.html"
    # 文件不存在时给友好提示。

    if not path.exists():
        # 返回 JSON 提示。

        return JSONResponse({"name": APP_NAME, "message": "管理端尚未构建"})
    # 返回管理页面 HTML。

    return FileResponse(path, media_type="text/html; charset=utf-8")


# SPA 兜底路由：所有未被前面路由匹配的路径都会到这里。
# 必须放在最后注册，否则会抢先匹配 /api 等更具体的路由。

@app.get("/{path:path}")
# path 是通配路径参数，例如 "documents/123"。

def spa_fallback(path: str):
    # API 路径不应回退到 index.html，否则前端会收到 HTML 而不是 404 JSON。

    if path.startswith("api/"):
        # 返回 404 统一错误结构。

        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "接口不存在"})
    # 把 URL 路径映射到前端目录下的文件。

    target = WEB_DIR / path
    # 如果目标文件真实存在，直接返回（图片、字体、favicon 等）。

    if target.exists() and target.is_file():
        # FileResponse 会自动猜 Content-Type。

        return FileResponse(target)
    # 否则回退到 SPA 入口。

    index_path = WEB_DIR / "index.html"
    # 只有“看起来像前端路由”（路径中没有点）时才回退 index.html；
    # 带点的路径通常表示静态资源，缺失时应返回 404 而不是 HTML。

    if index_path.exists() and "." not in path:
        # 返回 index.html，交给前端路由处理。

        return FileResponse(index_path, media_type="text/html; charset=utf-8")
    # 其他情况返回 404 资源不存在。
    raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "资源不存在"})
