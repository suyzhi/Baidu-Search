"""垂直资源站适配器测试（MockTransport，离线）。

背景：音乐制作插件（VST/音源）不在 TG 频道里发网盘链接，
实测 Serum / Omnisphere / 大气合成器 在 21 万条 TG 索引里是 0 条。
它们的资源发布在专业资源站上，且下载入口在**详情页**里。
"""

from __future__ import annotations

import httpx
import pytest

from pansearch.adapters.sitesearch import _SKIP_PATH_PARTS, SiteSearchAdapter

SEARCH_PAGE = """
<html><head><title>You searched for serum</title></head><body>
<a href="https://looptorrent.net/comments/">Comments</a>
<a href="https://looptorrent.net/wp-json/">wp-json</a>
<a href="https://looptorrent.net/category/vst/">VST 分类</a>
<a href="https://looptorrent.net/basic-wavez-melodic-dreams-vol-1-presets-for-serum-2/">
  Melodic Dreams for Serum 2</a>
<a href="https://looptorrent.net/amazound-ppg-storm-for-kontakt/">PPG Storm for Kontakt</a>
</body></html>
"""

POST_PAGE_SERUM = """
<html><head><title>Basic Wavez - Melodic Dreams Vol.1 Presets for Serum 2 - Loop Torrent</title>
</head><body>
<p>Download: magnet:?xt=urn:btih:0F0F45F06F13C55DF3384E4253FA6F69E99B73DF&dn=serum</p>
</body></html>
"""

POST_PAGE_KONTAKT = """
<html><head><title>Amazound - PPG Storm for KONTAKT - Loop Torrent</title></head><body>
<p>magnet:?xt=urn:btih:7A008EE6FAE92282EEB19470ED45C3236A46B773</p>
</body></html>
"""


def make_adapter(**cfg):
    base = {"max_pages": 4, "page_concurrency": 6, "page_timeout": 5, "concurrency": 2}
    base.update(cfg)
    return SiteSearchAdapter(base)


# ---------------------------------------------------------------- 候选页挑选
def test_pick_pages_filters_site_noise():
    """回归：/comments/ 与 /wp-json/ 会排在真详情页前面，把 max_pages 预算吃光。"""
    a = make_adapter()
    pages = a._pick_pages(
        SEARCH_PAGE, r"https://looptorrent\.net/[a-z0-9][a-z0-9-]{6,}/"
    )
    assert "https://looptorrent.net/comments/" not in pages
    assert "https://looptorrent.net/wp-json/" not in pages
    assert "https://looptorrent.net/category/vst/" not in pages
    assert "https://looptorrent.net/basic-wavez-melodic-dreams-vol-1-presets-for-serum-2/" in pages


def test_pick_pages_dedupes_and_handles_bad_regex():
    a = make_adapter()
    html = '<a href="https://x.com/aaaaaaaa/">1</a><a href="https://x.com/aaaaaaaa/">2</a>'
    assert a._pick_pages(html, r"https://x\.com/[a-z]{8}/") == ["https://x.com/aaaaaaaa/"]
    assert a._pick_pages(html, r"[unclosed") == []


def test_skip_list_covers_known_noise():
    for part in ("/comments", "/wp-json", "/wp-content", "/category/", "/tag/"):
        assert part in _SKIP_PATH_PARTS


# ---------------------------------------------------------------- 相关性闸门
@pytest.mark.parametrize(
    "title,kw,ok",
    [
        # 实测：中文软件站对 "Omnisphere" 没结果时，会回退显示"最新推荐"
        ("哔哩哔哩视频下载器 v0.2.0 便携版 - 果核剥壳", "Omnisphere", False),
        ("IObit Uninstaller - 大眼仔旭", "Omnisphere", False),
        ("Luftrum – Vangelis for Omnisphere 3 (SOUNDBANK)", "Omnisphere", True),
        # 搜 "Xfer Serum" 时标题只有 "Serum" 也算对
        ("Basic Wavez - Presets for Serum 2", "Xfer Serum", True),
        ("Page not found - Loop Torrent", "Omnisphere", False),
        (None, "Omnisphere", False),
    ],
)
def test_relevance_gate(title, kw, ok):
    assert SiteSearchAdapter._relevant(title, kw) is ok


# ---------------------------------------------------------------- 端到端（mock）
def router(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "/?s=" in url or "?s=" in url:
        return httpx.Response(200, text=SEARCH_PAGE)
    if "for-serum-2" in url:
        return httpx.Response(200, text=POST_PAGE_SERUM)
    if "for-kontakt" in url:
        return httpx.Response(200, text=POST_PAGE_KONTAKT)
    return httpx.Response(404, text="nope")


async def test_search_follows_detail_pages_and_extracts_magnets():
    a = make_adapter(sites=[{
        "name": "looptorrent",
        "search": "https://looptorrent.net/?s={q}",
        "result_re": r"https://looptorrent\.net/[a-z0-9][a-z0-9-]{6,}/",
    }])
    async with httpx.AsyncClient(transport=httpx.MockTransport(router)) as client:
        hits = await a.search("Serum", client)

    urls = {h.url for h in hits}
    assert any("0F0F45F06F13C55DF3384E4253FA6F69E99B73DF" in u for u in urls)
    # Kontakt 那页标题不含 "Serum"，必须被闸门挡掉
    assert not any("7A008EE6FAE92282EEB19470ED45C3236A46B773" in u for u in urls)
    assert all(h.source == "site:looptorrent" for h in hits)
    assert all(h.kind == "forum" for h in hits)
    assert all(h.origin for h in hits), "必须记录来源页，便于复核"


async def test_search_skips_site_with_no_results():
    """站点搜索返回"最新推荐"时不能把无关页面收进来。"""
    def only_noise(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "?s=" in url:
            return httpx.Response(
                200,
                text='<html><head><title>x</title></head><body>'
                     '<a href="https://x.com/aaaaaaaa/">1</a></body></html>',
            )
        return httpx.Response(
            200, text="<html><head><title>哔哩哔哩视频下载器 - 果核剥壳</title></head>"
                      "<body><a href='https://pan.quark.cn/s/abc123'>夸克</a></body></html>"
        )

    a = make_adapter(sites=[{
        "name": "ghxi",
        "search": "https://www.ghxi.com/?s={q}",
        "result_re": r"https://x\.com/[a-z]{8}/",
    }])
    async with httpx.AsyncClient(transport=httpx.MockTransport(only_noise)) as client:
        hits = await a.search("Omnisphere", client)
    assert hits == []


async def test_site_failure_is_isolated():
    def flaky(request: httpx.Request) -> httpx.Response:
        if "bad" in str(request.url):
            raise httpx.ConnectError("boom")
        return router(request)

    a = make_adapter(sites=[
        {"name": "bad", "search": "https://bad.example/?s={q}",
         "result_re": r"https://bad\.example/[a-z]{6,}/"},
        {"name": "looptorrent", "search": "https://looptorrent.net/?s={q}",
         "result_re": r"https://looptorrent\.net/[a-z0-9][a-z0-9-]{6,}/"},
    ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
        hits = await a.search("Serum", client)
    assert hits, "一个站挂了不能影响另一个站"
    assert all(h.source == "site:looptorrent" for h in hits)


def test_default_sites_include_vst_sources():
    from pansearch.adapters.sitesearch import DEFAULT_SITES

    names = {s["name"] for s in DEFAULT_SITES}
    assert {"audioz", "looptorrent", "vstorrent"} <= names
    for site in DEFAULT_SITES:
        assert "{q}" in site["search"]
        assert site["result_re"]
