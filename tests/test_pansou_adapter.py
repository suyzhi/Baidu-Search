"""PanSou 适配器测试（MockTransport，离线）。

重点锁住多实例降级：自建实例优先，失败要能自动落到公共实例。
"""

from __future__ import annotations

import httpx
import pytest

from pansearch.adapters.pansou import PansouAdapter

SAMPLE = {
    "code": 0,
    "data": {
        "total": 4,
        "merged_by_type": {
            "baidu": [
                {"url": "https://pan.baidu.com/s/1AAA?pwd=1111", "password": "1111",
                 "note": "三体 全集", "datetime": "2026-01-02T03:04:05+08:00",
                 "source": "tg:bdwpzhpd"},
                {"url": "https://pan.baidu.com/s/1BBB", "password": "",
                 "note": "三体 4K", "datetime": "", "source": "plugin:ikantv"},
            ],
            "quark": [
                {"url": "https://pan.quark.cn/s/cccc", "source": "plugin:wanou", "note": "三体"},
            ],
            "magnet": [
                {"url": "magnet:?xt=urn:btih:ABC", "source": "plugin:nyaa", "note": "三体"},
            ],
            "unknown_type": [
                {"url": "https://example.com/not-a-pan", "source": "plugin:x"},
            ],
        },
    },
}


def make_adapter(instances, **cfg):
    return PansouAdapter({"instances": instances, "retries": 0, "timeout": 5, **cfg})


async def test_parses_merged_by_type():
    def handler(request):
        return httpx.Response(200, json=SAMPLE)

    adapter = make_adapter(["http://a"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await adapter.search("三体", client)

    urls = {h.url for h in hits}
    assert "https://pan.baidu.com/s/1AAA?pwd=1111" in urls
    assert "https://pan.quark.cn/s/cccc" in urls
    assert "magnet:?xt=urn:btih:ABC" in urls
    # 非网盘链接必须被丢掉
    assert "https://example.com/not-a-pan" not in urls
    assert len(hits) == 4


async def test_source_kind_split():
    def handler(request):
        return httpx.Response(200, json=SAMPLE)

    adapter = make_adapter(["http://a"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await adapter.search("三体", client)

    by_source = {h.source: h.kind for h in hits}
    assert by_source["tg:bdwpzhpd"] == "tg"
    assert by_source["plugin:ikantv"] == "pansou"


async def test_password_and_time_parsed():
    def handler(request):
        return httpx.Response(200, json=SAMPLE)

    adapter = make_adapter(["http://a"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await adapter.search("三体", client)

    first = next(h for h in hits if h.url.endswith("1AAA?pwd=1111"))
    assert first.pwd == "1111"
    assert first.shared_at is not None
    assert first.shared_at.year == 2026


async def test_first_instance_wins():
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=SAMPLE)

    adapter = make_adapter(["http://primary", "http://backup"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await adapter.search("三体", client)

    assert all("primary" in c for c in calls), "首选实例可用时不该打备用实例"


async def test_falls_back_to_next_instance_on_error():
    """自建实例挂了要能自动降级到公共实例。"""
    calls: list[str] = []

    def handler(request):
        url = str(request.url)
        calls.append(url)
        if "primary" in url:
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=SAMPLE)

    adapter = make_adapter(["http://primary", "http://backup"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await adapter.search("三体", client)

    assert hits, "备用实例必须接上"
    assert any("primary" in c for c in calls)
    assert any("backup" in c for c in calls)


async def test_all_instances_down_raises():
    def handler(request):
        return httpx.Response(503, text="down")

    adapter = make_adapter(["http://a", "http://b"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            await adapter.search("三体", client)


async def test_non_json_response_does_not_crash():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    adapter = make_adapter(["http://a"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError):
            await adapter.search("三体", client)


async def test_empty_payload_returns_no_hits():
    def handler(request):
        return httpx.Response(200, json={"code": 0, "data": {"total": 0, "merged_by_type": {}}})

    adapter = make_adapter(["http://a"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await adapter.search("三体", client) == []


async def test_one_malformed_item_does_not_kill_the_batch():
    """回归：某个插件返回 netloc 带全角冒号的伪 URL，曾导致整条 ValueError
    把 PanSou 的所有结果带走（摘要里表现为 "pansou 失败"）。"""
    payload = {
        "code": 0,
        "data": {
            "total": 3,
            "merged_by_type": {
                "baidu": [
                    {"url": "https://pan.baidu.com/s/1AAA", "source": "plugin:good"},
                    {"url": "http://|file|电影：2026.mkv|2260", "source": "plugin:bad"},
                    {"url": "https://pan.quark.cn/s/cccc", "source": "plugin:good2"},
                ],
            },
        },
    }

    def handler(request):
        return httpx.Response(200, json=payload)

    adapter = make_adapter(["http://a"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await adapter.search("三体", client)

    urls = {h.url for h in hits}
    assert "https://pan.baidu.com/s/1AAA" in urls, "好数据必须保留"
    assert "https://pan.quark.cn/s/cccc" in urls
    assert len(hits) == 2, "坏数据被跳过，但不影响其余"


def test_instances_default_and_normalized():
    adapter = PansouAdapter({})
    assert adapter.instances == ["https://so.252035.xyz"]

    adapter2 = PansouAdapter({"instances": ["http://127.0.0.1:8888/", "http://x/"]})
    assert adapter2.instances == ["http://127.0.0.1:8888", "http://x"]
