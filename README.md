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
│   ├── proxy_utils.py      ← 动态代理池
│   ├── l2_client.py        ← Level2 实时行情 SDK
│   ├── tu_share.py         ← Tushare API 封装
│   ├── data_audit.py       ← 数据接口审计
│   └── send_report.py      ← 报告发送
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

| 场景 | 数据源 | 方式 |
|------|--------|------|
| 盘中涨速扫描 | 东方财富 push2 API | requests + 动态代理 |
| 实时行情/资金流 | 东方财富 clist API | requests + 动态代理 |
| Level2 实时数据 | scripts/l2_client.py | TCP 长连接 |
| 历史财务/日线 | scripts/tu_share.py | REST API |
| 涨停列表(盘后) | Tushare limit_list_d | REST API |

## 定时任务

| 任务 | 时间 | 调用 |
|------|------|------|
| 早盘扫描 | 周一至五 9:35~11:30 每5分钟 | `plays/limit_up/pipeline.py` |
| 午盘扫描 | 周一至五 13:05~14:55 每5分钟 | `plays/limit_up/pipeline.py` |
| 收盘复盘 | 周一至五 18:00 | `plays/limit_up/review.py` |
| 权重优化 | 周一至五 19:00 | `plays/limit_up/optimize.py` |
| Wiki 编译 | 周一至五 20:00 | `wiki/compile.py` |
| 健康巡检 | 周一至五 10~14点整点 | `plays/limit_up/health_patrol.py` |

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

### 启动

```bash
cd /root/maneki-agent

# 配置 .env（Tushare Token、飞书凭证、代理）
cp .env.example .env

# 1. 启动飞书Bot回调服务（接收webhook写入inbox）
python -m uvicorn feishu_bot.main:app --host 0.0.0.0 --port 8080 &

# 2. 启动 Claude SDK 管道（轮询inbox → Claude决策 → 回复）
python pipelines/maneki/maneki_pipe.py &

# 手动运行一次扫描
python plays/limit_up/pipeline.py
```

## 风险提示

- 本系统仅供研究参考，不构成投资建议
- 涨停预测受市场环境影响，历史表现不代表未来
- 权重调整建议需人工确认后生效
- 不得用于实际交易决策

## License

MIT