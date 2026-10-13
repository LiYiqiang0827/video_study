---
name: yuncong-youtube-posts
description: 采集、归档和分析云聪 YouTube 频道 Posts，包括文字、发布时间、图片和投票等动态，并按月生成图文 PPT；不用于下载频道视频或生成视频字幕/观点视频 PPT。
metadata:
  short-description: 采集云聪 Posts，按月归档并生成图文 PPT
---

# 云聪 YouTube Posts

通过 SSH 在已登录 YouTube 的 Mac mini 上采集用户指定的云聪频道 Posts，保留帖子原文、原始发布时间、链接、附件元数据和文件哈希。正式归档根目录为 `/Users/nickzhang/云聪帖子全集/`：所有月份 PPT 和历史合集 PPT 直接放在根目录；各月份子目录只存结构化 `posts.json`、原图和月份级 `MANIFEST.json`，根目录 `MANIFEST.json` 记录根级 PPT。帖子 PPT 是原文与配图的浏览档案，不替代远端 GPU Qwen 观点分析；分析仍复用观点分析板块的 GPU Qwen vLLM SSH 连接，按帖子 ID 提供证据，不把模型归纳当作已核实行情。

开始前阅读 [工作流](references/workflow.md)；需要理解字段时阅读 [数据格式](references/data_schema.md)。

- 只处理用户指定频道和本人账号可见的 Posts，不猜测或扩展频道/访问权限，不绕过会员、登录、地理或访问限制。未指定数量上限时，抓取当前可见范围。
- YouTube Data API v3 没有公开 Posts 列表方法；采集适配依赖 yt-dlp 的 Posts tab 解析逻辑。遇到解析器缺失、登录门槛或页面变更时记录失败并停止，不用其他频道/视频替代。
- 通过 `collect_posts_via_macmini.py` 从本机 SSH 启动 Mac mini 上的 `collect_posts.py`。默认沿用下载板块记录的 `macmini`、Mini Python、Cookie 和代理；凭据只在 Mini 本地读取，绝不复制到本机或同步包。
- 正式 Posts 数据必须与 `/Users/nickzhang/云聪视频全集/` 分开，写入 `/Users/nickzhang/云聪帖子全集/YYYY-MM/`。不能把帖子或月度 PPT 写入视频全集。
- 优先按 `timestampUsec` 换算北京时间月份。若只有分钟/小时/天/周级相对时间，按采集基准计算估算时间区间，记录区间、精度、基准和说明；保留原始相对时间，不能把估算值写成精确 `published_timestamp`。月级相对时间不推算具体日期。
- 将 `1 year ago` 及更早、无法可靠分月的年份级帖子放入 `未识别日期/`，并合并生成一个 PPT。PPT 封面根据该归档的 `month_assignment_reference` 推断粗略时间边界：采集月份为 2026-10 时，标题标为“约 2025年10月以前（推断）”。每条帖子仍显示 YouTube 原始相对时间并注明精确日期未提供；这个边界仅用于归档分组，不能当作逐条帖子的日期结论。若采集基准缺失或无效，使用“较早年份（推断；具体范围未确认）”，不可伪造边界。
- 每月子目录保存完整正文和附件元数据于 `posts.json`，图片保存于 `media/`，并保留月份级 `MANIFEST.json`。月度 PPT `云聪帖子_YYYY-MM.pptx` 与历史范围 PPT 统一直接放 `/Users/nickzhang/云聪帖子全集/` 根目录，由根级 `MANIFEST.json` 记录。PPT 展示原始相对时间、可计算的估算区间及精度说明、帖子 ID、来源链接和可用配图；长文拆页，不删减底稿。估算归档月份不能当作精确发帖日。
- 定时监控由 Hermes 脚本任务执行，Mac 本地时区每天 09:00–23:30 每半小时一次。Hermes `--no-agent` 把监控脚本输出直接发到已保存的 Feishu 私聊；无新帖时不输出、不通知，也不调用聊天模型。Hermes Gateway/cron scheduler 必须运行并连接 Feishu。
- 监控从最新帖子开始读取，以稳定 `post_id` 去重，并在第一次遇到本地已归档 ID 时停止。已见 ID 同时从每月 `posts.json` 与归档根 `monitor_state.json` 读取；首次缺少归档 ID 时拒绝做可能覆盖历史的增量扫描。每轮新帖归档后重建受影响月份完整 PPT 并刷新哈希清单，完成后才输出飞书通知；失败通过 Hermes 运行记录查看。
- 使用 `scripts/install_posts_monitor.py` 幂等安装任务 `yuncong-youtube-posts-monitor`。状态、互斥锁、Posts 和 PPT 都只写 `/Users/nickzhang/云聪帖子全集/`；项目 `tmp/` 仅存同步暂存和 PPT 构建文件，Git 忽略。Feishu 收件人从本机 Hermes 频道目录读取，不能把 ID 写进项目、日志或输出。
- 本机同步暂存、PPT候选稿、预览图、测试和临时脚本只放项目根 `tmp/`，Git 已忽略此目录。Mini 任务目录成功或中断清理；Cookie 仅留在 Mac mini。
- 关联视频只保存引用；实际下载继续使用 `yuncong-member-video-download`，不从帖子附件推断用户授权下载。
- 分析应引用帖子 ID 与原文证据，保留条件语气。模型归纳不能当作已核实行情；需人工复核来源。

使用 `scripts/collect_posts_via_macmini.py` 抓取、`scripts/archive_posts_by_month.py` 按月合并 JSON 与原图、`scripts/generate_monthly_posts_ppt.mjs` 生成月度 PPT；定时监控由 `scripts/monitor_new_posts.py` 运行，首次安装用 `scripts/install_posts_monitor.py`。使用 `scripts/analyze_posts.py` 做来源可追溯的观点分析；完整参数以板块 README 和 references 为准。
