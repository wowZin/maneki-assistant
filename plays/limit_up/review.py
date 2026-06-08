#!/usr/bin/env python3
"""
涨停预测日报复盘脚本
流程：检查交易日 → 汇总当日信号/分析 → 对比涨停结果 → 生成报告 → 飞书推送

用法:
  python plays/limit_up/review.py
"""

import json
import sys
import requests
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# 项目根目录
PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
PLAY_DIR = Path(__file__).resolve().parent

from scripts.tu_share import CONFIG  # noqa: E402

TUSHARE_TOKEN = CONFIG.get("TUSHARE_TOKEN", "")
FEISHU_APP_ID = CONFIG.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = CONFIG.get("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID_REPORT = CONFIG.get("FEISHU_CHAT_ID_REPORT", "")
FEISHU_TEST_MODE = CONFIG.get("FEISHU_TEST_MODE", "").lower() == "true"

# ===== 权重AB对比配置 =====
# 当前权重（从.env读取，和pipeline.py一致）
CURRENT_WEIGHTS = {
    "fundamental": float(CONFIG.get("AGENT_WEIGHT_FUNDAMENTAL", "1.5")),
    "technical": float(CONFIG.get("AGENT_WEIGHT_TECHNICAL", "1.0")),
    "fundflow": float(CONFIG.get("AGENT_WEIGHT_FUND_FLOW", "0.5")),
    "sentiment": float(CONFIG.get("AGENT_WEIGHT_SENTIMENT", "1.2")),
    "shortterm": float(CONFIG.get("AGENT_WEIGHT_SHORTTERM", "1.5")),
}
# 旧权重（权重变更后填入上一版权重，相同时AB不生效）
PREV_WEIGHTS = {
    "fundamental": float(CONFIG.get("AGENT_WEIGHT_FUNDAMENTAL_PREV", "1.5")),
    "technical": float(CONFIG.get("AGENT_WEIGHT_TECHNICAL_PREV", "1.0")),
    "fundflow": float(CONFIG.get("AGENT_WEIGHT_FUND_FLOW_PREV", "0.5")),
    "sentiment": float(CONFIG.get("AGENT_WEIGHT_SENTIMENT_PREV", "1.2")),
    "shortterm": float(CONFIG.get("AGENT_WEIGHT_SHORTTERM_PREV", "1.5")),
}
# AB是否生效：只有当前权重≠旧权重时才对比
AB_ENABLED = CURRENT_WEIGHTS != PREV_WEIGHTS

def feishu_title_prefix():
    """测试模式下返回'测试-'前缀"""
    return "测试-" if FEISHU_TEST_MODE else ""

def safe_float(val) -> float:
    """安全转换为float，None/空/异常返回0.0"""
    if val is None or val == '' or val == 'None':
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0

# ===== 1. 检查交易日 =====
def is_trade_day(check_date: str) -> bool:
    """用Tushare trade_cal检查是否交易日"""
    url = "http://api.tushare.pro"
    payload = {
        "api_name": "trade_cal",
        "token": TUSHARE_TOKEN,
        "params": {
            "exchange": "SSE",
            "start_date": check_date,
            "end_date": check_date
        }
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0 and data.get("data"):
            fields = data["data"]["fields"]
            records = data["data"]["items"]
            if records:
                is_open_idx = fields.index("is_open")
                return str(records[0][is_open_idx]) == "1"
    except Exception as e:
        print(f"检查交易日失败: {e}")
    return False

# ===== 2. 汇总当日信号 =====
def load_today_signals(today: str) -> list:
    """读取data/signals下今天的所有信号文件"""
    signals_dir = PLAY_DIR / "data" / "signals"
    all_signals = []
    
    for f in signals_dir.glob(f"{today}*.json"):
        try:
            with open(f) as fp:
                raw = json.load(fp)
            if isinstance(raw, dict) and "stocks" in raw:
                all_signals.extend(raw["stocks"])
            elif isinstance(raw, list):
                all_signals.extend(raw)
        except Exception as e:
            print(f"读取信号文件失败 {f}: {e}")
    
    return all_signals

# ===== 3. 汇总当日分析结果 =====
def load_today_analysis(today: str) -> tuple:
    """
    读取data/analysis下今天的所有分析文件。
    返回 (all_analysis, pushed_analysis)：
      - all_analysis: 全部分析结果（每只股票取最佳分数版本，用于维度统计和置信度分布）
      - pushed_analysis: 今日实际推送给用户的股票（累加去重，用于命中率计算）
    推送定义：综合分>=35取前3只（按总分降序），无>=35时不推送。
    推送池构建方式：每个时段独立应用推送规则，然后跨时段累加去重。
    """
    analysis_dir = PLAY_DIR / "data" / "analysis"
    stock_best = {}  # 每只股票取最佳分数版本（用于全量统计）

    # 按时段读取分析文件
    for f in sorted(analysis_dir.glob(f"{today}*.json")):
        try:
            with open(f) as fp:
                data = json.load(fp)
            if not isinstance(data, list):
                continue
            # 汇入全量分析（取每只股票最佳分数版本）
            for item in data:
                code = item.get("code", "")
                total = item.get("total", 0) or 0
                if code not in stock_best or total > stock_best[code].get("total", 0):
                    stock_best[code] = item
            # 时段信号汇总（仅用于全量统计，非推送依据）
            pass
        except Exception as e:
            print(f"读取分析文件失败 {f}: {e}")

    all_items = list(stock_best.values())
    # 从data/pushed/目录读取实际推送记录（唯一权威来源）
    pushed_dir = PLAY_DIR / "data" / "pushed"
    pushed_items = []
    if pushed_dir.exists():
        for pf in pushed_dir.glob(f"{today}*.json"):
            try:
                with open(pf) as fp:
                    pushed_data = json.load(fp)
                if isinstance(pushed_data, list):
                    pushed_items.extend(pushed_data)
                else:
                    pushed_items.append(pushed_data)
            except Exception as e:
                print(f"读取推送记录失败 {pf}: {e}")
    if pushed_items:
        # 推送记录存在，取每只股票最佳分数版本去重
        pushed_best = {}
        for item in pushed_items:
            code = item.get("code", "")
            total = item.get("total", 0) or 0
            if code not in pushed_best or total > pushed_best[code].get("total", 0):
                pushed_best[code] = item
        pushed = list(pushed_best.values())
        print(f"从推送记录获取: {len(pushed)}只")
    else:
        pushed = []
        print(f"今日无推送记录（push_feishu 未触发），推送数据: 0只")

    # 标记推送状态
    pushed_codes_set = set(item.get("code", "") for item in pushed)
    for item in all_items:
        item["is_pushed"] = item.get("code", "") in pushed_codes_set

    print(f"全量分析: {len(all_items)}只, 推送数据: {len(pushed)}只")
    return all_items, pushed

# ===== 4. 获取当日日线收盘数据 =====
def get_daily_close_data(today: str) -> dict:
    """
    获取当日全部股票的收盘价和涨跌幅。
    返回 {ts_code: {"close": float, "pct_chg": float}}
    """
    url = "http://api.tushare.pro"
    payload = {
        "api_name": "daily",
        "token": TUSHARE_TOKEN,
        "params": {"trade_date": today},
        "fields": "ts_code,close,pct_chg"
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        data = resp.json()
        if data.get("code") == 0 and data.get("data"):
            fields = data["data"]["fields"]
            records = data["data"]["items"]
            ts_code_idx = fields.index("ts_code")
            close_idx = fields.index("close")
            pct_idx = fields.index("pct_chg")
            result = {}
            for r in records:
                code = r[ts_code_idx]
                result[code] = {
                    "close": safe_float(r[close_idx]),
                    "pct_chg": safe_float(r[pct_idx])
                }
            return result
    except Exception as e:
        print(f"获取日线数据失败: {e}")
    return {}

def calculate_win_rate(pushed_analysis: list, daily_close_data: dict) -> dict:
    """
    计算胜率：推送股票中，收盘涨幅 > 推送时涨幅的比例。
    推送时涨幅从 pushed JSON 的 pct_chg 字段读取（pipeline 扫描时记录）。
    """
    win_count = 0
    total = 0
    for item in pushed_analysis:
        code = item.get("code", "")
        scan_pct = item.get("pct_chg", 0)
        close_info = daily_close_data.get(code)
        if scan_pct != 0 and close_info:
            close_pct = close_info.get("pct_chg", 0)
            if close_pct > scan_pct:
                win_count += 1
            total += 1
    win_rate = win_count / total * 100 if total > 0 else 0
    return {"win_count": win_count, "total": total, "win_rate": win_rate}

# ===== 5. 获取当日涨停股票 =====
def get_today_limit_up(today: str) -> list:
    """
    获取当日涨停股票列表，过滤条件与pipeline一致：
    - 排除 ST/*ST
    - 排除 创业板(300xxx) / 科创板(688xxx) / 北交所(8xxx/4xxx)
    - 主板非ST涨停: pct >= 9.9%
    策略：优先用daily接口(实时可用)，降级用limit_list_d(T+1延迟)。
    """
    url = "http://api.tushare.pro"
    
    # 获取stock_basic用于ST判断
    name_map = {}
    try:
        payload_basic = {
            "api_name": "stock_basic",
            "token": TUSHARE_TOKEN,
            "params": {"list_status": "L"},
            "fields": "ts_code,name"
        }
        resp_basic = requests.post(url, json=payload_basic, timeout=10)
        data_basic = resp_basic.json()
        if data_basic.get("code") == 0 and data_basic.get("data"):
            for r in data_basic["data"]["items"]:
                name_map[r[0]] = r[1]
    except Exception as e:
        print(f"获取stock_basic失败: {e}")
    
    # 方案1：用daily接口筛选（实时数据，T日可用）
    try:
        payload = {
            "api_name": "daily",
            "token": TUSHARE_TOKEN,
            "params": {"trade_date": today},
            "fields": "ts_code,pct_chg"
        }
        resp = requests.post(url, json=payload, timeout=15)
        data = resp.json()
        if data.get("code") == 0 and data.get("data"):
            fields = data["data"]["fields"]
            records = data["data"]["items"]
            ts_code_idx = fields.index("ts_code")
            pct_chg_idx = fields.index("pct_chg")
            
            limit_codes = []
            for r in records:
                code = r[ts_code_idx]
                pct = safe_float(r[pct_chg_idx])
                if not code:
                    continue
                
                # 过滤1: 排除创业板/科创板/北交所（与pipeline规则3一致）
                if code.startswith('300') or code.startswith('688') or code.startswith('8') or code.startswith('4'):
                    continue
                
                # 过滤2: 排除ST/*ST/新股(与pipeline规则一致)
                name = name_map.get(code, '')
                if 'ST' in name or name.startswith('N'):
                    continue
                
                # 主板非ST涨停阈值9.9%
                if pct >= 9.9:
                    limit_codes.append(code)
            
            if limit_codes:
                print(f"daily接口获取主板涨停(非ST): {len(limit_codes)}只")
                return limit_codes
    except Exception as e:
        print(f"daily接口获取涨停失败: {e}")
    
    # 方案2：降级用limit_list_d（T+1数据，当天可能为空），同样做过滤
    try:
        payload = {
            "api_name": "limit_list_d",
            "token": TUSHARE_TOKEN,
            "params": {
                "trade_date": today,
                "limit_type": "U"
            }
        }
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if data.get("code") == 0 and data.get("data"):
            fields = data["data"]["fields"]
            records = data["data"]["items"]
            if records:
                ts_code_idx = fields.index("ts_code")
                result = []
                for r in records:
                    code = r[ts_code_idx]
                    if not code:
                        continue
                    # 同样过滤创业板/科创板/北交所
                    if code.startswith('300') or code.startswith('688') or code.startswith('8') or code.startswith('4'):
                        continue
                    # 同样过滤ST/新股
                    name = name_map.get(code, '')
                    if 'ST' in name or name.startswith('N'):
                        continue
                    result.append(code)
                if result:
                    print(f"limit_list_d获取主板涨停(非ST): {len(result)}只")
                    return result
    except Exception as e:
        print(f"limit_list_d获取涨停失败: {e}")
    
    return []

# ===== 5. 计算命中 =====
def calculate_hits(predicted_signals: list, actual_limit: list) -> dict:
    """
    计算预测命中率
    predicted_signals: 当日所有信号文件中的股票（去重）
    actual_limit: 当日大盘涨停列表
    """
    # 提取所有被推荐过的股票代码（去重）
    predicted_codes = set()
    for p in predicted_signals:
        code = p.get("ts_code") or p.get("code") or p.get("代码", "")
        if "." not in str(code):
            if str(code).startswith("6"):
                code = f"{code}.SH"
            else:
                code = f"{code}.SZ"
        predicted_codes.add(code)
    
    actual_codes = set(actual_limit)
    hits = predicted_codes & actual_codes
    
    return {
        "predicted_count": len(predicted_codes),
        "actual_limit_count": len(actual_codes),
        "hit_count": len(hits),
        "hit_rate": len(hits) / len(predicted_codes) * 100 if predicted_codes else 0,
        "hit_codes": list(hits),
        "miss_codes": list(predicted_codes - actual_codes)
    }

# ===== 6. 分析各维度表现 =====
def analyze_dimension_performance(analysis_results: list) -> dict:
    """统计各评分维度的表现"""
    dim_stats = defaultdict(lambda: {"total": 0, "hit_scores": [], "miss_scores": []})
    
    for r in analysis_results:
        r.get("ts_code", "")
        scores = r.get("scores", {})
        hit = r.get("hit", False)
        
        for dim, score in scores.items():
            dim_stats[dim]["total"] += 1
            if hit:
                dim_stats[dim]["hit_scores"].append(score)
            else:
                dim_stats[dim]["miss_scores"].append(score)
    
    # 计算平均分
    result = {}
    for dim, stats in dim_stats.items():
        hit_avg = sum(stats["hit_scores"]) / len(stats["hit_scores"]) if stats["hit_scores"] else 0
        miss_avg = sum(stats["miss_scores"]) / len(stats["miss_scores"]) if stats["miss_scores"] else 0
        result[dim] = {
            "total": stats["total"],
            "hit_avg": round(hit_avg, 1),
            "miss_avg": round(miss_avg, 1)
        }
    
    return result

# ===== 7. 置信度分布 =====
def confidence_distribution(analysis_results: list) -> dict:
    """
    统计置信度分布。
    当confidence字段不存在/None时，用total综合分代替：
      - high: total >= 50（高分区间）
      - medium: total >= 40（中等）
      - low: total < 40（低分）
    """
    dist = {"high": 0, "medium": 0, "low": 0}
    for r in analysis_results:
        conf = r.get("confidence")
        if conf is None or conf == '':
            # 用total综合分替代
            conf = r.get("total", 0) or 0

        conf = safe_float(conf)
        if conf >= 50:
            dist["high"] += 1
        elif conf >= 40:
            dist["medium"] += 1
        else:
            dist["low"] += 1
    return dist

# ===== 8. 权重AB对比 =====
def compute_ab_comparison(pushed_analysis: list, all_analysis: list, limit_ups: list) -> dict:
    """比较新旧两套权重在实际推送股票上的命中差异

    - 新权重：直接用实际推送记录（pushed_analysis），反映当日真实结果
    - 旧权重：用旧权重从全量分析中重建推送池（模拟如果旧权重仍在用会推哪些）
    """
    if not AB_ENABLED:
        return {"enabled": False, "note": "当前权重与旧权重相同，AB不生效"}

    def weighted_top3_total(scores, weights):
        dims = ['fundamental', 'technical', 'fundflow', 'sentiment', 'shortterm']
        contribs = [(scores.get(d, 0) or 0, weights.get(d, 1.0)) for d in dims]
        contribs.sort(key=lambda x: x[0] * x[1], reverse=True)
        top3 = contribs[:3]
        total_s = sum(s * w for s, w in top3)
        total_w = sum(w for _, w in top3)
        return total_s / total_w if total_w > 0 else 0

    THRESHOLD = 35
    actual_set = set(limit_ups)

    # 新权重：实际推送记录（真实结果）
    curr_codes = set(item.get("code", "") for item in pushed_analysis)
    curr_hits = curr_codes & actual_set

    # 旧权重：从全量分析重建（模拟）
    for item in all_analysis:
        s = item.get("scores", {})
        if s:
            item["_prev_total"] = weighted_top3_total(s, PREV_WEIGHTS)
    prev_candidates = [item for item in all_analysis if item.get("_prev_total", 0) >= THRESHOLD]
    prev_candidates.sort(key=lambda x: x.get("_prev_total", 0), reverse=True)
    prev_codes = set(p.get("code", "") for p in prev_candidates)
    prev_hits = prev_codes & actual_set

    return {
        "enabled": True,
        "prev": {
            "weights": PREV_WEIGHTS,
            "pushed": len(prev_codes),
            "codes": list(prev_codes),
            "hits": len(prev_hits),
        },
        "curr": {
            "weights": CURRENT_WEIGHTS,
            "pushed": len(curr_codes),
            "codes": list(curr_codes),
            "hits": len(curr_hits),
        },
    }

# ===== 9. 飞书推送 =====
def send_feishu_report(report: dict):
    """发送飞书卡片消息"""
    # 获取token
    token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    token_resp = requests.post(token_url, json={
        "app_id": FEISHU_APP_ID,
        "app_secret": FEISHU_APP_SECRET
    }, timeout=10)
    token_data = token_resp.json()
    if token_data.get("code") != 0:
        print(f"获取飞书token失败: {token_data}")
        return False
    token = token_data["tenant_access_token"]
    
    # 构建卡片
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{feishu_title_prefix()}📊 涨停预测日报复盘"},
            "template": "blue"
        },
        "elements": [
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**日期**\n{report['date']}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**推送信号**\n{report['pushed_count']}只"}}
                ]
            },
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**大盘涨停**\n{report['cumulative_limit_count']}只"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**命中数量**\n{report['hit_count']}只"}}
                ]
            },
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**命中率**\n{report['hit_rate']:.1f}%"}
            },
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**胜率**\n{report.get('win_rate', 0):.1f}% ({report.get('win_count', 0)}/{report.get('win_total', 0)})"}
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**命中涨停**\n" + "\n".join(f"{d['code']} {d['name']}" for d in report.get('hit_details', [])) if report.get('hit_details') else "**命中涨停**\n无"}
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**各维度平均分(命中/未命中)**"}
            },
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"基本面: {report['dim_performance'].get('fundamental', {}).get('hit_avg', 0)}/{report['dim_performance'].get('fundamental', {}).get('miss_avg', 0)}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"技术面: {report['dim_performance'].get('technical', {}).get('hit_avg', 0)}/{report['dim_performance'].get('technical', {}).get('miss_avg', 0)}"}}
                ]
            },
            {
                "tag": "div",
                "fields": [
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"资金面: {report['dim_performance'].get('fundflow', {}).get('hit_avg', 0)}/{report['dim_performance'].get('fundflow', {}).get('miss_avg', 0)}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"情绪面: {report['dim_performance'].get('sentiment', {}).get('hit_avg', 0)}/{report['dim_performance'].get('sentiment', {}).get('miss_avg', 0)}"}}
                ]
            },
            {"tag": "hr"},
            {"tag": "div",
                "text": {"tag": "lark_md", "content": f"**置信度分布**: 高(>=50): {report['confidence_dist']['high']} | 中(40-50): {report['confidence_dist']['medium']} | 低(<40): {report['confidence_dist']['low']}"}
            }
        ]
    }
    # AB段（仅权重变更时显示）
    ab = report.get('ab_comparison', {})
    if ab.get('enabled'):
        prev = ab['prev']
        curr = ab['curr']
        prev_rate = prev['hits'] / prev['pushed'] * 100 if prev['pushed'] else 0
        curr_rate = curr['hits'] / curr['pushed'] * 100 if curr['pushed'] else 0
        verdict = '新权重更优' if curr_rate > prev_rate else ('旧权重更优' if prev_rate > curr_rate else '持平')
        card["elements"].extend([
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**权重AB对比(加权Top3择优)\n旧: 推送{prev['pushed']}只 命中{prev['hits']}只 ({prev_rate:.0f}%)\n新: 推送{curr['pushed']}只 命中{curr['hits']}只 ({curr_rate:.0f}%)\n结论: {verdict}"}}
        ])
    
    # 发送消息
    msg_url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.post(msg_url, headers=headers, json={
        "receive_id": FEISHU_CHAT_ID_REPORT,
        "msg_type": "interactive",
        "content": json.dumps(card)
    }, timeout=10)
    
    result = resp.json()
    if result.get("code") == 0:
        print(f"飞书推送成功: {result.get('data', {}).get('message_id')}")
        return True
    else:
        print(f"飞书推送失败: {result}")
        return False

# ===== 9. 生成Markdown报告 =====
def generate_markdown_report(report: dict) -> str:
    """生成人类可读的Markdown格式复盘报告"""
    date = report['date']
    pushed = report['pushed_count']
    cumulative = report['cumulative_limit_count']
    hit = report['hit_count']
    rate = report['hit_rate']
    hit_codes = report.get('hit_codes', [])
    miss_codes = report.get('miss_codes', [])
    hit_details = report.get('hit_details', [])
    dim_perf = report.get('dim_performance', {})
    conf_dist = report.get('confidence_dist', {})

    lines = []
    lines.append(f"# 涨停预测复盘报告 — {date}")
    lines.append("")
    lines.append("## 核心指标")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|------|------|")
    lines.append(f"| 今日推送股票 | **{pushed}** 只 |")
    lines.append(f"| 大盘涨停 | **{cumulative}** 只 |")
    lines.append(f"| 命中涨停 | **{hit}** 只 |")
    lines.append(f"| 命中率 | **{rate:.1f}%** |")
    lines.append(f"| 胜率 | **{report.get('win_rate', 0):.1f}%** ({report.get('win_count', 0)}/{report.get('win_total', 0)}) |")
    lines.append("")
    
    # 命中详情
    if hit_codes:
        lines.append("## ✅ 命中股票")
        lines.append("")
        for code in hit_codes:
            lines.append(f"- {code}")
        lines.append("")
    else:
        lines.append("## ✅ 命中股票")
        lines.append("")
        lines.append("_无_")
        lines.append("")
    
    # 未命中详情（最多显示30只，避免过长）
    if miss_codes:
        lines.append(f"## ❌ 未命中股票（共 {len(miss_codes)} 只，显示前30）")
        lines.append("")
        for code in miss_codes[:30]:
            lines.append(f"- {code}")
        if len(miss_codes) > 30:
            lines.append(f"- ... 等共 {len(miss_codes)} 只")
        lines.append("")

    # 命中涨停详情
    hit_details = report.get('hit_details', [])
    if hit_details:
        lines.append("## 🏆 命中涨停股票")
        lines.append("")
        lines.append("| 代码 | 名称 |")
        lines.append("|------|------|")
        for d in hit_details:
            lines.append(f"| {d['code']} | {d['name']} |")
        lines.append("")

    # 各维度表现
    lines.append("## 📊 各维度评分表现")
    lines.append("")
    lines.append("基于今日推送股票，统计各维度在命中/未命中上的平均分：")
    lines.append("")
    lines.append("| 维度 | 分析次数 | 命中均分 | 未命中均分 | 差异 |")
    lines.append("|------|----------|----------|------------|------|")
    
    dim_names = {
        'fundamental': '基本面',
        'technical': '技术面',
        'fundflow': '资金面',
        'sentiment': '情绪面'
    }
    
    for dim_key, dim_label in dim_names.items():
        stats = dim_perf.get(dim_key, {})
        total = stats.get('total', 0)
        hit_avg = stats.get('hit_avg', 0)
        miss_avg = stats.get('miss_avg', 0)
        diff = hit_avg - miss_avg if hit_avg and miss_avg else 0
        diff_str = f"{diff:+.1f}" if diff else "—"
        lines.append(f"| {dim_label} | {total} | {hit_avg} | {miss_avg} | {diff_str} |")
    
    lines.append("")
    
    # 置信度分布
    lines.append("## 🎯 置信度分布")
    lines.append("")
    lines.append("| 等级 | 阈值 | 数量 |")
    lines.append("|------|------|------|")
    lines.append(f"| 高置信度 | ≥50 | {conf_dist.get('high', 0)} |")
    lines.append(f"| 中置信度 | 40~50 | {conf_dist.get('medium', 0)} |")
    lines.append(f"| 低置信度 | <40 | {conf_dist.get('low', 0)} |")
    lines.append("")
    
    # 总结
    lines.append("## 📝 总结")
    lines.append("")
    if rate >= 20:
        lines.append(f"今日命中率 **{rate:.1f}%**，表现优秀。命中 {hit} 只涨停股，推荐池覆盖度良好。")
    elif rate >= 10:
        lines.append(f"今日命中率 **{rate:.1f}%**，表现尚可。命中 {hit} 只涨停股，仍有提升空间。")
    elif rate > 0:
        lines.append(f"今日命中率 **{rate:.1f}%**，表现一般。仅命中 {hit} 只涨停股，建议关注各维度评分阈值。")
    else:
        lines.append(f"今日命中率 **0%**，未命中任何涨停股。推送 {pushed} 只，大盘涨停 {cumulative} 只。")
        if cumulative == 0:
            lines.append("注：当日大盘涨停数为0，可能为市场整体低迷或数据获取异常。")
    lines.append("")
    
    lines.append("---")
    lines.append(f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    
    return "\n".join(lines)


# ===== 主流程 =====
def main():
    today = datetime.now().strftime("%Y%m%d")
    print(f"=== 涨停预测日报复盘 {today} ===")
    
    # Step 1: 检查交易日
    if not is_trade_day(today):
        print("今天不是交易日，跳过复盘")
        return
    
    # Step 2: 汇总信号
    signals = load_today_signals(today)
    print(f"今日预测信号: {len(signals)}只")
    
    # Step 3: 汇总分析结果
    all_analysis, pushed_analysis = load_today_analysis(today)
    print(f"今日全量分析结果: {len(all_analysis)}条, 推送数据: {len(pushed_analysis)}条")
    
    # Step 4: 获取涨停列表 & 日线收盘数据
    limit_ups = get_today_limit_up(today)
    print(f"今日大盘涨停: {len(limit_ups)}只")

    daily_close_data = get_daily_close_data(today)

    # 构建信号涨幅映射（用于胜率计算）
    # 信号→扫描涨幅映射（deprecated: 改用推送记录的pct_chg字段，保留兼容）
    
    # Step 5: 计算命中（以推送数据为预测池）
    pushed_codes = set()
    for item in pushed_analysis:
        code = item.get("code", "")
        if code:
            pushed_codes.add(code)

    # 如果推送数据为空，fallback到信号文件
    if not pushed_codes:
        for p in signals:
            code = p.get("ts_code") or p.get("code") or p.get("代码", "")
            if "." not in str(code):
                if str(code).startswith("6"):
                    code = f"{code}.SH"
                else:
                    code = f"{code}.SZ"
            pushed_codes.add(code)

    actual_codes = set(limit_ups)
    hits_set = pushed_codes & actual_codes

    hits = {
        "pushed_count": len(pushed_codes),
        "cumulative_limit_count": len(actual_codes),
        "hit_count": len(hits_set),
        "hit_rate": len(hits_set) / len(pushed_codes) * 100 if pushed_codes else 0,
        "hit_codes": list(hits_set),
        "miss_codes": list(pushed_codes - actual_codes),
        "hit_details": [{"code": code, "name": next((r.get("name","") for r in pushed_analysis if r.get("code")==code), "")} for code in hits_set]
    }
    print(f"命中情况: 推送{hits['pushed_count']}只中命中{hits['hit_count']}只 ({hits['hit_rate']:.1f}%)")
    
    # 标记推送分析结果中的命中
    hit_codes = set(hits["hit_codes"])
    for r in pushed_analysis:
        code = r.get("code", "") or r.get("ts_code", "")
        r["hit"] = code in hit_codes

    # Step 6: 计算胜率（基于推送股票）
    win_rate_info = calculate_win_rate(pushed_analysis, daily_close_data)
    print(f"胜率: {win_rate_info['win_count']}/{win_rate_info['total']} = {win_rate_info['win_rate']:.1f}%")

    # Step 7: 各维度表现（基于推送股票，而非全量分析）
    dim_perf = analyze_dimension_performance(pushed_analysis)

    # Step 8: 置信度分布（基于推送股票，而非全量分析）
    conf_dist = confidence_distribution(pushed_analysis)

    # Step 9: 权重AB对比（全量分析 vs 涨停名单）
    ab_result = compute_ab_comparison(pushed_analysis, all_analysis, limit_ups)
    if ab_result.get("enabled"):
        prev_hit_rate = ab_result["prev"]["hits"] / ab_result["prev"]["pushed"] * 100 if ab_result["prev"]["pushed"] else 0
        curr_hit_rate = ab_result["curr"]["hits"] / ab_result["curr"]["pushed"] * 100 if ab_result["curr"]["pushed"] else 0
        print(f"权重AB: 旧={prev_hit_rate:.0f}% 新={curr_hit_rate:.0f}%")
    else:
        print(f"权重AB: {ab_result.get('note', '未启用')}")
    
    # Step 10: 生成报告
    report = {
        "date": today,
        "pushed_count": hits["pushed_count"],
        "cumulative_limit_count": hits["cumulative_limit_count"],
        "hit_count": hits["hit_count"],
        "hit_rate": hits["hit_rate"],
        "win_rate": win_rate_info["win_rate"],
        "win_count": win_rate_info["win_count"],
        "win_total": win_rate_info["total"],
        "hit_codes": hits["hit_codes"],
        "miss_codes": hits.get("miss_codes", []),
        "hit_details": hits.get("hit_details", []),
        "dim_performance": dim_perf,
        "confidence_dist": conf_dist,
        "ab_comparison": ab_result,  # 权重AB
    }
    
    # 保存JSON报告（保留用于程序读取）
    report_file_json = PLAY_DIR / "data" / "reports" / f"{today}.json"
    report_file_json.parent.mkdir(parents=True, exist_ok=True)
    with open(report_file_json, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"JSON报告已保存: {report_file_json}")
    
    # 保存Markdown报告（人类可读）
    md_content = generate_markdown_report(report)
    report_file_md = PLAY_DIR / "data" / "reports" / f"{today}.md"
    with open(report_file_md, "w") as f:
        f.write(md_content)
    print(f"Markdown报告已保存: {report_file_md}")
    
    # Step 10: 飞书推送
    if FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_CHAT_ID_REPORT:
        send_feishu_report(report)
    else:
        print("飞书配置缺失，跳过推送")

if __name__ == "__main__":
    main()
