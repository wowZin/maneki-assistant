# 完整版 `new_total_v2` 实装方案

## 改动范围（只改 pipeline.py 的 push_feishu 和相关函数）

### 新增步骤：日线数据预取
在评分完成后、推送之前，为所有候选股批量拉取 Tushare daily 和 limit_list_d：
- **trailing_10 / trailing_5** — 10日/5日涨幅，判断短期热度
- **position_20d** — 现价处于20日区间的位置（0-1），防追高
- **pullback_10d** — 距10日高点的回撤幅度，回调质量
- **limit_up_gene_composite** — 涨停基因（近20日/60日涨停次数）

这些只需要 1-2 次 Tushare API 调用（daily 的 ts_code 传全部代码，limit_list_d 也一样）。

### 新评分 `new_total_v2`（替代 total 做排序）
```python
score = shortterm * 1.8          # 短线博弈为核心
score += fundamental * 0.6       # 基本面提胜率
score += technical * 0.5         # 技术面确认
score += limit_up_gene * 1.0     # 涨停基因
score += pullback_quality * 0.8  # 回调质量
# - chasing_penalty             # 追高惩罚（position_20d 高时降权）
```

### 推送逻辑：ScoreGap
- 按 `new_total_v2` 排序
- 取 `new_total_v2 >= max_score * 0.98` 的股票
- 最少 2 只，最多 5 只
- 保留原有日去重和午后情绪过滤

### 性能影响
- batch Tushare daily/limit_list_d: ~2-3秒
- 比现有流程慢不到 5 秒
