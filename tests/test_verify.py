"""验活逻辑测试 —— 用 MockTransport，完全离线。

锁住三个关键结论：
  1. shorturlinfo 用【完整 token（带开头 1）】
  2. share/verify  用【去掉开头 1】的 token
  3. errno 映射：0=码对 / -9=码错 / 105=不存在 / 140=不存在 / -9(shorturlinfo)=需码
"""

from __future__ import annotations

import httpx
import pytest

from pansearch.dedupe import build_resources
from pansearch.models import RawHit, Status
from pansearch.store import VerifyCache
from pansearch.verify import BaiduVerifier

SURL = "1AbCdEfGhIjKlMnOpQrStUv"
BARE = "AbCdEfGhIjKlMnOpQrStUv"


def make_resource(pwd: str | None = None):
    res = build_resources(
        [RawHit(source="t", kind="t", url=f"https://pan.baidu.com/s/{SURL}", pwd=pwd)]
    )
    assert res, "链接应能解析为百度资源"
    return res[0]


def make_verifier(tmp_path, handler):
    verifier = BaiduVerifier(
        cfg={
            "cache_ttl_hours": 6,
            "rate_limit_qps": 0,   # 测试中不限速
            "concurrency": 4,
            "timeout": 5,
            "retries": 0,
        },
        cache=VerifyCache(tmp_path / "cache.sqlite3"),
    )
    verifier._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    verifier._warmed = True
    return verifier


def record(seen: dict, share_errno=0, short_errno=2):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        params = dict(request.url.params)
        if "/share/verify" in url:
            seen["share_surl"] = params.get("surl")
            seen["share_pwd"] = request.content.decode().split("pwd=")[1].split("&")[0]
            return httpx.Response(200, json={"errno": share_errno})
        if "/api/shorturlinfo" in url:
            seen["short_surl"] = params.get("shorturl")
            return httpx.Response(200, json={"errno": short_errno})
        return httpx.Response(200, text="ok")

    return handler


async def test_share_verify_uses_stripped_token_and_detects_alive(tmp_path):
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen, share_errno=0))
    out = await verifier.verify(make_resource("abcd"))
    assert out.status is Status.ALIVE
    assert out.errno == 0
    assert seen["share_surl"] == BARE          # 关键：去掉了开头 1
    assert seen["share_pwd"] == "abcd"


async def test_shorturlinfo_uses_full_token(tmp_path):
    """回归：用去掉 1 的 token 会让短链信息接口恒返回 2（假阳性）。"""
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen, short_errno=-9))
    out = await verifier.verify(make_resource(None))
    assert out.status is Status.NEED_PWD
    assert seen["short_surl"] == SURL          # 关键：保留开头 1


async def test_wrong_password_falls_back_to_liveness(tmp_path):
    """-9 表示提取码错误；链接本身仍存活 -> wrong_pwd（不能判死）。"""
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen, share_errno=-9, short_errno=-9))
    out = await verifier.verify(make_resource("zzzz"))
    assert out.status is Status.WRONG_PWD
    assert out.errno == -9
    assert "提取码" in (out.note or "")


async def test_share_not_found(tmp_path):
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen, share_errno=105))
    out = await verifier.verify(make_resource("abcd"))
    assert out.status is Status.NOT_FOUND


async def test_shorturlinfo_140_is_not_found(tmp_path):
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen, short_errno=140))
    out = await verifier.verify(make_resource(None))
    assert out.status is Status.NOT_FOUND


async def test_unknown_errno_is_not_claimed_dead(tmp_path):
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen, share_errno=99999))
    out = await verifier.verify(make_resource("abcd"))
    assert out.status is Status.UNKNOWN


async def test_cache_hit_writes_back_to_resource(tmp_path):
    """回归：缓存命中必须写回 res.verify，否则会被显示成"未校验"。"""
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen, share_errno=0))
    res = make_resource("abcd")

    first = await verifier.verify(res)
    assert first.status is Status.ALIVE
    assert res.verify is first

    res2 = make_resource("abcd")
    second = await verifier.verify(res2)
    assert res2.verify is second
    assert second.status is Status.ALIVE
    assert (second.method or "").startswith("cache")


async def test_cache_key_includes_password(tmp_path):
    """同一链接、不同提取码必须分开缓存，否则会串味。"""
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen, share_errno=0))

    ok = make_resource("abcd")
    await verifier.verify(ok)

    bad = make_resource("zzzz")
    out = await verifier.verify(bad)
    # 若缓存键没带 pwd，这里会错误地命中 alive 缓存
    assert out.method == "share_verify"


async def test_non_baidu_is_unsupported(tmp_path):
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen))
    res = build_resources(
        [RawHit(source="t", kind="t", url="https://pan.quark.cn/s/251cd20497e6")]
    )[0]
    out = await verifier.verify(res)
    assert out.status is Status.UNSUPPORTED


@pytest.mark.parametrize(
    "short_errno,expected",
    [(0, Status.ALIVE),
     # errno=2 只会在"用去掉开头 1 的 token"时出现（对任何链接都返回 2），
     # 因此它是无区分度的假阳性信号 -> 保守判为 unknown，绝不宣称存活。
     (2, Status.UNKNOWN),
     (-9, Status.NEED_PWD), (-21, Status.NEED_PWD),
     (140, Status.NOT_FOUND), (-3, Status.DEAD)],
)
async def test_shorturlinfo_errno_table(tmp_path, short_errno, expected):
    seen: dict = {}
    verifier = make_verifier(tmp_path, record(seen, short_errno=short_errno))
    out = await verifier.verify(make_resource(None))
    assert out.status is expected
