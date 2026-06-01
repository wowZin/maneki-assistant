# 盯盘助手 V1.0

## 概述

基于 l2api Level2 实时数据 + 双引擎动量-均值回归策略，持续监控标的。通过飞书推送买卖信号，状态持久化到本地 JSON。

## 盯盘指令

| 指令 | 操作 | 说明 |
|------|------|------|
| 盯 CODE | 添加盯盘 | 最多5只，自动查询股票名称 |
| 停 CODE | 移除盯盘 | 入场状态下会附带持仓小结 |
| 盯盘列表 | 查看列表 | 展示名称+代码+状态 |
| 清盯盘 | 全部清空 | 清空所有监控标的 |

## 盯盘上限

5 只。添加前检查当前数量：
- 未满：直接添加
- 已满：提示用户释放已有标的

## 添加流程

1. 规范化股票代码（如 `601991` → `601991.SH`）
2. 通过 Tushare stock_basic 查询中文名称
3. 获取 120 条日线数据，计算全部指标（KAMA/EMA/SMA/ADX/ATR/BB/RSI）
4. 推送到飞书：名称+代码+趋势状态
5. 同步 l2api 订阅

## 监控循环

每 30 秒：

```
for 每个 watching 标的:
  - 更新日线指标（每日一次）
  - check_trend → Step 1 趋势过滤
  - check_pullback → Step 2 回调待机
  - 触发回调 → 标记 signal_pending

for 每个 signal_pending 标的:
  - check_entry_score → Step 3 计分入场
  - ≥2分 → 入场，推送入场信号
  - <2分 → 信号作废，回到 watching

for 每个 entered 标的:
  - 更新最高价
  - 移动止损检查
  - 止盈提醒
  - 趋势反转 / 时间止损检查
```

## 状态管理

- 状态持久化到 `plays/watchdog/data/state.json`
- 引擎启动时自动加载历史状态
- 每个标的记录：代码、名称、状态（watching/signal_pending/entered）、入场价、最高价、持仓K线数、信号触发价等

## 飞书推送

- 盯盘添加：名称 + 代码 + 趋势状态
- 回调待机信号：名称 + 趋势原因 + 触发原因 + 参考低点
- 入场信号：名称 + 入场价 + 评分详情 + ATR/VWAP + 止损位
- 移动止损：名称 + 入场价/现价/最高/止损价 + 盈亏%
- 止盈提醒：名称 + 入场价/现价 + 盈亏% + 建议平50%
- 趋势反转出场：名称 + 出场原因 + 入场价/现价 + 盈亏%/持仓K线数

## 依赖

- `.env` 中配置 `L2API_ENABLED=true`、`L2API_ACCOUNT`、`L2API_PASSWORD`
- `.env` 中配置 `TUSHARE_TOKEN`
- `scripts/l2_client.py` — Level2 SDK
- `scripts/tu_share.py` — Tushare 统一调用