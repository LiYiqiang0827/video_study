# 共通本地音视频基建｜local-media-platform 2.1.0

更新：2026-09-30。可安装的 Python 共用核心，供不同项目处理各自明确指定的媒体。源码、模型缓存与项目结果分开；不依赖“推し活”或 TradingAgent 的路径。

## 1. 安装与检查

需要 Python 3.10+；本机实际验收为 Windows / Python 3.12。FFmpeg 和 ffprobe 必须可从 PATH 调用。其他操作系统尚未实测。

从交付包安装（本包没有发布到 PyPI，不要按名称从公共索引寻找）：

```powershell
# 在下载的 wheel 文件所在目录；只做画面复核的最小依赖
python -m pip install .\local_media_platform-2.1.0-py3-none-any.whl

# 需要语音、OCR 和 Windows NVIDIA GPU 时
python -m pip install ".\local_media_platform-2.1.0-py3-none-any.whl[asr,ocr,gpu]"

# 安装后可在任何项目目录执行，不必再设 PYTHONPATH
python -m media_platform --version
python -m media_platform doctor
```

源码包解压后也可运行 `python -m pip install ".[asr,ocr,gpu]"`。仅 CPU 识别省略 `gpu`。安装到项目虚拟环境时，每个环境分别安装同一个 wheel；模型缓存不需要随项目复制。

本机已在常用 Python 3.12 环境安装 2.1.0。若 `python` 指向其他环境，请在该环境安装，或使用 `py -3.12 -m media_platform doctor` 检查。

`doctor` 只列出版本、安装位置、依赖、FFmpeg 路径及可用显存，不加载模型、不下载、不修改文件；它不是 GPU 推理或准确率测试。图像基础依赖与 FFmpeg 齐全时返回成功，语音和 OCR 是否安装需看相应字段。

## 2. 四个共用命令

| 命令 | 用途 | 主要输出 |
| --- | --- | --- |
| `ingest` | 单媒体或明确文件夹的 ASR、抽帧与 OCR | manifest、speech、visual、evidence、chunks |
| `frames` | 定时或指定时间的原分辨率画面复核 | 原图、实际 PTS、联系表、可搜索 HTML、frame_manifest |
| `verify` | 校验已完成提取的源与记录输出 | JSON 验收结果；不重新识别 |
| `doctor` | 检查当前 Python 环境和资源 | JSON 环境清单 |

原有 `python -m media_platform.ingest`、`python -m media_platform.frame_review` 仍可用。`ingest` 原始默认语言保留日语 `ja`，新任务请总是明确写 `--language zh / ja / en`，避免沿用错误语言。

### 中文讲课或访谈

```powershell
python -m media_platform ingest "D:\OtherProject\lecture.mkv" --language zh --model large-v3 --device cuda --compute-type float16 --no-video --chunk-seconds 300 --output "D:\OtherProject\results\speech"

python -m media_platform frames "D:\OtherProject\lecture.mkv" --interval 60 --extraction "D:\OtherProject\results\speech" --output "D:\OtherProject\results\frames"

# 有歧义时补看关键图形，时间支持秒数、MM:SS 或 HH:MM:SS
python -m media_platform frames "D:\OtherProject\lecture.mkv" --times "14:05,14:10,14:15" --extraction "D:\OtherProject\results\speech" --output "D:\OtherProject\results\detail"

python -m media_platform verify "D:\OtherProject\results\speech"
```

### CPU、音频、OCR 和小区间

```powershell
python -m media_platform ingest "D:\OtherProject\interview.mp3" --language ja --device cpu --compute-type int8 --output "D:\OtherProject\results\audio"

python -m media_platform ingest "D:\OtherProject\screen.mp4" --no-audio --output "D:\OtherProject\results\ocr"

# 对疑似漏字的小区间关闭 VAD 后另存复核
python -m media_platform ingest "D:\OtherProject\interview.mp4" --language zh --start 14:20 --end 14:55 --vad off --no-video --output "D:\OtherProject\results\speech_detail"
```

带 `--no-ocr` 的画面提取无需 OCR 依赖。`frames` 不加载语音模型或 OCR，适合图表、白板和讲义的原图核对。普通 `ingest` 视频抽样/OCR 和原分辨率 `frames` 是不同产物，不能用 OCR 缩略图代替细节复核。

## 3. 配置、模型与项目隔离

| 设置 | 默认值 / 优先级 | 作用 |
| --- | --- | --- |
| `MEDIA_PLATFORM_HOME` | Windows `%LOCALAPPDATA%\media-platform`；其他系统 `~/.local/share/media-platform` | 用户级共用运行配置根目录 |
| 设备档案 | 命令行 `--profile` → 兼容调用显式档案 → `MEDIA_PLATFORM_PROFILE` → 共用目录的 `device_profile.json` | 设备速度和资源测量；不随代码包发布 |
| `MEDIA_PLATFORM_MODEL_CACHE` | 共用目录的 `models` | 固定版本 medium 模型下载缓存 |
| `--output` | 建议每次显式指定调用项目的新目录 | 语音、画面、索引和证据留在调用项目 |

不修改系统环境变量即可使用默认值。需要改配置时，只在当前进程设置：

```powershell
$env:MEDIA_PLATFORM_HOME = 'D:\SharedMediaState'
$env:MEDIA_PLATFORM_MODEL_CACHE = 'D:\SharedModels\media'
python -m media_platform doctor
```

固定版本 `medium` 仍验证 model.bin 的既有 SHA-256，发现原来的 ANNX 缓存时可只读复用，避免重复下载。`large-v3` 等其他名称沿用 faster-whisper / Hugging Face 的缓存机制，本次没有把它们改成固定版本哈希校验；可按现有 Hugging Face 环境配置管理缓存。模型权重不包含在源码包或 wheel 中。

本机将既有设备档案复制到用户级共用目录，以保持已有参数哈希与续跑行为；该硬件测量不是所有电脑和所有模型的通用基准。另一台机器默认没有此档案，使用资源检查和基础选择逻辑，或明确指定设备。

## 4. 停止、续跑与证据读取

- `Ctrl+C` 停止；原命令加 `--resume`，必须保持同一媒体、参数和输出目录。更换参数或源文件请换新目录。
- 共用昂贵提取进程锁继续跨项目生效；同一用户一次运行一个识别任务。原分辨率定点取图不占用语音 GPU 模型。
- `ingest` 沿用原有分块、边界处理、失败状态和哈希校验。`frames` 逐帧校验源哈希、参数和已记录画面，篡改或参数冲突会拒绝续跑。
- `frames --max-frames` 默认 300；超限报错，不会悄悄稀释采样。画面时间记录实际解码 PTS，同时保留请求时间。
- `--extraction` 绑定完成的语音证据；源哈希必须相同。HTML 显示画面前后约 25 秒的识别候选，原稿不被解释文字覆盖。

```python
from pathlib import Path
from media_platform.evidence import load_evidence

evidence = load_evidence(Path(r"D:\OtherProject\results\speech"))
for item in evidence["speech_segments"]:
    print(item["start"], item["end"], item["text"], item["review_required"])
```

提取器版本继续为 `2.0`、统一证据 schema 为 `1.0`；`2.1.0` 是共用发行包版本，三者分别管理。此次没有改动旧证据格式，已有结果可以读取、核验和继续复核。

## 5. 与原项目工作流的关系

共用发行包包含 `ingest`、`evidence`、`frame_review`、统一 CLI 与路径配置。广播翻译、ミーグリ互动分析、投资研究报告等仍是项目工作流，不随共用核心安装。

本机“推し活”包增加兼容桥：已安装共用包时，三个通用模块优先从共用包加载；项目工作流仍从原目录加载。原有通用源码留存作为卸载共用包后的旧版回退，不作为新版维护入口。升级新版应修改中立源码并重新构建、安装 wheel，不能修改 site-packages。

在旧项目目录，原命令仍可用，例如：

```powershell
python -m media_platform.workflows.radio --extraction "D:\OtherProject\results\speech" --output "D:\OtherProject\results\radio_report"
python -m media_platform.workflows.video --extraction "D:\OtherProject\results\speech" --query "关键词" --output "D:\OtherProject\results\search"
```

旧工作流的 `--start/--end` 仍只接受秒数；共用 `ingest` 和 `frames --times` 支持 MM:SS。旧广播翻译必须显式启用；其他项目单独安装共用包不会启用翻译或外部模型。

## 6. 以理解为验收目标

1. 先核对媒体清单、时长、音轨和源哈希。
2. 全文阅读转写，按章理解前提、推理、例子和例外。
3. 对照画面中的图表、指示、字幕及现场修正；关键歧义补取更密的画面，必要时回听片段。
4. 保存“原话候选、观察、归纳、未决问题”的区分；术语归一不篡改原稿。
5. 总结须带原片时间和证据范围；机器覆盖完整不能替代语义复核。

目前不提供通用说话人分离、逐帧动作理解、完整烧录字幕抽取或自动确认语义正确。画面抽样之间仍可能漏事件；ASR/OCR 的分数不是逐字准确率。原项目固定时间、人名、裁剪坐标的专用脚本未伪装为通用能力。

## 7. 发布与验收

- 发行包：`local_media_platform-2.1.0-py3-none-any.whl`。
- 源码：`local-media-platform-2.1.0-source.zip`，含源码、构建配置、测试和说明。
- 最小安装只带 Pillow、psutil；语音、OCR、GPU 为可选依赖。
- 5 项共用测试通过（3 项真实取图测试、2 项跨项目/配置测试）；旧工作流 6 项测试通过，合计 11 项。
- 新安装包从无项目路径注入的进程，处理真实中文讲课 169–189 秒，large-v3 / CUDA / float16 成功，产生 13 段识别候选并通过证据校验。
- 已验证 Windows 本机；没有宣称其他平台或所有媒体格式都已实测。

源码包测试：`python -m unittest discover -s tests -v`。跨项目测试针对已安装发行包运行，因此修改源码后应重新安装再验收。

源码包只含通用程序、文档、测试和兼容桥示例，不含媒体、模型权重、个人互动材料、设备测量档案或认证文件。
