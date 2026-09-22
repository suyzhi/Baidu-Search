"""本地存储：验活结果 TTL 缓存（D 环私有索引的基础）。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import CACHE_DIR, DEFAULT_DB
from .models import Status, VerifyResult

# 缓存语义版本：新增字段 / 改变判定逻辑时 +1，旧缓存自动失效
CACHE_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verify_cache (
    surl         TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    errno        INTEGER,
    method       TEXT,
    note         TEXT,
    checked_at   REAL NOT NULL,
    pwd_verified INTEGER NOT NULL DEFAULT 0,
    title_hint   TEXT
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
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """旧库补列，避免升级后读不到 pwd_verified。"""
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(verify_cache)")}
        if "pwd_verified" not in cols:
            self.conn.execute(
                "ALTER TABLE verify_cache ADD COLUMN pwd_verified INTEGER NOT NULL DEFAULT 0"
            )
        if "title_hint" not in cols:
            self.conn.execute("ALTER TABLE verify_cache ADD COLUMN title_hint TEXT")

    @staticmethod
    def make_key(surl: str, pwd: str | None = None) -> str:
        # 版本前缀：语义变化时改 CACHE_VERSION 即可让旧缓存整体失效
        return f"v{CACHE_VERSION}|{surl}|{pwd or ''}"

    def get(self, surl: str, pwd: str | None = None) -> VerifyResult | None:
        row = self.conn.execute(
            "SELECT status, errno, method, note, checked_at, pwd_verified, title_hint"
            " FROM verify_cache WHERE surl = ?",
            (self.make_key(surl, pwd),),
        ).fetchone()
        if not row:
            return None
        status, errno, method, note, checked_at, pwd_verified, title_hint = row
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
            pwd_verified=bool(pwd_verified),
            title_hint=title_hint,
        )

    def put(self, surl: str, result: VerifyResult, pwd: str | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO verify_cache"
            " (surl, status, errno, method, note, checked_at, pwd_verified, title_hint)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.make_key(surl, pwd),
                result.status.value,
                result.errno,
                result.method,
                result.note,
                time.time(),
                1 if result.pwd_verified else 0,
                result.title_hint,
            ),
        )
        self.conn.commit()

    def stats(self) -> dict:
        row = self.conn.execute("SELECT COUNT(*) FROM verify_cache").fetchone()
        return {"cached_links": row[0] if row else 0, "db": str(self.path)}

    # ------------------------------------------------------------------ 定时复验
    def stale(self, older_than_hours: float, limit: int = 200) -> list[dict]:
        """列出已经/即将过期的缓存条目，供定时复验使用。

        为什么要它：验活缓存有 TTL（默认 6 小时），过期后**下一次搜索才会去重验**——
        也就是说一个关键词可能半年没人搜过，它下面的链接就半年没被复核过。
        定时任务按"最久没验的先验"把队列推平，而不是靠用户偶然搜到。

        返回 [{key, pwd, status, checked_at, age_hours}]；key 是资源键
        （baidu:1AbC / quark:pan.quark.cn/s/xxx），复验时用它重建 Resource。
        """
        cutoff = time.time() - older_than_hours * 3600
        rows = self.conn.execute(
            "SELECT surl, status, checked_at FROM verify_cache"
            " WHERE checked_at <= ? ORDER BY checked_at ASC LIMIT ?",
            (cutoff, int(limit)),
        ).fetchall()
        out: list[dict] = []
        for surl, status, checked_at in rows:
            key, _, pwd = str(surl).partition("|")
            key, _, _ = key.partition("|") if key.startswith(f"v{CACHE_VERSION}") else (surl, "", "")
            # 缓存键格式：v{CACHE_VERSION}|<resource_key>|<pwd>
            parts = str(surl).split("|")
            if len(parts) < 2:
                continue
            out.append({
                "key": parts[1],
                "pwd": parts[2] if len(parts) > 2 and parts[2] else None,
                "status": status,
                "checked_at": checked_at,
                "age_hours": round((time.time() - checked_at) / 3600, 1),
            })
        return out

    def pending_count(self, older_than_hours: float) -> int:
        cutoff = time.time() - older_than_hours * 3600
        row = self.conn.execute(
            "SELECT COUNT(*) FROM verify_cache WHERE checked_at <= ?", (cutoff,)
        ).fetchone()
        return int(row[0]) if row else 0


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
