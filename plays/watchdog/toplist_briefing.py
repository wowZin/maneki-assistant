#!/usr/bin/env python3
"""
龙虎榜优质股票简报（每日 9:00 定时执行）
=========================================

从 Tushare 获取上一交易日涨停股（龙虎榜），
用五维度评分 + XGBoost 模型 + 龙虎榜资金成分数据（Tushare top_inst / top_list）
分析后选出 top 5，推送飞书。

资金成分分析完全基于 Tushare 返回的实时数据，不硬编码游资名单。
"""

import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

load_dotenv(PROJECT_DIR / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("toplist_briefing")

# 推送目标
FEISHU_CHAT_ID = (
    os.getenv("FEISHU_CHAT_ID_SIGNAL") or os.getenv("FEISHU_BOT_CHAT_ID") or ""
)

# ── 仅保留两类结构性识别（不依赖具体游资品牌）──
# 拉萨系：东方财富在西藏的营业部，是结构性的散户聚集地，营业部名稳定含"拉萨"
_LHASA_KEYWORDS = ["拉萨", "香曲东路"]
# 机构专用席位：Tushare 直接标注的
_INST_KEYWORD = "机构专用"


# ── 1. 获取龙虎榜数据 ──

def get_limit_list() -> tuple[list[dict], str]:
    """从 Tushare 获取上一交易日涨停股列表。"""
    from scripts.tu_share import call_tushare

    trade_date = datetime.now().strftime("%Y%m%d")
    logger.info(f"获取龙虎榜数据: {trade_date}")
    resp = call_tushare(
        "limit_list_d",
        {"trade_date": trade_date, "limit_type": "U"},
        "ts_code,name,close,pct_chg,amount",
    )
    items = resp.get("data", {}).get("items", [])
    fields = resp.get("data", {}).get("fields", [])
    stocks = [dict(zip(fields, row)) for row in items]

    # 过滤涨停股
    stocks = [s for s in stocks if float(s.get("pct_chg", 0)) >= 9.5]
    stocks.sort(key=lambda s: float(s.get("amount", 0)), reverse=True)

    logger.info(f"龙虎榜涨停股 {len(stocks)} 只（涨幅>=9.5%）")
    return stocks, trade_date


# ── 2. 龙虎榜资金成分分析（纯数据驱动）──

def fetch_top_inst(trade_date: str) -> dict[str, list[dict]]:
    """获取龙虎榜营业部明细，按 ts_code 聚合。"""
    from scripts.tu_share import call_tushare

    resp = call_tushare(
        "top_inst", {"trade_date": trade_date},
        "ts_code,exalter,buy,buy_rate,sell,sell_rate,net_buy,side",
    )
    items = resp.get("data", {}).get("items", [])
    fields = resp.get("data", {}).get("fields", [])
    result: dict[str, list[dict]] = {}
    for row in items:
        d = dict(zip(fields, row))
        code = d["ts_code"]
        d["buy"] = float(d.get("buy") or 0)
        d["sell"] = float(d.get("sell") or 0)
        d["net_buy"] = float(d.get("net_buy") or 0)
        d["buy_rate"] = float(d.get("buy_rate") or 0)
        d["sell_rate"] = float(d.get("sell_rate") or 0)
        d["side"] = int(d.get("side") or 0)
        result.setdefault(code, []).append(d)
    logger.info(f"龙虎榜营业部明细: {len(items)} 条, {len(result)} 只股票")
    return result


def fetch_top_list_summary(trade_date: str) -> dict[str, dict]:
    """获取龙虎榜汇总，按 ts_code 聚合。"""
    from scripts.tu_share import call_tushare

    resp = call_tushare(
        "top_list", {"trade_date": trade_date},
        "ts_code,turnover_rate,amount,l_sell,l_buy,l_amount,net_amount,net_rate,reason",
    )
    items = resp.get("data", {}).get("items", [])
    fields = resp.get("data", {}).get("fields", [])
    result: dict[str, dict] = {}
    for row in items:
        d = dict(zip(fields, row))
        code = d["ts_code"]
        for k in ("net_amount", "net_rate", "turnover_rate", "l_buy", "l_sell", "l_amount"):
            d[k] = float(d.get(k) or 0) if d.get(k) is not None else 0.0
        result[code] = d
    logger.info(f"龙虎榜汇总: {len(result)} 只股票")
    return result


def analyze_capital(
    inst_records: list[dict],
    top_list_summary: dict | None = None,
) -> dict:
    """分析单只股票的资金成分。

    完全基于 Tushare 返回数据计算，不依赖硬编码游资名单。
    仅识别两类结构性特征：
    - 机构专用席位（Tushare 标注）
    - 拉萨系营业部（结构性散户集中地，营业部名含"拉萨"）

    返回:
    {
        capital_score, inst_count, inst_net_buy,
        top_buyer_concentration, lhasa_buy_ratio,
        is_institutional, net_rate, summary,
        top_buyers, top_sellers,  # 原始数据供展示
    }
    """
    if not inst_records:
        return {
            "capital_score": 0.0,
            "inst_count": 0,
            "inst_net_buy": 0.0,
            "top_buyer_concentration": 0.0,
            "lhasa_buy_ratio": 0.0,
            "is_institutional": False,
            "net_rate": 0.0,
            "summary": "无龙虎榜明细",
            "top_buyers": [],
            "top_sellers": [],
        }

    # 按方向分
    buyers = [r for r in inst_records if r["net_buy"] > 0 or r["side"] == 0]
    sellers = [r for r in inst_records if r["net_buy"] < 0 or r["side"] == 1]
    buyers.sort(key=lambda r: r["net_buy"], reverse=True)
    sellers.sort(key=lambda r: r["net_buy"])

    total_buy = sum(r["buy"] for r in buyers)
    total_sell = sum(r["sell"] for r in sellers)
    inst_net_buy = total_buy - total_sell
    inst_count = len(inst_records)

    # 机构专用席位
    is_institutional = any(_INST_KEYWORD in r["exalter"] for r in inst_records)

    # Top 买方/卖方（用于展示，按净买额合并同名营业部）
    buyer_agg: dict[str, float] = {}
    for r in buyers:
        if r["net_buy"] > 0:
            buyer_agg[r["exalter"]] = buyer_agg.get(r["exalter"], 0) + r["net_buy"]
    top_buyers = [
        {"name": name, "net": round(net, 2)}
        for name, net in sorted(buyer_agg.items(), key=lambda x: -x[1])[:5]
    ]
    seller_agg: dict[str, float] = {}
    for r in sellers:
        if r["net_buy"] < 0:
            seller_agg[r["exalter"]] = seller_agg.get(r["exalter"], 0) + abs(r["net_buy"])
    top_sellers = [
        {"name": name, "net": round(net, 2)}
        for name, net in sorted(seller_agg.items(), key=lambda x: -x[1])[:5]
    ]

    # 买方集中度
    top5_buy = buyers[:5]
    top5_buy_sum = sum(r["buy"] for r in top5_buy) if top5_buy else 1
    top3_buy_sum = sum(r["buy"] for r in buyers[:3]) if buyers else 0
    top_buyer_concentration = (top3_buy_sum / top5_buy_sum * 100) if top5_buy_sum > 0 else 0

    # 拉萨系占比（结构性散户指标）
    lhasa_buy = sum(r["buy"] for r in inst_records
                    if any(kw in r["exalter"] for kw in _LHASA_KEYWORDS))
    lhasa_buy_ratio = (lhasa_buy / total_buy * 100) if total_buy > 0 else 0

    # ── 评分（纯数据驱动）──
    score = 0.0
    reasons = []

    # 净买额
    if inst_net_buy > 50_000_000:
        score += 25
        reasons.append(f"净买{inst_net_buy/10000:.0f}万")
    elif inst_net_buy > 10_000_000:
        score += 15
        reasons.append(f"净买{inst_net_buy/10000:.0f}万")
    elif inst_net_buy > 0:
        score += 5
    else:
        score -= 10
        reasons.append(f"净卖{abs(inst_net_buy)/10000:.0f}万")

    # 机构加分
    if is_institutional:
        score += 15
        reasons.append("机构参与")

    # 买方集中度
    if top_buyer_concentration >= 70:
        score += 15
        reasons.append("买方集中")
    elif top_buyer_concentration >= 50:
        score += 8

    # 拉萨反指
    if lhasa_buy_ratio >= 30:
        score -= 20
        reasons.append(f"拉萨占{lhasa_buy_ratio:.0f}%")
    elif lhasa_buy_ratio >= 15:
        score -= 10
        reasons.append(f"拉萨{lhasa_buy_ratio:.0f}%")

    # 净买率（来自 top_list）
    net_rate = 0.0
    if top_list_summary:
        net_rate = top_list_summary.get("net_rate", 0.0)
        if net_rate >= 20:
            score += 10
        elif net_rate >= 10:
            score += 5
        elif net_rate <= -10:
            score -= 10

    capital_score = max(0.0, min(100.0, score))

    # 摘要
    parts = []
    if is_institutional:
        parts.append("机构参与")
    if inst_net_buy > 0:
        parts.append(f"净买{inst_net_buy/10000:.0f}万")
    else:
        parts.append(f"净卖{abs(inst_net_buy)/10000:.0f}万")
    if lhasa_buy_ratio >= 30:
        parts.append(f"拉萨集中⚠️{lhasa_buy_ratio:.0f}%")
    elif lhasa_buy_ratio >= 15:
        parts.append(f"拉萨{lhasa_buy_ratio:.0f}%")
    summary = " | ".join(parts) if parts else "资金分散"

    return {
        "capital_score": round(capital_score, 1),
        "inst_count": inst_count,
        "inst_net_buy": round(inst_net_buy, 2),
        "top_buyer_concentration": round(top_buyer_concentration, 1),
        "lhasa_buy_ratio": round(lhasa_buy_ratio, 1),
        "is_institutional": is_institutional,
        "net_rate": round(net_rate, 1) if net_rate else 0.0,
        "summary": summary,
        "top_buyers": top_buyers,
        "top_sellers": top_sellers,
    }


# ── 3. 五维度评分 + 模型分 ──

def score_stock(code: str) -> dict:
    """对单只股票执行五维度评分 + 模型总分。"""
    result = {"code": code}

    # 五维度评分
    try:
        from plays.limit_up.strategies.fundamental import score_fundamental
        from plays.limit_up.strategies.technical import score_technical
        from plays.limit_up.strategies.fundflow import score_fundflow
        from plays.limit_up.strategies.sentiment import score_sentiment
        from plays.limit_up.strategies.shortterm import score_shortterm

        funcs = {
            "fundamental": score_fundamental,
            "technical": score_technical,
            "fundflow": score_fundflow,
            "sentiment": score_sentiment,
            "shortterm": score_shortterm,
        }
        scores = {}
        reasons = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(fn, code): dim for dim, fn in funcs.items()}
            for f in as_completed(futs):
                dim = futs[f]
                try:
                    s, r = f.result(timeout=30)
                    scores[dim] = s
                    reasons[dim] = r
                except Exception as e:
                    scores[dim] = 0.0
                    reasons[dim] = f"异常:{e}"

        result["scores"] = scores
        result["reasons"] = reasons

    except Exception as e:
        logger.warning(f"{code} 评分失败: {e}")
        result["scores"] = {d: 0.0 for d in
                            ["fundamental", "technical", "fundflow", "sentiment", "shortterm"]}
        result["reasons"] = {}
        return result

    # XGBoost 模型分
    try:
        from plays.limit_up.pit_features import build_pit_features
        from plays.limit_up.factors.optimized.model_score import factor_model_score
        from scripts.tu_share import call_tushare

        daily_resp = call_tushare(
            "daily", {"ts_code": code, "limit": 120},
            "trade_date,open,high,low,close,pre_close,vol,amount,pct_chg",
        )
        daily_items = daily_resp.get("data", {}).get("items", [])
        daily_fields = daily_resp.get("data", {}).get("fields", [])
        daily_rows = [dict(zip(daily_fields, row)) for row in daily_items]
        daily_rows.sort(key=lambda x: x.get("trade_date", ""))

        basic_resp = call_tushare(
            "daily_basic", {"ts_code": code, "limit": 1},
            "ts_code,trade_date,pe,pb,circ_mv,turnover_rate,volume_ratio",
        )
        basic_items = basic_resp.get("data", {}).get("items", [])
        basic_fields = basic_resp.get("data", {}).get("fields", [])
        basic_by_date = {}
        if basic_items:
            row = dict(zip(basic_fields, basic_items[0]))
            basic_by_date[row.get("trade_date", "")] = row

        today_date = datetime.now().strftime("%Y%m%d")
        feats = build_pit_features(
            code=code,
            score_date=today_date,
            daily_rows=daily_rows,
            basic_by_date=basic_by_date,
        )
        feats["fundamental"] = scores.get("fundamental", 0)
        feats["technical"] = scores.get("technical", 0)
        feats["fundflow"] = scores.get("fundflow", 0)
        feats["sentiment"] = scores.get("sentiment", 0)
        feats["shortterm"] = scores.get("shortterm", 0)

        model_score = factor_model_score(feats)
        result["model_score"] = round(float(model_score), 2)

    except Exception as e:
        logger.warning(f"{code} 模型分失败: {e}")
        result["model_score"] = 0.0

    return result


def score_stocks_batch(stocks: list[dict], capital_analysis: dict[str, dict]) -> list[dict]:
    """批量评分所有股票，合并资金成分分析。"""
    results = []

    for s in stocks:
        code = s["ts_code"]
        r = score_stock(code)
        r["name"] = s["name"]
        r["close"] = float(s.get("close", 0))
        r["pct_chg"] = float(s.get("pct_chg", 0))
        r["amount"] = float(s.get("amount", 0))

        # 合并资金成分
        r["capital"] = capital_analysis.get(code, analyze_capital([]))

        # 综合分 = 模型分 + 资金成分修正
        capital_score = r["capital"].get("capital_score", 0.0)
        if capital_score >= 60:
            adjustment = 5
        elif capital_score >= 40:
            adjustment = 2
        elif capital_score <= 20:
            adjustment = -5
        elif capital_score <= 30:
            adjustment = -2
        else:
            adjustment = 0
        r["combined_score"] = round(r.get("model_score", 0) + adjustment, 1)
        r["adjustment"] = adjustment

        logger.info(
            f"{code} {r.get('name','')}: model={r['model_score']} "
            f"capital={capital_score} → combined={r['combined_score']}"
        )
        results.append(r)

    return results


# ── 4. 格式化 ──

def _star_label(score: float) -> str:
    if score >= 55:
        return "⭐⭐⭐⭐⭐"
    if score >= 45:
        return "⭐⭐⭐⭐"
    if score >= 35:
        return "⭐⭐⭐"
    return ""


def _fmt_money(val: float) -> str:
    """格式化金额：元 → 万/亿。"""
    if abs(val) >= 1e8:
        return f"{val/1e8:.1f}亿"
    if abs(val) >= 1e4:
        return f"{val/1e4:.0f}万"
    return f"{val:.0f}"


def format_message(results: list[dict], trade_date: str) -> str:
    """格式化为飞书消息。"""
    results.sort(key=lambda r: r.get("combined_score", 0), reverse=True)
    top5 = results[:5]

    lines = [
        f"📊 龙虎榜精筛 Top {len(top5)}",
        f"📅 {trade_date} | 含资金成分分析",
        "",
    ]

    labels = {
        "fundamental": "基本面",
        "technical": "技术面",
        "fundflow": "资金面",
        "sentiment": "情绪面",
        "shortterm": "短线博弈",
    }

    for i, r in enumerate(top5, 1):
        ms = r.get("model_score", 0)
        cs = r.get("combined_score", ms)
        star = _star_label(cs)
        amount_yi = r.get("amount", 0) / 1e8
        cap = r.get("capital", {})
        capital_score = cap.get("capital_score", 0)

        lines.append(f"──── {i}. {r.get('name','')}({r['code']}) ────")
        lines.append(f"综合 {star} ({cs}分) 模型{ms} 资金{capital_score:.0f}")
        lines.append(f"涨幅 {r.get('pct_chg',0):.1f}% | 成交额 {amount_yi:.1f}亿")

        # 资金成分摘要
        summary = cap.get("summary", "")
        if summary:
            lines.append(f"  💰 {summary}")

        # 买方营业部 Top 3（纯数据展示，不标游资品牌）
        top_buyers = cap.get("top_buyers", [])
        if top_buyers:
            for b in top_buyers[:3]:
                name = b["name"]
                net = _fmt_money(b["net"])
                if _INST_KEYWORD in name:
                    name = "🏦 机构专用"
                elif any(kw in name for kw in _LHASA_KEYWORDS):
                    name = name.replace("东方财富证券股份有限公司", "")
                    name = name.replace("证券营业部", "")
                    name = f"散户:{name}"
                elif "证券营业部" in name:
                    name = name.replace("证券营业部", "")
                lines.append(f"    ├ {name} +{net}")

        # 卖方 Top 1
        top_sellers = cap.get("top_sellers", [])
        if top_sellers:
            s = top_sellers[0]
            sname = s["name"].replace("证券营业部", "")
            net = _fmt_money(s["net"])
            lines.append(f"    └ 卖方: {sname} -{net}")

        # 拉萨警示
        lhasa = cap.get("lhasa_buy_ratio", 0)
        if lhasa >= 30:
            lines.append(f"  ⚠️ 拉萨占比 {lhasa:.0f}% — 抛压风险")
        elif lhasa >= 15:
            lines.append(f"  📊 拉萨占比 {lhasa:.0f}%")

        # 五维度评分精简行
        dim_parts = []
        scores = r.get("scores", {})
        for dim, label in labels.items():
            s = scores.get(dim, 0)
            icon = "🟢" if s >= 50 else "🟡" if s >= 30 else "🔴"
            dim_parts.append(f"{icon}{label}{s:.0f}")
        lines.append(f"  {' '.join(dim_parts)}")

        lines.append("")

    lines.append("🤖 数据源: Tushare limit_list_d / top_inst / top_list")
    lines.append("综合分 = 模型分 + 资金成分修正 | 拉萨占比高=抛压风险")
    return "\n".join(lines)


# ── 5. 飞书推送 ──

def push_feishu(text: str):
    """推送消息到飞书。"""
    if not FEISHU_CHAT_ID:
        logger.warning("未配置 FEISHU_CHAT_ID，跳过推送")
        print(text)
        return

    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        logger.warning("未配置飞书凭证，跳过推送")
        print(text)
        return

    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        token = resp.json().get("tenant_access_token", "")
        if not token:
            logger.error("飞书 token 获取失败")
            print(text)
            return

        send_resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "receive_id": FEISHU_CHAT_ID,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
            timeout=10,
        )
        result = send_resp.json()
        if result.get("code") == 0:
            logger.info("飞书推送成功")
        else:
            logger.error(f"飞书推送失败: {result}")
            print(text)
    except Exception as e:
        logger.error(f"飞书推送异常: {e}")
        print(text)


# ── 6. 主流程 ──

def main():
    logger.info("=" * 50)
    logger.info("龙虎榜简报开始（含资金成分分析）")
    logger.info("=" * 50)

    # 获取龙虎榜数据
    stocks, trade_date = get_limit_list()
    if not stocks:
        msg = f"📊 龙虎榜简报\n📅 {trade_date}\n\n昨日无涨停股或数据获取失败。"
        push_feishu(msg)
        logger.info("无数据，已推送提示")
        return

    # 获取资金成分数据
    logger.info("获取资金成分数据...")
    top_inst_data = fetch_top_inst(trade_date)
    top_list_summary = fetch_top_list_summary(trade_date)

    # 对所有股票进行资金成分分析
    capital_analysis: dict[str, dict] = {}
    for s in stocks:
        code = s["ts_code"]
        inst_records = top_inst_data.get(code, [])
        tl = top_list_summary.get(code)
        capital_analysis[code] = analyze_capital(inst_records, tl)

    # 批量评分 + 合并资金成分
    results = score_stocks_batch(stocks, capital_analysis)

    # 格式化推送
    msg = format_message(results, trade_date)
    push_feishu(msg)

    # 保存存档
    output_dir = PROJECT_DIR / "plays" / "watchdog" / "data"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"toplist_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    with open(output_file, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    logger.info(f"存档已保存: {output_file}")
    logger.info("龙虎榜简报完成")


if __name__ == "__main__":
    main()
