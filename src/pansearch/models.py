"""数据模型：适配器原始命中 → 归一化资源 → 校验结果。"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class PanType(str, Enum):
    """网盘类型。百度优先，其余兜底。"""

    BAIDU = "baidu"
    QUARK = "quark"
    ALIYUN = "aliyun"
    XUNLEI = "xunlei"
    TIANYI = "tianyi"
    UC = "uc"
    P123 = "123"
    P115 = "115"
    PIKPAK = "pikpak"
    MAGNET = "magnet"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _PAN_LABELS.get(self, self.value)


_PAN_LABELS = {
    PanType.BAIDU: "百度网盘",
    PanType.QUARK: "夸克网盘",
    PanType.ALIYUN: "阿里云盘",
    PanType.XUNLEI: "迅雷云盘",
    PanType.TIANYI: "天翼云盘",
    PanType.UC: "UC网盘",
    PanType.P123: "123网盘",
    PanType.P115: "115网盘",
    PanType.PIKPAK: "PikPak",
    PanType.MAGNET: "磁力链接",
    PanType.OTHER: "其他",
}


class Status(str, Enum):
    """链接存活状态。前三态都算"存活"。"""

    ALIVE = "alive"            # ✅ 存活，提取码已验证正确
    NEED_PWD = "need_pwd"      # ✅ 存活，需要提取码
    WRONG_PWD = "wrong_pwd"    # ⚠️ 链接存活，但提取码不对
    DEAD = "dead"              # ❌ 已失效/取消/过期
    NOT_FOUND = "not_found"    # ❌ 链接不存在
    UNKNOWN = "unknown"        # ⚠️ 无法判定
    UNCHECKED = "unchecked"    # 尚未校验
    UNSUPPORTED = "unsupported"  # 非百度，本工具不校验

    @property
    def alive(self) -> bool:
        return self in (Status.ALIVE, Status.NEED_PWD, Status.WRONG_PWD)

    @property
    def label(self) -> str:
        return _STATUS_LABELS.get(self, self.value)


_STATUS_LABELS = {
    Status.ALIVE: "有效(码已验证)",
    Status.NEED_PWD: "有效(需提取码)",
    Status.WRONG_PWD: "存活/码不对",
    Status.DEAD: "已失效",
    Status.NOT_FOUND: "不存在",
    Status.UNKNOWN: "未知",
    Status.UNCHECKED: "未校验",
    Status.UNSUPPORTED: "未校验(非百度)",
}


class RawHit(BaseModel):
    """适配器输出的原始命中（未归一化、未去重）。"""

    source: str                     # 插件名 / TG 频道名 / 引擎名
    kind: str                       # pansou | tg | websearch | bilibili | forum
    url: str
    pwd: str | None = None
    title: str | None = None
    size: str | None = None
    shared_at: datetime | None = None
    origin: str | None = None       # 来源页面（可点回原帖）


class VerifyResult(BaseModel):
    status: Status
    errno: int | None = None
    method: str | None = None       # share_verify | shorturlinfo | cache
    checked_at: datetime = Field(default_factory=now_utc)
    note: str | None = None


class Resource(BaseModel):
    """归一化 + 去重后的资源条目。"""

    key: str
    pan_type: PanType
    url: str
    surl: str | None = None
    pwd: str | None = None
    title: str | None = None
    size: str | None = None
    shared_at: datetime | None = None
    sources: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    origins: list[str] = Field(default_factory=list)
    hit_count: int = 1
    verify: VerifyResult | None = None
    score: float = 0.0

    @property
    def status(self) -> Status:
        return self.verify.status if self.verify else Status.UNCHECKED

    def open_url(self) -> str:
        """可直接打开的一键链接（百度带提取码）。"""
        if self.pan_type is PanType.BAIDU and self.surl and self.pwd:
            return f"https://pan.baidu.com/s/{self.surl}?pwd={self.pwd}"
        return self.url

    def copy_text(self) -> str:
        """一键复制用文本。"""
        if self.pan_type is PanType.BAIDU and self.surl:
            base = f"https://pan.baidu.com/s/{self.surl}"
            return f"{base} 提取码: {self.pwd}" if self.pwd else base
        return self.url

    def to_row(self) -> dict:
        return {
            "标题": self.title or "",
            "网盘": self.pan_type.label,
            "链接": self.open_url(),
            "提取码": self.pwd or "",
            "状态": self.status.label,
            "来源": "; ".join(self.sources[:3]),
            "命中次数": self.hit_count,
            "大小": self.size or "",
            "分享时间": self.shared_at.strftime("%Y-%m-%d") if self.shared_at else "",
            "来源页面": self.origins[0] if self.origins else "",
        }
