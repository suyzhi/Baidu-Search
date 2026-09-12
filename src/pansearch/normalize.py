"""链接 / 提取码的识别、抽取、归一化。

支持百度网盘的四种链接形态，以及 `pwd=` / `提取码:` / `#xxxx` 等多种提取码写法。
"""

from __future__ import annotations

import re
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit

from .models import PanType

# urlsplit 对某些畸形 URL 会抛 ValueError，而 URL 来自外部数据（聚合引擎的 JSON、
# 抓到的页面），不能让它把整批结果带走。典型触发：netloc 里有全角字符
#   http://|file|电影：2026.mkv|2260
#   -> ValueError: netloc '|file|电影：2026.mkv|2260' contains invalid characters
#      under NFKC normalization
_EMPTY_SPLIT = SplitResult(scheme="", netloc="", path="", query="", fragment="")
_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://(.*)$", re.S)


def safe_urlsplit(url: str) -> SplitResult:
    """永不抛异常的 urlsplit。解析失败时退化为"整串当作 path"。"""
    if not isinstance(url, str):
        return _EMPTY_SPLIT
    try:
        return urlsplit(url)
    except ValueError:
        m = _SCHEME_RE.match(url)
        if m:
            return SplitResult(scheme=m.group(1), netloc="", path=m.group(2),
                               query="", fragment="")
        return SplitResult(scheme="", netloc="", path=url, query="", fragment="")


def safe_hostname(url: str) -> str:
    """永不抛异常地取 hostname（小写）。畸形 URL 返回空串。"""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""

# 域名 → 网盘类型（按先后顺序匹配后缀）
_HOST_RULES: list[tuple[str, PanType]] = [
    ("pan.baidu.com", PanType.BAIDU),
    ("yun.baidu.com", PanType.BAIDU),
    ("eyun.baidu.com", PanType.BAIDU),
    ("pan.quark.cn", PanType.QUARK),
    ("alipan.com", PanType.ALIYUN),
    ("aliyundrive.com", PanType.ALIYUN),
    ("115cdn.com", PanType.P115),
    ("115.com", PanType.P115),
    ("anxia.com", PanType.P115),
    ("pan.xunlei.com", PanType.XUNLEI),
    ("cloud.189.cn", PanType.TIANYI),
    ("123pan.com", PanType.P123),
    ("123912.com", PanType.P123),
    ("123684.com", PanType.P123),
    ("123865.com", PanType.P123),
    ("drive.uc.cn", PanType.UC),
    ("mypikpak.com", PanType.PIKPAK),
    ("pikpak.me", PanType.PIKPAK),
]

PWD_KEYWORDS = (
    r"(?:pwd|passwd|password|passcode|code|提取码|提取碼|访问码|訪問碼|"
    r"提取密码|訪問密碼|访问密码|密码|密碼)"
)

# 带关键词的提取码：pwd=abcd / 提取码: abcd / 密码 abcd
PWD_RE = re.compile(
    rf"{PWD_KEYWORDS}\s*(?:是|为|：|:|＝|=)?\s*[「『\"'（(【\[]?\s*([A-Za-z0-9]{{4}})\b",
    re.I,
)
# 裸的括号提取码：（abcd）
BARE_PWD_RE = re.compile(r"[（(【\[]\s*([A-Za-z0-9]{4})\s*[）)】\]]")
# url 里的 query / fragment 形态
URL_PWD_RE = re.compile(r"[?&#](?:pwd|password|passwd|pwd=)=?([A-Za-z0-9]{4})\b", re.I)
# 分享链接本体（含 URL 编码形态）
# 字符类排除：全角标点、各类括号引号，以及**所有中日韩字符**
#   —— 否则 "https://drive.uc.cn/s/xxx我用夸克" 会把中文吃进 URL，产出坏链接
URL_RE = re.compile(
    r"https?://[^\s\"'<>()（）【】「」『』《》，,、。；;：:！!？?\]\[{}|\\^`"
    r"\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]+"
)

# 磁力链接：它没有 http(s):// 前缀，URL_RE 匹配不到 ——
# 所以之前所有磁力都只来自 PanSou 的 JSON，从页面/消息正文里一个都抽不出来。
# VST 音源、软件、影视的分享大量走磁力，必须单独抽。
MAGNET_RE = re.compile(
    r"magnet:\?xt=urn:btih:[A-Za-z0-9]{32,40}(?:&[^\s\"'<>()（）【】「」『』《》，,、。；;\]\[]+)*",
    re.I,
)
BAIDU_SURL_RE = re.compile(r"pan\.baidu\.com/s/([A-Za-z0-9_-]+)", re.I)
BAIDU_SURL_ENCODED_RE = re.compile(r"pan\.baidu\.com(?:%2F|/)s(?:%2F|/)([A-Za-z0-9_-]+)", re.I)


def detect_pan_type(url: str) -> PanType:
    """从 URL 判断网盘类型。"""
    if url.startswith("magnet:"):
        return PanType.MAGNET
    host = safe_hostname(url)
    if not host:
        return PanType.OTHER
    for suffix, pan in _HOST_RULES:
        if host == suffix or host.endswith("." + suffix):
            return pan
    return PanType.OTHER


def find_pwd(text: str) -> str | None:
    """从一段文本里找提取码。"""
    if not text:
        return None
    m = PWD_RE.search(text)
    if m:
        return m.group(1)
    m = BARE_PWD_RE.search(text)
    if m:
        return m.group(1)
    return None


def pwd_from_url(url: str) -> str | None:
    """从链接自身取提取码：?pwd=xxxx / #xxxx"""
    m = URL_PWD_RE.search(url)
    if m:
        return m.group(1)
    frag = safe_urlsplit(url).fragment
    if frag and re.fullmatch(r"[A-Za-z0-9]{4}", frag):
        return frag
    qs = parse_qs(safe_urlsplit(url).query)
    for k in ("pwd", "password", "passwd", "code"):
        v = qs.get(k)
        if v and re.fullmatch(r"[A-Za-z0-9]{4}", v[0]):
            return v[0]
    return None


def parse_baidu(url: str) -> tuple[str | None, str | None]:
    """把百度网盘链接拆成 (surl, pwd)。

    支持的形态：
      /s/1AbCdEf?pwd=1234      -> ("1AbCdEf", "1234")
      /s/1AbCdEf#1234          -> ("1AbCdEf", "1234")
      /share/init?surl=1AbCdEf -> ("1AbCdEf", None)
      /share/link?shareid=..   -> (None, None)   # 需请求才能解析，保留原 url
      URL 编码形态             -> 解码后同上
    """
    decoded = unquote(url)
    pwd = pwd_from_url(decoded)

    m = BAIDU_SURL_RE.search(decoded) or BAIDU_SURL_ENCODED_RE.search(url)
    if m:
        return m.group(1), pwd

    qs = parse_qs(safe_urlsplit(decoded).query)
    surl = (qs.get("surl") or [None])[0]
    if surl:
        return surl, pwd
    return None, pwd


def strip_leading_one(surl: str) -> str:
    """百度 API 的 surl 参数要去掉开头那个 '1'（实测关键坑）。"""
    return surl[1:] if surl.startswith("1") and len(surl) > 1 else surl


def normalize_url(pan_type: PanType, url: str, surl: str | None, pwd: str | None) -> str:
    """归一化出用于展示/去重的标准链接。"""
    if pan_type is PanType.BAIDU and surl:
        return f"https://pan.baidu.com/s/{surl}"
    if pan_type is PanType.MAGNET:
        return url.split("&dn=")[0]
    parsed = safe_urlsplit(url)
    if parsed.scheme and parsed.netloc:
        # 去掉跟踪参数，保留路径
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return url


def resource_key(pan_type: PanType, url: str, surl: str | None) -> str:
    """分享指纹：同一分享被多个源命中时用于合并。"""
    if pan_type is PanType.BAIDU and surl:
        return f"baidu:{surl}"
    if pan_type is PanType.MAGNET:
        m = re.search(r"btih:([A-Za-z0-9]+)", url)
        if m:
            return f"magnet:{m.group(1).lower()}"
        return f"magnet:{url}"
    parsed = safe_urlsplit(url)
    path = parsed.path.rstrip("/")
    if path:
        return f"{pan_type.value}:{parsed.hostname}{path}"
    return f"{pan_type.value}:{url}"
