# YuncongVideoTools

云聪视频下载后的纯工具工程：音频提取、Qwen MLX 转写与对齐、自动字幕、Mac mini 结果同步及统一归档。此仓库同时是 `yuncong-video-subtitles` 技能包，入口为 [SKILL.md](SKILL.md)。

已完成字幕的视频还可生成带北京时间的观点解析、原始画线截图与视频切片，见 [视频解析工作流](references/video_analysis.md)。

代码位于 `/Users/nickzhang/YuncongVideoTools`；媒体与运行资料默认位于 `/Users/nickzhang/YuncongVideoAnalysis`。默认路径可通过 `YUNCONG_DATA_ROOT` 修改。代码库不包含视频、音频、模型、虚拟环境、处理结果、测试脚本或临时脚本。

## 正式入口

| 脚本 | 用途 |
| --- | --- |
| `scripts/build_manifest.py` | 从完成下载的结果构建经 SHA 核验的批次清单 |
| `scripts/extract_audio.py` | 原音轨 M4A 与识别用 WAV 提取 |
| `scripts/asr_engine.py` | Qwen ASR、连续分块与 ForcedAligner |
| `scripts/batch_asr.py` | 批量处理、冻结清单、停止与有限续跑 |
| `scripts/export_subtitles.py` | 单项或批量生成五份正式字幕产物 |
| `scripts/sync_asr_batch.py` | 将 Mac mini 的闭合结果按 SHA 同步到本机 |
| `scripts/organize_video_collection.py` | 七文件归档、统一上传时间命名与核验 |
| `scripts/prepare_asr_models.py` | 准备或核对两个正式 Qwen 模型 |
| `scripts/video_clock.py` | 等距抽帧、桌面时钟 OCR、录制时间区间映射 |
| `scripts/analyze_video_transcript.py` | 用 Qwen 或 MiniMax 整理有字幕依据的讲者观点 |
| `scripts/build_video_analysis.py` | 组装双时间、截图与切片，再生成解析产物 |
| `scripts/render_video_analysis.py` | 从既有分析生成离线 HTML 与纯文本摘要 |
| `scripts/serve_video_analysis.py` | 本地预览解析页面，支持源视频跳转和拖动 |

其余 Python 文件是以上入口实际调用的字幕、来源、媒体与路径模块，不是独立试验。使用方式见 [workflow](references/workflow.md)，环境与模型见 [runtime](references/runtime.md)。

## 安装技能

本机只维护这一份正式代码，技能目录指向此工程：

```sh
ln -s /Users/nickzhang/YuncongVideoTools /Users/nickzhang/.codex/skills/yuncong-video-subtitles
```

若目标已存在，先检查它，不覆盖已有技能。整个工程包含 `SKILL.md`、`agents/`、`references/` 与 `scripts/`，可连同 Git 历史复制到另一台机器；技能名称使用 `yuncong-video-subtitles`。

## 代码与数据隔离

所有 CLI 输出目录位于工程外；模型缓存和推理临时文件使用外置 runtime。`.gitignore` 仅开放正式 Python、依赖声明、技能元数据与 Markdown 文档。现有 `YuncongVideoAnalysis/asr_tools` 的历史试验和闭合产物仍保留，未迁入本工程。

仅本地 Git 管理不等同于已上传远端。分享此工程时，不附带登录状态、下载凭据或媒体资料。

## 整理验证

已用既有完整样本通过公开入口重新建清单、提取音频、导出五份字幕产物并归档为七个同名文件；另核对原始资料未改、字幕时轴、重复运行保护与代码目录隔离。本次验证复用了原始 Qwen 转写结果，没有重新进行模型推理；测试资料与报告均保存在工程外。
