"""百度网盘分享链接验活。

核心（实测校准于 2026-09-12）：
  * surl 参数必须去掉开头那个 '1'，否则恒返回 105。
  * 有提取码 → POST /share/verify（权威）：0=码正确 / -12=码错 / 105=不存在
  * 无提取码 → GET /api/shorturlinfo：2=需提取码 / 0=无需码 / 140=不存在 / -3=失效

全部免登录、无验证码。
"""

from __future__ import annotations

import asyncio
import time

import httpx

from .config import errno_default, errno_map, verify_cfg
from .models import PanType, Resource, Status, VerifyResult
from .normalize import strip_leading_one
from .store import VerifyCache
from .util import RateLimiter

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
PAN_ORIGIN = "https://pan.baidu.com"


class BaiduVerifier:
    def __init__(self, cfg: dict | None = None, cache: VerifyCache | None = None):
        self.cfg = cfg or verify_cfg()
        self.sv_map = errno_map("share_verify")
        self.su_map = errno_map("shorturlinfo")
        self.default = errno_default()
        self.timeout = float(self.cfg.get("timeout") or 20)
        self.retries = int(self.cfg.get("retries") or 2)
        self.cache = cache if cache is not None else VerifyCache(
            ttl_hours=float(self.cfg.get("cache_ttl_hours") or 6)
        )
        self.limiter = RateLimiter(float(self.cfg.get("rate_limit_qps") or 3.0))
        self.sem = asyncio.Semaphore(int(self.cfg.get("concurrency") or 4))
        self._client: httpx.AsyncClient | None = None
        self._warmed = False
        self.stats = {"checked": 0, "cache_hit": 0, "alive": 0, "dead": 0, "error": 0}

    async def __aenter__(self) -> BaiduVerifier:
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            http2=True,
            headers={
                "User-Agent": UA,
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": PAN_ORIGIN + "/",
            },
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _warmup(self) -> None:
        """取一次 BAIDUID 等 Cookie，share/verify 依赖它。"""
        if self._warmed or self._client is None:
            return
        try:
            await self._client.get(PAN_ORIGIN + "/")
        except httpx.HTTPError:
            pass
        self._warmed = True

    @staticmethod
    def _status_from(value: str) -> Status:
        try:
            return Status(value)
        except ValueError:
            return Status.UNKNOWN

    async def _request(self, method: str, url: str, **kw) -> httpx.Response | None:
        assert self._client is not None
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            await self.limiter.acquire()
            try:
                return await self._client.request(method, url, **kw)
            except httpx.HTTPError as exc:  # 网络抖动 → 退避重试
                last = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.6 * (attempt + 1))
        if last is not None:
            return None
        return None

    async def _share_verify(self, bare: str, pwd: str) -> tuple[int | None, str | None]:
        ts = int(time.time() * 1000)
        url = (
            f"{PAN_ORIGIN}/share/verify?surl={bare}&t={ts}"
            "&channel=chunlei&web=1&app_id=250528&clienttype=0"
        )
        resp = await self._request(
            "POST",
            url,
            data={"pwd": pwd, "vcode": "", "vcode_str": ""},
            headers={
                "Referer": f"{PAN_ORIGIN}/share/init?surl={bare}",
                "Origin": PAN_ORIGIN,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        if resp is None or resp.status_code != 200:
            return None, f"http={getattr(resp, 'status_code', 'err')}"
        try:
            payload = resp.json()
        except ValueError:
            return None, "非 JSON 响应"
        return payload.get("errno"), None

    async def _shorturlinfo(self, bare: str) -> tuple[int | None, str | None]:
        ts = int(time.time() * 1000)
        url = (
            f"{PAN_ORIGIN}/api/shorturlinfo?shorturl={bare}&web=1"
            f"&app_id=250528&channel=chunlei&clienttype=0&t={ts}"
        )
        resp = await self._request("GET", url)
        if resp is None or resp.status_code != 200:
            return None, f"http={getattr(resp, 'status_code', 'err')}"
        try:
            payload = resp.json()
        except ValueError:
            return None, "非 JSON 响应"
        return payload.get("errno"), None

    async def verify(self, res: Resource, *, use_cache: bool = True) -> VerifyResult:
        """对一个 Resource 验活；非百度资源标记 unsupported。"""
        if res.pan_type is not PanType.BAIDU or not res.surl:
            return VerifyResult(status=Status.UNSUPPORTED, note="仅校验百度网盘")

        if use_cache:
            hit = self.cache.get(res.surl, res.pwd)
            if hit is not None:
                self.stats["cache_hit"] += 1
                if hit.status.alive:
                    self.stats["alive"] += 1
                elif hit.status in (Status.DEAD, Status.NOT_FOUND):
                    self.stats["dead"] += 1
                res.verify = hit          # 必须写回，否则会被当成"未校验"
                return hit

        async with self.sem:
            await self._warmup()

            if res.pwd:
                # share/verify 必须用「去掉开头 1」的 surl
                bare = strip_leading_one(res.surl)
                errno, err = await self._share_verify(bare, res.pwd)
                if errno is None:
                    result = VerifyResult(status=Status.UNKNOWN, method="share_verify", note=err)
                else:
                    status = self._status_from(str(self.sv_map.get(errno, self.default)))
                    note = None
                    if status is Status.WRONG_PWD:
                        # 码不对 ≠ 链接失效：用 shorturlinfo 查链接本身是否还在
                        alt_errno, _ = await self._shorturlinfo(res.surl)
                        alt = self._status_from(str(self.su_map.get(alt_errno, self.default)))
                        if alt in (Status.ALIVE, Status.NEED_PWD):
                            note = f"链接存活，但提取码不正确（shorturlinfo errno={alt_errno}）"
                        else:
                            status = alt
                            note = f"提取码错误且链接不可用（shorturlinfo errno={alt_errno}）"
                    result = VerifyResult(
                        status=status,
                        errno=errno,
                        method="share_verify",
                        note=note,
                        # share/verify 返回 0 说明提取码确实被服务端校验通过
                        pwd_verified=status is Status.ALIVE,
                    )
            else:
                # shorturlinfo 必须用「完整 token（带开头 1）」，否则恒返回 2（假阳性）
                errno, err = await self._shorturlinfo(res.surl)
                if errno is None:
                    result = VerifyResult(status=Status.UNKNOWN, method="shorturlinfo", note=err)
                else:
                    result = VerifyResult(
                        status=self._status_from(str(self.su_map.get(errno, self.default))),
                        errno=errno,
                        method="shorturlinfo",
                    )

        self.stats["checked"] += 1
        if result.status.alive:
            self.stats["alive"] += 1
        elif result.status in (Status.DEAD, Status.NOT_FOUND):
            self.stats["dead"] += 1
        else:
            self.stats["error"] += 1

        if result.status is not Status.UNKNOWN:
            self.cache.put(res.surl, result, res.pwd)
        res.verify = result
        return result


async def verify_all(resources: list[Resource], verifier: BaiduVerifier) -> None:
    targets = [r for r in resources if r.pan_type is PanType.BAIDU and r.surl]
    target_ids = {id(r) for r in targets}
    for r in resources:
        if id(r) not in target_ids:
            r.verify = VerifyResult(status=Status.UNSUPPORTED, note="仅校验百度网盘")
    if not targets:
        return
    results = await asyncio.gather(
        *(verifier.verify(r) for r in targets), return_exceptions=True
    )
    # 兜底：任何未被写入结果的（异常等）标记为未知，避免静默漏校验
    for res, result in zip(targets, results):
        if isinstance(result, BaseException):
            res.verify = VerifyResult(
                status=Status.UNKNOWN, method="error", note=f"{type(result).__name__}: {result}"
            )
        elif res.verify is None:
            res.verify = result
