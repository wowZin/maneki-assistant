# 评分体系 — XGBoost 模型分为主

> 2026-07-26 现状：生产环境的唯一评分是 **XGBoost 模型分（`model_score`，0–100 连续分）**。
> 五维度分（fundamental/technical/fundflow/sentiment/shortterm）已降级为模型 64 特征中的 5 个普通特征，不再用于选股展示。
> 面板（`wiki/raw/limit-up/panel/{date}.parquet`）是评分的单一事实源。

## 一、生产评分链路

```
panel_builder（00:01）  全市场主板面板：64 特征 + 五维度分列 + 预评 model_score（≥35 写 analysis）
      ↓
pipeline（09:30，一次性） 竞价刷新 auc_* → factor_model_score_batch 全量评分 → model_score 写回面板
      ↓
surge_scanner（09:35 起，60s） 主闸直接读面板 model_score ≥ 20
      ↓
watchdog（09:20 起，60s） realtime_row（日级特征 + 面板维度分 + L1）→ 同一模型逐只打分，入场闸 min_model_score=40
```

各环节用**同一个模型文件**，输入行分别为面板行（批量）和实时行（逐只）。

## 二、模型分实现

**入口**：
- 批量：`plays/limit_up/factors/optimized/model_score.py::factor_model_score_batch(df)`（pipeline / panel_builder 用）
- 逐行：`factor_model_score(row)`（watchdog 用）

**模型**：
- 文件：`plays/limit_up/data/backtest/models/limit_up_model.joblib`（`LimitUpModel`，XGBoost；环境变量 `LIMIT_UP_MODEL_PATH` 可覆盖目录）
- 特征：`backtest/model.py::DEFAULT_FEATURES`，共 **64 个**（`models/model_features.json` 同步维护）：
  - 价格位置/动量（`position_20d`、`trailing_5/10`、`max_step`、`was_limit` 等）
  - 波动与量价（`pct_chg_std_*`、`turnover_rate`、`volume_ratio`、`vol_accel` 等）
  - 市值估值（`circ_mv`、`cmv_yi`、`pe`、`pb`）
  - 资金流（`net_mf_amount`、`mf_accel`、`buy_elg_ratio` 等）
  - K 线形态（`close_pos`、`body_ratio`、`upper_ratio` 等）
  - 板块/概念（`sector_heat`、`sector_rank`、`n_concepts`）
  - 集合竞价（`auc_amount`、`auc_vol`、`auc_amt_ratio`、`auc_vol_ratio`）
  - 日内分时 T-1（`id_vwap_dev`、`id_range`、`id_morning_vol_ratio` 等）
  - 龙虎榜 PIT（`dt_is_listed`、`dt_net_amount`、`dt_inst_net_buy` 等）
  - **五维度分（`fundamental`、`technical`、`fundflow`、`sentiment`、`shortterm`）— 64 特征中的最后 5 个**
- 双头训练：`hit_limit_3`（命中率头）+ `fwd_ret_3 > 0`（胜率头），默认 0.6/0.4 混合为 0–100 分（`blend_hit=0.6, blend_win=0.4`）。
- 缺失值用训练时中位数填充。

**追高护栏**（乘性，`factor_model_score_batch` 向量化版本）：

| 条件 | 惩罚 |
|------|------|
| `trailing_10 > 0.30 / 0.20 / 0.10` | ×0.80 / ×0.90 / ×0.95 |
| `position_20d > 0.85` 且 `pullback_10d < 0.03` | ×0.85 |
| `trailing_5 > 0.15` | ×0.92 |
| 深跌+低位+有量有资金承接（`trailing_10 < -0.05`、`position_20d < 0.30`、`volume_ratio ≥ 1.0`、`net_mf_amount > 0`） | ×1.05 |

**回退**：模型文件缺失/加载失败/预测异常时，自动回退到 `factor_quality_combo`（硬规则 0/95/100 分档，见 [factors.md](./factors.md)）。

## 三、五维度分的当前角色

五维度分由 `panel_builder._add_strategy_scores` 在夜间从缓存数据计算（不调 Tushare），作为面板列存储：

| 维度 | 主要输入 | 备注 |
|------|----------|------|
| `fundamental` | 流通市值/pe/pb/扣非增速/概念数 | |
| `technical` | close_pos/振幅/量比/换手/5日收阳数 | base 15 |
| `fundflow` | 主力净额占流通市值比 | base 20 |
| `sentiment` | 概念涨停热度 | base 15 |
| `shortterm` | `auc_amt_ratio` 分档 | base 10；**唯一吃竞价数据的维度**，pipeline 09:30 竞价刷新时同步重算 |

角色定位：

1. **模型特征**：是 64 特征中的 5 个普通特征，单个特征重要性各约 1.4%（`models/feature_importance.json`：shortterm 1.48%、sentiment 1.46%、fundflow 1.41%、fundamental 1.38%、technical 1.36%）。
2. **不再用于选股展示**：推送卡片只展示 code/name/星级/涨幅/总分，不展示维度分。
3. **watchdog 实时行的输入**：surge_scanner 写 watchdog state 时必须带面板 `dim_scores`，否则 `realtime_row` 的五维度分=0，模型分被系统性压低。

## 四、面板 = 单一事实源

`wiki/raw/limit-up/panel/{date}.parquet` 一日三态：

| 时点 | 写入者 | 内容 |
|------|--------|------|
| 00:01 | panel_builder | T-1 的 64 特征 + 五维度分 + 预评 `model_score`（≥35 的写 `plays/limit_up/data/analysis/{date}.json`） |
| 09:30 | pipeline | 刷新 `auc_*` + 重算 `shortterm` + 全量重评 `model_score` 写回（终态） |
| 09:35–15:00 | surge_scanner 读取 | 主闸池 = 面板 `model_score ≥ 20` |

## 五、推送规则（与模型分配套）

- 排序键：`model_score` 降序（记录中 `total_score` 字段 = `model_score`，`score_mode="model_score"`）。
- 推送策略：**Top-N 相对标准（`PUSH_TOP_N`，默认 3）+ ≥55 绝对地板（`ULTIMATE_PUSH_THRESHOLD`）**，竞价涨幅 ≥9.8%（已封板）不推。
- 降噪：同一只股票评分提高 >0.5 才重推（`pipeline_feishu.push_feishu`）。

## 六、兼容层 `total.py`

`plays/limit_up/total.py::total_score(row)` 保留为对外兼容入口，按 `TOTAL_SCORE_COMPONENTS` 加权（`LIMIT_UP_USE_MODEL=true` 时为 `factor_model_score`，否则 `quality_combo`）。生产 pipeline 不经过它，直接调 `factor_model_score_batch`。

## 七、历史版本

`new_total_v2 / balanced_total / ultimate_total_v1~v5 / cpt_* / balanced_ensemble` 等全部废弃；`quality_combo`/`quality_gate` 保留在因子库作为模型回退与 watchdog 辅助筛选（`is_worth_watching`），不再是总分。
