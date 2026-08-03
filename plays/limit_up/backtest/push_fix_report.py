#!/usr/bin/env python3
"""推送模型修复+回测最终报告到飞书。"""
import sys
from pathlib import Path

PROJECT_DIR = Path("/root/maneki-agent")
sys.path.insert(0, str(PROJECT_DIR))

import requests
from plays.limit_up.pipeline_feishu import feishu_title_prefix
from scripts.tu_share import CONFIG

FEISHU_APP_ID = CONFIG.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = CONFIG.get("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID_REPORT = CONFIG.get("FEISHU_CHAT_ID_REPORT", "")

MSG = """【模型数据修复 + 一个月回测报告】
修复了 4 个数据 bug + 2 个口径问题：

1. 竞价数据源修复
   训练集 stk_auction_o(盘后,无price) → stk_auction(实时,有price)
   训练集 pct_chg_score_day 从 T-1 收盘涨幅 → 真实竞价涨幅

2. 训练目标修复
   未来3日涨停 → 当日涨停（与生产/复盘口径一致）
   训练集正样本 1434 只当日涨停，竞价涨幅相关性 0.0163→0.1824

3. 概念缓存污染修复
   700/883 非概念指数混入（全A指数挂 5000+ 股票）→ 过滤到 293 个真实概念
   概念行情补到 83 天（0401~0731），历史面板板块特征全部重算
   每股票概念数 59 → 9（恢复正常）

4. id_* 日内特征移除
   生产覆盖率仅 3%（训练 75%）→ 从 67 特征中移除 6 个生产不可用特征
   修复前 AUC 0.87 是虚高分（学了生产用不上的信号）

═══════════
【一个月回测：0701~0731 共 23 个交易日，09:30 视角】

指标    生产(旧)    修复v2
Top3    11.6%(8/69) 27.5%(19/69)  ← 2.4倍
Top5    10.4%       23.5%
Top10   11.3%       20.4%

高光：0729 三只全中、0730 中2、0728 中2
模型现在依赖真实信号：竞价高开+板块+连板
（603137 连板 6 天上榜多次命中）

模型：已部署到生产（plays/limit_up/data/backtest/models）"""


def main():
    token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(token_url, json={
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET,
    }, timeout=10)
    tok = resp.json()
    if tok.get("code") != 0:
        print(f"token失败: {tok}")
        return 1
    token = tok["tenant_access_token"]

    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    import json as _json
    content = _json.dumps({"text": f"{feishu_title_prefix()}\n{MSG}"}, ensure_ascii=False)
    r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json={
        "receive_id": FEISHU_CHAT_ID_REPORT,
        "msg_type": "text",
        "content": content,
    }, timeout=10)
    print("推送状态:", r.status_code, r.text[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
