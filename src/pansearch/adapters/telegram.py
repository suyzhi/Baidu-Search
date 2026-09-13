"""C 环：Telegram 频道（本地索引直连）。

不经过任何第三方聚合引擎 —— 直接把频道消息抓进本地 SQLite，再按关键词检索。
好处：不受公共实例限流、可向历史翻页、二次检索毫秒级。

索引是"越用越全"的：每次 crawl 都往历史深处挖，覆盖只增不减。
"""

from __future__ import annotations

import asyncio

import httpx

from ..extract import _parse_time, excerpt
from ..models import RawHit
from ..normalize import URL_RE, pwd_from_url
from ..tgindex import TgCrawler, TgIndex, load_channels
from .base import Adapter, register


@register
class TelegramAdapter(Adapter):
    name = "telegram"
    kind = "tg"
    primary_only = True  # 本地检索已经按主题词 OR 召回，无须重复扫描大索引。

    def __init__(self, cfg=None):
        super().__init__(cfg)
        self._seed_lock = asyncio.Lock()
        self._seed_attempted = False

    def _lookup(self, kw):
        index = TgIndex(self.cfg.get("index_path") or None)
        try:
            return index.search(kw, limit=int(self.cfg.get("max_hits") or 400)), index.is_empty()
        finally:
            index.close()

    @property
    def auto_index(self) -> bool:
        """索引为空时自动抓一轮最新消息，避免首次使用没有结果。"""
        return bool(self.cfg.get("auto_index", True))

    async def search(self, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        rows, empty = await asyncio.to_thread(self._lookup, kw)
        if empty and self.auto_index:
            async with self._seed_lock:
                if not self._seed_attempted:
                    self._seed_attempted = True
                    await self._seed()
            rows, _ = await asyncio.to_thread(self._lookup, kw)
        return self._to_hits(rows, kw)

    async def _seed(self):
        index = TgIndex(self.cfg.get("index_path") or None)
        try:
            if index.is_empty():
                channels = load_channels(self.cfg.get("channels_file") or None)
                if channels:
                    crawler = TgCrawler(
                        concurrency=int(self.cfg.get("crawl_concurrency") or 12),
                        timeout=float(self.cfg.get("timeout") or 20),
                        index=index,
                    )
                    await crawler.crawl(channels, pages=1)
        finally:
            index.close()

    @staticmethod
    def _local_title(text: str, url: str, kw: str, width: int = 300) -> str | None:
        """取链接**紧邻的前文**作为它自己的标题，而不是整条消息。

        频道里大量是"合集帖"：一条消息列出十几个资源、每个资源名后面跟一个链接，
        例如
            「会声会影软件及教程 https://…/jVF… iSkysoft PDF Editor https://…/CJi…
              … SolidWorks、CATIA） https://…/zLdw… Solidworks视频教程 https://…/k9i…」
        如果每个链接都拿**整条消息**当标题，搜「SolidWorks 破解」时，
        「iSkysoft PDF Editor」「AG视频解析」这些链接也会因为消息里出现过
        "SolidWorks" 被判成相关，全都挤进结果 —— 一条 21 链接的合集帖实测
        贡献了 22 条假相关。前文取到**上一个链接的结尾**为止；前文为空时返回 None，
        由调用方回退到整条消息的摘要。
        """
        pos = text.find(url)
        if pos < 0:
            return None
        prev_end = 0
        for m in URL_RE.finditer(text, 0, pos):
            prev_end = m.end()
        segment = text[prev_end:pos].strip(" \t\n:：-—|｜·,，、")
        if not segment:
            return None
        return excerpt(segment, kw, width) or None

    @staticmethod
    def _to_hits(rows: list[dict], kw: str = "") -> list[RawHit]:
        """标题按**每个链接自己的上下文**生成。

        优先用链接紧邻的前文（解决合集帖张冠李戴）；前文为空时回退到
        "围绕查询词的消息摘要"（不能用消息开头 —— 主题词可能在 300 字之后）。
        """
        hits: list[RawHit] = []
        for row in rows:
            origin = f"https://t.me/{row['channel']}/{row['msg_id']}"
            full_text = " ".join((row.get("text") or "").split())
            fallback = excerpt(full_text, kw, 300) or None
            posted = _parse_time(row.get("posted_at"))
            links = row.get("links") or []
            if not links:
                continue
            for link in links:
                try:
                    url = (link or {}).get("url")
                    if not url:
                        continue
                    title = TelegramAdapter._local_title(full_text, url, kw) or fallback
                    hits.append(
                        RawHit(
                            source=f"tg:{row['channel']}",
                            kind="tg",
                            url=url,
                            pwd=(link.get("pwd") or pwd_from_url(url)),
                            title=title,
                            shared_at=posted,
                            origin=origin,
                        )
                    )
                except Exception:
                    continue
        return hits


async def ensure_index(pages: int = 1, *, deepen: bool = False,
                       concurrency: int = 12) -> dict:
    """供 CLI 调用的便捷入口。"""
    index = TgIndex()
    try:
        channels = load_channels()
        if not channels:
            return {"error": "频道清单为空，请检查 config/tg_channels.txt"}
        moved = index.normalize_channel_keys(channels)
        crawler = TgCrawler(concurrency=concurrency, index=index)
        stats = await crawler.crawl(channels, pages=pages, deepen=deepen)
        if moved:
            stats["rekeyed"] = moved
        return stats
    finally:
        index.close()


__all__ = ["TelegramAdapter", "ensure_index", "asyncio"]
