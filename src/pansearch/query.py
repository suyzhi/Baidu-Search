"""查询词规范化：主题词与画质、格式等修饰词分开处理。"""

import re
import unicodedata
from functools import lru_cache

# 简繁归一（可选依赖 opencc-purepy）：TG 频道里繁体标题很常见，
# 「周杰倫」和「周杰伦」必须能互相命中。装不上就退化为不转换。
try:  # pragma: no cover - 依赖是否存在由环境决定
    from opencc_purepy import OpenCC as _OpenCC

    _T2S = _OpenCC("t2s")
except Exception:  # noqa: BLE001
    _T2S = None

# 分隔符：空白 + 中英文标点 / 引号 / 括号。
# 用户经常把标题原样粘进来（“三体”、"三体"、「沙丘」、《三体》全集、三体!），
# 标点若留在检索词里，`LIKE '%"三体"%'` 和各源的子串匹配都会 0 命中 ——
# 表现出来就是"搜什么都搜不到"。
# 注意**不要**把 "+" "#" 当分隔符：C++ / C# 是完整词。
_SEPARATORS = (
    r"\s,，、/|·:：;；!！?？.。…—–～~"
    r"()（）\[\]【】{}｛｝<>《》〈〉「」『』"
    r"“”‘’\"'`^_-"
)
_SPLIT = re.compile(f"[{_SEPARATORS}]+")

# 零宽 / 不可见格式字符（从网页或聊天里粘贴时经常混进来）
_INVISIBLE_RE = re.compile(
    r"[\u00ad\u180e\u200b-\u200f\u2028\u2029\u202a-\u202e\u2060-\u2064\ufeff]"
)

# ---- 检索词角色（轻量查询理解）----
# 主题词：真正界定"搜什么"的内容词（沙丘 / MATLAB / 三体）。
# 限定词：画质 / 介质 / 格式（4K / 蓝光 / epub），只说明"要哪个版本"。
# 通用词：动作 / 平台 / 泛化词（破解 / 下载 / 合集 / 网盘）。
#
# 为什么要分层：BM25 靠 IDF 自动压低"破解"这类几乎每条消息都出现的高频词
# （IDF→0，对排序几乎无贡献）。我们没有倒排词频索引，就用一份小而准的通用词表
# 做等价处理 —— 主题词只从内容词里取。
# 实测「MATLAB 破解」曾把电视剧《河神之渡阴》排到第一，只因正文里出现过"破解"：
# 破解被当成主题词命中 → relevance 0.725。分层后主题词只剩 matlab，立刻沉底。
QUALIFIERS = frozenset({
    "4k", "8k", "2160p", "1080p", "720p", "hd", "hdr", "hdr10", "dv",
    "杜比", "dolby", "atmos", "全景声", "高码", "高码率", "remux", "原盘",
    "蓝光", "bluray", "webrip", "webdl", "bdrip", "hdtv",
    "中字", "中文字幕", "字幕", "国语", "粤语", "双语", "简中", "繁中", "内嵌",
    "全集", "完整版", "完结", "未删减", "导演剪辑版", "加长版", "高清", "超清",
    "无损", "hires", "hi-res", "母带", "原抓", "整轨", "分轨",
    "pdf", "epub", "mobi", "azw3", "txt", "flac", "wav", "ape", "dsd",
    "mp3", "mp4", "mkv", "iso", "电子书",
})

GENERIC = frozenset({
    # 动作 / 状态
    "破解", "破解版", "激活", "激活版", "注册机", "绿色版", "便携版", "免安装",
    "免安装版", "安装包", "安装", "安装版", "硬盘版", "下载", "下载版",
    "在线", "观看", "播放", "免费", "免费版", "分享", "整理", "更新", "最新",
    # 平台 / 载体
    "网盘", "云盘", "百度", "百度网盘", "夸克", "阿里云盘", "115", "迅雷",
    "天翼", "磁力", "链接", "提取码", "密码",
    # 泛化容器 / 版本词
    "资源", "合集", "整合包", "教程", "教学", "网课", "课程", "视频", "音频",
    "软件", "版本", "版", "中文", "中文版", "汉化", "汉化版", "官方", "官方版",
    "正式版", "旗舰版", "专业版", "高清版", "手机版", "电脑版", "安卓版",
    # 课程级别 / 学习资料类（只说明"什么难度/什么形式"，不界定主题）
    "零基础", "入门", "初级", "中级", "高级", "进阶", "基础", "自学", "全套",
    "从零", "系统", "教材", "讲义", "课件", "真题", "题库", "笔记", "资料",
    "习题", "答案", "考点", "精讲",
})

# 兼容旧名：凡"不是主题词"的都算 MODIFIERS
MODIFIERS = QUALIFIERS | GENERIC

# 可以被剥离的通用词素：只取**长且无歧义**的通用/容器/版本词。
# 故意不含 hd / dv / 4k / pdf 这类两三字母缩写 —— 它们是 hdmi、dvr 等真词的前缀，
# 剥了会把「HDMI」切成 hd+mi。
_PEELABLE = GENERIC | {
    "全集", "完整版", "完结", "未删减", "导演剪辑版", "加长版",
    "蓝光", "原盘", "中字", "中文字幕", "字幕", "电子书", "高清", "超清",
}
_MORPHEMES = tuple(sorted((m for m in _PEELABLE if len(m) >= 2), key=len, reverse=True))


def _segment(token: str) -> list[str]:
    """把 token 前后的通用词素剥下来：AutoCAD破解版 -> [AutoCAD, 破解版]。

    只在两侧都能剥出**非空主干**（≥2 字符）时才剥，避免把
    「115」「4K」「全集」这种本身就是词素的 token、以及「HDMI」这类
    恰好以矮词素开头的真词拆坏。
    """
    if token in MODIFIERS:
        return [token]
    lead: list[str] = []
    rest = token
    changed = True
    while changed:
        changed = False
        for m in _MORPHEMES:
            if rest.startswith(m) and len(rest) - len(m) >= 2:
                lead.append(m)
                rest = rest[len(m):]
                changed = True
                break
    tail: list[str] = []
    changed = True
    while changed:
        changed = False
        for m in _MORPHEMES:
            if rest.endswith(m) and len(rest) - len(m) >= 2:
                tail.insert(0, m)
                rest = rest[:-len(m)]
                changed = True
                break
    return [*lead, rest, *tail] if rest else [*lead, *tail]


@lru_cache(maxsize=16384)
def _t2s(text: str) -> str:
    if _T2S is None:
        return text
    try:
        return _T2S.convert(text)
    except Exception:  # noqa: BLE001
        return text


@lru_cache(maxsize=16384)
def normalize_text(text: str) -> str:
    """繁→简 + NFKC 归一 + 去不可见字符 + 大小写折叠。"""
    folded = _t2s(_INVISIBLE_RE.sub("", str(text)))
    return unicodedata.normalize("NFKC", folded).casefold().strip()


def split_query(query: str) -> list[str]:
    """把查询切成词（保留原始大小写），丢掉纯标点碎片。

    保留大小写是为了补搜词展示/检索更自然（"Dune" 不该变成 "dune"）；
    真正比较匹配时统一走 normalize_text。
    无空格连写的通用词素会被剥开（见 _segment），否则「AutoCAD破解版」整串
    会被当成一个主题词，等于没分层。
    """
    text = unicodedata.normalize("NFKC", _t2s(_INVISIBLE_RE.sub("", str(query)))).strip()
    out: list[str] = []
    for token in _SPLIT.split(text):
        if not token or not any(ch.isalnum() for ch in token):
            continue
        out.extend(_segment(token))
    return [t for t in out if t and any(ch.isalnum() for ch in t)]


def query_terms(query: str) -> list[str]:
    return list(dict.fromkeys(t.casefold() for t in split_query(query)))


def subject_terms(query: str) -> list[str]:
    """主题词 = 去掉限定词、通用词和纯数字后剩下的内容词。

    全部被过滤时返回空列表 —— 调用方应回退到 query_terms（例如查询本身就是
    「4K」「下载」「网课」这种词）。
    """
    return [t for t in query_terms(query) if t not in MODIFIERS and not t.isdigit()]


def analyze(query: str) -> dict[str, list[str]]:
    """把查询拆成主题词 / 限定词 / 通用词 / 数字，便于排序和排查。"""
    terms = query_terms(query)
    return {
        "subjects": subject_terms(query),
        "qualifiers": [t for t in terms if t in QUALIFIERS],
        "generic": [t for t in terms if t in GENERIC],
        "numbers": [t for t in terms if t.isdigit()],
    }


def term_present(text: str, term: str) -> bool:
    if term in MODIFIERS:
        return term in text  # 4kHDR/DV 这类连写格式也应匹配。
    if re.search(r"[\u3400-\u9fff]", term):
        # 分享标题常在中文名/续集编号间插空格，不能因此漏掉同名资源。
        pattern = r"\s*".join(re.escape(ch) for ch in term)
        if term[-1:].isdigit():
            pattern += r"(?!\d)"
        return re.search(pattern, text) is not None
    # 英文词不能在 unrelated/machinery 这类单词内部误命中；允许 Serum2/4K。
    if re.fullmatch(r"[a-z][a-z0-9+]*", term):
        return re.search(r"(?<![a-z])" + re.escape(term) + r"(?![a-z])", text) is not None
    return term in text
