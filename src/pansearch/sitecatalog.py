r"""资源站目录：数据驱动的站点清单 + 搜索模式探测 + 健康度。

为什么要数据驱动：
    早先 sitesearch 适配器的站点是硬编码在源码里的 7 个（VST/软件方向）。
    要覆盖"所有领域"，站点清单必须能持续扩充且不需要改代码 ——
    所以搬到 config/sites.yaml，并配一个**自动探测**工具：
    给一个域名，试常见搜索 URL 模板，用"真词 vs 无意义串"的对照法判断哪个模板真的能搜。

目录结构（config/sites.yaml）：
    sites:
      - name: ghxi
        domain: www.ghxi.com
        verticals: [software]
        search: "https://www.ghxi.com/?s={q}"
        result_re: 'https://www\.ghxi\.com/[a-z0-9-]+\.html'
        verified: true          # 由 probe 验证过
"""

from __future__ import annotations

import asyncio
import random
import re
import string
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from .config import CONFIG_DIR

CATALOG_PATH = CONFIG_DIR / "sites.yaml"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 常见搜索 URL 模板，{q} 会被替换成 URL 编码后的查询词。
# 覆盖 WordPress / Discuz / DedeCMS / 帝国CMS / 自研等常见形态。
SEARCH_PATTERNS: tuple[str, ...] = (
    "/?s={q}",
    "/search?q={q}",
    "/search?keyword={q}",
    "/search?wd={q}",
    "/search?word={q}",
    "/search/?q={q}",
    "/search/{q}",
    "/so/{q}",
    "/index.php?search={q}",
    "/?keyword={q}",
    "/search.php?mod=forum&srchtxt={q}",
    "/search.php?keyword={q}",
    "/e/search/?searchget=1&tbname=news&keyboard={q}",
    # DataLife Engine（audioz / 4download 等一大批资源站用的就是它）
    "/index.php?do=search&subaction=search&story={q}",
    "/?do=search&subaction=search&story={q}",
    # MacCMS / 苹果 CMS —— 中文影视站的事实标准，之前完全没有，导致影视类全军覆没
    "/vodsearch/-------------.html?wd={q}",
    "/index.php/vod/search.html?wd={q}",
    "/index.php/vod/search/wd/{q}.html",
    "/vodsearch.html?wd={q}",
    "/search.php?searchword={q}",
    # 其他常见形态
    "/search.php?q={q}",
    "/find?q={q}",
    "/search?query={q}",
    "/?search={q}",
    "/search/{q}.html",
    "/search/{q}/",
)

# 探测用的"一定有结果"的高频词，按站点语言/领域分组
PROBE_TERMS: dict[str, list[str]] = {
    "zh": ["微信", "电影", "教程"],
    "en": ["download", "windows", "audio"],
}

# 垂直领域标签
VERTICALS: tuple[str, ...] = (
    "movie", "tv", "anime", "music", "ebook", "course", "software",
    "game", "design", "academic", "comic", "audio-tool", "general",
)

_SKIP_PATH = (
    # 静态资源
    "/static/", "/assets/", "/css/", "/js/", "/img/", "/images/", "/uploads/",
    "/templates/", "/fonts/", "/media/",
    # WordPress / 常见 CMS 自身页面
    "/wp-content/", "/wp-includes/", "/wp-json", "/wp-admin", "/wp-login",
    "/xmlrpc", "/comments", "/category/", "/tag/", "/author/", "/page/",
    "/feed", "/rss", "/sitemap",
    # 站点功能页
    "/about", "/contact", "/privacy", "/terms", "/dmca", "/disclaimer",
    "/login", "/register/", "/logout", "/search", "/user/", "/member/",
    "/cart", "/checkout", "/my-account", "/faq", "/help",
    # 插件/模板目录
    "/plugin/", "/plugins/", "/template/", "/themes/", "/engine/",
)
# 像内容详情页的路径：数字 ID、.html 结尾、常见栏目、或 WP 风格 slug（多段连字符）
_DETAIL_RE = re.compile(r'href="(?:https?://[^/"]+)?(/[^"#?]{3,})"')
# WordPress 风格 slug，允许一层栏目前缀：
#   /amazound-ppg-storm-for-kontakt/   （无前缀）
#   /comic/santi-huanchuangweilai      （/comic 前缀）—— 之前只认单段，漏掉一大批站
_SLUG_RE = re.compile(r"^/(?:[a-z0-9-]+/)?[a-z0-9]+(?:-[a-z0-9]+){1,}/?$")


def count_result_links(html: str) -> int:
    """粗略数出页面里"像内容详情页"的站内链接数量。

    这是探测器的核心判据，必须认得几种常见永久链接形态：
      * /12345.html（DedeCMS / 帝国CMS）
      * /archives/1234（Hexo / Typecho）
      * /amazound-ppg-storm-for-kontakt/（WordPress slug）
    """
    seen: set[str] = set()
    for path in _DETAIL_RE.findall(html):
        low = path.lower()
        if any(bad in low for bad in _SKIP_PATH):
            continue
        is_detail = (
            bool(re.search(r"/\d{3,}", path))
            or low.endswith((".html", ".htm"))
            or bool(re.search(r"/(?:post|article|archives?|thread|read|book|game|movie|music|download)/", low))
            or bool(_SLUG_RE.match(low))
        )
        if is_detail:
            seen.add(path)
    return len(seen)


_PH_DIGIT = "\x01"
_PH_WORD = "\x02"
_EXT_RE = re.compile(r"\.(html?|php|aspx?|jsp)$")


def _path_shape(path: str) -> str | None:
    r"""把一条详情页路径压成"形状"正则，例如：
        /bilibilispxzq.html   -> /[a-z0-9-]+\.html
        /15884.html           -> /\d+\.html
        /amazound-ppg-storm/  -> /[a-z0-9-]+/
        /archives/1234        -> /archives/\d+

    必须**一次分词**：先替换数字再替换单词，第二次会把第一次插入的
    "[a-z0-9-]+" 里的字母又匹配一遍，产出 "[a-z0-9-]+\[a-z0-9-]++-..." 这种垃圾。
    所以用占位符，最后统一还原。
    """
    low = path.lower().split("?")[0].split("#")[0]
    if any(bad in low for bad in _SKIP_PATH):
        return None

    # 文件后缀单独拆出来，避免 ".html" 被当成普通单词
    ext = ""
    m = _EXT_RE.search(low)
    if m:
        ext = re.escape(m.group(0))
        low = low[: m.start()]

    # 顺序很重要：**先替换单词**（[a-z0-9_-] 里已含数字，整条 slug 会变成一个占位符），
    # 再替换剩下的纯数字。反过来的话 "omnisphere-3-omnisphere-3" 会被数字切断成碎片。
    tmp = re.sub(r"[a-z][a-z0-9_-]*", _PH_WORD, low)
    tmp = re.sub(r"\d+", _PH_DIGIT, tmp)
    if _PH_DIGIT not in tmp and _PH_WORD not in tmp:
        return None

    out: list[str] = []
    for ch in tmp:
        if ch == _PH_DIGIT:
            out.append(r"\d+")
        elif ch == _PH_WORD:
            out.append("[a-z0-9-]+")
        else:
            out.append(re.escape(ch))
    return "".join(out) + ext


def derive_result_re(html: str, base: str) -> str:
    """从搜索结果页自动推导"详情页链接"的正则。

    探测器能找出可用的搜索 URL，但没有这个正则，站点就用不了 ——
    没有它等于目录建不起来。做法是统计页面上详情链接的"形状"出现次数，
    取最常见的那个形状作为正则。
    """
    shapes: dict[str, int] = {}
    for path in _DETAIL_RE.findall(html):
        shape = _path_shape(path)
        if shape:
            shapes[shape] = shapes.get(shape, 0) + 1
    if not shapes:
        return ""
    best = max(shapes.items(), key=lambda kv: kv[1])[0]
    host = re.escape(base.replace("https://", "").replace("http://", "").rstrip("/"))
    return f"https://{host}{best}"


def count_share_links(html: str) -> int:
    """数一数页面里有几条**网盘 / 磁力**链接。

    这是探测器的第二道闸门，比"能搜出结果"重要得多：
    实测 ikanbot / rytv 是在线播放站、assrt 是字幕站 —— 搜索完全正常，
    但详情页里根本没有网盘链接，收进目录纯属占位浪费请求。
    """
    from .normalize import MAGNET_RE, URL_RE
    from .normalize import detect_pan_type

    found: set[str] = set()
    for raw in URL_RE.findall(html):
        url = raw.rstrip("。，、；;!！?？'\"")
        if detect_pan_type(url).value != "other":
            found.add(url)
    for m in MAGNET_RE.findall(html):
        found.add(m)
    return len(found)


def _garbage(n: int = 10) -> str:
    return "".join(random.choices(string.ascii_lowercase, k=n))


@dataclass
class SiteEntry:
    name: str
    domain: str
    search: str = ""
    result_re: str = ""
    verticals: list[str] = field(default_factory=list)
    verified: bool = False
    note: str = ""

    @property
    def host(self) -> str:
        return self.domain.split("//")[-1].strip("/")

    def to_dict(self) -> dict:
        out = {"name": self.name, "domain": self.domain}
        if self.verticals:
            out["verticals"] = self.verticals
        if self.search:
            out["search"] = self.search
        if self.result_re:
            out["result_re"] = self.result_re
        out["verified"] = self.verified
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_dict(cls, d: dict) -> SiteEntry:
        return cls(
            name=str(d.get("name") or d.get("domain") or ""),
            domain=str(d.get("domain") or ""),
            search=str(d.get("search") or ""),
            result_re=str(d.get("result_re") or ""),
            verticals=[str(v) for v in (d.get("verticals") or [])],
            verified=bool(d.get("verified")),
            note=str(d.get("note") or ""),
        )


def load_catalog(path: str | Path | None = None) -> list[SiteEntry]:
    file = Path(path or CATALOG_PATH)
    if not file.exists():
        return []
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return [SiteEntry.from_dict(d) for d in (data.get("sites") or [])]


def save_catalog(sites: list[SiteEntry], path: str | Path | None = None) -> Path:
    file = Path(path or CATALOG_PATH)
    payload = {
        "sites": [s.to_dict() for s in sorted(sites, key=lambda x: (x.verticals[:1], x.domain))],
    }
    file.write_text(
        "# 资源站目录（由 pansearch sites probe 生成/更新）\n"
        "# verticals 决定查询时是否命中该站；search 里的 {q} 会被替换成 URL 编码后的关键词。\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=200),
        encoding="utf-8",
    )
    return file


@dataclass
class ProbeResult:
    domain: str
    ok: bool
    search: str = ""
    template: str = ""
    real_hits: int = 0
    noise_hits: int = 0
    verticals: list[str] = field(default_factory=list)
    result_re: str = ""
    link_yield: int = 0          # 详情页里抽到多少条网盘/磁力链接（0 = 这站对我们没用）
    error: str = ""


async def probe_domain(
    client: httpx.AsyncClient,
    domain: str,
    *,
    terms: list[str] | None = None,
    patterns: tuple[str, ...] = SEARCH_PATTERNS,
    min_hits: int = 3,
    min_gain: int = 3,
) -> ProbeResult:
    """探测一个域名的可用搜索 URL 模板。

    判据是**对照法**：真词的结果条目要明显多于无意义串，也要明显多于首页
    —— 只看"页面里有没有关键词"会误判（很多站不回显关键词）。
    """
    base = domain if domain.startswith("http") else f"https://{domain}"
    base = base.rstrip("/")
    host = base.split("//", 1)[-1]
    probe_terms = terms or (["微信", "电影", "教程"] if _is_cjk_domain(host) else ["download", "windows", "audio"])

    try:
        home = await client.get(base, timeout=15)
    except Exception as exc:                      # noqa: BLE001
        return ProbeResult(domain=domain, ok=False, error=type(exc).__name__)
    if home.status_code != 200:
        return ProbeResult(domain=domain, ok=False, error=f"http:{home.status_code}")
    home_hits = count_result_links(home.text)

    import urllib.parse

    best: ProbeResult | None = None
    saw_pattern = False          # 找到过可用搜索模板（只是可能没通过价值校验）
    best_yield = 0
    noise = _garbage()
    for tpl in patterns:
        for term in probe_terms:
            real_url = base + tpl.format(q=urllib.parse.quote(term))
            noise_url = base + tpl.format(q=noise)
            try:
                real = await client.get(real_url, timeout=15)
                if real.status_code != 200:
                    continue
                r_hits = count_result_links(real.text)
                if r_hits < min_hits:
                    continue
                noisy = await client.get(noise_url, timeout=15)
                n_hits = count_result_links(noisy.text) if noisy.status_code == 200 else 0
            except Exception:                      # noqa: BLE001
                continue
            # 只跟噪声查询比。不能再减首页结果数 ——
            # 资源站首页本身就列一堆文章，减掉会把 looptorrent/423down 这类误杀。
            # 用"搜索页与首页长度差异"来挡"搜索页=首页"的 JS 站。
            gain = r_hits - n_hits
            # **只用对照法判定**（真词结果数 - 无意义串结果数）。
            # 不要再夹"与首页比较"的条件：JS 站无视查询参数、永远返回同一页，
            # gain 本来就是 0 已经被挡住；而与首页比长度/比条目数都太脆 ——
            # 实测 looptorrent 首页与搜索页的详情链接数恰好相同，就被误杀了。
            if gain >= min_gain:
                result_re = derive_result_re(real.text, base)
                if not result_re:
                    continue
                saw_pattern = True
                # 价值校验：跟进几个详情页，看里面到底有没有网盘/磁力链接。
                # 没有就说明这站不是我们要的类型（在线播放站、字幕站、教程站…）。
                yield_ = await _measure_link_yield(client, result_re, real.text)
                best_yield = max(best_yield, yield_)
                if yield_ <= 0:
                    continue
                cand = ProbeResult(
                    domain=host, ok=True,
                    search=base + tpl, template=tpl,
                    real_hits=r_hits, noise_hits=n_hits,
                    result_re=result_re, link_yield=yield_,
                )
                if best is None or cand.real_hits > best.real_hits:
                    best = cand
                    # 已经找到一个很可靠的模板就别再试剩下的
                    # （21 个模板 × 3 个探测词 = 最多 84 次请求/域名，早退能省一大半）
                    if cand.real_hits >= 20 and gain >= 10:
                        return best
    if best:
        return best
    # 区分两种失败：搜不出结果 vs 搜得出但详情页没有网盘链接（后者常是在线播放/字幕/教程站）
    return ProbeResult(
        domain=host, ok=False,
        error="no-share-links" if saw_pattern else "no-pattern",
    )


def _is_cjk_domain(host: str) -> bool:
    return any(host.endswith(tld) for tld in (".cn", ".com.cn", ".net.cn", ".org.cn", ".cc", ".me", ".top", ".xyz"))


def extract_detail_urls(html: str, result_re: str, base: str = "") -> list[str]:
    """从搜索页里挑出详情页 URL —— **绝对与相对 href 都要认**。

    实测踩过：derive_result_re 产出的正则是 "https://host/xxx" 形式，
    但很多站的搜索结果页用的是相对 href（href="/comic/xxx"），
    于是两边都匹配不到，站点被误判为"没有结果"。
    """
    from urllib.parse import urljoin

    out: list[str] = []
    try:
        pattern = re.compile(result_re, re.I)
    except re.error:
        return out

    for raw in pattern.findall(html):
        url = raw if isinstance(raw, str) else raw[0]
        url = url.rstrip("。，、；;!！?？'\"")
        if url and url not in out:
            out.append(url)

    # 相对形式：把正则的 scheme://host 前缀去掉再匹配一次
    path_re_src = re.sub(r"^https?://[^/]+", "", result_re)
    if path_re_src and path_re_src != result_re and base:
        try:
            path_re = re.compile(path_re_src, re.I)
        except re.error:
            return out
        for raw in path_re.findall(html):
            path = raw if isinstance(raw, str) else raw[0]
            path = path.rstrip("。，、；;!！?？'\"")
            if not path:
                continue
            url = urljoin(base, path)
            if url not in out:
                out.append(url)
    return out


async def _measure_link_yield(client: httpx.AsyncClient, result_re: str,
                              search_html: str, *, sample: int = 6) -> int:
    """跟进最多 sample 个详情页，统计里面的网盘/磁力链接数。"""
    # 从正则在正文中的位置推不出 base，所以直接用 URL 里的 host
    m = re.match(r"(https?://[^/]+)", result_re)
    base = m.group(1) if m else ""
    urls = extract_detail_urls(search_html, result_re, base)[:sample]
    total = 0
    for url in urls:
        try:
            resp = await client.get(url, timeout=15)
        except Exception:                      # noqa: BLE001
            continue
        if resp.status_code == 200:
            total += count_share_links(resp.text)
    return total


async def probe_many(
    domains: list[str],
    *,
    concurrency: int = 8,
    terms: list[str] | None = None,
    on_result=None,
) -> list[ProbeResult]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(
        follow_redirects=True, http2=True,
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
    ) as client:
        async def one(d: str) -> ProbeResult:
            async with sem:
                result = await probe_domain(client, d, terms=terms)
                if on_result:
                    on_result(result)
                return result

        return list(await asyncio.gather(*(one(d) for d in domains)))


__all__ = [
    "CATALOG_PATH", "PROBE_TERMS", "SEARCH_PATTERNS", "VERTICALS",
    "ProbeResult", "SiteEntry", "count_result_links", "load_catalog",
    "probe_domain", "probe_many", "save_catalog",
]


class SiteHealth:
    """站点健康度：记录每个站被打了多少次、出了多少条、最近一次错误。

    用途是**自动淘汰**：目录会越加越多，但总有一部分站会挂掉/改版/被墙。
    连续失败若干次的站会被暂时跳过，不用手工维护清单。
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS site_health (
        domain      TEXT PRIMARY KEY,
        attempts    INTEGER NOT NULL DEFAULT 0,
        hits        INTEGER NOT NULL DEFAULT 0,
        ok_runs     INTEGER NOT NULL DEFAULT 0,
        fails       INTEGER NOT NULL DEFAULT 0,
        last_hits   INTEGER NOT NULL DEFAULT 0,
        last_ok_at  REAL,
        last_error  TEXT,
        updated_at  REAL
    );
    """

    def __init__(self, path: str | Path | None = None):
        from .config import CACHE_DIR
        import sqlite3
        import time as _time
        self._time = _time
        self.path = Path(path or (CACHE_DIR / "site_health.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def record(self, domain: str, hits: int, error: str | None = None) -> None:
        now = self._time.time()
        row = self.conn.execute(
            "SELECT attempts, hits, ok_runs, fails FROM site_health WHERE domain = ?",
            (domain,),
        ).fetchone()
        attempts, total_hits, ok_runs, fails = row if row else (0, 0, 0, 0)
        attempts += 1
        total_hits += hits
        if error:
            fails += 1
        elif hits > 0:
            ok_runs += 1
            fails = 0                       # 成功一次就重置连续失败
        self.conn.execute(
            "INSERT OR REPLACE INTO site_health"
            " (domain, attempts, hits, ok_runs, fails, last_hits, last_ok_at, last_error, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (domain, attempts, total_hits, ok_runs, fails, hits,
             now if hits > 0 else None, error, now),
        )
        self.conn.commit()

    def disabled(self, max_consecutive_fails: int = 6) -> set[str]:
        rows = self.conn.execute(
            "SELECT domain FROM site_health WHERE fails >= ?", (max_consecutive_fails,)
        ).fetchall()
        return {r[0] for r in rows}

    def stats(self, limit: int = 50) -> list[tuple]:
        return self.conn.execute(
            "SELECT domain, attempts, hits, ok_runs, fails, last_hits, last_error"
            " FROM site_health ORDER BY hits DESC, attempts DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def close(self) -> None:
        self.conn.close()


__all__ += ["SiteHealth"]
