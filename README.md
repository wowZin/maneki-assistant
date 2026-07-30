# Maneki Agent — A股量化策略系统

A 股量化策略集合。当前包含 **涨停预测(limit_up)**、**盘中异动扫描(surge)**、**买卖盯盘(watchdog)**，全 systemd 常驻进程。

```
daily(00:30→09:26) → surge(09:35-15:00) → watchdog(WS盯盘→信号→下单+飞书)
```

## 顶层结构

```
maneki-agent/
├── plays/                  ← 各玩法（垂直隔离，互不依赖）
│   ├── limit_up/           ← 涨停预测
│   ├── watchdog/           ← 买卖盯盘
│   └── trading/            ← 交易执行
├── scripts/                ← 共享基础设施
│   ├── tu_share.py          ← Tushare API 封装
│   ├── ths_client.py        ← 同花顺 Cookie 直连（并发批量）
│   ├── jvquant_client.py    ← jvQuant SQL 客户端（资金流/日内指标）
│   ├── jvquant_ws_client.py ← jvQuant WebSocket 实时行情
│   ├── jvquant_trade_client.py ← 实盘下单（CTP 接口）
│   └── ws_daemon.py         ← WS 守护（共享内存快照）
├── feishu_bot/             ← 飞书桥梁
├── wiki/                   ← 知识库
└── .env                    ← 环境变量
```

## 常驻进程（systemd 服务）

| 服务 | 进程 | 职责 |
|------|------|------|
| `maneki-ws-daemon` | `ws_daemon.py` | jvQuant WebSocket 共享内存，供 watchdog 读实时行情 |
| `maneki-daily` | `pipeline_daily.py` | 00:30 概念缓存 → 面板构建 → 09:26 竞价刷新+XGBoost评分+推送 |
| `maneki-surge` | `surge_scanner.py --daemon` | 09:35-15:00 每60s THS扫描异动，主闸≥20/排雷路由，写state.json |
| `maneki-watchdog` | `watchdog_daemon.py → watchdog.py` | 交易日 WS盯盘→信号检测→下单(jvquant_trade)+飞书通知 |
| `maneki-pipe` | 飞书Bot | 飞书消息路由 |

### 全天时间线

```
00:30  pipeline_daily      概念缓存加载 → 面板构建（逐只64特征×4988只）
09:26  pipeline_daily      竞价刷新面板(stk_auction) → XGBoost评分(0.8s) → Top3+≥55推送
09:35  surge_scanner       每60s: THS实时扫描池(~1100只) → 面板分≥20直通/排雷量比≥2
                             → 通过的票写 state.json(source=surge) + signals/ + pushed/
09:30  watchdog            每60s: 读 state.json → WS共享内存L1快照 → 信号检测
      ~15:00               连续3轮确认入场 → 下单(buy) + 飞书通知
                             止损/止盈/时间止损 → 卖出(sale) + 飞书通知
15:00  watchdog            EOD 汰换（surge 零信号票移除）
15:05  watchdog            引擎自退
15:00  surge_scanner       收盘休眠到次日 09:35
18:00  cron                收盘复盘
20:00  cron                wiki 编译推送
```

### 数据流

```
daily XGBoost 模型分 (panel/{date}.parquet)
  ↓
surge_scanner 主闸≥20 + 排雷检查
  ↓  写入 state.json (source="surge")
watchdog.py 自动拾取
  ↓  WS L1 实时监控 (60s一轮)
连续3轮满足入场条件 + 30s确认
  ↓
jvquant_trade_client.buy() + 飞书通知
  ↓
止损/止盈 → sale() + 飞书通知
```

## 涨停预测 (plays/limit_up)

### 模块

```
plays/limit_up/
├── pipeline_daily.py        ← 常驻入口（00:30→09:26→面板→评分→推送）
├── pipeline.py              ← 生产管线（被daily调用: 竞价刷新+模型评分+推送）
├── panel_builder.py         ← 全市场面板构建（被daily调用）
├── surge_scanner.py         ← 盘中异动扫描（独立常驻进程）
├── pusher.py                ← 推送判断（Top-N + ≥55 + 评分提高重推）
├── pipeline_feishu.py       ← ad-hoc 个股分析（飞书问股/手动）
├── strategies/              ← 五维度评分（模型普通特征）
├── factors/                 ← 因子库（XGBoost 模型分入口）
├── backtest/                ← 训练/回测
├── docs/                    ← 设计文档
└── data/                    ← 运行时产出（analysis/pushed/signals/pool）
```

## 买卖盯盘 (plays/watchdog)

```
plays/watchdog/
├── watchdog_daemon.py       ← 服务管理器，启停 watchdog.py 子进程
├── watchdog.py              ← 引擎核心（WatchState+WatchdogEngine+扫描循环）
├── watchdog_client.py       ← state.json 读写 CLI（盯/停/列表/清）
├── signals.py               ← 入场/出场/异常信号判断
├── indicators.py            ← 技术指标 (sma/atr/price_features)
└── docs/                    ← 设计文档
```

- 数据：ws_daemon 共享内存 `/dev/shm/ws_snap.json`（零 HTTP）
- 信号：实时因子分 + L1 盘口确认 → 连续3轮入场 → 30s确认 → 下单
- 出场：止损-3% / 移动止损-2% / 止盈5%/8% / 时间止损60分钟
- surge 票无上限，盘后零信号自动汰换

## 数据源

| 场景 | 数据源 | 成本 |
|------|--------|------|
| 实时行情（surge） | 同花顺 ths_client（并发批量） | 免费 |
| 盯盘实时（watchdog） | jvQuant WebSocket（ws_daemon 共享内存） | 免费 |
| 资金流向（watchdog） | jvQuant REST（300s 节流） | 按量 |
| 日线/基本面/竞价/涨停 | Tushare（按 trade_date 全量） | 积分 |

## 服务管理

```bash
# 状态查看
systemctl status maneki-daily maneki-surge maneki-watchdog maneki-pipe

# 日志查看
journalctl -u maneki-daily -n 50 --no-pager
journalctl -u maneki-surge -n 50 --no-pager
tail -f /root/maneki-agent/logs/watchdog.log

# 重新启动
systemctl restart maneki-daily
systemctl restart maneki-surge
systemctl restart maneki-watchdog

# 手动测试
python3 plays/limit_up/pipeline_daily.py --force   # 手动跑一次全流程
python3 plays/limit_up/surge_scanner.py --dry-run   # 预览 surge 路由决策
```

## 定时任务（hermes cron）

| 任务 | 时间 | 说明 |
|------|------|------|
| maneki-closing-review | 18:00 周一至五 | 收盘复盘 |
| wiki-compile | 20:00 周一至五 | wiki 编译推送git |

## 手动操作

```bash
# 飞书指令
盯 000001.SZ   → 加入盯盘
停 000001.SZ   → 移除盯盘
盯盘列表       → 查看监控列表
清盯盘         → 清空所有

# surge 预览（不下单不写文件）
python3 plays/limit_up/surge_scanner.py --dry-run

# 盯盘状态查询
python3 plays/watchdog/watchdog_client.py --status
python3 plays/watchdog/watchdog_client.py --list
```

## 风险提示

- 本系统仅供研究参考，不构成投资建议
- 涨停预测受市场环境影响，历史表现不代表未来
- 实盘交易风险自负
