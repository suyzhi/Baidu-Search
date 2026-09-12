"""从任意文本（HTML / JSON / 帖子正文 / 视频简介 / 评论）里抽取网盘链接。"""

from __future__ import annotations

import html as html_mod
import re
from datetime import datetime

from .models import PanType, RawHit
from .normalize import (
    BARE_PWD_RE,
    PWD_RE,
    URL_RE,
    detect_pan_type,
    parse_baidu,
    pwd_from_url,
)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u00a0]+")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_TERM_SPLIT = re.compile(r"[\s,，、/|·]+")


def excerpt(text: str | None, keyword: str | None, width: int = 90) -> str:
    """截取**包含关键词的那一段**，而不是永远取开头。

    Telegram 一条消息常常列了好几个资源，标题又取的是消息前若干字，
    于是搜索结果看起来像是不相关（实测「沙丘 2」排第一的标题显示成
    「Re：从零开始的异世界生活…」，而匹配词其实在消息后段）。
    这里围绕第一个命中词取片段，让人一眼看出"为什么这条相关"。
    """
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= width:
        return flat

    terms = [t for t in _TERM_SPLIT.split(keyword or "") if t]
    # 按查询顺序找：第一个词是主题词，优先用它定位。
    # 不能取"最早出现"的那个词 —— 「沙丘 2」的 "2" 会在 "2026" 里就命中，
    # 结果又把片段拉回开头，等于没修。
    pos = -1
    for term in terms:
        found = flat.lower().find(term.lower())
        if found >= 0:
            pos = found
            break

    if pos < 0:
        return flat[:width] + "…"

    half = width // 2
    start = max(0, pos - half)
    end = min(len(flat), start + width)
    start = max(0, end - width)
    return ("…" if start > 0 else "") + flat[start:end] + ("…" if end < len(flat) else "")


def html_to_text(raw: str) -> str:
    """粗暴但够用的 HTML → 文本（保留链接与提取码的邻近关系）。"""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html_mod.unescape(text)
    return _WS_RE.sub(" ", text)


def page_title(raw: str) -> str | None:
    m = _TITLE_RE.search(raw)
    if not m:
        return None
    t = html_mod.unescape(_TAG_RE.sub("", m.group(1))).strip()
    return t or None


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def extract_from_text(
    text: str,
    *,
    source: str,
    kind: str,
    origin: str | None = None,
    title: str | None = None,
    size: str | None = None,
    shared_at: object = None,
    pwd_hint: str | None = None,
    window: int = 120,
) -> list[RawHit]:
    """从一段文本里抽出所有网盘链接，并把提取码配给"最近的那个链接"。

    提取码归属采用两遍扫描（这是必需的，否则相邻链接的提取码会互相串味）：
      第 1 遍 找出所有链接、所有提取码候选
      第 2 遍 每个提取码分配给距离最近的链接（出现在链接之前的加罚分）
    """
    when = _parse_time(shared_at)

    # ---- 第 1 遍：找链接 ----
    links: list[dict] = []
    for m in URL_RE.finditer(text):
        raw_url = m.group(0).rstrip("。，、；;!！?？'\"")
        url = html_mod.unescape(raw_url)

        pan_type = detect_pan_type(url)
        if pan_type is PanType.OTHER:
            continue
        if "/share/init" in url and "surl=" not in url:
            continue

        surl: str | None = None
        if pan_type is PanType.BAIDU:
            surl, _ = parse_baidu(url)
            if not surl:
                # 解析不出 surl 的百度链接（如 share/link?shareid=）无法验活也无法去重
                continue

        links.append(
            {
                "start": m.start(),
                "end": m.end(),
                "url": url,
                "pan_type": pan_type,
                "pwd": pwd_from_url(url),
            }
        )

    if not links:
        return []

    occupied = [(lk["start"], lk["end"]) for lk in links]

    def inside_link(pos: int) -> bool:
        return any(s <= pos < e for s, e in occupied)

    # ---- 第 1 遍（续）：找提取码候选，排除落在链接内部的（那是 URL 自带的）----
    candidates: list[tuple[int, str]] = []
    for pattern in (PWD_RE, BARE_PWD_RE):
        for pm in pattern.finditer(text):
            if inside_link(pm.start()):
                continue
            candidates.append((pm.start(), pm.group(1)))

    # ---- 第 2 遍：全局贪心配对 —— 每个提取码只归给最近的那个链接 ----
    # （每个链接独立找"最近的码"是不够的：同一个码会被相邻链接重复认领）
    left_penalty = 20   # "提取码 xxxx 链接 ..." 比 "链接 ... 提取码 xxxx" 少见
    pairs: list[tuple[int, int, int]] = []          # (距离, 码下标, 链接下标)
    for pi, (pos, _value) in enumerate(candidates):
        for li, lk in enumerate(links):
            if pos >= lk["end"]:
                dist = pos - lk["end"]
            elif pos < lk["start"]:
                dist = (lk["start"] - pos) + left_penalty
            else:
                continue
            if dist <= window:
                pairs.append((dist, pi, li))

    pairs.sort()
    used_pwd: set[int] = set()
    for _dist, pi, li in pairs:
        if pi in used_pwd or links[li]["pwd"]:
            continue
        links[li]["pwd"] = candidates[pi][1]
        used_pwd.add(pi)

    for lk in links:
        if not lk["pwd"]:
            lk["pwd"] = pwd_hint or None

    seen: set[str] = set()
    hits: list[RawHit] = []
    for lk in links:
        if lk["url"] in seen:
            continue
        seen.add(lk["url"])
        hits.append(
            RawHit(
                source=source,
                kind=kind,
                url=lk["url"],
                pwd=lk["pwd"],
                title=title,
                size=size,
                shared_at=when,
                origin=origin or lk["url"],
            )
        )
    return hits
