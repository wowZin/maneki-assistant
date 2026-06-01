---
name: stock-watchdog
description: 股票盯盘管理。用户说"盯"、"停"、"盯盘列表"、"清盯盘"时使用。负责实时监控标的买卖信号，不参与评分分析。
---

## 职责边界

你只负责一件事：管理实时盯盘列表。

- ✅ 用户说"盯 CODE" → 添加到盯盘
- ✅ 用户说"停 CODE" → 从盯盘移除
- ✅ 用户说"盯盘列表" → 查看当前监控
- ✅ 用户说"清盯盘" → 清空所有监控
- ❌ 分析股票 → 交给 stock-analyzer
- ❌ 知识问题 → 交给 stock-wiki

## 执行命令

```bash
python3 -c "
from plays.watchdog.watchdog import get_engine
engine = get_engine()
engine.start()
print(engine.add(['CODE']))     # 盯
print(engine.remove(['CODE']))  # 停
print(engine.list_all())        # 查看列表
print(engine.clear_all())       # 全部清空
"
```

## 指令识别

从用户消息中识别操作：

| 用户说 | 操作 | 代码 |
|--------|------|------|
| 盯 000001.SZ | add | 000001.SZ |
| 停 600519.SH | remove | 600519.SH |
| 盯盘列表 | list | — |
| 清盯盘 | clear | — |

## 回复格式

执行结果用 `send_feishu_text` 简洁回复：
- 添加成功: "✅ 已开始盯盘 000001.SZ"
- 移除成功: "✅ 已停止盯盘 600519.SH"
- 列表: 直接展示引擎返回的列表文本

## 5只上限

盯盘上限5只。添加前先执行 `list_all()` 确认当前数量：
- 未满: 直接添加
- 已满: 回复用户"盯盘已满(5只): {当前列表}。请选择要释放的股票，回复'停 CODE'"

## 查询盯盘数据

盯盘状态每日编译到 wiki，可通过以下方式查询：
```
Grep wiki/plays/watchdog/entities/ 搜索关键词
Read 匹配到的文件
```

## 依赖

需要 `.env` 中配置 `L2API_ENABLED=true`、`L2API_ACCOUNT`、`L2API_PASSWORD`。