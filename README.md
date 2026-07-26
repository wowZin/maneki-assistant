# Maneki Agent — A股量化策略系统

A 股量化策略集合。当前包含 **涨停预测(limit_up)**、**买卖盯盘(watchdog)** 玩法，支持多玩法横向扩展。

```
面板(00:01) → pipeline(09:30 一次性评分推送) → surge(09:35 每分钟发现)
            → watchdog(09:20-15:05 盯盘信号) → 飞书
```

## 顶层结构

```
maneki-agent/
├── plays/                  ← 各玩法（垂直隔离，互不依赖）
│   ├── limit_up/           ← 涨停预测
│   └── watchdog/           ← 买卖盯盘
├── scripts/                ← 共享基础设施
│   ├── tu_share.py          ← Tushare API 封装
│   ├── ths_client.py        ← 同花顺 Cookie 直连（含并发批量 get_batch_quotes_fast）
│   ├── jvquant_client.py    ← jvQuant SQL 客户端（资金流/日内指标）
│   ├── jvquant_ws_client.py ← jvQuant WebSocket 实时行情 (L1/L2/L10)
│   └── ws_daemon.py         ← WS 守护（共享内存快照，cron 08:55 拉起）
├── feishu_bot/             ← 飞书桥梁（只写 inbox，不决策）
├── pipelines/              ← Claude SDK 管道（LLM 决策中枢）
├── skills/                 ← LLM 行为定义
├── wiki/                   ← 知识库（wiki/raw/limit-up/panel/ 为全市场面板）
├── tests/                  ← 单测（真实 API 调用，禁止 mock）
└── .env                    ← 环境变量
```

## 涨停预测 (plays/limit_up)

面板驱动四层架构：夜间全市场面板 → 早盘一次性评分推送 → 盘中 surge 发现 + watchdog 信号。
无常驻评分进程（全部 hermes cron 拉起）。详见 `plays/limit_up/docs/architecture.md`。

### 全日时间线

```
00:01  panel_builder   全市场面板 4993 只（T-1 64特征+五维度分）
00:30  concept_cache   概念缓存增量
08:55  ws_daemon       jvQuant WS 共享内存
09:20  watchdog        盯盘引擎（cron 拉起，15:05 自退）
09:30  pipeline        竞价刷面板 → XGBoost 全量评分 → Top3+≥55 推送（~4s）
09:35  surge_scanner   每60s：扫描池(主闸≥20∪排雷) ~950只 → 5-9.8% 窗口
                       → watchdog/analysis/pushed 三处写入
15:00  watchdog        EOD 汰换（surge 零信号票移除）
```

### 模块

```
plays/limit_up/
├── docs/                   ← 设计文档（与实现一一对应）
├── strategies/             ← 五维度评分（已降级为模型普通特征）
├── factors/                ← 因子库（XGBoost 模型分入口 factors/optimized/model_score.py）
├── backtest/               ← 训练/回测（training.py / model.py / dataset.py）
├── pipeline.py             ← 早盘一次性评分（cron 09:30，非常驻）
├── surge_scanner.py        ← 盘中异动发现（每60s，先筛后拉）
├── panel_builder.py        ← 全市场面板构建（cron 00:01）
├── pool_builder.py         ← 候选池（全市场主板非ST满120天，无市值带 3032只）
├── pusher.py               ← 推送判断（Top-N + ≥55 地板 + 9:30 时间闸）
├── pipeline_feishu.py      ← ad-hoc 个股分析（飞书问股/手动）
└── data/                   ← 运行时产出（analysis/pushed/signals/snapshot_log/pool）
```

## 买卖盯盘 (plays/watchdog)

- cron 09:20 拉起，内部 60s 轮询，15:05 自动退出（代码改动次日生效，无需手动重启）
- 数据：ws_daemon 共享内存（零 HTTP）；每票每天一次 tushare 日线（120条）
- 信号：realtime_row → 同一 XGBoost 实时打分（≥40）+ L1 盘口确认 → 入场
  → 止损/止盈/回撤/时间（真实持仓分钟）出场
- surge 票静默：只发【surge】入场/出场；盘后零信号汰换
- 上限：手动 20 / surge 无上限
- 详见 `plays/watchdog/docs/`

## 数据源

| 场景 | 数据源 | 成本 |
|------|--------|------|
| 实时行情（surge/ad-hoc） | 同花顺 ths_client（并发批量） | 免费 |
| 盯盘实时（watchdog） | jvQuant WebSocket（ws_daemon 共享内存） | 免费 |
| 资金流向（watchdog） | jvQuant REST（300s 节流） | 按量 |
| 日线/基本面/竞价/涨停名单 | Tushare（按 trade_date 全量，禁逐股） | 积分 |

## 定时任务（hermes cron，无 systemd 常驻）

| 任务 | 时间(工作日) |
|------|------|
| panel-builder-nightly | 00:01 |
| concept-refresh | 00:30 |
| ws-daemon | 08:55 |
| watchdog-day | 09:20 |
| pipeline-morning | 09:30 |
| surge-scanner | 09:30（pid 守卫） |
| toplist 龙虎榜简报 | 09:00 |
| review 收盘复盘 | 18:00 |
| wiki-compile | 20:00 |

## 手动操作

```bash
# 早盘评分（cron 自动，手动测试可用 --date）
python3 plays/limit_up/pipeline.py --date 20260724

# surge 单次扫描（dry-run 不写 watchdog）
python3 plays/limit_up/surge_scanner.py --dry-run

# 候选池构建
python3 plays/limit_up/pool_builder.py

# 盯盘指令：飞书发送 盯/停/盯盘列表/清盯盘
```

## 风险提示

- 本系统仅供研究参考，不构成投资建议
- 涨停预测受市场环境影响，历史表现不代表未来
- 不得用于实际交易决策
