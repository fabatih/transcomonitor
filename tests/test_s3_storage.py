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
    """End-to-end: write a fake DB file, upload to S3 under a TEST KEY,
    download to new path, verify content matches.

    Uses a unique test key so we don't pollute the canonical db/transcomonitor.sqlite."""
    test_key = f"_test/db_roundtrip_{uuid.uuid4().hex}.sqlite"
    payload = f"sqlite-fake-content-{uuid.uuid4().hex}\n".encode()

    with tempfile.TemporaryDirectory() as tmpdir:
        upload_path = Path(tmpdir) / "fake_db.sqlite"
        upload_path.write_bytes(payload)

        # Upload to S3 under a custom key (don't use s3_upload_db which would
        # overwrite the canonical db/transcomonitor.sqlite key)
        import boto3
        s3 = boto3.client("s3", region_name="eu-west-3")
        s3.upload_file(str(upload_path), "transcomonitor", test_key)

        # Verify accessible
        info = s3.head_object(Bucket="transcomonitor", Key=test_key)
        assert info["ContentLength"] == len(payload)

        # Download to a different path
        download_path = Path(tmpdir) / "downloaded_db.sqlite"
        s3.download_file("transcomonitor", test_key, str(download_path))
        assert download_path.read_bytes() == payload

        # Cleanup all versions of the test key
        versions = s3.list_object_versions(Bucket="transcomonitor", Prefix=test_key)
        for v in versions.get("Versions", []):
            s3.delete_object(Bucket="transcomonitor", Key=v["Key"], VersionId=v["VersionId"])
        for m in versions.get("DeleteMarkers", []):
            s3.delete_object(Bucket="transcomonitor", Key=m["Key"], VersionId=m["VersionId"])


def test_async_upload_debounced(tmp_path):
    """s3_upload_db_async must debounce: a 2nd call within MIN_UPLOAD_INTERVAL
    after a successful first upload must NOT trigger a second S3 PUT.

    Uses a test-only DB key to avoid polluting the canonical db/transcomonitor.sqlite.
    """
    # Override the module-level key for this test
    original_key = s3_storage._S3_DB_KEY
    test_key = f"_test/async_debounce_{uuid.uuid4().hex}.sqlite"
    s3_storage._S3_DB_KEY = test_key
    try:
        f = tmp_path / "x.sqlite"
        f.write_bytes(b"contentA")

        s3_storage._last_upload_time = 0.0

        s3_storage.s3_upload_db_async(str(f))
        for _ in range(30):
            time.sleep(0.1)
            if s3_storage._last_upload_time > 0:
                break
        first_upload = s3_storage._last_upload_time
        assert first_upload > 0, "first upload didn't complete in time"

        s3_storage.s3_upload_db_async(str(f))
        time.sleep(0.5)
        second_upload = s3_storage._last_upload_time

        assert second_upload == first_upload, (
            f"Debounce failed: _last_upload_time changed from {first_upload} "
            f"to {second_upload} within MIN_UPLOAD_INTERVAL"
        )
    finally:
        # Cleanup
        s3_storage._S3_DB_KEY = original_key
        try:
            import boto3
            s3 = boto3.client("s3", region_name="eu-west-3")
            versions = s3.list_object_versions(Bucket="transcomonitor", Prefix=test_key)
            for v in versions.get("Versions", []):
                s3.delete_object(Bucket="transcomonitor", Key=v["Key"], VersionId=v["VersionId"])
            for m in versions.get("DeleteMarkers", []):
                s3.delete_object(Bucket="transcomonitor", Key=m["Key"], VersionId=m["VersionId"])
        except Exception:
            pass


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
