#!/usr/bin/env python3
"""Send weight optimizer results to Feishu report group."""
import json
import os
import sys
import requests

# Read .env
def get_env(path):
    env = {}
    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env

env = get_env('/root/maneki-agent/.env')
APP_ID = env.get('FEISHU_APP_ID')
APP_SECRET = env.get('FEISHU_APP_SECRET')
CHAT_ID = env.get('FEISHU_CHAT_ID_REPORT')

if not all([APP_ID, APP_SECRET, CHAT_ID]):
    print("Missing Feishu credentials in .env")
    sys.exit(1)

# Read results
with open('/root/maneki-agent/plays/limit_up/data/weights/ranking_optimized.json', 'r') as f:
    results = json.load(f)

# Get tenant access token
resp = requests.post(
    'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
    json={
        'app_id': APP_ID,
        'app_secret': APP_SECRET
    },
    timeout=10
)
if resp.status_code != 200:
    print(f"Failed to get token: {resp.text}")
    sys.exit(1)

token = resp.json().get('tenant_access_token')
print(f"Got token: {token[:10]}...")

# Build baseline vs recommended comparison text
baseline = results['baseline']
rec = results['recommended']
dim_contrib = results['dim_contribution']
threshold_curve = results['threshold_curve']

bw = baseline['weights']
rw = rec['weights']

comparison_text = (
    f"📊 基准 (当前权重): "
    f"基本面={bw['fundamental']} 技术面={bw['technical']} "
    f"资金面={bw['fundflow']} 情绪面={bw['sentiment']} "
    f"短线博弈={bw['shortterm']}\n"
    f"   综合分 {baseline['composite']:.4f} | "
    f"涨停均排 {baseline['avg_rank']:.0f} | "
    f"AUC {baseline['auc']:.3f} | "
    f"分差 {baseline['sep']:.1f}\n"
    f"   📦 信号池(≥35): {baseline['pushed_count']}只(含涨停{baseline['push_limit_count']}只, "
    f"覆盖率{baseline['push_hit_rate']:.1f}%)\n\n"
    f"🏆 推荐 (Top1): "
    f"基本面={rw['fundamental']} 技术面={rw['technical']} "
    f"资金面={rw['fundflow']} 情绪面={rw['sentiment']} "
    f"短线博弈={rw['shortterm']}\n"
    f"   综合分 {rec['composite']:.4f} (+{rec['composite']-baseline['composite']:.4f}) | "
    f"涨停均排 {rec['avg_rank']:.0f} | "
    f"AUC {rec['auc']:.3f} | "
    f"分差 {rec['sep']:.1f}\n"
    f"   📦 信号池(≥35): {rec['pushed_count']}只(含涨停{rec['push_limit_count']}只, "
    f"覆盖率{rec['push_hit_rate']:.1f}%)"
)

# Build dimension contribution text
dim_labels = {
    'fundamental': '基本面',
    'technical': '技术面',
    'fundflow': '资金面',
    'sentiment': '情绪面',
    'shortterm': '短线博弈'
}
dim_lines = []
for dim, label in dim_labels.items():
    d = dim_contrib.get(dim, {})
    count = d.get('count', 0)
    rate = d.get('rate', 0)
    bar_len = max(1, int(rate / 5))
    bar = '█' * bar_len
    tag = ''
    if rate >= 70:
        tag = '✅ 核心维度'
    elif rate >= 30:
        tag = '📌 辅助维度'
    else:
        tag = '⚠️ 需修复'
    dim_lines.append(f"{label}: {rate:.0f}% ({count}/{results['total_limit_ups']}次) {bar} {tag}")

dim_text = '\n'.join(dim_lines)

# Build threshold curve summary
tc_lines = ['阈值 | 信号池 | 涨停 | 覆盖率 | 命中率']
for tc in threshold_curve:
    tc_lines.append(f"≥{tc['threshold']:2d} | {tc['pushed']:3d}只 | {tc['limit_above']:2d}只 | "
                    f"{tc['coverage_rate']:.0f}% | {tc['hit_rate']:.1f}%")

# Find recommended threshold
rec_threshold = None
for tc in threshold_curve:
    if 30 <= tc['coverage_rate'] <= 50:
        rec_threshold = tc
        break
if not rec_threshold and len(threshold_curve) > 4:
    rec_threshold = threshold_curve[4]

tc_text = '\n'.join(tc_lines[:6] + ['...'])  # Show first 6 lines

# Build card
card = {
    "config": {"wide_screen_mode": True},
    "header": {
        "title": {"tag": "plain_text", "content": "⚙️ 权重优化报告 V2 — 2026-06-25"},
        "template": "blue"
    },
    "elements": [
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**数据范围**: {results['data_range']} ({results['total_records']}条记录, {results['total_limit_ups']}只涨停)\n**交易日数**: 10天\n**搜索组合**: 2005种权重组合"
            }
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📊 基准 vs 推荐对比**\n{comparison_text}"
            }
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📋 维度贡献率 (当前权重)**\n{dim_text}"
            }
        },
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**📊 阈值校准曲线**\n{''.join(tc_lines[:8])}\n\n推荐阈值: 35~40（覆盖率30~50%，推送池10~25只）"
            }
        },
        {"tag": "hr"},
        {
            "tag": "note",
            "elements": [
                {
                    "tag": "plain_text",
                    "content": f"权重优化器 V2 | 加权Top3择优 | 详情: data/weights/ranking_optimized.json"
                }
            ]
        }
    ]
}

# Send message
headers = {
    'Authorization': f'Bearer {token}',
    'Content-Type': 'application/json; charset=utf-8'
}
body = {
    'receive_id': CHAT_ID,
    'msg_type': 'interactive',
    'content': json.dumps(card)
}

resp = requests.post(
    'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
    headers=headers,
    json=body,
    timeout=15
)

result = resp.json()
if result.get('code') == 0:
    print(f"✅ Feishu message sent successfully! message_id: {result.get('data', {}).get('message_id', 'N/A')}")
else:
    print(f"❌ Failed to send: {json.dumps(result, ensure_ascii=False, indent=2)}")
    # Debug: try with minimal card
    print("\nTrying minimal card debug...")
    minimal_card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "权重优化报告"},
            "template": "blue"
        },
        "elements": [
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": f"基准综合分: {baseline['composite']:.4f}\n推荐综合分: {rec['composite']:.4f}"
                }
            },
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "Auto-generated report"}
                ]
            }
        ]
    }
    body2 = {
        'receive_id': CHAT_ID,
        'msg_type': 'interactive',
        'content': json.dumps(minimal_card)
    }
    resp2 = requests.post(
        'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id',
        headers=headers,
        json=body2,
        timeout=15
    )
    print(f"Minimal card result: {resp2.text[:500]}")
