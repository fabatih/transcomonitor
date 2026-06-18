"""
utils/config_manager.py — Configuration loader (YAML + env vars).

Pattern repris d'icd11pycode/utils/config_manager.py.

The YAML config is the structural baseline; secrets are read directly from
os.environ wherever they're needed (never injected into the config dict
exposed to the UI). Admin-saved overrides (via the UI) are deep-merged on
top of YAML at startup via _restore_config_from_db() in app.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

_config: Optional[dict] = None
_DEFAULT_PATH = Path(__file__).parent.parent / "config" / "config.yml"


def load_config(config_path: Optional[str] = None) -> dict:
    """Load config from YAML. Caches result; pass an explicit path to bypass
    the cache (useful for tests)."""
    global _config
    if _config is not None and config_path is None:
        return _config

    path = config_path or str(_DEFAULT_PATH)
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    _config = cfg
    return _config


def get_config() -> dict:
    """Return the loaded config (lazy-loads if needed)."""
    if _config is None:
        return load_config()
    return _config


def update_config(new_cfg: dict) -> None:
    """Replace the in-memory config (called after admin save in UI)."""
    global _config
    if _config is None:
        _config = {}
    _config.clear()
    _config.update(new_cfg)


def reset_for_testing() -> None:
    """Reset cache — for tests only."""
    global _config
    _config = None


def deep_merge(base: dict, override: dict) -> dict:
    """Deep-merge `override` into `base`. For dict values, recurse;
    for other types, override wins. Keys in `base` not in `override`
    are preserved (forward-compat with new YAML sections)."""
    result = dict(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result
