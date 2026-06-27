# 全因子扫描评估报告

- 样本：3846 条记录，1527 只股票，20 个交易日
- 基线 hit_limit_3 均值：0.1750
- 基线 fwd_ret_3 均值：0.0012

## 因子排名（按 hit_limit_3 RankIC 降序）

| factor                  | category   |   ic_hit_limit_3 |   ic_fwd_ret_3 |   ic_fwd_max_3 |   ic_trailing_10 |   chasing_score |   hit@10 |   hit@20 |   fwd3@10 |   fwd3@20 |
|:------------------------|:-----------|-----------------:|---------------:|---------------:|-----------------:|----------------:|---------:|---------:|----------:|----------:|
| concept_turnover        | standalone |           0.3046 |         0.2107 |         0.3186 |           0.452  |          0.2413 |      0.4 |     0.45 |    0.0704 |    0.0546 |
| new_total_v2            | standalone |           0.2491 |         0.1267 |         0.2767 |           0.6    |          0.4733 |      0.4 |     0.55 |   -0.01   |    0.0415 |
| shortterm               | baseline   |           0.2337 |         0.0529 |         0.2183 |           0.5165 |          0.4635 |      0.7 |     0.65 |    0.0405 |    0.0412 |
| concept_momentum        | standalone |           0.2254 |         0.1572 |         0.2608 |           0.4336 |          0.2764 |      0.2 |     0.25 |   -0.0277 |    0.0195 |
| aggressive_total        | standalone |           0.2238 |         0.0397 |         0.2209 |           0.5453 |          0.5056 |      0.5 |     0.5  |    0.0308 |    0.0238 |
| limit_up_gene_composite | standalone |           0.2177 |         0.039  |         0.2223 |           0.4807 |          0.4417 |      0.5 |     0.3  |    0.0463 |    0.025  |
| limit_up_gene_60d       | standalone |           0.215  |         0.0489 |         0.2258 |           0.4636 |          0.4146 |      0.6 |     0.4  |   -0.0099 |   -0.0184 |
| limit_up_gene_20d       | standalone |           0.2119 |         0.0217 |         0.2069 |           0.4838 |          0.4621 |      0.4 |     0.35 |    0.0302 |    0.0109 |
| concept_up_streak       | standalone |           0.2056 |         0.1114 |         0.1931 |           0.3582 |          0.2468 |      0.4 |     0.3  |    0.099  |    0.0541 |
| balanced_total          | standalone |           0.1969 |         0.1448 |         0.244  |           0.3522 |          0.2074 |      0.6 |     0.7  |    0.1031 |    0.096  |
| technical               | baseline   |           0.1827 |         0.0568 |         0.1903 |           0.7225 |          0.6658 |      0.3 |     0.2  |    0.0256 |    0.0093 |
| shortterm_anti_chasing  | adjustment |           0.1736 |         0.0257 |         0.1593 |           0.2306 |          0.2049 |      0.5 |     0.35 |   -0.015  |    0.0105 |
| total_current           | baseline   |           0.1579 |         0.0652 |         0.171  |           0.4651 |          0.3999 |      0.3 |     0.3  |   -0.0244 |   -0.0197 |
| technical_anti_chasing  | adjustment |           0.155  |         0.049  |         0.1674 |           0.5994 |          0.5504 |      0.1 |     0.1  |   -0.0194 |   -0.0286 |
| sentiment               | baseline   |           0.1283 |         0.0225 |         0.1458 |           0.3756 |          0.3531 |      0.1 |     0.25 |   -0.0049 |    0.026  |
| sentiment_anti_chasing  | adjustment |           0.128  |         0.0224 |         0.1457 |           0.3749 |          0.3525 |      0.1 |     0.15 |    0.0338 |    0.0279 |
| circ_mv_tier            | standalone |           0.1168 |         0.155  |         0.2054 |           0.3329 |          0.1779 |      0.3 |     0.15 |    0.0749 |    0.0421 |
| inst_consistency        | standalone |           0.102  |        -0.0145 |         0.0991 |           0.3233 |          0.3378 |      0.3 |     0.25 |   -0.0219 |   -0.0019 |
| inst_following          | standalone |           0.0962 |        -0.0089 |         0.1023 |           0.322  |          0.3309 |      0.2 |     0.4  |   -0.0156 |    0.0366 |
| fundamental             | baseline   |           0.0467 |         0.1045 |         0.1105 |           0.1313 |          0.0268 |      0.2 |     0.15 |   -0.0444 |   -0.0324 |
| sentiment_contrarian    | standalone |           0.0466 |         0.0367 |         0.0412 |           0.1028 |          0.0661 |      0.2 |     0.35 |   -0.0138 |    0.0661 |
| return_optimized_total  | standalone |           0.0432 |         0.0929 |         0.0999 |           0.058  |         -0.0348 |      0.4 |     0.35 |    0.0523 |    0.0528 |
| top_list_quality        | standalone |           0.0399 |         0.0251 |         0.0924 |           0.1548 |          0.1297 |      0.2 |     0.2  |    0.0134 |    0.0112 |
| upper_shadow_risk       | standalone |           0.0308 |         0.0216 |         0.0182 |           0.0716 |          0.05   |      0   |     0.15 |   -0.0132 |    0.0193 |
| pullback_from_peak      | standalone |           0.03   |        -0.0048 |        -0.0045 |           0.0947 |          0.0994 |      0.4 |     0.45 |    0.0305 |    0.0355 |
| volatility_contraction  | standalone |           0.0258 |        -0.023  |        -0.0002 |           0.0418 |          0.0648 |      0.1 |     0.05 |   -0.0245 |   -0.0372 |
| fundflow                | baseline   |           0.024  |         0.0126 |         0.0259 |           0.0613 |          0.0487 |      0.4 |     0.3  |   -0.0021 |   -0.0323 |
| pullback_quality        | standalone |           0.0228 |         0.008  |        -0.0085 |           0.0822 |          0.0742 |      0.1 |     0.1  |    0.0194 |   -0.0053 |
| elg_inflow_signal       | standalone |           0.0182 |        -0.0266 |         0.0256 |           0.0686 |          0.0952 |      0.3 |     0.2  |    0.0355 |    0.0109 |
| total_quality_bonus     | standalone |           0.0169 |        -0.0004 |         0.02   |           0.0256 |          0.0261 |      0.2 |     0.15 |   -0.0001 |    0.0172 |
| volume_ratio_penalty    | standalone |           0.0159 |         0.0685 |         0.0529 |          -0.081  |         -0.1494 |      0   |     0.15 |   -0.0132 |    0.0213 |
| gap_up_quality          | standalone |          -0.0044 |         0.0017 |         0.0042 |          -0.0516 |         -0.0533 |      0   |     0    |   -0.0571 |   -0.0551 |
| amount_surge            | standalone |          -0.0149 |        -0.044  |        -0.0339 |           0.017  |          0.061  |      0.1 |     0.1  |   -0.0312 |   -0.013  |
| quality_value_total     | standalone |          -0.0158 |         0.0783 |         0.0407 |          -0.124  |         -0.2023 |      0.3 |     0.25 |    0.0399 |    0.0149 |
| amount_acceleration     | standalone |          -0.0201 |        -0.0194 |        -0.0224 |           0.0302 |          0.0496 |      0.1 |     0.25 |    0.0018 |    0.015  |
| low_amplitude_breakout  | standalone |          -0.024  |         0.0052 |        -0.0042 |           0.002  |         -0.0032 |      0   |     0.05 |   -0.0182 |   -0.0001 |
| consecutive_strength    | standalone |          -0.045  |         0.0099 |        -0.0309 |          -0.1178 |         -0.1277 |      0.3 |     0.25 |    0.035  |   -0.0007 |
| reversal_signal         | standalone |          -0.0526 |        -0.0357 |        -0.0471 |          -0.173  |         -0.1372 |      0.1 |     0.1  |    0.0373 |   -0.0037 |
| net_mf_signal           | standalone |          -0.0586 |        -0.0113 |        -0.0729 |          -0.0407 |         -0.0294 |      0.1 |     0.35 |    0.0385 |    0.0615 |
| vol_expansion_quality   | standalone |          -0.0658 |        -0.0452 |        -0.091  |          -0.0768 |         -0.0316 |      0.2 |     0.1  |   -0.0085 |   -0.0346 |
| large_amplitude_risk    | standalone |          -0.0686 |         0.0073 |        -0.054  |          -0.1235 |         -0.1308 |      0   |     0.15 |   -0.0132 |    0.0193 |
| position_optimal        | standalone |          -0.0778 |        -0.0527 |        -0.0996 |          -0.3359 |         -0.2832 |      0   |     0.05 |    0.0147 |    0.0131 |
| turnover_penalty        | standalone |          -0.1289 |         0.0087 |        -0.0866 |          -0.3053 |         -0.314  |      0   |     0.15 |   -0.0132 |    0.0193 |
| fundamental_quality     | standalone |          -0.143  |        -0.0419 |        -0.1546 |          -0.1853 |         -0.1434 |      0.1 |     0.05 |   -0.0028 |   -0.0115 |
| dimension_divergence    | standalone |         nan      |       nan      |       nan      |         nan      |          0      |      0   |     0.15 |   -0.0132 |    0.0193 |

## TOP 10 最佳因子详情

### concept_turnover

- IC hit_limit_3: 0.3046
- IC fwd_ret_3: 0.2107
- Chasing score: 0.2413
- hit@10: 0.4, hit@20: 0.45
- fwd3@10: 0.0704, fwd3@20: 0.0546

### new_total_v2

- IC hit_limit_3: 0.2491
- IC fwd_ret_3: 0.1267
- Chasing score: 0.4733
- hit@10: 0.4, hit@20: 0.55
- fwd3@10: -0.0100, fwd3@20: 0.0415

### shortterm

- IC hit_limit_3: 0.2337
- IC fwd_ret_3: 0.0529
- Chasing score: 0.4635
- hit@10: 0.7, hit@20: 0.65
- fwd3@10: 0.0405, fwd3@20: 0.0412

### concept_momentum

- IC hit_limit_3: 0.2254
- IC fwd_ret_3: 0.1572
- Chasing score: 0.2764
- hit@10: 0.2, hit@20: 0.25
- fwd3@10: -0.0277, fwd3@20: 0.0195

### aggressive_total

- IC hit_limit_3: 0.2238
- IC fwd_ret_3: 0.0397
- Chasing score: 0.5056
- hit@10: 0.5, hit@20: 0.5
- fwd3@10: 0.0308, fwd3@20: 0.0238

### limit_up_gene_composite

- IC hit_limit_3: 0.2177
- IC fwd_ret_3: 0.0390
- Chasing score: 0.4417
- hit@10: 0.5, hit@20: 0.3
- fwd3@10: 0.0463, fwd3@20: 0.0250

### limit_up_gene_60d

- IC hit_limit_3: 0.2150
- IC fwd_ret_3: 0.0489
- Chasing score: 0.4146
- hit@10: 0.6, hit@20: 0.4
- fwd3@10: -0.0099, fwd3@20: -0.0184

### limit_up_gene_20d

- IC hit_limit_3: 0.2119
- IC fwd_ret_3: 0.0217
- Chasing score: 0.4621
- hit@10: 0.4, hit@20: 0.35
- fwd3@10: 0.0302, fwd3@20: 0.0109

### concept_up_streak

- IC hit_limit_3: 0.2056
- IC fwd_ret_3: 0.1114
- Chasing score: 0.2468
- hit@10: 0.4, hit@20: 0.3
- fwd3@10: 0.0990, fwd3@20: 0.0541

### balanced_total

- IC hit_limit_3: 0.1969
- IC fwd_ret_3: 0.1448
- Chasing score: 0.2074
- hit@10: 0.6, hit@20: 0.7
- fwd3@10: 0.1031, fwd3@20: 0.0960

## 对比：当前 total vs 最佳新因子

### 当前 total
- IC hit_limit_3: 0.1579
- hit@10: 0.3, hit@20: 0.3

### 最佳新因子: concept_turnover
- IC hit_limit_3: 0.3046
- hit@10: 0.4, hit@20: 0.45
- 改善: IC 0.1467
