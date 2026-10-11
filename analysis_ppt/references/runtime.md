# 观点分析与PPT运行环境

普通Python负责观点引用校验、抽帧、媒体准备与构建调度，另需FFmpeg/FFprobe。观点模型可使用 Qwen vLLM 或 Hermes MiniMax；已有原始响应可通过 `--raw-response` 离线复验，已有冻结分析和大纲可直接构建，不调用模型。

```sh
TOOLS='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/analysis_ppt'
python3 "$TOOLS/scripts/check_environment.py" --mode basic
```

录制时钟OCR使用本机Swift/Vision，按 `video_clock.py --help` 操作。模型服务、Hermes账号和API配置留在工程外。新机器须检查实际可用服务，不能将别的机器配置当成本机已安装。

PPT 导出依赖外置 Node、Artifact Tool、演示文稿辅助模块与 Python。由当前 Codex 的 `load_workspace_dependencies` 获取 `RUNTIME_NODE`、`RUNTIME_NODE_MODULES`、`RUNTIME_PYTHON`；将 `PRESENTATIONS_SKILL_DIR` 指向当前安装版本的 presentations 技能根，不能固定沿用旧版本目录。按实际返回的绝对路径设置：

```sh
export RUNTIME_NODE='/实际外置运行环境/node/bin/node'
export RUNTIME_NODE_MODULES='/实际外置运行环境/node/node_modules'
export RUNTIME_PYTHON='/实际外置运行环境/python/bin/python3'
export PRESENTATIONS_SKILL_DIR='/实际当前版本/presentations技能目录'
python3 "$TOOLS/scripts/check_environment.py" --mode ppt
```

`requirements-presentation.txt` 仅提供 Python `lxml` 适配依赖；PPT 渲染与验收还需要 Artifact Tool、Skia、辅助模块和 Pillow。Git 克隆及 Python 安装均不能替代这些外置组件。检查报告只验证依赖存在与解释器可执行，实际截图、媒体、版式和原生播放器仍按视频解析工作流验收。可加 `--json` 将只读结果输出为 JSON；退出码为 0 表示该检查模式通过，1 表示有缺项。
