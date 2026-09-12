"""链接 / 提取码抽取测试（含两个已修复的真实 bug 的回归测试）。"""

from __future__ import annotations

from pansearch.extract import excerpt, extract_from_text, html_to_text, page_title


def _hits(text: str):
    return extract_from_text(text, source="t", kind="websearch")


def test_basic_link_and_pwd():
    hits = _hits("资源：三体 链接 https://pan.baidu.com/s/1etZdVXAv3tJBuk42BpmujA 提取码: hvdw")
    assert len(hits) == 1
    assert hits[0].url == "https://pan.baidu.com/s/1etZdVXAv3tJBuk42BpmujA"
    assert hits[0].pwd == "hvdw"


def test_pwd_left_of_link():
    hits = _hits("提取码 a1b2 链接 https://pan.baidu.com/s/1QqWwEeRrTtYy")
    assert hits[0].pwd == "a1b2"


def test_pwd_does_not_leak_across_links():
    """回归：第 1 个链接的提取码绝不能串到第 2 个链接上。"""
    text = (
        "https://pan.baidu.com/s/1AAAAAAAAAAAA 提取码: aaaa "
        "另一个 https://pan.baidu.com/s/1BBBBBBBBBBBB"
    )
    hits = {h.url.split("/")[-1]: h.pwd for h in _hits(text)}
    assert hits["1AAAAAAAAAAAA"] == "aaaa"
    assert hits["1BBBBBBBBBBBB"] is None


def test_fullwidth_paren_not_swallowed_into_url():
    """回归：全角括号不能被吃进 URL。"""
    hits = _hits("https://pan.quark.cn/s/251cd20497e6（密码 8x2k）")
    assert hits[0].url == "https://pan.quark.cn/s/251cd20497e6"
    assert hits[0].pwd == "8x2k"


def test_pwd_in_url_query():
    hits = _hits("看看 https://pan.baidu.com/s/1CCCCCCCCCCC?pwd=zz99 吧")
    assert hits[0].pwd == "zz99"


def test_multiple_links_each_get_own_pwd():
    text = "https://pan.baidu.com/s/1BBBBBBBBBBB?pwd=1111 和 https://pan.baidu.com/s/1CCCCCCCCCCC?pwd=2222"
    hits = _hits(text)
    assert len(hits) == 2
    assert [h.pwd for h in hits] == ["1111", "2222"]


def test_skips_unparseable_baidu_link():
    # share/link?shareid= 无法解析出 surl，无法验活也无法去重 -> 丢弃
    assert _hits("https://pan.baidu.com/share/link?shareid=123&uk=456") == []


def test_url_stops_at_cjk_text():
    """回归：URL 后面的中文不能被吃进链接。

    实测抓到过 https://drive.uc.cn/s/0b0352b237034我用夸克 这种坏链接。
    """
    hits = _hits("链接：https://drive.uc.cn/s/0b0352b237034我用夸克网盘分享了")
    assert hits[0].url == "https://drive.uc.cn/s/0b0352b237034"


def test_url_with_cjk_punctuation_suffix():
    hits = _hits("下载：https://pan.quark.cn/s/251cd20497e6。")
    assert hits[0].url == "https://pan.quark.cn/s/251cd20497e6"


def test_pwd_hint_fallback():
    hits = extract_from_text(
        "https://pan.baidu.com/s/1DDDDDDDDDDD", source="s", kind="pansou", pwd_hint="kkkk"
    )
    assert hits[0].pwd == "kkkk"


def test_html_to_text_and_title():
    html = "<html><head><title>三体 资源帖</title></head><body><p>链接<br>https://pan.baidu.com/s/1EEEEEEEEEE</p></body></html>"
    assert page_title(html) == "三体 资源帖"
    hits = extract_from_text(html_to_text(html), source="s", kind="t")
    assert len(hits) == 1


# ---------------------------------------------------------------- 展示用片段
LONG = (
    "🗄 Re：从零开始的异世界生活 第四季(2026） 1080p CR S04E01 - E16 内封简繁 Hiv "
    "描述：本剧改编自长月达平创作的同名轻小说 链接：https://pan.quark.cn/s/xxx "
    "另外本频道也收录 沙丘 2部 4K HDR 中字外挂字幕"
)


def test_excerpt_centers_on_subject_term():
    """回归：标题取消息前 300 字时，匹配词可能在后段，看起来像不相关。

    实测「沙丘 2」排第一的标题显示成「Re：从零开始的异世界生活…」。
    """
    out = excerpt(LONG, "沙丘 2", 80)
    assert "沙丘 2部" in out
    assert out.startswith("…"), "片段来自中段时应标出省略"


def test_excerpt_prefers_subject_over_earlier_weak_term():
    """回归：不能取"最早出现"的词 —— 「沙丘 2」的 "2" 会在 "2026" 里先命中。"""
    out = excerpt(LONG, "沙丘 2", 80)
    assert "Re：从零开始" not in out, "不该被 2026 里的 2 拉回开头"


def test_excerpt_uses_earliest_term_when_subject_absent():
    out = excerpt(LONG, "1080p 不存在", 80)
    assert "1080p" in out


def test_excerpt_falls_back_to_head_when_no_term_matches():
    out = excerpt(LONG, "完全不相关", 40)
    assert out.startswith("🗄 Re")
    assert out.endswith("…")


def test_excerpt_short_text_untouched():
    assert excerpt("沙丘 2部 4K", "沙丘", 80) == "沙丘 2部 4K"


def test_excerpt_handles_empty_inputs():
    assert excerpt(None, "沙丘") == ""
    assert excerpt("", "沙丘") == ""
    assert excerpt("x", None) == "x"


# ---------------------------------------------------------------- 磁力抽取
def test_extracts_magnet_links():
    """回归：URL_RE 只匹配 http(s)，磁力链接一个都抽不出来。

    VST 音源 / 软件 / 影视的分享大量走磁力，之前所有磁力都只来自 PanSou 的 JSON。
    """
    t = "资源 magnet:?xt=urn:btih:0F0F45F06F13C55DF3384E4253FA6F69E99B73DF&dn=serum 完"
    hits = _hits(t)
    assert len(hits) == 1
    assert hits[0].url.startswith("magnet:?xt=urn:btih:0F0F45F06F13C55DF3384E4253FA6F69E99B73DF")


def test_magnet_and_netdisk_in_same_text():
    t = ("磁力：magnet:?xt=urn:btih:0F0F45F06F13C55DF3384E4253FA6F69E99B73DF "
         "网盘：https://pan.quark.cn/s/251cd20497e6 提取码 8x2k")
    got = {h.url.split(":")[0]: h.pwd for h in _hits(t)}
    assert got["magnet"] is None
    assert got["https"] == "8x2k"


def test_bare_infohash_is_not_a_link():
    """裸的 40 位 hash 不算链接，别误收。"""
    assert _hits("哈希 0F0F45F06F13C55DF3384E4253FA6F69E99B73DF 单独出现") == []
