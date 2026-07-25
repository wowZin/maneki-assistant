#!/usr/bin/env python3
"""盘中异动扫描 → watchdog surge 盯盘（代替推送）。

口径（2026-07-24 与用户确认）：
- 扫描宇宙：pool(全市场主板，无市值带) ∪ 昨日涨停 ∪ 前20日涨停基因
- 行情源：ths_client.get_batch_quotes_fast（并发批量，~30s/轮）
- 路由：
  ① 面板早盘评分 ≥ SURGE_PANEL_SCORE(默认20) → 主闸（pipeline 09:30 评分产物）
  ② 面板外无分票 → 排雷兜底（首板: 量比≥2+窄概念联动≥2+筹码不压顶；
     昨日涨停无分票: 量比≥2+筹码不压顶）
- 通过的票同时写：watchdog state.json（source="surge"）、analysis.json、
  pushed/{date}_surge.json（pipeline 同构记录，按 code 去重）
- surge 票只发【surge】入场信号，无信号不通知；盘后零信号自动汰换（watchdog 侧实现）。

用法:
    python3 plays/limit_up/surge_scanner.py            # 扫描一次
    python3 plays/limit_up/surge_scanner.py --daemon   # 每5分钟循环
    python3 plays/limit_up/surge_scanner.py --dry-run  # 只打印路由决策，不写 watchdog
"""
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

PLAY_DIR = Path(__file__).resolve().parent
ANALYSIS_DIR = PLAY_DIR / "data" / "analysis"
SIGNALS_DIR = PLAY_DIR / "data" / "signals"
PANEL_DIR = PLAY_DIR.parent.parent / "wiki" / "raw" / "limit-up" / "panel"
WATCHDOG_STATE = PLAY_DIR.parent.parent / "plays" / "watchdog" / "data" / "state.json"

PCT_LOW = float(os.getenv("SURGE_PCT_LOW", "5.0"))    # 异动涨幅窗口（5%≤涨幅<9%）
PCT_HIGH = float(os.getenv("SURGE_PCT_HIGH", "9.0"))
SURGE_PANEL_SCORE = float(os.getenv("SURGE_PANEL_SCORE", "20"))  # 主闸：面板早盘评分阈值
SURGE_VOL_RATIO = float(os.getenv("SURGE_VOL_RATIO", "2.0"))  # 排雷：量比下限
SURGE_MAX_WATCH = int(os.getenv("SURGE_MAX_WATCH", "10"))     # watchdog surge 上限（与 watchdog 侧一致）
SURGE_DAILY_MAX = int(os.getenv("SURGE_DAILY_MAX", "10"))     # 每日最多新增
SURGE_ROUND_MAX = int(os.getenv("SURGE_ROUND_MAX", "5"))      # 每轮最多新增


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
    """pool ∪ 昨日涨停 ∪ 前20日涨停基因。返回 {code: name} 及分组。"""
    cache = PLAY_DIR / "data" / "pool" / f"surge_universe_{td}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass

    from scripts.tu_share import call_tushare

    # pool
    pool_file = PLAY_DIR / "data" / "pool" / f"pool_{td}.json"
    if not pool_file.exists():
        # pipeline 已改为一次性进程，不再建池；surge 自治补齐
        try:
            from plays.limit_up.pool_builder import ensure_pool
            ensure_pool(td)
        except Exception as e:
            print(f"  [surge] 建池失败: {e}")
    pool = json.loads(pool_file.read_text()) if pool_file.exists() else []
    pool_map = {s["code"]: s.get("name", "") for s in pool}

    # 昨日涨停 + 前20日基因（一次调用拉40天）
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

    universe = dict(pool_map)
    for m in (yesterday_map, gene_map):
        for c, n in m.items():
            universe.setdefault(c, n)

    out = {
        "date": td, "limit_yesterday_date": yesterday,
        "pool": pool_map, "yesterday_limit": yesterday_map,
        "gene": gene_map, "universe": universe,
    }
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
    """把 surge 票写入 watchdog state.json。entries: [{code,name}]。返回成功添加的 code。"""
    if dry_run:
        return [e["code"] for e in entries]
    added = []
    for attempt in range(3):
        try:
            states = json.loads(WATCHDOG_STATE.read_text()) if WATCHDOG_STATE.exists() else {}
            surge_count = sum(1 for s in states.values() if s.get("source") == "surge")
            for e in entries:
                if e["code"] in states or e["code"] in added:
                    continue
                if surge_count >= SURGE_MAX_WATCH:
                    break
                states[e["code"]] = {
                    "code": e["code"], "name": e.get("name", ""),
                    "added_at": datetime.now().isoformat(),
                    "status": "watching", "source": "surge",
                    "entry_pushed_date": "", "entry_price": 0, "entry_at": "",
                    "highest_since_entry": 0, "bars_held": 0,
                    "signal_type": "", "signal_reason": "", "signal_at": "",
                    "last_alert_at": "", "last_abnormal_level": "",
                    "last_abnormal_pushed_at": 0, "netflow_history": [],
                    "daily_basic": {}, "dim_scores": {}, "last_daily_update": "",
                }
                added.append(e["code"])
                surge_count += 1
            WATCHDOG_STATE.write_text(json.dumps(states, ensure_ascii=False, indent=2))
            # 回读校验（watchdog 引擎每30秒重写 state，可能覆盖；重写则重试）
            back = json.loads(WATCHDOG_STATE.read_text())
            if all(c in back for c in added):
                return added
        except Exception as ex:
            print(f"  [surge] 写 watchdog 失败(attempt {attempt+1}): {ex}")
            time.sleep(1)
    return added


def _wd_codes() -> set:
    try:
        states = json.loads(WATCHDOG_STATE.read_text())
        return set(states.keys())
    except Exception:
        return set()


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

def _surge_record(code: str, name: str, pct: float, morning_rec: dict | None) -> dict:
    """构造 surge 记录（pipeline 精简格式 + source=surge）。"""
    if morning_rec:
        rec = dict(morning_rec)
        rec["pct_chg"] = pct
        rec["source"] = "surge"
        return rec
    return {
        "code": code, "name": name,
        "model_score": None, "total_score": None,
        "score_mode": "surge_screen", "pct_chg": pct,
        "scores": {"technical": 0, "fundflow": 0, "sentiment": 0, "shortterm": 0},
        "fundamental": 0, "source": "surge",
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

def scan(dry_run: bool = False):
    td = _today()
    uni = build_universe(td)
    universe = uni["universe"]
    yesterday_limit = set(uni["yesterday_limit"].keys())

    # 面板早盘评分（pipeline 09:30 全量评分产物）→ 主闸
    morning: dict[str, dict] = {}
    af = ANALYSIS_DIR / f"{td}.json"
    if af.exists():
        try:
            for r in json.loads(af.read_text()):
                if isinstance(r, dict) and r.get("code"):
                    morning[r["code"]] = r
        except Exception:
            pass
    morning_scores = {c: float(r.get("model_score") or r.get("total_score") or 0)
                      for c, r in morning.items()}
    print(f"  [surge] 宇宙 {len(universe)} 只 | 面板分≥{SURGE_PANEL_SCORE:.0f}: "
          f"{sum(1 for s in morning_scores.values() if s >= SURGE_PANEL_SCORE)} 只")

    wd_codes = _wd_codes()

    # THS 并发批量实时行情（ths_client.get_batch_quotes_fast，线程池）
    from scripts.ths_client import get_ths_client as _ths
    _workers = int(os.getenv("SURGE_QUOTE_WORKERS", "24"))  # 全市场池(3000+)压测 24线程≈52s
    quotes = _ths().get_batch_quotes_fast(list(universe.keys()), workers=_workers)
    candidates = []  # (code, pct, vol_ratio)
    quote_map = {}   # full_code -> quote dict（快照用）
    for code, q in quotes.items():
        if q is None:
            continue
        pct = float(q.get("pct_chg", 0) or 0)
        if not (PCT_LOW <= pct < PCT_HIGH):
            continue
        full = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
        if full in wd_codes:  # 已在盯盘，去重
            continue
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
        _log_snapshots(td, [(c, p, v, quote_map[c]) for c, p, v in candidates], morning_scores)

    # 路由：① 面板早盘分≥20（主闸） ② 面板外无分票 → 排雷兜底
    round_codes = [c for c, _, _ in candidates]
    daily_added_file = SIGNALS_DIR / f"{td}.json"
    daily_count = 0
    if daily_added_file.exists():
        try:
            # 只数通过的（pass=True），被拒日志不占每日额度
            daily_count = sum(1 for l in json.loads(daily_added_file.read_text())
                              if isinstance(l, dict) and l.get("pass"))
        except Exception:
            pass

    picks, logs = [], []
    for c, pct, vr in candidates:
        if len(picks) >= SURGE_ROUND_MAX or daily_count + len(picks) >= SURGE_DAILY_MAX:
            break
        is_lb = c in yesterday_limit
        # 分数>0 才算"有分"：surge 排雷票写入 analysis 时 model_score=None→0，
        # 避免其下轮被误判为"面板分0<20"而跳过排雷通道
        if c in morning_scores and morning_scores[c] > 0:
            sc = morning_scores[c]
            ok = sc >= SURGE_PANEL_SCORE
            route = f"面板分{sc:.1f}{'≥' if ok else '<'}{SURGE_PANEL_SCORE:.0f}"
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
                ok = len(checks) == 2  # 面板外昨日涨停票：量比+筹码
            route = f"{'连板(无分)' if is_lb else '首板(无分)'} 排雷={'/'.join(checks) or '无'}"
        logs.append({"code": c, "name": universe.get(c, ""), "pct": pct,
                     "vol_ratio": vr, "route": route, "pass": ok,
                     "ts": datetime.now().isoformat()})
        if ok:
            picks.append({"code": c, "name": universe.get(c, ""),
                          "pct": pct, "morning_rec": morning.get(c)})

    if not dry_run:
        _log_signals(td, logs)

    # 写 watchdog state（surge 标签）+ analysis + pushed（格式一致，按 code 去重）
    recs = [_surge_record(p["code"], p["name"], p["pct"], p["morning_rec"]) for p in picks]
    added = _wd_add([{"code": p["code"], "name": p["name"]} for p in picks],
                    dry_run=dry_run) if picks else []
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
    parser.add_argument("--daemon", action="store_true", help="每5分钟循环")
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

        print(f"[surge] daemon 模式启动, 每300s扫描一次 → watchdog (窗口 09:35-11:30/13:00-15:00)")
        while True:
            now = datetime.now()
            hhmm = int(now.strftime("%H%M"))
            if (935 <= hhmm < 1130 or 1300 <= hhmm < 1500) and _is_trade_day(_today()):
                scan()
            time.sleep(300)
    else:
        scan(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
