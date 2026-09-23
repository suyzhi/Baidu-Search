# pansearch · 全网网盘资源搜索器

输入一个关键词，**并发打穿多个数据源**，把散落全网的网盘分享链接捞回来，**去重、验活、排序**，用一份干净的结果呈现。

**百度网盘优先 + 全类型兜底**（夸克 / 阿里云盘 / 115 / 迅雷 / 天翼 / 123 / UC / PikPak / 磁力）。

### 当前搜索策略（2026-09-13，搜索质量与响应优化）

- **统一匹配证据**：检索、评分、过滤共用繁简/全半角归一、中文标点及词边界规则；等价别名同时参与评分和过滤。「大气合成器」可保留标题为「Omnisphere 2 音源」的结果，「沙丘预言」可匹配「沙丘：预言」。URL 和残留的 `?pwd=` 参数不算标题证据。
- **真正的 FTS5 短语**：中文 bigram 在一个引号内连续匹配，例如 `"沙丘 丘预 预言"`，避免把位置分散或顺序颠倒的词片段当成片名。英文前缀召回覆盖 `Serum2`，随后检查词边界。原有索引可直接使用，无需重建。
- **多词意图**：具体主题共同约束结果，`machine learning` 不再只凭 `learning` 保留英语启蒙内容；画质、资源类别、修复版和最终季等描述允许缺省，完整匹配优先。缺标题与未知跨语言标题仍保守保留。词面匹配仍有边界，例如同名歧义、中文长词中包含片名，不能等同于语义理解。
- **减少重复计算**：缓存不可变的查询分析与标题匹配结果，单次打分复用相关性；CPU 处理移到工作线程。RRF 对同一来源只取最佳名次，重复转发或补搜不叠加独立来源奖励。
- **先显示，再补全**：网页通过 `/api/search/stream` 逐步显示各来源结果，联网搜索与验活继续进行；初步结果明确标为未校验。严格模式等待符合验证要求的结果。切换关键词/网盘类型会取消旧请求，旧结果不会覆盖新搜索。原 `/api/search` JSON 接口保留。
- **备用源有机会返回**：PanSou 主实例慢时延迟启动备用实例；主请求仍保留到总预算结束，避免丢掉冷启动结果。成功后取消多余请求，慢/失败实例暂时降优先级，后续可以恢复。
- **验活与故障信息**：保留缓存、最多 150 个未缓存联网校验及 8 秒验活预算；不会将超时标为失效。来源状态、降级原因及阶段耗时分别报告，`verify` 不再混入去重/评分耗时。8 秒仅是验活阶段预算。

### 验证与可复现基准

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/quality_eval.py --gate
.venv/bin/python scripts/fulltest.py --offline --no-verify --gate
.venv/bin/python scripts/benchmark_search.py --database .cache/tg_index.sqlite3
```

`quality_eval.py` 使用可审阅的固定相关性标签，独立计算实际返回条目的 precision、标注相关资源的 recall 与 NDCG。18 个查询覆盖别名、标点、多词意图、软件符号、繁简、画质和版本；它是回归样本，**不代表全网总体准确率**。`fulltest.py` 的 200 个查询用于真实索引覆盖与词面诊断，按实际被评估条目计算分母，不能把这个指标当独立语义准确率。GitHub CI 运行单元测试和独立标注门槛。

本次同机对照，以提交 `779c398` 为基线、读取同一个约 62.8 万条消息的索引。3 轮中位数允许进程内文本缓存变热，不含网络/验活；优化同时改变了候选筛选，不能将不同结果集解释为完全等价的性能对照：

| 本地查询 | 修复前总耗时 | 优化后总耗时 |
|---|---:|---:|
| 三体 电视剧 4K | 456 ms | 20 ms |
| 周杰伦 无损 全集 | 141 ms | 10 ms |
| Python 入门 | 263 ms | 25 ms |
| C# 教程 | 431 ms | 205 ms |

实际网页观察到「沙丘预言」约 0.1 秒显示首批本地结果；完整联网/验活耗时仍受第三方服务影响。精确测量命令与质量样本均在仓库中，以下章节中更早的网络实测仅作历史背景。

---

## 快速开始

```bash
cd pan-sousuo
uv venv && uv pip install -e ".[web,dev]"

# ① 先把 PanSou 聚合引擎跑起来（强烈推荐，召回差 7 倍）
./scripts/build-pansou.sh          # 可选但强烈建议：官方镜像只带 74 个插件，
                                   # 源码里还有 35 个没接线的 —— 自建后 107 个
./scripts/start-pansou.sh          # 需要 colima：brew install colima docker

# ①.5 自建 SearXNG（可选）：一个后端换回十几个引擎（含直连被墙的 Google）
./scripts/start-searxng.sh         # 端口 8889，配置在 config/searxng/settings.yml

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

# 定时维护：加深索引 + 复验过期链接（见下节）
pansearch maintain --json .cache/maintain-report.json
pansearch maintain --force --no-index --verify-limit 400   # 只做复验
```

### 让它自己"越用越全"：定时维护

召回的两个长期资产都是"不做就悄悄退化"的：TG 索引不加深就停在最后一次手动维护的位置；
验活缓存过期后**要等下一次搜索**才会重验 —— 冷门关键词可能几个月没人搜，链接就几个月没人复核。

```bash
./scripts/install-schedule.sh          # 装成 launchd 定时任务（每 6 小时一轮，可先 print 预览）
./scripts/install-schedule.sh status   # 看是否在跑 + 最近一次报告
./scripts/install-schedule.sh print    # 只打印 plist 不安装（plutil -lint 可直接校验）
./scripts/install-schedule.sh uninstall
```

一轮维护实测（2026-09-21，50 秒）：

| 步骤 | 结果 |
|---|---|
| 加深 TG 索引 | 398 频道 × 3 页 → **新增 8269 条消息**（37.8s）|
| 复验过期链接 | 候选 400 → **存活 366 / 失效 11**（23.8s）|
| 缓存状态 | 过期条目 9348 → 8971（每轮把最久没验的 400 条推平）|

设计上的几个关键点（都在 `src/pansearch/maintain.py` 里）：
- **运行锁**：两个定时任务重叠时后到的直接跳过；锁超过 3 小时视为陈旧可接管 ——
  否则一次 `kill -9` 就能让定时任务永久停摆。
- **最小间隔**：距上次维护不足 N 小时直接跳过（launchd 用 `StartInterval` + `--min-interval` 双保险）。
- **死链不重复浪费预算**：刚判死的链接跳过，超过 30 天才再验一次（覆盖"重新上传"的小概率）。
- **队列按"最久没验"排序**，而不是随机：积压越多越能保证每条都被轮到。

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

> ⚠️ **2026-09 起百度锁死了匿名内容访问**（实测）：
> `share/verify` 对**所有**链接恒返回 `-62`（连已验证存活的也一样），
> `share/wxlist` 也要求先 `verify`（`errno 9019 need verify`），
> 所以现在**拿不到提取码校验结论，也读不到分享里的真实文件名**。
>
> 应对：
> 1. 遇到没见过的码自动**回退到 `shorturlinfo`**，至少判定"链接还在不在"，
>    而不是把百度结果一律标成"未知"；
> 2. **"百度优先"只给能确认可用的链接**（`Resource.usable`）——
>    否则一堆"确定不了"的百度链接光靠域名就能压过已验证可用的夸克链接；
> 3. 排序里"确定性"单独分层：`确认存活` → `存活但没能确认可用` → `确定不了`。
>
> 实测「漂流少年」：修复前前排是 5 条不对版的百度结果、真资源在夸克却排在后面；
> 修复后前 10 全是已验证可用的夸克/阿里，百度降到第 11 位并标注
> 「链接存活，但提取码未经验证（share/verify 返回 -62）」。

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

- 内置 **398 个频道**：136 个来自 PanSou 官方配置（影视为主）+ **253 个从
  [tgnav.org](https://www.tgnav.org) 按分类采收并自动验活**（书报刊漫 / 知识学习 /
  软件综合 / 资源分享 / 壁纸图片 / ios资源 / 博客杂谈）+ 9 个从
  [itgoyo/TelegramGroup](https://github.com/itgoyo/TelegramGroup) 采收
- 用 `?before=<msg_id>` 向历史无限翻页；实测 398 频道 × 80 页 = **251014 条消息 / 0 失败**
- 索引是**只增不减的资产**：挖得越深同一个关键词召回越多，而检索是毫秒级
- 当前索引规模：**627411 条消息（270482 条含网盘链接）**

频道采收已经**命令化**，随时可以再扩：

```bash
pansearch channels harvest --source tgnav    # 从导航站分类采收 + 价值校验
pansearch channels harvest --source file -f 候选.txt
pansearch channels stats
```

价值校验的判据和站点探测器一致：**最近一页消息必须真含网盘/磁力链接**。
候选里绝大多数是机器人、交易所、新闻、表情包频道，不校验就全爬一遍纯属浪费。
（实测 itgoyo/TelegramGroup 的 1559 个候选只收下 9 个 —— 0.6%；
tgnav 的分类页命中率高得多，348 收 253。）

索引加深带来的跨领域覆盖变化（同一批关键词，38.7 万 → 62.7 万条时）：

| 垂直 | 关键词 | 加深前 | 加深后 | 增长 |
|---|---|---|---|---|
| 电子书 | epub | 1986 | **5197** | +162% |
| 电子书 | mobi | 1263 | **3307** | +162% |
| 电子书 | azw3 | 873 | **2437** | +179% |
| 游戏 | 汉化 | 496 | **1395** | +181% |
| 学术 | 论文 | 780 | **1415** | +81% |
| 课程 | 网课 | 116 | **213** | +84% |
| 音乐 | flac | 5059 | **6676** | +32% |

| 关键词 | 索引 6.5k 条 | 索引 212k 条 |
|---|---|---|
| 沙丘 4K HDR | 265 条 | **1469 条** |
| 三体（叠加自建 PanSou 后） | 54 条 | **425 条** |

> ⚠️ TG 频道天然**偏向近期**：广播性质决定它是"新片发布流"，经典老片很少重发。
> 所以**老资源主要靠 PanSou 的插件层**（它索引的是各个网盘搜索站），
> 这也是自建 PanSou 尤其重要的原因。

### 7️⃣ 影视剧之外还有别的垂直领域（音乐制作插件 / 软件）

实测教训：**「Serum」「Omnisphere」「大气合成器」在 21 万条 TG 索引里是 0 条**，
PanSou 的 65 个插件也几乎不覆盖 —— 因为我们的频道全是影视剧方向，
而 VST 音源/桌面软件是另一个垂直领域。

关键差异：**影视剧把网盘链接直接贴在 TG 里，VST 不是**。
它们发布在专业资源站上（AudioZ / LoopTorrent / 4DST / VSTorrent…），
而且下载入口在**详情页**里（搜索页只有标题链接），所以必须两阶段跟进。

新增 `sitesearch` 适配器，与 B 环同思路但候选页来自配置好的垂直站点：

| 关键词 | 修复前 | 修复后 |
|---|---|---|
| Serum | 22 条（多为无关） | **35 条**（含 21 磁力） |
| Omnisphere | 12 条 | **27 条**（25 磁力） |
| Kontakt | — | **27 条**（25 磁力） |
| 大气合成器 | **0 条** | **27 条**（走别名） |

配套修了一个抽取缺陷：**`URL_RE` 只匹配 http(s)，磁力链接从来没被抽取过**
（之前的磁力全部来自 PanSou 的 JSON）。VST 音源基本都走磁力，不修这条等于白做。

### 8️⃣ 从"几个垂直领域"扩到"所有领域"：目录 + 路由 + 自动探测

要覆盖所有领域，站点清单不可能硬编码在源码里（早期就是 7 个写死的）。
现在改成**数据驱动 + 自动探测**：

```
config/sites.yaml          资源站目录（域名 / 垂直领域 / 搜索模板 / 详情页正则）
config/sites_candidates.txt 候选域名清单（待探测）
pansearch sites probe <域名> 自动探测可用的搜索 URL 模板与详情页正则
pansearch sites route <查询> 预览一次查询会走哪些垂直领域和站点
pansearch sites health      站点健康度（连续失败的自动跳过）
```

**站点怎么加**：`pansearch sites probe www.example.com -v ebook` —— 探测器会试
21 种常见搜索 URL 模板（WordPress `/?s=`、DSE `index.php?do=search`、
Discuz、DedeCMS、帝国 CMS…），并**自动推导详情页正则**，直接写进目录。

探测判据是**对照法**：同一个模板用真词搜一次、用无意义串搜一次，
真词的结果条目要明显多于噪声。只看"页面里有没有关键词"会误判（很多站不回显关键词）。

**垂直路由**：一次搜索不可能打几百个站（分钟级）。所以先识别查询领域：

| 查询 | 识别结果 |
|---|---|
| 沙丘 4K HDR | `movie` |
| Serum 合成器 / 大气合成器 | `audio-tool` |
| 三体 电子书 | `ebook` |
| Photoshop 破解 | `software` |
| 论文 sci-hub | `academic` |

只打相关领域的站 + 通用站；认不出领域就只打通用站（不乱打）。
**健康度**则负责自动淘汰：连续失败 6 次的站会被跳过，不用手工维护清单。

### 8️⃣ 中文俗称搜不到英文资源

中文用户搜「大气合成器」「血清」「康泰克」，资源站里只有 Omnisphere / Serum / Kontakt。
`config/aliases.yaml` 做等价替换，**不降权**（它不是"放宽"，是同一个东西），
并且命中结果按**替换后的词**打分 —— 否则会自相矛盾：用别名取回一堆
"Omnisphere" 结果，再用「大气合成器」算相关性，主题词一个都不出现，全判 0.1 分。

> 只收录有把握的别名。错的别名会把不相关结果拉进来，比没有更糟。

### 7️⃣ 速度：给每个源自己的截止时间

各源耗时差了两个数量级，等最慢的就等于把搜索时长交给它：

| 数据源 | 耗时 | 单次产出 |
|---|---|---|
| **自建 PanSou（热缓存）** | **0.17s** | 370+ 条 |
| Telegram 本地索引 | 0.02s | 500~600 条 |
| 自建 PanSou（冷启动） | ~4.2s | 370~1500 条 |
| B 站内搜（视频 + 评论区） | 6.9s | 16~17 条，其中百度 14 条 |
| Bing / DDG / 搜狗（B 环） | 1~2s | 3~17 条，含贴吧 / 公众号内容页 |

**四个具体的坑**（都是实测踩出来的）：

1. **截止时间不能给太宽**。主查询与补搜查询是**并行**的，各自都会吃满上限 ——
   PanSou 设 45s 时，一次卡顿让整体变成 `45s(等待) + 验活 ≈ 52s`。收到 20s 后
   最坏情况降到 8.5s。
2. **PanSou 的 `ASYNC_RESPONSE_TIMEOUT` 决定"冷查询等多久"**。异步模式下超时就
   先返回已拿到的部分结果、后台继续跑并写缓存，所以调小是纯赚：8s → 4s，
   首次查询耗时减半而命中数几乎不变。
3. **验活是冷搜索的瓶颈**。255 次真实请求 ≈ 20 秒（夸克限速 10 qps 就占 18 秒）。
   所以默认只验活最相关的前 150 条，而不是全量。
4. **"补搜词"必须有区分度**。「沙丘 2」曾拆出补搜词 `"2"`，而索引里 18.4 万条消息
   含 `"2"`（占 87%）—— 等于拿无意义的词全库扫描。现在纯数字/单字符不参与补搜。

| 场景 | 优化前 | 优化后 |
|---|---|---|
| `沙丘 2`（异常卡顿） | 52s | **8.5s** |
| 全新关键词（冷启动） | 23~28s | **12~14s** |
| 复搜（走缓存） | 10~13s | **3~5s** |

---

## 架构

```
        ┌──────────────── 输入：关键词 ────────────────┐
        ▼
 A 环  PanSou 聚合引擎     107 个网盘搜索插件 + 110 TG 频道   ← 覆盖广，但会限流
 B 环  搜索引擎定向检索     6 个引擎 → 内容页 → 抓页面         ← 含自建 SearXNG（能到 Google）
 C 环  Telegram 频道索引     430 频道直连 t.me + 本地 SQLite     ← **召回主力，0.02s**
 C 环  B 站内搜（WBI 签名）  视频简介 + 评论区 + 专栏摘要        ← 资源贴真实产地
 C 环  BT / 磁力站           nyaa + dmhy（搜索页直出 magnet）    ← 磁力链路的直接产地
 D 环  公开 API 数据源       11 个（学术 / 电子书 / 漫画 / 公版书）
 E 环  垂直资源站目录        20 个站（探测验证过"详情页真有链接"才收录）
 D 环  本地私有索引         SQLite 验活缓存 + TG 消息沉淀      ← 越用越全
        │
        ▼
 抽取 → 归一化 → 去重 → 验活 → 排序 → 输出(CLI / Web / JSON / CSV)
```

### 数据源清单（2026-09-22，全部实测过产出）

| 适配器 | 可检索后端 | 数量 | 怎么来的（每一条都有实测依据） |
|---|---|---|---|
| `pansou` | 网盘聚合插件 | **108** | 官方 latest 镜像只带 74 个；`scripts/build-pansou.sh` 从源码重建并补齐上游 `main.go` **从没接线过的 34 个插件**（含 `javdb`） |
| `sitesearch` | 垂直资源站 | **61** | 候选池共 3721 个（SearXNG/直连引擎反查 415 + 从 62 万条 TG 消息的链接里反查 2393 + 已知站清单 224），按"详情页真有分享链接"的判据探测，通过率 1.7~4.5%。**并行分片会让 probe-all 互相覆盖目录**（实测丢了 12 个），已改为从日志顺序合并 |
| `apisources` | 公开 API | **20** | 学术/电子书/数据集/影视/动画：arxiv、crossref、openalex、europepmc、plos、doaj、osf、zenodo、datacite、figshare、archive.org、openlibrary、gutendex、wikisource、kitsu、tvmaze、yts、mangadex。为此给 API 层加了 **POST body、顶层数组、HTML link_re、per-API headers** 四种能力 |
| `websearch` | 搜索引擎 | **8** | bing / baidu / ddg / sogou / so360 / naver / toutiao + **自建 SearXNG**（一个后端换十几个引擎，含直连被 429/403 的 google/brave/qwant） |
| `btsearch` | BT / 磁力站 | **4** | nyaa（75 magnet）、dmhy（46）、mikan（41）、sukebei（75）；搜索页直出 magnet，标题取 `dn=` 或行内文本 |
| `bilibili` | B 站三路 | **3** | WBI 签名 + 游客 cookie → 视频简介 / 评论区（含置顶与楼中楼）/ 专栏摘要 |
| `telegram` | TG 频道 | **521** | 从 62 万条消息里反查 `t.me/xxx` 提及 → 2076 个候选频道 → 按"最近一页真有 ≥1 条分享链接"校验 → 收下 94 个（通过率 4.5%）。索引 633k 消息 / 268k 含链接 |

**自己写的后端（不含 PanSou 插件与 TG 频道）：87 → 96 个**；含 TG 频道合计 517 → 617 个。

> ⚠️ 诚实说明：**"源数量再翻 10 倍"做不到且不该硬凑**。各池子的实际情况：
> PanSou 插件 108/109 已是硬上限；可用搜索引擎实测只剩 8 个（其余 403/429/空页）；
> 公开 API 有产出的基本找完（试了 50 个，收 20 个）；候选域名的**验证通过率只有 1.7~4.5%**，
> 要拿 800 个新后端就需要 2 万个新候选域名 —— 而唯一能提供这种规模的 TG 频道目录站
> （telegramchannels.me / telemetr.io / tgstat.ru）全部 403，需要无头浏览器才能抓。
> **真正能翻 10 倍的是"索引深度"**：`index crawl --deepen` 每轮把每个频道的抓取位置
> 往历史推 30 页，反复跑就能把索引从 63 万条推到数百万条（Tavily 的召回主要来自这一层）。

#### 成人内容源：默认开启，可一键过滤

`javdb`（PanSou 插件）与 `sukebei`（nyaa 成人分区）**默认启用**，
过滤交给 `--sfw`，而不是在构建阶段把插件删掉：

```bash
pansearch search "SSIS-001"          # 默认包含成人源（javdb 一路实测 154 条）
pansearch search "SSIS-001" --sfw    # 按来源剔除成人源命中（会显示"已过滤 N 条"）
```

标记写在 `config/sources.yaml` 的 `adult_sources`（`plugin:javdb` / `bt:sukebei`）。
**按来源标签过滤而不是按标题关键词猜**：靠标题猜既会漏（缩写、外语），
也会误伤正常资源（"写真集""深夜剧"这类）。

**不收的源（同样实测过，避免滥竽充数）**：youtube / csdn / 贴吧 / 豆瓣 / 简书 / 博客园 /
微博 / 微信公众号（0 条链接或反爬）、1337x / btdig / bt4g / torrentz / acgnx / tokyotosho
（403/429/空页）、semantic scholar / core / opensubtitles / tmdb / omdb（需 key 或 429）、
jikan（504）、hathitrust（403）、standardebooks 与 gutenberg 的 HTML 搜索页（JS 渲染、
首屏抽不到链接）、以及 300+ 个探测后确认"详情页没有分享链接"的候选站。
### 目录

```
pan-sousuo/
├── scripts/
│   ├── build-pansou.sh       # 从源码自建 PanSou（补齐上游没接线的 35 个插件）
│   ├── start-pansou.sh       # 一键自建 PanSou（colima + Docker）
│   └── start-searxng.sh      # 自建 SearXNG（一个后端换十几个引擎）
├── config/
│   ├── sources.yaml          # 数据源开关 / 权重 / 限速 / 各源 deadline
│   ├── tg_channels.txt       # 389 个 TG 网盘分享频道（跨领域）
│   ├── aliases.yaml          # 中文俗称 → 实际检索词（大气合成器→Omnisphere）
│   ├── sites.yaml            # 资源站目录（跨领域，含垂直标签）
│   ├── sites_candidates.txt  # 待探测的候选域名
│   ├── baidu_errno.yaml      # 百度 errno 码表（实测校准）
│   └── pan_errno.yaml        # 夸克/阿里/115/天翼 码表（实测校准）
├── src/pansearch/
│   ├── models.py             # RawHit / Resource / VerifyResult / 状态枚举
│   ├── adapters/
│   │   ├── base.py           # 适配器基类 + 注册表（新增源 = 加一个文件）
│   │   ├── pansou.py         # A 环：聚合引擎
│   │   ├── bilibili.py       # C 环：B 站内搜（WBI 签名 + 简介 + 评论区）
│   │   ├── telegram.py       # C 环：TG 频道（查本地索引）
│   │   ├── sitesearch.py     # 垂直资源站（VST/软件）：搜索→跟进详情页→抽链接
│   │   └── websearch.py      # B 环：搜索引擎两阶段
│   ├── tgindex.py            # TG 频道抓取 + SQLite 索引（召回主力）
│   ├── extract.py            # 链接 / 提取码抽取（两遍扫描，全局贪心配对）
│   ├── normalize.py          # URL & 提取码归一 + 网盘类型识别
│   ├── dedupe.py             # 分享指纹去重合并
│   ├── verify.py             # 百度验活（share/verify + shorturlinfo）
│   ├── verifiers.py          # 验活调度池（夸克/阿里/115/天翼）+ 失效剔除 prune()
│   ├── sitecatalog.py        # 资源站目录 + 搜索模板探测 + 健康度
│   ├── routing.py            # 查询垂直领域识别（决定打哪些站）
│   ├── score.py              # 排序打分（相关性幂次闸门 + 全乘法加成）
│   ├── store.py              # SQLite 验活缓存 / 搜索日志
│   ├── pipeline.py           # 并发编排（单源超时隔离 + 验活预算 + 放宽查询）
│   ├── cli.py                # CLI
│   ├── webapp.py             # Web API
│   └── web/index.html        # 单页 UI（零构建）
└── tests/                    # 447 个离线测试
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
| **搜索引擎大多不可用** | 实测 2026-09-21：brave 429 / ecosia 403 / so.com 只有反爬壳 / yandex·mojeek·searx 零结果；可用的只剩 Bing、DuckDuckGo、搜狗。长期方案：自建 SearXNG（支持 JSON API，可聚合多引擎） |
| **TG 索引需要时间养，且偏近期** | 212k 条消息里 2026 年占 68%。频道是"新片发布流"，经典老片很少重发 → 老资源主要靠 PanSou 插件层。建议定期 `index crawl --deepen`（可挂 cron），索引在 `.cache/` 不进 git |
| **36 个频道抓不到内容** | 136 个里 14 个是搜索机器人频道或已失效/改名；实测有效频道 122 个 |
| **迅雷 / UC / 123 / PikPak / 磁力 无法验活** | 迅雷要 captcha、UC 与 123 的接口已变更、磁力需 DHT。这些显示为「未校验(该网盘不支持)」，**≠ 有效**，可用 `--strict` 剔除 |
| ~~B 站未接入~~ | **已接入**（`adapters/bilibili.py`）：WBI 签名 + 游客 buvid cookie，搜视频/专栏 → 跟进简介与评论区（含置顶与楼中楼）。实测「AE模板」百度链 25 → 38 条、「Omnisphere」10 → 14 条，单源耗时 6.9s / 27 请求 |
| **贴吧直连 403** | 百度安全验证，直连页面拿不到；改走搜索引擎的 `site:tieba.baidu.com` 模板 + 阶段 2 抓内容页 |
| **B 站专栏正文拿不到** | `read/cv*` 只回 3.3KB 壳、`x/article/view` 已 -509，需要无头浏览器。当前只用搜索接口返回的摘要（仍能抽到链接，见「AE模板」的 2 条夸克链）|
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
.venv/bin/python -m pytest -q        # 280 个测试，全离线，十几秒跑完
```

测试锁住了几个关键结论（token 形式、errno 码表、提取码不串味、缓存键带提取码、状态权重是乘法），改动这些逻辑时会立刻报警。
