# 云聪交接：看图、行情接口与天气基础设施盘点

日期：2026-10-10（JST）。本次是有界只读盘点；未执行源脚本、未重跑测试、未改 Git、未读凭证。只写本说明和 `infra_inventory.json`。清单列出 **196 个推荐文件，共 11,249,811 字节（约 10.73 MiB）**，逐文件记录来源、大小、SHA256。迁移者仍需做最终内容扫描、运行验证和 Git 验收。

## 1. 必须取对版本

| 内容 | 正确本地来源 | 分支 / 提交 |
|---|---|---|
| 最新看图工具 | `C:\Project Rocket\Codex\TradingAgent\chart_tool` | `chart-tool-v2-20261009` / `5bd2261a1a97da1a6cb56bead77652c2c6d54d87` |
| v2 较早验收 | `C:\Project Rocket\outputs\chart_tool_v2_20261009` | 较早提交 `166bd69472bfe0e8cf17021c07568586325d978c`，部分交付 |
| 最新天气工具与发布资料 | `C:\Project Rocket\worktrees\tradingagent-claude-mrwu-20261005` | `codex/sentiment-cycle-20261005` / `d5ec591137d143d94cc1e3618c1c89629f80604b` |

看图源码范围 Git 状态干净；总控注明两次看图提交尚未 push。天气引擎范围干净；天气报告区有未跟踪的历史人工测试目录，已排除。天气最新资料不在主工作树，不能只从主树打包。

## 2. 看图工具可以最小迁移

`chart_tool/` 共 35 文件、658,116 字节，含 13 个测试文件。自带 `README.md` 与 `CONTRACT.md`。包内含日线适配、截止时刻契约、分钟归一化、日线/15 分钟/1 分钟/分时绘图、均价昨收线、像素核验、冻结案例与分离揭晓。不要连整个 TradingAgent 一起复制。

**推荐离线入口：** 保持 `chart_tool/` 为可导入包，在其父目录运行：

```powershell
python -m chart_tool chart 002025.SZ --cutoff '2026-09-18T10:07:30+08:00' --stage study --window 10y --last-bars 250 --snapshot-dir '<迁移后 chart_inputs>' --cache-dir '<可写新缓存>' --out '<新输出>/daily.png' --width 5600
python -m pytest chart_tool -q --basetemp '<新的临时目录>'
```

完整命令参数以随附 README/`--help` 为准。`chart_daily_snapshots` 9 文件、563,249 字节，含 002025.SZ、002326.SZ、600990.SH 的日线和复权因子、SSE 日历与来源证据，可支撑跨机器日线示例。快照是未复权日线，量原单位为手、额原单位为千元；适配器转成股和元。冻结截止后以前一已收盘日为复权锚点；还原与揭晓不能混在同一个输入。

**依赖：** Python 3.12、numpy、pandas、pyarrow、matplotlib、Pillow；测试加 pytest。观察到现有环境版本为 numpy 2.5.3 / pandas 2.3.3 / pyarrow 25.0.1 / matplotlib 3.11.2 / Pillow 12.3.0 / pytest 9.1.1。它们是本机观察值，不是新建立的兼容性保证。中文图还需要匹配 `RenderOptions.font_family` 的中文字体。

**真实多周期例子：** `chart_real_examples` 是 `outputs/chart_tool_minutes_enabled_20261009/charts/` 下完整 18 文件（两张 PNG、对应四周期视图、manifest、geometry、pixel、run），3,109,415 字节：

- 002025.SZ 截止北京时间 2026-09-18 10:07:30；67 根 15 分钟、37 根当日分钟。
- 航天电器截止北京时间 2026-01-09 09:22:33；盘前图展示此前五交易日 80 根 15 分钟和上一交易日 240 分钟。

两张例图可直接阅读；通过原始分钟入口完全重建还需分钟 CSV。JSON 视图已携带实际绘图数据，可由移植者编写薄入口调用 `chart_tool.render_multi` 重画，须明确这是重画已导出视图，不冒充重新取数验证。

**分钟复建注意：** 生产注册表 `chart_tool/minute_sources.json` 的路径指向 Harry 的 D 盘，移机必须改配置或用 `--minute-registry`/`CHART_TOOL_MINUTE_REGISTRY` 指向新注册表。241 行合并 09:30 与 09:31；240 行开始时间网格整体后移一分钟；均不单列竞价，警告 `AUCTION_UNAVAILABLE`。每次请求仍逐股日核 OHLC/量/额。不合格拒绝。

不要整体复制 `chart_tool_v2_20261009/inputs/minutes/`：19 文件共 **1,484,457,108 字节**，单个全市场 CSV 约 130 MB。可以另行导出十个股日的小样例，并记录原文件 SHA256、筛选条件、新文件 SHA256和明确样例源 ID；这一步本次未做。不要把截取的小样例沿用为全量原 CSV 的 SHA。

## 3. 验收状态必须原样传达

最新 `delivery_acceptance.json`/`REPORT.md` 记录：411 passed、0 failed、1 skipped；10 个股日逐日核对通过；PNG 像素与人工目检通过。Windows 符号链接权限导致一个测试跳过；Pandas/NumPy 有弃用警告。**这是既有验收证据，本次未重新运行。**

v2 的 `ACCEPTANCE.json` 仍应随包保留，结论是 `partial_delivery_not_accepted`（6 pass、4 fail、1 N/A）。后续补充只解决真实分钟源与两张图，并没有抹去所有遗留：verify_v2 读 rows 而产物叫 bars、09:25 到首根分钟间空面板、图头、2015 年前日线、HTML交互、历史名称/除权日盘中锚点等。默认 3840 宽可能裁掉涨跌幅后缀，实际交付用 5600。

`chart_case_store` 15 文件、1,479,965 字节，为真实 `yuncong_20260109_002025_000632` 的原帧/转写/输入及多版本冻结、揭晓材料。保留修订和先还原后揭晓语义。07 统一提及记录规格到批量出图的入口仍未做，不应称完整决策日志生产线已完成。

## 4. 天气标签的最小必要依赖

**只使用历史天气标签：** 直接提供 `docs/research/mkt_weather_v02_20261005/data/mkt_daily.csv`、`mkt_raw_daily.csv`、`mkt_calibration_v02.json`、`DATA_DICTIONARY.md`、`REPORT.md`、`METHOD_REVIEW_R4.md`。共有 424 个交易日，2025-01-02～2026-09-30。天气是当日状态描述，次日预报未显示实用优势，不作为买卖建议。

**重算派生表：** 迁移 28 文件的 `policyStudy/policy/market_sentiment/`，并保持相对于这个根目录的 `docs/research/mkt_weather_v02_20261005/` 布局。随附 `REPRODUCE.md` 给出完整命令：

```powershell
python -m policyStudy.policy.market_sentiment.run history --raw-daily docs/research/mkt_weather_v02_20261005/data/mkt_raw_daily.csv --calibration docs/research/mkt_weather_v02_20261005/data/mkt_calibration_v02.json --calendar docs/research/mkt_weather_v02_20261005/data/market_calendar.csv --start 20250102 --end 20260930 --output '<可写新输出>'
```

这条路线不需数据库、联网或令牌。三个读数、天气、日序列和诊断可用小表复建。图表加 matplotlib，卡片 PDF 加 reportlab，完整测试加 pytest/pypdf；表依赖 numpy/pandas/pyarrow。随包的字体带 OFL 许可证，应一起保留。

**只有需要本机原库重算时** 再加入 `speculation_sentiment_lite/production.py`、`score.py`、`config/formal_spec.json`、其 requirements 和旧报告 `docs/research/mrwu_sentiment_cycle_20261005/data/calibration.json`。然后通过 `--data-root` 读取稳定且停止写入的 basic/kpl/index 三库。无需复制整个旧情绪项目；不需要旧 backfill/restore 脚本。

R4 证据记录天气 82 tests、8 subtests、旧 F1 37 tests；工程公式验收和项目方法审阅完成。无独立人工标签，不称为预测有效性验证。自动更新为 `ready_not_enabled`：原库日历缺 2026-10-07，此处没有接定时器。

## 5. 行情基建说明与边界

`market_data_reference` 7 文件、141,024 字节，包含 `coreClient/docs/data_provider.md`、1 分钟 Parquet 设计、one/fifteen minute store 核心与测试及其依赖说明，仅作源码参考。store 仍依赖原仓库 `config.settings`/`data_safety`，不是拷走就能独立跑的工具；不应顺手复制本机配置或凭证。

总控文档规定研究取数一般用 `coreClient.data_provider`，但已交付 `chart_tool/data.py` 的原库路径实际通过只读 sqlite3 连接复用 `offline_db_client.get_day/get_adj_factor/get_tradecal`。这是已观察的实现差异，应写清。推荐快照路线避免把这一依赖扩成整仓库迁移。

加工数据目录、数据库全集、原始全市场分钟数据、`.venv`、缓存和测试临时目录不进入 Git 包。描述路径、范围、口径和样例即可。看图 1 分钟的 09:30 合并/后移规则与 15 分钟均线实验的额外 09:30 整行排除规则是两套用途，切勿合并。

本盘点不覆盖共享媒体平台、ASR、直播转写、双突破学习内容的清单；由对应材料盘点汇总。这里的天气/行情仅为云聪案例还原的基础设施依赖，不意味着恢复暂停的题材或策略研究。
