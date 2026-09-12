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


def test_multiterm_query_scores_partial_matches():
    """回归：「沙丘 4K HDR」这种多词查询，标题里三个词都出现的必须排最前。

    旧实现把整串拿去匹配（含空格），结果这类标题反而被判成不相关。
    """
    from pansearch.score import _relevance

    full = _make("1AAA", Status.ALIVE, title="沙丘：预言 (2024) 4K DV＆HDR 内封简中")
    partial = _make("1BBB", Status.ALIVE, title="沙丘 全集 1080P")
    none = _make("1CCC", Status.ALIVE, title="完全无关")
    kw = "沙丘 4K HDR"
    assert _relevance(full, kw) > _relevance(partial, kw) > _relevance(none, kw)
    assert _relevance(full, kw) >= 0.95


def test_multiterm_query_ranks_full_match_first():
    from pansearch.score import _relevance

    assert _relevance(_make("1AAA", Status.ALIVE, title="沙丘 4K HDR 原盘"), "沙丘 4K HDR") == 1.0


def test_sort_puts_verified_above_unverified():
    """勾了"剔除失效"后，已验证可用的链接必须浮到未验活的上面。"""
    from pansearch.models import VerifyResult

    verified = _make("1AAA", Status.ALIVE)
    unverified = build_resources(
        [hit("https://pan.xunlei.com/s/VNshhdg5bL3QO86-DzVEgWRmA1", kind="pansou")]
    )[0]
    unverified.verify = VerifyResult(status=Status.UNSUPPORTED)
    # 迅雷在网盘优先级里比百度高也没用，未验活必须靠后
    scored = sort_resources(score_all([unverified, verified], "三体"))
    assert scored[0] is verified
    assert scored[-1] is unverified


def test_relevance_dominates_pan_priority():
    """回归：不相关的百度结果**不能**压过高度相关的夸克结果。

    旧实现把网盘优先级当独立排序层级排在做分数前面，导致
    「沙丘 4K HDR」的头几条是「地狱占星师 4K HDR」这种不相关结果。
    """
    irrelevant_baidu = _make("1AAA", Status.ALIVE, title="地狱占星师 (2026) 4K HDR 全9集")
    relevant_quark = build_resources(
        [hit("https://pan.quark.cn/s/aaaaaaaaaaaa", title="沙丘2 4K HDR 杜比视界")]
    )[0]
    from pansearch.models import VerifyResult

    relevant_quark.verify = VerifyResult(status=Status.ALIVE)

    scored = sort_resources(score_all([irrelevant_baidu, relevant_quark], "沙丘 4K HDR"))
    assert scored[0] is relevant_quark, "高度相关的结果必须排在前面，即使它不是百度网盘"


def test_relaxed_only_hits_are_downweighted():
    primary = _make("1AAA", Status.ALIVE)
    primary.from_primary = True
    relaxed = _make("1BBB", Status.ALIVE)
    relaxed.from_primary = False
    score_all([primary, relaxed], "三体")
    assert relaxed.score < primary.score


def test_missing_subject_term_is_heavily_penalized():
    """查询第一个词是主题词（片名），它缺失时必须重罚。"""
    from pansearch.score import _relevance

    subject = _make("1AAA", Status.ALIVE, title="沙丘 4K HDR 原盘")
    qualifier_only = _make("1BBB", Status.ALIVE, title="黑夏 4K HDR 中文字幕")
    assert _relevance(subject, "沙丘 4K HDR") == 1.0
    assert _relevance(qualifier_only, "沙丘 4K HDR") <= 0.4


def test_bonuses_cannot_overturn_missing_subject():
    """回归：只命中"4K HDR"的结果曾靠百度优先+提取码+多源命中反超真正相关的「沙丘」。

    这类结果（如「黑夏 4K HDR」「冬城猎凶 4K HDR」）实测占据过前排。
    """
    from pansearch.models import VerifyResult

    # 真正相关：夸克、单源、无提取码
    relevant = build_resources(
        [hit("https://pan.quark.cn/s/aaaaaaaaaaaa", title="沙丘2 4K HDR 杜比视界")]
    )[0]
    relevant.verify = VerifyResult(status=Status.ALIVE)

    # 不相关但"条件很好"：百度 + 有提取码 + 三个 TG 频道都命中
    irrelevant = build_resources(
        [
            hit("https://pan.baidu.com/s/1BBB?pwd=1111", pwd="1111", title="黑夏 4K HDR 中文字幕", kind="tg"),
            hit("https://pan.baidu.com/s/1BBB?pwd=1111", pwd="1111", title="黑夏 4K HDR 中文字幕", kind="tg"),
            hit("https://pan.baidu.com/s/1BBB?pwd=1111", pwd="1111", title="黑夏 4K HDR 中文字幕", kind="tg"),
        ]
    )[0]
    irrelevant.verify = VerifyResult(status=Status.ALIVE, pwd_verified=True)

    scored = sort_resources(score_all([irrelevant, relevant], "沙丘 4K HDR"))
    assert scored[0] is relevant, (
        f"主题词缺失的结果不该排第一（relevant={relevant.score}, irrelevant={irrelevant.score}）"
    )


def test_resource_merging_prefers_primary_hit():
    resources = build_resources(
        [
            hit("https://pan.baidu.com/s/1AAA", relaxed=True),
            hit("https://pan.baidu.com/s/1AAA", relaxed=False),
        ]
    )
    assert len(resources) == 1
    assert resources[0].from_primary is True

    only_relaxed = build_resources([hit("https://pan.baidu.com/s/1BBB", relaxed=True)])[0]
    assert only_relaxed.from_primary is False


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
