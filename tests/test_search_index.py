"""FTS5(CJK bigram) / BM25 / IDF / RRF / 简繁归一 的回归测试（全部离线）。"""

from __future__ import annotations

import pytest

from pansearch import textindex
from pansearch.models import RawHit
from pansearch.dedupe import build_resources
from pansearch.query import normalize_text, query_terms
from pansearch.score import _relevance, anchor_term, confidently_irrelevant, score_all
from pansearch.tgindex import TgIndex, TgMessage


# ---------------------------------------------------------------- 编码器
@pytest.mark.parametrize("text,expected", [
    ("沙丘", ["沙丘"]),
    ("沙丘预言", ["沙丘", "丘预", "预言"]),
    # 相邻 CJK 片段跨空白/标点合并：否则「沙丘预言」永远匹配不到「沙丘 预言」
    ("沙丘 预言", ["沙丘", "丘预", "预言"]),
    ("沙丘,预言", ["沙丘", "丘预", "预言"]),
    ("沙丘 4K", ["沙丘", "4k"]),
    ("MATLAB 破解", ["matlab", "破解"]),
    ("单", ["单"]),
])
def test_ngram_encoder(text, expected):
    assert textindex.ngram_tokens(text) == expected


def test_match_phrase_rejects_single_char():
    assert textindex.match_phrase("沙丘") == '"沙丘"'
    assert textindex.match_phrase("沙丘预言") == '"沙丘 丘预 预言"'
    assert textindex.match_phrase("单") is None      # 单字素 bigram 覆盖不了 -> 回退 LIKE


# ---------------------------------------------------------------- FTS 检索
def _index(tmp_path) -> TgIndex:
    idx = TgIndex(tmp_path / "tg.sqlite3")
    idx.upsert([
        TgMessage("a", 1, "2026-01-03T00:00:00Z", "沙丘 4K HDR 全集",
                  [{"url": "https://pan.quark.cn/s/a1"}]),
        TgMessage("a", 2, "2026-01-01T00:00:00Z", "沙丘 预言 电视剧",
                  [{"url": "https://pan.quark.cn/s/a2"}]),
        TgMessage("a", 3, "2026-01-02T00:00:00Z", "黑夏 4K HDR",
                  [{"url": "https://pan.quark.cn/s/a3"}]),
        TgMessage("a", 4, "2026-01-04T00:00:00Z", "沙丘预言 姐妹会",
                  [{"url": "https://pan.quark.cn/s/a4"}]),
        TgMessage("a", 5, "2026-01-05T00:00:00Z", "沙丘 没有链接的闲聊", []),
    ])
    return idx


def test_fts_ready_on_fresh_index(tmp_path):
    idx = _index(tmp_path)
    try:
        assert idx.fts_ready() is True
    finally:
        idx.close()


def test_fts_search_matches_across_spaces_and_skips_linkless(tmp_path):
    idx = _index(tmp_path)
    try:
        ids = lambda kw: [r["msg_id"] for r in idx.search(kw, limit=10)]
        assert ids("沙丘预言") == [4, 2]          # 跨空格命中；不含无链接的 5
        assert set(ids("沙丘")) == {1, 2, 4}
        assert ids("黑夏") == [3]
        # 主题词 4K/HDR 是限定词，不参与 WHERE；"沙丘 4K HDR" 只召回含沙丘的
        assert set(ids("沙丘 4K HDR")) == {1, 2, 4}
        assert ids("漫威") == []
    finally:
        idx.close()


def test_fts_hits_count_reflects_terms(tmp_path):
    idx = _index(tmp_path)
    try:
        rows = {r["msg_id"]: r["hits"] for r in idx.search("沙丘 4K HDR", limit=10)}
        assert rows[1] == 3 and rows[2] == 1
    finally:
        idx.close()


def test_idf_is_lower_for_common_terms(tmp_path):
    idx = _index(tmp_path)
    try:
        textindex.reset_cache()
        common = textindex.term_idf("沙丘", idx.path)     # 4 条里 3 条含它
        rare = textindex.term_idf("黑夏", idx.path)       # 只有 1 条
        assert common is not None and rare is not None
        assert common < rare
    finally:
        idx.close()
        textindex.reset_cache()


def test_like_fallback_when_fts_not_built(tmp_path):
    """旧库（未 build_fts）要能继续用 LIKE 检索。"""
    path = tmp_path / "old.sqlite3"
    idx = TgIndex(path)
    idx.upsert([TgMessage("a", 1, None, "沙丘 全集", [{"url": "https://pan.quark.cn/s/x1"}])])
    idx.conn.execute("UPDATE tg_meta SET value='0' WHERE key='fts_built'")
    idx.conn.commit()
    try:
        assert idx.fts_ready() is False
        assert [r["msg_id"] for r in idx.search("沙丘")] == [1]
    finally:
        idx.close()


def test_build_fts_backfills_existing_rows(tmp_path):
    """真实迁移路径：旧库（没有 tokens 列、没有 FTS 表）要能被 build_fts 接管。"""
    import sqlite3

    path = tmp_path / "mig.sqlite3"
    raw = sqlite3.connect(path)
    raw.executescript("""
        CREATE TABLE tg_messages (
            channel TEXT NOT NULL, msg_id INTEGER NOT NULL, posted_at TEXT,
            text TEXT NOT NULL, links TEXT NOT NULL, indexed_at REAL NOT NULL,
            PRIMARY KEY (channel, msg_id));
        CREATE TABLE tg_channels (channel TEXT PRIMARY KEY, newest_id INTEGER,
            oldest_id INTEGER, msg_count INTEGER NOT NULL DEFAULT 0,
            last_crawl REAL, last_status TEXT);
    """)
    raw.execute("INSERT INTO tg_messages VALUES (?,?,?,?,?,?)",
                ("a", 1, None, "沙丘 预言", '[{"url": "https://pan.quark.cn/s/m1"}]', 0.0))
    raw.commit()
    raw.close()

    idx = TgIndex(path)                        # 迁移：补 tokens 列 + 建 FTS（未回填）
    try:
        assert idx.fts_ready() is False
        assert [r["msg_id"] for r in idx.search("沙丘")] == [1]   # LIKE 回退可用
        info = idx.build_fts()
        assert info["fts_docs"] == 1
        assert idx.fts_ready() is True
        assert [r["msg_id"] for r in idx.search("沙丘预言")] == [1]
    finally:
        idx.close()


def test_trigger_keeps_fts_in_sync(tmp_path):
    idx = _index(tmp_path)
    try:
        idx.upsert([TgMessage("a", 6, None, "沙丘 新增消息",
                              [{"url": "https://pan.quark.cn/s/a6"}])])
        assert 6 in [r["msg_id"] for r in idx.search("沙丘")]
        # 同一条消息改写后不应重复命中
        idx.upsert([TgMessage("a", 6, None, "黑夏 改写了",
                              [{"url": "https://pan.quark.cn/s/a6"}])])
        ids = [r["msg_id"] for r in idx.search("沙丘")]
        assert ids.count(6) == 0
        assert idx.search("黑夏")[0]["msg_id"] == 3
    finally:
        idx.close()


# ---------------------------------------------------------------- 简繁归一
def test_traditional_query_matches_simplified_title():
    assert normalize_text("周杰倫 無損") == "周杰伦 无损"
    assert query_terms("周杰倫") == query_terms("周杰伦")


def test_traditional_title_matches_simplified_query(tmp_path):
    idx = TgIndex(tmp_path / "t.sqlite3")
    try:
        idx.upsert([TgMessage("a", 1, None, "周杰倫 無損 專輯",
                              [{"url": "https://pan.quark.cn/s/zh1"}])])
        assert [r["msg_id"] for r in idx.search("周杰伦")] == [1]
    finally:
        idx.close()


# ---------------------------------------------------------------- RRF
def test_rrf_rewards_resources_ranked_high_in_more_sources():
    high = build_resources([
        RawHit(source="tg:x", kind="tg", url="https://pan.quark.cn/s/high1",
               title="沙丘 全集", rank=0),
        RawHit(source="site:a", kind="forum", url="https://pan.quark.cn/s/high1",
               title="沙丘 全集", rank=0),
    ])[0]
    low = build_resources([
        RawHit(source="tg:y", kind="tg", url="https://pan.quark.cn/s/low1",
               title="沙丘 全集", rank=39),
    ])[0]
    assert high.rrf > low.rrf
    score_all([high, low], "沙丘")
    assert high.score > low.score


def test_rrf_ignores_missing_rank():
    res = build_resources([RawHit(source="s", kind="pansou",
                                  url="https://pan.quark.cn/s/norank1", title="沙丘")])[0]
    assert res.rrf == 0.0


def test_pipeline_records_source_rank(monkeypatch):
    """pipeline 必须给每个来源内部的名次打标（RRF 的数据来源）。"""
    import asyncio

    from pansearch import pipeline
    from pansearch.adapters.base import Adapter

    class Src(Adapter):
        name = "s"

        async def search(self, kw, client):
            return [RawHit(source="s", kind="pansou",
                           url=f"https://pan.quark.cn/s/r{i:08d}", title="沙丘") for i in range(3)]

    monkeypatch.setattr(pipeline, "build_adapters", lambda names=None: [Src()])
    out = asyncio.run(pipeline.search("沙丘", do_verify=False, alive_only=False, relax=False))
    assert out.resources and all(r.rrf > 0 for r in out.resources)


# ---------------------------------------------------------------- IDF 锚点
def test_anchor_picks_rarest_subject_term(monkeypatch):
    """「Serum 合成器」的锚点必须是罕见的 serum，而不是到处都是的"合成器"。

    旧行为：主题词"任一命中"即算相关 -> 标题只要提到"合成器"的音乐帖全进来。
    """
    weights = {"serum": 6.0, "合成器": 1.2}
    monkeypatch.setattr("pansearch.score.term_idf", lambda t: weights.get(t))
    assert anchor_term("Serum 合成器") == "serum"
    assert anchor_term("合成器 音源") == "合成器"


def test_anchor_falls_back_to_first_subject_without_idf(monkeypatch):
    monkeypatch.setattr("pansearch.score.term_idf", lambda t: None)
    assert anchor_term("Serum 合成器") == "serum"      # subjects[0]
    assert anchor_term("沙丘 4K HDR") == "沙丘"


def test_relevance_requires_anchor(monkeypatch):
    weights = {"serum": 6.0, "合成器": 1.2}
    monkeypatch.setattr("pansearch.score.term_idf", lambda t: weights.get(t))
    noise = build_resources([RawHit(source="s", kind="tg",
                                    url="https://pan.quark.cn/s/noise01",
                                    title="合成器 音乐合集 专辑")])[0]
    real = build_resources([RawHit(source="s", kind="tg",
                                   url="https://pan.quark.cn/s/real01",
                                   title="Xfer Serum 2 合成器")])[0]
    assert _relevance(noise, "Serum 合成器") < 0.4, "只命中非锚点主题词要沉底"
    assert _relevance(real, "Serum 合成器") >= 0.9
    assert confidently_irrelevant(noise, "Serum 合成器") is True


def test_anchor_keeps_cjk_query_latin_title(monkeypatch):
    """中文查询返回英文片名（翻译）时不能删 —— 这是有意的跨语言保留。"""
    monkeypatch.setattr("pansearch.score.term_idf", lambda t: 1.0)
    res = build_resources([RawHit(source="s", kind="tg",
                                  url="https://pan.quark.cn/s/dune01",
                                  title="Dune Part Two 2024")])[0]
    assert confidently_irrelevant(res, "沙丘 2") is False
