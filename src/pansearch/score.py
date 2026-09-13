"""排序打分：相关性 × 来源权重 × 存活状态 × 新鲜度 + 多源命中加成。"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from functools import lru_cache

from .config import pan_priority, scoring_cfg
from .models import PanType, Resource, Status
from .query import matching_text, normalize_text, query_info, query_terms, subject_terms, term_present
from .textindex import term_idf


def _pick_anchor(subjects: list[str]) -> str:
    """多主题词时选"锚点"：**IDF 最高**的那个（最稀有的词最能界定主题）。

    实测反例：「Serum 合成器」里"合成器"也会被当成主题词，于是标题只要出现
    "合成器"（音乐类帖子到处都是）就算相关，前排全是不相关的专辑。
    按 IDF 选锚点后锚点变成罕见的 "serum"，只命中"合成器"的结果直接沉底。
    拿不到 IDF 统计（索引未建 / 词不在语料里）时退回第一个词。
    """
    if not subjects:
        return ""
    if len(subjects) == 1:
        return subjects[0]
    best, best_w = subjects[0], 0.0
    for t in subjects:
        w = term_idf(t)
        if w is not None and w > best_w:
            best, best_w = t, w
    return best


def anchor_term(kw: str) -> str:
    """查询的锚点主题词：没有内容词时退回第一个查询词。"""
    subjects = subject_terms(kw)
    if subjects:
        return _pick_anchor(subjects)
    terms = query_terms(kw)
    return terms[0] if terms else ""


def _matched_terms(title: str, terms: list[str]) -> int:
    """统计标题命中了几个查询词。

    数字词要**紧贴前一个词**才算命中（"沙丘2" / "沙丘 2"）。
    否则「沙丘 2」里的 "2" 会在 "2026"、"1080p" 里到处命中，
    让「奥古利亚沙丘」「沙丘荒原」「沙丘鹤」和真正的「沙丘2部合集」
    全都算"全词命中" —— 实测这些不相关结果就是这样挤进前排的。
    """
    matched = 0
    for i, term in enumerate(terms):
        if term.isdigit() and i > 0:
            prev = terms[i - 1]
            if re.search(re.escape(prev) + r"[\s:：._-]*" + re.escape(term) + r"(?!\d)", title):
                matched += 1
        elif term_present(title, term):
            matched += 1
    return matched


@lru_cache(maxsize=32768)
def _title_relevance(title: str, kw: str) -> float:
    info = query_info(kw)
    terms = info.terms
    if not terms:
        return 1.0
    if not title:
        return 0.3
    matched = _matched_terms(title, terms)
    # 多个具体主题共同定义意图。最高 IDF 只能用来检索，不能用一个
    # learning 代替 machine learning，也不能用“合成器”代替 Serum。
    subject_present = (all(term_present(title, t) for t in info.required)
                       if info.required else bool(matched))
    if matched == len(terms):
        return 1.0 if title.startswith(terms[0]) else 0.95
    if not subject_present:
        return round(0.15 + 0.25 * matched / len(terms), 4) if matched else 0.1
    return round(0.45 + 0.55 * matched / len(terms), 4)


def _relevance_single(res: Resource, kw: str, *, title: str | None = None) -> float:
    return _title_relevance(matching_text(title if title is not None else (res.title or "")),
                            normalize_text(kw))


def _queries(res: Resource, kw: str) -> tuple[str, ...]:
    # res.queries 仅存放主查询/等价别名，补搜绝不能参与评分或过滤。
    return tuple(dict.fromkeys(normalize_text(q) for q in [kw, *res.queries] if q)) or ("",)


def _titles(res: Resource) -> tuple[str, ...]:
    return tuple(dict.fromkeys(t for t in [res.title, *res.titles] if t)) or ("",)


def _relevance(res: Resource, kw: str) -> float:
    return max(_relevance_single(res, q, title=t) for q in _queries(res, kw) for t in _titles(res))


def confidently_irrelevant(res: Resource, kw: str) -> bool:
    """评分和过滤使用同一组具体主题、别名和文本归一规则。

    无标题与未知跨语言标题仍保留。通用标题不能推翻另一来源的具体标题证据。
    """
    titles = tuple(dict.fromkeys(matching_text(t) for t in _titles(res)))
    informative = [t for t in titles if query_info(t).subjects and t not in
                   {"文件", "文件夹", "未命名", "download", "untitled"}]
    if not informative:
        return False
    queries = _queries(res, kw)
    for q in queries:
        required = query_info(q).required
        if not required or any(all(term_present(t, term) for term in required) for t in informative):
            return False
    # 中文名称可能对应尚未配置的英文译名；只能标成低相关，不能判定无关。
    if any(re.search(r"[\u3400-\u9fff]", t) for t in query_info(kw).required):
        if all(not re.search(r"[\u3400-\u9fff]", t) for t in informative):
            return False
    return True


def _kind_weight(res: Resource, cfg: dict) -> float:
    weights = cfg.get("kind_weight") or {}
    if not res.kinds:
        return 0.6
    return max(float(weights.get(k, 0.6)) for k in res.kinds)


def _status_weight(res: Resource, cfg: dict) -> float:
    weights = cfg.get("status_weight") or {}
    return float(weights.get(res.status.value, 0.5))


def _freshness(res: Resource, cfg: dict) -> float:
    # 没有分享时间 -> **中性 1.0**，不该扣分。
    # API 直链（arXiv/Crossref/MangaDex）天然没有 shared_at，原来给 0.8
    # 等于无端打八折 —— 实测这让学术查询里本该排第一的 arXiv 论文
    # 被"侵略机器 War Machine"这类只命中 "machine" 的网盘结果压下去。
    # 缺数据不等于不新鲜。
    if not res.shared_at:
        return 1.0
    halflife = float(cfg.get("freshness_halflife_days") or 730)
    when = res.shared_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = max(0.0, (datetime.now(timezone.utc) - when).days)
    return max(0.35, math.pow(0.5, days / halflife))


def score_resource(res: Resource, kw: str, cfg: dict | None = None, *,
                   relevance_value: float | None = None) -> float:
    """打分：全部加成都是**乘法因子**，最后再乘状态权重。

    两个反直觉的坑（都是被真实结果逼出来的）：

    1. 加法是有害的：百度优先、提取码、多源命中如果是加数，一条相关性只有 0.53
       的不相关结果能拿到 0.83 分，压过相关性 1.0 的相关结果（0.8 分）。
    2. 光靠因子相乘还不够：百度(×1.25) × 提取码(×1.1) × 多源命中(×1.35) = ×1.86，
       仍能翻过"主题词命中/缺失"之间约 3 倍的相关性差距。所以相关性再加一个幂次
       闸门（默认平方），把差距拉到 ~9 倍，任何加成组合都翻不过来。
    3. 状态权重必须最后相乘，否则失效链接仍会拿到高分。
    """
    cfg = cfg or scoring_cfg()

    rel = _relevance(res, kw) if relevance_value is None else relevance_value
    relevance = rel ** float(cfg.get("relevance_power") or 1.0)
    score = relevance * _kind_weight(res, cfg) * _freshness(res, cfg)

    # 多源命中加成（相对提升，封顶 35%）
    bonus = float(cfg.get("multi_source_bonus") or 0.0)
    source_count = len(set(res.sources))
    if source_count > 1:
        score *= 1.0 + min(0.35, (source_count - 1) * bonus)

    # RRF（Reciprocal Rank Fusion）多源融合：Σ 1/(60+rank)。
    # 比"命中次数"更细 —— 在 TG 的 BM25 序和站点/网页序里都排前面的资源更可信。
    rrf_bonus = float(cfg.get("rrf_bonus") or 0.0)
    if rrf_bonus > 0 and res.rrf:
        score *= 1.0 + min(0.30, rrf_bonus * res.rrf)

    # 百度优先 —— **只给能确认可用的链接**。
    # 若不加这个条件：百度 share/verify 被锁（恒返回 -62）时，一堆"确定不了"的
    # 百度链接光靠域名就能压过已验证可用的夸克链接，实测表现为
    # 「漂流少年」前排全是不对版的百度结果、真资源在夸克却排在后面。
    if res.pan_type is PanType.BAIDU and res.usable:
        score *= 1.0 + float(cfg.get("pan_priority_bonus") or 0.0)

    # 有提取码 = 可直接用 —— 但只在**链接确实可确认可用**时才算优势。
    # 否则一堆"提取码还没验证过"的百度链接会靠这个加成压过确认可用的夸克链接。
    if res.pwd and res.usable:
        score *= 1.1

    # 只被"放宽查询"命中的结果降权（保留可发现性，但不淹没主查询结果）
    if not res.from_primary and rel < 0.85:
        score *= float(cfg.get("relaxed_penalty") or 0.5)

    return round(score * _status_weight(res, cfg), 6)


def score_all(resources: list[Resource], kw: str) -> list[Resource]:
    cfg = scoring_cfg()
    for res in resources:
        queries = _queries(res, kw)
        rated = [(t, max(_relevance_single(res, q, title=t) for q in queries))
                 for t in _titles(res)]
        title, relevance = max(rated, key=lambda pair: (pair[1], len(pair[0])))
        res.title = title or None
        res.relevance = relevance
        res.score = score_resource(res, kw, cfg, relevance_value=relevance)
    return resources


def sort_resources(resources: list[Resource], *, alive_first: bool = True) -> list[Resource]:
    """排序：失效沉底 → 匹配程度 → 确定性分层 → 分数 → 可用性/网盘优先级。

    分层顺序（越靠前越可信）：
      0  确认存活（alive）
      1  存活但没能确认可用（需提取码 / 码不对）
      2  确定不了 / 该网盘不支持验活（unknown / unchecked / unsupported）

    为什么要单独分层：百度 share/verify 被锁后，大量百度链接只能判到"需提取码"。
    如果和"确认存活"的夸克链接混在同一层比分数，它们会靠"百度优先"占前排 ——
    实测「漂流少年」就是这个现象（前排全是不对版的百度结果，真资源在夸克）。
    """
    prio = {p: i for i, p in enumerate(pan_priority())}
    confirmed_dead = {Status.DEAD, Status.NOT_FOUND}
    alive_set = {Status.ALIVE}
    partial = {Status.NEED_PWD, Status.WRONG_PWD}

    def sort_key(res: Resource):
        st = res.status
        dead = 1 if st in confirmed_dead else 0
        if not alive_first:
            dead = 0
        if st in alive_set:
            tier = 0
        elif st in partial:
            tier = 1
        else:
            tier = 2
        # 只在分数相同时才用"可确认可用"和网盘优先级决胜，
        # 避免它们盖过相关性差异
        not_usable = 0 if res.usable else 1
        pan_rank = prio.get(res.pan_type.value, len(prio))
        # 先比较匹配程度，再在相近结果里比较存活状态。
        relevance_tier = 0 if res.relevance >= 0.85 else 1 if res.relevance >= 0.45 else 2
        return (dead, relevance_tier, tier, -res.score, not_usable, pan_rank)

    return sorted(resources, key=sort_key)
