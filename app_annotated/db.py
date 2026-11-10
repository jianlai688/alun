# =============================================================================
# 【模块导读】app/db.py —— 数据访问层（Data Access Layer，DAL）
# =============================================================================
# 一、这个文件负责什么
#   1) 定义数据库表结构 SCHEMA：document/node/chunk/.../document_version 共 26 张业务表；
#   2) 提供 connect()/init_db()：按配置连接 SQLite 或 PostgreSQL，并在启动时建表、补列、建索引；
#   3) 提供 db() 上下文管理器：把“取连接 → 执行 → 提交/回滚 → 关闭”封装成一个事务单元；
#   4) 提供 fetch_all()/fetch_one()/execute()/insert()/update() 等通用查询助手；
#   5) 提供 now()/iso()/new_id()/load_json()/dump() 等全应用复用的小工具。
#
# 二、它在整个应用中的位置（谁调用它）
#   app/config.py  ---->  提供 DB_BACKEND、DB_PATH、DATABASE_URL 三个配置
#   app/main.py    ---->  应用启动时调用 init_db()；请求处理中直接用 db()/fetch_* 读写
#   pipeline.py / sessions.py / reports.py / quality_api.py / ... ----> 业务逻辑层
#   调用方向：HTTP 路由(main.py) → 业务模块 → db.py → SQLite / PostgreSQL
#   反向：db.py 不导入任何业务模块，保持“数据层不依赖业务层”的单向依赖。
#
# 三、两种后端
#   - 默认 SQLite：单机/开发模式，数据库文件由 DB_PATH 指定，零外部服务，适合本地演示。
#   - 平台模式 PostgreSQL：设置 ALUN_DB_BACKEND=postgres 后启用；用 PgConnection 包装 psycopg，
#     让业务层继续写 SQLite 风格的 “?” 占位符，由 _pg_sql() 转成 “%s”。
#   - 两条路径共用同一份 SCHEMA；_pg_schema() 负责把 SQLite 专有语法替换掉。
#
# 四、贯穿全文的约定（读代码时请记住）
#   - 时间：统一用带时区的 datetime（东八区），落库前用 iso() 转成
#     “YYYY-MM-DDTHH:MM:SS+08:00” 字符串。这样 SQLite/PostgreSQL 都用 TEXT 存，
#     排序、比较、JSON 序列化都简单。
#   - JSON：options/answer/stats/plan 等字段在库里是 TEXT，写入前 dump()，读取后 load_json()。
#   - 主键：业务表用 TEXT 类型的业务 ID（new_id() 生成），方便跨库迁移、URL 传参和日志排查；
#     只有日志类表用自增整数。
#   - 事务：db() 正常退出自动 commit，异常自动 rollback 并重新抛出；不要在一个 db() 里再开写连接。
#   - 安全：所有“值”都用 ? 参数化，防 SQL 注入；表名/列名是代码里的常量，才允许用 f-string 拼接，
#     绝不能把用户输入拼进 SQL。
#   - 权限：本层不判断角色/租户，只原样存取 tenant_id/user_id；鉴权在 security.py 和路由层完成。
#   - 错误：本层不吞异常。sqlite3.Error / psycopg.Error 会向上抛，最终由 main.py 的全局异常处理器
#     转成统一的 JSON 错误码（如 INTERNAL_ERROR/500）；SQLite 的 “database is locked” 由
#     busy_timeout=30000 缓解，超过 30 秒仍会抛错。
#   - 线程/异步：这里全是同步阻塞调用；FastAPI 的 async 路由若直接调用会阻塞事件循环，
#     业务层应放在同步 def 路由或线程池中执行。db() 每次新建连接，连接不跨线程共享。
#
# 五、阅读建议
#   先看 now/iso/new_id 三个小工具 → 再看 SCHEMA 的表分组 → 再看 _pg_sql/_pg_schema 的后端适配
#   → 最后看 connect/init_db/db 与五个查询助手。理解了 db() 的事务边界，就理解了整个文件的骨架。
#
# 六、关于本注释版
#   下面只在原有代码行之间/行尾增加 # 注释，没有改动任何可执行语句、字符串、缩进、导入和原有空行。
#   SCHEMA 三引号字符串内部无法安全插入 #（会变成 SQL 文本），因此表结构说明集中放在它上方。
# =============================================================================
"""数据层：SQLite（默认）/ PostgreSQL（平台模式）双后端与通用查询助手。"""
# from __future__ 必须紧跟 docstring：把所有类型注解延迟到运行时求值，
# 这样 datetime | None、list[dict] 这类写法不会在导入时被立即解析，也方便前向引用。
from __future__ import annotations  # PEP 563：注解变成字符串，减少导入期开销

# json：数据库里的 JSON 字段是 TEXT；load_json()/dump() 用它做序列化与反序列化。
import json  # 标准库，无第三方依赖
# sqlite3：Python 内置的 SQLite 驱动，默认后端直接使用它。
import sqlite3  # 提供 Connection/Row/Error 等
# uuid：new_id() 用 uuid4 生成随机业务 ID，避免多进程/多实例下自增 ID 冲突。
import uuid  # uuid4() 基于随机数，不依赖数据库序列
# contextmanager：把 db() 生成器函数变成 “with db() as conn:” 可用的上下文管理器。
from contextlib import contextmanager  # 装饰器，负责 __enter__/__exit__ 协议
# datetime/timedelta/timezone：构造东八区时区、生成当前时间、做时间加减。
from datetime import datetime, timedelta, timezone  # 标准库时间类型
# typing：仅用于类型注解，帮助 IDE/读者理解参数和返回值；运行时不产生额外对象。
from typing import Any, Iterable  # Any=任意类型；Iterable=可迭代（list/tuple/生成器）

# 从同包 config.py 读取三个数据库配置：
#   DB_BACKEND：'sqlite' 或 'postgres'，决定 connect() 走哪条分支；
#   DB_PATH：SQLite 文件路径（pathlib.Path，sqlite3.connect 可直接接受）；
#   DATABASE_URL：PostgreSQL 连接串（含用户名/密码/主机/库名）。
# 只导入配置值而不是整个 config 模块，避免循环导入，也让依赖关系一目了然。
from .config import DATABASE_URL, DB_BACKEND, DB_PATH  # 相对导入：当前 app 包内的 config

# 常量 TZ：全应用统一时区 = UTC+8。
# 为什么不用 UTC：产品面向中文用户，报告/日志/截止时间都按北京时间展示；
# 为什么用固定偏移而不是 zoneinfo：中国不实行夏令时，固定 +8 足够且无第三方依赖。
# timezone(timedelta(hours=8)) 生成的是“带固定偏移的 tzinfo”，可安全用于 datetime.now()。
TZ = timezone(timedelta(hours=8))  # 东八区；iso() 输出会带 +08:00 后缀


# now()：返回“带时区”的当前时间。
# 为什么不用 datetime.utcnow()：它返回 naive datetime（没有 tzinfo），和带时区的时间比较/相减会报错，
# 且 Python 3.12 起已被标记为弃用；datetime.now(TZ) 明确表示“北京时间”。
def now() -> datetime:  # -> datetime 是返回值类型注解，便于调用方知道拿到什么
    # 每次调用都重新取系统时钟；返回的对象不可变，调用方可以放心传递。
    return datetime.now(TZ)  # 例：2026-11-06T17:40:28+08:00（含时区）


# iso()：把 datetime 转成数据库/JSON 里统一的字符串格式。
# 参数 dt 可为 None：不传时用 now()，方便“记录当前时间”的常见场景。
# timespec="seconds" 表示精确到秒、丢弃微秒：数据库里所有时间字符串长度一致，
# 字符串排序 = 时间排序；也避免同一秒内多次写入产生看似不同的时间。
def iso(dt: datetime | None = None) -> str:  # dt 默认 None；返回 str
    # `dt or now()`：datetime 对象在布尔判断中恒为 True，所以等价于“dt 不为 None 就用 dt，
    # 否则取当前时间”。.isoformat() 会保留 tzinfo 的 +08:00 偏移，便于跨时区系统正确解析。
    return (dt or now()).isoformat(timespec="seconds")  # 例：'2026-11-06T17:40:28+08:00'


# new_id()：生成带业务前缀的短 ID，例如 new_id("doc") -> "doc_9f2c1a7b3e"。
# 为什么不用数据库自增：文档/题目/会话可能先在内存或队列中创建，需要提前拿到 ID；
# 多实例部署时自增序列也可能冲突。uuid4 的随机性可避免这些问题。
# prefix 由调用方传入（doc/node/q/ses/...），让日志和排障一眼看出 ID 属于哪类对象。
def new_id(prefix: str) -> str:  # prefix：短的业务前缀，不要带下划线
    # uuid.uuid4().hex 是 32 位十六进制字符串；[:10] 只取前 10 位（40 bit 随机空间）。
    # 10 位在单应用规模下碰撞概率极低，又比完整 UUID 短，适合放进 URL/日志。
    # f-string 拼成 “前缀_随机串”，下划线是约定分隔符，不是必须。
    return f"{prefix}_{uuid.uuid4().hex[:10]}"  # 例：'doc_3f8a1c9d02'


# =============================================================================
# 常量 SCHEMA：SQLite 版完整建表脚本（三引号字符串，原样执行）
# =============================================================================
# 为什么是一整个字符串：
#   init_db() 里用 conn.executescript(SCHEMA) 一次性提交给 SQLite；SQLite 的 executescript
#   会先提交当前事务，再按分号逐条执行，适合“启动时确保表都存在”的场景。
# 为什么这里不能写 # 注释：
#   三引号内的每个字符都会成为 SQL 文本；# 不是 SQL 注释（SQL 用 -- 或 /* ... */），
#   写进去会导致语法错误。因此本注释版把逐表说明集中放在字符串上方，SQL 本身一字未改。
#
# 一、通用设计约定
#   1) 主键：业务表用 TEXT 存业务 ID（new_id() 生成），跨 SQLite/PostgreSQL 行为一致，
#      也方便把 ID 直接放进 URL、日志、JSON；只有纯日志表用 INTEGER 自增。
#   2) 时间：全部 TEXT，存 iso() 生成的 ISO-8601 字符串（东八区，精确到秒）。
#      好处：两种数据库都支持 TEXT 比较/排序；坏处：不能用数据库函数做时区换算，
#      时间计算要在 Python 里做（见 now()/iso()）。
#   3) JSON：SQLite 没有方便的数组/对象列，统一用 TEXT 存 JSON 字符串，
#      默认值写作 '[]'（空数组）或 '{}'（空对象）；读取时必须过 load_json()，
#      写入时用 dump()。这样即使某行是历史脏数据，也不会让接口崩溃。
#   4) 布尔：用 INTEGER 0/1 表示（如 passed、grounded、fallback），SQLite 没有独立 BOOLEAN。
#   5) 金额/分数：REAL（浮点）；计数/序号：INTEGER；文本：TEXT。
#   6) 软删除：document.deleted_at 为 NULL 表示未删除；查询时要自己加
#      “deleted_at IS NULL” 条件。其他表目前是物理删除。
#   7) 外键：这里不声明 FOREIGN KEY，connect() 还显式 PRAGMA foreign_keys=OFF。
#      原因：历史数据/导入顺序/删文档时的级联策略由业务层控制；代价是数据库不帮你保证
#      引用完整性，写代码时要自己保证 doc_id/node_id 有效。
#   8) 幂等：所有建表/建索引都带 IF NOT EXISTS，应用重启不会报“已存在”。
#   9) PostgreSQL 兼容：_pg_schema() 会删除 PRAGMA 行、把 AUTOINCREMENT 换成 BIGSERIAL；
#      其余 TEXT/INTEGER/REAL/IF NOT EXISTS 语法两边通用。
#  10) 索引命名：idx_<表>_<用途>，方便 EXPLAIN 时一眼看出命中哪个索引。
#
# 二、表结构总览（按业务域分组；括号里是类型/默认值要点）
#   【文档与解析结构】
#   document：文档主表。一行 = 一篇上传的论文/报告。
#     doc_id(PK) 文档 ID；tenant_id/user_id 归属（权限过滤靠它们）；
#     title 标题；doc_type 类型（research_paper/review/policy/news/other）；
#     source_uri 原始链接（URL 导入时用）；file_name/mime_type/size_bytes 文件元信息；
#     page_count 页数；sha256 内容哈希（可用于秒传/去重）；status 解析状态机；
#     doc_version 结构版本号；language 语言；quality_score/quality_level/quality_issues
#     质量评分、等级与问题清单（JSON）；stats 统计信息（JSON）；error 失败原因；
#     created_at/updated_at 创建/更新时间；deleted_at 软删除时间（NULL=未删除）。
#   node：文档结构树。一行 = 章节/小节/段落等节点。
#     node_id(PK)；doc_id 所属文档；parent_id 父节点（根节点为 NULL）；
#     level 层级（1=章、2=节...）；node_type 节点类型；order_no 同级排序号；
#     title/text 标题/正文；page_start/page_end 起止页；bbox 版面坐标（JSON）；
#     confidence 解析置信度；path 物化路径（如 '/1/3/2'，便于查子树）；
#     char_count 字符数；order_index 全文档顺序号。
#   chunk：检索切块。一行 = 一段用于向量/关键词检索的文本。
#     chunk_id(PK)；doc_id/node_id 来源；text 切块文本；token_count token 数；order_no 顺序。
#   knowledge_point：知识点。一行 = 从文档中抽取的一个概念/方法/结论。
#     kp_id(PK)；doc_id/node_id 来源；name 名称；kp_type 类型；definition 定义；
#     importance 重要度 0~1；difficulty 难度 1~5；evidence_ids 证据 ID 列表（JSON）；
#     prerequisites 先修知识点列表（JSON）；order_no 排序。
#   evidence：证据/引文。用于“答案有据可查”，报告和判题会引用。
#     evidence_id(PK)；doc_id/node_id 来源；page_no 页码；quote 原文摘录；
#     char_start/char_end 字符偏移（前端高亮用）；strength 证据强度；context 上下文。
#
#   【题库与练习】
#   question：题目主表。一行 = 一道题。
#     q_id(PK)；doc_id 所属文档；kp_id 关联知识点（可空）；node_id 来源节点；
#     q_type 题型（single_choice/multiple_choice/true_false/fill_blank/short_answer）；
#     bloom_level 布鲁姆认知层级；difficulty 难度；stem 题干；
#     options 选项（JSON）；answer 标准答案（JSON）；rubric 评分细则（JSON）；
#     evidence 证据引用（JSON）；hints 提示列表（JSON）；time_limit_sec 限时；
#     hint_levels 提示层数；status 状态（active/disabled...）；version 题目版本；
#     source 来源（llm/manual...）；created_at 创建时间。
#   question_set：题卷/题集。一行 = 一次组卷。
#     set_id(PK)；doc_id 文档；tenant_id 租户；title 标题；difficulty 难度策略；
#     question_ids 题目 ID 列表（JSON）；question_count 题目数；created_by 创建人；created_at。
#
#   【答题会话与报告】
#   session：一次练习/测评会话。一行 = 学生的一次完整答题过程。
#     session_id(PK)；doc_id/user_id/tenant_id 归属；mode 模式（guided/exam/defense）；
#     state 状态（ready/active/finished...）；total_questions 计划题数；
#     plan 出题计划（JSON）；current_turn_id 当前轮次；score 总分；
#     theta_before/theta_after 作答前后的能力值（IRT 简化模型，默认 500）；
#     chapter_ability 分章节能力（JSON）；skipped_count/hint_count 跳过/提示次数；
#     started_at/finished_at 开始/结束；expires_at 过期时间（防止会话永久占用）。
#   turn：会话中的一轮问答。一行 = 一道题的作答记录。
#     turn_id(PK)；session_id/q_id 关联；seq_no 第几题；answer 学生答案（JSON）；
#     evaluation 判题结果（JSON）；score 得分；status（asking/answered...）；
#     hint_level 已用提示等级；created_at/answered_at；latency_ms 答题耗时（毫秒）。
#   report：学习报告。一行 = 一次会话结束后的总结。
#     report_id(PK)；session_id 关联；summary 总评（JSON）；
#     section_mastery 章节掌握度（JSON）；bloom_distribution 认知层级分布（JSON）；
#     wrong_questions 错题列表（JSON）；strengths/weaknesses 优势/薄弱点（JSON）；
#     recommendations 学习建议（JSON）；generated_at 生成时间。
#
#   【任务、计费与幂等】
#   task：后台任务表。解析/生成等耗时操作先写一行 queued，再由 worker 更新进度。
#     task_id(PK)；type 任务类型；status/stage 状态与阶段；progress 0~100；
#     document_id/session_id 关联业务对象；result 结果（JSON 文本）；error 错误信息；
#     created_at/updated_at 时间。
#   usage_log：LLM 用量流水。id 自增；ts 时间；kind 调用类型；model 模型名；
#     prompt_tokens/completion_tokens token 数；credits 计费点数；document_id/session_id 关联。
#   idempotency：接口幂等表。key(PK) 幂等键；endpoint 接口标识；response 首次响应（JSON）；
#     created_at 时间。重复 POST 时先查这张表，避免重复扣费/重复创建。
#
#   【用户、配置与审计】
#   app_user：账号表。user_id(PK)；username 唯一登录名；password_hash 密码哈希（绝不存明文）；
#     role 角色（admin/teacher/user）；display_name/email/phone 资料；
#     tenant_id 租户；status 状态（active/disabled）；created_at/last_login_at。
#   app_setting：键值配置表。key(PK)；value 值（JSON/字符串）；updated_at 更新时间。
#   activity_event：用户行为审计。id 自增；ts 时间；user_id/username/tenant_id 操作者；
#     event_type 事件类型；detail 详情（JSON）；ip/user_agent 来源信息。
#   ai_audit：AI 调用审计。id 自增；ts 时间；user_id/document_id/session_id 关联；
#     kind 调用类型；model 模型；grounded 是否有证据支撑（0/1）；fallback 是否降级（0/1）；
#     latency_ms 延迟；detail 详情（JSON）。
#
#   【V1.1 质量与验收】
#   quality_run：一次评测运行。run_id(PK)；version 版本；suite 测试集；status 状态；
#     metrics 指标（JSON）；summary 摘要（JSON）；created_by/created_at/finished_at。
#   quality_case：评测用例结果。id 自增；run_id 所属运行；case_id 用例 ID；doc_id 文档；
#     metric 指标名；expected/actual 期望/实际值；passed 是否通过（0/1）；detail 详情（JSON）。
#   quality_review：人工复核。review_id(PK)；target_type/target_id 复核对象；
#     reviewer 复核人；scores 评分（JSON）；notes 备注；created_at。
#   quality_defect：缺陷台账。defect_id(PK)；severity 严重级别（P0~P3，默认 P2）；
#     area 模块；title 标题；detail 详情（JSON）；status 状态（open/fixed...）；
#     run_id 来源运行；created_at/updated_at。
#
#   【V1.2 教学闭环与阅读器】
#   follow_up：追问记录。follow_up_id(PK)；session_id/turn_id/q_id 关联；
#     kind 类型（clarify/...）；question/answer 问答；score 得分；feedback 反馈；
#     hit/miss 命中/遗漏点（JSON）；status 状态；created_at/answered_at。
#   paper_assignment：作业布置。assignment_id(PK)；paper_id/doc_id 题卷/文档；
#     teacher_id/tenant_id 布置者；title 标题；assignee_id/assignee_name 学生；
#     instructions 说明；due_at 截止；status 状态；session_id/score/report_id 关联作答；
#     teacher_comment 教师评语；created_at/started_at/submitted_at/reviewed_at 生命周期时间。
#   reader_state：阅读器位置。复合主键 (user_id, doc_id)；page_no 页码；
#     scroll_ratio 页内滚动比例 0~1；zoom 缩放；updated_at 更新时间。
#   reader_note：阅读笔记。note_id(PK)；user_id/doc_id 归属；page_no 页码；
#     quote 划线原文；note 笔记内容；created_at/updated_at。
#   document_version：文档结构版本快照。version_id(PK)；doc_id 文档；version_no 版本号；
#     snapshot 结构快照（JSON）；node_count 节点数；reason 变更原因；created_by/created_at。
#
# 三、索引说明（为什么建这些索引）
#   - idx_document_tenant(tenant_id, created_at DESC)：租户首页按时间倒序列文档；
#   - idx_node_doc(doc_id, parent_id, order_no)：按文档+父节点取子节点并按顺序展示；
#   - idx_node_path(doc_id, path)：用 path 前缀查整棵子树；
#   - idx_chunk_node(node_id)：按节点取切块；
#   - idx_kp_doc(doc_id, node_id)：按文档/节点取知识点；
#   - idx_evidence_node(node_id, page_no)：按节点+页码取证据，便于引用定位；
#   - idx_question_doc(doc_id, status)：按文档筛启用题目；
#   - idx_question_set_doc(doc_id, created_at DESC)：文档的题卷列表；
#   - idx_turn_session(session_id, seq_no)：按会话取答题记录并按题号排序；
#   - idx_report_session(session_id)：按会话取报告；
#   - idx_activity_ts / idx_activity_user：审计按时间、按用户查询；
#   - idx_ai_audit_ts：AI 审计按时间查询；
#   - idx_quality_* / idx_assignment_* / idx_reader_note_doc / idx_document_version_*：
#     分别服务质量管理、作业列表、笔记列表、版本历史；
#   - app_user.email/phone 的唯一索引不在 SCHEMA 里，而是在 init_db() 迁移后创建，
#     因为旧库可能还没有这两列；使用部分索引（WHERE ...<>''）允许多个空值。
# =============================================================================
SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS document (
    doc_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    doc_type TEXT NOT NULL DEFAULT 'other',
    source_uri TEXT DEFAULT '',
    file_name TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT 'application/pdf',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    page_count INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'uploaded',
    doc_version INTEGER NOT NULL DEFAULT 1,
    language TEXT NOT NULL DEFAULT 'zh-CN',
    quality_score REAL NOT NULL DEFAULT 0,
    quality_level TEXT NOT NULL DEFAULT 'review',
    quality_issues TEXT NOT NULL DEFAULT '[]',
    stats TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_document_tenant ON document(tenant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS node (
    node_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    parent_id TEXT,
    level INTEGER NOT NULL,
    node_type TEXT NOT NULL,
    order_no INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    page_start INTEGER NOT NULL DEFAULT 1,
    page_end INTEGER NOT NULL DEFAULT 1,
    bbox TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    path TEXT NOT NULL DEFAULT '/',
    char_count INTEGER NOT NULL DEFAULT 0,
    order_index INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_node_doc ON node(doc_id, parent_id, order_no);
CREATE INDEX IF NOT EXISTS idx_node_path ON node(doc_id, path);

CREATE TABLE IF NOT EXISTS chunk (
    chunk_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    order_no INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunk_node ON chunk(node_id);

CREATE TABLE IF NOT EXISTS knowledge_point (
    kp_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    name TEXT NOT NULL,
    kp_type TEXT NOT NULL,
    definition TEXT NOT NULL DEFAULT '',
    importance REAL NOT NULL DEFAULT 0.5,
    difficulty INTEGER NOT NULL DEFAULT 3,
    evidence_ids TEXT NOT NULL DEFAULT '[]',
    prerequisites TEXT NOT NULL DEFAULT '[]',
    order_no INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_kp_doc ON knowledge_point(doc_id, node_id);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    page_no INTEGER NOT NULL DEFAULT 1,
    quote TEXT NOT NULL,
    char_start INTEGER NOT NULL DEFAULT 0,
    char_end INTEGER NOT NULL DEFAULT 0,
    strength TEXT NOT NULL DEFAULT 'strong',
    context TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_evidence_node ON evidence(node_id, page_no);

CREATE TABLE IF NOT EXISTS question (
    q_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    kp_id TEXT,
    node_id TEXT NOT NULL,
    q_type TEXT NOT NULL,
    bloom_level TEXT NOT NULL,
    difficulty INTEGER NOT NULL DEFAULT 3,
    stem TEXT NOT NULL,
    options TEXT NOT NULL DEFAULT '[]',
    answer TEXT NOT NULL DEFAULT '{}',
    rubric TEXT NOT NULL DEFAULT '[]',
    evidence TEXT NOT NULL DEFAULT '[]',
    hints TEXT NOT NULL DEFAULT '[]',
    time_limit_sec INTEGER NOT NULL DEFAULT 90,
    hint_levels INTEGER NOT NULL DEFAULT 3,
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'llm',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_question_doc ON question(doc_id, status);

CREATE TABLE IF NOT EXISTS question_set (
    set_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'tnt_local',
    title TEXT NOT NULL,
    difficulty TEXT NOT NULL DEFAULT 'mixed',
    question_ids TEXT NOT NULL DEFAULT '[]',
    question_count INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_question_set_doc ON question_set(doc_id, created_at DESC);

CREATE TABLE IF NOT EXISTS session (
    session_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready',
    total_questions INTEGER NOT NULL DEFAULT 12,
    plan TEXT NOT NULL DEFAULT '{}',
    current_turn_id TEXT,
    score REAL,
    theta_before REAL NOT NULL DEFAULT 500,
    theta_after REAL NOT NULL DEFAULT 500,
    chapter_ability TEXT NOT NULL DEFAULT '{}',
    skipped_count INTEGER NOT NULL DEFAULT 0,
    hint_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turn (
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    q_id TEXT NOT NULL,
    seq_no INTEGER NOT NULL,
    answer TEXT NOT NULL DEFAULT '{}',
    evaluation TEXT NOT NULL DEFAULT '{}',
    score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'asking',
    hint_level INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    answered_at TEXT,
    latency_ms INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_turn_session ON turn(session_id, seq_no);

CREATE TABLE IF NOT EXISTS report (
    report_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}',
    section_mastery TEXT NOT NULL DEFAULT '[]',
    bloom_distribution TEXT NOT NULL DEFAULT '{}',
    wrong_questions TEXT NOT NULL DEFAULT '[]',
    strengths TEXT NOT NULL DEFAULT '[]',
    weaknesses TEXT NOT NULL DEFAULT '[]',
    recommendations TEXT NOT NULL DEFAULT '[]',
    generated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_report_session ON report(session_id);

CREATE TABLE IF NOT EXISTS task (
    task_id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    document_id TEXT,
    session_id TEXT,
    result TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    credits REAL NOT NULL DEFAULT 0,
    document_id TEXT,
    session_id TEXT
);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    endpoint TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_user (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    display_name TEXT NOT NULL DEFAULT '',
    email TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT 'tnt_local',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS app_setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_activity_ts ON activity_event(ts DESC);
CREATE INDEX IF NOT EXISTS idx_activity_user ON activity_event(user_id, ts DESC);

CREATE TABLE IF NOT EXISTS ai_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    document_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    grounded INTEGER NOT NULL DEFAULT 1,
    fallback INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ai_audit_ts ON ai_audit(ts DESC);

-- V1.1 质量与验收：评测运行、用例结果、人工复核与缺陷台账
CREATE TABLE IF NOT EXISTS quality_run (
    run_id TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    suite TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    metrics TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_quality_run_created ON quality_run(created_at DESC);

CREATE TABLE IF NOT EXISTS quality_case (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    doc_id TEXT NOT NULL DEFAULT '',
    metric TEXT NOT NULL,
    expected TEXT NOT NULL DEFAULT '',
    actual TEXT NOT NULL DEFAULT '',
    passed INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_quality_case_run ON quality_case(run_id, metric);

CREATE TABLE IF NOT EXISTS quality_review (
    review_id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    scores TEXT NOT NULL DEFAULT '{}',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_review_target ON quality_review(target_type, target_id);

CREATE TABLE IF NOT EXISTS quality_defect (
    defect_id TEXT PRIMARY KEY,
    severity TEXT NOT NULL DEFAULT 'P2',
    area TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'open',
    run_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quality_defect_status ON quality_defect(status, severity);

-- V1.2 教学闭环：追问持久化、题卷布置、阅读器状态/笔记、结构版本
CREATE TABLE IF NOT EXISTS follow_up (
    follow_up_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    q_id TEXT,
    kind TEXT NOT NULL DEFAULT 'clarify',
    question TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    score REAL,
    feedback TEXT NOT NULL DEFAULT '',
    hit TEXT NOT NULL DEFAULT '[]',
    miss TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'created',
    created_at TEXT NOT NULL,
    answered_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_follow_up_session ON follow_up(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_follow_up_turn ON follow_up(turn_id, kind);

CREATE TABLE IF NOT EXISTS paper_assignment (
    assignment_id TEXT PRIMARY KEY,
    paper_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    teacher_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'tnt_local',
    title TEXT NOT NULL DEFAULT '',
    assignee_id TEXT NOT NULL,
    assignee_name TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'assigned',
    session_id TEXT,
    score REAL,
    report_id TEXT,
    teacher_comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT,
    submitted_at TEXT,
    reviewed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_assignment_teacher ON paper_assignment(teacher_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_assignment_assignee ON paper_assignment(assignee_id, status);

CREATE TABLE IF NOT EXISTS reader_state (
    user_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    page_no INTEGER NOT NULL DEFAULT 1,
    scroll_ratio REAL NOT NULL DEFAULT 0,
    zoom REAL NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, doc_id)
);

CREATE TABLE IF NOT EXISTS reader_note (
    note_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    page_no INTEGER NOT NULL DEFAULT 1,
    quote TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reader_note_doc ON reader_note(user_id, doc_id, page_no);

CREATE TABLE IF NOT EXISTS document_version (
    version_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    version_no INTEGER NOT NULL,
    snapshot TEXT NOT NULL DEFAULT '[]',
    node_count INTEGER NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_document_version_no ON document_version(doc_id, version_no);
CREATE INDEX IF NOT EXISTS idx_document_version_doc ON document_version(doc_id, version_no DESC);
"""  # SCHEMA 结束；字符串内的分号由 executescript 逐条拆分执行


# _pg_sql()：把 SQLite 风格的 “?” 占位符转换成 psycopg 需要的 “%s”。
# 背景：业务层所有 SQL 都按 sqlite3 的习惯写 “WHERE doc_id=?”；PostgreSQL 驱动只认 “%s”。
# 为什么不能用 sql.replace("?", "%s")：如果字符串字面量里含有问号（如 title='什么?'），
# 会被误替换，导致 SQL 语义改变甚至报错。所以要逐字符扫描，识别单引号/双引号字符串，
# 只在“字符串外部”替换问号。
# 支持的转义：SQL 标准用两个连续引号表示一个引号（'It''s' / "a""b"），扫描时要跳过。
# 局限：不处理 PostgreSQL 的 $tag$...$tag$ 美元引用、反斜杠转义和 SQL 注释中的问号；
# 本项目的 SQL 都是代码内常量、写法简单，因此足够用。不要把用户输入拼进 SQL 再调用它。
def _pg_sql(sql: str) -> str:
    """把 sqlite 风格 ? 占位符转成 psycopg 的 %s，跳过字符串字面量。"""
    # out 收集输出字符；用 list.append + join 而不是字符串反复相加，避免 O(n²)。
    out: list[str] = []  # 类型注解：元素为 str 的列表；运行时只是空列表
    quote = ""  # 当前所在的引号类型：''=不在字符串内，' 或 " = 在对应字符串内
    i = 0  # 手动维护下标；因为要一次看两个字符（处理 '' 转义），不能用简单的 for ch in sql
    while i < len(sql):  # 逐字符扫描；i 在循环体内可能一次前进 1 或 2
        ch = sql[i]  # 当前字符
        if quote:  # 真值判断：quote 非空字符串 => 当前在字符串字面量内部
            out.append(ch)  # 字符串内部原样保留（包括问号，不能被替换）
            if ch == quote:  # 遇到与开启引号相同的字符，可能是字符串结束，也可能是转义
                if i + 1 < len(sql) and sql[i + 1] == quote:  # 连续两个相同引号 => SQL 转义的一个引号
                    out.append(sql[i + 1])  # 把第二个引号也原样输出
                    i += 2  # 两个字符都已处理，跳过它们
                    continue  # 继续下一轮，不执行后面的 i += 1
                quote = ""  # 单个引号 => 字符串结束，退出“字符串内部”状态
            i += 1  # 已处理当前字符，前进一格
            continue  # 字符串内部的分支处理完毕，回到 while 判断
        if ch in ("'", '"'):  # 字符串外部遇到引号 => 进入字符串
            quote = ch  # 记住是单引号还是双引号，之后要匹配同一个
            out.append(ch)  # 引号本身保留
        elif ch == "?":  # 字符串外部遇到 SQLite 占位符
            out.append("%s")  # 替换成 psycopg 的占位符
        else:  # 其他普通字符
            out.append(ch)  # 原样保留
        i += 1  # 普通字符/引号/占位符都只前进一格
    return "".join(out)  # 拼成最终 SQL；join 一次分配，效率高


# _pg_schema()：把 SQLite 版 SCHEMA 转成 PostgreSQL 可执行的版本。
# 只做两处替换：
#   1) 删除 “PRAGMA journal_mode=WAL;” —— PostgreSQL 没有 PRAGMA，WAL 是内置机制；
#   2) 把 “INTEGER PRIMARY KEY AUTOINCREMENT” 换成 “BIGSERIAL PRIMARY KEY” ——
#      PostgreSQL 的自增列类型，BIGSERIAL 会自动创建序列并设置默认值。
# 注意：这是字符串级替换，不是 SQL 解析器；如果未来 SQL 里出现同名文本（例如注释/字符串），
# 也会被替换。当前 SCHEMA 中只在 usage_log/activity_event/ai_audit/quality_case 的自增主键处出现。
def _pg_schema() -> str:
    schema = SCHEMA.replace("PRAGMA journal_mode=WAL;", "")  # 去掉 SQLite 专有的 WAL 设置
    return schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")  # 自增主键语法转换


# PgConnection：对 psycopg 连接做一层“最小 sqlite3 兼容包装”。
# 目的：业务层已经按 sqlite3 的 API 写了大量代码（conn.execute(sql, params).fetchone()、
# conn.executescript(...)、commit/rollback/close），如果为 PostgreSQL 重写所有业务代码，
# 改动面和风险都很大；这里用适配器把差异收敛在一个类里。
# 它不是完整的 DB-API 代理：没有 cursor()、没有 with 语句支持、没有 rowcount 属性转发；
# 只实现 db.py 和业务层实际用到的这几个方法。
class PgConnection:
    # ↑ 原有 docstring：说明“psycopg 连接的轻量 sqlite 兼容包装，业务层继续用 ? 占位符”。
    """psycopg 连接的轻量 sqlite 兼容包装，业务层继续使用 ? 占位符。"""

    # 构造时建立真实连接；url 形如 postgresql://user:pass@host:port/dbname
    def __init__(self, url: str):
        import psycopg  # 延迟导入：只有 PostgreSQL 模式才需要安装 psycopg，SQLite 部署无需依赖
        from psycopg.rows import dict_row  # 让查询结果直接是 dict，而不是 tuple
        # row_factory=dict_row：等价于 sqlite3.Row + dict(row)，但由驱动直接完成；
        # connect_timeout=10：网络/数据库不可用时 10 秒内失败，避免启动线程一直挂起。
        self.raw = psycopg.connect(url, row_factory=dict_row, connect_timeout=10)

    # execute()：接收 SQLite 风格 SQL 和参数，转成 psycopg 风格后执行。
    # 返回值是 psycopg 的 cursor，支持 .fetchone()/.fetchall()/.rowcount，保持与 sqlite3 类似。
    def execute(self, sql: str, params: Iterable[Any] = ()):
        return self.raw.execute(_pg_sql(sql), tuple(params))  # tuple(params)：把 list/生成器统一成元组

    # executescript()：模拟 sqlite3 的 executescript，把整段脚本按分号拆成单条语句执行。
    # 注意：这里是简单的 split(";")，不解析字符串内的分号；本项目 SCHEMA 的字符串字面量里没有分号，
    # 所以安全。若未来 SQL 含分号字面量，应改用更可靠的拆分方式。
    def executescript(self, script: str) -> None:
        for statement in script.split(";"):  # 逐条语句
            if statement.strip():  # 跳过空语句（例如末尾分号后的空白）
                self.raw.execute(_pg_sql(statement))  # 同样要把 ? 转成 %s；DDL 通常没有参数

    # commit()：提交当前事务。psycopg 默认不是 autocommit，不提交则数据不会真正落库。
    def commit(self) -> None:
        self.raw.commit()  # 把本连接上所有未提交的 INSERT/UPDATE/DDL 持久化

    # rollback()：回滚当前事务，撤销自上次 commit 以来的修改。
    def rollback(self) -> None:
        self.raw.rollback()  # 异常路径由 db() 调用，保证不留下半截事务

    # close()：关闭连接，释放服务端会话/套接字；关闭前未提交的事务会自动回滚。
    def close(self) -> None:
        self.raw.close()  # 幂等性由驱动保证；重复 close 通常无副作用


# connect()：根据配置创建并返回一个数据库连接。
# 这是全文件唯一的“连接工厂”：db()、init_db() 都通过它拿连接，
# 因此后端切换只需要改这里，业务代码完全无感。
def connect():
    if DB_BACKEND == "postgres":  # config.py 已把环境变量转小写，所以可直接比较
        return PgConnection(DATABASE_URL)  # PostgreSQL：用适配器包装 psycopg 连接
    # SQLite 分支：
    #   timeout=30：获取写锁最多等 30 秒（秒），超时抛 sqlite3.OperationalError: database is locked；
    #   check_same_thread=False：允许连接对象跨线程使用。FastAPI 的同步路由可能在线程池的不同线程
    #   执行，但这并不表示多个线程能同时安全共享同一连接——db() 每次新建连接，用完即关，
    #   所以实际不会并发共享。关闭该检查只是避免 sqlite3 的“同线程”断言误报。
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    # row_factory=sqlite3.Row：查询结果支持 r["列名"] 和 r[0] 两种取值方式；
    # fetch_all/fetch_one 再把它转成普通 dict。没有这行，dict(r) 会失败（tuple 没有列名）。
    conn.row_factory = sqlite3.Row
    # 关闭外键约束检查。SQLite 默认本来就是 OFF，这里显式写出来，提醒读者：
    # 表之间没有数据库级外键，删除文档时关联的 node/chunk 等需要业务层自己清理。
    conn.execute("PRAGMA foreign_keys=OFF")
    # busy_timeout=30000 毫秒：遇到其他连接持有写锁时，最多重试等待 30 秒再报错；
    # 与 connect(timeout=30) 呼应，减少高并发下 “database is locked” 的概率。
    conn.execute("PRAGMA busy_timeout=30000")
    return conn  # 返回原始 sqlite3.Connection，调用方负责 commit/rollback/close


# init_db()：应用启动时调用一次，确保数据库“可用且结构最新”。
# 调用点：main.py 在创建 FastAPI 应用前执行 init_db()；PostgreSQL 模式下随后还会执行
# platform/migrations.migrate() 做平台层迁移。
# 设计原则：幂等——重复运行不会报错；所有建表/建列/建索引都先判断是否存在。
# 并发：多个进程同时启动时，PostgreSQL 用 advisory lock 串行化；SQLite 依赖 busy_timeout
# 和 IF NOT EXISTS，极端情况下仍可能有一个进程因锁等待超时而启动失败，重试即可。
def init_db() -> None:
    conn = connect()  # 先拿连接；如果连接失败，异常直接向上抛，不会进入 try（也无需 close）
    try:  # try/finally：无论建表成功还是中途异常，最后都要关闭连接
        if DB_BACKEND == "postgres":  # PostgreSQL 分支：语法/迁移方式与 SQLite 不同
            # pg_advisory_xact_lock(key)：事务级咨询锁，key=724242 是本应用约定的常量。
            # 作用：多个实例同时启动时，只有一个能进入建表/迁移区，另一个阻塞等待，
            # 避免 “CREATE TABLE 已存在/ALTER 重复列” 的竞态。事务结束（commit/rollback）自动释放。
            # 为什么用 xact 锁而不是会话锁：不需要手动 unlock，连接异常关闭也会自动释放。
            conn.execute("SELECT pg_advisory_xact_lock(724242)")
            conn.executescript(_pg_schema())  # 执行转换后的 PostgreSQL 建表脚本（IF NOT EXISTS，幂等）
            # 轻量迁移清单：(表名, 列名, 列定义)。
            # 这些列是后续版本新增的，老库的 CREATE TABLE IF NOT EXISTS 不会补列，
            # 所以要逐列检查 information_schema，再 ALTER TABLE。
            # 表名/列名/DDL 都是代码内常量，f-string 拼接安全；值不能参数化列名。
            for table, column, ddl in (  # 元组列表：每项描述一个需要补的列
                ("app_user", "email", "TEXT DEFAULT ''"),  # 用户邮箱（注册资料）
                ("app_user", "phone", "TEXT DEFAULT ''"),  # 用户手机号
                ("document", "source_uri", "TEXT DEFAULT ''"),  # 文档原始链接（URL 导入）
                ("session", "assignment_id", "TEXT DEFAULT ''"),  # 会话关联的作业 ID
            ):  # 清单结束；下面逐项检查
                # 查 information_schema.columns：PostgreSQL 的系统目录，记录所有表/列。
                # 参数用 ?，PgConnection 会转成 %s；返回一行表示列已存在，None 表示不存在。
                exists = conn.execute(  # 查询目标列是否存在
                    "SELECT 1 FROM information_schema.columns WHERE table_name=? AND column_name=?",
                    (table, column),  # 两个参数分别对应上面的两个 ?
                ).fetchone()  # 只取第一行；存在时是包含 1 的 dict，不存在是 None
                if not exists:  # 列不存在才 ALTER，避免重复列报错
                    # ADD COLUMN 只加列，不改已有行；DEFAULT '' 让老数据读出来是空串而不是 NULL。
                    # 注意：PostgreSQL 的 ALTER TABLE 在事务内执行，最后统一 commit。
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")  # 表名/列名来自上方固定清单，安全
            conn.commit()  # 提交建表+迁移，同时释放 pg_advisory_xact_lock
            return  # PostgreSQL 分支结束，跳过下面的 SQLite 逻辑
        # SQLite：一次性执行完整建表脚本。sqlite3 的 executescript 会先提交当前事务，
        # 再按分号逐条执行；这里没有参数，DDL 都是固定文本。
        conn.executescript(SCHEMA)  # 执行 SQLite 建表脚本（幂等）
        # ↓ 原有注释保留：轻量迁移——给旧库补注册资料字段。
        # 轻量迁移：给旧库补注册资料字段
        # PRAGMA table_info(app_user) 返回每列一行：(cid, name, type, notnull, dflt_value, pk)。
        # r[1] 是列名；用集合推导式收集成 {'user_id','username',...}，后续 O(1) 判断。
        columns = {r[1] for r in conn.execute("PRAGMA table_info(app_user)").fetchall()}  # 收集 app_user 现有列名
        # 老库可能缺这两个注册资料列；逐个检查，缺哪个补哪个。
        for name in ("email", "phone"):  # 遍历需要确认的两个列名
            if name not in columns:  # 只在缺列时 ALTER，保证幂等
                conn.execute(f"ALTER TABLE app_user ADD COLUMN {name} TEXT DEFAULT ''")  # 加列并给默认空串
        # 同样检查 document.source_uri（URL 导入功能新增的列）。
        doc_columns = {r[1] for r in conn.execute("PRAGMA table_info(document)").fetchall()}  # 收集 document 现有列名
        if "source_uri" not in doc_columns:  # 缺列才补
            conn.execute("ALTER TABLE document ADD COLUMN source_uri TEXT DEFAULT ''")  # 原始链接列
        # 检查 session.assignment_id（作业功能新增：会话属于哪次布置）。
        session_columns = {r[1] for r in conn.execute("PRAGMA table_info(session)").fetchall()}  # 收集 session 现有列名
        if "assignment_id" not in session_columns:  # 缺列才补
            conn.execute("ALTER TABLE session ADD COLUMN assignment_id TEXT DEFAULT ''")  # 作业 ID 列
        # 邮箱唯一索引：WHERE email IS NOT NULL AND email<>'' 是“部分索引”，
        # 只约束非空邮箱；多个用户都不填邮箱（空串/NULL）不会互相冲突。
        # 放在迁移之后创建，是因为旧库可能刚补上 email 列。
        # 如果历史数据已有重复邮箱，这里会抛 IntegrityError，需要先清洗数据再启动。
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_app_user_email ON app_user(email) WHERE email IS NOT NULL AND email<>''")  # 部分唯一索引：只约束非空邮箱
        # 手机号唯一索引：逻辑同上；空手机号不参与唯一性约束。
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_app_user_phone ON app_user(phone) WHERE phone IS NOT NULL AND phone<>''")  # 部分唯一索引：只约束非空手机号
        conn.commit()  # 提交 SQLite 的建表/迁移/索引；不提交则连接关闭时全部回滚
    finally:  # 无论成功、失败还是 return，都会执行
        conn.close()  # 关闭连接；SQLite 关闭未提交事务会回滚，PostgreSQL 同理


# db()：事务上下文管理器，业务层最常用的入口。
# 用法：
#     with db() as conn:
#         conn.execute("UPDATE ...", (...))
#         rows = conn.execute("SELECT ...").fetchall()
# 语义：
#   - 进入 with：调用 connect() 新建连接；
#   - 正常离开：自动 commit()，把这一块里的所有写操作作为一个事务提交；
#   - 代码块抛异常：rollback() 撤销整个事务，然后 raise 原样抛出，绝不吞异常；
#   - 无论哪条路径：finally 都 close()，避免连接泄漏。
# 为什么用 contextmanager：把“提交/回滚/关闭”模板代码集中一次，业务层不会忘记。
# 注意：
#   1) 每次 db() 都是新连接，不是全局单例；SQLite 下开销很小，PostgreSQL 下可后续换连接池。
#   2) 不要在 with 块里再开一个写 db()：SQLite 同一时刻只有一个写者，嵌套写容易锁等待；
#      需要跨库事务时应在同一连接上操作。
#   3) 只捕获 Exception，不捕获 KeyboardInterrupt/SystemExit 等 BaseException；
#      这类退出仍会走 finally 关闭连接（关闭时未提交事务自动回滚）。
@contextmanager  # 把生成器函数包装成上下文管理器（__enter__ 返回 yield 的值）
def db():  # 生成器：yield 之前是 __enter__，yield 之后是 __exit__ 的正常路径
    conn = connect()  # 进入 with 时立即建立连接；连接失败会直接抛给调用方
    try:  # 统一处理提交/回滚
        yield conn  # 把连接交给 with 块；块内代码在这里“暂停执行”
        conn.commit()  # with 块正常结束：提交本块所有写操作
    except Exception:  # 只兜业务异常；捕获后必须回滚，否则连接关闭时行为依赖驱动
        conn.rollback()  # 撤销本块内已执行但未提交的 SQL，保持数据一致性
        raise  # 原样重新抛出，保留原始异常类型/堆栈；不要改成 return 或吞掉
    finally:  # 成功/异常都走这里
        conn.close()  # 释放连接；这是 finally，保证不会泄漏


# fetch_all()：执行 SELECT，返回“字典列表”。
# 这是只读查询的便捷封装：内部用 db() 自动提交/关闭；查询本身不需要事务语义时也安全。
# 参数 params 用 ? 占位，传 tuple/list 均可；SQL 必须是代码内常量或参数化，不能拼接用户输入。
# 返回 [] 表示没有行；每行是普通 dict，可直接 json.dumps 返回给前端。
def fetch_all(sql: str, params: Iterable[Any] = ()) -> list[dict]:  # 默认空元组，表示无参数
    with db() as conn:  # 新建连接；离开 with 时 commit（SELECT 的 commit 是空操作）并 close
        rows = conn.execute(sql, tuple(params)).fetchall()  # 执行并取全部行；tuple 统一参数容器
    return [dict(r) for r in rows]  # sqlite3.Row / psycopg dict -> 普通 dict；列表推导保持顺序


# fetch_one()：执行 SELECT，返回第一行（dict）或 None。
# 注意：SQL 没有 ORDER BY 时“第一行”由数据库决定，调用方若要确定性结果应自己写 ORDER BY；
# 本函数也不会自动加 LIMIT 1，大结果集请显式 LIMIT，避免驱动取回多余行。
# None 表示“没有查到”，调用方要自行判断，不能直接下标访问。
def fetch_one(sql: str, params: Iterable[Any] = ()) -> dict | None:  # 可能没有结果，所以是 dict | None
    with db() as conn:  # 连接生命周期与 fetch_all 相同
        row = conn.execute(sql, tuple(params)).fetchone()  # 只取第一行；没有则 None
    return dict(row) if row else None  # 有行转 dict；None 直接返回 None（保持“查不到”的语义）


# execute()：执行 INSERT/UPDATE/DELETE/DDL 等写语句，不返回结果。
# 为什么返回 None：调用方通常只关心“是否成功”；需要自增 ID 时本层不暴露 lastrowid，
# 业务表都用 new_id() 预先生成 TEXT 主键，所以不需要。
# 事务：with db() 正常退出自动 commit；异常自动 rollback 并抛出。
def execute(sql: str, params: Iterable[Any] = ()) -> None:  # 明确返回 None，避免调用方误用返回值
    with db() as conn:  # 自动提交/回滚/关闭
        conn.execute(sql, tuple(params))  # 参数化执行；不要用 f-string 拼用户输入


# insert()：按 dict 自动拼 INSERT 语句的便捷函数。
# 例：insert("document", {"doc_id": "doc_x", "tenant_id": "tnt_local", ...})
#   -> INSERT INTO document (doc_id, tenant_id, ...) VALUES (?, ?, ...)
# 安全边界：
#   - 表名/列名来自调用方代码，必须是可信常量；它们不能用 ? 参数化，f-string 拼接有注入风险；
#   - 所有“值”都放进 params，由驱动转义，安全。
# 限制：data 不能为空（否则生成 INSERT INTO t () VALUES ()，SQL 语法错误）；
#       不做 ON CONFLICT/REPLACE，主键冲突会抛 IntegrityError，由调用方处理。
def insert(table: str, data: dict) -> None:  # table 可信表名；data 列名->值
    cols = ", ".join(data.keys())  # 列名列表；dict 保持插入顺序（Python 3.7+），与 values 顺序一致
    marks = ", ".join("?" for _ in data)  # 每个列一个占位符；只遍历 key，不关心 value
    with db() as conn:  # 自动事务
        conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))  # values 的顺序必须与 cols 完全一致；tuple(data.values()) 与 data.keys() 同序


# update()：按主键更新一行/多行的便捷函数。
# 例：update("document", "doc_id", "doc_x", {"title": "新标题", "updated_at": iso()})
#   -> UPDATE document SET title=?, updated_at=? WHERE doc_id=?
# 安全边界同 insert：table/key_field 必须是可信标识符；data 的值和 key_value 都参数化。
# 注意：
#   - data 为空时直接 return，避免生成 “SET ” 这种非法 SQL；
#   - 会更新所有匹配 key_field 的行，调用方要确保 key 唯一（通常是主键）；
#   - 不自动维护 updated_at，需要调用方在 data 里显式传入；
#   - 不返回受影响行数，调用方若需判断“是否更新到”应先用 fetch_one 查。
def update(table: str, key_field: str, key_value: str, data: dict) -> None:  # key_value 一般是主键值
    if not data:  # 空 dict 是假值：没有要更新的列
        return  # 直接返回，什么都不做（幂等的“无更新”）
    sets = ", ".join(f"{k}=?" for k in data)  # 生成 "col1=?, col2=?"；列名来自代码，值用占位符
    with db() as conn:  # 自动事务
        conn.execute(  # 执行 UPDATE
            f"UPDATE {table} SET {sets} WHERE {key_field}=?",  # 表名/列名拼接；值全部参数化
            tuple(data.values()) + (key_value,),  # 先 SET 的值，最后补 WHERE 的值；顺序不能反
        )


# load_json()：把数据库里的 TEXT/JSON 值安全地转成 Python 对象。
# 为什么需要它：
#   - 老数据可能是 NULL/空串/非法 JSON，直接 json.loads 会让整个接口 500；
#   - 有些调用方可能已经把值解析成 dict/list（例如内存中的对象），此时应原样返回；
#   - 默认值让调用方决定“坏数据/空数据”时用什么兜底（通常是 [] 或 {}）。
# 行为：
#   None 或 "" -> default；
#   dict/list -> 原样返回（不复制）；
#   其他字符串 -> json.loads；解析失败（JSONDecodeError/TypeError 等）-> default。
# 注意：default 是原对象引用，不是拷贝；调用方若修改返回值，可能影响传入的 default。
def load_json(value: Any, default: Any):  # 返回类型取决于 JSON 内容，故不写死
    if value is None or value == "":  # NULL 和空串都视为“没有值”
        return default  # 直接返回兜底值（如 [] 或 {}）
    if isinstance(value, (dict, list)):  # 已经是解析好的结构（内存对象/驱动自动解析）
        return value  # 原样返回，避免二次 json.loads 报错
    try:  # 只有“字符串”才尝试解析；解析失败不能影响主流程
        return json.loads(value)  # 标准库解析；只解析数据，不执行代码，安全
    except Exception:  # 捕获所有解析异常（JSONDecodeError、TypeError 等）
        return default  # 坏数据降级为默认值，保证接口可用


# dump()：把 Python 对象序列化成 JSON 字符串，写入 TEXT 列。
# ensure_ascii=False：中文直接以 UTF-8 字符保存，而不是 \u4e2d 转义；
#   好处是数据库里可读、体积更小；前提是连接/文件/HTTP 都用 UTF-8。
# 注意：
#   - 不处理 datetime：调用方要先用 iso() 转字符串，否则抛 TypeError；
#   - 默认 allow_nan=True 会输出 NaN/Infinity（严格 JSON 不支持），
#     若数据可能含 NaN，调用方应先清洗；
#   - 输出带空格（默认 separators），不保证键排序；仅用于存储，不用于签名。
def dump(value: Any) -> str:  # 返回 JSON 文本
    return json.dumps(value, ensure_ascii=False)  # 中文不转义；结果直接作为 SQL 参数写入 TEXT 列
