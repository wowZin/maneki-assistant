#!/usr/bin/env python3
"""每日预测常驻进程。

时间线：
  00:30  更新概念缓存 → 评分(五维度+模型) → 推送
  09:26  获取竞价数据 → 重评 → Top5推送
  期间   sleep

强制模式：python3 pipeline_daily.py --force
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok= True)
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.utils import is_trading_time
from scripts.tu_share import call_tushare

_running = True
_FORCE = False


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")
    with open(LOG_DIR / "daily.log", "a") as f:
        f.write(f"[{ts}] {msg}\n")


def _signal_handler(sig, frame):
    global _running
    log(f"收到信号 {sig}，关闭中...")
    _running = False


def _is_trade_day(date_str: str) -> bool:
    try:
        r = call_tushare("trade_cal", {"cal_date": date_str}, "is_open")
        items = r.get("data", {}).get("items", [])
        return items and items[0][0] == 1 if items else False
    except Exception as e:
        log(f"交易日判断失败: {e}")
        return False


# ── 步骤 1: 概念缓存 ──

def step_concept_cache():
    log("[00:30] 加载概念缓存...")
    from plays.limit_up.strategies import factor_ctx
    cd, cm = factor_ctx.load_concept_data_from_cache()
    log(f"  概念行情 {len(cd)} 行, 成分股 {len(cm)} 行 ✓")


# ── 步骤 2: 评分（复用 pipeline 评分逻辑） ──

def step_score(trade_date: str, is_auction: bool = False):
    """全量评分：构建候选池 → 五维度评分 → 模型分 → 推送。"""
    tag = "竞价" if is_auction else "凌晨"
    log(f"[{tag}] 开始全量评分...")

    # 1. 候选池：从 stock_basic 拉主板股
    from scripts.tu_share import call_tushare as ts
    basic = ts("stock_basic", {}, "ts_code,name,list_date")
    items = basic.get("data", {}).get("items", [])
    fields = basic.get("data", {}).get("fields", [])

    candidates = []
    for row in items:
        r = dict(zip(fields, row))
        code = r["ts_code"]
        pure = code.split(".")[0]
        if not pure.startswith(("00", "60")):
            continue
        name = r.get("name", "") or ""
        if "ST" in name or "*ST" in name:
            continue
        candidates.append({"code": code, "name": name})

    log(f"[{tag}] 候选池 {len(candidates)} 只")

    # 2. 分批评分（一次 20 只，避免太慢）
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from plays.limit_up.strategies.fundamental import score_fundamental
    from plays.limit_up.strategies.technical import score_technical
    from plays.limit_up.strategies.fundflow import score_fundflow
    from plays.limit_up.strategies.sentiment import score_sentiment
    from plays.limit_up.strategies.shortterm import score_shortterm

    all_results = []
    batch_size = 20
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i:i + batch_size]
        batch_results = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {}
            for s in batch:
                code = s["code"]
                fn_map = {
                    "fundamental": score_fundamental,
                    "technical": score_technical,
                    "fundflow": score_fundflow,
                    "sentiment": score_sentiment,
                    "shortterm": score_shortterm,
                }
                for dim, fn in fn_map.items():
                    futures[pool.submit(fn, code)] = (code, dim, s["name"])

            scores_by_code = {}
            for future in as_completed(futures):
                code, dim, name = futures[future]
                if code not in scores_by_code:
                    scores_by_code[code] = {"code": code, "name": name, "scores": {}, "reasons": {}}
                try:
                    s, r = future.result()
                    scores_by_code[code]["scores"][dim] = float(s)
                    scores_by_code[code]["reasons"][dim] = str(r)
                except Exception as e:
                    scores_by_code[code]["scores"][dim] = 0.0
                    scores_by_code[code]["reasons"][dim] = f"err: {e}"

        for code, data in scores_by_code.items():
            # Top3 总分
            weights = {"fundamental": 0.5, "technical": 0.5,
                       "fundflow": 1.5, "sentiment": 1.0, "shortterm": 0.5}
            dc = [(data["scores"].get(d, 0), weights.get(d, 1.0)) for d in weights]
            dc.sort(key=lambda x: x[0] * x[1], reverse=True)
            top3 = dc[:3]
            total = sum(s * w for s, w in top3) / sum(w for _, w in top3) if sum(w for _, w in top3) > 0 else 0
            data["total"] = round(total, 2)
            data["top3_score"] = round(total, 1)
            batch_results.append(data)

        all_results.extend(batch_results)
        log(f"[{tag}]  进度 {min(i + batch_size, len(candidates))}/{len(candidates)}")

    # 3. 保存 analysis
    ts_str = datetime.now().strftime("%Y%m%d_%H%M")
    analysis_dir = DATA_DIR / "analysis"
    analysis_dir.mkdir(exist_ok=True)
    path = analysis_dir / f"{ts_str}.json"
    with open(path, "w") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    log(f"[{tag}]  分析已保存 → {path.name} ({len(all_results)} 只)")

    # 4. Top5 推送
    sorted_results = sorted(all_results, key=lambda x: x.get("total", 0), reverse=True)
    top5 = sorted_results[:5]
    log(f"[{tag}]  推送 Top5:")
    for r in top5:
        log(f"    {r['code']} {r['name']:<8} total={r['total']:.1f}")

    try:
        from plays.limit_up.pipeline import push_feishu
        push_feishu(top5)
        log(f"[{tag}]  已推送飞书")
    except Exception as e:
        log(f"[{tag}]  推送失败: {e}")


# ── 步骤 3: 竞价重评 ──

def step_auction(trade_date: str):
    """获取竞价数据，重跑评分。"""
    log("[09:26] 拉取竞价数据...")
    resp = call_tushare("stk_auction", {"trade_date": trade_date},
                        "ts_code,vol,amount,turnover_rate,price,pre_close")
    items = resp.get("data", {}).get("items", [])
    log(f"  竞价 {len(items)} 条")
    step_score(trade_date, is_auction=True)


# ── 主循环 ──

def main_loop():
    global _running, _FORCE

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    log("每日预测守护进程启动")

    if _FORCE:
        log("[强制模式] 忽略时间节点，按序执行后退出")
        td = datetime.now().strftime("%Y%m%d")
        step_concept_cache()
        step_score(td)
        step_auction(td)
        log("[强制模式] 全部完成")
        return

    while _running:
        now = datetime.now()
        td = now.strftime("%Y%m%d")
        hhmm = now.hour * 100 + now.minute

        if not _is_trade_day(td):
            time.sleep(3600)
            continue

        # 00:30 概念 + 评分
        if 30 <= hhmm < 100 and now.hour == 0:
            step_concept_cache()
            step_score(td)
            log("[00:30+] 凌晨评分完成，等待 09:26")
            time.sleep(60)

        # 09:26 竞价重评
        if 926 <= hhmm < 930 and now.hour == 9:
            step_auction(td)
            log("[09:26+] 竞价评分完成，今日工作结束")
            time.sleep(60)

        time.sleep(30)


def main():
    global _FORCE
    parser = argparse.ArgumentParser(description="每日预测守护进程")
    parser.add_argument("--force", action="store_true",
                        help="强制模式：忽略时间节点，按序执行所有步骤后退出")
    args = parser.parse_args()
    if args.force:
        _FORCE = True
    log("[daily] 启动")
    main_loop()


if __name__ == "__main__":
    main()
