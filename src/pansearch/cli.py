"""命令行入口：pansearch search / verify / stats"""

from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path
from typing import Optional

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .models import PanType, Resource, Status
from .pipeline import search as run_search
from .store import VerifyCache

app = typer.Typer(add_completion=False, help="全网网盘资源搜索器（百度优先 + 全类型兜底）")
console = Console()

STATUS_STYLE = {
    Status.ALIVE: "bold green",
    Status.NEED_PWD: "green",
    Status.WRONG_PWD: "yellow",
    Status.UNKNOWN: "yellow",
    Status.UNCHECKED: "dim",
    Status.UNSUPPORTED: "dim",
    Status.DEAD: "red",
    Status.NOT_FOUND: "red",
}

TYPE_ALIASES = {
    "bd": "baidu", "baidupan": "baidu", "百度": "baidu", "百度网盘": "baidu",
    "夸克": "quark", "阿里": "aliyun", "阿里云盘": "aliyun",
    "迅雷": "xunlei", "天翼": "tianyi", "磁力": "magnet",
}


def _parse_types(value: str | None) -> list[PanType] | None:
    if not value:
        return None
    out: list[PanType] = []
    for part in value.replace("，", ",").split(","):
        token = part.strip().lower()
        if not token:
            continue
        token = TYPE_ALIASES.get(token, token)
        try:
            out.append(PanType(token))
        except ValueError:
            valid = ", ".join(t.value for t in PanType)
            raise typer.BadParameter(f"未知网盘类型 {part!r}；可选：{valid}")
    return out or None


def _truncate(text: str | None, width: int) -> str:
    if not text:
        return ""
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _render(resources: list[Resource], outcome, *, show_origins: bool) -> None:
    """列表式输出：一行状态、一行标题、一行链接、一行提取码，方便直接复制。"""
    for i, res in enumerate(resources, 1):
        style = STATUS_STYLE.get(res.status, "")
        head = (
            f"[{style}]{res.status.label}[/{style}]"
            if style
            else res.status.label
        )
        meta = [res.pan_type.label]
        if res.hit_count > 1:
            meta.append(f"命中{res.hit_count}次")
        if res.sources:
            meta.append(_truncate("、".join(res.sources[:2]), 40))
        if res.size:
            meta.append(res.size)
        if res.shared_at:
            meta.append(res.shared_at.strftime("%Y-%m-%d"))

        console.print(f"[bold cyan]{i:>2}.[/bold cyan] {head} [dim]· {' · '.join(meta)}[/dim]")
        if res.title:
            console.print(f"    [white]{_truncate(res.title, 100)}[/white]")
        console.print(f"    {res.open_url()}")
        if res.pwd:
            console.print(f"    [bold yellow]提取码: {res.pwd}[/bold yellow]")
        if show_origins and res.origins:
            console.print(f"    [dim]来源页: {res.origins[0]}[/dim]")
        console.print()


def _summary(outcome, resources: list[Resource]) -> Panel:
    by_type: dict[str, int] = {}
    for res in resources:
        by_type[res.pan_type.label] = by_type.get(res.pan_type.label, 0) + 1
    parts = [f"{k} {v}" for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])]
    vstats = outcome.verify_stats or {}
    lines = [
        f"关键词 [bold]{outcome.keyword}[/bold] ｜ 数据源 {', '.join(outcome.used_sources) or '-'}",
        f"原始命中 [bold]{outcome.raw_hits}[/bold] → 去重 [bold]{outcome.dedup_count}[/bold] → 本次显示 [bold]{len(resources)}[/bold]"
        + (f"（{ '、'.join(parts) }）" if parts else ""),
    ]
    if vstats:
        lines.append(
            f"验活：存活 [green]{vstats.get('alive', 0)}[/green] ｜ "
            f"失效 [red]{vstats.get('dead', 0)}[/red] ｜ "
            f"缓存命中 {vstats.get('cache_hit', 0)} ｜ "
            f"实际请求 {vstats.get('checked', 0)}"
        )
    if outcome.errors:
        for name, err in outcome.errors.items():
            lines.append(f"[yellow]⚠ {name} 失败：{_truncate(err, 120)}[/yellow]")
    return Panel("\n".join(lines), title="pansearch", border_style="cyan")


@app.command()
def search(
    keyword: str = typer.Argument(..., help="搜索关键词"),
    type: Optional[str] = typer.Option(None, "--type", "-t", help="限定网盘类型，逗号分隔（baidu,quark,aliyun,115,xunlei,tianyi,123,uc,pikpak,magnet）"),
    source: Optional[str] = typer.Option(None, "--source", "-s", help="限定数据源（如 pansou）"),
    limit: int = typer.Option(30, "--limit", "-n", help="最多显示多少条"),
    show_all: bool = typer.Option(False, "--all", "-a", help="包含已失效的结果"),
    no_verify: bool = typer.Option(False, "--no-verify", help="跳过有效性校验（快很多，但不知道链接死活）"),
    show_origins: bool = typer.Option(False, "--origins", help="额外打印来源页面"),
    json_out: Optional[Path] = typer.Option(None, "--json", help="结果导出为 JSON"),
    csv_out: Optional[Path] = typer.Option(None, "--csv", help="结果导出为 CSV"),
) -> None:
    """搜索全网网盘资源（默认只显示存活链接）。"""
    types = _parse_types(type)
    sources = [s.strip() for s in source.split(",")] if source else None

    with console.status("[cyan]正在并发检索全网数据源…[/cyan]"):
        outcome = asyncio.run(
            run_search(
                keyword,
                types=types,
                source_names=sources,
                do_verify=not no_verify,
                alive_only=not show_all,
                limit=None,
            )
        )

    resources = outcome.resources[:limit] if limit else outcome.resources

    if not resources:
        console.print(_summary(outcome, resources))
        console.print("[yellow]没有找到结果。可以尝试：换关键词 / 去掉 --type 限制 / 加 --all 看失效链接。[/yellow]")
        return

    _render(resources, outcome, show_origins=show_origins)
    console.print(_summary(outcome, resources))

    cache = VerifyCache()
    cache.log_search(keyword, len(resources), outcome.alive_count)
    cache.close()

    if json_out:
        payload = {
            "keyword": outcome.keyword,
            "raw_hits": outcome.raw_hits,
            "used_sources": outcome.used_sources,
            "errors": outcome.errors,
            "verify_stats": outcome.verify_stats,
            "results": [
                {
                    **res.to_row(),
                    "score": res.score,
                    "key": res.key,
                    "surl": res.surl,
                    "pwd": res.pwd,
                    "status": res.status.value,
                    "errno": (res.verify.errno if res.verify else None),
                    "sources": res.sources,
                    "origins": res.origins,
                    "hit_count": res.hit_count,
                }
                for res in resources
            ],
        }
        json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[green]已导出 JSON → {json_out}[/green]")

    if csv_out:
        rows = [res.to_row() for res in resources]
        with csv_out.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        console.print(f"[green]已导出 CSV → {csv_out}[/green]")


@app.command()
def verify(
    url: str = typer.Argument(..., help="百度网盘分享链接"),
    pwd: Optional[str] = typer.Option(None, "--pwd", "-p", help="提取码"),
    no_cache: bool = typer.Option(False, "--no-cache", help="忽略缓存重新校验"),
) -> None:
    """校验单条百度网盘链接是否有效。"""
    from .dedupe import build_resources
    from .models import RawHit
    from .verify import BaiduVerifier

    res = build_resources(
        [RawHit(source="cli", kind="manual", url=url, pwd=pwd)]
    )
    if not res:
        console.print("[red]无法识别的链接（目前只支持百度网盘分享链接）[/red]")
        raise typer.Exit(1)

    target = res[0]

    async def _run():
        async with BaiduVerifier() as verifier:
            return await verifier.verify(target, use_cache=not no_cache)

    result = asyncio.run(_run())
    style = STATUS_STYLE.get(result.status, "")
    console.print(
        f"[{style}]{result.status.label}[/{style}]  "
        f"surl={target.surl}  errno={result.errno}  method={result.method}"
        + (f"  [dim]{result.note}[/dim]" if result.note else "")
    )


@app.command()
def stats() -> None:
    """查看本地缓存与历史搜索统计。"""
    cache = VerifyCache()
    info = cache.stats()
    console.print(Panel(json.dumps(info, ensure_ascii=False, indent=2), title="本地索引"))
    rows = cache.conn.execute(
        "SELECT kw, COUNT(*), MAX(ts) FROM search_log GROUP BY kw ORDER BY MAX(ts) DESC LIMIT 20"
    ).fetchall()
    if rows:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("关键词")
        table.add_column("搜索次数", justify="right")
        for kw, cnt, _ in rows:
            table.add_row(kw, str(cnt))
        console.print(table)
    cache.close()


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", help="监听地址"),
    port: int = typer.Option(8765, help="监听端口"),
    no_browser: bool = typer.Option(False, "--no-browser", help="不自动打开浏览器"),
) -> None:
    """启动本地网页版搜索器。"""
    import threading
    import webbrowser

    from .webapp import serve

    url = f"http://{host}:{port}"
    console.print(Panel(f"[bold green]pansearch Web UI[/bold green] → [link={url}]{url}[/link]\n[dim]Ctrl+C 退出[/dim]", border_style="cyan"))
    if not no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    serve(host, port)


@app.command()
def sources() -> None:
    """列出当前启用的数据源。"""
    from .pipeline import build_adapters

    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("数据源")
    table.add_column("类型")
    table.add_column("权重", justify="right")
    for adapter in build_adapters():
        table.add_row(adapter.name, adapter.kind, f"{adapter.weight:g}")
    console.print(table)


@app.callback(invoke_without_command=True)
def _root(
    version: bool = typer.Option(False, "--version", "-V", help="显示版本"),
    ctx: typer.Context = None,
) -> None:
    if version:
        console.print(f"pansearch {__version__}")
        raise typer.Exit()
    if ctx is not None and ctx.invoked_subcommand is None:
        console.print(
            Panel(
                "[bold]pansearch[/bold] —— 全网网盘资源搜索器\n\n"
                "  pansearch web                  启动本地网页版搜索器\n"
                "  pansearch search \"关键词\"      命令行搜索（默认只显示存活链接）\n"
                "  pansearch verify <链接> -p 提取码   校验单条链接\n"
                "  pansearch sources              列出启用的数据源\n"
                "  pansearch stats                本地缓存统计\n\n"
                "[dim]加 --help 查看完整参数[/dim]",
                border_style="cyan",
            )
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
