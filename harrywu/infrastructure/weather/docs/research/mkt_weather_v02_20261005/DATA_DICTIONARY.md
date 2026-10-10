# 字段字典

所有CSV UTF-8。trade_date为市场交易日ISO日期，每日一行。mkt_raw_daily.csv保留F1源字段作为复用证据；mkt_daily.csv包含它们及新指标。空值表示缺观测，真实零事件保留0。

| 字段 | 定义与单位 |
|---|---|
| method_version / threshold_version | 新方法版本、初版天气切点版本 |
| calibration_version / mkt_calibration_fingerprint | 2025固定校准版本、JSON内容指纹 |
| input_snapshot_id / generated_at / data_cutoff | 输入身份、UTC生成时刻、实际计算截止日 |
| mkt_eligible_n / up_n / down_n / broken_n | 当日F1合格股票数、涨停、跌停、炸板；单位只 |
| mkt_advance_n / advance_pct | 合格股票收盘分价>前收分价的家数及占比，平盘不算上涨 |
| mkt_all_* / mkt_chain_* | 昨日全部合格涨停、昨日高度≥2涨停群体；不按今日身份删人 |
| *_original_n / observed_n / paused_n / missing_n / unobservable_n | 原群体、mid_obs可观测、停牌、缺失、已找到但不可观测数量 |
| *_drop_k / drop_rate | 收盘<=前收×95%或跌停的并集数与原始k/n，n0时比例为空 |
| mkt_promo_h1_k/n 至 h4_k/n | 昨日1/2/3/≥4高度群体晋级数及可观测数，沿用F1 |
| mkt_height_1_n 至5_n / 6plus_n | 当日合格涨停在1/2/3/4/5/≥6高度档数量 |
| mkt_m1raw / ladder / max_height | 原F1分高度平滑晋级率、非空高度档数÷6、最高板 |
| mkt_turnover_cny / ma20_cny / ratio20 / log_ratio20 | F1全沪深A成交额元、此前20交易日均额、比值、自然对数；均额不含今日 |
| mkt_udens / ddens | 每1000只当日合格股的涨停/跌停数，无额外平滑 |
| mkt_index_sh_pct / sz_pct / cyb_pct | 本地指数库上证/深证/创业板日涨跌，单位百分数（1表示1%） |
| mkt_{九项}_value | 平滑后、标准化前的量；仅all_drop和chain_drop新增a5平滑 |
| mkt_{九项}_q | 2025线性P10/P90固定正向标准化0–100；常数锚点为50 |
| *_clip_low / clip_high | 严格低于P10或高于P90，等于边界不算超界 |
| *_endpoint_low / endpoint_high | 得分恰为0或100，包含边界和超界 |
| *_constant_anchor | P10=P90且当前值有效 |
| mkt_hit / cont / act | 挨打/延续/活跃，先逐项Q再固定权重合成，不再二次Q |
| mkt_{读数}_available_weight / quality / reasons | 实际可用原始权重、OK/DEGRADED/UNAVAILABLE、缺项原因 |
| mkt_quality_status / reasons | 三读数与源/适配器质量汇总；降级有效日仍进统计 |
| mkt_weather / weather_label / weather_reason | 五类稳定代码、中文、无法判定原因；缺失不是第六类 |
| mkt_extreme | ddens≥2025 P99，仅标签，不改变分类或概率 |
| mkt_relay_score / relay_grade / f1_m1 | 旧score5/grade5/M1原值，仅对照 |
| forecast_origin_date / target_date | 收盘后预报时点与下一市场交易日；无未来日历则目标空 |
| mkt_forecast_{code}_count / prob | 当时已完成的同起点天气转移次数及经验频率，n0时概率空 |
| mkt_forecast_n / top1 / status | 五类计数和、最大概率类、小样本/无样本/正常状态 |
| mkt_baseline_persistence / mode_known | 明日同今日、截至当日已知历史众数 |
| mkt_summary | 不附交易建议的一行摘要 |
| mkt_borderline / borderline_hint / borderline_details_json | 当前决定天气的分支距阈值严格小于3分时为真；提示方向、距离及另一侧天气，JSON保留完整精度；不改变分类 |
| mkt_upstream_receipt_status | 仅today运行输出：最新日attached或not_attached；无凭证通过本地检查后摘要末尾标“未附凭证”，历史日期为空 |

九项为all_drop、chain_drop、ddens、m1raw、ladder、max_height、log_ratio20、advance_pct、udens。五类代码固定sunny晴、cloudy多云、overcast阴、thunder雷阵雨、storm暴雨。精确旧字段映射见 data/adapter_field_mapping.json。forecast_evaluation_daily.csv以目标日排列，四方法共用evaluable分母；mode_ex_post只用于事后参照。clipping_statistics按年份和组成项区分严格截断与端点。

## R4解释与诊断副表

原mkt_daily.csv列与值不变。data/review_r4/daily_explanations.csv以trade_date一对一关联，provenance声明为总控编写、程序确定性生成；含三维事实、广度分歧、受罚与活跃并存、小群体及端点说明。component_contributions.csv每日期×九项保存名义/有效权重、Q值和贡献；correlations.csv按2025/2026分别给pairwise分母与Pearson相关。threshold_sensitivity.csv按年度/参数/±3分列可比天数、天气变化和可用性变化；threshold_changed_dates.csv保留每次变化明细，不把重复日期求和当唯一日期。review_summary.json记录版本、输入范围及未调参声明。
