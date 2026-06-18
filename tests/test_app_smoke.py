"""tests/test_app_smoke.py — End-to-end smoke tests for the running Shiny app.

These tests launch the actual Shiny server in a subprocess and verify :
  - root URL responds with 200 + correct title
  - static assets are served
  - WHO API proxy works (live call)
  - all module URIs are routable (CORS, no 500s)
  - Bootstrap CSS + JS are loaded

Slow tests (10-20s) but cover the integration that unit tests cannot.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).parent.parent


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """Block until a TCP port accepts connections, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.5)
    return False


def _find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_db_path(tmp_path_factory) -> str:
    """Use a fresh DB for the smoke tests (no auto-seed needed — empty DB is fine
    for the routes we test)."""
    p = tmp_path_factory.mktemp("smoke") / "test.sqlite"
    return str(p)


@pytest.fixture(scope="module")
def shiny_server(app_db_path):
    """Launch the Shiny app on a free port, yield (port, proc, base_url)."""
    port = _find_free_port()
    env = os.environ.copy()
    env["TRANSCOMONITOR_DB_PATH"] = app_db_path
    env["DEFAULT_ADMIN_PASS"] = "smokepass"
    # Prevent the auto-seed (no XLSX needed for smoke)
    # We rely on the data/seed/ check : remove the path temporarily by setting
    # a known non-existent path via env? Simpler : the auto-seed only runs if
    # the XLSX file exists. For smoke tests, we don't load 60k rows.
    # If the XLSX is in the repo, just accept the ~25s wait once.
    proc = subprocess.Popen(
        [sys.executable, "-m", "shiny", "run",
         "--host", "127.0.0.1", "--port", str(port), "app.py"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        assert _wait_for_port("127.0.0.1", port, timeout=120), "Shiny server didn't start"
        # Extra delay for application startup completion
        time.sleep(2)
        yield port, proc, base_url
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# ─────────────────────────────────────────────────────────────────────────
# Smoke tests
# ─────────────────────────────────────────────────────────────────────────

def test_root_serves_html(shiny_server):
    port, _, base_url = shiny_server
    r = requests.get(base_url + "/", timeout=15)
    assert r.status_code == 200
    assert "transcomonitor" in r.text.lower()
    assert "Plateforme ATIH de maintenance du transcodage CIM" in r.text


def test_root_loads_bootstrap_and_icons(shiny_server):
    _, _, base_url = shiny_server
    r = requests.get(base_url + "/", timeout=15)
    assert "bootswatch" in r.text or "bootstrap" in r.text
    assert "bootstrap-icons" in r.text


def test_root_loads_ect_bridge(shiny_server):
    _, _, base_url = shiny_server
    r = requests.get(base_url + "/", timeout=15)
    assert "ect_bridge.js" in r.text
    assert "ECTBridgeConfig" in r.text


def test_root_mounts_eb_modal(shiny_server):
    _, _, base_url = shiny_server
    r = requests.get(base_url + "/", timeout=15)
    assert "eb_browser_modal" in r.text


def test_static_ect_bridge_served(shiny_server):
    _, _, base_url = shiny_server
    r = requests.get(base_url + "/ect_bridge.js", timeout=15)
    assert r.status_code == 200
    assert "ECT" in r.text
    assert len(r.text) > 1000


def test_static_custom_css_served(shiny_server):
    _, _, base_url = shiny_server
    r = requests.get(base_url + "/custom.css", timeout=15)
    assert r.status_code == 200


def test_who_proxy_cors_preflight(shiny_server):
    _, _, base_url = shiny_server
    r = requests.options(
        base_url + "/who-api-proxy/icd/release/11/2024-01/mms",
        headers={
            "Origin": "http://127.0.0.1",
            "Access-Control-Request-Method": "GET",
        },
        timeout=15,
    )
    assert r.status_code == 204
    assert r.headers.get("access-control-allow-origin") == "*"


@pytest.mark.skipif(
    not os.environ.get("WHO_CLIENT_ID"),
    reason="WHO_CLIENT_ID not set in env (live API test)",
)
def test_who_proxy_live_call(shiny_server):
    """Full live test : proxy a real WHO API call."""
    _, _, base_url = shiny_server
    r = requests.get(
        base_url + "/who-api-proxy/icd/release/11/2024-01/mms/codeinfo/BA00",
        headers={"Accept": "application/json", "API-Version": "v2"},
        timeout=30,
    )
    assert r.status_code == 200
    data = r.json()
    assert "stemId" in data
    assert "BA00" in data.get("code", "")


def test_who_proxy_blocks_external_host(shiny_server):
    """Defense-in-depth : proxy should reject URL injection."""
    _, _, base_url = shiny_server
    r = requests.get(
        base_url + "/who-api-proxy/http://evil.example.com/secret",
        timeout=15,
    )
    assert r.status_code in (403, 502)
