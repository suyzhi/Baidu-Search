"""适配器注册：import 即注册。"""

from . import apisources, pansou, sitesearch, telegram, websearch  # noqa: F401
from .base import REGISTRY, Adapter, register

__all__ = ["REGISTRY", "Adapter", "register", "apisources", "pansou",
           "sitesearch", "telegram", "websearch"]
