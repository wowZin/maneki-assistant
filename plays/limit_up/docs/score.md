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

**默认公式**（未开启模型时）：

```
total_score = round(max(0.0, 1.0 * factor_quality_combo(row)), 2)
```

**模型公式**（设置 `LIMIT_UP_USE_MODEL=true` 时）：

```
total_score = round(max(0.0, 1.0 * factor_model_score(row)), 2)
```

`factor_model_score` 由 `plays.limit_up.factors.optimized.model_score` 实现：
- 加载 `plays/limit_up/data/backtest/models/limit_up_model.joblib`；
- 输出 0–100 连续分；
- 模型文件缺失或加载失败时，**自动回退**到 `factor_quality_combo`。

### 组件因子

| 组件 | 模块 | 启用条件 |
|------|------|----------|
| `factor_quality_combo` | `factors/optimized/quality_combo.py` | 默认（`LIMIT_UP_USE_MODEL` 未设置或 `false`） |
| `factor_model_score` | `factors/optimized/model_score.py` | `LIMIT_UP_USE_MODEL=true` |

`factor_quality_combo` 不再使用 60/80/100 的宽口径阶梯，而是提炼为两条高置信规则，并新增 **`fundflow` 硬过滤**：

- **100 分档**：`fundflow >= 10` + `turnover_rate >= 18` + `trailing_10 <= 0.20` + `technical >= 30` + `shortterm >= 30`；
- **95 分档**：`fundflow >= 10` + `turnover_rate >= 12` + `trailing_10 ∈ [0.05,0.20]` + `position_20d <= 0.70` + `pct_chg_score_day ∈ [0,5]` + `technical >= 30` + `shortterm >= 25` + `limit_up_count_20d >= 2`。

历史回测（全部轮次）中，阈值 ≥95 的推送命中率 **97.83%**、胜率 **100%**；日去重命中率 **90.91%**、胜率 **100%**。
原 60/80 宽口径档与 `quality_gate` 已从 `total_score` 汰换，保留在因子库作为备用。

组件因子的定义见 [`factors.md`](./factors.md)。因子有效性以最新训练集的评估为准，不引用历史回测数据。

## 三、模型分说明

当启用 `LIMIT_UP_USE_MODEL=true` 时：

- 模型输入为 `pit_features.py` 构建的 30+ 个 PIT 特征（含 `prev_turnover`、`max_step`、资金流加速度、K 线形态、板块动量等）。
- 模型由 `HistGradientBoostingClassifier` 训练，同时预测 `hit_limit_3` 与 `fwd_ret_3 > 0`，混合为 0–100 分。
- 反追高护栏作为乘性惩罚保留。
- 训练脚本见 [`backtest.md`](./backtest.md)。

## 四、维度权重（供其他用途，非 total）

维度权重仍从 `.env` 读取（用于监控、health_patrol、旧 review 兼容），**不参与 `total_score` 计算**。

| 维度 | .env 键 | 默认值 |
|------|---------|:--:|
| fundamental | `AGENT_WEIGHT_FUNDAMENTAL` | 0.5 |
| technical | `AGENT_WEIGHT_TECHNICAL` | 0.5 |
| fundflow | `AGENT_WEIGHT_FUND_FLOW` | 1.5 |
| sentiment | `AGENT_WEIGHT_SENTIMENT` | 1.0 |
| shortterm | `AGENT_WEIGHT_SHORTTERM` | 0.5 |

## 五、推送规则

- **排序键**：`total_score` 降序（唯一键，无 fallback）
- **推送数**：满足阈值后最多 Top-3；无满足阈值则当日不推送
- **推送阈值**：
  - `quality_combo` 模式下默认 `total_score >= 95`（由 `.env` 的 `ULTIMATE_PUSH_THRESHOLD` 控制）
  - `model_score` 模式下默认 `total_score >= 70`
- **午后过滤**：已移除
- **落盘**：`data/pushed/{HHMM}.json`

## 六、评级

| total_score | 星级 |
|:--:|:--:|
| ≥ 95 | ⭐⭐⭐⭐⭐ |
| = 100 | ⭐⭐⭐⭐⭐ |
| 0 | 不评级 |

> `quality_combo` 模式下 `total_score` 仅输出 0/95/100；`model_score` 模式下为连续 0–100。

## 七、缺失处理

- 子策略超时 / 报错：该维度记为 `null`（不参与 `total_score`；总分组件因子基于原始 panel 特征，独立于维度评分）
- 扫描返回空：写零结果文件到 `data/analysis/`
- 过滤后空：同上
- Level2 不可用：跳过 L2 观测，直接评分
- 模型文件缺失 / 加载失败：`factor_model_score` 回退到 `factor_quality_combo`

## 八、输出字段

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

## 九、AB 对比

如需 AB 对比不同权重或不同组件因子组合，走 `plays/limit_up/backtest/optimize.py`（详见 [`backtest.md`](./backtest.md)）。**不在生产 pipeline 中并行计算多套总分。**
