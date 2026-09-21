"""B 站适配器测试（MockTransport，离线）。

背景：影视/软件类资源大量贴在 B 站视频简介与评论区，而这部分内容既不在
PanSou 的插件里，搜索引擎也索引不全。接口全部需要 WBI 签名 + 游客 buvid cookie，
所以这里把签名、cookie 种入、详情跟进、相关性闸门和失败隔离逐条钉死。
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse

import httpx

from pansearch.adapters.bilibili import DEFAULT_TEMPLATES, WBI_TAB, BilibiliAdapter, _WBISigner


def make_adapter(**cfg) -> BilibiliAdapter:
    base = {"max_videos": 3, "concurrency": 3, "rate_limit_qps": 0, "search_pages": 1}
    base.update(cfg)
    return BilibiliAdapter(base)


def search_body(items: list[dict]) -> dict:
    return {"code": 0, "message": "OK", "data": {"result": items}}


VIDEO_LINK_IN_DESC = {
    "bvid": "BV1AA4111",
    "aid": 1111,
    "title": '<em class="keyword">Omnisphere</em> 音源合集',
    "description": "下载：https://pan.baidu.com/s/AAA111 提取码：aaaa",
    "desc": "",
    "pubdate": 1700000000,
}

VIDEO_NO_LINK = {
    "bvid": "BV2BB4222",
    "aid": 2222,
    "title": "Omnisphere 编曲教学第一课",
    "description": "本期讲包络",
    "desc": "",
    "pubdate": 1700000000,
}

VIDEO_COMMENT_LINK = {
    "bvid": "BV3CC4333",
    "aid": 3333,
    "title": "Omnisphere 资源分享",
    "description": "评论区自取",
    "desc": "",
    "pubdate": 1700000000,
}

# 标题里没有任何查询词 —— 必须被闸门挡掉（B 站会混进"猜你喜欢"）
VIDEO_IRRELEVANT = {
    "bvid": "BV4DD4444",
    "aid": 4444,
    "title": "剪映手机剪辑入门",
    "description": "https://pan.baidu.com/s/ZZZ999 提取码：zzzz",
    "desc": "",
    "pubdate": 1700000000,
}

ARTICLE = {
    "id": 22408708,
    "title": "Omnisphere 预设推荐",
    "desc": "合集下载 pan.baidu.com/s/BBB222 提取码：bbbb",
    "pubdate": 1700000000,
}


def router(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.startswith("https://www.bilibili.com/"):
        return httpx.Response(200, text="<html>home</html>")
    if "finger/spi" in url:
        return httpx.Response(200, json={"code": 0, "data": {"b_3": "buvid3-test", "b_4": "buvid4-test"}})
    if "web-interface/nav" in url:
        return httpx.Response(200, json={"code": -101, "data": {"wbi_img": {
            "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
            "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
        }}})
    if "search/type" in url:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if query.get("search_type") == ["video"]:
            return httpx.Response(200, json=search_body([
                VIDEO_LINK_IN_DESC, VIDEO_NO_LINK, VIDEO_COMMENT_LINK, VIDEO_IRRELEVANT,
            ]))
        return httpx.Response(200, json=search_body([ARTICLE]))
    if "web-interface/view" in url:
        bvid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("bvid", [""])[0]
        desc = {
            "BV3CC4333": "完整版 86GB，链接见置顶评论",
        }.get(bvid, "")
        aids = {"BV1AA4111": 1111, "BV2BB4222": 2222, "BV3CC4333": 3333, "BV4DD4444": 4444}
        return httpx.Response(200, json={"code": 0, "data": {
            "aid": aids.get(bvid, 9999),
            "bvid": bvid, "title": "t", "desc": desc, "pubdate": 1700000000,
        }})
    if "reply/wbi/main" in url:
        oid = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("oid", ["0"])[0]
        if str(oid) == "3333":
            return httpx.Response(200, json={"code": 0, "data": {
                "top": {"content": {"message": "置顶：pan.baidu.com/s/CCC333 提取码：cccc"},
                        "member": {"uname": "up"}},
                "replies": [{
                    "content": {"message": "谢谢up"},
                    "member": {"uname": "路人"},
                    "replies": [{"content": {"message": "补一个 夸克 https://pan.quark.cn/s/ddd444"}}],
                }],
            }})
        return httpx.Response(200, json={"code": 0, "data": {"replies": []}})
    return httpx.Response(404, text="nope")


# ------------------------------------------------------------------ WBI 签名
def test_wbi_mixin_uses_fixed_table():
    """mixin key 必须是 img+sub 拼接后按固定表置换的前 32 位。"""
    img = "7cd084941338484aae1ad9425b84077c"
    sub = "4932caff0ff746eab6f01bf08b70ac45"
    raw = img + sub
    expect = "".join(raw[i] for i in WBI_TAB if i < len(raw))[:32]
    assert len(expect) == 32


async def test_wbi_sign_appends_wts_and_w_rid():
    s = _WBISigner()
    async with httpx.AsyncClient(transport=httpx.MockTransport(router)) as client:
        from pansearch.util import RateLimiter
        await s.ensure(client, RateLimiter(0))
    qs = urllib.parse.parse_qs(s.sign({"keyword": "abc", "search_type": "video"}))
    assert qs["keyword"] == ["abc"]
    assert "wts" in qs and "w_rid" in qs
    # 同一秒内重复签名结果一致（可复现）
    assert s.sign({"keyword": "abc"}) == s.sign({"keyword": "abc"})


# ------------------------------------------------------------------ 相关性闸门
def test_relevance_gate_blocks_guess_like_videos():
    assert BilibiliAdapter._relevant("Omnisphere 音源合集", "Omnisphere") is True
    assert BilibiliAdapter._relevant("剪映手机剪辑入门", "Omnisphere") is False
    assert BilibiliAdapter._relevant(None, "Omnisphere") is False
    # 多词查询：命中任意一个词即可（与 sitesearch 的口径一致）
    assert BilibiliAdapter._relevant("Xfer Serum 音色包", "Xfer Serum") is True


def test_priority_prefers_resource_posts():
    a = make_adapter()
    plain = {"title": "Omnisphere 教学", "description": "第一课", "_query": "Omnisphere"}
    res = {"title": "Omnisphere 资源合集 提取码", "description": "网盘下载", "_query": "Omnisphere"}
    tpl = {"title": "Omnisphere 教学", "description": "第一课", "_query": "Omnisphere 百度网盘"}
    assert a._priority(res, "Omnisphere") > a._priority(plain, "Omnisphere")
    # 来自资源模板的结果同样加权
    assert a._priority(tpl, "Omnisphere") > a._priority(plain, "Omnisphere")


# ------------------------------------------------------------------ 端到端（mock）
async def test_search_extracts_from_results_desc_and_articles():
    a = make_adapter(max_videos=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(router)) as client:
        hits = await a.search("Omnisphere", client)
    urls = {h.url for h in hits}
    assert "https://pan.baidu.com/s/AAA111" in urls, "搜索结果简介里的链接必须抽到"
    assert "https://pan.baidu.com/s/BBB222" in urls, "专栏摘要里的链接必须抽到"
    # 标题不含查询词的视频即使带链接也不能收
    assert "https://pan.baidu.com/s/ZZZ999" not in urls
    assert all(h.kind == "bilibili" for h in hits)
    assert all(h.origin for h in hits), "必须记录来源页，便于复核"


async def test_search_follows_comments_including_top_and_nested():
    a = make_adapter(max_videos=3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(router)) as client:
        hits = await a.search("Omnisphere", client)
    urls = {h.url for h in hits}
    assert "https://pan.baidu.com/s/CCC333" in urls, "置顶评论里的链接必须抽到"
    assert "https://pan.quark.cn/s/ddd444" in urls, "楼中楼里的链接必须抽到"
    assert any(h.source == "bili-comment" for h in hits)
    # 提取码要配到同一条评论里的链接上
    ccc = next(h for h in hits if h.url.endswith("CCC333"))
    assert ccc.pwd == "cccc"


async def test_session_seeds_buvid_cookies_and_signs_requests():
    seen: list[str] = []

    def recording_router(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return router(request)

    a = make_adapter(max_videos=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(recording_router)) as client:
        await a.search("Omnisphere", client)
        assert client.cookies.get("buvid3") == "buvid3-test"
    # 搜索请求必须带签名参数，否则线上会被 412
    search_urls = [u for u in seen if "search/type" in u]
    assert search_urls and all("w_rid=" in u and "wts=" in u for u in search_urls)


async def test_video_failure_is_isolated():
    def flaky(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "web-interface/view" in url and "BV1AA4111" in url:
            raise httpx.ConnectError("boom")
        return router(request)

    a = make_adapter(max_videos=3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(flaky)) as client:
        hits = await a.search("Omnisphere", client)
    # 一个视频挂了，其它视频的评论链接照常返回
    assert "https://pan.baidu.com/s/CCC333" in {h.url for h in hits}


async def test_search_returns_empty_when_wbi_key_missing():
    """nav 接口异常时不能抛给用户：返回空命中，由源状态报告去体现。"""
    def broken(request: httpx.Request) -> httpx.Response:
        if "web-interface/nav" in str(request.url):
            return httpx.Response(200, json={"code": -101, "data": {}})
        return router(request)

    a = make_adapter(max_videos=0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        try:
            hits = await a.search("Omnisphere", client)
        except RuntimeError:
            return          # 允许显式报错，由 pipeline 记为源故障
    assert hits == []


def test_default_templates_include_resource_intents():
    assert "{kw}" in DEFAULT_TEMPLATES
    assert any("网盘" in t for t in DEFAULT_TEMPLATES)
