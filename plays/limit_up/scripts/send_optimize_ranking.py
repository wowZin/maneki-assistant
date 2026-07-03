#!/usr/bin/env python3
"""Send ranking optimization results to Feishu report group."""
import json
import os
import time
import urllib.request
import urllib.error

ENV_PATH = "/root/maneki-agent/.env"
JSON_PATH = "/root/maneki-agent/plays/limit_up/data/weights/ranking_optimized.json"


def read_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def http_post(url, data, headers=None):
    req = urllib.request.Request(url, data=data.encode("utf-8"), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}: {body}")
        return json.loads(body) if body else {}
    except Exception as e:
        print(f"Request failed: {e}")
        return {}


def get_tenant_token(app_id, app_secret):
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret})
    resp = http_post(url, data, {"Content-Type": "application/json; charset=utf-8"})
    token = resp.get("tenant_access_token", "")
    if not token:
        raise RuntimeError(f"Failed to get token: {resp}")
    return token


def build_card(data):
    base = data["baseline"]
    rec = data["recommended"]
    dc = data["dim_contribution"]
    tc = data["threshold_curve"]
    date_range = data["data_range"]
    limit_cnt = data["total_limit_ups"]

    w = rec["weights"]
    base_w = base["weights"]
    delta_composite = round((rec["composite"] - base["composite"]) * 10000)

    # Weight comparison
    weight_lines = (
        f"当前权重 (基准): 基本面={base_w['fundamental']} 技术面={base_w['technical']} 资金面={base_w['fundflow']} 情绪面={base_w['sentiment']} 短线博弈={base_w['shortterm']}\n"
        f"推荐权重 (Top1): 基本面={w['fundamental']} 技术面={w['technical']} 资金面={w['fundflow']} 情绪面={w['sentiment']} 短线博弈={w['shortterm']}"
    )

    # Comparison
    delta_sign = "+" if delta_composite >= 0 else ""
    comparison = (
        f"基准: 综合分 {base['composite']:.4f}, 涨停均排 {base['avg_rank']:.0f}, "
        f"Top20覆盖率 {base['top20_rate']*100:.1f}%, AUC {base['auc']:.3f}, 分差 {base['sep']:.1f}\n"
        f"📦 信号池: {base['pushed_count']}只(含涨停{base['push_limit_count']}只, 覆盖率{base['push_limit_count']/limit_cnt*100:.0f}%)\n\n"
        f"推荐: 综合分 {rec['composite']:.4f} ({delta_sign}{delta_composite:.0f}e-4), "
        f"涨停均排 {rec['avg_rank']:.0f}, Top20覆盖率 {rec['top20_rate']*100:.1f}%, "
        f"AUC {rec['auc']:.3f}, 分差 {rec['sep']:.1f}\n"
        f"📦 信号池: {rec['pushed_count']}只(含涨停{rec['push_limit_count']}只, 覆盖率{rec['push_limit_count']/limit_cnt*100:.0f}%)"
    )

    # Dim contribution
    dim_text = (
        f"基本面: {dc['fundamental']['count']}次 ({dc['fundamental']['rate']:.0f}%)\n"
        f"技术面: {dc['technical']['count']}次 ({dc['technical']['rate']:.0f}%)\n"
        f"资金面: {dc['fundflow']['count']}次 ({dc['fundflow']['rate']:.0f}%) ⚠️ <30% 需修复\n"
        f"情绪面: {dc['sentiment']['count']}次 ({dc['sentiment']['rate']:.0f}%)\n"
        f"短线博弈: {dc['shortterm']['count']}次 ({dc['shortterm']['rate']:.0f}%)"
    )

    # Threshold curve (summary row)
    best_row = tc[5] if len(tc) > 5 else tc[-1]  # threshold=35
    curve_text = ""
    for row in tc[2:8]:  # threshold 20~45
        bar_len = max(1, int(row["coverage_rate"] / 2))
        bar = "█" * bar_len
        curve_text += f"≥{row['threshold']:2d}: {row['pushed']:3d}只 涨停{row['limit_above']:2d}只 覆盖率{row['coverage_rate']:5.1f}% 命中率{row['hit_rate']:5.1f}%\n"

    # Build card content
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"⚙️ 权重优化报告 {date_range}"},
            "template": "indigo"
        },
        "elements": [
            # Section 1: metadata
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**数据范围**: {date_range} ({data['total_records']}条记录, {limit_cnt}只涨停)\n**优化模式**: 加权Top3择优, 搜索2005种权重组合"
                }
            },
            {"tag": "hr"},
            # Section 2: baseline vs recommendation
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📊 基准 vs 推荐对比**\n{comparison}"
                }
            },
            {"tag": "hr"},
            # Section 3: recommended weights
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**⚖️ 推荐权重设置 (Top1)**\n{weight_lines}"
                }
            },
            {"tag": "hr"},
            # Section 4: dimension contribution
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📋 维度贡献率** (涨停股中出现在Top3的比例)\n{dim_text}"
                }
            },
            {"tag": "hr"},
            # Section 5: threshold curve
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**📈 阈值校准曲线**\n{curve_text}推荐阈值: 35~40 (信号池10~25只)"
                }
            },
            {"tag": "hr"},
            # Section 6: top 5 summary
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"**🏆 Top5 权重组合**\n"
                    f"#1: 基本面={data['top_10'][0]['weights']['fundamental']} 技术面={data['top_10'][0]['weights']['technical']} "
                    f"资金面={data['top_10'][0]['weights']['fundflow']} 情绪面={data['top_10'][0]['weights']['sentiment']} "
                    f"短线博弈={data['top_10'][0]['weights']['shortterm']} → 综合分{data['top_10'][0]['composite']:.4f}\n"
                    f"#2: 基本面={data['top_10'][1]['weights']['fundamental']} 技术面={data['top_10'][1]['weights']['technical']} "
                    f"资金面={data['top_10'][1]['weights']['fundflow']} 情绪面={data['top_10'][1]['weights']['sentiment']} "
                    f"短线博弈={data['top_10'][1]['weights']['shortterm']} → 综合分{data['top_10'][1]['composite']:.4f}\n"
                    f"#3: 基本面={data['top_10'][2]['weights']['fundamental']} 技术面={data['top_10'][2]['weights']['technical']} "
                    f"资金面={data['top_10'][2]['weights']['fundflow']} 情绪面={data['top_10'][2]['weights']['sentiment']} "
                    f"短线博弈={data['top_10'][2]['weights']['shortterm']} → 综合分{data['top_10'][2]['composite']:.4f}\n"
                    f"#4: 基本面={data['top_10'][3]['weights']['fundamental']} 技术面={data['top_10'][3]['weights']['technical']} "
                    f"资金面={data['top_10'][3]['weights']['fundflow']} 情绪面={data['top_10'][3]['weights']['sentiment']} "
                    f"短线博弈={data['top_10'][3]['weights']['shortterm']} → 综合分{data['top_10'][3]['composite']:.4f}\n"
                    f"#5: 基本面={data['top_10'][4]['weights']['fundamental']} 技术面={data['top_10'][4]['weights']['technical']} "
                    f"资金面={data['top_10'][4]['weights']['fundflow']} 情绪面={data['top_10'][4]['weights']['sentiment']} "
                    f"短线博弈={data['top_10'][4]['weights']['shortterm']} → 综合分{data['top_10'][4]['composite']:.4f}"
                }
            },
            {"tag": "hr"},
            # Section 7: diagnosis note
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": "**🔍 诊断**\n"
                    f"• 综合分{rec['composite']:.4f} (<0.3), 信号弱, 数据源需要修复\n"
                    f"• 资金面贡献率仅{dc['fundflow']['rate']:.0f}% (<30%), 权重调整无效, 需优先修复数据源\n"
                    f"• 短线博弈({dc['shortterm']['rate']:.0f}%)和技术面({dc['technical']['rate']:.0f}%)是当前最强维度\n"
                    f"• Top1相比基准仅提升{delta_sign}{delta_composite:.0f}e-4, 差异极小, 当前权重已接近局部最优"
                }
            },
            {"tag": "hr"},
            # Footer
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"Maneki 权重优化器 · {data['optimized_at']} · 加权Top3择优"
                    }
                ]
            }
        ]
    }

    return card


def main():
    # Load data
    with open(JSON_PATH) as f:
        data = json.load(f)

    # Read Feishu credentials
    env = read_env(ENV_PATH)
    app_id = env.get("FEISHU_APP_ID")
    app_secret = env.get("FEISHU_APP_SECRET")
    chat_id = env.get("FEISHU_CHAT_ID_REPORT")

    if not all([app_id, app_secret, chat_id]):
        print("ERROR: Missing Feishu credentials in .env")
        return

    # Get token
    token = get_tenant_token(app_id, app_secret)
    print(f"Got tenant token: {token[:10]}...")

    # Build card
    card = build_card(data)

    # Send message
    url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token}",
    }
    body = json.dumps({
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card),
    })
    resp = http_post(url, body, headers)
    if resp.get("code") == 0:
        print("✅ Feishu message sent successfully!")
        print(f"   Message ID: {resp.get('data', {}).get('message_id', 'N/A')}")
    else:
        print(f"❌ Failed to send Feishu message: {resp}")
        # Debug: try with minimal card to isolate issue
        print("  Trying minimal card to debug...")
        minimal_card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "⚙️ 权重优化报告 (debug)"},
                "template": "indigo",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": "Debug test card"},
                },
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "test footer"}],
                },
            ],
        }
        body2 = json.dumps({
            "receive_id": chat_id,
            "msg_type": "interactive",
            "content": json.dumps(minimal_card),
        })
        resp2 = http_post(url, body2, headers)
        print(f"   Debug result: {resp2}")


if __name__ == "__main__":
    main()
