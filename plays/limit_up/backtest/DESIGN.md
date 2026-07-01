# 回测框架设计

## 目标

用历史涨停股的因子共性优化策略参数，再用真实扫描信号验证优化效果。

## 核心流程

```
训练期（找规律）               验证期（验证效果）
   │                              │
   ▼                              ▼
从 Tushare 召回 N 日真实涨停股    从 wiki/data 加载真实扫描信号
+ 非涨停对照组（同期限）          用训练期优化的参数重新评分
↓                              ↓
五维度评分（复用 strategies/）    ScoreGap 推送筛选
对比两组因子分布                 ↓
找最优权重/阈值 → 输出参数      计算命中率 + 胜率
```

## 训练期

### 数据源

全部从 Tushare + jvQuant SQL 拉取，一次性批量缓存：

| 数据 | 来源 | 用途 |
|------|------|------|
| limit_list_d | Tushare | 当日涨停列表（正样本来源） |
| daily | Tushare | 日线（候选股筛选/涨幅过滤） |
| daily_basic | Tushare | 换手率/流通市值（filter规则） |
| moneyflow | Tushare | 资金流评分降级数据 |
| stock_basic | Tushare | 主板/ST判断 |
| trade_cal | Tushare | 交易日历 |
| stk_auction | Tushare | 历史集合竞价（开盘竞价值/量，短线+情绪都用） |
| kline | **jvQuant SQL** | 历史K线（短线VWAP/均价替代实时盘口） |
| minute | **jvQuant SQL** | 历史分时（资金流细粒度） |

**jvQuant SQL 作为增强数据源**，有则用，没有则降级 Tushare：
- kline 用于短线博弈的 VWAP/均价计算
- minute 用于资金流细粒度判断
- 策略代码已有 Tushare fallback，回测不传 jv_client 也不崩

### 样本构造

```python
for each 交易日 in [start, end]:
    # 从 limit_list_d 取当日实际涨停股（主板、非ST）
    limit_stocks = get_limit_stocks(trade_date)
    
    # 对每只涨停股跑五维度评分
    for stock in limit_stocks:
        五维度评分(strategies/ 现有评分函数)
        计算 new_total_v2
        记录: {日期, 代码, 各维度分, total, nv2, 是否被 ScoreGap 捕获}
```

没有负样本。目标是看历史上真正的涨停股在现有评分体系下长什么样，调整权重/阈值让它们尽可能多地通过 ScoreGap。

### 分析输出

每个维度单独评估，再找最佳组合：

```
因子评估：
  - 对每个维度，统计历史涨停股在该维度的分数分布
  - 计算 IC（维度分与是否涨停的相关性）
  - 哪个维度对涨停的预测力最强？哪个最弱？
  - 某个维度低于多少分时涨停概率骤降？（找阈值）

组合优化：
  - 给定一组权重，对每只历史涨停股算 total/nv2
  - 看多少涨停股的 nv2 在当期候选股中排进前 N
  - 搜索使「涨停股覆盖比例」最大化的权重组合
  - 同时记录各维度贡献占比，避免单一维度主导
```

单维度有用但不是孤立看，是用来评估因子质量和找阈值。

### 参数搜索

```bash
python plays/limit_up/backtest/optimize.py --start 20260601 --end 20260620
```

搜索范围：
- 5 个维度权重: [0.1, 3.0] 步长 0.2
- ScoreGap 阈值: [0.85, 0.98] 步长 0.01
- 评估指标: 命中率 × 0.5 + 胜率 或 自定义

## 验证期

### 数据源

不拉 Tushare，直接用项目沉淀的真实扫描信号。

每日扫描信号存放在 `plays/limit_up/data/analysis/{YYYYMMDD_HHMM}.json`
这些文件自动 compile 到 `wiki/plays/limit_up/`，按日期索引。

```python
for each 有扫描记录的日子:
    candidates = load_analysis(date)  # 从 wiki 或 data/analysis 加载
    # candidates 是当日 pipeline 实际扫描到的股票
```

### 验证流程

```python
for each 有扫描记录的日子:
    1. 加载当日候选股（真实扫描结果）
    2. 用训练期优化出的权重重新评分（复用 strategies/ 评分函数）
       - 注意：评分函数本身也有参数可调
    3. 计算 new_total_v2（训练期优化后的公式）
    4. ScoreGap 推送筛选（阈值用训练期优化值）
    5. 查当日真实涨停列表，标记命中
    6. 查下一交易日收盘价，标记盈利（T+1 滑动窗口）
    7. 累计到总结果
```

### 指标

```
命中率(HR) = 推送股中当日涨停的 / 推送总数
胜率(WR)  = 推送股中 T+1 收盘 > 当日收盘×1.001 / 推送总数
```

## 文件结构

```
backtest/
├── DESIGN.md          ← 本文档
├── data.py            ← 训练期数据获取+缓存
├── analyze.py         ← 训练期分析（因子对比/IC计算）
├── optimize.py        ← 参数搜索（权重/阈值）
├── validate.py        ← 验证期回放（加载真实扫描信号→评分→推送→评估）
└── metrics.py         ← 指标计算（复用）
```

覆盖 old `backtest_v2.py` / `backtest_v3.py` / `optimize_v3.py`。

## 用法

```bash
# 训练：分析 20 日涨停股因子分布
python plays/limit_up/backtest/analyze.py --start 20260601 --end 20260620

# 优化：在训练数据上搜索最优权重
python plays/limit_up/backtest/optimize.py --start 20260601 --end 20260620

# 验证：用优化后的权重回放真实扫描信号
python plays/limit_up/backtest/validate.py --start 20260621 --end 20260630 --weights optimal.json
```
