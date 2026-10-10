# V2-X1 分钟来源核实

结论：两类文件可定位和复现，但均仅为候选，不能据本包启用生产。注册表严格保留字段，priority=[]；bar_end 是任务schema要求的未核实占位，has_auction_row=true仅标CSV有09:30行，绝非纯竞价证明。来源/禁用理由见 measurements.json#production_decision 与 minute_sources.json#sources。

Q1：原始CSV为按日全市场、按年度目录，2009-01-05—2026-09-18；单日模板 {yyyy}/{yyyymmdd}_A股1分钟K线@余哈哈的数据仓库 原创整理.csv。18文件实读为UTF-8，列为证券代码/证券名称/交易日期/分钟时间/开盘价（元）/最高价（元）/最低价（元）/收盘价（元）/成交量/成交额/复权因子。边界仅证明文件名存在，未验全股覆盖。TDX原生download_one_minute是另一OHLC来源，文档记最近约100交易日深度、不保证历史；本包未联网注册。证据：measurements.json#inventory、#source_reads；coreClient/docs/tdx_client.md#download_one_minute。

Q2：18文件、27股日、两股覆盖2018—2026，均有09:30及完整240连续分钟。09:30.open−日线开盘全为0；09:30.close−开盘最小−0.45、中位0、最大+0.72元；仅12/27收盘相等，15/27行OHLC不恒等，最大高低区间1.43元。2026-09-18航天电器该行O74.10/H74.45/L73.71/C73.95。因此不能证实纯竞价或bar_end，更不能把该行所有量额当09:25已知。证据：measurements.json#distributions、#stock_days；SNAP/csv_lineage_20260918.json#auction_rows。

Q3：09:30加09:31—15:00，量相对差范围−1.15e−16至1.37e−16、中位0；额相对差−4.91e−9至7.91e−9、中位0。日线量×100、额×1000。证据：measurements.json#distributions、#stock_days.full_day_including_0930；coreClient/docs/data_provider.md#get_day。

Q4：末价差中位0、26/27为0，例外600990.SH/2018-06-27为−0.06元；全天高低差各26/27为0，002025.SZ/2020-06-24高−0.01、低+0.03元。两日不通过0.005价格门，不能用放宽容差掩盖。证据：measurements.json#stock_days、#objective_checks（25/27通过全天数值门）。

Q5：CSV量单位股、额单位元，依据27股日总量与日线手×100一致、额与千元×1000近乎一致；价格为未复权，与含除权日原始日线核对。不能把旧price/vol分时表的手单位套用。证据：measurements.json#distributions、#exdates；SNAP/minute_overlap_20260918.json；docs/acceptance/DATA_DICTIONARY_SCHEMA_V1.md#policy_minute.db。

Q6：oneMinute为按日全市场Parquet，目录year=/month=/trade_date=/part-000.parquet；catalog记4304日期（2009-01-05—2026-09-18），不证明逐文件完整。2021-06-25、2026-01-08、2026-09-18航天电器的240根OHLCVA/因子与原CSV删除09:30后逐项完全一致，均缺09:30。旧manifest无逐股source_lineage；不能把来源标签猜为TDX。证据：measurements.json#processed_comparisons、#catalog；offlineDataManager/docs/代码详细设计/one_min_parquet.md#数据源切换与更新/#查询。

Q7：是，2026年1月和9月CSV都已解压。CASE-1真实最近5日为2025-12-31、2026-01-05/06/07/08；REG-1为2026-09-14至18。对应CSV均已实读并哈希，抽取1/8与9/18加工分区也存在。没有打开压缩包或解压。证据：fixture_plan.json；measurements.json#fixture_dates/#source_reads/#processed_comparisons。

Q8：保留默认量/额相对差0.005，价格/竞价开盘绝对差0.005元。样本量只涉及两股，不能据近乎零的量额误差宣布全源阈值已经校准；0930.close不等于官方开盘是语义冲突，数值容差不能解决。证据：measurements.json#thresholds。

夹具：REG/CASE/SH/OLD/EXDATE/NOAUCTION全部列实路径、完整文件SHA256，共13唯一文件。EXDATE是002025.SZ/2018-06-27，因子6.392→6.461；全部标生产禁用，适于原样研究和故障测试，不能宣称真实竞价验收通过。证据：fixture_plan.json；measurements.json#exdates。

边界与缺口：W0无解压进程。单进程串行，D盘25文件、30选取股日，目录不超两层，源文件前后stat一致；未改原库、未全市场研究、未安装依赖。仅元数据查询catalog使用DuckDB read_only=True。10月6日数据字典在指定文档检索未找到；data_safety实际在offlineDataManager/scripts/config。时间戳/纯竞价证据仍缺，主链须按部分交付处理。证据：measurements.json#disk_gate/#read_budget/#documentation_search/#unresolved；probe.py。
