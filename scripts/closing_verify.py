#!/usr/bin/env python3
"""收盘复盘一键交叉验证脚本（maneki-closing-review 技能配套）

用法: python3 scripts/closing_verify.py [YYYYMMDD]   # 默认今天
覆盖检查项（对应 SKILL.md 第 4 节 4a~4g）:
  1. Top10 total_score（analysis 快照）
  2. total_score>=50 候选（对照推送数 → 发现 Top3 截断漏推，4b-2）
  3. 推送记录（glob {td}*.json 去 surge）→ 推送分 vs analysis 最新分背离 → 异常推送（4b-3）
  4. tushare daily 收盘 → 涨停名单 + Top5 高分股收盘背离（4a）+ 推送票收盘（大跌/涨停标记）
  5. check_hold 真实持仓 vs state.json entered → state 缺失持仓（4e，含盘后汰换证据 grep）
  6. ws_snap L1 键覆盖（4e 行情链路）
  7. 概念板块 ths_daily top/bottom（卡片 1）
cron 模式注意：不能 python3 -c / execute_code，直接运行本文件即可。
"""
import json
import sys
import datetime
import os
from pathlib import Path

sys.path.insert(0, "/root/maneki-agent")
TD = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().strftime("%Y%m%d")
ROOT = Path("/root/maneki-agent")


def get(r, *ks):
    for k in ks:
        if isinstance(r, dict) and k in r:
            return r[k]
    return None


def total_score(r):
    v = get(r, "total_score", "total", "score")
    return v if isinstance(v, (int, float)) else 0.0


def name_of(r):
    return get(r, "name", "stock_name") or r.get("code", "")


def call_tu(token, api, params, fields=""):
    import requests
    r = requests.post("http://api.tushare.pro", json={
        "api_name": api, "token": token, "params": params, "fields": fields}, timeout=30)
    j = r.json()
    if j.get("code") != 0:
        print(f"  [tushare err] {api}: {j.get('msg')}")
        return []
    data = j.get("data") or {}
    return [dict(zip(data.get("fields") or [], it)) for it in (data.get("items") or [])]


def main():
    # --- 1/2. analysis ---
    ana_path = ROOT / "plays/limit_up/data/analysis" / f"{TD}.json"
    ana = json.loads(ana_path.read_text())
    if isinstance(ana, dict):
        ana = list(ana.values())
    print(f"[analysis] {len(ana)} 条")
    top = sorted(ana, key=total_score, reverse=True)[:10]
    print("\n===== Top10 total_score =====")
    for r in top:
        print(f"  {r.get('code')} {name_of(r)} total={total_score(r):.2f} pct={get(r,'pct_chg')}")
    over50 = sorted([r for r in ana if total_score(r) >= 50], key=total_score, reverse=True)
    print(f"\n===== total_score>=50 共 {len(over50)} 只 =====")
    for r in over50:
        print(f"  {r.get('code')} {name_of(r)} total={total_score(r):.2f} pct={get(r,'pct_chg')}")

    # --- 3. 推送 + 背离检查 ---
    pushed = []
    for p in sorted((ROOT / "plays/limit_up/data/pushed").glob(f"{TD}*.json")):
        if "surge" in p.name:
            continue
        data = json.loads(p.read_text())
        if isinstance(data, list):
            pushed.extend(data)
        elif isinstance(data, dict) and "stocks" in data:
            pushed.extend(data["stocks"])
    ana_map = {r.get("code"): r for r in ana}
    print(f"\n===== 推送 {len(pushed)} 条（去surge）vs analysis 最新分 ===== 文件: {[p.name for p in sorted((ROOT / 'plays/limit_up/data/pushed').glob(f'{TD}*.json')) if 'surge' not in p.name]}")
    for r in pushed:
        a = ana_map.get(r.get("code"))
        if a:
            diff = total_score(r) - total_score(a)
            flag = " ⚠️背离>20" if abs(diff) > 20 else ""
            print(f"  {r.get('code')} {r.get('name')} 推送分={total_score(r):.2f} 最新分={total_score(a):.2f} pct={get(r,'pct_chg')}{flag}")
        else:
            print(f"  {r.get('code')} {r.get('name')} 推送分={total_score(r):.2f} 不在今日analysis")

    # --- 4. tushare 收盘 ---
    env = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("TUSHARE_TOKEN="):
            env["TUSHARE_TOKEN"] = line.split("=", 1)[1].strip()
    tok = env.get("TUSHARE_TOKEN", "")
    daily_map = {d["ts_code"]: d for d in call_tu(tok, "daily", {"trade_date": TD}, "ts_code,close,pct_chg")}
    sb = {r["ts_code"]: r["name"] for r in call_tu(tok, "stock_basic", {"list_status": "L"}, "ts_code,name")}

    def is_main_board(code):
        return (code.endswith(".SH") and code.startswith(("600", "601", "603", "605"))) or \
               (code.endswith(".SZ") and code.startswith(("000", "001", "002", "003")))

    st_codes = {r.get("code") for r in ana if "ST" in str(name_of(r)).upper() or "退" in str(name_of(r))}
    limit_ups = [d for d in daily_map.values() if d["pct_chg"] >= 9.8 and is_main_board(d["ts_code"])
                 and d["ts_code"] not in st_codes]
    print(f"\n===== 主板非ST涨停 {len(limit_ups)} 只（≥9.8%）=====")
    scored = sorted(((d["ts_code"], total_score(ana_map[d["ts_code"]]) if d["ts_code"] in ana_map else 0,
                      d["pct_chg"], name_of(ana_map[d["ts_code"]]) if d["ts_code"] in ana_map else "")
                     for d in limit_ups), key=lambda x: x[1], reverse=True)
    for code, s, pct, nm in scored[:20]:
        print(f"  {code} {nm} score={s:.2f} close={pct:+.2f}")

    print("\n===== Top5 高分股收盘背离 =====")
    for r in top[:5]:
        d = daily_map.get(r.get("code"))
        if d:
            flag = " ⚠️背离" if d["pct_chg"] < -2 else ""
            print(f"  {r.get('code')} {name_of(r)} score={total_score(r):.2f} 快照pct={get(r,'pct_chg')} 收盘={d['pct_chg']:+.2f}{flag}")

    print("\n===== 推送票收盘 =====")
    for r in pushed:
        d = daily_map.get(r.get("code"))
        if d:
            tag = "🔴大跌" if d["pct_chg"] < -3 else ("🟢涨停" if d["pct_chg"] >= 9.8 else "")
            print(f"  {r.get('code')} {r.get('name')} 分={total_score(r):.2f} 推送pct={get(r,'pct_chg')} 收盘={d['pct_chg']:+.2f} {tag}")

    # --- 5. check_hold vs state ---
    from scripts.jvquant_trade_client import check_hold
    h = check_hold()
    state = json.loads((ROOT / "plays/watchdog/data/state.json").read_text())
    entered = {c: v for c, v in state.items() if v.get("status") == "entered"}
    # ⚠️ check_hold 数值字段全是 str（20260806 实测）：必须 int() 转换再比较，否则
    # '>' not supported between instances of 'str' and 'int' 直接崩脚本（20260807 实测）。
    held = [p for p in h.get("hold_list", []) if int(p.get("hold_vol", 0) or 0) > 0]
    print(f"\n===== 真实持仓 {len(held)} 只 vs state entered {len(entered)} =====")
    log = (ROOT / "logs/watchdog.log").read_text(errors="ignore")
    for p in held:
        code = p["code"] + (".SH" if p["code"].startswith("6") else ".SZ")
        st = entered.get(code)
        if st and daily_map.get(code):
            # reconcile 加回的票 entry_price 可能=0（对账自动加回 entry=0.00）→ 除零防护
            ep = st["entry_price"] or 0.0
            pnl = (daily_map[code]["close"] / ep - 1) * 100 if ep else 0.0
            print(f"  {code} {p.get('name')} hold={p.get('hold_vol')} entry={ep} 收盘={daily_map[code]['pct_chg']:+.2f}% 持仓盈亏={pnl:+.2f}% ✓")
        else:
            print(f"  {code} {p.get('name')} hold={p.get('hold_vol')} ✗ 不在state（明日无自动出场监控）")
    for sig in ["跳过买入: 已持仓", "盘后汰换"]:
        hits = [l.strip() for l in log.splitlines() if sig in l and f"{TD[:4]}-{TD[4:6]}-{TD[6:]}" in l]
        if hits:
            print(f"\n[{sig}] 今日 {len(hits)} 条, 最新: {hits[-1][:160]}")

    # --- 6. ws_snap L1 覆盖 ---
    snap = Path("/dev/shm/ws_snap.json")
    if snap.exists():
        d = json.loads(snap.read_text())
        keys = list(d.keys()) if isinstance(d, dict) else []
        held_codes = [p["code"] + (".SH" if p["code"].startswith("6") else ".SZ") for p in held]
        missing = [c for c in held_codes if c not in keys and c[:6] not in keys]
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(snap)).strftime("%H:%M")
        print(f"\n===== ws_snap mtime={mt} n_keys={len(keys)} 持仓缺L1: {missing} =====")

    # --- 7. 概念板块 ---
    resp = call_tu(tok, "ths_index", {"limit": 2000}, "ts_code,name")
    nm = {r["ts_code"]: r["name"] for r in resp}
    items = call_tu(tok, "ths_daily", {"trade_date": TD}, "ts_code,pct_change")
    exclude = ["样本股", "成份股", "三板", "两板", "停牌", "上市首", "打板", "炸板", "连板", "涨停表现"]
    concepts = [(nm.get(i["ts_code"], i["ts_code"]), float(i["pct_change"])) for i in items
                if i["ts_code"] in nm and not any(x in nm.get(i["ts_code"], "") for x in exclude)]
    concepts.sort(key=lambda x: x[1], reverse=True)
    print("\n===== 概念板块 前3/后3 =====")
    for n, p in concepts[:3] + concepts[-3:]:
        print(f"  {n} {p:+.2f}%")


if __name__ == "__main__":
    main()
