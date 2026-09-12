"""本地缓存（验活 TTL 缓存）测试。"""

from __future__ import annotations

import time

from pansearch.models import Status, VerifyResult
from pansearch.store import VerifyCache


def test_put_get_roundtrip(tmp_path):
    cache = VerifyCache(tmp_path / "c.sqlite3", ttl_hours=6)
    cache.put("1AAA", VerifyResult(status=Status.ALIVE, errno=0, method="share_verify"), "abcd")
    got = cache.get("1AAA", "abcd")
    assert got is not None
    assert got.status is Status.ALIVE
    assert got.errno == 0
    assert (got.method or "").startswith("cache")


def test_key_includes_pwd(tmp_path):
    cache = VerifyCache(tmp_path / "c.sqlite3")
    cache.put("1AAA", VerifyResult(status=Status.ALIVE), "abcd")
    assert cache.get("1AAA", "abcd") is not None
    assert cache.get("1AAA", "zzzz") is None      # 不同提取码
    assert cache.get("1AAA", None) is None


def test_ttl_expiry(tmp_path):
    cache = VerifyCache(tmp_path / "c.sqlite3", ttl_hours=0.0001)   # 0.36 秒
    cache.put("1AAA", VerifyResult(status=Status.ALIVE), "abcd")
    assert cache.get("1AAA", "abcd") is not None
    time.sleep(0.5)
    assert cache.get("1AAA", "abcd") is None


def test_stats_and_log(tmp_path):
    cache = VerifyCache(tmp_path / "c.sqlite3")
    cache.put("1AAA", VerifyResult(status=Status.ALIVE))
    cache.log_search("三体", 10, 4)
    info = cache.stats()
    assert info["cached_links"] == 1
    rows = cache.conn.execute("SELECT kw, hits, alive FROM search_log").fetchall()
    assert rows == [("三体", 10, 4)]
