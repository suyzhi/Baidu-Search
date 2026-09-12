"""排序打分：相关性 × 来源权重 × 存活状态 × 新鲜度 + 多源命中加成。"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .config import pan_priority, scoring_cfg
from .models import PanType, Resource, Status

_TERM_SPLIT = re.compile(r"[\s,，、/|·]+")


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
            if f"{prev}{term}" in title or f"{prev} {term}" in title:
                matched += 1
        elif term in title:
            matched += 1
    return matched


def _relevance(res: Resource, kw: str) -> float:
    """多词查询按「命中词数比例」打分，并**特殊对待第一个词**。

    中文没有分词，所以 "沙丘 4K HDR" 要拆成词分别匹配。
    但光看命中比例不够：「黑夏 4K HDR」这类只命中限定词的结果，
    靠百度优先 + 提取码 + 多源命中的连乘能反超真正相关的结果，
    所以查询的第一个词（通常是片名/主题词）缺失必须重罚。
    """
    k = kw.lower().strip()
    if not k:
        return 1.0
    title = (res.title or "").lower()
    if not title:
        return 0.3

    terms = [t for t in _TERM_SPLIT.split(k) if t]
    if not terms:
        return 1.0

    matched = _matched_terms(title, terms)
    subject_present = terms[0] in title

    if matched == len(terms):
        return 1.0 if title.startswith(terms[0]) else 0.95

    if not subject_present:
        # 主题词都没出现 -> 上限压到 0.4，保证任何加成组合都翻不了身
        return (round(0.15 + 0.25 * (matched / len(terms)), 4) if matched else 0.1)

    if matched == 0:  # 不会走到（subject_present 蕴含 matched>=1），保底
        return 0.2
    # 主题词在，但缺少限定词（4K/HDR/续集编号等）
    return round(0.45 + 0.55 * (matched / len(terms)), 4)


def _kind_weight(res: Resource, cfg: dict) -> float:
    weights = cfg.get("kind_weight") or {}
    if not res.kinds:
        return 0.6
    return max(float(weights.get(k, 0.6)) for k in res.kinds)


def _status_weight(res: Resource, cfg: dict) -> float:
    weights = cfg.get("status_weight") or {}
    return float(weights.get(res.status.value, 0.5))


def _freshness(res: Resource, cfg: dict) -> float:
    if not res.shared_at:
        return 0.8
    halflife = float(cfg.get("freshness_halflife_days") or 730)
    when = res.shared_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = max(0.0, (datetime.now(timezone.utc) - when).days)
    return max(0.35, math.pow(0.5, days / halflife))


def score_resource(res: Resource, kw: str, cfg: dict | None = None) -> float:
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

    relevance = _relevance(res, kw) ** float(cfg.get("relevance_power") or 1.0)
    score = relevance * _kind_weight(res, cfg) * _freshness(res, cfg)

    # 多源命中加成（相对提升，封顶 35%）
    bonus = float(cfg.get("multi_source_bonus") or 0.0)
    if res.hit_count > 1:
        score *= 1.0 + min(0.35, (res.hit_count - 1) * bonus)

    # 百度优先
    if res.pan_type is PanType.BAIDU:
        score *= 1.0 + float(cfg.get("pan_priority_bonus") or 0.0)

    # 有提取码 = 可直接用
    if res.pwd and res.status in (Status.ALIVE, Status.NEED_PWD):
        score *= 1.1

    # 只被"放宽查询"命中的结果降权（保留可发现性，但不淹没主查询结果）
    if not res.from_primary:
        score *= float(cfg.get("relaxed_penalty") or 0.5)

    return round(score * _status_weight(res, cfg), 6)


def score_all(resources: list[Resource], kw: str) -> list[Resource]:
    cfg = scoring_cfg()
    for res in resources:
        res.score = score_resource(res, kw, cfg)
    return resources


def sort_resources(resources: list[Resource], *, alive_first: bool = True) -> list[Resource]:
    """排序：失效沉底 → 未验活的沉到已验活之后 → 分数 → 网盘优先级（仅作平手时的兜底）。

    注意：网盘优先级**不能**作为独立层级排在分数前面，否则"不相关的百度结果"
    会压过"高度相关的夸克结果"。百度优先已由 scoring.pan_priority_bonus 体现。
    """
    prio = {p: i for i, p in enumerate(pan_priority())}
    verified_ok = {Status.ALIVE, Status.NEED_PWD, Status.WRONG_PWD}
    confirmed_dead = {Status.DEAD, Status.NOT_FOUND}

    def sort_key(res: Resource):
        st = res.status
        dead = 1 if st in confirmed_dead else 0
        if not alive_first:
            dead = 0
        # 验活过且可用（含码不对）= 0；未验活/无法判定/不支持 = 1
        unverified = 0 if st in verified_ok else 1
        pan_rank = prio.get(res.pan_type.value, len(prio))
        return (dead, unverified, -res.score, pan_rank)

    return sorted(resources, key=sort_key)
