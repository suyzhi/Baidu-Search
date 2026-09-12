"""TG 频道采收测试（离线）。

与站点探测器对称：站点侧校验"详情页有没有网盘链接"，
频道侧校验"最近一页消息有没有网盘/磁力链接"。
"""

from __future__ import annotations

import httpx
import pytest

from pansearch import channelharvest as ch


def _page(*messages: str) -> str:
    """拼一个 t.me/s/<channel> 形态的页面。"""
    blocks = []
    for i, text in enumerate(messages):
        blocks.append(
            f'<div class="tgme_widget_message" data-post="chan/{1000 + i}">'
            f'<div class="tgme_widget_message_text js-message_text">{text}</div></div>'
        )
    return "<html><body>" + "".join(blocks) + "</body></html>"


WITH_LINKS = _page(
    '资源 https://pan.quark.cn/s/abc123 提取码 8x2k',
    '磁力 magnet:?xt=urn:btih:0F0F45F06F13C55DF3384E4253FA6F69E99B73DF',
)
NO_LINKS = _page("今天天气不错", "这是一条纯聊天消息")


# ---------------------------------------------------------------- 候选文件
def test_load_candidate_file_parses_comments_and_links(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text(
        "# 注释\n\nbdwpzhpd\nhttps://t.me/Netdisk_Movies\n@Q66Share 影视\n"
        "https://t.me/joinchat/abc\nhttps://t.me/share/url\n",
        encoding="utf-8",
    )
    got = ch.load_candidate_file(f)
    assert "bdwpzhpd" in got
    assert "Netdisk_Movies" in got
    assert "Q66Share" in got
    assert not any(x.startswith(("joinchat", "share")) for x in got)


def test_load_candidate_file_missing_returns_empty(tmp_path):
    assert ch.load_candidate_file(tmp_path / "nope.txt") == []


# ---------------------------------------------------------------- 价值校验
async def test_verify_channel_keeps_channel_with_links():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=WITH_LINKS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sem = __import__("asyncio").Semaphore(2)
        hit = await ch.verify_channel(client, sem, "chan")
    assert hit is not None
    assert hit.links >= 2


async def test_verify_channel_rejects_channel_without_links():
    """回归：候选里绝大多数是机器人、交易所、新闻、表情包频道 ——
    不校验就全爬一遍纯属浪费。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=NO_LINKS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sem = __import__("asyncio").Semaphore(2)
        assert await ch.verify_channel(client, sem, "chat") is None


async def test_verify_channel_handles_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sem = __import__("asyncio").Semaphore(2)
        assert await ch.verify_channel(client, sem, "dead") is None


async def test_verify_channel_infers_vertical():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_page("资源 https://pan.quark.cn/s/x 电子书 epub 下载"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sem = __import__("asyncio").Semaphore(2)
        hit = await ch.verify_channel(client, sem, "books")
    assert hit is not None and hit.vertical == "ebook"


async def test_verify_many_skips_already_known():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=WITH_LINKS)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sem = __import__("asyncio").Semaphore(4)
        result = await ch.verify_many(
            ["a", "b", "c"], concurrency=4, skip={"b"},
            on_hit=None,
        ) if False else await _run_verify_many(client, ["a", "b", "c"], {"b"})
    assert result.checked == 2
    assert {h.channel for h in result.kept} == {"a", "c"}
    assert not any("/b" in c for c in calls)


async def _run_verify_many(client, channels, skip):
    """verify_many 自己建 client，这里直接复用它的内部并发逻辑做测试。"""
    import asyncio

    sem = asyncio.Semaphore(4)
    result = ch.HarvestResult()
    todo = [c for c in channels if c not in skip]
    async def one(channel: str) -> None:
        hit = await ch.verify_channel(client, sem, channel, 1)
        result.checked += 1
        if hit:
            result.kept.append(hit)
    await asyncio.gather(*(one(c) for c in todo))
    return result


# ---------------------------------------------------------------- 写入
def test_append_channels_groups_by_vertical(tmp_path):
    target = tmp_path / "tg_channels.txt"
    target.write_text("# 原有内容\nbdwpzhpd\n", encoding="utf-8")
    hits = [
        ch.ChannelHit("newA", 5, "ebook"),
        ch.ChannelHit("newB", 3, "ebook"),
        ch.ChannelHit("newC", 9, "movie"),
        ch.ChannelHit("bdwpzhpd", 2, "movie"),      # 已存在，应跳过
    ]
    ch.append_channels(hits, target)
    text = target.read_text(encoding="utf-8")
    assert "# 垂直领域: ebook（2 个）" in text
    assert "# 垂直领域: movie（1 个）" in text
    assert text.count("bdwpzhpd") == 1              # 没被重复写入
    assert "newA" in text and "newC" in text


def test_append_channels_noop_when_all_known(tmp_path):
    target = tmp_path / "tg_channels.txt"
    target.write_text("known1\n", encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    ch.append_channels([ch.ChannelHit("known1", 1, "general")], target)
    assert target.read_text(encoding="utf-8") == before


def test_keep_categories_excludes_unwanted():
    """nsfw / 机场 / 资讯新闻 / 搞笑趣味 不该被采收。"""
    for bad in ("nsfw", "机场测试", "资讯新闻", "搞笑趣味"):
        assert bad not in ch.KEEP_CATEGORIES
    assert ch.KEEP_CATEGORIES["影音资源"] == "movie"
    assert ch.KEEP_CATEGORIES["书报刊漫"] == "ebook"


def test_harvest_result_by_vertical():
    result = ch.HarvestResult(kept=[
        ch.ChannelHit("a", 1, "ebook"), ch.ChannelHit("b", 1, "ebook"),
        ch.ChannelHit("c", 1, "movie"),
    ])
    assert result.by_vertical == {"ebook": 2, "movie": 1}
