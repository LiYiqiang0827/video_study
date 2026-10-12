---
name: yuncong-youtube-posts
description: 采集、归档和分析云聪 YouTube 频道 Posts，包括文字、发布时间、图片和投票等动态；不用于下载频道视频或生成视频字幕/PPT。
metadata:
  short-description: 采集云聪频道 Posts 并用远端 GPU Qwen 分析
---

# 云聪 YouTube Posts

采集用户指定的云聪频道 Posts，保留帖子原文、原始时间显示、链接、附件元数据和文件哈希；按需保存公开或当前登录账号有权查看的图片。需要模型分析时，复用观点分析板块的远端 GPU Qwen vLLM SSH 连接，按帖子 ID 提供证据，不把模型归纳当作已核实行情。

开始前阅读 [工作流](references/workflow.md)；需要理解字段时阅读 [数据格式](references/data_schema.md)。

- 只处理用户指定频道和有权访问的 Posts，不猜测或扩展频道/时间范围，不绕过会员、登录、地理或访问限制。
- YouTube Data API v3 没有公开 Posts 列表方法；采集适配依赖 yt-dlp 的 Posts tab 私有解析逻辑。遇到解析器缺失、登录门槛或页面变更时记录失败，停止并报告，不抓取替代频道/视频来填补。
- Cookie 只能由用户明确指定外置文件路径传入；脚本不读取浏览器配置，不复制、显示或上传 Cookie。
- 若 VPN 或 YouTube 登录态只在 Mac mini 可用，在 Mini 上运行采集脚本并通过 SSH 同步闭合的 Posts 数据；不要把 Cookie 纳入同步包。
- 正式快照归档到 `/Users/nickzhang/云聪视频全集/youtube_posts/`。测试、临时脚本、模型原始请求和候选结果放项目根 `tmp/`。不把数据归档进代码板块或 Git。
- 关联视频只保存引用；视频下载走 `yuncong-member-video-download`。不要从帖子链接推断用户已授权下载帖子中的视频附件。
- 分析应引用 post ID 与原文证据，保留条件语气，并把缺少来源的判断标为不确定。复核模型输出后再作为正式分析归档。

使用 `scripts/collect_posts.py` 采集，使用 `scripts/analyze_posts.py` 分析；正式参数与归档约定以板块 README 和 references 为准。
