"""A 环：PanSou 网盘聚合引擎适配器。

PanSou 聚合了数十个网盘搜索插件 + 上千个 TG 频道，是覆盖面最广的单点数据源。

    GET {instance}/api/search?kw=<关键词>
    -> data.merged_by_type.{baidu|quark|aliyun|115|xunlei|123|tianyi|uc|pikpak|magnet}[]
       每项: url / password / note / datetime / source

实测公共实例会偶发 400 / timeout，因此这里做多实例 + 指数退避重试。
"""

from __future__ import annotations

import asyncio

import httpx

from ..extract import _parse_time
from ..models import PanType, RawHit
from ..normalize import detect_pan_type, pwd_from_url
from .base import Adapter, register

DEFAULT_INSTANCES = ["https://so.252035.xyz"]


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
        retries = int(self.cfg.get("retries") or 2)
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
        payload = None
        errors: list[str] = []
        for instance in self.instances:
            try:
                payload = await self._query(client, instance, kw)
            except RuntimeError as exc:
                errors.append(f"{instance}: {exc}")
                continue
            if payload:
                break
        if not payload:
            raise RuntimeError("; ".join(errors) or "PanSou 无响应")
        return self._parse(payload, kw)

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
                            title=_clean_note(item.get("note")),
                            shared_at=_parse_time(item.get("datetime")),
                            origin=None,
                        )
                    )
                except Exception:
                    # 单条数据畸形（实测有插件返回 netloc 带全角冒号的伪 URL）
                    # 不能让它把整个数据源的结果带走
                    continue
        return hits


def _clean_note(note: object) -> str | None:
    if not note:
        return None
    text = " ".join(str(note).split())
    return text[:300] or None
