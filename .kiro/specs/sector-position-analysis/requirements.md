# Requirements Document

## Introduction

板块拉升低位/高位分析（sector-position-analysis）是复盘看盘工具的一个新分析视图。核心诉求：针对用户选定的某个概念板块，在一个日期区间内，逐个交易日回答一个问题——**当天这个板块拉升（涨停/入选）的个股，主要是「低位个股」还是「高位个股」**，从而帮助用户判断该板块处于「启动低位」阶段还是「高位接力/退潮」阶段。

数据来源已具备：
- 板块-个股-日期的归属关系来自数据库表 `zt_stocks(trade_date, block, code, name, lianban, last_time, reason)`（每日涨停复盘导入）。
- 个股 K 线（用于判断价格位置）来自通达信量化接口 `tdx_source.kline(code, count, period)`，并可复用 `hot_track.py` 已有的区间涨幅（20 日/60 日）与 MA10 缓存能力。

本特性在此基础上，为每个入选个股计算「位置评分」并归类为 低位/中位/高位，按板块按日聚合，并以图表（ECharts）呈现，作为 `hot_track.html` 的一个新视图/标签页。

本文档只定义「做什么」（需求），不定义具体算法实现与代码结构（留待设计阶段）。文末列出关键操作性定义的默认取值与待确认假设，供评审调整。

## Glossary

- **Sector_Position_Analyzer**: 本特性的后端分析组件，负责取数、计算个股位置评分、按板块按日聚合，产出图表所需结构化数据。
- **Position_Classifier**: 位置分类子组件，把单个个股在某交易日的「位置评分」归类为 低位（Low）/ 中位（Mid）/ 高位（High）三档之一。
- **Position_Score（位置评分）**: 衡量个股当前价格在其近期区间中所处高度的指标，取值范围 0–100。计算方式为 `(收盘价 - 回看窗口内最低价) / (回看窗口内最高价 - 回看窗口内最低价) * 100`。数值越高代表价格越接近区间高点（越「高位」）。
- **Lookback_Window（回看窗口）**: 计算 Position_Score 所用的历史交易日根数，默认 60 个交易日。
- **Low_Threshold / High_Threshold（低位/高位阈值）**: 用于把 Position_Score 分档的两个边界，默认 Low_Threshold = 33、High_Threshold = 67。Position_Score ≤ Low_Threshold 记为低位；≥ High_Threshold 记为高位；两者之间记为中位。
- **Selected_Block（选定板块）**: 用户从 `zt_stocks.block` 已有取值中选定的一个概念板块名称。
- **Date_Range（日期区间）**: 用户指定的分析起止交易日 [start, end]（YYYYMMDD）。
- **Pulled_Up_Stock（入选个股 / 被拉升个股）**: 在某交易日属于 Selected_Block 且出现在 `zt_stocks` 中的个股（即当日该板块被复盘记录的涨停/热门个股）。
- **Lianban（连板数）**: 个股连续涨停板数，来自 `zt_stocks.lianban`，由 `hot_track.parse_lianban` 解析为当前连板数。用作辅助的「高位」信号。
- **Range_Gain（区间涨幅）**: 个股在近 20 日/60 日的累计涨幅百分比，复用 `hot_track` 缓存，作为辅助展示指标。
- **Sector_Position_API**: 对外的 HTTP 接口，路径约定为 `/api/hot/sector-position`，供前端获取分析结果。
- **Sector_Position_Chart**: 前端 ECharts 图表视图，展示按日的低位/中位/高位分布与板块平均位置。
- **Kline_Provider**: K 线数据来源，即 `tdx_source.kline`（通达信量化接口），失败时回退到 `hot_track` 的 mootdx/本地通达信通道。
- **No_Data（缺数据）**: 某个股在某交易日无法取得足以计算 Position_Score 的 K 线（停牌、新上市不足回看窗口、行情源全部失败等）的状态。

## Requirements

### Requirement 1: 选择板块与日期区间

**User Story:** 作为复盘用户，我想选定一个概念板块和一段日期区间，以便针对该板块在该区间内做低位/高位分析。

#### Acceptance Criteria

1. THE Sector_Position_Analyzer SHALL 接受 Selected_Block 与 Date_Range（start、end，均为 YYYYMMDD 格式）作为分析输入。
2. WHEN 前端请求可选板块列表，THE Sector_Position_API SHALL 返回 `zt_stocks` 中在指定 Date_Range 内出现过的去重板块名称列表。
3. IF Date_Range 的 start 晚于 end，THEN THE Sector_Position_API SHALL 返回参数错误响应并附带说明「起始日期不得晚于结束日期」。
4. IF Selected_Block 在 `zt_stocks` 的指定 Date_Range 内不存在，THEN THE Sector_Position_API SHALL 返回空结果集并附带说明「该板块在所选区间内无入选个股」。
5. THE Sector_Position_Analyzer SHALL 仅将 Date_Range 内、`zt_daily` 中已入库的交易日纳入分析。

### Requirement 2: 识别每日被拉升的个股

**User Story:** 作为复盘用户，我想知道选定板块每个交易日实际拉升了哪些个股，以便这些个股构成该日低位/高位判断的样本。

#### Acceptance Criteria

1. WHEN 分析某交易日，THE Sector_Position_Analyzer SHALL 从 `zt_stocks` 读取 trade_date 等于该日且 block 等于 Selected_Block 的全部个股作为该日的 Pulled_Up_Stock 集合。
2. THE Sector_Position_Analyzer SHALL 对每个 Pulled_Up_Stock 保留其代码、名称与 Lianban 字段用于后续计算与展示。
3. IF 某交易日 Selected_Block 无任何 Pulled_Up_Stock，THEN THE Sector_Position_Analyzer SHALL 将该日的各档计数记为 0 并在结果中保留该日期。

### Requirement 3: 计算个股位置评分

**User Story:** 作为复盘用户，我想为每个被拉升个股得到一个量化的位置评分，以便客观判断它处于低位还是高位。

#### Acceptance Criteria

1. WHEN 计算某个股在某交易日的 Position_Score，THE Sector_Position_Analyzer SHALL 通过 Kline_Provider 取得该个股截至该交易日、长度为 Lookback_Window 的日线数据。
2. WHEN 已取得回看窗口日线数据，THE Sector_Position_Analyzer SHALL 按公式 `(收盘价 - 窗口内最低价) / (窗口内最高价 - 窗口内最低价) * 100` 计算 Position_Score，结果范围为 0 到 100。
3. IF 回看窗口内最高价等于最低价，THEN THE Sector_Position_Analyzer SHALL 将该个股 Position_Score 记为 50。
4. IF 某个股在某交易日为 No_Data，THEN THE Sector_Position_Analyzer SHALL 将该个股标记为 No_Data 并将其排除在该日各档计数与平均位置计算之外。
5. WHERE 用户提供了自定义 Lookback_Window，THE Sector_Position_Analyzer SHALL 使用用户提供的取值替代默认值 60 计算 Position_Score。
6. THE Sector_Position_Analyzer SHALL 为每个成功计算的个股附带其 Lianban 与 Range_Gain（20 日、60 日）作为辅助展示指标。

### Requirement 4: 个股低位/中位/高位分类

**User Story:** 作为复盘用户，我想把每个个股按位置评分归入低位、中位、高位三档，以便统计板块当天的结构。

#### Acceptance Criteria

1. WHEN 个股 Position_Score 小于或等于 Low_Threshold，THE Position_Classifier SHALL 将该个股归类为低位。
2. WHEN 个股 Position_Score 大于或等于 High_Threshold，THE Position_Classifier SHALL 将该个股归类为高位。
3. WHEN 个股 Position_Score 大于 Low_Threshold 且小于 High_Threshold，THE Position_Classifier SHALL 将该个股归类为中位。
4. WHERE 用户提供了自定义 Low_Threshold 与 High_Threshold，THE Position_Classifier SHALL 使用用户提供的阈值进行分档。
5. IF 用户提供的 Low_Threshold 大于或等于 High_Threshold，THEN THE Sector_Position_API SHALL 返回参数错误响应并附带说明「低位阈值必须小于高位阈值」。

### Requirement 5: 按板块按日聚合

**User Story:** 作为复盘用户，我想看到板块每个交易日的低位/中位/高位个股数量与整体位置水平，以便判断板块所处阶段。

#### Acceptance Criteria

1. WHEN 完成某交易日全部个股分类，THE Sector_Position_Analyzer SHALL 输出该日低位、中位、高位各档的个股数量。
2. WHEN 某交易日存在至少一个成功计算 Position_Score 的个股，THE Sector_Position_Analyzer SHALL 计算并输出该日板块平均位置评分（该日所有非 No_Data 个股 Position_Score 的算术平均）。
3. THE Sector_Position_Analyzer SHALL 按交易日升序输出结果序列，每个交易日包含日期、三档计数、平均位置评分、No_Data 个股数量。
4. THE Sector_Position_Analyzer SHALL 在每个交易日结果中附带该日入选个股明细列表（代码、名称、Position_Score、所属档位、Lianban、Range_Gain），以支持图表下钻查看。

### Requirement 6: 图表可视化

**User Story:** 作为复盘用户，我想以图表方式直观查看板块每日的低位/高位结构走势，以便快速识别启动与退潮。

#### Acceptance Criteria

1. WHEN Sector_Position_API 返回分析结果，THE Sector_Position_Chart SHALL 以交易日为横轴、以低位/中位/高位个股数量为堆叠柱状呈现每日结构。
2. THE Sector_Position_Chart SHALL 以折线叠加呈现每日板块平均位置评分，纵轴范围为 0 到 100。
3. WHEN 用户将指针悬停于某交易日，THE Sector_Position_Chart SHALL 显示该日三档计数、平均位置评分与该日入选个股明细。
4. THE Sector_Position_Chart SHALL 以固定且可区分的配色呈现低位、中位、高位三档（低位与高位在视觉上明显区分）。
5. WHERE 某交易日存在 No_Data 个股，THE Sector_Position_Chart SHALL 在该日提示信息中标明被排除的 No_Data 个股数量。

### Requirement 7: 缺数据与行情源失败处理

**User Story:** 作为复盘用户，我想在部分个股取不到行情时仍能得到可用的分析结果，以便不因个别数据缺失而阻断整体判断。

#### Acceptance Criteria

1. IF 某个股经全部行情通道（tqcenter、mootdx、本地通达信）仍无法取得回看窗口 K 线，THEN THE Sector_Position_Analyzer SHALL 将该个股记为 No_Data 并继续处理其余个股。
2. WHEN 分析完成，THE Sector_Position_API SHALL 在响应中返回被标记为 No_Data 的个股代码列表及数量。
3. IF Kline_Provider 全部不可用（通达信量化版未启动且远程与本地均失败），THEN THE Sector_Position_API SHALL 返回明确的行情源不可用错误并附带说明。

### Requirement 8: 计算性能与批量取数

**User Story:** 作为复盘用户，我想在选定板块和区间后于可接受时间内得到结果，以便流畅复盘。

#### Acceptance Criteria

1. WHEN 分析涉及多个个股，THE Sector_Position_Analyzer SHALL 复用 `hot_track` 的价格/指标缓存，对同一个股同一交易日的 K 线数据不重复向 Kline_Provider 请求。
2. WHILE 分析任务正在执行，THE Sector_Position_API SHALL 返回可供前端展示的进度信息（已处理个股数与总数）。
3. WHEN 同一 Selected_Block、Date_Range、Lookback_Window 与阈值组合被再次请求且底层数据未变化，THE Sector_Position_Analyzer SHALL 复用已缓存的位置评分结果而不重新向 Kline_Provider 请求。

---

## 关键操作性定义：默认取值与待确认假设

以下为本次已采用的默认操作性定义，评审时可调整（调整后我会同步更新对应需求）：

1. **「位置」的主口径 = 价格区间百分位（Position_Score）**：默认用近 60 个交易日的 `(收盘-最低)/(最高-最低)*100`。这是最直接反映「离底部还是离顶部」的口径。
   - 待确认：回看窗口用 60 日是否合适？是否希望改为「近 M 个月」（如 6 个月≈120 日）或提供多个窗口对比？
2. **分档阈值**：默认低位 ≤33、高位 ≥67（三等分）。
   - 待确认：是否偏好其他阈值（如 25/75）？
3. **连板数与区间涨幅作为「辅助信号」而非主分类口径**：默认仅在明细/悬浮提示中展示 Lianban 与 20/60 日涨幅，不参与主分类。
   - 待确认：是否希望把「高连板（如 ≥3 板）」或「区间涨幅过大（如 60 日 >100%）」强制判为高位，即采用「价格位置 + 连板/涨幅」的复合口径？
4. **样本来源 = `zt_stocks` 中该板块当日全部记录**：即以复盘入选的个股为样本。
   - 待确认：是否需要把同花顺概念成员（`ths_concept_members_long.csv`）里当天涨停但未被复盘收录的个股也纳入？（当前默认不纳入，仅用复盘板块归属。）
5. **图表形态 = 堆叠柱（三档计数）+ 平均位置折线**。
   - 待确认：是否还需要「板块平均位置」以外的汇总线（如低位占比折线）或切换为百分比堆叠？
