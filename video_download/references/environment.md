# 执行环境与 Cookie

## 已验证环境

2026-10-09，用户指定的会员视频在 Mac mini 下载通过，之后复制到本机并核对哈希。复用这些资源前检查是否仍存在，不把路径当成其他机器的约定。

| 项目 | 本次已验证值 |
| --- | --- |
| SSH 别名 | `macmini`，用户已授权时可用 |
| 私人工作目录 | `/Users/nickzhang/YuncongVideoAnalysis` |
| 独立 Python | `/Users/nickzhang/YuncongVideoAnalysis/download_tools/venv/bin/python` |
| 单站导出 | `/Users/nickzhang/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt` |
| Mac mini 工具路径 | `/opt/homebrew/bin`，备用 Node 在 `~/.local/bin` |
| 下载网络 | 已有 mixed-port 7890 的 `socks5h://127.0.0.1:7890`，Chrome 网络后端 |
| Python 包 | yt-dlp 2026.08.19、yt-dlp-ejs 0.8.0、curl-cffi 0.16.3 |
| 桌面原始指南备份 | `~/Desktop/YouTube会员视频下载_AI接手指南.md` |
| 私人实测记录 | `~/YuncongVideoAnalysis/download_tools/MAC_TEST_RESULT.md` |

上面的代理只在该环境已存在且可用时使用。不更改系统代理、不替用户切换账号或浏览器安全设置。新建独立环境使用随附 `scripts/requirements.txt`，这是已验证版本组合，不是“永远最新版本”。

```sh
python3 -m venv '/absolute/private/root/download_tools/venv'
'/absolute/private/root/download_tools/venv/bin/python' -m pip install \
  -r '/absolute/skill/scripts/requirements.txt'
```

Python ≥3.11、Node ≥22；显式配置 Node 是必需步骤，脚本会找到 PATH 中的 Node 并交给 yt-dlp。复用已装的 FFmpeg/FFprobe；缺少工具时按机器实际配置补齐，无须为了本任务全局重装 Python。首次运行 `--check` 并检查工具版本。

## Mac mini 运行方式

把本技能目录同步到 Mac mini 的 `~/.codex/skills/yuncong-member-video-download`，使用那台机器的 Python、Cookie 和代理。也可把完整 `scripts` 目录复制到私人 `download_tools` 并替换调用路径；脚本间有依赖，不能只复制单个文件。不要把 Cookie 复制到本机或另一代理去做下载。

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 macmini \
  "env PATH=/opt/homebrew/bin:/Users/nickzhang/.local/bin:/usr/bin:/bin /Users/nickzhang/YuncongVideoAnalysis/download_tools/venv/bin/python /Users/nickzhang/.codex/skills/yuncong-member-video-download/scripts/download_video.py --check"
```

实际下载要在远端命令中也引用带 `?` 的视频 URL，否则远端 zsh 会把它当成通配符。用正确的 shell 参数引用；不能用 `JSON.stringify()` 代替 shell 转义。长下载使用可轮询的执行会话，定期报告实际文件增长和验收结果，不并发启动同一视频的重复实例。

已验证配置的调用示例，视频 ID 和参考秒数必须换成本次输入：

```sh
env PATH=/opt/homebrew/bin:/Users/nickzhang/.local/bin:/usr/bin:/bin \
  /Users/nickzhang/YuncongVideoAnalysis/download_tools/venv/bin/python \
  /Users/nickzhang/.codex/skills/yuncong-member-video-download/scripts/download_video.py \
  --url 'https://www.youtube.com/watch?v=VIDEO_ID' \
  --cookie-file /Users/nickzhang/YuncongVideoAnalysis/local_auth/www.youtube.com_cookies.txt \
  --proxy socks5h://127.0.0.1:7890 --impersonate-chrome \
  --tls-connection-retries 2 --expected-duration expected_seconds --frames
```

## 仅在认证需要更新时导出

不用把视频完整播放或等缓冲结束，浏览器能够正常开始播放即可。先确认是同一会员账号。正常导出有效时继续使用；不要要求每次下载都重复导出。

若浏览器可播放而下载器实际认证失效，可按 yt-dlp 的官方方式减少 Cookie 轮换：

1. 在 Chrome 按 `⌘⇧N` 打开无痕窗口，登录用户自己的会员账号。
2. 需要时在普通窗口 `chrome://extensions` 的 Get cookies.txt LOCALLY 详情中，由用户启用“在无痕模式下启用”。
3. 在同一个、唯一的无痕标签打开 `https://www.youtube.com/robots.txt`。
4. 使用插件导出 `youtube.com` 的 Netscape Cookie，保存到用户约定的 `local_auth`，关闭该无痕窗口。

只检查存在性、修改时间、格式与域名，不显示内容。导出名称可能为 `www.youtube.com_cookies.txt`，不要因不同于旧脚本的 `youtube.cookies.txt` 就认定未保存。macOS TCC 阻止 SSH 访问 Desktop/Downloads 时，不扫描或绕过；请用户把单个文件移到约定目录。

官方来源：

- [Cookie 导出](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies)
- [EJS 与 JavaScript 运行时](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [安装与可选依赖](https://github.com/yt-dlp/yt-dlp#installation)

只有排查变化时再查阅官方最新版；成功路径不需要每次重新检索。
