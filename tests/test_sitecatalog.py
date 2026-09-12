"""垂直路由 + 资源站目录测试（离线）。"""

from __future__ import annotations

import httpx
import pytest

from pansearch.routing import GENERAL_VERTICAL, VERTICAL_KEYWORDS, classify
from pansearch.sitecatalog import (
    SiteEntry,
    SiteHealth,
    _path_shape,
    count_result_links,
    derive_result_re,
    load_catalog,
    save_catalog,
)


# ---------------------------------------------------------------- 垂直路由
@pytest.mark.parametrize(
    "kw,expected",
    [
        ("沙丘 4K HDR", "movie"),
        ("Serum 合成器", "audio-tool"),
        ("大气合成器", "audio-tool"),
        ("三体 电子书", "ebook"),
        ("考研数学 网课", "course"),
        ("Photoshop 破解", "software"),
        ("塞尔达 switch 汉化", "game"),
        ("周杰伦 无损", "music"),
        ("论文 sci-hub", "academic"),
        ("新番 动漫", "anime"),
    ],
)
def test_classify_main_vertical(kw, expected):
    assert expected in classify(kw)


def test_classify_returns_empty_for_unknown():
    """一个特征词都没命中时返回空 —— 调用方只打通用站，而不是乱打一通。"""
    assert classify("zzzzz") == []
    assert classify("") == []


def test_classify_respects_max_verticals():
    # "4K HDR 音乐 无损 电子书" 同时踩到多个领域
    got = classify("4K HDR 无损 电子书", max_verticals=2)
    assert len(got) <= 2


def test_every_vertical_has_keywords():
    for vertical, words in VERTICAL_KEYWORDS.items():
        assert words, f"{vertical} 没有任何特征词"


# ---------------------------------------------------------------- 形状推导
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/bilibilispxzq.html", r"/[a-z0-9_-]+\.html"),
        ("/15884.html", r"/\d+\.html"),
        ("/amazound-ppg-storm-for-kontakt/", r"/[a-z0-9_-]+/"),
        # slug 里混数字也不能被切碎（先替换单词、再替换纯数字）
        ("/luftrum-chronos-for-omnisphere-3-omnisphere-3/", r"/[a-z0-9_-]+/"),
        ("/archives/1234", r"/[a-z0-9_-]+/\d+"),
    ],
)
def test_path_shape(path, expected):
    assert _path_shape(path) == expected


@pytest.mark.parametrize("path", ["/category/vst/", "/wp-json/", "/tag/abc/", "/feed"])
def test_path_shape_rejects_site_noise(path):
    assert _path_shape(path) is None


def test_derive_result_re_picks_dominant_shape():
    html = """
    <a href="/category/vst/">cat</a>
    <a href="/basic-wavez-presets-for-serum/">1</a>
    <a href="/ghosthack-serum-presets/">2</a>
    <a href="/another-serum-bank/">3</a>
    """
    got = derive_result_re(html, "https://looptorrent.net")
    assert got == r"https://looptorrent\.net/[a-z0-9_-]+/"


def test_derive_result_re_empty_when_no_detail_links():
    assert derive_result_re('<a href="/about">x</a>', "https://x.com") == ""


def test_count_result_links_recognizes_slug_permalinks():
    """回归：只认 /123.html 会漏掉 WordPress 的 /slug/ 永久链接。"""
    html = ('<a href="/about">a</a>'
            '<a href="/amazound-ppg-storm-for-kontakt/">1</a>'
            '<a href="/luftrum-luftrum-34-for-omnisphere-3/">2</a>')
    assert count_result_links(html) == 2


# ---------------------------------------------------------------- 目录读写
def test_catalog_roundtrip(tmp_path):
    path = tmp_path / "sites.yaml"
    sites = [
        SiteEntry(name="ghxi", domain="www.ghxi.com", search="https://www.ghxi.com/?s={q}",
                  result_re=r"https://www\.ghxi\.com/[a-z0-9_-]+\.html",
                  verticals=["software"], verified=True),
        SiteEntry(name="looptorrent", domain="looptorrent.net",
                  search="https://looptorrent.net/?s={q}",
                  result_re=r"https://looptorrent\.net/[a-z0-9_-]+/",
                  verticals=["audio-tool", "music"], verified=True),
    ]
    save_catalog(sites, path)
    loaded = load_catalog(path)
    assert {e.host for e in loaded} == {"www.ghxi.com", "looptorrent.net"}
    ghxi = next(e for e in loaded if e.host == "www.ghxi.com")
    assert ghxi.verticals == ["software"] and ghxi.verified
    assert ghxi.result_re == r"https://www\.ghxi\.com/[a-z0-9_-]+\.html"


def test_catalog_missing_file_is_empty(tmp_path):
    assert load_catalog(tmp_path / "nope.yaml") == []


def test_shipped_catalog_entries_are_usable():
    """仓库自带的目录里，verified 的条目必须同时有 search 和 result_re。"""
    for entry in load_catalog():
        if entry.verified:
            assert entry.search and "{q}" in entry.search, entry.domain
            assert entry.result_re, entry.domain
    assert GENERAL_VERTICAL  # 通用标签常量存在


# ---------------------------------------------------------------- 健康度
def test_health_tracks_and_disables_failing_sites(tmp_path):
    health = SiteHealth(tmp_path / "h.sqlite3")
    health.record("good.com", 5)
    health.record("good.com", 3)
    for _ in range(6):
        health.record("bad.com", 0, "timeout")
    assert health.disabled() == {"bad.com"}
    # 成功一次就重置连续失败计数
    health.record("bad.com", 2)
    assert "bad.com" not in health.disabled()
    health.close()


def test_health_stats_sorted_by_hits(tmp_path):
    health = SiteHealth(tmp_path / "h.sqlite3")
    health.record("few.com", 1)
    health.record("many.com", 9)
    rows = health.stats()
    assert [r[0] for r in rows] == ["many.com", "few.com"]
    health.close()


# ---------------------------------------------------------------- 路由选择
def test_select_sites_filters_by_vertical_without_catalog(monkeypatch, tmp_path):
    """目录为空时退回内置清单（内置的都没有 verticals -> 视为通用）。"""
    from pansearch.adapters import sitesearch as mod

    monkeypatch.setattr(mod, "load_catalog", lambda *a, **k: [])
    adapter = mod.SiteSearchAdapter({"health_tracking": False, "max_sites": 3})
    picked = adapter.select_sites("Serum 合成器")
    assert len(picked) == 3


def test_select_sites_uses_verticals(monkeypatch):
    from pansearch.adapters import sitesearch as mod

    catalog = [
        SiteEntry(name="vst", domain="vst.example", search="https://vst.example/?s={q}",
                  result_re="https://vst\\.example/[a-z]+/", verticals=["audio-tool"], verified=True),
        SiteEntry(name="book", domain="book.example", search="https://book.example/?s={q}",
                  result_re="https://book\\.example/[a-z]+/", verticals=["ebook"], verified=True),
        SiteEntry(name="any", domain="any.example", search="https://any.example/?s={q}",
                  result_re="https://any\\.example/[a-z]+/", verticals=["general"], verified=True),
    ]
    monkeypatch.setattr(mod, "load_catalog", lambda *a, **k: catalog)
    adapter = mod.SiteSearchAdapter({"health_tracking": False})

    picked = {e.name for e in adapter.select_sites("Serum 合成器")}
    assert picked == {"vst", "any"}, "只该打音频类和通用站，不该打电子书站"

    picked2 = {e.name for e in adapter.select_sites("电子书 epub")}
    assert picked2 == {"book", "any"}

    # 认不出领域时只打通用站
    picked3 = {e.name for e in adapter.select_sites("zzzzz")}
    assert picked3 == {"any"}


def test_select_sites_skips_unverified(monkeypatch):
    from pansearch.adapters import sitesearch as mod

    catalog = [
        SiteEntry(name="draft", domain="d.example", search="https://d.example/?s={q}",
                  result_re="x", verticals=["general"], verified=False),
    ]
    monkeypatch.setattr(mod, "load_catalog", lambda *a, **k: catalog)
    adapter = mod.SiteSearchAdapter({"health_tracking": False})
    assert adapter.select_sites("随便") == []


# ---------------------------------------------------------------- 价值校验
def test_count_share_links_finds_netdisk_and_magnet():
    from pansearch.sitecatalog import count_share_links

    html = ('<a href="https://pan.quark.cn/s/abc123">夸克</a>'
            '<a href="https://pan.baidu.com/s/1AbCdEfGhIjKlMnOp">百度</a>'
            '<p>magnet:?xt=urn:btih:0F0F45F06F13C55DF3384E4253FA6F69E99B73DF</p>')
    assert count_share_links(html) == 3


def test_count_share_links_zero_for_streaming_site():
    """回归：ikanbot/rytv 是在线播放站、assrt 是字幕站 —— 搜索完全正常，
    但详情页里没有网盘链接，收进目录纯属占位浪费请求。"""
    from pansearch.sitecatalog import count_share_links

    assert count_share_links('<a href="/play/12345.html">在线观看</a>') == 0
    assert count_share_links('<a href="https://example.com/download.zip">下载字幕</a>') == 0


def test_count_share_links_ignores_plain_http():
    from pansearch.sitecatalog import count_share_links

    assert count_share_links('<a href="https://www.google.com">x</a>') == 0


async def test_probe_rejects_site_without_share_links():
    """搜得出结果但详情页没网盘链接的站，必须被判为不可用。"""
    from pansearch import sitecatalog as sc

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "?s=" in url or "search" in url:
            return httpx.Response(200, text=(
                '<html><head><title>search</title></head><body>'
                '<a href="/play/video-aaa-bbb/">1</a>'
                '<a href="/play/video-ccc-ddd/">2</a>'
                '<a href="/play/video-eee-fff/">3</a>'
                '<a href="/play/video-ggg-hhh/">4</a>'
                '</body></html>'))
        # 详情页：只有在线播放，没有网盘
        return httpx.Response(200, text="<html><head><title>剧集</title></head>"
                                        "<body><a href='/play/x'>在线观看</a></body></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await sc.probe_domain(client, "stream.example", terms=["剧集"])
    assert result.ok is False


async def test_probe_accepts_site_with_share_links():
    from pansearch import sitecatalog as sc

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "page-" in url:                     # 详情页：含网盘链接
            return httpx.Response(200, text=(
                '<html><head><title>资源 剧集</title></head><body>'
                '<a href="https://pan.quark.cn/s/abc123">夸克</a>'
                '<a href="https://pan.baidu.com/s/1AbCdEfGhIjKlMnOp">百度</a>'
                '</body></html>'))
        # 只有"真词"能搜出结果；无意义串返回空列表（对照法要求）
        if "%E5%89%A7%E9%9B%86" in url or "剧集" in url:
            return httpx.Response(200, text=(
                '<html><head><title>search 剧集</title></head><body>'
                '<a href="/page-aaa/">1</a><a href="/page-bbb/">2</a>'
                '<a href="/page-ccc/">3</a><a href="/page-ddd/">4</a>'
                '</body></html>'))
        return httpx.Response(200, text='<html><head><title>空</title></head><body></body></html>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await sc.probe_domain(client, "res.example", terms=["剧集"])
    assert result.ok is True
    assert result.link_yield > 0
    assert result.result_re


def test_maccms_pattern_is_available():
    """MacCMS/苹果CMS 是中文影视站的事实标准，缺了它影视类会全军覆没。"""
    from pansearch.sitecatalog import SEARCH_PATTERNS

    assert any("vodsearch" in p for p in SEARCH_PATTERNS)
    assert any("vod/search" in p for p in SEARCH_PATTERNS)
