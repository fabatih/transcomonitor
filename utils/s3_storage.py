"""
utils/s3_storage.py — AWS S3 integration for transcomonitor.

Stores the SQLite database AND large reference caches in an S3 bucket for
persistence across shinyapps.io redeployments (filesystem in /tmp is wiped
on every container restart).

Strategy:
  - On startup : if local DB is empty/new, restore from S3
  - On save events : upload DB snapshot to S3 (async, debounced)
  - Reference caches (cim11_foundation, cim11_linearizations) sync separately
  - Frozen version snapshots uploaded to /snapshots/ prefix

Environment variables (AWS standard naming) :
  AWS_ACCESS_KEY_ID
  AWS_SECRET_ACCESS_KEY
  S3_BUCKET          (default: 'transcomonitor')
  S3_REGION          (default: 'eu-west-3' — Paris, FR souveraineté)
  S3_ENDPOINT_URL    (optional — for non-AWS S3-compatible providers)

Adapté d'icd11pycode/utils/s3_storage.py avec :
  - Naming AWS standard (vs AWS_KEY_ID legacy)
  - Endpoint URL configurable (compat OVH/Scaleway/Wasabi/MinIO)
  - Cache prefix séparé du library prefix
  - Bucket-level versioning friendly (object versioning géré par S3)
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("transcomonitor.s3")

# Bucket key prefixes (matches config.yml)
_S3_DB_KEY              = "db/transcomonitor.sqlite"
_S3_SNAPSHOTS_PREFIX    = "snapshots/"
_S3_CACHE_PREFIX        = "cache/"
_S3_LIBRARY_PREFIX      = "library/"

_upload_lock        = threading.Lock()
_last_upload_time   = 0.0
_MIN_UPLOAD_INTERVAL = 10.0   # seconds between async uploads (debounce)


# ─────────────────────────────────────────────────────────────────────────
# Configuration helpers (env → config → default)
# ─────────────────────────────────────────────────────────────────────────

def _env_or_config(env_key: str, config_section: str, config_key: str, default: str) -> str:
    """Resolve a value with priority : env > config.yml > default."""
    env_val = os.environ.get(env_key, "")
    if env_val:
        return env_val
    try:
        from utils.config_manager import get_config
        return get_config().get(config_section, {}).get(config_key, "") or default
    except Exception:
        return default


def get_bucket() -> str:
    return _env_or_config("S3_BUCKET", "s3", "bucket", "transcomonitor")


def get_region() -> str:
    return _env_or_config("S3_REGION", "s3", "region", "eu-west-3")


def get_endpoint_url() -> Optional[str]:
    """Return the endpoint URL (None = AWS default for the region)."""
    url = _env_or_config("S3_ENDPOINT_URL", "s3", "endpoint_url", "")
    return url or None


def is_s3_available() -> bool:
    """True if AWS credentials are configured (standard naming)."""
    return bool(os.environ.get("AWS_ACCESS_KEY_ID")
                and os.environ.get("AWS_SECRET_ACCESS_KEY"))


# ─────────────────────────────────────────────────────────────────────────
# Client factory (lazy)
# ─────────────────────────────────────────────────────────────────────────

def _get_s3_client():
    """Create an S3 client. Returns None if credentials or boto3 are missing."""
    if not is_s3_available():
        return None
    try:
        import boto3
        return boto3.client(
            "s3",
            region_name=get_region(),
            endpoint_url=get_endpoint_url(),
            # Credentials picked up automatically from env vars by boto3
        )
    except Exception as e:
        logger.warning(f"Cannot create S3 client: {e}")
        return None


def test_connection() -> dict:
    """Connectivity smoke test. Returns dict with ok/details."""
    if not is_s3_available():
        return {"ok": False, "error": "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY not configured"}
    try:
        s3 = _get_s3_client()
        if s3 is None:
            return {"ok": False, "error": "Cannot create S3 client"}
        bucket = get_bucket()
        s3.head_bucket(Bucket=bucket)
        # Get versioning status (informational)
        try:
            v = s3.get_bucket_versioning(Bucket=bucket)
            versioning = v.get("Status", "Disabled")
        except Exception:
            versioning = "unknown"
        return {
            "ok": True,
            "bucket": bucket,
            "region": get_region(),
            "endpoint": get_endpoint_url() or "AWS default",
            "versioning": versioning,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─────────────────────────────────────────────────────────────────────────
# DB upload / download
# ─────────────────────────────────────────────────────────────────────────

def s3_download_db(local_path: str) -> bool:
    s3 = _get_s3_client()
    if s3 is None:
        return False
    try:
        s3.download_file(get_bucket(), _S3_DB_KEY, local_path)
        logger.info(f"Downloaded DB from s3://{get_bucket()}/{_S3_DB_KEY}")
        return True
    except Exception as e:
        logger.info(f"No DB found in S3 (or download failed): {e}")
        return False


def s3_upload_db(local_path: str) -> bool:
    global _last_upload_time
    s3 = _get_s3_client()
    if s3 is None or not os.path.exists(local_path):
        return False
    try:
        s3.upload_file(local_path, get_bucket(), _S3_DB_KEY)
        _last_upload_time = time.time()
        logger.info(f"Uploaded DB to s3://{get_bucket()}/{_S3_DB_KEY}")
        return True
    except Exception as e:
        logger.error(f"S3 upload failed: {e}")
        return False


def s3_upload_db_async(local_path: str) -> None:
    """Upload DB to S3 in a background thread (debounced ; uses a temporary
    copy to avoid SQLite locking issues during the upload)."""
    if not is_s3_available():
        return

    def _do_upload() -> None:
        global _last_upload_time
        with _upload_lock:
            if time.time() - _last_upload_time < _MIN_UPLOAD_INTERVAL:
                return
            tmp_path = local_path + ".s3tmp"
            try:
                shutil.copy2(local_path, tmp_path)
                s3_upload_db(tmp_path)
            except Exception as e:
                logger.error(f"S3 async upload failed: {e}")
            finally:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    threading.Thread(target=_do_upload, daemon=True, name="s3-upload").start()


def s3_get_db_info() -> Optional[dict]:
    """Get info about the DB stored in S3 (size, last modified)."""
    s3 = _get_s3_client()
    if s3 is None:
        return None
    try:
        resp = s3.head_object(Bucket=get_bucket(), Key=_S3_DB_KEY)
        return {
            "size": resp["ContentLength"],
            "last_modified": resp["LastModified"].isoformat(),
            "bucket": get_bucket(),
            "key": _S3_DB_KEY,
            "version_id": resp.get("VersionId"),
        }
    except Exception:
        return None


def restore_db_from_s3_if_empty(local_path: str) -> bool:
    """At startup : if the local DB is empty (or doesn't exist), restore from S3.
    'Empty' means no real data — only default seeded catalogues."""
    if not is_s3_available():
        return False

    import sqlite3

    # Check if local DB has real data (more than just default catalogues / admin)
    try:
        con = sqlite3.connect(local_path)
        con.execute("PRAGMA foreign_keys=ON")
        user_count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        mapping_count = con.execute("SELECT COUNT(*) FROM mappings").fetchone()[0]
        con.close()
        if mapping_count > 0 or user_count > 1:
            return False  # already has real data
    except Exception:
        pass  # DB might not exist yet

    info = s3_get_db_info()
    if info is None:
        return False  # nothing in S3

    logger.info("Local DB is empty, restoring from S3...")
    return s3_download_db(local_path)


# ─────────────────────────────────────────────────────────────────────────
# Frozen version snapshots
# ─────────────────────────────────────────────────────────────────────────

def s3_upload_snapshot(local_path: str, version_label: str, filename: str) -> Optional[str]:
    """Upload a frozen version snapshot (XLSX/JSON/manifest).
    Returns the S3 URI (s3://bucket/snapshots/{label}/{filename}) or None on failure."""
    s3 = _get_s3_client()
    if s3 is None:
        return None
    key = f"{_S3_SNAPSHOTS_PREFIX}{version_label}/{filename}"
    try:
        s3.upload_file(local_path, get_bucket(), key)
        uri = f"s3://{get_bucket()}/{key}"
        logger.info(f"Uploaded snapshot to {uri}")
        return uri
    except Exception as e:
        logger.error(f"S3 snapshot upload failed: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────
# Reference caches (cim11_foundation / cim11_linearizations)
# ─────────────────────────────────────────────────────────────────────────

def s3_upload_cache(local_path: str, cache_name: str) -> bool:
    """Upload a cache file (e.g. 'cim11_foundation.json' or '.parquet')."""
    s3 = _get_s3_client()
    if s3 is None or not os.path.exists(local_path):
        return False
    key = f"{_S3_CACHE_PREFIX}{cache_name}"
    try:
        s3.upload_file(local_path, get_bucket(), key)
        logger.info(f"Uploaded cache to s3://{get_bucket()}/{key}")
        return True
    except Exception as e:
        logger.error(f"S3 cache upload failed ({cache_name}): {e}")
        return False


def s3_download_cache(local_path: str, cache_name: str) -> bool:
    s3 = _get_s3_client()
    if s3 is None:
        return False
    key = f"{_S3_CACHE_PREFIX}{cache_name}"
    try:
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(get_bucket(), key, local_path)
        logger.info(f"Downloaded cache from s3://{get_bucket()}/{key}")
        return True
    except Exception as e:
        logger.info(f"No cache {cache_name} in S3: {e}")
        return False
