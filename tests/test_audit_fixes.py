"""审计修复的回归测试（离线）。

这些 bug 原来都没有测试覆盖，所以修完也不会有人报警 —— 这里逐个锁住：
  1. 阿里云盘非 200 且无 code（429/5xx）被误判为"存活"并写进缓存
  2. libgen. / sci-hub. 前缀规则永远匹配不到 -> 链接被静默丢弃
  3. sources.yaml 的显式 sites: 覆盖了整份站点目录（垂直路由失效）
  4. 配置里的站点没有 domain -> 健康度全落在空键上
  5. verify.retries: 0 被 `or 2` 吃成 2（关不掉重试）
  6. /api/search?types=bogus 静默退化成"不过滤"（返回全部网盘）
  7. websearch 噪声域名用子串匹配，"x.com" 误杀 box.com / linux.com
  8. 验活缓存的 title_hint 不落库 -> 复搜时空标题被判成不相关
  9. 频道采收把邀请链接当频道名
"""

from __future__ import annotations

import httpx
import pytest

from pansearch.channelharvest import load_candidate_file
from pansearch.config import source_cfg
from pansearch.dedupe import build_resources
from pansearch.extract import extract_from_text
from pansearch.models import RawHit, Status, VerifyResult
from pansearch.normalize import detect_pan_type
from pansearch.store import VerifyCache
from pansearch.verifiers import VerifierPool


# ------------------------------------------------ 1. 阿里云盘非 200 不是 alive
def _pool(tmp_path, **cfg):
    base = {"cache_ttl_hours": 6, "retries": 0, "concurrency": 4, "timeout": 5}
    base.update(cfg)
    return VerifierPool(cfg=base, cache=VerifyCache(tmp_path / "c.sqlite3"))


async def _verify(pool, handler, resource):
    await pool.__aenter__()
    await pool._client.aclose()
    await pool.baidu._client.aclose()
    pool._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool.baidu._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool.baidu._warmed = True
    try:
        return await pool.verify(resource)
    finally:
        await pool.__aexit__()


def _json_handler(status_code: int, body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)
    return handler


async def test_aliyun_non_200_without_code_is_unknown_not_alive(tmp_path):
    pool = _pool(tmp_path)
    res = build_resources([RawHit(source="t", kind="t", url="https://www.alipan.com/s/BxHcDJNyfA3")])[0]
    out = await _verify(pool, _json_handler(429, {"message": "rate limited"}), res)
    assert out.status is Status.UNKNOWN, "被限流的检查不能显示成有效"
    assert pool.cache.get(res.key, res.pwd) is None, "假 ALIVE 不能写进缓存"


async def test_verify_retries_zero_is_respected(tmp_path):
    assert _pool(tmp_path, retries=0).retries == 0
    assert _pool(tmp_path, retries=3).retries == 3


# ------------------------------------------------ 2. libgen / sci-hub 识别
@pytest.mark.parametrize(
    "url",
    [
        "https://libgen.is/book/index.php?md5=abc",
        "https://libgen.li/index.php?req=x",
        "https://www.libgen.rs/book/index.php?md5=abc",
        "https://sci-hub.se/10.1000/xyz",
        "https://sci-hub.st/10.1000/xyz",
    ],
)
def test_libgen_and_scihub_are_direct_resources(url):
    assert detect_pan_type(url).value == "direct"


def test_libgen_links_are_extracted_not_dropped():
    hits = extract_from_text("下载 https://libgen.is/book/index.php?md5=abc", source="s", kind="forum")
    assert len(hits) == 1
    assert build_resources(hits), "识别为 direct 后必须能进入结果，而不是被当 other 丢掉"


# ------------------------------------------------ 3/4. 站点目录与健康度
def test_sitesearch_uses_catalog_vertical_routing():
    from pansearch.adapters.sitesearch import SiteSearchAdapter

    adapter = SiteSearchAdapter({"health_tracking": False})
    assert "vstorrent" in [s.name for s in adapter.select_sites("Serum 合成器")]
    assert "mikanani" in [s.name for s in adapter.select_sites("新番 anime")]
    assert "bookfere" in [s.name for s in adapter.select_sites("epub 电子书")]


def test_shipped_sitesearch_config_does_not_shadow_catalog():
    """sources.yaml 里不能再有 sites:（替换语义会让整份目录不可达）。"""
    assert not source_cfg("sitesearch").get("sites")


def test_site_entry_derives_host_from_search_template():
    from pansearch.sitecatalog import SiteEntry

    entry = SiteEntry.from_dict({"name": "audioz", "search": "https://audioz.download/?s={q}"})
    assert entry.host == "audioz.download", "没有 domain 的站点会让健康度全落在空键上"


# ------------------------------------------------ 5. webapp 类型参数
def test_webapp_rejects_unknown_pan_type(monkeypatch):
    from fastapi.testclient import TestClient

    from pansearch import pipeline, webapp

    captured: dict = {}

    async def fake_search(kw, **kwargs):
        captured.update(kwargs)
        return pipeline.SearchOutcome(keyword=kw, resources=[])

    monkeypatch.setattr(webapp, "run_search", fake_search)
    client = TestClient(webapp.app)

    assert client.get("/api/search", params={"kw": "沙丘", "types": "bogus"}).status_code == 422

    ok = client.get("/api/search", params={"kw": "沙丘", "types": "baidu"})
    assert ok.status_code == 200
    assert [t.value for t in captured["types"]] == ["baidu"]


# ------------------------------------------------ 6. 噪声域名边界匹配
def test_websearch_noise_host_uses_domain_boundaries():
    from pansearch.adapters.websearch import _is_noise_host

    assert _is_noise_host("www.bing.com")
    assert _is_noise_host("cn.bing.com")
    assert not _is_noise_host("box.com")
    assert not _is_noise_host("netflix.com")
    assert not _is_noise_host("linux.com")
    assert not _is_noise_host("mybox.com")
    # 内容页例外：公众号文章是"资源贴"的常见落点，但它挂在被整域排除的 qq.com 下
    assert not _is_noise_host("mp.weixin.qq.com"), "公众号文章必须能进阶段 2 抓取"
    assert _is_noise_host("im.qq.com")


# ------------------------------------------------ 7. title_hint 缓存
def test_verify_cache_persists_title_hint(tmp_path):
    cache = VerifyCache(tmp_path / "c.sqlite3")
    cache.put("k1", VerifyResult(status=Status.ALIVE, method="aliyun", title_hint="沙丘2 4K"))
    got = cache.get("k1")
    assert got is not None
    assert got.title_hint == "沙丘2 4K"
    cache.close()


async def test_warm_cache_refills_title(tmp_path):
    """冷启动补上的真实资源名，复搜（走缓存）时不能消失 ——
    空标题会被相关性闸门判成不相关（0.3 < 0.4）而丢掉。"""
    cache = VerifyCache(tmp_path / "c.sqlite3")
    handler = _json_handler(200, {"share_name": "沙丘2.2160p"})
    res1 = build_resources([RawHit(source="t", kind="t", url="https://www.alipan.com/s/BxHcDJNyfA3")])[0]
    pool1 = VerifierPool(cfg={"retries": 0, "timeout": 5}, cache=cache)
    await _verify(pool1, handler, res1)
    assert res1.title == "沙丘2.2160p"

    res2 = build_resources([RawHit(source="t", kind="t", url="https://www.alipan.com/s/BxHcDJNyfA3")])[0]
    pool2 = VerifierPool(cfg={"retries": 0, "timeout": 5}, cache=cache)
    out2 = await _verify(pool2, handler, res2)
    assert out2.method.startswith("cache")
    assert res2.title == "沙丘2.2160p", "缓存命中也要补标题"


# ------------------------------------------------ 8. 频道采收
def test_channel_candidates_skip_invite_links_keep_real_names(tmp_path):
    f = tmp_path / "cand.txt"
    f.write_text(
        "https://t.me/+AbCdEfGhIj\n"
        "t.me/joinchat/AAAAAE\n"
        "t.me/ivsky_pan\n"
        "@shareMovies\n"
        "realchannel\n",
        encoding="utf-8",
    )
    names = load_candidate_file(f)
    assert "ivsky_pan" in names and "shareMovies" in names and "realchannel" in names
    assert not any("http" in n for n in names), "邀请/私有链接不是可抓的频道名"
