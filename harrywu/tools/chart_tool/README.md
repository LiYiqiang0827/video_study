# 看图工具 v2 使用说明

工具按明确的截止时刻生成日线或多周期图，并保存 PNG、视图 JSON、绘图清单、几何记录、像素记录和 run 回执。价格按固定锚点前复权换算，成交量和成交额保留真实数量。还原案例只使用截止时刻可见的信息；揭晓结果单独写入结果区，不判定交易成败。

## 日线图

`chart` 默认自动加宽画布，尽量让每根 K 线至少占 3 像素；默认上限为 9,600 像素。`--width` 设置基础宽度，`--no-auto-width` 关闭自动加宽。`--price-scale log` 启用对数坐标。`--last-bars N` 在完成截止过滤后只保留最后 N 根显示和导出；`--zoom-start` / `--zoom-end` 按日期裁剪显示区间。裁剪不会重新读取截止点之后的数据。

```powershell
& '.\.venv\Scripts\python.exe' -m chart_tool chart 600990.SH `
  --cutoff '2026-09-30T15:00:00+08:00' --stage study --window 10y `
  --price-scale log --last-bars 250 `
  --snapshot-dir 'C:/Project Rocket/outputs/chart_tool_development_20261009/inputs' `
  --cache-dir 'C:/Project Rocket/outputs/chart_tool_v2_20261009/cache' `
  --out 'C:/Project Rocket/outputs/chart_tool_v2_20261009/daily.png'
```

十年日线也可使用 `--window 5y` 或 `--window all`。`--ma 5 20` 可叠加均线。查看当前命令完整参数：

```powershell
& '.\.venv\Scripts\python.exe' -m chart_tool chart --help
```

## 多周期图

`chart-multi` 通过分钟来源注册表选择数据源。`--minute-registry` 显式指定注册表；未指定时使用配置的默认注册表。`--minute-sessions N` 限制分钟面板显示的交易日数，`--daily-bars N` 限制组合图中日线面板显示的根数，`--period` 可单独展示 `daily`、`15m`、`1m` 或 `intraday`。15 分钟线由 1 分钟数据和可用竞价记录独立聚合；分钟视图会保留 `bar_end`、均价和昨收元数据（旧版视图除外）。

```powershell
& '.\.venv\Scripts\python.exe' -m chart_tool chart-multi 002025.SZ `
  --cutoff '2026-09-18T10:07:30+08:00' --stage restore `
  --cutoff-evidence '用户指定截止时刻：2026-09-18 10:07:30' --window 10y `
  --minute-registry 'C:/Project Rocket/Codex/TradingAgent/chart_tool/minute_sources.json' `
  --minute-sessions 5 --daily-bars 250 --period all `
  --snapshot-dir 'C:/Project Rocket/outputs/chart_tool_development_20261009/inputs' `
  --cache-dir 'C:/Project Rocket/outputs/chart_tool_v2_20261009/cache' `
  --out 'C:/Project Rocket/outputs/chart_tool_v2_20261009/multi.png'
```

生产注册表的 `priority` 已启用 `csv_1min_original`。按 2026-10-09 用户指定规则，完整 241 行（09:30 加标准结束时间网格）把 09:30 的开盘、最高、最低、成交量及成交额并入 09:31，收盘取 09:31；完整 240 行（09:30—11:29、13:00—14:59）整体后移一分钟。两种情况均不单列竞价，保留 `AUCTION_UNAVAILABLE`。每个股日仍核对开高低收、成交量及成交额，失败即拒绝；旧缓存自动重建。组合图日线末根为截止时已观察分钟合成的未完成柱，不能与全天日线比较；日线面板可显示最后 250 根，导出视图保留完整区间。

## 案例还原与 Amendment 3 字段

流程是先生成 `stage=restore` 图和记录，再冻结记录，最后揭晓。输入引用使用案例库内 `inputs/` 的相对路径；冻结后不可修改。下面四个新增字段是合成格式示例，所有文字均为 `SYNTHETIC`，不得直接作为真实直播内容：

```json
{
  "mention_type": "retrospective",
  "narrated_actions": [{
    "action": "watch",
    "tense": "past",
    "bar_ref": {"trade_date": "2096-01-02", "period": "daily", "basis": "inferred", "note": "SYNTHETIC: example bar reference"},
    "price_text": null,
    "stated_reason": "SYNTHETIC: example reason",
    "stated_expectation": "SYNTHETIC: example expectation",
    "stated_outcome": null,
    "evidence_refs": ["inputs/transcript.txt"],
    "segment_ids": ["SYNTHETIC-SEG-001"],
    "verification": "transcript_unverified"
  }],
  "frame_facts": [{
    "field": "indicator",
    "value_text": "SYNTHETIC: example indicator observation",
    "frame_ref": "inputs/synthetic_frame.png",
    "region": "SYNTHETIC: example chart region",
    "read_by": "human",
    "verified": false
  }],
  "frame_window": {
    "period": "daily",
    "first_visible_trade_date": "2095-12-01",
    "last_visible_trade_date": "2096-01-02",
    "bar_count_estimate": 20,
    "tolerance_bars": 2,
    "basis": "visual_estimate",
    "evidence_ref": "inputs/synthetic_frame.png",
    "frame_chart_region_px": null,
    "note": "SYNTHETIC: example visible window"
  }
}
```

`case-reveal` 使用 `fixed_horizon_v1`：锚定还原阶段因子，以提及时段规则确定参考价，计算第 1、3、5、10、20 个交易日的收盘收益、区间最高有利变动和最大不利变动，并记录首个可见、区间高低和最后可见事件。它不判定成败，也不计算胜率。结果覆盖不足时相应 horizon 保持未完成。

```powershell
& '.\.venv\Scripts\python.exe' -m chart_tool case-draft --store '<案例库>' --record '<记录.json>'
& '.\.venv\Scripts\python.exe' -m chart_tool case-freeze --store '<案例库>' --case-id '<案例编号>' --revision 1 --expected-sha256 '<case-draft 返回的 draft_bytes_sha256>'
& '.\.venv\Scripts\python.exe' -m chart_tool case-reveal --store '<案例库>' --case-id '<案例编号>' --revision 1 `
  --observed-through '2026-09-30T15:00:00+08:00' --snapshot-dir '<日线快照>' --cache-dir '<缓存>'
```

`case-reveal` 退出码：`0` 表示揭晓完成或没有待处理案例，`3` 表示至少一个案例失败，`2` 表示命令级错误。单例失败不会阻止其它案例继续处理。

## 揭晓图与核验

只有 Amendment 3 揭晓结果可以绘制 `case-reveal-chart`。图中价格继续使用还原锚点，并标出参考价和已完成 horizon。

```powershell
& '.\.venv\Scripts\python.exe' -m chart_tool case-reveal-chart `
  --store '<案例库>' --case-id '<案例编号>' --revision 1 `
  --observed-through '2026-09-30T15:00:00+08:00' --window 10y --last-bars 250 `
  --snapshot-dir '<日线快照>' --cache-dir '<缓存>' --out '<揭晓图.png>'
```

图表命令会自动运行简化像素核验，并将完整像素报告写入 `.pixel.json`。像素核验失败时命令退出码为 `4`；参数或数据错误退出码为 `2`。V2 独立核验不导入渲染、分钟聚合、揭晓计算或 `pixel_check.py`：

```powershell
& '.\.venv\Scripts\python.exe' -m chart_tool.verify_v2 `
  --targets 'C:/Project Rocket/outputs/chart_tool_v2_20261009/W5/verify_targets.json' `
  --snapshot-dir 'C:/Project Rocket/outputs/chart_tool_development_20261009/inputs' `
  --out 'C:/Project Rocket/outputs/chart_tool_v2_20261009/W7/verify_v2_checks.json'
```

全部目标通过时退出码为 `0`，任一核验失败时为 `1`。旧 F2 核验入口仍可按包方式运行：

```powershell
& '.\.venv\Scripts\python.exe' -m chart_tool.verify `
  --snapshot-dir 'C:/Project Rocket/outputs/chart_tool_development_20261009/inputs' --output-dir '<核验输出目录>'
& '.\.venv\Scripts\python.exe' -m chart_tool.verify_multi `
  --snapshot-dir 'C:/Project Rocket/outputs/chart_tool_development_20261009/inputs' --output-dir '<核验输出目录>'
```

## 已知限制与性能记录

除权日盘中如因子未被证明与前一交易日相同，会报 `FACTOR_UNAVAILABLE`。2015 年以前的日线尚未补齐。当前没有 HTML 输出，也不做 OCR。揭晓路径只描述固定口径下的后续数据，不判断案例成败。

生产分钟源已启用；本轮两张真实组合图、10 股日逐日核对与测试记录位于 `C:/Project Rocket/outputs/chart_tool_minutes_enabled_20261009/`。全历史覆盖尚未逐日验证，运行时继续按原阈值拦截数值不符的数据。W5 旧 F2 图及 `V2/W7/verify_v2_checks.json`、`V2/L4/performance.json` 保留当时状态，不能作为本轮启用结果。600990.SH 十年日线自动加宽后三次同进程完整 PNG 中位数为 0.919 秒，读、换算、绘制、编码分别记录在性能 JSON。
