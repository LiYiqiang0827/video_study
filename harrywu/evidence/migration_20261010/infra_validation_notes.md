# 新仓库基础设施迁移验证

日期：2026-10-10（JST）。仅验证明确可离线复现的路径；未改源码、未改写测试或映射旧夹具，未读源库或 D 盘，未调用外部模型。

- 日线烟测：新仓库 chart_inputs + 新仓库 chart_tool 成功，250 根日线，5600×1600 PNG，像素检查通过，已目检。截止盘中时只显示到 2026-09-17，保留 CALENDAR_UNAVAILABLE 警告。
- 独立测试：50 passed、11 deselected、68 条既有弃用警告。原411 passed/1 skipped属于历史验收，没有重新跑完整套件；旧测试的固定路径与夹具依赖已在 QUICKSTART 说明。
- 天气：新仓库派生 CSV 重算424日，350列比较通过；仅排除 generated_at、data_cutoff、input_snapshot_id，数值容差 rtol=1e-12/atol=1e-10。
- 源码完整性：看图35文件 + 天气28文件共63文件 SHA256 与盘点一致。
- 唯一新增仓库文件：harrywu/docs/QUICKSTART.md。通用 Python 代码块语法检查通过；依赖重新安装和其他操作系统未测试。

完整记录见 infra_validation.json；天气逐列比较见 weather_comparison.json；测试见 chart_subset_junit.xml / chart_subset_tests.log；出图见 chart_smoke/daily.png。
