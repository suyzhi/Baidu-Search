"""公开 API 数据源适配器（学术 / 漫画 / 公版书）。

为什么需要它：
    学术与漫画这两个垂直领域，用 HTTP 爬页面基本爬不到 —— 实测
    Gutenberg / libgen.li / manhuagui / dm5 / manhuaren 的搜索页只返回
    导航链接（JS 渲染），抽不到任何条目；值不值得收录的价值校验也因此判它们"无产出"。
    但这些站点都有公开 API：结构化、稳定、不用解析 HTML。

    所以这一层不猜模板、不抽链接，直接按配置读 JSON/XML。

配置见 config/apis.yaml。
"""

from __future__ import annotations

import asyncio
import html as html_mod
import re
import time
import urllib.parse
from typing import Any

import httpx

from ..config import CONFIG_DIR
from ..models import RawHit
from ..routing import classify
from .base import Adapter, publish_hits, register

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_.]+)\}")
_TAG_RE = re.compile(r"<{tag}[^>]*>(.*?)</{tag}>", re.I | re.S)


def _load_apis() -> list[dict]:
    import yaml

    path = CONFIG_DIR / "apis.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [a for a in (data.get("apis") or []) if a.get("enabled", True)]


def dig(obj: Any, path: str | None) -> Any:
    """按点路径取**单个值**，支持 dict 与 list：
        dig(item, "attributes.title.en")
        dig(item, "message.items")   -> 若是列表，返回第一个非空元素
    """
    if not path:
        return None
    cur = obj
    for part in str(path).split("."):
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            if part.isdigit():
                idx = int(part)
                cur = cur[idx] if idx < len(cur) else None
            else:
                # 数组里逐项取该字段，返回第一个非空（crossref 的 title 就是数组）
                vals = [dig(x, part) for x in cur]
                cur = next((v for v in vals if v), None)
        else:
            return None
    if isinstance(cur, list):
        return next((x for x in cur if x not in (None, "")), None)
    return cur


def dig_list(obj: Any, path: str | None) -> list[Any]:
    """按点路径取**列表**。

    必须和 dig() 分开：dig() 会把列表展开成第一个元素（那是"取字段"的语义），
    用它取 items 只会拿到 1 条 —— 实测 crossref/openalex 各返回 20 条却只出 1 条。
    """
    if not path:
        return []
    cur = obj
    for part in str(path).split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return []
    if isinstance(cur, list):
        return cur
    return [cur] if cur else []


def _xml_items(text: str, tag: str) -> list[str]:
    """抽出所有 <tag>...</tag> 块的内容（arxiv 的 feed 用这个）。"""
    if not tag:
        return []
    pattern = rf"<{re.escape(tag)}\b[^>]*>(.*?)</{re.escape(tag)}>"
    return [m.group(1) for m in re.finditer(pattern, text, re.I | re.S)]


def _xml_field(block: str, tag: str) -> str | None:
    m = re.search(rf"<{re.escape(tag)}\b[^>]*>(.*?)</{re.escape(tag)}>", block, re.I | re.S)
    if not m:
        return None
    return html_mod.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() or None


def parse_items(text: str, cfg: dict) -> list[Any]:
    """把响应解析成条目列表。XML 走简单的标签抽取，JSON 走点路径。"""
    fmt = str(cfg.get("format") or "json").lower()
    items_path = str(cfg.get("items") or "")
    if fmt == "xml":
        return _xml_items(text, items_path)
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return []
    return dig_list(data, items_path)


def build_link(item: Any, cfg: dict) -> str | None:
    """从条目里取/拼出资源链接。"""
    # XML 条目是**字符串块**，字段要用标签名再抽一次（dig 对字符串无效）
    if isinstance(item, str):
        link = _xml_field(item, cfg.get("link") or "")
        return link if link and link.startswith(("http://", "https://")) else None

    template = cfg.get("link_template")
    if template:
        missing = False

        def sub(m: re.Match) -> str:
            nonlocal missing
            val = dig(item, m.group(1))
            if val in (None, ""):
                missing = True
                return ""
            return urllib.parse.quote(str(val))

        url = _PLACEHOLDER_RE.sub(sub, str(template))
        # 缺字段就当作拿不到链接，而不是产出一个残缺 URL
        return None if (missing or "{" in url) else url
    link = dig(item, cfg.get("link"))
    if isinstance(link, str) and link.startswith(("http://", "https://")):
        return link
    return None


def build_title(item: Any, cfg: dict) -> str | None:
    if isinstance(item, str):
        return _xml_field(item, cfg.get("title") or "")
    title = dig(item, cfg.get("title"))
    if isinstance(title, list):
        title = next((t for t in title if t), None)
    return str(title).strip()[:300] if title else None


@register
class ApiSourcesAdapter(Adapter):
    name = "apisources"
    kind = "api"
    primary_only = True        # 走垂直路由，补搜词没必要再打一遍

    def __init__(self, cfg: dict | None = None) -> None:
        super().__init__(cfg)
        self._last_call: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self.rate_limited: set[str] = set()      # 本轮被 429 的 API（排查用）

    def _rate_lock(self, name: str) -> asyncio.Lock:
        lock = self._locks.get(name)
        if lock is None:
            lock = self._locks[name] = asyncio.Lock()
        return lock

    @property
    def apis(self) -> list[dict]:
        return self.cfg.get("apis") or _load_apis()

    def select_apis(self, kw: str) -> list[dict]:
        verticals = set(classify(kw))
        if not verticals:
            return []
        picked = []
        for api in self.apis:
            tags = {str(v).strip() for v in str(api.get("vertical") or "").split(",") if v.strip()}
            if tags & verticals:
                picked.append(api)
        return picked

    async def search(self, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        apis = self.select_apis(kw)
        if not apis:
            return []
        results = await asyncio.gather(
            *(self._one(api, kw, client) for api in apis), return_exceptions=True
        )
        hits: list[RawHit] = []
        seen: set[str] = set()
        for result in results:
            if isinstance(result, BaseException):
                continue
            for hit in result:
                if hit.url in seen:
                    continue
                seen.add(hit.url)
                hits.append(hit)
        return hits

    async def _one(self, api: dict, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        name = str(api.get("name") or "api")
        url = str(api.get("url") or "").format(q=urllib.parse.quote(kw))
        if not url:
            return []

        # 限流：arXiv 明确要求每次请求间隔 ≥3 秒，连发会被 429。
        # 实测被限流时那一发要等 16 秒还返回 0 条 —— 既慢又白等。
        interval = float(api.get("min_interval") or 0)
        if interval > 0:
            async with self._rate_lock(name):
                wait = interval - (time.monotonic() - self._last_call.get(name, 0.0))
                if wait > 0:
                    await asyncio.sleep(min(wait, interval))
                self._last_call[name] = time.monotonic()

        try:
            resp = await client.get(
                url, timeout=float(api.get("timeout") or 15),
                headers={"User-Agent": UA, "Accept": "application/json, text/xml, */*"},
            )
        except httpx.HTTPError:
            return []
        if resp.status_code == 429:
            self.rate_limited.add(name)      # 记下来，便于排查"为什么这个 API 没结果"
            return []
        if resp.status_code != 200:
            return []

        hits: list[RawHit] = []
        for item in parse_items(resp.text, api):
            link = build_link(item, api)
            if not link:
                continue
            hits.append(
                RawHit(
                    source=f"api:{name}",
                    kind="api",
                    url=link,
                    title=build_title(item, api),
                    origin=url,
                )
            )
        return publish_hits(hits)
