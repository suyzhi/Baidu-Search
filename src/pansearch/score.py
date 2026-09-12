"""排序打分：相关性 × 来源权重 × 存活状态 × 新鲜度 + 多源命中加成。"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .config import pan_priority, scoring_cfg
from .models import PanType, Resource, Status

_TERM_SPLIT = re.compile(r"[\s,，、/|·]+")


def _relevance(res: Resource, kw: str) -> float:
    """多词查询按「命中词数比例」打分。

    中文没有分词，所以对 "沙丘 4K HDR" 这种查询要拆成词分别匹配，
    否则标题「沙丘：预言 (2024) 4K DV＆HDR」会被判成不相关（旧实现的 bug）。
    """
    k = kw.lower().strip()
    if not k:
        return 1.0
    title = (res.title or "").lower()
    if not title:
        return 0.4

    terms = [t for t in _TERM_SPLIT.split(k) if t]
    if not terms:
        return 1.0

    matched = sum(1 for t in terms if t in title)
    if matched == len(terms):
        return 1.0 if title.startswith(terms[0]) else 0.95
    if matched == 0:
        # 整串（去掉分隔符）的每个字都出现时的近似命中
        compact = "".join(terms)
        if len(compact) > 1 and all(ch in title for ch in compact):
            return 0.45
        return 0.2
    # 部分命中要**明显**低于全命中，否则网盘优先加分会把不相关结果顶上去
    return round(0.2 + 0.5 * (matched / len(terms)), 4)


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

    加法是有害的：如果百度优先(+0.3)和提取码(+0.1)是加数，一条相关性只有 0.53
    的不相关百度结果会拿到 0.83 分，反而压过相关性 1.0 的夸克结果（0.8 分）。
    改成乘法后，相关性差距不会被固定加分抹平。
    状态权重同样必须最后相乘，否则失效链接仍会拿到高分。
    """
    cfg = cfg or scoring_cfg()

    score = _relevance(res, kw) * _kind_weight(res, cfg) * _freshness(res, cfg)

    # 多源命中加成（相对提升，封顶 50%）
    bonus = float(cfg.get("multi_source_bonus") or 0.0)
    if res.hit_count > 1:
        score *= 1.0 + min(0.5, (res.hit_count - 1) * bonus)

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
