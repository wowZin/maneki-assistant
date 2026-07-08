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
│   ├── tu_share.py           ← Tushare API 封装 (含缓存 & 日期回退)
│   └── health_check.py       ← 数据源健康巡检 (支持熔断阻断)
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

基于双引擎动量-均值回归策略，持续监控标的，通过飞书推送买卖信号。

## 数据源

| 场景 | 数据源 | 方式 | 类型 |
|------|--------|------|------|
| 盘中涨速扫描 | 同花顺热门榜 + 实时行情 | Cookie 直连 | 实时 |
| 实时行情/资金流 | 同花顺实时行情 API | Cookie 直连 | 实时 |
| 个股实时行情 | 同花顺批量行情 + Tushare | Cookie 直连 / Tushare | 实时/T+1 |
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
| 全量数据巡检 | 周一至五 9:25 | `python scripts/health_check.py --full` |
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
- 4C8G 以上服务器（推荐；最低 2C4G）
- 已配置 `.env`（Tushare Token、飞书凭证、代理）

### 全部服务启动

当前使用 **systemd** 统一管理服务。

```bash
# ===== 1. 核心管道（必须）：飞书 webhook + Claude Pipe =====
# 由 maneki-pipe.service 管理，接收飞书回调 → Claude 决策 → 回复
systemctl start maneki-pipe

# ===== 2. ngrok 内网穿透（必须）：飞书回调入口 =====
# 由 ngrok.service 管理（snap 安装），随 maneki-pipe 自动启动
# 公网 URL 需配置到飞书开发者后台的 Events Callback URL
systemctl start ngrok
# 查看公网 URL: curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])"

# ===== 3. 盯盘引擎（可选） =====
# 由 watchdog.service 管理，盘中每30秒自动扫描持仓股
# 检测到异常自动推送飞书
systemctl start watchdog

# ===== 4. 涨停预测（手动/定时触发） =====
# 由 crontab 自动调度，也可手动运行
python3 plays/limit_up/pipeline.py
python3 plays/limit_up/review.py

# ===== 4. 盯盘引擎（可选） =====
nohup python3 plays/watchdog/watchdog.py > data/logs/watchdog.log 2>&1 &
```

#### 重启流程（ECS 升降配后）

升级/降配 ECS 后需要重启服务：

```bash
# 1. 重启所有 maneki 服务
systemctl restart maneki-pipe ngrok watchdog

# 2. 确认全部启动正常
for svc in maneki-pipe ngrok watchdog; do
  echo "=== $svc ==="
  systemctl is-active $svc
done

# 3. 查看详细状态
journalctl -u maneki-pipe -n 20 --no-pager

# 4. 重启 ngrok
systemctl restart ngrok

# 4. 验证 ngrok 公网 URL
curl -s http://localhost:4040/api/tunnels | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['tunnels'][0]['public_url'])"

# 5. 验证飞书 webhook 连通性
curl -s http://localhost:8080/health  # 或 GET /
```

### 数据巡检

```bash
# 快速预检（pipeline 启动前自动调用，阻塞执行）
python3 scripts/health_check.py --preflight

# 全量巡检（检查 23 个 Tushare API + 东财缓存 + 代理）
python3 scripts/health_check.py --full

# 常规巡检（仅 CRITICAL + WARNING 级别）
python3 scripts/health_check.py
```

巡检异常时自动飞书告警到 `FEISHU_ALERT_CHAT_ID`，关键数据源故障触发 **5 分钟熔断**，阻塞 pipeline 执行。

### 服务状态检查

```bash
# 检查核心管道
systemctl status maneki-pipe --no-pager -l
journalctl -u maneki-pipe -n 30 --no-pager

# 检查 ngrok 公网地址
curl -s http://localhost:4040/api/tunnels | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])"

# 检查盯盘引擎
ps aux | grep watchdog | grep -v grep

# 检查 inbox 积压消息
ls -la pipelines/maneki/inbox/

# 查看最新分析记录
ls -lt plays/limit_up/data/analysis/ | head -5

# 查看巡检状态
cat data/health_state.json
```

## 风险提示

- 本系统仅供研究参考，不构成投资建议
- 涨停预测受市场环境影响，历史表现不代表未来
- 权重调整建议需人工确认后生效
- 不得用于实际交易决策

## License

MIT