"""中文文本的 FTS5 编码与词频（IDF）访问。

为什么需要它：
    SQLite 的 unicode61 分词器把一整串汉字当成**一个 token**，所以没法直接对中文做
    全文检索；而 `LIKE '%词%'` 又是全表扫描（实测 62.8 万条消息 / 451MB，单次
    0.3~0.6s，且随索引线性变差）。

    这里采用 Lucene CJKBigramFilter / Elasticsearch `cjk` 分析器的经典做法：
    把连续的汉字展开成**二元组（bigram）**、拉丁/数字保留整词，写进 FTS5 的
    `unicode61` 列。检索时对查询词做同样编码并用**短语**匹配（相邻 bigram），
    就能用上 FTS5 的倒排索引与内置 `bm25()` 排序。

    `term_idf()` 再通过 `fts5vocab` 拿到每个 bigram 的文档频率，得到**真实 IDF** ——
    像「破解」这种几乎每条都出现的高频词 IDF→0，不会再和治疗/软件名等低频主题词同权。
"""

from __future__ import annotations

import math
import re
import sqlite3

from .config import CACHE_DIR
from .query import normalize_text

DEFAULT_DB = CACHE_DIR / "tg_index.sqlite3"

# 参与 bigram 展开的字符：CJK 统一表意文字 + 日文假名 + 韩文
_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af"
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[A-Za-z0-9]+")


def _bigrams(run: str) -> list[str]:
    if len(run) == 1:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def ngram_tokens(text: str) -> list[str]:
    """CJK 走二元组、拉丁/数字走整词。

    关键：**相邻的 CJK 片段会跨越空白/标点合并**（"沙丘 预言" → "沙丘预言"），
    否则「沙丘预言」这个查询词因为中间有空格就再也匹配不到 —— 中文分享标题里
    空格非常随意。
    """
    out: list[str] = []
    buf = ""
    # 先繁→简归一：索引和查询都用同一套字形，否则「周杰倫」标题搜「周杰伦」搜不到。
    for chunk in _TOKEN_RE.findall(normalize_text(text) or ""):
        if chunk[0].isascii():
            if buf:
                out.extend(_bigrams(buf))
                buf = ""
            out.append(chunk.casefold())
        else:
            buf += chunk
    if buf:
        out.extend(_bigrams(buf))
    return out


def ngram_encode(text: str) -> str:
    """把文本编码成 FTS5 索引串。"""
    return " ".join(ngram_tokens(text))


def match_phrase(term: str) -> str | None:
    """把查询词编码成 FTS5 短语匹配表达式；含单字素时返回 None（bigram 覆盖不了）。

    同一个 CJK 片段展开出的 bigram 在文档里是**位置相邻**的，所以用短语
    `"沙丘 丘预 预言"` 能精确锁住「沙丘预言」这个子串。
    """
    toks = ngram_tokens(term)
    if not toks or any(len(t) < 2 for t in toks):
        return None
    return '"' + " ".join(toks) + '"'


# ---------------------------------------------------------------- IDF
_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_idf_cache: dict[str, float] = {}
_docs: int | None = None


def _connection(path) -> sqlite3.Connection | None:
    global _conn, _conn_path
    target = str(path or DEFAULT_DB)
    if _conn is not None and _conn_path == target:
        return _conn
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, check_same_thread=False)
        conn.execute("SELECT 1 FROM tg_vocab LIMIT 1")
    except sqlite3.Error:
        return None
    _conn, _conn_path = conn, target
    return conn


def reset_cache() -> None:
    """索引重建后清掉 IDF 缓存（测试/CLI 用）。"""
    global _conn, _conn_path, _docs
    if _conn is not None:
        try:
            _conn.close()
        except sqlite3.Error:
            pass
    _conn = _conn_path = None
    _docs = None
    _idf_cache.clear()


def _total_docs(conn: sqlite3.Connection) -> int:
    global _docs
    if _docs is None:
        try:
            _docs = int(conn.execute("SELECT COUNT(*) FROM tg_fts").fetchone()[0])
        except sqlite3.Error:
            _docs = 0
    return _docs


def term_idf(term: str, path=None) -> float | None:
    """返回术语的 IDF（越大越稀有）；索引不可用时返回 None。

    BM25 的 IDF：ln(1 + (N - df + 0.5) / (df + 0.5))。
    多字词取各 bigram 里**最大**的 IDF（最稀有的那个 bigram 最能代表它）。
    """
    key = term.casefold()
    if key in _idf_cache:
        return _idf_cache[key]
    conn = _connection(path)
    if conn is None:
        return None
    toks = ngram_tokens(term)
    if not toks:
        return None
    n = _total_docs(conn)
    if n <= 0:
        return None
    best = 0.0
    for t in toks:
        try:
            row = conn.execute("SELECT doc FROM tg_vocab WHERE term = ?", (t,)).fetchone()
        except sqlite3.Error:
            return None
        df = int(row[0]) if row else 0
        best = max(best, math.log(1.0 + (n - df + 0.5) / (df + 0.5)))
    _idf_cache[key] = best
    return best


__all__ = [
    "DEFAULT_DB", "match_phrase", "ngram_encode", "ngram_tokens", "reset_cache", "term_idf",
]
