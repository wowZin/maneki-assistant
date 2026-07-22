#!/usr/bin/env python3
"""盘中异动扫描：每 N 分钟扫涨幅榜，发现新异动股追加到预评分池。

用法:
    python3 plays/limit_up/surge_scanner.py          # 扫描一次
    python3 plays/limit_up/surge_scanner.py --daemon  # 每5分钟循环
"""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

ANALYSIS_DIR = Path(__file__).resolve().parent / "data" / "analysis"
PANEL_DIR = Path(__file__).resolve().parent.parent.parent / "wiki" / "raw" / "limit-up" / "panel"
PUSH_THRESHOLD = 35  # 预评分阈值（与 panel_builder >=35 对齐）


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _now() -> datetime:
    return datetime.now()


def scan():
    """扫涨幅榜 + 去重 + 预评分 → 更新 analysis.json"""
    td = _today()
    hhmm = int(datetime.now().strftime("%H%M"))

    # 读取现有预评分
    af = ANALYSIS_DIR / f"{td}.json"
    if not af.exists():
        print(f"  [surge] 无预评分文件: {af}")
        return
    existing = json.loads(af.read_text())
    existing_codes = {r["code"] for r in existing}
    print(f"  [surge] 现有预评分: {len(existing)}只")

    # 读取面板
    import pandas as pd
    pf = PANEL_DIR / f"{td}.parquet"
    if not pf.exists():
        print(f"  [surge] 无面板: {pf}")
        return
    panel = pd.read_parquet(pf).set_index("code")

    # 全市场扫描(THS SDK batch_quotes,无需cookie)
    from scripts.ths_client import get_ths_client as _ths
    try:
        pool_file = Path(__file__).resolve().parent / "data" / "pool" / f"pool_{td}.json"
        if not pool_file.exists():
            print(f"  [surge] 无候选池: {pool_file}")
            return
        pool = json.loads(pool_file.read_text())
        pool_codes = [s["code"] for s in pool]
        ths = _ths()
        new_codes = []
        for i in range(0, len(pool_codes), 50):
            batch = pool_codes[i:i+50]
            quotes = ths.get_batch_quotes(batch)
            for code, q in quotes.items():
                if q is None:
                    continue
                pct = float(q.get("pct_chg", 0) or 0)
                if not (3 <= pct < 9.8):
                    continue
                # THS SDK 返回短码,转全码
                full_code = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
                if full_code in existing_codes:
                    continue
                if full_code not in panel.index:
                    continue
                new_codes.append(full_code)
            if len(new_codes) >= 20:  # 最多20只
                break
    except Exception as e:
        print(f"  [surge] 扫描失败: {e}")
        return

    if not new_codes:
        print(f"  [surge] 无新增异动股")
        return
    print(f"  [surge] 发现 {len(new_codes)} 只新异动股: {new_codes[:5]}...")

    # 从面板取 T-1 特征 + 实时 pct_chg 覆盖
    pit_rows = []
    for code in new_codes:
        try:
            row = panel.loc[code].to_dict()
            row["code"] = code
            short = code.split(".")[0]
            q = _ths().get_quote(short)
            if q:
                rt_pct = float(q.get("pct_chg", 0) or 0)
                row["pct_chg_score_day"] = rt_pct
                rt_tr = float(q.get("turnover", 0) or 0)
                if rt_tr > 0: row["turnover_rate"] = rt_tr
                rt_vr = float(q.get("vol_ratio", 0) or 0)
                if rt_vr > 0: row["volume_ratio"] = rt_vr
            pit_rows.append(row)
        except Exception:
            continue

    if not pit_rows:
        return

    # 模型评分
    pit_df = pd.DataFrame(pit_rows)
    keep_cols = [c for c in pit_df.columns if c not in ("pit_date",) and not c.startswith("_")]
    pit_df = pit_df[keep_cols]
    try:
        from plays.limit_up.factors.optimized.model_score import factor_model_score_batch as _m
        pit_df["model_score"] = _m(pit_df)
    except Exception as e:
        print(f"  [surge] 评分失败: {e}")
        return

    # 合并到 analysis.json
    new_records = []
    for _, r in pit_df.iterrows():
        score = float(r["model_score"])
        if score >= PUSH_THRESHOLD:
            rec = {}
            for c in keep_cols:
                v = r[c]
                if isinstance(v, (int, float, __import__("numpy").floating)):
                    rec[c] = float(v)
                else:
                    rec[c] = v
            rec["model_score"] = float(score)
            rec["source"] = "surge"  # 标记来自异动扫描，推送时降阈
            new_records.append(rec)

    if new_records:
        all_records = existing + new_records
        all_records.sort(key=lambda x: -x["model_score"])
        tmp = af.with_suffix(".tmp")
        tmp.write_text(json.dumps(all_records, ensure_ascii=False))
        tmp.rename(af)  # 原子替换,避免pipeline读到半写文件
        print(f"  [surge] 新增 {len(new_records)} 只 ≥{PUSH_THRESHOLD}, 池子共 {len(all_records)} 只")
    else:
        print(f"  [surge] 新异动股均未达阈值")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘中异动扫描")
    parser.add_argument("--daemon", action="store_true", help="每5分钟循环")
    args = parser.parse_args()

    if args.daemon:
        print(f"[surge] daemon 模式启动, 每300s扫描一次")
        while True:
            now = _now()
            hhmm = int(now.strftime("%H%M"))
            if 925 <= hhmm < 1130 or 1300 <= hhmm < 1500:
                scan()
            time.sleep(300)
    else:
        scan()


if __name__ == "__main__":
    main()
