# 总分聚合规则

> 唯一总分 `total_score`。所有历史版本（`new_total_v2 / balanced_total / balanced_total_v2 / sentiment_adaptive_total / ultimate_total_v1~v5 / cpt_* / balanced_ensemble` 等）已废弃。

## 一、Pipeline 流程（简版）

```
scanner → filter → cache_layer → pre_rank → quote_cache
      → scoring (五维度并行) → total_score → push (Top-3)
```

完整分层见 [`architecture.md`](./architecture.md)。

## 二、唯一总分：`total_score`

**入口**：`plays/limit_up/total.py::total_score(row: dict) -> float`

**公式**：

```
total_score = round(max(0.0,
      0.4 * factor_sentiment_amount_boosted(row)
    + 0.5 * factor_sentiment_position_combo(row)
    + 0.7 * factor_sentiment_volatility_combo(row)
  ), 2)
```

**权重**：三个组件的权重（0.4 / 0.5 / 0.7）为初始值，待通过训练集在 `backtest/optimize.py` 上重新搜索。

**三个组件因子**：

| 组件 | 模块 |
|------|------|
| `factor_sentiment_amount_boosted` | `factors/sentiment/amount_boosted.py` |
| `factor_sentiment_position_combo` | `factors/sentiment/position_combo.py` |
| `factor_sentiment_volatility_combo` | `factors/sentiment/volatility_combo.py` |

组件因子的定义见 [`factors.md`](./factors.md)。因子有效性以最新训练集的评估为准，不引用历史回测数据。

## 三、维度权重（供其他用途，非 total）

维度权重仍从 `.env` 读取（用于监控、health_patrol、旧 review 兼容），**不参与 `total_score` 计算**。

| 维度 | .env 键 | 默认值 |
|------|---------|:--:|
| fundamental | `AGENT_WEIGHT_FUNDAMENTAL` | 0.5 |
| technical | `AGENT_WEIGHT_TECHNICAL` | 0.5 |
| fundflow | `AGENT_WEIGHT_FUND_FLOW` | 1.5 |
| sentiment | `AGENT_WEIGHT_SENTIMENT` | 1.0 |
| shortterm | `AGENT_WEIGHT_SHORTTERM` | 0.5 |

## 四、推送规则

- **排序键**：`total_score` 降序（唯一键，无 fallback）
- **推送数**：Top-3
- **午后过滤**：14:00 后若 `sentiment < 25`，跳过推送
- **落盘**：`data/pushed/{HHMM}.json`

## 五、评级

| total_score | 星级 |
|:--:|:--:|
| ≥ 55 | ⭐⭐⭐⭐⭐ |
| ≥ 45 | ⭐⭐⭐⭐ |
| ≥ 35 | ⭐⭐⭐ |
| < 35 | 不评级 |

## 六、缺失处理

- 子策略超时 / 报错：该维度记为 `null`（不参与 `total_score`；总分组件因子基于原始 panel 特征，独立于维度评分）
- 扫描返回空：写零结果文件到 `data/analysis/`
- 过滤后空：同上
- Level2 不可用：跳过 L2 观测，直接评分

## 七、输出字段

`data/analysis/{HHMM}.json` 每条记录：

```json
{
  "code": "600176.SH",
  "name": "中国巨石",
  "pct_chg": 3.21,
  "resonance": {"dims_75": 2, "total_score_tier": "⭐⭐⭐⭐"},
  "scores": {
    "fundamental": 45, "technical": 68, "fundflow": 30,
    "sentiment": 72, "shortterm": 55, "first_board": null
  },
  "reasons": {...},
  "factors": {
    "sentiment_amount_boosted": 22.5,
    "sentiment_position_combo": 18.0,
    "sentiment_volatility_combo": 15.7,
    "limit_up_gene_composite": 12.0,
    "technical_rebuilt": 55.0
  },
  "total_score": 48.7,
  "meta": {"trade_date": "20260702", "l2": true, "weights_hash": "abc123"}
}
```

## 八、AB 对比

如需 AB 对比不同权重或不同组件因子组合，走 `plays/limit_up/backtest/optimize.py`（详见 [`backtest.md`](./backtest.md)）。**不在生产 pipeline 中并行计算多套总分。**
