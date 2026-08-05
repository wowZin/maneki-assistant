#!/usr/bin/env python3
"""推送判断与去重 — 由 pipeline daemon 调用。

职责：
  1. 按 total_score 阈值过滤结果
  2. 调用 pipeline_feishu.push_feishu()（自带评分提高重推逻辑）
  3. 落盘 pushed/ 记录供外部查询

注意：重推决策由 push_feishu 内部的评分提高检查控制，本模块不做简单 code 去重。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from plays.limit_up.utils import is_trading_time

PUSH_THRESHOLD = float(os.environ.get("ULTIMATE_PUSH_THRESHOLD", "50"))


def _load_pushed_codes(pushed_dir: Path, today_str: str) -> set[str]:
    """加载今天已推送的股票代码集合。"""
    codes: set[str] = set()
    if not pushed_dir.exists():
        return codes
    for f in pushed_dir.glob(f"{today_str}*.json"):
        if f.name.endswith("_surge.json"):
            # surge 是盘中扫描候选池存档，非推送记录，排除以免污染已推送去重
            continue
        try:
            with open(f) as fp:
                pushed = json.load(fp)
            if isinstance(pushed, list):
                for p in pushed:
                    code = p.get("code", "")
                    if code:
                        codes.add(code)
        except Exception:
            continue
    return codes


def is_push_window() -> bool:
    """推送时段：工作日 09:26~11:30 / 13:00~15:00。

    早盘窗口从 09:26 起（而非 is_trading_time 的 09:30）——
    pipeline_daily 09:26 启动、竞价就绪后立即评分推送，若等到 09:30
    才放行会形成竞态：竞价数据早就绪时评分在 09:26~09:29 完成，
    被 is_trading_time() 拦截导致当日 0 推送（2026-08-05 实测：
    09:27 评分完成 max=66.1 但 pusher 返回空）。
    """
    from datetime import datetime
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if now.hour < 9 or (now.hour == 9 and now.minute < 26):
        return False
    if now.hour >= 15:
        return False
    if now.hour == 11 and now.minute >= 30:
        return False
    if now.hour == 12:
        return False
    return True


def check_and_push(results: list[dict], data_dir: Path) -> list[dict]:
    """检查并推送满足条件的股票。

    Args:
        results: 评分结果列表，每个结果必须含 code/name/total_score。
        data_dir: plays/limit_up/data 目录路径。

    Returns:
        实际新推送的股票列表（可能为空）。
    """
    if not is_push_window():
        return []

    from plays.limit_up.pipeline_feishu import push_feishu
    from datetime import datetime

    today_str = datetime.now().strftime("%Y%m%d")

    pushed_dir = data_dir / "pushed"
    pushed_dir.mkdir(parents=True, exist_ok=True)

    # 阈值过滤
    to_push = [r for r in results
               if r.get("total_score", 0) >= PUSH_THRESHOLD
               or r.get("l2_confirmed", False)]
    if not to_push:
        return []

    # 调用 push_feishu（自带评分提高重推逻辑,内部已打日志）
    try:
        push_feishu(to_push)

        # 存档已推记录供查询（原子写入）
        pushed_path = pushed_dir / f"{today_str}.json"
        existing = _load_pushed_codes(pushed_dir, today_str)
        all_pushed = existing | {r["code"] for r in to_push}
        records = [r for r in to_push if r["code"] in all_pushed]
        tmp = pushed_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(records, ensure_ascii=False))
        tmp.rename(pushed_path)
        return [r for r in to_push if r["code"] not in existing]
    except Exception as e:
        print(f"  [推送] 失败: {e}")
        return []
