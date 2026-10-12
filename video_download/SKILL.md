---
name: yuncong-member-video-download
description: "下载用户本人有访问权限的云聪 YouTube 会员回放，支持单视频、明确清单的批量调度、暂停续跑和经授权的频道日期范围发现；复用本机或 Mac mini 登录状态，核验音视频完整性。用于会员视频下载与验收，不自行扩展日期或频道，也不转录或公开发布。"
---

# 云聪会员视频下载

正式代码在本技能的 `scripts/`，本机目录为 `/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/video_download`。三个板块由父项目统一Git管理，下载工具代码、技能和文档纳入 `LiYiqiang0827/video_study`。本机项目临时目录为 `/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/`，已加入 `.gitignore`，只用于下载暂存、测试脚本、验收图片和日志；任务结束清理，不把临时产物放入视频归档。最终验收的视频归档根目录为 `/Users/nickzhang/云聪视频全集/`，按 `YYYYMMDD_HHMMSS_标题/` 建目录。Cookie、账号凭据和运行环境放在项目外，不放入 `tmp/` 或Git。旧本机 `~/YuncongVideoAnalysis` 已删除；不要依赖或重建该本机路径。若在Mac mini上执行，须明确该机自己的远端工作目录，不能把远端路径误作本机资料根。入口见 [README.md](README.md)。

把用户指定、本人可访问的会员回放保存为本地文件。使用技能不等于已获准下载；依照本次请求和已有授权确定链接、机器和输出范围。用户只要求创建技能时，不启动真实下载。

## 工作方式

1. 确认明确的视频链接、执行机器，以及已知播放器总时长。复用已授权的账号、SSH 和导出文件；已有有效登录状态时不要重复要求登录或导出。未知链接或实际认证失败才询问必要信息。
2. 新机器或依赖不全时读 [references/environment.md](references/environment.md)。优先复用独立环境，运行 `scripts/download_video.py --check`；确保使用显式启用的 Node（已验证基线要求 Node ≥22）、FFmpeg、FFprobe 和相匹配的 yt-dlp / EJS。不要自动升级全局工具。
3. 使用随附的 [scripts/download_video.py](scripts/download_video.py)。默认单独 `web_safari`，只选 HLS，720p，缺片段即失败，整段严格解码。Mac mini 出现频繁TLS断连时，用 [scripts/run_resilient_batch.py](scripts/run_resilient_batch.py) 执行已冻结批次，复用已验证的系统curl媒体路径：每请求最多额外重连6次，整条视频总预算按时长60～600次，单视频10～60分钟时限。普通下载器总额外重连仅6次，不能误当作此成功路径。具体参数与续跑边界见 [references/batch.md](references/batch.md)。
4. 核对 `result.json`：网页时长、冻结参考时长、实际时长分别记录；差异须 ≤2 秒，音视频轨起点和长度一致。`technical_checks_passed` 才表示程序技术验收通过；`--probe-only` 只证明资料可读取，其他待核状态与失败必须保留。仅有网页时长时说明缺少独立时长参考。
5. 用 `--frames` 保存中段及末段图片，通过图像查看工具检查实际画面；文字或图表被遮挡时补查邻近帧。整段解码通过不等于人工试听过，也不证明内容语义完整。只报告实际做过的检查。复制到另一台机器后比较 SHA256。
6. 返回视频链接、本地绝对路径、时长、实际分辨率、大小及验收结论。只处理用户指定的范围；明确授权的多个链接可逐个执行，认证/权限/验证失败时停止该登录状态下的后续任务。

## 批量与日期范围

明确清单用 [scripts/download_batch.py](scripts/download_batch.py)；操作格式、状态文件、暂停与续跑读 [references/batch.md](references/batch.md)。默认逐个下载、每场间隔7秒。任务文件固定视频ID与参考时长，已完成文件需重新比对大小及SHA256才跳过；失败与待核分开，不循环重试同一错误。

用户指定频道和月份时，先用 [scripts/discover_members.py](scripts/discover_members.py) 建立会员视频清单，再初始化批次。频道必须由用户链接或已确认样本解析，不从名字猜测。相对日期仅用于近期窗口的宽范围预筛，最终纳入以原始上传时间为准，不按标题日期筛选；边界日期、权限、元数据缺失与目录截断必须保留待核。只有清单发现为 `ready` 才自动进入下载阶段。

## 视频命名

下载暂存文件按 `视频标题_上传时间.mp4` 命名，中文标题来自 YouTube 返回元数据；文件名只清理非法字符并限制长度。能取得真实上传时间戳时，转为北京时间，形如 `标题_20261002_111344.mp4`；只有上传日期则用 `标题_20261002.mp4`，不得补造时分秒。日期也缺失时标记 `上传时间未知`。不要拿标题中的日期、下载时间或直播首映开始时间代替上传时间。

正式归档遵循视频全集现有命名：目录名和视频文件名共用 `YYYYMMDD_HHMMSS_标题` 主干，例如 `20261002_111344_标题/20261002_111344_标题.mp4`。主干中的时间必须来自 YouTube 上传时间戳并换算为北京时间；归档前将已验收的暂存文件改为这个主干，不复制未通过验收的 `.part` 或媒体文件。只有日期精度时依既有归档规则处理，不补造时分秒。

`result.json` 保留原始标题、`upload_date`、上传时间戳 `timestamp`、另列的 `release_timestamp`、北京时间与日期精度。视频 ID 仍作为目录名，同名文件不覆盖。给旧视频改名时复用其元数据，缺少上传日期可单独读取网上资料，毋须重新下载媒体；保留原路径引用时可用硬链接，不再占一份视频空间。

## 单视频调用

使用执行机器上已有环境，把变量换成实际路径；`expected_seconds` 是这一个视频的冻结参考时长，不是固定常数。

```sh
python scripts/download_video.py \
  --url 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --root '/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/download_staging' \
  --job-dir '/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/download_staging/jobs/VIDEO_ID' \
  --cookie-file '/private/path/outside-project/youtube.com_cookies.txt' \
  --expected-duration expected_seconds --frames
```

脚本目录相对于本技能；用独立环境的 Python 执行，不依赖当前目录。每次本机任务都显式将 `--root` 和 `--job-dir` 指向项目 `tmp/` 下的新任务目录，避免默认路径重新创建已删除的本机 `~/YuncongVideoAnalysis`。批次目录固定视频ID与配置以支持续传。已合并文件完整验收后，再由字幕归档流程写入 `/Users/nickzhang/云聪视频全集/YYYYMMDD_HHMMSS_标题/`；归档是正式产物，下载暂存、帧图和日志属于临时产物，应留在 `tmp/` 并在任务闭合后清理。Cookie 单独指定为项目外的私密文件，不放进项目 `tmp/`。

本地文件重验不读取 Cookie，也不访问网络：

```sh
python scripts/download_video.py --verify-file '/absolute/video.mp4' \
  --expected-duration expected_seconds --frames
```

`--decode sample` 仅作快速排查；抽样通过状态为 `sample_checks_passed_full_decode_pending`，退出 2，不能计为完整成功。正在直播、预约或直播后尚在处理的视频不进入此下载流程。

## 认证与故障

Cookie 使用 Netscape 单站导出，仅含 `youtube.com` 及其子域，权限 0600。只在执行机器读取并注入内存；不读取浏览器密码库，不保存 cookiejar，不打印 Cookie、认证头、原始异常或签名媒体地址，不保存完整 yt-dlp 返回对象。不向子代理或外部服务传递认证材料。

依赖排错和 Cookie 操作读 [references/environment.md](references/environment.md)；时长差异、TLS、403、PO Token、客户端选择读 [references/troubleshooting.md](references/troubleshooting.md)。错误类别可以是多个警告的并集，须结合最终错误与阶段判断，不能从单条警告认定根因。

传输错误只做有限重连，TLS 验证始终开启。遇到登录、会员权限、Google 验证或 CAPTCHA 时停止相关认证作业，请用户完成浏览器操作；不尝试绕过。macOS 阻止 SSH 访问受保护目录时，让用户把单个导出文件放到约定目录，不扩大文件系统权限。

技能、脚本可保存到代码仓库；实际 Cookie、会员视频、验收图片、私人结果和虚拟环境不进入 Git 或公开上传。截图只是视频内容，不是浏览器外层页面地址。转录、内容研究、批量发现或上传需要对应的用户任务，不由本技能自行追加。
