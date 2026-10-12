---
name: yuncong-video-subtitles
description: 从已下载的云聪中文财经视频提取原音轨和识别音频，使用Qwen MLX转写与对齐，导出句级字幕与全文，同步批次并按上传时间归档。用于音频提取、单视频或批量字幕、字幕重建和归档；观点分析与PPT使用独立技能yuncong-video-analysis。
---

# 云聪音频与字幕

正式代码位于本技能的 `scripts/`。本机路径为 `/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/audio_subtitles`。代码、技能和文档由整个项目统一Git管理。本机临时验证、WAV、ASR同步暂存、日志和临时脚本统一放 `/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/`；该目录被Git忽略，任务完成后清理。正式视频、M4A、SRT、VTT、全文、来源记录和需长期保留的分析交付归档到 `/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/`；归档脚本可用 `YUNCONG_VIDEO_COLLECTION_ROOT` 指定其他位置。旧本机 `~/YuncongVideoAnalysis` 已删除，不要在本机重新创建或把它用作默认资料根。

正式ASR在Mac mini上通过SSH启动Python任务，本机不需要Qwen HTTP服务，也不使用oMLX。以下 `~/YuncongVideoAnalysis/...` 路径仅指Mac mini上的远端运行环境、模型和任务目录，不是本机路径。本机目录已删除。运行时先把本板块代码和任务输入同步到Mini；使用Mini上的 `~/YuncongVideoAnalysis/asr_tools/venv/bin/python` 和 `~/YuncongVideoAnalysis/asr_tools/models/` 内的 `Qwen3-ASR-1.7B-8bit` 与 `Qwen3-ForcedAligner-0.6B-8bit`，由 `mlx-audio` 在Apple Silicon运行。任务闭合后经SSH同步到本机 `tmp/`，核对SHA并导出后，再把正式七文件归档到 `/Users/nickzhang/云聪视频全集/`。命令见 [运行环境](references/runtime.md)。

## 选择入口

- 迁移或依赖问题：`scripts/check_environment.py --mode basic`；推理前用 `--mode asr` 指定已有解释器与模型。见 [运行环境](references/runtime.md)。
- 仅音频或本地复测：`scripts/extract_audio.py --video 本地视频 --output-dir /Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/新任务`。确认后需保存的M4A再进入对应视频归档目录；临时WAV与日志留在 `tmp/` 并在任务结束后清理。
- 完整批次：读 [工作流程](references/workflow.md)，按已完成下载建立清单，再用 `batch_asr.py`、`export_subtitles.py` 和 `organize_video_collection.py`。
- 已有完整Qwen原始结果：`scripts/export_subtitles.py --run-dir 原始RUN --output-dir /Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/新任务 --video-id 视频ID`，不调用模型。确认后再把正式字幕七文件归档到对应视频目录。
- Mac mini闭合结果：`sync_asr_batch.py` 同步并核对SHA，再导出、归档。同步失败诊断不表示转写成功。
- 需要讲者观点或PPT：转用相邻的 [analysis_ppt](../analysis_ppt/README.md)，技能名为 `yuncong-video-analysis`。

## 处理规则

1. 读取用户指定的本地完整视频或已验证下载清单。不扩大频道范围，不从标题文字猜测上传时刻；缺少精确上传时间时先保留未知，不编造命名元数据。
2. 提取可播放的 M4A 原音轨及 16 kHz 单声道 PCM16 WAV。保留静音和原始时间轴，记录视频 SHA、音轨起点、采样数及实际时长。AAC 原音轨直接复制，不再次编码。
3. 使用 Qwen3-ASR-1.7B-8bit 和 Qwen3-ForcedAligner-0.6B-8bit，沿用已验证的连续分块与对齐流程。保留原始文本、原始 units、分块边界和模型配置；不能把音频块的起止时间直接冒充句级时间。
4. 成品采用自动字幕策略：标点与停顿分句，短口头语优先并入邻句；缺少独立正时长的短碎片可使用明确标记的邻句近似显示区间。保留原始文字与时间记录，不把近似显示说成真实逐字对齐。非关键拟声和口头碎片不需要逐条人工放行。
5. 提示词回声等显示过滤只能使用有视频、尝试和准确原文区间依据的登记记录；不得把某个旧样本的字符索引套到其他视频。金融名词、股票名和数字不得凭猜测补改。覆盖率与文件完整性不等于语音识别准确率。
6. 批次只消费当前已闭合且源 SHA 核对通过的尝试。同步成功也可能只是同步了失败诊断，不能据此称为转写成功。不以旧成功尝试遮盖当前未完成或失败项。
7. 默认不覆盖已有源数据与成品。推理输入、参数或工具版本变化需要新的输出目录；来源不符时报告冲突，每次重试保留原尝试。用户授权修订展示时，先将旧分析、大纲、PPT与闭合回执备份至项目 `tmp/` 审查目录并记录哈希，再用新构建目录生成同名交付；原七文件、字幕units及模型原响应保持不变，不为展示修订重新调用模型。

## 最终交付

`/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/`，日期时间为实际上传北京时间。每个目录七个内容文件共用目录名作为主文件名：

- `.mp4`：源视频。
- `.m4a`：可直接播放的原音频。
- `.srt`、`.vtt`：字幕。
- `.md`：带时间的全文。
- `.json`：原始文本、对齐、显示字幕和整理记录。
- `_来源记录.json`：输入、参数、工具及产物哈希。

WAV、原始模型输出和详细日志保留在项目 `tmp/` 的任务目录，不塞进最终七文件目录；用户明确要求长期保留的依据文件除外。两个 JSON 用后缀区分。原始生产文件名与最终文件名的对应关系存入归档根的正式批次映射 `COLLECTION_LAYOUT_批次名.json`；不为改名而改写冻结来源记录的内容。已有下载目录中的 `_下载来源记录.json` 必须保留，因此七份核心文件之外允许原有下载、分析和PPT等正式资料。

为已有视频目录补充音频字幕时，先用 `organize_video_collection.py` 在项目 `tmp/任务/collection_ready/` 构建并校验七文件包；不要直接把含下载来源记录的目录交给要求恰好七文件的整理器。逐项核对归档MP4与下载来源记录，再只发布缺少的六份文件，已有同名文件仅在SHA一致时跳过，不一致则保留并报告冲突。发布前临时副本只能放项目 `tmp/`，不得在归档目录写临时验证文件。最终保存正式批次文件映射与哈希，核验后清理本机和Mini的本任务临时目录，保留Mini解释器、模型及正式代码。

交付前核对实际完成数、当前尝试、源与成品哈希、字幕正时长/重叠/越界和七文件命名。清楚区分尚未转写、部分结果、自动成品和人工听音；不要把 CPU 导出验证称为新模型推理验证。报告成品绝对路径、未完成项和实际验证范围。
