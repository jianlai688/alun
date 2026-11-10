# =============================================================================
# 【文件定位】alun/app/config.py —— “A论”（论文导读智能体）的全局配置中心
#
# 一、这个文件负责什么：
#     1. 集中声明整个后端会用到的“常量”：应用名称/版本、磁盘路径、数据库连接、
#        对象存储、缓存队列、认证账号、模型网关地址、业务字典等；
#     2. 在进程启动（更准确地说：第一次 import 本模块）时读取环境变量，并把
#        字符串形式的配置转换成 Python 真正需要的类型（Path / float / int /
#        bool / list），避免每个使用方各自重复解析；
#     3. 让其它代码只依赖“配置项的名字”，而不关心值到底来自环境变量、代码默认值
#        还是本地 JSON 配置文件——这就是常说的“配置与代码分离”。
#
# 二、它在整个应用中的位置：
#     - 位于顶层包 app/ 之下，是“最底层”的模块：只 import 标准库
#       （json / os / pathlib），不 import 应用内的任何业务模块；
#     - 正因为不反向依赖，api/、services/、db/ 等各层都可以放心 import 它，
#       不会产生循环导入（circular import）；
#     - 其它模块的典型用法：`from app.config import APP_NAME, GATEWAY`。
#
# 三、设计原则（对应 12-Factor App 的“配置外置”）：
#     - 所有可调参数都优先读带 ALUN_ 前缀的环境变量，读不到才回退到代码里的默认值；
#     - 默认值面向“单机开发 / 演示”，生产部署必须覆盖密钥类配置
#       （AUTH_SECRET、数据库密码、模型 API Key 等）。
#
# 四、阅读提示：
#     - 本模块“导入即执行”：下面的 mkdir() 会真的创建目录，
#       _load_gateway_config() 会真的读一次磁盘文件；所以改完配置要重启进程才生效；
#     - 全文按“基础路径 → V2.0 平台组件 → 认证 → 模型网关 → 业务默认值”的顺序组织，
#       每一段前面都有分隔线注释，可以当作目录使用。
# =============================================================================
"""A论 · 全局配置。

所有配置均可通过环境变量覆盖，便于私有化部署。
"""
# 上面 4 行是“模块文档字符串”（module docstring）：Python 会把文件里第一条字符串
# 语句保存到模块的 __doc__ 属性中，help(app.config) 或 IDE 悬浮提示都能看到它。
# 它必须放在文件最前面（注释可以写在它前面，因为注释不是语句），否则就只是普通字符串。
# `from __future__ import annotations` 必须紧跟在 docstring 之后、其它语句之前。
# 作用：让本文件中的类型注解“延迟求值”（以字符串形式保存，用到时才解析），
# 这样即使注解里写了 `dict`、`list[str]` 等较新的写法，在较老的 Python 上也不会报错。
from __future__ import annotations

# json：标准库，用于解析 WorkBuddy2API 的本地 JSON 配置文件（见 _load_gateway_config）。
import json
# os：标准库，本文件最主要的工具——用 os.getenv() 读取环境变量实现“配置外置”。
import os
# pathlib.Path：标准库里面向对象的路径工具，跨 Windows / Linux 拼接路径比字符串安全，
# 还自带 read_text()、mkdir()、resolve() 等实用方法。
from pathlib import Path

# ---------------------------------------------------------------------------
# 应用元信息：会被页面标题、API 响应、日志、健康检查等位置引用。
# ---------------------------------------------------------------------------
# 产品名，同时用于日志与界面标题；改这里即可全局改名。
APP_NAME = "A论"
# 副标题/一句话定位，展示在登录页与页面头部。
APP_SUBTITLE = "论文导读智能体"
# 语义化版本号（主版本.次版本.修订号），V2.0 表示平台化改造后的版本。
APP_VERSION = "2.0.0"

# 项目根目录：__file__ 是当前文件 .../alun/app/config.py；
# .resolve() 先转成绝对路径（并消解 .. 与符号链接），
# 再 .parent 得到 app/ 目录，第二个 .parent 得到 alun/ 项目根目录。
# 以“文件位置”而不是“启动时的工作目录”为基准，能保证无论从哪个目录启动都找得到资源。
BASE_DIR = Path(__file__).resolve().parent.parent
# 数据根目录：优先读环境变量 ALUN_DATA_DIR，未设置时默认 <项目根>/data；
# 外层再包一层 Path(...) 是因为 os.getenv 的默认值可能是 Path 对象、环境变量则是 str，
# 统一转成 Path 后，后面才能安全地使用 / 运算符拼接子路径。
DATA_DIR = Path(os.getenv("ALUN_DATA_DIR", BASE_DIR / "data")).resolve()
# 用户上传文件的存放目录（DATA_DIR 的子目录），例如 PDF、Word 原稿。
UPLOAD_DIR = DATA_DIR / "uploads"
# SQLite 数据库文件的完整路径：可用 ALUN_DB_PATH 覆盖（例如指向挂载的持久化磁盘）；
# 默认放在 DATA_DIR 下，和上传文件同属一份“需要备份的数据”。
DB_PATH = Path(os.getenv("ALUN_DB_PATH", DATA_DIR / "alun.db")).resolve()
# 数据库后端类型：sqlite | postgres。strip() 去掉首尾空格、lower() 统一小写，
# 防止运维写成 "Postgres " 这种带空格/大写的值导致判断失败。
DB_BACKEND = os.getenv("ALUN_DB_BACKEND", "sqlite").strip().lower()  # sqlite | postgres
# PostgreSQL 连接串（DSN）：格式为 协议://用户名:密码@主机:端口/库名。
# 只有 DB_BACKEND=postgres 时才会真正使用；默认指向本机 55432 端口的开发库，
# 里面是明文开发密码，生产环境务必用 ALUN_DATABASE_URL 覆盖。
# 因为跨了多行，这里只在整块上方说明，不在括号内部插入注释，避免破坏语法。
DATABASE_URL = os.getenv(
    "ALUN_DATABASE_URL",
    "postgresql://alun:alun_dev_password@127.0.0.1:55432/alun",
)
# 前端静态资源目录（HTML / JS / CSS），由 Web 框架挂载为静态文件服务。
WEB_DIR = BASE_DIR / "web"

# ---------------------------------------------------------------------------
# V2.0 平台组件（默认关闭，开发/单机仍走 SQLite + 本地文件 + 线程）
# ---------------------------------------------------------------------------
# 下面这一组是“可选增强组件”的配置：全部留空/默认值时，系统退化为单机模式，
# 不依赖任何外部中间件；需要横向扩容时再逐项打开（Redis 队列、S3 对象存储等）。
# Redis 连接串，默认空字符串 = 不启用 Redis（例如 "redis://127.0.0.1:6379/0"）。
REDIS_URL = os.getenv("ALUN_REDIS_URL", "")
# 任务队列后端：inline 表示在当前进程内同步/线程执行，redis 表示交给 Redis 队列异步执行。
QUEUE_BACKEND = os.getenv("ALUN_QUEUE_BACKEND", "inline").strip().lower()  # inline | redis
# S3 兼容对象存储的访问端点，空字符串表示使用本地磁盘保存文件。
S3_ENDPOINT = os.getenv("ALUN_S3_ENDPOINT", "")
# 对象存储的访问密钥 ID（Access Key）；属于敏感信息，只应从环境变量注入。
S3_ACCESS_KEY = os.getenv("ALUN_S3_ACCESS_KEY", "")
# 对象存储的密钥 Secret（Secret Key），同样只能通过环境变量注入，切勿写进代码仓库。
S3_SECRET_KEY = os.getenv("ALUN_S3_SECRET_KEY", "")
# 存放文档的桶（bucket）名称，有默认值，通常无需修改。
S3_BUCKET = os.getenv("ALUN_S3_BUCKET", "alun-documents")
# 桶所在区域，多数自建 MinIO 不校验，但 AWS SDK 需要一个合法值，故给默认值 us-east-1。
S3_REGION = os.getenv("ALUN_S3_REGION", "us-east-1")
# 是否使用 HTTPS 访问对象存储：默认 "0"（不启用）；只有写成 "0"/"false"/"False" 才算关闭，
# 其它任何值（"1"、"true"，甚至不小心拼错的值）都按 True 处理——
# 这样即使手误，也只会“多加密”而不会悄悄退化成明文传输。
S3_SECURE = os.getenv("ALUN_S3_SECURE", "0") not in ("0", "false", "False")
# 对外公开的访问端点：浏览器拿到的预签名 URL 必须用它来拼，
# 否则会出现“服务器能连 MinIO、用户浏览器连不上内网地址”的问题；默认与 S3_ENDPOINT 相同。
S3_PUBLIC_ENDPOINT = os.getenv("ALUN_S3_PUBLIC_ENDPOINT", S3_ENDPOINT)
# 允许跨域访问的前端源列表（CORS）：环境变量里用英文逗号分隔多个地址，
# 这里用列表推导式逐个 strip() 去空格，并用 if x.strip() 过滤掉空串，避免产生非法项。
CORS_ORIGINS = [x.strip() for x in os.getenv("ALUN_CORS_ORIGINS", "").split(",") if x.strip()]
# 日志是否输出为 JSON 格式（1=是）：JSON 便于 ELK/Loki 等日志系统做结构化检索；
# 设为 "0" 则输出人类可读的普通文本，方便本地调试。
LOG_JSON = os.getenv("ALUN_LOG_JSON", "1") not in ("0", "false", "False")
# 是否给 HTTP 响应附加安全头（X-Content-Type-Options、X-Frame-Options 等），默认开启。
SECURITY_HEADERS = os.getenv("ALUN_SECURITY_HEADERS", "1") not in ("0", "false", "False")

# 创建数据目录与上传目录。parents=True：父目录不存在时一并创建；
# exist_ok=True：目录已存在也不报错（让这段代码可以反复安全执行，即“幂等”）。
# 注意这是“导入时就执行”的副作用，目的很明确——保证后续任何写文件的代码都不会因目录缺失而失败。
DATA_DIR.mkdir(parents=True, exist_ok=True)
# 同上，确保 uploads/ 存在，应用启动后立刻可以接收上传。
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 认证：demo 模式下允许无令牌访问（自动注入本地演示用户）；strict 模式强制 JWT
# ---------------------------------------------------------------------------
# 认证模式：strict = 所有接口必须携带有效 JWT；demo = 未登录时自动注入本地演示用户，
# 方便演示与本地试用，但绝不能在生产环境使用。
AUTH_MODE = os.getenv("ALUN_AUTH_MODE", "strict")  # demo | strict
# JWT 签名密钥：默认值只是开发用的固定串，生产环境必须通过环境变量换成高强度随机值，
# 否则任何人都能伪造令牌。
AUTH_SECRET = os.getenv("ALUN_AUTH_SECRET", "alun-local-development-secret")
# 令牌有效期（秒），固定 1 小时；写成常量而非环境变量，是为了让过期策略保持统一、可预期。
TOKEN_TTL_SECONDS = 3600
# 管理员账号：拥有全部权限（见下方 DEFAULT_SCOPES）。
ADMIN_USERNAME = os.getenv("ALUN_ADMIN_USERNAME", "admin")
# 管理员初始密码：仅用于首次初始化/演示，生产环境务必覆盖。
ADMIN_PASSWORD = os.getenv("ALUN_ADMIN_PASSWORD", "admin123")
# 演示账号：配合 demo 认证模式使用。
DEMO_USERNAME = os.getenv("ALUN_DEMO_USERNAME", "demo")
# 演示账号密码，同样只适合本地演示。
DEMO_PASSWORD = os.getenv("ALUN_DEMO_PASSWORD", "demo123")
# 教师账号：用于体验“审题/复核”类角色权限。
TEACHER_USERNAME = os.getenv("ALUN_TEACHER_USERNAME", "teacher")
# 教师账号初始密码。
TEACHER_PASSWORD = os.getenv("ALUN_TEACHER_PASSWORD", "teacher123")

# 默认租户 ID：单机版只有一个租户，预置固定值，方便数据库里直接引用。
DEFAULT_TENANT = "tnt_local"
# 默认用户 ID：与 DEFAULT_TENANT 配套，供演示模式或系统内部任务使用。
DEFAULT_USER = "usr_local"
# 管理员/系统级权限集合：空格分隔的 scope 字符串。
# 这里用“相邻字符串字面量自动拼接”的写法把一行拆成两行，编译期就会合并成一个字符串，
# 不会产生任何运行时开销；因为跨行，注释只写在这块的上方。
DEFAULT_SCOPES = (
    "documents:read documents:write sessions:read sessions:write "
    "questions:read questions:review reports:read admin:usage"
)
# 普通用户的权限集合：比管理员少了 questions:review（审题）和 admin:usage（用量统计），
# 体现“最小权限原则”——普通用户只能读自己的资料、生成会话与报告，不能看平台运营数据。
USER_SCOPES = "documents:read documents:write sessions:read sessions:write questions:read reports:read"

# ---------------------------------------------------------------------------
# 模型网关：默认复用本机 WorkBuddy2API（OpenAI 兼容）
# ---------------------------------------------------------------------------
# 函数名以下划线开头，是 Python 的约定：表示“模块内部使用”，不希望外部 import。
# 它在模块导入时被调用一次，结果保存在下面的 GATEWAY 里，之后不再重复读取，
# 因此运行中修改环境变量不会生效——这是刻意的，保证配置在一个进程生命周期内稳定。
def _load_gateway_config() -> dict:
    # 先用字典装好“环境变量或默认值”的基础配置；这里只做字符串读取，不做类型转换。
    cfg = {
        # 网关基地址：默认指向本机 WorkBuddy2API 的 OpenAI 兼容 /v1 端点。
        "base_url": os.getenv("ALUN_LLM_BASE_URL", "http://127.0.0.1:7863/v1"),
        # API Key：默认为空，表示“未配置”，稍后会尝试从本地配置文件补全。
        "api_key": os.getenv("ALUN_LLM_API_KEY", ""),
        # 主模型名：cn: 前缀是网关内部的供应商命名空间，表示走“国内”线路的模型。
        "model": os.getenv("ALUN_LLM_MODEL", "cn:deepseek-v4.1-flash"),
        # 备用模型：主模型调用失败/超时后降级使用，避免整个功能不可用。
        "fallback_model": os.getenv("ALUN_LLM_FALLBACK_MODEL", "cn:glm-5.3-flash"),
        # 请求超时时间（秒）：环境变量是字符串，必须用 float() 转换；
        # 默认 90 秒，是因为长文本生成（整篇论文导读）耗时明显高于普通接口。
        "timeout": float(os.getenv("ALUN_LLM_TIMEOUT", "90")),
    }
    # 只有当环境变量没有提供 api_key 时，才去读本地配置文件——
    # 环境变量优先级最高，便于容器化部署时直接注入，无需改文件。
    if not cfg["api_key"]:
        # 配置文件路径：优先用 WB2API_CONFIG 指定，否则取用户主目录下的
        # ~/.dsh/wb2api.json（Windows 上即 C:\Users\<用户名>\.dsh\wb2api.json）。
        path = Path(os.getenv("WB2API_CONFIG", Path.home() / ".dsh" / "wb2api.json"))
        # 用 try 包住，是因为读文件可能因“文件不存在/没有权限/JSON 写坏”等各种原因失败，
        # 而配置读取失败不应该让整个应用起不来。
        try:
            # 一次性读出整个文件并按 UTF-8 解码，再交给 json.loads 解析成 dict。
            raw = json.loads(path.read_text(encoding="utf-8"))
            # 取 Key：兼容驼峰 apiKey 与下划线 api_key 两种写法；
            # 用 or 串联可以把 None / 空串都当作“没取到”，继续尝试下一个键。
            cfg["api_key"] = raw.get("apiKey") or raw.get("api_key") or ""
            # 配置文件里的 baseUrl 若存在则覆盖默认值，方便不同机器指向不同的网关地址。
            cfg["base_url"] = raw.get("baseUrl") or cfg["base_url"]
            # 同理，配置文件可指定默认模型；为空则保留环境变量/代码里的默认模型。
            cfg["model"] = raw.get("defaultModel") or cfg["model"]
        # 宽泛捕获所有异常（文件不存在、解码失败、JSON 格式错误、权限不足……），
        # 目的是“尽力而为”：读不到就用上面的默认配置继续跑。
        except Exception:
            # pass 表示“什么都不做”，异常被静默吞掉；排查问题时可以临时改成打印日志。
            pass
    # 是否启用模型能力：只有真正拿到 api_key 才算 enabled=True，
    # 后续业务代码据此决定走“真模型”还是“本地规则/模板”的降级路径。
    cfg["enabled"] = bool(cfg["api_key"])
    # 把整理好的配置字典交回给调用方（模块级变量 GATEWAY）。
    return cfg


# 模块级“单例”：导入时执行一次并把结果缓存下来，全应用共享同一份配置。
GATEWAY = _load_gateway_config()

# 解析/生成策略开关
# 是否启用大模型（总开关）：关闭后所有 AI 能力走本地规则，便于离线演示或排障。
ENABLE_LLM = os.getenv("ALUN_ENABLE_LLM", "1") not in ("0", "false", "False")
# 是否允许用大模型生成“导读报告”：单独一个开关，可只关报告而保留出题等其它能力，控制成本。
ENABLE_LLM_REPORT = os.getenv("ALUN_LLM_REPORT", "1") not in ("0", "false", "False")
# 单次上传大小上限（字节）：环境变量以 MB 为单位（默认 100），
# 这里换算成字节（1 MB = 1024 * 1024 B）供 Web 框架直接比较。
MAX_UPLOAD_BYTES = int(os.getenv("ALUN_MAX_UPLOAD_MB", "100")) * 1024 * 1024

# 会话/题目默认值
# 提示扣分规则：key = 已使用的提示次数，value = 扣掉的分数。
# 用得越多扣得越狠（0/10/20/40），用递增的惩罚鼓励学生先独立思考。
HINT_PENALTIES = {0: 0, 1: 10, 2: 20, 3: 40}
# 布鲁姆认知层级（Bloom's Taxonomy）六级：从低阶“记忆”到高阶“创造”，
# 用于给题目打标签、统计学习深度，也用于前端按层级筛选。
BLOOM_LEVELS = ["记忆", "理解", "应用", "分析", "评价", "创造"]
# 支持的题型枚举值，前后端与数据库共用同一套英文标识，展示时再翻译成中文。
QUESTION_TYPES = ["single_choice", "multiple_choice", "true_false", "fill_blank", "short_answer"]

# 文档类型 → 中文标签的映射：存储层用英文 key，界面直接查表显示中文，
# 避免在模板里写 if/else，也方便以后新增类型时只改这一处。
DOC_TYPE_LABELS = {
    # 研究论文：最常见的学术文献。
    "research_paper": "研究论文",
    # 综述：对某一领域研究进展的总结性文章。
    "review": "综述",
    # 政策研究：政府/智库发布的政策分析文本。
    "policy": "政策研究",
    # 报纸报道：新闻类文本，语言风格与论文差异较大。
    "news": "报纸报道",
    # 其它：兜底类型，保证任何文档都有可展示的标签。
    "other": "其他文档",
}

# 学习模式 → 中文名称：导读（边读边讲）、测评（做题）、答辩（模拟提问答辩）。
MODE_LABELS = {"guided": "导读模式", "exam": "测评模式", "defense": "答辩模式"}
