"""测试全局配置。

查询级缓存是进程内的，跨测试会串味（同一个关键词 + 同一组参数会拿到上一条用例的
结果），所以每条用例前后都清一次。
"""

from __future__ import annotations

import pytest

from pansearch import pipeline


@pytest.fixture(autouse=True)
def _clear_query_cache():
    pipeline.clear_query_cache()
    yield
    pipeline.clear_query_cache()
