# 云聪股票交易分析 · video_study

本项目把云聪 YouTube 研究工具分为五个板块：视频下载、音频与字幕、观点分析与PPT、新视频监控、频道 Posts。新视频监控按顺序编排前三项视频工具，并在完成后通过飞书发送PPT；Posts 板块独立采集和分析文字/图片/投票等动态。各板块包含独立技能、脚本和运行说明。代码与文档统一上传到 [LiYiqiang0827/video_study](https://github.com/LiYiqiang0827/video_study)。

项目帮助用户保存本人可访问的视频和频道 Posts，获得带时间的财经字幕，再把讲者的个股与指数观点整理成带原始画线截图及原声短片的可编辑PPT。观点保持讲者原意和条件语气，时间与来源由程序保留，分析过程不补写外部行情或收益结论。

## 文件夹结构

```text
video_study/
├── README.md
├── .gitignore
├── video_download/                    视频下载
│   ├── README.md
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── scripts/                       下载、清单、验收与同步
│   │   └── requirements.txt
│   └── references/                    环境、批次与故障说明
├── audio_subtitles/                   音频提取与字幕提取
│   ├── README.md
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── requirements-asr.txt
│   ├── scripts/                       音频、Qwen ASR、字幕与归档
│   └── references/                    工作流与运行环境
├── analysis_ppt/                      观点分析与PPT生成
│   ├── README.md
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── requirements-presentation.txt
│   ├── scripts/                       时钟、观点、截图、短片与PPT
│   ├── prompts/                       观点提炼和PPT大纲提示词
│   └── references/                    视频解析流程与运行依赖
├── youtube_posts/                     云聪 YouTube 频道 Posts 采集、归档与分析
│   ├── README.md
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── scripts/                       帖子提取、媒体归档与 GPU 千问分析
│   ├── prompts/                       Posts 分析提示词
│   ├── references/                    数据格式、登录与来源边界
│   └── tests/                         离线结构与归档测试
├── youtube_video_monitor/             新视频监控、流水线编排与飞书交付
│   ├── README.md
│   ├── SKILL.md
│   ├── scripts/                       定时监控入口
│   └── references/                    流程与运行环境
└── tmp/                               本机临时文件（Git忽略）
```

## 五个板块的输入、处理与输出

| 板块 | 输入 | 主要处理 | 输出与下一步 |
| --- | --- | --- | --- |
| [视频下载](video_download/README.md) | 用户指定视频或授权清单、外置登录状态 | 下载、续传、真实上传时间命名、时长/轨道/完整解码核验 | MP4、`result.json`及来源信息，交给音频板块 |
| [音频与字幕](audio_subtitles/README.md) | 完整本地视频或冻结下载清单 | M4A/WAV提取、Qwen ASR与ForcedAligner、字幕导出、同步与归档 | MP4/M4A/SRT/VTT/MD/JSON/来源JSON七文件集合 |
| [观点与PPT](analysis_ppt/README.md) | 已有字幕、原视频与来源记录 | 原句引用、时钟核对、同对象截图、观点与大纲、原声短片及PPT | 可编辑离线PPT、分析JSON、截图切片与可选本地阅读页 |
| [频道 Posts](youtube_posts/README.md) | 指定频道 Posts 页；Mac mini 已登录 YouTube 账号 | 本机 SSH 到 Mini 抓取、哈希验收，按北京时间月份归档并生成图文 PPT；观点归纳调用远端 GPU Qwen | `/Users/nickzhang/云聪帖子全集/` 根目录存放 PPT，月份子目录存 JSON、原图和清单 |
| [新视频监控](youtube_video_monitor/README.md) | 云聪频道最新视频列表；Mac mini 登录态 | 以视频 ID 停止于首个已归档视频，依次调用下载、Mac mini Qwen ASR/ForcedAligner、远端 GPU Qwen 分析与PPT；验收后经Hermes飞书发送 | 正式视频与PPT进入 `/Users/nickzhang/云聪视频全集/`；状态和中间产物在 `tmp/youtube_video_monitor/` |

下载由 `download_video.py` 或 `download_batch.py` 处理，新清单使用通用批次入口。Mac mini频繁TLS断连时使用 `run_resilient_batch.py`，它将8、9月成功的系统curl媒体传输和按时长设置的60～600次有限总重连预算用于任意授权冻结清单，仍完整解码并核对时长、轨道和SHA256。三个历史34视频批次专用脚本保留原清单哈希和执行边界，只用于原批次，不作为新任务默认入口。

音频板块的 `build_manifest.py` 消费已验收下载，`batch_asr.py` 调用音频提取和模型推理，`export_subtitles.py` 生成五份字幕产物，`organize_video_collection.py` 完成七文件归档。已有闭合原始结果可直接重建字幕，无须再次调用模型。为已下载归档补充字幕时，先在项目 `tmp/` 构建独立七文件包，核对已有视频后只补缺少文件；保留下载来源记录和其他正式交付，内容不同的同名文件不覆盖。正式批次映射及验收依据长期保存在媒体归档根，临时同步、WAV和验证脚本完成后清理。

新的Qwen ASR在Mac mini运行：SSH启动Mini上的Python脚本任务。`Qwen3-ASR-1.7B-8bit`识别中文，`Qwen3-ForcedAligner-0.6B-8bit`做时间对齐；两个模型都经 `mlx-audio` 使用Mini的Apple Silicon。本流程没有常驻HTTP服务，不通过oMLX。脚本、manifest及输入视频必须预先在Mini本地。任务结束后，闭合结果经SSH同步回本机并核对SHA。具体远端路径与命令见 [字幕运行环境](audio_subtitles/references/runtime.md)。

观点模型使用远端GPU服务器的Qwen vLLM服务，复用本机Hermes的 `llm_server_vllm` 配置；当前为 `qwen3.8-27b-fp8`，通过 `ssh llm-server` 访问服务器回环接口。Mac mini的Qwen3-ASR与ForcedAligner负责语音识别和时间对齐，本机负责截图、审阅和PPT构建。不要把三个运行位置混淆；GPU服务不可用时不自动改用Mini文本模型。配置与凭据留在工程外，连接方法见 [观点运行环境](analysis_ppt/references/runtime.md)。

观点板块的 `analyze_video_transcript.py` 提炼并校验引用，`build_video_analysis.py` 组装原图、双时间和切片，`build_video_ppt.py` 从冻结分析与大纲生成含声音短片的PPT。已有分析与大纲直接构建，已有模型原响应可离线复验。各板块Python模块在自己目录内调用，不通过旧实验目录或跨板块导入运行。

## 代码、资料与运行环境

本机代码目录是 `/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析`。项目内 `tmp/` 是唯一的本机临时验证位置，已由根 `.gitignore` 排除；存放临时脚本、WAV、下载或同步暂存、测试输出、候选PPT、压缩短片、日志和回执，任务结束后清理。不要将临时产物写入正式归档。正式视频、音频字幕、保留的解析证据和视频分析PPT归档在 `/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/`；频道 Posts 独立归档在 `/Users/nickzhang/云聪帖子全集/`，JSON 与图片按月放在子目录，月度 PPT 和根级清单直接放在 Posts 根目录；字幕归档可通过 `YUNCONG_VIDEO_COLLECTION_ROOT` 修改。

```text
项目根目录/tmp/                        本机临时下载暂存、ASR同步、测试脚本和PPT构建（Git忽略）
Mac mini ~/YuncongVideoAnalysis/posts_tools/jobs/  Posts 抓取隔离任务目录（成功后自动清理）

~/云聪视频全集/                       视频、字幕和视频分析PPT正式归档（/Users/nickzhang/云聪视频全集）
├── COLLECTION_LAYOUT_批次名.json      正式批次的文件映射、哈希和自动验收依据
└── YYYYMMDD_HHMMSS_标题/
    ├── 同名.mp4、.m4a、.srt、.vtt、.md、.json
    ├── 同名_来源记录.json
    ├── 同名_下载来源记录.json          已有下载记录保留
    ├── 同名.pptx
    └── 解析/                         保留的分析JSON、关键原图和来源证据
```

~/云聪帖子全集/                       YouTube Posts PPT 汇总在根目录，JSON与图片按月放子目录

Mac mini 上的 `~/YuncongVideoAnalysis/` 仍是远端下载/Posts执行环境、ASR运行环境和模型路径；SSH命令中的该路径仅指Mac mini。本机同名目录已删除，不要在本机重建，也不要把远端运行路径当作本机临时目录。Posts任务成功后会清理Mini上的唯一任务目录；帖子正式归档只写入 `/Users/nickzhang/云聪帖子全集/`。

Git只包含正式源码、技能、依赖声明、提示词和文档；本机 `tmp/` 的任何内容均不提交。Cookie、账号凭据和Qwen模型始终保存在项目外。视频及分析交付物进入视频归档，频道 Posts 进入独立帖子归档。工具允许运行暂存写入项目 `tmp/`，其他项目源码路径继续受保护。`.gitignore` 按各板块开放正式文件，代码随父仓库提交，目录内没有独立Git或子模块。

## 开始使用

在实际机器上替换克隆路径和资料目录，含中文或空格的路径保留引号：

```sh
PROJECT='/实际克隆目录/video_study'
DOWNLOAD="$PROJECT/video_download"
SUB="$PROJECT/audio_subtitles"
ANALYSIS="$PROJECT/analysis_ppt"

python3 "$SUB/scripts/check_environment.py" --mode basic
python3 "$ANALYSIS/scripts/check_environment.py" --mode basic
```

基础检查只验证依赖，不安装、不访问模型服务、不产生媒体。下载环境见 [下载运行说明](video_download/references/environment.md)。真实ASR需要Apple Silicon MLX解释器和两个现成Qwen模型，依赖装在外置环境，见 [字幕运行环境](audio_subtitles/references/runtime.md)。PPT依赖外置Codex Artifact Tool、Node与Python，`requirements-presentation.txt` 仅提供Python适配依赖，完整配置见 [PPT运行环境](analysis_ppt/references/runtime.md)。

### 仅提取音频或重建已有字幕

```sh
python3 "$SUB/scripts/extract_audio.py" \
  --video '/绝对路径/视频.mp4' --output-dir "$PROJECT/tmp/audio_test_任务名"
python3 "$SUB/scripts/export_subtitles.py" \
  --run-dir '/已有完整Qwen原始RUN' --output-dir "$PROJECT/tmp/subtitle_test_任务名" --video-id VIDEO_ID
```

完整下载后批处理、停止续跑、Mac mini同步与命名见 [字幕工作流](audio_subtitles/references/workflow.md)。不覆盖来源不匹配的已有目录。

### 已有分析与大纲生成PPT

先按PPT运行说明设置 `RUNTIME_NODE`、`RUNTIME_NODE_MODULES`、`RUNTIME_PYTHON` 和 `PRESENTATIONS_SKILL_DIR`：

```sh
python3 "$ANALYSIS/scripts/check_environment.py" --mode ppt
python3 "$ANALYSIS/scripts/build_video_ppt.py" \
  --analysis '/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/解析/analysis.json' \
  --plan '/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/解析/ppt_plan.json' \
  --build-dir "$PROJECT/tmp/ppt_build_任务名" \
  --output '/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/标题.pptx'
```

正式PPT默认与源视频同名且在同一归档目录。复测用新的构建目录和输出文件，放在项目 `tmp/`，不要在归档根目录创建测试目录。候选PPT、压缩短片、临时包、回执和日志在验证完成后从 `tmp/` 清理。每个详情页包含原图和可编辑文字，短片取自同对象同段原视频，默认最高720p、15fps、H.264 CRF28和AAC64kbps，不放大源视频，保留声音。PPT结构与解码检查、逐页视觉检查、演示软件实际点击播放分开验收。

## 安装项目技能

在用户技能目录创建所需链接，替换为实际克隆位置；若目标已存在，先确认其现有指向再更新：

```sh
ln -s "$PROJECT/video_download" "$HOME/.codex/skills/yuncong-member-video-download"
ln -s "$PROJECT/audio_subtitles" "$HOME/.codex/skills/yuncong-video-subtitles"
ln -s "$PROJECT/analysis_ppt" "$HOME/.codex/skills/yuncong-video-analysis"
ln -s "$PROJECT/youtube_posts" "$HOME/.codex/skills/yuncong-youtube-posts"
ln -s "$PROJECT/youtube_video_monitor" "$HOME/.codex/skills/yuncong-youtube-video-monitor"
```

字幕技能只处理音频与字幕，观点与PPT使用新增 `yuncong-video-analysis`。安装入口均带自身脚本与支持资料。

## 从旧目录迁移

| 旧位置 | 新位置 |
| --- | --- |
| `member_video_download/` | `video_download/` |
| 根目录音频、ASR、字幕、清单与归档脚本 | `audio_subtitles/scripts/` |
| 根目录时钟、观点、截图、切片与PPT脚本 | `analysis_ppt/scripts/` |
| 根目录字幕技能 | `audio_subtitles/SKILL.md` |
| 根目录提示词与视频解析文档 | `analysis_ppt/prompts/`、`analysis_ppt/references/` |

旧的下载子工程Git历史在本机工程外备份。项目各板块统一使用父仓库Git。旧根目录 `scripts/` 已取消，命令应使用对应板块的新路径。`YuncongVideoTools` 旧工程链接继续指向项目根目录；已有旧脚本命令需按上表更新。已部署的三个下载脚本兼容链接和视频处理技能链接已按各自板块维护。

## 本地复测范围

整理后使用 `tmp/` 下的新目录运行音频与字幕、观点与PPT公开入口，保留旧视频、字幕、分析、大纲及PPT。测试报告只放 `tmp/`，闭合后清理；按实际结果记录输入与产物哈希、音轨时长及采样、字幕正时长/重叠/越界、引用覆盖、PPT结构/版式/媒体解码，以及重复运行保护。

从已有Qwen结果导出与已有观点响应复验，只验证离线处理，不宣称发生了新的ASR或观点模型推理。缺少本机模型时不把Mac mini环境算作本地已安装。媒体嵌入与包验证不代表所有演示软件均已试播。临时测试回执在项目 `tmp/`，任务结束后清理。

2026-10-12 实际处理了已下载的2026年7月会员视频15条，总长9,334.259228秒（约155.6分钟）。通过SSH在Mac mini完成完整音频提取、Qwen ASR与ForcedAligner新推理，导出2,332条字幕和全文。正式M4A、SRT、VTT、MD、JSON及来源记录已补入本机原视频目录，保留原下载来源记录；批次映射及自动验收依据见媒体归档根 `COLLECTION_LAYOUT_202607.json`。验收覆盖两端及归档文件SHA、原音轨数据哈希一致、M4A完整解码、字幕正时长/无重叠/无越界和原文映射完整；不代表已经逐条人工听音或核实股票名。
