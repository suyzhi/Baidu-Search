"""成人源标记与 --sfw 过滤（离线）。

背景：javdb（PanSou 插件）与 sukebei（nyaa 成人分区）默认**开启**，
需要时用 --sfw 一键过滤。过滤按"来源标签"而不是标题关键词 ——
关键词猜法既会漏（缩写/外语）也会误伤（"写真集"这类正常资源）。
"""

from __future__ import annotations

import yaml

from pansearch.config import CONFIG_DIR
from pansearch.models import RawHit
from pansearch.pipeline import adult_sources, filter_adult_hits, is_adult_source


def test_shipped_config_marks_adult_sources():
    cfg = yaml.safe_load((CONFIG_DIR / "sources.yaml").read_text(encoding="utf-8"))
    markers = cfg.get("adult_sources") or []
    assert "plugin:javdb" in markers, "javdb 插件必须被标记为成人源"
    assert "bt:sukebei" in markers, "sukebei 必须被标记为成人源"


def test_is_adult_source_matches_labels_containing_marker():
    """判据是"来源标签里含标记"（适配器总会带上 kind 前缀，如 bt:sukebei）。"""
    assert is_adult_source("plugin:javdb")
    assert is_adult_source("bt:sukebei")
    assert is_adult_source("bt:sukebei#mirror"), "带后缀的标签也要认"
    assert not is_adult_source("sukebei"), "裸站名不带前缀 —— 没有适配器会这么产出"
    assert not is_adult_source("plugin:yunso")
    assert not is_adult_source("bt:nyaa")
    assert not is_adult_source(None)


def test_filter_adult_hits_keeps_other_sources():
    hits = [
        RawHit(source="plugin:javdb", kind="pansou", url="magnet:?xt=urn:btih:AA"),
        RawHit(source="bt:sukebei", kind="bt", url="magnet:?xt=urn:btih:BB"),
        RawHit(source="plugin:yunso", kind="pansou", url="https://pan.baidu.com/s/1abc"),
        RawHit(source="bt:nyaa", kind="bt", url="magnet:?xt=urn:btih:CC"),
    ]
    kept, pruned = filter_adult_hits(hits)
    assert pruned == 2
    assert [h.source for h in kept] == ["plugin:yunso", "bt:nyaa"]


def test_adult_markers_exist():
    assert adult_sources(), "config 里必须有成人源标记，否则 --sfw 是空操作"
