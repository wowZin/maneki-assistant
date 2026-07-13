# Maneki Agent — A股量化策略系统

A 股量化策略集合。当前包含 **涨停预测(V2)**、**买卖盯盘** 玩法，支持多玩法横向扩展。

```
用户 → 飞书Bot(统一入口) → 路由到 plays/xxx/pipeline.py → 评分→推送

limit_up 当前路径：
plays/limit_up/pipeline.py --daemon  ← 常驻 daemon（盘中自动扫描推送）
plays/limit_up/pipeline_feishu.py    ← 旧版一次性流程（保留回退/手动分析）
```

## 顶层结构

```
maneki-agent/
├── plays/                  ← 各玩法（垂直隔离，互不依赖）
│   ├── limit_up/           ← 涨停预测 (daemon)
│   └── watchdog/           ← 买卖盯盘
├── scripts/                ← 共享基础设施
│   ├── tu_share.py          ← Tushare API 封装 (含缓存 & 日期修正)
│   ├── ths_client.py        ← 同花顺 Cookie 直连 (行情/热榜)
│   ├── jvquant_client.py    ← jvQuant SQL 客户端 (日内指标/资金流)
│   └── jvquant_ws_client.py ← jvQuant WebSocket 实时行情 (L1/L2/L10)
├── feishu_bot/             ← 飞书桥梁（只写 inbox，不决策）
├── pipelines/              ← Claude SDK 管道（LLM 决策中枢）
├── skills/                 ← LLM 行为定义
├── wiki/                   ← 知识库
├── tests/                  ← 单测（真实 API 调用，禁止 mock）
└── .env                    ← 环境变量
```

## 涨停预测 (plays/limit_up)

常驻 daemon，三层评分架构。

### 流程

```
09:15  → daily_basic(20积分) 构建候选池 1195只
         池筛选: 主板 + 非ST + 非次新 + 市值50-200亿

09:30~15:00 → 盘中循环（每轮扫描+评分耗时取决于 RPS 设置，默认约 40s）:

  ① scanner.scan_batch(1195只) → 涨幅+涨速 → 栈排序    ← 同花顺免费
     默认 RPS=30，可通过 LIMIT_UP_SCAN_RPS 调整

  ② 栈顶20只 → WS L1订阅(免费) → 五维度并行评分       ← 免费
     实时 pct_chg + 内外盘比 + vol_ratio/turnover 注入评分策略

  ③ total_score≥55:   直接推送
    total_score[45,55): WS L2/L10确认 → VWAP诱多检查 → 推送/拒绝
    total_score<45:    丢弃
```

### 模块

```
plays/limit_up/
├── docs/                   ← 设计文档
│   ├── architecture.md     ← V2 架构说明
│   ├── scanner.md          ← 扫描限流说明
│   ├── pusher.md           ← 推送阈值与去重
│   ├── score.md            ← 总分聚合规则
│   ├── filter.md           ← 实时过滤规则
│   ├── shortterm.md        ← 短线博弈维度
│   └── technical.md        ← 技术面维度
├── strategies/             ← 五维度评分
│   ├── fundamental.py      ← 基本面
│   ├── technical.py        ← 技术面
│   ├── fundflow.py         ← 资金面
│   ├── sentiment.py        ← 情绪面
│   ├── shortterm.py        ← 短线博弈（实时pct_chg+内外盘比增强）
│   └── realtime_ctx.py     ← 实时数据桥接（评分与行情之间的桥梁）
├── pipeline.py             ← 主循环 daemon（常驻服务，支持 --daemon）
├── pipeline_feishu.py      ← 旧版一次性流程（保留作为回退/手动分析）
├── scanner.py              ← 候选池批量扫描（RPS 限流）
├── pusher.py               ← 推送判断与去重
├── pool_builder.py         ← 候选池构建（daily_basic全市场扫描）
├── stack.py                ← 待评分栈（涨速排序/去重/持久化）
├── filter.py               ← 实时过滤（一字板判断）
├── total.py                ← 正式总分聚合（XGBoost/quality_combo）
├── review.py               ← 收盘复盘
├── backtest/               ← 回测框架
│   ├── backtest.py         ← 回测主入口
│   ├── dataset.py          ← 面板数据（含 jvQuant SQL 日内指标）
│   ├── model.py            ← XGBoost 模型
│   └── training.py         ← 训练集构建
└── data/                   ← 运行时产出
    ├── pool/               ← 候选池
    ├── queue/              ← 待评分栈（持久化，防进程崩溃）
    ├── analysis/           ← 每轮评分结果
    ├── pushed/             ← 推送记录
    ├── health/             ← daemon 心跳/pidfile
    └── signals/            ← 信号（review/compile 用）
```

### 数据流

```
                         daily_basic(20积分)
                              ↓
 ┌──────────────── 候选池 1195只 ────────────┐
 │  主板(00/60) + 非ST + 非次新 + 50-200亿   │
 └──────────────────────────────────────────┘
         ↓ (每 ~30s 循环)
 ┌───── batch_quotes(1195只) ← 同花顺免费 ──┐
 │     涨幅>0 → 入栈(去重)                  │
 │     涨速排序: pct×0.3 + speed×0.7        │
 │     涨幅<=0 → 踢出栈                     │
 └──────────────────────────────────────────┘
         ↓
 ┌───── 粗评: 栈顶20只 + WS L1(免费) ─────┐
 │     五维度并行评分(5线程)               │
 │     实时pct_chg + 内外盘比 增强评分      │
 └──────────────────────────────────────────┘
         ↓
         total_score≥55 → 推送
         total_score[45,55) → L2确认(VWAP/卖压) → 推送/拒绝
         total_score<45 → 丢弃
```

## 数据源

| 场景 | 数据源 | 成本 |
|------|--------|------|
| 盘中全量扫描(1195只) | 同花顺 `get_batch_quotes` | 免费 |
| 实时L1行情(20只) | jvQuant WebSocket | 免费 |
| 深度L2确认(1-3只) | jvQuant WebSocket | ~0.3元/只/天 |
| 日内历史指标 | jvQuant SQL `get_intraday_metrics` | 按股查询 |
| 日线/财务/龙虎榜 | Tushare REST API | 积分 |
| 全市场基础数据 | Tushare `daily_basic` | 20积分/天 |

## 部署

### 服务

```bash
# 涨停预测 daemon（常驻）
systemctl enable maneki-pipeline-daemon
systemctl start maneki-pipeline-daemon

# 手动启动（测试用）
python3 plays/limit_up/pipeline.py --daemon

# 飞书 Bot（接收飞书回调 → Claude 决策 → 回复）
uvicorn feishu_bot.main:app --host 0.0.0.0 --port 8080

# 盯盘引擎（可选）
python plays/watchdog/watchdog.py
```

### 手动候选池构建

```bash
python plays/limit_up/pool_builder.py
```

### 定时任务（独立，非 pipeline）

| 任务 | 时间 | 说明 |
|------|------|------|
| 龙虎榜简报 | 09:00 | 每日涨停/龙虎榜汇总 |
| 收盘复盘 | 18:00 | review.py |
| Wiki 编译 | 20:00 | wiki/compile.py + git push |
| 数据巡检 | 盘中 | health_check.py |
| 玩法巡检 | 盘中 | health_patrol.py |

### 服务状态

```bash
# 若使用 systemd，查看 daemon 日志
journalctl -u maneki-pipeline-daemon -n 30 --no-pager

# 健康巡检（不杀 daemon，仅检查心跳）
python plays/limit_up/health_patrol.py --dry-run
```

## 新旧对比

| 对比项 | 旧版 (已废弃) | 新版 |
|--------|------------|------|
| 扫描源 | 同花顺热门榜(100只) | daily_basic全市场(1195只) |
| 扫描间隔 | 10分钟(cron) | ~30秒(daemon) |
| 评分策略 | 全量评分(100只) | 栈顶20只 + L2精评 |
| 实时数据 | 无 | batch_quotes + WS L1 |
| 部署 | cron触发 | systemd常驻 |
| 成本 | Tushare逐股查询(高) | daily_basic 20积分/天 |

## 重启 daemon

代码变更后需要重启才能生效。Python 缓存已编译的 `.pyc`，保险做法是清缓存再重启：

```bash
# 1. 杀旧进程
pkill -f "pipeline.py --daemon"

# 2. 清 Python 缓存（避免用旧 `.pyc`）
find plays/limit_up/__pycache__ -name "*.pyc" -delete 2>/dev/null
find plays/limit_up/strategies/__pycache__ -name "*.pyc" -delete 2>/dev/null
find scripts/__pycache__ -name "*.pyc" -delete 2>/dev/null

# 3. 清理健康标记（否则新 daemon 日志显示旧 PID 可能误导）
rm -f plays/limit_up/data/health/pipeline_daemon.pid
rm -f plays/limit_up/data/health/pipeline_heartbeat.json

# 4. 启动
cd /root/maneki-agent && python3 plays/limit_up/pipeline.py --daemon
```

如果只想检查 daemon 是否活着：
```bash
ps aux | grep "pipeline.py --daemon" | grep -v grep
ls -t plays/limit_up/data/analysis/  # 看最新轮次的时间戳
```

## 风险提示

- 本系统仅供研究参考，不构成投资建议
- 涨停预测受市场环境影响，历史表现不代表未来
- 不得用于实际交易决策
