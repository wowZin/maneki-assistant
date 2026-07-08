---
name: stock-analyzer
description: A股五维度评分分析。用户要求分析股票、查看评分、点评个股时使用。负责评分计算和结果展示，不参与盯盘和知识查询。
---

## 职责边界

你只负责一件事：对A股股票执行五维度评分 + 模型总分，把结果展示给用户。

- ✅ 收到股票代码/名称 → 评分
- ✅ 用户追问评分详情 → 解读
- ❌ 盯盘管理 → 交给 stock-watchdog
- ❌ 概念/术语问题 → 交给 stock-wiki

## 运行评分

分两步执行：

### 步骤1：五维度评分

```bash
python3 -c "
from plays.limit_up.strategies.fundamental import score_fundamental
from plays.limit_up.strategies.technical import score_technical
from plays.limit_up.strategies.fundflow import score_fundflow
from plays.limit_up.strategies.sentiment import score_sentiment
from plays.limit_up.strategies.shortterm import score_shortterm
from concurrent.futures import ThreadPoolExecutor, as_completed

code = 'CODE'
funcs = {
    'fundamental': score_fundamental,
    'technical': score_technical,
    'fundflow': score_fundflow,
    'sentiment': score_sentiment,
    'shortterm': score_shortterm,
}
scores, reasons = {}, {}
with ThreadPoolExecutor(max_workers=8) as pool:
    futs = {pool.submit(fn, code): dim for dim, fn in funcs.items()}
    for f in as_completed(futs):
        dim = futs[f]
        try:
            s, r = f.result(timeout=30)
            scores[dim] = s
            reasons[dim] = r
        except Exception as e:
            scores[dim] = 0.0
            reasons[dim] = f'异常:{e}'

import json
print('SCORES:' + json.dumps(scores))
print('REASONS:' + json.dumps(reasons))
"
```

### 步骤2：XGBoost 模型总分

用以下命令获取 Tushare 基础特征并调用模型分：

```bash
python3 -c "
import json, os, sys
sys.path.insert(0, '/root/maneki-agent')

# 读取步骤1的评分结果
scores = json.loads(os.environ.get('DIM_SCORES', '{}'))
code = 'CODE'

# 从 Tushare 获取基础数据构建特征
from scripts.tu_share import pro
from datetime import datetime, timedelta

today = datetime.now().strftime('%Y%m%d')
code_short = code.split('.')[0]
feats = {
    'fundamental': scores.get('fundamental', 0),
    'technical': scores.get('technical', 0),
    'fundflow': scores.get('fundflow', 0),
    'sentiment': scores.get('sentiment', 0),
    'shortterm': scores.get('shortterm', 0),
}

# 获取日线数据计算基础特征
try:
    df = pro.daily(ts_code=code, start_date=(datetime.now()-timedelta(days=60)).strftime('%Y%m%d'), end_date=today)
    if df is not None and len(df) > 5:
        df = df.sort_values('trade_date')
        closes = df['close'].values
        pcts = df['pct_chg'].values / 100.0
        feats['prev_pct'] = float(pcts[-2]) if len(pcts) > 1 else 0.0
        feats['pct_5d'] = float((closes[-1] - closes[-6]) / closes[-6]) if len(closes) > 6 else 0.0
        feats['trailing_5'] = float(sum(pcts[-5:])) if len(pcts) >= 5 else 0.0
        feats['trailing_10'] = float(sum(pcts[-10:])) if len(pcts) >= 10 else 0.0
        feats['position_20d'] = float((closes[-1] - closes[-20:].min()) / (closes[-20:].max() - closes[-20:].min() + 1e-8)) if len(closes) >= 20 else 0.5
        feats['turnover_rate'] = float(df['turnover_rate'].iloc[-1]) if 'turnover_rate' in df.columns else 5.0
        feats['volume_ratio'] = float(df['vol_ratio'].iloc[-1]) if 'vol_ratio' in df.columns else 1.0
except Exception:
    pass

# 获取流通市值
try:
    basic = pro.daily_basic(ts_code=code, trade_date=today, fields='circ_mv')
    if basic is not None and len(basic) > 0:
        feats['circ_mv'] = float(basic['circ_mv'].iloc[0])
except Exception:
    pass

# 调用 XGBoost 模型分（缺失特征用训练中位数填充）
from plays.limit_up.factors.optimized.model_score import factor_model_score
total = factor_model_score(feats)
print(f'TOTAL_SCORE:{total:.2f}')
"
```

将 `CODE` 替换为实际股票代码（如 000001.SZ）。

## 结果展示

步骤1 的 `SCORES` 和 `REASONS` + 步骤2 的 `TOTAL_SCORE` 一起展示。使用 `send_feishu_markdown` 发送：

```
📊 {名称} ({代码})
综合评级 {星级} ({total_score}分)

基本面 {分}分 — {简述}
技术面 {分}分 — {简述}
资金面 {分}分 — {简述}
情绪面 {分}分 — {简述}
短线博弈 {分}分 — {简述}
```

**重要：总分必须以 XGBoost 模型输出的 `total_score` 为准，不要自己加权五维度分当综合分。**

## 追问解读

用户追问某个维度时：
- 用自己的语言解释该维度的评分依据
- 引用 `plays/limit_up/docs/` 下的策略文档获取详细信息
- 指出该维度的优势和风险点

## 评级标准

| 分数 | 评级 | 说明 |
|------|------|------|
| ≥55 | ⭐⭐⭐⭐⭐ | 优秀 |
| ≥45 | ⭐⭐⭐⭐ | 良好 |
| ≥35 | ⭐⭐⭐ | 中等 |
| <35 | 不评级 | 不推送 |
