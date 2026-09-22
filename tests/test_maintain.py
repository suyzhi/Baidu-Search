"""定时维护测试（离线）。

覆盖三件容易出错的事：
  · 资源键还原成 URL（百度的键里只有 surl，必须拼回 pan.baidu.com/s/<surl>）；
  · 复验队列的选择（按"最久没验"排序，且不把预算浪费在已确认死掉的老链接上）；
  · 最小间隔与运行锁（定时任务重叠时后到的要跳过，陈旧锁要能接管）。
"""

from __future__ import annotations

import json
import time

import pytest

from pansearch import maintain
from pansearch.models import Status, VerifyResult
from pansearch.store import VerifyCache


def make_cache(tmp_path, entries):
    """造一个带条目的验活缓存：entries = [(resource_key, pwd, status, age_hours)]"""
    cache = VerifyCache(tmp_path / "c.sqlite3", ttl_hours=6)
    for key, pwd, status, age in entries:
        cache.put(key, VerifyResult(status=Status(status)), pwd=pwd)
        old = time.time() - age * 3600
        cache.conn.execute("UPDATE verify_cache SET checked_at = ? WHERE surl = ?",
                           (old, cache.make_key(key, pwd)))
    cache.conn.commit()
    return cache


# ---------------------------------------------------------------- 键 → URL
@pytest.mark.parametrize("key,expect", [
    ("baidu:1AbCdEf", "https://pan.baidu.com/s/1AbCdEf"),
    ("quark:pan.quark.cn/s/abc123", "https://pan.quark.cn/s/abc123"),
    ("115:115cdn.com/s/xyz", "https://115cdn.com/s/xyz"),
    ("alipan:www.alipan.com/s/abc", "https://www.alipan.com/s/abc"),
    ("", None),
    ("nonsense", None),
    ("baidu:", None),
])
def test_rebuild_url(key, expect):
    assert maintain.rebuild_url(key) == expect


# ---------------------------------------------------------------- 队列选择
def test_stale_resources_orders_by_age_and_skips_fresh_dead(tmp_path):
    cache = make_cache(tmp_path, [
        ("baidu:AAAAAAAA", "aaaa", "alive", 240),      # 最久
        ("baidu:BBBBBBBB", "bbbb", "alive", 120),
        ("baidu:CCCCCCCC", "cccc", "dead", 10),        # 刚确认死掉：不该再花预算
        ("baidu:DDDDDDDD", None, "not_found", 5),
        ("quark:pan.quark.cn/s/xyz", None, "alive", 200),
    ])
    resources, picked = maintain.stale_resources(cache, older_than_hours=6, limit=10)
    keys = [p["key"] for p in picked]
    assert "baidu:CCCCCCCC" not in keys, "刚判死的链接不该反复验"
    assert "baidu:DDDDDDDD" not in keys
    assert keys[0] == "baidu:AAAAAAAA", "最久没验的排最前"
    assert {r.url for r in resources} == {
        "https://pan.baidu.com/s/AAAAAAAA",
        "https://pan.baidu.com/s/BBBBBBBB",
        "https://pan.quark.cn/s/xyz",
    }
    cache.close()


def test_stale_resources_retries_long_dead(tmp_path):
    """死链超过 dead_retry_days 再验一次：分享者可能重新上传。"""
    cache = make_cache(tmp_path, [("baidu:ZZZZZZZZ", None, "dead", 24 * 40)])
    resources, picked = maintain.stale_resources(cache, older_than_hours=6, limit=5)
    assert len(picked) == 1 and len(resources) == 1
    cache.close()


def test_stale_resources_respects_limit(tmp_path):
    cache = make_cache(tmp_path, [
        (f"baidu:{i:08d}", None, "alive", 100 + i) for i in range(10)
    ])
    _, picked = maintain.stale_resources(cache, older_than_hours=6, limit=3)
    assert len(picked) == 3
    cache.close()


# ---------------------------------------------------------------- 锁与间隔
def test_lock_is_exclusive_and_takes_over_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(maintain, "LOCK_PATH", tmp_path / "lock.json")
    assert maintain.acquire_lock() is True
    assert maintain.acquire_lock() is False, "第二个进程必须被挡住"
    maintain.release_lock()
    assert maintain.acquire_lock() is True

    # 陈旧锁（超过 LOCK_STALE_SECONDS = 3 小时）要能接管，
    # 否则一次 kill -9 就会让定时任务永久停摆
    maintain.LOCK_PATH.write_text(
        json.dumps({"pid": 1, "started_at": time.time() - 4 * 3600})
    )
    assert maintain.acquire_lock() is True
    maintain.release_lock()


async def test_run_maintenance_skips_when_recent(tmp_path, monkeypatch):
    monkeypatch.setattr(maintain, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(maintain, "LOCK_PATH", tmp_path / "lock.json")
    maintain.save_state({"last_run": time.time() - 60})          # 1 分钟前刚跑过

    report = await maintain.run_maintenance(min_interval_hours=6.0, skip_index=True,
                                            skip_verify=True)
    assert report.skipped and "小时" in report.skipped
    assert maintain.LOCK_PATH.exists() is False, "跳过时不该留下锁"


async def test_run_maintenance_forced_runs_both_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(maintain, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(maintain, "LOCK_PATH", tmp_path / "lock.json")

    calls = {}

    async def fake_index(**kwargs):
        calls["index"] = kwargs
        return {"channels": 430, "pages": 3, "messages": 120, "new": 42, "errors": 0}

    fake_telegram = type("M", (), {"ensure_index": staticmethod(fake_index)})
    monkeypatch.setitem(__import__("sys").modules, "pansearch.adapters.telegram", fake_telegram)

    report = await maintain.run_maintenance(force=True, index_pages=3, verify_limit=0)
    assert calls["index"]["deepen"] is True and calls["index"]["pages"] == 3
    assert report.index["new"] == 42
    assert report.skipped is None
    assert maintain.load_state()["runs"] == 1
    assert maintain.LOCK_PATH.exists() is False, "跑完要释放锁"
