# AI看盘 技术文档

> 目的：让新会话/新开发者快速理解全部代码结构、数据流与关键逻辑，直接接着开发。
> 更新时间：2026-07（对应分支 `feature/glm-extraction`）。

---

## 1. 项目概述

一个 A 股「涨停复盘」数据平台，三大功能：

1. **数据同步**：从淘股吧博客下载「湖南人涨停复盘」图片 → GLM-OCR + 视觉识别提取成 Excel → 写入 PostgreSQL。
2. **热门股追踪**：按入榜规则统计热点板块与个股，接通达信行情，逐日跟踪涨幅/均线并按规则移除。
3. **盯盘**：占位，未开发。

主入口：`api_server.py`（Flask，端口 5000）。首页 `/` 是左侧导航（数据同步 / 热门股复盘追踪 / 盯盘）。

---

## 2. 运行方式

```bash
python api_server.py          # 主服务, http://127.0.0.1:5000
# 或双击 start_server.bat
```

- 依赖见 `requirements.txt`；`bootstrap.py` 会在部分脚本启动时自动补装缺失依赖。
- Windows + PowerShell 环境。命令分隔用 `;`（不是 `&&`）。
- 关键依赖：flask, requests, beautifulsoup4, PyYAML, openpyxl, Pillow, numpy, **psycopg2-binary**（PostgreSQL）, **mootdx**（通达信行情）, akshare（旧，交易日历仍在用）, rapidocr_onnxruntime（旧本地 OCR，现主要用 GLM-OCR）。

### 外部依赖服务
- **智谱 GLM**：OCR(`glm-ocr`) + 视觉模型(`GLM-4.5V`)，key 在 `config.yml` 的 `ai.zhipu.api_key`。
- **PostgreSQL**：跑在 Docker 容器 `postgres-local`，映射 `localhost:15432`，账号 `postgres/postgres`，库 `postgres`。**需先启动 Docker Desktop 且容器运行**，否则入库/追踪不可用。
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
| `extract_glm.py` | **涨停复盘提取**：GLM-OCR 找 03/04 图、红色板块标题识别、导出 Excel。 |
| `download_by_id.py` | CLI 下载器（按图片ID命名 + 写 `_order.txt`），网页下载逻辑与其对齐。 |
| `bootstrap.py` | 依赖自动检测安装。 |
| `utils.py` | 旧的本地 RapidOCR 工具（`ocr_image` 等），现基本被 GLM-OCR 取代。 |
| `extract_api.py` | 旧的提取 API（独立），**当前主流程不用**，保留参考。 |
| `hot_track_api.py` | 旧的独立热门股 API（端口5001），**当前主流程用 api_server**。 |
| `templates/nav.html` | 左侧导航首页（iframe 加载子页）。 |
| `templates/update.html` | 数据同步页（下载/提取/入库/状态三模块）。 |
| `templates/hot_track.html` | 热门股追踪页（计算执行框、表格、图表、面板拖动）。 |
| `.price_cache.json` | 行情缓存（涨跌幅/20日/60日/MA10/是否破位），gitignore。 |
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

**返回结构**：`{start,end,sort,dates,by_date[{date,blocks[{block,cum_count,active_count,new_count,removed_today,times_count,total_count,active,removing,stocks[{code,name,is_kcb_cyb,first_date,warn_date,remove_date,track:{date:{desc,pct,pct_20d,pct_60d,ma10,below_ma10,present}}}]}]}], blocks_summary[...], missing_report, missing_codes}`

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
- 结果本地缓存(localStorage)，刷新自动恢复。
- 表格：日期行 × 板块卡片(全局排序)，个股缩略块；**预警**=保留涨跌色+黄色边框；**已移除**=灰色"跌破10日线第三日未收回"；新入选有标记。
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
- `hot_tasks/extract_tasks/submit_tasks/download_tasks` 都是**内存字典**，服务(debug)重载或重启会丢任务；结果靠前端 localStorage 兜底。
- `extract_api.py` / `hot_track_api.py` / `utils.py` 为旧实现，主流程未用，未清理。

**可能的后续 TODO**
- 盯盘模块开发。
- 行情源对无数据标的的兜底（换服务器重试）。
- 板块图表与活跃板块对齐。
- 把行情/追踪结果也持久化到 PostgreSQL（当前行情在 `.price_cache.json`）。

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
