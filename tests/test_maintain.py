"""定时维护测试（离线）。

覆盖三件容易出错的事：
  · 资源键还原成 URL（百度的键里只有 surl，必须拼回 pan.baidu.com/s/<surl>）；
  · 复验队列的选择（按"最久没验"排序，且不把预算浪费在已确认死掉的老链接上）；
  · 最小间隔与运行锁（定时任务重叠时后到的要跳过，陈旧锁要能接管）。
"""

from __future__ import annotations

import json
import time

import httpx
import pytest

from pansearch import maintain
from pansearch.dedupe import build_resources
from pansearch.models import RawHit, Status, VerifyResult
from pansearch.store import VerifyCache
from pansearch.verify import BaiduVerifier


def make_cache(tmp_path, entries):
    """造一个带条目的验活缓存：entries = [(cache_key, pwd, status, age_hours)]

    cache_key 必须和真实代码写进去的一致：百度验活按**裸 surl** 写（1AbCdEf），
    其它网盘按资源键写（quark:pan.quark.cn/s/xxx）。
    """
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


@pytest.mark.parametrize("key,expect", [
    ("1AbCdEf", "baidu:1AbCdEf"),          # 百度验活写缓存用的是裸 surl
    ("1Ab-Cd_Ef", "baidu:1Ab-Cd_Ef"),
    ("baidu:1AbCdEf", "baidu:1AbCdEf"),    # 已经是资源键的原样返回
    ("quark:pan.quark.cn/s/abc123", "quark:pan.quark.cn/s/abc123"),
    ("", ""),
    (None, ""),
])
def test_cache_key_to_resource_key(key, expect):
    assert maintain.cache_key_to_resource_key(key) == expect


# ---------------------------------------------------------------- 队列选择
def test_stale_resources_orders_by_age_and_skips_fresh_dead(tmp_path):
    cache = make_cache(tmp_path, [
        ("1AAAAAAA", "aaaa", "alive", 240),            # 最久
        ("1BBBBBBB", "bbbb", "alive", 120),
        ("1CCCCCCC", "cccc", "dead", 10),              # 刚确认死掉：不该再花预算
        ("1DDDDDDD", None, "not_found", 5),
        ("quark:pan.quark.cn/s/xyz", None, "alive", 200),
    ])
    resources, picked = maintain.stale_resources(cache, older_than_hours=6, limit=10)
    keys = [p["key"] for p in picked]
    assert "baidu:1CCCCCCC" not in keys, "刚判死的链接不该反复验"
    assert "baidu:1DDDDDDD" not in keys
    assert keys[0] == "baidu:1AAAAAAA", "最久没验的排最前"
    assert {r.url for r in resources} == {
        "https://pan.baidu.com/s/1AAAAAAA",
        "https://pan.baidu.com/s/1BBBBBBB",
        "https://pan.quark.cn/s/xyz",
    }
    cache.close()


async def test_baidu_rows_written_by_verifier_are_rechecked(tmp_path):
    """回归：百度验活按裸 surl 写缓存（v2|1AbC…|pwd），键里没有 "baidu:"。

    之前 rebuild_url 拆不出网盘类型直接返回 None，定时复验从来没验过一条百度链接。
    这里走真实的写缓存路径，而不是手造一个代码根本不会写的 "baidu:xxx" 键。
    """
    surl = "1AbCdEfGhIjKlMnOpQrStUv"
    cache = VerifyCache(tmp_path / "c.sqlite3", ttl_hours=6)
    verifier = BaiduVerifier(
        cfg={"rate_limit_qps": 0, "concurrency": 2, "timeout": 5, "retries": 0},
        cache=cache,
    )
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"errno": 0})

    verifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    verifier._warmed = True
    try:
        res = build_resources([RawHit(source="t", kind="t",
                                      url=f"https://pan.baidu.com/s/{surl}", pwd="abcd")])[0]
        await verifier.verify(res)
        rows = [r[0] for r in cache.conn.execute("SELECT surl FROM verify_cache")]
        assert rows == [cache.make_key(surl, "abcd")], "百度缓存键应是裸 surl"

        cache.conn.execute("UPDATE verify_cache SET checked_at = ?", (time.time() - 48 * 3600,))
        cache.conn.commit()

        resources, picked = maintain.stale_resources(cache, older_than_hours=6, limit=10)
        assert [p["key"] for p in picked] == [f"baidu:{surl}"]
        assert [(r.url, r.surl, r.pwd) for r in resources] == [
            (f"https://pan.baidu.com/s/{surl}", surl, "abcd")
        ]

        # 复验必须写回同一行，否则它会一直卡在队列最前面
        calls.clear()
        await verifier.verify(resources[0], use_cache=False)
        assert calls, "复验应真的发出请求"
        assert cache.pending_count(6) == 0
    finally:
        await verifier._client.aclose()
        cache.close()


def test_stale_resources_retries_long_dead(tmp_path):
    """死链超过 dead_retry_days 再验一次：分享者可能重新上传。"""
    cache = make_cache(tmp_path, [("1ZZZZZZZ", None, "dead", 24 * 40)])
    resources, picked = maintain.stale_resources(cache, older_than_hours=6, limit=5)
    assert len(picked) == 1 and len(resources) == 1
    cache.close()


def test_stale_resources_respects_limit(tmp_path):
    cache = make_cache(tmp_path, [
        (f"1{i:07d}", None, "alive", 100 + i) for i in range(10)
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
