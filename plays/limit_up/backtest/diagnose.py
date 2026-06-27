"""Layer1 全局诊断 — 用现有落库的维度分评估预测力，验证 H1~H5。

运行（需 .env / tushare token）：
    python3 -m plays.limit_up.backtest.diagnose

产出：
    out/panel.csv          带标签面板
    out/diagnose_report.md  诊断报告
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(ROOT))

from plays.limit_up.backtest import dataset as D
from plays.limit_up.backtest import metrics as M

DIMS = D.DIM_COLS
FWD_LABELS = ["fwd_ret_1", "fwd_ret_3", "fwd_max_3", "hit_limit_3"]
OUT = D.OUT_DIR


def run(dedup: str = "last") -> str:
    panel = D.build_panel(dedup=dedup)
    panel = panel[panel.get("_aligned", True) != False].copy()

    lines: list[str] = []
    w = lines.append
    w("# Layer1 全局诊断报告\n")
    w(f"- 样本：{len(panel)} 条已对齐 (code,date) 评分记录")
    w(f"- 维度：{', '.join(DIMS)}")
    w(f"- 标签：{', '.join(FWD_LABELS)}（fwd=未来收益, hit_limit_3=未来3日是否涨停）\n")

    # H1: 维度预测力 RankIC
    w("## H1 维度预测力（RankIC，越高越好；接近0或负=无预测力）\n")
    ic = M.dimension_report(panel, DIMS + ["total"], FWD_LABELS)
    w(_pivot_md(ic, "rank_ic"))
    w("")

    # H2: 追高系数（维度分 vs trailing 过去涨幅；显著为正=在追高）
    w("## H2 追高诊断（维度分 vs 过去涨幅 trailing 的 RankIC，正值大=系统性追高）\n")
    rows = []
    for dim in DIMS + ["total"]:
        rows.append(
            {
                "dimension": dim,
                "ic_trailing_5": M.rank_ic(panel[dim], panel["trailing_5"]),
                "ic_trailing_10": M.rank_ic(panel[dim], panel["trailing_10"]),
                "ic_fwd_ret_3": M.rank_ic(panel[dim], panel["fwd_ret_3"]),
            }
        )
    w(_df_md(pd.DataFrame(rows)))
    w("\n> 解读：若 ic_trailing 明显>0 而 ic_fwd_ret_3≈0/<0，说明该维度在『奖励已涨高的票』而非预测未来。\n")

    # H3/H5: total 与共振 的命中率分桶
    w("## H3 加权总分分桶命中率（理想：高分桶 hit_limit_3 单调更高）\n")
    bs = M.bucket_stats(panel, "total", FWD_LABELS, n_buckets=5)
    w(_df_md(bs))
    w("")

    # Precision@K：实战最关心 Top-K
    w("## 实战 Precision@K（按 total 排序的 Top-K 命中率/收益）\n")
    pk_rows = []
    for k in (10, 20, 50):
        pk_rows.append(
            {
                "TopK": k,
                "hit_limit_3": M.precision_at_k(panel, "total", "hit_limit_3", k)["value"],
                "avg_fwd_ret_3": M.precision_at_k(panel, "total", "fwd_ret_3", k)["value"],
                "avg_fwd_max_3": M.precision_at_k(panel, "total", "fwd_max_3", k)["value"],
            }
        )
    w(_df_md(pd.DataFrame(pk_rows)))
    w("")

    # 基线对照：全样本平均命中率/收益
    w("## 基线（全候选池平均，作为 Precision@K 的对照）\n")
    base = {
        "hit_limit_3_mean": panel["hit_limit_3"].mean(),
        "fwd_ret_3_mean": panel["fwd_ret_3"].mean(),
        "fwd_max_3_mean": panel["fwd_max_3"].mean(),
        "win_rate_fwd_3": M.win_rate(panel["fwd_ret_3"])["win_rate"],
    }
    w(_df_md(pd.DataFrame([base])))
    w("\n> 若 Top-K 命中率不显著高于基线，说明评分体系几乎没有择股能力。\n")

    # H4: 推送阈值与高确信度筛选分析
    w("## H4 阈值与筛选误杀（当前推送规则：总分≥30 + 情绪≥35 + 资金≥35 + 总分≥40）\n")
    w(_threshold_analysis(panel))
    w("")

    # H5: 维度共振有效性
    w("## H5 维度共振有效性（≥3 个维度≥75 分定义为共振）\n")
    w(_resonance_analysis(panel))
    w("")

    report = "\n".join(lines)
    (OUT / "diagnose_report.md").write_text(report, encoding="utf-8")
    return report


def _threshold_analysis(panel: pd.DataFrame) -> str:
    """分析当前推送阈值条件的表现与误杀。"""
    total_mask = panel["total"] >= 30
    push_mask = total_mask & (panel["sentiment"] >= 35) & (panel["fundflow"] >= 35) & (panel["total"] >= 40)
    filtered_by_threshold = total_mask & ~push_mask  # 过了总分门槛但被情绪/资金/总分二次过滤掉

    rows = []
    for name, mask in [
        ("总分≥30", total_mask),
        ("实际推送条件", push_mask),
        ("被高确信条件过滤", filtered_by_threshold),
        ("全样本", pd.Series(True, index=panel.index)),
    ]:
        sub = panel[mask]
        rows.append(
            {
                "group": name,
                "n": len(sub),
                "hit_limit_3": sub["hit_limit_3"].mean() if len(sub) else None,
                "avg_fwd_ret_3": sub["fwd_ret_3"].mean() if len(sub) else None,
                "avg_fwd_max_3": sub["fwd_max_3"].mean() if len(sub) else None,
                "win_rate_fwd_3": M.win_rate(sub["fwd_ret_3"])["win_rate"] if len(sub) else None,
            }
        )

    # 不同 total 阈值下的 precision / recall（以 hit_limit_3 为阳性）
    total_thresholds = [25, 30, 35, 40, 45, 50]
    thr_rows = []
    positives = panel["hit_limit_3"].sum()
    for thr in total_thresholds:
        pred = panel["total"] >= thr
        tp = int(((pred) & (panel["hit_limit_3"] == 1)).sum())
        fp = int(((pred) & (panel["hit_limit_3"] == 0)).sum())
        fn = int(((~pred) & (panel["hit_limit_3"] == 1)).sum())
        precision = tp / (tp + fp) if (tp + fp) else 0
        recall = tp / positives if positives else 0
        thr_rows.append(
            {
                "total_threshold": thr,
                "pred_n": int(pred.sum()),
                "precision": precision,
                "recall": recall,
                "f1": 2 * precision * recall / (precision + recall) if (precision + recall) else 0,
            }
        )

    lines: list[str] = []
    w = lines.append
    w("### 当前规则表现\n")
    w(_df_md(pd.DataFrame(rows)))
    w("\n> 若『被高确信条件过滤』组的命中率显著高于『实际推送条件』组，说明当前阈值在误杀。\n")
    w("### 不同总分阈值的 Precision / Recall / F1\n")
    w(_df_md(pd.DataFrame(thr_rows)))
    w("\n> 理想：阈值提高应带来 precision 提升且不严重牺牲 recall；若 precision 不升反降，说明总分排序能力弱。\n")
    return "\n".join(lines)


def _resonance_analysis(panel: pd.DataFrame) -> str:
    """分析 resonance.count / is_resonance 对未来涨停的区分能力。"""
    if "resonance_count" not in panel.columns:
        # analysis 记录里 resonance.count 是 dict，dataset 没有展开；这里兼容处理
        panel = panel.copy()
        panel["resonance_count"] = 0
    rows = []
    for cnt in sorted(panel["resonance_count"].dropna().unique()):
        sub = panel[panel["resonance_count"] == cnt]
        rows.append(
            {
                "resonance_count": int(cnt),
                "n": len(sub),
                "hit_limit_3": sub["hit_limit_3"].mean(),
                "avg_fwd_ret_3": sub["fwd_ret_3"].mean(),
                "avg_fwd_max_3": sub["fwd_max_3"].mean(),
            }
        )
    return _df_md(pd.DataFrame(rows)) + "\n> 若 resonance_count 高组命中率未显著更高，则共振信号无效。\n"


def _pivot_md(ic_df: pd.DataFrame, value: str) -> str:
    p = ic_df.pivot(index="dimension", columns="label", values=value).round(3)
    return _df_md(p.reset_index())


def _df_md(df: pd.DataFrame) -> str:
    df = df.round(4)
    try:
        return df.to_markdown(index=False)
    except Exception:
        return df.to_string(index=False)


if __name__ == "__main__":
    print(run())
    print("\n报告已写入 out/diagnose_report.md")
