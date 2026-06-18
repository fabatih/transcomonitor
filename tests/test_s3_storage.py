"""tests/test_s3_storage.py — LIVE test against the real AWS S3 bucket.

Uses the bucket 'transcomonitor' in eu-west-3. Creates and deletes a
unique-named test key so it's safe to run repeatedly. Skips if credentials
are not configured.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import s3_storage


pytestmark = pytest.mark.skipif(
    not s3_storage.is_s3_available(),
    reason="AWS credentials not configured",
)


def test_configuration_resolution():
    assert s3_storage.get_bucket() == "transcomonitor"
    assert s3_storage.get_region() == "eu-west-3"
    # Endpoint can be None (AWS default) or the explicit URL
    ep = s3_storage.get_endpoint_url()
    assert ep is None or "amazonaws" in ep or "scw" in ep or "ovh" in ep


def test_connection_smoke():
    result = s3_storage.test_connection()
    assert result["ok"] is True, f"S3 connection failed: {result.get('error')}"
    assert result["bucket"] == "transcomonitor"
    assert result["region"] == "eu-west-3"
    assert result["versioning"] == "Enabled", (
        f"Bucket versioning should be Enabled, got {result['versioning']!r}"
    )


def test_db_upload_download_roundtrip():
    """End-to-end: write a fake DB file, upload to S3, download to new path,
    verify content matches."""
    payload = f"sqlite-fake-content-{uuid.uuid4().hex}\n".encode()

    with tempfile.TemporaryDirectory() as tmpdir:
        upload_path = Path(tmpdir) / "fake_db.sqlite"
        upload_path.write_bytes(payload)

        # Upload to S3
        ok = s3_storage.s3_upload_db(str(upload_path))
        assert ok, "s3_upload_db failed"

        # Verify info accessible
        info = s3_storage.s3_get_db_info()
        assert info is not None
        assert info["size"] == len(payload)
        assert info["bucket"] == "transcomonitor"
        assert "version_id" in info  # versioning is enabled

        # Download to a different path
        download_path = Path(tmpdir) / "downloaded_db.sqlite"
        ok = s3_storage.s3_download_db(str(download_path))
        assert ok, "s3_download_db failed"
        assert download_path.read_bytes() == payload


def test_async_upload_debounced():
    """s3_upload_db_async must debounce: a 2nd call within MIN_UPLOAD_INTERVAL
    after a successful first upload must NOT trigger a second S3 PUT."""
    with tempfile.TemporaryDirectory() as tmpdir:
        f = Path(tmpdir) / "x.sqlite"
        f.write_bytes(b"contentA")

        # Reset debounce timer
        s3_storage._last_upload_time = 0.0

        s3_storage.s3_upload_db_async(str(f))
        # Wait long enough for the first upload to actually complete
        # (S3 PUT of a small file typically takes 200-800ms in eu-west-3)
        for _ in range(30):
            time.sleep(0.1)
            if s3_storage._last_upload_time > 0:
                break
        first_upload = s3_storage._last_upload_time
        assert first_upload > 0, "first upload didn't complete in time"

        # Immediate second call should be debounced (no new PUT)
        s3_storage.s3_upload_db_async(str(f))
        time.sleep(0.5)
        second_upload = s3_storage._last_upload_time

        assert second_upload == first_upload, (
            f"Debounce failed: _last_upload_time changed from {first_upload} "
            f"to {second_upload} within MIN_UPLOAD_INTERVAL"
        )


def test_snapshot_upload():
    """Upload a fake frozen version snapshot file."""
    version_label = f"_test_v_{uuid.uuid4().hex[:8]}"
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        f.write(b"fake-xlsx-content")
        path = f.name
    try:
        uri = s3_storage.s3_upload_snapshot(path, version_label, "transcodage.xlsx")
        assert uri is not None
        assert uri.startswith(f"s3://transcomonitor/snapshots/{version_label}/")

        # Cleanup
        import boto3
        client = boto3.client("s3", region_name="eu-west-3")
        client.delete_object(
            Bucket="transcomonitor",
            Key=f"snapshots/{version_label}/transcodage.xlsx",
        )
    finally:
        os.unlink(path)


def test_cache_upload_download():
    """Upload and download a fake cache file."""
    cache_name = f"_test_cache_{uuid.uuid4().hex[:8]}.json"
    payload = b'{"foundation_count": 42}'

    with tempfile.TemporaryDirectory() as tmpdir:
        up = Path(tmpdir) / "src.json"
        up.write_bytes(payload)

        assert s3_storage.s3_upload_cache(str(up), cache_name)

        down = Path(tmpdir) / "downloaded.json"
        assert s3_storage.s3_download_cache(str(down), cache_name)
        assert down.read_bytes() == payload

        # Cleanup
        import boto3
        boto3.client("s3", region_name="eu-west-3").delete_object(
            Bucket="transcomonitor", Key=f"cache/{cache_name}",
        )
