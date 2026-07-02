# 回测三件套

> 打板玩法的回测/挖掘/优化工具收敛为三个文件，替换原 20+ 脚本的散乱状态。

## 目录

```
plays/limit_up/backtest/
├── data.py          # 面板加载/切片/缓存
├── labels.py        # 标签生成（hit_limit_N / fwd_ret_N / fwd_max_N）
├── metrics.py       # IC / hit@K / chasing_score / sharpe
├── mine.py          # 因子挖掘（单/双/三因子组合）
├── validate.py      # 单因子 / total_score 的 IC & hit 报告
├── optimize.py      # 权重优化（total_score 组件权重 + 维度权重）
├── cache/           # .gitignore
└── out/             # .gitignore
```

## 三件套职责

### mine.py

因子挖掘：在训练集上遍历单/双/三因子组合，输出 Top-K 按 IC 排序的候选。

### validate.py

给定一个已注册的因子名（`factors/__init__.py::REGISTRY` 中的 key），在训练集上计算 RankIC / hit@10 / hit@20 / chasing_score。

### optimize.py

网格 / 贝叶斯搜索 `total_score` 三个组件的权重（A/B/C）与维度权重 `AGENT_WEIGHTS`。

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
