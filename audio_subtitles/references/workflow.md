# 下载后的正式工作流程

正式Qwen通过SSH在Mac mini启动Python任务：本流程没有常驻HTTP服务，也不通过oMLX。本文命令中 `/Users/nickzhang/YuncongVideoAnalysis/...` 全是Mac mini远端路径；本机旧同名目录已删除。本机的临时清单、同步结果和测试文件只写入 `/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/`，正式归档只写入 `/Users/nickzhang/云聪视频全集/`。运行脚本、manifest和完整源视频都必须预先位于Mini本地；闭合结果再经SSH同步回本机核对SHA。数据目录可通过 `YUNCONG_DATA_ROOT` 修改；示例中的 `new_batch` 是新任务目录，不能直接对已完成的老批次重新建池。

先把本板块 `audio_subtitles/` 代码和用户授权的视频同步到Mac mini，在Mini本地启动批次。Mac mini已配好的环境使用以下远端代码路径。路径有中文或空格时必须保留引号。

本机把代码同步到Mini（不带入Cookie、模型、视频或处理结果）：

```sh
PROJECT='/本机实际克隆目录/video_study'
REMOTE_TOOLS='/Users/nickzhang/YuncongVideoAnalysis/runtime/video_study/audio_subtitles'
rsync -a --exclude '__pycache__/' --exclude '*.pyc' "$PROJECT/audio_subtitles/" "macmini:$REMOTE_TOOLS/"
```

把明确授权的MP4也同步到Mini本地资料目录后，用已完整验收且位于Mini的 `result.json` 建单项清单。确保清单引用的是Mini本地视频绝对路径。下面的 SSH 命令在Mini运行清单构建、音频提取、模型推理、对齐和字幕导出：

```sh
ssh macmini 'bash -s' <<'REMOTE'
TOOLS=/Users/nickzhang/YuncongVideoAnalysis/runtime/video_study/audio_subtitles
PYTHON=/Users/nickzhang/YuncongVideoAnalysis/asr_tools/venv/bin/python
ROOT=/Users/nickzhang/YuncongVideoAnalysis/asr_batches/new_batch
RESULT=/Users/nickzhang/YuncongVideoAnalysis/batches/本批次/items/视频ID/result.json

python3 "$TOOLS/scripts/build_manifest.py" --result "$RESULT" --output "$ROOT/input_manifest.json"
"$PYTHON" -B "$TOOLS/scripts/batch_asr.py" \\
  --manifest "$ROOT/input_manifest.json" --job-root "$ROOT/job" \\
  --python "$PYTHON" --models-dir /Users/nickzhang/YuncongVideoAnalysis/asr_tools/models \\
  --ffmpeg /opt/homebrew/bin/ffmpeg --ffprobe /opt/homebrew/bin/ffprobe --limit 1
REMOTE
```

Mac mini上的 `mlx-audio` 加载 `Qwen3-ASR-1.7B-8bit` 与 `Qwen3-ForcedAligner-0.6B-8bit`。一次真实新推理需先完成该任务；依赖与模型清单检查只验证配置及文件是否存在，不会实际加载模型。

```sh
TOOLS='/Users/nickzhang/YuncongVideoAnalysis/runtime/video_study/audio_subtitles'
python3 "$TOOLS/scripts/check_environment.py" --mode basic
```

## 一、从完整下载建立清单

用单个结果或用户指定的本地下载目录，读取完成状态、实际上传时刻、时长、字节数和 SHA。不联网发现其他视频。

```sh
python3 "$TOOLS/scripts/build_manifest.py" \
  --download-root /Users/nickzhang/YuncongVideoAnalysis/batches/新下载批次/items \
  --output /Users/nickzhang/YuncongVideoAnalysis/asr_batches/new_batch/input_manifest.json
```

单视频改用 `--result 完成下载的result.json`，可重复传入多个 `--result`。缺少完整下载验证或秒精度时间时不编造元数据；同视频多份成功结果应明确选一份。已有不同清单不覆盖，换输出文件和任务目录。

## 二、批量音频提取、转写与对齐

```sh
python3 "$TOOLS/scripts/batch_asr.py" \
  --manifest /Users/nickzhang/YuncongVideoAnalysis/asr_batches/new_batch/input_manifest.json \
  --job-root /Users/nickzhang/YuncongVideoAnalysis/asr_batches/new_batch/job \
  --python /Users/nickzhang/YuncongVideoAnalysis/asr_tools/venv/bin/python \
  --models-dir /Users/nickzhang/YuncongVideoAnalysis/asr_tools/models
```

本步会复用正式 `extract_audio.py`、`asr_engine.py` 和 `export_subtitles.py`，不需要先手工提取每个音频。默认块长最大 180 秒，保留完整时间轴；不能为节省时间只转写开头却声称完整。正式任务在Mac mini通过SSH启动Python，使用 `~/YuncongVideoAnalysis/asr_tools/venv/bin/python` 和同目录 `models/` 中的Qwen ASR、ForcedAligner，由 `mlx-audio` 在Apple Silicon运行。任务脚本、manifest及完整视频须先存在Mini本地。本流程没有常驻HTTP服务，也不使用oMLX；运行前确认没有重复worker。具体参数见runtime文档。

`state.json`、`events.jsonl` 和每个 `视频ID/attempt_NNN/` 均在任务目录内。重试保留旧尝试。`--resume` 续跑；诊断后 `--resume --retry-failed` 可重试，单次不无限重试，每个视频最多两个尝试。`--limit 1` 可先验证单项，`--status` 只读进度。创建任务目录内的 `STOP_REQUESTED` 可停止当前子进程，移除后再续跑。已有 worker 或 GPU 子进程未结束时不另起重复任务。

## 三、正式五文件导出

任务完成后在计算机本地运行，或先镜像到本机再运行。为旧原始结果重建时使用新的导出目录：

```sh
python3 "$TOOLS/scripts/export_subtitles.py" \
  --job-root /Users/nickzhang/YuncongVideoAnalysis/asr_batches/new_batch/job \
  --output-root /Users/nickzhang/YuncongVideoAnalysis/asr_batches/new_batch/subtitle_exports \
  --once
```

`--watch` 等待当前批次闭合结果，不会重新调用模型。状态写在输出根的 `SUBTITLE_AUTO_STATUS.json`，每项包含真实本机生产目录和当前尝试路径，便于归档。成品为 `transcript.srt/vtt/md/json` 与 `AUTO_EXPORT_PROVENANCE.json`。

单个已有 RUN：

```sh
python3 "$TOOLS/scripts/export_subtitles.py" \
  --run-dir /Users/nickzhang/YuncongVideoAnalysis/asr_results/完整原始RUN \
  --output-dir /Users/nickzhang/YuncongVideoAnalysis/asr_results/新的字幕目录 \
  --video-id R76cFJaOYoo
```

有界的显示过滤登记用 `--blocked-spans 外部JSON`，不得默认沿用历史字符索引。批次格式为 `{"videos":{"视频ID":{"attempt_001":[{"reason":"suspected_prompt_echo","raw_span":[开始字符,结束字符],"text":"对应的准确原文"}]}}}`；单项也可用此格式或条目列表。工具核对精确文本并保留原文/units。明确过滤长重复拟声可用 `reason: long_repetition`。对正常财经短词不得使用污染登记作为猜改工具。

## 四、统一七文件目录

```sh
python3 "$TOOLS/scripts/organize_video_collection.py" \
  --job-root /Users/nickzhang/YuncongVideoAnalysis/asr_batches/new_batch/job \
  --export-status /Users/nickzhang/YuncongVideoAnalysis/asr_batches/new_batch/subtitle_exports/SUBTITLE_AUTO_STATUS.json \
  --output-root /Users/nickzhang/云聪视频全集
```

目录与文件共用 `YYYYMMDD_HHMMSS_标题`，来源 JSON 加 `_来源记录`。SHA 与当前尝试必须匹配；不能以旧字幕配新的音频。现有旧归档文件可按记录原地迁移命名，内容一致则跳过，不覆盖用户编辑。

已有单视频成品可用 `--extra-items-json 外部列表` 增加，列表条目需包含 `video_id/title/upload_time_beijing/upload_time_precision/video_path/video_sha256/bytes/production_directory/provenance_sha256/audio_path/audio_sha256/audio_bytes`，必要时从实际文件核验生成。这是明确的本地资料清单，不会自行扩展下载范围。

仅归档单视频时，`--job-root` 和清单可放本机项目 `tmp/` 下的新任务目录，不必构造批次状态；在没有批次快照时，工具仅处理明确传入的附加列表，正式视频写入归档根目录，临时报告目录保存 `COLLECTION_LAYOUT.json`：

```sh
python3 "$TOOLS/scripts/organize_video_collection.py" \
  --job-root /Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/asr_reviews/单视频归档报告 \
  --extra-items-json /Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/asr_reviews/单视频归档清单.json \
  --output-root /Users/nickzhang/云聪视频全集
```

## Mac mini 到本机

同步代码工程可使用普通代码复制或 Git 克隆；模型、源视频和 runtime 另行复用或配置。已有 SSH 别名 `macmini` 时：

```sh
python3 "$TOOLS/scripts/sync_asr_batch.py" \
  --host macmini \
  --remote-root /Users/nickzhang/YuncongVideoAnalysis/asr_batches/new_batch/job \
  --job-root /Users/nickzhang/Documents/ChatGPT/云聪股票交易分析/tmp/asr_sync/new_batch/mirror \
  --once
```

同步只复制已闭合尝试并核对声明 SHA，失败项也可作为诊断镜像，但不代表可导出。原 worker 的绝对路径通过 `local_sync_status.json` 映射到本机；字幕使用映射后的当前尝试。媒体另有本机副本时，若远端视频路径不同，归档加 `--media-manifest 本机清单.json`，工具按视频 ID 和 SHA/字节匹配，只替换本机视频路径。

## 现有结果

2026 年 8—9 月 34 个批量视频，加之前单独处理的 R76 样本，已在 `/Users/nickzhang/云聪视频全集` 形成归档。该路径是本机正式媒体与交付目录。Mac mini 上的 `~/YuncongVideoAnalysis` 是远端ASR代码、模型和批次环境；本机同名目录已删除，归档不通过旧路径兼容软链接访问。重新组织应复用原 `COLLECTION_LAYOUT.json`、状态与来源记录；不要因迁入新工具工程或目录移动而重跑模型或覆盖已有成品。目录数和文件数会随新任务变化，汇报时读取当前状态。临时测试放项目 `tmp/`，结束后删除。
