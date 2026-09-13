"""查询词标点规范化回归测试（离线）。

用户经常把标题原样粘进来：「三体」、「“三体”」、《三体》全集、三体!。
这些标点若留在检索词里，`LIKE '%"三体"%'` 与各源的子串匹配都会 0 命中 ——
表现为"搜什么都搜不到"。这些用例锁住"标点必须被当分隔符丢掉"。
"""

from __future__ import annotations

import pytest

from pansearch import pipeline
from pansearch.adapters.base import Adapter
from pansearch.dedupe import build_resources
from pansearch.extract import excerpt
from pansearch.models import RawHit
from pansearch.query import analyze, query_terms, split_query, subject_terms
from pansearch.score import _relevance, confidently_irrelevant
from pansearch.tgindex import TgIndex, TgMessage


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("三体", ["三体"]),
        ('"三体"', ["三体"]),
        ("'三体'", ["三体"]),
        ("“三体”", ["三体"]),
        ("「三体」", ["三体"]),
        ("『三体』", ["三体"]),
        ("〈三体〉", ["三体"]),
        ("《三体》", ["三体"]),
        ("《三体》全集", ["三体", "全集"]),
        ("三体!", ["三体"]),
        ("三体?", ["三体"]),
        ("三体。", ["三体"]),
        ("（三体）", ["三体"]),
        ("【三体】", ["三体"]),
        ('"沙丘" 4K', ["沙丘", "4k"]),
        ("\u200b三体\ufeff", ["三体"]),          # 零宽字符 / BOM
        ("  三体  ", ["三体"]),
    ],
)
def test_query_terms_strip_wrapping_punctuation(raw, expected):
    assert query_terms(raw) == expected


def test_query_terms_keep_meaningful_symbols():
    """C++ / C# / node.js 是完整词，不能被切碎。"""
    assert query_terms("C++") == ["c++"]
    assert query_terms("C#") == ["c#"]
    assert query_terms("node.js") == ["node", "js"]


def test_split_query_preserves_case():
    assert split_query("Dune 4K") == ["Dune", "4K"]


def test_relaxed_queries_normalize_punctuation():
    assert pipeline.relaxed_queries('"沙丘" 4K') == ["沙丘"]
    assert pipeline.relaxed_queries("《沙丘》 4K HDR") == ["沙丘"]


def test_alias_queries_normalize_punctuation():
    assert pipeline.alias_queries("《大气合成器》") == ["omnisphere"]
    assert pipeline.alias_queries("「血清」 4K") == ["serum 4k"]


def test_excerpt_locates_term_inside_quoted_query():
    text = "频道介绍 " * 20 + "三体 全集 4K"
    assert "三体" in excerpt(text, '"三体"', 40)


def test_tg_index_finds_quoted_query(tmp_path):
    """核心回归：`pansearch search '"三体"'` 原来 0 条。"""
    index = TgIndex(tmp_path / "tg.sqlite3")
    try:
        index.upsert([
            TgMessage("ch", 1, None, "三体 全集",
                      [{"url": "https://pan.quark.cn/s/abc", "pwd": None}]),
        ])
        for raw in ['"三体"', "「三体」", "《三体》", "三体!"]:
            assert [r["msg_id"] for r in index.search(raw)] == [1], raw
    finally:
        index.close()


class _Recording(Adapter):
    name = "recording"
    kind = "pansou"

    def __init__(self):
        super().__init__({})
        self.calls: list[str] = []

    async def search(self, kw, client):
        self.calls.append(kw)
        return [RawHit(source="s", kind="pansou",
                       url="https://pan.quark.cn/s/recorded0001", title="三体 全集")]


async def test_pipeline_sends_clean_query_but_keeps_original_keyword(monkeypatch):
    adapter = _Recording()
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [adapter])

    out = await pipeline.search('"三体"', do_verify=False, alive_only=False, relax=False)

    assert adapter.calls == ["三体"], "发到各源的检索词必须去掉引号"
    assert out.keyword == '"三体"', "展示仍用用户原词"
    assert out.resources, "去标点后必须能搜到结果"


# ---------------------------------------------------------------- 检索词角色分层
@pytest.mark.parametrize(
    "raw,subjects",
    [
        # 通用动作词不能当主题词 —— BM25 里它们的 IDF≈0
        ("MATLAB 破解", ["matlab"]),
        ("AutoCAD 破解版", ["autocad"]),
        ("AutoCAD破解版", ["autocad"]),
        ("SolidWorks 破解", ["solidworks"]),
        ("Photoshop 激活", ["photoshop"]),
        ("PS教程", ["ps"]),
        ("三体全集", ["三体"]),
        ("沙丘 4K HDR", ["沙丘"]),
        ("机器学习 课程 视频", ["机器学习"]),
        ("Notepad++ 中文", ["notepad++"]),
        ("我的世界整合包", ["我的世界"]),
        ("周杰伦 专辑 flac", ["周杰伦", "专辑"]),
    ],
)
def test_subject_role_classification(raw, subjects):
    assert subject_terms(raw) == subjects


@pytest.mark.parametrize(
    "raw,terms",
    [
        ("HDMI 线", ["hdmi", "线"]),      # 不能被 hd 前缀误切
        ("115", ["115"]),
        ("4K", ["4k"]),
        ("HDR", ["hdr"]),
        ("C++", ["c++"]),
        ("C#", ["c#"]),
        ("epub电子书", ["epub", "电子书"]),
        ("AutoCAD破解版", ["autocad", "破解版"]),
        ("三体全集", ["三体", "全集"]),
    ],
)
def test_segmentation_does_not_over_split(raw, terms):
    assert query_terms(raw) == terms


def test_analyze_exposes_roles():
    info = analyze("MATLAB 破解 4K")
    assert info["subjects"] == ["matlab"]
    assert info["generic"] == ["破解"]
    assert info["qualifiers"] == ["4k"]


def test_generic_only_match_is_not_relevant():
    """回归：「MATLAB 破解」的第一条曾是电视剧《河神之渡阴》——只因正文里有"破解"。

    旧算法把"破解"也当主题词命中 → relevance 0.725，直接排第一。
    """
    res = build_resources([RawHit(
        source="s", kind="tg", url="https://pan.quark.cn/s/noise000001",
        title="河神之渡阴 (2026) 剧情 4K HQ 破解版资源",
    )])[0]
    assert _relevance(res, "MATLAB 破解") < 0.45
    assert _relevance(
        build_resources([RawHit(source="s", kind="tg",
                                url="https://pan.quark.cn/s/real0000001",
                                title="MATLAB R2024b 破解版")])[0],
        "MATLAB 破解",
    ) >= 0.5


class _MixedSource(Adapter):
    name = "mixed"
    kind = "pansou"

    def __init__(self):
        super().__init__({})

    async def search(self, kw, client):
        return [
            RawHit(source="s", kind="pansou", url="https://pan.quark.cn/s/matlab000001",
                   title="MATLAB R2024b 破解版"),
            RawHit(source="s", kind="pansou", url="https://pan.quark.cn/s/noise0000001",
                   title="河神之渡阴 (2026) HMAX 破解版"),
        ]


async def test_pipeline_filters_generic_word_only_results(monkeypatch):
    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [_MixedSource()])
    out = await pipeline.search("MATLAB 破解", do_verify=False, alive_only=True, relax=False)
    titles = [r.title for r in out.resources]
    assert any("MATLAB" in (t or "") for t in titles)
    assert not any("河神" in (t or "") for t in titles), "只命中通用词的结果必须被丢弃"
    assert out.irrelevant_pruned >= 1
