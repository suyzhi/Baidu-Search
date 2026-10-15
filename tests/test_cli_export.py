"""CLI 导出测试（离线）。

--csv 导出的文件是给 Excel / WPS 打开的，而标题、大小、来源页都是从第三方页面抓来的：
以 = + - @ 开头的单元格会被当成公式执行（CSV 注入），必须按纯文本落盘。
"""

from __future__ import annotations

import csv
from datetime import datetime

import pytest
from typer.testing import CliRunner

from pansearch import cli, pipeline
from pansearch.dedupe import build_resources
from pansearch.models import RawHit
from pansearch.store import VerifyCache


@pytest.mark.parametrize("raw", ["=1+1", "+1GB", "-2", "@SUM(A1)", "\tx", "\rx"])
def test_csv_safe_prefixes_formula_cells(raw):
    assert cli._csv_safe(raw) == "'" + raw


@pytest.mark.parametrize("raw", [
    "沙丘 4K", "https://pan.quark.cn/s/abc", "magnet:?xt=urn:btih:aa", "2026-01-02",
    "1.2GB", "", 3, None,
])
def test_csv_safe_leaves_plain_values_alone(raw):
    assert cli._csv_safe(raw) == raw


def test_search_csv_export_neutralizes_formula_injection(tmp_path, monkeypatch):
    resources = build_resources([RawHit(
        source="@chan", kind="tg", url="https://pan.quark.cn/s/abc123",
        title='=HYPERLINK("http://evil.example","点我")', size="+1GB",
        origin="-cmd|' /C calc'!A0", shared_at=datetime(2026, 1, 2),
    )])

    async def fake_search(kw, **kwargs):
        return pipeline.SearchOutcome(keyword=kw, resources=resources)

    monkeypatch.setattr(cli, "run_search", fake_search)
    # 不碰真实的验活缓存库（search 会顺手记一条搜索日志）
    monkeypatch.setattr(cli, "VerifyCache", lambda: VerifyCache(tmp_path / "c.sqlite3"))

    out = tmp_path / "r.csv"
    result = CliRunner().invoke(cli.app, ["search", "沙丘", "--csv", str(out)])
    assert result.exit_code == 0, result.output

    with out.open(encoding="utf-8-sig", newline="") as fh:
        (row,) = list(csv.DictReader(fh))
    assert row["标题"] == '\'=HYPERLINK("http://evil.example","点我")'
    assert row["大小"] == "'+1GB"
    assert row["来源页面"] == "'-cmd|' /C calc'!A0"
    assert row["来源"] == "'@chan"
    # 链接 / 数字 / 日期列保持原样
    assert row["链接"] == "https://pan.quark.cn/s/abc123"
    assert row["命中次数"] == "1"
    assert row["分享时间"] == "2026-01-02"
