# 云聪 YouTube 频道 Posts

本板块采集云聪 YouTube 频道 Posts（频道动态），保存文字、原始发布时间、帖子链接、配图、投票信息和关联视频，并按月生成图文 PPT。Posts 正式归档独立放在 `/Users/nickzhang/云聪帖子全集/`，与视频、字幕和视频观点 PPT 使用的 `/Users/nickzhang/云聪视频全集/` 完全分开。帖子正文也可送到远端 GPU Qwen 服务做带来源编号的主题/观点梳理。

## 项目文件

```text
youtube_posts/
├── README.md
├── SKILL.md
├── agents/openai.yaml
├── scripts/
│   ├── collect_posts_via_macmini.py       SSH 启动 Mac mini 抓取、同步、验哈希并按月归档
│   ├── collect_posts.py                   Mini 端提取帖子；每张图片只下载最高分辨率版本
│   ├── archive_posts_by_month.py          按北京时间月份合并 JSON、原图和哈希清单
│   ├── generate_monthly_posts_ppt.mjs     生成月度图文 PPT
│   ├── monitor_new_posts.py                增量监控、更新归档/PPT并输出飞书通知
│   ├── install_posts_monitor.py            安装 Hermes 每半小时脚本任务
│   └── analyze_posts.py                   调用远端 GPU Qwen，保存分析与原始响应
├── prompts/analyze_posts.md               Posts 分析约束与输出格式
├── references/                            数据字段、授权边界和工作流程
└── tests/                                  不访问网络或模型的离线测试
```

## 采集与月度归档

采集固定在 Mac mini 执行，复用下载板块的 SSH 别名 `macmini`、独立 Python 环境、外置 YouTube Cookie 和代理。抓取脚本由本机临时 SCP 到 Mini，结果放在隔离任务目录，再通过 SCP 回传到项目 `tmp/` 并逐文件核对 SHA256。Cookie 从不传回本机，也不进入项目或 Git。YouTube Posts 使用网页解析适配器，页面或 yt-dlp 更新可能造成变化；不能把未经核对的空结果当成功。[官方 Posts 说明](https://support.google.com/youtube/answer/9409631) · [Data API 资源目录](https://developers.google.com/youtube/v3/docs) · [yt-dlp Posts renderer](https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_tab.py)。不得绕过账号权限、登录或地区限制。

云聪金融交易分析频道的已确认 ID 为 `UCg9zI64pEB_3xwJPur6isiQ`。历史全量归档可用 `--max-posts` 做小样本验证；监控模式从最新帖子开始，遇到首条已归档 ID 后停止：

```sh
POSTS='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/youtube_posts'
python3 "$POSTS/scripts/collect_posts_via_macmini.py" \
  --channel-url 'https://www.youtube.com/channel/UCg9zI64pEB_3xwJPur6isiQ' \
  --download-images
```

默认使用 Mini Cookie `/Users/nickzhang/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt` 和代理 `socks5h://127.0.0.1:7890`。Cookie 不导出、不复制到 MacBook。图片按每个图片 renderer 的分辨率挑选一个最高质量地址，避免重复保存各种缩略尺寸。

归档优先按 YouTube `timestampUsec` 换算的北京时间月份分组。当前抓取到的 Posts 页面多数只给相对时间，没有精确时间戳。分钟/小时/天/周级相对时间可结合采集基准计算估算区间，但不能冒充精确发帖日期；月级时间不推算具体日。原始相对时间与估算基准都会保留，月份标记为估算。`1 year ago` 及更早、无法可靠分月的年份级帖子合并到 `未识别日期/` 的一个 PPT；PPT 封面依据归档采集月份推断一个粗略边界，例如采集月份为 2026-10 时写作“约 2025年10月以前（推断）”。这只是归档分组标签，不代表每条帖子的精确日期。按帖子 ID 去重；再次采集会更新相应归档 JSON 与媒体，再重建 PPT。

```text
/Users/nickzhang/云聪帖子全集/
├── 2026-10/
│   ├── posts.json
│   └── media/<帖子ID>/image_01.jpg
└── 未识别日期/
    ├── posts.json
    └── MANIFEST.json

根目录直接放置所有按月 PPT、历史年份合并 PPT 和根级 MANIFEST.json：
云聪帖子_2026-10.pptx、云聪帖子_约2025年10月以前（推断）.pptx
```

PPT 每条帖子单独编排，保留正文、页面显示的相对时间、帖子 ID、来源链接和可用配图。若有分钟/小时/天/周级相对时间，显示按采集基准推算的时间范围，并明确标注“估算”；若精确日期未提供，也会直接注明。年份级旧帖在单一 PPT 中统一显示原始相对时间和推断范围。长文拆页，完整结构化文本仍以各月份 `posts.json` 为准，所有原始图片仍在对应 `media/`。PPT 是浏览档案，不替代模型观点分析。构建使用 Codex Artifact Tool；候选稿与预览放项目 `tmp/youtube_posts/`，检查后直接复制到 `/Users/nickzhang/云聪帖子全集/` 根目录，再刷新根级和月份级清单。

## 定时监控新帖子

监控使用 YouTube Posts 页现有解析器，通过 SSH 在已登录的 Mac mini 上拉取最新动态。每条帖子的稳定 `post_id`（YouTube `backstagePostRenderer.postId`）作为去重键；本机各月份 `posts.json` 与归档根目录 `monitor_state.json` 保存已见 ID。每轮从最新帖子向旧帖遍历，遇到第一个已归档 ID 即停止，避免重复扫描历史；缺少历史 ID 时拒绝启动增量模式。采集或 PPT 验收失败会让本轮返回错误，不发新帖通知。

安装创建 Hermes 脚本任务，按 Mac 本机 Asia/Shanghai 时区每天 09:00 至 23:30、每半小时运行（09:00、09:30 … 23:30）。Hermes `--no-agent` 将脚本 stdout 原样送到当前保存的 Feishu 私聊，不调用聊天模型。没有新帖时 stdout 为空，保持静默。发现新帖后会下载配图、更新按月 JSON 和媒体、重建受影响月份的完整图文 PPT 到归档根目录、刷新清单，再发送帖子链接、原始相对时间和正文摘要。

```sh
POSTS='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/youtube_posts'
python3 "$POSTS/scripts/install_posts_monitor.py"
hermes cron list
```

任务名为 `yuncong-youtube-posts-monitor`。Hermes Gateway 和 cron scheduler 必须保持运行且 Feishu 已连接。脚本外壳位于 `~/.hermes/scripts/`，实现仍由本项目管理。ID 状态只保存在 `/Users/nickzhang/云聪帖子全集/monitor_state.json`，锁文件也只在该归档根目录，不进入 Git。运行记录可用 `hermes cron runs` 查看。

## 观点分析

使用和 `analysis_ppt` 相同的远端 GPU Qwen vLLM SSH 通道；不会调用 Mac mini ASR 模型，也不会自动切换到本机 Hermes 默认聊天模型。

```sh
python3 "$POSTS/scripts/analyze_posts.py" \
  --input '/Users/nickzhang/云聪帖子全集/2026-10/posts.json' \
  --output-dir '/Users/nickzhang/云聪帖子全集/2026-10/analysis' \
  --transport ssh --ssh-host llm-server \
  --base-url http://127.0.0.1:8002/v1
```

分析输出应标明帖子 ID、原句和日期；模型归纳属于讲者观点整理，不等于外部事实或投资建议，正式使用前需核对出处。

## 路径与保密

正式 Posts JSON、图片按月放在 `/Users/nickzhang/云聪帖子全集/YYYY-MM/`，全部月度 PPT 直接放在归档根目录，绝不写到视频归档 `/Users/nickzhang/云聪视频全集/`。项目临时同步、测试脚本、候选 PPT、预览图和日志放在代码库的 `tmp/`（Git 忽略）。Mac mini 临时任务位于 `~/YuncongVideoAnalysis/posts_tools/jobs/<随机任务ID>/`，完成或中止后清理。分析归档放在对应月份下；Cookie 和模型凭据留在项目外。

字段和完整操作步骤见 [数据格式](references/data_schema.md) 与 [工作流](references/workflow.md)。
