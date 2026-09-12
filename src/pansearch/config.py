"""配置加载。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("PANSEARCH_CONFIG", PROJECT_ROOT / "config"))
CACHE_DIR = Path(os.environ.get("PANSEARCH_CACHE", PROJECT_ROOT / ".cache"))
DEFAULT_DB = CACHE_DIR / "index.sqlite3"


def _load(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


@lru_cache(maxsize=1)
def sources_config() -> dict:
    return _load("sources.yaml")


@lru_cache(maxsize=1)
def errno_config() -> dict:
    return _load("baidu_errno.yaml")


@lru_cache(maxsize=1)
def pan_errno_config() -> dict:
    return _load("pan_errno.yaml")


def reload_config() -> None:
    sources_config.cache_clear()
    errno_config.cache_clear()
    pan_errno_config.cache_clear()


def source_cfg(name: str) -> dict:
    return (sources_config().get("sources") or {}).get(name, {})


def pan_priority() -> list[str]:
    return sources_config().get("pan_priority") or []


def verify_cfg() -> dict:
    return sources_config().get("verify") or {}


def scoring_cfg() -> dict:
    return sources_config().get("scoring") or {}


def errno_map(endpoint: str) -> dict[int, str]:
    raw = errno_config().get(endpoint) or {}
    out: dict[int, str] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = str(v)
        except (TypeError, ValueError):
            continue
    return out


def errno_default() -> str:
    return errno_config().get("default", "unknown")
