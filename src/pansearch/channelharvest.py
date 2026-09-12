"""TG 频道采收：从导航站按分类取候选 → 价值校验 → 追加进频道清单。

为什么要做成正式命令：
    "持续扩大 TG 频道覆盖"如果只是一次性脚本，下次就没法重复。
    这里把三步固化下来，和站点目录（sitecatalog）对称：

        站点侧                          频道侧
        probe_domain()   搜索模板        verify_channel()   最近一页有没有链接
        count_share_links() 价值校验     count_share_links() 同一个函数
        config/sites.yaml 目录           config/tg_channels.txt 清单

价值校验的判据和站点侧一致：**最近一页消息里必须真含网盘/磁力链接**。
1559 个候选里绝大多数是机器人、交易所、新闻、表情包频道，
不校验就全爬一遍，纯属浪费。
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import CONFIG_DIR
from .routing import classify
from .sitecatalog import count_share_links
from .tgindex import DEFAULT_CHANNELS_FILE, load_channels, parse_channel_page

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

TGNav_INDEX = "https://www.tgnav.org/channel/"

# 只保留与资源分享相关的分类（其余：nsfw / 机场(VPN) / 资讯新闻 / 搞笑趣味）
KEEP_CATEGORIES: dict[str, str] = {
    "影音资源": "movie",
    "书报刊漫": "ebook",
    "知识学习": "course",
    "软件综合": "software",
    "资源分享": "general",
    "ios资源": "software",
    "壁纸图片": "design",
    "博客杂谈": "general",
}


@dataclass
class ChannelHit:
    channel: str
    links: int
    vertical: str = "general"


@dataclass
class HarvestResult:
    checked: int = 0
    kept: list[ChannelHit] = field(default_factory=list)
    errors: int = 0

    @property
    def by_vertical(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for hit in self.kept:
            out[hit.vertical] = out.get(hit.vertical, 0) + 1
        return out


async def harvest_tgnav(client: httpx.AsyncClient) -> dict[str, list[str]]:
    """从 tgnav.org 的分类页采收候选频道，返回 {垂直领域: [频道]}。"""
    index = await client.get(TGNav_INDEX, timeout=20)
    cats = sorted(set(re.findall(r'href="(/channel/[^"]+/)"', index.text)))
    out: dict[str, list[str]] = {}
    for cat in cats:
        name = urllib.parse.unquote(cat.strip("/").split("/")[-1])
        vertical = KEEP_CATEGORIES.get(name)
        if not vertical:
            continue
        try:
            page = await client.get("https://www.tgnav.org" + urllib.parse.quote(cat), timeout=20)
        except httpx.HTTPError:
            continue
        found = sorted(set(re.findall(r'href="/detail/([A-Za-z0-9_]{4,})/"', page.text)))
        out.setdefault(vertical, []).extend(found)
    return {v: list(dict.fromkeys(chans)) for v, chans in out.items()}


async def verify_channel(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                         channel: str, min_links: int = 1) -> ChannelHit | None:
    """抓最近一页，只有真含资源链接才收；顺带按内容推断垂直领域。"""
    async with sem:
        try:
            resp = await client.get(f"https://t.me/s/{channel}", timeout=12)
        except httpx.HTTPError:
            return None
    if resp.status_code != 200:
        return None
    msgs = parse_channel_page(resp.text, channel)
    if not msgs:
        return None
    links = sum(len(m.links) for m in msgs)
    if links < min_links:
        return None
    text = " ".join(m.text for m in msgs[:10])
    verticals = classify(text)
    return ChannelHit(channel=channel, links=links,
                      vertical=verticals[0] if verticals else "general")


async def verify_many(
    channels: list[str],
    *,
    concurrency: int = 24,
    min_links: int = 1,
    skip: set[str] | None = None,
    on_hit=None,
) -> HarvestResult:
    todo = [c for c in dict.fromkeys(channels) if c and c not in (skip or set())]
    sem = asyncio.Semaphore(concurrency)
    result = HarvestResult()
    async with httpx.AsyncClient(
        follow_redirects=True, http2=True, timeout=15,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
    ) as client:
        async def one(channel: str) -> None:
            hit = await verify_channel(client, sem, channel, min_links)
            result.checked += 1
            if hit:
                result.kept.append(hit)
                if on_hit:
                    on_hit(hit)

        await asyncio.gather(*(one(c) for c in todo))
    return result


def append_channels(hits: list[ChannelHit], path: str | Path | None = None) -> Path:
    """把采收结果按垂直领域分组追加到频道清单。"""
    file = Path(path or DEFAULT_CHANNELS_FILE)
    # 必须把 path 传下去，否则读的是默认清单，自定义路径下的重复项不会被跳过
    existing = set(load_channels(file))
    fresh = [h for h in hits if h.channel not in existing]
    if not fresh:
        return file
    lines: list[str] = []
    lines.append("\n# ---- 由 pansearch channels harvest 自动采收（已做价值校验）----")
    by_v: dict[str, list[str]] = {}
    for hit in fresh:
        by_v.setdefault(hit.vertical, []).append(hit.channel)
    for vertical, chans in sorted(by_v.items()):
        lines.append(f"# 垂直领域: {vertical}（{len(chans)} 个）")
        lines.extend(sorted(chans))
    with file.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return file


def load_candidate_file(path: str | Path) -> list[str]:
    """从文本文件读候选频道名（支持 # 注释，兼容 t.me/xxx 形式）。"""
    file = Path(path)
    if not file.exists():
        return []
    out: list[str] = []
    for raw in file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.search(r"t\.me/([A-Za-z0-9_]{4,})", line)
        name = (m.group(1) if m else line.split()[0]).lstrip("@")
        if name.startswith(("joinchat", "addstickers", "share", "iv")):
            continue
        out.append(name)
    return list(dict.fromkeys(out))


__all__ = [
    "KEEP_CATEGORIES", "ChannelHit", "HarvestResult", "append_channels",
    "harvest_tgnav", "load_candidate_file", "verify_channel", "verify_many",
]
