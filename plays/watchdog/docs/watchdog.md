# 盯盘助手

## 概述

基于 jvQuant WebSocket 实时行情 + limit_up 已验证因子库，持续监控标的。通过飞书推送买卖信号，状态持久化到本地 JSON。

与旧版不同：
- 不再使用 KAMA/ADX/布林带/RSI 传统指标
- 直接复用 `plays.limit_up.factors` 中回测验证过的因子
- 实时数据：`ws_daemon`（jvQuant WS 独占）写共享内存 `/dev/shm/ws_snap.json`，引擎只读不发 HTTP
- 候选股来源：手动指令 + surge_scanner 异动票（source="surge"）

## 运行方式（2026-07-26 起）

cron 每日 09:20 拉起（hermes cron `watchdog-day`），内部 60s 轮询，15:00 EOD 汰换后
15:05 自动退出；非交易日启动即退。pid 守卫防多实例。代码改动次日自然生效，无需手动重启。
（原 systemd 常驻已废弃）

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
watching     已加入盯盘，等待实时信号
alerted      触发入场信号，等待确认
entered      已入场持仓
exited       已出场，移出盯盘
```

## 入场信号

验证结论：XGBoost 模型以 T-1 数据训练，盘中极端走强值会使分数反而下跌
（median -7.6分），实时模型分不适合做盘中入场闸。入场决策完全交给盘中
实时形态信号 + L1/L2 盘口确认。面板分≥20 的筛选已在 surge 入盯前完成。
任一模式触发即进入 `alerted`：

| 模式 | 条件 |
|------|------|
| 突破 | 当前涨幅 ≥ 2%、量比 ≥ 1.3、换手 ≥ 5%、breakout_quality ≥ 10 |
| 放量拉升 | 5分钟涨幅 ≥ 2%、5分钟量比 ≥ 1.5、intraday_strength ≥ 10 |
| 涨停冲刺 | 当前涨幅 ≥ 7%、换手 ≥ 12%、turnover_momentum ≥ 12、technical ≥ 30、shortterm ≥ 25 |

`alerted` 后**信号满 30 秒**仍满足条件则自动 `entered`（真实时间判断）。

## 出场信号

| 规则 | 条件 |
|------|------|
| 固定止损 | 现价 ≤ 入场价 × 0.98 |
| 移动止损 | 现价 ≤ 入场后最高价 × 0.97 |
| 第一止盈 | 涨幅 ≥ 30% 提醒平 50% |
| 第二止盈 | 涨幅 ≥ 50% 提醒全平 |
| 日内反转 | 盘中强度转负且跌破 VWAP |
| 高位回撤 | 位置分≤-10 且 从持仓最高点回撤 ≥2%（2026-08-10 改：原"位置高+微利"就卖误杀强势股，招金黄金/盛达资源卖飞，改回撤确认） |

## 数据源

- **实时行情**：`ws_daemon` 共享内存快照（`/dev/shm/ws_snap.json`）
- **日线/基本面**：Tushare `daily` / `daily_basic`
- **五维度评分**：surge 写入 state 时播种（面板值），引擎异常分支会实时重算

## surge 票规则（source="surge"）

- 由 `plays/limit_up/surge_scanner.py` 写入 state.json
- 静默：不发触发信号、不发异常推送（critical 仍移除）；只发【surge】入场/出场
- 盘后 15:00：当天零入场信号（status != entered）自动汰换
- 数量无上限（手动通道上限 20，引擎 add 只数非 surge 票）

## 异常状态提醒

采用多因子置信度检测，避免单点阈值误报（诱空/对倒）：

| 因子 | 说明 |
|------|------|
| 资金流出金额 | 主力+大单+中单净额（jvQuant） |
| 价格行为 | 跌破 VWAP、放量、大跌 |
| 位置 | 高位加分，低位（<0.30）减 20 分保护 |
| 涨停基因后大跌 | 近 20 日 ≥2 次涨停 + 跌幅 >3% |
| 盘口压力 | 卖盘/买盘量比 |
| 连续净流出 | 最近 3 轮扫描内 ≥2 轮净流出 |
| 持仓急跌放量 | 相对入场价跌 3% 且量比 ≥1.5 |

阈值：置信度 ≥45 触发 warning，≥70 触发 critical。

推送策略（避免干扰）：
- 添加/查看盯盘：不推送飞书，仅在飞书 bot 指令中回复
- 正常状态：静默
- 异常状态：同一 level 5 分钟内不重复推送
- critical 级别自动移出盯盘

资金流向来自 jvQuant 的 `get_fundflow_single()`。

## 手动启动（调试用）

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
