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
STAGE1_TOP_N = int(os.environ.get("STAGE1_TOP_N", "50"))
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

# T-1 一次性预取缓存
_T1_MF_CACHE: dict[str, float] = {}
_T1_MF_PREV_CACHE: dict[str, float] = {}
_T1_TL_CACHE: dict[str, dict] = {}
_T1_TI_CACHE: dict[str, list] = {}

# WS 客户端（进程唯一，只由 pipeline 使用）
_WS = None


def _get_ws():
    """懒加载 WS 客户端。"""
    global _WS
    if _WS is None:
        from scripts.jvquant_ws_client import JvQuantWSClient
        _WS = JvQuantWSClient()
    return _WS


def _subscribe_pool(codes: list[str]):
    """开盘时全量订阅 L1+L10。"""
    try:
        ws = _get_ws()
        if not ws.is_connected():
            ws.connect()
        shorts = [c.split(".")[0] for c in codes]
        ws.subscribe_l1(shorts)
        ws.subscribe_l10(shorts)
    except Exception:
        print(f"  [pipeline] WS 订阅失败, 降级 THS")

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

# 向后兼容：THS 行情缓存（旧策略引用）
_THS_QUOTE_CACHE: dict = {}
_REALTIME_PCT_CACHE: dict = {}
_REALTIME_PCT_TS: str = ""


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
                    pool_extras: dict[str, dict] | None = None,
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
    quotes: dict[str, dict] = {}

    _now_ts = __import__("time").time()
    _should_scan = stack.size < 10
    if not _should_scan:
        try:
            _should_scan = _now_ts - getattr(_run_one_round, "last_scan", 0) > 60
        except Exception:
            _should_scan = True

    if _should_scan:
        if iter_count is not None:
            print(f"\n[{_now().strftime('%H:%M')}] ① batch_quotes {len(pool_codes)}只({stack.size}只栈中)...")
        _run_one_round.last_scan = _now_ts
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

        # 涨幅 > 7.0% 的不进栈（避免追高）
        stack.update({k: v for k, v in filtered_quotes.items() if (v.get("pct_chg") or 0) <= 7.0}, name_map=pool_name_map)
        if iter_count is not None:
            print(f"    栈: {stack.size}只待评分 | batch {len(quotes)}只 ✓")
    elif iter_count is not None:
        print(f"    评分(跳过扫盘,栈{stack.size}只)")

    to_score = stack.pop_top(200)
    if not to_score:
        if iter_count is not None:
            print(f"  ② 粗评: 无待评分股票")
        return [], stack

    # Top200 → 随机取 N 只（线程并发时重复概率极低）
    import random
    random.shuffle(to_score)
    to_score = to_score[:20]

    if iter_count is not None:
        print(f"  ② 粗评 {len(to_score)}只(Top200→rand→20)...")
    score_data = [
        (item.code, item.name or pool_name_map.get(item.code, ""),
         quotes.get(item.code, {"pct_chg": item.pct_chg, "speed": item.speed}))
        for item in to_score
    ]
    rough_results = stage1_rough(score_data)

    # ── 模型分覆盖：用 XGBoost 替代老的加权总分 ──
    # 分批处理：每批 10 只，评完立即走 L2/推送，不等全部完
    BATCH_SIZE = 10
    all_results = []
    push_candidates = []
    today_str = _today_str()

    try:
        from plays.limit_up.factors.optimized.model_score import factor_model_score_batch, _load_model
        from plays.limit_up.strategies import factor_ctx
        import pandas as pd
        import math
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from plays.limit_up.strategies.realtime_ctx import get_turnover, get_vol_ratio, _REALTIME_CACHE as _rc
        _model = _load_model()
        _feat_cols = _model.feature_cols if _model else []

        # 预取（全部一次性）
        code_list = [r["code"] for r in rough_results]
        for c in code_list:
            factor_ctx._ensure_daily_limit(c)

        _prev_date = _today_str()
        try:
            from scripts.tu_share import call_tushare as _ct
            _dt = datetime.strptime(_prev_date, "%Y%m%d")
            for _ in range(10):
                _dt -= timedelta(days=1)
                _try = _dt.strftime("%Y%m%d")
                _cal = _ct("trade_cal", {"exchange": "SSE", "start_date": _try, "end_date": _try}, "cal_date,is_open")
                _items = _cal.get("data", {}).get("items", [])
                if _items and _items[0] and len(_items[0]) > 1 and str(_items[0][1]) == "1":
                    _prev_date = _try
                    break
            for _batch in [code_list[:50], code_list[50:]]:
                if not _batch: continue
                _s = ",".join(_batch)
                _r = _ct("moneyflow", {"ts_code": _s, "trade_date": today_str}, "ts_code,net_mf_amount")
                for _it in _r.get("data",{}).get("items",[]): _T1_MF_CACHE[_it[0]] = float(_it[-1]) if len(_it)>1 else 0
                _r2 = _ct("moneyflow", {"ts_code": _s, "trade_date": _prev_date}, "ts_code,net_mf_amount")
                for _it in _r2.get("data",{}).get("items",[]): _T1_MF_PREV_CACHE[_it[0]] = float(_it[-1]) if len(_it)>1 else 0
            _r = _ct("top_list", {"trade_date": today_str}, "ts_code,amount,net_amount,l_buy,l_amount,net_rate")
            for _it in _r.get("data",{}).get("items",[]):
                _f = _r["data"]["fields"]; _T1_TL_CACHE[_it[0]] = dict(zip(_f, _it))
            _r = _ct("top_inst", {"trade_date": today_str}, "ts_code,exalter,net_buy")
            for _it in _r.get("data",{}).get("items",[]):
                _T1_TI_CACHE.setdefault(_it[0], []).append(dict(zip(_r["data"]["fields"], _it)))
            print(f"    资金流{len(_T1_MF_CACHE)}只 龙虎榜{len(_T1_TL_CACHE)}只 ✓")
        except Exception as _e:
            print(f"    资金流/龙虎榜预取失败: {_e}")

        # 并行预取竞价
        _AUC_CACHE: dict[str, dict] = {}
        try:
            from plays.limit_up.strategies.shortterm import _get_auction as _afn
            with ThreadPoolExecutor(max_workers=16) as _p:
                _fu = {_p.submit(_afn, c): c for c in code_list}
                for _f in as_completed(_fu):
                    try:
                        _r = _f.result()
                        if _r: _AUC_CACHE[_fu[_f]] = _r
                    except Exception: pass
            print(f"    竞价{len(_AUC_CACHE)}只 ✓")
        except Exception: pass

        # 分批：每批 10 只，构建特征 → 推理 → 处理结果
        for chunk_start in range(0, len(rough_results), BATCH_SIZE):
            chunk = rough_results[chunk_start:chunk_start + BATCH_SIZE]
            ferats_list = []
            for r in chunk:
                code = r["code"]
                short = code.split(".")[0]
                _sf = lambda v: float(v) if v else 0.0

                _raw = factor_ctx._DAILY_CACHE.get(code, [])
                _daily_rows = sorted(_raw, key=lambda x: x.get("trade_date", ""))
                _pit_date = _daily_rows[-1]["trade_date"] if _daily_rows else today_str

                # basic_by_date
                _basic_ent = {}
                if pool_extras and code in pool_extras:
                    _pe = pool_extras[code]
                    _tr = get_turnover(code); _vr = get_vol_ratio(code)
                    _basic_ent = {"turnover_rate": _pe.get("prev_turnover",5.0), "volume_ratio": _pe.get("prev_vol_ratio",1.0),
                                  "circ_mv": _pe.get("circ_mv",0), "pe": _pe.get("pe",999.0), "pb": _pe.get("pb",999.0)}

                # moneyflow
                _nm = float(_T1_MF_CACHE.get(code,0.0))
                _mf_ent = {"net_mf_amount":_nm, "buy_elg_amount":0, "sell_elg_amount":0, "buy_lg_amount":0, "sell_lg_amount":0} if _nm else {}
                _nm_prev = float(_T1_MF_PREV_CACHE.get(code,0.0)) if code in _T1_MF_PREV_CACHE else 0.0
                _mf_bd = {_pit_date: _mf_ent} if _mf_ent else None
                if _nm_prev and len(_daily_rows) >= 2:
                    _p2 = _daily_rows[-2].get("trade_date","")
                    if _p2:
                        _mf_bd = _mf_bd or {}; _mf_bd[_p2] = {"net_mf_amount":_nm_prev, "buy_elg_amount":0, "sell_elg_amount":0, "buy_lg_amount":0, "sell_lg_amount":0}

                # concept / auction / top
                _cm = factor_ctx.get_concept_momentum(short)
                if not _cm.get("n_concepts",0):
                    from plays.limit_up.strategies.concept_cache import get_concept_limit_ups as _gclu
                    _clu = _gclu(code)
                    _cnames = [k for k in _clu if not k.startswith("_")]
                    _best = max((_clu.get(n,0) for n in _cnames), default=0)
                    _cm["n_concepts"] = float(len(_cnames)); _cm["ret1_avg"] = float(_best); _cm["ret3_max"] = float(_best*2)
                _auc_ent = _AUC_CACHE.get(code, {})
                _tl_ent = _T1_TL_CACHE.get(code, {})
                _ti_list = _T1_TI_CACHE.get(code, [])

                # build_pit_features
                from plays.limit_up.pit_features import build_pit_features as _bpf
                feats = _bpf(code=code, score_date=today_str, daily_rows=_daily_rows,
                    basic_by_date={_pit_date: _basic_ent} if _basic_ent else None,
                    moneyflow_by_date=_mf_bd,
                    auction_by_date={_pit_date: _auc_ent} if _auc_ent else None,
                    concept_momentum=_cm,
                    top_list_by_date={_pit_date: _tl_ent} if _tl_ent else None,
                    top_inst_by_date={_pit_date: _ti_list} if _ti_list else None,
                    pit_mode=True)

                if _rc.get(code,{}).get("inner_vol"):
                    feats["inner_outer"] = (float(feats.get("inner_outer",1)) * 2 + 1) / 3
                if _vr2 is not None: feats["volume_ratio"] = _vr2
                try:
                    _c2 = _rc.get(code,{}); _h = _sf(_c2.get("high")); _l = _sf(_c2.get("low"))
                    if _h and _l and _l > 0: feats["id_range"] = _h/_l - 1.0
                    _amt = _sf(_c2.get("amount")); _a5 = _sf(feats.get("avg_amount_5d",1))
                    if _amt and _a5 > 0: feats["id_amount_ratio"] = _amt/_a5
                except: pass

                for dim in ("sentiment","shortterm","technical","fundflow","fundamental"):
                    feats[dim] = r["scores"].get(dim,0)
                ferats_list.append(feats)

            # 推理 + 覆盖总分
            _df = pd.DataFrame(ferats_list)
            if _feat_cols:
                _fill = sum(1 for c in _feat_cols if c not in _df.columns)
                if _fill > 0:
                    _missing = [c for c in _feat_cols if c not in _df.columns]; print(f"    缺: {','.join(_missing)}")
            model_scores = factor_model_score_batch(_df)
            for i, r in enumerate(chunk):
                ms = round(float(model_scores.iloc[i]), 2)
                r["factors"] = {"model_score": ms}; r["total_score"] = ms; r["score_mode"] = "model_score"

            # 处理本批结果（L2/推送）
            for r in chunk:
                score = r["total_score"]
                _log_snapshot(r, score, quotes.get(r["code"]))
                if score >= PUSH_THRESHOLD:
                    if iter_count is not None: print(f"    ≥{PUSH_THRESHOLD:.0f} {r['code']} {r['name']} total_score={score:.1f} → 推送")
                    all_results.append(r); push_candidates.append(r)
                elif score >= L2_GREY_LOW:
                    if iter_count is not None: print(f"    [{L2_GREY_LOW:.0f},{PUSH_THRESHOLD:.0f}) {r['code']} {r['name']} total_score={score:.1f} → L2确认...")
                    confirmed = stage2_deep(r["code"], r["name"], score)
                    _log_snapshot(r, score, quotes.get(r["code"]))
                    if confirmed:
                        for k in ("scores","reasons","pct_chg","resonance","score_mode","factors"):
                            confirmed[k] = r.get(k, {}) if k in ("scores","reasons","factors") else r.get(k, 0)
                        confirmed["l2_confirmed"] = True
                        if iter_count is not None: print(f"      L2通过 → 推送")
                        all_results.append(confirmed); push_candidates.append(confirmed)
                    else:
                        if iter_count is not None: print(f"      L2拒绝 (存档不推送)")
                        all_results.append(r)
                else:
                    if iter_count is not None: print(f"    <{L2_GREY_LOW} {r['code']} {r['name']} total_score={score:.1f} → 存档不推送")
                    all_results.append(r)

        if _feat_cols:
            print(f"    模型分 ✓")
    except Exception as e:
        print(f"    模型分失败, 回退加权总分: {e}")
        # 回退：全部走一次老流程
        all_results = []
        push_candidates = []
        for r in rough_results:
            score = r.get("total_score", 0)
            if score >= PUSH_THRESHOLD:
                all_results.append(r); push_candidates.append(r)
            elif score >= L2_GREY_LOW:
                confirmed = stage2_deep(r["code"], r["name"], score)
                if confirmed:
                    for k in ("scores","reasons","pct_chg","resonance","score_mode","factors"):
                        confirmed[k] = r.get(k, {}) if k in ("scores","reasons","factors") else r.get(k, 0)
                    confirmed["l2_confirmed"] = True
                    all_results.append(confirmed); push_candidates.append(confirmed)
                else:
                    all_results.append(r)
            else:
                all_results.append(r)

    # WS 订阅由 watchdog 独立管理，pipeline 不再直连

    # debug: 概念缓存状态
    try:
        from plays.limit_up.strategies.concept_cache import _LIMIT_CODES, _CONCEPT_LIMIT_UPS, _REFRESHED
        print(f"    概念缓存: REFRESHED={_REFRESHED} 涨停{len(_LIMIT_CODES)}只 概念{len(_CONCEPT_LIMIT_UPS)}个")
    except Exception:
        pass

    save_analysis(all_results)
    check_and_push(push_candidates, DATA_DIR)
    return all_results, stack


def stage1_rough(codes_with_names: list[tuple[str, str, dict]]) -> list[dict]:
    """粗评（纯策略评分，不依赖 WS L1 订阅）。"""
    # 预取当日资金流 + 龙虎榜（批量一次，供 fundflow 策略缓存读）
    try:
        from plays.limit_up.strategies.fundflow import set_fundflow_cache
        from scripts.tu_share import call_tushare as _ct
        _codes = [c for c, _, _ in codes_with_names]
        _today_str2 = _now().strftime("%Y%m%d")
        _mf = {}
        for _batch in [_codes[:50], _codes[50:]]:
            if not _batch: continue
            _r = _ct("moneyflow", {"ts_code": ",".join(_batch), "trade_date": _today_str2}, "ts_code,net_mf_amount,buy_elg_amount,sell_elg_amount,buy_lg_amount,sell_lg_amount")
            for _it in _r.get("data",{}).get("items",[]):
                _f = _r["data"]["fields"]; _mf[_it[0]] = dict(zip(_f, _it))
        _ti = {}
        _r2 = _ct("top_inst", {"trade_date": _today_str2}, "ts_code,exalter,net_buy")
        for _it in _r2.get("data",{}).get("items",[]):
            _ti.setdefault(_it[0], []).append(dict(zip(_r2["data"]["fields"], _it)))
        set_fundflow_cache(_mf, _ti)
    except Exception:
        pass

    results = []
    for code, name, realtime in codes_with_names:
        result = _raw_score(code, name, realtime=realtime)
        results.append(result)
    return results


def stage2_deep(code: str, name: str, total_score: float) -> dict | None:
    """灰色区间(45-55)用 THS 实时行情确认（VWAP 从 amount/volume 估算）。"""
    short = code.split(".")[0]
    try:
        from scripts.ths_client import get_ths_client as _ths
        q = _ths().get_quote(short)
        if not q:
            print(f"    L2 拒绝: 无 THS 行情")
            return None
        price = float(q.get("price", 0) or 0)
        vol = float(q.get("volume", 0) or 0)
        amt = float(q.get("amount", 0) or 0)
        bid1 = float(q.get("bid1", 0) or 0)
        ask1 = float(q.get("ask1", 0) or 0)
        vwap = amt / (vol * 100) if vol > 0 else 0
        last = price

        result = {
            "code": code,
            "name": name,
            "total_score": total_score,
            "score_mode": "daemon_weighted",
            "l2api": {"last": last, "bid1": bid1, "ask1": ask1,
                      "vwap": round(vwap, 2) if vwap else None},
        }

        if vwap > 0 and last > 0:
            vwap_dev = (last - vwap) / vwap
            if vwap_dev > 0.05:
                print(f"    L2 拒绝: VWAP偏离{vwap_dev*100:.1f}% > 5%（诱多）")
                return None
        if bid1 > 0 and ask1 > 0 and ask1 > bid1 * 3:
            print(f"    L2 拒绝: 卖压({ask1:.0f}) > 买压({bid1:.0f}) ×3")
            return None
        return result
    except Exception as e:
        print(f"    L2 拒绝: THS 行情异常: {e}")
        return None


def _log_snapshot(r: dict, score: float, quote: dict | None = None):
    """每股评分时的实时快照落盘（L1+L2+score），供训练使用。"""
    try:
        now = datetime.now()
        code = r["code"]
        q = quote or {}  # 用已有 batch_quotes 数据，不额外调 THS
        vwap = 0.0
        if q:
            vol = float(q.get("volume", 0) or 0)
            amt = float(q.get("amount", 0) or 0)
            vwap = amt / (vol * 100) if vol > 0 else 0
        rec = {
            "ts": now.strftime("%H:%M:%S"),
            "code": code,
            "name": r.get("name", ""),
            "total_score": score,
            "pct_chg": r.get("pct_chg", 0),
            "price": float(q.get("price", 0)) if q else 0,
            "bid1": float(q.get("bid1", 0)) if q else 0,
            "ask1": float(q.get("ask1", 0)) if q else 0,
            "turnover": float(q.get("turnover", 0)) if q else 0,
            "vol_ratio": float(q.get("vol_ratio", 0)) if q else 0,
            "inner_vol": float(q.get("inner_vol", 0)) if q else 0,
            "outer_vol": float(q.get("outer_vol", 0)) if q else 0,
            "vwap": vwap,
            "fundamental": r.get("scores", {}).get("fundamental", 0),
            "technical": r.get("scores", {}).get("technical", 0),
            "fundflow": r.get("scores", {}).get("fundflow", 0),
            "sentiment": r.get("scores", {}).get("sentiment", 0),
            "shortterm": r.get("scores", {}).get("shortterm", 0),
        }
        import pandas as pd
        from pathlib import Path
        log_dir = Path(__file__).resolve().parent.parent.parent / "data" / "snapshot_log"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{_today_str()}.parquet"
        if path.exists():
            old = pd.read_parquet(path)
            pd.concat([old, pd.DataFrame([rec])], ignore_index=True).to_parquet(path)
        else:
            pd.DataFrame([rec]).to_parquet(path)
    except Exception:
        pass


def _run_pre_scored_round(pool_codes: list[str] | None = None,
                           pool_name_map: dict[str, str] | None = None,
                           pool_extras: dict[str, dict] | None = None,
                           t1_panel: pd.DataFrame | None = None,
                           iter_count: int = 0) -> list[dict]:
    """盘中评分：从预评分池取股，实时 pct_chg 覆盖后批量评分。"""
    from plays.limit_up.factors.optimized.model_score import factor_model_score_batch as _m
    from plays.limit_up.pusher import check_and_push

    today_str = _today_str()
    hhmm = _hhmm()
    quotes: dict[str, dict] = {}

    # ① 读取最新预评分池（每轮刷新,接收surge_scanner追加）
    try:
        af = Path(__file__).resolve().parent.parent.parent / "data" / "analysis" / f"{_today_str()}.json"
        _pool = json.loads(af.read_text()) if af.exists() else []
        pool_codes = [r["code"] for r in _pool]
    except Exception:
        pool_codes = []
    if not pool_codes:
        return []

    # ② batch_quotes（有新票或每5轮刷新）
    _should_scan = iter_count % 5 == 0 or not getattr(_run_pre_scored_round, "_rc", None)
    _prev_codes = set(getattr(_run_pre_scored_round, "_prev_pool", []))
    _new_codes = set(pool_codes) - _prev_codes
    if _new_codes:
        _should_scan = True
    if _should_scan:
        if iter_count > 0:
            print(f"\n[{_now().strftime('%H:%M')}] batch_quotes {len(pool_codes)}只...")
        quotes = scan_batch(pool_codes)
        _run_pre_scored_round._rc = quotes
        _run_pre_scored_round._prev_pool = list(pool_codes)
    else:
        quotes = _run_pre_scored_round._rc

    # ② 从面板取 T-1 特征
    if t1_panel is not None:
        pit_df = t1_panel[t1_panel["code"].isin(pool_codes)].copy()
    else:
        pit_df = pd.DataFrame()
    if pit_df.empty:
        return []

    # ③ 实时覆盖（含 pct_chg！趋势评分）
    for code in pit_df["code"].tolist():
        q = quotes.get(code, {})
        m = pit_df["code"] == code
        rt_pct = float(q.get("pct_chg", 0) or 0)
        pit_df.loc[m, "pct_chg_score_day"] = rt_pct
        rt_tr = float(q.get("turnover", 0) or 0)
        if rt_tr > 0: pit_df.loc[m, "turnover_rate"] = rt_tr
        rt_vr = float(q.get("vol_ratio", 0) or 0)
        if rt_vr > 0: pit_df.loc[m, "volume_ratio"] = rt_vr

    # ⑤ 批量评分
    try:
        pit_df["model_score"] = _m(pit_df)
    except Exception as e:
        print(f"  评分失败: {e}")
        return []

    # ⑥ 推送（仅 涨停不推）
    all_results, push_candidates = [], []
    for _, r in pit_df.iterrows():
        score = float(r["model_score"])
        code = r["code"]
        name = (pool_name_map or {}).get(code, "") or code.split(".")[0]
        rt_pct = quotes.get(code, {}).get("pct_chg")
        if rt_pct is not None and float(rt_pct) >= 9.8:
            continue
        if score >= float(os.environ.get("ULTIMATE_PUSH_THRESHOLD", "55")):
            print(f"    ≥55 {code} {name} score={score:.1f} pct_chg={rt_pct}%")
            rec = {"code": code, "name": name, "total_score": score,
                   "score_mode": "model_score", "pct_chg": rt_pct,
                   "technical": float(r.get("technical", 0) or 0),
                   "fundflow": float(r.get("fundflow", 0) or 0),
                   "sentiment": float(r.get("sentiment", 0) or 0),
                   "shortterm": float(r.get("shortterm", 0) or 0)}
            all_results.append(rec); push_candidates.append(rec)
            # 自动加入 watchdog 盯盘
            try:
                watchdog_path = Path(__file__).resolve().parent.parent.parent / "plays" / "watchdog" / "data" / "state.json"
                if watchdog_path.exists():
                    wd = json.loads(watchdog_path.read_text())
                    if code not in wd:
                        wd[code] = {"code": code, "name": name, "status": "watching",
                                    "added_at": __import__("datetime").datetime.now().isoformat(),
                                    "entry_price": 0, "entry_at": "", "highest_since_entry": 0,
                                    "bars_held": 0, "signal_type": "", "signal_reason": "",
                                    "signal_at": "", "last_alert_at": "",
                                    "last_abnormal_level": "", "last_abnormal_pushed_at": 0,
                                    "netflow_history": [], "daily_basic": {}, "dim_scores": {},
                                    "last_daily_update": ""}
                        tmp = watchdog_path.with_suffix(".tmp")
                        tmp.write_text(json.dumps(wd, indent=2))
                        tmp.rename(watchdog_path)
                        print(f"      ↳ 已加入watchdog盯盘")
            except Exception:
                pass
        elif score >= float(os.environ.get("L2_GREY_LOW", "45")):
            print(f"    [45-55) {code} {name} score={score:.1f} → 灰区跳过")
            # 不推送,不盯盘

    save_analysis(all_results)
    check_and_push(push_candidates, PROJECT_DIR / "plays" / "limit_up" / "data")
    return all_results


def save_analysis(results: list[dict]):
    """单文件存档，同股票按 code 覆盖，不同股票追加。"""
    td = _today_str()
    analysis_dir = DATA_DIR / "analysis"
    analysis_dir.mkdir(exist_ok=True)
    path = analysis_dir / f"{td}.json"
    existing = {}
    if path.exists():
        try:
            existing = {r["code"]: r for r in json.loads(path.read_text())}
        except Exception:
            pass
    existing.update({r["code"]: r for r in results})
    with open(path, "w") as f:
        json.dump(list(existing.values()), f, ensure_ascii=False, indent=2)
    print(f"  [analysis] 已合并 {len(results)} 只到 {path} (累计{len(existing)}只)")


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

    pool_extras: dict[str, dict] = {}

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
                pool_extras = {s["code"]: {"circ_mv": s.get("circ_mv", 0),
                                             "pe": s.get("pe", 999.0),
                                             "pb": s.get("pb", 999.0),
                                             "prev_turnover": s.get("turnover_rate", 0.0),
                                             "prev_vol_ratio": s.get("volume_ratio", 0.0)} for s in pool}
                print(f"    候选池 {len(pool)} 只 ✓")
                pool_built = True  # 先放行评分循环

                # T-1 数据后台预取（预评分池模式跳过，面板已有全量数据）
                _af = Path(__file__).resolve().parent.parent.parent / "data" / "analysis" / f"{_today_str()}.json"
                if not _af.exists():
                    try:
                        from scripts.tu_share import call_tushare as _ct
                        from plays.limit_up.strategies import factor_ctx
                        _td = _today_str()
                        _prev_date = _td
                        _dt = datetime.strptime(_prev_date, "%Y%m%d")
                        for _ in range(10):
                            _dt -= timedelta(days=1)
                            _try = _dt.strftime("%Y%m%d")
                            _cal = _ct("trade_cal", {"exchange":"SSE","start_date":_try,"end_date":_try},"cal_date,is_open")
                            _items = _cal.get("data",{}).get("items",[])
                            if _items and _items[0] and len(_items[0])>1 and str(_items[0][1])=="1":
                                _prev_date = _try; break
                        for _c in pool_codes:
                            factor_ctx._ensure_daily_limit(_c)
                        global _T1_MF_CACHE, _T1_MF_PREV_CACHE
                        _T1_MF_CACHE.clear(); _T1_MF_PREV_CACHE.clear()
                        for _batch in [pool_codes[:50], pool_codes[50:]]:
                            if not _batch: continue
                            _s = ",".join(_batch)
                            _r = _ct("moneyflow",{"ts_code":_s,"trade_date":_td},"ts_code,net_mf_amount")
                            for _it in _r.get("data",{}).get("items",[]): _T1_MF_CACHE[_it[0]] = float(_it[-1]) if len(_it)>1 else 0
                            _r2 = _ct("moneyflow",{"ts_code":_s,"trade_date":_prev_date},"ts_code,net_mf_amount")
                            for _it2 in _r2.get("data",{}).get("items",[]): _T1_MF_PREV_CACHE[_it2[0]] = float(_it2[-1]) if len(_it2)>1 else 0
                        global _T1_TL_CACHE, _T1_TI_CACHE
                        _T1_TL_CACHE.clear(); _T1_TI_CACHE.clear()
                        _r = _ct("top_list",{"trade_date":_td},"ts_code,amount,net_amount,l_buy,l_amount,net_rate")
                        for _it in _r.get("data",{}).get("items",[]):
                            _f = _r["data"]["fields"]; _T1_TL_CACHE[_it[0]] = dict(zip(_f,_it))
                        _r = _ct("top_inst",{"trade_date":_td},"ts_code,exalter,net_buy")
                        for _it in _r.get("data",{}).get("items",[]):
                            _T1_TI_CACHE.setdefault(_it[0],[]).append(dict(zip(_r["data"]["fields"],_it)))
                        print(f"    T-1 预取: moneyflow {len(_T1_MF_CACHE)}只 龙虎榜{len(_T1_TL_CACHE)}只 ✓")
                    except Exception as _e:
                        print(f"    T-1 预取失败: {_e}")


            # ── 交易时段：持续评分 ──
            trading = _is_trading_session(hhmm)
            if not trading or not pool_built:
                _sim_sleep(60 if _SIM_TIME is not None else 1)
                continue

            if not trading_started:
                print(f"[{now.strftime('%H:%M')}] ② 开始盘中扫描(预评分池)")
                trading_started = True

            # 预评分池模式：从 panel + analysis.json 读取
            if getattr(_run_pre_scored_round, "_t1_panel", None) is None:
                import pandas as _pd
                pf = Path(__file__).resolve().parent.parent.parent / "wiki" / "raw" / "limit-up" / "panel"
                af = Path(__file__).resolve().parent.parent.parent / "data" / "analysis"
                today = _today_str()
                panel_file = pf / f"{today}.parquet"
                analysis_file = af / f"{today}.json"
                assert panel_file.exists(), f"面板文件不存在: {panel_file}"
                assert analysis_file.exists(), f"分析文件不存在: {analysis_file}"
                _run_pre_scored_round._t1_panel = _pd.read_parquet(panel_file)
                _run_pre_scored_round._pool = json.loads(analysis_file.read_text())
                pool_codes = [r["code"] for r in _run_pre_scored_round._pool]
                print(f"  [pipeline] 预评分池已加载: {len(pool_codes)}只")

            iter_count += 1
            t0 = time.time()
            deep_results = _run_pre_scored_round(pool_name_map=pool_name_map,
                                                  t1_panel=_run_pre_scored_round._t1_panel,
                                                  iter_count=iter_count)
            elapsed = time.time() - t0
            if deep_results:
                print(f"  [完成] {elapsed:.1f}s")
            _write_heartbeat()

            if sim_rounds and iter_count >= sim_rounds:
                print(f"[pipeline] 模拟完成 {iter_count} 轮，退出")
                break
        except Exception as e:
            print(f"[pipeline] 第{iter_count}轮异常: {e}")
            import traceback
            traceback.print_exc()
            _sim_sleep(10 if _SIM_TIME is not None else 10)

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
    pool_extras = {s["code"]: {"circ_mv": s.get("circ_mv", 0),
                                "pe": s.get("pe", 999.0),
                                "pb": s.get("pb", 999.0),
                                "prev_turnover": s.get("turnover_rate", 0.0),
                                "prev_vol_ratio": s.get("volume_ratio", 1.0)}
                    for s in pool}

    deep_results, _ = _run_one_round(pool_codes, pool_name_map, pool_extras=pool_extras)
    print(f"[pipeline] 单次扫描完成，分析 {len(deep_results)} 只")


if __name__ == "__main__":
    main()
