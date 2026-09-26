"""Telegram 频道索引 —— 全网网盘链接的主要产地，直连不经过聚合引擎。

为什么要自己抓：
  网盘聚合引擎（PanSou）本质也是抓这些 TG 频道，但公共实例既限流又超时，
  一次搜索只给你它当次抓到的部分结果。直连 t.me 可以：
    * 并行打上百个频道，不受第三方限流
    * 用 ?before=<msg_id> 向历史翻页，越挖越深
    * 落进本地 SQLite —— 之后按关键词检索是毫秒级，且覆盖只增不减

网页预览分页（实测可用）：
    https://t.me/s/<channel>           最新一页（约 20 条）
    https://t.me/s/<channel>?before=<oldest_msg_id>   继续往前翻
"""

from __future__ import annotations

import asyncio
import html as html_mod
import json
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import CACHE_DIR, CONFIG_DIR
from .extract import extract_from_text, html_to_text
from .normalize import URL_RE, detect_pan_type, pwd_from_url
from .query import matching_text, query_info, query_terms, subject_terms, term_present
from .textindex import match_phrase, ngram_encode, ngram_tokens

DEFAULT_DB = CACHE_DIR / "tg_index.sqlite3"
DEFAULT_CHANNELS_FILE = CONFIG_DIR / "tg_channels.txt"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_POST_RE = re.compile(r'data-post="([^"/]+)/(\d+)"')
_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*(?:<div|<span|</div>)', re.S
)
_TIME_RE = re.compile(r'<time[^>]*datetime="([^"]+)"')
_PURE_CJK = re.compile(r"[㐀-鿿]+")

# 候选数超过这个值就不做全量 bm25 排序：FTS5 的 ORDER BY rank 要给**每个**命中打分，
# 实测 473 万条索引里「电影」（31 万命中）0.5 s、「4K」4.5 s；ORDER BY rowid 只要几毫秒。
RANK_MAX_CANDIDATES = 20000
# 需要逐条复核的层最多检查多少条候选（「C#」的候选是所有含字母 c 的消息）
SCAN_CAP = 30000
RECHECK_BUDGET_SECONDS = 1.5


def _recheck(text: str, terms: list[str], need_all: bool) -> bool:
    """按 term_present 语义复核；先做一次廉价的子串预筛，绝大多数候选在这里就被排除。

    matching_text（繁简转换 + 去 URL）每条约 0.7 ms，对上万条候选逐条做太贵；
    拉丁词经 NFKC + casefold 后的子串包含是 term_present 的必要条件。
    """
    folded = None
    results = []
    for t in terms:
        if t.isascii():
            if folded is None:
                folded = unicodedata.normalize("NFKC", text).casefold()
            if t not in folded:
                results.append(False)
                if need_all:
                    return False
                continue
        results.append(None)                         # 待精确复核
    low = None
    for t, pre in zip(terms, results):
        if pre is False:
            continue
        if low is None:
            low = matching_text(text)
        if term_present(low, t):
            if not need_all:
                return True
        elif need_all:
            return False
    return need_all

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tg_messages (
    channel    TEXT NOT NULL,
    msg_id     INTEGER NOT NULL,
    posted_at  TEXT,
    text       TEXT NOT NULL,
    links      TEXT NOT NULL,      -- JSON: [{"url":..,"pwd":..}]
    indexed_at REAL NOT NULL,
    tokens     TEXT,               -- FTS5 的 CJK bigram 编码（见 textindex.py）
    PRIMARY KEY (channel, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_tg_channel ON tg_messages(channel, msg_id DESC);
CREATE TABLE IF NOT EXISTS tg_channels (
    channel      TEXT PRIMARY KEY,
    newest_id    INTEGER,
    oldest_id    INTEGER,
    msg_count    INTEGER NOT NULL DEFAULT 0,
    last_crawl   REAL,
    last_status  TEXT
);
CREATE TABLE IF NOT EXISTS tg_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# FTS5：外部内容表（索引存 tokens，正文仍在 tg_messages），触发器增量同步。
# WHEN 守卫很重要：旧库回填 tokens 之前都是 NULL，删/改时用 NULL 去做 'delete'
# 会让 FTS5 报错。
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS tg_fts USING fts5(
    tokens,
    content='tg_messages',
    content_rowid='rowid',
    tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS tg_messages_ai AFTER INSERT ON tg_messages
WHEN new.tokens IS NOT NULL BEGIN
    INSERT INTO tg_fts(rowid, tokens) VALUES (new.rowid, new.tokens);
END;
CREATE TRIGGER IF NOT EXISTS tg_messages_ad AFTER DELETE ON tg_messages
WHEN old.tokens IS NOT NULL BEGIN
    INSERT INTO tg_fts(tg_fts, rowid, tokens) VALUES ('delete', old.rowid, old.tokens);
END;
CREATE TRIGGER IF NOT EXISTS tg_messages_au AFTER UPDATE ON tg_messages BEGIN
    INSERT INTO tg_fts(tg_fts, rowid, tokens)
        SELECT 'delete', old.rowid, old.tokens WHERE old.tokens IS NOT NULL;
    INSERT INTO tg_fts(rowid, tokens)
        SELECT new.rowid, new.tokens WHERE new.tokens IS NOT NULL;
END;
CREATE VIRTUAL TABLE IF NOT EXISTS tg_vocab USING fts5vocab('tg_fts','row');
"""


def load_channels(path: str | Path | None = None) -> list[str]:
    """读取频道清单（# 注释、空行忽略）。"""
    file = Path(path or DEFAULT_CHANNELS_FILE)
    if not file.exists():
        return []
    out: list[str] = []
    for line in file.read_text(encoding="utf-8").splitlines():
        name = line.split("#", 1)[0].strip().lstrip("@")
        if name and name not in out:
            out.append(name)
    return out


@dataclass
class TgMessage:
    channel: str
    msg_id: int
    posted_at: str | None
    text: str
    links: list[dict] = field(default_factory=list)


def parse_channel_page(html: str, channel: str) -> list[TgMessage]:
    """解析 t.me/s/<channel> 页面里的消息。

    频道名一律用**我们请求的那个**（`channel` 参数），不用 `data-post` 里的：
      * 页面里的 data-post 大小写/别名可能与配置不一致
        （实测 data-post 是 `Baidu_Netdisk`，配置写的是 `Baidu_netdisk`），
        照抄会让消息存到另一个键下、频道统计显示 0 条；
      * 转发消息的 data-post 指向**原频道**，照抄会把归属搞乱。
    """
    marks = list(_POST_RE.finditer(html))
    if not marks:
        return []

    out: list[TgMessage] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(html)
        slice_ = html[m.start():end]

        text_m = _TEXT_RE.search(slice_)
        raw_text = text_m.group(1) if text_m else ""
        plain = " ".join(html_to_text(raw_text).split())

        # 链接：正文 href 里和纯文本里都要抓
        links: list[dict] = []
        seen: set[str] = set()
        for hit in extract_from_text(raw_text, source=f"tg:{channel}", kind="tg") + \
                extract_from_text(plain, source=f"tg:{channel}", kind="tg"):
            if hit.url in seen:
                continue
            seen.add(hit.url)
            links.append({"url": hit.url, "pwd": hit.pwd})
        if not links:
            # 兜底：消息里可能只有裸链接，没有 message_text 块
            for raw in URL_RE.findall(slice_):
                decoded = html_mod.unescape(raw).rstrip("。，、；;!！?？'\"")
                if detect_pan_type(decoded).value == "other" or decoded in seen:
                    continue
                seen.add(decoded)
                links.append({"url": decoded, "pwd": pwd_from_url(decoded)})

        if not plain and not links:
            continue

        time_m = _TIME_RE.search(slice_)
        out.append(
            TgMessage(
                channel=channel or m.group(1),
                msg_id=int(m.group(2)),
                posted_at=time_m.group(1) if time_m else None,
                text=plain[:4000],
                links=links,
            )
        )
    return out


class TgIndex:
    """TG 消息的本地索引（SQLite）。"""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15)
        # WAL：launchd 每小时深挖一轮、一次写几十万行，rollback journal 下写事务期间
        # Web 端的检索会被整段阻塞甚至报 database is locked。WAL 读写互不阻塞。
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass                                     # 另一进程正持有写锁：下次打开再切
        self.conn.execute("PRAGMA synchronous=NORMAL")
        existed = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tg_messages'"
        ).fetchone() is not None
        self.conn.executescript(_SCHEMA)
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(tg_messages)")}
        if "tokens" not in cols:                     # 旧库迁移：补列，FTS 待 build_fts 回填
            self.conn.execute("ALTER TABLE tg_messages ADD COLUMN tokens TEXT")
        ch_cols = {row[1] for row in self.conn.execute("PRAGMA table_info(tg_channels)")}
        if "link_count" not in ch_cols:              # 深挖按产出率分配页数用
            self.conn.execute("ALTER TABLE tg_channels ADD COLUMN link_count INTEGER")
        if "exhausted" not in ch_cols:               # 已挖到频道第一条消息
            self.conn.execute(
                "ALTER TABLE tg_channels ADD COLUMN exhausted INTEGER NOT NULL DEFAULT 0"
            )
        self.conn.executescript(_FTS_SCHEMA)
        if not existed:
            # 全新索引：空集即完整，可以直接用 FTS（触发器会增量维护）
            self._set_meta("fts_built", "1")
        self.conn.commit()

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO tg_meta(key, value) VALUES (?, ?)", (key, str(value))
        )

    def fts_ready(self) -> bool:
        """FTS 索引是否是**完整**的（旧库未回填时为 False，检索回退 LIKE）。"""
        row = self.conn.execute("SELECT value FROM tg_meta WHERE key='fts_built'").fetchone()
        return bool(row and row[0] == "1")

    # ---- 写入 ----
    def upsert(self, messages: list[TgMessage]) -> int:
        if not messages:
            return 0
        now = time.time()
        # 无链接消息的正文检索用不到，但频道/站点反查（找 t.me/xxx 提及）靠它，照存；
        # 要省空间用 `pansearch index compact`（约 0.9 GB / 10 GB）。
        rows = [
            (m.channel, m.msg_id, m.posted_at, m.text,
             json.dumps(m.links, ensure_ascii=False), now,
             ngram_encode(m.text) if m.links else None)
            for m in messages
        ]
        cur = self.conn.executemany(
            "INSERT OR REPLACE INTO tg_messages"
            " (channel, msg_id, posted_at, text, links, indexed_at, tokens)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        self.conn.commit()
        # 用 rowcount 而不是 total_changes：FTS 触发器产生的写入也会计入
        # total_changes，会把"新增/更新条数"统计得虚高。
        return max(0, cur.rowcount)

    # ---- FTS5 索引构建 ----
    def build_fts(self, *, batch: int = 2000) -> dict:
        """回填 tokens 并重建 FTS5 索引（旧库一次性迁移）。

        先删触发器（避免 27 万行逐条触发），按 rowid 分批编码回填，最后
        `rebuild` 一次性建倒排索引，再恢复触发器。全程按 rowid 区间推进，
        不持有打开的读游标，避免边读边写。
        """
        cur = self.conn
        for trg in ("tg_messages_ai", "tg_messages_ad", "tg_messages_au"):
            cur.execute(f"DROP TRIGGER IF EXISTS {trg}")
        cur.commit()

        last, done = 0, 0
        while True:
            rows = cur.execute(
                "SELECT rowid, text FROM tg_messages"
                " WHERE rowid > ? AND links != '[]' ORDER BY rowid LIMIT ?",
                (last, batch),
            ).fetchall()
            if not rows:
                break
            cur.executemany(
                "UPDATE tg_messages SET tokens = ? WHERE rowid = ?",
                [(ngram_encode(t), r) for r, t in rows],
            )
            last = rows[-1][0]
            done += len(rows)
            cur.commit()

        cur.execute("INSERT INTO tg_fts(tg_fts) VALUES('rebuild')")
        cur.executescript(_FTS_SCHEMA)              # 恢复触发器
        cur.execute("CREATE VIRTUAL TABLE IF NOT EXISTS tg_vocab USING fts5vocab('tg_fts','row')")
        self._set_meta("fts_built", "1")
        cur.commit()
        from . import textindex
        textindex.reset_cache()
        return {"fts_docs": done}

    def mark_channel(self, channel: str, newest: int | None, oldest: int | None,
                     status: str = "ok", *, exhausted: bool | None = None) -> None:
        count, links = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(links != '[]'), 0) FROM tg_messages WHERE channel = ?",
            (channel,),
        ).fetchone()
        prev = self.conn.execute(
            "SELECT newest_id, oldest_id, exhausted FROM tg_channels WHERE channel = ?", (channel,)
        ).fetchone()
        prev_newest = prev[0] if prev else None
        prev_oldest = prev[1] if prev else None
        done = bool(prev[2]) if prev and exhausted is None else bool(exhausted)

        merged_newest = max([v for v in (newest, prev_newest) if v is not None], default=None)
        merged_oldest = min([v for v in (oldest, prev_oldest) if v is not None], default=None)
        self.conn.execute(
            "INSERT OR REPLACE INTO tg_channels"
            " (channel, newest_id, oldest_id, msg_count, last_crawl, last_status,"
            "  link_count, exhausted)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (channel, merged_newest, merged_oldest, count, time.time(), status,
             links, 1 if done else 0),
        )
        self.conn.commit()

    def oldest_id(self, channel: str) -> int | None:
        row = self.conn.execute(
            "SELECT oldest_id FROM tg_channels WHERE channel = ?", (channel,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def newest_id(self, channel: str) -> int | None:
        row = self.conn.execute(
            "SELECT newest_id FROM tg_channels WHERE channel = ?", (channel,)
        ).fetchone()
        return row[0] if row and row[0] is not None else None

    def channel_yield(self) -> dict[str, tuple[int, int | None, bool]]:
        """{频道: (已索引消息数, 含链接消息数, 是否已挖到底)}，读 tg_channels 的缓存值。

        link_count 为 None 表示旧库还没被新版 mark_channel 统计过（下一轮抓取后补齐）。
        """
        return {
            ch: (int(n or 0), links, bool(done))
            for ch, n, links, done in self.conn.execute(
                "SELECT channel, msg_count, link_count, exhausted FROM tg_channels"
            )
        }

    def compact(self, *, batch: int = 20000, vacuum: bool = False) -> dict:
        """清空无链接消息的正文（保留行作为翻页/统计的占位），可选 VACUUM 回收磁盘。

        代价：之后无法再从这些消息里反查 t.me/xxx 频道提及（频道扩容的主要候选来源）。
        实测可省约 0.9 GB（10 GB 库）。VACUUM 需要约等于库大小的临时磁盘空间、
        耗时数分钟，且期间独占数据库，所以默认不做。
        """
        last, cleared = 0, 0
        while True:
            row = self.conn.execute(
                "SELECT MAX(rowid) FROM (SELECT rowid FROM tg_messages WHERE rowid > ?"
                " ORDER BY rowid LIMIT ?)", (last, batch),
            ).fetchone()
            if not row or row[0] is None:
                break
            cur = self.conn.execute(
                "UPDATE tg_messages SET text = '' WHERE rowid > ? AND rowid <= ?"
                " AND links = '[]' AND text != ''", (last, row[0]),
            )
            cleared += max(0, cur.rowcount)
            self.conn.commit()
            last = row[0]
        if vacuum:
            self.conn.execute("VACUUM")
        return {"cleared": cleared, "vacuumed": vacuum}

    def normalize_channel_keys(self, known: list[str]) -> int:
        """把历史遗留的频道键（大小写/别名不一致）归并到配置里的标准名。

        修复过的问题：data-post 写 `Baidu_Netdisk`、配置写 `Baidu_netdisk`，
        结果消息存进了另一个键，频道表里该频道显示 0 条、深度统计失真。
        """
        canonical = {c.lower(): c for c in known}
        moved = 0
        for table in ("tg_messages", "tg_channels"):
            rows = self.conn.execute(f"SELECT DISTINCT channel FROM {table}").fetchall()
            for (ch,) in rows:
                target = canonical.get(str(ch).lower())
                if target and target != ch:
                    try:
                        self.conn.execute(
                            f"UPDATE OR REPLACE {table} SET channel = ? WHERE channel = ?",
                            (target, ch),
                        )
                        moved += 1
                    except sqlite3.Error:
                        continue
        if moved:
            self.conn.commit()
        return moved

    # ---- 查询 ----
    def is_empty(self) -> bool:
        return self.conn.execute("SELECT 1 FROM tg_messages LIMIT 1").fetchone() is None

    def search(self, kw: str, limit: int = 500) -> list[dict]:
        """按关键词检索本地索引。

        有完整 FTS5 索引时走 **BM25**（CJK bigram 倒排，毫秒级、真 IDF 排序）；
        没有（旧库尚未 build_fts）就回退 `LIKE '%词%'`。两条路径都保证：
        只取含资源链接的消息、主题词命中优先、命中词数多的优先。
        """
        terms = query_terms(kw)[:6]
        if not any(terms):
            return []
        subjects = list(query_info(kw).required) or terms
        if self.fts_ready():
            rows = self._search_fts(terms, subjects, limit)
            if rows is not None:
                return rows
        return self._search_like(terms, subjects, limit)

    def _search_fts(self, terms: list[str], subjects: list[str], limit: int) -> list[dict] | None:
        """分层召回，每层都由倒排索引直接给出，不再"多取 4 倍再回 Python 重排"。

        层级（前一层不够 limit 才往下走，已取到的消息不重复）：
          1. 全部查询词都出现（主题词 + 画质/格式等限定词）
          2. 全部主题词都出现
          3. 任一主题词出现（仅多主题词查询；打分阶段会给低相关）

        原来是"主题词 OR 召回 → ORDER BY rank 取 4×limit → Python 逐条归一化复核"。
        索引涨到 473 万条后：ORDER BY rank 要给**每个**命中算 bm25（「4K」4.5 s），
        复核 1600 条长正文又是 1 秒多。现在大候选集按 rowid 取（毫秒级），
        纯中文主题词的 bigram 短语已与 term_present 等价，不再复核。
        """
        phrases = []
        recheck: list[str] = []
        for t in subjects:
            phrase = match_phrase(t)
            if phrase is None:
                # 含孤立单个汉字（「全DLC」「哆啦A梦」「书」）：bigram 短语表达不了。
                # 各 token 分别 AND，单个汉字用前缀匹配以它开头的 bigram，再逐条复核。
                # 原来这里回退全表 LIKE，473 万条索引上要 45~75 秒（fulltest 里直接超时）。
                # 词里还有别的 token 时干脆不把孤立汉字交给 FTS：「全」* 会展开成
                # 全集/全部/… 的全部 bigram，合并上百万条倒排，比复核本身还慢。
                toks = ngram_tokens(t)
                solid = [x for x in toks if len(x) > 1 or x.isascii()]
                if solid:
                    phrase = "(" + " AND ".join(f'"{x}"' for x in solid) + ")"
                elif toks:
                    phrase = "(" + " AND ".join(f'"{x}"*' for x in toks) + ")"
                else:
                    return None
            elif re.fullmatch(r"[a-z]{4,}", t):
                # 索引把 Serum2 当整词；前缀召回后按词边界剔除 serumology。
                # 两三个字母的词（fl、ps）前缀会展开成 flac/flv/… 大量无关 token，只做整词匹配。
                phrase += "*"
            if not (_PURE_CJK.fullmatch(t) and len(t) > 1):
                # 英文/符号词要复核词边界与 URL 子串（Serumology、C++ 与 C#、链接里的 c#）；
                # 纯中文多字词的 bigram 短语已与 term_present 等价，不再复核。
                recheck.append(t)
            phrases.append(phrase)
        if not phrases:
            return None
        extras = [p for t in terms if t not in subjects and (p := match_phrase(t))]

        base = " AND ".join(phrases)
        # (MATCH 表达式, 命中词数, 需要复核的词, 复核语义 all/any)
        tiers: list[tuple[str, int, list[str], bool]] = []
        if extras:
            tiers.append((" AND ".join([base, *extras]), len(phrases) + len(extras), recheck, True))
        tiers.append((base, len(phrases), recheck, True))
        if len(phrases) > 1:
            # OR 层不知道是哪个主题词命中的，只要任一主题词真实出现即可
            tiers.append((" OR ".join(phrases), 1, list(subjects), False))

        out: list[dict] = []
        seen: set[int] = set()
        # 复核（繁简归一 + 正则）每条约 0.7 ms；候选多而命中率低时给复核设个时间上限，
        # 宁可少几条也不能让 TG 源拖到超时、一条都交不出。只计复核本身的耗时：
        # 机器繁忙时 SQL 变慢不该把预算吃光（实测负载 57 时「FL Studio 破解」因此 0 条）。
        spent = 0.0
        try:
            for match, hits, check, need_all in tiers:
                want = limit - len(out)
                if want <= 0 or (check and spent > RECHECK_BUDGET_SECONDS):
                    break
                big = self.conn.execute(
                    "SELECT COUNT(*) FROM (SELECT 1 FROM tg_fts WHERE tg_fts MATCH ? LIMIT ?)",
                    (match, RANK_MAX_CANDIDATES + 1),
                ).fetchone()[0] > RANK_MAX_CANDIDATES
                order = "tg_fts.rowid DESC" if big else "rank"
                # 游标惰性读取：不复核时读够 want 条就停；复核时最多检查 SCAN_CAP 条
                cur = self.conn.execute(
                    "SELECT m.rowid, m.channel, m.msg_id, m.posted_at, m.text, m.links,"
                    f" {'0' if big else 'bm25(tg_fts)'}"
                    " FROM tg_fts JOIN tg_messages m ON m.rowid = tg_fts.rowid"
                    f" WHERE tg_fts MATCH ? AND m.links != '[]' ORDER BY {order} LIMIT ?",
                    (match, SCAN_CAP if check else want + len(seen)),
                )
                tier: list[dict] = []
                for rowid, channel, msg_id, posted_at, text, links, bm in cur:
                    if rowid in seen:
                        continue
                    if check:
                        t0 = time.perf_counter()
                        passed = _recheck(text or "", check, need_all)
                        spent += time.perf_counter() - t0
                        if not passed:
                            if spent > RECHECK_BUDGET_SECONDS:
                                break
                            continue
                    seen.add(rowid)
                    tier.append({
                        "channel": channel, "msg_id": msg_id, "posted_at": posted_at,
                        "text": text, "links": json.loads(links or "[]"),
                        "hits": hits, "bm25": -float(bm or 0.0),   # 越大越好
                    })
                    if len(tier) >= want:
                        break
                # 同层内 bm25 高者优先、同分按时间新→旧（候选太多时 bm25 全为 0，即纯时间序）
                tier.sort(key=lambda d: d["posted_at"] or "", reverse=True)
                tier.sort(key=lambda d: -d["bm25"])
                out.extend(tier)
        except sqlite3.OperationalError:
            return None
        return out[:limit]

    def _search_like(self, terms: list[str], subjects: list[str], limit: int) -> list[dict]:
        def pattern(term):
            return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

        like = [pattern(t) for t in terms]
        subject_like = [pattern(t) for t in subjects]
        score_expr = " + ".join(["CASE WHEN text LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END"] * len(terms))
        where = " OR ".join(["text LIKE ? ESCAPE '\\'"] * len(subjects))
        # LIKE 只负责粗筛；URL 子串和英文词内部命中必须在 LIMIT 前剔除。
        self.conn.create_function("pan_matches", 1,
                                  lambda text: any(term_present(text or "", t) for t in subjects))

        sql = (
            f"SELECT channel, msg_id, posted_at, text, links, ({score_expr}) AS hits"
            f" FROM tg_messages WHERE links != '[]' AND ({where}) AND pan_matches(text)"
            " ORDER BY hits DESC, posted_at DESC, channel, msg_id DESC LIMIT ?"
        )
        params = [*like, *subject_like, limit]
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {"channel": r[0], "msg_id": r[1], "posted_at": r[2],
             "text": r[3], "links": json.loads(r[4] or "[]"), "hits": r[5]}
            for r in rows
        ]

    def stats(self) -> dict:
        total = self.conn.execute("SELECT COUNT(*) FROM tg_messages").fetchone()[0]
        with_links = self.conn.execute(
            "SELECT COUNT(*) FROM tg_messages WHERE links != '[]'"
        ).fetchone()[0]
        # 以 tg_messages 为准实时统计，避免 tg_channels.msg_count 变脏
        channels = self.conn.execute(
            "SELECT COUNT(DISTINCT channel) FROM tg_messages"
        ).fetchone()[0]
        last = self.conn.execute("SELECT MAX(last_crawl) FROM tg_channels").fetchone()[0]
        return {
            "messages": total,
            "messages_with_links": with_links,
            "channels_indexed": channels,
            "db": str(self.path),
            "last_crawl": last,
        }

    def channel_rows(self) -> list[tuple]:
        """频道明细。消息数实时从 tg_messages 统计，不信 tg_channels 里的缓存值。"""
        return self.conn.execute(
            "SELECT c.channel,"
            "       (SELECT COUNT(*) FROM tg_messages m WHERE m.channel = c.channel),"
            "       c.newest_id, c.oldest_id, c.last_status"
            " FROM tg_channels c"
            " ORDER BY 2 DESC"
        ).fetchall()

    def close(self) -> None:
        self.conn.close()


class TgCrawler:
    """并发抓取 TG 频道页面并写入索引。"""

    def __init__(self, *, concurrency: int = 12, timeout: float = 20.0,
                 index: TgIndex | None = None, pages_delay: float = 0.15):
        self.concurrency = concurrency
        self.timeout = timeout
        self.index = index or TgIndex()
        self.sem = asyncio.Semaphore(concurrency)
        self.pages_delay = pages_delay
        self.stats = {"channels": 0, "pages": 0, "messages": 0, "new": 0, "errors": 0}

    async def _page(self, client: httpx.AsyncClient, channel: str,
                    before: int | None) -> tuple[list[TgMessage], str]:
        url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
        last = "ok"
        for attempt in range(2):          # 实测偶发 RemoteProtocolError，重试一次
            async with self.sem:
                try:
                    resp = await client.get(url, headers={"User-Agent": UA,
                                                          "Accept-Language": "zh-CN,zh;q=0.9"})
                except httpx.HTTPError as exc:
                    last = f"err:{type(exc).__name__}"
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
            if resp.status_code != 200:
                last = f"http:{resp.status_code}"
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            msgs = parse_channel_page(resp.text, channel)
            return msgs, "ok" if msgs else "empty"
        return [], last

    async def crawl_channel(self, client: httpx.AsyncClient, channel: str,
                            pages: int, *, deepen: bool = False) -> None:
        before: int | None = None
        # 抓最新时翻到上次已有的位置就停：之前每轮都把 N 页全翻一遍，
        # 绝大部分是重复消息。
        known_newest = None if deepen else self.index.newest_id(channel)
        if deepen:
            before = self.index.oldest_id(channel)

        got: list[TgMessage] = []
        status = "ok"
        exhausted = None
        for page in range(pages):
            msgs, status = await self._page(client, channel, before)
            self.stats["pages"] += 1
            if not msgs:
                if deepen and status == "empty" and before is not None:
                    exhausted = True         # 翻过了频道第一条消息
                break
            oldest = min(m.msg_id for m in msgs)
            if before is not None and oldest >= before:
                # t.me 对越界的 before 会回落到最新页：没往前走就说明已到底
                if deepen:
                    exhausted = True
                break
            got.extend(msgs)
            before = oldest
            if known_newest is not None and oldest <= known_newest:
                self.stats["caught_up"] = self.stats.get("caught_up", 0) + 1
                break
            if self.pages_delay:
                await asyncio.sleep(self.pages_delay)

        if got:
            self.stats["new"] += self.index.upsert(got)
            self.stats["messages"] += len(got)
            self.index.mark_channel(
                channel,
                newest=max(m.msg_id for m in got),
                oldest=min(m.msg_id for m in got),
                status=status,
                exhausted=exhausted,
            )
        else:
            self.index.mark_channel(channel, None, None, status=status, exhausted=exhausted)
            if status.startswith(("err:", "http:")):
                self.stats["errors"] += 1
        self.stats["channels"] += 1

    async def crawl(self, channels: list[str], pages: int = 1, *,
                    deepen: bool = False) -> dict:
        plan = {ch: pages for ch in channels}
        if deepen:
            # 深挖按频道产出率分配页数；已挖到底的频道跳过
            yields = self.index.channel_yield()
            for ch in channels:
                msgs, links, done = yields.get(ch, (0, None, False))
                plan[ch] = 0 if done else deepen_pages(pages, msgs, links)
            self.stats["skipped_exhausted"] = sum(1 for n in plan.values() if n == 0)
            self.stats["pages_planned"] = sum(plan.values())
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, http2=True,
            headers={"User-Agent": UA},
        ) as client:
            await asyncio.gather(
                *(self.crawl_channel(client, ch, n, deepen=deepen)
                  for ch, n in plan.items() if n > 0),
                return_exceptions=True,
            )
        return dict(self.stats)


def deepen_pages(pages: int, msg_count: int, link_count: int | None) -> int:
    """按"含链接消息占比"给深挖分配页数。

    频道清单里混着大量闲聊/资讯频道，平均分页数等于把一半预算花在不产出链接的
    历史上。样本太少（<200 条）或旧库还没统计时给满额，避免误杀新频道。
    """
    if pages <= 0:
        return 0
    if link_count is None or msg_count < 200:
        return pages
    ratio = link_count / msg_count
    if ratio >= 0.3:
        return pages
    if ratio >= 0.1:
        return max(1, pages // 2)
    if ratio >= 0.02:
        return max(1, pages // 5)
    return 1
