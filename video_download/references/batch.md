# 批量调度、暂停和续跑

使用执行机器的独立 Python 运行 `scripts/download_batch.py`。先有明确链接清单，或先按用户指定的频道、日期发现清单。不要把“实现批量工具”当成下载整个频道的授权。

本机正式代码目录为 `Documents/ChatGPT/云聪股票交易分析/video_download`，脚本相对目录为 `scripts/`。历史34视频批次的系统curl支持与同步脚本也已归入这里，读 [README.md](../README.md)；原有固定清单、哈希、重试边界保持有效。整理或环境检查不启动续跑。

## 清单输入

支持 TXT（每行一个ID或URL）、CSV（`url`或`video_id`、可选`expected_duration_seconds`）及 JSON 列表：

```json
[
  {"video_id": "R76cFJaOYoo", "expected_duration_seconds": 584},
  "https://www.youtube.com/watch?v=OTHER_ID_11"
]
```

ID必须为真实11位值；示例第二项是格式占位符。重复ID去重，已知时长补充未知时长；两个冲突的已知时长报错。URL会化为单个视频的公开watch链接，不把播放列表或频道当作视频。已有批次同名初始化只接受完全一致的冻结清单，不静默追加或改变视频。

```sh
python scripts/download_batch.py init --list '/private/videos.json' \
  --root '/private/YuncongVideoAnalysis' --name 'yuncong_202608_202609'
python scripts/download_batch.py run \
  --batch-dir '/private/YuncongVideoAnalysis/batches/yuncong_202608_202609' \
  --cookie-file '/private/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt'
```

Mac mini 实际下载配置需补 `--proxy socks5h://127.0.0.1:7890 --impersonate-chrome --tls-connection-retries 2`，只在现有代理可用时使用。默认720p、Safari/HLS、整段严格解码、保存两帧；每个视频7秒间隔。

## 状态与续跑

- `manifest.json`：固定清单、参考时长、画质及清单哈希。
- `state.json`：逐个视频状态、尝试次数、已验收文件收据与当前任务。
- `status.json`：总数、完成、失败、待核、待处理、当前视频与更新时间。
- `items/<视频ID>/`：固定下载目录，内有 `job.json`、`result.json`、媒体与未完成的 `.part`。
- `PAUSE.json`：暂停请求；下一次明确执行 `run` 会清除这次请求并续跑。

```sh
python scripts/download_batch.py status --batch-dir '/private/root/batches/BATCH'
python scripts/download_batch.py pause --batch-dir '/private/root/batches/BATCH'
# 与首次run相同的参数，可继续pending/interrupted，已完成项目核验后跳过。
python scripts/download_batch.py run --batch-dir '/private/root/batches/BATCH'
# 原因处理后，才显式重试failed/review；其它参数保持首次配置。
python scripts/download_batch.py run --batch-dir '/private/root/batches/BATCH' --retry-failed
```

`Ctrl+C`、终止信号和暂停请求会中断当前子进程并保存可恢复状态。根目录锁由下载子进程继承，即使父调度被强制结束，仍阻止本工具的其他批次重复启动；单视频目录另有锁。默认每个视频最多3次真实下载调用，每次run至多调度同一视频一次；本地重验不消耗下载次数。`--max-runtime-minutes` 可设置本轮时间上限。缺失或改变的已完成文件不能跳过；哈希改变保留待核，不覆盖。

认证、会员权限、Cookie格式、依赖或限流问题会阻断批次；连续两个传输失败会暂停。已恢复警告不使成功项目变失败。状态为 `completed_with_issues` 时仍需处理失败/待核，不能说全部完成。程序技术验收、帧抽查、人工试听的覆盖程度分别报告。

## 已授权频道的日期发现

```sh
python scripts/discover_members.py \
  --channel-id 'UCg9zI64pEB_3xwJPur6isiQ' \
  --start-date 20260801 --end-date 20260930 \
  --root '/private/YuncongVideoAnalysis' \
  --cookie-file '/private/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt'
```

这个频道ID已由用户指定样本解析为“云聪金融交易分析”；新频道仍需重新核对。相同Mac mini网络参数也用于发现。源码使用YouTube会员专用 `UUMO + channel_id[2:]` 自动目录，完整翻页后保存白名单清单。目录翻页整体最多30次瞬时TLS重连，每个请求最多2次；单视频仍按原来的6次总上限。认证、证书与限流不在重连范围。

元数据有缓存和独立锁，完整缓存可离线重建清单，不读取Cookie。每次重新计算会员/日期入选条件，不信任旧的派生标签。失败显式 `--retry-failed`，每项最多3次；损坏缓存默认明确报错，带该标志时先保留无效原文件，再重新获取。状态在持锁期间写完，另一频道不能复用同日期的发现目录；不保存完整响应或媒体链接。

输出在 `root/discovery/<开始日期>_<结束日期>/`：`member_inventory_v2.json`、逐视频`metadata/`、`selected_metadata.json`、`videos.json`与`discovery_status.json`。近期180日内的窗口默认先用相对日期加前后62天宽范围预筛；未知相对日期仍逐个核对。相对日期有近似误差，此层不是精确日期证据；`--exact-all`可关闭预筛，对整个会员目录逐个查询，适用于完整目录审计。最终入选依照上传时间戳转北京时间，并确认会员属性；仅有日期时不虚构时区，窗口边界保留待核。首映/直播开始时间不代替上传时间。

发现为 `ready` 后才把 `videos.json` 交给 `init`。`requires_review`、目录截断、权限/网络阻断时不得宣称完整发现或自动扩大范围。参考时长来自发现时冻结的网页资料，未逐条经过人工独立确认，报告时应说明。
