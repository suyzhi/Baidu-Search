"""排序打分：相关性 × 来源权重 × 存活状态 × 新鲜度 + 多源命中加成。"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .config import pan_priority, scoring_cfg
from .models import PanType, Resource, Status


def _relevance(res: Resource, kw: str) -> float:
    if not kw:
        return 1.0
    k = kw.lower().strip()
    title = (res.title or "").lower()
    if not k:
        return 1.0
    if title.startswith(k):
        return 1.0
    if k in title:
        return 0.85
    # 关键词每个字都出现（中文分词缺失时的近似）
    if len(k) > 1 and all(ch in title for ch in k):
        return 0.7
    return 0.5


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
    """状态权重必须**最后相乘**，否则加分项会盖过它，让失效链接拿到高分。"""
    cfg = cfg or scoring_cfg()

    base = _relevance(res, kw) * _kind_weight(res, cfg) * _freshness(res, cfg)

    bonus = float(cfg.get("multi_source_bonus") or 0.0)
    if res.hit_count > 1:
        base += min(1.0, (res.hit_count - 1) * bonus)

    if res.pan_type is PanType.BAIDU:
        base += float(cfg.get("pan_priority_bonus") or 0.0)

    # 有提取码 = 可直接用
    if res.pwd and res.status in (Status.ALIVE, Status.NEED_PWD):
        base += 0.1

    return round(base * _status_weight(res, cfg), 6)


def score_all(resources: list[Resource], kw: str) -> list[Resource]:
    cfg = scoring_cfg()
    for res in resources:
        res.score = score_resource(res, kw, cfg)
    return resources


def sort_resources(resources: list[Resource], *, alive_first: bool = True) -> list[Resource]:
    """默认：失效的沉底，其余按分数。"""
    prio = {p: i for i, p in enumerate(pan_priority())}

    def sort_key(res: Resource):
        dead = 0 if (res.status.alive or res.status in (Status.UNCHECKED, Status.UNSUPPORTED)) else 1
        pan_rank = prio.get(res.pan_type.value, len(prio))
        if not alive_first:
            dead = 0
        return (dead, pan_rank, -res.score)

    return sorted(resources, key=sort_key)
