"""A 环：PanSou 网盘聚合引擎适配器。

PanSou 聚合了数十个网盘搜索插件 + 上千个 TG 频道，是覆盖面最广的单点数据源。

    GET {instance}/api/search?kw=<关键词>
    -> data.merged_by_type.{baidu|quark|aliyun|115|xunlei|123|tianyi|uc|pikpak|magnet}[]
       每项: url / password / note / datetime / source

实测公共实例会偶发 400 / timeout，因此这里做多实例 + 指数退避重试。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from ..extract import _parse_time
from ..models import PanType, RawHit
from ..normalize import detect_pan_type, pwd_from_url
from ..query import normalize_text, subject_terms, term_present
from .base import Adapter, register

DEFAULT_INSTANCES = ["https://so.252035.xyz"]

# 实例健康度（进程内）：失败的实例在 TTL 内被排到最后，避免每次搜索都为
# 一个挂掉的实例白等一次超时 —— 公共实例实测经常 000/超时，而自建实例 0.16s。
_HEALTH: dict[str, float] = {}
HEALTH_TTL_SECONDS = 120.0


def _healthy_first(instances: list[str]) -> list[str]:
    """健康实例优先；全挂时仍按原顺序全试一遍（不能永久拉黑，要能自愈）。"""
    now = time.monotonic()
    good = [u for u in instances if _HEALTH.get(u, 0.0) <= now]
    bad = [u for u in instances if _HEALTH.get(u, 0.0) > now]
    return good + bad


def _loads_lenient(content: bytes) -> dict | None:
    """宽松解析 JSON。

    实测某些关键词（如「三体」）的响应里混了非法 UTF-8 字节，
    `resp.json()` 会抛 UnicodeDecodeError —— 它是 ValueError 的子类，
    原代码把它归为"非 JSON 响应"，于是**一个坏字节让整个 PanSou 源失效**。
    这里按 UTF-8 宽松解码（坏字节替换掉），能取回多少算多少，而不是整源丢弃。
    """
    import json

    try:
        return json.loads(content)
    except (ValueError, UnicodeDecodeError):
        pass
    for encoding in ("utf-8", "gb18030"):
        try:
            return json.loads(content.decode(encoding, errors="replace"))
        except ValueError:
            continue
    return None


@register
class PansouAdapter(Adapter):
    name = "pansou"
    kind = "pansou"

    @property
    def instances(self) -> list[str]:
        return [u.rstrip("/") for u in (self.cfg.get("instances") or DEFAULT_INSTANCES)]

    async def _query(self, client: httpx.AsyncClient, url: str, kw: str) -> dict | None:
        retries = max(0, int(self.cfg.get("retries", 2)))
        timeout = float(self.cfg.get("timeout") or 15)
        last_err: str | None = None
        for attempt in range(retries + 1):
            try:
                resp = await client.get(
                    f"{url}/api/search",
                    params={"kw": kw},
                    headers={"Accept": "application/json", "Referer": url + "/"},
                    timeout=timeout,
                )
            except httpx.HTTPError as exc:
                last_err = str(exc)
            else:
                if resp.status_code == 200:
                    payload = _loads_lenient(resp.content)
                    if payload is not None:
                        return payload
                    last_err = "非 JSON 响应"
                else:
                    last_err = f"HTTP {resp.status_code}"
            if attempt < retries:
                await asyncio.sleep(0.8 * (attempt + 1))
        if last_err:
            raise RuntimeError(last_err)
        return None

    async def search(self, kw: str, client: httpx.AsyncClient) -> list[RawHit]:
        instances = _healthy_first(self.instances)
        deadline = float(self.cfg.get("deadline") or 15)
        end = time.monotonic() + deadline * 0.95
        delay = max(0.0, float(self.cfg.get("failover_delay", min(3.0, deadline / 3))))
        pending = {}
        errors = []
        next_instance = 0
        self.used_instance = None

        def launch():
            nonlocal next_instance
            instance = instances[next_instance]
            next_instance += 1
            pending[asyncio.create_task(self._query(client, instance, kw))] = instance
            return time.monotonic() + delay

        next_launch = launch() if instances else end
        try:
            while pending:
                now = time.monotonic()
                timeout = min(end, next_launch if next_instance < len(instances) else end) - now
                done, _ = await asyncio.wait(pending, timeout=max(0.0, timeout),
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    # 慢实例暂时降优先级；备用请求启动后保留主请求，避免丢掉冷启动结果。
                    for instance in pending.values():
                        _HEALTH[instance] = time.monotonic() + HEALTH_TTL_SECONDS
                    if time.monotonic() >= end:
                        errors.extend(f"{u}: 实例超时" for u in pending.values())
                        break
                    next_launch = launch()
                    continue
                for task in done:
                    instance = pending.pop(task)
                    try:
                        payload = task.result()
                        if not payload:
                            raise RuntimeError("PanSou 无响应")
                    except (RuntimeError, asyncio.TimeoutError) as exc:
                        _HEALTH[instance] = time.monotonic() + HEALTH_TTL_SECONDS
                        errors.append(f"{instance}: {str(exc) or '实例超时'}")
                        continue
                    _HEALTH.pop(instance, None)
                    self.used_instance = instance
                    return self._parse(payload, kw)
                if not pending and next_instance < len(instances):
                    next_launch = launch()
        finally:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        raise RuntimeError("; ".join(errors) or "PanSou 无响应")

    def _parse(self, payload: dict, kw: str) -> list[RawHit]:
        data = payload.get("data") or {}
        merged = data.get("merged_by_type") or {}
        hits: list[RawHit] = []

        for _ptype, items in merged.items():
            for item in items or []:
                try:
                    url = (item.get("url") or "").strip()
                    if not url:
                        continue
                    pan_type = detect_pan_type(url)
                    if pan_type is PanType.OTHER:
                        continue
                    raw_source = str(item.get("source") or _ptype)
                    kind = "tg" if raw_source.startswith("tg:") else "pansou"
                    hits.append(
                        RawHit(
                            source=raw_source,
                            kind=kind,
                            url=url,
                            pwd=item.get("password") or pwd_from_url(url),
                            title=_clean_note(item.get("note"), kw),
                            shared_at=_parse_time(item.get("datetime")),
                            origin=None,
                        )
                    )
                except Exception:
                    # 单条数据畸形（实测有插件返回 netloc 带全角冒号的伪 URL）
                    # 不能让它把整个数据源的结果带走
                    continue
        return hits


def _clean_note(note: object, query: str = "") -> str | None:
    if not note:
        return None
    text = " ".join(str(note).split())
    # 部分插件把本轮查询原样前缀到标题。只有后面再次出现主题时才去掉，
    # 避免把“沙丘 4K HDR 沙丘 1080P”误当完整画质匹配，也不误删正常标题。
    prefix = " ".join(query.split())
    if prefix and text.casefold().startswith(prefix.casefold() + " "):
        remainder = text[len(prefix):].strip()
        if any(term_present(normalize_text(remainder), t) for t in subject_terms(query)):
            text = remainder
    return text[:300] or None
