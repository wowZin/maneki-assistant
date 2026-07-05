#!/usr/bin/env python3
"""解析 jvQuant download_history 下载的 2026.zip，生成 intraday 指标 parquet。

用法：
    python plays/limit_up/backtest/extract_intraday_zip.py --zip 2026.zip
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from collections import defaultdict

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent.parent
OUT_DIR = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel" / "intraday"


def _suffix(code: str) -> str:
    """根据短代码推断交易所后缀。"""
    if code.startswith(("600", "601", "603", "605", "688", "689")):
        return ".SH"
    if code.startswith(("8", "4", "920")):
        return ".BJ"
    return ".SZ"


def _parse_day(code_short: str, date: str, bars: list) -> dict | None:
    """从单日分钟 bars 计算聚合指标。

    bars 格式：第一项是 header [code, date, pre_close]，后面是 [price, avg_price, volume]。
    """
    if not bars or len(bars) < 2:
        return None
    data_bars = bars[1:]
    prices = [b[0] for b in data_bars]
    volumes = [b[2] for b in data_bars]

    total_vol = sum(volumes)
    if total_vol == 0:
        return None

    amount = sum(p * v for p, v in zip(prices, volumes))
    vwap = amount / total_vol

    n = len(data_bars)
    # 上午：前 120 根（09:30-11:30），下午：后面（13:00-15:00）
    morning_n = 120
    morning_vol = sum(volumes[:morning_n])
    afternoon_vol = sum(volumes[morning_n:])
    # 尾盘：最后 30 根（14:30-15:00）
    tail_vol = sum(volumes[-30:])

    return {
        "ts_code": f"{code_short}{_suffix(code_short)}",
        "trade_date": date.replace("-", ""),
        "open": prices[0],
        "close": prices[-1],
        "high": max(prices),
        "low": min(prices),
        "volume": total_vol,
        "amount_est": amount,
        "vwap": vwap,
        "morning_vol_ratio": morning_vol / total_vol,
        "afternoon_strength": afternoon_vol / morning_vol if morning_vol > 0 else 1.0,
        "tail_vol_ratio": tail_vol / total_vol,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True, help="jvQuant download_history 下载的 zip 路径")
    parser.add_argument("--limit", type=int, help="仅处理前 N 个文件（测试用）")
    args = parser.parse_args()

    zip_path = Path(args.zip)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    rows_by_date: dict[str, list[dict]] = defaultdict(list)

    with zipfile.ZipFile(zip_path, "r") as z:
        names = [n for n in z.namelist() if n.startswith("2026/") and n.endswith(".json") and n.split("/")[-1][0].isdigit()]
        if args.limit:
            names = names[:args.limit]
        print(f"[extract] 共 {len(names)} 只股票 JSON 待解析")

        for i, name in enumerate(names, 1):
            code_short = Path(name).stem
            try:
                with z.open(name) as f:
                    data = json.load(f)
            except Exception as e:
                print(f"  [warn] {name} 解析失败: {e}")
                continue

            for date, bars in data.items():
                row = _parse_day(code_short, date, bars)
                if row:
                    rows_by_date[row["trade_date"]].append(row)

            if i % 500 == 0 or i == len(names):
                print(f"  已处理 {i}/{len(names)} 只，累计 {sum(len(v) for v in rows_by_date.values())} 行")

    print(f"[extract] 写入 parquet 到 {OUT_DIR}")
    for date in sorted(rows_by_date):
        df = pd.DataFrame(rows_by_date[date])
        out_file = OUT_DIR / f"{date}.parquet"
        df.to_parquet(out_file, index=False)
        print(f"  {date}: {len(df)} 行 -> {out_file}")

    print("[extract] 完成")


if __name__ == "__main__":
    main()
