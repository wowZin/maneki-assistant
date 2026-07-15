#!/usr/bin/env python3
"""改造 pipeline: 从analysis.json加载预评分池, 盘中只评池内股。

移除: ScoreStack, pop_top粗评, 全池扫描
保留: batch_quotes, model_score, L2确认, 推送
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# ── 配置 ──
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
ANALYSIS_DIR = PROJECT_DIR / "data" / "analysis"
PANEL_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel"

PUSH_THRESHOLD = float(os.environ.get("ULTIMATE_PUSH_THRESHOLD", "55"))
L2_GREY_LOW = float(os.environ.get("L2_GREY_LOW", "45"))
LIMIT_UP_PCT = 9.8  # 涨停阈值


def _today_str() -> str:
    return datetime.now().strftime("%Y%m%d")


def _is_trade_day(d: str) -> bool:
    from plays.limit_up.pipeline import _is_trade_day as _check
    return _check(d)


def load_pool() -> tuple[list[dict], dict]:
    """加载预评分池 + 面板T-1特征。"""
    today = _today_str()
    
    # analysis.json
    af = ANALYSIS_DIR / f"{today}.json"
    if not af.exists():
        print(f"[pipeline] 无预评分文件: {af}")
        return [], {}
    with open(af) as f:
        pool = json.load(f)
    print(f"[pipeline] 预评分池: {len(pool)}只 (≥30分)")
    
    # 面板 parquet
    import pandas as pd
    pf = PANEL_DIR / f"{today}.parquet"
    panel = pd.read_parquet(pf) if pf.exists() else None
    if panel is not None:
        panel = panel.set_index("code", drop=False)
        print(f"[pipeline] 面板已加载: {len(panel)}只 x {len(panel.columns)}列")
    else:
        print(f"[pipeline] 面板未找到: {pf}")
    
    # 构建 code→panel_row 映射
    pool_map = {}
    for r in pool:
        code = r["code"]
        row = panel.loc[code] if panel is not None and code in panel.index else {}
        pool_map[code] = {
            "code": code,
            "name": r.get("name", ""),
            "t1_features": row.to_dict() if hasattr(row, "to_dict") else {},
            "t1_score": r.get("model_score", 0),
        }
    return pool, pool_map


def _log_snapshot(code: str, score: float, quote: dict):
    """快照落盘（复用 pipeline 的 _log_snapshot 逻辑）。"""
    try:
        from plays.limit_up.pipeline import _log_snapshot
        _log_snapshot({"code": code, "name": "", "pct_chg": quote.get("pct_chg", 0),
                        "scores": {}}, score, quote)
    except Exception:
        pass
