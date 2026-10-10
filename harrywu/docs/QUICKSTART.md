# 运行 HarryWu 的离线看图与天气示例

这些步骤使用本分支随附的小型行情快照和天气派生表。无需连接 Harry 的数据库、访问 D 盘、获取令牌或调用模型。所有例子都从显式输入读取，并把缓存和新产物写到新的输出目录。

## 本次实际验证结果

2026-10-10（JST），从本分支迁移后的目录执行，使用现有 Python 3.12 环境：

- 日线示例成功：002025.SZ，截止北京时间 2026-09-18 10:07:30，显示截至 2026-09-17 的最后 250 根已完成日线；PNG 5600×1600，自动像素核验通过并经目检。
- 独立契约、合成分钟和像素测试：**50 passed，11 deselected**。68 条 NumPy/Pandas 时间差弃用警告保留。
- 天气从随附小表重算：**424 日，2025-01-02～2026-09-30**；与随附发布日表比较 350 列全部一致，数值容差 `rtol=1e-12, atol=1e-10`。只排除生成时间、数据截止描述和输入来源指纹三列。

历史完整看图验收为 411 passed、1 skipped；**本次没有重新执行完整套件**。完整测试依赖原机器上固定路径的测试夹具，不能把历史计数当成本次跨目录独立验收。参考 [原验收](../evidence/chart_minutes_20261009/delivery_acceptance.json) 和 [看图使用说明](../tools/chart_tool/README.md)。

## 环境

使用 Python 3.12。基础包为 `numpy pandas pyarrow matplotlib Pillow`；测试加 `pytest`。天气图表也用 matplotlib；可选历史卡片 PDF 另需 reportlab、完整天气测试另需 pypdf。本次没有重新安装依赖；观察到的环境版本如下，可作为复现参考：

| 包 | 实测版本 |
|---|---|
| numpy | 2.5.3 |
| pandas | 2.3.3 |
| pyarrow | 25.0.1 |
| matplotlib | 3.11.2 |
| Pillow | 12.3.0 |
| pytest | 9.1.1 |

新环境可按上述依赖准备独立虚拟环境；新安装和其他操作系统没有在本次验收中覆盖。图表默认字体是 **Microsoft YaHei**，实测为 Windows；其他机器应准备该中文字体，或在 Python API 的 `RenderOptions.font_family` 中指定已安装的中文字体。

## PowerShell：日线、天气与独立测试

在 `video_study` 仓库根目录运行。下列 `$py` 使用当前已准备好的 Python；如果环境未激活，把它改成解释器的完整路径。

```powershell
$repo = (Get-Location).Path
$py = (Get-Command python).Source
$bundle = Join-Path $repo 'harrywu'
$out = Join-Path $env:TEMP ('yuncong-harrywu-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $out | Out-Null
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
$env:MPLCONFIGDIR = Join-Path $out 'matplotlib'

Push-Location (Join-Path $bundle 'tools')
& $py -B -m chart_tool chart 002025.SZ --cutoff '2026-09-18T10:07:30+08:00' --stage study --window 10y --last-bars 250 --snapshot-dir (Join-Path $bundle 'examples/chart_inputs') --cache-dir (Join-Path $out 'chart/cache') --out (Join-Path $out 'chart/daily.png') --width 5600
if ($LASTEXITCODE -ne 0) { throw '日线示例失败，请查看命令输出' }

& $py -B -m pytest chart_tool/test_render_options.py chart_tool/test_pixel_check.py chart_tool/test_minute_source.py -q -k 'not real and not dense_checker' --basetemp (Join-Path $out 'pytest_tmp') -o ('cache_dir=' + (Join-Path $out 'pytest_cache'))
if ($LASTEXITCODE -ne 0) { throw '独立测试失败，请查看命令输出' }
Pop-Location

Push-Location (Join-Path $bundle 'infrastructure/weather')
& $py -B -m policyStudy.policy.market_sentiment.run history --raw-daily docs/research/mkt_weather_v02_20261005/data/mkt_raw_daily.csv --calibration docs/research/mkt_weather_v02_20261005/data/mkt_calibration_v02.json --calendar docs/research/mkt_weather_v02_20261005/data/market_calendar.csv --start 20250102 --end 20260930 --generated-at '2026-10-05T00:00:00+00:00' --output (Join-Path $out 'weather')
if ($LASTEXITCODE -ne 0) { throw '天气重算失败，请查看命令输出' }
Pop-Location

Write-Output ('结果目录：' + $out)
```

本机验收实际使用的解释器为 `C:\Project Rocket\Codex\TradingAgent\.venv\Scripts\python.exe`；迁移产物放在 `C:\Project Rocket\outputs\yuncong_github_handoff_20261010\validation`。上方命令的模块、参数和测试选择已经执行验证，示例使用新的临时目录，避免覆盖既有证据。

## 通用 Python：从仓库根运行两项示例

这段只使用标准库组织同样的两条命令；当前 Python 环境需要事先装好上面的依赖。它不安装软件，不读原数据库。可粘贴到 Python 或保存为自行管理的脚本；工作目录应是仓库根。

```python
from pathlib import Path
import os
import subprocess
import sys
import tempfile

bundle = Path.cwd() / 'harrywu'
out = Path(tempfile.mkdtemp(prefix='yuncong-harrywu-'))
env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1',
           MPLCONFIGDIR=str(out / 'matplotlib'))

def run(cwd, *args):
    subprocess.run([sys.executable, '-B', *args], cwd=cwd,
                   env=env, check=True)

run(bundle / 'tools', '-m', 'chart_tool', 'chart', '002025.SZ',
    '--cutoff', '2026-09-18T10:07:30+08:00', '--stage', 'study',
    '--window', '10y', '--last-bars', '250', '--width', '5600',
    '--snapshot-dir', str(bundle / 'examples/chart_inputs'),
    '--cache-dir', str(out / 'chart/cache'),
    '--out', str(out / 'chart/daily.png'))

data = 'docs/research/mkt_weather_v02_20261005/data/'
run(bundle / 'infrastructure/weather', '-m',
    'policyStudy.policy.market_sentiment.run', 'history',
    '--raw-daily', data + 'mkt_raw_daily.csv',
    '--calibration', data + 'mkt_calibration_v02.json',
    '--calendar', data + 'market_calendar.csv',
    '--start', '20250102', '--end', '20260930',
    '--generated-at', '2026-10-05T00:00:00+00:00',
    '--output', str(out / 'weather'))
print(out)
```

## 怎样解释结果

日线只使用截止前已经收盘的日线。因此盘中截止时，示例最后一天是 9 月 17 日；9 月 18 日未完成的日线没有偷用全天数据。该快照运行还报告 `CALENDAR_UNAVAILABLE`；这是既有日历覆盖限制，输出仍保留明确警告。不要将它解读为股票停牌已核实。

日线 PNG 同时生成 `.view.json`、`.manifest.json`、`.geometry.json`、`.pixel.json`、`.run.json`，分别保留绘图数据、来源/参数、几何、像素检查和运行回执。失败时先看错误码与这些文件，不放宽校验阈值。

天气只重现当日市场状态，复用已冻结的 2025 标尺。它不代表买卖建议，也不证明次日预测有效。完整说明见 [天气复现文档](../infrastructure/weather/docs/research/mkt_weather_v02_20261005/REPRODUCE.md)。

## 分钟数据、完整套件和本机路径

本分支随附 [真实组合图及数据视图](../examples/chart_real_minutes)，可以直接阅读。`chart-multi` 重新取数仍需外部分钟 CSV；原生产 `minute_sources.json` 保留 Harry 的 D 盘绝对路径作为来源记录，未伪装成通用路径。接自己的数据时，复制一份注册表、修改 root/path_template/列名和来源证明，用 `--minute-registry <新文件>` 明确指定。不要把输出缓存放入源数据目录。

1 分钟原 CSV 的当前规则是：241 行将 09:30 并入 09:31；240 行开始时刻网格整体后移一分钟。不单列竞价，警告 `AUCTION_UNAVAILABLE`；逐股日核对 OHLC/成交量/成交额，失败拒绝。这与其他 15 分钟策略研究的额外 09:30 整行排除规则不同。全量分钟数据未放入 Git，未对全历史逐日验收。

完整 `python -m pytest chart_tool -q` 仍依赖原环境的以下目录，而且部分旧测试把产物写在这些目录：

- `C:\Project Rocket\outputs\chart_tool_development_20261009\inputs`（含 `minute`、`render` 等夹具）；`B`、`D`、`E`、`F2` 测试目录。
- `C:\Project Rocket\outputs\chart_tool_v2_20261009\fixtures`、`inputs\minutes`、`X2`、`case_store`。
- `C:\Project Rocket\outputs\chart_replay_upload_20261009\delivery\01_复核\actual_intraday_reveal_boundary.json`。

本次没有改这些测试、补齐全量夹具或用临时重定向掩盖依赖。上方独立测试选择可在随附包里生成自己的合成输入，不读取这些旧路径。若要推进全套可移植测试，应作为后续独立工作保存夹具与入口，再重新验收。
