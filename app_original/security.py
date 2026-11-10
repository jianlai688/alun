"""轻量 JWT（HS256）与请求身份解析。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, HTTPException, Request

from .config import AUTH_MODE, AUTH_SECRET, DEFAULT_TENANT, DEFAULT_USER, TOKEN_TTL_SECONDS


DEFAULT_SECRETS = {"alun-local-development-secret", "change-me-in-production", ""}


def validate_runtime_secret(backend: str) -> None:
    """平台模式禁止使用默认 JWT 密钥，避免部署时把开发密钥带入生产。"""
    if backend == "postgres" and AUTH_SECRET in DEFAULT_SECRETS:
        raise RuntimeError("ALUN_AUTH_SECRET 未设置或仍为默认值；平台模式必须配置独立随机密钥")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def issue_token(user_id: str, tenant_id: str, scopes: str, ttl: int = TOKEN_TTL_SECONDS,
                role: str = "user", username: str = "") -> tuple[str, int]:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
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
    segments = [
        _b64(json.dumps(header, separators=(",", ":")).encode()),
        _b64(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode()
    sig = hmac.new(AUTH_SECRET.encode(), signing_input, hashlib.sha256).digest()
    return ".".join(segments + [_b64(sig)]), ttl


def decode_token(token: str) -> dict[str, Any]:
    try:
        head, body, sig = token.split(".")
        expected = hmac.new(AUTH_SECRET.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _unb64(sig)):
            raise ValueError("bad signature")
        payload = json.loads(_unb64(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("expired")
        return payload
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": f"令牌无效：{exc}"}) from exc


@dataclass
class Principal:
    user_id: str = DEFAULT_USER
    tenant_id: str = DEFAULT_TENANT
    scopes: set[str] = field(default_factory=set)
    role: str = "user"
    username: str = ""


def hash_password(password: str, iterations: int = 120_000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(expected, actual)
    except Exception:
        return False


def principal_from_request(request: Request) -> Principal:
    auth = request.headers.get("Authorization", "")
    tenant = request.headers.get("X-Tenant-Id") or DEFAULT_TENANT
    user = request.headers.get("X-User-Id") or DEFAULT_USER
    if auth.startswith("Bearer "):
        payload = decode_token(auth[7:].strip())
        try:
            from .db import fetch_one

            account = fetch_one("SELECT status FROM app_user WHERE user_id=?", (payload.get("sub", ""),))
            if account and account.get("status") != "active":
                raise HTTPException(status_code=401, detail={"code": "ACCOUNT_DISABLED", "message": "账号已被禁用"})
        except HTTPException:
            raise
        except Exception:
            pass
        return Principal(
            user_id=payload.get("sub", DEFAULT_USER),
            tenant_id=payload.get("tenant_id", tenant),
            scopes=set(str(payload.get("scope", "")).split()),
            role=payload.get("role", "user"),
            username=payload.get("username", ""),
        )
    if AUTH_MODE == "strict":
        raise HTTPException(status_code=401, detail={"code": "UNAUTHORIZED", "message": "缺少访问令牌"})
    return Principal(user_id=user, tenant_id=tenant, scopes=set(), role="user", username="demo")


def require_admin(principal: Principal) -> None:
    if principal.role != "admin":
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "需要管理员权限"})


def require_teacher(principal: Principal) -> None:
    if principal.role not in ("admin", "teacher"):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "需要教师或管理员权限"})


def require(principal: Principal, scope: str) -> None:
    """demo 模式不做作用域拦截；strict 模式校验。"""
    if AUTH_MODE == "strict" and principal.scopes and scope not in principal.scopes:
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": f"缺少作用域 {scope}"})
