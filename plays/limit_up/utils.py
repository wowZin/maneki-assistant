"""limit_up 玩法工具函数：类型转换、交易时间、实时→Tushare 盘后降级

盘后降级原则：
- 仅在非交易时段（15:30~次日9:00 + 周末）自动切换数据源
- 盘中走同花顺实时行情，盘后降级Tushare，代理超时不会降级到 Tushare
- 降级逻辑全在此模块，不散落在策略文件中
"""

import logging
from datetime import datetime

log = logging.getLogger("limit_up_utils")

# ═══════════════════════════════════════════════════════════
# 交易时间判断
# ═══════════════════════════════════════════════════════════


def is_trading_time():
    """判断当前是否在A股交易时间(9:30~11:30, 13:00~15:00，工作日)"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return False
    if now.hour >= 15:
        return False
    if now.hour == 11 and now.minute >= 30:
        return False
    if now.hour == 12:
        return False
    return True


def is_market_closed():
    """盘后（非交易时段）"""
    return not is_trading_time()


# ═══════════════════════════════════════════════════════════
# 盘后降级 — Tushare 数据源
# ═══════════════════════════════════════════════════════════

def _call_tushare(api_name: str, params: dict, fields: str = ""):
    """调用 Tushare API，返回 list[dict] 或 []"""
    try:
        import tushare as ts
        from scripts.tu_share import CONFIG as _cfg
        ts.set_token(_cfg.get("TUSHARE_TOKEN", ""))
        pro = ts.pro_api()
        df = pro.query(api_name, **params, fields=fields)
        if df is None or df.empty:
            return []
        return df.to_dict("records")
    except Exception as e:
        log.warning("Tushare %s 失败: %s", api_name, e)
        return []


def _get_last_trade_date() -> str:
    """获取最近一个有日线数据的交易日（委托给 tu_share 统一处理）"""
    from scripts.tu_share import get_last_trade_date_with_data
    return get_last_trade_date_with_data()


def _ensure_suffix(code: str) -> str:
    """确保股票代码带后缀(.SZ/.SH)"""
    if "." in code:
        return code
    return code + (".SZ" if code.startswith(("00", "30", "8", "4")) else ".SH")


def batch_get_pct_tushare(trade_date: str = None) -> dict[str, float]:
    """盘后降级：从 Tushare daily 获取全市场涨幅
    返回: {code_short: pct_chg}
    """
    if trade_date is None:
        trade_date = _get_last_trade_date()

    rows = _call_tushare("daily", {
        "trade_date": trade_date,
    }, fields="ts_code,pct_chg")

    result = {}
    for r in rows:
        code = r.get("ts_code", "")
        pct = r.get("pct_chg")
        if code and pct is not None:
            code_short = code.split(".")[0]
            try:
                result[code_short] = float(pct)
            except (ValueError, TypeError):
                pass
    log.info("Tushare daily pct_chg: %d 条 (trade_date=%s)", len(result), trade_date)
    return result


def batch_get_fundflow_tushare(trade_date: str = None) -> dict[str, dict]:
    """盘后降级：从 Tushare moneyflow + daily_basic 获取资金流/量价数据
    返回: {code_short: {net_flow(元), vol_ratio, turnover(%), amount(元)}}
    与 _get_realtime_fund_cache() 格式一致
    """
    if trade_date is None:
        trade_date = _get_last_trade_date()

    # 主力资金流
    mf_rows = _call_tushare("moneyflow", {
        "trade_date": trade_date,
    }, fields="ts_code,net_mf_amount")

    fundflow = {}
    for r in mf_rows:
        code = r.get("ts_code", "")
        net = r.get("net_mf_amount")
        if code and net is not None:
            code_short = code.split(".")[0]
            try:
                # net_mf_amount 单位是万元，转成元保持与实时数据一致
                fundflow[code_short] = {"net_flow": float(net) * 10000}
            except (ValueError, TypeError):
                pass

    # 量比 + 换手率
    basic_rows = _call_tushare("daily_basic", {
        "trade_date": trade_date,
    }, fields="ts_code,volume_ratio,turnover_rate,amount")

    for r in basic_rows:
        code = r.get("ts_code", "")
        code_short = code.split(".")[0]
        if code_short not in fundflow:
            fundflow.setdefault(code_short, {"net_flow": 0})
        entry = fundflow[code_short]
        try:
            if r.get("volume_ratio") is not None:
                entry["vol_ratio"] = float(r["volume_ratio"])
            if r.get("turnover_rate") is not None:
                entry["turnover"] = float(r["turnover_rate"])
            if r.get("amount") is not None:
                entry["amount"] = float(r["amount"]) * 10000  # 万元→元
        except (ValueError, TypeError):
            pass

    log.info("Tushare fundflow: %d 条 (trade_date=%s)", len(fundflow), trade_date)
    return fundflow


def batch_get_daily_basic_tushare(trade_date: str = None) -> dict[str, dict]:
    """盘后降级：从 Tushare daily_basic 获取基础数据
    返回: {code_short: {circ_mv, total_mv, turnover_rate, volume_ratio}}
    """
    if trade_date is None:
        trade_date = _get_last_trade_date()

    rows = _call_tushare("daily_basic", {
        "trade_date": trade_date,
    }, fields="ts_code,turnover_rate,volume_ratio,circ_mv,total_mv")

    result = {}
    for r in rows:
        code = r.get("ts_code", "")
        if not code:
            continue
        code_short = code.split(".")[0]
        entry = {}
        try:
            if r.get("turnover_rate") is not None:
                entry["turnover_rate"] = float(r["turnover_rate"])
            if r.get("volume_ratio") is not None:
                entry["volume_ratio"] = float(r["volume_ratio"])
            if r.get("circ_mv") is not None:
                entry["circ_mv"] = normalize_circ_mv(r["circ_mv"], "万元", "元")
            if r.get("total_mv") is not None:
                entry["total_mv"] = normalize_circ_mv(r["total_mv"], "万元", "元")
        except (ValueError, TypeError):
            pass
        if entry:
            result[code_short] = entry

    log.info("Tushare daily_basic: %d 条 (trade_date=%s)", len(result), trade_date)
    return result


def get_stock_quote_tushare(code: str, trade_date: str = None) -> dict:
    """盘后降级：单只个股行情数据
    返回格式模拟 _get_jj_data_eastmoney():
      {change_pct, open_pct, amount, vol_ratio, turnover_rate,
       circ_mv, now_price, jj_active}
    """
    if trade_date is None:
        trade_date = _get_last_trade_date()

    code_ts = _ensure_suffix(code)
    code_short = code.split(".")[0]

    result = {
        "change_pct": 0, "open_pct": 0, "amount": 0,
        "vol_ratio": 0, "turnover_rate": 0,
        "circ_mv": 0, "now_price": 0, "jj_active": False,
    }

    # daily 表：涨跌幅、开盘价、成交额
    rows = _call_tushare("daily", {
        "ts_code": code_ts,
        "trade_date": trade_date,
        "start_date": trade_date,
        "end_date": trade_date,
    }, fields="ts_code,open,close,pre_close,pct_chg,amount")
    for r in rows:
        try:
            pct = float(r.get("pct_chg", 0))
            pre_close = float(r.get("pre_close", 1))
            open_p = float(r.get("open", 0))
            close = float(r.get("close", 0))
            amount = float(r.get("amount", 0)) * 10000  # 万元→元
            result["change_pct"] = pct
            result["open_pct"] = (open_p / pre_close - 1) * 100 if pre_close > 0 else 0
            result["amount"] = amount
            result["now_price"] = close
        except (ValueError, TypeError):
            pass

    # daily_basic 表：换手率、量比、流通市值
    rows2 = _call_tushare("daily_basic", {
        "ts_code": code_ts,
        "trade_date": trade_date,
    }, fields="ts_code,turnover_rate,volume_ratio,circ_mv")
    for r in rows2:
        try:
            if r.get("turnover_rate") is not None:
                result["turnover_rate"] = float(r["turnover_rate"])
            if r.get("volume_ratio") is not None:
                result["vol_ratio"] = float(r["volume_ratio"])
            if r.get("circ_mv") is not None:
                result["circ_mv"] = normalize_circ_mv(r["circ_mv"], "万元", "元")
        except (ValueError, TypeError):
            pass

    return result


def get_popularity_rank_tushare(trade_date: str = None) -> dict[str, int]:
    """盘后降级：按主力净流入排序作为人气排名
    返回: {code_short: rank_index(0-based)}
    """
    if trade_date is None:
        trade_date = _get_last_trade_date()

    rows = _call_tushare("moneyflow", {
        "trade_date": trade_date,
    }, fields="ts_code,net_mf_amount")

    entries = []
    for r in rows:
        code = r.get("ts_code", "")
        net = r.get("net_mf_amount")
        if code and net is not None:
            try:
                entries.append((code.split(".")[0], float(net)))
            except (ValueError, TypeError):
                pass

    # 按净流入降序
    entries.sort(key=lambda x: x[1], reverse=True)

    result = {code: i for i, (code, _) in enumerate(entries)}
    log.info("Tushare popularity rank: %d 条", len(result))
    return result


def get_stock_pct(code: str) -> float | None:
    """获取个股当日涨幅（%）
    盘后从 Tushare 获取，盘中返回 None（走盘中的实时缓存）
    """
    if is_market_closed():
        q = get_stock_quote_tushare(code)
        return q.get("change_pct")
    return None


def get_stock_quote(code: str) -> dict:
    """获取个股行情数据（同 get_stock_quote_tushare 签名）
    盘后走 Tushare，盘中返回空字典（走实时数据）
    """
    if is_market_closed():
        return get_stock_quote_tushare(code)
    return {}


# ═══════════════════════════════════════════════════════════
# 类型转换（保留原接口）
# ═══════════════════════════════════════════════════════════

def safe_float(val):
    """安全转换为float，失败返回0.0"""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def safe_float_none(val):
    """安全转换为float，失败返回None"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def safe_int_none(val):
    """安全转换为int，失败返回None"""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def list_to_dict(items, fields):
    """将Tushare返回的list格式转为dict格式"""
    if not items or not fields:
        return []
    result = []
    for item in items:
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, (list, tuple)):
            d = {}
            for i, f in enumerate(fields):
                if i < len(item):
                    d[f] = item[i]
            result.append(d)
    return result


# ═══════════════════════════════════════════════════════════
# 单位统一与数据审计
# ═══════════════════════════════════════════════════════════

# circ_mv 在面板、特征、模型输入中统一为"万元"（Tushare daily_basic 原生单位）。
# 需要以"元"做比较时，应显式调用 normalize_circ_mv(..., "万元", "元")。
CIRC_MV_PANEL_UNIT = "万元"


def normalize_circ_mv(value, source_unit: str = "万元", target_unit: str = "万元") -> float:
    """把 circ_mv 从 source_unit 换算到 target_unit，统一单位。

    支持的单位: 元、万元、亿元。
    """
    v = safe_float(value)
    if v == 0.0:
        return 0.0
    units = {"元": 1, "万元": 10_000, "亿元": 100_000_000}
    src = units.get(source_unit)
    dst = units.get(target_unit)
    if src is None or dst is None:
        raise ValueError(f"unsupported circ_mv unit: {source_unit} -> {target_unit}")
    return v * src / dst


def log_data_audit(message: str):
    """记录数据层异常到 plays/limit_up/data/logs/data_audit.log（按日追加）。"""
    from pathlib import Path

    log_dir = Path(__file__).resolve().parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "data_audit.log"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{now}] {message}\n")
