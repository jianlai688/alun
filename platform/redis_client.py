"""Redis 适配：缓存、限流与队列共享连接。"""
from __future__ import annotations

import json
import threading
import time
from collections import defaultdict

from ..app.config import REDIS_URL

_memory: dict[str, tuple[float, str]] = {}
_mem_lock = threading.Lock()


def enabled() -> bool:
    return bool(REDIS_URL)


_client = None


def client():
    global _client
    if not enabled():
        return None
    if _client is None:
        import redis
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True, protocol=2, socket_connect_timeout=3)
    return _client


def ping() -> dict:
    if not enabled():
        return {"backend": "memory", "ok": True}
    try:
        return {"backend": "redis", "ok": bool(client().ping()),
                "version": client().info().get("redis_version")}
    except Exception as exc:
        return {"backend": "redis", "ok": False, "error": str(exc)[:200]}


def get_json(key: str):
    if enabled():
        try:
            raw = client().get("alun:" + key)
            return json.loads(raw) if raw else None
        except Exception:
            return None
    with _mem_lock:
        item = _memory.get(key)
        if not item or item[0] < time.time():
            _memory.pop(key, None)
            return None
        return json.loads(item[1])


def set_json(key: str, value, ttl: int = 60) -> None:
    raw = json.dumps(value, ensure_ascii=False)
    if enabled():
        try:
            client().setex("alun:" + key, ttl, raw)
            return
        except Exception:
            pass
    with _mem_lock:
        _memory[key] = (time.time() + ttl, raw)


def delete(key: str) -> None:
    if enabled():
        try:
            client().delete("alun:" + key)
        except Exception:
            pass
    with _mem_lock:
        _memory.pop(key, None)


def rate_limit(scope: str, identity: str, limit: int = 10, window: int = 60) -> tuple[bool, int]:
    """固定窗口限流，Redis 不可用时使用进程内内存兜底。"""
    key = f"rl:{scope}:{identity}:{int(time.time() // window)}"
    if enabled():
        try:
            pipe = client().pipeline()
            pipe.incr("alun:" + key)
            pipe.expire("alun:" + key, window + 1)
            count = int(pipe.execute()[0])
            return count <= limit, max(0, limit - count)
        except Exception:
            pass
    with _mem_lock:
        expiry, raw = _memory.get(key, (0, "0"))
        if expiry < time.time():
            raw = "0"
        count = int(raw) + 1
        _memory[key] = (time.time() + window, str(count))
        return count <= limit, max(0, limit - count)
