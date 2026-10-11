# 视频下载

本板块提供 `yuncong-member-video-download` 技能及用户有访问权限的云聪会员回放下载工具。代码、文档与另外两个板块一起提交到主项目GitHub仓库。旧独立Git已归档，当前目录使用父项目Git，不建立子模块。

## 文件与入口

| 文件 | 用途 |
| --- | --- |
| `SKILL.md`、`agents/openai.yaml` | 技能说明与发现配置 |
| `scripts/download_video.py` | 单视频、续传、音视频完整性核验与命名 |
| `scripts/download_batch.py` | 明确清单调度、暂停及有限重试 |
| `scripts/discover_members.py` | 授权频道与日期范围的会员清单发现 |
| `scripts/video_naming.py` | 真实上传时间与标题命名 |
| `scripts/system_curl_transport.py` | 系统curl传输模块 |
| `scripts/download_batch_worker.py`、`run_batch_relaxed.py`、`sync_relaxed_batch.py` | 既有冻结34视频批次的专用执行与同步，保留范围和哈希核验 |
| `scripts/requirements.txt` | 下载依赖 |
| `references/` | 环境、批次和故障说明 |

## 运行与资料

```sh
DOWNLOAD_TOOLS='/实际克隆目录/video_download'
PYTHON='/实际外置下载环境/bin/python'
"$PYTHON" -B "$DOWNLOAD_TOOLS/scripts/download_video.py" --check
"$PYTHON" -B "$DOWNLOAD_TOOLS/scripts/download_batch.py" --help
```

新机器按 [environment](references/environment.md) 准备环境。通用工具可迁移；三个历史批次专用脚本仍绑定原资料目录和冻结清单，不作为任意新批次入口。原部署脚本通过兼容链接指向新位置。整理或检查不启动下载。

输入是用户指定链接、执行范围及必要的外置登录状态。下载脚本和登录状态应在有权限的执行机器上运行。输出在工程外，包括MP4、`result.json`、技术验收和来源信息。完成下载后，由相邻 `audio_subtitles/` 建清单、提取音频与字幕。

## 三板块衔接

数据迁移到Mac mini时，通过SSH/rsync同步用户授权的视频文件和本板块代码，Mini本地脚本只处理明确指定的视频。后处理的ASR仍由 `audio_subtitles/` 的独立技能负责：该板块脚本和待转写视频均需预先位于Mac mini，实际用SSH启动本地Qwen任务。观点及PPT在本机的 `analysis_ppt/` 继续处理同步回来的字幕和视频。认证文件、Cookie和模型不随代码同步，参考 [全项目说明](../README.md)。

Cookie、认证头、签名媒体地址、实际视频和私人结果均不随代码上传。将来更新在项目根目录执行普通Git提交，不再操作独立子仓库。
