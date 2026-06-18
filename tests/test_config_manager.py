"""tests/test_config_manager.py — Tests for utils/config_manager.py"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import config_manager


@pytest.fixture(autouse=True)
def reset():
    config_manager.reset_for_testing()
    yield
    config_manager.reset_for_testing()


def test_load_default_config():
    cfg = config_manager.load_config()
    assert cfg["app"]["short_name"] == "transcomonitor"
    assert cfg["app"]["name"] == "Plateforme ATIH de maintenance du transcodage CIM"
    assert cfg["s3"]["bucket"] == "transcomonitor"
    assert cfg["s3"]["region"] == "eu-west-3"
    assert cfg["cim11"]["release_current"] == "2026-01"


def test_load_caches_result():
    cfg1 = config_manager.load_config()
    cfg2 = config_manager.load_config()
    assert cfg1 is cfg2


def test_load_explicit_path_bypasses_cache(tmp_path):
    f = tmp_path / "alt.yml"
    f.write_text("app:\n  short_name: alt\n", encoding="utf-8")
    cfg = config_manager.load_config(str(f))
    assert cfg["app"]["short_name"] == "alt"


def test_get_config_lazy_loads():
    cfg = config_manager.get_config()
    assert "app" in cfg


def test_update_config_replaces():
    config_manager.load_config()
    config_manager.update_config({"app": {"short_name": "x"}})
    assert config_manager.get_config()["app"]["short_name"] == "x"


def test_deep_merge():
    base = {"a": 1, "b": {"c": 2, "d": 3}}
    over = {"b": {"c": 99}, "e": 5}
    merged = config_manager.deep_merge(base, over)
    assert merged == {"a": 1, "b": {"c": 99, "d": 3}, "e": 5}
    # base not mutated
    assert base["b"]["c"] == 2
