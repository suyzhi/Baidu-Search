"""垂直资源站适配器：搜索站 → 跟进详情页 → 抽网盘/磁力链接。

为什么需要它：
    影视剧的网盘链接会直接贴在 TG 频道里，但**音乐制作插件（VST/音源）不是**。
    实测 Serum / Omnisphere / 大气合成器 在 21 万条 TG 索引里是 **0 条**，
    PanSou 的 65 个插件也几乎不覆盖 —— 这个垂直领域的资源发布在专业资源站上
    （AudioZ、LoopTorrent、4Download…），而且下载入口往往在**详情页**里，
    搜索页只有标题链接。

    所以这里复用 B 环的"两阶段"套路，但把候选页来源换成配置好的垂直站点：
      阶段 1  拉站点的搜索页，用站点专属正则挑出详情页 URL
      阶段 2  并发抓详情页（过相关性闸门），从中抽出网盘链接与磁力链接

配置见 config/sources.yaml 的 sources.sitesearch.sites。
"""

from __future__ import annotations

import asyncio
import re
import urllib.parse
from urllib.parse import urlsplit

import httpx

from ..extract import extract_from_text, html_to_text, page_title
from ..models import RawHit
from ..routing import GENERAL_VERTICAL, classify
from ..sitecatalog import SiteEntry, SiteHealth, load_catalog
from .base import Adapter, register

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 内置站点清单（可用 config 覆盖）。result_re 用来从搜索结果页挑详情页。
DEFAULT_SITES: list[dict] = [
    {
        "name": "audioz",
        "search": "https://audioz.download/?s={q}",
        # 真帖子形如 /software/300623-download_native-instruments-kontakt-8....html
        # 别写成 /\d{4}/\d{2}/ —— 那会匹配到归档日期页（/2026/09/01/）
        "result_re": r"https://audioz\.download/[a-z]+/\d+-download_[^\"'<>\s]+\.html",
    },
    {
        "name": "looptorrent",
        "search": "https://looptorrent.net/?s={q}",
        "result_re": r"https://looptorrent\.net/[a-z0-9][a-z0-9-]{6,}/",
    },
    {
        "name": "4download",
        "search": "https://4download.net/?s={q}",
        "result_re": r"https://4download\.net/[a-z0-9-]{6,}/",
    },
    {
        "name": "vstorrent",
        "search": "https://vstorrent.org/?s={q}",
        "result_re": r"https://vstorrent\.org/[a-z0-9-]{6,}/",
    },
    {
        "name": "ghxi",
        "search": "https://www.ghxi.com/?s={q}",
        "result_re": r"https://www\.ghxi\.com/[a-z0-9-]+\.html",
    },
    {
        "name": "423down",
        "search": "https://www.423down.com/?s={q}",
        "result_re": r"https://www\.423down\.com/\d+\.html",
    },
    {
        "name": "dayanzai",
        "search": "https://www.dayanzai.me/?s={q}",
        "result_re": r"https://www\.dayanzai\.me/[a-z0-9-]+\.html",
    },
]

# 挑详情页时要排除的路径（分类/标签/站点自身页面等）
# 实测踩过：/comments/ 与 /wp-json/ 会排在真详情页前面，把 max_pages 预算吃光
_SKIP_PATH_PARTS = (
    "/category/", "/tag/", "/author/", "/page/", "/about", "/contact",
    "/dmca", "/feed", "/rss", "/wp-content", "/wp-login", "/wp-json",
    "/wp-admin", "/wp-includes", "/comments", "/privacy", "/terms",
    "/disclaimer", "/software/win/", "/software/mac/", "/samples/",
    "/presets/", "/?s=", "/cart", "/checkout", "/my-account",
)


@register
class SiteSearchAdapter(Adapter):
    name = "sitesearch"
    kind = "forum"

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.max_pages = int(self.cfg.get("max_pages") or 6)
        self.page_timeout = float(self.cfg.get("page_timeout") or 12)
        self.page_sem = asyncio.Semaphore(int(self.cfg.get("page_concurrency") or 6))
        self.site_sem = asyncio.Semaphore(int(self.cfg.get("concurrency") or 3))
        self.max_sites = int(self.cfg.get("max_sites") or 12)
        self._health = SiteHealth() if self.cfg.get("health_tracking", True) else None
        # 最近一次实际使用的站点，便于排查
        self.last_selected: list[str] = []

    # ---- 站点选择：目录 + 垂直路由 ----
    def select_sites(self, kw: str) -> list[SiteEntry]:
        """按查询的垂直领域从目录里挑站点。

        覆盖"所有领域"意味着目录里会有几百个站，一次全打既慢又浪费；
        所以先 classify 出领域，只打相关站 + 通用站。
        """
        catalog = load_catalog()
        if not catalog:
            # 目录还没建时退回内置清单，保证可用
            catalog = [SiteEntry.from_dict(d) for d in DEFAULT_SITES]
            for entry in catalog:
                entry.verified = True

        disabled = self._health.disabled() if self._health else set()
        verticals = set(classify(kw)) or {GENERAL_VERTICAL}

        picked: list[SiteEntry] = []
        for entry in catalog:
            if not (entry.search and entry.result_re and entry.verified):
                continue
            if entry.host in disabled:
                continue
            site_v = set(entry.verticals) or {GENERAL_VERTICAL}
            if site_v & verticals or GENERAL_VERTICAL in site_v:
                picked.append(entry)

        # 配置里显式给的站点优先（便于临时测试某个站）
        explicit = self.cfg.get("sites")
        if explicit:
            picked = [SiteEntry.from_dict(d) for d in explicit]

        picked = picked[: self.max_sites]
        self.last_selected = [e.name for e in picked]
        return picked

    async def search(self, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        sites = self.select_sites(kw)
        jobs = [self._one_site(site, kw, client) for site in sites]
        results = await asyncio.gather(*jobs, return_exceptions=True)

        hits: list[RawHit] = []
        seen: set[str] = set()
        for site, result in zip(sites, results):
            if isinstance(result, BaseException):
                if self._health:
                    self._health.record(site.host, 0, f"{type(result).__name__}")
                continue
            if self._health:
                self._health.record(site.host, len(result or []))
            for hit in result or []:
                if hit.url in seen:
                    continue
                seen.add(hit.url)
                hits.append(hit)
        return hits

    async def _one_site(self, site: SiteEntry, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        name = site.name or site.host
        search_tpl = site.search
        result_re = site.result_re
        if not search_tpl or not result_re:
            return []
        url = search_tpl.format(q=urllib.parse.quote(kw))

        async with self.site_sem:
            try:
                resp = await client.get(
                    url,
                    headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
                )
            except httpx.HTTPError:
                return []
        if resp.status_code != 200:
            return []

        # 阶段 1：直接命中（搜索页偶尔就带网盘/磁力链接）+ 挑详情页
        hits = extract_from_text(resp.text, source=f"site:{name}", kind="forum", origin=url)
        pages = self._pick_pages(resp.text, result_re)

        # 阶段 2：跟进详情页
        if pages:
            hits.extend(await self._fetch_pages(pages, name, kw, client))
        return hits

    @staticmethod
    def _relevant(title: str | None, kw: str) -> bool:
        """详情页标题里至少要出现一个查询词。

        没有这道闸门会大量误收：实测中文软件站（果核剥壳/423Down/大眼仔）
        对 "Omnisphere" 根本没有结果，页面会回退显示"最新推荐"内容，
        于是 B站下载器、IObit 卸载工具这类页面被当成搜索结果收了进来。
        命中**任意**一个查询词即可（搜 "Xfer Serum" 时标题只有 "Serum" 也算对）。
        """
        if not title:
            return False
        low = title.lower()
        terms = [t for t in re.split(r"[\s,，、/|·]+", (kw or "").strip().lower()) if t]
        return any(t in low for t in terms)

    async def _fetch_pages(self, pages: list[str], name: str, kw: str,
                           client: httpx.AsyncClient) -> list[RawHit]:
        # 候选排序：**URL slug 里含查询词的排前面**。
        # 实测 audioz 的 ?s=Serum 会先返回一堆 Kontakt/Roland 页面（它把"最新"
        # 也混进结果），真正的 Serum 帖子排在后面，候选位会被垃圾占满。
        terms = [t for t in re.split(r"[\s,，、/|·]+", (kw or "").strip().lower()) if t]

        def slug_rank(url: str) -> int:
            low = url.lower()
            return 0 if any(t in low for t in terms) else 1

        ordered = sorted(pages, key=slug_rank)      # 稳定排序，同组保持原顺序
        # 多取候选、只保留**通过相关性闸门**的：站点搜索结果里常混着
        # 评论页/站点自身页面，直接截前 N 个会把预算浪费在垃圾上。
        candidates = ordered[: self.max_pages * 2]

        async def one(page: str) -> list[RawHit] | None:
            async with self.page_sem:
                try:
                    resp = await client.get(
                        page,
                        timeout=self.page_timeout,
                        headers={"User-Agent": UA,
                                 "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
                    )
                except httpx.HTTPError:
                    return None
            if resp.status_code != 200:
                return None
            if not self._relevant(page_title(resp.text), kw):
                return None
            text = html_to_text(resp.text)
            return (
                extract_from_text(text, source=f"site:{name}", kind="forum", origin=page)
                + extract_from_text(resp.text, source=f"site:{name}", kind="forum", origin=page)
            )

        # 分批抓、够了就停：一次性 gather 全部候选会让慢站一直等到最后，
        # 而相关性闸门通常会淘汰掉大半候选。
        out: list[RawHit] = []
        kept = 0
        chunk = max(1, int(self.cfg.get("page_concurrency") or 6))
        for i in range(0, len(candidates), chunk):
            batch = candidates[i:i + chunk]
            results = await asyncio.gather(*(one(p) for p in batch), return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) or result is None:
                    continue
                out.extend(result)
                kept += 1
            if kept >= self.max_pages:
                break
        return out

    @staticmethod
    def _pick_pages(html: str, result_re: str) -> list[str]:
        try:
            pattern = re.compile(result_re, re.I)
        except re.error:
            return []
        out: list[str] = []
        for raw in pattern.findall(html):
            url = raw if isinstance(raw, str) else raw[0]
            url = url.rstrip("。，、；;!！?？'\"")
            low = url.lower()
            if any(part in low for part in _SKIP_PATH_PARTS):
                continue
            if not urlsplit(url).path.strip("/"):     # 站点根 / 纯路径
                continue
            if url not in out:
                out.append(url)
        return out
