"""公开 API 数据源适配器测试（离线）。

背景：学术与漫画爬页面基本爬不到 —— 实测 Gutenberg / libgen.li /
manhuagui / dm5 / manhuaren 的搜索页只返回导航链接（JS 渲染），
抽不到任何条目。但这几家有公开 API，所以单独做一层。
"""

from __future__ import annotations

import httpx
import pytest

from pansearch.adapters.apisources import (
    ApiSourcesAdapter,
    build_link,
    build_title,
    dig,
    dig_list,
    parse_items,
)

# ---------------------------------------------------------------- 点路径取值
def test_dig_scalar_paths():
    data = {"a": {"b": {"c": 42}}}
    assert dig(data, "a.b.c") == 42
    assert dig(data, "a.b") == {"c": 42}
    assert dig(data, "a.x") is None
    assert dig(data, None) is None
    assert dig("string", "a.b") is None


def test_dig_list_index_and_flatten():
    assert dig({"items": [{"t": "x"}, {"t": "y"}]}, "items.1.t") == "y"
    # 列表里逐项取字段，返回第一个非空
    assert dig({"items": [{}, {"t": "y"}]}, "items.t") == "y"
    # 目标本身是列表时取第一个非空（crossref 的 title 就是数组）
    assert dig({"title": ["", "real"]}, "title") == "real"


def test_dig_list_keeps_the_list():
    """回归：dig() 会把列表展开成第一个元素（那是"取字段"的语义），
    用它取 items 只会拿到 1 条 —— 实测 crossref/openalex 各返回 20 条却只出 1 条。"""
    data = {"message": {"items": [{"n": 1}, {"n": 2}, {"n": 3}]}}
    assert len(dig_list(data, "message.items")) == 3
    assert dig_list(data, "message.nope") == []
    assert dig_list({"x": {"y": 5}}, "x.y") == [5]      # 非列表包成单元素


# ---------------------------------------------------------------- 响应解析
CROSSREF = '{"message":{"items":[{"title":["A"],"URL":"https://doi.org/10.1/a"},' \
           '{"title":["B"],"URL":"https://doi.org/10.1/b"}]}}'
ARXIV_XML = ('<feed><entry><id>http://arxiv.org/abs/1234.5678v1</id>'
             '<title>Some Paper</title></entry>'
             '<entry><id>http://arxiv.org/abs/9999.0001v2</id>'
             '<title>Another Paper</title></entry></feed>')


def test_parse_items_json():
    cfg = {"format": "json", "items": "message.items"}
    items = parse_items(CROSSREF, cfg)
    assert len(items) == 2
    assert build_link(items[0], {"link": "URL"}) == "https://doi.org/10.1/a"


def test_parse_items_xml():
    """XML 条目是字符串块，字段要用标签名再抽一次（dig 对字符串无效）。"""
    cfg = {"format": "xml", "items": "entry", "title": "title", "link": "id"}
    items = parse_items(ARXIV_XML, cfg)
    assert len(items) == 2
    assert build_link(items[0], cfg) == "http://arxiv.org/abs/1234.5678v1"
    assert build_title(items[0], cfg) == "Some Paper"


def test_parse_items_bad_json_returns_empty():
    assert parse_items("not json", {"format": "json", "items": "x"}) == []


def test_build_link_template():
    item = {"id": "92164e01-0c3f-43b2"}
    url = build_link(item, {"link_template": "https://mangadex.org/title/{id}"})
    assert url == "https://mangadex.org/title/92164e01-0c3f-43b2"


def test_build_link_template_missing_field_returns_none():
    assert build_link({}, {"link_template": "https://x/{id}"}) is None


def test_build_link_rejects_non_http():
    assert build_link({"link": "ftp://x"}, {"link": "link"}) is None
    assert build_link({"link": "10.1234/abc"}, {"link": "link"}) is None


# ---------------------------------------------------------------- 垂直路由
def _adapter() -> ApiSourcesAdapter:
    return ApiSourcesAdapter({"apis": [
        {"name": "arxiv", "vertical": "academic", "url": "https://arxiv.test/?q={q}",
         "format": "xml", "items": "entry", "title": "title", "link": "id"},
        {"name": "mangadex", "vertical": "comic,anime", "url": "https://mangadex.test/?q={q}",
         "format": "json", "items": "data", "title": "attributes.title.en",
         "link_template": "https://mangadex.org/title/{id}"},
        {"name": "crossref", "vertical": "academic", "url": "https://crossref.test/?q={q}",
         "format": "json", "items": "message.items", "title": "title", "link": "URL"},
    ]})


def test_select_apis_routes_by_vertical():
    a = _adapter()
    assert {x["name"] for x in a.select_apis("quantum physics")} == {"arxiv", "crossref"}
    assert {x["name"] for x in a.select_apis("漫画 海贼王")} == {"mangadex"}
    # 认不出领域时一个都不打，避免无谓请求
    assert a.select_apis("zzzzz") == []


def test_select_apis_supports_multi_vertical():
    a = _adapter()
    assert "mangadex" in {x["name"] for x in a.select_apis("新番 anime")}


# ---------------------------------------------------------------- 端到端（mock）
def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "arxiv" in url:
        return httpx.Response(200, text=ARXIV_XML)
    if "crossref" in url:
        return httpx.Response(200, text=CROSSREF)
    if "mangadex" in url:
        return httpx.Response(200, json={"data": [
            {"id": "abc-123", "attributes": {"title": {"en": "Dungeon Meshi"}}}]})
    return httpx.Response(404)


async def test_search_aggregates_multiple_apis():
    a = _adapter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        hits = await a.search("machine learning", client)
    urls = {h.url for h in hits}
    assert "http://arxiv.org/abs/1234.5678v1" in urls
    assert "https://doi.org/10.1/a" in urls
    assert all(h.kind == "api" for h in hits)
    assert all(h.source.startswith("api:") for h in hits)
    assert all(h.title for h in hits)


async def test_search_returns_nothing_for_unrouted_query():
    a = _adapter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await a.search("zzzzz", client) == []


async def test_single_api_failure_is_isolated():
    def flaky(request: httpx.Request) -> httpx.Response:
        if "arxiv" in str(request.url):
            raise httpx.ConnectError("boom")
        return handler(request)

    a = _adapter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
        hits = await a.search("machine learning", client)
    assert hits, "一个 API 挂了不能影响另一个"
    assert all("arxiv" not in h.source for h in hits)


def test_shipped_api_config_is_valid():
    from pansearch.adapters.apisources import _load_apis

    apis = _load_apis()
    assert apis, "config/apis.yaml 里的 API 都不可用？"
    for api in apis:
        assert api.get("url") and "{q}" in api["url"], api.get("name")
        assert api.get("items"), api.get("name")
        assert api.get("link") or api.get("link_template"), api.get("name")
        assert api.get("vertical"), api.get("name")


def test_direct_pan_type_is_registered():
    """API 直链要被识别成一等资源类型，否则会被 build_resources 丢掉。"""
    from pansearch.models import PanType
    from pansearch.normalize import detect_pan_type

    assert detect_pan_type("http://arxiv.org/abs/1234.5678") is PanType.DIRECT
    assert detect_pan_type("https://doi.org/10.1/abc") is PanType.DIRECT
    assert detect_pan_type("https://mangadex.org/title/abc") is PanType.DIRECT
