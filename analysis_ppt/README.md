# 观点分析与PPT生成

输入是已有完整字幕、原视频及来源记录。技能为 `yuncong-video-analysis`，输出是有原句依据的个股与指数观点、同对象截图与切片、可编辑PPT和可选本地阅读页。

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
python3 "$ANALYSIS/scripts/build_video_ppt.py" --analysis '/视频目录/解析/analysis.json' --plan '/视频目录/解析/ppt_plan.json' --build-dir '/工程外且在视频目录之外/新构建目录'
```

PPT默认同源视频名且在同一目录，中间分析与截图在视频目录的解析子目录，构建预览和日志放在视频目录外。复测使用新的 `--output` 和构建目录，保留旧成品。图片、文字、原声短片均嵌入文件供离线使用。

使用 `load_workspace_dependencies` 提供的外置Node/Python/Artifact Tool，并设置 `RUNTIME_NODE`、`RUNTIME_NODE_MODULES`、`RUNTIME_PYTHON`、`PRESENTATIONS_SKILL_DIR`，详见runtime说明。已有分析与大纲的构建不调用模型；已有模型原响应复验也不代表新模型推理。PPT包验证与实际软件播放分别记录。
