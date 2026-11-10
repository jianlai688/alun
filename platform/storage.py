"""对象存储适配：本地磁盘与 S3/MinIO 双后端。"""
from __future__ import annotations

from pathlib import Path

from ..app.config import (S3_ACCESS_KEY, S3_BUCKET, S3_ENDPOINT, S3_REGION,
                          S3_SECRET_KEY, S3_SECURE, UPLOAD_DIR)


def enabled() -> bool:
    return bool(S3_ENDPOINT and S3_ACCESS_KEY and S3_SECRET_KEY)


def _client():
    import boto3
    from botocore.config import Config
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        use_ssl=S3_SECURE,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )


def ensure_bucket() -> None:
    if not enabled():
        return
    client = _client()
    try:
        client.head_bucket(Bucket=S3_BUCKET)
    except Exception:
        client.create_bucket(Bucket=S3_BUCKET)


def put(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """写入对象存储；未配置时回退到本地 uploads。"""
    local = UPLOAD_DIR / key
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(data)
    if enabled():
        ensure_bucket()
        _client().put_object(Bucket=S3_BUCKET, Key=key, Body=data, ContentType=content_type)
    return key


def get(key: str, dest: Path | None = None) -> Path:
    """优先本地缓存，缺失时从 S3 拉取到 uploads。"""
    local = dest or (UPLOAD_DIR / key)
    if local.exists():
        return local
    if not enabled():
        raise FileNotFoundError(key)
    local.parent.mkdir(parents=True, exist_ok=True)
    _client().download_file(S3_BUCKET, key, str(local))
    return local


def delete(key: str) -> None:
    local = UPLOAD_DIR / key
    try:
        local.unlink()
    except FileNotFoundError:
        pass
    if enabled():
        try:
            _client().delete_object(Bucket=S3_BUCKET, Key=key)
        except Exception:
            pass


def health() -> dict:
    if not enabled():
        return {"backend": "local", "ok": True}
    try:
        ensure_bucket()
        _client().head_bucket(Bucket=S3_BUCKET)
        return {"backend": "s3", "ok": True, "endpoint": S3_ENDPOINT, "bucket": S3_BUCKET}
    except Exception as exc:
        return {"backend": "s3", "ok": False, "error": str(exc)[:200]}
