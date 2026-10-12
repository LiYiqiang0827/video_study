# Posts 工作流

## 采集与归档

1. 用户指定频道 URL、需要采集的 Posts 范围和数量上限。当前脚本支持 `--max-posts` 上限；如果需要精确日期窗口，人工复核帖子时间后再筛选归档。只有用户提供登录态路径时才采集登录可见内容。若 VPN 或登录只在 Mac mini 可用，就在 Mini 上运行该脚本；代码、Cookie 与数据分开处理。
2. 运行 `collect_posts.py` 前确认目标目录不存在。Posts 正式快照保存在 `/Users/nickzhang/云聪视频全集/youtube_posts/频道/批次/`；一次实验用项目根 `tmp/youtube_posts/`。
3. 如果要保存图片，加 `--download-images`；核对 `posts.json` 的 `post_count`、帖子 ID、来源链接和每张图片的 SHA256。失败的图片下载留有错误记录，不将其报告为完整。
4. yt-dlp 或 YouTube 页面结构变化时，不使用未经核对的空结果。对照频道 Posts 页小样本，更新适配器和 fixture 测试后再进行完整采集。

## 分析

`analyze_posts.py` 通过 SSH 调用 `llm-server` 上的 Qwen vLLM `/v1/chat/completions`，输出 `analysis.md`、`ANALYSIS_PROVENANCE.json` 与原始请求/响应。Mac mini Qwen 只做音频 ASR/ForcedAligner，不用于 Posts 文本分析。分析输出先人工核对帖子 ID、原句、日期和图片，再作为正式记录；模型自动生成不等于事实核实。

示例：

```sh
python3 youtube_posts/scripts/collect_posts.py \
  --channel-url 'https://www.youtube.com/@HANDLE' \
  --output-dir '/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/youtube_posts/sample' \
  --max-posts 5 --download-images

python3 youtube_posts/scripts/analyze_posts.py \
  --input '/Users/nickzhang/云聪视频全集/youtube_posts/HANDLE/20261012/posts.json' \
  --output-dir '/Users/nickzhang/云聪视频全集/youtube_posts/HANDLE/20261012/analysis' \
  --transport ssh --ssh-host llm-server --base-url http://127.0.0.1:8002/v1
```

## 凭据

Cookie 只通过 `--cookie-file` 引用放在项目外的常规文件。不要使用浏览器配置目录、cookie 内容复制到命令行、终端输出、Git 或分析证据包。对会员 Posts 的可见性取决于传入账号本身的权限；脚本不请求绕过限制。
