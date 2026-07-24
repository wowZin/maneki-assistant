#!/usr/bin/env python3
"""盘中异动扫描 → watchdog surge 盯盘（代替推送）。

口径（2026-07-24 与用户确认）：
- 扫描宇宙：pool(50-300亿主板) ∪ 昨日涨停 ∪ 前20日涨停基因
  （覆盖率 ~81%，日扫描量 ~1500 只，仅为全市场 27%）
- 路由：
  - 昨日涨停票（在面板内）: 模型分 ≥ SURGE_LB_SCORE(默认20) → watchdog（连板通道）
  - 昨日涨停票（面板外）  : 排雷（量比≥2 + 筹码不压顶）→ watchdog
  - 首板票               : 排雷（量比≥2 + 板块联动≥2 + 筹码不压顶）→ watchdog
- surge 票写入 watchdog state.json（source="surge"），只发【surge】入场信号，
  无信号不通知；盘后零信号自动汰换（watchdog 侧实现）。
- 不再写 analysis.json（原推送链路废弃）。

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

PCT_LOW, PCT_HIGH = 3.0, 9.8            # 异动涨幅窗口
SURGE_LB_SCORE = float(os.getenv("SURGE_LB_SCORE", "20"))     # 连板通道模型分阈值
SURGE_VOL_RATIO = float(os.getenv("SURGE_VOL_RATIO", "2.0"))  # 排雷：量比下限
SURGE_MAX_WATCH = int(os.getenv("SURGE_MAX_WATCH", "10"))     # watchdog surge 上限（与 watchdog 侧一致）
SURGE_DAILY_MAX = int(os.getenv("SURGE_DAILY_MAX", "10"))     # 每日最多新增
SURGE_ROUND_MAX = int(os.getenv("SURGE_ROUND_MAX", "5"))      # 每轮最多新增


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


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


# ══════════════════════════════════════════════════════
# 主扫描
# ══════════════════════════════════════════════════════

def scan(dry_run: bool = False):
    td = _today()
    uni = build_universe(td)
    universe = uni["universe"]
    yesterday_limit = set(uni["yesterday_limit"].keys())
    print(f"  [surge] 宇宙: pool {len(uni['pool'])} + 昨日涨停 {len(yesterday_limit)}"
          f" + 基因 {len(uni['gene'])} = {len(universe)} 只")

    # 已在 analysis（pipeline 覆盖）或 watchdog 的票跳过
    existing_codes = set()
    af = ANALYSIS_DIR / f"{td}.json"
    if af.exists():
        try:
            existing_codes = {r["code"] for r in json.loads(af.read_text())}
        except Exception:
            pass
    wd_codes = _wd_codes()

    # THS 实时行情扫描
    from scripts.ths_client import get_ths_client as _ths
    ths = _ths()
    candidates = []  # (code, pct, vol_ratio)
    codes = list(universe.keys())
    for i in range(0, len(codes), 50):
        try:
            quotes = ths.get_batch_quotes(codes[i:i+50])
        except Exception as e:
            print(f"  [surge] 行情批次失败: {e}")
            continue
        for code, q in quotes.items():
            if q is None:
                continue
            pct = float(q.get("pct_chg", 0) or 0)
            if not (PCT_LOW <= pct < PCT_HIGH):
                continue
            full = f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
            if full in existing_codes or full in wd_codes:
                continue
            vr = float(q.get("vol_ratio", 0) or 0)
            candidates.append((full, pct, vr))

    if not candidates:
        print(f"  [surge] 无异动候选")
        return
    print(f"  [surge] 异动候选 {len(candidates)} 只: "
          + ", ".join(f"{c}({p:.1f}%)" for c, p, _ in candidates[:8]))

    # 连板通道：面板内昨日涨停票 → 模型分
    lb_candidates = [c for c, _, _ in candidates if c in yesterday_limit]
    lb_scored = {}
    if lb_candidates:
        pf = PANEL_DIR / f"{td}.parquet"
        if pf.exists():
            import pandas as pd
            panel = pd.read_parquet(pf).set_index("code")
            rows = []
            for c, pct, vr in candidates:
                if c not in lb_candidates or c not in panel.index:
                    continue
                row = panel.loc[c].to_dict()
                row["code"] = c
                row["pct_chg_score_day"] = pct
                if vr > 0:
                    row["volume_ratio"] = vr
                rows.append(row)
            if rows:
                try:
                    from plays.limit_up.factors.optimized.model_score import factor_model_score_batch
                    scores = factor_model_score_batch(pd.DataFrame(rows))
                    for r, s in zip(rows, scores):
                        lb_scored[r["code"]] = float(s)
                except Exception as e:
                    print(f"  [surge] 连板评分失败: {e}")

    # 路由
    round_codes = [c for c, _, _ in candidates]
    daily_added_file = SIGNALS_DIR / f"{td}.json"
    daily_count = 0
    if daily_added_file.exists():
        try:
            daily_count = len(json.loads(daily_added_file.read_text()))
        except Exception:
            pass

    picks, logs = [], []
    for c, pct, vr in candidates:
        if len(picks) >= SURGE_ROUND_MAX or daily_count + len(picks) >= SURGE_DAILY_MAX:
            break
        is_lb = c in yesterday_limit
        if is_lb and c in lb_scored:
            ok = lb_scored[c] >= SURGE_LB_SCORE
            route = f"连板 score={lb_scored[c]:.1f}{'≥' if ok else '<'}{SURGE_LB_SCORE:.0f}"
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
                ok = len(checks) == 2  # 面板外连板票：量比+筹码
            route = f"{'连板(面板外)' if is_lb else '首板'} 排雷={'/'.join(checks) or '无'}"
        logs.append({"code": c, "name": universe.get(c, ""), "pct": pct,
                     "vol_ratio": vr, "route": route, "pass": ok,
                     "ts": datetime.now().isoformat()})
        if ok:
            picks.append({"code": c, "name": universe.get(c, "")})

    if not dry_run:
        _log_signals(td, logs)
    added = _wd_add(picks, dry_run=dry_run) if picks else []
    tag = "[dry-run] " if dry_run else ""
    print(f"  [surge] {tag}路由: 候选{len(candidates)} → 通过{len(picks)} → 入watchdog {len(added)}")
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
        print(f"[surge] daemon 模式启动, 每300s扫描一次 → watchdog")
        while True:
            now = datetime.now()
            hhmm = int(now.strftime("%H%M"))
            if 925 <= hhmm < 1130 or 1300 <= hhmm < 1500:
                scan()
            time.sleep(300)
    else:
        scan(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
