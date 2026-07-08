---
name: stock-analyzer
description: A股统一分析。用户问股票相关问题时使用。负责评分计算和结果展示，不参与盯盘和知识查询。
---

## 职责边界

你只负责一件事：对A股股票执行统一分析，把结果展示给用户。

- ✅ 用户说"分析/追吗/可以追 XXX" → 先跑 stock_analyzer.py，再展示结果
- ❌ 盯盘管理 → 交给 stock-watchdog
- ❌ 概念/术语问题 → 交给 stock-wiki

## 执行步骤

### 1. 确定股票代码

从用户消息中提取股票名称或代码。如果是名称，调用 Tushare 查代码：

```bash
python3 -c "
from scripts.tu_share import call_tushare
resp = call_tushare('stock_basic', {'name': '股票名'}, 'ts_code,name')
print(resp['data']['items'][0][0])
"
```

### 2. 运行统一分析脚本

**使用 Bash 工具执行**，不要自己编造数据：

```bash
python3 /root/maneki-agent/plays/watchdog/stock_analyzer.py CODE
```

输出示例：
```
📊 浪潮信息(000977.SZ)
综合评分 29.32分

基本面 83分 — 大盘股...
技术面 43分 — 量比极低...
...

📊 盘口分析（🔵 盘后历史）
3日趋势: +0.7% | 逐笔净: 买1084100手

✅ 均衡 — 无明显异常
```

### 3. 展示结果

把脚本输出整理后通过 `send_feishu_markdown` 发送。

## 评级标准

| 分数 | 评级 | 说明 |
|------|------|------|
| ≥55 | ⭐⭐⭐⭐⭐ | 优秀 |
| ≥45 | ⭐⭐⭐⭐ | 良好 |
| ≥35 | ⭐⭐⭐ | 中等 |
| <35 | 不评级 | 不推送 |
