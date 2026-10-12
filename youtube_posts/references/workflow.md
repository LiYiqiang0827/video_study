# Posts 工作流

## 采集与归档

1. 用户指定频道和采集范围。没有数量上限时，采集当前账号可见的全部 Posts；小样本可传 `--max-posts`。不得扩展到其他频道或权限范围。
2. 本机入口 `collect_posts_via_macmini.py` 通过 SSH 别名 `macmini` 启动 Mini 任务，将 `collect_posts.py` SCP 至 `~/YuncongVideoAnalysis/posts_tools/project_scripts/`。使用已验证 Python `/Users/nickzhang/YuncongVideoAnalysis/download_tools/venv/bin/python`、外置 Cookie `/Users/nickzhang/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt`、PATH `/opt/homebrew/bin:/Users/nickzhang/.local/bin:/usr/bin:/bin`、代理 `socks5h://127.0.0.1:7890` 和 Chrome impersonation。Cookie 不经 SCP 回传。
3. Mini 结果位于唯一任务目录 `~/YuncongVideoAnalysis/posts_tools/jobs/<随机ID>/`。遇到登录、会员权限、验证码或地区拒绝即停止，不尝试绕过。
4. 本机把闭合结果同步到项目 `tmp/youtube_posts/<随机ID>/`，根据 `MANIFEST.json` 核对文件大小与 SHA256。校验成功后按帖子 ID 去重，并优先按 `timestampUsec` 换算的北京时间月份写入 `/Users/nickzhang/云聪帖子全集/YYYY-MM/`。若页面只给分钟/小时/天/周级相对时间，可按采集基准生成估算区间，记录为 `estimated_published_at_start/end` 并附精度、基准和“非精确时间”说明；原始相对文字保留在 `published_time_raw`，不能填入 `published_timestamp`。月级相对时间只用于估算归档月份，不推具体日；`1 year ago` 及更早、无法可靠分月的年份级记录放入 `未识别日期/`。Posts 严禁写进云聪视频全集。
5. 带 `--download-images` 时，每个图片 renderer 只保存最高分辨率版本。图片下载失败保留在帖子记录内并计入失败数；成功图片保存到月份 `media/<post_id>/` 并记录大小与 SHA256。
6. 月份子目录保存完整 `posts.json`、原图和月份级 `MANIFEST.json`；所有月度 PPT 与年份级旧帖合并 PPT 直接放在 `/Users/nickzhang/云聪帖子全集/` 根目录，根级 `MANIFEST.json` 记录这些 PPT。PPT 对有分钟/小时/天/周级相对时间的帖子显示推算范围并标“估算”；月/年级相对时间只展示 YouTube 原文并注明精确日期未提供。估算归档月份不能当作精确发帖日。`未识别日期/` 中年份级旧帖统一制作成一个 PPT；根据 `month_assignment_reference` 的采集月份推断粗略边界，例如 2026-10 的归档标为“约 2025年10月以前（推断）”，文件名也体现该边界。每条帖子仍保留原始“1 year ago”等文本，页面明确精确日期由 YouTube 未提供；推断边界只表示归档范围。缺少有效采集基准时不得生成具体年月。月度 JSON 与媒体是可追溯底稿，PPT 是图文浏览文件。
7. 完成或中止后清理本机项目 `tmp/youtube_posts/<随机ID>/` 与 Mini 任务目录。不要将账号 Cookie、终端秘密、模型凭据或数据归档提交 Git。
8. yt-dlp 或 YouTube 页面结构变化时，不把空结果当成功。核对频道 Posts 页小样本、更新适配器和 fixture 测试后再全量采集。

月度 PPT 用 `scripts/generate_monthly_posts_ppt.mjs` 与 Codex Artifact Tool 生成。构建候选与逐页预览保存在项目 `tmp/youtube_posts/monthly_ppt/`，检查内容、图片和文字适配后，将最终 PPT 复制到 `/Users/nickzhang/云聪帖子全集/` 根目录，再运行：

```sh
python3 youtube_posts/scripts/archive_posts_by_month.py \
  --archive-root '/Users/nickzhang/云聪帖子全集' --refresh-manifests
```

该命令刷新月份子目录中 JSON 与图片的清单，以及归档根目录中 PPT 的清单。新增采集会按帖子 ID 更新月度记录，随后应重建受影响月份 PPT 并刷新清单。

## 观点分析

`analyze_posts.py` 通过 SSH 调用 `llm-server` 上的 Qwen vLLM `/v1/chat/completions`，输出 `analysis.md`、`ANALYSIS_PROVENANCE.json` 与原始请求/响应。Mac mini Qwen 只做音频 ASR/ForcedAligner，不用于 Posts 文本分析。分析输出先人工核对帖子 ID、原句、日期和图片；模型归纳不等于事实核实。

示例：

```sh
python3 youtube_posts/scripts/collect_posts_via_macmini.py \
  --channel-url 'https://www.youtube.com/channel/UCg9zI64pEB_3xwJPur6isiQ' \
  --max-posts 5 --download-images

python3 youtube_posts/scripts/analyze_posts.py \
  --input '/Users/nickzhang/云聪帖子全集/2026-10/posts.json' \
  --output-dir '/Users/nickzhang/云聪帖子全集/2026-10/analysis' \
  --transport ssh --ssh-host llm-server --base-url http://127.0.0.1:8002/v1
```

## 凭据

Mac mini Cookie 只从视频下载板块已经验证的外置路径读取。不得通过本机命令行复制 Cookie 内容、终端输出、Git 或分析证据包。账号可见范围由登录账号本身决定；不绕过访问限制。
