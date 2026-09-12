"""C 环：Telegram 频道（本地索引直连）。

不经过任何第三方聚合引擎 —— 直接把频道消息抓进本地 SQLite，再按关键词检索。
好处：不受公共实例限流、可向历史翻页、二次检索毫秒级。

索引是"越用越全"的：每次 crawl 都往历史深处挖，覆盖只增不减。
"""

from __future__ import annotations

import asyncio

import httpx

from ..extract import _parse_time
from ..models import RawHit
from ..normalize import pwd_from_url
from ..tgindex import TgCrawler, TgIndex, load_channels
from .base import Adapter, register


@register
class TelegramAdapter(Adapter):
    name = "telegram"
    kind = "tg"

    @property
    def auto_index(self) -> bool:
        """索引为空时自动抓一轮最新消息，避免首次使用没有结果。"""
        return bool(self.cfg.get("auto_index", True))

    async def search(self, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        index = TgIndex(self.cfg.get("index_path") or None)
        try:
            rows = index.search(kw, limit=int(self.cfg.get("max_hits") or 400))

            if not rows and self.auto_index:
                channels = load_channels(self.cfg.get("channels_file") or None)
                if channels:
                    crawler = TgCrawler(
                        concurrency=int(self.cfg.get("crawl_concurrency") or 12),
                        timeout=float(self.cfg.get("timeout") or 20),
                        index=index,
                    )
                    await crawler.crawl(channels, pages=1)
                    rows = index.search(kw, limit=int(self.cfg.get("max_hits") or 400))

            return self._to_hits(rows)
        finally:
            index.close()

    @staticmethod
    def _to_hits(rows: list[dict]) -> list[RawHit]:
        hits: list[RawHit] = []
        for row in rows:
            origin = f"https://t.me/{row['channel']}/{row['msg_id']}"
            title = " ".join((row.get("text") or "").split())[:300] or None
            posted = _parse_time(row.get("posted_at"))
            links = row.get("links") or []
            if not links:
                continue
            for link in links:
                url = (link or {}).get("url")
                if not url:
                    continue
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
