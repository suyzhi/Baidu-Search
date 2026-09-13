"""排序打分：相关性 × 来源权重 × 存活状态 × 新鲜度 + 多源命中加成。"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from .config import pan_priority, scoring_cfg
from .models import PanType, Resource, Status
from .query import normalize_text, query_terms, subject_terms, term_present
from .textindex import term_idf


def _pick_anchor(subjects: list[str]) -> str:
    """多主题词时选"锚点"：**IDF 最高**的那个（最稀有的词最能界定主题）。

    实测反例：「Serum 合成器」里"合成器"也会被当成主题词，于是标题只要出现
    "合成器"（音乐类帖子到处都是）就算相关，前排全是不相关的专辑。
    按 IDF 选锚点后锚点变成罕见的 "serum"，只命中"合成器"的结果直接沉底。
    拿不到 IDF 统计（索引未建 / 词不在语料里）时退回第一个词。
    """
    if not subjects:
        return ""
    if len(subjects) == 1:
        return subjects[0]
    best, best_w = subjects[0], 0.0
    for t in subjects:
        w = term_idf(t)
        if w is not None and w > best_w:
            best, best_w = t, w
    return best


def anchor_term(kw: str) -> str:
    """查询的锚点主题词：没有内容词时退回第一个查询词。"""
    subjects = subject_terms(kw)
    if subjects:
        return _pick_anchor(subjects)
    terms = query_terms(kw)
    return terms[0] if terms else ""


def _matched_terms(title: str, terms: list[str]) -> int:
    """统计标题命中了几个查询词。

    数字词要**紧贴前一个词**才算命中（"沙丘2" / "沙丘 2"）。
    否则「沙丘 2」里的 "2" 会在 "2026"、"1080p" 里到处命中，
    让「奥古利亚沙丘」「沙丘荒原」「沙丘鹤」和真正的「沙丘2部合集」
    全都算"全词命中" —— 实测这些不相关结果就是这样挤进前排的。
    """
    matched = 0
    for i, term in enumerate(terms):
        if term.isdigit() and i > 0:
            prev = terms[i - 1]
            if re.search(re.escape(prev) + r"[\s:：._-]*" + re.escape(term) + r"(?!\d)", title):
                matched += 1
        elif term_present(title, term):
            matched += 1
    return matched


def _relevance_single(res: Resource, kw: str, *, title: str | None = None) -> float:
    """多词查询按「命中词数比例」打分，并**特殊对待第一个词**。

    中文没有分词，所以 "沙丘 4K HDR" 要拆成词分别匹配。
    但光看命中比例不够：「黑夏 4K HDR」这类只命中限定词的结果，
    靠百度优先 + 提取码 + 多源命中的连乘能反超真正相关的结果，
    所以查询的第一个词（通常是片名/主题词）缺失必须重罚。
    """
    k = normalize_text(kw)
    if not k:
        return 1.0
    title = normalize_text(title if title is not None else (res.title or ""))
    if not title:
        return 0.3

    terms = query_terms(k)
    if not terms:
        return 1.0

    matched = _matched_terms(title, terms)
    subjects = subject_terms(k)
    if subjects:
        # 锚点（最高 IDF 主题词）缺失 = 主题词缺失，不管其他词命中多少
        subject_present = term_present(title, _pick_anchor(subjects))
    else:
        subject_present = any(term_present(title, t) for t in terms)

    if matched == len(terms):
        return 1.0 if title.startswith(terms[0]) else 0.95

    if not subject_present:
        # 主题词都没出现 -> 上限压到 0.4，保证任何加成组合都翻不了身
        return (round(0.15 + 0.25 * (matched / len(terms)), 4) if matched else 0.1)

    if matched == 0:  # 不会走到（subject_present 蕴含 matched>=1），保底
        return 0.2
    # 主题词在，但缺少限定词（4K/HDR/续集编号等）
    return round(0.45 + 0.55 * (matched / len(terms)), 4)


def _relevance(res: Resource, kw: str) -> float:
    """相关性取原词或等价别名的最高值；补搜不能放宽评分标准。

    否则别名检索会自相矛盾：用户搜「大气合成器」，我们用别名 "Omnisphere"
    取回一堆标题写着 Omnisphere 的结果，再用「大气合成器」去算相关性
    —— 主题词一个都不出现，全被判成 0.1 分。
    """
    candidates = [kw] + [q for q in (res.queries or []) if q and q != kw]
    titles = list(dict.fromkeys([res.title or "", *res.titles]))
    return max(_relevance_single(res, q, title=t) for q in candidates for t in titles)


def confidently_irrelevant(res: Resource, kw: str) -> bool:
    """只过滤有标题、且**锚点主题词**（最高 IDF 的内容词）缺失的结果。

    为什么用锚点而不是"任一主题词"：「Serum 合成器」里"合成器"也是内容词，
    只看"任一命中"会让一堆只提到"合成器"的音乐帖留下来。锚点 = 最稀有的那个词，
    它缺失基本就说明不是要找的东西。跨语言标题不认识，保留而不是猜着删。
    """
    titles = [normalize_text(t) for t in [res.title, *res.titles] if t and t.strip()]
    if not titles:
        return False
    # 通用文件夹名不足以证明不相关，让验活接口补充信息。
    if any(not subject_terms(t) or t in {"文件", "文件夹", "未命名", "download", "untitled"}
           for t in titles):
        return False
    anchor = anchor_term(kw)
    if not anchor:
        return False
    # 只有"中文查询 → 非中文标题"才当作可能的翻译保留（例如中文查询返回英文片名）。
    # 反过来（英文锚点 → 中文标题）不能一律保留，否则「Serum 合成器」会留下一堆
    # 只提到"合成器"的中文音乐帖。
    if re.search(r"[\u3400-\u9fff]", anchor) and any(
        not re.search(r"[\u3400-\u9fff]", t) for t in titles
    ):
        return False
    return not any(term_present(title, anchor) for title in titles)


def _kind_weight(res: Resource, cfg: dict) -> float:
    weights = cfg.get("kind_weight") or {}
    if not res.kinds:
        return 0.6
    return max(float(weights.get(k, 0.6)) for k in res.kinds)


def _status_weight(res: Resource, cfg: dict) -> float:
    weights = cfg.get("status_weight") or {}
    return float(weights.get(res.status.value, 0.5))


def _freshness(res: Resource, cfg: dict) -> float:
    # 没有分享时间 -> **中性 1.0**，不该扣分。
    # API 直链（arXiv/Crossref/MangaDex）天然没有 shared_at，原来给 0.8
    # 等于无端打八折 —— 实测这让学术查询里本该排第一的 arXiv 论文
    # 被"侵略机器 War Machine"这类只命中 "machine" 的网盘结果压下去。
    # 缺数据不等于不新鲜。
    if not res.shared_at:
        return 1.0
    halflife = float(cfg.get("freshness_halflife_days") or 730)
    when = res.shared_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    days = max(0.0, (datetime.now(timezone.utc) - when).days)
    return max(0.35, math.pow(0.5, days / halflife))


def score_resource(res: Resource, kw: str, cfg: dict | None = None) -> float:
    """打分：全部加成都是**乘法因子**，最后再乘状态权重。

    两个反直觉的坑（都是被真实结果逼出来的）：

    1. 加法是有害的：百度优先、提取码、多源命中如果是加数，一条相关性只有 0.53
       的不相关结果能拿到 0.83 分，压过相关性 1.0 的相关结果（0.8 分）。
    2. 光靠因子相乘还不够：百度(×1.25) × 提取码(×1.1) × 多源命中(×1.35) = ×1.86，
       仍能翻过"主题词命中/缺失"之间约 3 倍的相关性差距。所以相关性再加一个幂次
       闸门（默认平方），把差距拉到 ~9 倍，任何加成组合都翻不过来。
    3. 状态权重必须最后相乘，否则失效链接仍会拿到高分。
    """
    cfg = cfg or scoring_cfg()

    relevance = _relevance(res, kw) ** float(cfg.get("relevance_power") or 1.0)
    score = relevance * _kind_weight(res, cfg) * _freshness(res, cfg)

    # 多源命中加成（相对提升，封顶 35%）
    bonus = float(cfg.get("multi_source_bonus") or 0.0)
    source_count = len(set(res.sources))
    if source_count > 1:
        score *= 1.0 + min(0.35, (source_count - 1) * bonus)

    # RRF（Reciprocal Rank Fusion）多源融合：Σ 1/(60+rank)。
    # 比"命中次数"更细 —— 在 TG 的 BM25 序和站点/网页序里都排前面的资源更可信。
    rrf_bonus = float(cfg.get("rrf_bonus") or 0.0)
    if rrf_bonus > 0 and res.rrf:
        score *= 1.0 + min(0.30, rrf_bonus * res.rrf)

    # 百度优先 —— **只给能确认可用的链接**。
    # 若不加这个条件：百度 share/verify 被锁（恒返回 -62）时，一堆"确定不了"的
    # 百度链接光靠域名就能压过已验证可用的夸克链接，实测表现为
    # 「漂流少年」前排全是不对版的百度结果、真资源在夸克却排在后面。
    if res.pan_type is PanType.BAIDU and res.usable:
        score *= 1.0 + float(cfg.get("pan_priority_bonus") or 0.0)

    # 有提取码 = 可直接用 —— 但只在**链接确实可确认可用**时才算优势。
    # 否则一堆"提取码还没验证过"的百度链接会靠这个加成压过确认可用的夸克链接。
    if res.pwd and res.usable:
        score *= 1.1

    # 只被"放宽查询"命中的结果降权（保留可发现性，但不淹没主查询结果）
    if not res.from_primary and _relevance(res, kw) < 0.85:
        score *= float(cfg.get("relaxed_penalty") or 0.5)

    return round(score * _status_weight(res, cfg), 6)


def score_all(resources: list[Resource], kw: str) -> list[Resource]:
    cfg = scoring_cfg()
    for res in resources:
        if res.titles:
            queries = [kw, *res.queries]
            res.title = max(
                dict.fromkeys([res.title or "", *res.titles]),
                key=lambda t: (max(_relevance_single(res, q, title=t) for q in queries), len(t)),
            ) or None
        res.relevance = _relevance(res, kw)
        res.score = score_resource(res, kw, cfg)
    return resources


def sort_resources(resources: list[Resource], *, alive_first: bool = True) -> list[Resource]:
    """排序：失效沉底 → 匹配程度 → 确定性分层 → 分数 → 可用性/网盘优先级。

    分层顺序（越靠前越可信）：
      0  确认存活（alive）
      1  存活但没能确认可用（需提取码 / 码不对）
      2  确定不了 / 该网盘不支持验活（unknown / unchecked / unsupported）

    为什么要单独分层：百度 share/verify 被锁后，大量百度链接只能判到"需提取码"。
    如果和"确认存活"的夸克链接混在同一层比分数，它们会靠"百度优先"占前排 ——
    实测「漂流少年」就是这个现象（前排全是不对版的百度结果，真资源在夸克）。
    """
    prio = {p: i for i, p in enumerate(pan_priority())}
    confirmed_dead = {Status.DEAD, Status.NOT_FOUND}
    alive_set = {Status.ALIVE}
    partial = {Status.NEED_PWD, Status.WRONG_PWD}

    def sort_key(res: Resource):
        st = res.status
        dead = 1 if st in confirmed_dead else 0
        if not alive_first:
            dead = 0
        if st in alive_set:
            tier = 0
        elif st in partial:
            tier = 1
        else:
            tier = 2
        # 只在分数相同时才用"可确认可用"和网盘优先级决胜，
        # 避免它们盖过相关性差异
        not_usable = 0 if res.usable else 1
        pan_rank = prio.get(res.pan_type.value, len(prio))
        # 先比较匹配程度，再在相近结果里比较存活状态。
        relevance_tier = 0 if res.relevance >= 0.85 else 1 if res.relevance >= 0.45 else 2
        return (dead, relevance_tier, tier, -res.score, not_usable, pan_rank)

    return sorted(resources, key=sort_key)
