"""C 环：B 站内搜 —— 视频简介 + 评论区 + 专栏摘要，网盘链接的真实产地。

为什么需要它（实测 2026-09）：
    影视剧的网盘链接大量出现在 **视频简介与评论区**（"链接：pan.baidu.com/s/xxx
    提取码：xxxx"），而这部分内容搜索引擎索引不全、PanSou 的 65 个插件基本不覆盖。
    实测「Omnisphere」在 B 站能搜到 20 条视频 + 20 篇专栏，评论区里就有资源线索。

为什么以前没接进来（关键）：
    B 站对搜索/评论接口强制 **WBI 签名**（w_rid + wts），裸请求一律 412；
    老评论接口 x/v2/reply 不带签名返回 0 条（实测 code=0 但 replies 为空），
    必须走 x/v2/reply/wbi/main。签名算法见 _WBISigner。

    另一个坑：不种 buvid3/buvid4 cookie 时搜索接口也会 412，所以先访问首页 +
    x/frontend/finger/spi 拿游客 cookie。注意 nav 接口即使未登录（code=-101）
    仍会返回 wbi_img，所以不能因为 code != 0 就放弃取 key。

三路产出（按性价比排序）：
    ① 搜索结果自带文本（视频标题/简介片段/标签、专栏摘要）—— 零额外请求
    ② 视频完整简介（x/web-interface/view，1 请求/视频）
    ③ 评论区（x/v2/reply/wbi/main，含置顶与楼中楼）—— 资源线索最集中的地方

    专栏正文接口需要浏览器渲染（read/cv* 只回 3.3KB 壳；x/article/view 已 -509），
    拿不到就只用摘要，不为它引入无头浏览器。

配置见 config/sources.yaml 的 sources.bilibili。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import urllib.parse

import httpx

from ..extract import extract_from_text
from ..models import RawHit
from ..query import query_terms
from ..util import RateLimiter
from .base import Adapter, publish_hits, register

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
API = "https://api.bilibili.com"

# WBI mixin key 置换表（B 站前端固定表，2023 起未变）
WBI_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
    26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
    20, 34, 44, 52,
]

# 搜索模板：第一条给原始关键词，后两条专门去捞"贴了网盘链接"的资源贴。
# 模板越多请求越多，默认三条刚好覆盖"正片 + 资源贴"两类意图。
DEFAULT_TEMPLATES = ["{kw}", "{kw} 百度网盘", "{kw} 资源 提取码"]

_TAG_RE = re.compile(r"<[^>]+>")

# 标题/简介里出现这些词，说明这条视频更可能"贴了资源"，值得优先跟详情。
# 实测（Omnisphere / AE模板）：跟 8 个视频只回 4 条链接，而带"资源/提取码/合计"的
# 帖子命中率明显更高 —— 详情预算有限，顺序比数量更重要。
_RESOURCE_HINTS = (
    "网盘", "资源", "下载", "合集", "提取码", "分享", "全集", "整合", "打包",
    "白嫖", "免费领取", "链接在", "简介处", "评论区", "安装包", "素材包",
)

# 资源模板搜出来的结果，本身就更可能是资源贴（"{kw} 百度网盘" / "{kw} 资源 提取码"）
_RESOURCE_TEMPLATE_HINTS = ("百度网盘", "网盘", "提取码", "资源")


def _plain(value: str | None) -> str:
    """搜索结果里的标题带 <em class="keyword"> 高亮标签，去掉再抽取。"""
    if not value:
        return ""
    return _TAG_RE.sub("", str(value))


class _WBISigner:
    """WBI 签名器：缓存 mixin key（约 30 分钟刷新一次）。

    签名步骤：
      1. GET /x/web-interface/nav 取 wbi_img.img_url / sub_url 的文件名
      2. 拼接后按 WBI_TAB 置换，取前 32 位 = mixin key
      3. params + wts 排序后 urlencode，w_rid = md5(query + mixin key)
    """

    def __init__(self, ttl: float = 1800.0):
        self._mixin: str | None = None
        self._at = 0.0
        self.ttl = ttl

    async def ensure(self, client: httpx.AsyncClient, limiter: RateLimiter) -> str:
        now = time.monotonic()
        if self._mixin and now - self._at < self.ttl:
            return self._mixin
        await limiter.acquire()
        resp = await client.get(
            f"{API}/x/web-interface/nav",
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
        )
        data = (resp.json() or {}).get("data") or {}
        wbi = data.get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise RuntimeError("B 站 nav 接口没有返回 wbi key（接口可能已变更）")
        raw = img + sub
        self._mixin = "".join(raw[i] for i in WBI_TAB if i < len(raw))[:32]
        self._at = now
        return self._mixin

    def sign(self, params: dict) -> str:
        if not self._mixin:
            raise RuntimeError("WBI mixin key 尚未初始化")
        p = dict(params)
        p["wts"] = int(time.time())
        query = urllib.parse.urlencode(
            [(k, re.sub(r"[!'()*]", "", str(v))) for k, v in sorted(p.items())]
        )
        p["w_rid"] = hashlib.md5((query + self._mixin).encode()).hexdigest()
        return urllib.parse.urlencode(sorted(p.items()))


@register
class BilibiliAdapter(Adapter):
    name = "bilibili"
    kind = "bilibili"
    # 每个视频都要跟简介 + 评论，慢；补搜词不值得再跑一遍
    primary_only = True

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg)
        self.search_pages = int(self.cfg.get("search_pages") or 1)
        self.page_size = int(self.cfg.get("page_size") or 20)
        self.max_videos = int(self.cfg.get("max_videos") or 10)
        self.max_articles = int(self.cfg.get("max_articles") or 6)
        self.comment_pages = int(self.cfg.get("comment_pages") or 1)
        self.comment_size = int(self.cfg.get("comment_size") or 20)
        self.concurrency = int(self.cfg.get("concurrency") or 5)
        self.templates = list(self.cfg.get("templates") or DEFAULT_TEMPLATES)
        self.qps = float(self.cfg.get("rate_limit_qps") or 4.0)
        self.include_comments = bool(self.cfg.get("comments", True))
        self.include_articles = bool(self.cfg.get("articles", True))
        self.limiter = RateLimiter(self.qps)
        self.sem = asyncio.Semaphore(self.concurrency)
        self.signer = _WBISigner()
        self._session_ready = False
        # 便于测试与排查：最近一次实际发出的请求数与各阶段产出
        self.last_stats: dict[str, int] = {}

    # ------------------------------------------------------------------ 会话
    async def _ensure_session(self, client: httpx.AsyncClient) -> None:
        """种游客 cookie（buvid3/buvid4）+ 取 wbi key。

        没有 buvid cookie 时搜索接口直接 412 —— 实测不种 cookie 只有 3% 的概率过。
        """
        if not self._session_ready:
            try:
                await self.limiter.acquire()
                await client.get(
                    "https://www.bilibili.com/",
                    headers={"User-Agent": UA, "Accept": "text/html"},
                )
                await self.limiter.acquire()
                spi = await client.get(
                    f"{API}/x/frontend/finger/spi",
                    headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/"},
                )
                data = (spi.json() or {}).get("data") or {}
                if data.get("b_3"):
                    client.cookies.set("buvid3", data["b_3"], domain=".bilibili.com")
                if data.get("b_4"):
                    client.cookies.set("buvid4", data["b_4"], domain=".bilibili.com")
            except Exception:
                # 拿不到 cookie 也继续试：有时接口仍会放行，失败交给上层源状态
                pass
            self._session_ready = True
        await self.signer.ensure(client, self.limiter)

    async def _api(self, client: httpx.AsyncClient, path: str, params: dict) -> dict:
        """带 WBI 签名的 GET，返回 JSON body（失败回 {}）。"""
        query = self.signer.sign(params)
        await self.limiter.acquire()
        async with self.sem:
            resp = await client.get(
                f"{API}{path}?{query}",
                headers={
                    "User-Agent": UA,
                    "Referer": "https://www.bilibili.com/",
                    "Accept": "application/json, text/plain, */*",
                },
            )
        if resp.status_code != 200:
            return {}
        try:
            return resp.json() or {}
        except ValueError:
            return {}

    # ------------------------------------------------------------------ 搜索
    async def _search_videos(self, kw: str, client: httpx.AsyncClient) -> list[dict]:
        out: list[dict] = []
        seen: set[str] = set()
        for template in self.templates:
            query = template.format(kw=kw)
            for page in range(1, self.search_pages + 1):
                body = await self._api(
                    client,
                    "/x/web-interface/wbi/search/type",
                    {
                        "search_type": "video",
                        "keyword": query,
                        "page": page,
                        "page_size": self.page_size,
                    },
                )
                if body.get("code") != 0:
                    break
                for item in (body.get("data") or {}).get("result") or []:
                    bvid = item.get("bvid")
                    if not bvid or bvid in seen:
                        continue
                    seen.add(bvid)
                    item["_query"] = query
                    out.append(item)
        return out

    async def _search_articles(self, kw: str, client: httpx.AsyncClient) -> list[dict]:
        seen: set[str] = set()
        out: list[dict] = []
        query = self.templates[0].format(kw=kw) if self.templates else kw
        body = await self._api(
            client,
            "/x/web-interface/wbi/search/type",
            {
                "search_type": "article",
                "keyword": query,
                "page": 1,
                "page_size": max(self.page_size, self.max_articles),
            },
        )
        if body.get("code") != 0:
            return []
        for item in (body.get("data") or {}).get("result") or []:
            cid = item.get("id")
            if not cid or str(cid) in seen:
                continue
            seen.add(str(cid))
            item["_query"] = query
            out.append(item)
        return out

    # ------------------------------------------------------------------ 抽取
    @staticmethod
    def _priority(item: dict, kw: str) -> float:
        """候选视频排序：更可能带资源的排前面，详情预算花在刀刃上。"""
        text = f"{_plain(item.get('title'))} {_plain(item.get('description'))} {_plain(item.get('desc'))}"
        score = float(sum(1 for w in _RESOURCE_HINTS if w in text))
        query = item.get("_query") or ""
        if any(w in query for w in _RESOURCE_TEMPLATE_HINTS):
            # 这条来自资源模板（"{kw} 百度网盘"），同一视频在原始词下也会出现，
            # 这里给它加权，保证"按资源意图召回的视频"先被跟详情。
            score += 2.0
        score += 0.5 * sum(1 for t in query_terms(kw) if t in text.lower())
        return score

    @staticmethod
    def _relevant(text: str | None, kw: str) -> bool:
        """简介/标题里至少要出现一个查询词才值得跟进。

        没有这道闸门会把 B 站"猜你喜欢"式的不相关视频全抓一遍：
        搜「Omnisphere」时首页混进来的编曲教程、软件下载视频都带网盘链接，
        收进来会把结果质量拉低，还白烧掉详情预算。
        """
        if not text:
            return False
        low = text.lower()
        return any(t in low for t in query_terms(kw))

    def _hits_from_video(self, item: dict, kw: str, *, stage: str) -> list[RawHit]:
        """把一条视频的**搜索结果字段**里的链接抽出来（不发额外请求）。"""
        title = _plain(item.get("title"))
        desc = " ".join(
            x for x in (_plain(item.get("description")), _plain(item.get("desc")),
                        _plain(item.get("tag"))) if x
        )
        text = f"{title}\n{desc}"
        if not self._relevant(text, kw):
            return []
        bvid = item.get("bvid")
        url = f"https://www.bilibili.com/video/{bvid}"
        return extract_from_text(
            text,
            source=f"bili-{stage}",
            kind=self.kind,
            origin=url,
            title=title or None,
            shared_at=item.get("pubdate"),
        )

    @staticmethod
    def _comment_texts(payload: dict) -> list[tuple[str, str]]:
        """从评论接口的返回里取出 (评论文本, 作者) —— 含置顶、热评与楼中楼。

        实测「链接：… 提取码：…」这类留言常常不在第一条，而在楼中楼里被
        顶上来；只看 replies[0] 会漏掉大部分资源线索。
        """
        data = payload.get("data") or {}
        out: list[tuple[str, str]] = []

        def walk(reply: dict) -> None:
            content = (reply.get("content") or {}).get("message")
            if content:
                out.append((content, (reply.get("member") or {}).get("uname") or ""))
            for sub in (reply.get("replies") or []):
                walk(sub)

        for key in ("top", "top_replies", "replies"):
            value = data.get(key)
            if isinstance(value, dict):
                walk(value)
            elif isinstance(value, list):
                for reply in value:
                    if isinstance(reply, dict):
                        walk(reply)
        return out

    async def _video_detail(self, item: dict, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        """视频完整简介 + 评论区。"""
        bvid = item.get("bvid")
        title = _plain(item.get("title"))
        url = f"https://www.bilibili.com/video/{bvid}"
        hits: list[RawHit] = []

        view = await self._api(client, "/x/web-interface/view", {"bvid": bvid})
        aid = item.get("aid")
        if view.get("code") == 0:
            info = view.get("data") or {}
            aid = info.get("aid") or aid
            desc = info.get("desc") or ""
            if desc and desc != _plain(item.get("description")):
                hits += extract_from_text(
                    desc,
                    source="bili-video",
                    kind=self.kind,
                    origin=url,
                    title=title or info.get("title"),
                    shared_at=info.get("pubdate"),
                )

        if self.include_comments and aid:
            for page in range(1, self.comment_pages + 1):
                payload = await self._api(
                    client,
                    "/x/v2/reply/wbi/main",
                    {"type": 1, "oid": aid, "mode": 3, "ps": self.comment_size, "pn": page},
                )
                if payload.get("code") != 0:
                    break
                texts = self._comment_texts(payload)
                if not texts:
                    break
                for text, author in texts:
                    hits += extract_from_text(
                        text,
                        source="bili-comment",
                        kind=self.kind,
                        origin=url,
                        title=title or None,
                        size=author or None,
                    )
        return hits

    async def _article_hits(self, item: dict, kw: str) -> list[RawHit]:
        """专栏只取搜索接口返回的摘要（正文需要浏览器渲染，见模块注释）。"""
        title = _plain(item.get("title"))
        desc = _plain(item.get("desc"))
        cid = item.get("id")
        url = f"https://www.bilibili.com/read/cv{cid}"
        text = f"{title}\n{desc}"
        if not self._relevant(text, kw):
            return []
        return extract_from_text(
            text,
            source="bili-article",
            kind=self.kind,
            origin=url,
            title=title or None,
            shared_at=item.get("pubdate"),
        )

    # ------------------------------------------------------------------ 入口
    async def search(self, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        await self._ensure_session(client)

        videos = await self._search_videos(kw, client)
        articles = await self._search_articles(kw, client) if self.include_articles else []

        hits: list[RawHit] = []
        seen: set[str] = set()

        def absorb(batch: list[RawHit]) -> None:
            fresh = [h for h in batch if h.url not in seen]
            for h in fresh:
                seen.add(h.url)
            if fresh:
                hits.extend(fresh)
                publish_hits(fresh)   # 超时也要把已完成的页面带回去

        # ① 搜索结果自带文本（零额外请求）
        for item in videos:
            absorb(self._hits_from_video(item, kw, stage="search"))
        for item in articles:
            absorb(await self._article_hits(item, kw))
        self.last_stats = {"videos": len(videos), "articles": len(articles),
                           "hits_from_search": len(hits)}

        # ②③ 详情：简介 + 评论（只跟标题/简介相关的，按预算截断）
        candidates = [
            v for v in videos
            if self._relevant(
                f"{_plain(v.get('title'))} {_plain(v.get('description'))} {_plain(v.get('desc'))}", kw
            )
        ]
        candidates.sort(key=lambda v: self._priority(v, kw), reverse=True)
        candidates = candidates[: self.max_videos]
        if candidates:
            results = await asyncio.gather(
                *(self._video_detail(v, kw, client) for v in candidates),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    continue          # 单个视频失败不影响其它视频
                absorb(result)
        self.last_stats["videos_followed"] = len(candidates)
        self.last_stats["hits_total"] = len(hits)
        self.last_stats["hits_from_detail"] = len(hits) - self.last_stats["hits_from_search"]
        return hits
