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

## 远端GPU千问服务

本机Hermes的 `providers.llm_server_vllm` 当前配置模型为 `/home/sysadmin/data/disk02/jc/models/qwen3.8-27b-fp8`，入口为 `http://localhost:8002/v1`。2026-10-12通过 `ssh llm-server` 核验服务器的 `http://127.0.0.1:8002/v1/models` 返回相同模型，服务由vLLM提供。观点分析使用这一GPU服务，Mac mini的Qwen只承担音频ASR和对齐；本机负责来源校验、抽帧和PPT构建。Hermes默认聊天提供商可能仍是MiniMax，不能将其默认项误当成此项目的观点模型。

直接调用同一服务的例子：

```sh
ssh llm-server 'curl --noproxy 127.0.0.1,localhost -fsS http://127.0.0.1:8002/v1/models'
python3 "$TOOLS/scripts/analyze_video_transcript.py" \
  --input '/Users/nickzhang/云聪视频全集/视频目录/视频主名.json' \
  --output-dir '/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/本任务/model_gpu_001' \
  --provider qwen-vllm --transport ssh --ssh-host llm-server \
  --base-url http://127.0.0.1:8002/v1 \
  --model /home/sysadmin/data/disk02/jc/models/qwen3.8-27b-fp8
```

配置与凭据留在工程外，模型和连接参数须按当次实际环境核验。服务不可用时保留失败记录，不自动启用Mini文本模型。只有完整响应通过引用、覆盖和语义审阅后才可生成正式PPT；原始响应不覆盖。

`video_clock.py`抽帧用FFmpeg，macOS Vision OCR若在沙箱内返回 `nilError`，可在获准的受限环境外只读执行同一图片OCR；保留首次诊断及新识别依据，不把失败OCR当可靠锚。时钟不一致时保留未知全局映射，已核验关键帧可登记直接可见桌面分钟。
