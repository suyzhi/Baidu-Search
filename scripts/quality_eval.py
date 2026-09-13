"""独立标注集评测：标签固定，不调用评分函数来判定结果是否正确。

precision 使用实际返回的 top-k 条目为分母；recall 的分母包括漏搜的标注相关条目。
NDCG 区分完全符合查询(2)、主题相关但版本/形式不符(1)、无关(0)。
这是可审阅的小型回归集，不代表全网资源的总体准确率。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pansearch.adapters.telegram import TelegramAdapter
from pansearch.dedupe import build_resources
from pansearch.pipeline import alias_queries
from pansearch.query import split_query
from pansearch.score import confidently_irrelevant, score_all, sort_resources
from pansearch.tgindex import TgIndex, TgMessage


def metrics(returned: list[str], grades: dict[str, int], k: int = 10) -> dict:
    top = returned[:k]
    relevant = {key for key, grade in grades.items() if grade > 0}
    found = set(top) & relevant
    gains = [grades.get(key, 0) for key in top]
    ideal = sorted(grades.values(), reverse=True)[:k]
    def dcg(values):
        return sum((2 ** grade - 1) / math.log2(i + 2) for i, grade in enumerate(values))
    return {"precision": len(found) / len(top) if top else 0.0,
            "recall": len(found) / len(relevant) if relevant else 1.0,
            "ndcg": dcg(gains) / dcg(ideal) if dcg(ideal) else 1.0,
            "returned": len(top), "relevant": len(relevant),
            "missing": sorted(relevant - set(top)),
            "false_positives": [key for key in top if grades.get(key, 0) == 0]}


def evaluate(fixture: Path | None = None) -> dict:
    cases = json.loads((fixture or ROOT / "tests/fixtures/search_judgments.json").read_text())
    results = []
    with tempfile.TemporaryDirectory(prefix="pansearch-quality-") as tmp:
        for i, case in enumerate(cases):
            idx = TgIndex(Path(tmp) / f"{i}.sqlite3")
            try:
                docs = case["documents"]
                idx.upsert([TgMessage("judged", j, None, doc["title"],
                                     [{"url": f'https://pan.quark.cn/s/{doc["id"]}'}])
                            for j, doc in enumerate(docs)])
                queries = list(dict.fromkeys([" ".join(split_query(case["query"])),
                                              *alias_queries(case["query"])]))
                hits = []
                for q in queries:
                    got = TelegramAdapter._to_hits(idx.search(q, limit=400), q)
                    for rank, h in enumerate(got):
                        h.query, h.rank = q, rank
                    hits.extend(got)
                resources = build_resources(hits)
                for r in resources:
                    r.queries = queries
                score_all(resources, case["query"])
                resources = sort_resources([r for r in resources
                                            if not confidently_irrelevant(r, case["query"])])
                returned = [r.url.rsplit("/", 1)[-1] for r in resources]
                results.append({"query": case["query"],
                                **metrics(returned, {d["id"]: d["grade"] for d in docs})})
            finally:
                idx.close()
    summary = {name: sum(r[name] for r in results) / len(results)
               for name in ("precision", "recall", "ndcg")}
    summary["queries"] = len(results)
    summary["gate"] = all(summary[name] >= 0.95 for name in ("precision", "recall", "ndcg"))
    return {"summary": summary, "results": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = evaluate()
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.gate and not report["summary"]["gate"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
