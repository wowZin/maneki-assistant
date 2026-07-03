# 盯盘助手

## 概述

基于 jvQuant WebSocket 实时行情 + limit_up 已验证因子库，持续监控标的。通过飞书推送买卖信号，状态持久化到本地 JSON。

与旧版不同：
- 不再使用 KAMA/ADX/布林带/RSI 传统指标
- 直接复用 `plays.limit_up.factors` 中回测验证过的因子
- 数据源唯一：jvQuant WebSocket（无需守护进程）
- 候选股优先来自 limit_up pipeline 高分结果

## 盯盘指令

| 指令 | 操作 | 说明 |
|------|------|------|
| 盯 CODE | 添加盯盘 | 最多20只，自动查询股票名称并加载日线 |
| 停 CODE | 移除盯盘 | 入场状态下会附带持仓小结 |
| 盯盘列表 | 查看列表 | 展示名称+代码+状态 |
| 清盯盘 | 全部清空 | 清空所有监控标的 |

## 盯盘上限

20 只。添加前检查当前数量：
- 未满：直接添加
- 已满：提示用户释放已有标的

## 状态机

```
candidate    候选股池（来自 limit_up pipeline total_score >= 85）
watching     已加入盯盘，等待实时信号
alerted      触发入场信号，等待确认
entered      已入场持仓
exited       已出场，移出盯盘
```

## 入场信号

任一条件触发即进入 `alerted`：

| 模式 | 条件 |
|------|------|
| 突破 | 当前涨幅 ≥ 2%、量比 ≥ 1.3、换手 ≥ 5%、breakout_quality ≥ 10 |
| 放量拉升 | 5分钟涨幅 ≥ 2%、5分钟量比 ≥ 1.5、intraday_strength ≥ 10 |
| 涨停冲刺 | 当前涨幅 ≥ 7%、换手 ≥ 12%、turnover_momentum ≥ 12、technical ≥ 30、shortterm ≥ 25 |

`alerted` 后下一扫描周期仍满足条件则自动 `entered`。

## 出场信号

| 规则 | 条件 |
|------|------|
| 固定止损 | 现价 ≤ 入场价 × 0.98 |
| 移动止损 | 现价 ≤ 入场后最高价 × 0.97 |
| 第一止盈 | 涨幅 ≥ 5% 提醒平 50% |
| 第二止盈 | 涨幅 ≥ 10% 提醒全平 |
| 时间止损 | 持仓 60 分钟未达目标强制出场 |
| 日内反转 | 盘中强度转负且跌破 VWAP |

## 数据源

- **实时行情**：`scripts.jvquant_ws_client.JvQuantWSClient`
- **日线/基本面**：Tushare `daily` / `daily_basic`
- **五维度评分**：`wiki/raw/limit-up/analysis/` 或 `plays/limit_up/data/analysis/` 最新一轮结果

## 异常状态提醒

对每只盯盘标的持续检测：

| 异常 | 级别 | 动作 |
|------|------|------|
| 大单资金离场 | critical | 飞书提醒并自动移出盯盘 |
| 卖盘压力过大 | warning | 飞书提醒 |
| 放量跌破 VWAP | warning | 飞书提醒 |
| 持仓急跌放量 | critical | 飞书提醒并自动移出盯盘 |

资金流向来自 jvQuant 的 `get_fundflow_single()`。

## 启动

```bash
python plays/watchdog/watchdog.py
```

引擎读取 `.env` 中的 `JVQUANT_TOKEN`、`TUSHARE_TOKEN`、飞书凭证。

## 依赖

- `.env` 中配置 `JVQUANT_TOKEN`
- `.env` 中配置 `TUSHARE_TOKEN`
- `.env` 中配置 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_CHAT_ID_SIGNAL`
- `scripts.jvquant_ws_client` — jvQuant WebSocket 客户端
- `scripts.tu_share` — Tushare 统一调用
