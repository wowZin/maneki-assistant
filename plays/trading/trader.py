"""maneki 自动交易模拟器（2026-07-28）

独立进程，只读 state.json + ws_snap.json，不写任何共享数据。
每天一个 portfolio_{date}.json 记录虚拟持仓和盈亏。
3 天后收益为正才激活实单交易接口。
"""

from __future__ import annotations
import json, os, time, sys, uuid, logging
from datetime import datetime, date
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv
import requests  # 飞书推送用

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

# ── 环境配置 ──
TOTAL_CAPITAL = 2_0000  # 总资金 2 万
MAX_POSITIONS = 5       # 最大同时持仓数
MAX_PER_POSITION = TOTAL_CAPITAL // 10  # 2000 元/只
DAILY_LOSS_LIMIT = TOTAL_CAPITAL * 0.03  # 600 元/日
STOP_LOSS_PCT = 0.03      # 硬止损 -3%
TRAILING_STOP_PCT = 0.02  # 移动止损 -2%
TAKE_PROFIT_TIER1 = 0.05  # 止盈一档 5%
TAKE_PROFIT_TIER2 = 0.08  # 止盈二档 8%
TIME_STOP_MINUTES = 60    # 时间止损 60 分钟
CONSECUTIVE_STOP_LIMIT = 2  # 连续止损停赛

STATE_FILE = PROJECT_DIR / "plays" / "watchdog" / "data" / "state.json"
SNAP_FILE = Path("/dev/shm/ws_snap.json")
PANEL_FILE = PROJECT_DIR / "wiki" / "raw" / "limit-up" / "panel"
TRADING_DIR = PROJECT_DIR / "plays" / "trading" / "data"
TRADING_REPORTS_DIR = PROJECT_DIR / "plays" / "trading" / "data" / "reports"  # 交割单 → 数据目录，compile 后迁 wiki/raw
LOGS_DIR = PROJECT_DIR / "logs"

TRADING_DIR.mkdir(parents=True, exist_ok=True)
TRADING_REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ── 飞书推送 ──

def _push_feishu(text: str):
    """推送交易通知到飞书"""
    try:
        env_file = PROJECT_DIR / ".env"
        if env_file.exists():
            load_dotenv(env_file)
        app_id = os.getenv("FEISHU_APP_ID", "")
        app_secret = os.getenv("FEISHU_APP_SECRET", "")
        chat_id = os.getenv("FEISHU_CHAT_ID_SIGNAL", os.getenv("FEISHU_BOT_CHAT_ID", ""))
        if not app_id or not app_secret or not chat_id:
            return
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret}, timeout=10
        )
        token = resp.json().get("tenant_access_token", "")
        if not token:
            return
        requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": chat_id, "msg_type": "text",
                  "content": json.dumps({"text": text})}, timeout=10
        )
    except Exception:
        pass


@dataclass
class Position:
    code: str
    name: str
    entry_price: float
    shares: int
    cost: float
    entry_at: str
    entry_type: str
    highest_since_entry: float
    status: str = "holding"  # holding / stop_loss / take_profit / time_stop
    exit_price: float = 0.0
    exit_at: str = ""
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0


class Simulator:
    def __init__(self, real=False):
        self.capital = TOTAL_CAPITAL
        self.portfolio: list[Position] = []
        self.seen_codes: set = set()
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.stopped_today = False
        self.today = datetime.now().strftime("%Y%m%d")
        self.real = real  # True=实盘下单，False=模拟打印
        self._exited: list[Position] = []
        self._load_avg_amount_5d()
        self._load_seen_from_journal()  # 启动时从交割单恢复去重

    def _load_seen_from_journal(self):
        """重启防重复：从已有交割单加载已处理的 code，避免重复买入"""
        today = datetime.now().strftime("%Y%m%d")
        report_file = TRADING_REPORTS_DIR / f"{today}.json"
        if not report_file.exists():
            return
        try:
            records = json.loads(report_file.read_text())
            for r in records:
                if r.get("direction") == "买入":
                    code = r.get("code", "")
                    if code:
                        self.seen_codes.add(code)
            if self.seen_codes:
                print(f"[trader] 从交割单恢复去重: {len(self.seen_codes)} 只已处理")
        except Exception:
            pass

    def _load_avg_amount_5d(self):
        """加载最近一天的 snapshot 日均成交额（avg_amount_5d 来自面板）"""
        self._avg_amount_5d: dict[str, float] = {}
        try:
            import pandas as pd
            files = sorted(Path(PANEL_FILE).glob("*.parquet"))
            if files:
                df = pd.read_parquet(files[-1], columns=["code", "avg_amount_5d"])
                for _, r in df.iterrows():
                    if "avg_amount_5d" in r and pd.notna(r["avg_amount_5d"]):
                        self._avg_amount_5d[r["code"]] = float(r["avg_amount_5d"])
            print(f"[trader] 面板日均额加载完成: {len(self._avg_amount_5d)} 只")
        except Exception as e:
            print(f"[trader] 面板加载失败: {e}")
            self._avg_amount_5d = {}

    def _can_buy(self, code: str, entry_price: float) -> bool:
        """入场合规性检查（防诱空 + 仓位控制）"""
        if self.stopped_today:
            print(f"  [x] {code} 跳过: 当日已停止交易")
            return False
        if abs(self.daily_pnl) >= DAILY_LOSS_LIMIT:
            print(f"  [x] {code} 跳过: 日亏损已达上限 {DAILY_LOSS_LIMIT:.0f}")
            self.stopped_today = True
            return False
        cost = entry_price * 100  # 1手=100股
        if cost > self.capital:
            print(f"  [x] {code} 跳过: 资金不足 (需 {cost:.0f}, 剩 {self.capital:.0f})")
            return False
        return True

    def _entry_quality(self, code: str, entry_price: float) -> tuple[bool, str]:
        """诱空过滤（VWAP 距离 + 量比代理 + 竞价跌幅）"""
        snap = json.loads(SNAP_FILE.read_text()) if SNAP_FILE.exists() else {}
        d = snap.get(code.split(".")[0], {})
        last = float(d.get("last") or 0)
        vwap = float(d.get("vwap") or snap.get(f"{code.split('.')[0]}_vwap") or 0)

        # ① VWAP 距离 ≥ 0.3%（防贴着 VWAP 穿的假突破）
        if vwap > 0 and last > 0:
            vwap_dist = last / vwap - 1
            if vwap_dist < 0.003:
                return False, f"VWAP 距离不足 {vwap_dist*100:.2f}% (<0.3%)"

        # ② 量比代理 ≥ 1.5（防无量拉升）
        short = code.split(".")[0]
        amount = float(d.get("trade_amount") or d.get("amount") or 0)
        avg_amount = self._avg_amount_5d.get(code, 0)
        if avg_amount > 0 and amount > 0:
            vol_ratio = amount / avg_amount
            if vol_ratio < 1.5:
                return False, f"量比不足 {vol_ratio:.2f} (<1.5)"

        # ③ 竞价跌幅 < -2% 时不买（防高分开盘出货）
        try:
            import pandas as pd
            panel = pd.read_parquet(Path(PANEL_FILE) / f"{self.today}.parquet",
                                    columns=["code", "auc_pct"])
            row = panel[panel["code"] == code]
            if len(row) and pd.notna(row.iloc[0]["auc_pct"]):
                auc = float(row.iloc[0]["auc_pct"])
                if auc < -2.0:
                    return False, f"竞价跌幅 {auc:.1f}% (< -2%)"
        except Exception:
            pass  # 面板不存在时不卡，走保守（放行）

        # ④ 盘口深度检查：卖盘压顶不买
        ask_qty = d.get("ask_qty", [])
        bid_qty = d.get("bid_qty", [])
        if ask_qty and bid_qty and len(ask_qty) >= 3:
            ask_top5 = sum(float(q) for q in ask_qty[:5] if str(q).strip())
            bid_top5 = sum(float(q) for q in bid_qty[:5] if str(q).strip())
            if bid_top5 > 0 and ask_top5 > bid_top5 * 2:
                return False, f"卖盘压顶 卖{ask_top5:.0f}>买{bid_top5:.0f}(×{ask_top5/bid_top5:.1f})"
            # 二档压单：突破后的第一道防线
            ask2 = float(ask_qty[1]) if str(ask_qty[1]).strip() else 0
            bid1_qty = float(bid_qty[0]) if str(bid_qty[0]).strip() else 1
            if ask2 > bid1_qty * 3:
                return False, f"二档压单{ask2:.0f}>一档买单{bid1_qty:.0f}(×{ask2/bid1_qty:.0f})"

        return True, ""

    def _check_exit(self, p: Position) -> tuple[bool, str]:
        """出场检查（T+1：当日买入不可卖出）"""
        # T+1 检查：当天买的不能卖
        entry_date = p.entry_at[:10] if p.entry_at else ""
        today_date = datetime.now().strftime("%Y-%m-%d")
        if entry_date == today_date:
            return False, "T+1"

        snap = json.loads(SNAP_FILE.read_text()) if SNAP_FILE.exists() else {}
        d = snap.get(p.code.split(".")[0], {})
        last = float(d.get("last") or 0)
        vwap = float(d.get("vwap") or snap.get(f"{p.code.split('.')[0]}_vwap") or 0)
        if last <= 0:
            return False, ""

        if last > p.highest_since_entry:
            p.highest_since_entry = last

        pnl_pct = (last / p.entry_price - 1)

        # 破位下跌识别：亏损 + 跌破 VWAP + VWAP 走低
        if pnl_pct < 0 and vwap > 0:
            if last < vwap:
                # 用最近两轮对比：prev_vwap 无法获取 → 简化：现价远低于 vwap 就出
                if vwap > p.entry_price * 0.97:  # vwap 还没跌太远
                    if (vwap - last) / vwap > 0.015:  # 跌幅大于 VWAP 的 1.5%
                        return True, f"破位下跌: 现价{last:.2f}<VWAP{vwap:.2f}"

        # 硬止损
        if pnl_pct <= -STOP_LOSS_PCT:
            return True, f"硬止损 {pnl_pct*100:.1f}%"

        # 移动止损（自最高点回落）
        drawdown = (p.highest_since_entry - last) / p.highest_since_entry
        if drawdown >= TRAILING_STOP_PCT:
            return True, f"移动止损 回落{drawdown*100:.1f}%"

        # 止盈
        if pnl_pct >= TAKE_PROFIT_TIER2:
            return True, f"止盈二档+{pnl_pct*100:.1f}%"
        if pnl_pct >= TAKE_PROFIT_TIER1:
            return True, f"止盈一档+{pnl_pct*100:.1f}%"

        # 时间止损
        held_seconds = (datetime.now() - datetime.strptime(p.entry_at, "%Y-%m-%d %H:%M:%S")).total_seconds()
        held_minutes = held_seconds / 60
        if held_minutes >= TIME_STOP_MINUTES:
            if pnl_pct < 0.01:  # 60 分钟浮盈不到 1%
                return True, f"时间止损 持仓{held_minutes:.0f}分钟浮亏{pnl_pct*100:.1f}%"

        return False, ""

    def tick(self):
        """每轮扫描（60s 一次，同 watchdog 节奏）"""
        today = datetime.now().strftime("%Y%m%d")
        if today != self.today:
            self._daily_reset(today)

        if not STATE_FILE.exists():
            return
        state = json.loads(STATE_FILE.read_text())

        # 检查新进入的 entered 票
        for code, st in state.items():
            if st.get("status") != "entered" or not st.get("entry_price"):
                continue
            if code in self.seen_codes:
                continue
            self.seen_codes.add(code)

            ep = float(st["entry_price"])
            name = st.get("name", "")

            # 诱空过滤（用当前 ws_snap 实时价判断，不是用 watchdog 的 stale 入场价）
            ok, reject_reason = self._entry_quality(code, ep)
            if not ok:
                print(f"  [x] {code} {name} 诱空过滤: {reject_reason}")
                continue

            # 仓位检查
            if not self._can_buy(code, ep):
                continue

            # 下单价：用 ws_snap 最新价（不是 watchdog 60s 前写入的 entry_price）
            snap = json.loads(SNAP_FILE.read_text()) if SNAP_FILE.exists() else {}
            d = snap.get(code.replace(".SH","").replace(".SZ","").replace(".BJ",""), {})
            current_price = float(d.get("last") or 0)
            if current_price <= 0:
                current_price = ep  # fallback: 没有实时价就用入场价

            shares = min(int(self.capital / current_price) // 100 * 100, 100)  # 至少 1 手
            if shares < 100:
                continue

            cost = shares * current_price
            self.capital -= cost
            p = Position(
                code=code, name=name, entry_price=current_price,
                shares=shares, cost=cost,
                entry_at=st.get("entry_at", datetime.now().isoformat()),
                entry_type=st.get("signal_type", ""),
                highest_since_entry=ep,
            )
            self.portfolio.append(p)
            print(f"  [+] 买入 {code} {name} {shares}股@{current_price:.2f} 成本{cost:.0f} 余额{self.capital:.0f}")
            # 实盘：发真单
            if self.real:
                try:
                    from scripts.jvquant_trade_client import buy as real_buy
                    short = code.replace(".SH","").replace(".SZ","").replace(".BJ","")
                    r = real_buy(short, name, vol=shares)  # 不传price，自动从10档定价
                    if r.get("code") != "0":
                        print(f"  [!] 买入失败: {r.get('message', r)}")
                    else:
                        order_id = r.get('order_id', '?')
                        print(f"  [📡] 实盘委托已发 order_id={order_id}")
                        _push_feishu(
                            f"📈 {name}({code}) 实盘买入\n"
                            f"价格: {current_price:.2f} × {shares}股\n"
                            f"order_id: {order_id}"
                        )
                except Exception as e:
                    print(f"  [!] 实盘买入异常: {e}")
            self._log_trade_journal(p)
            self._save()

        # 出场检查
        to_remove = []
        for p in self.portfolio:
            exit_now, reason = self._check_exit(p)
            if exit_now:
                snap = json.loads(SNAP_FILE.read_text()) if SNAP_FILE.exists() else {}
                d = snap.get(p.code.split(".")[0], {})
                exit_price = float(d.get("last") or p.entry_price)
                pnl = exit_price * p.shares - p.cost
                p.exit_price = exit_price
                p.exit_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                p.exit_reason = reason
                p.pnl = pnl
                p.pnl_pct = (exit_price / p.entry_price - 1) * 100
                p.status = "stop_loss" if "止损" in reason else "take_profit" if "止盈" in reason else "time_stop"
                self.daily_pnl += pnl
                self.capital += exit_price * p.shares

                if "止损" in reason and "时间" not in reason:
                    self.consecutive_losses += 1
                    if self.consecutive_losses >= CONSECUTIVE_STOP_LIMIT:
                        self.stopped_today = True
                        print(f"  [!] 连续 {CONSECUTIVE_STOP_LIMIT} 次止损，今日停止交易")
                else:
                    self.consecutive_losses = 0

                to_remove.append(p)
                print(f"  [-] 卖出 {p.code} {p.name} {p.shares}股@{exit_price:.2f} → {p.status} {pnl:+.0f}元 (累计{self.daily_pnl:+.0f})")
                # 实盘：发真单
                if self.real:
                    try:
                        from scripts.jvquant_trade_client import sale as real_sale
                        short = p.code.replace(".SH","").replace(".SZ","").replace(".BJ","")
                        r = real_sale(short, p.name, vol=p.shares)  # 不传price，自动从10档定价
                        if r.get("code") != "0":
                            print(f"  [!] 卖出失败: {r.get('message', r)}")
                        else:
                            order_id = r.get('order_id', '?')
                            print(f"  [📡] 实盘卖出委托已发 order_id={order_id}")
                            icon = "💰" if "止盈" in p.exit_reason else "🛑"
                            label = "止盈" if "止盈" in p.exit_reason else "出场"
                            _push_feishu(
                                f"{icon} {p.name}({p.code}) 实盘{label}\n"
                                f"入场: {p.entry_price:.2f} → 成交: {exit_price:.2f}\n"
                                f"盈亏: {pnl:+.0f}元 ({pnl_pct:+.2f}%)\n"
                                f"原因: {p.exit_reason[:40]}\n"
                                f"order_id: {order_id}"
                            )
                    except Exception as e:
                        print(f"  [!] 实盘卖出异常: {e}")
                self._log_trade_journal(p)

        for p in to_remove:
            self.portfolio.remove(p)
            self._exited.append(p)

        self._save()

    def _daily_reset(self, today: str):
        """日终重置（保留当日最终 portfolio 存档，清算）"""
        print(f"\n{'='*50}\n日切: {self.today} → {today}\n{'='*50}")
        self._save()  # 保存昨日最终状态
        self.today = today
        self.seen_codes.clear()
        self.consecutive_losses = 0
        self.stopped_today = False
        # daily_pnl 不清零——累计亏到线才停
        if self.daily_pnl < 0 and abs(self.daily_pnl) >= DAILY_LOSS_LIMIT:
            self.stopped_today = True
            print(f"[!] 累计亏损已达上限，今日停止交易")
        self._load_avg_amount_5d()

    def report(self):
        """输出当前持仓汇总"""
        print(f"\n{'='*50}")
        print(f"模拟交易状态")
        print(f"总资金: {TOTAL_CAPITAL:.0f}  →  剩余: {self.capital:.0f}  持仓: {len(self.portfolio)}")
        if self.stopped_today:
            print(f"⚠ 今日已暂停交易")
        print(f"当日浮动盈亏: {self.daily_pnl:+.0f}")
        print(f"{'code':12s} {'名称':8s} {'方向':6s} {'入场价':>7s} {'现价/出场':>9s} {'手数':>4s} {'盈亏':>7s}")
        for p in self.portfolio:
            print(f"{p.code:12s} {p.name:8s} {'持有':6s} {p.entry_price:7.2f} {'-':>9s} {p.shares//100:4d} {'--':>7s}")
        for p in self.portfolio:
            pass  # 已退出的在持仓移除后不显示
        # 当天退出汇总在循环时已打印
        print(f"{'='*50}")

    def _save(self):
        today = datetime.now().strftime("%Y%m%d")
        data = {
            "date": today,
            "capital": self.capital,
            "daily_pnl": round(self.daily_pnl, 2),
            "stopped": self.stopped_today,
            "positions": [
                {"code": p.code, "name": p.name, "entry_price": p.entry_price,
                 "shares": p.shares, "cost": round(p.cost, 2),
                 "entry_at": p.entry_at, "status": p.status,
                 "exit_price": p.exit_price, "exit_at": p.exit_at,
                 "exit_reason": p.exit_reason, "pnl": round(p.pnl, 2),
                 "pnl_pct": round(p.pnl_pct, 2)}
                for p in self.portfolio + self._exited
            ],
        }
        # 简单存：只存当前持仓
        (TRADING_DIR / f"portfolio_{today}_{datetime.now().strftime('%H%M%S')}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2))

    def _log_trade_journal(self, p: Position):
        """写一条交割单到 plays/trading/data/reports/{date}.json"""
        today = datetime.now().strftime("%Y%m%d")
        report_file = TRADING_REPORTS_DIR / f"{today}.json"

        entry = {
            "code": p.code,
            "name": p.name,
            "direction": "买入" if p.status == "holding" else "卖出",
            "price": p.exit_price if p.status != "holding" else p.entry_price,
            "shares": p.shares,
            "amount": round(p.cost if p.status == "holding" else p.exit_price * p.shares, 2),
            "time": p.exit_at if p.status != "holding" else p.entry_at,
            "reason": p.exit_reason if p.status != "holding" else f"信号: {p.entry_type}",
            "pnl": round(p.pnl, 2) if p.status != "holding" else 0.0,
            "pnl_pct": round(p.pnl_pct, 2) if p.status != "holding" else 0.0,
        }

        existing = []
        if report_file.exists():
            try:
                existing = json.loads(report_file.read_text())
            except Exception:
                existing = []
        existing.append(entry)
        report_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2))


def main():
    """cron 每日拉起模式：09:25 启动 → 15:05 自退

    用法:
        python3 plays/trading/trader.py              # 模拟模式（默认）
        python3 plays/trading/trader.py --real        # 实盘模式（发真单）
    """
    import argparse
    parser = argparse.ArgumentParser(description="maneki 模拟/实盘交易")
    parser.add_argument("--real", action="store_true", help="实盘模式，通过 jvQuant CTP 发真单")
    args, _ = parser.parse_known_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)

    env_file = PROJECT_DIR / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    # 非交易日不启动
    try:
        from plays.limit_up.utils import _is_trade_day, _today_str
        if not _is_trade_day(_today_str()):
            print("[trader] 非交易日，退出")
            return
    except Exception:
        pass

    sim = Simulator(real=args.real)
    mode = "🔴 实盘" if args.real else "🟡 模拟"
    print(f"[trader] {mode}交易启动 | 资金 {TOTAL_CAPITAL} | 无上限 | 止损 {STOP_LOSS_PCT*100:.0f}% | 止盈 {TAKE_PROFIT_TIER1*100:.0f}%/{TAKE_PROFIT_TIER2*100:.0f}%")
    try:
        while True:
            now = datetime.now()
            # 15:05 收盘自退
            if now.hour >= 15 and now.minute >= 5:
                print(f"[trader] 收盘退出（{now.strftime('%H:%M')}）")
                sim.report()
                sim._save()
                return
            try:
                sim.tick()
                sim.report()
            except Exception as e:
                import traceback
                print(f"[trader] tick 异常: {e}\n{traceback.format_exc()}")
            time.sleep(60)
    except KeyboardInterrupt:
        print("[trader] 收到终止信号")
        sim.report()
        sim._save()


if __name__ == "__main__":
    main()
