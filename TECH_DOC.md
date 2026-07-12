# AI看盘 技术文档

> 目的：让新会话/新开发者快速理解全部代码结构、数据流与关键逻辑，直接接着开发。
> 更新时间：2026-07（对应分支 `feature/glm-extraction`）。
> 最近一轮开发详见 **第 12 节「本轮开发（产业链缝合 + 复盘可视化 + 盯盘增强）」**。

---

## 1. 项目概述

一个 A 股「涨停复盘」数据平台，三大功能：

1. **数据同步**：从淘股吧博客下载「湖南人涨停复盘」图片 → GLM-OCR + 视觉识别提取成 Excel → 写入 PostgreSQL。
2. **热门股复盘追踪**：多 tab 复盘页 —— ①湖南人涨停复盘追踪（板块/个股逐日跟踪+移除）②同花顺概念追踪 ③每日热力·在炒啥（产业链地图，每日谁在涨谁在杀）④板块梯队·龙头健康（情绪梯队 / 趋势主升双口径）⑤历史爆发扫描。
3. **盯盘**：已开发 —— 指数日K/分时、领先指标、创业板/科创板个股榜、自选股、概念涨停统计、量比/分时量比/开盘涨幅筛选、**产业链盯盘**（按上下游实时聚合成员涨幅/涨停）。
4. **产业链缝合**：把同花顺 390+ 概念按上下游关系缝合成 18 条产业链（`ths_concept_chains.json` + `concept_chain.py`），供「每日热力」「产业链盯盘」「板块梯队」共同复用。

主入口：`api_server.py`（Flask，端口 5000）。首页 `/` 是左侧导航（数据同步 / 热门股复盘追踪 / 盯盘）。

---

## 2. 运行方式

```bash
python api_server.py          # 主服务, http://127.0.0.1:5000
# 或双击 start_server.bat
```

- 依赖见 `requirements.txt`；`bootstrap.py` 会在部分脚本启动时自动补装缺失依赖。
- Windows + PowerShell 环境。命令分隔用 `;`（不是 `&&`）。
- 关键依赖：flask, requests, beautifulsoup4, PyYAML, openpyxl, Pillow, numpy, **psycopg2-binary**（PostgreSQL）, **mootdx**（通达信行情 + 交易日历）, rapidocr_onnxruntime（旧本地 OCR，现主要用 GLM-OCR）。

### 外部依赖服务
- **智谱 GLM**：OCR(`glm-ocr`) + 视觉模型(`GLM-4.5V`)，key 在 `config.yml` 的 `ai.zhipu.api_key`。
- **PostgreSQL**：跑在 Docker 容器 `some-postgres`，映射 `localhost:15432`，账号 `postgres/mysecretpassword`，库 `postgres`。**需先启动 Docker Desktop 且容器运行**，否则入库/追踪不可用。
- **通达信 mootdx**：TCP 直连行情服务器（`Quotes.factory(market='std')`），取日线。

---

## 3. 配置 `config.yml`（被 .gitignore，示例见 `config.yml.example`）

```yaml
ai:
  zhipu:
    api_key: "..."
    text_model: "GLM-4-Flash-250414"
    vision_model: "GLM-4.5V"
    base_url: "https://open.bigmodel.cn/api/paas/v4/"
ocr: { ... }              # 提取关键字/板块配置
database:                 # PostgreSQL(本次新增)
  host: "localhost"
  port: 15432
  user: "postgres"
  password: "postgres"
  dbname: "postgres"
```

---

## 4. 目录/文件说明

| 文件 | 作用 |
|------|------|
| `api_server.py` | **主 Flask 服务**：导航页、下载、提取、入库、Excel/DB 状态、热门股计算等全部接口。 |
| `db.py` | **PostgreSQL 模块**：连接/建表/解析Excel/入库/查询。 |
| `hot_track.py` | **热门股追踪核心算法**：DB 数据源、入榜规则、mootdx 行情、逐日跟踪、移除规则。 |
| `extract_glm.py` | **涨停复盘提取**：GLM-OCR 找 03/04 图、红色板块标题识别（自适应阈值）、导出 Excel。 |
| `concept_chain.py` | **产业链映射工具**：读 `ths_concept_chains.json`，提供 `concept_to_chain()`/`chain_info()`/`is_blocked()`/`meaningful_concepts()`；被每日热力/产业链盯盘/板块梯队复用。 |
| `ths_concept_chains.json` | **产业链缝合配置**：`industrial_chains`(18链，含 tier_order 上下游分层) + `company_chains`(公司链) + `themes_no_chain`(资金/风格/区域/政策等噪音黑名单)。 |
| `ths_sync.py` | 同花顺概念同步：adata 拉概念+成分股 → CSV → PostgreSQL `ths.concept_member`。 |
| `tdx_source.py` | **通达信 tqcenter/tq 行情源**：批量报价、K线、`is_available()` 探测（True 永久缓存 / False 只缓存 30s 后重探）。 |
| `download_by_id.py` | CLI 下载器（按图片ID命名 + 写 `_order.txt`），网页下载逻辑与其对齐。 |
| `bootstrap.py` | 依赖自动检测安装。 |
| `utils.py` | 旧的本地 RapidOCR 工具（`ocr_image` 等），现基本被 GLM-OCR 取代。 |
| `extract_api.py` | 旧的提取 API（独立），**当前主流程不用**，保留参考。 |
| `hot_track_api.py` | 旧的独立热门股 API（端口5001），**当前主流程用 api_server**。 |
| `templates/nav.html` | 左侧导航首页（iframe 加载子页）。 |
| `templates/update.html` | 数据同步页（下载/提取/入库/状态三模块）。 |
| `templates/hot_track.html` | 热门股追踪页（计算执行框、表格、图表、面板拖动）。 |
| `.price_cache.json` | 行情缓存（涨跌幅/20日/60日/MA10/是否破位），gitignore。 |
| `.hot_last_result.json` | 最近一次热门股计算结果的服务端缓存（刷新/重启恢复），gitignore。 |
| `.member_closes.json` | **概念成员日收盘缓存**（供「每日热力」当日涨/杀统计），`{code:{YYYYMMDD:close}}`，约 5000+ 只，由 `/api/hot/daymap/build` 构建并持久化，gitignore。 |
| `.concept_day_cache.json` | 同花顺概念追踪的**按天结果缓存**（已算过的交易日不重算，force 可强刷），gitignore。 |
| `.watchlist.json` | 盯盘自选股列表，gitignore。 |
| `.exp_picks.json` / `.exp_scan.json` | 历史爆发扫描的已选/上次扫描结果，gitignore。 |
| `config.yml` | 配置（含密钥/DB），gitignore。 |

### 数据目录
- `dataresource/<YYYYMMDD>/`：每日下载的图片，按图片ID命名，含 `_order.txt`（图片顺序清单，用于完整性核对与找 03/04）。gitignore。
- `excelDataSource/<YYYYMMDD>_涨停复盘_{verified|manualcheck}.xlsx`：提取产出。

---

## 5. 数据流总览

```
淘股吧博客文章 --下载--> dataresource/<日期>/*.png (+_order.txt)
     |                          |
     |                     extract_glm 提取(GLM-OCR+视觉)
     v                          v
                excelDataSource/<日期>_涨停复盘_{verified|manualcheck}.xlsx
                               |
                          db.submit_date 入库(仅verified)
                               v
              PostgreSQL: zt_daily(涨跌家数) + zt_stocks(板块个股)
                               |
                     hot_track.track_hot_stocks 读DB
                               |  + 通达信mootdx行情
                               v
                 热门股追踪结果(板块/个股逐日跟踪/移除)
```

---

## 6. PostgreSQL 表结构（`db.py` 建表）

```sql
zt_daily (
  trade_date   DATE PRIMARY KEY,   -- 一天一行, 兼作"已入库"标记
  up_count     INTEGER,            -- 上涨家数(03提取)
  down_count   INTEGER,            -- 下跌家数
  total_amount NUMERIC,            -- 总成交额(亿)
  status       TEXT,               -- verified / manualcheck
  updated_at   TIMESTAMP
)
zt_stocks (
  id SERIAL PK, trade_date DATE,
  block, code, name, last_time, lianban, reason   -- 04提取每只票
)  -- 索引 idx_zt_stocks_date
```

- 入库覆盖式：`submit_date` 先删该日期旧数据再插入。
- **只允许 verified 入库**：`submit_date` 遇到 manualcheck 抛 `NotVerifiedError`。
- `db.get_submitted_dates()` / `get_submitted_status()`：查已入库日期及其状态。

---

## 7. 关键流程与算法

### 7.1 下载（api_server.py）
- `get_article_list('444409')`：抓博客文章列表（标题/链接/pub_time/date_folder）。
- **只下载 A 股交易日**：`is_trading_day()`（akshare `tool_trade_date_hist_sina()` 交易日历，内存缓存；不可用或超范围回退到工作日）；`filter_trading_articles()` 过滤。
- **命名与完整性**：图片按 `<图片ID>.png` 命名并写 `_order.txt`（与 CLI 一致）。`/api/files` 用 `_order.txt` 判定 complete/incomplete/unknown。
- 下载失败按日期记 `failed_details`；前端可「再次下载」（强制不跳过、清理旧命名多余文件）。

### 7.2 提取（extract_glm.py）
- 找图：**直接取 `_order.txt` 第3/4张**（不轮询）。
  - **03 判定**：OCR 后开头200字含 `880005` 或 `涨跌家数` → 提取涨跌家数/成交额。
  - **04 判定**：`extract_sectors_by_red` 识别的板块标题含「市场连板股」→ 提取板块个股。
- 04 板块归属：`_find_red_bands` 红色检测定位板块标题 y 坐标 → 裁小图用视觉模型识别板块名+数量 → OCR 抽所有股票(token流按6位代码重组, 兼容表格结构退化) → 按声明数量顺序切分, 「涨停炸板」后丢弃。
- 缓存：`<img>.glmocr.json`(OCR)、`<img>.sectors.json`(板块标题, 仅0失败才写)。
- 导出：03/04 命中其一即生成 Excel，缺失方在表内写明原因；都无返回失败。全部通过=verified，否则=manualcheck。

### 7.3 热门股追踪（hot_track.py `track_hot_stocks`）
签名：`track_hot_stocks(start, end, sort, with_price=True, progress=None, source='db', apply_removal=True)`

**入榜规则**：
- 排除板块「市场连板股」。
- **板块入选**(`block_qualifies`，首次满足即保留)：板块内任一票 连板≥2 / `X/Y天Z板` / 9:25一字板。
- **个股入选**(`stock_qualifies`)：`X/Y天Z板` / 9:25 / 300或688开头且≥1板 / 连板≥2。

**行情(通达信 mootdx)**：
- `fetch_range_mootdx(code,...)`：`client.bars(symbol, frequency=9, offset=N)` 日线 → 算当日涨跌幅、20/60日涨幅、MA10、是否破MA10，写 `.price_cache.json`。
- `prefetch_prices(..., check_dates=dates)`：**按实际交易日逐日核对**，任一交易日缺 pct 或 MA10 就重取（修复了旧"有任意缓存就跳过"导致个别日期补不上的 bug）。
- ⚠️ 已知：极个别标的服务器无数据（如 `003133 中量科技`），mootdx 返回 0 行，无法补齐。

**移除规则（当前版本，重点）**：
- 跌破10日线当日 → **预警**（`warn_date`，不移除）。
- 自预警日起（预警日记为第1日），到**第三个交易日**收盘仍 < MA10 → **移除**（`remove_date`，`remove_reason='跌破10日线第三日未收回'`）。
- 期间收回10日线或涨停 → 解除预警（`warn_date=None`）。涨停(普通≥9.8/科创创业≥19.4/一字板)不算破位。
- 移除股：移除当日仍显示(标记)，次日消失。
- **板块移除**：某板块当天无活跃个股(`active_count==0`)则当天不显示；全部个股移除的次日板块消失；有新票入选会重现。

**分阶段计算(异步, 见 7.4)**：build 建板块/个股 → price 取行情 → 若缺数据**暂停询问**(不擅自移除) → remove 移除规则 → 结果 + 缺失报告。

前端展示：**预警**股保留涨跌幅颜色，仅叠加黄色边框+「预警」标签（不再整块涂黄，避免盖掉涨跌色）；**移除**股灰色标「跌破10日线第三日未收回」。

**返回结构**：`{start,end,sort,dates,by_date[{date,blocks[{block,cum_count,active_count,new_count,removed_today,times_count,total_count,active,removing,stocks[{code,name,is_kcb_cyb,first_date,warn_date,remove_date,track:{date:{desc,pct,pct_20d,pct_60d,ma10,below_ma10,present}}}]}]}], blocks_summary[...], date_summary{date:{up_count,down_count,total_amount,high_stocks[{code,name,lianban,block}],broken_stocks[{code,name,block,pct}]}}, missing_report, missing_codes}`

**左侧汇总列 `date_summary`（板块统计每日行左侧两列的数据源）**：
- 列1 涨跌数：`up_count/down_count/total_amount`（来自 `zt_daily`）；前端在 涨>3000&跌<2000 或 跌>3000&涨<2000 时整列标红。
- 列2 高位股：`high_stocks`=当日 3板及以上（`parse_lianban.current>=3`，概念优先取非「市场连板股」的板块）；`broken_stocks`=昨日3板+、今日未涨停(断板)的个股，带今日涨跌幅(前端按涨跌色)。
- ⚠️ 3板+高位股代码已一并加入行情预取集合，保证断板股涨跌幅可取。

### 7.4 热门股异步计算任务（api_server.py `hot_compute_task`）
- 状态机：`pending → running → (awaiting 缺数据暂停) → running → completed/failed`。
- 用 `threading.Event` 阻塞等待用户在 `awaiting` 时的选择：
  - `resync`：强制 mootdx 重取缺失代码后重新检查。
  - `skip`：忽略缺失，执行移除并出结果。
- `apply_removal=False` 先算(不移除)以得到缺失报告；确认后 `apply_removal=True` 出最终结果。
- `_build_missing_report(result)`：按 每日→板块 列出当天在榜但缺 pct/MA10 的个股。

---

## 8. API 接口清单（api_server.py，端口5000）

### 页面
- `GET /` 导航首页(nav.html) · `GET /update` 数据同步页 · `GET /hot` 热门股追踪页

### 下载
- `GET /api/articles` 文章列表
- `POST /api/download` 异步下载(start_date/end_date/skip_existing) · `POST /api/download/sync` 同步
- `GET /api/status/<task_id>` 下载进度(含 failed_details)
- `GET /api/files` 已下载文件(含完整性 complete/incomplete/unknown) · `GET /api/files/<date>`

### 提取
- `POST /api/extract` 按区间提取(+可选入库, submit_to_db)，只处理交易日
- `GET /api/extract/status/<task_id>` 逐日实时状态(items: extracting/submitting/submitted/failed/need_review)

### 入库/数据库
- `GET /api/db/status` 连接探测
- `POST /api/db/submit` 单日入库(仅verified，manualcheck 返回409 need_review)
- `POST /api/db/submit-batch` 按交易日区间批量入库 · `GET /api/db/submit-batch/status/<task_id>`
- `GET /api/excel/list` 按交易日列出应有Excel + 入库状态(verified/manualcheck/缺失, submitted, db_status)

### 热门股追踪
- `GET /api/hot/dates` 可选日期=数据库已入库日期(约束前端范围)
- `GET /api/hot/last` 最近一次计算结果(服务端持久化, 刷新恢复用; has=false 表示无)
- `POST /api/hot/compute` 启动分阶段异步计算(start/end/price；范围须在已入库区间内)
- `GET /api/hot/compute/status/<task_id>?since=N` 进度/日志(awaiting返回缺失报告, completed返回result)
- `POST /api/hot/compute/resolve` {task_id, action: resync|skip}
- `POST /api/hot/resync` 强制重取指定codes行情
- 旧接口(仍在)：`/api/hot/track`(同步, 读Excel), `/api/hot/sync`, `/api/hot/sync-ma10`, `/api/hot/cache/clear`, `/api/hot/refetch`

### OCR(旧)
- `GET /api/ocr/title` · `POST /api/ocr/recognize`

---

## 9. 前端页面

### `update.html`（数据同步，iframe 内）
三个可折叠模块：
1. **下载**：日期区间/只下载今日/跳过已存在；进度；失败列表(原因+再次下载)；已下载数据表(完整性 + 重试)。
2. **数据提取**：区间/仅今日；「提取并提交数据库」；逐日实时状态。
3. **Excel 与入库状态**：DB 连接指示灯；按交易日列出 Excel(verified/manualcheck/缺失)与入库状态；单日「上传/重新上传」；批量上传；缺Excel给「提取并上传」；批量入库逐日状态(含 Excel不存在)。

### `hot_track.html`（热门股追踪）
- 顶部：起止日期**下拉**(只列已入库日期，超范围选不了)、排序切换、「计算」「仅计算今日」(要求当日已入库否则报错)。
- **计算执行框(modal)**：分阶段进度条+分色日志；缺数据时暂停显示报告+「再次同步/跳过」；关闭按钮常显；render 有 try/catch 容错。
- **结果恢复**：刷新时**优先从服务端** `/api/hot/last` 恢复(`restoreServerResult`，不受浏览器配额限制)，失败再退回 localStorage(`restoreLastResult`)。大范围结果几MB，localStorage 会超配额，所以服务端为主。
- 表格：日期行左侧**两列**(列1 涨跌数极端标红；列2 3板+高位股/昨断板，见 `renderRow` 的 col1/col2) × 板块卡片(全局排序)，个股缩略块；**预警**=保留涨跌色+黄色边框；**已移除**=灰色"跌破10日线第三日未收回"；新入选有标记。
- 底部板块图表；上下面板间**可拖动分隔条** + 两面板**可收缩/展开**(高度与折叠状态记忆到 localStorage)。

---

## 10. 重要注意事项 / 已知问题 / TODO

**注意事项**
- PowerShell：命令用 `;` 分隔；`count(*)`、`&`、`*` 等在内联 `python -c` 里易被解析坏，复杂脚本写临时 `.py` 文件跑完删。
- 提交约定：只提交**代码/模板/配置示例**，`dataresource/`、`excelDataSource/*.xlsx`、`*.log`、`config.yml`、缓存 json 不入库。
- git remote URL 里内嵌了 PAT（有泄露风险，建议后续改用凭据管理器/SSH）。当前分支 `feature/glm-extraction`，远程 `eziojianzheng/richproject`。
- 服务默认 `debug=on` 且绑定 `0.0.0.0`，仅本机测试用，勿上公网。

**已知问题**
- `003133 中量科技` 等极个别标的 mootdx 无数据 → 缺失报告会列出，只能「跳过」。
- 热门股底部**板块图表仍展示全部入选过的板块**，与上方"只显示活跃板块"不完全对齐（待定：图表是否也只显示活跃板块）。
- `hot_tasks/extract_tasks/submit_tasks/download_tasks` 都是**内存字典**，服务(debug)重载或重启会丢**进行中的任务**；但热门股**最终计算结果**已服务端持久化到 `.hot_last_result.json`(`/api/hot/last`)，重启不丢、刷新可恢复。
- `extract_api.py` / `hot_track_api.py` / `utils.py` 为旧实现，主流程未用，未清理。

**可能的后续 TODO**
- ~~盯盘模块开发~~（已完成，见第 12.6 及盯盘页各接口）。
- 行情源对无数据标的的兜底（换服务器重试）。
- 板块图表与活跃板块对齐。
- 把行情/追踪结果也持久化到 PostgreSQL（当前行情在 `.price_cache.json`）。
- 成员日收盘缓存 `.member_closes.json` 增量更新到最新交易日（当前需整体 rebuild）。
- 产业链 `ths_concept_chains.json` 上下游缝合的自动化/半自动维护。

> 本轮（2026-07）新增能力与修复详见 **第 12 节**。

---

## 11. 快速自测片段

```bash
# 数据库连通 + 建表
python -c "import db; print(db.ping()); db.init_db()"

# 单日提取
python extract_glm.py 20260701

# 热门股计算(API)
# POST /api/hot/compute {"start":"20260601","end":"20260630","price":true}
# 轮询 /api/hot/compute/status/<id>?since=N ; awaiting 时 POST /api/hot/compute/resolve {action:"skip"}
```

---

## 12. 本轮开发（产业链缝合 + 复盘可视化 + 盯盘增强）

> 时间：2026-07。以下为在上述基础版本之上新增/修复的能力，均已合入 `feature/glm-extraction`。

### 12.1 产业链缝合（`concept_chain.py` + `ths_concept_chains.json`）
把同花顺 390+ 概念，结合行业认知，缝合成 **18 条产业链**，每条链内再按 **上游→中游→下游** 分层。

- 配置文件 `ths_concept_chains.json` 三块：
  - `industrial_chains`：`{chain_id: {name, tier_order, tiers:{层名:[概念...]}}}`，18 条主链。
  - `company_chains`：围绕单一龙头公司的概念集合（渲染成「公司链·XX」单列）。
  - `themes_no_chain`：资金/风格/区域/政策/金融/事件等**无法反映"在炒什么"的噪音标签黑名单**，不进热力/盯盘。
- `concept_chain.py` API（带 `_cache` 惰性加载，`reload_chains()` 可热重载）：
  - `concept_to_chain(concept)` → `{chain_id, chain_name, tier}` 或 `None`
  - `chain_info()` → `{chain_id: {name, tiers:[{tier,concepts}], concepts:[]}}`
  - `is_blocked(concept)` → 是否噪音标签
  - `meaningful_concepts()` → 产业链内全部概念（去重、保序）
- **三处复用**：每日热力地图、产业链盯盘、板块梯队概念下拉。

### 12.2 复盘页「每日热力·在炒啥」tab（每日产业链地图）
回答「**今天/某天到底在炒哪条链、在拉谁、在杀谁**」。

- 后端 `GET /api/hot/concept-daymap?date=YYYYMMDD`：按 产业链 → 上下游层级 → 概念 组织；每个概念给 4 个互斥计数 `lu`(涨停)/`up5`(涨>5%)/`dn5`(跌<-5%)/`ld`(跌停) + `zt`(复盘涨停)。涨/杀口径来自成员日收盘缓存。
- 依赖 **成员日收盘缓存** `.member_closes.json`：`POST /api/hot/daymap/build` 异步构建（首次较慢，拉全部概念成员日线），`GET /api/hot/daymap/build/status/<task_id>` 查进度；构建后持久化，之后秒开。
- 前端（`hot_track.html`，`renderDaymap`/`barChip`）：
  - **日期区间**（起始/结束）+ **滑块逐日**查看，结果缓存到 `_heatCache` 避免重复请求。
  - **自定义产业链**勾选（默认前 10 条按体量），可加自己关心的链。
  - 每个概念渲染成**占比色条**：`【名称】[金涨停│红涨>5│绿跌<-5│紫跌停]`，段宽按数量占比、条长按异动体量；**大链标题**用同款大号色条（`barChip(label,o,true)`）+ 净方向底色（红涨绿跌）。
  - 布局为**单列纵向堆叠**（`.cm-grid{flex-direction:column}`），只上下滚动。

### 12.3 复盘页「板块梯队·龙头健康」tab（双口径）
回答「**板块内部是龙头带高位在走（强），还是滞涨后全低位轮动（弱）**」。

- 概念下拉 `GET /api/hot/ladder-concepts`：产业链概念 + 累计复盘活跃度，按活跃度降序。
- 明细 `GET /api/hot/sector-ladder?concept=&start=&end=`，一次返回两套口径：
  - **emotion（情绪梯队）**：逐日 龙头高度(最高连板) + 高位(≥3板)/中位(2板)/低位(首板)涨停数 + 结构标签。→ 短线高低位以**连板数**度量。
  - **trend（趋势主升）**：逐日 创 60 日新高数 / 沿均线主升数(收盘>MA20 且 MA5>MA20) / MA20 上方数 + 龙头是否新高 + 标签。
- 前端双模式切换：ECharts 堆叠柱 + 折线 + 逐日彩色标签。

### 12.4 复盘页「同花顺概念追踪」独立化 + 按天缓存
- 独立日期框 `thsStart`/`thsEnd`（与湖南人复盘互不干扰），支持「查看/强制刷新」。
- 服务端**按天结果缓存** `.concept_day_cache.json`：已算过的交易日直接命中不重算（`_load_concept_day_cache()` 启动载入）；`force` 参数强制重算。
- 路由：`GET /api/hot/concept-track`（启动异步）、`.../status/<task_id>?since=N`、`.../cancel/<task_id>`。

### 12.5 复盘页顶部工具栏按 tab 收放
- 顶部「起始/结束/排序/计算」等控件包进 `#hnrControls`，**仅在「湖南人涨停复盘」tab 显示**（`display:contents/none` 切换），其余 tab 隐藏。
- 修复 `switchTab` 漏切 `view-heat`/`view-ladder` 显示的 bug。

### 12.6 盯盘页「产业链盯盘」面板
- 后端 `GET /api/monitor/chain-board`：实时批量报价，按 产业链 → 上下游层级 → 概念 聚合成员实时涨幅/涨停数/领涨股，**15 秒缓存**。
- 前端（`monitor.html`）：gridstack 全宽面板，20 秒刷新。

### 12.7 提取重试与 tqcenter 修复（本轮 bug 修复）
- **提取「重新提取」按钮**（`update.html` + `/api/extract` 的 `force` 参数）：跳过「已存在 Excel」检查，成功后清理 stale 文件；manualcheck/verified 行均可强制重提。
- **`extract_glm._find_red_bands` 卡死修复**：旧固定阈值把红色价格文字误判成板块标题（曾检出上百个 → 触发上百次视觉调用卡死）。改为**自适应阈值**（从 `width*0.024` 起，带 `max_bands=30` 上限逐级提高）+ 恢复 `min_height` 过滤。
- **排除板块**：`EXCLUDED_SECTORS=['涨停炸板','首板']` + `_is_excluded_sector()`（含「其他」子串一律排除，如「其他XX」）。
- **`tdx_source.is_available()` 缓存修复**：旧逻辑永久缓存 False，导致 api_server 先于通达信客户端启动后无法自恢复。改为 **True 永久缓存 / False 只缓存 30s（`_TQ_RECHECK_COOLDOWN`）后重探**；并修复 finally 块 `_tq` 的 `UnboundLocalError`（改用局部 `_probe`）。

### 12.8 本轮清理（死代码/临时产物）
- 删除 `hot_track.html` 废弃 CSS（`.cm-name`/`.cm-headbar`/`.cm-total`/`.cm-n*` —— 链头改用 `barChip` 后不再用）。
- 删除后端旧路由 `/api/hot/concept-heatmap`（被每日热力 daymap 取代，前端已无引用）。
- 删除一次性探针脚本与产出（`_april_*.py/json`、`_env_dashboard.py`、`_leader_relay.py`、`_sector_overlay.py`、`_strategy_backtest.py`、`env_dashboard.png`/`sector_rotation.png`/`strategy_curve.png`/`_overlay_*.png`）。
- 保留 `_codes_cyb.json`/`_codes_kcb.json`（gitignore 数据缓存，未删）。

### 12.9 本轮相关 API 汇总
| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/hot/concept-daymap?date=` | GET | 某日产业链地图（链→上下游→概念，含涨停/涨>5/跌<-5/跌停计数） |
| `/api/hot/daymap/build` | POST | 异步构建成员日收盘缓存（首次慢） |
| `/api/hot/daymap/build/status/<task_id>` | GET | 构建进度 |
| `/api/hot/ladder-concepts` | GET | 板块梯队概念下拉（按累计活跃度） |
| `/api/hot/sector-ladder?concept=&start=&end=` | GET | 板块内部梯队(emotion)+趋势(trend)双口径 |
| `/api/hot/concept-track` (+`/status`/`/cancel`) | GET/POST | 同花顺概念涨停追踪（按天缓存，force 强刷） |
| `/api/monitor/chain-board` | GET | 产业链实时盯盘（15s 缓存） |
| ~~`/api/hot/concept-heatmap`~~ | — | 已删除（被 daymap 取代） |

### 12.10 本轮 TODO / 注意
- **首次用「每日热力」必须先点构建**（或调 `/api/hot/daymap/build`）生成 `.member_closes.json`，否则只有复盘涨停数、没有涨/杀统计。
- 成员收盘缓存不会自动增量更新到最新交易日，新交易日数据需重新 build（后续可做增量）。
- `ths_concept_chains.json` 的上下游缝合是人工+认知梳理，概念更新后需人工维护。
