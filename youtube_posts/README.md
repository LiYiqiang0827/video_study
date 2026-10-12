# 云聪 YouTube 频道 Posts

本板块用于采集并保存云聪 YouTube 频道 Posts（频道动态），包括文字、发布时间原文、帖子链接、可识别的图片/投票信息和关联视频；可选择下载图片，并将帖子正文送到远端 GPU Qwen 服务做带来源编号的主题/观点梳理。它与会员视频下载、ASR 字幕和视频 PPT 是独立流程。

## 目录结构

```text
youtube_posts/
├── README.md
├── SKILL.md
├── agents/openai.yaml
├── scripts/
│   ├── collect_posts.py       从频道 Posts 页提取动态，保存 JSON，可选下载图片
│   └── analyze_posts.py       将已归档帖子发送至 GPU Qwen，保存原始响应和分析
├── prompts/analyze_posts.md   财经 Posts 分析约束与输出结构
├── references/
│   ├── data_schema.md         JSON 字段与附件说明
│   └── workflow.md            登录、采集、分析与归档步骤
└── tests/                     不访问网络和模型的离线测试
```

## 采集

使用 `video_download/scripts/requirements.txt` 中的 yt-dlp 环境；采集脚本通过 yt-dlp 的 YouTube Posts tab 解析器读取页面。YouTube 官方帮助说明 Posts 位于频道 Posts tab；当前 Data API v3 资源目录未列出 Posts 列表接口，所以采集适配依赖网页结构，可能随 YouTube 页面或 yt-dlp 版本变化。[官方 Posts 说明](https://support.google.com/youtube/answer/9409631) · [Data API 资源目录](https://developers.google.com/youtube/v3/docs) · [yt-dlp Posts renderer](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_tab.py)。不要绕过登录、会员权限或地区限制；需要登录时，仅传入用户明确提供且存放在工程外的 YouTube cookies 文件。若 VPN 或登录态只在 Mac mini 可用，就在 Mac mini 执行采集，再把闭合结果同步回本机归档。不要将 cookie、认证头或登录态提交 Git。

先确认视频全集中的正式 Posts 数据归档目录，再运行：

```sh
POSTS='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/youtube_posts'
PYTHON='/实际可用的yt-dlp环境/bin/python'
"$PYTHON" "$POSTS/scripts/collect_posts.py" \
  --channel-url 'https://www.youtube.com/@频道名' \
  --output-dir '/Users/nickzhang/云聪视频全集/youtube_posts/频道名/20261012' \
  --download-images
```

如访问公开 Posts 无需登录，不传 `--cookie-file`。需要认证时，指定工程外 Cookie 文件，例如 `--cookie-file '/外置安全目录/youtube-cookies.txt'`。脚本不会自动从浏览器读取或导出登录态。每次输出必须使用新目录，禁止覆盖已有采集快照。Posts 正文/元数据落在 `posts.json`；可选图片落在 `media/`。关联视频仅记下视频 ID 和链接，实际下载继续使用独立的视频下载流程。

## 分析

使用和 `analysis_ppt` 相同的远端 GPU Qwen vLLM SSH 通道；不会调用 Mac mini ASR 模型，也不会自动切换到本机 Hermes 默认聊天模型。

```sh
"$PYTHON" "$POSTS/scripts/analyze_posts.py" \
  --input '/Users/nickzhang/云聪视频全集/youtube_posts/频道名/20261012/posts.json' \
  --output-dir '/Users/nickzhang/云聪视频全集/youtube_posts/频道名/20261012/analysis' \
  --transport ssh --ssh-host llm-server \
  --base-url http://127.0.0.1:8002/v1
```

正式帖子数据和分析归档放在 `/Users/nickzhang/云聪视频全集/youtube_posts/`；临时采集、测试或候选结果放在项目 `tmp/`，Git 已忽略 `tmp/`。采集内容必须按帖子 ID 去重并保留原文、时间原文、链接与文件哈希。模型结论应明确标记为讲者表达/模型归纳，不等同于外部行情事实或投资建议；需人工核对出处。

字段、局限和完整命令见 [数据格式](references/data_schema.md) 与 [工作流](references/workflow.md)。
