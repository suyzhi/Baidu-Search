"""BT / 磁力站适配器测试（MockTransport，离线）。

背景：BT 站的搜索页**就是**结果页，magnet 直接写在行里；标题要么在 magnet 的
dn= 参数里，要么在同一行的标题链接里。这两条路都踩过坑（&amp; 把链接截断、
方括号被当成正文符号），所以逐条钉死。
"""

from __future__ import annotations

import httpx

from pansearch.adapters.btsearch import (
    BtSearchAdapter,
    _magnet_title,
    prepare_html,
    row_titles,
)

H1 = "0F0F45F06F13C55DF3384E4253FA6F69E99B73DF"
H2 = "7A008EE6FAE92282EEB19470ED45C3236A46B773"

# nyaa 风格：dn 里有标题，且带 &amp; 与裸方括号
NYAA_PAGE = f"""
<html><body><table>
<tr><td><a href="/view/1">[SubsPlease] Three-Body 01 (1080p)</a></td>
    <td><a href="magnet:?xt=urn:btih:{H1}&amp;dn=%5BSubsPlease%5D+Three-Body+01+(1080p)&amp;tr=udp">M</a></td></tr>
</table></body></html>
"""

# dmhy 风格：dn 为空，标题只在行内 <a> 文本里
DMHY_PAGE = f"""
<html><body><table>
<tr><td><a href="/topics/view/1">[GM-Team][国漫][三体][2022][01-15 Fin][AVC]</a></td>
    <td><a href="magnet:?xt=urn:btih:{H2}&dn=&tr=udp%3A%2F%2Ftracker">M</a></td></tr>
</table></body></html>
"""


def make_adapter(**cfg) -> BtSearchAdapter:
    base = {"concurrency": 2, "rate_limit_qps": 0, "retries": 0, "page_concurrency": 2}
    base.update(cfg)
    return BtSearchAdapter(base)


# ---------------------------------------------------------------- 预处理
def test_prepare_html_unescapes_amp_and_encodes_brackets():
    """回归：&amp; 里的分号会截断 magnet，裸方括号也会 —— 75 条 magnet 因此全无标题。"""
    prepared = prepare_html(NYAA_PAGE)
    assert "&amp;dn=" not in prepared
    assert "&dn=%5B" in prepared


def test_magnet_title_reads_dn():
    url = f"magnet:?xt=urn:btih:{H1}&dn=%5BSubsPlease%5D+Three-Body+01+%281080p%29&tr=x"
    assert _magnet_title(url) == "[SubsPlease] Three-Body 01 (1080p)"


def test_magnet_title_returns_none_without_dn():
    assert _magnet_title(f"magnet:?xt=urn:btih:{H2}&dn=&tr=x") is None


def test_row_titles_map_magnet_to_row_anchor_text():
    """dmhy 的 magnet 里 dn= 是空的，标题只能从同一行取。"""
    titles = row_titles(prepare_html(DMHY_PAGE))
    key = next(iter(titles))
    assert key.startswith("magnet:")
    assert H2.lower() in key.lower()
    assert titles[key].startswith("[GM-Team]")


# ---------------------------------------------------------------- 垂直路由
def test_select_sites_follows_verticals():
    a = make_adapter()
    # 音源（audio-tool）只打 nyaa；动画两个站都打；设计类查询一个都不打（避免白跑）
    assert [s["name"] for s in a.select_sites("Omnisphere")] == ["nyaa"]
    # "三体" 不含任何垂直领域特征词 → 走"全都打"的兜底分支，所以是全部站点；
    # 数量由 max_sites 兜底，不会因为加站而无限变慢。
    expected = {s["name"] for s in a.sites}
    assert {s["name"] for s in a.select_sites("三体")} == expected
    assert a.select_sites("PPT模板 素材") == []


# ---------------------------------------------------------------- 端到端（mock）
def router(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "nyaa" in url:
        return httpx.Response(200, text=NYAA_PAGE)
    if "dmhy" in url:
        return httpx.Response(200, text=DMHY_PAGE)
    return httpx.Response(404)


async def test_search_extracts_magnets_with_titles():
    a = make_adapter(sites=[
        {"name": "nyaa", "search": "https://nyaa.si/?q={q}", "verticals": ["anime"]},
        {"name": "dmhy", "search": "https://share.dmhy.org/list?keyword={q}", "verticals": ["anime"]},
    ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(router)) as client:
        hits = await a.search("三体", client)

    by_hash = {h.url.split("btih:")[1][:4]: h for h in hits}
    assert set(by_hash) == {H1[:4], H2[:4]}
    # nyaa 的标题来自 dn=，dmhy 的来自行内文本
    assert "SubsPlease" in (by_hash[H1[:4]].title or "")
    assert (by_hash[H2[:4]].title or "").startswith("[GM-Team]")
    assert all(h.kind == "bt" for h in hits)
    assert all(h.origin for h in hits), "必须记录来源页，便于复核"
    assert all(h.source.startswith("bt:") for h in hits)


async def test_site_failure_is_isolated_and_retried():
    calls = {"bad": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "bad" in url:
            calls["bad"] += 1
            return httpx.Response(504, text="gateway timeout")
        return router(request)

    a = make_adapter(retries=1, sites=[
        {"name": "bad", "search": "https://bad.example/?q={q}", "verticals": ["anime"]},
        {"name": "nyaa", "search": "https://nyaa.si/?q={q}", "verticals": ["anime"]},
    ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
        hits = await a.search("三体", client)

    assert calls["bad"] == 2, "5xx 要重试一次再放弃"
    assert any("nyaa" in h.source for h in hits), "一个站挂了不能影响另一个站"


async def test_no_site_selected_returns_empty_without_requests():
    seen: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="")

    a = make_adapter(sites=[
        {"name": "nyaa", "search": "https://nyaa.si/?q={q}", "verticals": ["anime"]},
    ])
    async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as client:
        hits = await a.search("PPT模板", client)     # design 领域，不该打 BT 站
    assert hits == [] and seen == []
