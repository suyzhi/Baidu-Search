"""批量验活：一次校验一批网盘链接（`pansearch verify` 多个链接 / `-f 文件` / `-f -`）。

输入按**内容**识别（不看扩展名 —— `-f -` 读标准输入时根本没有扩展名）：
  * JSON：读 results[] 里的链接与提取码 —— `search --json` 是「链接」+ pwd（to_row 展开），
    `verify --json` 与网页版 /api/search 是 url + pwd；也接受纯字符串数组；
  * CSV ：`search --csv` / `verify --csv` 导出，表头同时有「链接」「提取码」两列；
  * 其它任意文本（帖子正文、聊天记录、一行一个链接…）：走 extract.extract_from_text，
    提取码配对规则与搜索链路完全一致。

之后按分享指纹去重（dedupe.build_resources：同一分享出现多次只验一次，保留最先出现的
提取码），再交给 VerifierPool.verify_all —— 缓存、限速、各网盘码表都复用搜索那一套。
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .dedupe import build_resources
from .extract import extract_from_text
from .models import PanType, RawHit, Resource, Status, VerifyResult
from .normalize import detect_pan_type, looks_like_share, parse_baidu, pwd_from_url
from .verifiers import VerifierPool

KIND = "manual"
DEFAULT_SOURCE = "cli"

# cli._csv_safe 导出时给以这些字符开头的单元格补了一个 '（防 CSV 公式注入），读回来要去掉。
# 两边必须一致：tests/test_batchverify.py 用 cli._csv_safe 做往返测试锁住它。
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_CSV_LINK_COL = "链接"
_CSV_PWD_COL = "提取码"
_CSV_TITLE_COL = "标题"

# "看起来像链接"的片段，用来统计无法识别的输入。与 normalize.URL_RE 的区别是保留 ? 与 :
# 之后的部分：抽取器在 ? 处截断 URL（提取码另行配对），于是 share/init?surl= 这类
# 靠 query 定位的百度链接在正文里抽不出来，这里要拿完整片段去兜底。
_LINKISH_RE = re.compile(
    r"https?://[^\s\"'<>()（）【】「」『』《》，,、。；;！!\]\[{}|\\^`"
    r"\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]+",
    re.I,
)
_MAGNETISH_RE = re.compile(r"magnet:\?[^\s\"'<>()（）【】「」『』《》，,、。；;]+", re.I)
_TRAILING_PUNCT = "。，、；;!！?？'\""

# 汇总口径（与 verifiers.prune(strict=True) 一致：只有 存活 / 需提取码 算"能用"）
SUMMARY_KEYS = ("total", "alive", "wrong_pwd", "dead", "unsupported", "unknown", "cache_hit")


@dataclass
class Collected:
    """从一份输入里收集到的命中，以及认不出来的片段。"""

    hits: list[RawHit] = field(default_factory=list)
    unrecognised: list[str] = field(default_factory=list)
    fmt: str = "text"           # text | csv | json

    def extend(self, other: Collected) -> None:
        self.hits.extend(other.hits)
        for item in other.unrecognised:
            if item not in self.unrecognised:
                self.unrecognised.append(item)


def _uncsv(value: object) -> str:
    """cli._csv_safe 的逆操作，并去掉首尾空白。"""
    text = "" if value is None else str(value)
    if text[:1] == "'" and text[1:2] and text[1] in _CSV_FORMULA_PREFIXES:
        text = text[1:]
    return text.strip()


def _single_link(token: str, source: str, title: str | None = None) -> list[RawHit]:
    """整段就是一条链接、抽取器却没认出来时的兜底（与单条校验一样按原 URL 构造）。

    只放行抽取器同样会放行的形态（网盘域名 + 分享路径 + 百度能解析出 surl），
    唯一的区别是保留了 query —— 用来接住 share/init?surl= 这类链接。
    """
    if not token or any(ch.isspace() for ch in token):
        return []
    pan_type = detect_pan_type(token)
    if pan_type is PanType.OTHER or not looks_like_share(pan_type, token):
        return []
    if pan_type is PanType.BAIDU and not parse_baidu(token)[0]:
        return []
    return [RawHit(source=source, kind=KIND, url=token, pwd=pwd_from_url(token),
                   title=title or None)]


def _hits_from_cell(link: str, pwd: str | None, title: str | None,
                    source: str) -> list[RawHit]:
    """导出文件 / 命令行里的一个"链接"单元格 → 命中；提取码列优先于链接里自带的 ?pwd=。"""
    hits = (extract_from_text(link, source=source, kind=KIND, title=title or None)
            or _single_link(link.strip(), source, title))
    if pwd:
        for hit in hits:
            hit.pwd = pwd
    return hits


def _from_rows(rows: Iterable[tuple[int, str, str | None, str | None]], source: str,
               fmt: str) -> Collected:
    out = Collected(fmt=fmt)
    for lineno, link, pwd, title in rows:
        if not link:
            out.unrecognised.append(f"第 {lineno} 条：链接为空")
            continue
        hits = _hits_from_cell(link, pwd, title, source)
        if hits:
            out.hits.extend(hits)
        elif link not in out.unrecognised:
            out.unrecognised.append(link)
    return out


def _parse_json(text: str, source: str) -> Collected | None:
    stripped = text.strip()
    if stripped[:1] not in ("{", "["):
        return None
    try:
        data = json.loads(stripped)
    except ValueError:
        return None
    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None

    rows: list[tuple[int, str, str | None, str | None]] = []
    for i, item in enumerate(items, 1):
        if isinstance(item, str):
            rows.append((i, item.strip(), None, None))
        elif isinstance(item, dict):
            # search --json 的结果是 to_row() 展开（链接 / 提取码 / 标题）再加 pwd；
            # verify --json 与 Web API 用 url / pwd / title。
            link = item.get("url") or item.get(_CSV_LINK_COL) or ""
            pwd = item.get("pwd") or item.get(_CSV_PWD_COL) or None
            title = item.get("title") or item.get(_CSV_TITLE_COL) or None
            rows.append((i, str(link).strip(), str(pwd).strip() if pwd else None,
                         str(title) if title else None))
        else:
            rows.append((i, "", None, None))
    return _from_rows(rows, source, "json")


def _parse_csv(text: str, source: str) -> Collected | None:
    first = next((line for line in text.splitlines() if line.strip()), "")
    header = [h.strip() for h in next(csv.reader([first]), [])]
    if _CSV_LINK_COL not in header or _CSV_PWD_COL not in header:
        return None

    reader = csv.reader(io.StringIO(text.lstrip()))
    next(reader, None)                                   # 表头
    link_i, pwd_i = header.index(_CSV_LINK_COL), header.index(_CSV_PWD_COL)
    title_i = header.index(_CSV_TITLE_COL) if _CSV_TITLE_COL in header else None

    def cell(row: list[str], idx: int | None) -> str:
        return _uncsv(row[idx]) if idx is not None and idx < len(row) else ""

    rows: list[tuple[int, str, str | None, str | None]] = []
    for n, row in enumerate(reader, 1):
        if not any(c.strip() for c in row):
            continue                                     # 空行（文件末尾常见）
        rows.append((n, cell(row, link_i), cell(row, pwd_i) or None,
                     cell(row, title_i) or None))
    return _from_rows(rows, source, "csv")


def _parse_text(text: str, source: str) -> Collected:
    out = Collected(hits=extract_from_text(text, source=source, kind=KIND), fmt="text")
    # 逐个"链接样"片段复核（用同一个抽取器判定，口径不会和上面打架）：
    #   * 抽取器认得 → 上面已经收了；
    #   * 抽取器不认、但整段是合法分享链接（share/init?surl=…）→ 兜底收下；
    #   * 都不是（非网盘域名、网盘首页、百度 share/link?shareid= 等无法验活的形态）
    #     → 计入"无法识别"报给用户，而不是悄悄丢掉。
    magnets = list(_MAGNETISH_RE.finditer(text))
    # 磁力里的 tr=http://tracker/… 不是一条独立输入，不能算"无法识别"
    tokens = [m.group(0) for m in _LINKISH_RE.finditer(text)
              if not any(g.start() <= m.start() < g.end() for g in magnets)]
    tokens += [m.group(0) for m in magnets]
    for token in tokens:
        token = token.rstrip(_TRAILING_PUNCT)
        if token in out.unrecognised or extract_from_text(token, source=source, kind=KIND):
            continue
        fallback = _single_link(token, source)
        if fallback:
            out.hits.extend(fallback)
        else:
            out.unrecognised.append(token)
    return out


def collect_hits(text: str, *, source_name: str | None = None) -> Collected:
    """把一份输入（文件内容 / 标准输入 / 命令行参数）解析成待验活的命中。"""
    text = (text or "").lstrip("\ufeff")
    source = source_name or DEFAULT_SOURCE
    return (_parse_json(text, source) or _parse_csv(text, source)
            or _parse_text(text, source))


def build_batch(collected: Collected) -> list[Resource]:
    """按分享指纹去重（保留输入顺序、最先出现的提取码）。"""
    return build_resources(collected.hits)


# ------------------------------------------------------------------ 验活
def bucket(status: Status) -> str:
    """把状态归到汇总口径里的一类。"""
    if status in (Status.ALIVE, Status.NEED_PWD):
        return "alive"
    if status is Status.WRONG_PWD:
        return "wrong_pwd"
    if status in (Status.DEAD, Status.NOT_FOUND):
        return "dead"
    if status is Status.UNSUPPORTED:
        return "unsupported"
    return "unknown"                                    # UNKNOWN / UNCHECKED


def was_cached(res: Resource) -> bool:
    return bool(res.verify and (res.verify.method or "").startswith("cache"))


def summarize(resources: list[Resource]) -> dict[str, int]:
    counts = dict.fromkeys(SUMMARY_KEYS, 0)
    counts["total"] = len(resources)
    for res in resources:
        counts[bucket(res.status)] += 1
        if was_cached(res):
            counts["cache_hit"] += 1
    return counts


@dataclass
class BatchReport:
    """一批验活的结果：资源（输入顺序，结论在 res.verify）+ 汇总计数（SUMMARY_KEYS）。"""

    resources: list[Resource]
    counts: dict[str, int]

    @property
    def alive(self) -> list[Resource]:
        """能用的那些（存活 / 需提取码），即 --alive-only 输出的内容。"""
        return [res for res in self.resources if bucket(res.status) == "alive"]


async def verify_batch(
    resources: list[Resource],
    pool: VerifierPool,
    *,
    use_cache: bool = True,
    on_progress: Callable[[int, int], None] | None = None,
    tick: float = 0.2,
) -> BatchReport:
    """验活一批（刚由 build_batch 建好、尚未验过的）资源，结果写回 res.verify。

    * 全量验活（budget=0、无 deadline）：批量校验就是要每条都有答案；
      并发与限速仍由 VerifierPool 按网盘分别控制。
    * 池不支持的类型直接标"不支持"，**不交给 verify_all**：那里会把 API 直链
      （arxiv 等）当成"有效"，而这里回答的是"这条链接验过没有"——没验过就不能说有效。
    * on_progress(已完成, 总数) 每 tick 秒回调一次，供命令行刷新进度。
    """
    supported: list[Resource] = []
    for res in resources:
        if pool.supports(res.pan_type):
            supported.append(res)
        else:
            res.verify = VerifyResult(status=Status.UNSUPPORTED,
                                      note=f"{res.pan_type.label} 暂不支持验活")

    total = len(resources)
    ticker: asyncio.Task | None = None
    if on_progress is not None:
        async def _tick() -> None:
            while True:
                on_progress(sum(r.verify is not None for r in resources), total)
                await asyncio.sleep(tick)

        ticker = asyncio.create_task(_tick())
    try:
        if supported:
            await pool.verify_all(supported, budget=0, use_cache=use_cache)
    finally:
        if ticker is not None:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
    if on_progress is not None:
        on_progress(total, total)
    return BatchReport(resources=resources, counts=summarize(resources))


# ------------------------------------------------------------------ 导出
def remark(res: Resource) -> str:
    """备注列：验活接口的说明 + 资源名（有的话）。"""
    note = res.verify.note if res.verify else None
    return " · ".join(p for p in (note, res.title) if p)


def to_export_row(res: Resource) -> dict:
    """CSV 一行。含「链接」「提取码」两列，所以导出的文件能直接 `verify -f` 读回来复验。"""
    return {
        "状态": res.status_label,
        "网盘": res.pan_type.label,
        _CSV_LINK_COL: res.open_url(),
        _CSV_PWD_COL: res.pwd or "",
        _CSV_TITLE_COL: res.title or "",
        "备注": res.verify.note if res.verify and res.verify.note else "",
    }


def to_export_item(res: Resource) -> dict:
    """JSON 一项（url / pwd 两个键能被 collect_hits 读回来）。"""
    return {
        "url": res.open_url(),
        "pwd": res.pwd,
        "pan_type": res.pan_type.value,
        "网盘": res.pan_type.label,
        "status": res.status.value,
        "status_label": res.status_label,
        "alive": bucket(res.status) == "alive",
        "errno": res.verify.errno if res.verify else None,
        "method": res.verify.method if res.verify else None,
        "note": res.verify.note if res.verify else None,
        "title": res.title,
        "cached": was_cached(res),
        "key": res.key,
    }


__all__ = [
    "BatchReport", "Collected", "SUMMARY_KEYS", "bucket", "build_batch", "collect_hits",
    "remark", "summarize", "to_export_item", "to_export_row", "verify_batch", "was_cached",
]
