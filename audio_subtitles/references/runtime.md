# 运行环境与恢复

## 代码与算力

本机标准 Python 可运行清单、音频提取、同步、五文件导出和归档；音频提取另需 FFmpeg/FFprobe。Qwen 推理使用 Apple Silicon 的 MLX。正式方案直接调用 `mlx-audio`，不依赖 oMLX 应用；不为此任务强制更新系统应用。

本机工程路径为 `/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析`；本机唯一临时目录是 `$PROJECT/tmp/`，正式归档为 `/Users/nickzhang/云聪视频全集/`。本机旧 `~/YuncongVideoAnalysis` 已删除。以下命令在同一终端先设置 `TOOLS` 和 `PROJECT`，其他机器改为实际克隆路径；所有脚本路径使用引号以支持中文与空格。

```sh
TOOLS='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/audio_subtitles'
PROJECT='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析'
python3 "$TOOLS/scripts/check_environment.py" --mode basic
```

代码工程保持纯工具。本机临时依赖与检查缓存可放 `"$PROJECT/tmp/runtime/"`，该路径已忽略；Cookie、Qwen模型权重及正式凭据仍放项目外。Mac mini已有的Python与模型使用下一节列出的远端路径。若要在本机创建新的临时Python环境：

```sh
python3 -m venv "$PROJECT/tmp/runtime/asr/venv"
"$PROJECT/tmp/runtime/asr/venv/bin/python" -m pip install \
  -r "$TOOLS/requirements-asr.txt"
```

依赖文件冻结已使用推理环境的版本；不能据此保证其他芯片/系统自动兼容。先用 `asr_engine.py --check-dependencies` 检查当前解释器。模型不存在时先查看准备计划，不进行无意义重复下载：

```sh
python3 "$TOOLS/scripts/prepare_asr_models.py" --plan
python3 "$TOOLS/scripts/prepare_asr_models.py" --check
```

实际准备运行 `prepare_asr_models.py`，支持 `--runtime-dir`、`--models-dir`、`--cache-dir`、`--proxy`。只处理正式两个公开模型，固定 revision 并核验权重，不读取个人账户凭据。`--status` 读进度；`--plan/--check` 不下载。

## 复用已配好的 Mac mini

字幕转写在Mac mini的Apple Silicon上通过 `mlx-audio` 运行。操作方式是SSH启动Python任务；本流程没有常驻HTTP服务，不通过oMLX。以下均为Mac mini远端路径，本机不得创建这些路径来冒充远端环境。已核实的正式环境与模型路径为：

```text
~/YuncongVideoAnalysis/asr_tools/venv/bin/python
~/YuncongVideoAnalysis/asr_tools/models/Qwen3-ASR-1.7B-8bit
~/YuncongVideoAnalysis/asr_tools/models/Qwen3-ForcedAligner-0.6B-8bit
```

ASR模型识别中文，ForcedAligner把原文与音频对齐。通过SSH启动前，应同步本板块的脚本、依赖和文档到Mini；manifest、源视频及脚本引用的全部文件也必须能被Mini本机读取。

批次参数用：

```sh
--python /Users/nickzhang/YuncongVideoAnalysis/asr_tools/venv/bin/python \
--models-dir /Users/nickzhang/YuncongVideoAnalysis/asr_tools/models
```

批次脚本会经SSH在Mac mini上运行指定的Python；该命令中的manifest与manifest引用的视频路径须是Mac mini本地路径。运行前检查Mini模型环境及正在运行的worker；任务结果闭合后用本机 `sync_asr_batch.py` 同步并按SHA核对。不要把本机Qwen服务或oMLX写作此流程的调用方式。

## 仅提取音频

```sh
python3 "$TOOLS/scripts/extract_audio.py" \
  --video /绝对路径/视频.mp4 \
  --output-dir "$PROJECT/tmp/audio_test_任务名"
```

输出原音轨 M4A、16 kHz mono PCM16 WAV、probe 和来源记录。使用 `--ffmpeg/--ffprobe` 指定程序路径。保留源音频起点，不删除静音；若源不适合既定 AAC 与完整时轴契约，检查报错而不是偷偷拼接、截断或填充。

## 问题处理

- 完整源下载没有通过：先修复下载，由独立下载技能处理，后处理不绕过源完整性。
- CPU Python 未装 MLX：清单/字幕导出仍可使用，推理调用已配好的 Mini Python，不能把缺依赖写成转写完成。
- 任务中断、空文本块、截断或对齐缺失：保留旧尝试，查状态与原始输出；按批次有限重试。仅需导出时不重跑推理。
- 成品代码/输入契约变化：用新的 `--output-dir` 或 `--output-root`；既有文件编辑不覆盖。
- 镜像未闭合、SHA 不符或当前失败：等待/修复对应项，不借旧成功结果表示当前已完成。
- 临时输出需要复测：放到项目 `tmp/`；正式成品放到 `/Users/nickzhang/云聪视频全集/`。其他代码目录输出仍会被路径保护拒绝。

标准自动字幕允许非关键口头语近似显示，无须逐条人工确认；词汇准确率仍未由文件检查证明。报告测试范围时区分实际音频提取、从既有原始结果导出、CPU 模拟与真实新模型推理。

## ASR 环境检查

```sh
python3 "$TOOLS/scripts/check_environment.py" --mode asr \
  --asr-python '/实际外置环境/bin/python' \
  --models-dir '/实际外置模型目录'
```

依赖检查不加载模型。`prepare_asr_models.py --check --models-dir 实际目录` 负责权重核验。观点与PPT已拆到相邻的 `analysis_ppt/` 板块，运行方法见其 README。
