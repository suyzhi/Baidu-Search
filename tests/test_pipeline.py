"""查询放宽（多词查询召回塌陷的补救）测试。"""

from __future__ import annotations

import pytest

from pansearch.pipeline import relaxed_queries


@pytest.mark.parametrize(
    "kw,expected",
    [
        # 实测「沙丘 4K HDR」在 PanSou 只有 3 条，「沙丘」有 180+ 条
        ("沙丘 4K HDR", ["沙丘", "4K HDR"]),
        ("三体 全集", ["三体", "全集"]),
        ("Dune 4K", ["Dune", "4K"]),
        ("沙丘、4K、HDR", ["沙丘", "4K HDR"]),
    ],
)
def test_relaxed_queries_multiword(kw, expected):
    assert relaxed_queries(kw) == expected


@pytest.mark.parametrize("kw", ["沙丘", "三体", "", "  "])
def test_relaxed_queries_single_word_is_noop(kw):
    assert relaxed_queries(kw) == []


def test_relaxed_queries_never_repeats_original():
    assert "沙丘 4K HDR" not in relaxed_queries("沙丘 4K HDR")
