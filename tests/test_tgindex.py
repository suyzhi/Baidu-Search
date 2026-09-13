"""Telegram 频道索引测试（离线，用真实页面片段做 fixture）。"""

from __future__ import annotations

from pansearch.tgindex import TgIndex, TgMessage, load_channels, parse_channel_page

# 取自 t.me/s/ 真实页面的结构片段（保留关键 class 与 data-post）
PAGE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message text_not_supported_wrap js-widget_message"
       data-post="Netdisk_Movies/5094" data-view="eyJ4IjoxfQ==">
    <div class="tgme_widget_message_bubble">
      <div class="tgme_widget_message_text js-message_text" dir="auto">
        资源名称：沙丘：预言 (2024) 4K HDR 内封简中<br/>
        链接：https://pan.baidu.com/s/1etZdVXAv3tJBuk42BpmujA 提取码: hvdw
      </div>
      <div class="tgme_widget_message_footer">
        <time datetime="2026-09-11T10:00:00+00:00">Sep 11</time>
      </div>
    </div>
  </div>
</div>
<div class="tgme_widget_message_wrap js-widget_message_wrap">
  <div class="tgme_widget_message text_not_supported_wrap js-widget_message"
       data-post="Netdisk_Movies/5093">
    <div class="tgme_widget_message_bubble">
      <div class="tgme_widget_message_text js-message_text" dir="auto">
        三体 全集 https://pan.quark.cn/s/251cd20497e6 和
        https://115cdn.com/s/swwfcbh3wrb 密码 p9f2
      </div>
      <time datetime="2026-09-10T08:30:00+00:00">Sep 10</time>
    </div>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <div class="tgme_widget_message" data-post="Netdisk_Movies/5092">
    <div class="tgme_widget_message_text js-message_text">这条没有任何网盘链接，只是闲聊</div>
  </div>
</div>
"""


def test_parse_channel_page_extracts_messages():
    msgs = parse_channel_page(PAGE, "Netdisk_Movies")
    assert [m.msg_id for m in msgs] == [5094, 5093, 5092]
    assert all(m.channel == "Netdisk_Movies" for m in msgs)


def test_parse_extracts_links_and_passwords():
    msgs = {m.msg_id: m for m in parse_channel_page(PAGE, "ch")}
    first = msgs[5094]
    assert len(first.links) == 1
    assert first.links[0]["url"] == "https://pan.baidu.com/s/1etZdVXAv3tJBuk42BpmujA"
    assert first.links[0]["pwd"] == "hvdw"
    assert first.posted_at == "2026-09-11T10:00:00+00:00"

    second = msgs[5093]
    urls = {l["url"] for l in second.links}
    assert urls == {"https://pan.quark.cn/s/251cd20497e6", "https://115cdn.com/s/swwfcbh3wrb"}
    p115 = next(l for l in second.links if "115" in l["url"])
    assert p115["pwd"] == "p9f2"


def test_message_without_links_is_still_indexed():
    msgs = {m.msg_id: m for m in parse_channel_page(PAGE, "ch")}
    assert msgs[5092].links == []
    assert "闲聊" in msgs[5092].text


def test_parse_empty_page():
    assert parse_channel_page("<html><body>no messages</body></html>", "ch") == []


def test_message_channel_uses_requested_name_not_data_post():
    """回归：data-post 的大小写/别名与配置不一致，照抄会把消息存到别的键下。

    实测 data-post 是 `Baidu_Netdisk`，而配置里写的是 `Baidu_netdisk`，
    结果该频道在频道表里显示 0 条消息、深度统计也失真。
    """
    page = '<div class="tgme_widget_message" data-post="Baidu_Netdisk/777">' \
           '<div class="tgme_widget_message_text js-message_text">沙丘 4K</div></div>'
    msgs = parse_channel_page(page, "Baidu_netdisk")
    assert [m.channel for m in msgs] == ["Baidu_netdisk"]


def test_forwarded_message_does_not_hijack_channel_key():
    """转发消息的 data-post 指向原频道，也不能改归属。"""
    page = '<div class="tgme_widget_message" data-post="kfcfoodcourt/1">' \
           '<div class="tgme_widget_message_text js-message_text">三体</div></div>'
    msgs = parse_channel_page(page, "yunpanx")
    assert msgs[0].channel == "yunpanx"


# ---------------------------------------------------------------- 索引
def make_index(tmp_path) -> TgIndex:
    return TgIndex(tmp_path / "tg.sqlite3")


def test_index_upsert_and_search(tmp_path):
    idx = make_index(tmp_path)
    msgs = parse_channel_page(PAGE, "Netdisk_Movies")
    assert idx.upsert(msgs) == 3
    idx.mark_channel("Netdisk_Movies", newest=5094, oldest=5092)

    rows = idx.search("沙丘")
    assert len(rows) == 1
    assert rows[0]["msg_id"] == 5094
    assert rows[0]["links"][0]["pwd"] == "hvdw"
    idx.close()


def test_index_search_multiterm_ranks_by_hit_count(tmp_path):
    idx = make_index(tmp_path)
    idx.upsert([
        TgMessage(channel="a", msg_id=1, posted_at=None, text="沙丘 4K HDR 全集",
                  links=[{"url": "https://pan.baidu.com/s/1AAA", "pwd": None}]),
        TgMessage(channel="b", msg_id=2, posted_at=None, text="沙丘 纪录片",
                  links=[{"url": "https://pan.baidu.com/s/1BBB", "pwd": None}]),
        TgMessage(channel="c", msg_id=3, posted_at=None, text="完全无关",
                  links=[{"url": "https://pan.baidu.com/s/1CCC", "pwd": None}]),
    ])
    rows = idx.search("沙丘 4K")
    assert [r["msg_id"] for r in rows] == [1, 2]      # 命中两词的排前面，无关的不返回
    idx.close()


def test_index_search_subject_term_is_never_crowded_out(tmp_path):
    """回归（核心 bug）：多词查询是 OR 语义，只含限定词的消息会挤满 LIMIT。

    实测「SolidWorks 破解」400 条里含 subject 的只有 **2** 条，
    只含「破解」的有 399 条；按命中词数排序时主题词命中的消息一条都剩不下。
    """
    idx = make_index(tmp_path)
    idx.upsert([
        TgMessage(channel=f"noise{i}", msg_id=i, posted_at=None, text=f"4K HDR 通用内容 {i}",
                  links=[{"url": f"https://pan.quark.cn/s/noise{i:010d}", "pwd": None}])
        for i in range(50)
    ] + [
        TgMessage(channel="real", msg_id=999, posted_at=None, text="沙丘 纪录片",
                  links=[{"url": "https://pan.quark.cn/s/subject0001", "pwd": None}]),
    ])
    rows = idx.search("沙丘 4K HDR", limit=10)
    assert rows[0]["msg_id"] == 999, "主题词命中的消息必须永远排在只含限定词的消息前面"
    idx.close()


def test_is_empty(tmp_path):
    idx = make_index(tmp_path)
    assert idx.is_empty() is True
    idx.upsert([TgMessage(channel="a", msg_id=1, posted_at=None, text="随便")])
    assert idx.is_empty() is False
    idx.close()


def test_index_upsert_is_idempotent(tmp_path):
    idx = make_index(tmp_path)
    msgs = parse_channel_page(PAGE, "ch")
    idx.upsert(msgs)
    idx.upsert(msgs)                                   # 同一批再写一次
    assert idx.stats()["messages"] == 3
    idx.close()


def test_tg_digest_links_get_their_own_title():
    """回归（核心 bug 的更深根因）：合集帖里每个链接只该拿自己那段前文当标题。

    实测一条 21 个链接的合集帖，搜「SolidWorks」时 21 个链接全被判相关
    （「iSkysoft PDF Editor」「AG视频解析」…）—— 就因为整条消息里出现过
    "SolidWorks"。按链接取局部标题后，只有真正那条留下。
    """
    from pansearch.adapters.telegram import TelegramAdapter

    text = ("会声会影软件及教程 https://www.aliyundrive.com/s/AAAA "
            "iSkysoft PDF Editor PDF转WORD工具 https://www.aliyundrive.com/s/BBBB "
            "常用软件（MATLAB、AutoCAD、SolidWorks、CATIA） https://www.aliyundrive.com/s/CCCC "
            "Solidworks视频教程 https://www.aliyundrive.com/s/DDDD")

    assert "PDF Editor" in TelegramAdapter._local_title(
        text, "https://www.aliyundrive.com/s/BBBB", "SolidWorks")
    assert "SolidWorks" in TelegramAdapter._local_title(
        text, "https://www.aliyundrive.com/s/CCCC", "SolidWorks")

    rows = [{
        "channel": "c", "msg_id": 1, "posted_at": None, "text": text,
        "links": [{"url": f"https://www.aliyundrive.com/s/{x}", "pwd": None}
                  for x in ("AAAA", "BBBB", "CCCC", "DDDD")],
    }]
    hits = TelegramAdapter._to_hits(rows, "SolidWorks")
    titles = {h.url.rsplit("/", 1)[-1]: (h.title or "").lower() for h in hits}
    assert "solidworks" in titles["CCCC"]
    assert "solidworks" in titles["DDDD"]
    assert "solidworks" not in titles["BBBB"], "PDF Editor 那条不该带上别人的主题词"


def test_tg_local_title_falls_back_when_link_not_in_text():
    from pansearch.adapters.telegram import TelegramAdapter

    assert TelegramAdapter._local_title("没有任何链接的正文", "https://x/y", "k") is None


def test_index_tracks_oldest_for_deepening(tmp_path):
    idx = make_index(tmp_path)
    idx.upsert(parse_channel_page(PAGE, "ch"))
    idx.mark_channel("ch", newest=5094, oldest=5092)
    assert idx.oldest_id("ch") == 5092

    # 继续向历史翻页：oldest 应该变小，newest 保持
    idx.upsert([TgMessage(channel="ch", msg_id=5000, posted_at=None, text="更早的消息")])
    idx.mark_channel("ch", newest=5000, oldest=5000)
    assert idx.oldest_id("ch") == 5000
    idx.close()


def test_index_stats(tmp_path):
    idx = make_index(tmp_path)
    idx.upsert(parse_channel_page(PAGE, "Netdisk_Movies"))
    idx.mark_channel("Netdisk_Movies", newest=5094, oldest=5092)
    info = idx.stats()
    assert info["messages"] == 3
    assert info["messages_with_links"] == 2
    assert info["channels_indexed"] == 1
    idx.close()


def test_search_empty_index_returns_nothing(tmp_path):
    idx = make_index(tmp_path)
    assert idx.search("沙丘") == []
    assert idx.search("") == []
    idx.close()


def test_normalize_channel_keys_merges_case_mismatch(tmp_path):
    """历史遗留的大小写不一致键要能被归并到配置里的标准名。"""
    idx = make_index(tmp_path)
    idx.upsert([
        TgMessage(channel="Baidu_Netdisk", msg_id=1, posted_at=None,
                  text="沙丘", links=[{"url": "https://pan.baidu.com/s/1AAA", "pwd": None}]),
        TgMessage(channel="dianying4K", msg_id=2, posted_at=None,
                  text="三体", links=[{"url": "https://pan.baidu.com/s/1BBB", "pwd": None}]),
    ])
    idx.mark_channel("Baidu_Netdisk", 1, 1)
    idx.mark_channel("dianying4K", 2, 2)

    moved = idx.normalize_channel_keys(["Baidu_netdisk", "dianying4k", "other"])
    assert moved == 4                                   # 两张表各 2 行

    channels = {r[0] for r in idx.conn.execute("SELECT DISTINCT channel FROM tg_messages")}
    assert channels == {"Baidu_netdisk", "dianying4k"}
    assert idx.oldest_id("Baidu_netdisk") == 1
    assert idx.stats()["messages"] == 2
    idx.close()


def test_normalize_channel_keys_handles_pk_collision(tmp_path):
    """归并时若同一 msg_id 在两个键下都有，不能因主键冲突而失败。"""
    idx = make_index(tmp_path)
    idx.upsert([
        TgMessage(channel="Q_dongman", msg_id=5, posted_at=None, text="旧"),
        TgMessage(channel="Q_dongman".lower() if False else "q_dongman",
                  msg_id=5, posted_at=None, text="新"),
    ])
    idx.normalize_channel_keys(["Q_dongman"])
    rows = idx.conn.execute("SELECT channel, text FROM tg_messages").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Q_dongman"
    idx.close()


# ---------------------------------------------------------------- 频道清单
def test_load_channels_parses_comments_and_blanks(tmp_path):
    f = tmp_path / "ch.txt"
    f.write_text(
        "# 注释行\n\nNetdisk_Movies\n  bdwpzhpd  \n@ucquark\n# 另一个注释\nNetdisk_Movies\n",
        encoding="utf-8",
    )
    assert load_channels(f) == ["Netdisk_Movies", "bdwpzhpd", "ucquark"]


def test_default_channel_list_exists_and_is_big():
    channels = load_channels()
    assert len(channels) >= 80, "内置 TG 频道清单不应少于 80 个"
    assert "Netdisk_Movies" in channels
    assert all(not c.startswith("@") for c in channels)
