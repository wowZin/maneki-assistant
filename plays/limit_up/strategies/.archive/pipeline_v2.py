
#!/usr/bin/env python3
"""
涨停预测 V2 — 信号模式匹配管线

与 V1 的核心差异:
  - 不再计算五维度评分和加权总分
  - 改为检测 7 个离散信号的触发状态
  - 基于信号组合规则判断推送
  - 结果保存到 data/analysis/v2_*.json

用法:
  python plays/limit_up/pipeline_v2.py                  # 完整流程
  python plays/limit_up/pipeline_v2.py --from-file=data/signals/xxx.json
  python plays/limit_up/pipeline_v2.py --top 30 --no-l2
"""

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# 项目根目录
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"
sys.path.insert(0, str(PROJECT_DIR))

from scripts.tu_share import CONFIG, clear_tushare_cache, call_tushare, build_concept_map  # noqa: E402
from plays.limit_up.utils import (is_trading_time, is_market_closed, safe_float,  # noqa: E402
                                  batch_get_pct_tushare, batch_get_daily_basic_tushare,
                                  batch_get_fundflow_tushare)
from plays.limit_up.filter import filter_candidates  # noqa: E402
from plays.limit_up.signals import (check_all_signals, signal_combination_judge,  # noqa: E402
                                    triggered_signals, signals_summary,
                                    SIGNAL_LABELS, SIGNAL_ICONS, SIGNAL_PRIORITY)

# ===== 从 V1 复用核心函数 =====
# 复用 V1 的扫描、预排、同花顺缓存、飞书推送等基础设施
from plays.limit_up.pipeline import (  # noqa: E402
    scan_surge, load_from_file, _pre_rank,
    _batch_fetch_ths_for_candidates, _fetch_ths_hot_list,
    _write_empty_result, _get_feishu_token,
    _THS_QUOTE_CACHE, _HOT_CONCEPT_CACHE, _HOT_LIST_ITEMS,
    _POPULARITY_RANK_CACHE, _get_l2_net_flow,
)

FEISHU_TEST_MODE = CONFIG.get("FEISHU_TEST_MODE", "").lower() == "true"


def feishu_title_prefix():
    return "TEST-" if FEISHU_TEST_MODE else ""


# =========================================
# SignalContext 构建
# =========================================

def _build_concept_limit_counts(hot_list_items: list) -> dict:
    """从同花顺热门榜计算各概念的涨停股数。

    复用 sentiment.py 的逻辑：遍历热门榜股票的概念标签，
    统计每个概念下涨幅 >= 9.5% 的股票数量。
    """
    from collections import defaultdict
    concept_counts = defaultdict(int)
    for item in hot_list_items:
        pct = safe_float(item.get("pct_chg", 0))
        if pct < 9.5:
            continue
        tags = item.get("tag", {}).get("concept_tag", [])
        for tag in tags:
            concept_counts[tag] += 1
    return dict(concept_counts)


def _fetch_limit_history_batch(codes: list) -> dict:
    """批量获取涨停历史（近20日和近60日最高连板）。

    Returns: {code_short: {count_20d: int, max_cons_60d: int}}
    """
    from datetime import timedelta
    today = datetime.now()
    d20 = (today - timedelta(days=30)).strftime("%Y%m%d")  # 多几天余量
    d60 = (today - timedelta(days=70)).strftime("%Y%m%d")

    result = {c.split(".")[0]: {"count_20d": 0, "max_cons_60d": 0} for c in codes}

    # 查询近 60 天涨停记录
    try:
        rows = _tushare_dicts("limit_list_d", {
            "start_date": d60, "end_date": today.strftime("%Y%m%d"),
            "limit_type": "U",
        }, "ts_code,trade_date,limit_times")
    except Exception:
        return result

    today_str = today.strftime("%Y%m%d")
    d20_str = (today - timedelta(days=20)).strftime("%Y%m%d")

    for r in rows:
        code_full = r.get("ts_code", "")
        code = code_full.split(".")[0]
        if code not in result:
            continue
        trade_date = str(r.get("trade_date", ""))
        limit_times = safe_float(r.get("limit_times", 0))

        # 近20日
        if d20_str <= trade_date <= today_str:
            result[code]["count_20d"] += 1
        # 近60日最高连板
        if limit_times > result[code]["max_cons_60d"]:
            result[code]["max_cons_60d"] = int(limit_times)

    return result


def _fetch_auction_batch(codes: list) -> dict:
    """批量获取集合竞价数据。

    返回: {code_short: {vol, price, amount, pre_close}}
    盘中可能为空（stk_auction 是 T+1 更新）。
    """
    result = {}
    for code in codes:
        code_full = code
        if "." not in code_full:
            code_full = code + (".SZ" if code.startswith(("00", "30", "8", "4")) else ".SH")
        try:
            rows = _tushare_dicts("stk_auction", {"ts_code": code_full},
                                  "ts_code,vol,price,amount,pre_close")
            if rows:
                r = rows[0]
                code_short = code_full.split(".")[0]
                result[code_short] = {
                    "vol": safe_float(r.get("vol")),
                    "price": safe_float(r.get("price")),
                    "amount": safe_float(r.get("amount")),
                    "pre_close": safe_float(r.get("pre_close")),
                }
        except Exception:
            pass
    return result


def _tushare_dicts(api_name: str, params: dict, fields: str = "",
                   timeout: int = 30) -> list:
    """调用 Tushare 并返回 list[dict]"""
    try:
        resp = call_tushare(api_name, params, fields, timeout)
        data = resp.get("data", {})
        flds = data.get("fields", [])
        items = data.get("items", [])
        if not flds or not items:
            return []
        return [dict(zip(flds, item)) for item in items if item]
    except Exception as e:
        print("  Tushare %s 失败: %s" % (api_name, e))
        return []


def build_signal_context_batch(candidates: list, l2_available: bool = False) -> dict:
    """为每只候选股构建 SignalContext。

    数据来源（并行拉取）：
    1. 同花顺实时行情（已在 _THS_QUOTE_CACHE 中）
    2. L2 大单净流向（如果可用）
    3. 概念标签 + 概念涨停统计
    4. daily_basic 基础数据
    5. 竞价数据
    6. 涨停历史
    """
    codes = [c["code"] for c in candidates]
    code_shorts = [c["code"].split(".")[0] for c in candidates]
    n = len(codes)

    print("  [信号上下文] 构建 %d 只候选股数据..." % n)

    # 1. 同花顺行情已在 _THS_QUOTE_CACHE 中
    ths_quotes = {}
    for c in candidates:
        short = c["code"].split(".")[0]
        ths_quotes[short] = _THS_QUOTE_CACHE.get(short, {})

    # 2. 概念标签 + 概念涨停统计
    _fetch_ths_hot_list()
    concept_limit_counts = _build_concept_limit_counts(_HOT_LIST_ITEMS)
    hot_concept_tags = {}
    for c in candidates:
        short = c["code"].split(".")[0]
        hot_concept_tags[short] = _HOT_CONCEPT_CACHE.get(short, [])

    # 3. 基础数据（盘后用 Tushare，盘中用 THS 缓存）
    daily_basic = {}
    fundflow = {}
    daily_pct = {}  # {code_short: pct_chg}
    if is_market_closed():
        daily_basic = batch_get_daily_basic_tushare()
        fundflow = batch_get_fundflow_tushare()
        daily_pct = batch_get_pct_tushare()
    else:
        # 盘中从 THS 缓存提取
        for short, q in _THS_QUOTE_CACHE.items():
            if q:
                daily_basic[short] = {
                    "circ_mv": safe_float(q.get("circ_mv", 0)),
                    "turnover_rate": safe_float(q.get("turnover", 0)),
                    "volume_ratio": safe_float(q.get("vol_ratio", 0)),
                }
                daily_pct[short] = safe_float(q.get("pct_chg", 0))

    # 4. 竞价数据 + 涨停历史
    print("    拉取竞价+涨停历史...", end="", flush=True)
    auction_data = _fetch_auction_batch(codes) if codes else {}
    limit_history = _fetch_limit_history_batch(codes) if codes else {}
    print(" 竞价%d 历史%d" % (len(auction_data), len(limit_history)))

    # 5. 组装上下文
    now = datetime.now()
    is_morning = now.hour < 10 or (now.hour == 10 and now.minute <= 30)
    is_afternoon = now.hour >= 13

    contexts = {}
    for c in candidates:
        code = c["code"]
        short = code.split(".")[0]
        quote = ths_quotes.get(short, {})
        basic = dict(daily_basic.get(short, {}))
        fund = fundflow.get(short, {})

        # 合并资金流
        if fund:
            basic["net_mf_amount"] = fund.get("net_flow", 0)

        # 合并涨幅：优先 THS 实时 > Tushare daily > 扫描涨幅
        pct_from_quote = safe_float(quote.get("pct_chg", 0))
        pct_from_tushare = safe_float(daily_pct.get(short, 0))
        pct_from_scan = safe_float(c.get("pct_chg", 0))
        merged_pct = pct_from_quote or pct_from_tushare or pct_from_scan
        basic["pct_chg"] = merged_pct

        # volume_ratio 为 None 时用 turnover_rate 估算（换手率>10%视为放量）
        if basic.get("volume_ratio", 0) <= 0:
            turnover = basic.get("turnover_rate", 0) or 0
            if turnover > 15:
                basic["volume_ratio"] = 3.0
            elif turnover > 10:
                basic["volume_ratio"] = 2.0
            elif turnover > 5:
                basic["volume_ratio"] = 1.5
            else:
                basic["volume_ratio"] = turnover / 5.0  # 粗略估算

        # L2 数据
        l2_net = None
        if l2_available:
            l2_net = _get_l2_net_flow(short)

        # 昨日成交量（从同花顺或 daily_basic 推算）
        prev_vol = 0
        if quote.get("amount") and quote.get("turnover"):
            # amount / turnover% = approximate total float market value
            # 不需要精确昨日量，后续可以通过 daily 获取
            pass

        ctx = {
            "ths_quote": quote,
            "l2_net_flow": l2_net,
            "l2_available": l2_available and l2_net is not None,
            "hot_concept_tags": hot_concept_tags.get(short, []),
            "concept_limit_counts": concept_limit_counts,
            "hot_list_items": _HOT_LIST_ITEMS,
            "basic_info": basic,
            "auction_data": auction_data.get(short),
            "prev_day_vol": prev_vol,
            "limit_history_20d": limit_history.get(short, {}).get("count_20d", 0),
            "limit_history_60d_max": limit_history.get(short, {}).get("max_cons_60d", 0),
            "is_morning": is_morning,
            "is_afternoon": is_afternoon,
            "scan_pct": safe_float(c.get("pct_chg", 0)),
        }
        contexts[code] = ctx

    print("    上下文构建完成: %d 只, 概念标签覆盖 %d 只" % (
        len(contexts),
        sum(1 for ctx in contexts.values() if ctx["hot_concept_tags"])))

    return contexts


# =========================================
# 结果构建 & 推送
# =========================================

def _build_v2_result(code: str, name: str, pct_chg: float,
                     signals: dict, should_push: bool,
                     combination: str, push_conf: float) -> dict:
    """构建 V2 格式结果"""
    triggered = triggered_signals(signals)
    return {
        "code": code, "name": name, "pct_chg": pct_chg,
        "signals": {k: dict(v) for k, v in signals.items()},
        "triggered_count": len(triggered),
        "triggered_signals": triggered,
        "push_decision": {
            "should_push": should_push,
            "combination": combination,
            "confidence": round(push_conf, 3),
        },
        "signal_summary": signals_summary(signals),
    }


def push_feishu_v2(results: list):
    """发送飞书 V2 格式卡片

    V2 卡片格式：
    - 标题标注 [信号版]
    - 展示触发信号图标而非数字分数
    - 展示组合规则名称
    """
    token = _get_feishu_token()
    if not token:
        print("飞书token获取失败")
        return False

    # 筛选推送
    push_list = [r for r in results if r.get("push_decision", {}).get("should_push")]
    if not push_list:
        print("  无可推送股票")
        return False

    # 按确信度排序
    push_list.sort(key=lambda x: x.get("push_decision", {}).get("confidence", 0), reverse=True)
    push_list = push_list[:5]

    # 去重
    pushed_codes_today = set()
    pushed_dir = DATA_DIR / "pushed"
    today_prefix = datetime.now().strftime("%Y%m%d")
    if pushed_dir.exists():
        for pf in pushed_dir.glob("v2_%s_*.json" % today_prefix):
            try:
                for item in json.loads(pf.read_text()):
                    if isinstance(item, dict) and "code" in item:
                        pushed_codes_today.add(item["code"])
            except Exception:
                pass

    push_list = [r for r in push_list if r["code"] not in pushed_codes_today]
    if not push_list:
        print("  全部已推送")
        return False

    # 保存推送记录
    pushed_dir = DATA_DIR / "pushed"
    pushed_dir.mkdir(parents=True, exist_ok=True)
    pushed_file = pushed_dir / ("v2_%s.json" % datetime.now().strftime("%Y%m%d_%H%M"))
    with open(pushed_file, "w") as f:
        json.dump(push_list, f, ensure_ascii=False, indent=2)
    print("  推送记录已保存: %s" % pushed_file)

    # 构建飞书卡片
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": "%sSIGNAL Top%d (%s)" % (
                          feishu_title_prefix(), len(push_list),
                          datetime.now().strftime("%H:%M"))},
            "template": "blue"
        },
        "elements": []
    }

    for r in push_list:
        sigs = r.get("signals", {})
        pd = r.get("push_decision", {})
        pct = r.get("pct_chg", 0)

        # 构建信号行
        signal_lines = []
        for sname in SIGNAL_PRIORITY:
            s = sigs.get(sname, {})
            if s.get("triggered"):
                label = SIGNAL_LABELS.get(sname, sname)
                signal_lines.append("%s %s" % (SIGNAL_ICONS.get(sname, ""), label))

        combo_name = pd.get("combination", "?")
        combo_conf = pd.get("confidence", 0)

        content = "**%s %s**  %.1f%%\n" % (r["code"], r["name"], pct)
        content += "SIG %s\n" % " ".join(signal_lines)
        content += "RULE %s (%.0f%%)" % (combo_name, combo_conf * 100)

        card["elements"].append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": content}
        })

    # 发送
    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": "Bearer " + token},
        json={
            "receive_id": CONFIG["FEISHU_CHAT_ID_SIGNAL"],
            "msg_type": "interactive",
            "content": json.dumps(card)
        }
    )
    result = resp.json()
    if result.get("code") == 0:
        print("  飞书推送 V2 成功: %s" % result.get("data", {}).get("message_id"))
        return True
    else:
        print("  飞书推送 V2 失败: %s" % result)
        return False


# =========================================
# 主流程
# =========================================

def main():
    import argparse
    parser = argparse.ArgumentParser(description="涨停预测 V2 (信号版)")
    parser.add_argument("--from-file", help="从已有信号文件加载", default=None)
    parser.add_argument("--top", type=int, default=50, help="分析前N只股票（默认50）")
    parser.add_argument("--no-l2", action="store_true", help="跳过L2初始化")
    args = parser.parse_args()

    # 进程锁
    lock_file = DATA_DIR / "pipeline_v2.lock"
    if lock_file.exists():
        try:
            old_pid = int(lock_file.read_text().strip())
            os.kill(old_pid, 0)
            print("跳过: 另一个 pipeline_v2 实例正在运行 (PID=%d)" % old_pid)
            return
        except (OSError, ValueError):
            lock_file.unlink(missing_ok=True)
    lock_file.write_text(str(os.getpid()))

    def _release_lock():
        try:
            lock_file.unlink(missing_ok=True)
        except Exception:
            pass

    try:
        _run_pipeline(args)
    finally:
        _release_lock()


def _run_pipeline(args):
    clear_tushare_cache()

    # 预检
    from scripts.health_check import preflight_check
    if not preflight_check():
        print("[预检] 关键数据源异常，阻塞执行")
        _write_empty_result("预检阻断: 关键数据源不可用")
        return

    print("=" * 50)
    print("SIGNAL V2 %s" % datetime.now())
    print("=" * 50)

    # 1. 获取候选股
    if args.from_file:
        candidates = load_from_file(args.from_file)
    else:
        print("\n[1/6] 异动扫描...")
        candidates = scan_surge()

    if not candidates:
        print("无候选股，退出")
        _write_empty_result("扫描无候选股")
        return

    # 2. 过滤
    print("\n[2/6] 全系统过滤...")
    candidates = filter_candidates(candidates)
    if not candidates:
        print("过滤后无候选股，退出")
        _write_empty_result("过滤后无候选股")
        return

    # 3. L2 检查
    l2_available = False
    if not args.no_l2:
        from scripts.jvquant_ws_client import daemon_alive
        l2_available = daemon_alive()
        if l2_available:
            print("  [L2] 守护进程已就绪")
        else:
            print("  [L2] 守护进程未运行")
    else:
        print("  [L2] --no-l2 模式")

    # 4. 热门榜 + 概念映射
    print("\n[3/6] 热门榜+概念映射...")
    _fetch_ths_hot_list()
    build_concept_map(_HOT_CONCEPT_CACHE)

    # 5. 预排 + 同花顺行情预取
    print("\n[4/6] 预排 + 行情预取...")
    candidates = _pre_rank(candidates, top_n=args.top)
    _batch_fetch_ths_for_candidates(candidates)

    # 6. 构建信号上下文
    print("\n[5/6] 构建信号上下文...")
    contexts = build_signal_context_batch(candidates, l2_available)

    # 7. 并行信号检测
    print("\n[6/6] 并行信号检测...")
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {}
        for stock in candidates:
            code = stock["code"]
            ctx = contexts.get(code, {})
            if ctx:
                futures[pool.submit(check_all_signals, code, ctx)] = stock

        for future in as_completed(futures):
            stock = futures[future]
            code = stock["code"]
            name = stock["name"]
            ctx = contexts.get(code, {})
            # 用 context 中合并后的真实涨幅（THS实时 > Tushare daily > 原始扫描）
            pct = safe_float(ctx.get("basic_info", {}).get("pct_chg", 0)
                             or ctx.get("scan_pct", 0)
                             or stock.get("pct_chg", 0))
            try:
                signals = future.result()
                should_push, combo, conf = signal_combination_judge(signals)
                r = _build_v2_result(code, name, pct, signals, should_push, combo, conf)
                results.append(r)

                triggered = triggered_signals(signals)
                push_mark = "PUSH" if should_push else "SKIP"
                print("  %s %s %s [%s] %d sigs: %s" % (
                    push_mark, code, name, combo,
                    len(triggered), signals_summary(signals)))
            except Exception as e:
                print("  ERR %s %s: %s" % (code, name, e))

    # 8. 排序 + 保存
    results.sort(key=lambda x: (x.get("push_decision", {}).get("should_push", False),
                                x.get("push_decision", {}).get("confidence", 0)),
                 reverse=True)

    pushed_count = sum(1 for r in results if r.get("push_decision", {}).get("should_push"))
    total_triggered = sum(1 for r in results if r["triggered_count"] > 0)

    print("\n[SIGNAL 结果]")
    print("  总候选: %d, 触发信号: %d, 推送: %d" % (len(results), total_triggered, pushed_count))
    for i, r in enumerate(results[:10]):
        push_mark = "PUSH" if r.get("push_decision", {}).get("should_push") else "SKIP"
        print("  %d. %s %s %s - %d sigs [%s] conf=%.2f" % (
            i + 1, push_mark, r["code"], r["name"],
            r["triggered_count"], r.get("push_decision", {}).get("combination", "?"),
            r.get("push_decision", {}).get("confidence", 0)))

    # 保存结果
    output_dir = DATA_DIR / "analysis"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / ("v2_%s.json" % datetime.now().strftime("%Y%m%d_%H%M"))
    with open(output_file, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\nResult saved: %s" % output_file)

    # 9. 飞书推送
    push_feishu_v2(results)

    print("\n" + "=" * 50)
    print("SIGNAL V2 Done!")
    print("=" * 50)


if __name__ == "__main__":
    main()
