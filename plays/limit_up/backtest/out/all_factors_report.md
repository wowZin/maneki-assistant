# 全因子扫描评估报告

- 样本：3811 条记录，1527 只股票，20 个交易日
- 基线 hit_limit_3 均值：0.1748
- 基线 fwd_ret_3 均值：0.0014

## 因子排名（按 hit_limit_3 RankIC 降序）

| factor                  | category   |   ic_hit_limit_3 |   ic_fwd_ret_3 |   ic_fwd_max_3 |   ic_trailing_10 |   chasing_score |   hit@10 |   hit@20 |   fwd3@10 |   fwd3@20 |
|:------------------------|:-----------|-----------------:|---------------:|---------------:|-----------------:|----------------:|---------:|---------:|----------:|----------:|
| shortterm               | baseline   |           0.2342 |         0.0561 |         0.2192 |           0.5112 |          0.455  |      0.7 |     0.6  |    0.052  |    0.0399 |
| aggressive_total        | standalone |           0.2248 |         0.0434 |         0.2221 |           0.5439 |          0.5005 |      0.5 |     0.5  |    0.0308 |    0.0238 |
| limit_up_gene_composite | standalone |           0.2196 |         0.0438 |         0.2241 |           0.4736 |          0.4298 |      0.6 |     0.35 |    0.0585 |    0.0313 |
| limit_up_gene_60d       | standalone |           0.2175 |         0.0543 |         0.2284 |           0.4567 |          0.4024 |      0.5 |     0.45 |   -0.0092 |    0.0118 |
| limit_up_gene_20d       | standalone |           0.2134 |         0.0263 |         0.2083 |           0.477  |          0.4507 |      0.4 |     0.35 |    0.0302 |    0.0109 |
| new_total_v2            | standalone |           0.2076 |         0.0873 |         0.2264 |           0.5446 |          0.4573 |      0.6 |     0.6  |    0.0329 |    0.0573 |
| technical               | baseline   |           0.1823 |         0.0605 |         0.1918 |           0.7201 |          0.6596 |      0.3 |     0.2  |    0.0256 |    0.0093 |
| shortterm_anti_chasing  | adjustment |           0.1754 |         0.0288 |         0.1608 |           0.2325 |          0.2038 |      0.5 |     0.35 |   -0.015  |    0.0105 |
| total_current           | baseline   |           0.1573 |         0.0685 |         0.1727 |           0.4642 |          0.3957 |      0.3 |     0.3  |   -0.0244 |   -0.0197 |
| technical_anti_chasing  | adjustment |           0.1556 |         0.0529 |         0.1694 |           0.6003 |          0.5474 |      0.1 |     0.1  |   -0.0194 |   -0.0286 |
| sentiment               | baseline   |           0.1277 |         0.023  |         0.1454 |           0.3715 |          0.3485 |      0.1 |     0.25 |   -0.0049 |    0.026  |
| sentiment_anti_chasing  | adjustment |           0.1275 |         0.0229 |         0.1452 |           0.3707 |          0.3479 |      0.1 |     0.15 |    0.0338 |    0.0279 |
| balanced_total          | standalone |           0.1192 |         0.0866 |         0.1587 |           0.2211 |          0.1346 |      0.7 |     0.5  |    0.1515 |    0.0743 |
| return_optimized_total  | standalone |           0.0909 |         0.0854 |         0.138  |           0.1606 |          0.0752 |      0.3 |     0.4  |    0.0963 |    0.0752 |
| fundamental             | baseline   |           0.0471 |         0.1064 |         0.1129 |           0.1305 |          0.0241 |      0.2 |     0.15 |   -0.0444 |   -0.0324 |
| sentiment_contrarian    | standalone |           0.0423 |         0.0329 |         0.0378 |           0.1024 |          0.0695 |      0.2 |     0.3  |   -0.0138 |    0.0491 |
| quality_value_total     | standalone |           0.0347 |         0.0741 |         0.0815 |          -0.0265 |         -0.1006 |      0.2 |     0.15 |    0.0312 |    0.0382 |
| upper_shadow_risk       | standalone |           0.0303 |         0.0215 |         0.0179 |           0.0703 |          0.0488 |      0   |     0.15 |   -0.0132 |    0.0193 |
| pullback_from_peak      | standalone |           0.0294 |        -0.0107 |        -0.0055 |           0.1106 |          0.1212 |      0.4 |     0.45 |    0.0305 |    0.0355 |
| pullback_quality        | standalone |           0.0219 |         0.0066 |        -0.0083 |           0.0859 |          0.0794 |      0.1 |     0.1  |    0.0194 |   -0.0053 |
| fundflow                | baseline   |           0.0219 |         0.0119 |         0.0239 |           0.0621 |          0.0503 |      0.4 |     0.3  |   -0.0021 |   -0.0323 |
| volatility_contraction  | standalone |           0.0219 |        -0.025  |        -0.0039 |           0.0384 |          0.0634 |      0.1 |     0.05 |   -0.0245 |   -0.037  |
| total_quality_bonus     | standalone |           0.017  |        -0.0005 |         0.0201 |           0.0263 |          0.0269 |      0.2 |     0.15 |   -0.0001 |    0.0172 |
| gap_up_quality          | standalone |          -0.0058 |         0.0017 |         0.0043 |          -0.0433 |         -0.045  |      0   |     0    |   -0.0571 |   -0.0497 |
| amount_surge            | standalone |          -0.013  |        -0.0415 |        -0.0309 |           0.0151 |          0.0567 |      0.1 |     0.1  |   -0.0312 |   -0.013  |
| amount_acceleration     | standalone |          -0.0177 |        -0.0181 |        -0.0231 |           0.0287 |          0.0468 |      0.1 |     0.25 |    0.0018 |    0.015  |
| low_amplitude_breakout  | standalone |          -0.023  |         0.005  |        -0.0065 |           0.0006 |         -0.0044 |      0   |     0.05 |   -0.0256 |    0.0046 |
| consecutive_strength    | standalone |          -0.0399 |         0.0124 |        -0.0265 |          -0.1094 |         -0.1218 |      0.3 |     0.25 |    0.035  |   -0.0007 |
| reversal_signal         | standalone |          -0.0525 |        -0.0374 |        -0.047  |          -0.1695 |         -0.1321 |      0.1 |     0.1  |    0.0373 |   -0.0037 |
| vol_expansion_quality   | standalone |          -0.0647 |        -0.0454 |        -0.0915 |          -0.0742 |         -0.0288 |      0.2 |     0.1  |   -0.0085 |   -0.0346 |
| large_amplitude_risk    | standalone |          -0.0699 |         0.0064 |        -0.0565 |          -0.1248 |         -0.1312 |      0   |     0.15 |   -0.0132 |    0.0193 |
| position_optimal        | standalone |          -0.0762 |        -0.0558 |        -0.1009 |          -0.3227 |         -0.2669 |      0   |     0.05 |    0.0147 |    0.0131 |
| dimension_divergence    | standalone |         nan      |       nan      |       nan      |         nan      |          0      |      0   |     0.15 |   -0.0132 |    0.0193 |
| circ_mv_tier            | standalone |         nan      |       nan      |       nan      |         nan      |          0      |      0   |     0.15 |   -0.0132 |    0.0193 |
| fundamental_quality     | standalone |         nan      |       nan      |       nan      |         nan      |          0      |      0   |     0.15 |   -0.0132 |    0.0193 |
| turnover_penalty        | standalone |         nan      |       nan      |       nan      |         nan      |          0      |      0   |     0.15 |   -0.0132 |    0.0193 |
| volume_ratio_penalty    | standalone |         nan      |       nan      |       nan      |         nan      |          0      |      0   |     0.15 |   -0.0132 |    0.0193 |
| net_mf_signal           | standalone |         nan      |       nan      |       nan      |         nan      |          0      |      0   |     0.15 |   -0.0132 |    0.0193 |
| elg_inflow_signal       | standalone |         nan      |       nan      |       nan      |         nan      |          0      |      0   |     0.15 |   -0.0132 |    0.0193 |

## TOP 10 最佳因子详情

### shortterm

- IC hit_limit_3: 0.2342
- IC fwd_ret_3: 0.0561
- Chasing score: 0.4550
- hit@10: 0.7, hit@20: 0.6
- fwd3@10: 0.0520, fwd3@20: 0.0399

### aggressive_total

- IC hit_limit_3: 0.2248
- IC fwd_ret_3: 0.0434
- Chasing score: 0.5005
- hit@10: 0.5, hit@20: 0.5
- fwd3@10: 0.0308, fwd3@20: 0.0238

### limit_up_gene_composite

- IC hit_limit_3: 0.2196
- IC fwd_ret_3: 0.0438
- Chasing score: 0.4298
- hit@10: 0.6, hit@20: 0.35
- fwd3@10: 0.0585, fwd3@20: 0.0313

### limit_up_gene_60d

- IC hit_limit_3: 0.2175
- IC fwd_ret_3: 0.0543
- Chasing score: 0.4024
- hit@10: 0.5, hit@20: 0.45
- fwd3@10: -0.0092, fwd3@20: 0.0118

### limit_up_gene_20d

- IC hit_limit_3: 0.2134
- IC fwd_ret_3: 0.0263
- Chasing score: 0.4507
- hit@10: 0.4, hit@20: 0.35
- fwd3@10: 0.0302, fwd3@20: 0.0109

### new_total_v2

- IC hit_limit_3: 0.2076
- IC fwd_ret_3: 0.0873
- Chasing score: 0.4573
- hit@10: 0.6, hit@20: 0.6
- fwd3@10: 0.0329, fwd3@20: 0.0573

### technical

- IC hit_limit_3: 0.1823
- IC fwd_ret_3: 0.0605
- Chasing score: 0.6596
- hit@10: 0.3, hit@20: 0.2
- fwd3@10: 0.0256, fwd3@20: 0.0093

### shortterm_anti_chasing

- IC hit_limit_3: 0.1754
- IC fwd_ret_3: 0.0288
- Chasing score: 0.2038
- hit@10: 0.5, hit@20: 0.35
- fwd3@10: -0.0150, fwd3@20: 0.0105

### total_current

- IC hit_limit_3: 0.1573
- IC fwd_ret_3: 0.0685
- Chasing score: 0.3957
- hit@10: 0.3, hit@20: 0.3
- fwd3@10: -0.0244, fwd3@20: -0.0197

### technical_anti_chasing

- IC hit_limit_3: 0.1556
- IC fwd_ret_3: 0.0529
- Chasing score: 0.5474
- hit@10: 0.1, hit@20: 0.1
- fwd3@10: -0.0194, fwd3@20: -0.0286

## 对比：当前 total vs 最佳新因子

### 当前 total
- IC hit_limit_3: 0.1573
- hit@10: 0.3, hit@20: 0.3

### 最佳新因子: aggressive_total
- IC hit_limit_3: 0.2248
- hit@10: 0.5, hit@20: 0.5
- 改善: IC 0.0675
