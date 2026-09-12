# pansearch · 全网网盘资源搜索器

输入一个关键词，**并发打穿多个数据源**，把散落全网的网盘分享链接捞回来，**去重、验活、排序**，用一份干净的结果呈现。

**百度网盘优先 + 全类型兜底**（夸克 / 阿里云盘 / 115 / 迅雷 / 天翼 / 123 / UC / PikPak / 磁力）。

---

## 快速开始

```bash
cd pan-sousuo
uv venv && uv pip install -e ".[web,dev]"

# 网页版（推荐）
.venv/bin/pansearch web
# → http://127.0.0.1:8765

# 命令行
.venv/bin/pansearch search "周杰伦" --type baidu -n 20
```

### 命令行用法

```bash
# 搜索（默认只显示存活链接）
pansearch search "三体"
pansearch search "三体" --type baidu,quark      # 限定网盘类型
pansearch search "三体" --all                   # 包含失效链接
pansearch search "三体" --no-verify             # 跳过验活（快很多）
pansearch search "三体" --json out.json --csv out.csv
pansearch search "三体" --origins               # 打印来源页面

pansearch verify "https://pan.baidu.com/s/1xxxx" -p 提取码   # 校验单条
pansearch sources                               # 列出启用的数据源
pansearch stats                                 # 本地缓存统计
```

---

## 为什么这个工具"准"——三个实测结论

### 1️⃣ 百度链接可以免登录验活，但要小心一个坑

百度 API 的 `surl` 参数形式，**两个接口是相反的**：

| 接口 | token 形式 | 用途 |
|---|---|---|
| `POST /share/verify` | **去掉开头 `1`** | 有提取码时的权威校验 |
| `GET /api/shorturlinfo` | **保留开头 `1`**（完整） | 无提取码时判断存活 |

用错就完蛋：`shorturlinfo` 用去掉 `1` 的 token，**对任何链接都返回 `errno=2`**（假阳性，垃圾链接会被判成"有效"）；`share/verify` 用完整 token 则**恒返回 105**。

实测校准结果（真实链接 + 随机伪造 token 交叉验证）：

| 接口 | errno | 含义 |
|---|---|---|
| `share/verify` | `0`（含 `randsk`） | ✅ 提取码正确，可直接打开 |
| | `-9` | ⚠️ 提取码错误（链接本身可能存活） |
| | `105` | ❌ 分享不存在 |
| `shorturlinfo` | `-9` / `-21` | ✅ 存活，需要提取码 |
| | `0` | ✅ 存活，无需提取码 |
| | `140` | ❌ 链接不存在 |
| | `-3` | ❌ 已失效 |

码表在 `config/baidu_errno.yaml`，接口变了改配置就行。全部免登录、无验证码。

### 2️⃣ 不能只靠搜索引擎找网盘链接

实测（关键词「周杰伦」「三体」）——**绝大多数搜索引擎并不索引 `pan.baidu.com/s/xxx` 本身**：

| 引擎 | `site:pan.baidu.com/s <kw>` 结果 |
|---|---|
| Brave | ✅ 20 条（**主力**，但会 429 限流） |
| 360 / so.com | ⚠️ 偶有 1 条 |
| Google / Bing / DDG / Mojeek / Startpage / 百度 | ❌ 一律 0 条 |

所以 B 环采用**两阶段**：先搜到「讨论这个资源的页面」（论坛帖 / 博客），再抓这些页面抽链接——链接就藏在正文里。

### 3️⃣ 排序里"状态"必须是乘法，不能是加法

如果存活/失效只是一个加数，百度优先加分和提取码加分就会把**失效链接抬到高分**。
本工具把状态权重**最后相乘**：失效链接分数直接掉到存活链接的 1/50，稳稳沉底。

---

## 架构

```
        ┌──────────────── 输入：关键词 ────────────────┐
        ▼
 A 环  PanSou 聚合引擎     48 个网盘搜索插件 + 上千 TG 频道   ← 覆盖面最广
 B 环  搜索引擎定向检索     Brave / 360 → 找内容页 → 抓页面   ← 捞"野链接"
 C 环  UGC 平台内搜         B站 / 贴吧 / 论坛                 ← 规划中
 D 环  本地私有索引         SQLite 缓存 + 历史沉淀            ← 规划中
        │
        ▼
 抽取 → 归一化 → 去重 → 验活 → 排序 → 输出(CLI / Web / JSON / CSV)
```

### 目录

```
pan-sousuo/
├── config/
│   ├── sources.yaml          # 数据源开关 / 权重 / 限速
│   └── baidu_errno.yaml      # 百度 errno 码表（实测校准）
├── src/pansearch/
│   ├── models.py             # RawHit / Resource / VerifyResult / 状态枚举
│   ├── adapters/
│   │   ├── base.py           # 适配器基类 + 注册表（新增源 = 加一个文件）
│   │   ├── pansou.py         # A 环
│   │   └── websearch.py      # B 环（两阶段）
│   ├── extract.py            # 链接 / 提取码抽取（两遍扫描，全局贪心配对）
│   ├── normalize.py          # URL & 提取码归一 + 网盘类型识别
│   ├── dedupe.py             # 分享指纹去重合并
│   ├── verify.py             # 百度链接验活 + 限速 + TTL 缓存
│   ├── score.py              # 排序打分
│   ├── store.py              # SQLite 缓存 / 搜索日志
│   ├── pipeline.py           # 并发编排（单源失败隔离）
│   ├── cli.py                # CLI
│   ├── webapp.py             # Web API
│   └── web/index.html        # 单页 UI（零构建）
└── tests/                    # 52 个离线测试
```

### 加一个新数据源

```python
# src/pansearch/adapters/mysite.py
from .base import Adapter, register
from ..models import RawHit

@register
class MySiteAdapter(Adapter):
    name = "mysite"
    kind = "forum"

    async def search(self, kw, client) -> list[RawHit]:
        ...
```

在 `config/sources.yaml` 里加一段 `mysite: {enabled: true, ...}` 即可。单个源超时/报错**不影响整体结果**，只会在摘要里提示。

---

## 配置

`config/sources.yaml` 常用项：

```yaml
sources:
  pansou:
    instances: ["https://so.252035.xyz"]   # 可写多个，按序降级
    rate_limit_qps: 1.5
  websearch:
    rate_limit_qps: 0.4        # brave 对频率敏感，别调高
    fetch_result_pages: true   # 阶段 2：抓内容页（提升召回，但更慢）
    max_pages: 6

verify:
  concurrency: 4
  rate_limit_qps: 3.0          # 对 pan.baidu.com 温柔点
  cache_ttl_hours: 6
```

---

## 已知限制

| 限制 | 说明 / 下一步 |
|---|---|
| **公共 PanSou 实例不稳定** | 实测偶发 400 / timeout，已做多实例 + 重试降级。**下一步自建 Docker 版 PanSou**（48 插件全量 + 不限流），这是提升覆盖最有效的一步 |
| **Brave 会 429** | 已实现限速 + 本轮冷却。长期方案：自建 SearXNG 实例（支持 JSON API，可聚合多引擎） |
| **B 站未接入** | `api.bilibili.com` 返回 412（风控），需要 WBI 签名 + Cookie。B站视频简介和评论区是网盘链接的高产来源，值得做 |
| **贴吧 403** | 百度安全验证，需走移动端接口 |
| **只验活百度** | 夸克/阿里/115 等的验活接口是另一套，尚未实现，这些结果显示为"未校验(非百度)" |
| **提取码可能不准** | 抽取自页面文本，可能拿到别处的 4 位串。这类会显示"存活/码不对"而不是误判失效 |

---

## 合规声明

本工具**只做公开链接的检索与索引**：

- ❌ 不下载、不存储任何文件
- ❌ 不破解、不绕过验证码、不绕过登录
- ❌ 不做"一键转存到自己网盘"
- ✅ 全局限速，尊重目标站点
- ✅ 界面带免责提示

搜索结果均来自第三方公开索引，请自行判断合法性与版权，优先通过官方渠道获取。

---

## 开发

```bash
.venv/bin/python -m pytest -q        # 52 个测试，全离线，1 秒跑完
```

测试锁住了几个关键结论（token 形式、errno 码表、提取码不串味、缓存键带提取码、状态权重是乘法），改动这些逻辑时会立刻报警。
