# 已有字幕的视频观点解析

输入是单个已归档视频目录，其中七个文件共用目录名作为主文件名。只读原视频、字幕 JSON 与来源记录；所有新增结果保存在其旁边的 `解析` 子目录。不要重跑下载或 ASR 来做摘要。

## 处理顺序

1. 去掉开头、结尾各 30 秒，在剩余部分等距抽取六个原始帧，保留右下角时钟裁图与 OCR。
2. 核对电脑桌面的完整日期、时刻、上午/下午及跨午夜情况。股票软件状态栏、图中交易日期与上传时间都不替代桌面录制时钟。分钟显示对应一分钟的区间；用多个可用锚的偏移区间求交集，不虚构精确秒。
3. 通过 Qwen vLLM 或 Hermes MiniMax 对完整 cue 文本做内容提炼。模型只看 cue ID 与文字，不生成时间；程序用引用的原始 cues 求各段时间。输入、提示词、原始响应、模型、用量和验证记录落盘。
4. 同一股票只建立一个对象，按首次出现排序，再按时间保留多次独立讨论。比较段允许关联多个对象。不能合并“现在保留少量仓位”和“未来到目标价退出”等不同时点的条件。
5. 按讲解段提取原图与完整视频切片。指定已核验的关键帧时说明实际可见股票、代码和画线；没核验的只能标为代表画面。不能以桌面正在显示某股票证明正在说它。
6. 组装每段的视频时间、估计北京时间区间、讲者判断、买卖条件、实际/历史/假设动作、pass原因、截图及可播放视频。生成离线 HTML 与纯文本摘要。

全量引用与映射保留在 `analysis.json`，展示摘要不重复打印每条证据。金融名称和数值的疑点保留原文；不能因看到了画面就静默改字幕。

## 命令示例

以下变量必须换成实际路径。工程不保存任何输入、输出、模型或凭据。

```sh
TOOLS=/Users/nickzhang/YuncongVideoTools
VIDEO_DIR='/用户指定的数据目录/日期_时间_视频标题'
STEM="$(basename "$VIDEO_DIR")"
OUT="$VIDEO_DIR/解析"

python3 "$TOOLS/scripts/video_clock.py" extract \
  --video "$VIDEO_DIR/$STEM.mp4" --output-dir "$OUT/clock"

# 优先核对 OCR 原图。可靠锚可另存 verified_anchors.json，保留原始 OCR。
python3 "$TOOLS/scripts/video_clock.py" align \
  --anchors "$OUT/clock/verified_anchors.json" \
  --duration 视频实际秒数 --timezone Asia/Shanghai \
  --output "$OUT/clock/time_alignment.json"

python3 "$TOOLS/scripts/analyze_video_transcript.py" \
  --input "$VIDEO_DIR/$STEM.json" --output-dir "$OUT/model_qwen_001" \
  --provider qwen-vllm --transport ssh --ssh-host llm-server \
  --base-url http://127.0.0.1:8002/v1

python3 "$TOOLS/scripts/build_video_analysis.py" \
  --video-dir "$VIDEO_DIR" --output-dir "$OUT" \
  --content-analysis "$OUT/model_qwen_001/content_analysis.json" \
  --alignment "$OUT/clock/time_alignment.json"
```

`--timezone-confirmed` 只有在独立证据确认桌面时区时才使用。时钟未知时可以省略对齐文件；产物保留“北京时间未确认”，不以视频上传时间回填。抽帧依赖 FFmpeg/FFprobe；自动 OCR 依赖 macOS Vision 和 Swift 编译器，编译结果位于数据 runtime。可用 `--no-ocr` 保留原图后做视觉核对。

`--raw-response` 支持既有模型输出的离线校验，不再消耗模型。结构修复最多两轮；输出截断、缺引用或不连续段落应暴露失败。语义审阅修改必须保留原始响应，另存修改依据、原值与新值，再离线重验，不能把人工审阅后的结果称为原始模型输出。

## 关键帧登记

`build_video_analysis.py --visual-selection 工程外JSON` 可指定已核验画线帧。登记包含 `source_video_sha256`、`segments`，每段 `frames` 的 `video_seconds`、`caption`、`visual_verified`、`review_note`。只允许落在该段附近的原视频时点。可另列 `entities` 的画面名称/代码依据与 `context` 的录制类型、图中最近交易日；不据此改写音频原话。

## 输出与检查

```text
视频目录/
  原七个文件
  解析/
    index.html             # 本地浏览：目录、时间线、图像、切片
    summary.txt            # 纯文本摘要，每段双时间
    analysis.json          # 完整观点、出处、媒体关联与来源
    clock/                 # 六帧、OCR与时钟区间映射
    model_qwen_001/         # 提示词、输入、原始响应、验证
    frames/                # 原视频关键截图
    clips/                 # 每段MP4与相对时间VTT
    assets_records/        # 各段切片/图片哈希及重用契约
```

HTML 不依赖 CDN，视频路径相对解析目录；VTT 同时内嵌以适应 `file://`。可以直接打开 `index.html`，也可用绑定 `127.0.0.1` 的本地预览服务。移动资料时保持视频父目录与解析子目录关系。

检查原七文件未改、名字与引用无遗漏、连续片段没跨过其他话题、pass与条件例外均保留、历史收益/当前计划没有混淆；检查截图的讨论对象、视频可播放、字幕为切片局部时间。没有明确指数观点时写未明确，不从屏幕行情补观点。

同一输入与工具可重用已核验媒体；工具或输入变化时用新的直接子目录（例如 `解析_v2`），不要覆盖已冻结或用户修改的结果。首次样例验证不等于全库自动解析准确率验证。

本地预览可用 `serve_video_analysis.py --video-dir 视频目录 --status-file 工程外状态路径`。服务只绑定 `127.0.0.1`，支持视频拖动需要的单段 HTTP Range，默认运行24小时；服务状态/日志始终在工程外。只读输入在组装前锁定哈希，完成前如发生变化则拒绝发布结果。
