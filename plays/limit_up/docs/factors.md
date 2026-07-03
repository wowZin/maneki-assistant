# 因子库大全

> 打板玩法所有因子的定义、依赖、使用位置。按维度分节。
>
> **本文件不引用历史回测数据**。因子有效性以最新训练集评估为准（见 `training-set.md` 与 `backtest.md`）。

## 目录

- [维度：fundamental](#维度fundamental)
- [维度：technical](#维度technical)
- [维度：fundflow](#维度fundflow)
- [维度：sentiment](#维度sentiment)
- [维度：shortterm](#维度shortterm)
- [维度：optimized（训练集优化）](#维度optimized训练集优化)
- [跨维度组合](#跨维度组合)
- [总分](#总分)
- [附录：废弃因子清单（供参考）](#附录废弃因子清单供参考避免重复挖同样的坑)

---

## 字段说明

每个因子块的固定字段：

- **签名**：`factor_xxx(row: pd.Series | dict) -> float`
- **依赖列**：需要 panel/factor_ctx 中存在的字段
- **PIT**：是否满足时点严格（避免未来函数）
- **使用位置**：`total_score` 组件 / 维度评分器内使用 / mine 备用
- **源文件**：`factors/<dim>/*.py::factor_xxx`

---

## 维度：fundamental

基本面维度默认打分函数：`strategies/fundamental.py::score_fundamental`。

### factor_fundamental_rebuilt

- **签名**：`factor_fundamental_rebuilt(row) -> float`
- **依赖列**：`circ_mv`, `pe`, `pb`, `n_concepts`
- **PIT**：是
- **使用位置**：fundamental 维度候选
- **源文件**：`factors/fundamental/rebuilt.py`

---

## 维度：technical

技术面维度默认打分函数：`strategies/technical.py::score_technical`。

### factor_technical_rebuilt

- **签名**：`factor_technical_rebuilt(row) -> float`
- **依赖列**：`turnover_rate`, `pct_chg_std_10d`, `position_20d`, `limit_up_count_20d/60d`, `avg_amount_5d`, `pullback_10d`, `upper_shadow_pct`
- **PIT**：是
- **使用位置**：technical 维度打分主力
- **源文件**：`factors/technical/rebuilt.py`

### factor_technical_nonlinear

- **依赖列**：`vol_ratio_proxy`, `turnover_rate`, `pullback_10d`, `close/ma20`
- **使用位置**：mine 备用
- **源文件**：`factors/technical/nonlinear.py`

### factor_pullback_quality / factor_pullback_from_peak / factor_position_optimal

- **依赖列**：`pullback_10d / pullback_20d / position_20d / vol_ratio_proxy`
- **使用位置**：technical 组件（反追高辅助）
- **源文件**：`factors/technical/pullback.py`

### factor_vol_expansion_quality / factor_amount_acceleration / factor_amount_surge

- **依赖列**：`vol_ratio_proxy`, `pct_chg_score_day`, `pullback_10d`, `amount_ratio`, `amount_3d_increasing`
- **使用位置**：mine 备用
- **源文件**：`factors/technical/volume.py`

### factor_reversal_signal / factor_gap_up_quality / factor_consecutive_strength

- **依赖列**：K 线形态字段
- **使用位置**：mine 备用
- **源文件**：`factors/technical/pattern.py`

### factor_breakout_quality

- **依赖列**：`position_20d`, `vol_ratio_proxy`, `amount_ratio`, `pullback_10d/20d`
- **使用位置**：technical 组件
- **源文件**：`factors/technical/breakout.py`

---

## 维度：fundflow

资金面维度默认打分函数：`strategies/fundflow.py::score_fundflow`。

### factor_fundflow_rebuilt

- **签名**：`factor_fundflow_rebuilt(row) -> float`
- **依赖列**：`turnover_rate`, `turnover_rate_f`, `avg_amount_5d`, `net_mf_ratio`
- **PIT**：是（走 jvQuant fundflow_single）
- **使用位置**：fundflow 维度打分主力
- **源文件**：`factors/fundflow/rebuilt.py`

---

## 维度：sentiment

情绪面维度默认打分函数：`strategies/sentiment.py::score_sentiment`。

> **`total_score` 唯一组件来自 optimized 维度的 `factor_quality_gate`。**

### factor_quality_gate  🟢 total_score 组件（weight 1.0）

- **签名**：`factor_quality_gate(row) -> float`
- **依赖列**：`limit_up_count_20d`, `turnover_rate`, `trailing_10`, `position_20d`, `technical`, `pct_chg_score_day`
- **PIT**：是
- **使用位置**：`total_score` 唯一组件
- **源文件**：`factors/optimized/quality_gate.py`

### factor_sentiment_amount_combo / factor_sentiment_ensemble / factor_sentiment_pure_boosted

- **依赖列**：`sentiment` + 各类量能/位置/追高指标
- **使用位置**：mine 备用
- **源文件**：`factors/sentiment/{amount_combo,ensemble,pure_boosted}.py`

---

## 维度：shortterm

短线博弈维度默认打分函数：`strategies/shortterm.py::score_shortterm`。

### factor_limit_up_gene_20d / _60d / _composite

- **依赖列**：`limit_up_count_20d`, `limit_up_count_60d`
- **PIT**：是
- **使用位置**：shortterm 组件
- **源文件**：`factors/shortterm/limit_gene.py`

### factor_limit_gene_amount / factor_limit_gene_momentum

- **依赖列**：涨停基因 + 量能/技术分
- **使用位置**：shortterm 组件
- **源文件**：`factors/shortterm/limit_gene_amount.py`, `factors/shortterm/limit_gene_momentum.py`

### factor_trailing_momentum / factor_intraday_strength / factor_turnover_momentum / factor_growth_momentum

- **依赖列**：动量、盘中强度、换手、成长估值组合
- **使用位置**：shortterm 组件
- **源文件**：`factors/shortterm/{trailing,intraday,turnover,growth}.py`

### factor_chasing_guardrail

- **签名**：`factor_chasing_guardrail(total, trailing_5, trailing_10, position_20d, pullback_10d) -> float`（返回乘性调整后的 total）
- **使用位置**：**内部使用**，供维度评分器调整；不入 total_score，不入 REGISTRY
- **源文件**：`factors/shortterm/guardrail.py`

---

## 维度：optimized（训练集优化）

> 本维度因子基于最新训练集与回测面板优化，直接作为 `total_score` 生产组件。

### factor_quality_gate

- **签名**：`factor_quality_gate(row) -> float`
- **依赖列**：`limit_up_count_20d`, `turnover_rate`, `trailing_10`, `position_20d`, `technical`, `pct_chg_score_day`
- **PIT**：是
- **使用位置**：mine 备用（已被 `quality_combo` 汰换）
- **源文件**：`factors/optimized/quality_gate.py`

### factor_quality_combo  🟢 total_score 唯一组件（weight 1.0）

- **签名**：`factor_quality_combo(row) -> float`
- **依赖列**：`limit_up_count_20d`, `turnover_rate`, `trailing_10`, `position_20d`, `pct_chg_score_day`, `technical`
- **PIT**：是（使用 T-1 收盘及之前数据，与生产 pipeline 对齐）
- **使用位置**：唯一生产总分 `total_score`
- **源文件**：`factors/optimized/quality_combo.py`
- **依赖列**：`turnover_rate`, `trailing_10`, `position_20d`, `pct_chg_score_day`, `technical`, `shortterm`, `limit_up_count_20d`
- **PIT**：是（使用 T-1 收盘及之前数据，与生产 pipeline 对齐）
- **使用位置**：唯一生产总分 `total_score`
- **源文件**：`factors/optimized/quality_combo.py`
- **逻辑**：
  - 前置硬门槛：`fundflow >= 10`，资金维度不足直接 0 分；
  - 100 分档：`turnover_rate >= 18` + `trailing_10 <= 0.20` + `technical >= 30` + `shortterm >= 30`；
  - 95 分档：`turnover_rate >= 12` + `trailing_10 ∈ [0.05,0.20]` + `position_20d <= 0.70` + `pct_chg_score_day ∈ [0,5]` + `technical >= 30` + `shortterm >= 25` + `limit_up_count_20d >= 2`；
  - 其余情况 0 分。
- **设计依据**：从历史涨停优质股中挖掘出的高置信共振规则，并加入 `fundflow` 硬过滤剔除资金维度不足的追高/跟风案例。全部轮次回测阈值 ≥95 时命中率 97.83%、胜率 100%；日去重命中率 90.91%、胜率 100%。
- **推送阈值**：建议 `total_score >= 95`。

### factor_model_score  🟢 total_score 生产组件（模型模式）

- **签名**：`factor_model_score(row) -> float`
- **依赖列**：`pit_features.py` 构建的全部 PIT 特征（见下节「PIT 特征字段」）
- **PIT**：是
- **使用位置**：`LIMIT_UP_USE_MODEL=true` 时的 `total_score` 唯一组件
- **源文件**：`factors/optimized/model_score.py`
- **逻辑**：
  - 懒加载 `data/backtest/models/limit_up_model.joblib`；
  - 模型输出（0–1）乘以 100 后，再通过 `factor_chasing_guardrail` 做追高惩罚；
  - 模型文件缺失/加载失败/预测失败时，自动回退到 `factor_quality_combo`。
- **推送阈值**：建议 `total_score >= 70`。

## PIT 特征字段

以下字段由 `plays/limit_up/pit_features.py::build_pit_features` 统一构建，是 `factor_model_score` 的输入，也沉淀到回测面板 `panel.csv` 与训练集 `training_set.csv`。

| 字段 | 说明 |
|------|------|
| `position_20d` | T-1 收盘价在 20 日高低点区间位置 |
| `trailing_10` / `trailing_5` | T-1 相对 T-11/T-6 收益率 |
| `pct_chg_std_10d` / `pct_chg_std_5d` | 10/5 日 pct_chg 标准差 |
| `max_pct_chg_5d` | 5 日内最大涨幅 |
| `limit_up_count_20d` / `limit_up_count_60d` | 20/60 日涨停次数 |
| `max_step` | T-1 连板高度（连续 pct_chg>=9.8 天数） |
| `was_limit` | T-1 是否涨停 |
| `avg_amount_5d` | 5 日均成交额（元） |
| `pct_chg_score_day` | 评分日（T）涨幅，生产中为盘中观测值 |
| `turnover_rate` / `volume_ratio` | T-1 换手率/量比 |
| `prev_turnover` / `prev_vol_ratio` | T-2 换手率/量比 |
| `vol_accel` | 量比加速度 |
| `circ_mv` / `cmv_yi` | T-1 流通市值（万元/亿元） |
| `pe` / `pb` | T-1 估值 |
| `pullback_10d` / `pullback_20d` | T-1 相对窗口高点回撤 |
| `prev_pct` / `pct_5d` / `positive_5d` | T-2 涨幅 / T-5~T-1 累计涨幅 / 正涨幅天数 |
| `close_pos` / `body_ratio` / `upper_ratio` / `lower_ratio` / `amplitude` | T-1 K 线形态 |
| `net_mf_amount` / `net_mf_ratio` | T-1 主力净流入（万元）/ 成交额占比 |
| `buy_elg_ratio` / `buy_lg_ratio` | 超大单/大单买入占比 |
| `mf_net` / `mf_accel` / `mf_pct` | 净流入/加速度/占比 |
| `sector_heat` / `sector_rank` / `n_concepts` | 概念动量 |
| `auc_amount` / `auc_vol` / `auc_amt_ratio` / `auc_vol_ratio` | 竞价金额/量及比率 |

---

## 跨维度组合

### factor_dimension_divergence / factor_total_quality_bonus

- **依赖列**：五维度分数
- **使用位置**：mine 备用
- **源文件**：`factors/crossdim/divergence.py`, `factors/crossdim/quality_bonus.py`

---

## 总分

### total_score  🟢 唯一生产总分

- **签名**：`total_score(row: dict | pd.Series) -> float`
- **默认公式**：`round(max(0, 1.0 * factor_quality_combo(row)), 2)`
- **模型公式**：`round(max(0, 1.0 * factor_model_score(row)), 2)`（需 `LIMIT_UP_USE_MODEL=true`）
- **权重**：`quality_combo` 或 `model_score` 权重 1.0，由环境变量切换
- **使用位置**：pipeline 唯一排序键
- **源文件**：`plays/limit_up/total.py`

---

## 修改流程

当发现新因子或调整现有因子时：

1. 在对应 `factors/<dim>/*.py` 增删函数；
2. 更新本文件的对应小节（**同一提交中**，文档与代码同步）；
3. 增补 `tests/factors/test_<factor>.py` 真实调用单测；
4. 跑 `python plays/limit_up/backtest/validate.py --factor <name>` 在训练集上评估。

**红线**：本文件是因子清单的唯一事实。生产代码不允许出现未在本文档登记的因子函数。

---

## 附录：废弃因子清单（供参考，避免重复挖同样的坑）

以下因子在过往训练集评估中**表现不佳或方向不对**，已从代码库删除。保留清单是为了未来因子挖掘时避免重蹈覆辙。**这份清单不引用具体 IC 数字**（数字取决于训练集，样本变了结论可能变）；关注废弃原因即可。

### 版本化复合总分（多个版本迭代收敛为 total_score）

`factor_new_total_v2`, `factor_balanced_total`, `factor_balanced_total_pit`, `factor_balanced_adaptive_total_pit`, `factor_balanced_total_pit_v2`, `factor_aggressive_total`, `factor_return_optimized_total`, `factor_quality_value_total`, `factor_sentiment_adaptive_total_pit`, `factor_sentiment_conditional_pit`, `factor_ultimate_total_v1~v5`, `factor_new_total_mined_v2`, `factor_deep_total_v5`, `factor_cpt_amount_percentile`, `factor_balanced_ensemble`。

**教训**：多版本并行迭代造成代码腐化。唯一总分只能有一个，权重靠训练集网格搜索决定。

### CPT / 概念系列

`factor_cpt_amount_combo`, `factor_cpt_amount_anti_chase`, `factor_cpt_streak_turn`, `factor_cpt_streak_turn_combo`, `factor_cpt_turn_high_turn`, `factor_cpt_turn_pullback`, `factor_activity_rank_combo`, `factor_quality_momentum`, `factor_mv_turn_cpt`, `factor_residual_shortterm`, `factor_residual_technical`, `factor_activity_combo`, `factor_large_cap_growth_combo`。

**教训**：概念板块数据（`cpt_*`）在面板中稳定性差，衍生的组合因子过拟合严重，训练集效果与实盘偏离大。

### 方向不明或负向单因子

`factor_volatility_contraction`, `factor_large_amplitude_risk`, `factor_upper_shadow_risk`, `factor_sentiment_contrarian`, `factor_low_sentiment_contrarian_pit`, `factor_low_amplitude_breakout`。

**教训**：这些因子的假设（"波动收敛=蓄势"、"高振幅=分歧"、"低情绪=逆向机会"）在训练集上均未验证——A 股打板场景里"顺势 + 高活跃"比"反转 + 低位"更有效。

### 单变量 IC 高但过拟合

`factor_amount_power_pit`, `factor_volatility_activation_pit`, `factor_large_cap_limit_gene_pit`, `factor_momentum_amount_combo_pit`。

**教训**：这些因子在训练集上 IC 尚可但 chasing_score 高（跟 trailing 强正相关），实盘容易在追高位置发出信号。挖掘因子时必须同时看 IC 和 chasing_score。

### anti-chasing 派生变体

`factor_technical_anti_chasing`, `factor_shortterm_anti_chasing`, `factor_sentiment_anti_chasing`。

**教训**：这些是主因子 × 追高惩罚的变体，与主因子高度相关，属于冗余。追高惩罚应作为 `factor_chasing_guardrail` 独立组件叠加，而不是散落到每个维度的变体里。

### 概念 / 龙虎榜（面板缺列，未评估）

`factor_concept_momentum`, `factor_concept_up_streak`, `factor_concept_turnover`, `factor_concept_turn_5d_max`, `factor_concept_heat_combo`, `factor_concept_activity_combo`, `factor_inst_following`, `factor_top_list_quality`, `factor_inst_consistency`。

**教训**：面板缺 `cpt_*` / `inst_*` 列，IC 全部 NaN。等 `enrich_panel` 补齐这些列后可重新评估，届时**如有有效者**应重新加入 `factors/<dim>/`。

### rebuilt 系列的 v2 变体

`factor_fundamental_rebuilt_v2`, `factor_fundflow_rebuilt_v2`, `factor_technical_rebuilt_v2`。

**教训**：迭代版本 v2 相对 v1 无显著提升，且引入更多参数造成过拟合。保留最简版本即可。
