"""适配器注册：import 即注册。"""

from . import pansou, telegram, websearch  # noqa: F401
from .base import REGISTRY, Adapter, register

__all__ = ["REGISTRY", "Adapter", "register", "pansou", "telegram", "websearch"]
