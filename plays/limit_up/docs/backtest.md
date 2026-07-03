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

训练 `HistGradientBoostingClassifier` 模型，分别预测 3 日涨停概率与 3 日胜率，混合输出连续 model score。

```bash
python plays/limit_up/backtest/train_model.py \
    --train-start 20260519 --train-end 20260620 \
    --test-start 20260621 --test-end 20260702
```

产物：`plays/limit_up/data/backtest/models/`：
- `limit_up_model.joblib`
- `model_features.json`
- `feature_importance.json`
- `validation_report.json`

训练完成后，设置 `LIMIT_UP_USE_MODEL=true` 即可在回测/生产中使用模型分。

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
