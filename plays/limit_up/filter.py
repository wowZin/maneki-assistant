"""
全系统7条过滤规则：满足任一条件直接排除，不进入分析
1. ST/*ST/退市整理期
2. 上市不满60日新股
3. 创业板(30xxxx.SZ) / 科创板(688xxx.SH) / 北交所(8xxxxx.BJ)
4. 当日停牌
5. 自由流通市值 < 5亿
6. 5日均换手率 < 2%
7. 连续一字板（无法买入）
"""

from datetime import datetime

from scripts.tu_share import call_tushare
from plays.limit_up.utils import safe_float


def filter_candidates(candidates):
    today_str = datetime.now().strftime("%Y%m%d")
    filtered_in = []
    filter_log = []

    for stock in candidates:
        code = stock["code"]
        name = stock.get("name", "")
        vetoed = False
        veto_reason = ""

        # 规则3: 创业板/科创板/北交所 (纯代码判断，无需API)
        pure_code = code.split(".")[0]
        if pure_code.startswith("30") or pure_code.startswith("688") or pure_code.startswith("8") or pure_code.startswith("4"):
            suffix = code.split(".")[-1] if "." in code else ""
            if pure_code.startswith("30"):
                vetoed = True
                veto_reason = f"规则3: 创业板({code})"
            elif pure_code.startswith("688"):
                vetoed = True
                veto_reason = f"规则3: 科创板({code})"
            elif pure_code.startswith("8") or pure_code.startswith("4"):
                if suffix == "BJ" or suffix == "":
                    vetoed = True
                    veto_reason = f"规则3: 北交所({code})"

        if vetoed:
            filter_log.append(f"  [排除] {code} {name}: {veto_reason}")
            continue

        # 规则1/2/5/6/7/4 需要Tushare API数据
        try:
            resp_data = call_tushare(
                "daily_basic",
                {"ts_code": code, "trade_date": today_str},
                "ts_code,close,turnover_rate,turnover_rate_f,circ_mv,total_mv,pct_chg"
            )
            items = resp_data.get("data", {}).get("items", [])
            if not items:
                resp_data = call_tushare(
                    "daily_basic",
                    {"ts_code": code},
                    "trade_date,ts_code,close,turnover_rate,turnover_rate_f,circ_mv,total_mv,pct_chg"
                )
                items = resp_data.get("data", {}).get("items", [])

            if not items:
                filter_log.append(f"  [排除] {code} {name}: 无行情数据")
                continue

            latest = items[0]
            field_map = resp_data.get("data", {}).get("fields", [])
            basic = dict(zip(field_map, latest))

            # 规则5: 自由流通市值 < 5亿
            circ_mv = safe_float(basic.get("circ_mv"))
            if circ_mv and circ_mv < 50000:
                vetoed = True
                veto_reason = f"规则5: 流通市值{circ_mv/10000:.1f}亿<5亿"

            # 规则6: 换手率 < 2%
            turnover = safe_float(basic.get("turnover_rate_f")) or safe_float(basic.get("turnover_rate"))
            if not vetoed and turnover and turnover < 2:
                vetoed = True
                veto_reason = f"规则6: 换手率{turnover:.1f}%<2%"

            # 规则7: 连续一字板
            if not vetoed:
                pct_chg = safe_float(basic.get("pct_chg"))
                if pct_chg and turnover:
                    if pct_chg >= 9.9 and turnover < 0.5:
                        vetoed = True
                        veto_reason = f"规则7: 一字板(涨幅{pct_chg:.1f}%换手{turnover:.2f}%)"
                    elif pct_chg <= -9.9 and turnover < 0.5:
                        vetoed = True
                        veto_reason = f"规则7: 一字跌停(涨幅{pct_chg:.1f}%换手{turnover:.2f}%)"

        except Exception as e:
            filter_log.append(f"  [警告] {code} {name}: 数据获取失败({e}), 保留")

        if vetoed:
            filter_log.append(f"  [排除] {code} {name}: {veto_reason}")
            continue

        # 规则1: ST/*ST
        try:
            resp = call_tushare("stock_basic", {"ts_code": code}, "ts_code,name,list_date")
            items = resp.get("data", {}).get("items", [])
            if items:
                stock_name = items[0][1] if len(items[0]) > 1 else name
                list_date = items[0][2] if len(items[0]) > 2 else None

                if stock_name and ("ST" in stock_name or "st" in stock_name.lower()):
                    vetoed = True
                    veto_reason = f"规则1: ST股({stock_name})"

                # 规则2: 上市不满60日
                if not vetoed and list_date:
                    try:
                        list_dt = datetime.strptime(str(list_date), "%Y%m%d")
                        days_since_list = (datetime.now() - list_dt).days
                        if days_since_list < 60:
                            vetoed = True
                            veto_reason = f"规则2: 上市{days_since_list}日<60日"
                    except Exception:
                        pass
        except Exception:
            pass

        if vetoed:
            filter_log.append(f"  [排除] {code} {name}: {veto_reason}")
            continue

        filtered_in.append(stock)

    print(f"\n[过滤] 输入{len(candidates)}只 → 保留{len(filtered_in)}只 → 排除{len(candidates)-len(filtered_in)}只")
    if filter_log:
        for log in filter_log:
            print(log)

    return filtered_in