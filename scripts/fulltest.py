"""200 词跨领域全量回归测试。

用法：
    .venv/bin/python scripts/fulltest.py --tag baseline              # 全源
    .venv/bin/python scripts/fulltest.py --tag baseline --offline    # 只打本地 TG 索引（秒级）
    .venv/bin/python scripts/fulltest.py --tag baseline -c 5 --limit 40

产出：
    .cache/fulltest_<tag>.json     每条查询的完整指标
    控制台摘要 + 问题清单（最差查询）

判据（与搜索词"相关性"直接对应）：
    subject_absent  标题里找不到查询的**第一个词（主题词）**
    low_rel         代码自身相关性 < 0.4（主题词缺失档）
    两者在 topN 里的占比就是"不相关内容"的可见程度。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pansearch.pipeline import alias_queries, search  # noqa: E402
from pansearch.query import matching_text, normalize_text, query_info, subject_terms, term_present  # noqa: E402
from pansearch.score import _relevance  # noqa: E402

QUERIES = ROOT / "scripts" / "fulltest_queries.txt"
CACHE = ROOT / ".cache"
_SPLIT = re.compile(r"[\s,，、/|·]+")


def load_queries() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in QUERIES.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) == 2:
            out.append((parts[0].strip(), parts[1].strip()))
    return out


def subject_of(kw: str) -> str:
    """词面诊断展示具体主题；独立质量分数由 quality_eval.py 的固定标签计算。"""
    return " ".join(query_info(kw).required)


def evaluate(vertical: str, kw: str, outcome, elapsed: float, top_n: int) -> dict:
    required_queries = [query_info(q).required for q in [kw, *alias_queries(kw)]]
    rows = outcome.resources

    def exempt(r) -> bool:
        # 不参与"主题词缺失"判定的两类：
        #   * API 直链：标题常为英文/不同语言，按设计豁免
        #   * 无标题证据（标题为空或全是"夸克/全集"这类通用词）：引擎刻意保留，
        #     不拿它当"不相关"来扣分
        if "api" in (r.kinds or []):
            return True
        for t in [r.title, *(getattr(r, "titles", None) or [])]:
            nt = matching_text(t or "")
            if not nt or not subject_terms(nt):
                return True
        return False

    def has_anchor(r) -> bool:
        """诊断完整主题/等价别名，不能把稀有的“素材包”误当资源名称。"""
        for t in [r.title, *(getattr(r, "titles", None) or [])]:
            if any(all(term_present(t or "", term) for term in required)
                   for required in required_queries):
                return True
        return False

    rels = [_relevance(r, kw) for r in rows]
    top = rows[:top_n]
    checked_top = [r for r in top if not exempt(r)]

    absent_all = sum(1 for r in rows if not exempt(r) and not has_anchor(r))
    low_rel_all = sum(1 for r, x in zip(rows, rels) if not exempt(r) and x < 0.4)
    absent_top = sum(1 for r in checked_top if not has_anchor(r))
    low_rel_top = sum(
        1 for r, x in zip(top, rels[:top_n]) if not exempt(r) and x < 0.4
    )
    empty_title_top = sum(1 for r in checked_top if not (r.title or "").strip())

    urls = [r.url for r in rows]
    dupes = len(urls) - len(set(urls))
    keys = [r.key for r in rows]
    dupe_keys = len(keys) - len(set(keys))

    return {
        "vertical": vertical,
        "keyword": kw,
        "subject": subject_of(kw),
        "raw_hits": outcome.raw_hits,
        "dedup": outcome.dedup_count,
        "shown": len(rows),
        "alive": outcome.alive_count,
        "filtered": outcome.irrelevant_pruned,
        "api_top": sum(1 for r in top if exempt(r)),
        "elapsed": round(elapsed, 2),
        "status": "empty" if not rows else "ok",
        "absent_all": absent_all,
        "low_rel_all": low_rel_all,
        "absent_top": absent_top,
        "low_rel_top": low_rel_top,
        "absent_top_ratio": round(absent_top / len(checked_top), 3) if checked_top else 0.0,
        "evaluated_top": len(checked_top),
        "empty_title_top": empty_title_top,
        "dupe_urls": dupes,
        "dupe_keys": dupe_keys,
        "queries_used": outcome.queries_used,
        "errors": outcome.errors,
        "top_titles": [((r.title or "")[:100]) for r in top[:8]],
        "top_scores": [r.score for r in top[:8]],
    }


async def one(vertical: str, kw: str, sem: asyncio.Semaphore, args) -> dict:
    async with sem:
        t0 = time.monotonic()
        try:
            outcome = await asyncio.wait_for(
                search(
                    kw,
                    source_names=["telegram"] if args.offline else None,
                    do_verify=not args.no_verify,
                    alive_only=True,     # 量的是用户默认看到的结果（只看存活 + 过滤无关）
                ),
                timeout=args.query_timeout,
            )
            return evaluate(vertical, kw, outcome, time.monotonic() - t0, args.top)
        except Exception as exc:  # noqa: BLE001
            return {
                "vertical": vertical, "keyword": kw, "subject": subject_of(kw),
                "status": "error", "error": f"{type(exc).__name__}: {exc}",
                "elapsed": round(time.monotonic() - t0, 2), "shown": 0,
                "absent_top": 0, "absent_top_ratio": 0.0, "low_rel_top": 0,
            }


async def run(args) -> list[dict]:
    queries = load_queries()
    if args.limit:
        queries = queries[: args.limit]
    sem = asyncio.Semaphore(args.concurrency)
    t0 = time.monotonic()
    results = await asyncio.gather(*(one(v, k, sem, args) for v, k in queries))
    print(f"\n[fulltest] {len(results)} 条查询，墙钟 {time.monotonic() - t0:.0f}s")
    return list(results)


def report(results: list[dict], top_n: int) -> dict:
    ok = [r for r in results if r["status"] == "ok"]
    emptied = [r for r in results if r["status"] == "empty"]
    errors = [r for r in results if r["status"] == "error"]
    total_shown = sum(r.get("shown", 0) for r in ok)
    absent = sum(r.get("absent_top", 0) for r in ok)
    low = sum(r.get("low_rel_top", 0) for r in ok)
    api_top = sum(r.get("api_top", 0) for r in ok)
    filtered = sum(r.get("filtered", 0) for r in ok)
    slots = sum(r["evaluated_top"] for r in ok)
    dupe = sum(r.get("dupe_urls", 0) for r in ok)
    empty_title = sum(r.get("empty_title_top", 0) for r in ok)

    print(f"\n=== 全量测试汇总（top{top_n}）===")
    print(f"查询 {len(results)} ｜ 有结果 {len(ok)} ｜ 空结果 {len(emptied)} ｜ 异常 {len(errors)}")
    print(f"结果总数 {total_shown} ｜ 平均 {total_shown / max(1, len(ok)):.1f} 条/查询")
    print(f"相关性闸门共丢弃 {filtered} 条不相关结果")
    print(f"top{top_n} API/无标题证据豁免 {api_top} 条（不计入词面匹配判定）")
    print(f"top{top_n} 主题词缺失 {absent}/{slots} = {absent / max(1, slots):.1%}")
    print(f"top{top_n} 低相关(rel<0.4) {low}/{slots} = {low / max(1, slots):.1%}")
    print(f"top{top_n} 空标题 {empty_title} ｜ 重复 URL {dupe}")

    src_err: dict[str, int] = {}
    for r in results:
        for name in (r.get("errors") or {}):
            src_err[name] = src_err.get(name, 0) + 1
    if src_err:
        print("各源失败次数：" + "，".join(f"{k} {v}" for k, v in
              sorted(src_err.items(), key=lambda kv: -kv[1])))

    ok_ratio = len(ok) / max(1, len(results))
    absent_ratio = absent / max(1, slots)
    # 可靠性门槛：有结果率 ≥95%、top20 无关 ≤1%、无异常。
    # 不苛求 0：引擎会刻意保留"跨语言标题 / 无标题证据"的结果（那是召回保险，
    # 不是 bug），允许 1% 的保守保留。
    gate = ok_ratio >= 0.95 and absent_ratio <= 0.01 and not errors
    print(f"\n词面回归门槛（不是独立准确率；独立标注见 quality_eval.py）："
          f"{'[green]PASS[/green]' if gate else '[red]FAIL[/red]'}"
          f"  有结果率 {ok_ratio:.1%} ｜ top{top_n}无关 {absent_ratio:.2%}")

    worst = sorted(ok, key=lambda r: (-r["absent_top"], -r["absent_top_ratio"]))[:25]
    print(f"\n--- 主题词缺失最严重的查询 ---")
    for r in worst:
        if r["absent_top"] == 0:
            break
        print(f"  [{r['vertical']:<10}] {r['keyword']:<24} "
              f"shown={r['shown']:<4} absent_top={r['absent_top']:<3} "
              f"low_rel_top={r['low_rel_top']:<3} {r['elapsed']}s")

    if emptied:
        print(f"\n--- 零结果查询（{len(emptied)}）---")
        for r in sorted(emptied, key=lambda x: x["keyword"]):
            print(f"  [{r['vertical']:<10}] {r['keyword']}")
    if errors:
        print(f"\n--- 异常查询（{len(errors)}）---")
        for r in errors:
            print(f"  [{r['vertical']:<10}] {r['keyword']}: {r.get('error')}")

    byv: dict[str, list[int]] = {}
    for r in ok:
        b = byv.setdefault(r["vertical"], [0, 0, 0])
        b[0] += 1
        b[1] += r["absent_top"]
        b[2] += r["shown"]
    print(f"\n--- 分领域 ---")
    for v in sorted(byv):
        n, ab, sh = byv[v]
        print(f"  {v:<12} 查询 {n:<3} 结果 {sh:<5} top{top_n}缺失 {ab}")

    return {
        "queries": len(results), "ok": len(ok), "empty": len(emptied),
        "errors": len(errors), "total_shown": total_shown,
        "absent_top": absent, "low_rel_top": low, "slots": slots,
        "dupe_urls": dupe, "empty_title_top": empty_title,
        "ok_ratio": round(ok_ratio, 4), "absent_ratio": round(absent_ratio, 4),
        "gate": gate, "source_errors": src_err,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="run")
    ap.add_argument("--offline", action="store_true", help="只打本地 TG 索引")
    ap.add_argument("--no-verify", action="store_true", default=True)
    ap.add_argument("--verify", dest="no_verify", action="store_false")
    ap.add_argument("-c", "--concurrency", type=int, default=5)
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 个查询")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--query-timeout", type=float, default=90.0)
    ap.add_argument("--gate", action="store_true", help="按可靠性门槛返回退出码（CI 门槛用）")
    args = ap.parse_args()

    results = asyncio.run(run(args))
    summary = report(results, args.top)
    CACHE.mkdir(exist_ok=True)
    out = CACHE / f"fulltest_{args.tag}.json"
    out.write_text(json.dumps(
        {"args": vars(args), "summary": summary, "results": results},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[fulltest] 报告 → {out}")
    if args.gate and not summary["gate"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
