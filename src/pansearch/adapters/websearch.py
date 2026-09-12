"""B 环：通用搜索引擎定向检索 —— 捞聚合站索引不到的"野链接"。

两阶段策略（关键）：
  阶段 1  用搜索引擎找到「讨论这个资源的页面」（论坛帖 / 博客 / 资源站）
  阶段 2  抓取这些页面，从中抽取 pan.baidu.com 链接

为什么要两阶段：实测绝大多数搜索引擎**并不索引** `pan.baidu.com/s/xxx` 本身
（Google / Bing / DDG / Mojeek / Startpage / Baidu 对 `site:pan.baidu.com/s`
一律返回 0 条），但它们索引"资源讨论页"，链接就藏在那些页面里。

⚠️ 实测（2026-09-12，关键词「周杰伦」「三体」）：
    brave   直接命中 20 条 ✅ 主力，但频繁 429，需长间隔
    so(360) 偶有直接命中
    google / bing / ddg / mojeek / startpage / baidu -> 0 条，已默认关闭
"""

from __future__ import annotations

import asyncio
import urllib.parse
from urllib.parse import urlparse

import httpx

from ..extract import extract_from_text, html_to_text
from ..models import RawHit
from ..normalize import URL_RE
from ..util import RateLimiter
from .base import Adapter, register

DEFAULT_ENGINES = {
    # 实测（2026-09）可用性：
    #   bing / duckduckgo-html  200，结果页里直接含 pan.baidu 链接 ✅
    #   brave                   429（长期限流）
    #   so.com                  200 但只有 9.8KB 的反爬空壳页 ❌
    #   startpage/marginalia/searx/yandex/ecosia  200 但零网盘结果或 403 ❌
    "bing": "https://cn.bing.com/search?q={q}",
    "ddg": "https://html.duckduckgo.com/html/?q={q}",
}

DEFAULT_TEMPLATES = [
    "site:pan.baidu.com/s {kw}",
    '"{kw}" 百度网盘 提取码',
    '"{kw}" 百度网盘 下载 提取码',
]

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 阶段 2 不该去抓的域名（搜索引擎自身 / 自家 CDN / 统计 / 导航站）
# 注意：不要排除整个 baidu.com —— 贴吧(tieba.baidu.com)正是高价值内容源
_NOISE_HOSTS = (
    "google.", "gstatic.com", "googleapis.com", "googlesyndication",
    "bing.com", "bing.net", "brave.com", "search.brave",
    "so.com", "360.cn", "360.com", "360kan.com", "qhimg.com", "qhmsg.com",
    "www.baidu.com", "baidustatic.com", "bdstatic.com", "hao123.com",
    "duckduckgo.com", "mojeek.com", "startpage.com", "yandex.", "yimg.com",
    "w3.org", "schema.org", "cloudflare.com", "cloudflareinsights",
    "facebook.com", "twitter.com", "x.com", "youtube.com", "doubleclick.net",
    "wikipedia.org", "creativecommons.org", "mozilla.org", "apple.com",
    "alicdn.com", "aliyun.com", "gtimg.com", "qq.com", "sina.com.cn",
    "googletagmanager.com", "google-analytics.com", "cnzz.com", "umeng.com",
)
_NOISE_EXT = (
    ".css", ".js", ".mjs", ".json", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".woff", ".woff2", ".ttf", ".mp4", ".webp", ".xml", ".txt",
)


@register
class WebSearchAdapter(Adapter):
    name = "websearch"
    kind = "websearch"

    @property
    def engines(self) -> dict[str, str]:
        return self.cfg.get("engines") or DEFAULT_ENGINES

    @property
    def templates(self) -> list[str]:
        return self.cfg.get("templates") or DEFAULT_TEMPLATES

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        qps = float(self.cfg.get("rate_limit_qps") or 0.5)
        self.limiters = {name: RateLimiter(qps) for name in self.engines}
        self.sem = asyncio.Semaphore(int(self.cfg.get("concurrency") or 2))
        self.fetch_pages = bool(self.cfg.get("fetch_result_pages", True))
        self.max_pages = int(self.cfg.get("max_pages") or 6)
        self.page_timeout = float(self.cfg.get("page_timeout") or 12)
        self.page_sem = asyncio.Semaphore(int(self.cfg.get("page_concurrency") or 4))
        # 被限流的引擎在本轮搜索内直接跳过，避免无谓等待
        self._cooled: set[str] = set()

    async def search(self, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        self._cooled = set()
        jobs = [
            self._one(engine, url_tmpl, template, kw, client)
            for engine, url_tmpl in self.engines.items()
            for template in self.templates
        ]
        results = await asyncio.gather(*jobs, return_exceptions=True)

        direct: list[RawHit] = []
        candidate_pages: list[str] = []
        seen_urls: set[str] = set()
        seen_pages: set[str] = set()

        for result in results:
            if isinstance(result, BaseException) or not result:
                continue
            hits, pages = result
            for hit in hits:
                if hit.url in seen_urls:
                    continue
                seen_urls.add(hit.url)
                direct.append(hit)
            for page in pages:
                if page in seen_pages:
                    continue
                seen_pages.add(page)
                candidate_pages.append(page)

        # 阶段 2：抓内容页
        if self.fetch_pages and candidate_pages:
            deep = await self._fetch_pages(candidate_pages[: self.max_pages], client)
            for hit in deep:
                if hit.url in seen_urls:
                    continue
                seen_urls.add(hit.url)
                direct.append(hit)

        return direct

    async def _one(
        self,
        engine: str,
        url_tmpl: str,
        template: str,
        kw: str,
        client: httpx.AsyncClient,
    ) -> tuple[list[RawHit], list[str]]:
        if engine in self._cooled:
            return [], []

        query = template.format(kw=kw)
        url = url_tmpl.format(q=urllib.parse.quote(query))

        async with self.sem:
            await self.limiters[engine].acquire()
            try:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": UA,
                        "Accept": "text/html,application/xhtml+xml",
                        "Accept-Language": "zh-CN,zh;q=0.9",
                    },
                )
            except httpx.HTTPError:
                return [], []
        if resp.status_code in (429, 403):
            self._cooled.add(engine)   # 本轮不再打这个引擎
            return [], []
        if resp.status_code != 200:
            return [], []

        hits = extract_from_text(html_to_text(resp.text), source=engine, kind="websearch", origin=url)
        hits += extract_from_text(resp.text, source=engine, kind="websearch", origin=url)
        pages = self._candidate_pages(resp.text)
        return hits, pages

    def _candidate_pages(self, html: str) -> list[str]:
        """从搜索结果页里挑出值得进一步抓取的内容页 URL。"""
        out: list[str] = []
        per_host: dict[str, int] = {}
        for raw in URL_RE.findall(html):
            url = raw.rstrip("。，、；;!！?？'\"")
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            if not host or parsed.scheme not in ("http", "https"):
                continue
            if any(noise in host for noise in _NOISE_HOSTS):
                continue
            if parsed.path.lower().endswith(_NOISE_EXT):
                continue
            if "pan.baidu.com" in host:
                continue          # 直接命中已由 extract_from_text 处理
            # 只抓有具体路径的页面：首页/栏目页几乎不会是资源帖
            if parsed.path.strip("/") == "":
                continue
            if per_host.get(host, 0) >= 2:      # 同一站点最多 2 个页面，保证多样性
                continue
            per_host[host] = per_host.get(host, 0) + 1
            out.append(url)
        return out

    async def _fetch_pages(self, pages: list[str], client: httpx.AsyncClient) -> list[RawHit]:
        async def one(page: str) -> list[RawHit]:
            async with self.page_sem:
                try:
                    resp = await client.get(
                        page,
                        timeout=self.page_timeout,
                        headers={
                            "User-Agent": UA,
                            "Accept": "text/html,application/xhtml+xml",
                            "Accept-Language": "zh-CN,zh;q=0.9",
                        },
                    )
                except httpx.HTTPError:
                    return []
            if resp.status_code != 200:
                return []
            text = html_to_text(resp.text)
            return extract_from_text(text, source="page", kind="forum", origin=page) + extract_from_text(
                resp.text, source="page", kind="forum", origin=page
            )

        results = await asyncio.gather(*(one(p) for p in pages), return_exceptions=True)
        out: list[RawHit] = []
        for result in results:
            if isinstance(result, BaseException):
                continue
            out.extend(result)
        return out
