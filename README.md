# pansearch · 全网网盘资源搜索器

输入一个关键词，**并发打穿多个数据源**，把散落全网的网盘分享链接捞回来，**去重、验活、排序**，用一份干净的结果呈现。

**百度网盘优先 + 全类型兜底**（夸克 / 阿里云盘 / 115 / 迅雷 / 天翼 / 123 / UC / PikPak / 磁力）。

---

## 快速开始

```bash
cd pan-sousuo
uv venv && uv pip install -e ".[web,dev]"

# ① 先把 PanSou 聚合引擎跑起来（强烈推荐，召回差 7 倍）
./scripts/start-pansou.sh          # 需要 colima：brew install colima docker

# ② 建立 TG 频道索引（召回主力，越深越全）
.venv/bin/pansearch index crawl --pages 3             # 136 频道 × 3 页 ≈ 30 秒
.venv/bin/pansearch index crawl --pages 100 --deepen  # 继续向历史深挖（可挂 cron）

# ③ 搜
.venv/bin/pansearch web            # 网页版 → http://127.0.0.1:8765
.venv/bin/pansearch search "周杰伦" --type baidu -n 20
```

> ①②不做也能用，只是召回少很多：TG 索引为空时会自动抓一轮最新消息，
> PanSou 会自动降级到公共实例。但**自建 + 深挖索引是召回的两个主要来源**。

### 命令行用法

```bash
# 搜索（默认只显示存活链接，已自动剔除失效）
pansearch search "三体"
pansearch search "三体" --type baidu,quark      # 限定网盘类型
pansearch search "三体" --strict                # 严格模式：连无法验活的网盘也剔除
pansearch search "三体" --all                   # 包含已失效链接
pansearch search "三体" --no-verify             # 跳过验活（快很多）
pansearch search "三体" --no-relax              # 不自动补搜（只要精确匹配时用）
pansearch search "三体" --json out.json --csv out.csv
pansearch search "三体" --origins               # 打印来源页面

pansearch index crawl --pages 30 --deepen       # 加深 TG 索引（长期资产）
pansearch index stats                           # 索引覆盖情况
pansearch verify "https://pan.quark.cn/s/xxx"   # 校验单条（支持 5 种网盘）
pansearch verify "https://115.com/s/xxx" -p 提取码
pansearch sources                               # 列出启用的数据源
pansearch stats                                 # 本地缓存统计
```

### 剔除失效链接

搜索默认就会**验活并剔除失效链接**。结果里每一条都带状态：

| 状态 | 含义 | 默认是否保留 |
|---|---|---|
| `有效(码已验证)` | 存活，提取码已被接口校验通过 | ✅ |
| `有效(需提取码)` | 存活，但需要提取码 | ✅ |
| `有效(码未验证)` | 存活；该网盘接口不校验提取码 | ✅ |
| `存活/码不对` | 链接还在，但记录到的提取码是错的 | ✅（`--strict` 剔除） |
| `已失效` / `不存在` | 已被取消／过期／不存在 | ❌ **剔除** |
| `未知` | 接口异常，无法判定 | ✅（`--strict` 剔除） |
| `未校验(该网盘不支持)` | 迅雷/UC/123/PikPak/磁力，无公开校验接口 | ✅（`--strict` 剔除） |

```bash
pansearch search "沙丘 4K HDR"            # 默认：剔除已确认失效的
pansearch search "沙丘 4K HDR" --strict   # 严格：只留已验证可用的
```

Web UI 上对应「只看存活」和「严格剔除」两个开关。摘要里会给出**按网盘的验活明细**
（例：`quark 194活/69死/14其他 ｜ baidu 88活/0死`），一眼能看出剔掉了多少。

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

### 3️⃣ 非百度网盘也能验活（实测校准）

只有百度能验活是不够的——实测「沙丘」140 条结果里 **137 条是夸克/迅雷/磁力**，
不验活的话用户点开全是死链。现已支持 5 种网盘：

| 网盘 | 接口 | 存活 | 需码 | 码错 | 失效 |
|---|---|---|---|---|---|
| 百度 | `POST /share/verify` | `errno 0` | `-9`(shorturlinfo) | `-9`(share/verify) | `105` |
| 夸克 | `POST .../sharepage/token` | `code 0` + stoken | — | — | `41006` / 404 |
| 阿里云盘 | `POST .../get_share_by_anonymous` | 200 + share_name | — | — | `NotFound.ShareLink` / `ShareLink.Cancelled` |
| 115 | `GET webapi.115.com/share/snap` | `state: true` | `4100012` | `4100008` | `990002` |
| 天翼 189 | `GET .../getShareInfoByCodeV2.action` | 200 + `<shareVO>` | — | — | `ShareInfoNotFound` |

⚠️ **只有百度和 115 的接口会真正校验提取码**。夸克/阿里/天翼的接口不校验，
所以它们只会显示「有效(码未验证)」——本工具不会替它们宣称"码是对的"。

顺带白捡一个好处：阿里/115/天翼的响应里带着**真实资源名**，会自动用来补全空标题。

### 4️⃣ 排序里"状态""优先""相关"必须层层相乘，且相关性要有幂次闸门

三个反直觉的坑，都是被真实结果逼出来的：

- **状态权重若是加数**，失效链接会被"百度优先 +0.3"和"有提取码 +0.1"抬到高分。
- **加成若是加数**，一条相关性只有 0.53 的「地狱占星师 4K HDR」会拿到 0.83 分，
  **压过**相关性 1.0 的「沙丘2 4K HDR」（0.8 分）。
- **光改成乘法还不够**：百度(×1.25) × 提取码(×1.1) × 多源命中(×1.35) = ×1.86，
  仍能翻过「主题词命中 / 主题词缺失」之间约 3 倍的相关性差距——实测「黑夏 4K HDR」
  「冬城猎凶 4K HDR」就是这样挤进前排的。

现在所有加成都是乘法因子、状态权重最后相乘，并给相关性加一个**平方闸门**
（`relevance_power: 2.0`），把差距拉到 ~9 倍，任何加成组合都翻不过来。

### 5️⃣ 多词查询会召回塌陷

实测「沙丘 4K HDR」在聚合引擎只有 **3~25 条**，而「沙丘」有 **180+ 条**。
所以主查询与放宽查询会**并行发出**（省掉一轮串行等待），并把补搜结果降权 50%，
既拿到召回又不让噪声淹没主查询结果（摘要里会显示补搜了什么）。

### 6️⃣ 召回瓶颈是"数据源"，不是关键词 —— 两个大杠杆

**杠杆 A：自建 PanSou（召回 ×7）**

公共实例实测「三体」只有 41 条（百度 **1** 条），而且频繁 429 / 403 / 超时。
自建后同一关键词 **374 条（百度 28 条）**，热缓存 **0.17 秒**：

| | 结果数 | 百度链接 | 耗时 |
|---|---|---|---|
| 公共实例 | 41 | 1 | 7.5~23s（且常失败） |
| **自建实例** | **374** | **28** | 冷启动 8~30s / **热缓存 0.17s** |

```bash
./scripts/start-pansou.sh     # 一键：起 colima + 拉镜像 + 跑容器
```

**杠杆 B：TG 频道索引（召回主力）**

网盘链接的主要产地是 Telegram 频道。聚合引擎本质也只是在抓它们，
所以本工具直接抓 `t.me/s/<channel>` 并落进本地 SQLite：

- 内置 **136 个频道**（合并自 PanSou 官方配置），用 `?before=<msg_id>` 向历史无限翻页
- 实测 **136 频道 × 3 页 = 6551 条消息，28.9 秒，0 失败**
- 索引是**只增不减的资产**：挖得越深同一个关键词召回越多，而检索是毫秒级

| 关键词 | 索引 6.5k 条 | 索引 212k 条 |
|---|---|---|
| 沙丘 4K HDR | 265 条 | **1469 条** |
| 三体（叠加自建 PanSou 后） | 54 条 | **425 条** |

> ⚠️ TG 频道天然**偏向近期**：212k 条消息里 2026 年占 68%、2025 年占 23%。
> 频道本质是"新片发布流"，经典老片很少重发 —— 所以**老资源主要靠 PanSou 的插件层**
> （它索引的是各个网盘搜索站），这也是自建 PanSou 尤其重要的原因。

### 7️⃣ 速度：给每个源自己的截止时间

各源耗时差了两个数量级，等最慢的就等于把搜索时长交给它：

| 数据源 | 耗时 | 单次产出 |
|---|---|---|
| **自建 PanSou（热缓存）** | **0.17s** | 370+ 条 |
| Telegram 本地索引 | 0.02s | 500~600 条 |
| 自建 PanSou（冷启动） | 8~30s | 370+ 条 |
| 公共 PanSou | 7~25s | 40~170 条 |
| Brave / 360 搜索引擎 | 12~22s | 3~6 条 |

按各源配置独立 `deadline`（PanSou 45s / Brave 12s），超时跳过、其余结果照常返回；
再配合"先排序、只验活最相关的前 300 条"的**验活预算**（700+ 条全量验活要 1~2 分钟，
而用户只看前几十条），实测 **47s → 17~23s**。

---

## 架构

```
        ┌──────────────── 输入：关键词 ────────────────┐
        ▼
 A 环  PanSou 聚合引擎     48 个网盘搜索插件 + 上千 TG 频道   ← 覆盖广，但会限流
 B 环  搜索引擎定向检索     Brave / 360 → 找内容页 → 抓页面   ← 捞"野链接"，产出低
 C 环  Telegram 频道索引    136 频道直连 t.me + 本地 SQLite    ← **召回主力，0.02s**
 D 环  本地私有索引         SQLite 验活缓存 + TG 消息沉淀      ← 越用越全
        │
        ▼
 抽取 → 归一化 → 去重 → 验活 → 排序 → 输出(CLI / Web / JSON / CSV)
```

### 目录

```
pan-sousuo/
├── scripts/
│   └── start-pansou.sh       # 一键自建 PanSou（colima + Docker）
├── config/
│   ├── sources.yaml          # 数据源开关 / 权重 / 限速 / 各源 deadline
│   ├── tg_channels.txt       # 136 个 TG 网盘分享频道
│   ├── baidu_errno.yaml      # 百度 errno 码表（实测校准）
│   └── pan_errno.yaml        # 夸克/阿里/115/天翼 码表（实测校准）
├── src/pansearch/
│   ├── models.py             # RawHit / Resource / VerifyResult / 状态枚举
│   ├── adapters/
│   │   ├── base.py           # 适配器基类 + 注册表（新增源 = 加一个文件）
│   │   ├── pansou.py         # A 环：聚合引擎
│   │   ├── telegram.py       # C 环：TG 频道（查本地索引）
│   │   └── websearch.py      # B 环：搜索引擎两阶段
│   ├── tgindex.py            # TG 频道抓取 + SQLite 索引（召回主力）
│   ├── extract.py            # 链接 / 提取码抽取（两遍扫描，全局贪心配对）
│   ├── normalize.py          # URL & 提取码归一 + 网盘类型识别
│   ├── dedupe.py             # 分享指纹去重合并
│   ├── verify.py             # 百度验活（share/verify + shorturlinfo）
│   ├── verifiers.py          # 验活调度池（夸克/阿里/115/天翼）+ 失效剔除 prune()
│   ├── score.py              # 排序打分（相关性幂次闸门 + 全乘法加成）
│   ├── store.py              # SQLite 验活缓存 / 搜索日志
│   ├── pipeline.py           # 并发编排（单源超时隔离 + 验活预算 + 放宽查询）
│   ├── cli.py                # CLI
│   ├── webapp.py             # Web API
│   └── web/index.html        # 单页 UI（零构建）
└── tests/                    # 130 个离线测试
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
    deadline: 12               # 产出低就不多等（PerSource 超时）
    rate_limit_qps: 1.5
    fetch_result_pages: true   # 阶段 2：抓内容页（比阶段 1 值钱）
    max_pages: 20

  telegram:
    auto_index: true           # 索引为空时自动抓一轮最新消息
    crawl_concurrency: 12

verify:
  concurrency: 16
  rate_limit_qps: 5.0          # 对 pan.baidu.com 温柔点
  budget: 300                  # 只验活最相关的前 N 条；0 = 不限制

fetch_deadline: 15             # 各源缺省超时（可被源自己的 deadline 覆盖）
```

---

## 已知限制

| 限制 | 说明 / 下一步 |
|---|---|
| **公共 PanSou 实例不可靠** | 实测「三体」只有 41 条（百度 1 条），且频繁 400 / timeout / **429 / 403**。已支持多实例降级，**建议用 `scripts/start-pansou.sh` 自建**（374 条 / 百度 28 条 / 热缓存 0.17s） |
| **自建 PanSou 依赖 colima** | `colima start` 后容器才能用；容器设了 `--restart unless-stopped`，但重启电脑后需先起 colima |
| **Brave 会 429** | 已实现限速 + 本轮冷却 + 12s 超时。长期方案：自建 SearXNG 实例（支持 JSON API，可聚合多引擎） |
| **TG 索引需要时间养，且偏近期** | 212k 条消息里 2026 年占 68%。频道是"新片发布流"，经典老片很少重发 → 老资源主要靠 PanSou 插件层。建议定期 `index crawl --deepen`（可挂 cron），索引在 `.cache/` 不进 git |
| **36 个频道抓不到内容** | 136 个里 14 个是搜索机器人频道或已失效/改名；实测有效频道 122 个 |
| **迅雷 / UC / 123 / PikPak / 磁力 无法验活** | 迅雷要 captcha、UC 与 123 的接口已变更、磁力需 DHT。这些显示为「未校验(该网盘不支持)」，**≠ 有效**，可用 `--strict` 剔除 |
| **B 站未接入** | `api.bilibili.com` 返回 412（风控），需要 WBI 签名 + Cookie。B站视频简介和评论区是网盘链接的高产来源，值得做 |
| **贴吧 403** | 百度安全验证，需走移动端接口 |
| **超出验活预算的条目标为"未校验"** | 默认只验活最相关的前 300 条（大结果集全量验活要 1~2 分钟）。要全量可把 `verify.budget` 设为 0 |
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
.venv/bin/python -m pytest -q        # 130 个测试，全离线，2 秒跑完
```

测试锁住了几个关键结论（token 形式、errno 码表、提取码不串味、缓存键带提取码、状态权重是乘法），改动这些逻辑时会立刻报警。
