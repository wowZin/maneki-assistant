#!/usr/bin/env python3
"""盘中异动扫描 → watchdog surge 盯盘（代替推送）。

口径（2026-07-25 v2 与用户确认）：
- 扫描池（先筛后拉，THS 压力最小化）：
  ① 主闸池：面板 model_score ≥ SURGE_PANEL_SCORE(默认20)
     （pipeline 09:30 全量评分写回面板，surge 直接读面板）
  ② 排雷池：昨日涨停 ∪ 前20日涨停基因 中不在主闸池的票
- 行情源：ths_client.get_batch_quotes_fast（并发批量，仅扫描池 ~1100 只/轮）
- 路由：主闸池直接过；排雷池走排雷（首板: 量比≥2+窄概念联动≥2+筹码不压顶；
  昨日涨停: 量比≥2+筹码不压顶）。无数量上限（2026-07-26 用户拍板：入盯不设限）
- 通过的票同时写：watchdog state.json（source="surge"）、analysis.json、
  pushed/{date}_surge.json（pipeline 同构记录，按 code 去重）
- surge 票只发【surge】入场信号，无信号不通知；盘后零信号自动汰换（watchdog 侧实现）。

用法:
    python3 plays/limit_up/surge_scanner.py            # 扫描一次
    python3 plays/limit_up/surge_scanner.py --daemon   # 每60s循环
    python3 plays/limit_up/surge_scanner.py --dry-run  # 只打印路由决策，不写 watchdog
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

PLAY_DIR = Path(__file__).resolve().parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
SIGNALS_DIR = PLAY_DIR / "data" / "signals"
PANEL_DIR = PLAY_DIR.parent.parent / "wiki" / "raw" / "limit-up" / "panel"
WATCHDOG_STATE = PLAY_DIR.parent.parent / "plays" / "watchdog" / "data" / "state.json"

PCT_LOW = float(os.getenv("SURGE_PCT_LOW", "3.0"))    # 异动涨幅窗口（3%≤涨幅<9.8%，留0.2%防已封板）
# 2026-08-10 调低 5.0→3.0：5%门槛+3轮确认延迟=系统性买在高位（用户分时图实证：
# 立新能源/运机集团/三羊马 5分钟内完成拉升，5%捕获时已在半山腰）。
# 回测(20260810主闸316只)：3%触发109只 vs 5%触发56只；30min胜率31% vs 22%、
# 收盘胜率39% vs 29%、30min均收益-0.15% vs -0.51%——3%全面占优。
# 代价：扫描池变大（更多3%+票进池），依赖 check_entry L1 过滤假突破。
PCT_HIGH = float(os.getenv("SURGE_PCT_HIGH", "9.8"))  # 上限9.8：连板秒板高发，9.0会丢窗口
SURGE_PANEL_SCORE = float(os.getenv("SURGE_PANEL_SCORE", "30"))  # 主闸：面板早盘评分阈值（2026-08-01 修复v2模型分布下 20→30，池子减半噪声更少）
SURGE_VOL_RATIO = float(os.getenv("SURGE_VOL_RATIO", "2.0"))  # 排雷：量比下限



def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


_TRADE_DAY_CACHE: dict[str, bool] = {}


def _is_trade_day(date_str: str) -> bool:
    """交易日判断（tushare 交易日历，带缓存）。非交易日 surge 不扫。"""
    if date_str in _TRADE_DAY_CACHE:
        return _TRADE_DAY_CACHE[date_str]
    try:
        from scripts.tu_share import call_tushare
        r = call_tushare("trade_cal", {"cal_date": date_str}, "exchange,cal_date,is_open")
        for row in r.get("data", {}).get("items", []):
            _TRADE_DAY_CACHE[date_str] = int(row[2]) == 1 if len(row) > 2 else False
            return _TRADE_DAY_CACHE[date_str]
    except Exception:
        pass
    _TRADE_DAY_CACHE[date_str] = datetime.now().weekday() < 5  # 接口失败按星期判断
    return _TRADE_DAY_CACHE[date_str]


# ══════════════════════════════════════════════════════
# 扫描宇宙（每日构建一次，缓存）
# ══════════════════════════════════════════════════════

def build_universe(td: str) -> dict:
    """构建扫描池数据（日缓存）。

    返回:
      scores: {code: 面板 model_score}（pipeline 09:30 全量写回，主板）
      dims:   {code: {technical,fundflow,sentiment,shortterm,fundamental}}（面板列）
      yesterday_limit / gene: 昨日涨停 / 前20日涨停基因（排雷候选池）
      names:  {code: name}（pool 文件 + 涨停名单）
    """
    cache = PLAY_DIR / "data" / "pool" / f"surge_universe_{td}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass

    from scripts.tu_share import call_tushare
    import pandas as pd

    # 面板：主板 model_score + 维度分（pipeline 09:30 全量评分产物）
    scores: dict[str, float] = {}
    dims: dict[str, dict] = {}
    basics: dict[str, dict] = {}
    uncertain = False  # 面板存在但 model_score 未写回（pipeline 未跑/失败）→ 不写日缓存
    panel_file = PANEL_DIR / f"{td}.parquet"
    if panel_file.exists():
        _DIM_COLS = ["technical", "fundflow", "sentiment", "shortterm", "fundamental"]
        _BASE_COLS = ["circ_mv", "pe", "pb"]  # watchdog realtime_row 需要（turnover/基本面因子）
        # model_score 由 pipeline 09:30 写回；未写回时列不存在，需容错（全走排雷，下轮重试）
        import pyarrow.parquet as _pq
        _avail = _pq.read_schema(panel_file).names  # 只读元数据，不读数据
        _want = ["code"] + [c for c in ["model_score"] + _DIM_COLS + _BASE_COLS if c in _avail]
        panel = pd.read_parquet(panel_file, columns=_want)
        if "model_score" not in _avail:
            uncertain = True
            print(f"  [surge] 面板 model_score 未写回（pipeline 未评分？），本轮全走排雷")
        panel = panel[panel["code"].str[:2].isin(["00", "60"])]  # 打板只看主板
        for _, r in panel.iterrows():
            ms = r.get("model_score")
            if ms is not None and ms == ms:  # 非 NaN（列不存在时 r.get 返回 None）
                scores[r["code"]] = float(ms)
            dims[r["code"]] = {d: float(r.get(d, 0) or 0) for d in _DIM_COLS if d in panel.columns}
            basics[r["code"]] = {b: float(r.get(b, 0) or 0) for b in _BASE_COLS if b in panel.columns}
    else:
        uncertain = True
        print(f"  [surge] 面板不存在: {panel_file}，今日无面板分可用（全部走排雷）")

    # 名称：面板 name 列（pool_builder 已删除，2026-07-30 起不再建池）
    # 面板在 panel_builder 构建时已带 name 列；缺失的从涨停名单补
    names: dict[str, str] = {}
    if panel_file.exists():
        try:
            _np = pd.read_parquet(panel_file, columns=["code", "name"])
            names = {r["code"]: str(r.get("name") or "") for _, r in _np.iterrows()}
        except Exception as e:
            print(f"  [surge] 面板名称读取失败: {e}")

    # 昨日涨停 + 前20日基因（一次调用拉45天）
    from datetime import timedelta
    start = (datetime.strptime(td, "%Y%m%d") - timedelta(days=45)).strftime("%Y%m%d")
    resp = call_tushare("limit_list_d",
                        {"start_date": start, "end_date": td, "limit_type": "U"},
                        "ts_code,trade_date,name", timeout=60)
    fields = resp.get("data", {}).get("fields", [])
    items = resp.get("data", {}).get("items", [])
    from plays.limit_up.utils import is_tradable_stock
    limit_dates = sorted({dict(zip(fields, r)).get("trade_date", "") for r in items} - {""})
    yesterday = limit_dates[-1] if limit_dates else ""
    # 前20个交易日窗口
    window = set(limit_dates[-21:-1]) if len(limit_dates) > 1 else set()
    yesterday_map, gene_map = {}, {}
    for r in items:
        d = dict(zip(fields, r))
        c, n, dte = d.get("ts_code", ""), d.get("name", "") or "", d.get("trade_date", "")
        if not is_tradable_stock(c, n):
            continue
        if dte == yesterday:
            yesterday_map[c] = n
        if dte in window:
            gene_map[c] = n

    names.update({c: n for c, n in yesterday_map.items() if c not in names})
    names.update({c: n for c, n in gene_map.items() if c not in names})

    out = {
        "date": td, "limit_yesterday_date": yesterday,
        "scores": scores, "dims": dims, "basics": basics, "names": names,
        "yesterday_limit": yesterday_map, "gene": gene_map,
    }
    # 状态不确定（无面板分）时不写日缓存——避免把"主闸空"锁死一整天，下轮重试
    if not uncertain:
        cache.write_text(json.dumps(out, ensure_ascii=False))
    return out


# ══════════════════════════════════════════════════════
# 排雷条件（首板通道 + 面板外连板票）
# ══════════════════════════════════════════════════════

_cyq_cache: dict = {}


def _load_cyq(td: str):
    """T-1 筹码：{code: (close_t1, cost_50pct)}。现价≥成本中位=上方无峰压。"""
    if _cyq_cache.get("date") == td:
        return
    import pandas as pd
    cyq = pd.read_parquet(PANEL_DIR / "cyq_perf.parquet")
    cyq["trade_date"] = cyq["trade_date"].astype(str)
    t1 = cyq[cyq.trade_date < td].trade_date.max()
    cyq = cyq[cyq.trade_date == t1].set_index("ts_code")
    daily = pd.read_parquet(PANEL_DIR / "daily" / f"{t1}.parquet").set_index("ts_code")
    close = daily["close"]
    m = {}
    for c in cyq.index:
        if c in close.index:
            m[c] = (float(close[c]), float(cyq.loc[c, "cost_50pct"] or 0))
    _cyq_cache.clear()
    _cyq_cache.update({"date": td, "map": m})


def cyq_no_pressure(code: str, td: str) -> bool:
    """筹码不压顶：T-1 收盘 ≥ 筹码成本中位（上方套牢盘轻）。"""
    _load_cyq(td)
    v = _cyq_cache.get("map", {}).get(code)
    if not v or v[1] <= 0:
        return True  # 无数据不拦
    return v[0] >= v[1]


_concept_cache: dict = {}


def _load_concepts():
    """code → set(窄概念)。剔除成员>300的宽概念（沪深300/融资融券等），否则联动恒真。"""
    if _concept_cache:
        return _concept_cache["map"]
    import pandas as pd
    m = {}
    max_size = int(os.getenv("SURGE_CONCEPT_MAX_SIZE", "300"))
    for d in [PANEL_DIR / "concept", PLAY_DIR / "backtest" / "cache"]:
        f = d / "concept_members.parquet"
        if f.exists():
            df = pd.read_parquet(f)
            # 结构: cpt_code(概念代码), stock_code(6位股票代码), con_name(股票名)
            sizes = df.groupby("cpt_code")["stock_code"].nunique()
            narrow = set(sizes[sizes <= max_size].index.astype(str))
            for code, g in df.groupby("stock_code"):
                m[str(code)] = set(g["cpt_code"].astype(str)) & narrow
            break
    _concept_cache["map"] = m
    return m


def sector_resonance(code: str, round_codes: list[str], min_peers: int = 2) -> bool:
    """板块联动：本轮异动候选中同概念票 ≥ min_peers 只。"""
    cmap = _load_concepts()
    my = cmap.get(code.split(".")[0])
    if not my:
        return False
    peers = 0
    for other in round_codes:
        if other == code:
            continue
        if cmap.get(other.split(".")[0], set()) & my:
            peers += 1
    return peers >= min_peers - 1  # 含自己共 min_peers 只


# ══════════════════════════════════════════════════════
# watchdog state.json 写入（文件协议，禁止跨玩法 import）
# ══════════════════════════════════════════════════════

def _wd_add(entries: list[dict], dry_run: bool = False) -> list[str]:
    """把 surge 票写入 watchdog state.json。

    entries: [{code, name, dim_scores?, daily_basic?}]
      dim_scores/daily_basic 必须带上（面板值）——否则 watchdog realtime_row
      的五维度分=0，模型分被系统性压低，入场闸(min_model_score=40)永远够不到。
    """
    if dry_run:
        return [e["code"] for e in entries]
    added = []
    for attempt in range(3):
        try:
            states = json.loads(WATCHDOG_STATE.read_text()) if WATCHDOG_STATE.exists() else {}
            for e in entries:
                if e["code"] in states or e["code"] in added:
                    continue
                states[e["code"]] = {
                    "code": e["code"], "name": e.get("name", ""),
                    "added_at": datetime.now().isoformat(),
                    "status": "watching", "source": "surge",
                    "entry_pushed_date": "", "entry_price": 0, "entry_at": "",
                    "highest_since_entry": 0, "bars_held": 0,
                    "signal_type": "", "signal_reason": "", "signal_at": "",
                    "last_alert_at": "", "last_abnormal_level": "",
                    "last_abnormal_pushed_at": 0, "netflow_history": [],
                    "daily_basic": e.get("daily_basic", {}),
                    "dim_scores": e.get("dim_scores", {}),
                    "last_daily_update": "",
                }
                added.append(e["code"])
            # 2026-08-06：原子写（tempfile + rename）——与 watchdog 共写 state.json，
            # 非原子写会互相截断（实测 10:31 损坏）。原子写保证读取方永远见完整 JSON。
            # 2026-08-10：tmp 名带 pid，避免与 watchdog/restore_positions 共用
            # state.json.tmp 并发冲突（A 写被 B 覆盖 → A rename 失败，13:03 实测）。
            _tmp = WATCHDOG_STATE.with_name(f"state.json.tmp.{os.getpid()}")
            _tmp.write_text(json.dumps(states, ensure_ascii=False, indent=2))
            _tmp.rename(WATCHDOG_STATE)
            # 回读校验（watchdog 引擎每30秒重写 state，可能覆盖；重写则重试）
            back = json.loads(WATCHDOG_STATE.read_text())
            if all(c in back for c in added):
                return added
        except Exception as ex:
            print(f"  [surge] 写 watchdog 失败(attempt {attempt+1}): {ex}")
            time.sleep(1)
    return added



def _log_signals(td: str, recs: list[dict]):
    """surge 路由记录（盘后归档 wiki/raw/limit-up/signals/）。"""
    if not recs:
        return
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    f = SIGNALS_DIR / f"{td}.json"
    existing = []
    if f.exists():
        try:
            existing = json.loads(f.read_text())
        except Exception:
            existing = []
    existing.extend(recs)
    f.write_text(json.dumps(existing, ensure_ascii=False))


SNAPSHOT_DIR = PLAY_DIR / "data" / "snapshot_log"


def _log_snapshots(td: str, quote_rows: list[dict], morning_scores: dict):
    """候选股实时快照落盘（原 pipeline._log_snapshot 迁移，供盘中模型训练）。

    quote_rows: [(code, pct, vol_ratio, quote_dict)]
    """
    if not quote_rows:
        return
    try:
        import pandas as pd
        now = datetime.now().strftime("%H:%M:%S")
        rows = []
        for code, pct, vr, q in quote_rows:
            vol = float(q.get("volume", 0) or 0)
            amt = float(q.get("amount", 0) or 0)
            rows.append({
                "ts": now, "code": code, "pct_chg": pct,
                "price": float(q.get("price", 0) or 0),
                "bid1": float(q.get("bid1", 0) or 0),
                "ask1": float(q.get("ask1", 0) or 0),
                "turnover": float(q.get("turnover", 0) or 0),
                "vol_ratio": vr,
                "inner_vol": float(q.get("inner_vol", 0) or 0),
                "outer_vol": float(q.get("outer_vol", 0) or 0),
                "vwap": amt / (vol * 100) if vol > 0 else 0,
                "panel_score": morning_scores.get(code),
            })
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SNAPSHOT_DIR / f"{td}.parquet"
        new = pd.DataFrame(rows)
        if path.exists():
            new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
        new.to_parquet(path, index=False)
    except Exception as e:
        print(f"  [surge] 快照落盘失败: {e}")


# ══════════════════════════════════════════════════════
# analysis / pushed 写入（与 pipeline 记录格式一致，按 code 去重）
# ══════════════════════════════════════════════════════

def _surge_record(code: str, name: str, pct: float,
                  score: float | None, dims: dict | None) -> dict:
    """构造 surge 记录（pipeline 精简格式 + source=surge）。

    score: 面板 model_score（排雷票为 <20 的真实分，无分为 None）。
    dims:  面板维度分列（无则全 0）。
    """
    score_mode = "model_score" if (score is not None and score >= SURGE_PANEL_SCORE) else "surge_screen"
    return {
        "code": code, "name": name,
        "model_score": score, "total_score": score,
        "score_mode": score_mode, "pct_chg": pct,
        "scores": dims or {"technical": 0, "fundflow": 0, "sentiment": 0, "shortterm": 0},
        "fundamental": (dims or {}).get("fundamental", 0),
        "source": "surge",
    }


def _write_analysis(recs: list[dict], td: str):
    """合并写入 analysis/{td}.json（按 code 去重覆盖）。"""
    if not recs:
        return
    af = ANALYSIS_DIR / f"{td}.json"
    af.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if af.exists():
        try:
            existing = {r["code"]: r for r in json.loads(af.read_text())}
        except Exception:
            pass
    existing.update({r["code"]: r for r in recs})
    tmp = af.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(existing.values()), ensure_ascii=False))
    tmp.rename(af)


def _write_pushed(recs: list[dict], td: str):
    """写入 pushed/{td}_surge.json（按 code 去重覆盖，供回测）。"""
    if not recs:
        return
    pd_dir = PLAY_DIR / "data" / "pushed"
    pd_dir.mkdir(parents=True, exist_ok=True)
    pf = pd_dir / f"{td}_surge.json"
    existing = {}
    if pf.exists():
        try:
            existing = {r["code"]: r for r in json.loads(pf.read_text())}
        except Exception:
            pass
    existing.update({r["code"]: r for r in recs})
    tmp = pf.with_suffix(".tmp")
    tmp.write_text(json.dumps(list(existing.values()), ensure_ascii=False))
    tmp.rename(pf)


# ══════════════════════════════════════════════════════
# 主扫描
# ══════════════════════════════════════════════════════

# 大盘弱势阈值：上证指数 < -0.3% 视为弱势市（2026-08-11 调回：
# 08-10 曾从 -0.3%→0% 提高敏感度，但 08-11 实测大盘 -0.15% 微跌即全收，
# 扫描池骤减（主闸76只+5%门槛+关排雷），行情不该如此静默——0% 过于谨慎，
# 误伤轻度回调日。改回 -0.3%，翻绿不立即收闸。）
MKT_WEAK_THRESHOLD = float(os.getenv("SURGE_MKT_WEAK", "-0.3"))
# 大盘强势判定（2026-08-10 改）：不再看瞬时涨幅，改看 3 分钟变化率。
# 尾盘放水事故实测：14:30 大盘瞬时 +0.52% 触发旧"强势"判 → 门槛 3%→2% →
# 24 笔尾盘杂毛票买入（申华控股 1.71 等）。但 3 分钟变化仅 +0.05%（缓涨），
# 并非真强势。改为急拉（3min ≥ +0.2%）才算强势；缓涨/尾盘异动按中性。
MKT_STRONG_DELTA = float(os.getenv("SURGE_MKT_STRONG_DELTA", "0.2"))  # 3分钟变化率 ≥0.2% 才算强势
MKT_WEAK_DELTA = float(os.getenv("SURGE_MKT_WEAK_DELTA", "-0.2"))      # 3分钟变化率 ≤-0.2% 判弱势
# 动态捕获门槛（2026-08-10 用户拍板）：行情好往下放，行情不好往上收。
# 强市 2% / 中性 3% / 弱市 5%——弱势档与 5% 旧门槛一致，保证不劣于改动前。
PCT_LOW_STRONG = float(os.getenv("SURGE_PCT_LOW_STRONG", "2.0"))
PCT_LOW_NORMAL = float(os.getenv("SURGE_PCT_LOW_NORMAL", "3.0"))
PCT_LOW_WEAK = float(os.getenv("SURGE_PCT_LOW_WEAK", "5.0"))


def _mkt_gate() -> tuple[str, float]:
    """大盘走势栅栏 + 动态捕获门槛。

    数据源：同花顺 v6/time/hs_1A0001 当日分时（Cookie 直连，30s 缓存）。
    判定逻辑（2026-08-10 改，修复尾盘放水）：
      - 强势：分时 3 分钟变化率 ≥ +0.2%（急拉才算真强势）→ 门槛 2%
      - 弱势：瞬时 < 0% 或 3 分钟变化率 ≤ -0.2%（跳水）→ 门槛 5% + 关排雷池
      - 中性：其余（缓涨/横盘，即使瞬时 +0.5%+）→ 门槛 3%
    接口故障/异常 → 保守返回 ('weak', 5.0)（宁可少扫不可乱买）。
    """
    try:
        from scripts.ths_client import get_ths_client as _ths_idx
        _idx = _ths_idx().get_index_intraday("1A0001")
        if _idx is None:
            print("  [surge] 大盘栅栏: 指数获取失败 → 保守弱市(关排雷池+门槛5%)")
            return "weak", PCT_LOW_WEAK
        _pct = _idx.get("pct_chg", 0.0)
        # NaN/None/非数值 → 保守弱市（NaN <= -0.3 在 Python 恒 False，
        # 不防御会误判"正常"——2026-08-06 实测）
        if _pct is None or not isinstance(_pct, (int, float)) \
                or _pct != _pct:
            print(f"  [surge] 大盘栅栏: 指数数据异常({_pct!r}) → 保守弱市")
            return "weak", PCT_LOW_WEAK
        # 3 分钟变化率：取最近分时点 vs 3 分钟前的点
        _delta = 0.0
        _pts = _idx.get("points") or []
        if len(_pts) >= 2:
            _t_now, _p_now = _pts[-1]
            _p_prev = None
            for _t, _p in reversed(_pts):
                if _t <= _t_now - 3:
                    _p_prev = _p
                    break
            if _p_prev:
                _delta = (_p_now / _p_prev - 1) * 100 if _p_prev > 0 else 0.0
        # 判定顺序：急拉强势 > 跳水/翻绿弱势 > 中性
        if _delta >= MKT_STRONG_DELTA and _pct > 0:
            print(f"  [surge] 大盘栅栏: 上证{_idx.get('latest'):.2f} {_pct:+.2f}% "
                  f"3min{_delta:+.2f}% → 强势(门槛{PCT_LOW_STRONG:.1f}%全开)")
            return "strong", PCT_LOW_STRONG
        if _pct < MKT_WEAK_THRESHOLD or _delta <= MKT_WEAK_DELTA:
            print(f"  [surge] 大盘栅栏: 上证{_idx.get('latest'):.2f} {_pct:+.2f}% "
                  f"3min{_delta:+.2f}% → 弱势(关排雷池+门槛{PCT_LOW_WEAK:.1f}%)")
            return "weak", PCT_LOW_WEAK
        print(f"  [surge] 大盘栅栏: 上证{_idx.get('latest'):.2f} {_pct:+.2f}% "
              f"3min{_delta:+.2f}% → 中性(门槛{PCT_LOW_NORMAL:.1f}%全开)")
        return "normal", PCT_LOW_NORMAL
    except Exception as _e:
        print(f"  [surge] 大盘栅栏异常 → 保守弱市: {_e}")
        return "weak", PCT_LOW_WEAK


def scan(dry_run: bool = False):
    td = _today()
    uni = build_universe(td)
    scores: dict[str, float] = uni["scores"]
    dims: dict[str, dict] = uni["dims"]
    names: dict[str, str] = uni["names"]
    yesterday_limit = set(uni["yesterday_limit"].keys())
    gene = set(uni["gene"].keys())

    # ── 扫描池预筛（先筛后拉，THS 压力最小化）──
    # 主闸池：面板 model_score ≥ 20（pipeline 09:30 全量评分写回）
    main_pool = {c for c, s in scores.items() if s >= SURGE_PANEL_SCORE}
    # 排雷池：昨日涨停 ∪ 前20日基因 中不在主闸池的（分<20或无分的票）
    screen_pool = (yesterday_limit | gene) - main_pool

    # ── 大盘走势栅栏 + 动态捕获门槛（2026-08-06/08-10）──
    # 弱势市（上证 ≤ -0.3%）：只放主闸池（排雷全关）+ 门槛收 5%（不劣于旧配置）。
    # 强势市（上证 ≥ +0.5%）：门槛放 2%（早捕获，多吃拉升段）。
    # 中性市：门槛 3%。
    # 依据：打板吃大盘 beta——午盘大盘向下时排雷票大面积亏损（08-06 实测）；
    #       用户分时图实证 5% 门槛+确认延迟=系统性买在高位（08-10 立新能源等）。
    _mkt_state, _pct_low = _mkt_gate()
    if _mkt_state == "weak":
        screen_pool = set()
    watch = sorted(main_pool | screen_pool)
    print(f"  [surge] 扫描池 {len(watch)} 只（主闸{len(main_pool)} + 排雷{len(screen_pool)}）| "
          f"动态门槛 {_pct_low:.1f}%")

    # THS 并发批量实时行情（ths_client.get_batch_quotes_fast，线程池）
    from scripts.ths_client import get_ths_client as _ths
    _workers = int(os.getenv("SURGE_QUOTE_WORKERS", "24"))
    quotes = _ths().get_batch_quotes_fast(watch, workers=_workers)
    candidates = []  # (code, pct, vol_ratio)
    quote_map = {}   # full_code -> quote dict（快照用）
    for code, q in quotes.items():
        if q is None:
            continue
        pct = float(q.get("pct_chg", 0) or 0)
        if not (_pct_low <= pct < PCT_HIGH):
            continue
        full = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        vr = float(q.get("vol_ratio", 0) or 0)
        candidates.append((full, pct, vr))
        quote_map[full] = q

    if not candidates:
        print(f"  [surge] 无异动候选")
        return
    print(f"  [surge] 异动候选 {len(candidates)} 只: "
          + ", ".join(f"{c}({p:.1f}%)" for c, p, _ in candidates[:8]))

    # 候选快照落盘（盘中模型训练素材，原 pipeline 职责迁入）
    if not dry_run:
        _log_snapshots(td, [(c, p, v, quote_map[c]) for c, p, v in candidates], scores)

    # 路由：① 主闸池直接过 ② 排雷池 → 排雷（首板3项/昨停2项）
    round_codes = [c for c, _, _ in candidates]
    picks, logs = [], []
    for c, pct, vr in candidates:
        is_lb = c in yesterday_limit
        if c in main_pool:
            sc = scores[c]
            ok = True
            route = f"面板分{sc:.1f}≥{SURGE_PANEL_SCORE:.0f}"
        else:
            checks = []
            if vr >= SURGE_VOL_RATIO:
                checks.append("量比")
            if cyq_no_pressure(c, td):
                checks.append("筹码")
            if not is_lb:
                if sector_resonance(c, round_codes):
                    checks.append("联动")
                ok = len(checks) == 3
            else:
                ok = len(checks) == 2  # 昨日涨停票：量比+筹码
            route = f"{'连板' if is_lb else '首板'} 排雷={'/'.join(checks) or '无'}"
        logs.append({"code": c, "name": names.get(c, ""), "pct": pct,
                     "vol_ratio": vr, "route": route, "pass": ok,
                     "ts": datetime.now().isoformat()})
        if ok:
            picks.append({"code": c, "name": names.get(c, ""), "pct": pct})

    if not dry_run:
        _log_signals(td, logs)

    # 写 watchdog state（surge 标签）+ analysis + pushed（格式一致，按 code 去重）
    recs = [_surge_record(p["code"], p["name"], p["pct"],
                          scores.get(p["code"]), dims.get(p["code"])) for p in picks]
    added = _wd_add([{"code": p["code"], "name": p["name"],
                      "dim_scores": dims.get(p["code"], {}),
                      "daily_basic": uni["basics"].get(p["code"], {})}
                     for p in picks], dry_run=dry_run) if picks else []
    if not dry_run:
        _write_analysis(recs, td)
        _write_pushed(recs, td)
    tag = "[dry-run] " if dry_run else ""
    print(f"  [surge] {tag}路由: 候选{len(candidates)} → 通过{len(picks)} → 入watchdog {len(added)}"
          f"{'（analysis/pushed 已更新）' if recs and not dry_run else ''}")
    for l in logs:
        mark = "✓" if l["pass"] else "✗"
        print(f"    {mark} {l['code']} {l['name']} {l['pct']:.1f}% vr={l['vol_ratio']:.1f} [{l['route']}]")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="盘中异动扫描 → watchdog surge")
    parser.add_argument("--daemon", action="store_true", help="每60s循环")
    parser.add_argument("--dry-run", action="store_true", help="只打印决策，不写 watchdog/signals")
    args = parser.parse_args()

    if args.daemon:
        # pid 防多实例（cron 每日启动 + 手动启动 撞车保护）
        pid_file = PLAY_DIR / "data" / "health" / "surge_scanner.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        if pid_file.exists():
            try:
                old_pid = int(pid_file.read_text().strip())
                os.kill(old_pid, 0)
                print(f"[surge] 已有实例在跑 (PID {old_pid})，退出")
                return
            except (ValueError, PermissionError):
                pass
            except OSError:
                pass  # 旧进程不存在
        pid_file.write_text(str(os.getpid()))
        import atexit
        atexit.register(lambda: pid_file.unlink() if pid_file.exists() else None)

        print(f"[surge] daemon 模式启动, 每60s扫描一次 → watchdog (窗口 09:35-11:30/13:00-15:00)")
        # 心跳文件：每轮循环更新 mtime，巡检脚本据此判断"进程活着但卡死"
        #（2026-08-05/08-10 两次实测：进程卡 futex 3天+，systemd 检测不到假死）
        heartbeat = PLAY_DIR / "data" / "health" / "surge_heartbeat"
        heartbeat.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                heartbeat.write_text(datetime.now().isoformat())
            except Exception:
                pass  # 心跳写失败不阻塞主循环
            now = datetime.now()
            hhmm = int(now.strftime("%H%M"))
            if hhmm >= 1500:
                # 睡到明天早盘，避免 systemd Restart=always 空转
                next_day = now + timedelta(days=1)
                if next_day.weekday() >= 5:
                    next_day += timedelta(days=7 - next_day.weekday())
                target = next_day.replace(hour=9, minute=35, second=0, microsecond=0)
                sleep_s = max((target - datetime.now()).total_seconds(), 60)
                print(f"[surge] 收盘({hhmm}), 休眠 {sleep_s/3600:.1f}h 到 {target.strftime('%m-%d %H:%M')}")
                time.sleep(sleep_s)
                continue
            if (935 <= hhmm < 1130 or 1300 <= hhmm < 1500) and _is_trade_day(_today()):
                try:
                    scan()
                except Exception as e:
                    # 瞬时错误（THS 超时/文件竞争）不杀 daemon，下轮重试
                    import traceback
                    print(f"  [surge] 本轮异常(已吞，下轮重试): {e}")
                    traceback.print_exc()
            time.sleep(60)
    else:
        scan(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
