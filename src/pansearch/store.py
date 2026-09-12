"""本地存储：验活结果 TTL 缓存（D 环私有索引的基础）。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import CACHE_DIR, DEFAULT_DB
from .models import Status, VerifyResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verify_cache (
    surl       TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    errno      INTEGER,
    method     TEXT,
    note       TEXT,
    checked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS search_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kw       TEXT NOT NULL,
    ts       REAL NOT NULL,
    hits     INTEGER,
    alive    INTEGER
);
"""


class VerifyCache:
    """按 (surl, 提取码) 缓存验活结果，避免重复打百度接口。

    注意：状态依赖提取码（码对=alive / 码错=wrong_pwd），所以缓存键必须带上 pwd。
    """

    def __init__(self, path: str | Path | None = None, ttl_hours: float = 6.0):
        self.path = Path(path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_hours * 3600
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    @staticmethod
    def make_key(surl: str, pwd: str | None = None) -> str:
        return f"{surl}|{pwd or ''}"

    def get(self, surl: str, pwd: str | None = None) -> VerifyResult | None:
        row = self.conn.execute(
            "SELECT status, errno, method, note, checked_at FROM verify_cache WHERE surl = ?",
            (self.make_key(surl, pwd),),
        ).fetchone()
        if not row:
            return None
        status, errno, method, note, checked_at = row
        if self.ttl > 0 and time.time() - checked_at > self.ttl:
            return None
        try:
            st = Status(status)
        except ValueError:
            return None
        return VerifyResult(
            status=st,
            errno=errno,
            method=f"cache:{method or ''}".rstrip(":"),
            note=note,
            checked_at=_dt(checked_at),
        )

    def put(self, surl: str, result: VerifyResult, pwd: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO verify_cache (surl, status, errno, method, note, checked_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                self.make_key(surl, pwd),
                result.status.value,
                result.errno,
                result.method,
                result.note,
                time.time(),
            ),
        )
        self.conn.commit()

    def stats(self) -> dict:
        row = self.conn.execute("SELECT COUNT(*) FROM verify_cache").fetchone()
        return {"cached_links": row[0] if row else 0, "db": str(self.path)}

    def log_search(self, kw: str, hits: int, alive: int) -> None:
        self.conn.execute(
            "INSERT INTO search_log (kw, ts, hits, alive) VALUES (?, ?, ?, ?)",
            (kw, time.time(), hits, alive),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def _dt(ts: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc)


__all__ = ["VerifyCache", "CACHE_DIR"]
