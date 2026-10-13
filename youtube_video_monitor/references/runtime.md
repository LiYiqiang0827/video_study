# 运行环境

- Mac mini SSH 主机别名：`macmini`。YouTube 登录 cookie、日区代理、yt-dlp 下载 venv 和会员视频下载凭证都留在 Mac mini。监控脚本同步当前 `video_download/` 与 `audio_subtitles/` 代码到 Mac mini 私有目录 `~/YuncongVideoAnalysis/monitor_tools/`，下载任务放在 `~/YuncongVideoAnalysis/batches/video_monitor_<ID>/`。
- Mac mini ASR Python：`~/YuncongVideoAnalysis/asr_tools/venv/bin/python`。模型：`~/YuncongVideoAnalysis/asr_tools/models/Qwen3-ASR-1.7B-8bit`、`Qwen3-ForcedAligner-0.6B-8bit`。ASR 临时任务放在 `~/YuncongVideoAnalysis/monitor_jobs/<ID>/asr/`。
- 观点分析和 PPT 大纲通过 SSH 主机 `llm-server` 访问兼容 OpenAI 的 Qwen GPU 服务。此服务和 Mac mini ASR 是两条不同工作流。
- 本机 Hermes 可用飞书通道接收 `MEDIA:` 附件。脚本从 `~/.hermes/channel_directory.json` 读取唯一的飞书 DM 目标，目标标识不写进项目文件或 Git。
- PPT 的 Node/Python 运行时来自 Codex workspace dependencies，演示文稿技能路径从当前已安装版本中查找。
- 主视频归档：`~/云聪视频全集/`。所有中间材料及状态：项目 `tmp/youtube_video_monitor/`（Git 忽略）。
- Hermes cron 应以 `Asia/Shanghai` 时区运行，脚本入口为本目录下 `scripts/monitor_new_videos.py`。
