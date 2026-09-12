"""去重合并与排序打分测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pansearch.dedupe import build_resources
from pansearch.models import PanType, RawHit, Status, VerifyResult
from pansearch.score import score_all, sort_resources


def hit(url, **kw):
    return RawHit(source=kw.pop("source", "s"), kind=kw.pop("kind", "pansou"), url=url, **kw)


def test_merges_same_share_from_multiple_sources():
    resources = build_resources(
        [
            hit("https://pan.baidu.com/s/1AAA?pwd=1111", source="plugin:a", title="短", pwd="1111"),
            hit("https://pan.baidu.com/s/1AAA?pwd=1111", source="tg:b", title="更长的标题名称", pwd="1111"),
            hit("https://pan.baidu.com/s/1AAA", source="brave"),
        ]
    )
    assert len(resources) == 1
    res = resources[0]
    assert res.hit_count == 3
    assert set(res.sources) == {"plugin:a", "tg:b", "brave"}
    assert res.title == "更长的标题名称"      # 保留信息量最大的标题
    assert res.pwd == "1111"


def test_keeps_different_shares_separate():
    resources = build_resources(
        [
            hit("https://pan.baidu.com/s/1AAA"),
            hit("https://pan.baidu.com/s/1BBB"),
        ]
    )
    assert len(resources) == 2


def test_merges_magnet_by_infohash():
    resources = build_resources(
        [
            hit("magnet:?xt=urn:btih:ABC111&dn=a"),
            hit("magnet:?xt=urn:btih:abc111&dn=b"),
        ]
    )
    assert len(resources) == 1
    assert resources[0].pan_type is PanType.MAGNET


def test_skips_unknown_hosts():
    assert build_resources([hit("https://example.com/whatever")]) == []


def test_earliest_share_time_wins():
    early = datetime(2016, 1, 1, tzinfo=timezone.utc)
    late = datetime(2024, 1, 1, tzinfo=timezone.utc)
    resources = build_resources(
        [
            hit("https://pan.baidu.com/s/1AAA", shared_at=late),
            hit("https://pan.baidu.com/s/1AAA", shared_at=early),
        ]
    )
    assert resources[0].shared_at == early


def _make(surl, status, pwd="abcd", kind="pansou", title="三体 全集"):
    res = build_resources([hit(f"https://pan.baidu.com/s/{surl}", pwd=pwd, title=title, kind=kind)])[0]
    res.verify = VerifyResult(status=status)
    return res


def test_alive_beats_dead():
    alive = _make("1AAA", Status.ALIVE)
    dead = _make("1BBB", Status.DEAD)
    scored = sort_resources(score_all([dead, alive], "三体"))
    assert scored[0] is alive
    assert scored[-1] is dead


def test_baidu_ranks_before_other_pans():
    baidu = _make("1AAA", Status.ALIVE)
    quark = build_resources(
        [hit("https://pan.quark.cn/s/xyz", pwd=None, title="三体 全集")]
    )[0]
    quark.verify = VerifyResult(status=Status.UNSUPPORTED)
    scored = sort_resources(score_all([quark, baidu], "三体"))
    assert scored[0].pan_type is PanType.BAIDU


def test_dead_links_get_near_zero_status_weight():
    alive = _make("1AAA", Status.ALIVE)
    dead = _make("1BBB", Status.DEAD)
    score_all([alive, dead], "三体")
    assert dead.score < alive.score / 5


def test_keyword_relevance_affects_score():
    on_topic = _make("1AAA", Status.ALIVE, title="三体 全集 1080P")
    off_topic = _make("1BBB", Status.ALIVE, title="完全无关的东西")
    score_all([on_topic, off_topic], "三体")
    assert on_topic.score > off_topic.score


def test_multi_source_bonus():
    one = _make("1AAA", Status.ALIVE)
    many = build_resources(
        [
            hit("https://pan.baidu.com/s/1BBB", pwd="abcd", title="三体 全集", kind="pansou"),
            hit("https://pan.baidu.com/s/1BBB", pwd="abcd", title="三体 全集", kind="tg"),
            hit("https://pan.baidu.com/s/1BBB", pwd="abcd", title="三体 全集", kind="websearch"),
        ]
    )[0]
    many.verify = VerifyResult(status=Status.ALIVE)
    score_all([one, many], "三体")
    assert many.score > one.score


def test_freshness_decay():
    fresh = _make("1AAA", Status.ALIVE)
    fresh.shared_at = datetime.now(timezone.utc) - timedelta(days=1)
    old = _make("1BBB", Status.ALIVE)
    old.shared_at = datetime.now(timezone.utc) - timedelta(days=3000)
    score_all([fresh, old], "三体")
    assert fresh.score > old.score


def test_resource_open_url_and_copy_text():
    res = _make("1AAA", Status.ALIVE, pwd="abcd")
    assert res.open_url() == "https://pan.baidu.com/s/1AAA?pwd=abcd"
    assert res.copy_text() == "https://pan.baidu.com/s/1AAA 提取码: abcd"
