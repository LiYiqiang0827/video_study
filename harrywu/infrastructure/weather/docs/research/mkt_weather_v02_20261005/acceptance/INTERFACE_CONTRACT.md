# MKT v0.2 接口冻结：20261005-01

依据 ASTRA_MKT_WEATHER_V02_EXECUTION_BRIEF_20261005.md 与同日第二版设计。主控拥有最终验收、配置、Git；所有执行者 gpt-6.1-sol / xhigh，最多3个并行。原库正在独立任务中更新，本轮只读稳定 F1 formal_corrected 派生文件与 LIVE；不写数据库、不调用外部Worker、不接题材、收益或交易。

工作树 C:/Project Rocket/worktrees/tradingagent-claude-mrwu-20261005。运行根 D:/ProjectRocketData/processed/TradingAgent/outputs/market_sentiment/mkt_weather_v02_20261005_01。OLD_RUN 为 D:/ProjectRocketData/processed/TradingAgent/outputs/speculation_sentiment/sentiment_cycle_20261005_01/formal_corrected。旧发布表 docs/research/mrwu_sentiment_cycle_20261005/data/formal_daily_2025_2026.csv 与 OLD_RUN/sentiment_daily.csv 字节一致。不得选择未修正 formal。

所有日期对外 ISO YYYY-MM-DD，trade_date唯一、升序。函数纯计算接收 DataFrame，不隐式读全局路径。UTF-8、CSV保留精度（读取 float_precision='round_trip'）。JSON严格有限值或null。新指标均mkt_前缀。共享接口变更先报主控；各Agent不得改别人文件。

## W01 adapter.py

`build_raw(events, cohorts, formal_daily, calendar=None) -> DataFrame`：events和cohorts为F1最终输出，formal_daily为F1正式表（score5/M1raw已用固定2025校准）。calendar可为ISO日期列表；默认formal_daily日期。保留每日日行，事件缺失不能造0；完整无事件则真实0。可公开 `load_formal(formal_dir)` 便利函数返回 `(events, cohorts, daily)`。

保留旧所有字段（用于证据），新增以下映射：mkt_eligible_n=N, mkt_up_n=U, mkt_down_n=D, mkt_broken_n=Z, mkt_turnover_cny=amount, mkt_ma20_cny=amt_ma20, mkt_ratio20=ratio20, mkt_log_ratio20=lr20, mkt_udens=Udens, mkt_ddens=Ddens, mkt_m1raw=M1raw, mkt_ladder=ladder, mkt_max_height=max_h, mkt_relay_score=score5, mkt_relay_grade=grade5, mkt_f1_m1=M1。mkt_source_quality/mkt_source_reasons保留旧quality_status/reasons。

新增mkt_advance_n/mkt_advance_pct（今日合格cC>cP，平盘不计）；mkt_height_1_n到mkt_height_5_n与mkt_height_6plus_n。群体组`all`为全部昨日合格涨停，`chain`为prev_height>=2，不按今日eligible筛选。每组字段mkt_{g}_original_n/observed_n/paused_n/missing_n/unobservable_n/drop_k/drop_rate；observed为mid_obs；unobservable为found且非mid_obs；大跌(cC*20<=cP*19)|Dn并集。保存mkt_promo_h{1..4}_k/n沿用k_h/n_h，4为>=4。指数涨跌由root独立补mkt_index_*字段。字段映射JSON和事件输入sha+源不修改证据另交。

## W02 readings.py（主控实施）

`calibrate(raw, input_fingerprint='') -> dict`; `compute_readings(raw, calibration, generated_at=None) -> DataFrame`; `clipping_statistics(daily, calibration) -> DataFrame`。calibration独立，不修改F1。配置根拥有，a5，2025线性P10/P90；新all/chain大跌先验2025累计k/n。九项key：all_drop, chain_drop, ddens, m1raw, ladder, max_height, log_ratio20, advance_pct, udens。输出mkt_{metric}_value/q/clip_low/clip_high/endpoint_low/endpoint_high/constant_anchor。前三组权重 .40/.35/.25、.40/.30/.30、.40/.30/.30；正向，缺项本读数内分配。mkt_hit/cont/act、mkt_{reading}_available_weight/quality/reasons，mkt_quality_status/reasons，mkt_weather/weather_label/weather_reason、mkt_extreme，方法/校准/输入/阈值版本。

## W03 forecast.py

`add_forecasts(daily, calendar=None) -> DataFrame`; `evaluate_forecasts(daily, start='2026-01-01', end='2026-09-30') -> (summary:dict, detail:DataFrame)`; `transition_table(daily) -> DataFrame`。calendar包含交易日lookahead，若不足目标留空并写原因；不得猜下一自然日。列forecast_origin_date/forecast_target_date以及mkt_forecast_{code}_count/prob，mkt_forecast_n/top1/status，mkt_baseline_persistence/mode_known。代码固定sunny,cloudy,overcast,thunder,storm。每个t先累计t-1→t，仅相邻市场日且两端有效，再输出本日概率；n0空概率，n1..9标不足。并列先今日天气后固定序。历史众数含今日，固定同规则；全期众数只在evaluate计算，明确ex_post。2026按目标日期共同分母四法比较，n小不剔除。返回summary需methods内各method的n/hits/accuracy、exclusions、sample_small_n、conclusion；detail每目标日真实/四预测/evaluable/reason。

## W04 当前人工参考接口：R3 / 20261005

`li_packet.create_li_packet(daily, output_dir, seed=20261005)`：固定用户指定10日，顺序独立于读数打乱，生成10页盲卡和Li单人空表，机器答案单独存private_key。hit/cont/act三档主要必填，weather和notes选填，空白或跳过保留缺失。新入口li-pack。

`li_review.evaluate_li_labels(daily, li_path, output_dir, threshold_revision_counts=None)`：固定10日；逐读数报一致天数与可比/缺失数，至少7/10为基本一致，reference_only=true。机器高于人/低于人分别计数，一方向至少3日且该维度修订次数为0才adjustment_allowed；次数hit/cont/act各0或1，不自动修改阈值。空表awaiting_labels、部分填写partial_labels、已填review_recorded；不设双人共同分母或整体pass门槛。

`human.create_packets(daily, output_dir, nominations=None, seed=20261005)`保留随机/提名的可选功能，不再要求补足或第二人填写。旧验收JSON及sources记录旧提交，不是当前协议。模拟标签只放RUN/tests新目录。

## W05 run.py / figures.py / 文档（字段稳定后派发）

主控冻结history/today/human-pack/li-pack/evaluate-human公共入口后派发。发布小型mkt_raw_daily.csv可独立重建读数/预报/图，无数据库、无需网络；原库路线只读复用F1。今天入口不得把陈旧日说成今日，无上游完成证据不挂自动调度，仍可手动打印截至最新已完成日摘要。原库合并进行中不读取混合快照。

## 执行合同共同条款

各包任务书写入RUN/tasks，完成JSON包含task_id/status/input_version/output_paths/changed_files/checks/known_issues/next_owner。目标是交付与少量实质测试通过立即返回；交付期限为本次连续执行期内，异常先带具体输入/测试证据报告，不用任意10/20分钟截断。相同失败最多两次定向修复后交root。子Agent不得改参数/扩大写入范围/推Git/宣布最终通过。根验收亲查源、边界、真实前缀、复跑、直接日期、报告数字与图片。人工未到位不阻塞其余工程。
