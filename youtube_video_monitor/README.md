# 云聪 YouTube 新视频监控

此模块在 Mac mini 上使用已登录的 YouTube 账号定期检查云聪频道。发现新视频后，依次执行 resilient 下载、Mac mini 本地 Qwen ASR 与 ForcedAligner 字幕、远端 GPU Qwen 观点分析、可编辑 PPT 生成，最后通过 Hermes 飞书发送 PPT。

## 目录

- `scripts/monitor_new_videos.py`：定时任务入口及流水线编排。
- `SKILL.md`：供人工调用或代理执行时遵循的行为、模型位置、归档及验收约束。
- `references/workflow.md`：每阶段命令、状态与恢复方式。
- `references/runtime.md`：Mac mini、远端 GPU、Hermes 与本机运行环境。

## 处理顺序

1. 通过 SSH 在 Mac mini 使用登录态、日区代理和 yt-dlp 按最新顺序列出频道视频。以 11 位 YouTube 视频 ID 去重，遇到本地归档中的第一个 ID 就停止。找不到归档边界时失败退出，避免漏视频或误抓历史内容。
2. 用 Mac mini 上已经验证的 `download_batch.py` 和 `run_resilient_batch.py` 下载视频，复用 cookie、代理、curl 传输和完整解码/音视频轨道检查。
3. 在 Mac mini 上通过 `mlx-audio` 运行 Qwen3-ASR-1.7B-8bit 与 Qwen3-ForcedAligner-0.6B-8bit，生成句级 SRT、VTT、全文和来源记录。下载结果和 ASR 导出必须闭合且哈希检查通过。
4. 将视频与字幕先暂存于仓库 `tmp/youtube_video_monitor/`。观点分析使用 `llm-server` 上的远端 GPU Qwen；Mac mini 的 Qwen 只承担语音识别和时间对齐。
5. macOS Vision 对原视频截图做 OCR 身份核验，只把画面文字能确认的标的用于截图详情和原声短片。完整观点分析照常归档；另生成带完整分析哈希、纳入与排除标的清单的 `analysis_for_ppt.json` 和 `PPT_SCOPE.json`，让 PPT 只覆盖截图身份已确认的股票。待人工复核标的写在 PPT 封面重点和飞书消息中。检查导出 receipt、PPTX 包完整性和版式验证。流水线结束后才将视频、音轨字幕、来源记录、完整分析、PPT 范围记录和同名 PPT 原子归档到 `~/云聪视频全集/标题_YYYYMMDD_HHMMSS/`；文件名采用下载器的 `canonical_filename`。
6. 验证归档 PPT 后，由 Hermes Feishu 发送附件。任何阶段失败都保留视频 ID 在 `tmp/youtube_video_monitor/monitor_state.json` 的待处理列表中，由下次运行重试；不会提前标记成功。

## 定时

Hermes cron 使用本机时区 `Asia/Shanghai`，每天 06:00、10:00、14:00、18:00、22:00 执行。脚本无新视频时静默结束。监控有文件锁，避免同时运行两份完整下载和 ASR 流水线。

## 临时文件与归档

下载批次和 ASR 原始尝试留在 Mac mini 的 `~/YuncongVideoAnalysis/monitor_jobs/<video_id>/`；Mac 本机中间结果、模型输入、PPT build 及状态都在仓库的 `tmp/youtube_video_monitor/`，该路径被 Git 忽略。正式结果写入 `~/云聪视频全集/`，不写入工具代码目录，也不把临时文件放入视频全集。

## 限制与人工复核

时间戳必须由已验证下载结果提供；缺失精确上传时间或规范文件名时停止归档，不从相对时间推断。字幕输出标记为自动生成、待听音抽检；模型分析和 PPT 的机器校验不能替代人工核对观点和截图。新视频不会覆盖已有归档；视频 ID、输入哈希、导出状态或 PPT 验证 receipt 不匹配时终止并保留现场。
