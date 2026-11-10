"""轻量 JWT（HS256）与请求身份解析。"""
# =============================================================================
# 【文件定位】security.py —— 认证（Authentication）与鉴权（Authorization）底座
#
# 一、这个文件负责什么
#   它是整个应用的“身份基础设施”，只用标准库手写了一套轻量 JWT 方案，具体做四件事：
#   1. 签发令牌：issue_token() 把用户 ID、租户、角色、权限作用域写进 payload，
#      用 HMAC-SHA256 签名后拼成 JWT 字符串；
#   2. 校验令牌：decode_token() 重新计算签名、做常量时间比较，并检查 exp 是否过期；
#   3. 解析请求身份：principal_from_request() 从 HTTP 请求头取出 Bearer 令牌，
#      还原成 Principal 对象（当前用户 + 租户 + 角色 + 作用域）；
#   4. 密码与权限：hash_password()/verify_password() 负责密码的加盐哈希与校验，
#      require_admin()/require_teacher()/require() 负责角色与作用域拦截。
#
# 二、在整个应用中的位置
#   - 层级：app/ 包内的“基础设施层”，不包含任何业务逻辑，也不直接注册路由；
#   - 上游调用者：main.py（登录、全局 principal 依赖）、admin.py、teaching_api.py、
#     quality_api.py、platform_api.py、reader_api.py、chart_api.py、version_api.py
#     等路由模块，以及 users.py（注册/登录时的密码处理）；
#   - 下游依赖：config.py（读取 AUTH_MODE、AUTH_SECRET 等配置）、db.py（查询账号状态）。
#
# 三、主要调用关系（按一次请求的生命周期）
#   1. 应用启动：main.py 在导入阶段调用 validate_runtime_secret(DB_BACKEND)，
#      如果使用 PostgreSQL 平台模式却仍用默认密钥，直接拒绝启动；
#   2. 用户登录：main.py::_login_response() 调用 issue_token(...) 生成令牌，
#      并把返回值 (token, ttl) 组装成 {access_token, expires_in, ...} 响应；
#   3. 访问受保护接口：路由通过 FastAPI 依赖 Depends(principal) 调用
#      principal_from_request(request)，后者再调用 decode_token() 验签；
#   4. 接口内部：根据业务需要调用 require_admin()/require_teacher()/require(scope)，
#      校验失败会抛 HTTPException，由 FastAPI 转成 401/403 JSON 响应；
#   5. 账号管理：users.py 在创建用户时调用 hash_password() 存库，登录时调用
#      verify_password() 比对明文密码与库中的哈希串。
#
# 四、JWT 结构速览（本文件手写的紧凑序列化格式）
#       base64url(header) . base64url(payload) . base64url(signature)
#   - header  ：{"alg": "HS256", "typ": "JWT"}，声明使用 HMAC-SHA256；
#   - payload ：业务声明（claims），如 sub/tenant_id/scope/role/username/iat/exp/jti；
#   - signature：HMAC-SHA256(secret, "header.payload")，缺一不可，防止篡改。
#   HS256 是对称签名：签发方和校验方共用同一个 AUTH_SECRET，因此密钥绝不能泄露。
#
# 五、安全设计要点（初学者最容易忽略的地方）
#   - 签名比较必须用 hmac.compare_digest()（常量时间），不能用 ==，否则可能被时序攻击；
#   - 密码只存 PBKDF2 派生值，永不存明文；盐值随机且随密码一起保存；
#   - 令牌校验失败一律转成 401，不把内部异常类型直接暴露给调用方；
#   - demo 模式为了本地演示允许无令牌访问，strict 模式（默认）强制 Bearer 令牌；
#   - 平台模式（PostgreSQL）启动时会禁止默认/空密钥，防止开发密钥被带上生产。
# =============================================================================
# `from __future__ import annotations` 必须是模块中除文档字符串外的第一条语句。
# 作用：让所有类型注解“延迟求值”（以字符串形式保存），这样 tuple[str, int]、
# dict[str, Any]、set[str] 这类写法在较老的 Python 版本上也不会在导入时报错。
from __future__ import annotations

# ---------------------------------------------------------------------------
# 标准库导入：JWT 的编码、签名、时间戳全部手写，不依赖 PyJWT 等第三方库，
# 好处是部署轻量、可控；代价是需要自己处理 Base64URL、填充和算法细节。
# ---------------------------------------------------------------------------
import base64  # Base64URL 编解码：JWT 的 header/payload/signature 三段都要转成 URL 安全字符串
import hashlib  # 哈希算法：MD5 生成 jti、SHA-256 用于 HMAC 签名和 PBKDF2 密码派生
import hmac  # HMAC 签名与 compare_digest() 常量时间比较，避免签名被时序攻击逐字节猜出
import json  # 序列化/反序列化 JWT 的 header 与 payload（JSON 是 JWT 的标准载荷格式）
import os  # os.urandom(16) 生成密码盐值，利用操作系统提供的加密安全随机数
import time  # time.time() 取当前 Unix 时间戳，用于 iat（签发时间）和 exp（过期时间）
from dataclasses import dataclass, field  # dataclass 简化 Principal 数据类；field 用于可变默认值工厂
from typing import Any  # 类型标注：payload 的值类型不固定，用 Any 表示“任意类型”

# ---------------------------------------------------------------------------
# FastAPI 相关对象：
#   Header         —— 本文件当前未直接使用（历史/预留导入），保留是为了不改变原文件的导入清单；
#   HTTPException  —— 鉴权失败时抛出，FastAPI 会自动转成带 status_code 和 detail 的 JSON 响应；
#   Request        —— 读取原始 HTTP 请求头（Authorization / X-Tenant-Id / X-User-Id）。
# ---------------------------------------------------------------------------
from fastapi import Header, HTTPException, Request

# ---------------------------------------------------------------------------
# 从 .config 读取运行期配置（所有配置都可由环境变量覆盖，便于私有化部署）：
#   AUTH_MODE         —— 认证模式："demo" 允许匿名演示访问，"strict" 强制 Bearer JWT；
#   AUTH_SECRET       —— HS256 的共享密钥，签发与校验必须使用同一个值；
#   DEFAULT_TENANT    —— 未传 X-Tenant-Id 时使用的本地默认租户；
#   DEFAULT_USER      —— 未传 X-User-Id 或令牌缺少 sub 时使用的本地默认用户；
#   TOKEN_TTL_SECONDS —— 令牌默认有效期（秒），这里作为 issue_token 的默认参数。
# ---------------------------------------------------------------------------
from .config import AUTH_MODE, AUTH_SECRET, DEFAULT_TENANT, DEFAULT_USER, TOKEN_TTL_SECONDS


# DEFAULT_SECRETS：被视为“不安全”的 JWT 密钥集合（默认开发密钥、示例占位符、空字符串）。
# 平台模式启动时会拿当前 AUTH_SECRET 与本集合比对，命中任意一个就拒绝启动，
# 避免开发者把本地默认密钥原封不动带到生产环境。
DEFAULT_SECRETS = {"alun-local-development-secret", "change-me-in-production", ""}


# ---------------------------------------------------------------------------
# validate_runtime_secret：启动期的“安全闸门”。
# 由 main.py 在应用初始化时调用（validate_runtime_secret(DB_BACKEND)）。
# 参数 backend 是数据库后端名称："sqlite" 表示单机/开发，"postgres" 表示平台化部署。
# 返回值：无（None）。校验不通过时抛 RuntimeError，让进程直接启动失败——
# 这是“快速失败（fail fast）”策略，比带着弱密钥上线安全得多。
# ---------------------------------------------------------------------------
def validate_runtime_secret(backend: str) -> None:
    """平台模式禁止使用默认 JWT 密钥，避免部署时把开发密钥带入生产。"""
    # 只有当后端是 postgres（平台模式）且密钥仍属于默认集合时才报错；
    # SQLite 单机模式允许使用默认密钥，方便本地零配置启动。
    if backend == "postgres" and AUTH_SECRET in DEFAULT_SECRETS:
        # 抛 RuntimeError 而不是 HTTPException：这属于部署配置错误，
        # 应该在启动阶段就终止进程，而不是等到某个请求进来才返回 500。
        raise RuntimeError("ALUN_AUTH_SECRET 未设置或仍为默认值；平台模式必须配置独立随机密钥")


# ---------------------------------------------------------------------------
# _b64：把原始字节做 Base64URL 编码，并去掉末尾的 "=" 填充。
# JWT 规范（RFC 7515）要求各段使用 base64url 且不带 padding，
# 因此这里先 urlsafe_b64encode，再用 rstrip(b"=") 去掉所有尾部等号。
# 参数 data：待编码的字节串；返回值：URL 安全的字符串。
# ---------------------------------------------------------------------------
def _b64(data: bytes) -> str:
    # urlsafe_b64encode 会把 +/ 换成 -_，适合放进 URL/JSON；
    # .decode() 默认按 UTF-8 解码，得到可直接拼接的 str。
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# _unb64：_b64 的逆操作，把 Base64URL 字符串还原成字节。
# Base64 要求输入长度是 4 的倍数，JWT 去掉了 "="，所以解码前要补回来。
# 参数 data：不含填充的 Base64URL 字符串；返回值：解码后的原始字节。
# ---------------------------------------------------------------------------
def _unb64(data: str) -> bytes:
    # (-len(data) % 4) 计算“还差几个字符才凑满 4 的倍数”：
    # 例如长度 5 → 3，长度 6 → 2，长度 8 → 0；再用 "=" 乘出对应数量的填充。
    pad = "=" * (-len(data) % 4)
    # 补上填充后调用 urlsafe_b64decode；输入非法时会抛 binascii.Error，
    # 由上层 decode_token 的 try/except 统一转成 401。
    return base64.urlsafe_b64decode(data + pad)


# ---------------------------------------------------------------------------
# issue_token：签发一枚 HS256 JWT。
# 参数：
#   user_id   —— 用户唯一 ID，写入标准声明 sub（subject）；
#   tenant_id —— 租户 ID，用于多租户数据隔离；
#   scopes    —— 空格分隔的权限作用域字符串，如 "documents:read reports:read"；
#   ttl       —— 有效期（秒），默认取配置 TOKEN_TTL_SECONDS（3600 秒）；
#   role      —— 角色（user/teacher/admin），用于粗粒度权限判断；
#   username  —— 登录名，便于审计日志直接显示。
# 返回值：二元组 (token, ttl)。token 是 "header.payload.signature" 字符串；
#         ttl 原样返回，调用方（main.py::_login_response）用它填 expires_in 字段。
# 注意：函数本身不查数据库、不校验密码，只负责“把已确认的身份写成令牌”。
# ---------------------------------------------------------------------------
def issue_token(user_id: str, tenant_id: str, scopes: str, ttl: int = TOKEN_TTL_SECONDS,
                role: str = "user", username: str = "") -> tuple[str, int]:
    # JWT 头部：alg 指定签名算法为 HS256（HMAC-SHA256），typ 固定为 JWT。
    # 注意：服务端 decode_token 并不会信任头部的 alg，而是固定按 HS256 重新计算，
    # 这样可以避免“算法混淆”攻击（例如把 alg 改成 none 绕过签名）。
    header = {"alg": "HS256", "typ": "JWT"}
    # 当前 Unix 时间戳（秒）。iat（签发时间）和 exp（过期时间）都以它为准。
    now = int(time.time())
    # payload 是令牌的“业务身份证”，下面逐项说明每个声明的含义：
    #   sub       —— subject，用户 ID；
    #   tenant_id —— 租户 ID，多租户隔离的关键；
    #   scope     —— 权限作用域（字符串，空格分隔，解析时再 split 成集合）；
    #   role      —— 角色，require_admin/require_teacher 会读取；
    #   username  —— 登录名，仅用于展示/审计；
    #   iat       —— issued at，签发时间；
    #   exp       —— expiration，过期时间 = 现在 + ttl；
    #   jti       —— JWT ID，令牌唯一标识，用 user_id+时间戳的 MD5 前 16 位生成，
    #                便于日志追踪或未来实现吊销名单（当前未做吊销）；
    #                这里的 MD5 只用于生成短标识，不承担密码学安全职责。
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "scope": scopes,
        "role": role,
        "username": username,
        "iat": now,
        "exp": now + ttl,
        "jti": hashlib.md5(f"{user_id}{now}".encode()).hexdigest()[:16],
    }
    # JWT 的前两段：分别把 header/payload 序列化成 JSON 再做 Base64URL。
    # separators=(",", ":") 去掉 JSON 中多余的空格，让令牌更短；
    # .encode() 把 str 转成 UTF-8 字节，供 _b64 编码。
    segments = [
        _b64(json.dumps(header, separators=(",", ":")).encode()),
        _b64(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    # 签名输入 = "header段.payload段" 的字节形式（不包含第三段的签名本身）。
    signing_input = ".".join(segments).encode()
    # 用服务端密钥 AUTH_SECRET 做 HMAC-SHA256，得到 32 字节签名；
    # 任何人篡改 header/payload 都会导致重算出的签名不同。
    sig = hmac.new(AUTH_SECRET.encode(), signing_input, hashlib.sha256).digest()
    # 拼成三段式 JWT；同时把 ttl 一起返回，方便登录接口返回 expires_in。
    return ".".join(segments + [_b64(sig)]), ttl


# ---------------------------------------------------------------------------
# decode_token：校验并解析 JWT。
# 处理流程（任何一步失败都会转成 HTTP 401）：
#   1. 按 "." 切成三段；
#   2. 用同样的密钥和算法重新计算签名；
#   3. 用 hmac.compare_digest 做常量时间比较，防时序攻击；
#   4. Base64URL 解码 payload 并 json.loads；
#   5. 检查 exp 是否早于当前时间（过期即拒绝）；
#   6. 返回 payload 字典，供 principal_from_request 组装 Principal。
# 参数 token：客户端 Authorization 头里 "Bearer " 后面的那串字符。
# ---------------------------------------------------------------------------
def decode_token(token: str) -> dict[str, Any]:
    # 整个解析过程包在 try 中：格式错误、签名错误、JSON 错误、时间字段异常
    # 都统一走 except 分支，避免把底层异常类型泄露给客户端。
    try:
        # 紧凑 JWT 必须恰好由三段组成；用 split(".") 解包，
        # 段数不对会抛 ValueError，进入下面的统一异常处理。
        head, body, sig = token.split(".")
        # 用与签发时完全相同的算法重算签名：
        # 输入是 "header.payload" 的 UTF-8 字节，密钥仍是 AUTH_SECRET。
        expected = hmac.new(AUTH_SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
        # compare_digest 的比较耗时与内容无关，攻击者无法通过响应时间逐字节猜测签名；
        # _unb64(sig) 先把第三段还原成原始签名字节再比较。
        if not hmac.compare_digest(expected, _unb64(sig)):
            # 签名不一致说明令牌被篡改或密钥不匹配，抛 ValueError 交给外层统一处理。
            raise ValueError("bad signature")
        # 签名通过后才解析 payload；JWT 的 payload 本身就是普通 JSON 对象。
        payload = json.loads(_unb64(body))
        # 过期校验：payload.get("exp", 0) 缺失时按 0 处理（即立即视为过期）；
        # int() 兼容某些实现把 exp 写成字符串或浮点数的情况。
        if int(payload.get("exp", 0)) < int(time.time()):
            # 已过期：同样抛 ValueError，最终对外表现为 401。
            raise ValueError("expired")
        # 校验全部通过，返回 claims 字典（包含 sub/tenant_id/scope/role 等）。
        return payload
    # 捕获所有异常（# noqa: BLE001 是告诉 lint 工具“这里是刻意宽泛捕获”）：
    # 对认证接口而言，失败原因不应区分太细，统一返回 401 更安全。
    except Exception as exc:  # noqa: BLE001
        # HTTPException 是 FastAPI 的“业务异常”：status_code=401 表示未认证；
        # detail 采用统一的错误结构 {code, message}，前端可据此做提示；
        # f"令牌无效：{exc}" 把具体原因带出来，便于联调排查。
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": f"令牌无效：{exc}"}) from exc


# ---------------------------------------------------------------------------
# Principal：当前请求的“身份主体”数据类。
# 它把认证结果标准化，路由里拿到的永远是这个对象，而不必反复解析请求头。
# 使用 @dataclass 自动生成 __init__/__repr__/__eq__，减少样板代码。
# 默认值面向本地演示：未登录时也能得到一个本地用户，便于 demo 模式直接跑通。
# ---------------------------------------------------------------------------
@dataclass
class Principal:
    # 用户唯一 ID；默认 usr_local，与 config.DEFAULT_USER 保持一致。
    user_id: str = DEFAULT_USER
    # 租户 ID；默认 tnt_local，用于多租户数据隔离。
    tenant_id: str = DEFAULT_TENANT
    # 权限作用域集合，例如 {"documents:read", "reports:read"}。
    # 必须用 field(default_factory=set)：如果写成 = set()，所有实例会共享同一个可变集合，
    # 一个请求往里加权限会影响另一个请求（经典的 Python 可变默认值陷阱）。
    scopes: set[str] = field(default_factory=set)
    # 角色：user 普通用户 / teacher 教师 / admin 管理员，供 require_* 判断。
    role: str = "user"
    # 登录名，用于展示和审计；匿名演示时默认空串。
    username: str = ""


# ---------------------------------------------------------------------------
# hash_password：把明文密码转换成可安全存库的哈希串。
# 算法：PBKDF2-HMAC-SHA256，16 字节随机盐，默认迭代 120000 次。
# 迭代次数越高，暴力破解成本越大，但登录校验也越慢；120000 是性能与安全的折中。
# 返回格式：pbkdf2_sha256$<迭代次数>$<base64盐>$<base64摘要>
# 这种“算法$参数$盐$摘要”的自描述格式，方便以后升级算法或迭代次数。
# ---------------------------------------------------------------------------
def hash_password(password: str, iterations: int = 120_000) -> str:
    # 每个用户、每次改密都生成新的 16 字节随机盐：
    # 即使两个人密码相同，入库的哈希也完全不同，可抵御彩虹表。
    salt = os.urandom(16)
    # PBKDF2 把“明文密码 + 盐 + 迭代次数”反复做 HMAC-SHA256，派生出 32 字节摘要；
    # password.encode("utf-8") 保证中文/emoji 等字符也能稳定编码。
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    # 用 $ 分隔四部分拼成字符串；盐和摘要都用 Base64URL 编码，避免出现 $ 或不可见字符。
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


# ---------------------------------------------------------------------------
# verify_password：校验“用户输入的明文密码”是否匹配“数据库里的哈希串”。
# 步骤：解析存储格式 → 确认算法 → 用同一盐和迭代次数重算 → 常量时间比较。
# 返回值：True 表示密码正确；False 表示错误或存储格式损坏。
# 注意：任何异常都返回 False（fail closed），绝不让格式错误变成“校验通过”。
# ---------------------------------------------------------------------------
def verify_password(password: str, encoded: str) -> bool:
    # 统一兜底：encoded 可能为空、被截断、算法未知，任何解析失败都视为验证失败。
    try:
        # 按 $ 切成四段：算法名、迭代次数、盐、摘要；
        # 段数不是 4 会抛 ValueError，直接进入 except 返回 False。
        algo, iterations, salt_b64, digest_b64 = encoded.split("$")
        # 只支持当前使用的 pbkdf2_sha256；其他算法（如历史遗留的 bcrypt）一律拒绝，
        # 这样可以避免降级攻击，也提示需要迁移密码格式。
        if algo != "pbkdf2_sha256":
            return False
        # 还原出原始盐值和数据库里保存的期望摘要。
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        # 用相同的盐、相同的迭代次数重新派生，得到本次输入的摘要；
        # int(iterations) 兼容字符串形式的迭代次数。
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        # 常量时间比较，避免攻击者通过响应时间差异推断哈希前缀；
        # 返回 True/False 作为最终校验结果。
        return hmac.compare_digest(expected, actual)
    # 捕获所有异常（空串、分段错误、Base64 非法、迭代次数不是数字等），
    # 统一返回 False，保证“异常不等于通过”。
    except Exception:
        return False


# ---------------------------------------------------------------------------
# principal_from_request：从 HTTP 请求还原当前身份，是整个鉴权链路的入口。
# 输入：FastAPI 的 Request 对象（可直接读取 headers）。
# 输出：Principal 实例。
# 分支逻辑：
#   1. Authorization 以 "Bearer " 开头 → 走 JWT 校验，解析 claims，并查库确认账号状态；
#   2. 没有 Bearer 令牌且 AUTH_MODE == "strict" → 抛 401，拒绝匿名访问；
#   3. 没有 Bearer 令牌且是 demo 模式 → 用 X-User-Id/X-Tenant-Id（或默认值）构造演示身份。
# 设计取舍：JWT 自带签名，正常情况下无需查库；这里额外查 app_user.status，
# 是为了让“管理员禁用账号”能立即生效，而不必等令牌自然过期。
# ---------------------------------------------------------------------------
def principal_from_request(request: Request) -> Principal:
    # 读取 Authorization 请求头；不存在时返回空串，后续用 startswith 判断。
    auth = request.headers.get("Authorization", "")
    # 租户优先取 X-Tenant-Id 请求头；没有或为空则回落到默认租户 tnt_local。
    tenant = request.headers.get("X-Tenant-Id") or DEFAULT_TENANT
    # 用户优先取 X-User-Id 请求头；没有或为空则回落到默认用户 usr_local。
    # 注意：这两个头只在 demo/无令牌场景下作为身份来源；有 JWT 时以令牌 claims 为准。
    user = request.headers.get("X-User-Id") or DEFAULT_USER
    # Bearer 是 HTTP 标准的令牌认证方案，前缀大小写敏感（RFC 6750 规定为 "Bearer"）。
    if auth.startswith("Bearer "):
        # 去掉前 7 个字符（"Bearer " 的长度），再 strip() 去掉首尾空白，
        # 然后交给 decode_token 验签、验过期。
        payload = decode_token(auth[7:].strip())
        # 账号状态检查放在独立 try 中：
        # 目的是“令牌有效”与“账号仍可用”双重校验，同时不让数据库故障拖垮所有请求。
        try:
            # 延迟导入 db.fetch_one：避免 security 模块在导入期就依赖数据库层，
            # 也降低模块之间形成循环导入的风险。
            from .db import fetch_one

            # 参数化 SQL：用 ? 占位符而不是字符串拼接，防止 SQL 注入；
            # 只取 status 一列，按 user_id 精确查询当前令牌对应的账号。
            account = fetch_one("SELECT status FROM app_user WHERE user_id=?", (payload.get("sub", ""),))
            # 只有“查到了账号”且“状态不是 active”时才拒绝；
            # 查不到（account 为 None）时放行，兼容演示账号/历史令牌等场景。
            if account and account.get("status") != "active":
                # 账号被禁用：返回 401 + ACCOUNT_DISABLED，提示前端不要继续重试。
                raise HTTPException(status_code=401, detail={"code": "ACCOUNT_DISABLED", "message": "账号已被禁用"})
        # HTTPException 必须原样向上抛，不能被下面的 Exception 兜底吞掉，
        # 否则“账号被禁用”会被误判为数据库异常而放行。
        except HTTPException:
            raise
        # 其他异常（数据库连接失败、表不存在、驱动报错等）选择“降级放行”：
        # 可用性优先，令牌签名本身已经通过校验；生产环境若要求更严格，
        # 可以把这里改成 raise 401/503，避免数据库故障期间禁用逻辑失效。
        except Exception:
            pass
        # 用令牌里的 claims 构造 Principal：
        #   sub        —— 用户 ID，缺失时回落默认用户；
        #   tenant_id  —— 租户 ID，缺失时回落到请求头/默认租户；
        #   scope      —— 空格分隔字符串，split() 后转成 set，便于 O(1) 判断；
        #   role       —— 角色，缺失按普通用户处理；
        #   username   —— 登录名，缺失为空串。
        return Principal(
            user_id=payload.get("sub", DEFAULT_USER),
            tenant_id=payload.get("tenant_id", tenant),
            scopes=set(str(payload.get("scope", "")).split()),
            role=payload.get("role", "user"),
            username=payload.get("username", ""),
        )
    # 走到这里说明没有 Bearer 令牌。strict 模式（默认）必须拒绝匿名访问：
    # 401 表示“未认证”，错误码 UNAUTHORIZED 供前端统一跳转登录页。
    if AUTH_MODE == "strict":
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "缺少访问令牌"})
    # demo 模式：构造一个本地演示身份，作用域为空集合，角色为普通用户，
    # username 固定为 "demo"，方便前端展示“演示账号”。user/tenant 可被请求头覆盖。
    return Principal(user_id=user, tenant_id=tenant, scopes=set(), role="user", username="demo")


# ---------------------------------------------------------------------------
# require_admin：管理员专用接口的权限闸门。
# 调用方式：路由里先拿到 Principal，再调用本函数；不满足条件会抛 403。
# 403（Forbidden）与 401（Unauthorized）的区别：
#   401 = 你没登录/令牌无效；403 = 你登录了，但角色不够。
# 返回值：None（通过时什么都不返回，失败时抛异常）。
# ---------------------------------------------------------------------------
def require_admin(principal: Principal) -> None:
    # 只有 role 恰好等于 "admin" 才放行；teacher/user 都会被拒绝。
    if principal.role != "admin":
        # 统一错误结构：code=FORBIDDEN，message 为中文提示，便于前端直接展示。
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "需要管理员权限"})


# ---------------------------------------------------------------------------
# require_teacher：教师/管理员接口的权限闸门。
# 教师能用的功能管理员通常也能用，所以这里用“角色集合”判断而不是单值比较。
# 返回值：None（通过）或抛 403（权限不足）。
# ---------------------------------------------------------------------------
def require_teacher(principal: Principal) -> None:
    # role 为 "admin" 或 "teacher" 都放行；普通用户 "user" 被拒绝。
    if principal.role not in ("admin", "teacher"):
        # 403 + FORBIDDEN，提示需要教师或管理员权限。
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "需要教师或管理员权限"})


# ---------------------------------------------------------------------------
# require：细粒度“作用域（scope）”校验。
# 与角色校验的区别：角色是粗粒度岗位（admin/teacher/user），
# 作用域是具体能力（documents:read、reports:read、admin:usage 等）。
# 当前策略（见函数文档字符串）：
#   - demo 模式完全不拦截，方便本地演示；
#   - strict 模式才检查，且只有当 principal.scopes 非空时才检查；
#   - scopes 为空集合被视为“未配置作用域”，等同于不限制（兼容旧令牌/匿名身份）。
# 参数 principal：当前身份；scope：需要的能力名，如 "documents:write"。
# 返回值：None（通过）或抛 403（缺少作用域）。
# ---------------------------------------------------------------------------
def require(principal: Principal, scope: str) -> None:
    """demo 模式不做作用域拦截；strict 模式校验。"""
    # 三个条件同时成立才拒绝：
    #   1) 当前是 strict 模式；
    #   2) 该身份确实带了作用域集合（空集合视为不限制）；
    #   3) 所需 scope 不在集合中。
    if AUTH_MODE == "strict" and principal.scopes and scope not in principal.scopes:
        # 错误信息里带上具体缺少的 scope，便于前端/日志定位是哪个权限没配。
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": f"缺少作用域 {scope}"})
