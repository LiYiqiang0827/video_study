---
name: yuncong-youtube-video-monitor
description: 定时检查云聪 YouTube 频道新视频，并依次通过 Mac mini 下载、Qwen ASR/ForcedAligner 字幕、远端 GPU Qwen 观点分析和 PPT 生成，再用 Hermes 飞书发送已验证 PPT。
---

# 云聪新视频监控技能

## 适用范围

仅处理云聪频道 `/videos` 最新列表中，位于本地视频全集最新归档 ID 之前的新视频。用视频 ID 去重，向旧视频方向遇到第一个归档 ID 后停止。不要扩展为下载历史频道或帖子。

## 固定执行边界

- 登录态、YouTube cookie 和日区代理留在 Mac mini；通过 SSH 调用 Mac mini 上的下载 venv 和 resilient 下载脚本。下载失败要保留批次现场，尊重重试预算和冻结清单。
- 视频音轨提取、16kHz 单声道识别音频、中文转录和强制对齐在 Mac mini 本地执行：`~/YuncongVideoAnalysis/asr_tools/venv/bin/python`、`~/YuncongVideoAnalysis/asr_tools/models/Qwen3-ASR-1.7B-8bit`、`Qwen3-ForcedAligner-0.6B-8bit`，由 `mlx-audio` 提供推理。禁止把观点分析路由到这两个语音模型。
- 观点分析用 `analysis_ppt/scripts/analyze_video_transcript.py --provider qwen-vllm --transport ssh --ssh-host llm-server` 调用远端 GPU Qwen；PPT 大纲也使用同一远端 GPU 服务。禁止 Mac mini Qwen 或 Hermes MiniMax 替代该观点模型。
- 新视频视频 ID 和处理阶段记在仓库 `tmp/youtube_video_monitor/monitor_state.json`。任务锁也放在 `tmp/`。失败保留 pending，成功完成归档与 Feishu 附件投递后才移除 pending。
- Mac 本机临时文件、模型输入、analysis/PPT build 位于仓库 `tmp/youtube_video_monitor/`；Mac mini 的下载和 ASR 工作目录位于 `~/YuncongVideoAnalysis/monitor_jobs/<video_id>/`。正式档案只存入 `~/云聪视频全集/标题_YYYYMMDD_HHMMSS/`，文件名沿用已验证下载结果的 `canonical_filename`。不把临时数据写入档案或工具源代码目录。
- 命名使用下载结果的 `canonical_filename`，必须含已验证的精确上传时间；缺少精确秒级上传时间或规范文件名时停止，不从 `1 day ago`、日期标题或扫描时间猜测。
- Hermes Feishu 私聊用于发送最终 PPT 附件。只在 PPT 导出 receipt、PPTX 完整性和版式验证全部通过后发送。发送失败保留 pending，以便重试。

## 验收

下载要求源 ID、字节数、SHA-256、媒体时长、音视频轨道和完整解码检查通过。字幕要求 ASR/对齐 attempt 闭合、自动字幕导出状态完整、transcript 含时间有效的 cues，且镜像哈希通过。观点 JSON 必须通过既有出处、实体和时间校验；macOS Vision OCR 只有在原始截图识别到实体全名时才标记该截图身份已核验。完整分析必须原样保留；另生成供 PPT 使用的 `analysis_for_ppt.json`，只含 OCR 已确认身份的股票，并用 `PPT_SCOPE.json` 记录完整分析哈希、纳入标的和排除标的。PPT 详情只引用同实体且 OCR 已核验的截图；封面及飞书消息提示待人工复核标的。PPT 必须有与输出哈希匹配的验证 receipt，导出和版式检查通过。

自动字幕仍需人工听音抽检；模型观点与图像/实体对应需人工抽查。通知应明确这些验收边界，不将机器校验说成内容已经人工确认。

## 定时配置

Hermes cron 在 Asia/Shanghai 每天 06:00、10:00、14:00、18:00、22:00 运行 `scripts/monitor_new_videos.py`。配置应使用 no-agent script 模式；无新视频时不发通知。定时入口不能重叠运行。

## 相关技能

- 下载：`yuncong-member-video-download`
- 音频和字幕：`yuncong-video-subtitles`
- 观点分析和演示文稿：`yuncong-video-analysis`
