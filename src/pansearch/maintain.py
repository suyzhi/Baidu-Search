"""定时维护：加深 TG 索引 + 复验过期链接 —— 让"越用越全"自动发生。

为什么需要它（不是可有可无的锦上添花）：
    · TG 索引是召回主力（62.8 万条消息 / 27 万条含链接），但 index crawl --deepen
      得有人手动跑 —— 不跑，索引就停在最后一次手动维护的位置；
    · 验活缓存有 TTL，过期后**要等下一次搜索**才会重验。冷门关键词可能几个月
      没人搜，它下面的链接就几个月没被复核过（实测缓存里最老的条目已经 10 天）。
    这个模块把两件事变成"定时跑一次"，并且带锁、带状态、带报告 ——
    cron / launchd 只负责按点启动它。

用法：
    pansearch maintain                      # 手动跑一轮
    pansearch maintain --json report.json   # 顺便落一份报告
    ./scripts/install-schedule.sh           # 装成 launchd 定时任务（每 6 小时）
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .config import CACHE_DIR
from .dedupe import build_resources
from .models import PanType, RawHit
from .store import VerifyCache
from .verifiers import VerifierPool

STATE_PATH = CACHE_DIR / "maintain-state.json"
LOCK_PATH = CACHE_DIR / "maintain.lock"
# 锁多久算"上一个进程已经死了"：正常一轮不会超过这个数
LOCK_STALE_SECONDS = 3 * 3600


def rebuild_url(key: str) -> str | None:
    """把资源键还原成可验活的 URL。

    键的形态（见 normalize.resource_key）：baidu:1AbCdEf / quark:pan.quark.cn/s/xxx /
    115:115cdn.com/s/xxx。百度必须拼回 pan.baidu.com/s/<surl>（键里只存了 surl），
    其它网盘直接用原来的 host+path。
    """
    pan, _, rest = str(key or "").partition(":")
    if not rest or not pan:
        return None
    if pan == PanType.BAIDU.value:
        return f"https://pan.baidu.com/s/{rest}"
    return f"https://{rest}"


@dataclass
class MaintainReport:
    started_at: str = ""
    finished_at: str = ""
    elapsed: float = 0.0
    skipped: str | None = None
    index: dict = field(default_factory=dict)
    verify: dict = field(default_factory=dict)
    cache: dict = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ 状态与锁
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def last_run_age_hours() -> float | None:
    ts = load_state().get("last_run")
    return None if not ts else (time.time() - float(ts)) / 3600


def acquire_lock() -> bool:
    """抢占运行锁：两个定时任务重叠时，后到的直接跳过而不是互相打架。

    锁是"进程级"的：崩溃留下的陈旧锁（超过 LOCK_STALE_SECONDS）会被接管，
    否则一次 kill -9 就能让定时任务永久停摆。
    """
    if LOCK_PATH.exists():
        try:
            info = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            started = float(info.get("started_at") or 0)
        except (OSError, ValueError):
            started = LOCK_PATH.stat().st_mtime
        if time.time() - started < LOCK_STALE_SECONDS:
            return False
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(
        json.dumps({"pid": os.getpid(), "started_at": time.time()}), encoding="utf-8"
    )
    return True


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ 主体
def stale_resources(cache: VerifyCache, *, older_than_hours: float, limit: int,
                    dead_retry_days: float = 30.0, now: float | None = None
                    ) -> tuple[list, list[dict]]:
    """挑出这轮要复验的资源：**按"最久没验"排序**，并跳过已确认死掉的老链接。

    死链不会复活，反复验它们是纯浪费预算。超过 dead_retry_days 再验一次，
    是为了覆盖"分享者重新上传"这种小概率情况。
    """
    now = now or time.time()
    entries = cache.stale(older_than_hours=older_than_hours, limit=max(limit * 3, limit))
    picked: list[dict] = []
    hits: list[RawHit] = []
    for entry in entries:
        status = str(entry.get("status") or "")
        if status in ("dead", "not_found") and entry.get("age_hours", 0) < dead_retry_days * 24:
            continue
        url = rebuild_url(entry.get("key"))
        if not url:
            continue
        picked.append(entry)
        hits.append(RawHit(source="maintain", kind="recheck", url=url, pwd=entry.get("pwd")))
        if len(picked) >= limit:
            break
    return build_resources(hits), picked


async def run_maintenance(
    *,
    index_pages: int = 3,
    verify_limit: int = 150,
    budget: float = 240.0,
    min_interval_hours: float = 6.0,
    force: bool = False,
    skip_index: bool = False,
    skip_verify: bool = False,
    now: float | None = None,
) -> MaintainReport:
    started = time.monotonic()
    st = datetime.fromtimestamp(now or time.time(), tz=timezone.utc).isoformat(timespec="seconds")
    report = MaintainReport(started_at=st)

    age = last_run_age_hours()
    if not force and age is not None and age < min_interval_hours:
        report.skipped = f"距上次维护 {age:.1f} 小时 < {min_interval_hours:g} 小时"
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report.elapsed = round(time.monotonic() - started, 1)
        return report

    if not force and not acquire_lock():
        report.skipped = "已有维护在跑（锁被占用）"
        report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        report.elapsed = round(time.monotonic() - started, 1)
        return report

    try:
        # ---- ① 加深 TG 索引（长期资产：越挖越全）----
        if index_pages > 0 and not skip_index:
            t0 = time.monotonic()
            try:
                from .adapters.telegram import ensure_index

                stats = await ensure_index(pages=index_pages, deepen=True, concurrency=12)
                report.index = {k: v for k, v in stats.items() if k != "error"}
                if stats.get("error"):
                    report.errors.append(f"index: {stats['error']}")
            except Exception as exc:                      # noqa: BLE001
                report.errors.append(f"index: {type(exc).__name__}: {exc}")
            report.index["seconds"] = round(time.monotonic() - t0, 1)

        # ---- ② 复验过期链接（把"等用户搜到才验"变成"主动巡检"）----
        if verify_limit > 0 and not skip_verify:
            t0 = time.monotonic()
            from .config import verify_cfg

            ttl = float(verify_cfg().get("cache_ttl_hours") or 6)
            cache = VerifyCache(ttl_hours=ttl)
            try:
                before_pending = cache.pending_count(ttl)
                resources, picked = stale_resources(
                    cache, older_than_hours=ttl, limit=verify_limit, now=now
                )
                report.cache = {
                    "cached_links": cache.stats().get("cached_links", 0),
                    "expired_before": before_pending,
                }
                if resources:
                    remaining = max(5.0, budget - (time.monotonic() - started))
                    async with VerifierPool(cache=cache) as pool:
                        try:
                            await asyncio.wait_for(
                                pool.verify_all(resources, budget=0), timeout=remaining
                            )
                        except asyncio.TimeoutError:
                            report.errors.append(
                                f"verify: 超过本轮预算 {remaining:.0f}s，保留已完成部分"
                            )
                    stats = pool.stats
                    report.verify = {
                        "candidates": len(resources),
                        "checked": stats.get("checked", 0),
                        "alive": stats.get("alive", 0)
                        + stats.get("need_pwd", 0) + stats.get("wrong_pwd", 0),
                        "dead": stats.get("dead", 0) + stats.get("not_found", 0),
                        "unsupported": stats.get("unsupported", 0),
                        "errors": stats.get("error", 0),
                        "oldest_age_hours": picked[0]["age_hours"] if picked else None,
                    }
                else:
                    report.verify = {"candidates": 0, "checked": 0, "alive": 0, "dead": 0}
                report.verify["seconds"] = round(time.monotonic() - t0, 1)
                report.verify["expired_after"] = cache.pending_count(ttl)
            finally:
                cache.close()

        state = load_state()
        state.update({
            "last_run": time.time(),
            "last_report": asdict(report),
            "runs": int(state.get("runs") or 0) + 1,
        })
        save_state(state)
    finally:
        release_lock()

    report.elapsed = round(time.monotonic() - started, 1)
    report.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return report


__all__ = [
    "LOCK_PATH", "STATE_PATH", "MaintainReport", "acquire_lock", "last_run_age_hours",
    "load_state", "rebuild_url", "release_lock", "run_maintenance", "save_state",
    "stale_resources",
]
