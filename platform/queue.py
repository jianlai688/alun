"""Redis Streams 任务队列：解析任务异步执行，支持多 worker。"""
from __future__ import annotations

import json
import time

from ..app.config import QUEUE_BACKEND
from . import redis_client

STREAM = "alun:jobs"
GROUP = "alun-workers"


def enabled() -> bool:
    return QUEUE_BACKEND == "redis" and redis_client.enabled()


def enqueue(kind: str, payload: dict) -> str | None:
    if not enabled():
        return None
    client = redis_client.client()
    try:
        client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception:
        pass
    return client.xadd(STREAM, {"kind": kind, "payload": json.dumps(payload, ensure_ascii=False)})


def consume(consumer: str = "worker-1", block_ms: int = 2000) -> dict | None:
    if not enabled():
        return None
    client = redis_client.client()
    try:
        client.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception:
        pass
    rows = client.xreadgroup(GROUP, consumer, {STREAM: ">"}, count=1, block=block_ms)
    if not rows:
        return None
    _, messages = rows[0]
    msg_id, fields = messages[0]
    return {"id": msg_id, "kind": fields.get("kind"), "payload": json.loads(fields.get("payload") or "{}")}


def ack(message_id: str) -> None:
    if enabled():
        redis_client.client().xack(STREAM, GROUP, message_id)


def depth() -> int:
    """待处理消息数（XPENDING），不是 stream 历史长度。"""
    if not enabled():
        return 0
    try:
        info = redis_client.client().xpending(STREAM, GROUP)
        return int(info.get("pending", 0)) if isinstance(info, dict) else 0
    except Exception:
        return 0


def wait_for_idle(timeout: float = 30.0) -> bool:
    """压测辅助：等待队列清空。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = redis_client.client().xpending(STREAM, GROUP)
        if depth() == 0 and (not pending or int(pending.get("pending", 0)) == 0):
            return True
        time.sleep(0.5)
    return False
