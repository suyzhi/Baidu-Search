"""链接解析 / 提取码 / 归一化 的单元测试。"""

from __future__ import annotations

import pytest

from pansearch.models import PanType
from pansearch.normalize import (
    detect_pan_type,
    normalize_url,
    parse_baidu,
    pwd_from_url,
    resource_key,
    strip_leading_one,
)


def test_parse_baidu_query_pwd():
    assert parse_baidu("https://pan.baidu.com/s/1AbCdEf?pwd=1234") == ("1AbCdEf", "1234")


def test_parse_baidu_fragment_pwd():
    assert parse_baidu("https://pan.baidu.com/s/1AbCdEf#1234") == ("1AbCdEf", "1234")


def test_parse_baidu_share_init():
    surl, pwd = parse_baidu("https://pan.baidu.com/share/init?surl=1XyZ_ab")
    assert surl == "1XyZ_ab"
    assert pwd is None


def test_parse_baidu_url_encoded():
    surl, _ = parse_baidu("https://pan.baidu.com%2Fs%2F1AbCdEf")
    assert surl == "1AbCdEf"


def test_parse_baidu_no_pwd():
    assert parse_baidu("https://pan.baidu.com/s/1AbCdEf") == ("1AbCdEf", None)


def test_strip_leading_one():
    # 百度 API 的 surl 要去掉开头那个 1（用错会得到恒为 2 的假阳性）
    assert strip_leading_one("1etZdVXAv3tJBuk42BpmujA") == "etZdVXAv3tJBuk42BpmujA"
    assert strip_leading_one("abc") == "abc"
    assert strip_leading_one("1") == "1"  # 不能变成空串


def test_detect_pan_type():
    cases = {
        "https://pan.baidu.com/s/1abc": PanType.BAIDU,
        "https://yun.baidu.com/s/1abc": PanType.BAIDU,
        "https://pan.quark.cn/s/deadbeef": PanType.QUARK,
        "https://www.alipan.com/s/xyz": PanType.ALIYUN,
        "https://115cdn.com/s/swwybvp3zs4?password=g2b0": PanType.P115,
        "https://pan.xunlei.com/s/VO0wkjS2": PanType.XUNLEI,
        "https://cloud.189.cn/t/UVfMFje2IRRn": PanType.TIANYI,
        "https://123pan.com/s/7Tx1jv-JwP7v?pwd=xoxo": PanType.P123,
        "https://drive.uc.cn/s/8355afb5e73f4": PanType.UC,
        "magnet:?xt=urn:btih:abc": PanType.MAGNET,
        "https://example.com/x": PanType.OTHER,
    }
    for url, expected in cases.items():
        assert detect_pan_type(url) is expected, url


def test_detect_pan_type_no_substring_false_positive():
    # 不能把 "notpan.baidu.com.evil.com" 当成百度网盘
    assert detect_pan_type("https://pan.baidu.com.evil.com/s/1abc") is PanType.OTHER


def test_pwd_from_url():
    assert pwd_from_url("https://115cdn.com/s/x?password=g2b0") == "g2b0"
    assert pwd_from_url("https://pan.baidu.com/s/1abc?pwd=9z8y") == "9z8y"
    assert pwd_from_url("https://pan.baidu.com/s/1abc#9z8y") == "9z8y"
    assert pwd_from_url("https://pan.baidu.com/s/1abc") is None
    # 太短/太长的都不是提取码
    assert pwd_from_url("https://pan.baidu.com/s/1abc?pwd=abc") is None


def test_normalize_url_strips_query():
    assert normalize_url(PanType.BAIDU, "https://pan.baidu.com/s/1abc?pwd=1234", "1abc", "1234") == (
        "https://pan.baidu.com/s/1abc"
    )
    assert normalize_url(PanType.QUARK, "https://pan.quark.cn/s/abc?x=1", None, None) == (
        "https://pan.quark.cn/s/abc"
    )


def test_resource_key_magnet_case_insensitive():
    a = resource_key(PanType.MAGNET, "magnet:?xt=urn:btih:ABC123&dn=x", None)
    b = resource_key(PanType.MAGNET, "magnet:?xt=urn:btih:abc123&dn=y", None)
    assert a == b


def test_resource_key_baidu_uses_surl():
    assert resource_key(PanType.BAIDU, "https://pan.baidu.com/s/1abc?pwd=1", "1abc") == "baidu:1abc"


# ---------------------------------------------------------------- 畸形 URL 稳健性
# urlsplit 对 netloc 里带全角字符的 URL 会抛
#   ValueError: netloc '...' contains invalid characters under NFKC normalization
# 这些 URL 来自外部数据（聚合引擎 JSON / 抓到的页面），一个坏链接就能把整个
# 数据源的结果带走 —— 所以所有解析函数都必须不抛异常。
NFKC_BAD_URLS = [
    "http://|file|电影：2026.mkv|2260",       # 全角冒号
    "http://a：b/x",
    "magnet://|file|测试：片名|123",
    "http://a＠b/x",                          # 全角 @
    "http://a／b/x",                          # 全角斜杠
]


@pytest.mark.parametrize("url", NFKC_BAD_URLS)
def test_detect_pan_type_never_raises(url):
    assert detect_pan_type(url) in set(PanType)


@pytest.mark.parametrize("url", NFKC_BAD_URLS)
def test_all_parsers_survive_malformed_urls(url):
    from pansearch.verifiers import path_id

    pwd_from_url(url)                       # 不抛即通过
    parse_baidu(url)
    path_id(url)
    normalize_url(PanType.OTHER, url, None, None)
    resource_key(PanType.OTHER, url, None)


def test_safe_urlsplit_falls_back_gracefully():
    from pansearch.normalize import safe_hostname, safe_urlsplit

    assert safe_hostname("http://a：b/x") == ""
    split = safe_urlsplit("http://a：b/x")
    assert split.path                      # 退化成 path，但拿到了内容
    assert safe_urlsplit(None).path == ""  # type: ignore[arg-type]


def test_safe_hostname_normal_case():
    from pansearch.normalize import safe_hostname

    assert safe_hostname("https://Pan.Baidu.com/s/1abc") == "pan.baidu.com"
