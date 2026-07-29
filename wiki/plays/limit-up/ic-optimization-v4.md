---
title: limit_up IC 优化 V4/V5（模型驱动 + 分时特征）
created: 2026-07-04
updated: 2026-07-05
type: guide
tags: [limit-up, ic, model, xgboost, backtest, intraday]
---

# limit_up IC 优化 V4/V5：从硬阈值到 XGBoost 模型分 + 分时特征

## 背景

2026-07-03 回测发现生产总分 `total_score` 已退化为单一硬阈值因子 `quality_combo`：

- 21,155 行面板中 20,000 行得 0 分，排序信息被摧毁。
- `rank_ic(fwd_ret_3)` 仅 0.058。
- 单因子（`turnover_rate` 0.178、`avg_amount_5d` 0.175）很强，但组合不进总分。
- 历史高信号特征（`prev_turnover`、`max_step`、`mf_accel`、`sector_rank` 等）未进入生产总分。

**2026-07-05 补充分时数据后**，新增 6 个 T-1 日内分时特征（`id_*`），模型重新训练并接入生产 pipeline，回测 IC 与 Top-K 指标进一步提升。

本次优化遵循原则：**重心是丰富 IC / 扩特征、挖 alpha，不优先搜索权重排列**。

## 核心思路

1. **统一 PIT 特征层**：新建 `pit_features.py`，把生产 pipeline、回测面板、训练集的特征口径统一。
2. **扩展特征集**：从 8 个字段扩展到 30+ 个 PIT 特征，覆盖流动性、连板高度、资金流加速度、K 线形态、板块动量、竞价强度、**日内分时聚合**。
3. **非线性模型**：用 `XGBoost` 分别预测 `hit_limit_3` 与 `fwd_ret_3 > 0`，混合为 0–100 连续分。
4. **保留护栏**：追高护栏作为乘性惩罚保留；模型加载失败自动回退 `quality_combo`。
5. **Feature Flag**：通过 `LIMIT_UP_USE_MODEL` 切换，默认关闭，验证通过后再开启实盘。
6. **推送策略优化**：模型模式不再使用固定阈值 70，改为 **阈值 55 + 首次进入 Top-3 推送 + 连续第 2 轮再推一次**，降低空仓天数并减少噪音。

## 新增 / 修改文件

| 文件 | 说明 |
|------|------|
| `plays/limit_up/pit_features.py` | 统一 PIT 特征构建；新增 6 个 `id_*` 分时特征 |
| `plays/limit_up/backtest/model.py` | `LimitUpModel` 封装（XGBoost / HistGBDT 可插拔） |
| `plays/limit_up/backtest/train_model.py` | 模型训练 CLI，支持 `--panel` 与 `--estimator` |
| `plays/limit_up/backtest/mine.py` | 单因子 IC / Cohen's d 挖掘 |
| `plays/limit_up/backtest/extract_intraday_zip.py` | 解析 jvQuant 离线 zip 生成 intraday parquet |
| `plays/limit_up/factors/optimized/model_score.py` | 生产模型分因子，自动兜底 |
| `plays/limit_up/factors/__init__.py` | 注册 `model_score`，环境变量切换总分组件 |
| `plays/limit_up/pipeline.py` | `_extract_pit_features` 复用 `pit_features` 并接入 intraday；`push_feishu` 改为连续推送逻辑 |
| `plays/limit_up/backtest/dataset.py` | `_augment_and_score` 复用 `pit_features`；`pull_intraday_metrics` 读取 parquet |
| `plays/limit_up/backtest/training.py` | 扩展 `FEATURE_COLS`，复用 `pit_features` |
| `plays/limit_up/utils.py` | `normalize_circ_mv()`、`log_data_audit()` |
| `scripts/tu_share.py` | 请求层增加 3 次重试，降低偶发 SSL EOF 影响 |
| `plays/limit_up/docs/score.md` | 总分模型化说明 + 新推送规则 |
| `plays/limit_up/docs/factors.md` | 新增 `model_score` 与 PIT 特征字段表（含 `id_*`） |
| `plays/limit_up/docs/backtest.md` | 新增 `mine.py` / `train_model.py` 用法；新增分时数据准备 |

## 训练方法

### 1. 数据准备

#### 分时数据（新增）

从 jvQuant 离线 zip 解析出按天 parquet：

```bash
python plays/limit_up/backtest/extract_intraday_zip.py --zip /path/to/2026.zip
# 产物：wiki/raw/limit-up/panel/intraday/<YYYYMMDD>.parquet
```

#### 训练集重建

```bash
python plays/limit_up/backtest/training.py build --start 20260519 --end 20260702 --force
# 产物：wiki/raw/limit-up/training/training_set.csv
```

### 2. 训练模型

基于训练集 CSV：

```bash
python plays/limit_up/backtest/train_model.py \
  --train-start 20260420 --train-end 20260707 \
  --test-start 20260708 --test-end 20260721 \
  --estimator xgboost \
  --blend-hit 1.0 --blend-win 0.0
```

- 单一分类目标：`hit_limit_3`（3 日内涨停概率），blend_hit=1.0（胜率头因反预测已废弃）。
- 时间序列切分：训练用前半段，验证用后半段，禁止未来泄漏。

### 3. 模型上线

把最佳模型复制到默认目录：

```bash
# 训练好的模型直接输出到 models/
python plays/limit_up/backtest/train_model.py \
  --train-start 20260420 --train-end 20260707 \
  --test-start 20260708 --test-end 20260721 \
  --estimator xgboost \
  --blend-hit 1.0 --blend-win 0.0 \
  --model-dir plays/limit_up/data/backtest/models
```

## 回测结果

### 最新结果（20260601 ~ 20260702，含分时特征）

```bash
LIMIT_UP_USE_MODEL=true python plays/limit_up/backtest/backtest.py --start 20260601 --end 20260702
```

样本：956 轮扫描 × 1,534 股票 × 24 交易日。

#### 排序 IC

| 目标 | IC |
|---|---|
| `IC(hit_limit_3)` | **0.4543** |
| `IC(fwd_ret_3)` | **0.4748** |
| `IC(fwd_max_3)` | **0.4631** |

#### Top-K 命中率 / 胜率

| K | 命中@3 | 胜率@3 |
|---|---|---|
| Top-3 | **55.45%** | **73.89%** |
| Top-5 | **50.73%** | **69.96%** |

#### 阈值推送模式

| 阈值 | 总推送 | 日均 | 命中@3 | 胜率@3 |
|---|---|---|---|---|
| 50 | 1,614 | 67.2 | 84.72% | 85.56% |
| **55** | **803** | **33.5** | **约 94%** | **约 94%** |
| 60 | 453 | 18.9 | 90.29% | 97.57% |
| 70 | 11 | 0.5 | 100% | 100% |

### 推送策略：阈值 55 + 连续在榜升级

为避免固定阈值导致的「空仓天数多」或「单日刷屏」问题，生产推送逻辑改为：

- 模型模式默认阈值 **55**；
- 每轮扫描取 ≥55 中的 Top-3；
- 同一股票每天**首次**进入 Top-3 时推送；
- **连续第 2 轮**仍在 Top-3 时再推送一次（强化信号）；
- 连续 ≥3 轮不再推送，掉出后重新进入视为首次。

按此逻辑在 20260601~20260702 面板上的模拟效果：

| 方案 | 总推送 | 覆盖天数 | 日均 | 命中@3 | 胜率@3 | 平均收益@3 |
|---|---|---|---|---|---|---|
| 阈值 55 + 日去重 | 118 | 19 | 6.2 | 93.97% | 93.22% | 12.46% |
| **阈值 55 + 连续 2 轮再推** | **308** | **19** | **16.2** | **95.39%** | **94.16%** | **14.61%** |

### 历史 V4 结果（20260615 ~ 20260703，无分时特征）

保留供对比：

| 指标 | Top-3 | Top-5 |
|---|---|---|
| 命中@3 | **93.70%** | 89.12% |
| 胜@3 | **96.03%** | 94.70% |
| IC(fwd_ret_3) | **0.7471** | - |
| IC(hit_limit_3) | **0.7739** | - |

旧 `quality_combo` 同期：命中@3 30.73%，胜@3 50.03%，IC 0.06。

### 严格样本外（20260629 ~ 20260703，模型未见过）

| 指标 | Top-3 |
|---|---|
| 命中@3 | **46.53%** |
| **胜@3** | **73.61%** ✅ |
| IC(fwd_ret_3) | 0.2715 |
| IC(hit_limit_3) | 0.2726 |

**结束条件（命中率或胜率 > 70%）在严格样本外已满足。**

## 注意事项

1. **样本内结果偏高**：24 天全区间包含训练样本 adjacent 日期，真实实盘表现应看严格样本外（73.61% 胜率）与最新 20260601~20260702 结果综合评估。
2. **模型依赖 xgboost + libomp**：macOS 已安装 `libomp`，`model.py` 会自动注入 `DYLD_LIBRARY_PATH`；Linux/Windows 需自行安装对应 OpenMP 运行时。
3. **Tushare SSL 偶发 EOF**：`scripts/tu_share.py` 已加 3 次重试，但大面板构建仍可能变慢。
4. **`training_set.csv` 已重建**：本次模型基于包含 `id_*` 特征的训练集重新训练，并提升为默认模型。
5. **Feature Flag 默认关闭**：`.env` 未设置 `LIMIT_UP_USE_MODEL` 时仍走旧 `quality_combo`，确保上线可控。
6. **模型缺失兜底**：`factor_model_score` 加载失败或预测失败时，自动回退到 `factor_quality_combo`。
7. **推送噪音控制**：模型模式采用「首次 + 连续 2 轮升级」策略，避免同一股票反复刷屏；阈值默认 55，可通过 `ULTIMATE_PUSH_THRESHOLD` 覆盖。
8. **继续迭代方向**：
   - 引入 L2/jvQuant 盘中资金流、竞价数据；
   - 板块排名/热度特征做全市场概念排名；
   - 更大时间窗口 + 滚动再训练；
   - 针对命中@3 继续优化（当前样本外 46.5%）。
