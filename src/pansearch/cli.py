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
from .extract import excerpt
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
            f"[{style}]{res.status_label}[/{style}]"
            if style
            else res.status_label
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
            console.print(f"    [white]{excerpt(res.title, outcome.keyword, 100)}[/white]")
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
    if len(outcome.queries_used) > 1:
        lines.append(
            f"[dim]原始查询召回不足，已自动补搜：{'、'.join(outcome.queries_used[1:])}[/dim]"
        )
    if outcome.irrelevant_pruned:
        lines.append(f"已过滤明确无关结果 {outcome.irrelevant_pruned} 条")
    if outcome.verify_timeout_skipped:
        lines.append(f"验活等待已结束，保留 {outcome.verify_timeout_skipped} 条未校验结果")
    if vstats:
        pruned = outcome.pruned
        lines.append(
            f"验活：存活 [green]{vstats.get('alive', 0)}[/green] ｜ "
            f"失效 [red]{vstats.get('dead', 0)}[/red] ｜ "
            f"剔除 [bold]{pruned}[/bold] ｜ "
            f"缓存命中 {vstats.get('cache_hit', 0)} ｜ "
            f"实际请求 {vstats.get('checked', 0)}"
        )
        by_service = vstats.get("by_service") or {}
        if by_service:
            seg = []
            for name, s in sorted(by_service.items(), key=lambda kv: -kv[1]["checked"]):
                seg.append(
                    f"{name} {s['alive']}活/{s['dead']}死"
                    + (f"/{s['other']}其他" if s.get("other") else "")
                )
            lines.append("[dim]按网盘：" + " ｜ ".join(seg) + "[/dim]")
        unsupported = vstats.get("unsupported", 0)
        if unsupported and outcome.strict:
            lines.append(
                f"[dim]已按严格模式剔除 {unsupported} 条不支持验活的网盘"
                f"（迅雷/UC/123/PikPak/磁力）[/dim]"
            )
        elif unsupported:
            lines.append(
                f"[yellow]⚠ 保留 {unsupported} 条不支持验活的网盘结果"
                f"（迅雷/UC/123/PikPak/磁力），它们**未经验证**，可能已失效；"
                f"加 --strict 可一并剔除[/yellow]"
            )
    if outcome.degraded:
        lines.append(
            f"[bold yellow]⚠ 召回降级：{'、'.join(outcome.degraded)} 失败/超时，"
            f"本次结果可能不全[/bold yellow]"
        )
    if getattr(outcome, "from_cache", False):
        lines.append("[dim]（来自查询缓存，结果与上一次同词搜索一致）[/dim]")
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
    strict: bool = typer.Option(False, "--strict", help="严格模式：连码错的、无法验活的也一并剔除"),
    relax: bool = typer.Option(True, "--relax/--no-relax", help="多词查询时自动补搜主词（默认开，大幅提升召回）"),
    verify_budget: int = typer.Option(None, "--verify-budget", help="最多验活多少条（默认取配置，0=不限制）"),
    no_verify: bool = typer.Option(False, "--no-verify", help="跳过有效性校验（快很多，但不知道链接死活）"),
    show_origins: bool = typer.Option(False, "--origins", help="额外打印来源页面"),
    json_out: Optional[Path] = typer.Option(None, "--json", help="结果导出为 JSON"),
    csv_out: Optional[Path] = typer.Option(None, "--csv", help="结果导出为 CSV"),
) -> None:
    """搜索全网网盘资源（默认只显示存活链接）。"""
    types = _parse_types(type)
    sources = [s.strip() for s in source.split(",")] if source else None

    with console.status("[cyan]正在并发检索全网数据源并校验链接有效性…[/cyan]"):
        outcome = asyncio.run(
            run_search(
                keyword,
                types=types,
                source_names=sources,
                do_verify=not no_verify,
                alive_only=not show_all,
                strict=strict,
                relax=relax,
                verify_budget=verify_budget,
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
    url: str = typer.Argument(..., help="网盘分享链接"),
    pwd: Optional[str] = typer.Option(None, "--pwd", "-p", help="提取码"),
    no_cache: bool = typer.Option(False, "--no-cache", help="忽略缓存重新校验"),
) -> None:
    """校验单条网盘链接是否有效（支持百度/夸克/阿里/115/天翼）。"""
    from .dedupe import build_resources
    from .models import RawHit
    from .verifiers import VerifierPool

    resources = build_resources([RawHit(source="cli", kind="manual", url=url, pwd=pwd)])
    if not resources:
        console.print("[red]无法识别的链接（支持百度网盘 / 夸克 / 阿里云盘 / 115 / 天翼189）[/red]")
        raise typer.Exit(1)

    target = resources[0]

    async def _run():
        async with VerifierPool() as pool:
            if not pool.supports(target.pan_type):
                return None
            return await pool.verify(target, use_cache=not no_cache)

    result = asyncio.run(_run())
    if result is None:
        console.print(
            f"[yellow]{target.pan_type.label} 暂不支持验活（接口需要验证码或已变更）[/yellow]"
        )
        raise typer.Exit(2)

    style = STATUS_STYLE.get(result.status, "")
    detail = f"method={result.method}"
    if result.errno is not None:
        detail += f"  errno={result.errno}"
    if result.status is Status.ALIVE:
        label = ("有效(码已验证)" if result.pwd_verified
                 else ("有效(码未验证)" if target.pwd else "有效"))
    else:
        label = result.status.label
    console.print(
        f"[{style}]{label}[/{style}]  {target.pan_type.label}  {detail}"
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


index_app = typer.Typer(help="Telegram 频道索引：越挖越全，之后检索毫秒级")
app.add_typer(index_app, name="index")


@index_app.command("crawl")
def index_crawl(
    pages: int = typer.Option(1, "--pages", "-p", help="每个频道抓多少页（每页约 20 条）"),
    deepen: bool = typer.Option(False, "--deepen", help="从上次挖到的位置继续向历史深挖"),
    concurrency: int = typer.Option(12, "--concurrency", "-c", help="并发抓取数"),
) -> None:
    """抓取 TG 频道消息进本地索引。"""
    from .adapters.telegram import ensure_index
    from .tgindex import load_channels

    channels = load_channels()
    if not channels:
        console.print("[red]config/tg_channels.txt 为空[/red]")
        raise typer.Exit(1)

    mode = "向历史深挖" if deepen else "抓取最新"
    console.print(f"[cyan]{mode}：{len(channels)} 个频道 × {pages} 页…[/cyan]")
    with console.status("[cyan]抓取中…[/cyan]"):
        stats = asyncio.run(ensure_index(pages=pages, deepen=deepen, concurrency=concurrency))

    if "error" in stats:
        console.print(f"[red]{stats['error']}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]完成[/green]：频道 {stats['channels']} 个 ｜ 页面 {stats['pages']} ｜ "
        f"消息 {stats['messages']} 条 ｜ 新增 {stats['new']} 条 ｜ 失败 {stats['errors']} 个"
        + (f" ｜ 频道名归并 {stats['rekeyed']} 处" if stats.get("rekeyed") else "")
    )


@index_app.command("build-fts")
def index_build_fts() -> None:
    """建立/重建 FTS5 全文索引（CJK bigram）。

    一次性迁移：把 62 万条消息编码成 bigram 写进 FTS5，之后检索走倒排索引 +
    BM25（毫秒级、真 IDF），新消息由触发器增量维护。旧库在完成前会继续用
    `LIKE` 回退，不会影响使用。
    """
    from .tgindex import TgIndex

    index = TgIndex()
    try:
        if index.fts_ready():
            console.print("[cyan]FTS 索引已存在，将重建以纳入全部历史消息…[/cyan]")
        with console.status("[cyan]正在回填 tokens 并重建倒排索引（需要一两分钟）…[/cyan]"):
            info = index.build_fts()
    finally:
        index.close()
    console.print(
        f"[green]完成[/green]：已索引 {info['fts_docs']} 条含链接的消息；"
        f"检索现在走 BM25（可用 pansearch index stats 复核）"
    )


@index_app.command("stats")
def index_stats() -> None:
    """查看 TG 索引覆盖情况。"""
    from .tgindex import TgIndex, load_channels

    index = TgIndex()
    try:
        info = index.stats()
        rows = index.channel_rows()
        fts_ready = index.fts_ready()
    finally:
        index.close()

    total_channels = len(load_channels())
    console.print(
        Panel(
            f"已索引消息 [bold]{info['messages']}[/bold] 条"
            f"（其中 [bold]{info['messages_with_links']}[/bold] 条含网盘链接）\n"
            f"有数据的频道 [bold]{info['channels_indexed']}[/bold] / {total_channels}\n"
            f"FTS5 全文索引：{'[green]已建[/green]' if fts_ready else '[yellow]未建[/yellow]（运行 pansearch index build-fts）'}\n"
            f"数据库：{info['db']}",
            title="TG 索引",
            border_style="cyan",
        )
    )
    if rows:
        table = Table(box=box.SIMPLE, header_style="bold cyan")
        table.add_column("频道")
        table.add_column("消息数", justify="right")
        table.add_column("最新 ID", justify="right")
        table.add_column("最旧 ID", justify="right")
        table.add_column("状态")
        for channel, count, newest, oldest, status in rows[:25]:
            table.add_row(channel, str(count), str(newest or "-"), str(oldest or "-"),
                          str(status or "-"))
        console.print(table)


sites_app = typer.Typer(help="资源站目录：探测 / 列表 / 健康度 / 垂直路由")
app.add_typer(sites_app, name="sites")


@sites_app.command("list")
def sites_list(
    vertical: Optional[str] = typer.Option(None, "--vertical", "-v", help="只看某个垂直领域"),
) -> None:
    """列出资源站目录。"""
    from .sitecatalog import load_catalog

    catalog = load_catalog()
    if not catalog:
        console.print("[yellow]目录为空。用 pansearch sites probe <域名...> 探测并写入。[/yellow]")
        return
    rows = [s for s in catalog if not vertical or vertical in s.verticals]
    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("站点")
    table.add_column("垂直领域")
    table.add_column("搜索模板")
    table.add_column("已验证")
    for entry in sorted(rows, key=lambda x: (x.verticals[:1], x.host)):
        table.add_row(entry.name, ",".join(entry.verticals) or "-",
                      entry.search or "-", "✓" if entry.verified else "✗")
    console.print(table)
    console.print(f"[dim]共 {len(rows)} 个站点[/dim]")


@sites_app.command("probe")
def sites_probe(
    domains: list[str] = typer.Argument(..., help="要探测的域名"),
    vertical: str = typer.Option("general", "--vertical", "-v", help="写入目录时标注的垂直领域"),
    concurrency: int = typer.Option(6, "--concurrency", "-c"),
    save: bool = typer.Option(True, "--save/--no-save", help="把探到的结果写进 config/sites.yaml"),
) -> None:
    """探测域名可用的搜索 URL 模板，并可选写入目录。

    判据是「真词 vs 无意义串」对照：真词的结果条目要明显多于噪声。
    只看页面里有没有关键词会误判（很多站不回显关键词）。
    """
    import asyncio as _asyncio

    from .sitecatalog import load_catalog, probe_many, save_catalog

    console.print(f"[cyan]探测 {len(domains)} 个域名（每个最多试 21 种搜索模板）…[/cyan]")

    def show(r) -> None:
        if r.ok:
            console.print(f"  [green]✓[/green] {r.domain:<26} {r.template:<46} "
                          f"真={r.real_hits} 噪={r.noise_hits}")
        else:
            console.print(f"  [dim]✗ {r.domain:<26} {r.error}[/dim]")

    results = _asyncio.run(probe_many(domains, concurrency=concurrency, on_result=show))
    good = [r for r in results if r.ok]

    if save and good:
        from .sitecatalog import SiteEntry
        existing = {e.host: e for e in load_catalog()}
        for r in good:
            existing[r.domain] = SiteEntry(
                name=r.domain.split(".")[-2] if r.domain.count(".") >= 1 else r.domain,
                domain=r.domain,
                search=r.search,
                # 用探测器**推导出来**的详情页正则；留空该站就不参与搜索。
                # 之前这里是复用旧条目的值，但旧条目可能已被批量重探删掉，
                # 结果写出一个 result_re 为空、实际不可用的条目。
                result_re=r.result_re or (
                    existing.get(r.domain).result_re if existing.get(r.domain) else ""
                ),
                verticals=[vertical],
                verified=True,
                note=f"probe {r.template} yield={r.link_yield}",
            )
        path = save_catalog(list(existing.values()))
        console.print(f"[green]已写入 {path}[/green]（{len(good)} 个可用）")
    elif save:
        console.print("[yellow]没有探到可用模板，目录未改动。[/yellow]")


@sites_app.command("probe-all")
def sites_probe_all(
    cand_file: str = typer.Option("config/sites_candidates.txt", "--file", "-f",
                                  help="候选清单（每行：域名 [垂直领域,领域]）"),
    concurrency: int = typer.Option(8, "--concurrency", "-c"),
    budget: float = typer.Option(75.0, "--budget", help="每个域名的探测墙钟预算（秒）"),
) -> None:
    """批量探测候选域名并**合并**进目录。

    关键：合并语义，不是替换。探测会因为网络抖动偶发失败，
    如果用"这轮没探到就删掉"的语义，可用站点会被误删 ——
    实测 www.ypojie.com 上一轮 y=6 通过，下一轮被判 no-pattern，
    隔几秒重试又是 y=6。真正的失效交给运行时的健康度去淘汰。
    """
    import asyncio as _asyncio
    from pathlib import Path as _Path

    import httpx as _httpx

    from .sitecatalog import (
        UA, SiteEntry, load_catalog, probe_domain, save_catalog,
    )

    path = _Path(cand_file)
    if not path.exists():
        console.print(f"[red]候选清单不存在：{path}[/red]")
        raise typer.Exit(1)

    cands: list[tuple[str, list[str]]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        verticals = [v for v in parts[1].split(",") if v] if len(parts) > 1 else ["general"]
        cands.append((parts[0], verticals or ["general"]))

    existing = {e.host: e for e in load_catalog()}
    known = len(existing)
    console.print(f"[cyan]探测 {len(cands)} 个候选（并发 {concurrency}，每域名预算 {budget:.0f}s）…[/cyan]")

    async def _run() -> None:
        sem = _asyncio.Semaphore(concurrency)
        lock = _asyncio.Lock()
        ok = 0
        done = 0

        async with _httpx.AsyncClient(
            follow_redirects=True, http2=True, timeout=20,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"},
        ) as client:
            async def one(domain: str, verticals: list[str]) -> None:
                nonlocal ok, done
                async with sem:
                    result = await probe_domain(client, domain, budget=budget)
                async with lock:
                    done += 1
                    if result.ok and result.result_re and result.link_yield > 0:
                        ok += 1
                        existing[result.domain] = SiteEntry(
                            name=result.domain.split(".")[-2],
                            domain=result.domain,
                            search=result.search,
                            result_re=result.result_re,
                            verticals=verticals,
                            verified=True,
                            note=f"probe {result.template} yield={result.link_yield}",
                        )
                        console.print(f"[{done:>3}/{len(cands)}] [green]OK[/green] "
                                      f"{result.domain:<28} y={result.link_yield:<4} "
                                      f"{result.template:<40} {result.result_re[:44]}")
                    else:
                        console.print(f"[{done:>3}/{len(cands)}] [dim]--  {domain:<28} "
                                      f"{result.error or 'yield=0'}[/dim]")

            await _asyncio.gather(*(one(d, v) for d, v in cands))

        saved = save_catalog(list(existing.values()))
        kept = [e for e in existing.values() if e.verified]
        console.print(f"\n本轮通过 [bold green]{ok}[/bold green]/{len(cands)}"
                      f"；目录 [bold]{len(kept)}[/bold] 个站（原有 {known} 个保留）")
        console.print(f"[green]已写入 {saved}[/green]")

    _asyncio.run(_run())


@sites_app.command("health")
def sites_health(
    reset: bool = typer.Option(False, "--reset", help="清空健康度记录"),
) -> None:
    """查看/重置站点健康度（连续失败的站会被自动跳过）。"""
    from .sitecatalog import SiteHealth

    health = SiteHealth()
    try:
        if reset:
            health.conn.execute("DELETE FROM site_health")
            health.conn.commit()
            console.print("[green]已清空[/green]")
            return
        rows = health.stats()
        disabled = health.disabled()
    finally:
        health.close()

    if not rows:
        console.print("[dim]还没有记录（跑几次搜索就有了）[/dim]")
        return
    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("站点")
    table.add_column("尝试", justify="right")
    table.add_column("累计命中", justify="right")
    table.add_column("连续失败", justify="right")
    table.add_column("状态")
    for domain, attempts, hits, ok_runs, fails, last_hits, last_error in rows:
        table.add_row(domain, str(attempts), str(hits), str(fails),
                      "[red]已跳过[/red]" if domain in disabled else "[green]正常[/green]")
    console.print(table)


@sites_app.command("route")
def sites_route(
    keyword: str = typer.Argument(..., help="查询词，看它会走哪些垂直领域和站点"),
) -> None:
    """预览一次查询的垂直路由结果（不实际发请求）。"""
    from .adapters.sitesearch import SiteSearchAdapter
    from .routing import classify

    verticals = classify(keyword)
    console.print(f"查询 [bold]{keyword}[/bold] -> 垂直领域 "
                  f"[cyan]{', '.join(verticals) or '(无特征词，只用通用站)'}[/cyan]")
    adapter = SiteSearchAdapter({"health_tracking": False})
    sites = adapter.select_sites(keyword)
    if not sites:
        console.print("[yellow]没有匹配的站点[/yellow]")
        return
    for entry in sites:
        console.print(f"  {entry.name:<18} [{','.join(entry.verticals) or '-'}] {entry.search}")


@sites_app.command("coverage")
def sites_coverage() -> None:
    """看各垂直领域的覆盖情况：资源站 + TG 频道。"""
    import re as _re

    from .routing import VERTICAL_KEYWORDS
    from .sitecatalog import load_catalog
    from .tgindex import TgIndex, load_channels

    catalog = load_catalog()
    sites_by_v: dict[str, int] = {}
    for entry in catalog:
        for v in entry.verticals or ["general"]:
            sites_by_v[v] = sites_by_v.get(v, 0) + 1

    # tg_channels.txt 里采收的频道按 "# 垂直领域: xxx（N 个）" 分组标注
    from .tgindex import DEFAULT_CHANNELS_FILE

    tagged: dict[str, int] = {}
    current: str | None = None
    tag_re = _re.compile(r"^#\s*垂直领域:\s*([a-z-]+)")
    try:
        lines = DEFAULT_CHANNELS_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        text = line.strip()
        m = tag_re.match(text)
        if m:
            current = m.group(1)
            tagged.setdefault(current, 0)
            continue
        if text and not text.startswith("#") and current:
            tagged[current] = tagged.get(current, 0) + 1

    index = TgIndex()
    try:
        total_msgs = index.stats()["messages"]
    finally:
        index.close()

    # API 数据源（学术/漫画等爬不到页面的领域靠它们）
    from .adapters.apisources import _load_apis

    apis_by_v: dict[str, int] = {}
    for api in _load_apis():
        for v in str(api.get("vertical") or "").split(","):
            v = v.strip()
            if v:
                apis_by_v[v] = apis_by_v.get(v, 0) + 1

    table = Table(box=box.SIMPLE, header_style="bold cyan")
    table.add_column("垂直领域")
    table.add_column("资源站", justify="right")
    table.add_column("API", justify="right")
    table.add_column("TG 频道", justify="right")
    table.add_column("状态")
    for v in list(VERTICAL_KEYWORDS) + ["general"]:
        n_sites = sites_by_v.get(v, 0)
        n_api = apis_by_v.get(v, 0)
        n_ch = tagged.get(v, 0)
        kinds = sum(1 for n in (n_sites, n_api, n_ch) if n)
        state = ("[green]✓[/green]" if kinds >= 2
                 else ("[yellow]偏薄[/yellow]" if kinds == 1 else "[red]缺[/red]"))
        table.add_row(
            v,
            str(n_sites) if n_sites else "-",
            str(n_api) if n_api else "-",
            str(n_ch) if n_ch else "-",
            state,
        )
    console.print(table)
    console.print(
        f"[dim]资源站 {len(catalog)} 个 ｜ API {sum(apis_by_v.values())} 个 ｜ "
        f"TG 频道 {len(load_channels())} 个 ｜ 索引 {total_msgs} 条消息[/dim]"
    )
    console.print("[dim]注：TG 频道只有采收来的那部分带垂直标注，其余按影视/通用计。[/dim]")


channels_app = typer.Typer(help="TG 频道采收：从导航站找候选 → 价值校验 → 追加进清单")
app.add_typer(channels_app, name="channels")


@channels_app.command("harvest")
def channels_harvest(
    source: str = typer.Option("tgnav", "--source", "-s", help="tgnav（导航站分类）或 file（本地候选文件）"),
    cand_file: Optional[str] = typer.Option(None, "--file", "-f", help="--source file 时的候选清单路径"),
    min_links: int = typer.Option(1, "--min-links", help="最近一页至少要有几条资源链接"),
    concurrency: int = typer.Option(24, "--concurrency", "-c"),
    save: bool = typer.Option(True, "--save/--no-save"),
) -> None:
    """采收 TG 频道：只有最近一页真含网盘/磁力链接的才收。

    和站点探测器的价值校验同一套判据 —— 候选里绝大多数是机器人、交易所、
    新闻、表情包频道，不校验就全爬一遍是纯浪费。
    """
    import asyncio as _asyncio

    import httpx as _httpx

    from .channelharvest import (
        UA, append_channels, harvest_tgnav, load_candidate_file, verify_many,
    )

    async def _run() -> None:
        async with _httpx.AsyncClient(
            follow_redirects=True, http2=True, timeout=20,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        ) as client:
            if source == "file":
                if not cand_file:
                    console.print("[red]--source file 需要同时给 --file[/red]")
                    raise typer.Exit(1)
                cands = load_candidate_file(cand_file)
            else:
                by_v = await harvest_tgnav(client)
                cands = [c for chans in by_v.values() for c in chans]
                console.print(f"[dim]tgnav 采收 {len(cands)} 个候选："
                              f"{ {k: len(v) for k, v in by_v.items()} }[/dim]")

            console.print(f"[cyan]对 {len(cands)} 个候选做价值校验（并发 {concurrency}）…[/cyan]")

            def show(hit) -> None:
                console.print(f"  [green]✓[/green] {hit.channel:<32} 链接={hit.links:<4} {hit.vertical}")

            result = await verify_many(cands, concurrency=concurrency,
                                       min_links=min_links, on_hit=show)
            console.print(f"\n检查 [bold]{result.checked}[/bold] 个，收下 "
                          f"[bold green]{len(result.kept)}[/bold green] 个")
            console.print(f"按垂直领域：{result.by_vertical}")
            if save and result.kept:
                path = append_channels(result.kept)
                console.print(f"[green]已追加到 {path}[/green]；"
                              f"接着跑 pansearch index crawl --pages 3 入库")

    _asyncio.run(_run())


@channels_app.command("stats")
def channels_stats() -> None:
    """频道清单与索引的概况。"""
    from .routing import VERTICAL_KEYWORDS
    from .tgindex import TgIndex, load_channels

    channels = load_channels()
    index = TgIndex()
    try:
        info = index.stats()
    finally:
        index.close()
    console.print(
        f"频道清单 [bold]{len(channels)}[/bold] 个 ｜ "
        f"已索引消息 [bold]{info['messages']}[/bold] 条"
        f"（{info['messages_with_links']} 条含链接）｜ "
        f"有数据的频道 {info['channels_indexed']}"
    )
    console.print(f"[dim]垂直领域标签共 {len(VERTICAL_KEYWORDS)} 种；"
                  f"用 pansearch sites coverage 看逐领域的覆盖情况[/dim]")


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
