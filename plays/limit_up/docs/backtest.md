# 回测三件套

> 打板玩法的回测/挖掘/优化工具收敛为三个文件，替换原 20+ 脚本的散乱状态。

## 目录

```
plays/limit_up/backtest/
├── dataset.py       # 面板加载/切片/缓存
├── labels.py        # 标签生成（hit_limit_N / fwd_ret_N / fwd_max_N）
├── metrics.py       # IC / hit@K / chasing_score / sharpe
├── mine.py          # 因子挖掘（单因子 IC / Cohen's d）
├── validate.py      # 单因子 / total_score 的 IC & hit 报告
├── optimize.py      # 权重优化（total_score 组件权重 + 维度权重）
├── model.py         # 树模型封装（HistGradientBoostingClassifier）
├── train_model.py   # 模型训练 CLI
├── cache/           # .gitignore
└── out/             # .gitignore
```

## 三件套职责

### mine.py

因子挖掘：在训练集上计算每个特征对目标标签的 Rank IC、Pearson IC 与 Cohen's d。

```bash
python plays/limit_up/backtest/mine.py --label hit_limit_3
python plays/limit_up/backtest/mine.py --label fwd_ret_3 --top 20
```

产物：`backtest/out/mine_<label>.csv`。

### validate.py

给定一个已注册的因子名（`factors/__init__.py::REGISTRY` 中的 key），在训练集上计算 RankIC / hit@10 / hit@20 / chasing_score。

### optimize.py

网格 / 贝叶斯搜索 `total_score` 三个组件的权重（A/B/C）与维度权重 `AGENT_WEIGHTS`。

### train_model.py

训练树模型，分别预测 3 日涨停概率（`hit_limit_3`）与 3 日正收益概率（`fwd_ret_3 > 0`），混合输出连续 model score。

```bash
python plays/limit_up/backtest/train_model.py \
    --train-start 20260519 --train-end 20260620 \
    --test-start 20260621 --test-end 20260702 \
    --estimator xgboost
```

支持 `--estimator xgboost` 或 `--estimator hist`。当前默认模型使用 XGBoost；训练前需确认训练集已包含最新特征（含日内 `id_*` 字段），否则新特征重要性为 0。

产物：`plays/limit_up/data/backtest/models/`：
- `limit_up_model.joblib`
- `model_features.json`
- `feature_importance.json`
- `validation_report.json`

训练完成后，设置 `LIMIT_UP_USE_MODEL=true` 即可在回测/生产中使用模型分。

## 分时数据准备（新增）

模型中的日内特征（`id_vwap_dev`、`id_range`、`id_morning_vol_ratio` 等）依赖 jvQuant 历史分时数据。离线数据以 zip 包形式提供，需先解析为按天 parquet：

```bash
python plays/limit_up/backtest/extract_intraday_zip.py --zip /path/to/2026.zip
```

产物：`wiki/raw/limit-up/panel/intraday/<YYYYMMDD>.parquet`。

`dataset.py::pull_intraday_metrics(codes, [pit_date])` 会优先读取本地 parquet；缺失时自动调用 jvQuant 单股接口补数（较慢，适合盘前数据管线）。生产 pipeline 在推理时同样从该路径加载 T-1 分时指标。

训练集重建、回测面板生成、生产 pipeline 都会自动使用这些 parquet，无需额外配置。

## 概念缓存（PIT 前置依赖）

模型训练与回测面板依赖概念动量特征（`sector_heat`、`sector_rank`、`n_concepts`）。这些特征由 Tushare `ths_daily` + `ths_member` 构建，必须先构建概念缓存：

```bash
# 构建概念日线行情（按交易日期增量追加）
python plays/limit_up/backtest/concept_cache.py build --start 20260601 --end 20260702

# 构建概念成分股映射（静态，首次运行约 8-10 分钟）
python plays/limit_up/backtest/concept_cache.py build-members

# 检查缓存覆盖
python plays/limit_up/backtest/concept_cache.py check
```

缓存归档路径：`wiki/raw/limit-up/panel/concept/`。
训练集/回测/生产会优先从此路径加载；如缺失则回退到旧的 `plays/limit_up/backtest/cache/`。

## 训练集重建

新增 PIT 特征（概念动量、龙虎榜、**日内分时指标**）后，必须重建训练集 CSV，否则新特征全为默认值：

```bash
python plays/limit_up/backtest/training.py build --start 20260519 --end 20260702 --force
```

产物：`wiki/raw/limit-up/training/training_set.csv`。

重建后建议检查 `id_*` 列非空率：

```bash
python -c "
import pandas as pd
df = pd.read_csv('wiki/raw/limit-up/training/training_set.csv')
for c in [c for c in df.columns if c.startswith('id_')]:
    print(c, df[c].notna().mean())
"
```

## 面板生成

`dataset.py::build_panel(dates=[...])` 一站式：读 wiki/raw/limit-up/analysis 里的评分记录 + 拉 Tushare daily/daily_basic + join 未来标签 + 重算 `total_score`。

面板列的详细定义见 `factors.md` 的每个因子"依赖列"字段。

## 每日回测

```bash
python plays/limit_up/backtest/backtest.py --days 20        # 最近 20 天
python plays/limit_up/backtest/backtest.py --start 20260601 --end 20260630
python plays/limit_up/backtest/backtest.py --dates 20260615,20260618
```

输出 Top-K 命中率、胜率、平均收益，以及阈值推送模式的数据。

## 数据资产

面板按天沉淀到 `wiki/raw/limit-up/panel/<api>/<YYYYMMDD>.parquet`：
- **行增量**（新日期）：只拉未缓存的交易日
- **列增量**（新字段）：只拉缺失的字段列，合并到已有 parquet
- **git 跟踪**：跨会话持久，是宝贵的回测资产
