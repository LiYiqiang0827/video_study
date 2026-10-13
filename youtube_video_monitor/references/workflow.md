# 自动视频监控工作流

脚本入口为 `scripts/monitor_new_videos.py`。它先以非阻塞文件锁排除并发运行，再检查待续跑视频。通过 Mac mini 登录态抓取频道最新 12 条，按列表顺序比较本机归档来源记录的 YouTube ID，并在第一个旧 ID 处停止。列表没有归档边界时直接失败，不将列表耗尽误判为无新视频。

发现的新视频先写入仓库 `tmp/youtube_video_monitor/monitor_state.json`，之后按从旧到新的顺序串行处理。下载清单包含视频 URL 和频道列表提供的时长，冻结在 Mac mini 专属批次；使用 `run_resilient_batch.py`、cookie、代理和最多 8 次生命周期尝试。只接受带完整解码及流信息的 `technical_checks_passed` 结果。

下载结果在 Mac mini 上构建字幕 manifest；`batch_asr.py` 调用本机 MLX Qwen ASR 和 ForcedAligner，写入 ASR 状态和 attempt。闭合 attempt 由 `export_subtitles.py` 导出四类主字幕文件及其哈希来源证明。仅接受 `processed_pending_review` 和 `exported_auto`。该状态表示计算流程完成、待听音验收，不表示人工核对。

视频与字幕同步到仓库 `tmp/`，核对 SHA 后在临时目录建立正式归档结构。观点 JSON 通过 SSH 访问远端 GPU Qwen；分析器绑定原始字幕、cue 引用和来源视频。macOS Vision OCR 检查候选帧中的实体全名；仅 OCR 确认的帧能进入 PPT 详情和原声短片。未确认实体从截图详情筛除，并标记在封面重点和飞书消息中，完整转录和分析仍归档。另生成带完整分析哈希和纳入/排除清单的 `analysis_for_ppt.json` 与 `PPT_SCOPE.json`；大纲模型读取前者和 `video_ppt_outline.md`。PPT 构建压缩相关原视频片段，并检查构建状态、输出哈希、PPTX 完整性与版式 receipt。全部通过后临时归档目录才移动到 `~/云聪视频全集/`。

最后用 `hermes send --to feishu:<私聊目标> "... MEDIA:<pptx>"` 发送 PPT。只有发送命令成功返回后脚本才从 pending 移除此视频 ID。出错信息不附带 cookie 或频道认证信息，任务状态留在 `tmp/` 供下一轮恢复。

人工排查时先检查 `tmp/youtube_video_monitor/monitor_state.json`、单视频下载批次 `~/YuncongVideoAnalysis/batches/video_monitor_<ID>/state.json`、远端 ASR `~/YuncongVideoAnalysis/monitor_jobs/<ID>/asr/batch/state.json` 和本机 PPT build 下的 `VALIDATION.json`。不要手工把不完整任务从 pending 清除。
