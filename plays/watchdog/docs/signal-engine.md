# 盯盘信号引擎（当前实现）

> 本文已按 2026-07-26 实现更新。引擎：cron 09:20 拉起、60s 轮询、15:05 自退；
> surge 票静默+盘后汰换；时间语义全部真实化。
> 入场 XGBoost 实时分闸已于 2026-07-26 去除（验证结论：模型 T-1 训练，盘中 OOD
> 值导致分数下跌，不适合做盘中闸；入场完全交实时形态+L1确认，面板分先由 surge 筛过）。

## 1. 重构目标

当前 watchdog 基于 KAMA/ADX/布林带/RSI 的均值回归策略，与 limit_up 已验证的打板因子体系脱节，导致信号质量差、用户感知"几乎没用"。

v2 目标：
- **复用 limit_up 因子库**：把回测验证过的日线/实时因子直接用于盯盘信号
- **专注动量突破**：从"回调买入"改为"放量突破 + 涨停基因 + 质量共振"
- **数据源唯一**：实时数据从 `ws_daemon` 共享内存 `/dev/shm/ws_snap.json` 读取（引擎不发 HTTP）
- **状态机**：watching → alerted → entered →（出场移除）

## 2. 数据源

| 数据类型 | 来源 | 用途 |
|----------|------|------|
| 实时价格/成交量/VWAP/盘口 | `ws_daemon` 共享内存快照 | 盘中信号触发 |
| 日线历史 | Tushare `daily` / `daily_basic` / `limit_list_d` | 日线背景因子（quality_combo、涨停基因、位置） |
| 五维度评分 | surge 写入 state 时播种（面板值） | realtime_row 输入 |

## 3. 候选来源

- **surge 通道**（主力）：`surge_scanner` 盘中异动票写入 state.json（source="surge"，无上限），
  只发【surge】入场/出场信号，盘后零信号汰换
- **手动通道**：飞书指令 盯 CODE（上限 20）

## 4. 盯盘信号体系

### 4.1 实时因子（盘中每 60 秒计算）

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

## 8. 异常状态检测（多因子置信度）

**问题**：单点阈值容易误报——大单净流出可能是诱空/对倒/拆单，不一定是真出货。

**方案**：6 因子加权记分：

| 因子 | 权重（示例） | 说明 |
|------|--------|------|
| 资金流出金额 | 6~30 分 | 净流出 -500万 起加分；-1亿加 30 |
| 价格行为 | 0~40 分 | 跌破 VWAP、放量、大跌 |
| 位置 | -20~+27 分 | 高位 pos ≥0.85 加 15；低位 pos ≤0.30 减 20；涨停基因后大跌加 12 |
| 盘口压力 | 6~15 分 | 卖盘/买盘量比 ≥2.0 起加分 |
| 连续净流出 | 8~15 分 | 最近 3 轮 ≥2 轮净流出 |
| 持仓急跌 | 15~25 分 | 相对入场价跌 3~5% + 放量 |

阈值：
- **warning（置信度 ≥45）**：飞书提醒
- **critical（置信度 ≥70）**：飞书提醒 + 自动移出盯盘

资金流向通过 `scripts.jvquant_client.JvQuantClient.get_fundflow_single()` 获取。

## 推送去重

- 添加盯盘/查看列表：不推送飞书，仅回复指令
- 正常状态：静默
- 异常状态：同一 level 冷却 5 分钟不重复推送
- level 变化（warning → critical）立即推送
- 状态恢复正常后清空冷却，下次异常立即推送

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
