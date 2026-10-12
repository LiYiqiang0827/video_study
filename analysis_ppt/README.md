# 观点分析与PPT生成

输入是已有完整字幕、原视频及来源记录。技能为 `yuncong-video-analysis`，输出是有原句依据的个股与指数观点、同对象截图与切片、可编辑PPT和可选本地阅读页。正式归档根目录为 `/Users/nickzhang/云聪视频全集/`，保留的PPT和分析证据放对应视频目录；临时分析测试、压缩短片缓存、PPT候选版、构建状态和日志统一放项目根目录 `tmp/`（Git忽略），完成后清理。

## 目录与入口

| 文件或目录 | 用途 |
| --- | --- |
| `SKILL.md`、`agents/openai.yaml` | 独立观点分析技能与发现配置 |
| `prompts/video_analysis.md` | 观点提炼及来源引用契约 |
| `prompts/video_ppt_outline.md` | 精简PPT大纲契约 |
| `references/video_analysis.md` | 抽帧、双时间、观点与PPT工作流 |
| `references/runtime.md` | 模型服务和外置PPT运行依赖 |
| `scripts/check_environment.py` | basic/ppt检查，不调用观点模型 |
| `scripts/video_clock.py` | 桌面时钟抽帧、OCR与录制时间映射 |
| `scripts/analyze_video_transcript.py` | Qwen/Hermes观点提炼或已有响应离线复验 |
| `scripts/build_video_analysis.py` | 来源、截图与视频切片组装 |
| `scripts/render_video_analysis.py`、`serve_video_analysis.py` | 离线阅读材料和本地预览 |
| `scripts/build_video_ppt.py` | 短片、PPT导出、嵌入与闭合验证的总入口 |
| `scripts/prepare_ppt_video_clips.py` | 同对象、同段原声短片压缩与解码核验 |
| `scripts/export_video_analysis_pptx.mjs` | Artifact Tool生成可编辑PPT |
| `scripts/embed_ppt_videos.py` | 内部媒体关系适配 |
| `requirements-presentation.txt` | Python媒体嵌入依赖，不含整个PPT运行环境 |

`analysis_storage.py`、`video_analysis_assets.py`、`tool_paths.py` 为本板块内部模块，不跨目录导入字幕代码。

```sh
ANALYSIS='/实际克隆目录/analysis_ppt'
python3 "$ANALYSIS/scripts/check_environment.py" --mode basic
python3 "$ANALYSIS/scripts/analyze_video_transcript.py" --help
python3 "$ANALYSIS/scripts/build_video_ppt.py" --analysis '/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/解析/analysis.json' --plan '/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/解析/ppt_plan.json' --build-dir '/项目克隆目录/tmp/ppt_build_任务名/build' --output '/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/标题.pptx'
```

PPT正式成品默认同源视频名且在同一目录；可保留的 analysis JSON、审阅大纲和必要截图放在 `解析/`。WAV、压缩短片、候选文件、PPT临时包、构建状态、预览和日志均放项目 `tmp/`，禁止在视频归档里创建测试目录或留下临时构建物。复测时 `--build-dir` 和 `--output` 都使用 `tmp/` 下彼此分开的子目录；确认完成后删除整项测试目录。图片、文字、原声短片均嵌入正式PPT供离线使用。

使用 `load_workspace_dependencies` 提供的外置Node/Python/Artifact Tool，并设置 `RUNTIME_NODE`、`RUNTIME_NODE_MODULES`、`RUNTIME_PYTHON`、`PRESENTATIONS_SKILL_DIR`，详见runtime说明。已有分析与大纲的构建不调用模型；已有模型原响应复验也不代表新模型推理。PPT包验证与实际软件播放分别记录。

观点分析使用本机Hermes已配置的远端GPU千问服务 `llm_server_vllm`，当前模型为 `qwen3.8-27b-fp8`。脚本通过 `ssh llm-server` 调用同一vLLM接口；本机负责截图和PPT构建。Mac mini的Qwen3-ASR与ForcedAligner仅用于音频字幕流程，不用于观点分析。连接核验与调用例子见运行环境。

新批次可在项目 `tmp/` 完成分析、媒体缓存、候选PPT及逐页检查，再发布到源视频的 `解析/` 和同名PPT。分析仍绑定归档视频/字幕真实路径及SHA；压缩缓存可在项目 `tmp/` 的冻结媒体清单目录内，清理暂存后正式分析与PPT仍可使用。
