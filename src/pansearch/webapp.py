"""Web UI：本地网页版搜索器（FastAPI + 零构建单页）。"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .config import verify_ttl_hours
from .extract import excerpt
from .logutil import get_logger, setup_logging
from .models import PanType
from .pipeline import SearchOutcome, search as run_search
from .store import VerifyCache

INDEX_HTML = Path(__file__).parent / "web" / "index.html"

logger = get_logger("webapp")

# --sfw / adult 过滤的接口说明（CLI 与 Web 的默认值刻意不同，见 README）
SFW_DESC = "过滤成人来源（javdb / sukebei，见 sources.yaml 的 adult_sources）；Web 端默认开启"

# Web 端**共用一条**验活缓存连接。
# 原来每个搜索请求都会在 pipeline 里 VerifierPool() 自建一个 VerifyCache —— 既白付
# 一次建连接 + schema/migrate 检查，又让并发请求各自持一条连接写同一张表
# （配合 store 里的 WAL + busy_timeout 才是真的安全）。这里在应用生命周期内复用一个实例。
_shared_cache: VerifyCache | None = None


def shared_cache() -> VerifyCache:
    """进程内共享的验活缓存（懒建；由 lifespan 负责关闭）。"""
    global _shared_cache
    if _shared_cache is None:
        _shared_cache = VerifyCache(ttl_hours=verify_ttl_hours())
    return _shared_cache


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _shared_cache
    shared_cache()      # 启动即建连：把 WAL / 迁移的一次性成本挪出请求路径
    try:
        yield
    finally:
        if _shared_cache is not None:
            _shared_cache.close()
            _shared_cache = None


app = FastAPI(title="pansearch", description="全网网盘资源搜索器", docs_url="/docs",
              lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


def _parse_types(types: str | None) -> list[PanType] | None:
    type_list: list[PanType] | None = None
    if types:
        type_list = []
        for token in types.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                type_list.append(PanType(token))
            except ValueError:
                # 不能静默跳过：一个都不认识时会退化成 types=None（= 不过滤），
                # 调用方以为限定了类型，实际拿到全部网盘的结果。
                raise HTTPException(status_code=422, detail=f"未知网盘类型: {token!r}")
        type_list = type_list or None

    return type_list


def _serialize(outcome: SearchOutcome, limit: int) -> dict:
    results = outcome.resources[:limit]
    # 全量结果（不止 limit 条）里每种网盘有多少条：前端据此在筛选芯片上显示数量，
    # 并在本地切换类型，不必为换个网盘类型把整次搜索重跑一遍。
    type_counts: dict[str, int] = {}
    for res in outcome.resources:
        type_counts[res.pan_type.value] = type_counts.get(res.pan_type.value, 0) + 1

    return {
        "type_counts": type_counts,
        "total": len(outcome.resources),
        "keyword": outcome.keyword,
        "raw_hits": outcome.raw_hits,
        "dedup_count": outcome.dedup_count,
        "pruned": outcome.pruned,
        "irrelevant_pruned": outcome.irrelevant_pruned,
        "verify_budget_skipped": outcome.verify_budget_skipped,
        "verify_timeout_skipped": outcome.verify_timeout_skipped,
        "timings": outcome.timings,
        "strict": outcome.strict,
        "shown": len(results),
        "alive_count": outcome.alive_count,
        "used_sources": outcome.used_sources,
        "queries_used": outcome.queries_used,
        "errors": outcome.errors,
        "degraded": outcome.degraded,
        "source_report": outcome.source_report,
        "from_cache": outcome.from_cache,
        # 过滤掉多少条要如实说，否则用户只会看到"结果变少了但不知道为什么"
        "nsfw_pruned": outcome.nsfw_pruned,
        "adult_pruned": outcome.adult_pruned,
        "verify_stats": outcome.verify_stats,
        "results": [
            {
                "title": res.title,
                "display_title": excerpt(res.title, outcome.keyword, 110),
                "pan_type": res.pan_type.value,
                "pan_label": res.pan_type.label,
                "url": res.open_url(),
                "copy_text": res.copy_text(),
                "pwd": res.pwd,
                "status": res.status.value,
                "status_label": res.status_label,
                "alive": res.status.alive,
                "pwd_verified": res.pwd_verified,
                "errno": res.verify.errno if res.verify else None,
                "note": res.verify.note if res.verify else None,
                "method": res.verify.method if res.verify else None,
                "sources": res.sources,
                "origins": res.origins,
                "hit_count": res.hit_count,
                "size": res.size,
                "shared_at": res.shared_at.strftime("%Y-%m-%d") if res.shared_at else None,
                "score": res.score,
            }
            for res in results
        ],
    }

@app.get("/api/search")
async def api_search(
    kw: str = Query(..., description="搜索关键词"),
    types: Optional[str] = Query(None, description="网盘类型，逗号分隔"),
    limit: int = Query(50, ge=1, le=500),
    alive_only: bool = Query(True),
    strict: bool = Query(False, description="严格模式：连无法验活的网盘一并剔除"),
    verify: bool = Query(True),
    sfw: bool = Query(True, description=SFW_DESC),
) -> JSONResponse:
    type_list = _parse_types(types)

    try:
        outcome = await run_search(
            kw,
            types=type_list,
            do_verify=verify,
            alive_only=alive_only,
            strict=strict,
            sfw=sfw,
            limit=None,
            cache=shared_cache(),
        )
    except Exception:
        # 能走到这里的通常是真正的程序 bug（数据源故障已经在 pipeline 内部被隔离成
        # errors/degraded），必须服务端留痕，而不是只让客户端看到一个 500。
        logger.exception("搜索失败（非流式）kw=%r types=%s sfw=%s", kw, types, sfw)
        raise
    return JSONResponse(_serialize(outcome, limit))


@app.get("/api/search/stream")
async def api_search_stream(
    kw: str = Query(..., min_length=1, max_length=500),
    types: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    alive_only: bool = Query(True),
    strict: bool = Query(False),
    verify: bool = Query(True),
    sfw: bool = Query(True, description=SFW_DESC),
) -> StreamingResponse:
    type_list = _parse_types(types)

    async def events():
        # 最多缓存最新一帧；客户端慢也不会积累大量完整结果快照。
        queue = asyncio.Queue(maxsize=1)

        async def emit(event, data):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait({"event": event, "data": data})

        async def progress(outcome):
            await emit("progress", _serialize(outcome, limit))

        async def run():
            try:
                outcome = await run_search(kw, types=type_list, do_verify=verify,
                                           alive_only=alive_only, strict=strict,
                                           sfw=sfw, on_progress=progress,
                                           cache=shared_cache())
                await emit("complete", _serialize(outcome, limit))
            except Exception as exc:
                # 这个 except 原来是"静默吞掉"：异常变成推给**这一个**客户端的文案就
                # 结束了，服务端零留痕（uvicorn 的 access log 只记请求）。真正的程序
                # bug 因此表现为"用户说搜不出来、日志里什么都没有"。
                logger.exception("流式搜索失败 kw=%r types=%s sfw=%s", kw, types, sfw)
                await emit("error", {"message": str(exc)})

        task = asyncio.create_task(run())
        try:
            yield json.dumps({"event": "start"}) + "\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield json.dumps({"event": "heartbeat"}) + "\n"
                    continue
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event["event"] in ("complete", "error"):
                    break
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(events(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    # 先装好项目日志，再起服务：Web 层的异常才有地方落。
    setup_logging()
    logger.info("pansearch Web UI: http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
