"""运行时可修改的模型与导读设置。"""
from __future__ import annotations

from .config import GATEWAY
from .db import db, fetch_all, fetch_one, iso

DEFAULTS: dict[str, str] = {
    "llm.enabled": "1",
    "llm.model": GATEWAY.get("model") or "cn:deepseek-v4-flash",
    "llm.fallback_model": GATEWAY.get("fallback_model") or "cn:glm-5.3-flash",
    "llm.temperature": "0.45",
    "llm.max_tokens": "1800",
    "guide.system_prompt": "",
    "guide.default_question_count": "12",
    "guide.max_question_count": "30",
    "guide.hint_levels": "3",
    "ocr.enabled": "1",
    "ocr.model": "cn:deepseek-v4-flash",
    "ocr.dpi": "120",
    "ocr.max_pages": "20",
    "ocr.min_chars_per_page": "450",
    "chart.enabled": "1",
    "chart.model": "cn:deepseek-v4-flash",
    "chart.max_pages": "6",
    "chart.dpi": "140",
}


def ensure_defaults() -> None:
    with db() as conn:
        for key, value in DEFAULTS.items():
            conn.execute(
                "INSERT INTO app_setting (key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO NOTHING",
                (key, value, iso()),
            )


def get(key: str, default: str | None = None) -> str:
    row = fetch_one("SELECT value FROM app_setting WHERE key=?", (key,))
    if row is not None:
        return row["value"]
    return DEFAULTS.get(key, default if default is not None else "")


def get_bool(key: str, default: bool = True) -> bool:
    return get(key, "1" if default else "0") not in ("0", "false", "False", "")


def get_float(key: str, default: float) -> float:
    try:
        return float(get(key, str(default)))
    except Exception:
        return default


def get_int(key: str, default: int) -> int:
    try:
        return int(float(get(key, str(default))))
    except Exception:
        return default


def all_settings() -> dict:
    rows = fetch_all("SELECT key, value, updated_at FROM app_setting ORDER BY key")
    data = dict(DEFAULTS)
    updated = {}
    for row in rows:
        data[row["key"]] = row["value"]
        updated[row["key"]] = row["updated_at"]
    return {"values": data, "updated_at": updated}


def set_many(values: dict) -> dict:
    allowed = set(DEFAULTS) | {"llm.model", "llm.fallback_model", "llm.temperature", "llm.max_tokens",
                                "llm.enabled", "guide.system_prompt", "guide.default_question_count",
                                "guide.max_question_count", "guide.hint_levels", "chart.enabled",
                                "chart.model", "chart.max_pages", "chart.dpi"}
    with db() as conn:
        for key, value in values.items():
            if key not in allowed:
                continue
            if key == "llm.temperature":
                try:
                    value = str(max(0.0, min(1.5, float(value))))
                except Exception:
                    continue
            if key in ("llm.max_tokens", "guide.default_question_count", "guide.max_question_count", "guide.hint_levels",
                       "ocr.dpi", "ocr.max_pages", "ocr.min_chars_per_page", "chart.max_pages", "chart.dpi"):
                try:
                    upper = 32000 if key == "llm.max_tokens" else 30
                    if key == "ocr.dpi":
                        upper = 180
                    if key in ("ocr.max_pages", "ocr.min_chars_per_page"):
                        upper = 2000
                    if key in ("chart.max_pages", "chart.dpi"):
                        upper = 300 if key == "chart.dpi" else 30
                    value = str(max(1, min(upper, int(float(value)))))
                except Exception:
                    continue
            conn.execute(
                "INSERT INTO app_setting (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, str(value), iso()),
            )
    try:
        from ..platform import redis_client
        redis_client.delete("meta:v2")
    except Exception:
        pass
    return all_settings()
