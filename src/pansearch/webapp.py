"""Web UI：本地网页版搜索器（FastAPI + 零构建单页）。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .extract import excerpt
from .models import PanType
from .pipeline import search as run_search

INDEX_HTML = Path(__file__).parent / "web" / "index.html"

app = FastAPI(title="pansearch", description="全网网盘资源搜索器", docs_url="/docs")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/search")
async def api_search(
    kw: str = Query(..., description="搜索关键词"),
    types: Optional[str] = Query(None, description="网盘类型，逗号分隔"),
    limit: int = Query(50, ge=1, le=500),
    alive_only: bool = Query(True),
    strict: bool = Query(False, description="严格模式：连无法验活的网盘一并剔除"),
    verify: bool = Query(True),
) -> JSONResponse:
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
                continue
        type_list = type_list or None

    outcome = await run_search(
        kw,
        types=type_list,
        do_verify=verify,
        alive_only=alive_only,
        strict=strict,
        limit=None,
    )
    results = outcome.resources[:limit]

    return JSONResponse(
        {
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
                    "display_title": excerpt(res.title, kw, 110),
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
    )


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port, log_level="info")
