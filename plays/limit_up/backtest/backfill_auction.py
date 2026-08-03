#!/usr/bin/env python3
"""回填历史面板（0720~0723）竞价数据：auc_amount/auc_vol/auc_pct/auc_amt_ratio/auc_vol_ratio + shortterm。

复用 pipeline 竞价刷新逻辑（按 trade_date 全量拉 stk_auction，只接受当日行，
auc_pct=(price/pre_close-1)*100，auc_amt_ratio=amt/avg_amount_5d，auc_vol_ratio=vol/T-1 vol，
shortterm 重算 base10+量比分档）。
"""
import sys
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path("/root/maneki-agent")
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import call_tushare

DATES = ["20260701", "20260702", "20260703", "20260706", "20260707",
         "20260708", "20260709", "20260710", "20260713", "20260714",
         "20260715", "20260716", "20260717", "20260720", "20260721",
         "20260722", "20260723"]
PANEL_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel"


def trade_dates(start: str, end: str) -> list[str]:
    from plays.limit_up.backtest.dataset import _trade_dates
    return _trade_dates(start, end)


def main():
    force = "--force" in sys.argv
    for date in DATES:
        panel_file = PANEL_DIR / f"{date}.parquet"
        if not panel_file.exists():
            print(f"[{date}] 面板不存在，跳过")
            continue
        df = pd.read_parquet(panel_file)
        if not force and "auc_pct" in df.columns and df["auc_pct"].notna().sum() > 100:
            print(f"[{date}] 已有竞价数据（非空 {df['auc_pct'].notna().sum()} 只），跳过（--force 重跑）")
            continue

        print(f"[{date}] 拉取竞价...")
        r = call_tushare("stk_auction", {"trade_date": date},
                         "ts_code,trade_date,amount,vol,price,pre_close", timeout=120)
        items = r.get("data", {}).get("items", [])
        fields = r.get("data", {}).get("fields", [])
        auc: dict[str, tuple] = {}
        for row in items:
            d = dict(zip(fields, row))
            c = d.get("ts_code", "")
            if not c or d.get("trade_date") != date:
                continue
            amt = float(d.get("amount") or 0)
            vol = float(d.get("vol") or 0)
            price = float(d.get("price") or 0)
            pre = float(d.get("pre_close") or 0)
            pct = (price / pre - 1.0) * 100 if pre > 0 and price > 0 else None
            auc[c] = (amt, vol, pct)
        print(f"[{date}] 竞价 {len(auc)} 只")

        # T-1 vol（auc_vol_ratio 分母）：从 daily parquet 目录找 date 之前最近的交易日
        daily_dir = PANEL_DIR / "daily"
        t1_vol = {}
        prev_date = None
        if daily_dir.exists():
            existing = sorted(p.name.replace(".parquet", "") for p in daily_dir.glob("*.parquet"))
            candidates = [d for d in existing if d < date]
            if candidates:
                prev_date = candidates[-1]
                _d = pd.read_parquet(daily_dir / f"{prev_date}.parquet", columns=["ts_code", "vol"])
                t1_vol = dict(zip(_d["ts_code"], _d["vol"]))
        print(f"[{date}] T-1 日 {prev_date}, vol 覆盖 {len(t1_vol)} 只")

        # 写回面板
        if "auc_pct" not in df.columns:
            df["auc_pct"] = None
        n_hit = 0
        for i, code in enumerate(df["code"]):
            if code not in auc:
                continue
            amt, vol, pct = auc[code]
            df.iat[i, df.columns.get_loc("auc_amount")] = amt
            df.iat[i, df.columns.get_loc("auc_vol")] = vol
            df.iat[i, df.columns.get_loc("auc_pct")] = pct
            a5 = float(df.iat[i, df.columns.get_loc("avg_amount_5d")] or 0)
            if a5 > 0:
                df.iat[i, df.columns.get_loc("auc_amt_ratio")] = amt / a5
            v1 = float(t1_vol.get(code, 0) or 0)
            if v1 > 0:
                df.iat[i, df.columns.get_loc("auc_vol_ratio")] = vol / v1
            n_hit += 1

        if "shortterm" in df.columns:
            _ar = df["auc_amt_ratio"].fillna(0).astype(float)
            df["shortterm"] = (10.0 + _ar.map(
                lambda a: 20 if a > 1 else (10 if a > 0.5 else (5 if a > 0.1 else 0)))).clip(upper=100)

        df.to_parquet(panel_file, index=False)
        print(f"[{date}] 面板竞价回填完成: {n_hit}/{len(df)} 只")


if __name__ == "__main__":
    main()
