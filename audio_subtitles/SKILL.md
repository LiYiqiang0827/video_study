---
name: yuncong-video-subtitles
description: 从已下载的云聪中文财经视频提取原音轨和识别音频，使用Qwen MLX转写与对齐，导出句级字幕与全文，同步批次并按上传时间归档。用于音频提取、单视频或批量字幕、字幕重建和归档；观点分析与PPT使用独立技能yuncong-video-analysis。
---

# 云聪音频与字幕

正式代码位于本技能的 `scripts/`。本机路径为 `/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/audio_subtitles`。代码、技能和文档由整个项目统一Git管理。下载、ASR任务、模型、环境和日志的资料根为 `~/YuncongVideoAnalysis`，可用 `YUNCONG_DATA_ROOT` 修改。最终视频归档目录独立位于 `~/云聪视频全集`；归档脚本可用 `YUNCONG_VIDEO_COLLECTION_ROOT` 指定其他目录。旧路径 `~/YuncongVideoAnalysis/云聪视频全集` 目前是指向新位置的兼容软链接。所有媒体与处理结果均在整个代码项目之外。

正式ASR在Mac mini上通过SSH启动Python任务，本机不需要Qwen HTTP服务，也不使用oMLX。运行时先把本板块代码同步到Mini，输入视频也必须在Mini上。使用 `~/YuncongVideoAnalysis/asr_tools/venv/bin/python` 和 `~/YuncongVideoAnalysis/asr_tools/models/` 内的 `Qwen3-ASR-1.7B-8bit` 与 `Qwen3-ForcedAligner-0.6B-8bit`；两个模型通过 `mlx-audio` 在Apple Silicon运行。提交任务前确认Mini当前没有重复worker。任务闭合后经SSH同步并核对SHA。命令见 [运行环境](references/runtime.md)。

## 选择入口

- 迁移或依赖问题：`scripts/check_environment.py --mode basic`；推理前用 `--mode asr` 指定已有解释器与模型。见 [运行环境](references/runtime.md)。
- 仅音频：`scripts/extract_audio.py --video 本地视频 --output-dir 工程外新目录`。
- 完整批次：读 [工作流程](references/workflow.md)，按已完成下载建立清单，再用 `batch_asr.py`、`export_subtitles.py` 和 `organize_video_collection.py`。
- 已有完整Qwen原始结果：`scripts/export_subtitles.py --run-dir 原始RUN --output-dir 新目录 --video-id 视频ID`，不调用模型。
- Mac mini闭合结果：`sync_asr_batch.py` 同步并核对SHA，再导出、归档。同步失败诊断不表示转写成功。
- 需要讲者观点或PPT：转用相邻的 [analysis_ppt](../analysis_ppt/README.md)，技能名为 `yuncong-video-analysis`。

## 处理规则

1. 读取用户指定的本地完整视频或已验证下载清单。不扩大频道范围，不从标题文字猜测上传时刻；缺少精确上传时间时先保留未知，不编造命名元数据。
2. 提取可播放的 M4A 原音轨及 16 kHz 单声道 PCM16 WAV。保留静音和原始时间轴，记录视频 SHA、音轨起点、采样数及实际时长。AAC 原音轨直接复制，不再次编码。
3. 使用 Qwen3-ASR-1.7B-8bit 和 Qwen3-ForcedAligner-0.6B-8bit，沿用已验证的连续分块与对齐流程。保留原始文本、原始 units、分块边界和模型配置；不能把音频块的起止时间直接冒充句级时间。
4. 成品采用自动字幕策略：标点与停顿分句，短口头语优先并入邻句；缺少独立正时长的短碎片可使用明确标记的邻句近似显示区间。保留原始文字与时间记录，不把近似显示说成真实逐字对齐。非关键拟声和口头碎片不需要逐条人工放行。
5. 提示词回声等显示过滤只能使用有视频、尝试和准确原文区间依据的登记记录；不得把某个旧样本的字符索引套到其他视频。金融名词、股票名和数字不得凭猜测补改。覆盖率与文件完整性不等于语音识别准确率。
6. 批次只消费当前已闭合且源 SHA 核对通过的尝试。同步成功也可能只是同步了失败诊断，不能据此称为转写成功。不以旧成功尝试遮盖当前未完成或失败项。
7. 默认不覆盖已有源数据与成品。推理输入、参数或工具版本变化需要新的输出目录；来源不符时报告冲突，每次重试保留原尝试。用户授权修订展示时，先将旧分析、大纲、PPT与闭合回执归档至工程外审查目录并记录哈希，再用新构建目录生成同名交付；原七文件、字幕units及模型原响应保持不变，不为展示修订重新调用模型。

## 最终交付

`~/云聪视频全集/YYYYMMDD_HHMMSS_标题/`，日期时间为实际上传北京时间。每个目录七个内容文件共用目录名作为主文件名：

- `.mp4`：源视频。
- `.m4a`：可直接播放的原音频。
- `.srt`、`.vtt`：字幕。
- `.md`：带时间的全文。
- `.json`：原始文本、对齐、显示字幕和整理记录。
- `_来源记录.json`：输入、参数、工具及产物哈希。

WAV、原始模型输出和详细日志保留在工程外的工作目录，不塞进最终七文件目录。两个 JSON 用后缀区分。原始生产文件名与最终文件名的对应关系存入外部 `COLLECTION_LAYOUT.json`；不为改名而改写冻结来源记录的内容。

交付前核对实际完成数、当前尝试、源与成品哈希、字幕正时长/重叠/越界和七文件命名。清楚区分尚未转写、部分结果、自动成品和人工听音；不要把 CPU 导出验证称为新模型推理验证。报告成品绝对路径、未完成项和实际验证范围。
