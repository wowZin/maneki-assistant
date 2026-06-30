
#!/usr/bin/env python3
"""
V1 vs V2 对比复盘 — 评分管线 vs 信号管线

对比维度:
  1. 命中率对比 (推送命中/总推送)
  2. V2 信号有效性 (每个信号的 recall/precision/F1)
  3. V2 组合规则效果矩阵
  4. 重叠分析 (V1和V2同时推送的股票)

用法:
  python plays/limit_up/review_v2.py [--date 20260615]
"""

import json
import sys
import requests
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_DIR))
PLAY_DIR = Path(__file__).resolve().parent
DATA_DIR = PLAY_DIR / "data"

from scripts.tu_share import CONFIG, call_tushare  # noqa: E402
from plays.limit_up.utils import safe_float  # noqa: E402

TUSHARE_TOKEN = CONFIG.get("TUSHARE_TOKEN", "")
FEISHU_APP_ID = CONFIG.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = CONFIG.get("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID_REPORT = CONFIG.get("FEISHU_CHAT_ID_REPORT", "")
FEISHU_TEST_MODE = CONFIG.get("FEISHU_TEST_MODE", "").lower() == "true"

from plays.limit_up.signals import SIGNAL_LABELS, SIGNAL_PRIORITY  # noqa: E402


def feishu_prefix():
    return "TEST-" if FEISHU_TEST_MODE else ""


# =========================================
# 1. 数据加载
# =========================================

def load_pushed_v1(today: str) -> list:
    """加载 V1 推送记录"""
    pushed_dir = DATA_DIR / "pushed"
    items = []
    if pushed_dir.exists():
        for pf in sorted(pushed_dir.glob("%s_*.json" % today)):
            # 跳过 v2 文件
            if pf.name.startswith("v2_"):
                continue
            try:
                data = json.loads(pf.read_text())
                if isinstance(data, list):
                    items.extend(data)
                else:
                    items.append(data)
            except Exception:
                pass
    return items


def load_pushed_v2(today: str) -> list:
    """加载 V2 推送记录"""
    pushed_dir = DATA_DIR / "pushed"
    items = []
    if pushed_dir.exists():
        for pf in sorted(pushed_dir.glob("v2_%s_*.json" % today)):
            try:
                data = json.loads(pf.read_text())
                if isinstance(data, list):
                    items.extend(data)
                else:
                    items.append(data)
            except Exception:
                pass
    return items


def load_analysis_v2(today: str) -> list:
    """加载 V2 分析记录（含全部候选股，不限于推送）"""
    analysis_dir = DATA_DIR / "analysis"
    stock_best = {}
    if analysis_dir.exists():
        for pf in sorted(analysis_dir.glob("v2_%s_*.json" % today)):
            try:
                data = json.loads(pf.read_text())
                if not isinstance(data, list):
                    continue
                for item in data:
                    code = item.get("code", "")
                    conf = item.get("push_decision", {}).get("confidence", 0)
                    if code not in stock_best or conf > stock_best[code].get("push_decision", {}).get("confidence", 0):
                        stock_best[code] = item
            except Exception:
                pass
    return list(stock_best.values())


def get_today_limit_ups(today: str) -> set:
    """获取当日涨停股集合（与 review.py 过滤一致）"""
    name_map = {}
    try:
        rows = _tushare_dicts("stock_basic", {"list_status": "L"}, "ts_code,name")
        for r in rows:
            name_map[r.get("ts_code", "")] = r.get("name", "")
    except Exception:
        pass

    limit_codes = set()
    # 优先用 daily 接口
    try:
        rows = _tushare_dicts("daily", {"trade_date": today}, "ts_code,pct_chg")
        for r in rows:
            code = r.get("ts_code", "")
            pct = safe_float(r.get("pct_chg"))
            if not code:
                continue
            if code.startswith(("300", "301", "688", "8", "4")):
                continue
            name = name_map.get(code, "")
            if "ST" in name or name.startswith("N"):
                continue
            if pct >= 9.9:
                limit_codes.add(code)
        if limit_codes:
            return limit_codes
    except Exception:
        pass

    # 降级 limit_list_d
    try:
        rows = _tushare_dicts("limit_list_d", {"trade_date": today, "limit_type": "U"},
                              "ts_code,name")
        for r in rows:
            code = r.get("ts_code", "")
            if not code:
                continue
            if code.startswith(("300", "301", "688", "8", "4")):
                continue
            name = r.get("name", "") or name_map.get(code, "")
            if "ST" in name or name.startswith("N"):
                continue
            limit_codes.add(code)
    except Exception:
        pass

    return limit_codes


def _tushare_dicts(api_name: str, params: dict, fields: str = "",
                   timeout: int = 20) -> list:
    try:
        resp = call_tushare(api_name, params, fields, timeout)
        data = resp.get("data", {})
        flds = data.get("fields", [])
        items = data.get("items", [])
        if not flds or not items:
            return []
        return [dict(zip(flds, item)) for item in items if item]
    except Exception as e:
        print("  Tushare %s: %s" % (api_name, e))
        return []


# =========================================
# 2. 计算指标
# =========================================

def compute_hit_rate(pushed: list, limit_ups: set) -> dict:
    """计算命中率"""
    pushed_codes = set()
    for item in pushed:
        code = item.get("code", "")
        if code:
            pushed_codes.add(code)

    hits = pushed_codes & limit_ups
    return {
        "pushed_count": len(pushed_codes),
        "limit_up_count": len(limit_ups),
        "hit_count": len(hits),
        "hit_rate": round(len(hits) / len(pushed_codes) * 100, 1) if pushed_codes else 0,
        "hit_codes": list(hits),
        "miss_codes": list(pushed_codes - limit_ups),
    }


def compute_signal_effectiveness(analysis_records: list, limit_ups: set) -> dict:
    """计算每个信号的 recall/precision/F1

    Recall: 涨停股中信号触发比例
    Precision: 触发信号的股票中涨停比例
    F1: 调和均值
    """
    limit_set = limit_ups  # full code with suffix
    effectiveness = {}

    for sname in SIGNAL_PRIORITY:
        triggered_total = 0
        triggered_hit = 0
        total_hit = 0

        for r in analysis_records:
            code = r.get("code", "")
            sig = r.get("signals", {}).get(sname, {})
            is_hit = code in limit_set

            if is_hit:
                total_hit += 1
                if sig.get("triggered"):
                    triggered_hit += 1

            if sig.get("triggered"):
                triggered_total += 1

        recall = triggered_hit / total_hit if total_hit > 0 else 0
        precision = triggered_hit / triggered_total if triggered_total > 0 else 0
        f1 = 2 * recall * precision / (recall + precision) if (recall + precision) > 0 else 0

        effectiveness[sname] = {
            "label": SIGNAL_LABELS.get(sname, sname),
            "recall": round(recall, 3),
            "precision": round(precision, 3),
            "f1": round(f1, 3),
            "triggered_total": triggered_total,
            "triggered_hit": triggered_hit,
            "total_hit": total_hit,
        }

    return effectiveness


def compute_combination_matrix(pushed_records: list, limit_ups: set) -> dict:
    """计算各组合规则的命中率"""
    matrix = defaultdict(lambda: {"matched": 0, "hit": 0})

    for r in pushed_records:
        pd = r.get("push_decision", {})
        combo = pd.get("combination", "未知")
        code = r.get("code", "")
        matrix[combo]["matched"] += 1
        if code in limit_ups:
            matrix[combo]["hit"] += 1

    result = {}
    for combo, stats in sorted(matrix.items()):
        result[combo] = {
            "matched": stats["matched"],
            "hit": stats["hit"],
            "hit_rate": round(stats["hit"] / stats["matched"] * 100, 1) if stats["matched"] > 0 else 0,
        }

    return result


# =========================================
# 3. 报告生成
# =========================================

def generate_comparison(v1_hit: dict, v2_hit: dict, signal_eff: dict,
                        combo_matrix: dict, overlap: dict) -> dict:
    """生成对比报告"""
    return {
        "date": datetime.now().strftime("%Y%m%d"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "v1": v1_hit,
        "v2": v2_hit,
        "comparison": {
            "winner": "V2" if v2_hit["hit_rate"] > v1_hit["hit_rate"] else (
                "V1" if v1_hit["hit_rate"] > v2_hit["hit_rate"] else "TIE"),
            "hit_rate_diff": round(v2_hit["hit_rate"] - v1_hit["hit_rate"], 1),
            "v1_unique": len(overlap.get("v1_only", [])),
            "v2_unique": len(overlap.get("v2_only", [])),
            "overlap": len(overlap.get("both", [])),
        },
        "signal_effectiveness": signal_eff,
        "combination_matrix": combo_matrix,
        "overlap": overlap,
    }


def generate_markdown(report: dict) -> str:
    """生成 Markdown 对比报告"""
    v1 = report["v1"]
    v2 = report["v2"]
    comp = report["comparison"]
    sig_eff = report.get("signal_effectiveness", {})
    combo_mat = report.get("combination_matrix", {})
    overlap = report.get("overlap", {})

    lines = [
        "# V1 vs V2 对比复盘",
        "",
        "> %s" % report["generated_at"],
        "",
        "## 核心指标对比",
        "",
        "| 指标 | V1 (评分) | V2 (信号) | 差异 |",
        "|------|-----------|-----------|------|",
        "| 推送数 | %d | %d | %+d |" % (v1["pushed_count"], v2["pushed_count"],
                                          v2["pushed_count"] - v1["pushed_count"]),
        "| 涨停总数 | %d | %d | - |" % (v1["limit_up_count"], v2["limit_up_count"]),
        "| 命中数 | %d | %d | %+d |" % (v1["hit_count"], v2["hit_count"],
                                          v2["hit_count"] - v1["hit_count"]),
        "| 命中率 | %.1f%% | %.1f%% | %+.1f%% |" % (v1["hit_rate"], v2["hit_rate"],
                                                       comp["hit_rate_diff"]),
        "",
        "**Winner: %s**" % comp["winner"],
        "",
        "## 重叠分析",
        "",
        "| 类型 | 数量 |",
        "|------|------|",
        "| V1+V2同时推送 | %d |" % len(overlap.get("both", [])),
        "| 仅V1推送 | %d |" % len(overlap.get("v1_only", [])),
        "| 仅V2推送 | %d |" % len(overlap.get("v2_only", [])),
        "",
    ]

    # V2 信号有效性
    if sig_eff:
        lines.extend([
            "## V2 信号有效性",
            "",
            "| 信号 | Recall | Precision | F1 | 触发总数 | 触发命中 |",
            "|------|--------|-----------|-----|----------|----------|",
        ])
        for sname in SIGNAL_PRIORITY:
            e = sig_eff.get(sname, {})
            if e:
                lines.append("| %s | %.1f%% | %.1f%% | %.2f | %d | %d |" % (
                    e["label"], e["recall"] * 100, e["precision"] * 100,
                    e["f1"], e["triggered_total"], e["triggered_hit"]))
        lines.append("")

    # 组合规则矩阵
    if combo_mat:
        lines.extend([
            "## V2 组合规则效果",
            "",
            "| 组合 | 匹配数 | 命中数 | 命中率 |",
            "|------|--------|--------|--------|",
        ])
        for combo, stats in sorted(combo_mat.items()):
            lines.append("| %s | %d | %d | %.1f%% |" % (
                combo, stats["matched"], stats["hit"], stats["hit_rate"]))
        lines.append("")

    lines.extend([
        "---",
        "*报告由 review_v2.py 自动生成*",
    ])

    return "\n".join(lines)


# =========================================
# 4. 飞书推送
# =========================================

def send_feishu_report(report: dict) -> bool:
    """发送对比报告到飞书"""
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_CHAT_ID_REPORT):
        print("飞书配置缺失，跳过推送")
        return False

    # 获取 token
    token_resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET}, timeout=10)
    token_data = token_resp.json()
    if token_data.get("code") != 0:
        print("飞书token获取失败: %s" % token_data)
        return False
    token = token_data["tenant_access_token"]

    v1 = report["v1"]
    v2 = report["v2"]
    comp = report["comparison"]

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": "%s V1 vs V2 Signal Compare" % feishu_prefix()},
            "template": "blue"
        },
        "elements": [
            {"tag": "div", "fields": [
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": "**V1(%s)**\nPush %d\nHit %d (%.1f%%)" % (
                     "Score", v1["pushed_count"], v1["hit_count"], v1["hit_rate"])}},
                {"is_short": True, "text": {"tag": "lark_md",
                 "content": "**V2(%s)**\nPush %d\nHit %d (%.1f%%)" % (
                     "Signal", v2["pushed_count"], v2["hit_count"], v2["hit_rate"])}},
            ]},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md",
             "content": "**Result: %s** (diff %+.1f%%)" % (
                 comp["winner"], comp["hit_rate_diff"])}},
        ]
    }

    msg_url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {"Authorization": "Bearer " + token}
    resp = requests.post(msg_url, headers=headers, json={
        "receive_id": FEISHU_CHAT_ID_REPORT,
        "msg_type": "interactive",
        "content": json.dumps(card)
    }, timeout=10)

    result = resp.json()
    if result.get("code") == 0:
        print("飞书推送成功: %s" % result.get("data", {}).get("message_id"))
        return True
    else:
        print("飞书推送失败: %s" % result)
        return False


# =========================================
# Main
# =========================================

def main(date: str = None):
    if date is None:
        date = datetime.now().strftime("%Y%m%d")

    print("=" * 60)
    print("V1 vs V2 对比复盘 — %s" % date)
    print("=" * 60)

    # 1. 加载推送数据
    print("\n[1/5] 加载推送数据...")
    v1_pushed = load_pushed_v1(date)
    v2_pushed = load_pushed_v2(date)
    v2_analysis = load_analysis_v2(date)
    print("  V1推送: %d 条, V2推送: %d 条, V2分析: %d 条" % (
        len(v1_pushed), len(v2_pushed), len(v2_analysis)))

    # 2. 获取实际涨停
    print("\n[2/5] 获取实际涨停...")
    limit_ups = get_today_limit_ups(date)
    print("  涨停: %d 只" % len(limit_ups))

    # 3. 计算命中率
    print("\n[3/5] 计算命中率...")
    v1_hit = compute_hit_rate(v1_pushed, limit_ups)
    v2_hit = compute_hit_rate(v2_pushed, limit_ups)
    print("  V1: %d/%d = %.1f%%" % (v1_hit["hit_count"], v1_hit["pushed_count"], v1_hit["hit_rate"]))
    print("  V2: %d/%d = %.1f%%" % (v2_hit["hit_count"], v2_hit["pushed_count"], v2_hit["hit_rate"]))

    # 4. 信号有效性
    print("\n[4/5] 计算信号有效性...")
    sig_eff = compute_signal_effectiveness(v2_analysis, limit_ups) if v2_analysis else {}
    combo_mat = compute_combination_matrix(v2_pushed, limit_ups) if v2_pushed else {}

    if sig_eff:
        print("  Signal effectiveness:")
        for sname in SIGNAL_PRIORITY:
            e = sig_eff.get(sname, {})
            if e:
                print("    %s: R=%.1f%% P=%.1f%% F1=%.2f" % (
                    e["label"], e["recall"] * 100, e["precision"] * 100, e["f1"]))

    # 5. 重叠分析
    v1_codes = set(item.get("code", "") for item in v1_pushed)
    v2_codes = set(item.get("code", "") for item in v2_pushed)
    overlap = {
        "both": list(v1_codes & v2_codes),
        "v1_only": list(v1_codes - v2_codes),
        "v2_only": list(v2_codes - v1_codes),
    }
    print("\n  重叠: %d, V1独有: %d, V2独有: %d" % (
        len(overlap["both"]), len(overlap["v1_only"]), len(overlap["v2_only"])))

    # 6. 生成报告
    print("\n[5/5] 生成报告...")
    report = generate_comparison(v1_hit, v2_hit, sig_eff, combo_mat, overlap)

    reports_dir = DATA_DIR / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    json_path = reports_dir / ("v1v2_%s.json" % date)
    with open(json_path, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("  JSON: %s" % json_path)

    md_path = reports_dir / ("v1v2_%s.md" % date)
    md_content = generate_markdown(report)
    with open(md_path, "w") as f:
        f.write(md_content)
    print("  MD: %s" % md_path)

    # 7. 飞书推送
    send_feishu_report(report)

    print("\n" + "=" * 60)
    print("Winner: %s (diff %+.1f%%)" % (report["comparison"]["winner"],
                                           report["comparison"]["hit_rate_diff"]))
    print("=" * 60)


if __name__ == "__main__":
    date = None
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            if arg.startswith("--date="):
                date = arg.split("=")[1]
    main(date)
