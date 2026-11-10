"""用户、角色与初始化账号。"""
from __future__ import annotations

import re
from typing import Any

from .config import (ADMIN_PASSWORD, ADMIN_USERNAME, DEMO_PASSWORD, DEMO_USERNAME,
                     TEACHER_PASSWORD, TEACHER_USERNAME, USER_SCOPES)
from .db import db, fetch_one, iso, new_id
from .security import hash_password, verify_password

ADMIN_SCOPES = (
    "documents:read documents:write sessions:read sessions:write questions:read questions:review "
    "reports:read admin:usage admin:users admin:settings admin:monitor"
)


def ensure_seed_users() -> None:
    """首次启动时创建管理员、教师与演示账号；已有账号不覆盖密码。"""
    _ensure_user(
        user_id="usr_admin",
        username=ADMIN_USERNAME,
        password=ADMIN_PASSWORD,
        role="admin",
        display_name="系统管理员",
        tenant_id="tnt_local",
    )
    _ensure_user(
        user_id="usr_local",
        username=DEMO_USERNAME,
        password=DEMO_PASSWORD,
        role="user",
        display_name="演示用户",
        tenant_id="tnt_local",
    )
    _ensure_user(
        user_id="usr_teacher",
        username=TEACHER_USERNAME,
        password=TEACHER_PASSWORD,
        role="teacher",
        display_name="李老师",
        tenant_id="tnt_local",
    )


def _ensure_user(user_id: str, username: str, password: str, role: str, display_name: str, tenant_id: str) -> None:
    existing = fetch_one("SELECT user_id FROM app_user WHERE username=?", (username,))
    if existing:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO app_user (user_id, username, password_hash, role, display_name, tenant_id, status,"
            " created_at, last_login_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, username, hash_password(password), role, display_name, tenant_id, "active", iso(), None),
        )


def authenticate(username: str, password: str) -> dict | None:
    user = fetch_one("SELECT * FROM app_user WHERE username=?", (username.strip(),))
    if not user or user["status"] != "active":
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    with db() as conn:
        conn.execute("UPDATE app_user SET last_login_at=? WHERE user_id=?", (iso(), user["user_id"]))
    user["last_login_at"] = iso()
    return user


def create_user(username: str, password: str, display_name: str = "", tenant_id: str = "tnt_local",
                email: str = "", phone: str = "") -> dict:
    username = (username or "").strip()
    email = (email or "").strip().lower()
    phone = (phone or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]{3,32}", username):
        raise ValueError("用户名需为 3—32 位字母、数字、下划线或中文")
    if len(password or "") < 8 or not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ValueError("密码至少 8 位，且必须同时包含字母和数字")
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ValueError("邮箱格式不正确")
    if phone and not re.fullmatch(r"\+?[0-9][0-9\- ]{5,19}", phone):
        raise ValueError("手机号格式不正确")
    if fetch_one("SELECT user_id FROM app_user WHERE username=?", (username,)):
        raise ValueError("用户名已存在")
    if email and fetch_one("SELECT user_id FROM app_user WHERE email=? AND email<>''", (email,)):
        raise ValueError("邮箱已被注册")
    if phone and fetch_one("SELECT user_id FROM app_user WHERE phone=? AND phone<>''", (phone,)):
        raise ValueError("手机号已被注册")
    user_id = new_id("usr")
    with db() as conn:
        conn.execute(
            "INSERT INTO app_user (user_id, username, password_hash, role, display_name, email, phone, tenant_id,"
            " status, created_at, last_login_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (user_id, username, hash_password(password), "user", display_name or username, email, phone,
             tenant_id, "active", iso(), None),
        )
    return fetch_one("SELECT * FROM app_user WHERE user_id=?", (user_id,)) or {}


def public_user(user: dict) -> dict:
    return {
        "user_id": user.get("user_id"),
        "username": user.get("username"),
        "display_name": user.get("display_name") or user.get("username"),
        "email": user.get("email") or "",
        "phone": user.get("phone") or "",
        "role": user.get("role", "user"),
        "tenant_id": user.get("tenant_id"),
        "status": user.get("status", "active"),
        "created_at": user.get("created_at"),
        "last_login_at": user.get("last_login_at"),
    }


def scopes_for(role: str) -> str:
    if role == "admin":
        return ADMIN_SCOPES
    if role == "teacher":
        return USER_SCOPES + " questions:review teaching:manage"
    return USER_SCOPES


def list_users() -> list[dict]:
    from .db import fetch_all

    return fetch_all("SELECT * FROM app_user ORDER BY role DESC, created_at")
