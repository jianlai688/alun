"""活动事件与 AI 输出审计。"""
# =============================================================================
# 【模块职责】
# 本模块是应用的“审计（audit）日志层”，只做两件事：
#   1. 记录“人”的操作——谁在什么时间、从哪个 IP、用什么浏览器，对系统做了什么
#      （登录/登出、文档上传/删除、会话开始、提问、交卷、设置修改等）；
#   2. 记录“AI”的输出——哪一次生成/评判调用了哪个模型、是否基于原文（grounded）、
#      是否走了降级（fallback）、耗时多少，便于事后追溯与质量分析。
#
# 【在整个应用中的位置】
#   app/
#     db.py       数据层：提供 db() 连接上下文、fetch_all() 查询、dump()/load_json() JSON 编解码、iso() 时间戳
#     audit.py    ← 本文件：建立在 db.py 之上的两张审计表的读写封装
#     main.py / admin.py / sessions.py / pipeline.py / assistant.py
#                 业务层：在关键动作处调用 log_event()
#     generator.py / judge.py
#                 AI 调用层：在每次模型调用后调用 record_ai_output()
#
# 【数据表】（表结构定义在 db.py 的 SCHEMA 常量中）
#   activity_event(id, ts, user_id, username, tenant_id, event_type, detail, ip, user_agent)
#   ai_audit(id, ts, user_id, document_id, session_id, kind, model, grounded, fallback,
#            latency_ms, detail)
#
# 【主要调用关系】
#   main.py        —— 登录成功/失败、登出、文档上传/删除等入口事件
#   admin.py       —— 用户启用/禁用、系统设置修改，以及后台“最近审计”列表
#   sessions.py    —— 答题会话全生命周期（开始、提问、提交、提示、跳过、报告生成）
#   pipeline.py    —— 文档解析流水线的关键节点
#   generator.py   —— 题目生成后写入 AI 审计（record_ai_output）
#   judge.py       —— 主观题评判后写入 AI 审计（record_ai_output）
#   assistant.py   —— 助手对话事件与最近事件展示
#
# 【设计要点：审计失败不影响主流程】
#   两个写函数都把数据库操作包在 try/except Exception 中，异常时直接 pass。
#   原因：审计只是“旁路记录”，如果因为磁盘满、数据库锁冲突、字段超长导致登录或出题失败，
#   对用户来说是不可接受的；因此这里选择“宁可少记一条日志，也不能中断业务”。
#   代价：出错时没有任何提示，排障时需要结合应用日志与数据库状态排查。
#
# 【说明】本文件是学习注释版：所有可执行语句、字符串、缩进均与源文件保持一致，
#         仅新增中文注释与文档字符串，便于初学者逐行理解。
# =============================================================================
# 导入“延迟注解”特性（PEP 563）：让类型注解在运行时不被求值，而是按字符串保存。
# 这样即使运行环境是 Python 3.8/3.9，也能书写 dict | None 这类 3.10 才支持的联合类型语法。
from __future__ import annotations

# 从同包的 db 模块导入四个基础设施函数：
#   db()        —— @contextmanager 装饰的连接上下文，正常退出自动 commit，异常自动 rollback 并关闭连接；
#   dump()      —— 把 Python 对象序列化成 JSON 字符串（ensure_ascii=False，中文不会被转义）；
#   fetch_all() —— 执行 SELECT 并把所有结果行转成 list[dict]；
#   iso()       —— 生成东八区、精确到秒的 ISO 8601 时间字符串，用作审计时间戳。
from .db import db, dump, fetch_all, iso


def log_event(
    # 单独的 * 表示后面全部是“仅关键字参数”：调用时必须写 log_event(event_type="login")，
    # 不能按位置传参，避免长参数列表被传错位置（这也是 Python 中强制可读性的一种惯用法）。
    *,
    # 用户唯一 ID；未登录场景（例如登录失败）允许为空字符串。
    user_id: str = "",
    # 用户名/登录名，便于人眼阅读日志；同样是可选的冗余字段（登录失败时也能记录尝试的账号）。
    username: str = "",
    # 租户（多租户 SaaS 场景下的组织）ID，用于按客户隔离与检索审计数据。
    tenant_id: str = "",
    # 事件类型，必填，例如 "login"、"logout"、"document_upload"；是审计检索的主要维度。
    event_type: str,
    # 事件详情字典，可为 None；写入前会被 dump() 序列化为 JSON 文本。
    detail: dict | None = None,
    # 客户端 IP，用于安全审计（异常登录、暴力破解排查）。
    ip: str = "",
    # 客户端 User-Agent，用于区分浏览器/客户端类型。
    user_agent: str = "",
) -> None:
    """记录一条用户活动事件（写入 activity_event 表）。

    参数:
        user_id / username / tenant_id: 操作者身份信息，未登录时可为空字符串。
        event_type: 事件类型字符串，必填，是后续检索与统计的维度。
        detail: 任意可 JSON 序列化的附加信息，落库前转成 JSON 文本。
        ip / user_agent: 请求来源信息，分别截断到 64 / 240 个字符。

    返回:
        None。函数不返回新事件 id，也不向外抛异常（写入失败会被静默忽略）。
    """
    # 用 try 包住整段数据库写入：审计是旁路功能，任何异常都不应向上冒泡影响业务。
    try:
        # db() 是上下文管理器：进入时 connect()，正常退出时 commit()，异常时 rollback() 并关闭连接。
        with db() as conn:
            # 执行参数化 INSERT：SQL 里的 ? 是占位符，真正的值放在第二个参数元组里，
            # 由数据库驱动负责转义，从根上避免 SQL 注入，也省去手工拼接引号的麻烦。
            conn.execute(
                # SQL 第 1 段：目标表与列名。列顺序必须与下面参数元组的顺序严格一致。
                "INSERT INTO activity_event (ts, user_id, username, tenant_id, event_type, detail, ip, user_agent)"
                # SQL 第 2 段：8 个占位符，与前面的 8 个列一一对应（相邻字符串字面量会被 Python 自动拼接）。
                " VALUES (?,?,?,?,?,?,?,?)",
                # 参数元组，顺序与上面的列名一一对应：
                #   iso()              -> ts，当前时间（东八区、精确到秒）
                #   user_id            -> user_id
                #   username           -> username
                #   tenant_id          -> tenant_id
                #   event_type         -> event_type
                #   dump(detail or {}) -> detail；None 时用空字典兜底，再序列化为 JSON 文本
                #   ip[:64]            -> ip，切片截断到 64 字符，防止超长输入撑爆字段/索引
                #   user_agent[:240]   -> user_agent，截断到 240 字符，同理防御异常客户端
                (iso(), user_id, username, tenant_id, event_type, dump(detail or {}), ip[:64], user_agent[:240]),
            )
    # 捕获所有异常（数据库不可用、锁等待超时、字段类型错误等），故意不记录也不抛出。
    except Exception:
        # 空语句：明确表示“什么都不做”。审计失败不阻断主业务流程。
        pass


def recent_events(limit: int = 100, since_id: int = 0) -> list[dict]:
    """按 id 倒序读取最近的活动事件，供后台页面展示或前端增量轮询。

    参数:
        limit: 最多返回多少条；内部会被夹到 1~300 之间，非法/缺省值回退为 100。
        since_id: 只取 id 大于该值的事件；0 表示不限制（首次加载）。

    返回:
        list[dict]：每行是 activity_event 的一条记录，其中 detail 已被解析成 dict。
    """
    # 一行完成“容错 + 限幅”：
    #   int(limit or 100) —— limit 为 None/0/空时用 100 兜底，再转成整数（非数字会抛 ValueError）；
    #   min(300, ...)     —— 上限 300，防止一次查询拖垮页面；
    #   max(1, ...)       —— 下限 1，避免 LIMIT 0 查出空列表或负数导致 SQL 异常。
    limit = max(1, min(300, int(limit or 100)))
    # 有 since_id 时走“增量查询”分支：前端轮询时只取比上次最大 id 更新的事件，减少传输量。
    if since_id:
        # 参数化查询：WHERE id>? 用自增主键当游标（比时间戳更可靠，不会因同一秒多条而漏数据）；
        # ORDER BY id DESC 让最新事件排在最前；LIMIT ? 控制条数。
        rows = fetch_all("SELECT * FROM activity_event WHERE id>? ORDER BY id DESC LIMIT ?", (int(since_id), limit))
    # 否则是首次加载：不限制 id，直接取最新 limit 条。
    else:
        # 同样按 id 倒序 + LIMIT；注意参数元组只有一个元素，必须写成 (limit,) 带逗号才是元组。
        rows = fetch_all("SELECT * FROM activity_event ORDER BY id DESC LIMIT ?", (limit,))
    # 在函数内部导入 load_json：只有真正读取事件时才需要 JSON 解析；
    # 放在这里也让模块顶层的依赖更聚焦（db 模块此时早已加载，没有额外开销）。
    from .db import load_json

    # 逐行处理查询结果：fetch_all() 返回的是 list[dict]，这里直接原地修改每个字典。
    for row in rows:
        # detail 在库里是 JSON 文本（如 '{"reason": "invalid_credentials"}'），
        # 这里反序列化成 Python 对象，方便前端/调用方直接使用；
        # row.get("detail") 取不到时返回 None，load_json 会回退到默认值 {}，保证字段类型稳定。
        row["detail"] = load_json(row.get("detail"), {})
    # 返回处理后的列表；空结果时就是空列表 []，调用方无需额外判空。
    return rows


def record_ai_output(
    # 同样用 * 强制关键字传参，避免十几个参数按位置传错。
    *,
    # AI 调用类型，例如 "generate_questions"、"judge_answer"，用于按场景统计调用量与质量。
    kind: str,
    # 实际使用的模型名（如 "deepseek-chat"），用于对比不同模型的质量与成本。
    model: str,
    # 本次输出是否“有依据”：是否基于检索到的原文/证据，而不是模型自由发挥。
    grounded: bool,
    # 是否发生了降级（例如主模型失败后改用备用模型或规则兜底），默认 False。
    fallback: bool = False,
    # 调用耗时（毫秒），默认 0；用于性能监控与超时分析。
    latency_ms: int = 0,
    # 发起调用的用户 ID，可为空（如系统后台任务）。
    user_id: str = "",
    # 关联的文档 ID，可为空；用于把这次 AI 输出追溯到具体文档。
    document_id: str = "",
    # 关联的答题会话 ID，可为空；用于把这次 AI 输出追溯到具体练习会话。
    session_id: str = "",
    # 附加上下文（提示词版本、token 数、错误信息等），落库前序列化为 JSON 文本。
    detail: dict | None = None,
) -> None:
    """记录一次 AI 输出审计（写入 ai_audit 表）。

    参数:
        kind: AI 调用场景（生成题目、评判答案等），必填。
        model: 实际调用的模型名称。
        grounded: 输出是否有原文/证据支撑，落库时转成 0/1 整数。
        fallback: 是否发生降级，落库时同样转成 0/1。
        latency_ms: 调用耗时（毫秒），None（或 0）会被兜底为 0。
        user_id / document_id / session_id: 可选的追溯维度。
        detail: 任意可 JSON 序列化的附加信息。

    返回:
        None。写入失败由内部静默吞掉，不向调用方抛异常。
    """
    # 与 log_event 相同：审计写入失败不能影响 AI 主流程，因此整体用 try 包裹。
    try:
        # 打开数据库连接上下文，正常结束自动提交，异常自动回滚。
        with db() as conn:
            # 参数化 INSERT，列顺序与下面参数元组严格对应，避免拼接 SQL 带来的注入风险。
            conn.execute(
                # SQL 第 1 段：表名与 10 个列名（分两行书写只为可读性，都是同一个字符串字面量）。
                "INSERT INTO ai_audit (ts, user_id, document_id, session_id, kind, model, grounded, fallback,"
                # SQL 第 2 段：10 个占位符，与上面的列一一对应。
                " latency_ms, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                # 参数元组，顺序说明：
                #   iso()                        -> ts，当前时间
                #   user_id                      -> 用户 ID
                #   document_id                  -> 文档 ID
                #   session_id                   -> 会话 ID
                #   kind                         -> AI 调用场景
                #   model                        -> 模型名
                #   "1 if grounded else 0"       -> 布尔值统一转成 0/1 整数存储（SQLite 无布尔类型）
                #   "1 if fallback else 0"       -> 是否降级，同样转 0/1
                #   int(latency_ms or 0)         -> 耗时；None/0 时兜底 0，并强制转成整数
                #   dump(detail or {})           -> 附加详情；None 时用空字典，再序列化为 JSON
                (iso(), user_id, document_id, session_id, kind, model, 1 if grounded else 0, 1 if fallback else 0,
                 int(latency_ms or 0), dump(detail or {})),
            )
    # 捕获所有异常并静默忽略：审计失败不应让一次成功的 AI 调用变成业务报错。
    except Exception:
        # 空语句：什么都不做（不打印、不重试、不抛出）。
        pass
