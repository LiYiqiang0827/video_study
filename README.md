# 云聪股票交易分析 · video_study

云聪视频下载后的纯工具工程：音频提取、Qwen MLX 转写与对齐、自动字幕、Mac mini 结果同步及统一归档。此仓库同时是 `yuncong-video-subtitles` 技能包，入口为 [SKILL.md](SKILL.md)。

已完成字幕的视频还可提炼观点，默认交付含原声片段的可编辑 PPT：先列大盘、主动讲股和网友提问大纲，再配指数及个股的原始画线截图与简要说明。截图与压缩短片嵌入文件，不需视频外链。PPT与源视频同名，放在源视频同一目录；中间材料在解析子目录。同时保留带双时间的本地解析与切片，见 [视频解析工作流](references/video_analysis.md)。

GitHub 仓库：[LiYiqiang0827/video_study](https://github.com/LiYiqiang0827/video_study)。本机代码位于 `/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析`；媒体与运行资料位于 `/Users/nickzhang/YuncongVideoAnalysis`。其他用户的默认资料目录为其主目录下的 `YuncongVideoAnalysis`，可通过 `YUNCONG_DATA_ROOT` 修改。代码库不包含视频、音频、模型、虚拟环境、处理结果、测试脚本或临时脚本。

## 开始使用

已有本机工程可直接运行下面的检查。另一台机器先克隆本仓库，并将 `TOOLS` 改为实际克隆目录；模型、算力和资料目录按 [运行环境](references/runtime.md) 配置。

```sh
TOOLS='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析'
export YUNCONG_DATA_ROOT='/Users/nickzhang/YuncongVideoAnalysis'
python3 "$TOOLS/scripts/check_environment.py" --mode basic
```

基础检查验证 Python、FFmpeg/FFprobe 和资料路径；`--mode asr` 检查指定解释器与本地模型存在性，`--mode ppt` 检查外置 PPT 运行依赖。检查不加载模型、不访问网络、不产生媒体。环境通过后选择 [音频与字幕工作流](references/workflow.md) 或 [单视频观点PPT工作流](references/video_analysis.md)。已有字幕直接用于观点提炼，已有冻结分析和大纲直接用于 PPT 构建。

## 正式入口

| 脚本 | 用途 |
| --- | --- |
| `scripts/check_environment.py` | 只读检查基础、ASR或PPT依赖，迁移后核对路径 |
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
| `scripts/export_video_analysis_pptx.mjs` | 从既有分析和精简大纲生成可编辑文字、离线截图的 PPT |
| `scripts/prepare_ppt_video_clips.py` | 在同票同段内选择截图附近短片，压缩、解码核验并登记媒体清单 |
| `scripts/embed_ppt_videos.py` | 给已导出的 PPT 截图附加内部视频关系与点击播放时序，不做转写或压缩 |
| `scripts/build_video_ppt.py` | 从冻结分析与大纲完成压缩、嵌入和验证；默认同视频名同目录，带状态锁与哈希复用 |

其余 Python 文件是以上入口实际调用的字幕、来源、媒体与路径模块，不是独立试验。使用方式见 [workflow](references/workflow.md)，环境与模型见 [runtime](references/runtime.md)。

默认入口 `build_video_ppt.py` 接受 `--analysis`、`--plan`、`--build-dir`，可选 `--output` 覆盖同视频名同目录的默认位置。底层PPT导出器保留原参数。大纲使用 [video_ppt_outline](prompts/video_ppt_outline.md) 的 `yuncong_ppt_plan_v1` 结构，来源为已有 `analysis.json`，股票放 `stocks`，实际指数/大盘K线图解放 `market_items`；按原文区分主动讲股和网友提问；每票一个态度关键词、一句话结论，每个详情配同对象截图和 2—4 个短点。条件买入保留“计划”，资金举例保留“假设”；大盘没有独立判断时只写“本片未作独立判断”。运行时通过 `RUNTIME_NODE`、`RUNTIME_NODE_MODULES`、`PRESENTATIONS_SKILL_DIR`、`RUNTIME_PYTHON` 指定现成的外置依赖，命令示例见视频解析工作流。

短片默认不放大原视频，最高 1280×720、15 fps、H.264 CRF 28、AAC 64 kbps，保留声音和原图范围。每段以截图为中心选取约 32 秒，优先字幕句界；较短讲解保留全段，不跨话题凑长度。媒体清单绑定分析、大纲和每段文件哈希，可续用相同输入；参数变化用新目录。嵌入不等于播放验收：必须在本地演示软件实际点击验证后才称为可播放。本机没有 PowerPoint，Keynote/WPS 的实际试播结果单独记录，不用媒体数量替代播放证明。

## 安装技能

本机只维护这一份正式代码，技能目录指向此工程：

```sh
ln -s '/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析' /Users/nickzhang/.codex/skills/yuncong-video-subtitles
```

若目标已存在，先检查它，不覆盖已有技能。本机技能链接已随项目迁移更新；旧代码路径 `/Users/nickzhang/YuncongVideoTools` 保留为新工程的兼容链接，用于仍引用旧路径的已有任务。整个工程包含 `SKILL.md`、`agents/`、`references/` 与 `scripts/`，可连同 Git 历史复制到另一台机器；技能名称使用 `yuncong-video-subtitles`。在其他机器上用实际主目录与克隆位置创建技能链接。

## 代码与数据隔离

所有 CLI 输出目录位于工程外；模型缓存、PPT 构建预览和推理临时文件使用外置 runtime。`.gitignore` 仅开放正式 Python/Node 入口、依赖声明、技能元数据与 Markdown 文档。现有 `YuncongVideoAnalysis/asr_tools` 的历史试验和闭合产物仍保留，未迁入本工程。

远端为本 README 顶部链接的 `video_study` 仓库。分享此工程时，不附带登录状态、下载凭据或媒体资料。

## 当前工具覆盖

音频、字幕、录制时钟、观点分析与带原声片段的 PPT 入口已包含在仓库中。视频下载由独立的 `yuncong-member-video-download` 技能负责。观点提炼支持 Qwen vLLM 和 Hermes MiniMax，PPT 构建本身不调用大模型；Hermes/Qwen 从原字幕自动做到整份 PPT 的试跑仍需按单项结果验收，不将这套工具的存在视为全库自动分析已通过。PPT 依赖外置 Codex Artifact Tool 与演示文稿辅助工具，`requirements-presentation.txt` 只提供 Python 视频嵌入适配依赖，不能单独安装完整 PPT 运行环境。

## 整理验证

已用既有完整样本通过公开入口重新建清单、提取音频、导出五份字幕产物并归档为七个同名文件；另核对原始资料未改、字幕时轴、重复运行保护与代码目录隔离。本次验证复用了原始 Qwen 转写结果，没有重新进行模型推理；测试资料与报告均保存在工程外。
