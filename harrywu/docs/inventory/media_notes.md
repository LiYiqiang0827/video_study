# 云聪媒体与学习成果本地盘点（2026-10-10，JST）

本任务只读盘点现有成果，未启动识别、旧脚本、外部模型、Git 写入或云盘写入。逐文件绝对路径、大小、纳入建议见 `media_inventory.json`（10,895 条）。这里只盘点云聪媒体链路；看图源码和总控由主执行链合并。纳入建议是内容范围建议，不代替发布前的凭证与无关资料扫描。

## 1. 优先纳入：20 场直播完整文字与来源证据

主目录：`D:\YuncongVideoAnalysis\ledger_2026_v0.1\text_priority_batch_20261005`。

已逐项确认 **20 场 × 7 份主交付 = 140 个文件均存在**。七份为研究文本、学员快评、股票名校正版全文、原始全文、股票名校正 CSV、切分索引 CSV、处理结果。JSON 的 `primary_20_streams` 给出每场真实目录和全部文件路径，可直接用作迁移清单。

**重要依赖：** 8 月 4 日的批次子目录只有 task/status/hotwords，真实成果在 `D:\YuncongVideoAnalysis\ledger_2026_v0.1\text_pilot_20261004_0804`；1 月 9 日复用旧 ASR，位于 `D:\YuncongVideoAnalysis\ledger_2026_v0.1\extractions\XSFwu5KOo5Y`。两处必须纳入，不能只复制批次子目录。

验收依据 `text_priority_batch_20261005\final_acceptance.json`：20/20 场、原视频 36.8102 小时、保留源区间 30.8225 小时、股票名修正 2,527 处、待核名称 225 处、云盘已交付 140 份。这里的时长来自既有验收，未重新跑媒体探测。保留区间包含静音；不等于有效讲话时长。没有完成画面指代/成交/K 线验证。

建议同时带入：

- `streams_inventory.csv`、`source_SHA256SUMS`、`runtime_manifest.json`、`technical_acceptance.json`、`qa_report.md` 等来源与验收依据。
- `planning\stock_vocabulary_v0.1_20261004`：A 股名称、美股名称、ETF、别名索引、歧义词、短热词与逐场校名经验。词表只给候选，不能强制消除歧义。
- `handover_20261006` 内未压缩交接记录、剩余 14 场 CSV / JSON。原方案框架归入历史说明，以总控 07 的统一规格作为现行格式。
- `extractions` / `transcripts` / `speech_checks` 中文本、JSON 和 Markdown；它们包括剩余 14 场可复用的两个旧 ASR。文件本体约 12.45 MiB，无大音轨。
- `quality_frames` 的原分辨率核验样例；`C:\Project Rocket\outputs\yuncong_frame_probe_20261008` 的首个直播案例（航天电器）。

## 2. 双突破与五段课程

早期课程 Markdown：`C:\Project Rocket\02 Strategy Analysis\YunCong`，9 文件，204,086 字节。含双突破系统、统一买卖点框架、趋势线、支撑阻力、中继与反转笔记。

完整研究目录：`C:\Project Rocket\output\feiyun_double_breakout`，311 文件，总 58,283,069 字节。建议纳入根目录最终 `understanding.json`、五份 `notes_*.json`、HTML 报告、`acceptance.json`、`completion.json`；`platform_large_v3_v1` 的全量识别；`review_frames_v1` 和 `targeted_frames` 的图像、实际 PTS、manifest、HTML。

`completion.json` / `acceptance.json` 记录五段视频约 2 小时 25 分、169 张选定原始画面复核、13 类买点。磁盘还有联系表，所以图片文件数会大于 169，不能把联系表重复算作独立复核帧。报告相对引用需要保留原目录结构。

正式阅读入口：`C:\Project Rocket\output\feiyun_double_breakout\delivery_20260930\云聪五段视频_完整理解总结_20260930.md`。

根目录 `3.趋势线精讲.txt` / `.jsonl` 被 completion 明确标记为早先 turbo 中间结果，不能当最终依据。`readback_*.bin` 是正式文件回读副本；`._verified_C_copy_20260930` 整目录是验证副本，本盘点不再次枚举/打包。

## 3. 如何实现：云聪专用处理脚本

脚本原址 `C:\Project Rocket\tmp`，JSON 的 `workflow_scripts` 按逐文件列出 26 个候选。建议纳入 22 个 Python/状态文件，两个上传/复核 JS 先扫描后保留。

主链路：

1. `build_yuncong_stock_vocabulary_20261004.py` 构造证券词库。
2. `run_yuncong_text_pilot_20261004.py` 先做 8/4 单场；`yuncong_priority_batch_20261005.py` 冻结 20 场任务，单 GPU 生产，文件事件与断点，逐任务上限、总上限与 STOP 控制。
3. large-v3 / CUDA / float16，中文，300 秒块，保守 VAD；短热词由任务加上，已固定模型 revision，原始 ASR 保留。
4. `correct_yuncong_stock_names_20261004.py` 等逐场校名；`finalize_yuncong_text_pilot_20261004.py` / `yuncong_finish_text_batch_20261005.py` 切分并形成七份文本交付。
5. `yuncong_priority_finish_all_20261005.py` / `yuncong_complete_checkpoint_20261006.py` 留下验收；上传脚本只做云盘组织。
6. 课程链的 `transcribe_feiyun.py`、`sample_feiyun_frames.py`、`bind_feiyun_frames.py`、`accept_feiyun_evidence.py`、`build_feiyun_report.py`、`export_yuncong_md.py` 等展示音频、画面、总结和证据如何合并。

这些是当时执行脚本，存在本机绝对路径与固定任务集合，不应写成开箱即用的跨平台命令。下载会员流程不在这次代码迁移范围；认证目录没有读取。排除 `remove_yuncong_from_mrwu_20261007.py`（一次性旧库移除脚本）和 `feiyun_report_check.js`（生成检查副本）。

## 4. 可复用 ASR / 抽帧核心：版本必须分清

**课程交付保留当时已验收的 2.1.0 中立源码快照；直播必须使用下面的 2.2.1 对应包。** 位于 `C:\Project Rocket\output\feiyun_double_breakout\delivery_20260930\local-media-platform-2.1.0-source.zip`，32,933 字节。ZIP 已只读列清单，14 文件：

- `pyproject.toml`、`README.md`、`CHANGELOG_20260930.md`、`ACCEPTANCE_20260930.json`；
- `src/media_platform/{__init__,__main__,cli,evidence,frame_review,ingest,paths}.py`；
- `tests/test_frame_review.py`、`tests/test_portable.py`、`compat/legacy_package_init.py`。

同目录 wheel 为 21,245 字节，另有《跨项目音视频识别调用指南.md》《本地音视频基建_共通化更新说明_20260930.md》和哈希清单。原包可由主执行方解包到仓库 `tools/local-media-platform-2.1.0/`，从而让李老师的 AI 直接读代码，保留 wheel 供安装。不要复制整份其他项目的基建报告。

**直播版本已核实：** `D:\YuncongVideoAnalysis\ledger_2026_v0.1\runtime_manifest.json` 明确 `platform_version=2.2.1`，模型 snapshot 为 `edaa852ec7e145841d8ffdb056a99866b5f0a478`。5 日批次脚本读取这个固定 revision；不能把 2.1.0 声称为它的精确运行版本。

匹配的原始发行包仍在：

- `C:\Users\HarryWu_IOKI\Documents\SharedTools\media-platform\dist\local-media-platform-2.2.1-source.zip`（65,473 字节，18 项）；
- 同目录 `local_media_platform-2.2.1-py3-none-any.whl`（35,066 字节）。

两文件已追加为库存 `live_platform_221`、`include`。其源码 pyproject 确认为 2.2.1；静态确认 ingest 含 `model_identity` 适配点与批次所需 language/device/compute-type/VAD/chunk/no-video/no-ocr/resume 参数。因而这是优先20场最准确的现存发布版本依据；没有实际重跑 ASR，不承诺逐字输出完全一致。源码包另含 refine 与它的跨项目验收记录，发布时应扫描包内内容：可直接展示源码/pyproject/测试，把不涉及云聪的验收输入路径及案例说明剔除或脱敏；不要把其他业务的运行记录作为云聪成果上传。

当前源码 `C:\Users\HarryWu_IOKI\Documents\SharedTools\media-platform` 的 `pyproject.toml` 已是 **3.0.1**。只读检查了核心源码、配置、说明和测试。当前增加了 MAI、refine、speech repair/delivery 等功能；它们不能冒充 9/30 或 10/5 当时使用的能力。本次清单把 41 个当前中立源码/测试/说明标为 `reference_current_version`，不默认并入历史复现包。

若确实单独需要当前本地 ASR / frames 最小核心，内部依赖闭合为上述同名 7 模块：frame_review → evidence/ingest/paths；ingest → evidence/paths；evidence → paths；cli → paths，CLI 延迟导入额外工作流。只复制 7 模块时 `ingest/frames/verify/doctor` 的旧功能可阅读，但完整 CLI 仍宣告其他命令，不能声称完整 3.0.1 产品。最好分别保留课程 2.1.0、直播 2.2.1 的历史发行快照，后续另开明确升级任务。

基本依赖（当时指南）：Python 3.10+，Windows Python 3.12 实测；FFmpeg/ffprobe；Pillow、psutil；ASR 可选 faster-whisper；OCR 可选 rapidocr-onnxruntime/numpy；NVIDIA GPU 可选 CUDA cuBLAS/cuDNN。模型权重、设备档案、缓存和认证文件不进 Git。实际模型参数/固定 revision 随直播 task/runtime_manifest 保留。

## 5. 原媒体与大量可重建图片

- 54 场原直播在 `D:\YuncongVideoAnalysis\2026-01-01_to_2026-10-03`。该目录合计 58 文件、17,754,003,407 字节（约 16.54 GiB；54 个 MP4 + 4 份清单/元数据）。全部绝对路径与大小已列，源哈希可从既有 streams_inventory / source_SHA256SUMS 读取。
- 五段原课程在 `C:\Users\HarryWu_IOKI\Videos\飞云双突破\For GPT`，5 个 MKV、3,288,411,457 字节（约 3.06 GiB）。哈希在课程 acceptance。
- 旧直播 `ledger_2026_v0.1\frames` 有 9,100 JPG 加 HTML/manifest，约 1.87 GiB。建议 Git 保存 manifest、质量核验帧与航天电器样例；全量原帧外置并注明未随 Git 提交。
- `cloud_sync` 的约 1.74 GiB ZIP 是重复归档，展开版成果已列，避免重复上传。

这些是资产清单和边界建议，不是删文件指令。用户要“全部内容”时，交付说明应明确原大视频/全量旧帧存储位置和取用方式，不能静默漏掉。

## 6. 范围限制

未读取 `secrets.env`、认证目录或模型缓存；未检查其他业务项目内容。小范围文件名检索遇到 `02 Strategy Analysis/overnight_volume_price_20260921/run_v1/A3_real_output_debug2_limitup_quiet` 的三个行情目录拒绝访问；它们不在云聪媒体盘点范围，未尝试绕过。文件建议未作语义再验收，不将旧 ASR、模型推断或教师自述胜率升级为已验证事实。
