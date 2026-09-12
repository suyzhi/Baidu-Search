"""多网盘验活器 + 失效剔除 的测试（MockTransport，完全离线）。

锁住的实测结论：
  夸克     code 0=存活 / 41006=不存在 / HTTP 404=不存在（**不校验提取码**）
  阿里     200 无 code=存活 / code NotFound.ShareLink=不存在
  115      state true=存活 / 4100012=需码 / 4100008=码错 / 990002=不存在（**校验提取码**）
  天翼     <shareVO>=存活 / ShareInfoNotFound=不存在
  迅雷/UC/123/PikPay/磁力 -> unsupported（**≠ 有效**）
"""

from __future__ import annotations

import httpx
import pytest

from pansearch.dedupe import build_resources
from pansearch.models import RawHit, Status
from pansearch.store import VerifyCache
from pansearch.verifiers import VerifierPool, path_id, prune


def make_resource(url: str, pwd: str | None = None):
    res = build_resources([RawHit(source="t", kind="t", url=url, pwd=pwd)])
    assert res, f"链接未被识别: {url}"
    return res[0]


# ---------------------------------------------------------------- path_id
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://pan.quark.cn/s/cc1cb432f2cf", "cc1cb432f2cf"),
        ("https://www.alipan.com/s/BxHcDJNyfA3", "BxHcDJNyfA3"),
        ("https://www.aliyundrive.com/s/LFQNsMHnisJ", "LFQNsMHnisJ"),
        # 阿里链接可能带子路径，必须取 /s/ 之后那一段而不是最后一段
        ("https://www.alipan.com/s/KsnHinF22tu/folder/6604c6b87bfa", "KsnHinF22tu"),
        ("https://115cdn.com/s/swwfcbh3wrb", "swwfcbh3wrb"),
        ("https://115.com/s/swzjt593ztd", "swzjt593ztd"),
        ("https://cloud.189.cn/t/Vba2UbQ3myMv", "Vba2UbQ3myMv"),
    ],
)
def test_path_id(url, expected):
    assert path_id(url) == expected


# ---------------------------------------------------------------- pool 搭建
def make_pool(tmp_path, handler):
    pool = VerifierPool(
        cfg={"cache_ttl_hours": 6, "retries": 0, "concurrency": 4, "timeout": 5},
        cache=VerifyCache(tmp_path / "c.sqlite3"),
    )
    return pool


async def run_with(pool, request_handler, resource):
    await pool.__aenter__()
    await pool._client.aclose()
    await pool.baidu._client.aclose()
    pool._client = httpx.AsyncClient(transport=httpx.MockTransport(request_handler))
    pool.baidu._client = httpx.AsyncClient(transport=httpx.MockTransport(request_handler))
    pool.baidu._warmed = True
    try:
        return await pool.verify(resource)
    finally:
        await pool.__aexit__()


def static_handler(payload: dict):
    """按 URL 子串匹配返回预设响应。"""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key, (status_code, body, kind) in payload.items():
            if key in url:
                if kind == "json":
                    return httpx.Response(status_code, json=body)
                return httpx.Response(status_code, text=body)
        return httpx.Response(404, json={"code": "NotFound"})
    return handler


# ---------------------------------------------------------------- 夸克
async def test_quark_alive(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"sharepage/token": (200, {"status": 200, "code": 0, "data": {"stoken": "x"}}, "json")})
    out = await run_with(pool, handler, make_resource("https://pan.quark.cn/s/cc1cb432f2cf", "abcd"))
    assert out.status is Status.ALIVE
    assert out.method == "quark"
    # 夸克接口不校验提取码 -> 不能宣称"码已验证"
    assert out.pwd_verified is False


async def test_quark_not_found(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"sharepage/token": (404, {"status": 404, "code": 41006, "message": "分享不存在"}, "json")})
    out = await run_with(pool, handler, make_resource("https://pan.quark.cn/s/ZZZZnotexist999"))
    assert out.status is Status.NOT_FOUND


async def test_quark_business_code_not_found(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"sharepage/token": (200, {"status": 200, "code": 41006, "message": "分享不存在"}, "json")})
    out = await run_with(pool, handler, make_resource("https://pan.quark.cn/s/ZZZZnotexist999"))
    assert out.status is Status.NOT_FOUND


# ---------------------------------------------------------------- 阿里云盘
async def test_aliyun_alive(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"get_share_by_anonymous": (200, {"share_name": "沙丘", "expiration": ""}, "json")})
    out = await run_with(pool, handler, make_resource("https://www.alipan.com/s/BxHcDJNyfA3"))
    assert out.status is Status.ALIVE
    assert out.method == "aliyun"


async def test_aliyun_not_found(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"get_share_by_anonymous": (404, {"code": "NotFound.ShareLink", "message": "…"}, "json")})
    out = await run_with(pool, handler, make_resource("https://www.alipan.com/s/ZZZZnotexist99"))
    assert out.status is Status.NOT_FOUND


async def test_aliyun_expired_share_is_dead(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"get_share_by_anonymous": (200, {"share_name": "x", "expiration": "2020-01-01T00:00:00Z"}, "json")})
    out = await run_with(pool, handler, make_resource("https://www.alipan.com/s/BxHcDJNyfA3"))
    assert out.status is Status.DEAD


@pytest.mark.parametrize(
    "code,expected",
    [
        # 实测 aliyun 返回的是带点号的 ShareLink.Cancelled（HTTP 400）
        ("ShareLink.Cancelled", Status.DEAD),
        ("NotFound.ShareLink", Status.NOT_FOUND),
        ("ShareLink.Expired", Status.DEAD),
    ],
)
async def test_aliyun_code_contains_fallback(tmp_path, code, expected):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"get_share_by_anonymous": (400, {"code": code, "message": "x"}, "json")})
    out = await run_with(pool, handler, make_resource("https://www.alipan.com/s/zLHZL1RK4vg"))
    assert out.status is expected


async def test_aliyun_unknown_code_is_not_claimed_dead(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"get_share_by_anonymous": (400, {"code": "SomethingBrandNew"}, "json")})
    out = await run_with(pool, handler, make_resource("https://www.alipan.com/s/zLHZL1RK4vg"))
    assert out.status is Status.UNKNOWN


# ---------------------------------------------------------------- 115
async def test_115_alive_with_verified_password(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"share/snap": (200, {"state": True, "errno": 0, "data": {}}, "json")})
    out = await run_with(pool, handler, make_resource("https://115cdn.com/s/swwfcbh3wrb", "p9f2"))
    assert out.status is Status.ALIVE
    assert out.pwd_verified is True      # 115 会真正校验 receive_code


@pytest.mark.parametrize(
    "errno,expected",
    [(4100012, Status.NEED_PWD), (4100008, Status.WRONG_PWD), (990002, Status.NOT_FOUND)],
)
async def test_115_errno_table(tmp_path, errno, expected):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"share/snap": (200, {"state": False, "errno": errno}, "json")})
    out = await run_with(pool, handler, make_resource("https://115cdn.com/s/swwfcbh3wrb", "abcd"))
    assert out.status is expected


# ---------------------------------------------------------------- 天翼
async def test_tianyi_alive(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"getShareInfoByCodeV2": (200, "<shareVO><fileName>沙丘2</fileName></shareVO>", "text")})
    out = await run_with(pool, handler, make_resource("https://cloud.189.cn/t/Vba2UbQ3myMv"))
    assert out.status is Status.ALIVE


async def test_tianyi_not_found(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"getShareInfoByCodeV2": (400, '<?xml version="1.0"?><error><code>ShareInfoNotFound</code></error>', "text")})
    out = await run_with(pool, handler, make_resource("https://cloud.189.cn/t/ZZZZnotexist"))
    assert out.status is Status.NOT_FOUND


# ---------------------------------------------------------------- 不支持的网盘
@pytest.mark.parametrize(
    "url",
    [
        "https://pan.xunlei.com/s/VNshhdg5bL3QO86-DzVEgWRmA1",
        "https://drive.uc.cn/s/2a34f32e45584",
        "https://123pan.com/s/dU7jjv-obUHA",
        "magnet:?xt=urn:btih:abc123",
    ],
)
async def test_unsupported_pans_are_not_claimed_alive(tmp_path, url):
    pool = make_pool(tmp_path, None)

    async def handler(request):
        raise AssertionError("不支持验活的网盘不应该发起网络请求")

    out = await run_with(pool, handler, make_resource(url))
    assert out.status is Status.UNSUPPORTED
    assert "暂不支持" in (out.note or "")


def test_supports_matrix(tmp_path):
    pool = make_pool(tmp_path, None)
    from pansearch.models import PanType

    assert pool.supports(PanType.BAIDU)
    assert pool.supports(PanType.QUARK)
    assert pool.supports(PanType.ALIYUN)
    assert pool.supports(PanType.P115)
    assert pool.supports(PanType.TIANYI)
    assert not pool.supports(PanType.XUNLEI)
    assert not pool.supports(PanType.UC)
    assert not pool.supports(PanType.P123)
    assert not pool.supports(PanType.MAGNET)


# ---------------------------------------------------------------- 剔除失效
def _res(url, status, pwd=None):
    res = make_resource(url, pwd)
    from pansearch.models import VerifyResult

    res.verify = VerifyResult(status=status)
    return res


def test_prune_default_keeps_usable_and_unknown():
    items = [
        _res("https://pan.quark.cn/s/aaaaaaaaaaaa", Status.ALIVE),
        _res("https://pan.quark.cn/s/bbbbbbbbbbbb", Status.NEED_PWD),
        _res("https://pan.quark.cn/s/cccccccccccc", Status.WRONG_PWD),
        _res("https://pan.quark.cn/s/dddddddddddd", Status.UNSUPPORTED),
        _res("https://pan.quark.cn/s/eeeeeeeeeeee", Status.DEAD),
        _res("https://pan.quark.cn/s/ffffffffffff", Status.NOT_FOUND),
    ]
    kept, pruned = prune(items, strict=False)
    assert pruned == 2                                  # 只剔掉 DEAD / NOT_FOUND
    assert [r.status for r in kept] == [
        Status.ALIVE, Status.NEED_PWD, Status.WRONG_PWD, Status.UNSUPPORTED
    ]


def test_prune_strict_drops_everything_unverified():
    items = [
        _res("https://pan.quark.cn/s/aaaaaaaaaaaa", Status.ALIVE),
        _res("https://pan.quark.cn/s/bbbbbbbbbbbb", Status.NEED_PWD),
        _res("https://pan.quark.cn/s/cccccccccccc", Status.WRONG_PWD),
        _res("https://pan.quark.cn/s/dddddddddddd", Status.UNSUPPORTED),
        _res("https://pan.quark.cn/s/eeeeeeeeeeee", Status.UNKNOWN),
        _res("https://pan.quark.cn/s/ffffffffffff", Status.DEAD),
    ]
    kept, pruned = prune(items, strict=True)
    assert [r.status for r in kept] == [Status.ALIVE, Status.NEED_PWD]
    assert pruned == 4


# ---------------------------------------------------------------- 标题补全
async def test_verify_fills_missing_title_from_share_name(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler(
        {"get_share_by_anonymous": (200, {"share_name": "沙丘2.2160p.DV.HDR (2024)"}, "json")}
    )
    res = make_resource("https://www.alipan.com/s/BxHcDJNyfA3")
    assert not res.title
    await run_with(pool, handler, res)
    assert res.title == "沙丘2.2160p.DV.HDR (2024)"


async def test_verify_does_not_overwrite_existing_title(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"get_share_by_anonymous": (200, {"share_name": "网盘里的名字"}, "json")})
    res = make_resource("https://www.alipan.com/s/BxHcDJNyfA3")
    res.title = "索引里的标题"
    await run_with(pool, handler, res)
    assert res.title == "索引里的标题"


async def test_tianyi_extracts_filename_as_title_hint(tmp_path):
    pool = make_pool(tmp_path, None)
    xml = "<shareVO><fileName>沙丘2（2024）4K原盘REMUX</fileName></shareVO>"
    handler = static_handler({"getShareInfoByCodeV2": (200, xml, "text")})
    res = make_resource("https://cloud.189.cn/t/Vba2UbQ3myMv")
    out = await run_with(pool, handler, res)
    assert out.title_hint == "沙丘2（2024）4K原盘REMUX"
    assert res.title == "沙丘2（2024）4K原盘REMUX"


# ---------------------------------------------------------------- 统计聚合
async def test_stats_dead_counted_once(tmp_path):
    """回归：_bump 与 _tally 都加 dead 会导致失效数翻倍（摘要里 132 vs 实际 66）。"""
    pool = make_pool(tmp_path, None)
    handler = static_handler({"sharepage/token": (404, {"code": 41006}, "json")})
    await run_with(pool, handler, make_resource("https://pan.quark.cn/s/ZZZZnotexist999"))
    assert pool.stats["dead"] == 1
    assert pool.stats["checked"] == 1
    assert pool.stats["by_service"]["quark"]["dead"] == 1


async def test_stats_cache_hit_not_counted_as_request(tmp_path):
    pool = make_pool(tmp_path, None)
    handler = static_handler({"sharepage/token": (200, {"status": 200, "code": 0}, "json")})
    await run_with(pool, handler, make_resource("https://pan.quark.cn/s/cc1cb432f2cf"))
    assert pool.stats["checked"] == 1
    assert pool.stats["cache_hit"] == 0

    await run_with(pool, handler, make_resource("https://pan.quark.cn/s/cc1cb432f2cf"))
    assert pool.stats["checked"] == 1          # 没有增加
    assert pool.stats["cache_hit"] == 1
    assert pool.stats["alive"] == 2            # 但存活计数两次都算


# ---------------------------------------------------------------- 状态文案
def test_status_label_reflects_pwd_verification():
    from pansearch.models import VerifyResult

    res = make_resource("https://pan.quark.cn/s/cc1cb432f2cf", "abcd")
    res.verify = VerifyResult(status=Status.ALIVE, pwd_verified=False)
    assert res.status_label == "有效(码未验证)"

    res.verify = VerifyResult(status=Status.ALIVE, pwd_verified=True)
    assert res.status_label == "有效(码已验证)"

    res.verify = VerifyResult(status=Status.ALIVE)
    res.pwd = None
    assert res.status_label == "有效"
