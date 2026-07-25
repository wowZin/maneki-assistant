#!/usr/bin/env python3
"""打板早盘评分（一次性进程，hermes cron 09:30 触发，非常驻）。

流程（2026-07-25 重构）：
  1. 交易日判断（非交易日直接退出）
  2. stk_auction 按日期全量拉当日竞价（重试3次，持续失败→写日志+飞书通知一次）
  3. 竞价数据刷新面板：auc_amount/auc_vol/auc_amt_ratio/auc_vol_ratio/auc_pct，
     并重算 shortterm 维度分（唯一吃竞价数据的维度，panel_builder 同公式）
  4. 全量静态模型评分（面板 64 特征 + auc_pct 覆盖 pct_chg_score_day）
  5. model_score 写回面板；≥55 写 analysis + pushed + 飞书推送
  6. 任何未捕获异常 → crash 日志 + 飞书通知 + 非零退出（无心跳/无常驻）

用法：
    python3 plays/limit_up/pipeline.py                     # 跑一次（cron 调用）
    python3 plays/limit_up/pipeline.py --date 20260724     # 指定日期（测试用）

日志：logs/pipeline.log（RotatingFileHandler）+ stdout（cron 输出）。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent
HEALTH_DIR = PLAY_DIR / "data" / "health"
LOG_DIR = PROJECT_DIR / "logs"
HEALTH_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.utils import _is_trade_day, _today_str  # noqa: E402

PUSH_THRESHOLD = float(os.environ.get("ULTIMATE_PUSH_THRESHOLD", "55"))
PANEL_FILE = lambda td: PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel" / f"{td}.parquet"  # noqa: E731
ANALYSIS_FILE = lambda td: PLAY_DIR / "data" / "analysis" / f"{td}.json"  # noqa: E731

# ── 日志：文件（滚动）+ stdout ──
log = logging.getLogger("pipeline")
log.setLevel(logging.INFO)
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%m-%d %H:%M:%S")
    _fh = RotatingFileHandler(LOG_DIR / "pipeline.log", maxBytes=5 * 1024 * 1024,
                              backupCount=3, encoding="utf-8")
    _fh.setFormatter(_fmt)
    log.addHandler(_fh)
    _sh = logging.StreamHandler(sys.stdout)
    _sh.setFormatter(_fmt)
    log.addHandler(_sh)


def _notify_text(text: str):
    """飞书文本通知（失败告警专用，不经 pusher 的 9:30 交易时间闸）。"""
    try:
        import requests
        from dotenv import load_dotenv
        load_dotenv(PROJECT_DIR / ".env")
        app_id = os.getenv("FEISHU_APP_ID", "")
        app_secret = os.getenv("FEISHU_APP_SECRET", "")
        chat_id = os.getenv("FEISHU_CHAT_ID_SIGNAL", os.getenv("FEISHU_BOT_CHAT_ID", ""))
        if not (app_id and app_secret and chat_id):
            return
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
        token = resp.json().get("tenant_access_token", "")
        if not token:
            return
        requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": chat_id, "msg_type": "text",
                  "content": json.dumps({"text": text})}, timeout=10)
    except Exception as e:
        log.warning(f"飞书通知失败: {e}")


def _refresh_panel_auction(today: str) -> bool:
    """① 竞价刷新面板：stk_auction 按日期全量（禁逐股），重试3次。

    刷新列：auc_amount/auc_vol/auc_pct，重算 auc_amt_ratio(÷avg_amount_5d)、
    auc_vol_ratio(÷T-1 vol)，并同步重算 shortterm 维度分（唯一吃竞价的策略分，
    公式与 panel_builder._add_strategy_scores 一致：base10 + 量比分档 5/10/20）。
    持续失败：写 pipeline_crash.log + 飞书通知一次，返回 False（不阻断，
    面板保留 T-1 夜间竞价值兜底）。
    """
    import pandas as pd
    from scripts.tu_share import call_tushare as _ct

    panel_file = PANEL_FILE(today)
    if not panel_file.exists():
        log.error(f"面板不存在: {panel_file}")
        return False

    # ── 拉取竞价（9:25 快照，按 trade_date 全市场一次调用）──
    items, fields = [], []
    for attempt in range(3):
        try:
            r = _ct("stk_auction", {"trade_date": today},
                    "ts_code,trade_date,amount,vol,price,pre_close", timeout=120)
            items = r.get("data", {}).get("items", [])
            fields = r.get("data", {}).get("fields", [])
            if items:
                break
        except Exception as e:
            log.warning(f"竞价拉取失败(attempt {attempt + 1}/3): {e}")
        time.sleep(2 * (attempt + 1))

    if not items:
        msg = f"{today} 竞价数据(stk_auction)连续3次拉取失败，面板 auc_* 维持 T-1 夜间值"
        with open(HEALTH_DIR / "pipeline_crash.log", "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {msg}\n")
        _notify_text(f"⚠️ [pipeline] {msg}")
        log.error(msg)
        return False

    auc = {}
    for row in items:
        d = dict(zip(fields, row))
        c = d.get("ts_code", "")
        if c:
            amt = float(d.get("amount") or 0)
            vol = float(d.get("vol") or 0)
            price = float(d.get("price") or 0)
            pre = float(d.get("pre_close") or 0)
            pct = (price / pre - 1.0) * 100 if pre > 0 and price > 0 else None
            auc[c] = (amt, vol, pct)
    log.info(f"竞价拉取成功: 全市场 {len(auc)} 只")

    # T-1 日 vol（auc_vol_ratio 分母，与 pit_features 同口径）
    from plays.limit_up.backtest.dataset import _trade_dates
    prev_days = _trade_dates(
        (datetime.strptime(today, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d"), today)
    prev_date = prev_days[-2] if len(prev_days) >= 2 else today
    t1_vol = {}
    daily_file = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel" / "daily" / f"{prev_date}.parquet"
    if daily_file.exists():
        _d = pd.read_parquet(daily_file, columns=["ts_code", "vol"])
        t1_vol = dict(zip(_d["ts_code"], _d["vol"]))

    # ── 写回面板 ──
    df = pd.read_parquet(panel_file)
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

    # shortterm 是五维度中唯一吃竞价（auc_amt_ratio）的策略分，随竞价重算；
    # 其余四个维度（fundamental/technical/fundflow/sentiment）全是 T-1 静态输入
    if "shortterm" in df.columns:
        _ar = df["auc_amt_ratio"].fillna(0).astype(float)
        df["shortterm"] = (10.0 + _ar.map(
            lambda a: 20 if a > 1 else (10 if a > 0.5 else (5 if a > 0.1 else 0)))).clip(upper=100)

    df.to_parquet(panel_file, index=False)
    log.info(f"面板竞价刷新完成: {n_hit}/{len(df)} 只 (auc_*+auc_pct+shortterm)")
    return True


def morning_pass(today: str) -> list[dict]:
    """② 全量静态模型评分（不拉实时行情，pct 用竞价涨幅 auc_pct）。

    数据源：analysis/{today}.json 预评池（panel_builder 00:01 产物，≥35 预评，
    含名称与五维度分）∩ 当日面板特征。
    产出：model_score 写回面板；全部记录合并写 analysis（按 code 去重覆盖，
    维度分继承预评记录）；≥PUSH_THRESHOLD 经 pusher 推送 + 落 pushed/。
    """
    import pandas as pd
    from plays.limit_up.factors.optimized.model_score import factor_model_score_batch
    from plays.limit_up.pusher import check_and_push

    af = ANALYSIS_FILE(today)
    if not af.exists():
        raise RuntimeError(f"预评分池不存在: {af}（panel_builder 未运行？）")
    pool = json.loads(af.read_text())
    pool_recs = {r["code"]: r for r in pool if isinstance(r, dict) and r.get("code")}
    pool_codes = list(pool_recs.keys())
    log.info(f"预评池 {len(pool_codes)} 只")

    panel_file = PANEL_FILE(today)
    if not panel_file.exists():
        raise RuntimeError(f"面板不存在: {panel_file}")
    pit_df = pd.read_parquet(panel_file)
    pit_df = pit_df[pit_df["code"].isin(pool_codes)].copy()
    if pit_df.empty:
        raise RuntimeError(f"面板与预评池无交集 ({len(pool_codes)} 只)")

    # 竞价涨幅作为当日涨幅特征（09:30 静态评分，连续竞价尚未开始）
    if "auc_pct" in pit_df.columns:
        has_pct = pit_df["auc_pct"].notna()
        pit_df.loc[has_pct, "pct_chg_score_day"] = pit_df.loc[has_pct, "auc_pct"].astype(float)

    # ── XGBoost 批量评分（grid_v1，64 特征 hit-only）──
    t0 = time.time()
    pit_df["model_score"] = factor_model_score_batch(pit_df)
    _scores = pit_df["model_score"]
    log.info(f"模型评分完成: {len(pit_df)} 只, {time.time() - t0:.1f}s, "
             f"max={_scores.max():.1f} mean={_scores.mean():.1f} "
             f"≥{PUSH_THRESHOLD:.0f}={int((_scores >= PUSH_THRESHOLD).sum())}只")

    # model_score 写回面板（面板 = T-1 特征 + 当日竞价 + 早盘模型分，终态）
    full_panel = pd.read_parquet(panel_file)
    full_panel["model_score"] = full_panel["code"].map(
        dict(zip(pit_df["code"], pit_df["model_score"])))
    full_panel.to_parquet(panel_file, index=False)
    log.info("model_score 已写回面板")

    # ── 组装记录：维度分继承预评记录（面板列与 analysis 同值，analysis 为准）──
    all_results, push_candidates = [], []
    for _, r in pit_df.iterrows():
        score = float(r["model_score"])
        code = r["code"]
        ap = r.get("auc_pct")
        pct = round(float(ap), 2) if ap is not None and ap == ap else None
        if pct is not None and pct >= 9.8:
            continue  # 竞价已涨停不推
        old = pool_recs.get(code, {})
        rec = {"code": code, "name": old.get("name", "") or code.split(".")[0],
               "model_score": score, "total_score": score,
               "score_mode": "model_score", "pct_chg": pct,
               "scores": old.get("scores", {}),
               "fundamental": old.get("fundamental", float(r.get("fundamental", 0) or 0))}
        all_results.append(rec)
        if score >= PUSH_THRESHOLD:
            push_candidates.append(rec)

    # analysis 合并（按 code 去重覆盖）
    existing = {}
    if af.exists():
        try:
            existing = {r["code"]: r for r in json.loads(af.read_text())}
        except Exception:
            pass
    existing.update({r["code"]: r for r in all_results})
    tmp = af.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(existing.values()), ensure_ascii=False))
    tmp.rename(af)
    log.info(f"analysis 已合并 {len(all_results)} 只 (累计 {len(existing)} 只)")

    pushed = check_and_push(push_candidates, PLAY_DIR / "data")
    log.info(f"推送: ≥{PUSH_THRESHOLD:.0f} 候选 {len(push_candidates)} 只, 新推送 {len(pushed)} 只")
    return all_results


def main():
    parser = argparse.ArgumentParser(description="打板早盘评分（一次性）")
    parser.add_argument("--date", help="指定日期 YYYYMMDD（默认今天，测试用）")
    args = parser.parse_args()

    today = args.date or _today_str()
    log.info(f"===== {today} 早盘流程启动 =====")
    if not _is_trade_day(today):
        log.info(f"{today} 非交易日，退出")
        return

    try:
        _refresh_panel_auction(today)  # 失败不阻断（面板有 T-1 夜间值兜底）
        morning_pass(today)
        log.info(f"===== {today} 早盘流程完成 =====")
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        log.error(f"早盘流程失败: {e}\n{tb}")
        with open(HEALTH_DIR / "pipeline_crash.log", "a") as f:
            f.write(f"[{datetime.now().isoformat()}] {e}\n{tb}\n")
        _notify_text(f"❌ pipeline {today} 早盘评分失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
