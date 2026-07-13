#!/usr/bin/env python3
"""打板玩法主循环 Daemon — 三层评分架构。

数据流：
  ① scanner.scan_batch(候选池) → 涨幅+涨速 → 栈排序
  ② 栈顶 N 只 → WS L1 → 粗评(实时 bid/ask/内外盘)
  ③ 粗评[45,55) → WS L2/L10 → VWAP/卖压确认 → 推送
  ④ 粗评≥55    → 直接推送
  ⑤ 粗评<45    → 丢弃

用法：
    python plays/limit_up/pipeline.py --daemon
    python plays/limit_up/pipeline.py --daemon --sim-time 0930 --sim-rounds 3
    python plays/limit_up/pipeline.py              # 非 daemon 跑一次即退出
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"
HEALTH_DIR = DATA_DIR / "health"
HEALTH_DIR.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(PROJECT_DIR))

from plays.limit_up.pool_builder import ensure_pool
from plays.limit_up.stack import ScoreStack, save_queue, load_queue, clear_queue
from plays.limit_up.filter import filter_realtime
from plays.limit_up.scanner import scan_batch, build_name_map
from plays.limit_up.pusher import check_and_push
from plays.limit_up.utils import is_trading_time
from scripts.ths_client import get_ths_client

# ── 配置（可被环境变量覆盖，方便测试）──
STAGE1_TOP_N = int(os.environ.get("STAGE1_TOP_N", "100"))
PUSH_THRESHOLD = float(os.environ.get("ULTIMATE_PUSH_THRESHOLD", "55"))
L2_GREY_LOW = int(os.environ.get("L2_GREY_LOW", "45"))
POOL_TIME = int(os.environ.get("POOL_TIME", "915"))        # 建池时间
TRADE_START = int(os.environ.get("TRADE_START", "930"))     # 开盘时间
TRADE_END = int(os.environ.get("TRADE_END", "1130"))        # 上午收盘时间
TRADE_PM_START = int(os.environ.get("TRADE_PM_START", "1300"))  # 下午开盘时间
TRADE_PM_END = int(os.environ.get("TRADE_PM_END", "1500"))      # 下午收盘时间
FEISHU_TEST_MODE = os.environ.get("FEISHU_TEST_MODE", "").lower() == "true"
HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "60"))

_running = True

# 时间模拟
_SIM_TIME: datetime | None = None
_SIM_TICK: int = 0


def _signal_handler(sig, frame):
    global _running
    print(f"[pipeline] 收到信号 {sig}，正在关闭...")
    _running = False


def _now() -> datetime:
    """返回当前时间（模拟模式下返回模拟时间，不自动推进）。"""
    return _SIM_TIME if _SIM_TIME is not None else datetime.now()


def _sim_sleep(seconds: int):
    """模拟模式下的 sleep：推进模拟时间而非真实等待。"""
    global _SIM_TIME
    if _SIM_TIME is not None:
        _SIM_TIME = _SIM_TIME + timedelta(seconds=seconds)
    else:
        time.sleep(seconds)


def _hhmm() -> int:
    n = _now()
    return n.hour * 100 + n.minute


def _today_str() -> str:
    return _now().strftime("%Y%m%d")


# ===== 交易日判断 =====

_TRADE_DAY_CACHE: dict[str, bool] = {}


def _is_trade_day(date_str: str) -> bool:
    """判断某天是否为交易日。结果缓存。"""
    if date_str in _TRADE_DAY_CACHE:
        return _TRADE_DAY_CACHE[date_str]
    try:
        from scripts.tu_share import call_tushare
        result = call_tushare(
            "trade_cal", {"cal_date": date_str},
            "exchange,cal_date,is_open",
        )
        items = result.get("data", {}).get("items", [])
        for row in items:
            opened = int(row[2]) if len(row) > 2 else 0
            _TRADE_DAY_CACHE[date_str] = opened == 1
            return opened == 1
    except Exception:
        pass
    _TRADE_DAY_CACHE[date_str] = False
    return False


# ===== 向后兼容（旧策略引用） =====

_REALTIME_FUND_CACHE: dict = {}
_REALTIME_FUND_TS: str = ""


def _get_realtime_fund_cache():
    """兼容旧策略 import。盘后用 Tushare 兜底。"""
    global _REALTIME_FUND_CACHE, _REALTIME_FUND_TS
    td = _today_str()
    if _REALTIME_FUND_CACHE and _REALTIME_FUND_TS == td:
        return _REALTIME_FUND_CACHE
    from plays.limit_up.utils import batch_get_fundflow_tushare
    cache = batch_get_fundflow_tushare(td)
    if cache:
        _REALTIME_FUND_CACHE = cache
        _REALTIME_FUND_TS = td
    return cache or {}


# 向后兼容：THS 行情缓存（旧策略引用）
_THS_QUOTE_CACHE: dict = {}
_REALTIME_PCT_CACHE: dict = {}
_REALTIME_PCT_TS: str = ""


# ===== WS 管理 =====

_WS_CLIENT = None
_WS_L1_CODES: set[str] = set()
_WS_L2_CODES: set[str] = set()
_WS_CONNECTED_TODAY = False


def _ensure_ws():
    """懒加载 jvQuant WS。"""
    global _WS_CLIENT
    if _WS_CLIENT is None:
        from scripts.jvquant_ws_client import JvQuantWSClient
        _WS_CLIENT = JvQuantWSClient()
    return _WS_CLIENT


def _ensure_ws_connected() -> bool:
    """确保 WS 已连接。自动重连。返回是否连接成功。"""
    global _WS_CLIENT, _WS_CONNECTED_TODAY, _WS_L1_CODES, _WS_L2_CODES

    now = _now()
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < market_open:
        return False
    if not is_trading_time():
        return False

    ws = _ensure_ws()
    try:
        if ws.is_connected():
            _WS_CONNECTED_TODAY = True
            return True
    except Exception:
        # is_connected() 本身失败时，降级为重新创建连接
        _WS_CLIENT = None

    print(f"[pipeline] WS 重连中...")
    try:
        ws = _ensure_ws()
        ws.connect()
        _WS_CONNECTED_TODAY = True
        _WS_L1_CODES.clear()
        _WS_L2_CODES.clear()
        print(f"[pipeline] WS 已重连")
        return True
    except Exception as e:
        print(f"[pipeline] WS 重连失败: {e}")
        return False


def _close_ws():
    """关闭 WS 连接。daemon 退出时调用。"""
    global _WS_CLIENT
    if _WS_CLIENT is not None:
        try:
            _WS_CLIENT.disconnect()
            print("[pipeline] WS 已关闭")
        except Exception as e:
            print(f"[pipeline] WS 关闭异常: {e}")
        _WS_CLIENT = None


def _close_pool():
    """关闭评分线程池。daemon 退出时调用。"""
    global _SCORE_POOL
    if _SCORE_POOL is not None:
        try:
            _SCORE_POOL.shutdown(wait=True)
            print("[pipeline] 评分线程池已关闭")
        except Exception as e:
            print(f"[pipeline] 评分线程池关闭异常: {e}")


def _subscribe_l1(codes_short: list[str]):
    if not _ensure_ws_connected():
        return 0
    ws = _ensure_ws()
    new = [c for c in codes_short if c not in _WS_L1_CODES]
    if new:
        n = ws.subscribe_l1(new)
        _WS_L1_CODES.update(new)
        return n
    return 0


def _subscribe_l2(codes_short: list[str]):
    if not _ensure_ws_connected():
        return 0
    ws = _ensure_ws()
    new = [c for c in codes_short if c not in _WS_L2_CODES]
    if new:
        n_l10 = ws.subscribe_l10(new)
        n_l2 = ws.subscribe_l2(new)
        _WS_L2_CODES.update(new)
        return max(n_l10, n_l2)
    return 0


def _unsubscribe_l1(codes_short: list[str]):
    ws = _ensure_ws()
    if ws is None or not ws.is_connected():
        return
    to_remove = [c for c in codes_short if c in _WS_L1_CODES]
    if to_remove:
        try:
            ws.unsubscribe_l1(to_remove)
        except Exception:
            pass
        _WS_L1_CODES.difference_update(to_remove)


def _unsubscribe_l2(codes_short: list[str]):
    ws = _ensure_ws()
    if ws is None or not ws.is_connected():
        return
    to_remove = [c for c in codes_short if c in _WS_L2_CODES]
    if to_remove:
        try:
            ws.unsubscribe_l2(to_remove)
            ws.unsubscribe_l10(to_remove)
        except Exception:
            pass
        _WS_L2_CODES.difference_update(to_remove)


def _sync_subscriptions(current_shorts: set[str]):
    """同步 WS 订阅：只保留当前栈顶股票的 L1/L2 订阅。"""
    l1_to_remove = _WS_L1_CODES - current_shorts
    l2_to_remove = _WS_L2_CODES - current_shorts
    if l1_to_remove:
        _unsubscribe_l1(list(l1_to_remove))
    if l2_to_remove:
        _unsubscribe_l2(list(l2_to_remove))


def _get_l1_snapshot(code_short: str) -> dict:
    """获取实时快照。优先级: WS L1缓存 > jvQuant SQL > 空"""
    ws = _ensure_ws()
    try:
        data = ws.get_market(code_short)
        if data:
            return data
    except Exception:
        pass
    try:
        from scripts.jvquant_client import get_jvquant_client
        client = get_jvquant_client()
        metrics = client.get_intraday_metrics(code_short, _now().strftime("%Y%m%d"))
        if metrics:
            return {
                "last": str(metrics.get("close", 0)),
                "pre_close": "0",
                "vwap": str(metrics.get("vwap", 0)),
                "volume": str(metrics.get("volume", 0)),
                "bid_prices": [],
                "ask_prices": [],
            }
    except Exception:
        pass
    return {}


def _get_l2_data(code_short: str) -> dict:
    ws = _ensure_ws()
    mkt = ws.get_market(code_short)
    vwap = ws.get_vwap(code_short)
    kline = ws.get_kline(code_short, n=5)
    bid1 = float(mkt.get("bid_prices", [0])[0]) if mkt and mkt.get("bid_prices") else 0
    ask1 = float(mkt.get("ask_prices", [0])[0]) if mkt and mkt.get("ask_prices") else 0
    last = float(mkt.get("last", 0)) if mkt else 0
    return {
        "last": last,
        "bid1": bid1,
        "ask1": ask1,
        "vwap": round(vwap, 2) if vwap else None,
        "kline_bars": len(kline) if kline else 0,
    }


# ===== 评分 =====

WEIGHTS = {
    "fundamental": 0.5,
    "technical": 0.5,
    "fundflow": 1.5,
    "sentiment": 1.0,
    "shortterm": 0.5,
}

# 模块级线程池，避免每只股票新建
_SCORE_POOL = ThreadPoolExecutor(max_workers=5)


def _weighted_total_score(scores: dict[str, float]) -> float:
    """Top-3 加权总分。"""
    dc = [(scores.get(d, 0), WEIGHTS.get(d, 1.0)) for d in WEIGHTS]
    dc.sort(key=lambda x: x[0] * x[1], reverse=True)
    top3 = dc[:3]
    weight_sum = sum(w for _, w in top3)
    if weight_sum <= 0:
        return 0.0
    total = sum(s * w for s, w in top3) / weight_sum
    return round(total, 2)


def _raw_score(code: str, name: str, realtime: dict | None = None,
               l2_data: dict | None = None) -> dict:
    """五维度并行评分。"""
    from plays.limit_up.strategies.fundamental import score_fundamental
    from plays.limit_up.strategies.technical import score_technical
    from plays.limit_up.strategies.fundflow import score_fundflow
    from plays.limit_up.strategies.sentiment import score_sentiment
    from plays.limit_up.strategies.shortterm import score_shortterm

    funcs: dict[str, Callable] = {
        "fundamental": score_fundamental,
        "technical": score_technical,
        "fundflow": score_fundflow,
        "sentiment": score_sentiment,
        "shortterm": score_shortterm,
    }

    scores: dict[str, float] = {}
    reasons: dict[str, str] = {}

    futures = {_SCORE_POOL.submit(fn, code): dim for dim, fn in funcs.items()}
    for future in as_completed(futures):
        dim = futures[future]
        try:
            s, r = future.result()
            scores[dim] = float(s)
            reasons[dim] = str(r)
        except Exception as e:
            scores[dim] = 0.0
            reasons[dim] = f"评分异常: {e}"

    total_score = _weighted_total_score(scores)
    rc = sum(1 for d in funcs if scores.get(d, 0) >= 75)

    result = {
        "code": code,
        "name": name,
        "scores": scores,
        "reasons": reasons,
        "total_score": total_score,
        "top3_score": total_score,
        "score_mode": "daemon_weighted",
        "pct_chg": round(float(realtime.get("pct_chg", 0) or 0), 2) if realtime else 0,
        "resonance": {"count": rc, "threshold": 75, "is_resonance": rc >= 3},
    }
    if l2_data:
        result["l2api"] = l2_data
    return result


def _run_one_round(pool_codes: list[str], pool_name_map: dict[str, str],
                    iter_count: int | None = None,
                    stack: ScoreStack | None = None) -> tuple[list[dict], ScoreStack]:
    """执行一轮完整流程：扫描 → 过滤 → 栈排序 → 粗评 → 精评决策。

    供 main_loop（daemon 模式）和 _run_once（单次模式）复用。

    所有进入粗评的股票都会存档（save_analysis），但只有满足推送条件的
    （≥PUSH_THRESHOLD 或 L2 灰区确认通过）才会触发飞书推送（check_and_push）。

    Args:
        pool_codes: 候选股代码列表。
        pool_name_map: {code: name} 映射。
        iter_count: 轮次编号，用于打印；None 时不打印详细日志。
        stack: 外部传入的 ScoreStack，main_loop 需要跨轮复用并保存队列。
            不传则内部新建，适用于 _run_once。

    Returns:
        (all_results, stack) — all_results 包含所有进入粗评的股票。
    """
    if stack is None:
        stack = ScoreStack()
    if iter_count is not None:
        print(f"\n[{_now().strftime('%H:%M')}] 第{iter_count}轮 ① batch_quotes {len(pool_codes)}只...")
    quotes = scan_batch(pool_codes)
    filtered_quotes = {}

    # 刷新概念热点缓存（基于涨幅榜 API）
    try:
        from plays.limit_up.strategies.concept_cache import refresh_concept_limit_ups
        refresh_concept_limit_ups()
    except Exception:
        pass

    for code, q in quotes.items():
        if q is None:
            continue
        vetoed, reason = filter_realtime(q)
        if not vetoed:
            filtered_quotes[code] = q

    stack.update(filtered_quotes, name_map=pool_name_map)
    if iter_count is not None:
        print(f"    栈: {stack.size}只待评分 | batch {len(quotes)}只 ✓")

    to_score = stack.pop_top(STAGE1_TOP_N)
    if not to_score:
        if iter_count is not None:
            print(f"  ② 粗评: 无待评分股票")
        return [], stack

    if iter_count is not None:
        print(f"  ② 粗评 {len(to_score)}只(L1)...")
    score_data = [
        (item.code, item.name or pool_name_map.get(item.code, ""),
         quotes.get(item.code, {"pct_chg": item.pct_chg, "speed": item.speed}))
        for item in to_score
    ]
    rough_results = stage1_rough(score_data)

    # ── 模型分覆盖：用 XGBoost 替代老的加权总分 ──
    try:
        from plays.limit_up.factors.optimized.model_score import factor_model_score_batch
        from plays.limit_up.strategies import factor_ctx
        from plays.limit_up.utils import safe_float
        import pandas as pd
        # 预热 factor_ctx 缓存（逐只懒加载 daily/daily_basic/limit_list_d）
        code_list = [r["code"] for r in rough_results]
        for c in code_list:
            factor_ctx.ensure_data(c)
        feats_list = []
        for r in rough_results:
            code = r["code"]
            # PIT 特征
            feats = factor_ctx.get_price_features(code)
            basic = factor_ctx.get_daily_basic(code)
            if basic:
                feats["turnover_rate"] = safe_float(basic.get("turnover_rate", 5.0))
                feats["volume_ratio"] = safe_float(basic.get("volume_ratio", 1.0))
                feats["circ_mv"] = safe_float(basic.get("circ_mv", 0.0))
                feats["pe"] = safe_float(basic.get("pe", 999.0))
                feats["pb"] = safe_float(basic.get("pb", 999.0))
            feats["limit_up_count_20d"] = float(factor_ctx.get_limit_up_count(code, 20))
            feats["limit_up_count_60d"] = float(factor_ctx.get_limit_up_count(code, 60))
            # 策略维度分
            for dim in ("sentiment", "shortterm", "technical", "fundflow", "fundamental"):
                feats[dim] = r["scores"].get(dim, 0)
            feats_list.append(feats)
        model_scores = factor_model_score_batch(pd.DataFrame(feats_list))
        for i, r in enumerate(rough_results):
            ms = round(float(model_scores.iloc[i]), 2)
            r["factors"] = {"model_score": ms}
            r["total_score"] = ms
            r["score_mode"] = "model_score"
        print(f"    模型分 ✓")
    except Exception as e:
        print(f"    模型分失败, 回退加权总分: {e}")

    # 同步 WS 订阅：只保留当前栈顶（WS 使用短代码）
    current_shorts = {item.code.split(".")[0] for item in to_score}
    _sync_subscriptions(current_shorts)

    all_results = []      # 所有进入评分的都存档
    push_candidates = []  # 只有满足推送条件的才推

    for r in rough_results:
        score = r.get("total_score", 0)
        if score >= PUSH_THRESHOLD:
            if iter_count is not None:
                print(f"    ≥{PUSH_THRESHOLD:.0f} {r['code']} {r['name']} total_score={score:.1f} → 推送")
            all_results.append(r)
            push_candidates.append(r)
        elif score >= L2_GREY_LOW:
            if iter_count is not None:
                print(f"    [{L2_GREY_LOW},{PUSH_THRESHOLD:.0f}) {r['code']} {r['name']} total_score={score:.1f} → L2确认...")
            confirmed = stage2_deep(r["code"], r["name"], score)
            if confirmed:
                confirmed["scores"] = r.get("scores", {})
                confirmed["reasons"] = r.get("reasons", {})
                confirmed["pct_chg"] = r.get("pct_chg", 0)
                confirmed["resonance"] = r.get("resonance", {})
                confirmed["score_mode"] = r.get("score_mode", "model_score")
                confirmed["factors"] = r.get("factors", {})
                if iter_count is not None:
                    print(f"      L2通过 → 推送")
                all_results.append(confirmed)
                push_candidates.append(confirmed)
            else:
                # L2 拒绝：仍然存档原始粗评结果，但不推送
                if iter_count is not None:
                    print(f"      L2拒绝 (存档不推送)")
                all_results.append(r)
        else:
            if iter_count is not None:
                print(f"    <{L2_GREY_LOW} {r['code']} {r['name']} total_score={score:.1f} → 存档不推送")
            all_results.append(r)

    save_analysis(all_results)
    check_and_push(push_candidates, DATA_DIR)
    return all_results, stack


def stage1_rough(codes_with_names: list[tuple[str, str, dict]]) -> list[dict]:
    """粗评。"""
    from plays.limit_up.strategies.realtime_ctx import set_l1_snapshots

    shorts = [c.split(".")[0] for c, _, _ in codes_with_names]
    _subscribe_l1(shorts)
    _sim_sleep(1)

    l1_snapshots = {}
    for code, _, _ in codes_with_names:
        short = code.split(".")[0]
        snap = _get_l1_snapshot(short)
        if snap:
            l1_snapshots[code] = snap
    if l1_snapshots:
        set_l1_snapshots(l1_snapshots)

    results = []
    for code, name, realtime in codes_with_names:
        short = code.split(".")[0]
        l1 = _get_l1_snapshot(short)
        merged = dict(realtime) if realtime else {}
        if l1:
            merged["l1"] = l1
        result = _raw_score(code, name, realtime=merged)
        results.append(result)
    return results


def stage2_deep(code: str, name: str, total_score: float) -> dict | None:
    """灰色区间(45-55)用 L2/L10 确认。"""
    short = code.split(".")[0]
    _subscribe_l2([short])
    _sim_sleep(1)

    ws = _ensure_ws()
    mkt = ws.get_market(short)
    vwap = ws.get_vwap(short)
    bid1 = float(mkt.get("bid_prices", [0])[0]) if mkt and mkt.get("bid_prices") else 0
    ask1 = float(mkt.get("ask_prices", [0])[0]) if mkt and mkt.get("ask_prices") else 0
    last = float(mkt.get("last", 0)) if mkt else 0

    result = {
        "code": code,
        "name": name,
        "total_score": total_score,
        "score_mode": "daemon_weighted",
        "l2api": {"last": last, "bid1": bid1, "ask1": ask1,
                  "vwap": round(vwap, 2) if vwap else None},
    }

    if vwap and vwap > 0 and last > 0:
        vwap_dev = (last - vwap) / vwap
        if vwap_dev > 0.05:
            print(f"    L2 拒绝: VWAP偏离{vwap_dev*100:.1f}% > 5%（诱多）")
            return None
    if bid1 > 0 and ask1 > 0 and ask1 > bid1 * 3:
        print(f"    L2 拒绝: 卖压({ask1:.0f}) > 买压({bid1:.0f}) ×3")
        return None
    return result


def save_analysis(results: list[dict]):
    td = _today_str()
    ts = _now().strftime("%H%M")
    analysis_dir = DATA_DIR / "analysis"
    analysis_dir.mkdir(exist_ok=True)
    path = analysis_dir / f"{td}_{ts}.json"
    with open(path, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"  [analysis] 已保存 {path} ({len(results)} 只)")


# ===== 心跳 =====

_PIDFILE = HEALTH_DIR / "pipeline_daemon.pid"
_HEARTBEAT_FILE = HEALTH_DIR / "pipeline_heartbeat.json"


def _write_pidfile():
    try:
        _PIDFILE.write_text(str(os.getpid()))
    except Exception:
        pass


def _write_heartbeat():
    try:
        data = {
            "pid": os.getpid(),
            "ts": datetime.now().isoformat(),
            "epoch": time.time(),
        }
        with open(_HEARTBEAT_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass


def _remove_pidfile():
    try:
        if _PIDFILE.exists():
            _PIDFILE.unlink()
    except Exception:
        pass


# ===== 主循环 =====

def _is_trading_session(hhmm: int) -> bool:
    """判断当前时间是否在交易时段内。"""
    return (TRADE_START <= hhmm < TRADE_END) or (TRADE_PM_START <= hhmm < TRADE_PM_END)


def main_loop():
    global _running
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    ths = get_ths_client()
    stack = ScoreStack()
    pool_built = False
    trading_started = False
    pool_codes: list[str] = []
    pool_name_map: dict[str, str] = {}
    iter_count = 0
    sim_rounds = int(os.environ.get("_SIM_ROUNDS", "0"))

    _write_pidfile()

    print(f"[pipeline] 启动，当前时间 {_now().strftime('%H:%M')}")
    print(f"[pipeline] 节点: {POOL_TIME//100:02d}:{POOL_TIME%100:02d} 建池 | {TRADE_START//100:02d}:{TRADE_START%100:02d} 开扫")

    while _running:
        try:
            now = _now()
            today_str = _today_str()
            hhmm = _hhmm()

            # ── 非交易日跳过 ──
            if not _is_trade_day(today_str):
                _sim_sleep(3600 if _SIM_TIME is not None else 3600)
                continue

            # ── 候选池 ──
            if not pool_built and hhmm >= POOL_TIME:
                print(f"[{now.strftime('%H:%M')}] ① 构建候选池...")
                pool = ensure_pool(_today_str())
                pool_codes = [s["code"] for s in pool]
                pool_name_map = build_name_map(pool)
                print(f"    候选池 {len(pool)} 只 ✓")
                pool_built = True

            # ── 交易时段 ──
            trading = _is_trading_session(hhmm)
            if not trading or not pool_built:
                _sim_sleep(60 if _SIM_TIME is not None else 1)
                continue

            if not trading_started:
                print(f"[{now.strftime('%H:%M')}] ② 开始盘中扫描")
                trading_started = True

            # ── 执行一轮扫描评分 ──
            iter_count += 1
            t0 = time.time()
            deep_results, stack = _run_one_round(pool_codes, pool_name_map, iter_count=iter_count, stack=stack)

            save_queue(stack, _today_str())
            _write_heartbeat()
            elapsed = time.time() - t0
            print(f"  [完成] {elapsed:.1f}s")

            if sim_rounds and iter_count >= sim_rounds:
                print(f"[pipeline] 模拟完成 {iter_count} 轮，退出")
                break
        except Exception as e:
            print(f"[pipeline] 第{iter_count}轮异常: {e}")
            import traceback
            traceback.print_exc()
            _sim_sleep(10 if _SIM_TIME is not None else 10)

    _close_ws()
    _close_pool()
    _remove_pidfile()
    print("[pipeline] 已停止")


def main():
    global _SIM_TIME, _SIM_TICK
    parser = argparse.ArgumentParser(description="打板 Daemon")
    parser.add_argument("--daemon", action="store_true",
                        help="常驻 daemon 模式；不带此参数跑一次即退出")
    parser.add_argument("--sim-time", type=str, help="模拟时间 HHMM")
    parser.add_argument("--sim-tick", type=int, default=0,
                        help="模拟模式每次循环推进的秒数（默认0=不推进）")
    parser.add_argument("--sim-rounds", type=int, default=0,
                        help="模拟模式运行 N 轮后退出（0=无限）")
    args = parser.parse_args()

    if args.sim_time:
        hhmm = args.sim_time.strip()
        _SIM_TIME = datetime.now().replace(
            hour=int(hhmm[:2]), minute=int(hhmm[2:]),
            second=0, microsecond=0,
        )
        _SIM_TICK = args.sim_tick
        from plays.limit_up import utils
        _real_is_trading = utils.is_trading_time
        def _is_trading_at(dt):
            if dt.weekday() >= 5:
                return False
            h, m = dt.hour, dt.minute
            if h < 9 or (h == 9 and m < 30):
                return False
            if h >= 15:
                return False
            if h == 11 and m >= 30:
                return False
            if h == 12:
                return False
            return True
        utils.is_trading_time = lambda: _is_trading_at(_now())

        print(f"[pipeline] 模拟模式: 起始 {_SIM_TIME.strftime('%H:%M')} "
              f"每轮+{_SIM_TICK}s 上限{args.sim_rounds}轮")

    if args.sim_rounds:
        os.environ["_SIM_ROUNDS"] = str(args.sim_rounds)

    if args.daemon or args.sim_time:
        main_loop()
    else:
        # 非 daemon 模式：跑一次完整扫描评分后退出
        _run_once()


def _run_once():
    """非 daemon 模式：执行一轮完整流程后退出（供旧 CLI/LLM 调用）。"""
    from datetime import datetime
    td = _today_str()
    if not _is_trade_day(td):
        print("[pipeline] 非交易日，跳过")
        return

    print(f"[{_now().strftime('%H:%M')}] 单次扫描模式")
    pool = ensure_pool(td)
    pool_codes = [s["code"] for s in pool]
    pool_name_map = build_name_map(pool)

    deep_results, _ = _run_one_round(pool_codes, pool_name_map)
    print(f"[pipeline] 单次扫描完成，分析 {len(deep_results)} 只")


if __name__ == "__main__":
    main()
