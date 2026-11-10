# =============================================================================
# 【文件定位】alun/app/llm.py —— A论后端的“大模型网关适配层”
#
# 一、这个文件负责什么
#   1. 统一封装 OpenAI 兼容的 Chat Completions 接口，让业务代码不必直接拼 URL、
#      请求头或处理 HTTP 细节；
#   2. 判断模型功能是否开启：静态开关（环境变量/配置）+ 网关是否配置 + 后台运行设置；
#   3. 按“主模型 → 备用模型”的顺序容错调用，并在网关不支持 response_format 时降级重试；
#   4. 从模型自由文本中提取 JSON，并在 JSON 不合法时让模型自修复一次；
#   5. 把 token/credit 用量尽量写入 usage_log 表（失败不影响主流程）；
#   6. 提供 /v1/models 健康检查，供启动检查、管理后台使用。
#
# 二、在整个应用中的位置
#   FastAPI 路由 / 业务层（main、admin、assistant、generator、judge、sessions、parser）
#           │ 同步函数调用
#           ▼
#   llm.py【本文件】—— 只负责“模型网关客户端”这一件事
#           ├─ httpx.post() ──► OpenAI 兼容网关 /v1/chat/completions
#           ├─ config.GATEWAY / ENABLE_LLM ──► 地址、密钥、默认模型、总开关
#           ├─ settings ──► 后台数据库里可热改的模型、开关、温度和 token 上限
#           └─ db.db / iso ──► usage_log 用量记账
#
# 三、主要调用关系
#   - main.py：模块级 import，用于健康检查和状态展示；
#   - admin.py：调用 health() 展示网关状态；
#   - generator.py / judge.py：先 available()，再 chat_json() 生成题目、评判答案；
#   - sessions.py：追问与追问评判调用 chat()；
#   - assistant.py：助手问答调用 chat()；
#   - parser.py：OCR、图表理解等可选能力调用 chat()/chat_json()。
#
# 四、关键设计（也是阅读重点）
#   - 同步阻塞：这里使用 httpx 的同步 API，不是 async def；在 FastAPI 的异步路由中，
#     调用方必须把它放进线程池，否则会卡住事件循环；
#   - 并发限流：_SEMAPHORE 是“进程内”最多 3 个并发 HTTP 请求，不是跨机器的全局限流；
#   - 降级策略：主模型失败后尝试备用模型；两种模型都失败才抛 LLMError；
#   - JSON 修复：chat() 只返回文本，chat_json() 才负责解析并在失败后请求模型重答；
#   - 错误模型：没有业务错误码；统一抛 LLMError（继承 RuntimeError），HTTP 状态码写在消息里；
#   - 权限边界：本层不做登录、租户或文档权限校验，只接收上游已经授权后的 document_id/session_id；
#     真正越权问题应由路由和业务层拦截；
#   - 密钥安全：API Key 只放在 Authorization 请求头中，不写日志；但网关错误正文会被截取 300 字返回。
#
# 五、建议阅读顺序
#   available() → _extract_json() → _post() → chat() → chat_json() → _log_usage() → health()。
#
# 六、说明
#   本文件是学习注释版：原文件的每条可执行语句、字符串、缩进、导入和空行都保留；
#   新增内容全部是 # 注释，没有新增会参与运行的语句。
# =============================================================================
# 下面这行是原有的模块文档字符串（docstring）；注释写在它前面不会改变 __doc__。
"""OpenAI 兼容模型网关客户端：结构化输出、重试、用量记账。"""
# from __future__ import annotations：延迟求值类型注解。
# 这样 list[dict]、str | None 等写法会以字符串形式保存，兼容较老的 Python 解释器；
# 它必须位于模块 docstring 之后、其它可执行语句之前。
from __future__ import annotations

# json：标准库 JSON 编解码器；_extract_json() 用 json.loads() 把模型文本变成 Python 对象。
# 注意：它的解析非常严格，不允许尾逗号、注释或未加引号的键，所以后面还要做修复重试。
import json
# re：正则表达式库，用来剥掉模型常见的 Markdown 代码围栏（```json ... ```）。
import re
# threading：提供 BoundedSemaphore，做进程内并发限流。
import threading
# time：记录请求开始时间，预留给耗时统计；当前版本只赋值，没有再参与计算。
import time
# Any：表示“任意类型”。JSON 可能是对象、数组、字符串或数字，因此 _extract_json() 返回 Any。
from typing import Any

# httpx：现代 HTTP 客户端。这里使用同步的 httpx.post()/httpx.get()，
# 而不是 requests，也不是 httpx.AsyncClient；调用线程会被网络 I/O 阻塞。
import httpx

# 从配置模块导入两个“启动时确定”的值：
#   ENABLE_LLM —— 环境变量级别的总开关；GATEWAY —— 网关地址、密钥、默认模型、超时等字典。
# GATEWAY.get("enabled") 由 _load_gateway_config() 根据是否读到 API Key 计算得出。
from .config import ENABLE_LLM, GATEWAY
# db：数据库连接上下文管理器；成功退出自动 commit，异常时 rollback 并关闭连接。
# iso：生成东八区、精确到秒的 ISO 时间字符串，用作 usage_log.ts。
from .db import db, iso
# 导入 settings 模块本身，而不是只导入某个函数：这样每次调用都读取运行时的数据库设置，
# 管理员在后台修改模型/温度/token 上限后无需重启进程即可生效。
from . import settings

# 进程级“并发闸门”：同一时刻最多允许 3 个 HTTP 请求真正发往模型网关。
# BoundedSemaphore 比普通 Semaphore 多一道保护：release() 次数超过初始值会直接报错，
# 可以尽早暴露“多 release 少 acquire”的编程错误。
# 注意：它只约束单个 Python 进程；多 worker/多容器部署时，总并发数会成倍增加。
_SEMAPHORE = threading.BoundedSemaphore(3)


# 自定义业务异常：把所有模型层的失败统一成一种类型，方便路由层捕获并返回友好提示。
# 继承 RuntimeError 而不是 Exception，语义上表示“运行期外部依赖失败”，不是参数类型错误。
class LLMError(RuntimeError):
    # 类体不能为空，pass 只占位、不执行任何操作。
    # 这里不需要额外字段：错误原因直接放在异常消息 str(exc) 中。
    pass


# 判断当前是否“允许且看起来能够”调用模型。
# 返回 True 只代表配置层面可用，不保证网络通、密钥有效或模型一定成功；真正调用仍可能失败。
def available() -> bool:
    # 两个静态开关任一关闭就立即返回 False：
    #   ENABLE_LLM 是部署时的环境变量总开关；GATEWAY["enabled"] 表示是否成功加载到 API Key。
    # 用 or 短路可以避免在功能关闭时还去查数据库。
    if not ENABLE_LLM or not GATEWAY.get("enabled"):
        # 明确返回 False，调用方据此跳过 AI 功能或显示“未配置”。
        return False
    # 第三个开关放在数据库 app_setting 表中，是管理员可在后台热修改的 llm.enabled。
    # 数据库可能尚未初始化、被锁住或连接失败，因此这里必须捕获异常。
    try:
        # settings.get_bool() 读取字符串并转布尔；默认 True 表示“配置项不存在时按开启处理”。
        # 数据库中的 "0"、"false"、"False"、空串会被视为 False。
        return settings.get_bool("llm.enabled", True)
    # 捕获所有异常：可用性查询不应该让应用启动或页面渲染直接崩溃。
    except Exception:
        # 读取设置失败时选择“失败开放”（fail-open）：只要静态配置和 Key 看起来存在，就暂时认为可用。
        # 代价是数据库故障时可能先返回 True，直到真正调用模型才暴露错误；这是可用性优先的取舍。
        return True


# 从模型输出中尽量提取一个 JSON 值。
# 处理顺序：去空白 → 剥 Markdown 围栏 → 直接 json.loads → 扫描第一个平衡的 {…} 或 […] → 再解析。
# 为什么要扫描：模型经常在 JSON 前后加“好的，结果如下”等说明文字。
def _extract_json(text: str) -> Any:
    # text or ""：兼容调用方传入 None 的情况；strip() 去掉首尾空白和换行，避免围栏判断失败。
    # 注意这是重新绑定局部变量，不会修改调用方传入的原字符串。
    text = (text or "").strip()
    # 只有以三个反引号开头才尝试剥代码围栏；普通说明文字不会进入这个分支。
    if text.startswith("```"):
        # 去掉开头的 ``` 及可选语言标记（如 json、JSON、python）和紧随的空白。
        # 字符类 [a-zA-Z]* 只允许字母，不匹配空格或换行，避免误吞正文。
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        # 去掉结尾的 ```（前面允许有空白），得到裸 JSON 文本。
        text = re.sub(r"\s*```$", "", text)
    # 先尝试最理想的情况：整段文本本身就是合法 JSON。
    try:
        # json.loads() 成功时直接返回 dict/list/str/int 等 Python 对象；
        # 函数返回类型写 Any，就是因为 JSON 顶层不一定是对象。
        return json.loads(text)
    # 解析失败不在这里报错，而是继续走“从混合文本中截取 JSON”的兜底逻辑。
    except Exception:
        # pass 表示忽略这个异常；真正的错误要等后面确认找不到 JSON 时才抛出。
        pass
    # 寻找第一个左花括号 {；如果模型返回的是数组而不是对象，再找左方括号 [。
    # 注意当前写法优先选择 {：即使 [ 出现得更早，只要文中还有 {，也会从 { 开始。
    start = text.find("{")
    # start < 0 表示完全没有找到 {。
    if start < 0:
        # 退而求其次，定位数组的起始 [。
        start = text.find("[")
    # 两种起始符都不存在，说明这段文本里没有可提取的 JSON。
    if start < 0:
        # 抛出统一的 LLMError，消息是中文，便于前端或日志直接展示。
        raise LLMError("模型输出中没有 JSON")
    # 手写一个轻量级括号扫描器：
    #   depth  —— 当前嵌套深度；每遇到 { 或 [ 加一，每遇到 } 或 ] 减一；
    #   in_str —— 是否位于 JSON 字符串内部；
    #   esc    —— 当前字符是否被反斜杠转义。
    # 状态变量的意义：字符串里的 "{123}" 不应被当成结构括号。
    depth = 0
    in_str = False
    esc = False
    # 从 JSON 疑似起点开始逐字符扫描；range 不包含 len(text)，所以后面用 i + 1 取切片。
    for i in range(start, len(text)):
        # ch 是当前字符。把索引和字符都保留下来，是为了最后能精确切片。
        ch = text[i]
        # 如果正在字符串内部，只处理转义和结束引号，不统计括号。
        if in_str:
            # esc=True 表示上一个字符是反斜杠，当前字符无论是引号还是反斜杠都只是普通内容。
            if esc:
                # 消费掉转义状态，下一个字符恢复正常判断。
                esc = False
            # 遇到反斜杠（字符串字面量在源码中写成 "\\"）时，标记下一个字符被转义。
            elif ch == "\\":
                # 例如 JSON 中的 \" 不会结束字符串。
                esc = True
            # 遇到未转义的英文双引号，说明字符串结束。
            elif ch == '"':
                # 退出字符串状态，后续字符重新参与括号计数。
                in_str = False
            # 字符串内部的字符已经处理完，直接跳到下一个字符，绝不能统计其中的花括号。
            continue
        # 不在字符串中时，遇到双引号表示进入字符串。
        if ch == '"':
            # 开始跟踪字符串，防止把字符串内容里的括号误认为结构。
            in_str = True
        # 遇到任意左括号：对象 { 或数组 [，嵌套层级加一。
        elif ch in "{[":
            # 深度加一。这里把两种括号放在一起计数，适合常见且格式正确的模型输出。
            depth += 1
        # 遇到任意右括号：对象 } 或数组 ]。
        elif ch in "}]":
            # 先减一层，再判断是否回到最外层。
            depth -= 1
            # 深度回到 0，说明从 start 开始的这个结构已经闭合。
            if depth == 0:
                # 切出 text[start : i + 1]（包含当前右括号）并解析。
                # 这是最常见路径：从说明文字中截出完整 JSON。若括号类型不匹配，json.loads() 会抛异常并向上传播。
                return json.loads(text[start : i + 1])
    # 扫描到文本末尾仍未闭合，抛“JSON 不完整”。
    # 与“没有 JSON”区分开，便于排查是模型截断还是根本没按要求输出。
    raise LLMError("JSON 不完整")


# 真正执行一次 HTTP POST 的底层函数。
# 它不判断业务状态、不重试、不做 JSON 提取；这些职责都在 chat() 中，保持单一职责。
def _post(payload: dict, timeout: float) -> dict:
    # 拼接 OpenAI Chat Completions 路径。rstrip("/") 防止 base_url 末尾已有 / 时出现 //。
    # 例如 http://127.0.0.1:7863/v1 + /chat/completions。
    url = GATEWAY["base_url"].rstrip("/") + "/chat/completions"
    # 请求头字典：
    #   Authorization: Bearer <API Key> —— OpenAI 兼容网关的认证方式；
    #   Content-Type: application/json —— 告诉服务端请求体是 JSON。
    # 密钥来自 GATEWAY，绝不能把它打印到日志或返回给前端。
    headers = {
        "Authorization": "Bearer " + GATEWAY["api_key"],
        "Content-Type": "application/json",
    }
    # with 会在进入时 acquire、离开时 release；即使网络异常也一定释放信号量，避免并发额度泄漏。
    # 这段是阻塞式网络调用：同一线程在此期间无法处理其它任务。
    with _SEMAPHORE:
        # httpx.post 的 json=payload 会自动序列化请求体并设置 JSON 内容；timeout 同时约束连接和读取。
        # httpx 对 4xx/5xx 默认不抛异常，所以下面必须自己检查 status_code。
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    # HTTP 状态码 >= 400 表示客户端或服务端错误，需要转成统一的 LLMError。
    if resp.status_code >= 400:
        # 把状态码和服务端正文前 300 个字符放进异常，便于看到网关的具体报错。
        # 截断可以避免超长 HTML 错误页撑爆日志；但正文可能包含敏感信息，生产日志要控制访问权限。
        raise LLMError(f"网关返回 {resp.status_code}: {resp.text[:300]}")
    # 把响应体解析为字典并返回。
    # 如果网关返回的不是合法 JSON，resp.json() 会抛异常，由 chat() 外层捕获并尝试备用模型。
    return resp.json()


# chat()：对业务层暴露的统一文本生成接口。
# 参数说明：
#   messages     —— OpenAI 格式的消息列表，如 [{"role": "user", "content": "..."}]；
#   *            —— 星号后的参数只能按关键字传递，防止长参数列表顺序传错；
#   kind         —— 用量分类标签（chat/generate/judge 等），只用于 usage_log；
#   model        —— 本次指定模型；为 None 时依次回退到后台设置和 GATEWAY 默认模型；
#   max_tokens   —— 期望的最大输出 token 数，最终还会被后台全局上限钳制；
#   temperature  —— 采样温度，越高越发散，越低越稳定；
#   json_mode    —— 是否请求 response_format={"type":"json_object"}；
#   document_id  —— 关联的文档 ID，仅用于用量审计，可为空；
#   session_id   —— 关联的答题/对话会话 ID，仅用于用量审计，可为空。
# 返回值：模型回复的纯文本字符串。
# 异常：不可用、HTTP 错误、空内容或所有候选模型都失败时抛 LLMError。
def chat(
    messages: list[dict],
    *,
    kind: str = "chat",
    model: str | None = None,
    max_tokens: int = 3000,
    temperature: float = 0.3,
    json_mode: bool = True,
    document_id: str | None = None,
    session_id: str | None = None,
) -> str:
    # 原有函数 docstring 保留不动：它说明“返回文本，并自动尝试主模型与备用模型”。
    # 实现上，主模型失败不一定马上换模型；若错误疑似来自 response_format，会先对同一模型再试一次。
    """调用模型并返回文本；自动尝试主模型与备用模型。"""
    # 调用前先做低成本可用性检查；关闭模型功能时不应发出任何网络请求。
    if not available():
        # 用面向用户的中文消息抛错，路由层可以直接转成 503/400 类响应。
        raise LLMError("模型网关未配置或已在后台关闭")
    # 确定主模型，优先级从高到低：本次显式传入 > 后台设置 llm.model > 配置文件默认模型。
    # 使用 or 意味着空字符串也会被跳过，不会把空模型名发给网关。
    primary = model or settings.get("llm.model", GATEWAY["model"])
    # 确定备用模型：后台设置 llm.fallback_model > 配置里的 fallback_model > 主模型本身。
    # 如果最终与主模型相同，后面的去重逻辑会把它合并，不会重复调用同一个模型。
    fallback = settings.get("llm.fallback_model", GATEWAY.get("fallback_model") or primary)
    # 按顺序组成候选列表；后面的 dict.fromkeys 会去重但保留这个先后顺序。
    models = [primary, fallback]
    # 运行时的温度覆盖：管理员在后台设置的 llm.temperature 优先于调用方参数。
    # 这使全站输出风格可以由后台统一控制，但也意味着调用方传入的温度通常会被覆盖。
    try:
        temperature = settings.get_float("llm.temperature", temperature)
    # 设置读取失败（例如数据库临时不可用）时保留调用方传入的温度，保证仍可继续。
    except Exception:
        # 静默回退，不因为读不到可选项而中断主流程。
        pass
    # 下面计算最终 max_tokens：后台设置是“全局上限”，调用方只能请求更小的值。
    try:
        # 后台设置是全局上限，调用方可以请求更小的输出；不能反过来把 1800
        # 的题目生成放大到 32000，否则模型会长篇输出、时延不可控。
        # 拆解这一行：
        #   1) settings.get_int("llm.max_tokens", max_tokens) 读取后台值，缺失/非法时用本次参数；
        #   2) int(...) 再确保是整数；
        #   3) min(32000, ...) 限制硬上限，防止一次请求消耗过多 token；
        #   4) max(64, ...) 保证至少 64，避免 0 或负数导致网关拒绝。
        # 所以 setting_cap 是“系统允许的最大输出预算”。
        setting_cap = max(64, min(32000, int(settings.get_int("llm.max_tokens", max_tokens))))
        # 再与调用方请求值取较小者：调用方只能向下调，不能突破后台上限。
        # 最终范围仍是 [64, 32000]；若调用方请求 10，会被抬高到 64，这是当前实现的边界行为。
        # 整个 try 中任何一步因类型转换失败，都会跳到 except，保留原来的 max_tokens。
        max_tokens = max(64, min(setting_cap, int(max_tokens)))
    # 后台值损坏或数据库读取失败时，不对 max_tokens 做任何改动。
    except Exception:
        # pass 表示“使用调用方原来的值继续”，而不是让请求失败。
        pass
    # 保存最后一个异常，循环结束后用它生成统一错误消息；初值为 None 便于最终兜底。
    last_error: Exception | None = None
    # 遍历去重后的模型列表。dict.fromkeys(...) 保持插入顺序；
    # 生成器表达式过滤掉 None/空字符串，避免向网关发送空模型名。
    # 变量 idx 当前没有参与逻辑，保留它不影响行为，也方便调试时观察第几个候选。
    for idx, name in enumerate(dict.fromkeys(m for m in models if m)):
        # 构造 OpenAI Chat Completions 请求体（字典）：
        #   model       —— 当前候选模型名；
        #   messages    —— 对话历史，角色通常是 system/user/assistant；
        #   stream=False—— 一次性拿到完整结果，简化解析、重试和用量统计；
        #   max_tokens  —— 上面钳制后的输出上限；
        #   temperature —— 上面确定的采样温度。
        # 每个候选模型都重新构造一份 payload，避免后面 pop 修改影响下一轮。
        payload: dict[str, Any] = {
            "model": name,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # 只有 json_mode=True 才声明希望返回 JSON 对象。
        # 这不是本地解析开关，而是给网关的提示/约束；部分模型或网关版本并不支持。
        if json_mode:
            # 按 OpenAI 规范加入 response_format，值是一个小字典。
            payload["response_format"] = {"type": "json_object"}
        # 开始一次候选模型调用；这里的 try 覆盖网络、响应解析、内容检查和用量记账全过程。
        try:
            # 记录开始时间。当前只赋值，没有用于计算耗时；保留原语句以维持行为完全一致。
            started = time.time()
            # _post 内部会拿信号量、发请求、检查 HTTP 状态并返回 JSON 字典。
            # timeout 从 GATEWAY 读取，默认 90 秒；它是单次 HTTP 请求的超时。
            data = _post(payload, GATEWAY.get("timeout", 90))
            # 从响应中取第一条 choice 的 message。
            # data.get("choices") or [{}] 防止 choices 缺失或为空列表；[0] 再取首项。
            # 如果服务端返回结构异常，这里可能抛 IndexError/AttributeError，随后进入 except 尝试备用模型。
            message = (data.get("choices") or [{}])[0].get("message", {})
            # 取 message.content；有些兼容网关可能返回 null，所以再用 or "" 归一成空字符串。
            # 当前实现假定 content 是字符串；若返回多模态列表，后续 .strip() 会失败并被捕获。
            text = message.get("content") or ""
            # usage 是网关返回的计费信息；缺失时用空字典，后续按 0 处理。
            usage = data.get("usage") or {}
            # 先记用量再检查内容是否为空：即使最终判定为空并换模型，这次请求的 token 也已经消耗。
            # 记账失败不会抛异常（_log_usage 内部吞掉），不会影响正常返回。
            _log_usage(kind, name, usage, document_id, session_id)
            # strip() 去掉纯空白；空内容视为无效响应，不能交给 JSON 解析或业务层。
            if not text.strip():
                # 抛错后会进入本层 except，记录错误并继续尝试下一个候选模型。
                raise LLMError("模型返回空内容")
            # 返回原始文本（保留模型输出的换行和格式，不在这里 strip，避免改变内容）。
            return text
        # 宽泛捕获 Exception，是为了让任何单模型故障都能触发降级，包括 httpx 网络错误、
        # JSON 解码错误、空内容 LLMError 以及响应结构异常。
        # 代价是可能连编程错误也一起吞掉，排障时要看最后的 last_error。
        except Exception as exc:  # noqa: BLE001
            # 保存当前异常；如果后面没有模型成功，最终把这条原因抛给调用方。
            last_error = exc
            # 部分网关不支持 response_format，去掉后重试同一模型
            # 这是对错误文本的字符串匹配，比较朴素：只有异常消息中出现 response_format 才触发。
            # 如果网关返回的报错措辞不同，就不会走这个专用重试，只能直接尝试备用模型。
            if json_mode and "response_format" in str(exc):
                # 从 payload 中删除 response_format，其他参数（模型、消息、温度等）保持不变。
                payload.pop("response_format", None)
                # 对同一个模型发起第二次 HTTP 请求；这第二次不计入“候选模型”循环次数。
                try:
                    # 重新取响应，逻辑与第一次完全相同。
                    data = _post(payload, GATEWAY.get("timeout", 90))
                    # 再次安全地取第一条 message，避免 choices 缺失时崩溃。
                    message = (data.get("choices") or [{}])[0].get("message", {})
                    # 取文本内容并归一化空值。
                    text = message.get("content") or ""
                    # 第二次请求同样记用量；usage 缺失时传空字典。
                    _log_usage(kind, name, data.get("usage") or {}, document_id, session_id)
                    # 只有非空文本才视为成功。
                    if text.strip():
                        # 成功立即返回，不再尝试后面的备用模型。
                        return text
                # 第二次请求也失败时捕获新异常。
                except Exception as exc2:  # noqa: BLE001
                    # 用第二次的异常覆盖 last_error，最终错误消息更贴近最后一次真实失败。
                    last_error = exc2
            # 本轮失败，继续 for 循环的下一个候选模型。
            # 注意：如果第二次请求成功但内容为空，不会更新 last_error，最终可能报告第一次的 response_format 错误。
            continue
    # 所有候选模型都失败后抛 LLMError。str(last_error) 保留原始异常文本；
    # 若连异常都没有（理论上的空列表），使用中文兜底“模型调用失败”。
    # 调用方只看到 LLMError，具体是超时、401 还是 500 要看消息内容。
    raise LLMError(str(last_error or "模型调用失败"))


# chat_json()：在 chat() 的文本结果上增加 JSON 解析和一次“修复重试”。
# **kwargs 会把 kind/model/max_tokens/json_mode/document_id/session_id 等原样转交给 chat()。
# 返回值是 json.loads() 产生的任意 Python 对象，所以注解为 Any。
def chat_json(messages: list[dict], **kwargs) -> Any:
    # 原有 docstring 保留：调用模型并解析 JSON，解析失败时发起一次修复。
    """调用模型并解析 JSON，解析失败时发起一次修复。"""
    # 第一次正常调用；如果网络或网关失败，异常在这里直接向上抛，不会进入下面的修复逻辑。
    raw = chat(messages, **kwargs)
    # 只有“已经拿到文本、但解析失败”的情况才进入修复分支。
    try:
        # 成功解析就直接返回，不再发起第二次模型请求。
        return _extract_json(raw)
    # 捕获解析异常（JSONDecodeError、LLMError 等），准备让模型自己纠正格式。
    except Exception:
        # 构造修复对话：原 messages + 模型刚才的错误输出 + 一条严格的用户指令。
        # 列表相加会创建新列表，不会修改调用方传入的 messages。
        repair = messages + [
            # 把模型上次输出作为 assistant 消息放回上下文；截断到 4000 字符，
            # 避免错误输出过长导致第二次请求超出上下文窗口。
            {"role": "assistant", "content": raw[:4000]},
            # 明确的修复指令：只输出一个合法 JSON 对象，不要解释、不要代码块。
            # 字符串内容保持原样，注释不会改变它。
            {"role": "user", "content": "上面的输出不是合法 JSON。请只输出一个合法 JSON 对象，不要解释、不要代码块。"},
        ]
        # 第二次调用 chat()。kwargs 中排除了 temperature，再显式传 temperature=0.1，意图是让修复更稳定。
        # 但 chat() 内部通常会再用后台 llm.temperature 覆盖它；因此 0.1 只在设置读取失败时作为兜底。
        # 其余参数（包括 json_mode）继续传递，网关仍会收到 JSON 模式请求。
        raw2 = chat(repair, **{k: v for k, v in kwargs.items() if k not in ("temperature",)}, temperature=0.1)
        # 再次提取 JSON 并直接返回。这里没有再包 try：第二次仍不合法时，异常会向上抛给业务层。
        # 因此“修复一次”不是无限重试，避免模型反复失败时拖垮请求。
        return _extract_json(raw2)


# _log_usage()：把一次模型调用的用量写入 usage_log 表。
# 参数：kind=业务分类，model=实际使用的模型，usage=网关返回的计费字典，
# document_id/session_id=可选的业务关联 ID。
# 设计原则是“旁路记账”：任何数据库错误都被吞掉，绝不能因为记账失败让用户拿不到模型结果。
def _log_usage(kind: str, model: str, usage: dict, document_id: str | None, session_id: str | None) -> None:
    # 整个函数体包在 try 中：连接、INSERT、类型转换中的任何异常都不会向上传播。
    try:
        # db() 是上下文管理器：正常退出 commit，异常 rollback，最后 close。
        with db() as conn:
            # 执行参数化 INSERT。SQL 分成两个相邻字符串字面量，Python 会自动拼接成一条完整语句。
            # 列顺序必须与下面参数元组严格一致：8 个列对应 8 个 ? 占位符。
            conn.execute(
                "INSERT INTO usage_log (ts, kind, model, prompt_tokens, completion_tokens, credits, document_id, session_id)"
                " VALUES (?,?,?,?,?,?,?,?)",
                # 参数元组（顺序与上面 8 列一一对应）：
                #   iso() → ts，东八区当前时间；kind → 业务分类；model → 实际模型名；
                #   prompt_tokens/completion_tokens → 输入/输出 token；
                #   credit → 网关返回的积分（非标准字段，缺失按 0）；
                #   document_id/session_id → 关联业务对象，可为 None。
                # 所有值通过 ? 绑定，不拼接到 SQL 字符串中，因此不存在 SQL 注入问题。
                (
                    iso(),
                    kind,
                    model,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    float(usage.get("credit") or 0),
                    document_id,
                    session_id,
                ),
            )
    # 静默捕获记账异常：数据库锁、表不存在、字段类型错误都不影响主业务。
    except Exception:
        # pass 表示放弃本次记账；代价是排障时可能缺少用量记录，需要结合应用日志排查。
        pass


# health()：轻量级健康检查，返回 (是否健康, 说明文本)。
# 供 main.py 的启动依赖检查和管理后台状态页使用。
# 它只判断网关是否可达，不验证模型是否真的能生成内容。
def health() -> tuple[bool, str]:
    # 先检查静态配置和后台开关；完全未启用时不必发网络请求。
    if not available():
        # 返回固定的中文原因，前端可显示为“未配置”。
        return False, "未配置"
    # 网络请求可能超时、DNS 失败或被拒绝，因此整体包在 try 中。
    try:
        # 构造模型列表地址：先去尾部斜杠，再把 base_url 中的 /v1 去掉，最后拼 /v1/models。
        # 意图是兼容 base_url 形如 http://host/v1 或 http://host 的两种配置。
        # replace("/v1", "") 会替换所有出现位置；如果 URL 路径里别处也有 /v1，可能被意外删掉，这是当前实现的边界。
        url = GATEWAY["base_url"].rstrip("/").replace("/v1", "") + "/v1/models"
        # 直接调用 httpx.get，不走 _post、也不占用 _SEMAPHORE；
        # timeout=5 比模型调用短得多，避免健康检查拖慢启动页面。
        # 只取状态码，不解析响应体。
        r = httpx.get(url, headers={"Authorization": "Bearer " + GATEWAY["api_key"]}, timeout=5)
        # status_code < 500 就认为“服务可达”：4xx 也可能是网关正常返回的鉴权/路径错误。
        # 所以 ok=True 只表示没有 5xx，不代表 Key 正确或模型列表接口完全可用。
        # 三元表达式把布尔结果和 ok/HTTP 状态文本一起返回。
        return (r.status_code < 500, "ok" if r.status_code < 500 else f"HTTP {r.status_code}")
    # 连接失败、超时、DNS 错误等任何异常都视为不健康。
    except Exception as exc:  # noqa: BLE001
        # 返回 False 和异常文本前 100 个字符，既能提示原因，又不会让健康检查响应过长。
        return False, str(exc)[:100]
