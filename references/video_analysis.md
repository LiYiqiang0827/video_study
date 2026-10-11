# 已有字幕的视频观点解析

输入是单个已归档视频目录，其中七个文件共用目录名作为主文件名。只读原视频、字幕 JSON 与来源记录。成品 PPT 与视频同名、放在源视频同一目录：`YYYYMMDD_HHMMSS_标题.pptx`。分析JSON、截图、短片和阅读页放在其直接子目录 `解析` 或新版本目录；构建预览与验收日志放在视频目录外的工作目录。不要重跑下载或 ASR 来做摘要。

## 处理顺序

1. 去掉开头、结尾各 30 秒，在剩余部分等距抽取六个原始帧，保留右下角时钟裁图与 OCR。
2. 核对电脑桌面的完整日期、时刻、上午/下午及跨午夜情况。股票软件状态栏、图中交易日期、视频标题与上传时间都不替代桌面录制时钟。分钟显示对应一分钟的区间；用多个可用锚的偏移区间求交集，不虚构精确秒。锚不一致时不强行线性映射。关键原帧直接显示日期与时刻时，优先登记 `desktop_clock_verified=true`、分钟格式的 `desktop_time_text=YYYY-MM-DD HH:MM` 和原帧依据；组装器显示该截图直接可见分钟，全局推算保留为内部段落依据，不用推算秒覆盖已看清的钟面，不回填其他帧。
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

大盘或指数图解放在股票详情前。讲者对着指数K线讲形态、支撑压力、缺口或画线时，计划的 `market_items` 必须包含该 `index/market` 实体及匹配截图、压缩片段；纯泛论只放大纲，不借个股图冒充指数。市场项沿用股票详情字段和态度枚举，但不含 `origin`。所选帧登记 `chart_verified=true` 与 `visual_verified=true`。

每个详情页使用一张该实体的关键截图，加 2—4 个短点说明判断、价格条件或仓位思路；标题包含可读实体名，标题和正文可编辑，图片嵌入 PPT，离线打开不依赖图片外链。详情保留视频时间与已有依据的北京时间，模型不能生成时刻。全局时钟不一致时，封面仅展示核验帧直接可见的桌面日期，不写“北京时间未确认”；未核验时刻不补算。`frame_path` 必须原样取自 `analysis.entities` 中同一 `entity_id` 的 `segments[].frames[].path`，并匹配该段讲解；没有匹配图就先补登记，不借其他股票画面凑数。每个已分析股票都要有至少一个详情。默认完整原图，必要的 `crop` 表示从四边裁掉的比例，只移除软件菜单或边栏，图表价格、日期、成交量及原画线都要保留。导出后检查截图对象和裁切范围、文字可编辑、双时间及实际离线显示。

大纲提示词为 [prompts/video_ppt_outline.md](../prompts/video_ppt_outline.md)，采用 `yuncong_ppt_plan_v1`。`analysis_sha256` 为原 `analysis.json` 整个文件的 SHA，由调用方提供；模型按原分析与原句判断 `origin`（`self/viewer/both/unknown`）和 `attitude`（`buy_plan/watch/no_buy/bearish/hold/sell`）。`label` 为简短中文态度，`outline_sentence` 为一句话。不优先选择不等于绝对不买；历史卖出不等于当前建议卖出；条件计划不等于已买入。模型原响应、输入与提示词哈希、验证及生成状态保留在内部资料，不展示成审计附注。

字幕名字与画面名字不同，保留原实体音频名与引用；视觉核验记录用 `visual_identity={display_name,visual_verified:true,basis,...}` 保存正确展示名。帧用 `visible_entity_name` 或旧 `visible_stock_name` 核对，PPT只接受同一实体原名或已核验展示名；泛题材示例可用 `visible_chart_name` 明确画面对象，但不能冒充独立个股观点。

已有 `analysis.json` 和大纲时只做导出，不再次调用观点模型。需要新大纲时从已审阅分析提炼，不重新检索行情或添加交易建议；提示词定义的是内容计划，不能用截图替代原文证明实际持仓。

## 可选内嵌视频

详情页可将截图附近的原始讲解压缩并嵌入 PPT。原截图用作原生视频海报，文字和位置不变；在演示软件中点击画面播放声音和画线过程。股票、指数与市场图解采用同一规则：只保存截图对应的同对象、同段片段，不借相邻话题视频，也不为凑长度跨过话题。当前 11 页样例嵌入后仍为 11 页，不新增视频页。

`prepare_ppt_video_clips.py` 默认围绕截图选择约 32 秒，优先靠近登记的字幕句界；短于目标时长的段落保留全段。最长边不超过 1280×720 且不放大原分辨率，15 fps、H.264 Main/yuv420p、CRF 24、AAC 64 kbps，保留声音和完整原图范围。参数可改，但更改输入或参数应换新输出目录。相同输入重跑会核对缓存哈希并重新解码，不重新调用模型或改原七文件。

字幕句界不保证画面对象一致。若一条字幕横跨换票，先用原帧和原对齐分句核验安全起止点，在 `visual_selection.segments[segment_id].ppt_clip_bounds` 登记 `start_seconds/end_seconds/visual_verified=true/basis`，组装器复制到分析段。边界必须处于原段内并包含选帧，不修改原句时间。压缩器和导出器都验证这些边界。只闪现画面的对象若没有完整同对象分句，可用详情 `embed_video=false` 与内部 `video_omission_reason` 保留截图；媒体清单的 `omissions` 绑定对象、截图哈希与原因。读者页不展示内部检查说明。

生成 `yuncong_ppt_media_v1` 媒体清单，绑定 `analysis_sha256`、`plan_sha256`、原视频与字幕哈希；`clips` 每项含 `entity_id`、`frame_path`、`segment_id`、相对 `path`、原视频 `start_seconds/end_seconds` 和文件 `sha256`，另有编码与解码记录。主导出器通过 `--media-manifest` 消费该清单。它先用 Artifact Tool 创建 PPT，再调用 `embed_ppt_videos.py` 追加内部 MP4、视频关系和点击播放时序；该适配器本身不提取视频、不转写、不总结。独立使用时接受 `--input`、`--spec`、`--output`、`--receipt`，匹配输入 PPT 哈希及各页海报哈希，不修改布局或覆盖源文件。

MP4 直接存入 PPT 包内，移动 PPT 不需要旁边附带视频文件。先核对媒体关系没有外链、文件字节与清单一致、每段完整解码，再在原生演示软件实际点击播放，检查画面进展和声音。仅包内有 MP4 或导入后媒体数量正确，不表示已经成功播放。当前本机没有 PowerPoint，可用已装 Keynote/WPS 试播并记录实际软件、版本与范围；不能据此称所有 PowerPoint 版本已验证。阅读页不添加这些内部验收记录。

## 命令示例

以下变量必须换成实际路径。工程不保存任何输入、输出、模型或凭据。

```sh
TOOLS='/Users/nickzhang/Documents/ChatGPT/云聪股票交易分析'
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
# 股票在 stocks，实际K线图解的大盘/指数在 market_items。
# 三项环境变量指向现成的外置运行依赖，先设置为本机实际路径。
export RUNTIME_NODE_MODULES='/实际运行依赖/node_modules'
export PRESENTATIONS_SKILL_DIR='/实际技能目录/presentations'
export RUNTIME_PYTHON='/实际运行依赖/bin/python'
BUILD_DIR='/工程外工作目录/视频PPT构建'
node "$TOOLS/scripts/export_video_analysis_pptx.mjs" \
  --analysis "$OUT/analysis.json" --plan "$OUT/ppt_plan.json" \
  --output "$VIDEO_DIR/$STEM.pptx" --build-dir "$BUILD_DIR"

# 可选：从同票同段原视频准备有声短片。输出位于解析目录内的新子目录。
python3 "$TOOLS/scripts/prepare_ppt_video_clips.py" \
  --analysis "$OUT/analysis.json" --plan "$OUT/ppt_plan.json" \
  --output-dir "$OUT/ppt_media"

# 默认交付带视频版：自动压缩、嵌入、验证，同视频名且在同一目录。
# 选择此入口时不要先生成同名纯截图版；已有成品默认保护，不覆盖。
export RUNTIME_NODE='/实际运行依赖/node'
python3 "$TOOLS/scripts/build_video_ppt.py" \
  --analysis "$OUT/analysis.json" --plan "$OUT/ppt_plan.json" \
  --build-dir "${BUILD_DIR}_media"
```

`--timezone-confirmed` 只有在独立证据确认桌面时区时才使用。时钟未知时可以省略对齐文件，内部保留未知状态，不以视频上传时间回填；阅读页不展示时间不确定性附注。抽帧依赖 FFmpeg/FFprobe；自动 OCR 依赖 macOS Vision 和 Swift 编译器，编译结果位于数据 runtime。可用 `--no-ocr` 保留原图后做视觉核对。

`--raw-response` 支持既有模型输出的离线校验，不再消耗模型。结构修复最多两轮；输出截断、缺引用或不连续段落应暴露失败。语义审阅修改必须保留原始响应，另存修改依据、原值与新值，再离线重验，不能把人工审阅后的结果称为原始模型输出。

## 关键帧登记

`build_video_analysis.py --visual-selection 工程外JSON` 可指定已核验画线帧。登记包含 `source_video_sha256`、`segments`，每段 `frames` 的 `video_seconds`、`caption`、`visual_verified`、`review_note`。只允许落在该段附近的原视频时点。可另列 `entities` 的画面名称/代码依据与 `context` 的录制类型、图中最近交易日；不据此改写音频原话。

## 输出与检查

```text
视频目录/
  原七个文件
  YYYYMMDD_HHMMSS_标题.pptx # 与源视频同名，默认阅读交付，截图与有声短片内嵌
  解析/
    ppt_plan.json          # 精简大纲、态度与同票截图选择
    ppt_media/             # 可选：压缩短片、媒体清单与缓存核验记录
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

内嵌 PPT 另检查冻结分析与大纲哈希、截图和视频段的一一关联、音视频同步、包内视频哈希、海报点击与实际试播；确认原七文件和已有分析未改。试播报告与构建检查保存在工程外，不写入股票观点或阅读页。

同一输入与工具可重用已核验媒体；推理输入或工具变化时默认用新的直接子目录（例如 `解析_v2`），不要覆盖已冻结或用户修改的结果。用户已明确授权仅修订阅读展示或采用已确认术语时，先将旧 `analysis.json`、`index.html`、`summary.txt` 备份到工程外审查目录、记录备份与来源哈希，再更新同一个解析链接；保留内部证据、原七文件、原始units和模型原响应，不新增模型调用，可复用已校验媒体。首次样例验证不等于全库自动解析准确率验证。

本地预览可用 `serve_video_analysis.py --video-dir 视频目录 --status-file 工程外状态路径`。服务只绑定 `127.0.0.1`，支持视频拖动需要的单段 HTTP Range，默认运行24小时；服务状态/日志始终在工程外。只读输入在组装前锁定哈希，完成前如发生变化则拒绝发布结果。

## 复用与批量稳定性

`build_video_ppt.py` 只消费已审阅并冻结的分析和大纲，不调用模型；单入口完成压缩、PPT导出、内部媒体登记和正式文件验证。默认 H.264 CRF28、15fps、AAC64kbps、目标32秒，可通过参数调整；底层压缩CLI保留CRF24默认。输入、选帧、参数或工具版本改变应使用新的构建目录，不混用缓存。相同输入重跑会核对源视频/字幕、所有注册截图/短片、媒体清单、成品和验收回执哈希后复用，不重复生成；素材缺失或变更会拒绝旧缓存。每个构建目录有文件锁、`STATUS.json`、阶段日志及独立导出尝试；失败保留原因和已完成媒体，恢复时先诊断日志，不启动重复实例。若输出已存在而没有匹配闭合回执，保护文件并停下核验。任务内修订时先把旧PPT、旧冻结输入和旧回执归档到工程外，记录SHA，再使用新的构建目录生成同名PPT。

多视频按用户授权的目录列表逐个运行同一入口，或对独立视频并行；一条视频只运行一个实例。批次完成数以逐项闭合回执和实际文件为准。最终核对原七文件哈希、股票/指数图解覆盖、读者文案与原句、每张幻灯片视觉效果、同对象媒体关联和离线内部关系；素材、模型原响应和测试脚本始终在工程外。三条样例验证说明流程可复用，不代表全库摘要准确率已经测定。
