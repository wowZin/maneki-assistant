# 评分聚合

## 总体框架

5 个策略各自独立对每只股票输出 0-100 的评分，pipeline 负责扫描、过滤、并行评分、加权聚合、排序、推送。

## Pipeline 流程

```
异动扫描(同花顺API+Cookie直连) → 全系统过滤 → 预排(涨速+涨幅+人气) → 
分批Level2观测 → 五维并行评分 → 加权Top3择优 → 排序 → 飞书推送
```

### 1. 异动扫描

同花顺实时行情 API，涨速(f11) + 涨幅(f3) 双路合并去重。

过滤条件：
- ST/*ST/退市/新股(N开头) 排除
- 创业板(300/301)、科创板(688)、北交所(8/4) 排除
- 涨幅 2%-9.5%

最多 3 次重试，返回候选股列表 `[{code, name, pct_chg}]`。

### 2. 全系统过滤

7 条规则，满足任一直接排除：

1. ST/*ST/退市
2. 上市不满60日
3. 创业板/科创板/北交所（代码前缀判断）
4. 当日停牌（无行情数据）
5. 自由流通市值 < 5亿
6. 5日均换手率 < 2%
7. 连续一字板（涨幅≥9.9%且换手<0.5%）

### 3. 涨停相关性预排

取 Top N 只（默认50），按涨速+涨幅+人气排名打分排序：

| 涨速 | 涨幅 | 人气排名 | 得分 |
|------|------|------|:--:|
| ≥5 | ≥7 | ≤100 | +3/+3/+4 |
| ≥3 | ≥5 | ≤200 | +2/+2/+3 |
| ≥2 | ≥3 | ≤300 | +1/+1/+2 |
| — | — | ≤500 | +1 |

### 4. 分批 Level2 观测

每批 25 只，订阅 l2api，观测 60s 后逐只评分，评分完成后取消订阅。

### 5. 五维并行评分

ThreadPoolExecutor(max_workers=4) 并行调用五个评分函数：

| 维度 | 函数 | 文件 |
|------|------|------|
| 基本面 | score_fundamental | strategies/fundamental.py |
| 技术面 | score_technical | strategies/technical.py |
| 资金面 | score_fundflow | strategies/fundflow.py |
| 情绪面 | score_sentiment | strategies/sentiment.py |
| 短线博弈 | score_shortterm | strategies/shortterm.py |

### 6. 加权Top3择优聚合

对每只股票，按加权贡献（维度原始分 × 权重）降序排列，取前3个维度：

```
dims_sorted = sort(dims, by=raw_score × weight, desc)
top3 = dims_sorted[:3]
total = Σ(raw_i × weight_i for i in top3) / Σ(weight_i for i in top3)
```

### 7. 维度共振

统计 ≥75 分的维度数量：
- ≥3 个维度 ≥75 → 标记共振信号

### 8. 评分缓存

同一天内同一股票的评分结果缓存到 `data/analysis/`，后续轮次命中缓存时直接复用，不重复调用 Tushare。

## 权重配置

| 维度 | 默认权重 | .env 配置键 |
|------|:--:|------|
| 基本面 | 1.5 | AGENT_WEIGHT_FUNDAMENTAL |
| 技术面 | 1.0 | AGENT_WEIGHT_TECHNICAL |
| 资金面 | 1.0 | AGENT_WEIGHT_FUND_FLOW |
| 情绪面 | 1.2 | AGENT_WEIGHT_SENTIMENT |
| 短线博弈 | 1.5 | AGENT_WEIGHT_SHORTTERM |

权重从 `.env` 统一读取，支持复盘 AB 对比。

## 综合评分方案

目前系统同时保留五种综合分，用于不同目标：

| 方案 | 目标 | 核心权重 | 适用场景 |
|------|------|---------|----------|
| `total` | 原加权 Top3 择优 | 情绪/短线/基本面 | 兼容旧复盘 |
| `new_total_v2` | 命中率优先 | 短线*1.8 + 涨停基因 + 反追高 | 需要覆盖更多涨停 |
| `balanced_total` | **命中率+胜率均衡（PIT 版）** | 五维聚合 + 反追高 | **实战推送首选** |
| `sentiment_adaptive_total` | Sentiment-自适应综合分 | sentiment 中轴 + 区间条件子因子 | 实验性因子 |
| `balanced_total_v2` | **权重优化后的均衡分** | shortterm=0.5 + sentiment=0.2 + technical=0.4 + 反追高 | **实战推送首选** |

## 均衡综合分 `balanced_total_v2`（PIT 版）

基于重建数据 `panel_rebuilt.csv` / `analysis_rebuilt` 权重搜索得到：
**shortterm=0.5, sentiment=0.2, technical=0.4**。

在真实扫描验证（2026-06，22 个交易日）中表现优于原 `balanced_total`：

| 方案 | hit@3 | hit@5 | win@3 | win@5 |
|------|:--:|:--:|:--:|:--:|
| `balanced_total` | 37.88% | 34.55% | 56.06% | 51.82% |
| `sentiment_adaptive_total` | 31.82% | 35.45% | 56.06% | 51.82% |
| `balanced_total_v2` | **40.91%** | **37.27%** | **56.06%** | 51.82% |

公式：

```
balanced_total_v2 = (shortterm * 0.50
                   + sentiment * 0.20
                   + technical * 0.40)
                   × chasing_penalty
```

追高惩罚与 v1 保持一致。

## Sentiment-自适应综合分 `sentiment_adaptive_total`（PIT 版）

因子挖掘显示 `sentiment` 是预测未来 3 日涨停的最强单变量（RankIC ~0.34），但简单线性加权会稀释其信息（`balanced_total` 的 RankIC 仅 ~0.09）。进一步的条件挖掘发现，`sentiment` 是主导中轴变量，不同 sentiment 区间有效子因子的方向不同：

- **高情绪区**（sentiment ≥ 55）：`position_20d`、`trailing_10_pit`、`pullback_20d` 是二次精选关键；`fundamental` 呈负向。
- **中情绪区**（35 ≤ sentiment < 55）：`technical` 是陷阱（负 IC），`shortterm + limit_up_count_20d` 更有效。
- **低情绪区**（sentiment < 35）：整体命中率仅 ~5%，整体降权，仅保留波动率 + 涨停基因。

因此 `sentiment_adaptive_total` 按 sentiment 区间切换子因子组合，在保留 sentiment 核心信息的同时，对各区间做差异化增强。

面板评估显示 `sentiment_adaptive_total` 的 Top-K 命中率优于 `sentiment`，但在真实扫描验证中不及 `balanced_total_v2`，因此作为实验性因子保留，不用于默认推送。

公式（伪代码）：

```
if sentiment < 25:
    score = sentiment * 0.7 + volatility_bonus + limit_gene_bonus
elif sentiment < 32:
    score = sentiment * 0.4 + shortterm_bonus - technical_penalty + limit_gene_bonus
else:
    score = sentiment * 0.45 + position_bonus + amount_bonus + limit_gene_bonus

score = max(0, score)
```

其中子因子具体规则见 `plays/limit_up/backtest/factor_lib.py::factor_sentiment_adaptive_total_pit`。

### 效果对比（基于 `backtest/out/panel_enriched_pit.csv`）

| 因子 | hit_limit_3 RankIC | hit@5 | hit@10 | hit@20 |
|------|:--:|:--:|:--:|:--:|
| `sentiment` | **0.338** | 33.7% | 37.4% | 37.6% |
| `balanced_total_pit` | 0.094 | 29.5% | 25.8% | 26.3% |
| `sentiment_adaptive_total_pit` | 0.255 | **41.1%** | **39.5%** | **40.8%** |

说明：`sentiment` 全样本排序能力最强；`sentiment_adaptive_total_pit` 在面板 **Top-K 头部精选** 上优于 `sentiment` 和 `balanced_total`，但在真实扫描验证中不及 `balanced_total_v2`。

## 均衡综合分 `balanced_total`（PIT 版）

`balanced_total` 保留作为 AB 对比基线，计算方式不变：

```
balanced_total = (sentiment * 0.40
                + shortterm * 0.30
                + technical * 0.20
                + fundflow * 0.05
                + fundamental * 0.05)
                × chasing_penalty
```

追高惩罚（乘法）：

| 条件 | 惩罚 |
|------|------|
| trailing_10 > 30% | ×0.75 |
| trailing_10 > 20% | ×0.85 |
| trailing_10 > 10% | ×0.93 |
| trailing_5 > 15% | ×0.90 |
| position_20d > 0.85 且 pullback_10d < 3% | ×0.80 |
| sentiment > 60 且 trailing_10 > 15% | ×0.85 |

各维度分的具体计算见对应策略文件与 docs：

| 维度 | 文件 | 说明 |
|------|------|------|
| `sentiment` | `strategies/sentiment.py` | 市场情绪、题材、竞价 |
| `shortterm` | `strategies/shortterm.py` | 涨停基因、开盘博弈、位置波动、连板溢价 |
| `technical` | `strategies/technical.py` | 量能、趋势、筹码、形态 |
| `fundflow` | `strategies/fundflow.py` | 中单/主力/龙虎榜/融资 |
| `fundamental` | `strategies/fundamental.py` | 小市值、业绩、筹码、题材 |

## 推送规则

- 按 `balanced_total_v2` 降序排序（fallback: `balanced_total` → `sentiment_adaptive_total` → `new_total_v2` → `total`）
- 取前 **3 只** 推送（默认 Top-3，兼顾命中率与胜率）
- 午后情绪面 < 25 过滤（14:00 后）
- 推送记录保存到 `data/pushed/`

## 旧推送规则（保留用于 AB 对比）

原规则：总分≥30 + 情绪≥35 + 资金≥35 + 总分≥40

### 评级显示

| 总分 | 星级 |
|------|:--:|
| ≥ 55 | ⭐⭐⭐⭐⭐ |
| ≥ 45 | ⭐⭐⭐⭐ |
| ≥ 35 | ⭐⭐⭐ |

## 缺失处理

- 子策略超时未返回：该维度记为 0 分
- 扫描返回空：写入零结果文件，不静默失败
- 过滤后空：同上
- l2api 不可用：跳过 Level2 观测，直接评分