#!/usr/bin/env python3
"""清理 state.json 中"已卖出但残留 entered"的盯盘记录（2026-08-17）。

依据（当日已核实）：
- 7 只（603161/000596/000952/003023/600664/601026/603496）：用户 11:02-11:06 手动卖出，
  jvquant check_order 委托全部"已成"，check_hold hold_vol=0
- 2 只（600653/002679）：08-10 买入，用户更早手动卖出，check_hold 完全无仓（连残留都无），
  state 残留 entered 多日
删除后 watchdog 引擎经 _reload_state_if_changed 自动移除盯盘并退订行情。

用法：
  python3 plays/watchdog/cleanup_state.py --dry-run   # 只打印将删除的票
  python3 plays/watchdog/cleanup_state.py             # 正式清理（原子写）
"""
import json
import os
import sys
from pathlib import Path

STATE = Path("/root/maneki-agent/plays/watchdog/data/state.json")

# code -> 删除依据
TARGETS = {
    "600653.SH": "用户手动卖出(state残留, check_hold无仓)",
    "002679.SZ": "用户手动卖出(state残留, check_hold无仓)",
    "603161.SH": "11:04 手动卖出 16.30x200 已成",
    "000596.SZ": "11:02 手动卖出 93.00x200 已成",
    "000952.SZ": "11:03 手动卖出 6.36x200 已成",
    "003023.SZ": "11:05 手动卖出 27.50x200 已成",
    "600664.SH": "11:03 手动卖出 8.84x200 已成",
    "601026.SH": "11:05 手动卖出 16.60x200 已成",
    "603496.SH": "11:06 手动卖出 26.62x200 已成",
}


def _ctp_has_position(codes: list[str]) -> set[str]:
    """保险检查：查 check_hold 是否仍有持仓，失败则放行（当日 check_order 已成证据已核实）。"""
    try:
        sys.path.insert(0, "/root/maneki-agent")
        from scripts.jvquant_trade_client import check_hold

        r = check_hold()
        hl = r.get("hold_list") or []
        with_pos = set()
        for h in hl:
            try:
                if int(h.get("hold_vol") or 0) > 0:
                    with_pos.add(str(h["code"]).split(".")[0])
            except (ValueError, TypeError, KeyError):
                continue
        return {c for c in codes if c.split(".")[0] in with_pos}
    except Exception as e:  # noqa: BLE001
        print(f"[warn] check_hold 检查失败({e})，按已核实证据放行清理")
        return set()


def main() -> None:
    dry = "--dry-run" in sys.argv
    if not STATE.exists():
        print(f"[error] state 文件不存在: {STATE}")
        sys.exit(1)

    s = json.loads(STATE.read_text())
    existing = [c for c in TARGETS if c in s]
    if not existing:
        print("[cleanup] 目标票均已不在 state，无需清理")
        return

    # 保险：跳过仍有 CTP 持仓的票
    still_hold = _ctp_has_position(existing)
    if still_hold:
        print(f"[warn] 以下票 check_hold 仍有持仓，跳过: {sorted(still_hold)}")
        existing = [c for c in existing if c not in still_hold]

    if dry:
        print(f"[dry-run] 将删除 {len(existing)} 只残留 entered:")
        for c in existing:
            v = s.get(c, {})
            print(f"  {c} {v.get('status')} entry={v.get('entry_price')} {str(v.get('entry_at'))[:16]}  ← {TARGETS[c]}")
        return

    removed = []
    for c in existing:
        v = s.get(c)
        if v is not None:
            removed.append((c, v.get("status"), v.get("entry_price"), str(v.get("entry_at"))[:16]))
            del s[c]

    # 原子写（tmp 带 pid，防多进程并发覆盖）
    tmp = STATE.with_name(f"state.json.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=2))
    tmp.replace(STATE)

    print(f"[cleanup] 已删除 {len(removed)} 只残留 entered, state 剩余 {len(s)} 条")
    for c, st, ep, ea in removed:
        print(f"  删除 {c} {st} entry={ep} {ea}  ← {TARGETS[c]}")


if __name__ == "__main__":
    main()
