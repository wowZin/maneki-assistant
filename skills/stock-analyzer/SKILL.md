---
name: stock-analyzer
description: A股五维度评分分析。用户要求分析股票、查看评分、点评个股时使用。负责评分计算和结果展示，不参与盯盘和知识查询。
---

## 职责边界

你只负责一件事：对A股股票执行五维度评分，把结果展示给用户。

- ✅ 收到股票代码/名称 → 评分
- ✅ 用户追问评分详情 → 解读
- ❌ 盯盘管理 → 交给 stock-watchdog
- ❌ 概念/术语问题 → 交给 stock-wiki

## 执行评分

```bash
python3 -c "
from plays.limit_up.pipeline import score_fundamental, score_technical, score_fundflow, score_sentiment
from plays.limit_up.strategies.shortterm import score_shortterm
from plays.limit_up.pipeline import AGENT_WEIGHTS
from concurrent.futures import ThreadPoolExecutor, as_completed

code = 'CODE'
funcs = {'fundamental':score_fundamental,'technical':score_technical,'fundflow':score_fundflow,'sentiment':score_sentiment,'shortterm':score_shortterm}
scores, reasons = {}, {}
with ThreadPoolExecutor(max_workers=4) as pool:
    futs = {pool.submit(fn, code): dim for dim, fn in funcs.items()}
    for f in as_completed(futs):
        dim = futs[f]
        try: s, r = f.result(timeout=30); scores[dim]=s; reasons[dim]=r
        except Exception as e: scores[dim]=0.0; reasons[dim]=f'异常:{e}'

dims = ['fundamental','technical','fundflow','sentiment','shortterm']
w = AGENT_WEIGHTS
contribs = [(scores.get(d,0) or 0, w.get(d,1.0)) for d in dims]
contribs.sort(key=lambda x: x[0]*x[1], reverse=True)
top3 = contribs[:3]
total = sum(s*wt for s,wt in top3) / sum(wt for _,wt in top3) if top3 else 0

labels = {'fundamental':'基本面','technical':'技术面','fundflow':'资金面','sentiment':'情绪面','shortterm':'短线博弈'}
print(f'综合评分: {total:.1f}')
for d in dims:
    r = reasons.get(d,'')
    print(f'  {labels[d]}: {scores.get(d,0):.0f}分 — {r.split(\";\")[0] if r else \"无数据\"}')
"
```

将 `CODE` 替换为实际股票代码（如 000001.SZ）。

## 评级标准

| 分数 | 评级 | 说明 |
|------|------|------|
| ≥55 | ⭐⭐⭐⭐⭐ | 优秀 |
| ≥45 | ⭐⭐⭐⭐ | 良好 |
| ≥35 | ⭐⭐⭐ | 中等 |
| <35 | 不评级 | 不推送 |

## 五维度说明

| 维度 | 中文 | 解读 |
|------|------|------|
| fundamental | 基本面 | ROE、扣非净利增速、营收增速、财务避雷、见光死惩罚 |
| technical | 技术面 | 均线排列、量比、MACD/KDJ、换手率、布林带 |
| fundflow | 资金面 | 主力净流入、龙虎榜、北向资金、融资余额 |
| sentiment | 情绪面 | 涨停基因、连板效应、竞价博弈、人气排名 |
| shortterm | 短线博弈 | 封板质量、连板动量、开盘博弈、攻击独特性 |

**总分 = 加权Top3择优**：取加权贡献最高的3个维度算加权均值。

## 追问解读

用户追问某个维度时：
- 用自己的语言解释该维度的评分依据
- 引用 `plays/limit_up/docs/` 下的策略文档获取详细信息
- 指出该维度的优势和风险点

## 回复格式

评分结果用 `send_feishu_markdown` 发送：
```
📊 {名称} ({代码})
综合评级 {星级} ({总分}分)

基本面 {分}分 — {简述}
技术面 {分}分 — {简述}
资金面 {分}分 — {简述}
情绪面 {分}分 — {简述}
短线博弈 {分}分 — {简述}
```