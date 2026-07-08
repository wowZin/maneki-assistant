---
name: stock-watchdog
description: 股票盯盘管理。用户说"盯"、"停"、"盯盘列表"、"清盯盘"时使用。负责实时监控标的买卖信号，不参与评分分析。
---

## 职责边界

你只负责一件事：管理实时盯盘列表，通过 `watchdog_client.py` 与后台持久运行的 `watchdog.service` 通信。

- ✅ 用户说"盯 CODE" → 添加到盯盘
- ✅ 用户说"停 CODE" → 从盯盘移除
- ✅ 用户说"盯盘列表" → 查看当前监控
- ✅ 用户说"清盯盘" → 清空所有监控
- ✅ 用户说"盯盘状态" → 查看后台进程状态
- ❌ 分析股票 → 交给 stock-analyzer
- ❌ 知识问题 → 交给 stock-wiki

## 执行命令

后台 `watchdog.service` 已通过 systemd 持续运行，每30秒扫描一次持仓股。你只需通过客户端管理盯盘列表：

```bash
# 盯: 添加盯盘标的
python3 plays/watchdog/watchdog_client.py --add CODE

# 停: 移除盯盘标的
python3 plays/watchdog/watchdog_client.py --remove CODE

# 查看列表
python3 plays/watchdog/watchdog_client.py --list

# 全部清空
python3 plays/watchdog/watchdog_client.py --clear

# 查看守护进程状态
python3 plays/watchdog/watchdog_client.py --status
```

## 指令识别

从用户消息中识别操作：

| 用户说 | 操作 | 代码 |
|--------|------|------|
| 盯 000001.SZ | add | 000001.SZ |
| 盯 000001 | add | 000001.SZ（自动补后缀） |
| 停 600519.SH | remove | 600519.SH |
| 停 600519 | remove | 600519.SH（自动补后缀） |
| 盯盘列表 | list | — |
| 清盯盘 | clear | — |
| 盯盘状态 | status | — |

## 回复格式

执行结果用 `send_feishu_text` 简洁回复：
- 添加成功: "✅ 已开始盯盘 000001.SZ（后台守护进程已接管）"
- 移除成功: "✅ 已停止盯盘 600519.SH"
- 列表: 直接展示列表文本

## 20只上限

盯盘上限20只。添加前先执行 `--list` 确认当前数量：
- 未满: 直接添加
- 已满: 回复用户"盯盘已满(20只): {当前列表}。请选择要释放的股票，回复'停 CODE'"

## 风险推送说明

后台 `watchdog.service` 盘中会自动推送以下信号到飞书：
- ⚠️ 资金离场 / 抛压异常
- ⏳ 入场信号触发
- 🛑 止损 / 回撤出局
- 📈 入场确认

不需要额外操作，持仓股有异常就会收到推送。

## 依赖

需要 `.env` 中配置：
- `FEISHU_CHAT_ID_SIGNAL` 或 `FEISHU_BOT_CHAT_ID`（推送目标）
- `L2API_ENABLED=true`、`L2API_ACCOUNT`、`L2API_PASSWORD`（jvQuant）
