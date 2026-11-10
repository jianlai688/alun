"""V2.0 平台状态 API（管理员）。"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..platform import migrations, observability, vector_store
from .db import fetch_one
from .security import principal_from_request, require_admin

router = APIRouter(prefix="/api/v1/admin/platform", tags=["platform"])


@router.get("/status")
def platform_status(request: Request):
    p = principal_from_request(request)
    require_admin(p)
    out = {"health": observability.refresh_dependencies()}
    try:
        out["migrations"] = migrations.status()
    except Exception as exc:
        out["migrations"] = {"error": str(exc)[:200]}
    try:
        out["vectors"] = vector_store.stats(p.tenant_id)
    except Exception as exc:
        out["vectors"] = {"error": str(exc)[:200]}
    out["documents"] = fetch_one("SELECT count(*) AS c FROM document WHERE deleted_at IS NULL")["c"]
    return out
