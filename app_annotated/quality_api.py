# =============================================================================
# 文件：app/quality_api.py
# 项目：A论 · 论文导读智能体（上传论文 → 解析结构 → 生成导读/题目 → 判分）
#
# 一、这个文件负责什么
#   本文件是「质量看板」的 HTTP 路由层（Controller），本身不实现评测算法，只负责：
#     1. 校验调用者是不是管理员；
#     2. 校验并透传参数；
#     3. 调用 alun.quality 下的评测引擎；
#     4. 把结果整理成前端需要的 JSON，或把 Markdown 报告作为附件返回。
#   它覆盖四类管理动作：
#     - 评测运行（quality_run / quality_case）：总览、列表、详情、报告、发起后台评测；
#     - 黄金集（golden set）：浏览某个版本的标注样本，并给出数据校验问题与统计；
#     - 缺陷台账（quality_defect）：按状态/等级筛选，手工流转缺陷状态；
#     - 人工复核（quality_review）：录入判分一致性 / 题目可答性的人工评分。
#
# 二、在应用中的位置（主要调用关系）
#   app/main.py
#     └─ app.include_router(quality_api.router)       # 应用启动时挂载本路由
#   web/admin.js（管理后台「质量看板」页签）
#     ├─ GET   /api/v1/admin/quality/overview         # 首屏聚合数据
#     ├─ GET   /api/v1/admin/quality/runs?limit=8     # 最近运行列表
#     ├─ POST  /api/v1/admin/quality/runs             # 点击「开始评测」
#     ├─ GET   /api/v1/admin/quality/runs/{id}        # 轮询运行状态/用例明细
#     ├─ GET   /api/v1/admin/quality/runs/{id}/report # 下载 Markdown 报告
#     └─ GET   /api/v1/admin/quality/golden、/defects、/reviews、/thresholds
#   下层模块：
#     alun/quality/golden.py   读取 alun/quality/goldens/<version>/ 下的 JSON 样本；
#     alun/quality/runner.py   真正执行解析/生成/判分/时延四类评测，写库并起后台线程；
#     alun/quality/report.py   把一次运行渲染成 Markdown 验收报告；
#     alun/quality/metrics.py  指标计算与阈值常量（本文件 /thresholds 直接引用）。
#   数据层：app/db.py（默认 SQLite，平台模式 PostgreSQL）中的
#     quality_run / quality_case / quality_defect / quality_review 四张表。
#
# 三、权限与错误码
#   所有接口都通过 Depends(quality_admin) 强制管理员权限：
#     - strict 模式缺少/无效 Bearer Token → 401 UNAUTHORIZED（security 模块抛出）；
#     - 已登录但 role != "admin"          → 403 FORBIDDEN；
#     - demo 模式未带令牌时回退为演示用户，但演示用户不是 admin，仍会被 403 拦住。
#   业务错误统一用 HTTPException(detail={"code": ..., "message": ...})：
#     RUN_NOT_FOUND    —— 404（运行记录不存在）；
#     GOLDEN_NOT_FOUND —— GET /golden 返回 404，POST /runs 预检失败返回 422；
#     INVALID_SUITE / INVALID_STATUS / INVALID_TARGET / INVALID_SCORE —— 422；
#     成功创建资源用 201，后台任务受理用 202。
#
# 四、阅读提示
#   - 路由函数都用同步 def：FastAPI 会把它们放进线程池执行，避免阻塞事件循环；
#   - 所有 SQL 都通过 ? 占位符传参（app/db.py 会在 PostgreSQL 下转换成 %s），
#     不要把用户输入拼进 SQL 文本，否则会有 SQL 注入风险；
#   - 本文件只做「薄」路由：参数校验 + 调用引擎 + 组装响应，复杂逻辑都在 runner.py；
#   - 多行调用/字典字面量内部的注释放在参数行上方或整块上方，不插进字符串字面量，
#     这样既方便阅读，又不会破坏 Python 语法。
# =============================================================================
# ↓ 源文件第 1 行：模块级 docstring（三引号字符串）。
#   注释可以写在它前面，但它必须是文件中的第一条“语句”，否则就不再是模块文档字符串。
"""质量看板 API：评测运行、黄金集、缺陷台账与人工复核。"""
# ↑ 原样保留源文件的模块说明；运行时可通过 quality_api.__doc__ 读到。
# -----------------------------------------------------------------------------
# 导入区：先标准库/框架，再项目内部模块
# -----------------------------------------------------------------------------
# 启用“延迟注解”（PEP 563）：函数签名中的 dict | None、list 等注解在运行时不会被求值，
# 而是保存成字符串。这样即使运行在较旧的 Python 版本上，这类写法也不会在导入时报错。
# 该语句必须位于模块 docstring 之后、其它所有可执行语句之前。
from __future__ import annotations

# FastAPI 核心组件（逐个说明）：
#   APIRouter     —— 路由分组容器，最后在 main.py 中挂到主应用上；
#   Depends       —— 声明依赖注入：FastAPI 会先调用被依赖的函数，再把返回值传给参数；
#   HTTPException —— 抛出带 HTTP 状态码和结构化 detail 的错误响应；
#   Query         —— 用于声明查询参数的约束；本文件目前没有使用它（limit 等直接写成普通参数），
#                    属于保留导入，不影响运行，阅读时不必寻找它的调用点；
#   Request       —— 当前 HTTP 请求对象，用来读取认证相关的请求头。
from fastapi import APIRouter, Depends, HTTPException, Query, Request
# Response：返回非 JSON 的原始响应。这里用它输出 Markdown 文本，并设置下载响应头。
from fastapi.responses import Response

# 项目数据层助手（来自 app/db.py）：
#   db         —— @contextmanager 包装的数据库连接：正常退出 commit，异常 rollback，最后 close；
#   dump       —— json.dumps(..., ensure_ascii=False)，把 dict/list 存成 JSON 文本；
#   fetch_all  —— 执行查询并返回 list[dict]（每行都已转成普通字典）；
#   fetch_one  —— 执行查询并返回第一行 dict，没有则 None（本文件目前未直接使用，保留导入）；
#   iso        —— 生成带 +08:00 时区、精确到秒的 ISO 时间字符串，统一写库的时间格式；
#   load_json  —— 安全地把数据库里的 JSON 文本还原成对象，解析失败时返回给定的默认值；
#   new_id     —— 生成“前缀_10位十六进制”的短主键，例如 qrev_1a2b3c4d5e。
from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id
# 身份与权限（来自 app/security.py）：
#   Principal              —— 当前调用者的身份数据类（user_id/tenant_id/role/username/scopes）；
#   principal_from_request —— 从 Authorization Bearer 令牌（或 demo 模式的 X-User-Id 头）解析身份；
#   require_admin          —— 角色不是 admin 时抛 403 FORBIDDEN。
from .security import Principal, principal_from_request, require_admin
# 黄金集模块：读取 alun/quality/goldens/<version>/ 下的 index.json 与样本 JSON，
# 并提供结构校验 validate_case() 与统计 stats()。
from alun.quality import golden as golden_mod
# 评测运行器：创建/查询运行记录、后台线程执行四类评测、同步缺陷台账。
from alun.quality import runner as quality_runner
# 指标模块：本文件只用到它的阈值常量（TITLE_F1_TARGET 等），用于 /thresholds 接口。
from alun.quality import metrics as quality_metrics
# 报告模块：把一次运行（run dict）渲染成 Markdown 文本，供 /runs/{id}/report 下载。
from alun.quality import report as quality_report

# 创建本文件的路由器：
#   prefix="/api/v1/admin/quality" —— 下面所有路径都会自动加上该前缀；
#   tags=["quality"]               —— 在 OpenAPI/Swagger 文档中归入 quality 分组。
# 该 router 在 app/main.py 中通过 app.include_router(quality_api.router) 挂载。
router = APIRouter(prefix="/api/v1/admin/quality", tags=["quality"])


# =============================================================================
# 权限依赖：本文件所有接口共用
# =============================================================================
# 这是一个 FastAPI 依赖函数（dependency）：
#   - 参数 request 由 FastAPI 自动注入，客户端不需要也不应该自己传；
#   - 返回的 Principal 会被注入到路由函数的 p 参数；
#   - 这里一旦抛异常（401/403），路由函数根本不会执行，天然形成统一鉴权。
def quality_admin(request: Request) -> Principal:
    # 第一步：解析身份。strict 模式下没有令牌会抛 401；demo 模式下回退为演示用户。
    p = principal_from_request(request)
    # 第二步：检查角色。非 admin 立即抛 403 FORBIDDEN，后面的业务代码不会执行。
    require_admin(p)
    # 第三步：把身份返回给路由函数，便于记录“谁发起了评测 / 谁录入了复核”。
    return p


# =============================================================================
# 接口 1：GET /api/v1/admin/quality/overview
# 作用：质量看板首屏聚合数据（最近一次运行、黄金集清单、缺陷台账、阈值）。
# 返回：{latest, golden: {...}, defects: [...], open_defects: [...], thresholds: {...}}
# =============================================================================
# 注册 GET 路由；完整路径 = 路由器前缀 + "/overview"。
@router.get("/overview")
# p 参数：声明 Depends(quality_admin) 就会触发上面的鉴权。
# 函数体里没用到 p，但绝不能删——FastAPI 只执行声明过的依赖，删了就失去管理员保护。
def overview(p: Principal = Depends(quality_admin)):
    # 调用运行器的聚合函数：内部会查最近一次运行、黄金集清单、全部缺陷和阈值。
    # 注意：runner.overview() 目前固定读取 golden-v1（该接口不接收 version 参数）。
    data = quality_runner.overview()
    # latest 可能是 None（从未跑过评测），也可能是 list_runs 返回的“摘要行”，
    # 其中 metrics / summary 已从 JSON 文本解析成 dict。
    latest = data.get("latest")
    # 只有确实存在运行记录、并且带有 run_id 时，才需要二次查询完整详情。
    if latest and latest.get("run_id"):
        # 按 run_id 重新读取：include_cases=False 表示不带逐条用例（首屏不需要，减少数据量）；
        # 返回的 run 里还包含该次运行关联的 defects 列表。
        latest = quality_runner.get_run(latest["run_id"], include_cases=False)
        # 用完整对象覆盖摘要对象，前端因此能拿到 finished_at、defects 等字段。
        data["latest"] = latest
    # 原样返回聚合结果；FastAPI 会自动把它序列化成 JSON 响应。
    return data


# =============================================================================
# 接口 2：GET /api/v1/admin/quality/runs
# 作用：按创建时间倒序返回最近的评测运行列表（看板上的历史表格）。
# 查询参数 limit：期望返回条数，默认 20；真正范围由 runner.list_runs 再夹紧。
# 返回：{"items": [...]}，每项含 metrics / summary（已解析为对象）。
# =============================================================================
# 注册 GET 路由；limit 是查询参数（?limit=8），FastAPI 会把它转成 int。
@router.get("/runs")
# limit: int = 20 —— 客户端不传时使用默认值 20；传入非整数时 FastAPI 自动返回 422。
def runs(limit: int = 20, p: Principal = Depends(quality_admin)):
    # 调用运行器查询列表：内部会执行 SELECT ... ORDER BY created_at DESC LIMIT ?，
    # 并把 limit 夹紧到 1—100，避免一次拉取过多数据。
    return {"items": quality_runner.list_runs(limit)}


# =============================================================================
# 接口 3：GET /api/v1/admin/quality/runs/{run_id}
# 作用：查看单次运行的完整详情，包括逐条用例（quality_case）和缺陷。
# 路径参数 run_id：形如 qrun_1a2b3c4d5e 的运行主键。
# 返回：运行 dict；不存在时 404 RUN_NOT_FOUND。
# =============================================================================
# 注册 GET 路由；{run_id} 是路径参数，FastAPI 会从 URL 中提取并传给函数。
@router.get("/runs/{run_id}")
# p 仍然只用于鉴权；run_id 才是业务参数。
def run_detail(run_id: str, p: Principal = Depends(quality_admin)):
    # try 包住查询：get_run 在找不到记录时会主动 raise KeyError(run_id)。
    try:
        # include_cases=True 表示要带用例明细；case_limit=800 是本次最多返回的用例条数，
        # 防止一次运行用例过多导致响应过大（runner 内部还会再做 1—2000 的夹紧）。
        return quality_runner.get_run(run_id, include_cases=True, case_limit=800)
    # 只捕获“运行不存在”的 KeyError；数据库连接失败等其它异常仍会向上冒泡成 500。
    except KeyError:
        # 转成对前端友好的 404，detail 用 code + message 便于前端分支处理与提示。
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "评测运行不存在"})


# =============================================================================
# 接口 4：GET /api/v1/admin/quality/runs/{run_id}/report
# 作用：把一次运行渲染成 Markdown 验收报告，并以附件形式下载。
# 返回：Content-Type: text/markdown; charset=utf-8，
#       Content-Disposition: attachment; filename="{run_id}.md"。
# =============================================================================
# 注册 GET 路由；路径比详情接口多一段 /report。
@router.get("/runs/{run_id}/report")
# 参数与 run_detail 相同：run_id 指定运行，p 负责鉴权。
def run_report(run_id: str, p: Principal = Depends(quality_admin)):
    # 先读取完整运行数据（最多 2000 条用例），报告需要这些明细来做统计与列举。
    try:
        # include_cases=True：报告要展示逐条用例结果；case_limit=2000 是上限。
        run = quality_runner.get_run(run_id, include_cases=True, case_limit=2000)
    # 运行不存在时 get_run 抛 KeyError，这里统一转 404。
    except KeyError:
        # 与详情接口保持同样的错误码，前端可以用同一套逻辑处理。
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND", "message": "评测运行不存在"})
    # 调用报告模块，把 run dict 渲染成 Markdown 文本（纯字符串）。
    text = quality_report.render_markdown(run)
    # 构造原始文本响应：
    #   content      —— 响应正文，即 Markdown 字符串；
    #   media_type   —— 浏览器据此识别为 Markdown（UTF-8，避免中文乱码）；
    #   headers      —— attachment 让浏览器弹出“下载”而不是直接打开，文件名用 run_id.md。
    return Response(
        content=text,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{run_id}.md"'},
    )


# =============================================================================
# 接口 5：POST /api/v1/admin/quality/runs
# 作用：发起一次质量评测。评测可能跑几十秒到几分钟，因此立即返回 202，
#       真正的执行放到后台线程（runner.run_background），前端之后轮询详情接口。
# 请求体（可选 JSON）：{"suite": "all|parse|generation|grading|latency",
#                       "version": "golden-v1"}
# 返回：新建的 quality_run 记录（status 通常为 queued）。
# =============================================================================
# 注册 POST 路由；status_code=202 表示“已受理、尚未完成”，是异步任务的标准语义。
@router.post("/runs", status_code=202)
# payload: dict | None = None —— 请求体可以省略；省略时为 None。
# 注意：这里没有用 Pydantic 模型校验，而是在函数体里手工校验，灵活性更高但约束较弱。
def start_run(payload: dict | None = None, p: Principal = Depends(quality_admin)):
    # 兼容“没有请求体”和“传了空对象”两种情况，统一变成空字典，后续 .get 才不会报错。
    payload = payload or {}
    # 读取 suite 并转成字符串；缺省或空字符串时使用 "all"（跑全部四类评测）。
    suite = str(payload.get("suite") or "all")
    # 白名单校验：只允许 all / parse / generation / grading / latency 五种取值，
    # 防止把任意字符串传给 runner 后出现难以排查的行为。
    if suite not in ("all", "parse", "generation", "grading", "latency"):
        # 422 Unprocessable Entity：参数语义不合法；code 供前端识别，message 给用户看。
        raise HTTPException(status_code=422, detail={"code": "INVALID_SUITE", "message": "suite 不合法"})
    # 读取黄金集版本，缺省为 golden-v1；版本号最终对应 goldens 目录下的一个子目录名。
    version = str(payload.get("version") or "golden-v1")
    # 在创建后台任务前先加载一次黄金集，尽早发现“版本不存在/JSON 损坏”等问题。
    try:
        # load_version 会读取 index.json 以及其中列出的所有样本文件。
        golden_mod.load_version(version)
    # GoldenError（ValueError 子类）、文件 IO 错误、JSON 解析错误都在这里统一兜住。
    except Exception as exc:
        # 转成 422 而不是 500：这是调用方给了不存在的版本，属于请求参数问题。
        raise HTTPException(status_code=422, detail={"code": "GOLDEN_NOT_FOUND", "message": str(exc)})
    # 创建运行记录并启动后台线程：
    #   run_background 内部先 INSERT quality_run（status=queued），再 threading.Thread(...).start()；
    #   返回的是运行记录（不含用例明细），前端拿到 run_id 后可轮询 /runs/{run_id}；
    #   created_by 记录操作者：优先用可读的 username，没有则退回 user_id。
    # 注意：后台线程是 daemon 线程，进程退出时会被强制结束、任务不会自动续跑；
    #       生产环境应保证服务常驻，或改用外部队列/任务系统来获得重试与持久化能力。
    return quality_runner.run_background(version, suite, p.username or p.user_id)


# =============================================================================
# 接口 6：GET /api/v1/admin/quality/golden
# 作用：浏览某个黄金集版本的全部标注样本，并顺带做结构校验和统计。
# 查询参数 version：默认 golden-v1。
# 返回：{version, index, items: [...], total, headings}
#   index   —— index.json 的原始内容（含 documents 清单等）；
#   items   —— 每个样本一条摘要：基本信息 + 校验问题列表 + 统计；
#   total   —— 样本数量；
#   headings—— 所有样本的标题总数（用于看板展示标注规模）。
# =============================================================================
# 注册 GET 路由；version 是查询参数，默认 golden-v1。
@router.get("/golden")
# p 负责鉴权；version 指定黄金集目录名。
def golden(version: str = "golden-v1", p: Principal = Depends(quality_admin)):
    # 加载指定版本：返回 {"version": ..., "index": ..., "cases": [case, ...]}。
    try:
        # 内部会逐个读取样本 JSON，并补齐 doc_type、case_id 等缺省字段。
        bundle = golden_mod.load_version(version)
    # 版本目录不存在、index.json 缺失或样本 JSON 损坏都会抛异常，这里统一转 404。
    except Exception as exc:
        # 404 比 500 更准确：请求的黄金集版本在这个部署里不存在/不可读。
        raise HTTPException(status_code=404, detail={"code": "GOLDEN_NOT_FOUND", "message": str(exc)})
    # 预先准备结果列表；下面把每个样本整理成前端易用的扁平结构。
    items = []
    # 遍历该版本下的所有样本（每个 case 是一个 dict，至少含 doc_id/title/headings）。
    for case in bundle["cases"]:
        # 追加一条摘要。字典字面量跨多行，注释写在每个键上方，避免破坏语法。
        items.append({
            # 文档主键，黄金集与真实文档通过它关联。
            "doc_id": case["doc_id"],
            # 文档标题；可能为空，所以用 .get 而不是下标。
            "title": case.get("title"),
            # 文档类型（paper/report/...），用于分类统计。
            "doc_type": case.get("doc_type"),
            # 页数，可能为 None（例如内联标注没有页数）。
            "page_count": case.get("page_count"),
            # 标题数量：先取 headings，若为 None 则用空列表兜底，再取长度。
            "headings": len(case.get("headings") or []),
            # 标注元信息（标注人、方法、时间等）；没有时返回空字典而不是 None。
            "annotation": case.get("annotation") or {},
            # 结构校验结果：返回问题描述字符串列表，空列表表示通过校验。
            "problems": golden_mod.validate_case(case),
            # 统计信息：标题总数、各层级数量、页数、文档类型、标注方法等。
            "stats": golden_mod.stats(case),
        })
    # 组装最终响应：
    #   version —— 实际加载的版本号；
    #   index   —— 原始 index.json，含 documents 数组等清单信息；
    #   items   —— 上面整理的样本摘要；
    #   total   —— 样本条数；
    #   headings—— 对每个 case 的 headings 取长度后求和（None 用空列表兜底）。
    return {
        "version": bundle["version"],
        "index": bundle["index"],
        "items": items,
        "total": len(items),
        "headings": sum(len(c.get("headings") or []) for c in bundle["cases"]),
    }


# =============================================================================
# 接口 7：GET /api/v1/admin/quality/defects
# 作用：查询缺陷台账，可按状态、严重级别筛选。
# 查询参数：
#   status   —— open / fixed / wontfix / resolved，空字符串表示不筛选；
#   severity —— P0 / P1 / P2，空字符串表示不筛选。
# 返回：{"items": [...], "total": 当前返回条数}（本接口不分页）。
# =============================================================================
# 注册 GET 路由；两个查询参数默认空串，表示“全部”。
@router.get("/defects")
# p 负责鉴权；status/severity 是筛选条件。
def defects(status: str = "", severity: str = "", p: Principal = Depends(quality_admin)):
    # 基础 SQL：WHERE 1=1 是一个常用技巧——恒真条件，后面可以无脑追加 " AND ..."，
    # 不必判断“这是第一个条件吗”，从而简化动态 SQL 的拼接。
    sql = "SELECT * FROM quality_defect WHERE 1=1"
    # 参数列表：顺序必须与 SQL 中 ? 的出现顺序一致，绝不能把用户输入拼进 SQL 文本。
    params: list = []
    # 若传了 status，就追加一个等值条件，并把值放进参数列表。
    if status:
        # 只追加占位符 ?，真正的值通过 params 传入，由数据库驱动负责转义。
        sql += " AND status=?"
        # 参数顺序与上面的 ? 对应。
        params.append(status)
    # 若传了 severity，同样追加条件。
    if severity:
        # 严重级别筛选，例如只看 P0 阻断级缺陷。
        sql += " AND severity=?"
        # 与 SQL 中的第二个 ? 对应。
        params.append(severity)
    # 固定排序：CASE 把 P0 排最前、P1 其次、其余（P2 或未知值）最后；
    # 同一级别内按 updated_at 倒序，最近更新的缺陷排在前面。
    # updated_at 是 ISO 字符串，格式统一，因此按文本排序等价于按时间排序。
    sql += " ORDER BY CASE severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END, updated_at DESC"
    # 执行查询，返回 list[dict]；每行是 quality_defect 表的一条记录。
    rows = fetch_all(sql, params)
    # 数据库里的 detail 是 JSON 文本，直接返回会让前端拿到字符串，这里统一还原成对象。
    for r in rows:
        # load_json 在值为 None/空串/非法 JSON 时返回默认值 {}，保证前端拿到的始终是对象。
        r["detail"] = load_json(r.get("detail"), {})
    # 返回结果；total 只是本次筛选命中的条数，不是全表总数（本接口没有分页）。
    return {"items": rows, "total": len(rows)}


# =============================================================================
# 接口 8：PATCH /api/v1/admin/quality/defects/{defect_id}
# 作用：手工流转单个缺陷的状态（例如修复后标记 fixed，或评审后标记 wontfix）。
# 请求体：{"status": "open|fixed|wontfix|resolved"}
# 返回：{"defect_id": ..., "status": ...}
# =============================================================================
# 注册 PATCH 路由：PATCH 表示“局部更新一个资源”，比 PUT 更贴合只改状态的语义。
@router.patch("/defects/{defect_id}")
# defect_id 来自路径；payload 是必填的 JSON 对象（不能省略）。
def update_defect(defect_id: str, payload: dict, p: Principal = Depends(quality_admin)):
    # 读取目标状态，缺省或空串时按 "open" 处理。
    status = str(payload.get("status") or "open")
    # 白名单校验，避免写入后端无法识别的状态值。
    if status not in ("open", "fixed", "wontfix", "resolved"):
        # 422：状态值不合法，前端可根据 code 做提示。
        raise HTTPException(status_code=422, detail={"code": "INVALID_STATUS", "message": "状态不合法"})
    # with db() as conn：进入时建立连接，正常退出自动 commit，异常自动 rollback，最后 close。
    with db() as conn:
        # 参数化 UPDATE：SET 两个字段，WHERE 精确定位一条缺陷；
        # updated_at 用 iso() 生成当前时间，保证排序和“最近更新”展示正确。
        conn.execute("UPDATE quality_defect SET status=?, updated_at=? WHERE defect_id=?", (status, iso(), defect_id))
    # 回显更新结果。注意：这里没有检查 defect_id 是否存在——
    # 若不存在，UPDATE 影响 0 行，接口仍返回成功（不会报 404，调用方需自行确认）。
    return {"defect_id": defect_id, "status": status}


# =============================================================================
# 接口 9：GET /api/v1/admin/quality/reviews
# 作用：查询人工复核记录，可按复核对象类型、对象 ID 过滤。
# 查询参数：
#   target_type —— grading（判分一致性）或 answerability（题目可答性）；
#   target_id   —— 被复核对象的主键；
#   limit       —— 最多返回多少条，默认 100，内部夹紧到 1—500。
# 返回：{"items": [...], "total": 当前返回条数}。
# =============================================================================
# 注册 GET 路由；三个查询参数都有默认值。
@router.get("/reviews")
# p 负责鉴权；target_type/target_id 用于过滤，limit 控制条数。
def reviews(target_type: str = "", target_id: str = "", limit: int = 100, p: Principal = Depends(quality_admin)):
    # 基础 SQL，同样使用 WHERE 1=1 技巧方便追加条件。
    sql = "SELECT * FROM quality_review WHERE 1=1"
    # 参数列表，顺序与 ? 一致。
    params: list = []
    # 按复核对象类型过滤。
    if target_type:
        # 占位符传参，避免 SQL 注入。
        sql += " AND target_type=?"
        # 记录参数值。
        params.append(target_type)
    # 按被复核对象 ID 过滤（例如某道题、某次判分记录）。
    if target_id:
        # 追加等值条件。
        sql += " AND target_id=?"
        # 记录参数值。
        params.append(target_id)
    # 固定按创建时间倒序，并限制条数；LIMIT 的 ? 也在下面通过参数传入。
    sql += " ORDER BY created_at DESC LIMIT ?"
    # 夹紧 limit：
    #   int(limit or 100) —— limit 为 0 或 None 时回退到 100，并确保是整数；
    #   min(500, ...)     —— 最多 500 条，防止一次拉取过多；
    #   max(1, ...)       —— 至少 1 条，避免负数等非法值（0 已在上一步回退为 100）。
    params.append(max(1, min(500, int(limit or 100))))
    # 执行查询，得到复核记录列表。
    rows = fetch_all(sql, params)
    # scores 列存的是 JSON 文本，这里统一还原成 dict 再返回。
    for r in rows:
        # 解析失败或为空时返回 {}，前端不必再判断字符串/对象两种形态。
        r["scores"] = load_json(r.get("scores"), {})
    # 返回列表与本次条数。
    return {"items": rows, "total": len(rows)}


# =============================================================================
# 接口 10：POST /api/v1/admin/quality/reviews
# 作用：录入一条人工复核评分。
# 请求体：
#   grading 类型：{"target_type":"grading","target_id":"...",
#                 "scores":{"human_score":0-100,"model_score":0-100},"notes":"..."}
#   answerability 类型：{"target_type":"answerability","target_id":"...",
#                        "scores":{"answerability":1-5},"notes":"..."}
# 返回：{"review_id", "target_type", "target_id", "scores"}，HTTP 201 Created。
# =============================================================================
# 注册 POST 路由；201 表示成功创建了一条新记录。
@router.post("/reviews", status_code=201)
# payload 必填：JSON 对象；p 负责鉴权，并作为 reviewer 记录操作者。
def add_review(payload: dict, p: Principal = Depends(quality_admin)):
    # 读取复核对象类型并转成字符串；缺省为空串，下面会校验。
    target_type = str(payload.get("target_type") or "")
    # 读取被复核对象 ID；缺省为空串。
    target_id = str(payload.get("target_id") or "")
    # 类型白名单：本系统目前只支持两类人工复核。
    if target_type not in ("grading", "answerability"):
        # 422：类型不合法，message 直接列出允许值，方便调用方修正。
        raise HTTPException(status_code=422, detail={"code": "INVALID_TARGET", "message": "target_type 只能是 grading / answerability"})
    # 对象 ID 不能为空，否则复核记录无法关联到具体题目/判分结果。
    if not target_id:
        # 422：缺少 target_id。
        raise HTTPException(status_code=422, detail={"code": "INVALID_TARGET", "message": "target_id 不能为空"})
    # scores 必须是字典；如果传了列表、字符串等非字典值，就当作空字典处理，
    # 后面会因为缺少必需分数而返回 422（比直接 500 更友好）。
    scores = payload.get("scores") if isinstance(payload.get("scores"), dict) else {}
    # 判分一致性复核：需要人工分和模型分两个 0—100 的分数。
    if target_type == "grading":
        # 逐个字段校验。
        for key in ("human_score", "model_score"):
            # try 包住类型转换：float(None) 抛 TypeError，float("abc") 抛 ValueError。
            try:
                # 先转 float，再用 min/max 夹紧到 [0, 100]：
                # 超出范围不会被拒绝，而是被静默截断到边界值。
                scores[key] = max(0.0, min(100.0, float(scores.get(key))))
            # 缺少字段或无法转成数字时，统一返回 422 INVALID_SCORE。
            except (TypeError, ValueError):
                # message 说明了合法区间与必需字段。
                raise HTTPException(status_code=422, detail={"code": "INVALID_SCORE", "message": "grading 需要 0—100 的 human_score / model_score"})
    # 题目可答性复核：只需要一个 1—5 分的评分。
    else:
        # 同样先转 float 再夹紧到 [1, 5]。
        try:
            # 1 分表示“完全不可答”，5 分表示“完全可答”，越界值被截断。
            scores["answerability"] = max(1.0, min(5.0, float(scores.get("answerability"))))
        # 缺字段或非数字 → 422。
        except (TypeError, ValueError):
            # 明确提示区间为 1—5。
            raise HTTPException(status_code=422, detail={"code": "INVALID_SCORE", "message": "answerability 需要 1—5 分"})
    # 生成复核记录主键，形如 qrev_1a2b3c4d5e（前缀 qrev + 10 位十六进制）。
    review_id = new_id("qrev")
    # 打开数据库连接并写入记录；with 块结束自动 commit，异常自动 rollback。
    with db() as conn:
        # 多行 SQL：INSERT 七个字段，对应 VALUES 的七个 ? 占位符；
        # 两段字符串字面量紧挨着写，Python 会自动把它们拼接成一个完整 SQL；
        # 为保持这种写法清晰，说明性注释统一放在整条语句上方。
        conn.execute(
            "INSERT INTO quality_review (review_id, target_type, target_id, reviewer, scores, notes, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            # 参数顺序与字段顺序严格一致：
            #   review_id —— 新生成的主键；
            #   target_type/target_id —— 复核对象；
            #   reviewer —— 优先 username，缺失时用 user_id；
            #   dump(scores) —— 把评分字典序列化成 JSON 文本存入 TEXT 列；
            #   notes —— 备注，截断到前 500 个字符，避免超长文本；
            #   iso() —— 当前时间（+08:00，精确到秒）。
            (review_id, target_type, target_id, p.username or p.user_id, dump(scores),
             str(payload.get("notes") or "")[:500], iso()),
        )
    # 返回新建记录的关键信息，前端可立即用它刷新列表或做后续跳转。
    return {"review_id": review_id, "target_type": target_type, "target_id": target_id, "scores": scores}


# =============================================================================
# 接口 11：GET /api/v1/admin/quality/thresholds
# 作用：返回质量验收阈值，供看板展示“目标线”和前端做达标标色。
# 说明：这些值来自 alun/quality/metrics.py 的模块级常量，改常量需重启服务才生效；
#       它们与 runner._THRESHOLDS 中的键名保持一致。
# =============================================================================
# 注册 GET 路由；无业务参数，p 只用于鉴权。
@router.get("/thresholds")
# p 是唯一参数，声明依赖即完成管理员校验。
def thresholds(p: Principal = Depends(quality_admin)):
    # 返回五个核心阈值（键名与前端/报告中的指标名对应）：
    #   title_f1               —— 标题结构 F1 目标 0.95；
    #   evidence_support_rate  —— 证据支撑率目标 1.0（每条结论都要有证据）；
    #   answerability          —— 可答性均分目标 4.3（5 分制）；
    #   grading_within10_rate  —— 判分误差在 ±10 分以内的比例目标 0.85；
    #   parse_30p_p95_sec      —— 30 页文档解析耗时 P95 目标 90 秒。
    return {
        # 标题匹配 F1 的目标值（metrics.TITLE_F1_TARGET）。
        "title_f1": quality_metrics.TITLE_F1_TARGET,
        # 证据支撑率目标（metrics.EVIDENCE_TARGET）。
        "evidence_support_rate": quality_metrics.EVIDENCE_TARGET,
        # 题目可答性目标均分（metrics.ANSWERABILITY_TARGET）。
        "answerability": quality_metrics.ANSWERABILITY_TARGET,
        # 判分一致率目标（metrics.GRADING_WITHIN10_TARGET）。
        "grading_within10_rate": quality_metrics.GRADING_WITHIN10_TARGET,
        # 30 页解析 P95 时延目标，单位秒（metrics.PARSE_30P_TARGET_SEC）。
        "parse_30p_p95_sec": quality_metrics.PARSE_30P_TARGET_SEC,
    }
