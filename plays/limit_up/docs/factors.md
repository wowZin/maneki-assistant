# 因子库大全

> 打板玩法所有因子的定义、依赖、期望 IC、使用位置。按维度分节，无版本号（旧的 v1/v2/v3/v4/v5 / balanced / adaptive / ensemble / conditional / cpt_* 系列已全部废弃或合并）。
> 因子回测数据源：`plays/limit_up/backtest/out/all_factors_report.md`（面板 3897 条记录，1529 只股票，22 个交易日）。

## 目录

- [维度：fundamental](#维度fundamental)
- [维度：technical](#维度technical)
- [维度：fundflow](#维度fundflow)
- [维度：sentiment](#维度sentiment)
- [维度：shortterm](#维度shortterm)
- [跨维度组合（仅供 mine 使用）](#跨维度组合仅供-mine-使用)
- [总分](#总分)
- [废弃因子清单](#废弃因子清单)

---

## 字段说明

每个因子块的固定字段：

- **签名**：`factor_xxx(row: pd.Series | dict) -> float`
- **依赖列**：需要 panel/factor_ctx 中存在的字段
- **PIT**：是否满足时点严格（避免未来函数）
- **IC hit_limit_3**：面板 RankIC，正号预测涨停
- **使用位置**：是否在 `total_score` / 维度评分 / mine 备用
- **源文件**：`factors/<dim>/*.py::factor_xxx`

---

## 维度：fundamental

基本面维度默认打分函数：`strategies/fundamental.py::score_fundamental`。

### factor_fundamental_rebuilt

- **签名**：`factor_fundamental_rebuilt(row) -> float`
- **依赖列**：`circ_mv`, `pe`, `pb`, `roe`（当前面板 NaN 率过高，需修 `enrich_panel`）
- **PIT**：是
- **IC hit_limit_3**：NaN（面板未触发）
- **使用位置**：维度评分候选；`total_score` 不使用
- **源文件**：`factors/fundamental/rebuilt.py`
- **注**：面板列修复前暂不入生产。

### factor_fundamental_quality

- **依赖列**：`circ_mv_tier`, `earnings_surprise`, `holder_concentration`
- **IC hit_limit_3**：NaN（面板未触发）
- **使用位置**：mine 备用
- **注**：待面板列补齐后重启。

---

## 维度：technical

技术面维度默认打分函数：`strategies/technical.py::score_technical`。

> 基线 `technical` IC=0.085，被 `technical_rebuilt` 替代作为维度默认打分。

### factor_technical_rebuilt

- **签名**：`factor_technical_rebuilt(row) -> float`
- **依赖列**：`ma20`, `vol_ratio_proxy`, `turnover_rate`, `close`, `pct_chg_score_day`
- **PIT**：是
- **IC hit_limit_3**：0.240
- **chasing_score**：0.64（偏高，与短期动量强相关，需搭配 guardrail）
- **使用位置**：technical 维度默认打分
- **源文件**：`factors/technical/rebuilt.py`

### factor_technical_nonlinear

- **依赖列**：`vol_ratio_proxy`, `turnover_rate`, `pullback_10d`, `close/ma20`
- **IC hit_limit_3**：0.220
- **使用位置**：mine 备用
- **源文件**：`factors/technical/nonlinear.py`

### factor_pullback_quality

- **依赖列**：`pullback_10d`, `vol_ratio_proxy`
- **IC hit_limit_3**：-0.03
- **使用位置**：technical 组件（反追高辅助）
- **源文件**：`factors/technical/pullback.py`
- **注**：单变量 IC 弱，作为组合成分保留。

### factor_pullback_from_peak

- **依赖列**：`pullback_20d`
- **IC hit_limit_3**：0.036
- **使用位置**：technical 组件
- **源文件**：`factors/technical/pullback.py`

### factor_position_optimal

- **依赖列**：`position_20d`
- **IC hit_limit_3**：0.006
- **使用位置**：technical 组件
- **源文件**：`factors/technical/pullback.py`

### factor_vol_expansion_quality

- **依赖列**：`vol_ratio_proxy`, `pct_chg_score_day`, `pullback_10d`
- **IC hit_limit_3**：-0.017
- **使用位置**：mine 备用（辨识吸筹放量 vs 出货放量，量化 IC 差）
- **源文件**：`factors/technical/volume.py`

### factor_amount_acceleration

- **依赖列**：`amount_3d_increasing`, `pct_chg_score_day`, `vol_ratio_proxy`
- **IC hit_limit_3**：0.056
- **使用位置**：mine 备用
- **源文件**：`factors/technical/volume.py`

### factor_amount_surge

- **依赖列**：`amount_ratio`, `pct_chg_score_day`
- **IC hit_limit_3**：0.015
- **使用位置**：mine 备用
- **源文件**：`factors/technical/volume.py`

### factor_reversal_signal

- **依赖列**：K 线形态（当日开盘/收盘/最高/最低）
- **IC hit_limit_3**：-0.0004
- **使用位置**：mine 备用
- **源文件**：`factors/technical/pattern.py`

### factor_gap_up_quality

- **依赖列**：`open`, `pre_close`, `vol_ratio_proxy`
- **IC hit_limit_3**：-0.022
- **使用位置**：mine 备用
- **源文件**：`factors/technical/pattern.py`

### factor_consecutive_strength

- **依赖列**：`limit_up_count_5d`, `pct_chg_score_day`
- **IC hit_limit_3**：-0.004
- **使用位置**：mine 备用
- **源文件**：`factors/technical/pattern.py`

### factor_breakout_quality

- **依赖列**：`position_20d`, `vol_ratio_proxy`, `amount_ratio`
- **IC hit_limit_3**：0.085
- **使用位置**：technical 组件
- **源文件**：`factors/technical/breakout.py`

---

## 维度：fundflow

资金面维度默认打分函数：`strategies/fundflow.py::score_fundflow`。

> 基线 `fundflow` IC=-0.11，被 `fundflow_rebuilt` 替代作为维度默认打分。

### factor_fundflow_rebuilt

- **签名**：`factor_fundflow_rebuilt(row) -> float`
- **依赖列**：`net_mf_amount`, `mid_mf_amount`, `retail_amount`, `elg_amount`
- **PIT**：是（走 jvQuant fundflow_single）
- **IC hit_limit_3**：0.201
- **使用位置**：fundflow 维度默认打分
- **源文件**：`factors/fundflow/rebuilt.py`

### factor_net_mf_signal / factor_elg_inflow_signal

- **IC hit_limit_3**：NaN（面板未触发）
- **使用位置**：mine 备用
- **源文件**：`factors/fundflow/signals.py`
- **注**：面板列（`net_mf_amount / elg_amount`）待补。

---

## 维度：sentiment

情绪面维度默认打分函数：`strategies/sentiment.py::score_sentiment`。

> 基线 `sentiment` 是当前**最强单变量**，IC=0.338。`total_score` 三个组件全部来自本维度。

### factor_sentiment_amount_boosted  🟢 total_score 组件 A

- **签名**：`factor_sentiment_amount_boosted(row) -> float`
- **依赖列**：`sentiment_score`, `amount_ratio`, `turnover_rate`
- **PIT**：是
- **IC hit_limit_3**：0.333
- **chasing_score**：0.175
- **使用位置**：`total_score` (weight 0.4)
- **源文件**：`factors/sentiment/amount_boosted.py`

### factor_sentiment_position_combo  🟢 total_score 组件 B

- **签名**：`factor_sentiment_position_combo(row) -> float`
- **依赖列**：`sentiment_score`, `position_20d`, `pullback_10d`
- **PIT**：是
- **IC hit_limit_3**：0.271
- **chasing_score**：0.091（最优）
- **使用位置**：`total_score` (weight 0.5)
- **源文件**：`factors/sentiment/position_combo.py`

### factor_sentiment_volatility_combo  🟢 total_score 组件 C

- **签名**：`factor_sentiment_volatility_combo(row) -> float`
- **依赖列**：`sentiment_score`, `volatility_20d`, `amplitude_5d`
- **PIT**：是
- **IC hit_limit_3**：0.279
- **chasing_score**：0.161
- **使用位置**：`total_score` (weight 0.7)
- **源文件**：`factors/sentiment/volatility_combo.py`

### factor_sentiment_amount_combo

- **依赖列**：`sentiment_score`, `amount_ratio`, `pct_chg_score_day`
- **IC hit_limit_3**：0.278
- **使用位置**：mine 备用
- **源文件**：`factors/sentiment/amount_combo.py`

### factor_sentiment_ensemble

- **依赖列**：多因子集成（`sentiment` + `position` + `amount`）
- **IC hit_limit_3**：0.281
- **使用位置**：mine 备用
- **源文件**：`factors/sentiment/ensemble.py`

### factor_sentiment_pure_boosted

- **依赖列**：`sentiment_score`, `pct_chg_score_day`
- **IC hit_limit_3**：0.233
- **chasing_score**：-0.214（极强反追高）
- **使用位置**：mine 备用
- **源文件**：`factors/sentiment/pure_boosted.py`

---

## 维度：shortterm

短线博弈维度默认打分函数：`strategies/shortterm.py::score_shortterm`。

### factor_limit_up_gene_20d / _60d / _composite

- **依赖列**：`limit_up_count_20d`, `limit_up_count_60d`
- **PIT**：是
- **IC hit_limit_3**：0.206 / 0.212 / 0.213
- **使用位置**：shortterm 组件
- **源文件**：`factors/shortterm/limit_gene.py`

### factor_limit_gene_amount

- **依赖列**：`limit_up_count_20d`, `amount_ratio`
- **IC hit_limit_3**：0.233
- **使用位置**：shortterm 组件
- **源文件**：`factors/shortterm/limit_gene_amount.py`

### factor_limit_gene_momentum

- **依赖列**：`limit_up_count_20d`, `pct_chg_score_5d`
- **IC hit_limit_3**：0.182
- **使用位置**：shortterm 组件
- **源文件**：`factors/shortterm/limit_gene_momentum.py`

### factor_trailing_momentum

- **依赖列**：`trailing_10`, `trailing_5`
- **IC hit_limit_3**：0.024
- **使用位置**：shortterm 组件
- **源文件**：`factors/shortterm/trailing.py`

### factor_intraday_strength

- **依赖列**：`pct_chg_score_day`, `open/pre_close`, `close/high`
- **IC hit_limit_3**：-0.003
- **使用位置**：mine 备用
- **源文件**：`factors/shortterm/intraday.py`

### factor_turnover_momentum

- **依赖列**：`turnover_rate`, `turnover_rate_5d_avg`
- **IC hit_limit_3**：0.120
- **使用位置**：shortterm 组件
- **源文件**：`factors/shortterm/turnover.py`

### factor_growth_momentum

- **依赖列**：`pct_chg_score_5d`, `pct_chg_score_20d`
- **IC hit_limit_3**：0.053
- **使用位置**：shortterm 组件
- **源文件**：`factors/shortterm/growth.py`

### factor_chasing_guardrail

- **签名**：`factor_chasing_guardrail(row) -> float`（返回乘性惩罚系数 0.75~1.0）
- **依赖列**：`trailing_10`, `trailing_5`, `position_20d`, `pullback_10d`, `sentiment_score`
- **使用位置**：**内部使用**，供 `technical_rebuilt` / `shortterm` 内部叠加，不入 total_score
- **源文件**：`factors/shortterm/guardrail.py`

---

## 跨维度组合（仅供 mine 使用）

### factor_dimension_divergence

- **依赖列**：五维度分数
- **IC hit_limit_3**：0.012
- **使用位置**：mine 备用
- **源文件**：`factors/crossdim/divergence.py`

### factor_total_quality_bonus

- **依赖列**：五维度分数
- **IC hit_limit_3**：-0.013
- **使用位置**：mine 备用
- **源文件**：`factors/crossdim/quality_bonus.py`

---

## 总分

### total_score  🟢 唯一生产总分

- **签名**：`total_score(row: dict) -> float`
- **公式**：`round(max(0, 0.4*A + 0.5*B + 0.7*C), 2)`，其中 A/B/C 依次为 `sentiment_amount_boosted / sentiment_position_combo / sentiment_volatility_combo`
- **IC hit_limit_3**：0.338（等价原 `ultimate_total_v5`）
- **使用位置**：pipeline 唯一排序键
- **源文件**：`plays/limit_up/total.py`

---

## 废弃因子清单

以下因子在本次重构中删除（不迁移到 `factors/`），生产代码与文档中不再出现：

### 版本化复合总分（全删）

`factor_new_total_v2`, `factor_balanced_total`, `factor_balanced_total_pit`, `factor_balanced_adaptive_total_pit`, `factor_balanced_total_pit_v2`, `factor_aggressive_total`, `factor_return_optimized_total`, `factor_quality_value_total`, `factor_sentiment_adaptive_total_pit`, `factor_sentiment_conditional_pit`, `factor_ultimate_total_v1`, `factor_ultimate_total_v2`, `factor_ultimate_total_v3`, `factor_ultimate_total_v4`, `factor_ultimate_total_v5`（其逻辑成为 `total_score`）, `factor_new_total_mined_v2`, `factor_deep_total_v5`, `factor_cpt_amount_percentile`, `factor_balanced_ensemble`。

### CPT / 概念系列（cpt_*）

`factor_cpt_amount_combo`, `factor_cpt_amount_anti_chase`, `factor_cpt_streak_turn`, `factor_cpt_streak_turn_combo`, `factor_cpt_turn_high_turn`, `factor_cpt_turn_pullback`, `factor_activity_rank_combo`, `factor_quality_momentum`, `factor_mv_turn_cpt`, `factor_residual_shortterm`, `factor_residual_technical`, `factor_activity_combo`, `factor_large_cap_growth_combo`。

### 负 IC 单因子（全删）

`factor_volatility_contraction` (-0.045), `factor_large_amplitude_risk` (-0.055), `factor_upper_shadow_risk` (0.003 但方向可疑), `factor_sentiment_contrarian` (-0.044), `factor_low_sentiment_contrarian_pit` (NaN), `factor_low_amplitude_breakout` (-0.009), `factor_amount_power_pit` (0.134 但过拟合迹象), `factor_volatility_activation_pit` (0.194 但 chasing 0.47), `factor_large_cap_limit_gene_pit` (0.216 但过拟合), `factor_momentum_amount_combo_pit` (0.210 但 chasing 0.56)。

### anti-chasing 派生变体（全删）

`factor_technical_anti_chasing`, `factor_shortterm_anti_chasing`, `factor_sentiment_anti_chasing`（原效果与主因子近乎相同，冗余）。

### 概念 / 龙虎榜（暂存 _deprecated/）

`factor_concept_momentum`, `factor_concept_up_streak`, `factor_concept_turnover`, `factor_concept_turn_5d_max`, `factor_concept_heat_combo`, `factor_concept_activity_combo`, `factor_inst_following`, `factor_top_list_quality`, `factor_inst_consistency`。面板缺列（IC=NaN），暂时移入 `factors/_deprecated/`，等 `enrich_panel` 补齐后再重启。

### rebuilt 的 v2 变体（若存在则删）

`factor_fundamental_rebuilt_v2`, `factor_fundflow_rebuilt_v2`, `factor_technical_rebuilt_v2` 与 v1 无显著提升，直接删。

---

## 修改流程（Contract for future changes）

当发现新因子或调整现有因子时：

1. 在对应 `factors/<dim>/*.py` 增删函数；
2. 更新本文件的对应小节（**同一提交中**，文档与代码同步）；
3. 增补 `tests/factors/test_<factor>.py` 真实调用单测；
4. 跑 `python plays/limit_up/backtest/validate.py --factor <name>`，把新 IC 记录到本文档。

**红线**：本文件是因子清单的唯一事实。生产代码不允许出现未在本文档登记的因子函数。
