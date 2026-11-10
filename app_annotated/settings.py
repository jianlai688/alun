# =============================================================================
# 【文件定位】alun/app/settings.py —— “运行时可改设置”的数据访问与解析层
#
# 一、这个文件负责什么
#   1. 定义所有可热改配置项的出厂默认值 DEFAULTS（大模型、导读、OCR、图表）；
#   2. 启动时把默认值幂等写入 app_setting 键值表，不覆盖管理员已经改过的值；
#   3. 提供 get()/get_bool()/get_float()/get_int() 四个读取接口，把数据库里的
#      TEXT 值转换成业务需要的字符串、布尔、浮点数和整数；
#   4. 提供 all_settings()/set_many() 给管理后台读取和批量保存设置；
#   5. 保存设置后清理 /api/v1/meta 的缓存，让前端及时看到新的模型信息。
#
# 二、在整个应用中的位置
#   FastAPI 路由 / 业务层
#     main.py      —— 启动时执行 ensure_defaults()；/meta 读取 llm.model
#     admin.py     —— 管理员读取/保存全部设置
#     llm.py       —— 模型开关、模型名、温度、最大 token
#     generator.py / assistant.py / parser.py —— 提示词、OCR、图表参数
#           │ 普通同步函数调用
#           ▼
#   settings.py【本文件】—— 只负责“配置存取与类型转换”
#           ├─ db.db() 上下文管理器 ──► SQLite / PostgreSQL 的 app_setting 表
#           └─ platform.redis_client ──► 删除 meta:v2 缓存（可选依赖）
#
# 三、主要调用关系
#   - main.py 启动阶段：init_db() → users.ensure_seed_users() → settings.ensure_defaults()；
#   - admin.py：GET /admin/settings 调 all_settings()，PUT /admin/settings 调 set_many()；
#   - llm.py：get_bool("llm.enabled")、get("llm.model")、get_float("llm.temperature")、
#     get_int("llm.max_tokens")；
#   - generator.py / assistant.py：读取 guide.system_prompt 等导读配置；
#   - parser.py：读取 OCR 与图表的开关、DPI、页数、最小字符数等参数。
#
# 四、关键设计（阅读重点）
#   - 统一存字符串：app_setting.value 是 TEXT，布尔和数字也以字符串落库，读取时再转换；
#   - 两层来源：数据库有值就优先用数据库，否则回退 DEFAULTS，再否则用调用方默认值；
#   - 幂等初始化：ensure_defaults() 使用 ON CONFLICT DO NOTHING，重启不会覆盖用户配置；
#   - 防御性写入：set_many() 先白名单过滤，再对温度/数量/DPI 做范围限制，最后 UPSERT；
#   - 同步阻塞：本文件全部是普通 def，数据库操作会阻塞当前线程；FastAPI 的同步路由
#     会在线程池中运行，不要在 async def 中直接调用后又不做线程池隔离；
#   - 权限边界：本层不校验登录、角色或租户，鉴权由 admin.py 的 admin_principal 完成；
#   - 错误码：本层没有业务错误码；数据库异常向上抛，读取转换失败则回退默认值；
#     缓存清理失败被静默忽略，不会影响设置保存。
#
# 五、建议阅读顺序
#   DEFAULTS → ensure_defaults() → get()/get_bool()/get_float()/get_int()
#   → all_settings() → set_many()。
#
# 六、说明
#   本文件是逐行学习注释版：原文件的全部可执行语句、字符串、缩进、导入和空行均保留；
#   新增内容全部是 # 注释（原有模块 docstring 原样保留），不改变任何运行逻辑。
# =============================================================================
# 下面一行是原文件已有的模块文档字符串（docstring），必须保持为模块的第一条语句。
"""运行时可修改的模型与导读设置。"""
# ---------------------------------------------------------------------------
# 导入区：只引入本文件直接需要的依赖，避免把具体数据库/配置实现耦合进来。
# ---------------------------------------------------------------------------
# from __future__ import annotations：开启类型注解的“延迟求值”。
# 这样 dict[str, str]、str | None 等写法在较旧的 Python 中也不会在导入时求值；
# 它必须位于模块 docstring 之后、其它可执行语句之前。
from __future__ import annotations

# 从同包的 config 模块导入 GATEWAY（进程启动时由环境变量/WorkBuddy2API 配置生成）。
# 用它作为模型默认值的动态来源：部署环境改了模型配置后，DEFAULTS 也会跟着变化。
from .config import GATEWAY
# 从数据层导入四个助手：
#   db        —— @contextmanager 事务上下文：成功自动 commit、异常 rollback、最后 close；
#   fetch_all —— 执行 SELECT，返回 list[dict]；
#   fetch_one —— 执行 SELECT，返回第一行 dict，查不到则返回 None；
#   iso       —— 生成带 +08:00 时区、精确到秒的 ISO 时间字符串，用于 updated_at。
from .db import db, fetch_all, fetch_one, iso

# ---------------------------------------------------------------------------
# DEFAULTS：所有“允许在后台修改”的配置项及其出厂默认值。
# 使用 dict[str, str] 是刻意设计：数据库 app_setting.value 是 TEXT，布尔/数字也存字符串，
# 读取时再由 get_bool/get_float/get_int 转换，这样 SQLite 与 PostgreSQL 行为一致。
# 注意：这里是“默认值 + 读取兜底”，不是运行时缓存；数据库中的值优先。
# ---------------------------------------------------------------------------
DEFAULTS: dict[str, str] = {
    "llm.enabled": "1",  # 大模型总开关：1=启用；get_bool 只把 0/false/False/空串视为关闭。
    "llm.model": GATEWAY.get("model") or "cn:deepseek-v4-flash",  # 主模型：优先取网关配置中的 model，为空时兜底到 DeepSeek Flash。
    "llm.fallback_model": GATEWAY.get("fallback_model") or "cn:glm-5.3-flash",  # 备用模型：主模型调用失败后由 llm.py 自动降级使用。
    "llm.temperature": "0.45",  # 采样温度：0 更确定，数值越大越发散；set_many 限制在 0.0～1.5。
    "llm.max_tokens": "1800",  # 单次生成最多输出的 token 数；set_many 允许范围 1～32000。
    "guide.system_prompt": "",  # 导读系统提示词；空串表示由 generator.py 使用代码内置的 SYSTEM_PROMPT。
    "guide.default_question_count": "12",  # 一次导读默认生成的题目数量；set_many 允许 1～30。
    "guide.max_question_count": "30",  # 用户可请求的题目数量上限；set_many 允许 1～30。
    "guide.hint_levels": "3",  # 每道题可提供的提示层级数；set_many 允许 1～30。
    "ocr.enabled": "1",  # OCR 总开关：1=启用；还要同时满足 llm.available() 才会真正调用视觉模型。
    "ocr.model": "cn:deepseek-v4-flash",  # OCR 使用的视觉模型名。
    "ocr.dpi": "120",  # PDF 渲染为图片时的 DPI；越高越清晰，但更慢、更占内存。
    "ocr.max_pages": "20",  # 一次 OCR 最多处理的页数；parser.py 还会再限制到 60。
    "ocr.min_chars_per_page": "450",  # 页面文本层平均字符数低于该值时，parser.py 才考虑启用 OCR。
    "chart.enabled": "1",  # 图表理解总开关：1=启用；同时要求模型网关可用。
    "chart.model": "cn:deepseek-v4-flash",  # 图表理解使用的视觉模型名。
    "chart.max_pages": "6",  # 一次图表提取最多处理的页数；parser.py 还会再限制到 30。
    "chart.dpi": "140",  # 图表页面渲染 DPI；parser.py 会再限制在 90～300。
}


# ---------------------------------------------------------------------------
# 函数：ensure_defaults
# 作用：应用启动时确保 app_setting 表中存在全部默认配置键。
# 幂等：ON CONFLICT(key) DO NOTHING —— 已存在的键不更新，因此不会覆盖管理员
#       在后台保存的值；以后新增的默认键会在下次启动时自动补齐。
# 事务：with db() 正常退出时 commit，任意异常 rollback 后继续向上抛。
# 返回：None（本函数只产生数据库副作用）。
# ---------------------------------------------------------------------------
def ensure_defaults() -> None:
    # 打开数据库连接并进入事务；db 在正常退出时 commit，异常时 rollback，最后 close。
    with db() as conn:
        # 逐项写入默认值；遍历顺序与 DEFAULTS 的插入顺序一致。
        for key, value in DEFAULTS.items():
            # 使用参数化 SQL 写入，? 由 db 层按后端转换，绝不能把 key/value 拼进 SQL。
            # 表结构：app_setting(key 主键, value 文本值, updated_at 更新时间)。
            # ON CONFLICT(key) DO NOTHING 是 SQLite/PostgreSQL 通用的“存在即跳过”语法。
            conn.execute(
                "INSERT INTO app_setting (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO NOTHING",
                # 三个参数依次是键名、默认值、当前时间；iso() 统一生成带时区的时间。
                (key, value, iso()),
            )


# ---------------------------------------------------------------------------
# 函数：get —— 读取单个设置并返回字符串
# 查找顺序：数据库 app_setting 中有该 key（包括空字符串）→ 直接返回；
#           否则取 DEFAULTS 中的默认值 → 否则用调用方传入的 default → 最后空字符串。
# 参数：key 为设置名；default 是调用方兜底值，可为 None。
# 返回：str；查不到不会报错。
# ---------------------------------------------------------------------------
def get(key: str, default: str | None = None) -> str:
    # 只查询 value 一列；fetch_one 返回 dict 或 None，参数化查询避免 SQL 注入。
    row = fetch_one("SELECT value FROM app_setting WHERE key=?", (key,))
    # 用 is not None 判断：空字符串也是有效配置，不能把“有记录但值为空”当成没查到。
    if row is not None:
        # 数据库优先：管理员改过的值会覆盖 DEFAULTS。
        return row["value"]
    # 数据库没有时依次回退到 DEFAULTS、调用方 default；两者都没有则返回空串。
    return DEFAULTS.get(key, default if default is not None else "")


# ---------------------------------------------------------------------------
# 函数：get_bool —— 把文本设置解析成布尔值
# 规则：仅 "0"、"false"、"False"、"" 这四种文本视为 False，其它任何字符串都是 True。
# 注意：判断区分大小写，所以 "FALSE"、"no" 仍会被当成 True；这是本项目既定语义。
# 参数：default 决定“数据库和 DEFAULTS 都没有该键时”的默认布尔值。
# 返回：bool。
# ---------------------------------------------------------------------------
def get_bool(key: str, default: bool = True) -> bool:
    # 先把布尔默认值转成 "1"/"0" 再交给 get()，复用统一的数据库/默认值查找逻辑；
    # 然后做“黑名单式”判断：只有列出的四种文本是 False，其余一律为 True。
    return get(key, "1" if default else "0") not in ("0", "false", "False", "")


# ---------------------------------------------------------------------------
# 函数：get_float —— 读取浮点配置
# 做什么：把数据库中的 TEXT 转成 float；非法文本、None 或类型错误时返回 default。
# 注意：这里只保证“能转换”，不限制数值范围；温度范围在 set_many 写入时限制。
# 返回：float。
# ---------------------------------------------------------------------------
def get_float(key: str, default: float) -> float:
    # 尝试把 get() 取到的文本转为浮点数；get() 内部已处理键不存在的情况。
    try:
        # 转换成功就走正常返回路径。
        return float(get(key, str(default)))
    # 捕获所有转换异常（主要是 ValueError/TypeError），避免一个坏配置让业务报错。
    except Exception:
        # 兜底返回调用方给的默认值。
        return default


# ---------------------------------------------------------------------------
# 函数：get_int —— 读取整数配置
# 技巧：先 float 再 int，因此 "12.0" 可用，"12.9" 会被截断为 12（向零取整）。
# 异常：非法文本或 None 时静默回退 default，体现“配置损坏不能拖垮主流程”的取舍。
# 返回：int。
# ---------------------------------------------------------------------------
def get_int(key: str, default: int) -> int:
    # 用 try 包住整个“读取 + 转换”过程，任何一步失败都不会让调用方崩溃。
    try:
        # 先转 float 以兼容带小数点的文本，再由 int 截断为整数。
        return int(float(get(key, str(default))))
    # 任意转换异常都回退默认值。
    except Exception:
        # 返回调用方给的整数默认值。
        return default


# ---------------------------------------------------------------------------
# 函数：all_settings —— 返回后台设置页所需的完整快照
# 返回结构：{"values": {key: value, ...}, "updated_at": {key: ISO 时间, ...}}
# 特点：先复制 DEFAULTS 再叠加数据库值，所以从未落库的默认键也会出现在 values，
#       但 updated_at 只包含数据库里真实存在的键。
# 排序：SQL ORDER BY key 让管理页面每次展示顺序稳定。
# 返回：dict；本函数只读，不修改数据库。
# ---------------------------------------------------------------------------
def all_settings() -> dict:
    # 一次取回所有设置，按 key 排序；fetch_all 返回 list[dict]。
    rows = fetch_all("SELECT key, value, updated_at FROM app_setting ORDER BY key")
    # dict(DEFAULTS) 创建浅拷贝，后续修改不会污染模块级默认字典。
    data = dict(DEFAULTS)
    # 记录每个键的最后更新时间；纯默认值没有数据库记录，因此不会出现在这里。
    updated = {}
    # 遍历数据库行，把真实值叠加到默认值之上。
    for row in rows:
        # row 是 dict；数据库值优先于 DEFAULTS。
        data[row["key"]] = row["value"]
        # 同步记录更新时间，供前端提示“最后保存于……”。
        updated[row["key"]] = row["updated_at"]
    # 返回统一结构：values 给表单显示，updated_at 给界面展示。
    return {"values": data, "updated_at": updated}


# ---------------------------------------------------------------------------
# 函数：set_many —— 批量保存后台设置（管理端 PUT /admin/settings 的落库核心）
# 处理流程：
#   1. allowed 白名单过滤，未知键直接跳过，防止任意 key 污染配置表；
#   2. llm.temperature 限制到 [0.0, 1.5]，非法值跳过（保留原值）；
#   3. 各类数量/DPI 限制到 [1, 各自上限]，非法值跳过；
#   4. 参数化 UPSERT：不存在则插入，存在则更新 value 和 updated_at；
#   5. 提交后删除 meta:v2 缓存，让 /api/v1/meta 重新读取最新配置。
# 参数：values 是 {设置名: 新值} 字典，值可能是 str/int/float。
# 返回：调用 all_settings() 得到的最新完整设置快照。
# 安全：鉴权在 admin.py 的 admin_principal，本函数默认调用者已获管理员权限。
# 异常：数据库异常从 with db() 抛出并回滚；Redis 清理失败被吞掉，不影响保存结果。
# ---------------------------------------------------------------------------
def set_many(values: dict) -> dict:
    # allowed 是允许写入的键集合：先取 DEFAULTS 的全部键，再并上一组显式键名。
    # 当前这些显式键都已在 DEFAULTS 中，属于“重复但更醒目”的防御性白名单；
    # 以后调整 DEFAULTS 时，这段仍能清楚表达后台可写字段的范围。
    allowed = set(DEFAULTS) | {"llm.model", "llm.fallback_model", "llm.temperature", "llm.max_tokens",
                                "llm.enabled", "guide.system_prompt", "guide.default_question_count",
                                "guide.max_question_count", "guide.hint_levels", "chart.enabled",
                                "chart.model", "chart.max_pages", "chart.dpi"}
    # 开启事务：本批设置要么一起提交，要么因异常一起回滚。
    with db() as conn:
        # 遍历调用方提交的键值对；字典本身无序，但保存结果与顺序无关。
        for key, value in values.items():
            # 白名单校验：不认识的键直接跳过，不报错也不写入。
            if key not in allowed:
                # continue 只跳过当前非法项，其余合法设置仍会保存。
                continue
            # 温度单独做浮点范围校验：0.0 最保守，1.5 最发散。
            if key == "llm.temperature":
                # try 包住转换与夹取，避免 "abc" 之类的输入中断整批保存。
                try:
                    # float(value) 可能抛异常；max/min 夹到 [0,1.5] 后 str() 回文本格式。
                    value = str(max(0.0, min(1.5, float(value))))
                # 非法温度直接放弃该项，保留数据库里的旧值。
                except Exception:
                    # continue 继续处理下一个设置，不把整批请求判为失败。
                    continue
            # 下面这些键是整数型配置，需要统一做范围校验：
            #   llm.max_tokens            → 上限 32000
            #   guide.* / ocr.* / chart.* → 默认上限 30，再按 key 单独覆盖
            # 数字先经 float 再 int，兼容 "12.0" 这类前端传值；最终统一存为字符串。
            if key in ("llm.max_tokens", "guide.default_question_count", "guide.max_question_count", "guide.hint_levels",
                       "ocr.dpi", "ocr.max_pages", "ocr.min_chars_per_page", "chart.max_pages", "chart.dpi"):
                # 捕获非法数字；非法值跳过，不中断其它键的保存。
                try:
                    # 默认上限 30；llm.max_tokens 是唯一例外，最多 32000。
                    upper = 32000 if key == "llm.max_tokens" else 30
                    # OCR 渲染 DPI 上限 180，避免高分辨率图片拖慢处理或撑爆内存。
                    if key == "ocr.dpi":
                        # 单独给 OCR 渲染分辨率赋值上限。
                        upper = 180
                    # OCR 页数、最小字符阈值允许到 2000。
                    if key in ("ocr.max_pages", "ocr.min_chars_per_page"):
                        # 对 ocr.max_pages 和 ocr.min_chars_per_page 统一覆盖为 2000。
                        upper = 2000
                    # 图表相关键单独处理。
                    if key in ("chart.max_pages", "chart.dpi"):
                        # 图表 DPI 上限 300，图表最大页数仍为 30。
                        upper = 300 if key == "chart.dpi" else 30
                    # 先 int(float()) 截断小数，再用 max(1, min(upper, ...)) 夹到 [1, upper]，
                    # 最后 str() 转回数据库需要的文本格式。
                    value = str(max(1, min(upper, int(float(value)))))
                # 任意转换异常都跳过该键，避免一条坏数据影响整批。
                except Exception:
                    # 继续下一个设置。
                    continue
            # 参数化 UPSERT：
            #   INSERT ... ON CONFLICT(key) DO UPDATE —— 键不存在则插入，
            #   存在则覆盖 value 与 updated_at；与 ensure_defaults 的 DO NOTHING 不同，
            #   这里代表“管理员明确保存”。两段字符串只是源码换行，会拼成一条完整 SQL。
            conn.execute(
                "INSERT INTO app_setting (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                # 参数依次是：键、强制转字符串的值、当前 ISO 时间；用户输入不拼进 SQL。
                (key, str(value), iso()),
            )
    # 数据库事务已经提交；下面尝试清理缓存。缓存是可选优化，失败不影响配置保存。
    try:
        # 延迟导入可选平台组件：未启用 Redis/平台模式时，也不会让本模块导入失败。
        from ..platform import redis_client
        # 删除 /api/v1/meta 的缓存键；redis_client 在未配置 Redis 时也会清理进程内缓存。
        redis_client.delete("meta:v2")
    # 缓存清理属于“尽力而为”：Redis 断连等异常不应让已成功的设置保存报错。
    except Exception:
        # pass：显式忽略异常，继续返回最新设置。
        pass
    # 重新从数据库读取并返回完整快照，管理后台可直接用该结果回显。
    return all_settings()
