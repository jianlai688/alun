"""用户、角色与初始化账号。"""
# =============================================================================
# 【文件定位】users.py —— 用户账号、角色与权限作用域
#
# 一、这个文件负责什么（按一次典型使用的顺序）
#   1. 初始化账号：ensure_seed_users() 在应用启动时确保内置的“管理员 / 演示用户 / 教师”
#      三个账号存在；已存在就跳过，绝不覆盖用户后来改过的密码（幂等操作）。
#   2. 登录校验：authenticate() 按用户名取行 → 检查 status 是否可用 → 用 PBKDF2 哈希比对密码
#      → 刷新 last_login_at → 返回整行用户数据，供上层签发 JWT。
#   3. 注册新用户：create_user() 先做格式校验（用户名 / 密码 / 邮箱 / 手机号），
#      再做三项唯一性校验，最后用 hash_password() 存哈希、插入 app_user 表，并回读整行返回。
#   4. 脱敏输出：public_user() 把数据库行裁剪成“可以给前端看”的字段白名单，
#      password_hash 永远不会出现在返回值里。
#   5. 权限计算：scopes_for() 把角色（admin / teacher / user）翻译成空格分隔的权限串，
#      该字符串会被写进 JWT 的 scope 声明，供 security.require() 做接口级鉴权。
#   6. 账号列表：list_users() 返回全部账号，供管理后台或运维脚本使用。
#
# 二、在整个应用中的位置
#   - 层次：app/ 包里的“账号业务层”。它不注册任何 HTTP 路由，也不接触 Request 对象，
#     只做“参数校验 + 拼 SQL + 调用密码学函数”，所以路由、脚本、测试都能直接复用它。
#   - 上游（谁调用它）：
#       * main.py 模块导入阶段：init_db() 建表之后调用 users.ensure_seed_users()；
#       * POST /api/v1/auth/login     → users.authenticate()，再由 _login_response()
#                                       调用 users.scopes_for() 与 users.public_user()；
#       * POST /api/v1/auth/token     → users.authenticate()（兼容 client_credentials 写法）；
#       * POST /api/v1/auth/register  → users.create_user()，ValueError 被转成
#                                        HTTP 422，错误码 REGISTER_INVALID；
#       * GET  /api/v1/auth/me        → users.public_user(row)；
#       * admin.py / teaching_api.py / assistant.py 目前直接写 SQL 查 app_user 表，
#         list_users() 是给后续管理接口预留的统一入口。
#   - 下游（它依赖谁）：
#       * config.py   —— 种子账号的用户名/密码、普通用户的 USER_SCOPES；
#       * db.py       —— db() 事务上下文、fetch_one() 单行查询、iso() 时间戳、new_id() 主键；
#       * security.py —— hash_password() / verify_password()（PBKDF2-HMAC-SHA256）。
#   - 一句话调用链：
#       FastAPI 路由 → users.py（业务规则）→ db.py（连接/事务/SQL）→ SQLite 或 PostgreSQL
#
# 三、app_user 表结构（DDL 定义在 db.py 的 SCHEMA 常量里）
#   user_id       主键，形如 usr_9f3c1a2b4d（new_id("usr")：前缀 + uuid4 十六进制前 10 位）
#   username      登录名，UNIQUE；3—32 位字母/数字/下划线/连字符/中文
#   password_hash 形如 pbkdf2_sha256$120000$<盐base64>$<摘要base64>，绝不保存明文密码
#   role          角色：admin（管理员）/ teacher（教师）/ user（普通用户，默认）
#   display_name  展示名，允许为空，为空时回退显示 username
#   email, phone  选填资料；非空时要求全局唯一（db.py 建了带 WHERE 条件的唯一索引）
#   tenant_id     租户隔离键，本地单机固定 tnt_local，平台模式用于多租户隔离
#   status        active（可登录）/ disabled（被禁用，登录直接失败）
#   created_at    创建时间，ISO-8601 字符串（UTC+8，精确到秒）
#   last_login_at 最近一次成功登录时间，从未登录过为 NULL
#
# 四、值得单独记住的设计点
#   - 密码安全：库里只有 PBKDF2 派生值；verify_password() 用 hmac.compare_digest()
#     做常量时间比较，避免攻击者通过响应时间逐字节猜出摘要。
#   - 不泄露账号是否存在：authenticate() 对“用户不存在 / 账号被禁用 / 密码错误”
#     统一返回 None，路由层统一提示“用户名或密码错误”，防止用户名枚举攻击。
#   - 最小暴露：public_user() 用白名单挑字段，而不是复制整行再删 password_hash，
#     这样以后表里新增敏感列也不会被意外带出去。
#   - SQL 注入防护：所有 SQL 结构都写死，变量只通过 ? 占位符 + 参数元组传入；
#     db.py 在 PostgreSQL 模式下会自动把 ? 改写成 %s，业务代码无需关心后端差异。
#   - 事务语义：`with db() as conn:` 来自 db.py 的 @contextmanager——正常退出自动 commit，
#     抛异常自动 rollback 并把异常继续抛给调用方，最后无论如何都会 close 连接。
#   - 连接模型：fetch_one() / db() 每次调用都新开一个数据库连接（没有连接池）；
#     SQLite 侧用 WAL 模式 + 30 秒 busy_timeout，读写并发时靠锁等待而不是直接报错。
#   - 同步函数 + 异步路由：本文件全是普通 def，却被 async def 的 FastAPI 路由直接调用。
#     本地 SQLite 查询通常在一毫秒内返回，这样写简单直接；若日后换成远程 PostgreSQL 且并发很高，
#     应改为 run_in_threadpool 或使用异步驱动，避免阻塞事件循环。
#   - 权限模型：scope 采用 OAuth2 风格的“空格分隔字符串”（写进 JWT 的 scope 声明），
#     校验时拆成 set 再判断，属于粗粒度的接口级鉴权；角色只是 scope 的计算依据。
# =============================================================================
# `from __future__ import annotations` 必须是模块里除文档字符串外的第一条语句。
# 作用：让所有类型注解“延迟求值”（以字符串形式保存），于是 `dict | None`、`list[dict]`
# 这类新式写法在较老的 Python 版本上也不会在导入时抛 TypeError，同时略微加快导入。
from __future__ import annotations

# 标准库 re：用于 4 条格式校验（用户名 / 密码 / 邮箱 / 手机号）的正则匹配。
# 这里刻意不引入 pydantic 等第三方校验库——校验逻辑手写，报错信息可直接展示给终端用户。
import re
# typing.Any 表示“任意类型”。本文件当前没有直接用到 Any（原文件就是这样导入的），
# 保留它是为了不改变模块的导入清单，也方便将来给 dict[str, Any] 之类的签名补标注。
from typing import Any

# ---------------------------------------------------------------------------
# 第一方配置：种子账号凭据 + 普通用户权限串
#   ADMIN_USERNAME / ADMIN_PASSWORD     —— 内置管理员账号（默认 admin / admin123，
#                                          可用 ALUN_ADMIN_USERNAME / ALUN_ADMIN_PASSWORD 覆盖）
#   DEMO_USERNAME  / DEMO_PASSWORD      —— 内置演示账号（默认 demo / demo123）
#   TEACHER_USERNAME / TEACHER_PASSWORD —— 内置教师账号（默认 teacher / teacher123）
#   USER_SCOPES                         —— 普通用户的基础权限串，教师会在此基础上追加两项
# 注意：默认密码只适合本地演示；部署到公网前必须通过环境变量改掉。
# 这段 import 用圆括号跨两行，属于“隐式续行”：第二行的缩进只为对齐排版，不改变语义。
# ---------------------------------------------------------------------------
from .config import (ADMIN_PASSWORD, ADMIN_USERNAME, DEMO_PASSWORD, DEMO_USERNAME,
                     TEACHER_PASSWORD, TEACHER_USERNAME, USER_SCOPES)
# ---------------------------------------------------------------------------
# 数据层助手（都在 db.py 中实现）：
#   db         —— @contextmanager 包装的连接工厂：进入时 connect，退出时 commit/rollback/close；
#   fetch_one  —— 执行 SELECT 并返回第一行（dict）或 None，内部自己开关连接；
#   iso        —— 当前时间（UTC+8）的 ISO-8601 字符串，精确到秒，直接存进 TEXT 列；
#   new_id     —— 生成“前缀_10位十六进制”的业务主键，比自增 ID 更适合迁移与合并数据。
# 这里只导入 fetch_one 而没导入 fetch_all：只有 list_users() 需要列表查询，
# 它在函数内部做局部导入（见文件末尾），可以减少模块顶层的导入面。
# ---------------------------------------------------------------------------
from .db import db, fetch_one, iso, new_id
# ---------------------------------------------------------------------------
# 密码学助手（security.py）：
#   hash_password   —— 生成 16 字节随机盐 + PBKDF2-HMAC-SHA256（默认 120000 次迭代），
#                      返回 "pbkdf2_sha256$迭代数$盐$摘要" 这种自描述字符串；
#   verify_password —— 按存储串里记录的算法/迭代数/盐重新派生，并用常量时间比较摘要。
# 本文件调用前者保存密码、调用后者校验密码，从不自己拼接或比较明文。
# ---------------------------------------------------------------------------
from .security import hash_password, verify_password

# ---------------------------------------------------------------------------
# ADMIN_SCOPES：管理员的完整权限串（空格分隔，这是 OAuth2 / JWT 的 scope 惯例写法）。
# 逐项含义：
#   documents:read / documents:write   —— 文档列表、详情、上传、删除
#   sessions:read  / sessions:write    —— 导读/测评会话的读取与推进
#   questions:read / questions:review  —— 题目读取与人工复核（教师也只有 review）
#   reports:read                       —— 学习报告
#   admin:usage                        —— 模型调用量、积分等用量统计
#   admin:users                        —— 账号管理（查看列表、禁用/启用）
#   admin:settings                     —— 系统设置
#   admin:monitor                      —— 运行监控
# 写法细节：这是“两个相邻字符串字面量”的隐式拼接，Python 会把它们连成一个字符串。
# 第一段末尾刻意留了一个空格（"...questions:review "），否则会和第二段的
# "reports:read" 粘成 "questions:reviewreports:read"，导致权限判断永远失败。
# 之所以拆成两行，只是为了遵守每行长度限制，运行结果与写成一整行完全相同。
# ---------------------------------------------------------------------------
ADMIN_SCOPES = (
    "documents:read documents:write sessions:read sessions:write questions:read questions:review "
    "reports:read admin:usage admin:users admin:settings admin:monitor"
)


# =============================================================================
# 一、初始化：内置账号
# =============================================================================
def ensure_seed_users() -> None:
    """首次启动时创建管理员、教师与演示账号；已有账号不覆盖密码。"""
    # 本函数是幂等的（idempotent）：main.py 每次启动都会调用它，
    # 重复执行不会产生重复账号，也不会把用户改过的密码重置回默认值。
    # 执行顺序为 管理员 → 演示用户 → 教师，顺序本身不影响结果，只是让启动日志更易读。
    # 每个账号都交给 _ensure_user() 处理“查重 + 插入”的细节，本函数只负责声明“要有谁”。
    _ensure_user(  # 内置管理员：拥有全部权限，用于后台管理与人工审核
        user_id="usr_admin",  # 固定主键：文档、演示数据、测试都靠它稳定引用
        username=ADMIN_USERNAME,  # 默认 "admin"，可用环境变量 ALUN_ADMIN_USERNAME 覆盖
        password=ADMIN_PASSWORD,  # 默认 "admin123"，仅在首次建号时哈希后入库
        role="admin",  # 角色决定 scopes_for() 返回哪一串权限
        display_name="系统管理员",  # 中文展示名，前端直接显示
        tenant_id="tnt_local",  # 本地默认租户；平台模式下可替换为真实租户
    )
    _ensure_user(  # 内置演示账号：给访客/验收演示用的普通用户
        user_id="usr_local",  # 固定主键，config.DEFAULT_USER 也指向这个值
        username=DEMO_USERNAME,  # 默认 "demo"
        password=DEMO_PASSWORD,  # 默认 "demo123"
        role="user",  # 普通用户角色，只拿 USER_SCOPES
        display_name="演示用户",  # 展示名
        tenant_id="tnt_local",  # 与管理员同租户，便于演示时共享数据
    )
    _ensure_user(  # 内置教师账号：用于布置作业、查看学情等教学场景
        user_id="usr_teacher",  # 固定主键
        username=TEACHER_USERNAME,  # 默认 "teacher"
        password=TEACHER_PASSWORD,  # 默认 "teacher123"
        role="teacher",  # 教师角色，权限 = 普通用户 + 题目复核 + 教学管理
        display_name="李老师",  # 演示用的中文姓名
        tenant_id="tnt_local",  # 租户键
    )


def _ensure_user(user_id: str, username: str, password: str, role: str, display_name: str, tenant_id: str) -> None:
    """按用户名判断账号是否已存在；不存在才插入（内部函数，仅供 ensure_seed_users 使用）。"""
    # 参数说明：
    #   user_id      —— 主键，种子账号用可读的固定值（usr_admin / usr_local / usr_teacher）；
    #   username     —— 登录名，同时也是“是否已存在”的判断依据（表上有 UNIQUE 约束）；
    #   password     —— 明文密码，只在本函数内立刻交给 hash_password()，不会落库；
    #   role         —— admin / teacher / user；
    #   display_name —— 展示名；
    #   tenant_id    —— 租户键。
    # 返回 None：调用方只关心“账号有没有建好”，不需要拿到行数据。
    existing = fetch_one("SELECT user_id FROM app_user WHERE username=?", (username,))
    # 参数化查询：只取 user_id 一列，比 SELECT * 更省 IO，也够判断“是否存在”。
    # 注意第二个参数必须是元组 (username,)；写成 (username) 只是加了括号的字符串，会报参数错误。
    if existing:
        # 查到行说明账号已存在（fetch_one 返回 dict，如 {"user_id": "usr_admin"}）。
        # 直接返回，绝不覆盖已有账号的密码/角色——避免每次重启把用户改过的密码重置成默认值。
        return
    # 走到这里说明需要新建账号；with db() 开启事务，正常结束自动 commit。
    with db() as conn:
        # conn.execute 执行 INSERT；SQL 与参数分离，参数按 ? 出现的顺序依次绑定。
        # 下面这条 SQL 由两个相邻字符串字面量拼成（隐式拼接），完整语句是：
        #   INSERT INTO app_user (user_id, username, password_hash, role, display_name,
        #                         tenant_id, status, created_at, last_login_at)
        #   VALUES (?,?,?,?,?,?,?,?,?)
        # 共 9 个占位符，对应下方元组的 9 个值，顺序必须严格一致。
        conn.execute(
            "INSERT INTO app_user (user_id, username, password_hash, role, display_name, tenant_id, status,"
            " created_at, last_login_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, username, hash_password(password), role, display_name, tenant_id, "active", iso(), None),
        )
        # 参数逐个说明：
        #   user_id                 —— 调用方传入的固定主键；
        #   username                —— 登录名；
        #   hash_password(password) —— 现算 PBKDF2 哈希，库里永远不会有明文；
        #   role                    —— 角色字符串；
        #   display_name            —— 展示名；
        #   tenant_id               —— 租户键；
        #   "active"                —— 新账号默认启用；
        #   iso()                   —— 当前时间（UTC+8，秒级）作为 created_at；
        #   None                    —— last_login_at 先置 NULL，登录成功后才会写入。
        # 离开 with 块时 db() 自动 commit；若 INSERT 抛异常（例如用户名唯一约束冲突），
        # db() 会自动 rollback 并重新抛出，不会留下半条脏数据。


# =============================================================================
# 二、登录：校验用户名 / 密码
# =============================================================================
def authenticate(username: str, password: str) -> dict | None:
    """校验登录凭据：成功返回用户整行（dict），失败返回 None。"""
    # 返回值约定：成功 → dict（含 password_hash 等全部列，供路由层再脱敏）；
    #             失败 → None（不区分“用户不存在 / 账号被禁用 / 密码错误”，防止用户名枚举）。
    # 本函数是同步阻塞的：SQLite 本地查询很快，但 PBKDF2 的 12 万次迭代会有几十毫秒 CPU 开销。
    user = fetch_one("SELECT * FROM app_user WHERE username=?", (username.strip(),))
    # 先去掉用户名首尾空格（用户常在输入框里误带空格），再按用户名取整行。
    # 用 SELECT * 是因为后面要读 status、password_hash、user_id、tenant_id、role 等多列，
    # 一次取回比多次查询更划算；真正的“脱敏”交给 public_user() 完成。
    if not user or user["status"] != "active":
        # not user         → 用户名不存在（fetch_one 返回 None）；
        # status != active → 账号被管理员禁用（admin.py 的禁用接口会写入 disabled）。
        # 两种情况合并判断，既少写分支，也避免向调用方泄露“这个用户名其实存在”。
        return None
    if not verify_password(password, user["password_hash"]):
        # 用 security.verify_password 比对：从库里的 "pbkdf2_sha256$..." 串解析算法/迭代数/盐，
        # 重新派生摘要后做常量时间比较；格式异常时它会返回 False，而不是抛异常。
        return None
    # 走到这一步说明凭据正确，下面刷新“最近登录时间”。
    with db() as conn:
        conn.execute("UPDATE app_user SET last_login_at=? WHERE user_id=?", (iso(), user["user_id"]))
        # 用主键 user_id 定位（比 username 更快，也不受改名影响），写入当前时间。
        # 只更新 last_login_at 这一列，避免并发登录时互相覆盖角色、状态等资料。
    user["last_login_at"] = iso()
    # 同步修改内存中的返回值：让调用方立刻看到“本次登录时间”。
    # 若这里不写，路由层拿到的是登录前的旧值，前端会显示上一次的登录时间。
    # 注意这里又调用了一次 iso()：跨秒边界时可能与上面写入数据库的值相差 1 秒，不影响业务。
    # 返回完整行（dict）。main.py::_login_response() 会从中取 user_id / tenant_id / role /
    # username 去签发 JWT，并用 public_user() 脱敏后再返回给前端。
    return user


# =============================================================================
# 三、注册：创建普通用户
# =============================================================================
def create_user(username: str, password: str, display_name: str = "", tenant_id: str = "tnt_local",
                email: str = "", phone: str = "") -> dict:
    """注册一个普通用户：校验格式与唯一性 → 保存哈希 → 返回新用户整行。"""
    # 参数说明：
    #   username     —— 登录名，必填；会先 strip，再做正则校验；
    #   password     —— 明文密码，必填；只用于计算哈希，不会落库；
    #   display_name —— 展示名，可空；为空时回退成 username；
    #   tenant_id    —— 租户键，默认 tnt_local（路由层目前不传，所以注册用户都进本地租户）；
    #   email/phone  —— 选填资料，空串表示不填。
    # 失败约定：所有校验失败都 raise ValueError("中文提示")，由 main.py 的注册接口捕获，
    #           转换成 HTTP 422（code=REGISTER_INVALID），前端可以直接展示该消息。
    # 成功约定：返回刚插入的整行 dict（含 created_at，不含明文密码）。
    username = (username or "").strip()
    # (username or "") 先把 None 变成 ""，再 strip 掉首尾空白；
    # 这样后面的 re.fullmatch 面对的必定是字符串，不会因 None 抛 TypeError。
    email = (email or "").strip().lower()
    # 邮箱统一转小写并去空格：避免 "A@X.com" 与 "a@x.com" 被当成两个不同账号注册。
    phone = (phone or "").strip()
    # 手机号只去首尾空格，不做格式归一化（保留用户输入的 + 号与分隔符原样）。
    if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]{3,32}", username):
        # re.fullmatch 要求“整个字符串”都匹配（区别于 re.search 的“包含即可”）。
        # 字符集：[A-Za-z0-9_] 字母数字下划线，\- 连字符（转义，避免被当成范围），
        #         \u4e00-\u9fff 中日韩统一表意文字基本区（覆盖常用汉字）。
        # 长度 {3,32}：太短容易撞名，太长不利于展示与索引。
        # 错误信息直接面向终端用户，所以写成中文并说明“怎么改”。
        raise ValueError("用户名需为 3—32 位字母、数字、下划线或中文")
    if len(password or "") < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        # 三条同时满足才算通过：① 长度 ≥ 8；② 至少一个英文字母；③ 至少一个数字。
        # len(password or "") 同样是为了兼容 None；re.search 表示“只要出现过即可”。
        # 这是“最低可用强度”策略，不强制大小写与符号，兼顾演示环境的易用性。
        raise ValueError("密码至少 8 位，且必须同时包含字母和数字")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        # email 为空串时整个条件短路，跳过校验（选填字段）。
        # 正则含义：@ 前至少 1 个非 @、非空白字符；@ 后同理；必须有一个点；点后至少 1 个非空白字符。
        # 这是“够用就好”的格式检查，不追求 RFC 5322 完备（真实有效性只能靠发信验证）。
        raise ValueError("邮箱格式不正确")
    if phone and not re.fullmatch(r"\+?[0-9][0-9\- ]{5,19}", phone):
        # 手机号：可选的国际区号前缀 +，首位必须是数字，其后 5—19 位只允许数字、连字符、空格；
        # 总长度 6—20 位，能兼容 "+86 138-0000-0000" 与 "13800000000" 这类输入。
        raise ValueError("手机号格式不正确")
    if fetch_one("SELECT user_id FROM app_user WHERE username=?", (username,)):
        # 唯一性校验之一：用户名不能重复。先查再插是为了给用户友好提示；
        # 真正的兜底是表上的 UNIQUE 约束（并发注册时仍可能撞车，那时 INSERT 会抛异常）。
        raise ValueError("用户名已存在")
    if email and fetch_one("SELECT user_id FROM app_user WHERE email=? AND email<>''", (email,)):
        # 唯一性校验之二：邮箱不能重复。`email<>''` 排除历史数据里的空串，
        # 否则所有“没填邮箱”的账号都会互相冲突。
        # db.py 里还建了部分唯一索引 idx_app_user_email，作为数据库层的最终兜底。
        raise ValueError("邮箱已被注册")
    if phone and fetch_one("SELECT user_id FROM app_user WHERE phone=? AND phone<>''", (phone,)):
        # 唯一性校验之三：手机号不能重复，逻辑与邮箱完全一致。
        raise ValueError("手机号已被注册")
    user_id = new_id("usr")
    # 生成主键：db.new_id("usr") = "usr_" + uuid.uuid4().hex 的前 10 位，
    # 例如 "usr_3f9a1c7d2e"。用随机 ID 而非自增，便于跨库迁移与合并数据。
    with db() as conn:
        # 开事务执行 INSERT；成功自动 commit，异常自动 rollback。
        # 完整 SQL（两段字符串隐式拼接）：
        #   INSERT INTO app_user (user_id, username, password_hash, role, display_name,
        #                         email, phone, tenant_id, status, created_at, last_login_at)
        #   VALUES (?,?,?,?,?,?,?,?,?,?,?)
        # 共 11 个占位符，与下面 11 个值一一对应。
        conn.execute(
            "INSERT INTO app_user (user_id, username, password_hash, role, display_name, email, phone, tenant_id,"
            " status, created_at, last_login_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, username, hash_password(password), "user", display_name or username, email, phone,
             tenant_id, "active", iso(), None),
        )
        # 参数逐个说明：
        #   user_id                 —— 刚生成的 "usr_xxx"；
        #   username                —— 已 strip 并校验过的登录名；
        #   hash_password(password) —— 现算的 PBKDF2 哈希；
        #   "user"                  —— 角色写死为普通用户：注册接口绝不允许自封 admin/teacher，
        #                              这是一条关键的“防权限提升”设计；
        #   display_name or username —— 没填展示名就用登录名兜底（空串是 falsy）；
        #   email / phone           —— 可能为空串，查重时已用 <>'' 排除；
        #   tenant_id               —— 租户键；
        #   "active"                —— 注册即可使用；
        #   iso()                   —— 创建时间；
        #   None                    —— 还没登录过，last_login_at 置 NULL。
    # 回读整行再返回：这样调用方能拿到数据库里真实的 created_at 等字段，
    # 而不是自己拼一个“猜出来的”dict。
    # `or {}` 兜底：理论上插入后立刻就能查到，但若极端情况下查不到，
    # 也返回空 dict 而不是 None，避免调用方崩在 None["user_id"] 上。
    return fetch_one("SELECT * FROM app_user WHERE user_id=?", (user_id,)) or {}


# =============================================================================
# 四、输出脱敏：把数据库行裁剪成 API 可见字段
# =============================================================================
def public_user(user: dict) -> dict:
    """把 app_user 整行转换成可安全返回给前端的公开字段（白名单）。"""
    # 为什么单独写一个函数：SELECT * 出来的行包含 password_hash，
    # 一旦直接返回给前端就是严重的安全事故。这里用“白名单”逐个挑字段，
    # 比“复制整行再 del password_hash”更安全——以后表里新增敏感列也不会被带出去。
    return {
        # 下面是一个字典字面量：键是 API 字段名，值从数据库行里读取。
        "user_id": user.get("user_id"),
        # 主键。用 get 而不是 user["user_id"]，行里缺列时返回 None 而不是抛 KeyError。
        "username": user.get("username"),
        # 登录名。
        "display_name": user.get("display_name") or user.get("username"),
        # 展示名：数据库里可能是空串，`or` 会回退到 username，保证前端永远有字可显示。
        "email": user.get("email") or "",
        # 邮箱：NULL 统一转成空串，前端不必处理 null。
        "phone": user.get("phone") or "",
        # 手机号：同上。
        "role": user.get("role", "user"),
        # 角色：缺列时默认 "user"（最小权限原则）。
        "tenant_id": user.get("tenant_id"),
        # 租户键：前端据此显示或切换租户。
        "status": user.get("status", "active"),
        # 账号状态：缺列时默认 active。
        "created_at": user.get("created_at"),
        # 注册时间。
        "last_login_at": user.get("last_login_at"),
        # 最近登录时间（可能为 None，表示从未登录）。
        # 注意：这里刻意没有 password_hash —— 它永远不应该离开服务端。
    }


# =============================================================================
# 五、权限：角色 → 权限串
# =============================================================================
def scopes_for(role: str) -> str:
    """按角色返回空格分隔的权限串（会被写入 JWT 的 scope 声明）。"""
    # 设计思路：角色是“粗粒度身份”，scope 是“细粒度能力”。
    # main.py 登录成功后把本函数返回值交给 security.issue_token()，令牌里就带上 scope 声明；
    # 之后 security.require(scope) 用 "scope in principal.scopes" 判断是否放行。
    if role == "admin":
        # 管理员：直接拿完整的 ADMIN_SCOPES（文档/会话/题目/报告/后台全部能力）。
        return ADMIN_SCOPES
    if role == "teacher":
        # 教师：普通用户能力 + 题目复核（questions:review）+ 教学管理（teaching:manage）。
        # USER_SCOPES 末尾没有空格，所以这里手动在前面补一个空格再拼接，
        # 否则会粘成 "...reports:readquestions:review" 这样的非法 scope。
        return USER_SCOPES + " questions:review teaching:manage"
    # 兜底分支：user 以及任何未知/拼错的角色都只拿到普通用户权限（最小权限原则）。
    # 这样即使数据库里的 role 被写脏，也不会意外获得更高权限。
    return USER_SCOPES


# =============================================================================
# 六、列表：账号查询
# =============================================================================
def list_users() -> list[dict]:
    """返回全部账号（按角色降序、创建时间升序），供管理后台使用。"""
    # 局部导入：fetch_all 只有这里用到，放在函数体内可以避免模块顶层导入清单膨胀，
    # 也减少“导入 users 模块就必然连带导入 fetch_all”的耦合。
    # 说明：Python 的局部 import 只在函数第一次执行时真正加载模块，之后走缓存，开销可忽略。
    from .db import fetch_all

    # SELECT *：管理后台需要 status / email / phone / last_login_at 等全部资料。
    # 排序：role 是字符串，DESC 的字典序是 user > teacher > admin，
    #       所以结果顺序是“普通用户 → 教师 → 管理员”；同角色内按 created_at 升序（早注册的在前）。
    # 返回 list[dict]：fetch_all 内部已经把 sqlite3.Row 转成普通 dict。
    # 提醒：本函数返回的是原始行（含 password_hash），只能给管理端内部使用；
    #       若要返回给前端，必须逐条经过 public_user() 脱敏。
    return fetch_all("SELECT * FROM app_user ORDER BY role DESC, created_at")
