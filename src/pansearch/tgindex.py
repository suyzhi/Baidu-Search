"""Telegram 频道索引 —— 全网网盘链接的主要产地，直连不经过聚合引擎。

为什么要自己抓：
  网盘聚合引擎（PanSou）本质也是抓这些 TG 频道，但公共实例既限流又超时，
  一次搜索只给你它当次抓到的部分结果。直连 t.me 可以：
    * 并行打上百个频道，不受第三方限流
    * 用 ?before=<msg_id> 向历史翻页，越挖越深
    * 落进本地 SQLite —— 之后按关键词检索是毫秒级，且覆盖只增不减

网页预览分页（实测可用）：
    https://t.me/s/<channel>           最新一页（约 20 条）
    https://t.me/s/<channel>?before=<oldest_msg_id>   继续往前翻
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import CACHE_DIR, CONFIG_DIR
from .extract import extract_from_text, html_to_text
from .normalize import URL_RE, detect_pan_type, pwd_from_url

DEFAULT_DB = CACHE_DIR / "tg_index.sqlite3"
DEFAULT_CHANNELS_FILE = CONFIG_DIR / "tg_channels.txt"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_POST_RE = re.compile(r'data-post="([^"/]+)/(\d+)"')
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*(?:<div|<span|</div>)', re.S
)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_messages (
    channel    TEXT NOT NULL,
    msg_id     INTEGER NOT NULL,
    posted_at  TEXT,
    text       TEXT NOT NULL,
    links      TEXT NOT NULL,      -- JSON: [{"url":..,"pwd":..}]
    indexed_at REAL NOT NULL,
    PRIMARY KEY (channel, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_tg_channel ON tg_messages(channel, msg_id DESC);
CREATE TABLE IF NOT EXISTS tg_channels (
    channel      TEXT PRIMARY KEY,
    newest_id    INTEGER,
    oldest_id    INTEGER,
    msg_count    INTEGER NOT NULL DEFAULT 0,
    last_crawl   REAL,
    last_status  TEXT
);
"""


def load_channels(path: str | Path | None = None) -> list[str]:
    """读取频道清单（# 注释、空行忽略）。"""
    file = Path(path or DEFAULT_CHANNELS_FILE)
    if not file.exists():
        return []
    out: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip().lstrip("@")
        if name and name not in out:
            out.append(name)
    return out


@dataclass
class TgMessage:
    channel: str
    msg_id: int
    posted_at: str | None
    text: str
    links: list[dict] = field(default_factory=list)


def parse_channel_page(html: str, channel: str) -> list[TgMessage]:
    """解析 t.me/s/<channel> 页面里的消息。"""
    marks = list(_POST_RE.finditer(html))
    if not marks:
        return []

    out: list[TgMessage] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        slice_ = html[m.start():end]

        text_m = _TEXT_RE.search(slice_)
        raw_text = text_m.group(1) if text_m else ""
        plain = " ".join(html_to_text(raw_text).split())

        # 链接：正文 href 里和纯文本里都要抓
        links: list[dict] = []
        seen: set[str] = set()
        for hit in extract_from_text(raw_text, source=f"tg:{channel}", kind="tg") + \
                extract_from_text(plain, source=f"tg:{channel}", kind="tg"):
            if hit.url in seen:
                continue
            seen.add(hit.url)
            links.append({"url": hit.url, "pwd": hit.pwd})
        if not links:
            # 兜底：消息里可能只有裸链接，没有 message_text 块
            for raw in URL_RE.findall(slice_):
                decoded = html_mod.unescape(raw).rstrip("。，、；;!！?？'\"")
                if detect_pan_type(decoded).value == "other" or decoded in seen:
                    continue
                seen.add(decoded)
                links.append({"url": decoded, "pwd": pwd_from_url(decoded)})

        if not plain and not links:
            continue

        time_m = _TIME_RE.search(slice_)
        out.append(
            TgMessage(
                channel=m.group(1) or channel,
                msg_id=int(m.group(2)),
                posted_at=time_m.group(1) if time_m else None,
                text=plain[:4000],
                links=links,
            )
        )
    return out


class TgIndex:
    """TG 消息的本地索引（SQLite）。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---- 写入 ----
    def upsert(self, messages: list[TgMessage]) -> int:
        if not messages:
            return 0
        now = time.time()
        rows = [
            (m.channel, m.msg_id, m.posted_at, m.text,
             json.dumps(m.links, ensure_ascii=False), now)
            for m in messages
        ]
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR REPLACE INTO tg_messages"
            " (channel, msg_id, posted_at, text, links, indexed_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        return self.conn.total_changes - before

    def mark_channel(self, channel: str, newest: int | None, oldest: int | None,
                     status: str = "ok") -> None:
        cur = self.conn.execute(
            "SELECT COUNT(*) FROM tg_messages WHERE channel = ?", (channel,)
        ).fetchone()
        count = cur[0] if cur else 0
        prev = self.conn.execute(
            "SELECT newest_id, oldest_id FROM tg_channels WHERE channel = ?", (channel,)
        ).fetchone()
        prev_newest = prev[0] if prev else None
        prev_oldest = prev[1] if prev else None

        merged_newest = max([v for v in (newest, prev_newest) if v is not None], default=None)
        merged_oldest = min([v for v in (oldest, prev_oldest) if v is not None], default=None)
        self.conn.execute(
            "INSERT OR REPLACE INTO tg_channels"
            " (channel, newest_id, oldest_id, msg_count, last_crawl, last_status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (channel, merged_newest, merged_oldest, count, time.time(), status),
        )
        self.conn.commit()

    def oldest_id(self, channel: str) -> int | None:
        row = self.conn.execute(
            "SELECT oldest_id FROM tg_channels WHERE channel = ?", (channel,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    # ---- 查询 ----
    def search(self, kw: str, limit: int = 500) -> list[dict]:
        """按关键词检索本地索引，命中词数多的优先。"""
        terms = [t for t in re.split(r"[\s,，、/|·]+", kw.strip()) if t] or [kw.strip()]
        terms = terms[:6]
        if not any(terms):
            return []

        like = [f"%{t}%" for t in terms]
        score_expr = " + ".join(["CASE WHEN text LIKE ? THEN 1 ELSE 0 END"] * len(terms))
        where = " OR ".join(["text LIKE ?"] * len(terms))

        sql = (
            f"SELECT channel, msg_id, posted_at, text, links, ({score_expr}) AS hits"
            f" FROM tg_messages WHERE {where}"
            " ORDER BY hits DESC, msg_id DESC LIMIT ?"
        )
        params = [*like, *like, limit]
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {"channel": r[0], "msg_id": r[1], "posted_at": r[2],
             "text": r[3], "links": json.loads(r[4] or "[]"), "hits": r[5]}
            for r in rows
        ]

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM tg_messages").fetchone()[0]
        with_links = self.conn.execute(
            "SELECT COUNT(*) FROM tg_messages WHERE links != '[]'"
        ).fetchone()[0]
        channels = self.conn.execute(
            "SELECT COUNT(*) FROM tg_channels WHERE msg_count > 0"
        ).fetchone()[0]
        last = self.conn.execute("SELECT MAX(last_crawl) FROM tg_channels").fetchone()[0]
        return {
            "messages": total,
            "messages_with_links": with_links,
            "channels_indexed": channels,
            "db": str(self.path),
            "last_crawl": last,
        }

    def channel_rows(self) -> list[tuple]:
        return self.conn.execute(
            "SELECT channel, msg_count, newest_id, oldest_id, last_status"
            " FROM tg_channels ORDER BY msg_count DESC"
        ).fetchall()

    def close(self) -> None:
        self.conn.close()


class TgCrawler:
    """并发抓取 TG 频道页面并写入索引。"""

    def __init__(self, *, concurrency: int = 12, timeout: float = 20.0,
                 index: TgIndex | None = None, pages_delay: float = 0.15):
        self.concurrency = concurrency
        self.timeout = timeout
        self.index = index or TgIndex()
        self.sem = asyncio.Semaphore(concurrency)
        self.pages_delay = pages_delay
        self.stats = {"channels": 0, "pages": 0, "messages": 0, "new": 0, "errors": 0}

    async def _page(self, client: httpx.AsyncClient, channel: str,
                    before: int | None) -> tuple[list[TgMessage], str]:
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        async with self.sem:
            try:
                resp = await client.get(url, headers={"User-Agent": UA,
                                                      "Accept-Language": "zh-CN,zh;q=0.9"})
            except httpx.HTTPError as exc:
                return [], f"err:{type(exc).__name__}"
        if resp.status_code != 200:
            return [], f"http:{resp.status_code}"
        msgs = parse_channel_page(resp.text, channel)
        return msgs, "ok" if msgs else "empty"

    async def crawl_channel(self, client: httpx.AsyncClient, channel: str,
                            pages: int, *, deepen: bool = False) -> None:
        before: int | None = None
        if deepen:
            before = self.index.oldest_id(channel)

        got: list[TgMessage] = []
        status = "ok"
        for page in range(pages):
            msgs, status = await self._page(client, channel, before)
            self.stats["pages"] += 1
            if not msgs:
                break
            got.extend(msgs)
            before = min(m.msg_id for m in msgs)
            if self.pages_delay:
                await asyncio.sleep(self.pages_delay)

        if got:
            self.stats["new"] += self.index.upsert(got)
            self.stats["messages"] += len(got)
            self.index.mark_channel(
                channel,
                newest=max(m.msg_id for m in got),
                oldest=min(m.msg_id for m in got),
                status=status,
            )
        else:
            self.index.mark_channel(channel, None, None, status=status)
            if status.startswith(("err:", "http:")):
                self.stats["errors"] += 1
        self.stats["channels"] += 1

    async def crawl(self, channels: list[str], pages: int = 1, *,
                    deepen: bool = False) -> dict:
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, http2=True,
            headers={"User-Agent": UA},
        ) as client:
            await asyncio.gather(
                *(self.crawl_channel(client, ch, pages, deepen=deepen) for ch in channels),
                return_exceptions=True,
            )
        return dict(self.stats)
