# 已废弃因子归档

以下因子面板缺列（IC=NaN）或效果不达标，暂存于此目录，等 enrich_panel 补齐所需字段后再评估重启。

## 概念 / 龙虎榜（面板缺列）
- factor_concept_momentum / concept_up_streak / concept_turnover / concept_turn_5d_max / concept_heat_combo / concept_activity_combo
- factor_inst_following / top_list_quality / inst_consistency

## 触发方式
待 backtest/data.py::build_panel 补齐 `cpt_*`, `inst_*` 列后，重新验证 IC 决定是否迁入 factors/<dim>/。
