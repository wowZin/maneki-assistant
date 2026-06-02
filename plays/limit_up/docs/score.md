# 评分聚合

## 总体框架

5 个策略各自独立对每只股票输出 0-100 的评分，pipeline 负责扫描、过滤、并行评分、加权聚合、排序、推送。

## Pipeline 流程

```
异动扫描(东财API+代理) → 全系统过滤 → 预排(涨速+涨幅+人气) → 
分批Level2观测 → 五维并行评分 → 加权Top3择优 → 排序 → 飞书推送
```

### 1. 异动扫描

东方财富 clist API，涨速(f11) + 涨幅(f3) 双路合并去重。

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

## 推送规则

- 综合分 ≥ 35 取前 3 只推送（飞书卡片）
- 无 ≥ 35 不推送
- 推送记录保存到 `data/pushed/`

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