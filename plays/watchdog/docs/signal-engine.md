# 盯盘信号引擎 v2（重构设计）

## 1. 重构目标

当前 watchdog 基于 KAMA/ADX/布林带/RSI 的均值回归策略，与 limit_up 已验证的打板因子体系脱节，导致信号质量差、用户感知"几乎没用"。

v2 目标：
- **复用 limit_up 因子库**：把回测验证过的日线/实时因子直接用于盯盘信号
- **专注动量突破**：从"回调买入"改为"放量突破 + 涨停基因 + 质量共振"
- **数据源唯一**：实时数据只从 `scripts.jvquant_ws_client` L2 守护进程获取
- **状态机简化**：candidate → watching → alerted → entered → exited

## 2. 数据源

| 数据类型 | 来源 | 用途 |
|----------|------|------|
| 实时价格/成交量/VWAP/盘口 | `scripts.jvquant_ws_client.JvQuantWSClient` | 盘中信号触发 |
| 日线历史 | Tushare `daily` / `daily_basic` / `limit_list_d` | 日线背景因子（quality_combo、涨停基因、位置） |
| 五维度评分 | limit_up pipeline 产出的 analysis JSON | 候选股池筛选 |

## 3. 候选股池生成

每日开盘前/早盘，从 `wiki/raw/limit-up/analysis/` 和 `plays/limit_up/data/analysis/` 读取最新一轮 pipeline 结果，筛选：

- `total_score >= 85`（quality_combo 高分或次高分）
- 或用户手动添加的代码

作为今日盯盘 **candidate pool**。

## 4. 盯盘信号体系

### 4.1 实时因子（盘中每 30 秒计算）

复用 `plays.limit_up.factors` 中可直接实时化的因子：

| 因子 | 来源文件 | 实时输入 |
|------|----------|----------|
| `factor_intraday_strength` | `shortterm/intraday.py` | 当前涨幅、开盘缺口、量比、20日位置 |
| `factor_vol_expansion_quality` | `technical/volume.py` | 量比、涨幅、10日回撤 |
| `factor_turnover_momentum` | `shortterm/turnover.py` | 换手率、量比、5日波动、位置 |
| `factor_breakout_quality` | `technical/breakout.py` | 10日回撤、20日回撤、位置、量比、成交额比 |

实时字段构造：
- `pct_chg_score_day`：当前涨幅（%）
- `gap_up`：`(开盘价 / 昨收 - 1) × 100`
- `vol_ratio_proxy`：当日累计成交量 / 近20日同期均量（L2 累计 volume / Tushare 历史同日均量）
- `turnover_rate`：当日累计换手（L2 提供或估算）
- `pullback_10d / pullback_20d`：基于日线高点
- `position_20d`：基于日线 20 日高低点
- `trailing_10`：10 日累计涨幅（日线）

### 4.2 背景过滤（日线，每日更新）

- `factor_quality_combo`：只盯 95/100 分档
- `limit_up_count_20d` / `limit_up_count_60d`：涨停基因
- `fundamental / technical / fundflow / sentiment / shortterm`：五维度分

## 5. 状态机

```
candidate      今日候选股池（quality_combo 高分）
   │
   ▼ 用户手动添加 或 早盘自动订阅
watching       已加入盯盘，等待实时信号
   │
   ▼ 实时因子触发突破/放量条件
alerted        触发提醒，等待用户确认或自动入场（可配置）
   │
   ▼ 确认/自动入场
entered        已持仓
   │
   ▼ 止损/止盈/时间止损/反转
exited         已出场，移出盯盘
```

## 6. 入场规则（可配置）

默认三档信号，任一触发即 `alerted`：

### 突破模式 A
- 当前价 ≥ 今日开盘价 × 1.02
- `vol_ratio_proxy` ≥ 1.5
- `turnover_rate` ≥ 5%
- `factor_breakout_quality` ≥ 10

### 放量拉升模式 B
- 5 分钟涨幅 ≥ 2%
- 5 分钟成交量 ≥ 前 5 分钟均值 × 1.5
- `factor_intraday_strength` ≥ 10

### 涨停冲刺模式 C
- 当前涨幅 ≥ 7%
- `turnover_rate` ≥ 12%
- `factor_turnover_momentum` ≥ 12
- 背景 `technical` ≥ 30 且 `shortterm` ≥ 25

> 不用 `quality_combo`，因为该因子 95 分档要求当日涨幅 ≤ 5%，与冲刺模式互斥。

## 7. 出场规则

| 规则 | 条件 |
|------|------|
| 固定止损 | 现价 ≤ 入场价 × 0.98 |
| 移动止损 | 现价 ≤ 入场后最高价 × 0.97 |
| 分批止盈 | 涨幅 ≥ 5% 提醒平 50%；≥ 10% 提醒全平 |
| 时间止损 | 持仓 ≥ 30 分钟且未达 5% 盈利，触发预警；≥ 60 分钟强制出场 |
| 反转出场 | 实时 `factor_intraday_strength` 转负且跌破 VWAP |

## 8. 异常状态检测（资金离场 / 抛压）

对所有盯盘标的每轮扫描检查：

| 异常 | 级别 | 条件 | 动作 |
|------|------|------|------|
| 大单资金离场 | critical | 主力+大单+中单净流出 ≤ -500万 | 飞书提醒 + 移出盯盘 |
| 卖盘压力 | warning | 卖一~卖十总量 / 买一~买十总量 ≥ 2.0 | 飞书提醒 |
| 放量跌破 VWAP | warning | 现价低于 VWAP 2% 且量比 ≥ 1.5 | 飞书提醒 |
| 持仓急跌放量 | critical | 相对入场价跌 3% 且量比 ≥ 1.5 | 飞书提醒 + 移出盯盘 |

资金流向通过 `scripts.jvquant_client.JvQuantClient.get_fundflow_single()` 获取。

## 9. 指令不变

| 指令 | 操作 |
|------|------|
| 盯 CODE | 添加盯盘 |
| 停 CODE | 停止盯盘 |
| 盯盘列表 | 查看监控列表 |
| 清盯盘 | 全部停止 |

## 9. 文件变更计划

- 新增 `plays/watchdog/signals.py`：实时信号计算（复用 limit_up 因子）
- 重写 `plays/watchdog/indicators.py`：仅保留通用技术指标（ATR/SMA）和实时字段构造
- 重写 `plays/watchdog/watchdog.py`：新状态机 + 候选池 + L2 数据循环
- 更新 `plays/watchdog/docs/signal-engine.md`：本文档
- 更新 `plays/watchdog/docs/watchdog.md`：使用文档
- 保留 `plays/watchdog/tests/` 并补充新单测

## 10. 关键约束

- 实时数据源唯一：`scripts.jvquant_ws_client`
- 复用 limit_up 因子，不新建独立指标体系
- 候选股优先来自 limit_up pipeline 高分结果
- 保持现有飞书推送格式和用户指令
