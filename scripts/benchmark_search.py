"""只读真实索引的本地耗时基准；不访问网络、不修改索引、不复用查询级缓存。"""
import argparse
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pansearch.adapters.telegram import TelegramAdapter
from pansearch.dedupe import build_resources
from pansearch.score import confidently_irrelevant, score_all, sort_resources
from pansearch.tgindex import DEFAULT_DB, TgIndex

QUERIES = ["沙丘 4K HDR", "三体 电视剧 4K", "周杰伦 无损 全集", "Python 入门",
           "Adobe Photoshop", "machine learning", "沙丘预言", "C# 教程"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    idx = TgIndex.__new__(TgIndex)
    idx.path = args.database
    idx.conn = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    results = []
    try:
        for kw in QUERIES:
            samples = []
            for _ in range(args.rounds):
                start = time.perf_counter()
                rows = idx.search(kw, limit=400)
                fetched = time.perf_counter()
                hits = TelegramAdapter._to_hits(rows, kw)
                for rank, h in enumerate(hits):
                    h.query, h.rank = kw, rank
                resources = build_resources(hits)
                score_start = time.perf_counter()
                score_all(resources, kw)
                kept = [r for r in resources if not confidently_irrelevant(r, kw)]
                kept = sort_resources(score_all(kept, kw))
                end = time.perf_counter()
                samples.append({"lookup_ms": (fetched-start)*1000,
                                "rank_filter_ms": (end-score_start)*1000,
                                "total_ms": (end-start)*1000})
            results.append({"query": kw, "messages": len(rows), "candidates": len(resources),
                            "shown": len(kept),
                            **{k: round(statistics.median(r[k] for r in samples), 2)
                               for k in samples[0]}})
    finally:
        idx.close()
    report = {"rounds": args.rounds, "database": str(args.database), "results": results}
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
