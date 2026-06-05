# Maneki Agent — A股量化策略系统

A 股量化策略集合。当前包含 **涨停预测**、**买卖盯盘** 玩法，支持多玩法横向扩展。

```
用户 → 飞书Bot(统一入口) → 路由到 plays/xxx/pipeline.py → 评分→推送
```

## 顶层结构

```
maneki-agent/
├── plays/                  ← 各玩法（垂直隔离，互不依赖）
│   ├── limit_up/           ← 涨停预测
│   └── watchdog/           ← 买卖盯盘
├── scripts/                ← 共享基础设施
│   ├── proxy_utils.py       ← 动态代理池 + 重试机制
│   ├── l2_client.py          ← Level2 实时行情 SDK (TCP 长连接)
│   ├── tu_share.py           ← Tushare API 封装 (含缓存 & 日期回退)
│   ├── health_check.py       ← 数据源健康巡检 (支持熔断阻断, 自动拉起L2)
│   ├── l2_daemon.py           ← L2 守护进程 (TCP代理, 单例长连接)
│   └── l2_daemon_client.py    ← L2 守护进程 TCP 客户端
├── feishu_bot/             ← 飞书桥梁（只写 inbox，不决策）
│   ├── main.py             ← FastAPI → 写入 inbox
│   └── feishu_client.py    ← 飞书 API 封装
├── pipelines/              ← Claude SDK 管道（LLM 决策中枢）
│   └── maneki/             ← 股票助手管道
│       ├── maneki_pipe.py  ← 主循环：轮询 inbox → Claude 决策 → 回复
│       ├── config.yaml     ← Claude 配置
│       └── inbox/          ← 消息队列（按群聊隔离）
├── skills/                 ← LLM 行为定义（Claude SDK 原生读取）
│   ├── maneki_stock_bot/   ← 系统入口 + 路由规则
│   ├── stock-analyzer/     ← 五维度评分分析
│   ├── stock-watchdog/     ← 盯盘管理
│   └── stock-wiki/         ← 知识库查询
├── wiki/                   ← 知识库
├── tests/                  ← 单测
└── .env                    ← 环境变量
```

## 当前玩法

### limit_up — 涨停预测

盘中实时扫描涨速异动股，五维度评分 + 加权 Top3 择优推送，盘后自动复盘。

```
plays/limit_up/
├── docs/                   ← 设计文档
├── strategies/             ← 五维度评分
│   ├── fundamental.py      ← 基本面
│   ├── technical.py        ← 技术面
│   ├── fundflow.py         ← 资金面
│   ├── sentiment.py        ← 情绪面
│   └── shortterm.py        ← 短线博弈
├── pipeline.py             ← 主流程编排
├── filter.py               ← 候选股过滤
├── review.py               ← 收盘复盘
├── optimize.py             ← 权重优化
├── health_patrol.py        ← 健康巡检
└── data/                   ← 运行时产出
    ├── analysis/ signals/ pushed/
    ├── reports/ weights/ history/
    └── logs/
```

#### 五维度评分

| 维度 | 说明 |
|------|------|
| 基本面 | ROE、扣非净利、营收增速、公告、解禁 |
| 技术面 | 均线排列、MACD/KDJ、量价配合、换手 |
| 资金面 | 主力净流入、龙虎榜、封板质量、融资 |
| 情绪面 | 涨停基因、连板效应、竞价博弈、人气排名 |
| 短线博弈 | 封板质量、连板动量、开盘博弈、攻击独特性 |

**总分 = 加权 Top3 择优** — 取加权贡献最高的 3 个维度计算加权均值。

### watchdog — 盯盘助手

基于 L2 实时数据 + 双引擎动量-均值回归策略，持续监控标的。通过飞书推送买卖信号。

## 数据源

| 场景 | 数据源 | 方式 | 类型 |
|------|--------|------|------|
| 盘中涨速扫描 | 东方财富 push2 clist API | requests + 动态代理 | 实时 |
| 实时行情/资金流 | 东方财富 push2 clist API | requests + 动态代理(失败自动换IP重试) | 实时 |
| 个股实时行情 | 东方财富 push2 stock/get API | requests + 代理 → clist缓存降级 | 实时 |
| Level2 实时数据 | dy1.l2api.cn :18100/18103/18105 | TCP 长连接 | 实时(Tick) |
| 历史日线/财务 | Tushare REST API | scripts/tu_share.py | T+1 / 季度 |
| 涨停列表/龙虎榜 | Tushare REST API | scripts/tu_share.py | T+1 |

## 定时任务

| 任务 | 时间 | 调用 |
|------|------|------|
| 早盘扫描 | 周一至五 9:35~11:30 每5分钟 | `python plays/limit_up/pipeline.py` |
| 午盘扫描 | 周一至五 13:05~14:55 每5分钟 | `python plays/limit_up/pipeline.py` |
| 收盘复盘 | 周一至五 18:00 | `python plays/limit_up/review.py` |
| 权重优化 | 周一至五 19:00 | `python plays/limit_up/optimize.py` |
| Wiki 编译 | 周一至五 20:00 | `python wiki/compile.py` |
| L2 守护进程 | 周一至五 9:25 (随--full巡检自动拉起) | `scripts/l2_daemon.py` (15:05自动退出) |
| 全量数据巡检(含L2启动) | 周一至五 9:25 | `python scripts/health_check.py --full` |
| 数据巡检(盘中) | 周一至五 9:35~14:30 每小时 | `python scripts/health_check.py` |
| 玩法巡检 | 周一至五 10~14点整点 | `python plays/limit_up/health_patrol.py` |

## 扩展新玩法

参见 `CLAUDE.md` 中的扩展规范。新建 `plays/新玩法名/` 即可：

```
plays/新玩法名/
├── docs/             ← 设计文档
├── strategies/       ← 评分维度
├── pipeline.py       ← 主流程
├── filter.py         ← 过滤规则
└── data/             ← 运行时数据
```

## 部署

### 环境要求
- Python 3.10+
- 2C4G 以上服务器
- 已配置 `.env`（Tushare Token、飞书凭证、L2 账号、代理）

### 全部服务启动

```bash
cd /root/maneki-agent

# ===== 1. 飞书 Bot 回调服务（必须） =====
# 接收飞书 webhook → 写入 inbox → 即时回复"请稍候"
nohup python3 -m uvicorn feishu_bot.main:app --host 0.0.0.0 --port 8080 \
  > data/logs/feishu_bot.log 2>&1 &

# ===== 2. Claude SDK 管道（必须） =====
# 轮询 inbox → Claude 决策 → 调用评分/盯盘/查wiki → 回复飞书
nohup python3 pipelines/maneki/maneki_pipe.py \
  > data/logs/maneki_pipe.log 2>&1 &

# ===== 3. 盯盘引擎（可选） =====
# 基于 L2 实时数据监控标的，推送买卖信号
nohup python3 plays/watchdog/watchdog.py \
  > data/logs/watchdog.log 2>&1 &

# ===== 4. L2 守护进程（自动/手动） =====
# 维护唯一 L2 长连接，供 pipeline/watchdog 通过 TCP 共享 (127.0.0.1:18999)
# 开盘日 9:25 由 health_check --full 自动拉起，15:05 自动退出
# 也可手动启动：
# python3 scripts/l2_daemon.py --daemon

# ===== 5. 涨停预测（手动/定时触发） =====
# 主流程: 异动扫描 → 五维评分 → 排序 → 飞书推送
python3 plays/limit_up/pipeline.py

# 收盘复盘: 汇总当日预测 vs 实际涨停
python3 plays/limit_up/review.py
```

### 数据巡检

```bash
# 快速预检（pipeline 启动前自动调用，阻塞执行）
python3 scripts/health_check.py --preflight

# 全量巡检（检查 23 个 Tushare API + 东财缓存 + L2 + 代理）
python3 scripts/health_check.py --full

# 常规巡检（仅 CRITICAL + WARNING 级别）
python3 scripts/health_check.py
```

巡检异常时自动飞书告警到 `FEISHU_ALERT_CHAT_ID`，关键数据源故障触发 **5 分钟熔断**，阻塞 pipeline 执行。

### 服务状态检查

```bash
# 检查各服务进程
ps aux | grep -E "uvicorn|maneki_pipe|watchdog|l2_daemon" | grep -v grep

# 检查飞书 Bot 健康
curl http://localhost:8080/

# 检查 L2 守护进程
echo "HEALTH" | nc localhost 18999

# 检查 inbox 积压消息
ls -la pipelines/maneki/inbox/

# 查看巡检状态
cat data/health_state.json

# 查看最新分析记录
ls -lt plays/limit_up/data/analysis/ | head -5
```

## 风险提示

- 本系统仅供研究参考，不构成投资建议
- 涨停预测受市场环境影响，历史表现不代表未来
- 权重调整建议需人工确认后生效
- 不得用于实际交易决策

## License

MIT