"""Web UI：本地网页版搜索器（FastAPI + 零构建单页）。"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .extract import excerpt
from .models import PanType
from .pipeline import SearchOutcome, search as run_search

INDEX_HTML = Path(__file__).parent / "web" / "index.html"

app = FastAPI(title="pansearch", description="全网网盘资源搜索器", docs_url="/docs")


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

    return {
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
) -> JSONResponse:
    type_list = _parse_types(types)

    outcome = await run_search(
        kw,
        types=type_list,
        do_verify=verify,
        alive_only=alive_only,
        strict=strict,
        limit=None,
    )
    return JSONResponse(_serialize(outcome, limit))


@app.get("/api/search/stream")
async def api_search_stream(
    kw: str = Query(..., min_length=1, max_length=500),
    types: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    alive_only: bool = Query(True),
    strict: bool = Query(False),
    verify: bool = Query(True),
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
                                           on_progress=progress)
                await emit("complete", _serialize(outcome, limit))
            except Exception as exc:
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

    uvicorn.run(app, host=host, port=port, log_level="info")
