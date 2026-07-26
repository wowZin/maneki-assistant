# pusher.py — 推送判断与去重

## 作用

`plays/limit_up/pusher.py::check_and_push(results, data_dir)` 判断评分结果是否满足推送条件，调用 `pipeline_feishu.push_feishu()` 发送飞书卡片，并落盘存档。

生产调用方：`pipeline.py`（09:30 一次性进程），传入 `model_score ≥ 55` 且按分降序的 Top-N 候选。

## 推送链路

```
pipeline.morning_pass
  → 候选: model_score >= 55（ULTIMATE_PUSH_THRESHOLD）且 竞价涨幅 < 9.8%
  → 排序取 Top-N（PUSH_TOP_N，默认 3）
  → pusher.check_and_push(candidates, data_dir)
      ① is_trading_time() 交易时段闸（9:30-11:30 / 13:00-15:00 工作日）
      ② total_score >= ULTIMATE_PUSH_THRESHOLD（默认 55）或 l2_confirmed 放行
      ③ pipeline_feishu.push_feishu(to_push)
      ④ 落盘 data/pushed/{today}.json（原子写，返回本次新推送的票）
```

## 阈值：Top-N + ≥55 地板（2026-07-26 定）

模型是排序导向：**Top-N 相对标准 + ≥55 绝对地板**双条件。

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ULTIMATE_PUSH_THRESHOLD` | 55 | 绝对地板分（`pipeline.py` 与 `pusher.py` 各自读取，需一致） |
| `PUSH_TOP_N` | 3 | 相对 Top-N（pipeline 侧截取） |

- ≥55 的全量候选带留在 `data/analysis/{date}.json` 供回测；`data/pushed/` 存档 = 真实推送的 Top-N。
- `pipeline_feishu.push_feishu` 内部再降噪：同一只股票只有本次 `total_score` 比上次推送**高 0.5 分以上**才重推；卡片最多取 eligible 前 3 只。

## 推送卡片格式

每条股票**三行**（lark_md），不展示维度分：

```
**600176.SH 中国巨石** ⭐⭐⭐
涨幅:3.2%
总分:78
```

- 卡片标题：`涨停预测 (HH:MM)`（测试模式有前缀），蓝色 header。
- 星级映射（`_stars`，按总分）：≥90 → ⭐⭐⭐⭐⭐；≥80 → ⭐⭐⭐⭐；≥70 → ⭐⭐⭐；≥60 → ⭐⭐；<60 → 无星。

## 落盘

| 文件 | 写入者 | 内容 |
|------|--------|------|
| `data/pushed/{YYYYMMDD}.json` | `pusher.check_and_push` | 当日推送记录（合并已推代码，原子写） |
| `data/pushed/{YYYYMMDD_HHMM}.json` | `pipeline_feishu.push_feishu` | 每次实际发送的卡片内容 |
| `data/pushed/{YYYYMMDD}_surge.json` | `surge_scanner` | surge 通道入盯记录（不经 pusher，供回测） |

## 与 pipeline_feishu.push_feishu 的分工

- `pusher.check_and_push()`：交易时段闸 + 阈值过滤 + 落盘，决定"哪些票值得推"
- `pipeline_feishu.push_feishu()`：排序、评分提高重推判断、Top-3 截取、卡片渲染、HTTP 发送，决定"怎么推"

数据契约：`results` 中必须有 `code` / `name` / `total_score`（缺 `total_score` 时回退读 `total` 字段）。

## 失败告警（不经 pusher）

pipeline 的 crash/竞价失败告警走 `pipeline._notify_text`（飞书文本消息，绕过 pusher 的交易时段闸），凭证：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_CHAT_ID_SIGNAL`（回退 `FEISHU_BOT_CHAT_ID`）。
