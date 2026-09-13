"""针对真实审计反例及流式生命周期的行为回归。"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from pansearch import pipeline, webapp
from pansearch.adapters.base import Adapter
from pansearch.adapters import pansou
from pansearch.dedupe import build_resources
from pansearch.models import RawHit, Status, VerifyResult
from pansearch.query import query_terms, split_query, term_present
from pansearch.score import confidently_irrelevant, score_all
from pansearch.tgindex import TgIndex, TgMessage


def hit(key, title):
    return RawHit(source="test", kind="tg", url=f"https://pan.quark.cn/s/{key}", title=title)


@pytest.mark.parametrize("query,title", [
    ("大气合成器", "Omnisphere 2 音源"),
    ("沙丘预言", "沙丘：预言 4K"),
    ("周杰伦", "周 傑 倫 無損"),
])
def test_same_matching_evidence_for_ranking_and_filter(query, title):
    res = build_resources([hit("good", title)])[0]
    res.queries = [query, *pipeline.alias_queries(query)]
    score_all([res], query)
    assert res.relevance >= 0.95
    assert not confidently_irrelevant(res, query)


@pytest.mark.parametrize("title", ["ELF Learning 英语启蒙", "War Machine", "Machinery Learning"])
def test_multisubject_queries_require_content_words(title):
    res = build_resources([hit("noise", title)])[0]
    score_all([res], "machine learning")
    assert res.relevance < 0.45
    assert confidently_irrelevant(res, "machine learning")


def test_generic_duplicate_cannot_override_informative_title():
    res = build_resources([hit("same", "黑夏 4K"), hit("same", "夸克")])[0]
    assert confidently_irrelevant(res, "沙丘")


def test_symbol_terms_ignore_urls_and_respect_boundaries():
    assert term_present("C# 教程", "c#")
    assert not term_present("ABC# 文档", "c#")
    assert not term_present("提取码 https://pan.baidu.com/s/1abc?pwd=wc#", "c#")
    assert not term_present("C++ 教程", "c")


def test_cached_query_lists_are_not_shared():
    terms = query_terms("沙丘 4K")
    terms.clear()
    parts = split_query("沙丘 4K")
    parts.append("noise")
    assert query_terms("沙丘 4K") == ["沙丘", "4k"]
    assert split_query("沙丘 4K") == ["沙丘", "4K"]


def test_fts_phrase_and_version_suffix(tmp_path):
    index = TgIndex(tmp_path / "tg.sqlite3")
    try:
        texts = ["沙丘：预言", "沙丘 科幻 丘预 故事 预言", "预言 丘预 沙丘",
                 "Serum2 合成器", "Serumology 专辑"]
        index.upsert([TgMessage("a", i, None, t, [{"url": f"https://pan.quark.cn/s/{i}"}])
                      for i, t in enumerate(texts)])
        assert [r["text"] for r in index.search("沙丘预言")] == [texts[0]]
        assert [r["text"] for r in index.search("Serum")] == [texts[3]]
    finally:
        index.close()


def test_rrf_counts_best_rank_once_per_source():
    one = hit("same", "沙丘").model_copy(update={"rank": 2})
    res = build_resources([one, one, one.model_copy(update={"rank": 20})])[0]
    assert res.rrf == pytest.approx(1 / 62)
    other = one.model_copy(update={"source": "independent", "rank": 0})
    assert build_resources([one, other])[0].rrf == pytest.approx(1 / 62 + 1 / 60)


async def test_instance_timeout_leaves_time_for_backup(monkeypatch):
    pansou._HEALTH.clear()
    calls = []
    adapter = pansou.PansouAdapter({"instances": ["http://slow", "http://fast"], "deadline": .15})

    async def query(client, url, kw):
        calls.append(url)
        if url.endswith("slow"):
            await asyncio.Event().wait()
        return {"data": {"merged_by_type": {}}}

    monkeypatch.setattr(adapter, "_query", query)
    try:
        _, errors, _ = await pipeline._fetch_hits([adapter], None, "沙丘")
        assert not errors
        assert calls == ["http://slow", "http://fast"]
        calls.clear()
        await pipeline._fetch_hits([adapter], None, "沙丘")
        assert calls == ["http://fast"]
    finally:
        pansou._HEALTH.clear()


async def test_progress_arrives_before_slow_source_and_final_is_complete(monkeypatch):
    release = asyncio.Event()
    calls = []

    class Fast(Adapter):
        name = "fast"
        async def search(self, kw, client):
            calls.append(self.name)
            return [hit("fast", "沙丘")]

    class Slow(Adapter):
        name = "slow"
        async def search(self, kw, client):
            calls.append(self.name)
            await release.wait()
            return [hit("slow", "沙丘 4K")]

    monkeypatch.setattr(pipeline, "build_adapters", lambda _: [Fast(), Slow()])
    frames = []
    async def progress(out):
        frames.append(out)
        if not release.is_set():
            assert len(out.resources) == 1
            release.set()
    out = await asyncio.wait_for(pipeline.search("沙丘", do_verify=False, relax=False,
                                               on_progress=progress), 2)
    assert len(frames[0].resources) == 1
    assert len(out.resources) == 2
    assert sorted(calls) == ["fast", "slow"]
    assert out.timings["verify"] == 0


async def test_cancel_search_cancels_sources(monkeypatch):
    started, stopped = asyncio.Event(), asyncio.Event()
    class Slow(Adapter):
        name = "slow"
        async def search(self, kw, client):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
    monkeypatch.setattr(pipeline, "build_adapters", lambda _: [Slow()])
    task = asyncio.create_task(pipeline.search("沙丘", do_verify=False))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


async def test_stream_disconnect_stops_runner(monkeypatch):
    started, stopped = asyncio.Event(), asyncio.Event()
    async def fake(kw, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    monkeypatch.setattr(webapp, "run_search", fake)
    response = await webapp.api_search_stream("沙丘", None, 50, True, False, False)
    iterator = response.body_iterator
    assert '"start"' in await anext(iterator)
    await asyncio.wait_for(started.wait(), 2)
    await iterator.aclose()
    assert stopped.is_set()


def test_stream_schema_cache_error_and_type_validation(monkeypatch):
    async def fake(kw, **kwargs):
        if kw == "error":
            raise RuntimeError("模拟网络中断")
        res = build_resources([hit("one", "沙丘")])
        res[0].verify = VerifyResult(status=Status.UNCHECKED)
        out = pipeline.SearchOutcome(keyword=kw, resources=res, from_cache=True)
        return out
    monkeypatch.setattr(webapp, "run_search", fake)
    with TestClient(webapp.app) as client:
        resp = client.get("/api/search/stream", params={"kw": "沙丘"})
        events = [json.loads(line) for line in resp.text.splitlines()]
        assert [e["event"] for e in events] == ["start", "complete"]
        assert events[-1]["data"]["from_cache"]
        assert not events[-1]["data"]["results"][0]["alive"]
        bad = client.get("/api/search/stream", params={"kw": "error"})
        assert json.loads(bad.text.splitlines()[-1])["event"] == "error"
        assert client.get("/api/search/stream", params={"kw": "沙丘", "types": "bogus"}).status_code == 422


def test_independent_labels_count_missing_and_short_lists():
    from scripts.quality_eval import metrics
    result = metrics(["good", "bad"], {"good": 2, "missing": 2, "bad": 0})
    assert result["precision"] == .5
    assert result["recall"] == .5
    assert result["missing"] == ["missing"]
    assert result["ndcg"] < 1


def test_independent_judgment_gate():
    from scripts.quality_eval import evaluate
    report = evaluate()
    assert report["summary"]["gate"], report


def test_word_metric_uses_actual_evaluated_slots():
    from scripts.fulltest import evaluate, report
    res = build_resources([hit("a", "沙丘"), hit("b", "无关节目")])
    out = pipeline.SearchOutcome(keyword="沙丘", resources=res)
    row = evaluate("movie", "沙丘", out, .1, 20)
    summary = report([row], 20)
    assert row["evaluated_top"] == 2
    assert summary["absent_ratio"] == .5


@pytest.mark.parametrize('query,title', [
    ('霸王别姬 修复版', '霸王别姬 蓝光'),
    ('进击的巨人 最终季', '进击的巨人 全系列'),
    ('钢琴曲 纯音乐 无损', '钢琴曲 无损合集'),
])
def test_optional_resource_facets_do_not_delete_subject_matches(query, title):
    res = build_resources([hit('facet', title)])[0]
    score_all([res], query)
    assert not confidently_irrelevant(res, query)
    assert .45 <= res.relevance < .95


def test_query_parameters_cannot_be_title_evidence():
    from pansearch.query import matching_text
    assert matching_text('?pwd=8888') == ''
    assert not term_present('?pwd=abc#', 'c#')


async def test_backup_failure_does_not_cancel_useful_cold_primary(monkeypatch):
    pansou._HEALTH.clear()
    adapter = pansou.PansouAdapter({'instances': ['http://primary', 'http://backup'],
                                   'deadline': .3, 'failover_delay': .01})
    async def query(client, url, kw):
        if url.endswith('backup'):
            raise RuntimeError('备用实例挂了')
        await asyncio.sleep(.17)
        return {'data': {'merged_by_type': {}}}
    monkeypatch.setattr(adapter, '_query', query)
    try:
        await adapter.search('沙丘', None)
        assert adapter.used_instance == 'http://primary'
        assert 'http://primary' not in pansou._HEALTH
    finally:
        pansou._HEALTH.clear()
