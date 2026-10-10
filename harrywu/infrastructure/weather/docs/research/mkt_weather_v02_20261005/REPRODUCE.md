# 复刻与日更

仓库根运行，Python 3.12，使用免费包 numpy、pandas、pyarrow、matplotlib；卡片PDF另需reportlab，测试另需pytest和pypdf。随附派生表路线不需要数据库、联网、令牌或模型。Windows现有环境为 `C:\Project Rocket\Codex\TradingAgent\.venv\Scripts\python.exe`。

## 路线一：从随附派生小表复刻

```powershell
python -m policyStudy.policy.market_sentiment.run history --raw-daily docs/research/mkt_weather_v02_20261005/data/mkt_raw_daily.csv --calibration docs/research/mkt_weather_v02_20261005/data/mkt_calibration_v02.json --calendar docs/research/mkt_weather_v02_20261005/data/market_calendar.csv --start 20250102 --end 20260930 --generated-at 2026-10-05T00:00:00+00:00 --output C:/ProjectRocketWeatherRebuild
python -m policyStudy.policy.market_sentiment.figures --daily C:/ProjectRocketWeatherRebuild/mkt_daily.csv --font docs/research/mkt_weather_v02_20261005/reproduction/MktSansSC.ttf --output C:/ProjectRocketWeatherRebuild
```

读数、天气、逐日预报、评价、截断和图表可全部重建。运行时间与生成时间、输入路径身份允许不同，计算值必须一致；每日表的 input_snapshot_id 区分读入已发布小表和原始事件的两条溯源路线。生产校准一直复用同一个文件。完全重新拟合2025标尺时去掉 `--calibration`，用完整424日小表；值应相同，但校准所记录的输入指纹随溯源路线变化。

## 路线二：从本地稳定 F1 事件或行情库计算

本轮使用 `D:\ProjectRocketData\processed\TradingAgent\outputs\speculation_sentiment\sentiment_cycle_20261005_01\formal_corrected` 的 events.parquet、cohorts.parquet、sentiment_daily.csv。发布 F1 日表与该目录日表的 SHA256 完全一致；错误的早期 formal 目录不作为正式事件来源。逐股明细留在本机，不入 Git。

```powershell
python -m policyStudy.policy.market_sentiment.run history --formal-dir '<formal_corrected目录>' --index-daily docs/research/mkt_weather_v02_20261005/data/index_daily.csv --calendar docs/research/mkt_weather_v02_20261005/data/market_calendar.csv --start 20250102 --end 20260930 --output '<新RUN目录>'
python -m policyStudy.policy.market_sentiment.run history --data-root 'D:/ProjectRocketData/live' --calibration docs/research/mkt_weather_v02_20261005/data/mkt_calibration_v02.json --start 20250102 --end 20260930 --output '<另一个新RUN目录>'
```

行情路线只读复用未改动的 F1 production/score/formal_spec 与旧固定校准。2024-11-01起既有预热用于前20市场日均额和昨日群体，没有更早数据下载。数据库必须无非空 WAL/journal，且读取前后文件状态一致。首次需读取历史及验证来源，后续以源状态检查复用缓存；历史修订重算后继，不拼接旧规则和新规则。

统一后的总库路径为 `D:/ProjectRocketData/legacy_C_Project_Rocket/Trading Agent Database`。只有独立数据统一任务完成旧F1的181日直接原库复算对账后才切换；不能对正在合并中的原库执行本路线。

## 今天与自动接入

```powershell
python -m policyStudy.policy.market_sentiment.run today --data-root '<已验证且停写的总库>' --calibration docs/research/mkt_weather_v02_20261005/data/mkt_calibration_v02.json --output '<持续天气输出目录>'
```

`today` 锁住派生目录，完成凭证为可选。未提供或文件不存在时，检查日历确定的预期已完成日、三库关键截止日相同、无未完成残留、文件静置180秒；通过即运行并在最新行末尾标“未附凭证”。行情未完成才停止，普通质量告警照常输出。已有凭证可加 `--upstream-receipt '<实际凭证.json>'`，仍核验成功状态和源快照。详细规则及本机真实限制见[自动接入合同](acceptance/DAILY_AUTOMATION.md)。未挂接时不要把手动重跑成功当自动运行成功，节后首日无人干预成功另记。

日表与摘要增加临界提示：仅检查当天决定天气的分支，距离严格小于3分才提示另一侧天气；等于边界时明确写“低于某值”，不改变原分类。下一交易日日期仍由统一交易日历提供，不单独推算。

## 项目内审与每日解释（当前入口）

2026-10-07起不再等待人工标注。history/today自动写出daily_explanations.csv；解释只使用当日已计算事实，不联网、不生成新标签。完整诊断命令：

```powershell
python -m policyStudy.policy.market_sentiment.run review --daily docs/research/mkt_weather_v02_20261005/data/mkt_daily.csv --output C:/ProjectRocketWeatherReview
```

生成424日解释、3816行分项贡献、分年相关性、八个单参数±3分诊断及变更日期。诊断不修改阈值、原日表或校准。方法判断见METHOD_REVIEW_R4.md，源资料见sources/public_methods_r4.json。

## 可选的历史人工参考工具

此前只请李老师一人，使用 `human/li10/packets/cards.pdf` 和同序 `Li_labels.csv`。用户指定10日随机打乱顺序，每页保留原10项事实及2025 P10/中位/P90，不附机器读数、天气答案、日期分组或临界提示。hit/cont/act为三个主要必填项；weather/notes选填。不确定可留空或整张跳过，程序按维度保留有效填写，另列未完成日。

```powershell
python -m policyStudy.policy.market_sentiment.run li-pack --daily docs/research/mkt_weather_v02_20261005/data/mkt_daily.csv --output '<新的李老师10日目录>'
python -m policyStudy.policy.market_sentiment.run evaluate-human --daily docs/research/mkt_weather_v02_20261005/data/mkt_daily.csv --li '<Li标签CSV>' --revision-ledger docs/research/mkt_weather_v02_20261005/human/li10/private_key/threshold_revision_ledger.json --output '<标注完成后的私有评估目录>'
```

逐读数报一致天数、已填天数、可比天数、缺失及同方向分歧。固定10日中至少7日一致记为“基本一致”，仅作参考，不设工程通过关卡；缺失不计不一致，也不缩小7/10的参考基数。同一读数至少3个可比日机器档位高于人，或至少3日低于人，且该读数修订次数为0，才允许总控依据证据调整该读数分档阈值一次。两个方向不能相加；机器缺失不构成方向证据。次数按hit/cont/act分别保存，评估本身不修改阈值或增加次数；当前均为0；此人工协议现在仅为可选历史工具。weather只作选填补充，不参与调整条件。

原 `human/packets/cards.pdf` 的15张随机卡片与提名入口保留为可选材料，无需填写或提名，也不纳入当前10日评估。历史Harry模板仅留档，不再需要第二位标注者；旧双人规则已由当前单人规则替代。

```powershell
python -m policyStudy.policy.market_sentiment.run human-pack --daily docs/research/mkt_weather_v02_20261005/data/mkt_daily.csv --nominations '<可选提名CSV>' --output '<新的可选材料目录>'
```

三个读数、天气、先验、锚点与权重在本轮保持不变。不要把机器答案目录交给尚未标注的人；程序不以旧聊天或AI代填标签。

## 验收入口

```powershell
python -m pytest policyStudy/policy/market_sentiment/tests -q
python -m policyStudy.policy.market_sentiment.acceptance --formal-dir '<formal_corrected>' --published-daily docs/research/mrwu_sentiment_cycle_20261005/data/formal_daily_2025_2026.csv --daily docs/research/mkt_weather_v02_20261005/data/mkt_daily.csv --calibration docs/research/mkt_weather_v02_20261005/data/mkt_calibration_v02.json --calendar docs/research/mkt_weather_v02_20261005/data/market_calendar.csv --output '<独立验收目录>'
```

实际前缀是先截断事件、群体、旧日表，再重跑新增计算；不是裁剪结果CSV。2025全年固定锚点的事后性明确保留。验收记录与源码/输入清单见 acceptance，状态见 STATUS.json。

如果本机系统 TEMP 的 pytest 目录无写权限，可为pytest加 `--basetemp <已创建父目录下的全新子目录>`。初版验证为32项测试加8个边界子用例；本次修订50项测试全部通过，另8个边界子用例和672组临界反算通过，JUnit保存50项测试记录。初版手动LIVE入口曾从通过文件状态校验的既有原始帧缓存完成端到端计算，后续同日复跑约1秒；新无凭证生产预检因现存残件/日历范围而明确停止（见修订审计），不将初版成功冒充新入口实盘通过；源数据修订后的完整读取耗时取决于磁盘和数据规模，未将缓存复跑时间说成冷读性能。

## 2026-10-07重新验收

当前最终测试计数和证据以acceptance/revision_r4_controller.json及revision_r4_tests.xml为准，上述初版/R2计数留作历史记录。旧F1本次另跑37项合成测试。总库181日score5/grade5已重新核验一致；原库today预检仍因日历无20261007而停。无需完成凭证，但也不猜测缺失日历。发布数据路线和显式快照today均可完整运行。
