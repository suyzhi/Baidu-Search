"""非百度网盘链接验活 + 统一调度池。

实测校准于 2026-09-12（真实链接 + 伪造 ID 交叉验证）：

| 网盘     | 接口                                                              | 存活              | 需码          | 码错          | 失效                  |
|----------|-------------------------------------------------------------------|-------------------|---------------|---------------|-----------------------|
| 夸克     | POST drive-pc.quark.cn/1/clouddrive/share/sharepage/token          | code 0 + stoken   | —             | —             | code 41006 / HTTP 404 |
| 阿里云盘 | POST api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous | 200 + share_name | —            | —             | 404 NotFound.ShareLink|
| 115      | GET  webapi.115.com/share/snap                                     | state true        | errno 4100012 | errno 4100008 | errno 990002          |
| 天翼189  | GET  cloud.189.cn/api/open/share/getShareInfoByCodeV2.action       | 200 + <shareVO>   | —             | —             | 400 ShareInfoNotFound |

暂不支持（接口需验证码 / 已变更）：迅雷、UC、123网盘、PikPak、磁力。
这些会保持 "unsupported" 状态 —— 注意 **unsupported ≠ 有效**。
"""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import httpx

from .config import pan_errno_config, verify_cfg
from .models import PanType, Resource, Status, VerifyResult
from .store import VerifyCache
from .util import RateLimiter
from .verify import BaiduVerifier

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 分享 ID 取 /s/<id> 或 /t/<id> 之后的那一段
_ID_RE = re.compile(r"/(?:s|t)/([A-Za-z0-9_-]+)")


def path_id(url: str) -> str | None:
    """从网盘分享链接里取出分享 ID / code。"""
    path = urlparse(url).path
    m = _ID_RE.search(path)
    if m:
        return m.group(1)
    tail = path.strip("/").split("/")[-1] if path.strip("/") else ""
    return tail or None


def _decide(mapping: dict, value: object, default: str = "unknown") -> str:
    return str(mapping.get(str(value), default))


class ServiceVerifier(ABC):
    """单个网盘服务的验活器。"""

    name: str = ""
    pan_types: tuple[PanType, ...] = ()
    default_qps: float = 3.0
    referer: str = ""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    @property
    def qps(self) -> float:
        return float(self.cfg.get("qps") or self.default_qps)

    def supports(self, pan_type: PanType) -> bool:
        return pan_type in self.pan_types

    @staticmethod
    def _status(value: str, errno: object = None, note: str | None = None,
                method: str = "", pwd_verified: bool = False,
                title_hint: str | None = None) -> VerifyResult:
        try:
            st = Status(value)
        except ValueError:
            st = Status.UNKNOWN
        return VerifyResult(status=st, errno=errno if isinstance(errno, int) else None,
                            method=method or None, note=note, pwd_verified=pwd_verified,
                            title_hint=(str(title_hint).strip() or None) if title_hint else None)

    @abstractmethod
    async def check(self, res: Resource, client: httpx.AsyncClient) -> VerifyResult:
        ...


class QuarkVerifier(ServiceVerifier):
    name = "quark"
    pan_types = (PanType.QUARK,)
    default_qps = 4.0
    referer = "https://pan.quark.cn/"

    async def check(self, res, client):
        share_id = path_id(res.url)
        if not share_id:
            return self._status("unknown", note="无法解析分享 ID", method=self.name)
        resp = await client.post(
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token",
            params={"pr": "ucpro", "fr": "pc", "uc_param_str": ""},
            json={"pwd_id": share_id, "passcode": res.pwd or ""},
            headers={"Referer": self.referer},
        )
        by_http = _decide(self.cfg.get("http") or {}, resp.status_code)
        if by_http != "unknown":
            return self._status(by_http, errno=resp.status_code, method=self.name)
        try:
            payload = resp.json()
        except ValueError:
            return self._status("unknown", errno=resp.status_code, note="非 JSON 响应", method=self.name)
        code = payload.get("code")
        status = _decide(self.cfg.get("code") or {}, code)
        if status == "unknown" and code:
            # 精确 code 对不上时按关键字模糊匹配，避免接口改文案就全变 unknown
            text = f"{code} {payload.get('message') or ''}"
            for needle, value in (self.cfg.get("code_contains") or {}).items():
                if needle.lower() in text.lower():
                    status = value
                    break
        if status == "unknown" and payload.get("status") == 200:
            status = "alive"
        title = (payload.get("data") or {}).get("title") or payload.get("share_name")
        note = None if status == "alive" else str(payload.get("message") or "")[:60] or None
        return self._status(status, errno=code if isinstance(code, int) else None,
                            note=note, method=self.name, title_hint=title)


class AliyunVerifier(ServiceVerifier):
    name = "aliyun"
    pan_types = (PanType.ALIYUN,)
    default_qps = 4.0
    referer = "https://www.alipan.com/"

    async def check(self, res, client):
        share_id = path_id(res.url)
        if not share_id:
            return self._status("unknown", note="无法解析分享 ID", method=self.name)
        resp = await client.post(
            "https://api.aliyundrive.com/adrive/v3/share_link/get_share_by_anonymous",
            json={"share_id": share_id},
            headers={"Referer": self.referer, "Origin": self.referer.rstrip("/")},
        )
        try:
            payload = resp.json()
        except ValueError:
            return self._status(_decide(self.cfg.get("http") or {}, resp.status_code,
                                        "unknown"), errno=resp.status_code,
                                note="非 JSON 响应", method=self.name)
        code = payload.get("code")
        if code:
            status = _decide(self.cfg.get("code") or {}, code)
            if status == "unknown":
                text = f"{code} {payload.get('message') or ''}"
                for needle, value in (self.cfg.get("code_contains") or {}).items():
                    if needle.lower() in text.lower():
                        status = value
                        break
            return self._status(status, note=str(payload.get("message") or "")[:60] or None,
                                method=self.name)
        by_http = _decide(self.cfg.get("http") or {}, resp.status_code)
        if by_http != "unknown":
            return self._status(by_http, errno=resp.status_code, method=self.name)

        # 有 expiration 字段时据此判过期
        expiration = payload.get("expiration")
        if expiration:
            from datetime import datetime, timezone
            try:
                when = datetime.fromisoformat(str(expiration).replace("Z", "+00:00"))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when < datetime.now(timezone.utc):
                    return self._status("dead", note=f"分享已于 {expiration} 过期", method=self.name)
            except ValueError:
                pass
        return self._status("alive", method=self.name,
                            title_hint=payload.get("share_name"))


class Pan115Verifier(ServiceVerifier):
    name = "115"
    pan_types = (PanType.P115,)
    default_qps = 3.0
    referer = "https://115.com/"
    # 115 的 receive_code 会被服务端真正校验，所以能判定"码已验证"
    validates_password = True

    async def check(self, res, client):
        code = path_id(res.url)
        if not code:
            return self._status("unknown", note="无法解析分享 code", method=self.name)
        resp = await client.get(
            "https://webapi.115.com/share/snap",
            params={"share_code": code, "offset": 0, "limit": 10, "receive_code": res.pwd or ""},
            headers={"Referer": self.referer},
        )
        try:
            payload = resp.json()
        except ValueError:
            return self._status("unknown", errno=resp.status_code, note="非 JSON 响应", method=self.name)
        errno = payload.get("errno")
        errno_map_ = self.cfg.get("errno") or {}
        if payload.get("state") is True and str(errno) == "0":
            title = ((payload.get("data") or {}).get("shareinfo") or {}).get("share_title")
            return self._status("alive", errno=0, method=self.name,
                                pwd_verified=bool(res.pwd), title_hint=title)
        status = _decide(errno_map_, errno, "unknown")
        if status == "unknown" and payload.get("state") is False:
            status = str(self.cfg.get("state_false_default") or "unknown")
        note = str(payload.get("error") or "")[:60] or None
        return self._status(status, errno=errno if isinstance(errno, int) else None,
                            note=note, method=self.name)


class TianyiVerifier(ServiceVerifier):
    name = "tianyi"
    pan_types = (PanType.TIANYI,)
    default_qps = 3.0
    referer = "https://cloud.189.cn/"

    async def check(self, res, client):
        code = path_id(res.url)
        if not code:
            return self._status("unknown", note="无法解析分享 code", method=self.name)
        resp = await client.get(
            "https://cloud.189.cn/api/open/share/getShareInfoByCodeV2.action",
            params={"shareCode": code},
            headers={"Referer": self.referer},
        )
        text = resp.text
        if resp.status_code == 200 and "<shareVO>" in text:
            m = re.search(r"<fileName>(.*?)</fileName>", text, re.S)
            title = m.group(1).strip() if m else None
            return self._status("alive", errno=200, method=self.name, title_hint=title)
        for key, value in (self.cfg.get("xml") or {}).items():
            if f"<code>{key}</code>" in text:
                return self._status(value, errno=resp.status_code, note=key, method=self.name)
        by_http = _decide(self.cfg.get("http") or {}, resp.status_code)
        return self._status(by_http, errno=resp.status_code, method=self.name)


# 注册顺序即调度顺序
SERVICE_VERIFIERS: tuple[type[ServiceVerifier], ...] = (
    QuarkVerifier,
    AliyunVerifier,
    Pan115Verifier,
    TianyiVerifier,
)


class VerifierPool:
    """统一验活入口：按网盘类型分发到对应验活器，共享限速与缓存。

    * 百度 -> 复用 BaiduVerifier（自带客户端与 Cookie 预热）
    * 其它已支持网盘 -> 本池的共享客户端
    * 未支持网盘 -> unsupported（**注意 unsupported ≠ 有效**）
    """

    def __init__(self, cfg: dict | None = None, cache: VerifyCache | None = None):
        vcfg = cfg or verify_cfg()
        self.cfg = vcfg
        ttl = float(vcfg.get("cache_ttl_hours") or 6)
        self.cache = cache if cache is not None else VerifyCache(ttl_hours=ttl)
        self.timeout = float(vcfg.get("timeout") or 20)
        self.retries = int(vcfg.get("retries") or 2)
        self.sem = asyncio.Semaphore(int(vcfg.get("concurrency") or 8))
        self.baidu = BaiduVerifier(cfg=vcfg, cache=self.cache)

        services_cfg = (pan_errno_config().get("services") or {})
        self.verifiers: list[ServiceVerifier] = []
        for cls in SERVICE_VERIFIERS:
            merged = dict(services_cfg.get(cls.name) or {})
            override = (vcfg.get("services") or {}).get(cls.name) or {}
            merged.update(override)
            verifier = cls(merged)
            if verifier.enabled:
                self.verifiers.append(verifier)
        self.limiters = {v.name: RateLimiter(v.qps) for v in self.verifiers}
        self._client: httpx.AsyncClient | None = None
        self.stats: dict = {
            "checked": 0, "cache_hit": 0, "alive": 0, "dead": 0, "error": 0,
            "need_pwd": 0, "wrong_pwd": 0, "unsupported": 0, "pruned": 0,
            "by_service": {},
        }

    # ---- 生命周期 ----
    async def __aenter__(self) -> VerifierPool:
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            http2=True,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        )
        await self.baidu.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.baidu.__aexit__(*exc)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- 能力查询 ----
    def supports(self, pan_type: PanType) -> bool:
        if pan_type is PanType.BAIDU:
            return True
        return any(v.supports(pan_type) for v in self.verifiers)

    def _service_for(self, pan_type: PanType) -> ServiceVerifier | None:
        for v in self.verifiers:
            if v.supports(pan_type):
                return v
        return None

    def _bump(self, service: str, status: Status) -> None:
        """只统计**按网盘**的分布；全局计数由 _tally 负责，避免重复计数。"""
        bucket = self.stats["by_service"].setdefault(
            service, {"checked": 0, "alive": 0, "dead": 0, "other": 0}
        )
        bucket["checked"] += 1
        if status.alive:
            bucket["alive"] += 1
        elif status in (Status.DEAD, Status.NOT_FOUND):
            bucket["dead"] += 1
        else:
            bucket["other"] += 1

    def _tally(self, status: Status, *, cached: bool = False) -> None:
        if cached:
            self.stats["cache_hit"] += 1
        else:
            self.stats["checked"] += 1
        if status.alive:
            self.stats["alive"] += 1
        if status is Status.NEED_PWD:
            self.stats["need_pwd"] += 1
        if status is Status.WRONG_PWD:
            self.stats["wrong_pwd"] += 1
        if status in (Status.DEAD, Status.NOT_FOUND):
            self.stats["dead"] += 1
        if status in (Status.UNKNOWN, Status.UNSUPPORTED):
            self.stats["error"] += 1

    @staticmethod
    def _was_cached(result: VerifyResult) -> bool:
        return bool(result.method and result.method.startswith("cache"))

    # ---- 单个资源 ----
    async def verify(self, res: Resource, *, use_cache: bool = True) -> VerifyResult:
        if res.pan_type is PanType.BAIDU:
            result = await self.baidu.verify(res, use_cache=use_cache)
            self._tally(result.status, cached=self._was_cached(result))
            self._bump("baidu", result.status)
            return result

        service = self._service_for(res.pan_type)
        if service is None:
            res.verify = VerifyResult(
                status=Status.UNSUPPORTED,
                note=f"{res.pan_type.label} 暂不支持验活",
            )
            self.stats["unsupported"] += 1
            return res.verify

        if use_cache:
            hit = self.cache.get(res.key, res.pwd)
            if hit is not None:
                self._tally(hit.status, cached=True)
                self._bump(service.name, hit.status)
                res.verify = hit
                return hit

        assert self._client is not None, "VerifierPool 必须在 async with 中使用"
        result = VerifyResult(status=Status.UNKNOWN, method=service.name)
        for attempt in range(self.retries + 1):
            await self.limiters[service.name].acquire()
            try:
                async with self.sem:
                    result = await service.check(res, self._client)
                break
            except httpx.HTTPError as exc:
                result = VerifyResult(
                    status=Status.UNKNOWN, method=service.name, note=str(exc)[:80]
                )
                if attempt < self.retries:
                    await asyncio.sleep(0.6 * (attempt + 1))

        if result.status is not Status.UNKNOWN:
            self.cache.put(res.key, result, res.pwd)
        # 验活接口顺带给的真实资源名，用来补全空标题（不覆盖已有的更好标题）
        if result.title_hint and not res.title:
            res.title = result.title_hint
        res.verify = result
        self._tally(result.status)
        self._bump(service.name, result.status)
        return result

    # ---- 批量 ----
    async def verify_all(self, resources: list[Resource]) -> None:
        await asyncio.gather(*(self.verify(r) for r in resources), return_exceptions=True)
        for res in resources:
            if res.verify is None:
                res.verify = VerifyResult(status=Status.UNKNOWN, method="error",
                                          note="校验异常，未取得结果")


def prune(
    resources: list[Resource],
    *,
    strict: bool = False,
    keep_unsupported: bool = True,
) -> tuple[list[Resource], int]:
    """剔除失效链接。

    strict=False（默认）：保留 存活/需码/码错 + 未校验(不支持的网盘)
    strict=True：只保留 存活/需码 —— 码错的、未知的、不支持验活的一律剔除
    """
    kept: list[Resource] = []
    for res in resources:
        st = res.status
        if st in (Status.ALIVE, Status.NEED_PWD):
            kept.append(res)
        elif st is Status.WRONG_PWD:
            if not strict:
                kept.append(res)
        elif st in (Status.UNSUPPORTED, Status.UNCHECKED, Status.UNKNOWN):
            if keep_unsupported and not strict:
                kept.append(res)
    return kept, len(resources) - len(kept)
