# Posts 工作流

## 采集与归档

1. 用户指定频道 URL、需要采集的 Posts 范围和数量上限。当前脚本支持 `--max-posts` 上限；若需要精确日期窗口，人工复核帖子时间后再筛选归档。
2. 本机入口 `collect_posts_via_macmini.py` 通过 SSH 别名 `macmini` 启动任务：将仅含代码的 `collect_posts.py` SCP 到 Mini 的 `~/YuncongVideoAnalysis/posts_tools/project_scripts/`，使用已验证 Python `/Users/nickzhang/YuncongVideoAnalysis/download_tools/venv/bin/python` 与外置 Cookie `/Users/nickzhang/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt`。Cookie 不经 SCP 回传，任务输出隔离于 `posts_tools/jobs/<随机ID>/`。
3. Mini 上沿用已验证 PATH `/opt/homebrew/bin:/Users/nickzhang/.local/bin:/usr/bin:/bin`、代理 `socks5h://127.0.0.1:7890` 和 Chrome impersonation。只给 yt-dlp 少量瞬时重试；遇到登录、会员权限、验证码或地区拒绝即停止。
4. 本机从该唯一任务目录 SCP 闭合结果到项目 `tmp/youtube_posts/<随机ID>/`，按 `MANIFEST.json` 核对全部文件大小与 SHA256，再复制进新建的正式归档路径。成功后清理本机暂存与 Mini 任务目录；已有正式目录不覆盖。
5. 如果要保存图片，加 `--download-images`；核对 `posts.json` 的 `post_count`、帖子 ID、来源链接和每张图片的 SHA256。失败的图片下载留有错误记录，不将其报告为完整。
6. yt-dlp 或 YouTube 页面结构变化时，不使用未经核对的空结果。对照频道 Posts 页小样本，更新适配器和 fixture 测试后再进行完整采集。

## 分析

`analyze_posts.py` 通过 SSH 调用 `llm-server` 上的 Qwen vLLM `/v1/chat/completions`，输出 `analysis.md`、`ANALYSIS_PROVENANCE.json` 与原始请求/响应。Mac mini Qwen 只做音频 ASR/ForcedAligner，不用于 Posts 文本分析。分析输出先人工核对帖子 ID、原句、日期和图片，再作为正式记录；模型自动生成不等于事实核实。

示例：

```sh
python3 youtube_posts/scripts/collect_posts_via_macmini.py \
  --channel-url 'https://www.youtube.com/@HANDLE' \
  --output-dir '/Users/nickzhang/云聪视频全集/youtube_posts/HANDLE/20261012' \
  --max-posts 5 --download-images

python3 youtube_posts/scripts/analyze_posts.py \
  --input '/Users/nickzhang/云聪视频全集/youtube_posts/HANDLE/20261012/posts.json' \
  --output-dir '/Users/nickzhang/云聪视频全集/youtube_posts/HANDLE/20261012/analysis' \
  --transport ssh --ssh-host llm-server --base-url http://127.0.0.1:8002/v1
```

## 凭据

Mac mini Cookie 只从视频下载板块已经验证的外置路径读取。不得通过本机命令行复制 Cookie 内容、终端输出、Git 或分析证据包。对会员 Posts 的可见性取决于该账号本身的权限；脚本不请求绕过限制。
