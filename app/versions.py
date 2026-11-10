"""V1.2 结构版本：快照、列表与回滚。"""
# =============================================================================
# 文件：alun/app/versions.py
# 主题：V1.2「结构版本」——把一份文档的解析结构（节点/切片/知识点/证据）
#       打包成 JSON 快照存进 document_version 表，并提供“查列表、看详情、回滚”能力。
#
# 一、这个文件负责什么（用一句话概括）
#     它像游戏里的“存档/读档”系统：
#       snapshot()    = 存档：把文档当前的四张结构表整体复制成一份 JSON；
#       list_versions() = 看存档列表（只读摘要，不读大 JSON）；
#       get_version()   = 打开某个存档看详情（含完整 JSON）；
#       restore()       = 读档：先给当前状态自动存档，再用目标存档覆盖四张表。
#
# 二、在整个应用中的位置与主要调用关系
#     - 本文件属于“业务/数据访问层”，只依赖同包的 .db，不依赖 FastAPI、
#       不依赖 security、也不直接处理 HTTP 请求或权限。
#     - HTTP 入口在 app/version_api.py：
#         GET  /api/v1/documents/{id}/versions          -> list_versions()
#         GET  /api/v1/documents/{id}/versions/{no}     -> get_version()
#         POST /api/v1/documents/{id}/versions/{no}/restore -> restore()
#       version_api 在调用本文件前会用 _doc() 做“租户隔离 + 管理员/所有者”校验，
#       并把这里的 ValueError("版本不存在") 翻译成 HTTP 404 + 错误码 VERSION_NOT_FOUND。
#     - 其它调用点：
#         app/main.py    在“结构编辑”前调用 snapshot(reason="结构编辑前自动快照")；
#         app/pipeline.py 在“重新解析”前调用 snapshot(reason="重新解析前自动快照")；
#         app/version_api.py 在 restore 成功后还会调用
#         pipeline.repair_sessions_for_document() / repair_questions_for_document()，
#         修复引用了旧 node_id 的会话与题目。
#     - 底层表结构定义在 app/db.py 的 SCHEMA 中：
#         document_version(version_id, doc_id, version_no, snapshot, node_count,
#                          reason, created_by, created_at)
#       其中 (doc_id, version_no) 上有唯一索引 idx_document_version_no，
#       所以本文件才敢用 SQLite/PostgreSQL 都支持的 ON CONFLICT 做“覆盖式写入”。
#
# 三、核心数据与版本号规则
#     - document.doc_version 是“当前结构版本号”，默认 1；每次 restore 成功后 +1。
#     - snapshot() 不自增版本号，而是直接使用 document.doc_version 作为 version_no。
#       因此同一版本号反复存档会命中唯一索引，走 ON CONFLICT DO UPDATE 覆盖旧快照。
#     - 快照 JSON 的形状（dict，四个键，每个值都是“行字典”的列表）：
#         {
#           "node": [...], "chunk": [...],
#           "knowledge_point": [...], "evidence": [...]
#         }
#       选择“整份 JSON 存一列”而不是拆成历史表，优点是实现简单、SQLite/PostgreSQL
#       通用、回滚时一次性读取；代价是占用空间更大、无法用 SQL 直接查询快照内部字段。
#
# 四、事务、并发与权限（阅读前必看）
#     - 本文件所有数据库操作都是同步（阻塞）的，没有 async/await、没有手动开线程。
#       FastAPI 中同步 def 路由会在线程池里执行，因此这里可能被多个线程同时调用；
#       db() 每次调用都会新建一条连接（SQLite 连接参数 check_same_thread=False），
#       连接不跨线程共享，事务边界就是 with db() 这一小段。
#     - snapshot() 自己一个事务；restore() 中“删除 + 插入 + 更新 document”在同一个
#       事务里，但“回滚前快照”和“回滚后快照”是事务外的两次独立 snapshot() 调用。
#       若中途异常，结构表的删除/插入会回滚，但已经写下的自动快照不会撤销。
#     - 并发调用 snapshot() 时，两个请求可能拿到同一个 doc_version，唯一索引会让
#       后写入者覆盖先写入者（last-write-wins）；本文件没有额外加锁。
#     - 权限不在本文件：直接调用这些函数会绕过 API 的租户/所有者校验，
#       所以它们只应被已经完成鉴权的上层代码调用。
#
# 五、建议阅读顺序
#     VERSION_TABLES -> snapshot() -> list_versions() -> get_version() -> restore()
# =============================================================================

# 启用“延迟求值”的类型注解：注解在运行时不会被求值，而是以字符串保存，
# 这样 `dict | None`、`list[dict]` 等新写法在旧解释器/前向引用场景下也不会报 NameError。
from __future__ import annotations

# 从同包的 db.py 导入数据访问工具（逐个说明，后面用到时还会再标注）：
#   db          —— @contextmanager 包装的连接上下文：正常退出 commit，异常 rollback，最后 close；
#   dump        —— json.dumps(..., ensure_ascii=False) 的简写，把 Python 对象转成 JSON 文本；
#   fetch_all   —— 执行查询并返回 list[dict]（内部自动开连接、提交、关闭）；
#   fetch_one   —— 执行查询并返回第一行 dict，没查到返回 None；
#   iso         —— 生成东八区、精确到秒的 ISO 时间字符串，用作 created_at/updated_at；
#   load_json   —— 容错解析 JSON：None/空串返回默认值，已经是 dict/list 就原样返回，坏 JSON 也不抛异常；
#   new_id      —— 生成带前缀的短随机 ID，例如 ver_1a2b3c4d5e。
from .db import db, dump, fetch_all, fetch_one, iso, load_json, new_id

# ---------------------------------------------------------------------------
# VERSION_TABLES：版本快照的“表清单 + 列清单”注册表。
#
# 结构：dict[表名, tuple(列清单字符串, 字段名元组)]
#   - 第 1 项 cols：逗号分隔的列名，直接拼进 SELECT / INSERT 的 SQL；
#   - 第 2 项 fields：Python 元组，restore 时按这个顺序用 row.get(f) 取参数。
# 为什么要两份？SQL 需要的是 "a, b, c" 这样的文本，而 Python 构造参数需要可遍历的
# 字段名序列；把两者放在一起，能保证“读出来的列”和“写回去的列”永远同序、同集合。
# 安全说明：表名、列名全部来自本常量（受控白名单），不是用户输入；真正的值仍然
# 使用 ? 占位符参数化，因此不存在 SQL 注入风险。
# 范围说明：只包含“文档结构”四表，不含 document 主表本身，也不含 question、
# report、session 等派生数据——回滚结构后由 version_api 另行修复这些派生数据。
# ---------------------------------------------------------------------------
VERSION_TABLES = {
    # node：解析后的文档结构节点（章节 section、段落、表格 table、图表 chart 等）。
    # 列含义：node_id 节点主键；doc_id 所属文档；parent_id 父节点（顶层为 NULL）；
    # level 层级深度；node_type 节点类型；order_no 同级排序号；title 标题；
    # text 正文；page_start/page_end 起止页码；bbox 版面坐标 JSON；confidence 解析置信度；
    # path 结构路径（如 /1/2）；char_count 字符数；order_index 文档内全局顺序。
    "node": (
        "node_id, doc_id, parent_id, level, node_type, order_no, title, text, page_start, page_end,"
        " bbox, confidence, path, char_count, order_index",
        ("node_id", "doc_id", "parent_id", "level", "node_type", "order_no", "title", "text",
         "page_start", "page_end", "bbox", "confidence", "path", "char_count", "order_index"),
    ),
    # chunk：按 token 切好的检索片段，供向量检索/出题时定位原文。
    # node_id 指回所属节点；token_count 是估算的 token 数；order_no 是片段序号。
    "chunk": ("chunk_id, doc_id, node_id, text, token_count, order_no",
              ("chunk_id", "doc_id", "node_id", "text", "token_count", "order_no")),
    # knowledge_point：从节点中抽取的知识点。
    # name 名称；kp_type 类型；definition 定义；importance 重要度(0~1)；
    # difficulty 难度(整数)；evidence_ids 证据 ID 的 JSON 数组；
    # prerequisites 先修知识点的 JSON 数组；order_no 展示顺序。
    "knowledge_point": (
        "kp_id, doc_id, node_id, name, kp_type, definition, importance, difficulty, evidence_ids, prerequisites, order_no",
        ("kp_id", "doc_id", "node_id", "name", "kp_type", "definition", "importance", "difficulty",
         "evidence_ids", "prerequisites", "order_no"),
    ),
    # evidence：支撑知识点的原文证据（可回溯到页码与字符区间）。
    # page_no 页码；quote 原文摘录；char_start/char_end 字符偏移；
    # strength 证据强度(strong/weak 等)；context 上下文说明。
    "evidence": (
        "evidence_id, doc_id, node_id, page_no, quote, char_start, char_end, strength, context",
        ("evidence_id", "doc_id", "node_id", "page_no", "quote", "char_start", "char_end", "strength", "context"),
    ),
}


# ---------------------------------------------------------------------------
# snapshot：给指定文档“存一次档”。
# 参数：
#   doc_id     —— 文档主键；
#   reason     —— 存档原因，会显示在版本列表里（如“重新解析前自动快照”）；
#   created_by —— 操作者显示名（API 传 username，没有则传 user_id）。
# 返回：
#   成功 -> dict：version_id / version_no / node_count / reason / created_at；
#   文档不存在，或该文档还没有任何 node（尚未解析/已被清空）-> None。
# 失败语义：不抛异常，用 None 表示“没有可存的档”，调用方通常忽略即可。
# ---------------------------------------------------------------------------
def snapshot(doc_id: str, reason: str = "", created_by: str = "") -> dict | None:
    # 第 1 步：先确认文档存在，同时取出 doc_version（下面要用它当版本号）。
    # fetch_one 返回 dict 或 None；SQL 的 ? 是参数占位符，db 层会自动适配 PostgreSQL 的 %s。
    doc = fetch_one("SELECT * FROM document WHERE doc_id=?", (doc_id,))
    # 文档不存在（或传入了错误 ID）时直接返回 None，避免写出“孤儿快照”。
    if not doc:
        # 这里选择静默返回 None，而不是抛异常：自动存档是“尽力而为”的辅助动作，
        # 不应该因为文档刚被删除就让主业务流程失败。
        return None
    # 第 2 步：准备一个 dict，按表名收集四张表的全部行。
    data = {}
    # 遍历注册表：VERSION_TABLES.items() 每次给出 (表名, (列清单, 字段元组))。
    # 下划线 _ 表示“字段元组这里用不到”，只是解包占位，不是特殊语法。
    for table, (cols, _) in VERSION_TABLES.items():
        # 动态拼出 SELECT 列清单 FROM 表 WHERE doc_id=?：
        # 表名/列名来自受控常量，doc_id 用参数化绑定；fetch_all 返回 list[dict]。
        data[table] = fetch_all(f"SELECT {cols} FROM {table} WHERE doc_id=?", (doc_id,))
    # 第 3 步：node 是结构快照的核心。如果一条 node 都没有，说明文档还没解析成功，
    # 存下来只会得到一份“空档”，回滚时还会把现有结构清空，所以直接放弃。
    if not data["node"]:
        # 返回 None 让调用方知道“本次没有产生版本”，而不是误以为存档成功。
        return None
    # 第 4 步：确定版本号。doc 是 dict，.get 在键缺失时返回 None；
    # `or 1` 兼容 doc_version 为 NULL、0 或空字符串的脏数据；int() 再统一成整数。
    version_no = int(doc.get("doc_version") or 1)
    # 生成版本主键：new_id("ver") 形如 ver_ + uuid4 十六进制前 10 位；
    # 前缀便于日志和排查时一眼看出这是版本记录。
    version_id = new_id("ver")
    # 第 5 步：写入 document_version 表。
    # with db() as conn 是上下文管理器：代码块正常结束自动 commit；
    # 任何异常都会 rollback 并把异常继续抛出；最后一定 close 连接。
    with db() as conn:
        # conn.execute 执行一条带参数的 SQL；参数按 ? 的位置依次传入。
        conn.execute(
            # 下面四段字符串在括号内“隐式拼接”为一条完整 SQL（Python 相邻字符串字面量会自动连接）。
            # INSERT 列出 8 列，对应 8 个 ? 占位符。
            "INSERT INTO document_version (version_id, doc_id, version_no, snapshot, node_count, reason, created_by, created_at)"
            # VALUES 绑定 8 个参数；ON CONFLICT 针对唯一索引 (doc_id, version_no)：
            # 若同一文档同一版本号已存在，则改成 UPDATE（upsert），保证“一个版本号只留一份快照”。
            " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(doc_id, version_no) DO UPDATE SET"
            # 冲突时覆盖快照正文、节点数、原因、操作者和时间；SET 子句没有更新 version_id，
            # 所以覆盖场景下“库里的 version_id”仍是第一次插入时的旧 ID，而函数返回值却是本次新生成的
            # version_id——两者可能不一致（学习时留意这一点，注释版不改变原逻辑）。
            " snapshot=excluded.snapshot, node_count=excluded.node_count, reason=excluded.reason,"
            " created_by=excluded.created_by, created_at=excluded.created_at",
            # 参数顺序必须与上面的列顺序完全一致：
            # version_id 新 ID；doc_id 文档；version_no 当前版本号；
            # dump(data) 把四表数据序列化为 JSON 文本（ensure_ascii=False，中文不转义）；
            # len(data["node"]) 节点总数；reason[:200] 只存前 200 字，避免原因字段过长；
            # created_by 操作者；iso() 东八区当前时间（精确到秒）。
            (version_id, doc_id, version_no, dump(data), len(data["node"]), reason[:200], created_by, iso()),
        )
    # 第 6 步：返回摘要给调用方（不返回完整 data，避免把大 JSON 在内存里再传一遍）。
    # 注意两个小细节：
    #   1) reason 返回的是完整原文，而数据库里只保存了前 200 字；
    #   2) created_at 这里重新调用 iso()，可能与数据库中写入的时间相差几毫秒/1 秒。
    return {"version_id": version_id, "version_no": version_no, "node_count": len(data["node"]), "reason": reason,
            "created_at": iso()}


# ---------------------------------------------------------------------------
# list_versions：查询某文档的版本列表（按版本号从新到旧）。
# 只 SELECT 摘要列，故意不查 snapshot 大字段——列表页不需要几 MB 的 JSON，
# 这样能显著减少 IO 和内存占用。
# ---------------------------------------------------------------------------
def list_versions(doc_id: str) -> list[dict]:
    # fetch_all 内部会 with db()：自动提交、关闭连接，返回 list[dict]。
    rows = fetch_all(
        # 两段相邻字符串拼成一条 SQL：选择列表所需列，按 doc_id 过滤，版本号降序。
        "SELECT version_id, doc_id, version_no, node_count, reason, created_by, created_at"
        " FROM document_version WHERE doc_id=? ORDER BY version_no DESC", (doc_id,)
    )
    # 直接把查询结果返回；没有记录时是空列表 []，不是 None。
    return rows


# ---------------------------------------------------------------------------
# get_version：读取单个版本的完整详情（包含 snapshot 字段）。
# 返回 None 表示该文档没有这个版本号；找到时 snapshot 会从 JSON 文本解析成 dict。
# ---------------------------------------------------------------------------
def get_version(doc_id: str, version_no: int) -> dict | None:
    # 同时用 doc_id + version_no 精确定位一行；SELECT * 会带出 snapshot 大字段。
    row = fetch_one("SELECT * FROM document_version WHERE doc_id=? AND version_no=?", (doc_id, version_no))
    # 没查到就返回 None，由调用方决定是报 404 还是走其它逻辑。
    if not row:
        # 注意：这里不抛异常，保持“查询函数”的纯粹；restore 会把它转成 ValueError。
        return None
    # 数据库里的 snapshot 是 TEXT（JSON 字符串），这里转回 Python 对象再返回。
    # load_json 的第二个参数 {} 是默认值：NULL、空串或坏 JSON 都退化成空字典，
    # 正常情况下调用方拿到的 row["snapshot"] 就是 dict，可以安全地 .get(...)；
    # 但如果库里存的 JSON 本身是数组等非 dict 结构，load_json 会原样返回，需由调用方保证数据形状。
    row["snapshot"] = load_json(row.get("snapshot"), {})
    # 返回完整行（含已解析的 snapshot dict）。
    return row


# ---------------------------------------------------------------------------
# restore：把文档回滚到指定版本。
# 参数：
#   doc_id     —— 要回滚的文档；
#   version_no —— 目标版本号；
#   created_by —— 操作者显示名，用于两条自动快照的审计。
# 返回：dict，包含 document_id/restored_from/version 以及三个统计数。
# 异常：目标版本不存在时抛 ValueError("版本不存在")；version_api 会转成 404。
#
# 回滚流程（重要）：
#   1) 读目标快照；
#   2) 给“当前结构”先自动快照（后悔药，防止回滚本身把现状弄丢）；
#   3) 在一个事务里：删四张结构表 -> 按快照逐行插回 -> 更新 document 的版本号和统计；
#   4) 事务提交后，再给“回滚后的新结构”自动快照，形成新的版本记录。
# 注意：document 的标题等元数据不会被回滚，只更新 doc_version、stats、updated_at。
# ---------------------------------------------------------------------------
def restore(doc_id: str, version_no: int, created_by: str = "") -> dict:
    # 先取目标版本；get_version 已经把 snapshot 解析成 dict。
    target = get_version(doc_id, version_no)
    # 目标版本不存在时抛 ValueError；这是本文件唯一的业务异常，
    # 由 version_api 捕获并映射为 HTTP 404 + {"code": "VERSION_NOT_FOUND"}。
    if not target:
        # 错误信息是中文，直接展示给前端用户；注释版不改变原文。
        raise ValueError("版本不存在")
    # 回滚前先保存当前结构。reason 里带上目标版本号，方便在版本列表里追溯。
    # 此时 document.doc_version 还是旧值，所以这次快照占用的是“当前版本号”，
    # 若该版本号已有记录，会走 upsert 覆盖（同一版本号只保留最后一次快照）。
    snapshot(doc_id, reason=f"回滚前自动快照（目标 v{version_no}）", created_by=created_by)
    # 取出快照内容；`or {}` 兜底 snapshot 为 None/空 dict 的情况。
    # 正常情况下它应形如 {"node": [...], "chunk": [...], ...}；
    # 如果手工往库里塞了非 dict 的 JSON（如 []），后续 .get 会报错——这属于脏数据。
    data = target["snapshot"] or {}
    # 开启一个事务，把“删旧结构 + 写回旧版本 + 更新文档元信息”作为一个原子操作。
    with db() as conn:
        # 第 1 步：清空当前文档的四张结构表，为写回快照腾位置。
        # 删除顺序是“父表 node 在前、子表在后”，但 db.py 里 SQLite 关闭了外键
        # （PRAGMA foreign_keys=OFF），PostgreSQL 也没有建外键，所以顺序不影响正确性。
        for table in ("node", "chunk", "knowledge_point", "evidence"):
            # 只删当前文档的数据（WHERE doc_id=?），不会影响其它文档。
            conn.execute(f"DELETE FROM {table} WHERE doc_id=?", (doc_id,))
        # 第 2 步：按 VERSION_TABLES 注册的顺序逐表写回。
        # cols 是列清单字符串；fields 是字段名元组，用来按顺序取值。
        for table, (cols, fields) in VERSION_TABLES.items():
            # 生成与字段数量相同的占位符，例如 6 个字段 -> "?, ?, ?, ?, ?, ?"。
            # 这样 INSERT 语句无需为每张表手写一遍，增删字段时只改 VERSION_TABLES 即可。
            values = ", ".join("?" for _ in fields)
            # data.get(table, []) 兼容旧快照缺某张表的情况：缺表就当空列表，不报 KeyError。
            for row in data.get(table, []):
                # 逐行插入：row 是快照里的 dict，row.get(f) 按字段名取值，缺失则给 None。
                # tuple(...) 把生成器固化成与占位符顺序一致的参数元组；
                # 这里没有做类型转换，SQLite 会按列亲和性自动处理。
                conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({values})", tuple(row.get(f) for f in fields))
        # 第 3 步：重新计算统计数，写回 document.stats，供文档列表/概览页展示。
        # node_count：所有结构节点数。
        node_count = len(data.get("node", []))
        # section_count：只统计 node_type 恰好等于 "section" 的节点（章节数）。
        # sum(1 for ... if ...) 是“满足条件就记 1”的惯用写法，比 filter+len 更直观。
        section_count = sum(1 for n in data.get("node", []) if n.get("node_type") == "section")
        # kp_count：知识点数量。
        kp_count = len(data.get("knowledge_point", []))
        # chunk_count：检索切片数量。
        chunk_count = len(data.get("chunk", []))
        # 在同一个事务里读取 document 的当前版本号和 stats。
        # 这里直接用 conn.execute(...).fetchone()，而不是 fetch_one()，
        # 因为 fetch_one 会另开一条连接/事务，读不到本事务尚未提交的修改。
        # 返回类型：SQLite 是 sqlite3.Row，PostgreSQL 是 dict（db.PgConnection 配了 dict_row），
        # 两者都支持 doc["列名"] 下标访问。
        doc = conn.execute("SELECT doc_version, stats FROM document WHERE doc_id=?", (doc_id,)).fetchone()
        # stats 在库里是 JSON 文本；若 document 行意外不存在则用 "{}" 兜底，
        # load_json 解析失败也会回退到空 dict，保证后面 update 不会崩。
        stats = load_json(doc["stats"] if doc else "{}", {})
        # 用最新的四个统计值覆盖旧值（dict.update 会保留 stats 里其它未提及的键，
        # 例如解析质量、页数等元信息不会被清掉）。
        stats.update({"node_count": node_count, "section_count": section_count, "kp_count": kp_count, "chunk_count": chunk_count})
        # 回滚也算一次“结构变更”，所以版本号 +1。
        # doc 理论上应存在（前面 get_version 已证明 document_version 行存在），但两表没有外键，
        # 文档仍可能被并发删除，所以代码用 if doc else 1 做了兜底：doc 为空时按 1 再 +1 计算。
        new_version = int(doc["doc_version"] if doc else 1) + 1
        # 更新文档主表：版本号、统计 JSON、更新时间；WHERE 限定 doc_id。
        # dump(stats) 把 dict 序列化为 JSON 文本；iso() 生成东八区当前时间。
        conn.execute("UPDATE document SET doc_version=?, stats=?, updated_at=? WHERE doc_id=?",
                     (new_version, dump(stats), iso(), doc_id))
    # 第 4 步：事务提交后再给“回滚后的结构”存档。
    # 这次 snapshot 会读到刚更新的 doc_version=new_version，因此生成 v(new_version) 的记录；
    # 如果这步失败（例如数据库瞬时故障），结构已经回滚成功，但会缺少“回滚后快照”。
    snapshot(doc_id, reason=f"回滚到 v{version_no} 后快照", created_by=created_by)
    # 返回给 API 的结果：
    #   document_id  —— 注意键名是 document_id（不是内部参数名 doc_id），前端按这个字段取；
    #   restored_from —— 从哪个版本恢复；
    #   version       —— 回滚后 document 的新版本号；
    #   node_count / section_count / kp_count —— 回滚后的统计；
    #   chunk_count 虽然上面算过，但没有放进返回值（历史接口约定），只写进了 stats。
    return {"document_id": doc_id, "restored_from": version_no, "version": new_version,
            "node_count": node_count, "section_count": section_count, "kp_count": kp_count}
