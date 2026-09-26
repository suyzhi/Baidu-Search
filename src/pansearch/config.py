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


@lru_cache(maxsize=1)
def alias_config() -> dict:
    return (_load("aliases.yaml").get("aliases") or {})


def reload_config() -> None:
    sources_config.cache_clear()
    errno_config.cache_clear()
    pan_errno_config.cache_clear()
    alias_config.cache_clear()


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


# --------------------------------------------------------------- 数值 / 布尔配置
def cfg_float(cfg: dict | None, key: str, default: float) -> float:
    """读数值配置：**只有"没配"才回落到默认值**，显式配的 0 是合法值。

    为什么要有这个统一入口：Python 里 0 / 0.0 是 falsy，`cfg.get(key) or default`
    会把用户显式写的 0 悄悄换成默认值。项目里已经踩到两次，症状都是"改了配置但不
    生效、也不报错"：
      * verify.retries: 0 被吃成 2（关不掉重试）
      * scoring.relaxed_penalty: 0 被吃成 0.5（想让补搜结果彻底垫底，实际只是降权）
    这类 bug 不崩溃、不写日志、测试也测不到，所以统一走这一个函数，而不是一处处
    补 `is None` 判断 —— 补漏一处就复发一次。
    """
    value = (cfg or {}).get(key)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        # 配了非法值（比如 "abc"）时退回默认：宁可少一个配置生效，也不要整个搜索挂掉。
        return float(default)


def cfg_int(cfg: dict | None, key: str, default: int) -> int:
    return int(cfg_float(cfg, key, default))


def cfg_bool(cfg: dict | None, key: str, default: bool = False) -> bool:
    """读布尔配置：只有 None / 空串才算"没配"（False 同样是显式选择）。"""
    value = (cfg or {}).get(key)
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def verify_ttl_hours() -> float:
    """验活缓存 TTL（小时）：Web / CLI / 定时复验共用同一份默认值。"""
    return cfg_float(verify_cfg(), "cache_ttl_hours", 6.0)
