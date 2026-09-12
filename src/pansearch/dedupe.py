"""去重：把多源命中的同一分享合并成一条 Resource。"""

from __future__ import annotations

from collections.abc import Iterable

from .models import PanType, RawHit, Resource
from .normalize import detect_pan_type, normalize_url, parse_baidu, resource_key


def _pick_longer(a: str | None, b: str | None) -> str | None:
    if not a:
        return b
    if not b:
        return a
    return a if len(a) >= len(b) else b


def build_resources(hits: Iterable[RawHit]) -> list[Resource]:
    """按分享指纹合并，保留全部来源信息。"""
    buckets: dict[str, Resource] = {}

    for hit in hits:
        pan_type = detect_pan_type(hit.url)
        if pan_type is PanType.OTHER:
            continue

        surl: str | None = None
        if pan_type is PanType.BAIDU:
            surl, _ = parse_baidu(hit.url)

        key = resource_key(pan_type, hit.url, surl)
        res = buckets.get(key)

        if res is None:
            buckets[key] = Resource(
                key=key,
                pan_type=pan_type,
                url=normalize_url(pan_type, hit.url, surl, hit.pwd),
                surl=surl,
                pwd=hit.pwd,
                title=hit.title,
                size=hit.size,
                shared_at=hit.shared_at,
                sources=[hit.source],
                kinds=[hit.kind],
                origins=[hit.origin] if hit.origin else [],
                hit_count=1,
                from_primary=not hit.relaxed,
                queries=[hit.query] if hit.query else [],
            )
            continue

        # 合并
        res.hit_count += 1
        if hit.query and hit.query not in res.queries:
            res.queries.append(hit.query)
        if not hit.relaxed:
            res.from_primary = True
        if hit.pwd and not res.pwd:
            res.pwd = hit.pwd
        res.title = _pick_longer(res.title, hit.title)
        if hit.size and not res.size:
            res.size = hit.size
        if hit.shared_at and (res.shared_at is None or hit.shared_at < res.shared_at):
            res.shared_at = hit.shared_at
        if hit.source not in res.sources:
            res.sources.append(hit.source)
        if hit.kind not in res.kinds:
            res.kinds.append(hit.kind)
        if hit.origin and hit.origin not in res.origins and len(res.origins) < 8:
            res.origins.append(hit.origin)

    return list(buckets.values())
