from plays.limit_up.factors.optimized.quality_combo import factor_quality_combo


def test_quality_combo_tier_100():
    # 100 分档：高换手 + 技术面/短线分双高 + 动量不过高 + 资金分过关
    assert factor_quality_combo({
        "turnover_rate": 20.0, "trailing_10": 0.15,
        "position_20d": 0.60, "pct_chg_score_day": 4.0,
        "technical": 35.0, "shortterm": 32.0, "fundflow": 15.0,
        "limit_up_count_20d": 1,
    }) == 100.0


def test_quality_combo_tier_95():
    # 95 分档：涨停基因 + 高换手 + 温和动量 + 不追高 + 维度优秀 + 资金分过关
    assert factor_quality_combo({
        "turnover_rate": 15.0, "trailing_10": 0.12,
        "position_20d": 0.60, "pct_chg_score_day": 4.0,
        "technical": 35.0, "shortterm": 28.0, "fundflow": 12.0,
        "limit_up_count_20d": 3,
    }) == 95.0


def test_quality_combo_excludes_low_fundflow():
    # 资金分不足直接 0 分
    assert factor_quality_combo({
        "turnover_rate": 20.0, "trailing_10": 0.12,
        "position_20d": 0.60, "pct_chg_score_day": 4.0,
        "technical": 35.0, "shortterm": 32.0, "fundflow": 5.0,
        "limit_up_count_20d": 3,
    }) == 0.0


def test_quality_combo_excludes_chasing():
    # 追高：trailing 过高
    assert factor_quality_combo({
        "turnover_rate": 20.0, "trailing_10": 0.50,
        "position_20d": 0.60, "pct_chg_score_day": 4.0,
        "technical": 35.0, "shortterm": 32.0, "fundflow": 15.0,
        "limit_up_count_20d": 3,
    }) == 0.0

    # 无动量：trailing 为负，不满足 95 分档的动量区间，也不满足 100 分档（调低 shortterm）
    assert factor_quality_combo({
        "turnover_rate": 15.0, "trailing_10": -0.05,
        "position_20d": 0.60, "pct_chg_score_day": 4.0,
        "technical": 35.0, "shortterm": 28.0, "fundflow": 12.0,
        "limit_up_count_20d": 3,
    }) == 0.0

    # 位置过高
    assert factor_quality_combo({
        "turnover_rate": 15.0, "trailing_10": 0.12,
        "position_20d": 0.90, "pct_chg_score_day": 4.0,
        "technical": 35.0, "shortterm": 28.0, "fundflow": 12.0,
        "limit_up_count_20d": 3,
    }) == 0.0

    # 当日涨幅过大
    assert factor_quality_combo({
        "turnover_rate": 15.0, "trailing_10": 0.12,
        "position_20d": 0.60, "pct_chg_score_day": 8.0,
        "technical": 35.0, "shortterm": 28.0, "fundflow": 12.0,
        "limit_up_count_20d": 3,
    }) == 0.0

    # 涨停基因不足
    assert factor_quality_combo({
        "turnover_rate": 15.0, "trailing_10": 0.12,
        "position_20d": 0.60, "pct_chg_score_day": 4.0,
        "technical": 35.0, "shortterm": 28.0, "fundflow": 12.0,
        "limit_up_count_20d": 1,
    }) == 0.0


def test_quality_combo_excludes_low_turnover():
    assert factor_quality_combo({
        "turnover_rate": 5.0, "trailing_10": 0.12,
        "position_20d": 0.60, "pct_chg_score_day": 4.0,
        "technical": 35.0, "shortterm": 28.0, "fundflow": 12.0,
        "limit_up_count_20d": 3,
    }) == 0.0
