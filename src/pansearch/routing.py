"""查询的垂直领域识别 —— 决定一次搜索该打哪些资源站。

为什么必需：要覆盖"所有领域"就得有几百个站，但一次搜索不可能全打一遍
（几百个站 × 每个几秒 = 分钟级）。所以先判断查询属于哪些垂直领域，
只打相关的站点子集 + 通用站点。

判据是"查询里出现了哪些领域的特征词"，取命中数最高的若干领域；
一个都没命中就退回 general（只打通用站）。
"""

from __future__ import annotations

import re
from functools import lru_cache

# 各垂直领域的特征词（小写匹配）。中英混排，覆盖常见说法。
VERTICAL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "movie": (
        "电影", "影片", "蓝光", "原盘", "remux", "bdrip", "web-dl", "webdl",
        "1080p", "2160p", "4k", "hdr", "杜比", "dolby", "atmos", "美剧", "英剧",
        "韩剧", "日剧", "剧集", "连续剧", "电视剧", "纪录片", "综艺", "movie", "film",
    ),
    "anime": (
        "动漫", "番剧", "新番", "漫画", "同人", "ova", "tva", "anime", "manga",
        "轻小说", "galgame", "里番", "剧场版",
    ),
    "music": (
        "音乐", "专辑", "无损", "flac", "wav", "ape", "dsd", "hires", "歌曲",
        "单曲", "ost", "原声", "歌单", "演唱会", "专辑下载", "music", "album",
        "hi-res", "mora",
    ),
    "ebook": (
        "电子书", "小说", "图书", "书籍", "教程书", "pdf", "epub", "mobi", "azw3",
        "txt", "ebook", "杂志", "期刊下载", "书单", "阅读",
    ),
    "course": (
        "教程", "课程", "网课", "培训", "教学", "视频教程", "公开课", "训练营",
        "课件", "讲义", "course", "tutorial", "lecture",
    ),
    "software": (
        "软件", "破解", "激活", "注册机", "绿色版", "便携版", "免安装", "office",
        "adobe", "photoshop", "windows", "mac", "工具", "效率", "app", "工具包",
    ),
    "audio-tool": (
        "vst", "vst3", "音源", "合成器", "采样", "kontakt", "serum", "omnisphere",
        "宿主", "daw", "cubase", "fl studio", "ableton", "logic", "插件",
        "混音", "母带", "效果器", "preset", "音色", "soundbank", "大气合成器",
    ),
    "game": (
        "游戏", "steam", "switch", "ps5", "ps4", "xbox", "汉化", "免安装",
        "模拟器", "rom", "单机", "手游", "游戏下载", "game", "repack",
    ),
    "design": (
        "素材", "模板", "psd", "笔刷", "字体", "设计", "ui", "icon", "mockup",
        "预设", "调色", "ae 模板", "pr 模板", "素材包",
    ),
    "academic": (
        "论文", "文献", "sci", "sci-hub", "期刊", "arxiv", "知网", "万方",
        "学位", "考研资料", "paper", "journal", "research", "preprint",
        "thesis", "dissertation", "citation", "doi", "study", "scholar",
        "physics", "chemistry", "biology", "mathematics", "machine learning",
        "neural", "quantum", "algorithm",
    ),
    "comic": (
        "漫画", "条漫", "汉化组", "comic", "cbr", "cbz",
        # 英文侧：API（MangaDex）主要靠这些词命中
        "manga", "manhwa", "manhua", "webtoon", "doujin",
    ),
}

# 通用站永远参与（不挑领域）
GENERAL_VERTICAL = "general"

_SPLIT = re.compile(r"[\s,，、/|·+]+")


@lru_cache(maxsize=1)
def _lowered_keywords() -> dict[str, tuple[tuple[str, int], ...]]:
    """预排序：长词优先，避免 "4k" 抢在 "4k hdr" 前面重复计分。"""
    out: dict[str, tuple[tuple[str, int], ...]] = {}
    for vertical, words in VERTICAL_KEYWORDS.items():
        pairs = sorted(((w.lower(), len(w)) for w in words), key=lambda x: -x[1])
        out[vertical] = tuple(pairs)
    return out


def classify(kw: str, *, max_verticals: int = 3) -> list[str]:
    """返回查询命中的垂直领域，按命中强度排序。

    命中强度 = 命中的特征词个数，长词加权（"4k hdr" 比 "4k" 更能说明问题）。
    一个都没命中时返回 []，调用方应只使用通用站点。
    """
    text = (kw or "").strip().lower()
    if not text:
        return []
    scores: dict[str, float] = {}
    for vertical, words in _lowered_keywords().items():
        score = 0.0
        for word, weight in words:
            if word in text:
                score += 1.0 + weight / 10.0     # 长词稍微加点权
        if score:
            scores[vertical] = score
    if not scores:
        return []
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    return [v for v, _ in ranked[:max_verticals]]


def keyword_hints(kw: str) -> list[str]:
    """从查询里抽出可用于筛选站内结果的领域特征词（暂时未用于打分，留给后续）。"""
    text = (kw or "").lower()
    hits: list[str] = []
    for words in _lowered_keywords().values():
        for word, _ in words:
            if word in text and word not in hits:
                hits.append(word)
    return hits


__all__ = ["GENERAL_VERTICAL", "VERTICAL_KEYWORDS", "classify", "keyword_hints"]
