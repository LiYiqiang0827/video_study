# 云聪 YouTube 频道 Posts

本板块用于采集并保存云聪 YouTube 频道 Posts（频道动态），包括文字、发布时间原文、帖子链接、可识别的图片/投票信息和关联视频；可选择下载图片，并将帖子正文送到远端 GPU Qwen 服务做带来源编号的主题/观点梳理。它与会员视频下载、ASR 字幕和视频 PPT 是独立流程。

## 目录结构

```text
youtube_posts/
├── README.md
├── SKILL.md
├── agents/openai.yaml
├── scripts/
│   ├── collect_posts_via_macmini.py  SSH 启动 Mac mini 抓取并同步验哈希
│   ├── collect_posts.py       Mac mini 端提取帖子，保存 JSON，可选下载图片
│   └── analyze_posts.py       将已归档帖子发送至 GPU Qwen，保存原始响应和分析
├── prompts/analyze_posts.md   财经 Posts 分析约束与输出结构
├── references/
│   ├── data_schema.md         JSON 字段与附件说明
│   └── workflow.md            登录、采集、分析与归档步骤
└── tests/                     不访问网络和模型的离线测试
```

## 采集

采集固定在 Mac mini 执行，复用下载板块的 SSH 别名 `macmini`、独立 Python 环境与外置 YouTube Cookie；抓取脚本由本机临时 SCP 到 Mini，Mini 上只保存隔离的任务输出，随后经 SCP 传回本机并逐文件核对 SHA256。Cookie 从不传回本机，也不进入项目或 Git。YouTube 官方帮助说明 Posts 位于频道 Posts tab；当前 Data API v3 资源目录未列出 Posts 列表接口，所以采集适配依赖网页结构，可能随 YouTube 页面或 yt-dlp 版本变化。[官方 Posts 说明](https://support.google.com/youtube/answer/9409631) · [Data API 资源目录](https://developers.google.com/youtube/v3/docs) · [yt-dlp Posts renderer](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_tab.py)。不得绕过登录、会员权限或地区限制。

先确认视频全集中的正式 Posts 数据归档目录，再运行：

```sh
POSTS='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/youtube_posts'
python3 "$POSTS/scripts/collect_posts_via_macmini.py" \
  --channel-url 'https://www.youtube.com/@频道名' \
  --output-dir '/Users/nickzhang/云聪视频全集/youtube_posts/频道名/20261012' \
  --max-posts 20 --download-images
```

默认使用 Mini 上已有 Cookie 文件 `/Users/nickzhang/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt`，以及视频下载流程使用的代理 `socks5h://127.0.0.1:7890`。若 Mac mini 登录 Cookie 或代理路径变化，更新本板块配置，不要导出或复制 Cookie 到 MacBook。本机正式输出路径必须是一个新目录，脚本会拒绝覆盖已有快照。Posts 正文/元数据落在 `posts.json`；`MANIFEST.json` 对帖子快照和图片逐文件记录大小及 SHA256；可选图片落在 `media/`。关联视频仅记下视频 ID 和链接，实际下载继续使用独立的视频下载流程。

## 分析

使用和 `analysis_ppt` 相同的远端 GPU Qwen vLLM SSH 通道；不会调用 Mac mini ASR 模型，也不会自动切换到本机 Hermes 默认聊天模型。

```sh
python3 "$POSTS/scripts/analyze_posts.py" \
  --input '/Users/nickzhang/云聪视频全集/youtube_posts/频道名/20261012/posts.json' \
  --output-dir '/Users/nickzhang/云聪视频全集/youtube_posts/频道名/20261012/analysis' \
  --transport ssh --ssh-host llm-server \
  --base-url http://127.0.0.1:8002/v1
```

正式帖子数据和分析归档放在 `/Users/nickzhang/云聪视频全集/youtube_posts/`；本机同步暂存与测试结果放在项目 `tmp/`，Git 已忽略 `tmp/`；Mini 任务输出在 `~/YuncongVideoAnalysis/posts_tools/jobs/<随机任务ID>/`，成功后自动清理。采集内容必须按帖子 ID 去重并保留原文、时间原文、链接与文件哈希。模型结论应明确标记为讲者表达/模型归纳，不等同于外部行情事实或投资建议；需人工核对出处。

字段、局限和完整命令见 [数据格式](references/data_schema.md) 与 [工作流](references/workflow.md)。
