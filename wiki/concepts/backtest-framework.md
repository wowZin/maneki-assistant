# 回测框架与因子优化

## 整体思路

```
历史真实涨停股（正样本） + 同池非涨停股（负样本）
         ↓
    五维度评分
         ↓
    搜索权重组合
         ↓
 最大化 命中率 - 误报率惩罚
```

不依赖传统回测（逐日 replay），而是用**因子分析 + 权重搜索**的方式优化策略。

## 训练方法

### 样本构造

每个交易日从 Tushare 拉取数据：

```
候选池（涨幅 0~9.5%, 过 filter.py 7 条规则）
  ├── 当日实际涨停的 → 正样本
  └── 当日没涨停的   → 负样本（等量采样）
```

正负样本来自**同一个候选池**，对比的是"系统盯着的股票里，为什么 A 涨停了 B 没有"。

### 评分

对每只股票跑五维度评分（复用 `strategies/` 下的实际策略函数）：
- 基本面 (fundamental)
- 技术面 (technical)  
- 资金流 (fundflow)
- 情绪面 (sentiment)
- 短线博弈 (shortterm)

策略函数通过 `trade_date` 参数支持历史日期回放，不加全局 date patcher。

### 全局缓存

评分前预填充策略依赖的全局缓存（`_THS_QUOTE_CACHE` / `_HOT_CONCEPT_CACHE` / `_HOT_LIST_ITEMS`），从 Tushare 历史数据构造，而非实时 API。

## 优化方法

### 权重搜索

网格搜索五维度权重 + ScoreGap 阈值：

| 参数 | 范围 | 步长 |
|------|------|------|
| 各维度权重 | 0.1 ~ 3.0 | 0.2/0.3/0.5 |
| ScoreGap | 0.85 ~ 0.95 | 0.02/0.03 |

对所有权重组合：

```
对每组权重:
  计算每只股票的总分（加权 Top3 择优）
  对正样本：命中率 = 总分 >= 阈值 / 正样本总数
  对负样本：误报率 = 总分 >= 阈值 / 负样本总数
  综合评分 = 命中率 × 100 - 误报率 × 30
```

### 最优选择标准

```python
综合评分 = 命中率 × 100 - 误报率 × 30
```

- 命中率：涨停股被 ScoreGap 捕获的比例（越高越好）
- 误报率：非涨停股被 ScoreGap 捕获的比例（越低越好，×30 惩罚权重）

## 验证方法

用 `validate.py` 在真实 pipeline 扫描信号上回放：

```
从 data/analysis/ 加载历史扫描结果
用优化后的权重重新评分
ScoreGap 推送筛选
查当日真实涨停列表 → 算命中率
查下一交易日收盘价 → 算胜率（T+1）
```

## 三件套（2026-07-02 重构后）

原 20+ 回测脚本（validate*.py, optimize*.py, mine*.py, enrich*.py, evaluate*.py, topk_backtest.py, train_walkforward.py, ...）合并为三件：

- `plays/limit_up/backtest/mine.py` — 因子挖掘（单/双/三因子组合）
- `plays/limit_up/backtest/validate.py` — 单因子 / total_score 的 IC & hit 报告
- `plays/limit_up/backtest/optimize.py` — 权重优化（total_score 组件权重 + AGENT_WEIGHTS）

原 `factor_lib.py`（2900 行）已废弃；有效因子下沉到 `plays/limit_up/factors/<dim>/`，负 IC 与冗余组合直接删除。详见 [[../../plays/limit_up/docs/backtest.md]] 与 [[../../plays/limit_up/docs/factors.md]]。

## 因子修正记录

### 短线博弈 (shortterm) V2 → V3
旧版偏重连板基因（首板+15、连板+30~50），但 81% 的涨停是首板，预测力差。
新版聚焦首板场景：竞价异动25 + 分歧转一致25 + 首板基因20 + 资金共振20 + 连板溢价10。

### 资金流 (fundflow)
中单净流出扣分上限从 -12 降至 -3。涨停当天中单止盈卖出是正常现象，不该扣到 0。

### 技术面 (technical)
市值分级放宽：小中市值(<100亿)+8, 中市值(<300亿)+5, 大市值(<500亿)+2, 超大市值(>500亿)+0。
原版 50/100/200 亿阈值过于激进，261/285 只涨停股被错误扣分。

### 情绪面 (sentiment)
熊市态竞价乘数从 0.7 放宽至 0.9。市场偏弱时竞价评分不应被过度惩罚。

## 已知限制

- 策略函数中 `today_str = datetime.now()` 仍用实时时间（已在函数签名中加 `trade_date` 参数覆盖）
- Tushare daily 每天限 6000 条，批量拉取时可能截断
- 集合竞价数据 (`stk_auction`) 部分历史日期可能不完整
- jvQuant SQL 客户端可补充历史 K 线/分钟/竞价数据（按股 1 分/次，免费额度够用）
