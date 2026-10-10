# 大盘天气预报 v0.2

每天分别测量强势股挨打、延续、全市场活跃，按固定规则形成五种天气；经验转移频率用于次日描述性预报。旧 F1 五模块分原样保留为 `mkt_relay_score`。2025 是回顾性标尺期，2026 已被项目研究使用。入口不调用 C0、题材、策略收益或交易。

详细发现见 [报告](../../../docs/research/mkt_weather_v02_20261005/REPORT.md)，两条重建路线见 [复刻说明](../../../docs/research/mkt_weather_v02_20261005/REPRODUCE.md)。在仓库根运行：

```powershell
python -m policyStudy.policy.market_sentiment.run history --raw-daily docs/research/mkt_weather_v02_20261005/data/mkt_raw_daily.csv --calibration docs/research/mkt_weather_v02_20261005/data/mkt_calibration_v02.json --calendar docs/research/mkt_weather_v02_20261005/data/market_calendar.csv --start 20250102 --end 20260930 --output <独立输出目录>
python -m policyStudy.policy.market_sentiment.run today --data-root '<安全停写的行情库目录>' --calibration docs/research/mkt_weather_v02_20261005/data/mkt_calibration_v02.json --output <持续派生目录>
```

手动 `today` 明示数据截止日；休市显示最新完成日。日历覆盖不足、上游仍写入或失败会明确标记；不得将旧快照称为今天。日表按日期重建后原子替换，不追加重复行；来源快照变更时保守重算历史和后继，记录历史差异。固定校准不随更新改变。

完成凭证可选。未提供（含指定路径尚不存在）时，入口检查沪深日历推导的预期已完成交易日、三库六个关键截止日、未完成残留文件、数据库静置至少180秒及读取前后状态；全部满足即运行，行尾标“未附凭证”。质检降级仅报警，不拦截有效计算。已存在但失败或不匹配的凭证仍报错。每日自动接入单独验收；保留上游互斥机制，不要求数据任务额外产出凭证。数据统一任务尚未安全交接时，仅交付入口，不启用计划任务。

一行摘要含临界提示：当前生效分支中任一读数距边界严格小于3分时，说明跨到另一侧的天气；仅描述，原分类不变。盲标包提供每日单页PDF和HTML，10项事实旁附2025 P10/中位/P90，机器答案隔离。

```powershell
python -m pytest policyStudy/policy/market_sentiment/tests -q
python -m policyStudy.policy.market_sentiment.run li-pack --daily <mkt_daily.csv> --output <李老师10日目录>
python -m policyStudy.policy.market_sentiment.run evaluate-human --daily <mkt_daily.csv> --li <Li_labels.csv> --output <评估目录>
```

权重、天气阈值、a=5、2025线性分位与当前机器三档固定在config/mkt_spec_v02.json，本轮未修改。人工参考只使用用户指定10日及李老师标签：逐读数至少7/10天一致记为基本一致，无硬关卡。同一读数至少3日同方向不一致，且该读数未修订，才允许总控调整其分档阈值一次；评估不自动调参。空白或跳过单列，weather/notes选填。原15张随机卡片与human-pack提名入口保留为可选，无需填写。

## R4当前运行方式

2026-10-07已按用户授权结束人工等待，以公开方法对照和项目内审接受描述性定义。上述人工工具保留可选。history/today随结果输出daily_explanations.csv，逐日显示实际分子分母、指数与广度分歧、小群体及标尺饱和。

```powershell
python -m policyStudy.policy.market_sentiment.run review --daily <mkt_daily.csv> --output <诊断目录>
```

review另生成分项贡献核验、分年相关性和四个天气切点各±3分的敏感性诊断；不择优，不改历史分数或阈值。方法判断与已披露边界见报告目录METHOD_REVIEW_R4.md。
