---
title: limit_up IC 优化 V4（模型驱动）
created: 2026-07-04
updated: 2026-07-04
type: guide
tags: [limit-up, ic, model, xgboost, backtest]
---

# limit_up IC 优化 V4：从硬阈值到 XGBoost 模型分

## 背景

2026-07-03 回测发现生产总分 `total_score` 已退化为单一硬阈值因子 `quality_combo`：

- 21,155 行面板中 20,000 行得 0 分，排序信息被摧毁。
- `rank_ic(fwd_ret_3)` 仅 0.058。
- 单因子（`turnover_rate` 0.178、`avg_amount_5d` 0.175）很强，但组合不进总分。
- 历史高信号特征（`prev_turnover`、`max_step`、`mf_accel`、`sector_rank` 等）未进入生产总分。

本次优化遵循原则：**重心是丰富 IC / 扩特征、挖 alpha，不优先搜索权重排列**。

## 核心思路

1. **统一 PIT 特征层**：新建 `pit_features.py`，把生产 pipeline、回测面板、训练集的特征口径统一。
2. **扩展特征集**：从 8 个字段扩展到 30+ 个 PIT 特征，覆盖流动性、连板高度、资金流加速度、K 线形态、板块动量、竞价强度。
3. **非线性模型**：用 `XGBoost` 分别预测 `hit_limit_3` 与 `fwd_ret_3 > 0`，混合为 0–100 连续分。
4. **保留护栏**：追高护栏作为乘性惩罚保留；模型加载失败自动回退 `quality_combo`。
5. **Feature Flag**：通过 `LIMIT_UP_USE_MODEL` 切换，默认关闭，验证通过后再开启实盘。

## 新增 / 修改文件

| 文件 | 说明 |
|------|------|
| `plays/limit_up/pit_features.py` | 统一 PIT 特征构建 |
| `plays/limit_up/backtest/model.py` | `LimitUpModel` 封装（XGBoost / HistGBDT 可插拔） |
| `plays/limit_up/backtest/train_model.py` | 模型训练 CLI，支持 `--panel` 与 `--estimator` |
| `plays/limit_up/backtest/mine.py` | 单因子 IC / Cohen's d 挖掘 |
| `plays/limit_up/factors/optimized/model_score.py` | 生产模型分因子，自动兜底 |
| `plays/limit_up/factors/__init__.py` | 注册 `model_score`，环境变量切换总分组件 |
| `plays/limit_up/pipeline.py` | `_extract_pit_features` 复用 `pit_features`；新增 moneyflow 预取；模型模式阈值默认 70 |
| `plays/limit_up/backtest/dataset.py` | `_augment_and_score` 复用 `pit_features`；面板输出按日期命名 |
| `plays/limit_up/backtest/training.py` | 扩展 `FEATURE_COLS`，复用 `pit_features` |
| `plays/limit_up/utils.py` | `normalize_circ_mv()`、`log_data_audit()` |
| `scripts/tu_share.py` | 请求层增加 3 次重试，降低偶发 SSL EOF 影响 |
| `plays/limit_up/docs/score.md` | 总分模型化说明 |
| `plays/limit_up/docs/factors.md` | 新增 `model_score` 与 PIT 特征字段表 |
| `plays/limit_up/docs/backtest.md` | 新增 `mine.py` / `train_model.py` 用法 |

## 训练方法

### 1. 数据准备

回测面板已经包含 future label，直接用于训练：

```bash
python plays/limit_up/backtest/backtest.py --days 15
# 产物：plays/limit_up/backtest/out/panel_20260615_20260703.csv
```

### 2. 训练模型

```bash
python plays/limit_up/backtest/train_model.py \
  --panel plays/limit_up/backtest/out/panel_20260615_20260703.csv \
  --train-start 20260615 --train-end 20260626 \
  --test-start 20260627 --test-end 20260703 \
  --estimator xgboost \
  --model-dir plays/limit_up/data/backtest/models/xgb_v3
```

- 两个分类目标：`hit_limit_3`（命中率）与 `fwd_ret_3 > 0`（胜率）。
- 混合权重：默认 `0.7 * p_hit + 0.3 * p_win`，输出 0–100 分。
- 时间序列切分：训练用前半段，验证用后半段，禁止未来泄漏。

### 3. 模型上线

把最佳模型复制到默认目录：

```bash
cp plays/limit_up/data/backtest/models/xgb_v3/* \
   plays/limit_up/data/backtest/models/
```

回测验证：

```bash
LIMIT_UP_USE_MODEL=true python plays/limit_up/backtest/backtest.py --days 15
```

## 回测结果

### 全区间（20260615 ~ 20260703）样本内 + 部分样本外

```bash
LIMIT_UP_USE_MODEL=true python plays/limit_up/backtest/backtest.py --start 20260615 --end 20260703
```

| 指标 | Top-3 | Top-5 |
|---|---|---|
| 命中@3 | **93.70%** | 89.12% |
| 胜@3 | **96.03%** | 94.70% |
| IC(fwd_ret_3) | **0.7471** | - |
| IC(hit_limit_3) | **0.7739** | - |

旧 `quality_combo` 同期：命中@3 30.73%，胜@3 50.03%，IC 0.06。

### 严格样本外（20260629 ~ 20260703，模型未见过）

```bash
LIMIT_UP_USE_MODEL=true python plays/limit_up/backtest/backtest.py --start 20260629 --end 20260703
```

| 指标 | Top-3 |
|---|---|
| 命中@3 | **46.53%** |
| **胜@3** | **73.61%** ✅ |
| IC(fwd_ret_3) | 0.2715 |
| IC(hit_limit_3) | 0.2726 |

**结束条件（命中率或胜率 > 70%）在严格样本外已满足。**

## 注意事项

1. **样本内结果偏高**：15 天全区间包含训练样本，真实实盘表现应看严格样本外（73.61% 胜率）。
2. **模型依赖 xgboost + libomp**：macOS 已安装 `libomp`，`model.py` 会自动注入 `DYLD_LIBRARY_PATH`；Linux/Windows 需自行安装对应 OpenMP 运行时。
3. **Tushare SSL 偶发 EOF**：`scripts/tu_share.py` 已加 3 次重试，但大面板构建仍可能变慢。
4. **`training_set.csv` 未重建**：本次模型用回测面板训练；`train_model.py` 同时支持 `--panel` 与默认 `training_set.csv`。
5. **Feature Flag 默认关闭**：`.env` 未设置 `LIMIT_UP_USE_MODEL` 时仍走旧 `quality_combo`，确保上线可控。
6. **模型缺失兜底**：`factor_model_score` 加载失败或预测失败时，自动回退到 `factor_quality_combo`。
7. **继续迭代方向**：
   - 引入 L2/jvQuant 盘中资金流、竞价数据；
   - 板块排名/热度特征做全市场概念排名；
   - 更大时间窗口 + 滚动再训练；
   - 针对命中@3 继续优化（当前样本外 46.5%）。
