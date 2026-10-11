# 运行环境与恢复

## 代码与算力

本机标准 Python 可运行清单、音频提取、同步、五文件导出和归档；音频提取另需 FFmpeg/FFprobe。Qwen 推理使用 Apple Silicon 的 MLX。正式方案直接调用 `mlx-audio`，不依赖 oMLX 应用；不为此任务强制更新系统应用。

本机工程已迁到 `Documents/ChatGPT/云聪股票交易分析`。以下命令在同一终端先设置 `TOOLS`，其他机器改为实际克隆路径；所有脚本路径使用引号以支持中文与空格。

```sh
TOOLS='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析'
python3 "$TOOLS/scripts/check_environment.py" --mode basic
```

代码工程保持纯工具。新虚拟环境放在数据根的 `runtime/asr/venv`，模型放在 `runtime/asr/models`，HF/临时缓存和状态也在外置 runtime。新机器安装时：

```sh
python3 -m venv /Users/nickzhang/YuncongVideoAnalysis/runtime/asr/venv
/Users/nickzhang/YuncongVideoAnalysis/runtime/asr/venv/bin/python -m pip install \
  -r "$TOOLS/requirements-asr.txt"
```

依赖文件冻结已使用推理环境的版本；不能据此保证其他芯片/系统自动兼容。先用 `asr_engine.py --check-dependencies` 检查当前解释器。模型不存在时先查看准备计划，不进行无意义重复下载：

```sh
python3 "$TOOLS/scripts/prepare_asr_models.py" --plan
python3 "$TOOLS/scripts/prepare_asr_models.py" --check
```

实际准备运行 `prepare_asr_models.py`，支持 `--runtime-dir`、`--models-dir`、`--cache-dir`、`--proxy`。只处理正式两个公开模型，固定 revision 并核验权重，不读取个人账户凭据。`--status` 读进度；`--plan/--check` 不下载。

## 复用已配好的 Mac mini

已有验证环境位于 Mac mini 的 `/Users/nickzhang/YuncongVideoAnalysis/asr_tools/venv/bin/python`，模型位于同目录的 `models/`。正式工程与旧环境分离；这些路径是既有实例，不是新工程内部目录。

批次参数用：

```sh
--python /Users/nickzhang/YuncongVideoAnalysis/asr_tools/venv/bin/python \
--models-dir /Users/nickzhang/YuncongVideoAnalysis/asr_tools/models
```

默认外置 runtime 继续保存新缓存和临时文件。实际执行前核对 Mini 环境仍存在、当前没有重复 worker，不移动旧模型、不重建旧成品。

## 仅提取音频

```sh
python3 "$TOOLS/scripts/extract_audio.py" \
  --video /绝对路径/视频.mp4 \
  --output-dir /Users/nickzhang/YuncongVideoAnalysis/audio_research/新任务
```

输出原音轨 M4A、16 kHz mono PCM16 WAV、probe 和来源记录。使用 `--ffmpeg/--ffprobe` 指定程序路径。保留源音频起点，不删除静音；若源不适合既定 AAC 与完整时轴契约，检查报错而不是偷偷拼接、截断或填充。

## 问题处理

- 完整源下载没有通过：先修复下载，由独立下载技能处理，后处理不绕过源完整性。
- CPU Python 未装 MLX：清单/字幕导出仍可使用，推理调用已配好的 Mini Python，不能把缺依赖写成转写完成。
- 任务中断、空文本块、截断或对齐缺失：保留旧尝试，查状态与原始输出；按批次有限重试。仅需导出时不重跑推理。
- 成品代码/输入契约变化：用新的 `--output-dir` 或 `--output-root`；既有文件编辑不覆盖。
- 镜像未闭合、SHA 不符或当前失败：等待/修复对应项，不借旧成功结果表示当前已完成。
- 输出目录误填到工具工程：路径检查会拒绝，改为工程外目录。

标准自动字幕允许非关键口头语近似显示，无须逐条人工确认；词汇准确率仍未由文件检查证明。报告测试范围时区分实际音频提取、从既有原始结果导出、CPU 模拟与真实新模型推理。

## ASR 与 PPT 环境检查

在实际执行 ASR 的机器上检查现有解释器和模型目录；远程 Mac mini 的路径不代表本机已经可推理。

```sh
python3 "$TOOLS/scripts/check_environment.py" --mode asr \
  --asr-python '/Users/nickzhang/YuncongVideoAnalysis/asr_tools/venv/bin/python' \
  --models-dir '/Users/nickzhang/YuncongVideoAnalysis/asr_tools/models'
```

模型检查只确认配置与权重文件存在；完整权重哈希检查仍使用 `prepare_asr_models.py --check --models-dir 实际目录`。检查器不加载 MLX 模型，不调用 Qwen/MiniMax。

PPT 导出依赖外置 Node、Artifact Tool、演示文稿辅助模块与 Python。由当前 Codex 的 `load_workspace_dependencies` 获取 `RUNTIME_NODE`、`RUNTIME_NODE_MODULES`、`RUNTIME_PYTHON`；将 `PRESENTATIONS_SKILL_DIR` 指向当前安装版本的 presentations 技能根，不能固定沿用旧版本目录。按实际返回的绝对路径设置：

```sh
export RUNTIME_NODE='/实际外置运行环境/node/bin/node'
export RUNTIME_NODE_MODULES='/实际外置运行环境/node/node_modules'
export RUNTIME_PYTHON='/实际外置运行环境/python/bin/python3'
export PRESENTATIONS_SKILL_DIR='/实际当前版本/presentations技能目录'
python3 "$TOOLS/scripts/check_environment.py" --mode ppt
```

`requirements-presentation.txt` 仅提供 Python `lxml` 适配依赖；PPT 渲染与验收还需要 Artifact Tool、Skia、辅助模块和 Pillow。Git 克隆及 Python 安装均不能替代这些外置组件。检查报告只验证依赖存在与解释器可执行，实际截图、媒体、版式和原生播放器仍按视频解析工作流验收。可加 `--json` 将只读结果输出为 JSON；退出码为 0 表示该检查模式通过，1 表示有缺项。
