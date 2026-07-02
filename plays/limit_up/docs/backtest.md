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

发现新因子候选。输入面板 + 目标标签，遍历单/双/三因子组合，输出 Top-K 按 IC 排序的候选到 `out/mined_factors.json` + `out/mined_factors.md`。

```bash
python plays/limit_up/backtest/mine.py \
  --panel out/panel_enriched.csv \
  --label hit_limit_3 \
  --topk 30
```

### validate.py

给定一个已注册的因子名（`factors/__init__.py` 注册表中的 key），计算面板上的 RankIC / hit@10 / hit@20 / chasing_score，与基线对比。

```bash
python plays/limit_up/backtest/validate.py --factor total_score
python plays/limit_up/backtest/validate.py --factor sentiment_amount_boosted
```

输出：`out/validate_{factor}.json`。

### optimize.py

网格 / 贝叶斯搜索：

- `total_score` 三个组件的权重（A/B/C）
- `AGENT_WEIGHTS`（维度权重）

```bash
python plays/limit_up/backtest/optimize.py --target total_score --method grid
```

输出：`out/optimize_{target}.json`。

## 面板生成

`data.py` 提供 `build_panel(start, end)` 一站式：读 Tushare 日线 + daily_basic + limit_list_d + jvQuant fundflow，拼接为 wide-format DataFrame，落 `cache/panel_{start}_{end}.parquet`。

面板列的详细定义见 `factors.md` 的每个因子"依赖列"字段。

## 面板搬迁

历史面板 CSV（`panel_enriched_v3/v4/v5.csv`, `panel_enriched_pit.csv`, `panel_rebuilt.csv` 等）在重构中删除。新面板统一由 `data.py::build_panel` 生成到 `cache/`（`.gitignore` 托管）。

## 一次性执行的验证

重构完成后应通过：

```bash
python plays/limit_up/backtest/validate.py --factor total_score
# 期望：IC hit_limit_3 ≥ 0.33
```

若低于基线，说明因子迁移过程中语义漂移，需要回滚上一个 commit 排查。
