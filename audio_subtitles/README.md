# 音频提取与字幕提取

输入是已完整下载的本地视频或冻结下载清单，输出是可播放M4A、识别用WAV、句级字幕和全文。技能为 `yuncong-video-subtitles`。本板块的Python模块全部位于自己的 `scripts/`，不依赖观点板块。

## 目录

- `SKILL.md`、`agents/openai.yaml`：技能和发现配置。
- `scripts/`：音频提取、Qwen转写对齐、字幕导出、批次管理及来源校验。
- `references/workflow.md`：完整批次、字幕重建、同步与七文件归档。
- `references/runtime.md`：本机和Mac mini环境、模型准备与恢复。
- `requirements-asr.txt`：Apple Silicon MLX推理依赖，安装在工程外。

## 主要入口

| 脚本 | 用途 |
| --- | --- |
| `check_environment.py` | basic/asr环境检查，不调用模型 |
| `extract_audio.py` | 原音轨M4A与16kHz单声道PCM16 WAV |
| `asr_engine.py`、`batch_asr.py` | Qwen ASR与ForcedAligner、完整批次处理 |
| `export_subtitles.py` | 从闭合原始RUN导出五份正式字幕产物 |
| `build_manifest.py` | 从已完整下载结果构建清单并核对SHA |
| `sync_asr_batch.py` | Mac mini闭合结果同步 |
| `organize_video_collection.py` | 七文件命名、归档和校验 |
| `prepare_asr_models.py` | 正式两个模型准备与核验 |

其余文件是上述入口使用的字幕策略、存储和路径模块。

```sh
SUB='/实际克隆目录/audio_subtitles'
python3 "$SUB/scripts/check_environment.py" --mode basic
python3 "$SUB/scripts/extract_audio.py" --video '/绝对路径/视频.mp4' --output-dir '/工程外/新音频目录'
python3 "$SUB/scripts/export_subtitles.py" --run-dir '/已有闭合RUN' --output-dir '/工程外/新字幕目录' --video-id VIDEO_ID
```

M4A复制AAC原音轨，WAV保留完整时间轴。五份字幕产物是 `transcript.json/md/srt/vtt` 和 `AUTO_EXPORT_PROVENANCE.json`。最终归档包含MP4、M4A、SRT、VTT、MD、JSON和来源JSON，七个文件共用上传时间与标题主文件名。

已有原始结果导出不等于新ASR推理。复测报告须说明实际运行的是哪条路径。字幕完成后交给相邻 `analysis_ppt/`，不在这里生成观点或PPT。

## Mac mini Qwen 推理

正式识别通过SSH启动Mac mini的Python任务。流程没有常驻HTTP服务，不通过oMLX。`Qwen3-ASR-1.7B-8bit`识别中文，`Qwen3-ForcedAligner-0.6B-8bit`生成文字时间对齐，两者由 `mlx-audio` 在Mac mini Apple Silicon上运行。

```text
~/YuncongVideoAnalysis/asr_tools/venv/bin/python
~/YuncongVideoAnalysis/asr_tools/models/Qwen3-ASR-1.7B-8bit/
~/YuncongVideoAnalysis/asr_tools/models/Qwen3-ForcedAligner-0.6B-8bit/
```

SSH启动任务前，把本板块脚本同步到Mac mini，输入MP4也必须在那里；启动时给 `batch_asr.py` 指定上述Python和模型目录。音频提取、最长180秒连续分块、ASR、ForcedAligner和字幕导出都由该任务在Mini上执行。完成后 `sync_asr_batch.py` 经SSH取得闭合结果并核对哈希，再由本机归档。不可把远端模型状态描述为本机已安装；当前无正在运行的转写任务时才启动复测。详见 [Mac mini运行环境](references/runtime.md) 和 [完整批次流程](references/workflow.md)。
