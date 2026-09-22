"""BT / 磁力站适配器 —— 磁力链接的直接产地。

为什么单独一层（而不是塞进 sitesearch）：
    sitesearch 的模型是"搜索页只有标题 → 跟详情页才知道下载入口"，所以它必须
    按站点写 result_re 正则、再并发抓详情页。BT 站不是这个形状：
      · 搜索页**就是**结果页，magnet 直接写在行内（实测 nyaa 75 条、dmhy 46 条，
        关键词「三体」）；
      · 标题信息在 magnet 的 dn= 参数或**同一行的标题链接**里，而不是页面 title ——
        不取它，结果会因为"标题=站点名"在打分阶段被判低相关而沉底；
      · 磁力站按做种数/体积排序，语义和网盘站不同。
    所以单独实现：搜索页直抽 + dn=/行内标题 + 可选跟进详情页。

实测站点可用性（2026-09-21）：
    nyaa.si        200 时 75 条 magnet（偶发 504，所以带一次重试）           ✅
    share.dmhy.org 46 条 magnet；它同时在 sites.yaml 里（那里能拿到标题），
                   这里保留是为了"目录没配好"的部署也能用，去重在 dedupe 层  ✅
    acg.rip        搜索页与详情页都没有 magnet（只有 .torrent 跳转）          ❌ 不收
    1337x 403 / btdig 429 / torrentz 空页 / sukebei 成人站                    ❌ 不收

配置见 config/sources.yaml 的 sources.btsearch。
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
import urllib.parse

import httpx

from ..extract import extract_from_text
from ..models import RawHit
from ..query import query_terms
from ..routing import classify
from ..util import RateLimiter
from .base import Adapter, publish_hits, register

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_SITES: list[dict] = [
    {
        "name": "nyaa",
        "search": "https://nyaa.si/?f=0&c=0_0&q={q}",
        # 单站超时：nyaa 实测要 9.6s（偶发 504 还要重试一次）。
        # 12s 是"跑得完 + 不把整源预算吃光"的折中 —— 补搜那几遍会并发打它。
        "timeout": 12,
        # audio-tool 也归进来：VST / 音源 / 采样包的 BT 发布大量在 nyaa
        "verticals": ["anime", "movie", "music", "game", "software", "audio-tool"],
    },
    {
        "name": "dmhy",
        "search": "https://share.dmhy.org/topics/list?keyword={q}",
        "verticals": ["anime", "music"],
    },
    {
        # 蜜柑计划：动画 BT，搜索页直出 41 条 magnet（实测 2026-09-21），零 Header。
        # 以前它只在 sites.yaml 里走"两阶段"路径（搜索页→详情页），多绕一层还更慢。
        "name": "mikan",
        "search": "https://mikanani.me/Home/Search?searchstr={q}",
        "verticals": ["anime"],
    },
    {
        # sukebei = nyaa 的成人分区。实测（2026-09-21）搜索页直出 magnet。
        # 默认**开启**；要过滤用 pansearch search --sfw（见 config/sources.yaml 的 adult_sources）。
        "name": "sukebei",
        "search": "https://sukebei.nyaa.si/?f=0&c=0_0&q={q}",
        "verticals": ["general", "anime", "movie"],
        "adult": True,
    },
]

_MAGNET_ATTR_RE = re.compile(r"magnet:\?[^\"'\s<>]+")
# HTML 里的 & 会被写成 &amp;，magnet 从页面抽出来时带着实体，两种形态都要认
_DN_RE = re.compile(r"[?&](?:amp;)?dn=([^&]+)", re.I)
_ANCHOR_RE = re.compile(r"<a\b[^>]*>(.*?)</a>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_ROW_SPLIT_RE = re.compile(r"</tr>", re.I)


def prepare_html(html: str) -> str:
    """抽出 magnet 之前先修两个坑，否则链接会被截断或丢掉标题参数。

    1. "&amp;dn=" 里的分号是抽取正则的中断字符 —— 不还原实体，链接只剩
       "magnet:?xt=urn:btih:HASH&"，dn/tr 全丢（实测 75 条 magnet 标题全空）。
    2. 裸的方括号同样会被当成"正文符号"排除，nyaa 的 dn=[字幕组] 就是这种。
    """
    def fix(m: re.Match) -> str:
        raw = m.group(0).replace("&amp;", "&")
        return raw.replace("[", "%5B").replace("]", "%5D")

    return _MAGNET_ATTR_RE.sub(fix, html)


def _magnet_title(url: str) -> str | None:
    """磁力的 dn= 就是发布名（发布组、分辨率、编码都在里面），比页面 title 有用。"""
    m = _DN_RE.search(url.replace("&amp;", "&"))
    if not m:
        return None
    raw = m.group(1)
    for _ in range(2):                     # dn 可能被编码两层
        decoded = urllib.parse.unquote_plus(raw)
        if decoded == raw:
            break
        raw = decoded
    name = raw.replace("+", " ").strip()
    return name or None


def row_titles(html: str) -> dict[str, str]:
    """行内标题映射：magnet 所在那一行里最长的链接文本。

    实测 dmhy 的 magnet 里 dn= 是空的（发布名只在行内 <a> 文本里），
    所以 dn 之外还需要这条兜底；nyaa 有 dn，但偶发缺失时同样受益。
    """
    out: dict[str, str] = {}
    for row in _ROW_SPLIT_RE.split(html):
        if "magnet:" not in row:
            continue
        magnets = _MAGNET_ATTR_RE.findall(row)
        if not magnets:
            continue
        best = ""
        for anchor in _ANCHOR_RE.finditer(row):
            text = html_mod.unescape(_TAG_RE.sub("", anchor.group(1))).strip()
            if len(text) > len(best):
                best = text
        if not best:
            continue
        for url in magnets:
            out.setdefault(url, best[:300])
    return out


@register
class BtSearchAdapter(Adapter):
    name = "btsearch"
    kind = "bt"
    # 注意：**不要**设 primary_only —— 实测多词查询（如 "Ayira Oba"）里
    # BT 站的结果恰恰来自补搜词那几遍（主查询太具体，nyaa/sukebei 一条不中）。
    # 关掉补搜会把 1075 条打成 0 条。慢的问题改用"放开限速、并行跑"解决（见 config）。

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.sites = list(self.cfg.get("sites") or DEFAULT_SITES)
        self.concurrency = int(self.cfg.get("concurrency") or 3)
        self.page_timeout = float(self.cfg.get("page_timeout") or 12)
        self.page_concurrency = int(self.cfg.get("page_concurrency") or 4)
        self.max_sites = int(self.cfg.get("max_sites") or 6)
        self.retries = int(self.cfg.get("retries") or 1)
        self.limiter = RateLimiter(float(self.cfg.get("rate_limit_qps") or 1.5))
        self.sem = asyncio.Semaphore(self.concurrency)
        self.page_sem = asyncio.Semaphore(self.page_concurrency)
        self.last_selected: list[str] = []

    # ------------------------------------------------------------------ 站点选择
    def select_sites(self, kw: str) -> list[dict]:
        """按查询的垂直领域挑站点；识别不出领域时全都打（BT 站对通用词也有货）。"""
        verticals = set(classify(kw))
        picked = []
        for site in self.sites:
            tags = set(site.get("verticals") or [])
            if not verticals or tags & verticals:
                picked.append(site)
        return picked[: self.max_sites]

    # ------------------------------------------------------------------ 抓取
    async def _get(self, client: httpx.AsyncClient, url: str, *, timeout: float | None = None
                   ) -> httpx.Response | None:
        """带一次重试的 GET：nyaa 偶发 504/429，直接放弃会白白丢掉 75 条 magnet。"""
        attempts = self.retries + 1
        last: httpx.Response | None = None
        for attempt in range(attempts):
            await self.limiter.acquire()
            try:
                resp = await client.get(
                    url,
                    timeout=timeout,
                    headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
                )
            except httpx.HTTPError:
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.5)
                continue
            if resp.status_code >= 500 or resp.status_code == 429:
                last = resp
                if attempt + 1 < attempts:
                    await asyncio.sleep(1.5)
                continue
            return resp
        return last

    async def search(self, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        sites = self.select_sites(kw)
        self.last_selected = [s.get("name", "") for s in sites]
        if not sites:
            return []

        results = await asyncio.gather(
            *(self._one_site(site, kw, client) for site in sites),
            return_exceptions=True,
        )
        hits: list[RawHit] = []
        seen: set[str] = set()
        for result in results:
            if isinstance(result, BaseException):
                continue                    # 单站失败不影响其它站
            for hit in result:
                if hit.url in seen:
                    continue
                seen.add(hit.url)
                hits.append(hit)
        return hits

    # ------------------------------------------------------------------ 单站
    async def _one_site(self, site: dict, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        name = site.get("name") or "bt"
        url = str(site["search"]).format(q=urllib.parse.quote(kw))
        site_timeout = float(site.get("timeout") or self.page_timeout)
        async with self.sem:
            resp = await self._get(client, url, timeout=site_timeout)
        if resp is None or resp.status_code != 200:
            return []

        hits = self._extract(resp.text, name, url)
        publish_hits(hits)

        # 第二阶段（只有配了 detail_re 的站需要）
        detail_re = site.get("detail_re")
        if detail_re:
            pages = self._detail_pages(resp.text, detail_re)[: int(site.get("max_pages") or 4)]
            if pages:
                deep = await asyncio.gather(
                    *(self._one_page(p, name, kw, client) for p in pages), return_exceptions=True)
                for batch in deep:
                    if isinstance(batch, BaseException):
                        continue
                    hits.extend(batch)
        return hits

    @staticmethod
    def _detail_pages(html: str, pattern: str) -> list[str]:
        try:
            rx = re.compile(pattern)
        except re.error:
            return []
        out: list[str] = []
        for m in rx.finditer(html):
            value = m.group(0)
            if value not in out:
                out.append(value)
        return out

    async def _one_page(self, page: str, name: str, kw: str,
                        client: httpx.AsyncClient) -> list[RawHit]:
        async with self.page_sem:
            resp = await self._get(client, page, timeout=self.page_timeout)
        if resp is None or resp.status_code != 200:
            return []
        return publish_hits(self._extract(resp.text, name, page))

    @staticmethod
    def _relevant(text: str | None, kw: str) -> bool:
        """详情页至少要有查询词，避免站点回退到"最新发布"。"""
        if not text:
            return False
        low = text.lower()
        return any(t in low for t in query_terms(kw))

    def _extract(self, html: str, name: str, origin: str) -> list[RawHit]:
        prepared = prepare_html(html)
        rows = row_titles(prepared)
        hits = extract_from_text(prepared, source=f"bt:{name}", kind=self.kind, origin=origin)
        out: list[RawHit] = []
        seen: set[str] = set()
        for hit in hits:
            if hit.url in seen:
                continue
            seen.add(hit.url)
            if hit.url.startswith("magnet:"):
                title = _magnet_title(hit.url) or rows.get(hit.url)
                if title:
                    hit = hit.model_copy(update={"title": title})
            out.append(hit)
        return out
