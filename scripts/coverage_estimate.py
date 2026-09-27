"""覆盖率估计：用"多个来源彼此的重合程度"估计每个查询的资源总量。

思路（生态学里估计物种数的标准做法，搬到"估计一个关键词全网有多少分享"）：
    * 每个来源独立去"捕获"资源。如果几个来源找到的几乎是同一批，说明池子快被捞干了；
      如果每个来源找到的大多是别人没有的，说明外面还有很多。
    * Chao2（incidence 版）：S_hat = S_obs + (m-1)/m · Q1(Q1-1) / (2(Q2+1))
        Q1 = 只被 1 个来源找到的资源数，Q2 = 恰好被 2 个来源找到的，m = 来源数
    * Chapman（两来源 Lincoln–Petersen 的无偏修正）：N = (n1+1)(n2+1)/(m12+1) - 1

两种估计都是**下界**：来源之间正相关（都爱收录热门资源、PanSou 自己也爬 TG）
会让重合偏多 → 总量被低估 → 覆盖率被高估。所以读数应理解为"至少还差多少"。

口径：
    * 每个查询只统计**高相关**（相关性 ≥ 0.85）的网盘分享；磁力与 API 直链另算
      —— 它们只有 BT/API 能产出，混进来会把"只被一个来源找到"虚增。
    * 按"资源键"判定同一资源（与去重同一套规则）。
    * 来源分组：TG 本地索引 / PanSou 的 TG 频道部分 / PanSou 插件 / 开放网页（搜索引擎 +
      资源站 + B 站）。BT 与公开 API 不产出网盘分享，不参与。
    * 每个"查询 × 来源"的原始命中落盘到 .cache/coverage/，重跑时直接读缓存（--refresh 强制重抓）。

用法：
    .venv/bin/python scripts/coverage_estimate.py                 # 每个领域 5 个查询
    .venv/bin/python scripts/coverage_estimate.py --per-vertical 3 --concurrency 3
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import httpx  # noqa: E402

from pansearch.dedupe import build_resources  # noqa: E402
from pansearch.models import PanType, RawHit  # noqa: E402
from pansearch.pipeline import UA, _fetch_hits, alias_queries, build_adapters  # noqa: E402
from pansearch.score import score_all  # noqa: E402

QUERIES = ROOT / "scripts" / "fulltest_queries.txt"
CACHE = ROOT / ".cache" / "coverage"
HIGH_REL = 0.85
SKIP_TYPES = {PanType.MAGNET, PanType.DIRECT}
GROUPS = ("tg_local", "pansou_tg", "pansou_plugin", "open_web")
GROUP_LABEL = {"tg_local": "TG 本地索引", "pansou_tg": "PanSou·TG 频道",
               "pansou_plugin": "PanSou·插件", "open_web": "开放网页"}


def group_of(adapter: str, hit: RawHit) -> str | None:
    if adapter == "telegram":
        return "tg_local"
    if adapter == "pansou":
        return "pansou_tg" if hit.kind == "tg" else "pansou_plugin"
    if adapter in ("websearch", "sitesearch", "bilibili"):
        return "open_web"
    return None                       # btsearch / apisources：不产出网盘分享


def load_queries(per_vertical: int) -> list[tuple[str, str]]:
    by_v: dict[str, list[str]] = defaultdict(list)
    for line in QUERIES.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "\t" not in line:
            continue
        v, kw = line.split("\t", 1)
        by_v[v.strip()].append(kw.strip())
    picked = []
    for v, kws in by_v.items():
        # 等距抽样：每个领域的词表前面偏热门，只取前 N 个会高估覆盖率
        step = max(1, len(kws) // per_vertical)
        picked += [(v, kw) for kw in kws[::step][:per_vertical]]
    return picked


def _cache_path(kw: str) -> Path:
    return CACHE / (hashlib.sha1(kw.encode()).hexdigest()[:16] + ".json")


async def fetch_query(kw: str, adapters, client, refresh: bool) -> dict:
    path = _cache_path(kw)
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    queries = [kw, *alias_queries(kw)]          # 主查询 + 等价别名，不含放宽补搜
    per_adapter: dict[str, list[dict]] = {}
    report: dict[str, dict] = {}
    for adapter in adapters:
        hits: list[RawHit] = []
        for q in queries:
            got, _errs, rep = await _fetch_hits([adapter], client, q)
            hits += got
            report.setdefault(adapter.name, rep.get(adapter.name, {}))
        per_adapter[adapter.name] = [h.model_dump(mode="json") for h in hits]
    data = {"keyword": kw, "queries": queries, "hits": per_adapter, "report": report,
            "fetched_at": time.time()}
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def analyse(data: dict) -> dict:
    kw = data["keyword"]
    tagged: list[tuple[str, RawHit]] = []
    for adapter, raw in data["hits"].items():
        for h in raw:
            hit = RawHit.model_validate(h)
            g = group_of(adapter, hit)
            if g:
                tagged.append((g, hit))
    # 合并后打分：同一资源在各来源的标题都参与，取最佳相关性
    merged = build_resources([h for _, h in tagged])
    for res in merged:
        res.queries = list(data["queries"])
    score_all(merged, kw)
    population = {r.key for r in merged if r.relevance >= HIGH_REL and r.pan_type not in SKIP_TYPES}

    found: dict[str, set[str]] = defaultdict(set)      # 资源键 -> 找到它的来源组
    for g, hit in tagged:
        for res in build_resources([hit]):
            if res.key in population:
                found[res.key].add(g)

    s_obs = len(found)
    freq = defaultdict(int)
    for gs in found.values():
        freq[len(gs)] += 1
    q1, q2 = freq[1], freq[2]
    active = [g for g in GROUPS if any(g in gs for gs in found.values())]
    m = max(1, len(active))
    chao2 = s_obs + ((m - 1) / m) * q1 * (q1 - 1) / (2 * (q2 + 1)) if m > 1 else float(s_obs)

    n = {g: sum(1 for gs in found.values() if g in gs) for g in GROUPS}
    only = {g: sum(1 for gs in found.values() if gs == {g}) for g in GROUPS}

    def chapman(a: str, b: str) -> float | None:
        both = sum(1 for gs in found.values() if a in gs and b in gs)
        if not n[a] or not n[b]:
            return None
        return (n[a] + 1) * (n[b] + 1) / (both + 1) - 1

    # 按消息计（一条消息常带多个链接）；含别名查询时可能略超 400
    tg_rows = len({h.get("origin") for h in data["hits"].get("telegram") or []})
    return {
        "keyword": kw, "s_obs": s_obs, "q1": q1, "q2": q2, "sources": m,
        "chao2": round(chao2, 1), "n": n, "only": only,
        "chapman_tg_plugin": chapman("tg_local", "pansou_plugin"),
        "chapman_tg_web": chapman("tg_local", "open_web"),
        "tg_capped": tg_rows >= 400,
        "report": {k: v.get("status") for k, v in data.get("report", {}).items()},
    }


def summarize(rows: list[dict]) -> dict:
    by_v: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_v[r["vertical"]].append(r)
    out = {}
    for v, rs in sorted(by_v.items()) + [("ALL", rows)]:
        s = sum(r["s_obs"] for r in rs)
        est = sum(r["chao2"] for r in rs)
        n = {g: sum(r["n"][g] for r in rs) for g in GROUPS}
        only = {g: sum(r["only"][g] for r in rs) for g in GROUPS}
        pairs_p = [r for r in rs if r["chapman_tg_plugin"] is not None]
        pairs_w = [r for r in rs if r["chapman_tg_web"] is not None]
        out[v] = {
            "queries": len(rs), "zero": sum(1 for r in rs if not r["s_obs"]),
            "s_obs": s, "chao2": round(est), "coverage_chao2": round(s / est, 3) if est else None,
            "coverage_chapman_tg_plugin": round(
                sum(r["s_obs"] for r in pairs_p) / sum(r["chapman_tg_plugin"] for r in pairs_p), 3)
                if pairs_p else None,
            "coverage_chapman_tg_web": round(
                sum(r["s_obs"] for r in pairs_w) / sum(r["chapman_tg_web"] for r in pairs_w), 3)
                if pairs_w else None,
            "found_by": n, "only_by": only,
            "tg_capped": sum(1 for r in rs if r["tg_capped"]),
        }
    return out


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-vertical", type=int, default=5)
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--refresh", action="store_true", help="忽略缓存重新抓取")
    ap.add_argument("--deadline", type=float, default=1500, help="整批的墙钟上限（秒）")
    ap.add_argument("--output", type=Path, default=ROOT / ".cache" / "coverage_report.json")
    args = ap.parse_args()

    queries = load_queries(args.per_vertical)
    adapters = [a for a in build_adapters() if a.name in
                ("telegram", "pansou", "websearch", "sitesearch", "bilibili")]
    sem = asyncio.Semaphore(args.concurrency)
    rows: list[dict] = []
    started = time.monotonic()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=True,
                                 http2=True, headers={"User-Agent": UA,
                                                      "Accept-Language": "zh-CN,zh;q=0.9"}) as client:
        async def one(v: str, kw: str) -> None:
            async with sem:
                if time.monotonic() - started > args.deadline:
                    print(f"[skip] {kw}（超过整批上限）", flush=True)
                    return
                try:
                    data = await asyncio.wait_for(fetch_query(kw, adapters, client, args.refresh), 120)
                except Exception as exc:  # noqa: BLE001
                    print(f"[error] {kw}: {type(exc).__name__}: {exc}", flush=True)
                    return
                row = {"vertical": v, **analyse(data)}
                rows.append(row)
                cov = row["s_obs"] / row["chao2"] if row["chao2"] else 0
                print(f"[{len(rows):>3}/{len(queries)}] {v:<10} {kw:<24} "
                      f"找到 {row['s_obs']:>4}  估计 {row['chao2']:>7.0f}  覆盖 {cov:>5.0%}", flush=True)

        await asyncio.gather(*(one(v, kw) for v, kw in queries))

    summary = summarize(rows)
    args.output.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print("\n=== 分领域覆盖率（Chao2 下界口径；越接近 100% 越接近捞干）===")
    for v, s in summary.items():
        print(f"{v:<11} 查询 {s['queries']:>2}（零结果 {s['zero']}） 找到 {s['s_obs']:>5}  估计 {s['chao2']:>6}"
              f"  覆盖 Chao2 {s['coverage_chao2'] or 0:>5.0%}  "
              f"TG×插件 {s['coverage_chapman_tg_plugin'] or 0:>5.0%}  TG×网页 {s['coverage_chapman_tg_web'] or 0:>5.0%}")
    s = summary["ALL"]
    print("\n各来源找到 / 只有它找到：" + " ｜ ".join(
        f"{GROUP_LABEL[g]} {s['found_by'][g]}/{s['only_by'][g]}" for g in GROUPS))
    print(f"TG 本地索引触顶 400 条的查询：{s['tg_capped']}/{s['queries']}")
    print(f"报告 → {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
