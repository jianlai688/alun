"""A论 · 全局配置。

所有配置均可通过环境变量覆盖，便于私有化部署。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "A论"
APP_SUBTITLE = "论文导读智能体"
APP_VERSION = "2.0.0"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("ALUN_DATA_DIR", BASE_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = Path(os.getenv("ALUN_DB_PATH", DATA_DIR / "alun.db")).resolve()
DB_BACKEND = os.getenv("ALUN_DB_BACKEND", "sqlite").strip().lower()  # sqlite | postgres
DATABASE_URL = os.getenv(
    "ALUN_DATABASE_URL",
    "postgresql://alun:alun_dev_password@127.0.0.1:55432/alun",
)
WEB_DIR = BASE_DIR / "web"

# ---------------------------------------------------------------------------
# V2.0 平台组件（默认关闭，开发/单机仍走 SQLite + 本地文件 + 线程）
# ---------------------------------------------------------------------------
REDIS_URL = os.getenv("ALUN_REDIS_URL", "")
QUEUE_BACKEND = os.getenv("ALUN_QUEUE_BACKEND", "inline").strip().lower()  # inline | redis
S3_ENDPOINT = os.getenv("ALUN_S3_ENDPOINT", "")
S3_ACCESS_KEY = os.getenv("ALUN_S3_ACCESS_KEY", "")
S3_SECRET_KEY = os.getenv("ALUN_S3_SECRET_KEY", "")
S3_BUCKET = os.getenv("ALUN_S3_BUCKET", "alun-documents")
S3_REGION = os.getenv("ALUN_S3_REGION", "us-east-1")
S3_SECURE = os.getenv("ALUN_S3_SECURE", "0") not in ("0", "false", "False")
S3_PUBLIC_ENDPOINT = os.getenv("ALUN_S3_PUBLIC_ENDPOINT", S3_ENDPOINT)
CORS_ORIGINS = [x.strip() for x in os.getenv("ALUN_CORS_ORIGINS", "").split(",") if x.strip()]
LOG_JSON = os.getenv("ALUN_LOG_JSON", "1") not in ("0", "false", "False")
SECURITY_HEADERS = os.getenv("ALUN_SECURITY_HEADERS", "1") not in ("0", "false", "False")

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# 认证：demo 模式下允许无令牌访问（自动注入本地演示用户）；strict 模式强制 JWT
# ---------------------------------------------------------------------------
AUTH_MODE = os.getenv("ALUN_AUTH_MODE", "strict")  # demo | strict
AUTH_SECRET = os.getenv("ALUN_AUTH_SECRET", "alun-local-development-secret")
TOKEN_TTL_SECONDS = 3600
ADMIN_USERNAME = os.getenv("ALUN_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ALUN_ADMIN_PASSWORD", "admin123")
DEMO_USERNAME = os.getenv("ALUN_DEMO_USERNAME", "demo")
DEMO_PASSWORD = os.getenv("ALUN_DEMO_PASSWORD", "demo123")
TEACHER_USERNAME = os.getenv("ALUN_TEACHER_USERNAME", "teacher")
TEACHER_PASSWORD = os.getenv("ALUN_TEACHER_PASSWORD", "teacher123")

DEFAULT_TENANT = "tnt_local"
DEFAULT_USER = "usr_local"
DEFAULT_SCOPES = (
    "documents:read documents:write sessions:read sessions:write "
    "questions:read questions:review reports:read admin:usage"
)
USER_SCOPES = "documents:read documents:write sessions:read sessions:write questions:read reports:read"

# ---------------------------------------------------------------------------
# 模型网关：默认复用本机 WorkBuddy2API（OpenAI 兼容）
# ---------------------------------------------------------------------------
def _load_gateway_config() -> dict:
    cfg = {
        "base_url": os.getenv("ALUN_LLM_BASE_URL", "http://127.0.0.1:7863/v1"),
        "api_key": os.getenv("ALUN_LLM_API_KEY", ""),
        "model": os.getenv("ALUN_LLM_MODEL", "cn:deepseek-v4.1-flash"),
        "fallback_model": os.getenv("ALUN_LLM_FALLBACK_MODEL", "cn:glm-5.3-flash"),
        "timeout": float(os.getenv("ALUN_LLM_TIMEOUT", "90")),
    }
    if not cfg["api_key"]:
        path = Path(os.getenv("WB2API_CONFIG", Path.home() / ".dsh" / "wb2api.json"))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cfg["api_key"] = raw.get("apiKey") or raw.get("api_key") or ""
            cfg["base_url"] = raw.get("baseUrl") or cfg["base_url"]
            cfg["model"] = raw.get("defaultModel") or cfg["model"]
        except Exception:
            pass
    cfg["enabled"] = bool(cfg["api_key"])
    return cfg


GATEWAY = _load_gateway_config()

# 解析/生成策略开关
ENABLE_LLM = os.getenv("ALUN_ENABLE_LLM", "1") not in ("0", "false", "False")
ENABLE_LLM_REPORT = os.getenv("ALUN_LLM_REPORT", "1") not in ("0", "false", "False")
MAX_UPLOAD_BYTES = int(os.getenv("ALUN_MAX_UPLOAD_MB", "100")) * 1024 * 1024

# 会话/题目默认值
HINT_PENALTIES = {0: 0, 1: 10, 2: 20, 3: 40}
BLOOM_LEVELS = ["记忆", "理解", "应用", "分析", "评价", "创造"]
QUESTION_TYPES = ["single_choice", "multiple_choice", "true_false", "fill_blank", "short_answer"]

DOC_TYPE_LABELS = {
    "research_paper": "研究论文",
    "review": "综述",
    "policy": "政策研究",
    "news": "报纸报道",
    "other": "其他文档",
}

MODE_LABELS = {"guided": "导读模式", "exam": "测评模式", "defense": "答辩模式"}
