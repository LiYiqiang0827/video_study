# 已有字幕的视频观点解析

输入是单个已归档视频目录，其中七个文件共用目录名作为主文件名。只读原视频、字幕 JSON 与来源记录；所有新增结果保存在其旁边的 `解析` 子目录。不要重跑下载或 ASR 来做摘要。

## 处理顺序

1. 去掉开头、结尾各 30 秒，在剩余部分等距抽取六个原始帧，保留右下角时钟裁图与 OCR。
2. 核对电脑桌面的完整日期、时刻、上午/下午及跨午夜情况。股票软件状态栏、图中交易日期与上传时间都不替代桌面录制时钟。分钟显示对应一分钟的区间；用多个可用锚的偏移区间求交集，不虚构精确秒。
3. 通过 Qwen vLLM 或 Hermes MiniMax 对完整 cue 文本做内容提炼。模型只看 cue ID 与文字，不生成时间；程序用引用的原始 cues 求各段时间。输入、提示词、原始响应、模型、用量和验证记录落盘。
4. 同一股票只建立一个对象，按首次出现排序，再按时间保留多次独立讨论。比较段允许关联多个对象。不能合并“现在保留少量仓位”和“未来到目标价退出”等不同时点的条件。
5. 按讲解段提取原图与完整视频切片。指定已核验的关键帧时说明实际可见股票、代码和画线；没核验的只能标为代表画面。不能以桌面正在显示某股票证明正在说它。
6. 组装每段的视频时间、北京时间、讲者判断、买卖条件、实际/历史/假设动作、pass原因、截图及可播放视频。默认将已有分析整理为精简 PPT 大纲并导出可编辑 PPT；本地 HTML、纯文本及切片保留为展开查看的材料。时间映射区间及依据保留在内部记录。

全量引用与映射保留在 `analysis.json`，展示摘要不重复打印每条证据。金融名称和数值的疑点保留原文；不能因看到了画面就静默改字幕。

## 阅读展示与术语

PPT、页面和纯文本摘要只展示观点、操作思路、时间、原始画线截图与切片。去掉“注意”“不确定性”等附注、审核状态、字幕编号、时钟误差及映射说明；普通时间标签不加“约”或“±”。原始cue引用、时间区间、时区假设、疑词和审阅记录继续保存在JSON与来源文件中，不删除或改写证据。没有可用北京时间时只标“未知”，不用上传时间代填。

用户已确认云聪语境中的“做套”就是“做T”：用部分仓位低买高卖，或先卖再买以降低成本。摘要统一用“做T”，不解释为套牢、不继续标成ASR疑词；原字幕、原句引用和模型原响应仍保留原词。

保留操作性质，但只用简短措辞区分：历史操作用“已做/曾做”，未来动作用“计划/目标”，资金举例用“假设分仓”。例如“假设50万：15+10+25万”不表示实盘已持有这些金额。不要逐条追加未核验、未执行或收益保证等审计解释；也不要因此删去讲者原有的仓位前提、重仓止损、条件买入或价格区间。“图示买点”“图示加仓区间”可直接结合截图展示，视觉依赖细节留在内部。

## 默认 PPT 交付

先做大纲，再展开逐股截图。大纲包含大盘观点、云聪主动分析的股票、网友提问的股票和本片重点；每票只列态度关键词与一句话结论。股票按首次出现排序，同票归并，回头再讲保留对应详情，不把相隔的讨论拼成一项新操作。没有独立大盘判断时写“本片未作独立判断”；没有网友提问就不补提问股票或空添一页。原文不能确认谁发起时记录 `unknown`，不强行归入主动或网友类。

每个详情页使用一张该股票的关键截图，加 2—4 个短点说明判断、价格条件或仓位思路；标题和正文可编辑，图片嵌入 PPT，离线打开不依赖图片外链。详情保留视频时间与北京时间，时间来自已有分析，模型不能生成时刻。`frame_path` 必须原样取自 `analysis.entities` 中同一 `entity_id` 的 `segments[].frames[].path`，并匹配该段讲解；没有匹配图就先补登记，不借其他股票画面凑数。每个已分析股票都要有至少一个详情。默认完整原图，必要的 `crop` 表示从四边裁掉的比例，只移除软件菜单或边栏，图表价格、日期、成交量及原画线都要保留。导出后检查截图对象和裁切范围、文字可编辑、双时间及实际离线显示。

大纲提示词为 [prompts/video_ppt_outline.md](../prompts/video_ppt_outline.md)，采用 `yuncong_ppt_plan_v1`。`analysis_sha256` 为原 `analysis.json` 整个文件的 SHA，由调用方提供；模型按原分析与原句判断 `origin`（`self/viewer/both/unknown`）和 `attitude`（`buy_plan/watch/no_buy/bearish/hold/sell`）。`label` 为简短中文态度，`outline_sentence` 为一句话。不优先选择不等于绝对不买；历史卖出不等于当前建议卖出；条件计划不等于已买入。模型原响应、输入与提示词哈希、验证及生成状态保留在内部资料，不展示成审计附注。

已有 `analysis.json` 和大纲时只做导出，不再次调用观点模型。需要新大纲时从已审阅分析提炼，不重新检索行情或添加交易建议；提示词定义的是内容计划，不能用截图替代原文证明实际持仓。

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

# ppt_plan.json 来自已有 analysis.json，按精简大纲提示词生成并校验。
# 三项环境变量指向现成的外置运行依赖，先设置为本机实际路径。
export RUNTIME_NODE_MODULES='/实际运行依赖/node_modules'
export PRESENTATIONS_SKILL_DIR='/实际技能目录/presentations'
export RUNTIME_PYTHON='/实际运行依赖/bin/python'
BUILD_DIR='/工程外工作目录/视频PPT构建'
node "$TOOLS/scripts/export_video_analysis_pptx.mjs" \
  --analysis "$OUT/analysis.json" --plan "$OUT/ppt_plan.json" \
  --output "$OUT/观点摘要.pptx" --build-dir "$BUILD_DIR"
```

`--timezone-confirmed` 只有在独立证据确认桌面时区时才使用。时钟未知时可以省略对齐文件，内部保留未知状态，不以视频上传时间回填；阅读页不展示时间不确定性附注。抽帧依赖 FFmpeg/FFprobe；自动 OCR 依赖 macOS Vision 和 Swift 编译器，编译结果位于数据 runtime。可用 `--no-ocr` 保留原图后做视觉核对。

`--raw-response` 支持既有模型输出的离线校验，不再消耗模型。结构修复最多两轮；输出截断、缺引用或不连续段落应暴露失败。语义审阅修改必须保留原始响应，另存修改依据、原值与新值，再离线重验，不能把人工审阅后的结果称为原始模型输出。

## 关键帧登记

`build_video_analysis.py --visual-selection 工程外JSON` 可指定已核验画线帧。登记包含 `source_video_sha256`、`segments`，每段 `frames` 的 `video_seconds`、`caption`、`visual_verified`、`review_note`。只允许落在该段附近的原视频时点。可另列 `entities` 的画面名称/代码依据与 `context` 的录制类型、图中最近交易日；不据此改写音频原话。

## 输出与检查

```text
视频目录/
  原七个文件
  解析/
    观点摘要.pptx           # 默认阅读交付，文字可编辑、截图嵌入
    ppt_plan.json          # 精简大纲、态度与同票截图选择
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

检查原七文件未改、名字与引用无遗漏、连续片段没跨过其他话题、pass与条件例外均保留、历史收益/当前计划没有混淆；检查截图的讨论对象、视频可播放、字幕为切片局部时间。PPT 检查主动/网友归类依据、每票态度与一句话、同票截图、裁切未伤图表信息、离线嵌图与可编辑正文。没有独立大盘观点时写“本片未作独立判断”，不从屏幕行情补观点。

同一输入与工具可重用已核验媒体；推理输入或工具变化时默认用新的直接子目录（例如 `解析_v2`），不要覆盖已冻结或用户修改的结果。用户已明确授权仅修订阅读展示或采用已确认术语时，先将旧 `analysis.json`、`index.html`、`summary.txt` 备份到工程外审查目录、记录备份与来源哈希，再更新同一个解析链接；保留内部证据、原七文件、原始units和模型原响应，不新增模型调用，可复用已校验媒体。首次样例验证不等于全库自动解析准确率验证。

本地预览可用 `serve_video_analysis.py --video-dir 视频目录 --status-file 工程外状态路径`。服务只绑定 `127.0.0.1`，支持视频拖动需要的单段 HTTP Range，默认运行24小时；服务状态/日志始终在工程外。只读输入在组装前锁定哈希，完成前如发生变化则拒绝发布结果。
