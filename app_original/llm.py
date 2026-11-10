"""OpenAI 兼容模型网关客户端：结构化输出、重试、用量记账。"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

import httpx

from .config import ENABLE_LLM, GATEWAY
from .db import db, iso
from . import settings

_SEMAPHORE = threading.BoundedSemaphore(3)


class LLMError(RuntimeError):
    pass


def available() -> bool:
    if not ENABLE_LLM or not GATEWAY.get("enabled"):
        return False
    try:
        return settings.get_bool("llm.enabled", True)
    except Exception:
        return True


def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    if start < 0:
        start = text.find("[")
    if start < 0:
        raise LLMError("模型输出中没有 JSON")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise LLMError("JSON 不完整")


def _post(payload: dict, timeout: float) -> dict:
    url = GATEWAY["base_url"].rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + GATEWAY["api_key"],
        "Content-Type": "application/json",
    }
    with _SEMAPHORE:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    if resp.status_code >= 400:
        raise LLMError(f"网关返回 {resp.status_code}: {resp.text[:300]}")
    return resp.json()


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
    """调用模型并返回文本；自动尝试主模型与备用模型。"""
    if not available():
        raise LLMError("模型网关未配置或已在后台关闭")
    primary = model or settings.get("llm.model", GATEWAY["model"])
    fallback = settings.get("llm.fallback_model", GATEWAY.get("fallback_model") or primary)
    models = [primary, fallback]
    try:
        temperature = settings.get_float("llm.temperature", temperature)
    except Exception:
        pass
    try:
        # 后台设置是全局上限，调用方可以请求更小的输出；不能反过来把 1800
        # 的题目生成放大到 32000，否则模型会长篇输出、时延不可控。
        setting_cap = max(64, min(32000, int(settings.get_int("llm.max_tokens", max_tokens))))
        max_tokens = max(64, min(setting_cap, int(max_tokens)))
    except Exception:
        pass
    last_error: Exception | None = None
    for idx, name in enumerate(dict.fromkeys(m for m in models if m)):
        payload: dict[str, Any] = {
            "model": name,
            "messages": messages,
            "stream": False,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            started = time.time()
            data = _post(payload, GATEWAY.get("timeout", 90))
            message = (data.get("choices") or [{}])[0].get("message", {})
            text = message.get("content") or ""
            usage = data.get("usage") or {}
            _log_usage(kind, name, usage, document_id, session_id)
            if not text.strip():
                raise LLMError("模型返回空内容")
            return text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            # 部分网关不支持 response_format，去掉后重试同一模型
            if json_mode and "response_format" in str(exc):
                payload.pop("response_format", None)
                try:
                    data = _post(payload, GATEWAY.get("timeout", 90))
                    message = (data.get("choices") or [{}])[0].get("message", {})
                    text = message.get("content") or ""
                    _log_usage(kind, name, data.get("usage") or {}, document_id, session_id)
                    if text.strip():
                        return text
                except Exception as exc2:  # noqa: BLE001
                    last_error = exc2
            continue
    raise LLMError(str(last_error or "模型调用失败"))


def chat_json(messages: list[dict], **kwargs) -> Any:
    """调用模型并解析 JSON，解析失败时发起一次修复。"""
    raw = chat(messages, **kwargs)
    try:
        return _extract_json(raw)
    except Exception:
        repair = messages + [
            {"role": "assistant", "content": raw[:4000]},
            {"role": "user", "content": "上面的输出不是合法 JSON。请只输出一个合法 JSON 对象，不要解释、不要代码块。"},
        ]
        raw2 = chat(repair, **{k: v for k, v in kwargs.items() if k not in ("temperature",)}, temperature=0.1)
        return _extract_json(raw2)


def _log_usage(kind: str, model: str, usage: dict, document_id: str | None, session_id: str | None) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO usage_log (ts, kind, model, prompt_tokens, completion_tokens, credits, document_id, session_id)"
                " VALUES (?,?,?,?,?,?,?,?)",
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
    except Exception:
        pass


def health() -> tuple[bool, str]:
    if not available():
        return False, "未配置"
    try:
        url = GATEWAY["base_url"].rstrip("/").replace("/v1", "") + "/v1/models"
        r = httpx.get(url, headers={"Authorization": "Bearer " + GATEWAY["api_key"]}, timeout=5)
        return (r.status_code < 500, "ok" if r.status_code < 500 else f"HTTP {r.status_code}")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:100]
