"""批量验活（pansearch verify 多链接 / -f 文件 / -f -）的测试 —— MockTransport，完全离线。

锁住的约定：
  * 输入按内容识别：任意文本 / search·verify 导出的 CSV（去掉防注入的 '）/ 导出的 JSON；
  * 按分享指纹去重，保留最先出现的提取码；认不出的链接要计数并报出来；
  * 不支持验活的网盘（迅雷 / 磁力 / API 直链…）一律"不支持"，绝不算存活，也不发请求；
  * 只给一条链接、不带 -f 时，输出与退出码和以前的单条校验完全一致；
  * --alive-only 的 stdout 只有存活链接（copy_text 形态），其余信息走 stderr。
"""

from __future__ import annotations

import csv
import json

import httpx
import pytest
from rich.console import Console
from typer.testing import CliRunner

from pansearch import cli, pipeline, verifiers
from pansearch.batchverify import (
    Collected, _uncsv, build_batch, collect_hits, summarize, verify_batch,
)
from pansearch.dedupe import build_resources
from pansearch.models import PanType, RawHit, Status
from pansearch.store import VerifyCache

# ---------------------------------------------------------------- 假网络
# 百度：share/verify 用去掉开头 1 的 surl，shorturlinfo 用完整 surl（见 test_verify.py）
SHARE_ERRNO = {"BaiduOK": 0, "BaiduWrong": -9}           # 其余 105 = 不存在
SHORT_ERRNO = {"1BaiduNeed": -9, "1BaiduWrong": -9, "1BaiduDead": 140}
EVIL_TITLE = '=HYPERLINK("http://evil.example","点我")'
QUARK = {
    "qalive": (200, {"status": 200, "code": 0, "data": {"title": "沙丘2 4K"}}),
    "qformula": (200, {"status": 200, "code": 0, "data": {"title": EVIL_TITLE}}),
    "qdead": (200, {"status": 200, "code": 41006, "message": "分享不存在"}),
}


def route(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    params = dict(request.url.params)
    if "/share/verify" in url:
        return httpx.Response(200, json={"errno": SHARE_ERRNO.get(params.get("surl"), 105)})
    if "/api/shorturlinfo" in url:
        return httpx.Response(200, json={"errno": SHORT_ERRNO.get(params.get("shorturl"), 0)})
    if "sharepage/token" in url:
        pwd_id = json.loads(request.content)["pwd_id"]
        if pwd_id == "qunknown":
            return httpx.Response(500, text="oops")
        status, body = QUARK.get(pwd_id, (404, {"code": 41006}))
        return httpx.Response(status, json=body)
    raise AssertionError(f"不该请求：{url}")


TEST_CFG = {
    "cache_ttl_hours": 6, "retries": 0, "concurrency": 4, "timeout": 5, "rate_limit_qps": 0,
    "services": {name: {"qps": 0} for name in ("quark", "aliyun", "115", "tianyi")},
}


@pytest.fixture
def seen():
    return []


@pytest.fixture
def fake_pool(tmp_path, monkeypatch, seen):
    """把 VerifierPool 换成走 MockTransport 的版本；seen 记录每一个发出的请求。"""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return route(request)

    class FakePool(verifiers.VerifierPool):
        def __init__(self, cfg=None, cache=None):
            super().__init__(cfg=TEST_CFG, cache=VerifyCache(tmp_path / "c.sqlite3"))

        async def __aenter__(self):
            self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            self.baidu._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            self.baidu._warmed = True
            return self

        async def __aexit__(self, *exc):
            await super().__aexit__(*exc)
            self.cache.close()

    monkeypatch.setattr(verifiers, "VerifierPool", FakePool)
    return FakePool


@pytest.fixture
def wide(monkeypatch):
    """表格测试用固定宽度的控制台（不随运行测试的终端宽度变化）。"""
    monkeypatch.setattr(cli, "console", Console(width=200, force_terminal=False))
    monkeypatch.setattr(cli, "err_console",
                        Console(width=200, force_terminal=False, stderr=True))


def _flat(text: str) -> str:
    """去掉空白与 Rich 错误面板的边框，便于断言被折行的提示文字。"""
    return "".join(ch for ch in text if not ch.isspace() and ch not in "│┌┐└┘─╭╮╰╯")


MIXED = """\
沙丘2 4K 百度：https://pan.baidu.com/s/1BaiduOK 提取码: abcd
百度要码 https://pan.baidu.com/s/1BaiduNeed
码不对 https://pan.baidu.com/s/1BaiduWrong?pwd=zzzz
百度死链 https://pan.baidu.com/s/1BaiduDead
夸克 https://pan.quark.cn/s/qalive
夸克死链 https://pan.quark.cn/s/qdead
夸克抽风 https://pan.quark.cn/s/qunknown
迅雷 https://pan.xunlei.com/s/VNxunlei123
磁力 magnet:?xt=urn:btih:%s&tr=http://tracker.example.com/announce
论文 https://arxiv.org/abs/2401.00001
不是网盘 https://example.com/page
百度首页 https://pan.baidu.com/
""" % ("a" * 40)


# ---------------------------------------------------------------- 输入解析
def test_collect_free_text_extracts_links_codes_and_reports_unrecognised():
    got = collect_hits(MIXED)
    assert got.fmt == "text"
    by_url = {h.url: h.pwd for h in got.hits}
    assert by_url["https://pan.baidu.com/s/1BaiduOK"] == "abcd"
    assert by_url["https://pan.baidu.com/s/1BaiduWrong"] == "zzzz"     # ?pwd= 当提取码配对
    assert by_url["https://pan.quark.cn/s/qalive"] is None
    assert len(got.hits) == 10
    # 非网盘链接、网盘首页 → 认不出；磁力里的 tr=http://… 不算一条独立输入
    assert got.unrecognised == ["https://example.com/page", "https://pan.baidu.com/"]


def test_collect_bare_domain_and_hit_metadata():
    got = collect_hits("链接：pan.quark.cn/s/qalive", source_name="links.txt")
    (hit,) = got.hits
    assert hit.url == "https://pan.quark.cn/s/qalive"
    assert hit.source == "links.txt" and hit.kind == "manual"
    assert got.unrecognised == []


def test_collect_query_addressed_baidu_link_falls_back_to_single_link():
    """抽取器在 ? 处截断 URL，认不出 share/init?surl=；单条校验认得它，批量也不能丢。"""
    got = collect_hits("旧版链接 https://pan.baidu.com/share/init?surl=BaiduOK，"
                       "另一条 https://pan.baidu.com/share/link?shareid=1&uk=2")
    assert [h.url for h in got.hits] == ["https://pan.baidu.com/share/init?surl=BaiduOK"]
    (res,) = build_batch(got)
    assert res.surl == "BaiduOK"
    # share/link?shareid= 解析不出 surl，单条和批量都无法验活 → 报"无法识别"
    assert got.unrecognised == ["https://pan.baidu.com/share/link?shareid=1&uk=2"]

    got = collect_hits(json.dumps(["https://pan.baidu.com/share/init?surl=BaiduOK"]))
    assert [h.url for h in got.hits] == ["https://pan.baidu.com/share/init?surl=BaiduOK"]


def _fake_search(monkeypatch, tmp_path, resources):
    async def fake_search(kw, **kwargs):
        return pipeline.SearchOutcome(keyword=kw, resources=resources)

    monkeypatch.setattr(cli, "run_search", fake_search)
    monkeypatch.setattr(cli, "VerifyCache", lambda: VerifyCache(tmp_path / "log.sqlite3"))


def _search_export_resources():
    return build_resources([
        RawHit(source="@chan", kind="tg", url="https://pan.baidu.com/s/1BaiduOK?pwd=abcd",
               title=EVIL_TITLE),
        RawHit(source="@chan", kind="tg", url="https://pan.quark.cn/s/qalive", title="沙丘"),
    ])


def test_collect_reads_search_csv_export(tmp_path, monkeypatch):
    _fake_search(monkeypatch, tmp_path, _search_export_resources())
    out = tmp_path / "r.csv"
    result = CliRunner().invoke(cli.app, ["search", "沙丘", "--csv", str(out)])
    assert result.exit_code == 0, result.output

    text = out.read_text(encoding="utf-8")            # 保留 BOM：collect_hits 自己处理
    assert text.startswith("\ufeff")
    got = collect_hits(text, source_name="r.csv")
    assert got.fmt == "csv"
    assert [(h.url, h.pwd) for h in got.hits] == [
        ("https://pan.baidu.com/s/1BaiduOK", "abcd"),
        ("https://pan.quark.cn/s/qalive", None),
    ]
    assert got.hits[0].title == EVIL_TITLE            # 去掉了防注入补的 '
    assert got.unrecognised == []


def test_collect_reads_search_json_export(tmp_path, monkeypatch):
    _fake_search(monkeypatch, tmp_path, _search_export_resources())
    out = tmp_path / "r.json"
    result = CliRunner().invoke(cli.app, ["search", "沙丘", "--json", str(out)])
    assert result.exit_code == 0, result.output

    got = collect_hits(out.read_text(encoding="utf-8"))
    assert got.fmt == "json"
    assert [(h.url, h.pwd, h.title) for h in got.hits] == [
        ("https://pan.baidu.com/s/1BaiduOK", "abcd", EVIL_TITLE),
        ("https://pan.quark.cn/s/qalive", None, "沙丘"),
    ]


def test_collect_json_url_pwd_shape_and_plain_list():
    got = collect_hits(json.dumps({"results": [
        {"url": "https://pan.baidu.com/s/1BaiduOK", "pwd": "abcd"},
        {"url": "https://example.com/x"},
        {"title": "没有链接"},
    ]}))
    assert got.fmt == "json"
    assert [(h.url, h.pwd) for h in got.hits] == [("https://pan.baidu.com/s/1BaiduOK", "abcd")]
    assert got.unrecognised == ["https://example.com/x", "第 3 条：链接为空"]

    got = collect_hits('["https://pan.quark.cn/s/qalive", "pan.baidu.com/s/1BaiduOK 提取码 abcd"]')
    assert [(h.url, h.pwd) for h in got.hits] == [
        ("https://pan.quark.cn/s/qalive", None), ("https://pan.baidu.com/s/1BaiduOK", "abcd"),
    ]


def test_collect_other_json_and_bracket_text_fall_back_to_free_text():
    got = collect_hits('{"msg": "看这个 https://pan.quark.cn/s/qalive"}')
    assert got.fmt == "text" and len(got.hits) == 1
    got = collect_hits("[夸克] https://pan.quark.cn/s/qalive")
    assert got.fmt == "text" and len(got.hits) == 1


def test_csv_column_code_wins_over_code_in_link():
    text = "链接,提取码\nhttps://pan.baidu.com/s/1BaiduOK?pwd=old1,new2\n\n"
    got = collect_hits(text)
    assert [(h.url, h.pwd) for h in got.hits] == [("https://pan.baidu.com/s/1BaiduOK", "new2")]


@pytest.mark.parametrize("raw", ["=1+1", "+1GB", "-2", "@SUM(A1)", "\tx", "\rx",
                                 "沙丘", "'quoted", "https://pan.quark.cn/s/abc", ""])
def test_uncsv_is_inverse_of_cli_csv_safe(raw):
    assert _uncsv(cli._csv_safe(raw)) == raw.strip()


# ---------------------------------------------------------------- 去重
def test_dedupe_merges_same_share_and_keeps_first_code():
    got = collect_hits(
        "https://pan.baidu.com/s/1BaiduOK?pwd=aaaa\n"
        "转发：http://pan.baidu.com/s/1BaiduOK 提取码 bbbb\n"
        "https://pan.quark.cn/s/qalive\n"
        "http://pan.quark.cn/s/qalive/\n"
        "https://pan.baidu.com/s/1BaiduNeed\n"
        "补码：https://pan.baidu.com/s/1BaiduNeed?pwd=cccc\n"
    )
    resources = build_batch(got)
    assert len(got.hits) == 5 and len(resources) == 3
    assert [r.key for r in resources] == ["baidu:1BaiduOK", "quark:pan.quark.cn/s/qalive",
                                          "baidu:1BaiduNeed"]
    assert resources[0].pwd == "aaaa"          # 两个码都有：取最先出现的
    assert resources[2].pwd == "cccc"          # 先出现的没码：取第一个出现的码


def test_collected_extend_dedupes_unrecognised():
    a = Collected(unrecognised=["x"])
    a.extend(Collected(hits=[RawHit(source="t", kind="t", url="https://pan.quark.cn/s/q")],
                       unrecognised=["x", "y"]))
    assert a.unrecognised == ["x", "y"] and len(a.hits) == 1


# ---------------------------------------------------------------- 验活
async def test_verify_batch_counts_and_never_claims_unsupported_alive(fake_pool, seen):
    resources = build_batch(collect_hits(MIXED))
    progress: list[tuple[int, int]] = []
    async with fake_pool() as pool:
        report = await verify_batch(resources, pool,
                                    on_progress=lambda d, t: progress.append((d, t)))

    assert report.resources is resources
    status = {r.url: r.status for r in resources}
    assert status["https://pan.baidu.com/s/1BaiduOK"] is Status.ALIVE
    assert status["https://pan.baidu.com/s/1BaiduNeed"] is Status.NEED_PWD
    assert status["https://pan.baidu.com/s/1BaiduWrong"] is Status.WRONG_PWD
    assert status["https://pan.baidu.com/s/1BaiduDead"] is Status.NOT_FOUND
    assert status["https://pan.quark.cn/s/qdead"] is Status.NOT_FOUND
    assert status["https://pan.quark.cn/s/qunknown"] is Status.UNKNOWN
    unsupported = [r for r in resources if r.status is Status.UNSUPPORTED]
    # API 直链（arxiv）在搜索链路里被当成"有效"，批量验活里必须如实标"不支持"
    assert {r.pan_type for r in unsupported} == {PanType.XUNLEI, PanType.MAGNET, PanType.DIRECT}
    assert all("暂不支持验活" in (r.verify.note or "") for r in unsupported)
    assert not any(h in u for u in seen for h in ("xunlei", "arxiv", "magnet"))

    assert report.counts == {"total": 10, "alive": 3, "wrong_pwd": 1, "dead": 2,
                             "unsupported": 3, "unknown": 1, "cache_hit": 0}
    # 只有 存活 / 需提取码 算能用（与 search 的 --strict 口径一致），码不对的不算
    assert [r.url for r in report.alive] == [
        "https://pan.baidu.com/s/1BaiduOK", "https://pan.baidu.com/s/1BaiduNeed",
        "https://pan.quark.cn/s/qalive",
    ]
    assert progress[0][1] == 10 and progress[-1] == (10, 10)


async def test_verify_batch_uses_cache_unless_disabled(fake_pool, seen):
    async with fake_pool() as pool:
        await verify_batch(build_batch(collect_hits(MIXED)), pool)
        first = len(seen)

        again = build_batch(collect_hits(MIXED))
        counts = (await verify_batch(again, pool)).counts
        # 6 条有确定结论的走缓存；UNKNOWN 不进缓存，所以夸克抽风那条会再请求一次
        assert counts["cache_hit"] == 6
        assert len(seen) == first + 1
        assert summarize(again) == counts

        # --no-cache：一条缓存都不读，发出的请求与第一次（冷缓存）完全相同
        fresh = await verify_batch(build_batch(collect_hits(MIXED)), pool, use_cache=False)
        assert fresh.counts["cache_hit"] == 0
        assert len(seen) == 2 * first + 1


# ---------------------------------------------------------------- CLI：单条兼容
@pytest.mark.parametrize("args,code,expected", [
    (["https://pan.baidu.com/s/1BaiduOK", "-p", "abcd"], 0,
     "有效(码已验证)  百度网盘  method=share_verify  errno=0\n"),
    (["https://pan.baidu.com/s/1BaiduNeed"], 0,
     "有效(需提取码)  百度网盘  method=shorturlinfo  errno=-9\n"),
    (["https://pan.quark.cn/s/qalive", "--pwd", "abcd"], 0,
     "有效(码未验证)  夸克网盘  method=quark  errno=0\n"),
    (["https://pan.quark.cn/s/qdead", "--no-cache"], 0,
     "不存在  夸克网盘  method=quark  errno=41006  分享不存在\n"),
    (["https://pan.xunlei.com/s/VNxunlei123"], 2,
     "迅雷云盘 暂不支持验活（接口需要验证码或已变更）\n"),
    (["https://example.com/x"], 1,
     "无法识别的链接（支持百度网盘 / 夸克 / 阿里云盘 / 115 / 天翼189）\n"),
])
def test_single_url_output_and_exit_code_unchanged(fake_pool, args, code, expected):
    result = CliRunner().invoke(cli.app, ["verify", *args])
    assert result.exit_code == code
    assert result.output == expected


# ---------------------------------------------------------------- CLI：批量
def test_cli_batch_table_and_summary(fake_pool, wide):
    result = CliRunner().invoke(cli.app, [
        "verify", "https://pan.baidu.com/s/1BaiduOK?pwd=abcd", "https://pan.quark.cn/s/qalive",
        "https://pan.xunlei.com/s/VNxunlei123", "https://pan.quark.cn/s/qalive#dup",
        "https://example.com/nope",
    ])
    assert result.exit_code == 0, result.output
    out = result.output
    for header in ("#", "状态", "网盘", "链接", "提取码", "备注"):
        assert header in out
    rows = {line.split()[0]: line for line in out.splitlines()
            if line.strip()[:1].isdigit() and "http" in line}
    assert "有效(码已验证)" in rows["1"] and "https://pan.baidu.com/s/1BaiduOK?pwd=abcd" in rows["1"]
    assert "有效" in rows["2"] and "沙丘2 4K" in rows["2"]
    assert "未校验(该网盘不支持)" in rows["3"] and "有效" not in rows["3"]
    assert "共 3 条（去重合并 1 条）" in out
    assert "存活 2" in out and "不支持 1" in out and "无法识别 1" in out
    assert "https://example.com/nope" in out


def test_cli_alive_only_prints_only_live_links_to_stdout(fake_pool, wide):
    result = CliRunner().invoke(cli.app, ["verify", "-f", "-", "--alive-only"], input=MIXED)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "https://pan.baidu.com/s/1BaiduOK 提取码: abcd",
        "https://pan.baidu.com/s/1BaiduNeed",
        "https://pan.quark.cn/s/qalive",
    ]
    assert "存活 3" in result.stderr and "不支持 3" in result.stderr


def test_cli_single_url_with_export_flag_goes_batch_and_applies_pwd(fake_pool, wide):
    result = CliRunner().invoke(
        cli.app, ["verify", "https://pan.baidu.com/s/1BaiduOK", "-p", "abcd", "--alive-only"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "https://pan.baidu.com/s/1BaiduOK 提取码: abcd\n"


def test_cli_csv_export_is_escaped_and_round_trips(fake_pool, wide, tmp_path):
    src = tmp_path / "links.txt"
    src.write_text("https://pan.quark.cn/s/qformula\nhttps://pan.baidu.com/s/1BaiduOK 提取码: abcd\n"
                   "https://pan.quark.cn/s/qdead\n", encoding="utf-8")
    out_csv = tmp_path / "out.csv"
    result = CliRunner().invoke(cli.app, ["verify", "-f", str(src), "--csv", str(out_csv)])
    assert result.exit_code == 0, result.output

    with out_csv.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == ["状态", "网盘", "链接", "提取码", "标题", "备注"]
    assert rows[0]["标题"] == "'" + EVIL_TITLE          # 验活接口给的标题按纯文本落盘
    assert rows[0]["链接"] == "https://pan.quark.cn/s/qformula"
    assert rows[1]["链接"] == "https://pan.baidu.com/s/1BaiduOK?pwd=abcd"
    assert rows[1]["提取码"] == "abcd" and rows[1]["状态"] == "有效(码已验证)"
    assert rows[2]["状态"] == "不存在" and rows[2]["备注"] == "分享不存在"

    # 导出的 CSV 能直接喂回来复验；--json 的 url/pwd 同样能读回
    out_json = tmp_path / "again.json"
    result = CliRunner().invoke(cli.app, ["verify", "-f", str(out_csv), "--json", str(out_json)])
    assert result.exit_code == 0, result.output
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert [(r["url"], r["pwd"]) for r in payload["results"]] == [
        ("https://pan.quark.cn/s/qformula", None),
        ("https://pan.baidu.com/s/1BaiduOK?pwd=abcd", "abcd"),
        ("https://pan.quark.cn/s/qdead", None),
    ]
    assert payload["results"][0]["title"] == EVIL_TITLE
    assert [r["cached"] for r in payload["results"]] == [True, True, True]
    assert payload["summary"] == {"total": 3, "alive": 2, "wrong_pwd": 0, "dead": 1,
                                  "unsupported": 0, "unknown": 0, "cache_hit": 3,
                                  "inputs": 3, "duplicates": 0, "unrecognised": 0}
    again = build_batch(collect_hits(out_json.read_text(encoding="utf-8")))
    assert [(r.key, r.pwd) for r in again] == [(r["key"], r["pwd"]) for r in payload["results"]]


def test_cli_json_reports_unrecognised_and_unsupported(fake_pool, wide, tmp_path):
    out_json = tmp_path / "r.json"
    result = CliRunner().invoke(cli.app, ["verify", "-f", "-", "--json", str(out_json)],
                                input=MIXED)
    assert result.exit_code == 0, result.output
    payload = json.loads(out_json.read_text(encoding="utf-8"))
    assert payload["unrecognised"] == ["https://example.com/page", "https://pan.baidu.com/"]
    assert payload["summary"]["unrecognised"] == 2
    unsupported = [r for r in payload["results"] if r["status"] == "unsupported"]
    assert len(unsupported) == 3 and not any(r["alive"] for r in unsupported)


@pytest.mark.parametrize("via_stdin", [False, True])
def test_cli_reads_gb18030_file_and_stdin(fake_pool, wide, tmp_path, via_stdin):
    """Windows 记事本 / cmd 管道常是 GBK：文件与 -f - 都按 UTF-8 → GB18030 解码。"""
    data = "夸克资源：https://pan.quark.cn/s/qalive 百度：pan.baidu.com/s/1BaiduOK 密码：abcd\n"
    if via_stdin:
        args, stdin = ["-f", "-"], data.encode("gb18030")
    else:
        src = tmp_path / "gbk.txt"
        src.write_bytes(data.encode("gb18030"))
        args, stdin = ["-f", str(src)], None
    result = CliRunner().invoke(cli.app, ["verify", *args, "--alive-only"], input=stdin)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["https://pan.quark.cn/s/qalive",
                                          "https://pan.baidu.com/s/1BaiduOK 提取码: abcd"]


# ---------------------------------------------------------------- CLI：参数错误
@pytest.mark.parametrize("args", [
    ["https://pan.baidu.com/s/1BaiduOK", "https://pan.quark.cn/s/qalive", "-p", "abcd"],
    ["-f", "-", "-p", "abcd"],
    ["https://pan.baidu.com/s/1BaiduOK", "-f", "-", "-p", "abcd"],
])
def test_cli_pwd_rejected_for_batch(fake_pool, seen, args):
    result = CliRunner().invoke(cli.app, ["verify", *args], input="https://pan.quark.cn/s/qalive")
    assert result.exit_code == 2
    assert "只能配合单条链接使用" in _flat(result.output)
    assert seen == []


def test_cli_without_any_input_is_usage_error(fake_pool):
    result = CliRunner().invoke(cli.app, ["verify"])
    assert result.exit_code == 2
    assert "请给出至少一条链接" in _flat(result.output)


def test_cli_missing_file(fake_pool, tmp_path):
    result = CliRunner().invoke(cli.app, ["verify", "-f", str(tmp_path / "nope.txt")])
    assert result.exit_code == 1
    assert "无法读取" in result.output


def test_cli_nothing_recognisable(fake_pool, wide, seen):
    result = CliRunner().invoke(cli.app, ["verify", "-f", "-"],
                                input="没有链接\nhttps://example.com/a\n")
    assert result.exit_code == 1
    assert "没有识别到可校验的网盘链接" in result.output
    assert "无法识别 1 条" in result.output and "https://example.com/a" in result.output
    assert seen == []
