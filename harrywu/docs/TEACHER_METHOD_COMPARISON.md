# 李老师云聪视频方法：源码阅读与 Harry 流水线对接

日期：2026-10-10（JST）。阅读基线：`LiYiqiang0827/video_study` 的 `003ff7fed1b03360dda6053c63f4e53dd033d001`。这是静态源码阅读；未执行上游脚本、未运行 MLX、未调用 Qwen/MiniMax、未验证真实视频或 PPT。计划依据为本次云盘快照的 01、02、03a、03b、07、08。

## 结论与分工

李老师已把“下载完成的视频 → 保留原时间轴的字幕 → 有引用的逐对象分析 → 桌面时钟映射 → 原帧/原声 → PPT”连成工具链。值得接入的是 **完整分析 JSON、连续片段及媒体证据**，PPT 保持为阅读出口。Harry 的主线仍是自己的 8 月 4–7 日四场直播决策日志，不等待李老师的样例；随后接看图、天气/题材、规则和留出案例一致率。这与 01 第 56、75 行、02 第 9、27–32 行、03a 第 66–79 行一致。

需要修正对其方法的认识：03a 第 78 行对 **PPT 展示**的局限描述，不能直接扩展成“原始分析丢了原话、没有区分历史与计划”。实际提示词和代码保留 `quotes`、`original_excerpt`，并区分当前执行、条件计划、历史回顾。07 第 200 行原本注明“按 PPT 所见推断”，现在可升级为下面的源码映射，但仍不能宣称真实 `analysis.json` 已逐例核对。[S3][S4]

| 层 | 李老师已有方法 | Harry 负责的后续 |
|---|---|---|
| 素材与转录 | 完整视频、SHA、音频、ASR 对齐、七文件归档 | 复用已完成的 20 场直播和课程转录；保留各来源原始格式 |
| 视频理解 | 原句引用、对象及多段讨论、时钟、原图和原声 | 转为 07 的逐次提及；补身份、细分动作、截止时刻和原话标签 |
| 还原与研究 | 原屏幕证据，不补行情或收益判断 | 股票代码 + 安全截止 → 看图工具；原话/画面/推断分离，揭晓另存 |
| 展示 | HTML、文本、可编辑且可含视频的 PPT | 不把 PPT 加到决策日志的必经环节；跨视频串链及统计用 JSONL |

两边的“七文件”不是同一个格式：李老师是 MP4、M4A、SRT、VTT、MD、字幕 JSON、来源 JSON；Harry 直播的七份是研究文本、学员快评、校正版全文、原始全文、名称校正、切分索引、处理结果（03a 第 37 行）。不能按数量或同名目录认定互相兼容。[S1]

## 实际链条与可复用点

1. **冻结来源。** 从已完成下载的 `result.json` 建清单，核对完整性、上传时刻与 SHA；缺精确时间不编造。`batch_asr.py` 消费冻结清单，保存当前尝试、事件和有限重试；同步只接闭合尝试，失败诊断不能算成功。可复用清单、闭合状态、哈希及续跑规则，不必迁移整套推理环境。[S1][S2]
2. **音频和字幕。** 保留静音和源时间轴，输出 M4A 与 16 kHz 单声道 PCM16 WAV；Apple Silicon MLX 上用 Qwen3-ASR-1.7B-8bit + Qwen3-ForcedAligner-0.6B-8bit。自动句级字幕与原始 units 分开，近似显示不冒充逐字精确对齐。纯导出可复用旧 ASR，不能称作新推理验证。[S1][S2]
3. **模型只提炼文字。** 输入仅 cue ID 和文字，模型禁止生成时间。对象按首次出现排序，同对象保留独立连续 segment；比较段可同时关联多个对象；未买和排除也建对象。观点、条件、动作都带原文子串引用，程序检查 cue 连续性、引用、重名、重复提及和全文覆盖，再由源 cue 计算时间及拼接全文。[S3][S4]
4. **原图与时钟独立校验。** 字幕名、屏幕名、视觉核验记录分开；自动抽帧默认只是代表画面。已审阅关键帧另外登记，不能由屏幕正在显示某股票推断讲者正在说它。[S5][S7]
5. **组装与展示分层。** `build_video_analysis.py` 将原文、时间、图片、切片及来源哈希组成 `analysis.json`；再由 `yuncong_ppt_plan_v1` 整理主动讲股/网友提问、大盘、态度和精简要点。PPT 短片有独立 `yuncong_ppt_media_v1` 清单及同对象边界；嵌入、解码检查、实际软件试播是不同验证。可复用数据与展示分层，不需为研究转库生成 PPT。[S5][S8]

## 时间与验证：接入前必须保持的含义

**录制时刻、上传时刻、所讨论交易日是三个字段。** 上游默认剔除头尾各 30 秒后取六帧，保留实际帧 PTS、原图和 OCR；桌面分钟钟面对应 `[该分钟, 下一分钟)`，由“钟面区间减视频偏移”的交集得到录制时间范围。交集为空保留不一致，不用上传日期顶替。置信信息保留是否视觉检查、时区是否确认及稀疏帧无法排除剪辑的假设。[S6]

**显示时间不是还原截止策略。** 上游会生成区间中点估计；07 要求多个来源不一致时取较早者，并记录安全截止。转换器应读取 `alignment.offset_seconds_range` 和原始锚，不能解析 PPT 上的秒值当精确事实。建议将“有效区间下界 + 片段偏移，再与其他可靠依据取较早值”作为候选安全口径，并在转换规则中冻结；这是对接建议，不是上游已经实现的 07 截止算法。无可靠安全时点的记录保留待核，不能声称已经满足 M1 的每条可出图。[S5][S6]（07 第 51–57、184–186 行）

关键帧直接看清桌面分钟时，可单独登记 `desktop_clock_verified`，组装器只覆盖该帧的展示时间；它不证明整段有统一映射，也不能回填其他片段。上游 `confidence_grade` 不是 07 的 `high/medium/low`，需保留来源等级后另定映射；`session` 还需交易日历，不能由 `recording_kind` 或钟面是否处于交易时段直接替代。[S5][S6]

**结构通过不等于理解准确。** 引用是原文子串、全 cue 有归属，仍可能存在错误解释；关键对象、金额、行动性质、条件分支要对照原声/原图验收。普通媒体帧记录的 `video_seconds` 是选帧时点，不能自动冒充实际解码 PTS；时钟抽帧清单有实际 PTS，普通 `frame_refs.pts` 没有对应证据时留空。[S4][S6][S7]

## 从实际 JSON 到 07 的准确映射

转换单位以 `entity + segment` 起步，一段有不同对象或不同性质时按原句拆开；不能把整票 `overall_view` 或一页 PPT 当成一次提及。表中“补抽”指从冻结原文和已核验画面补齐，不用行情事后表现猜测。

| 上游字段/证据 | 07 目标 | 可直接转与缺口 |
|---|---|---|
| `metadata.title/source_video_sha256`、归档目录/来源记录 | `source.title/media_sha256/id/type/producer` | 标题、哈希可转；按 07 的来源规则生成 ID，短视频用 `lv_<文件夹时间戳>`。视频类型、版本、抽取者与时间须明确登记，不由文件扩展名猜 |
| `entity.name/kind/codes/name_evidence/visual_identity` | `scope.*` | 原口述名保留；视觉名另存。上游 `codes` 仅允许明确口述六位代码，未必有交易所后缀；须按截止时身份补 `ts_code`、核对依据/状态。上游无 `sector/own_position` 枚举，不能把 `other` 无条件转换成板块 |
| `segment.start_seconds/end_seconds` | `time.offset_start_s/offset_end_s` | 由 cue 推导，可直接转；保留 cue 来源。`clip.path` 可作 `clip_ref`，但原声切片有前后 padding，不能用切片边界替代提及区间 |
| `alignment`、段落双时间、已核验桌面钟面 | `mention_time_bj/time_basis/time_confidence/cutoff_bj/session` | 时钟来源可记 `screen_clock`；必须保留区间与假设，不能把桌面时钟映射误记成直播开播元数据。截止、会话、置信枚举仍需适配；未知不由上传时刻补齐 |
| `segment.original_excerpt/cue_ids/summary` | `text.verbatim/segment_ids/summary` | 原文与摘要分别转；segment IDs 要能回到具体转写文件和 cue 命名空间，保留上游 segment ID。摘要不作原话依据 |
| `actions[].classification`、相应 quotes | `actions[].tense/evidence_level/action`；辅助 `nature.mention_type` | `historical_review` 通常对应 past；`conditional_plan` 对应 plan；`actual_action` 需复核“正在/刚做”。三者均不能自动证明画面成交。还须补具体动作、价位/金额、bar_ref 与结果；不能按整个片段一刀切 |
| `views/buy_conditions/sell_conditions/pass_reason/visual_focus` 及 quotes | `reasons/conditions/expected_path/stance/checklist_quote` | 证据可复用，标签和细分条件需补抽；买卖两类容器不等于 07 的 entry/stop/target 等枚举。回顾性条件只入 past actions；排除原因应进入理由/比较组，不误作当前买点 |
| PPT 计划 `origin/attitude/label/outline_sentence` | `nature`、`stance.label_raw`、`text.summary` 的辅助材料 | 原始分析不保证有这些 PPT 字段。`origin=viewer` 是讨论发起者，不自动证明动作属于会员；`attitude=buy_plan` 也不等于实际买入。持有周期、动作归属、想法来源、提及类型仍按逐段原话抽取 |
| 多实体共享比较段、`pass_reason` | `comparison` | 可发现候选比较关系；无现成 group_id、rank、selected 或 peers 的 `ts_code` 清单。只按明确原话建立，未说排序留空 |
| `overview`、`kind=index/market` 的引用观点 | `market_view` 及对应对象记录 | 已有市场/指数观点来源，但没有“指数结构/短线情绪”固定两格；需分开补抽。没有 index 实体不自动等于全文无大盘观点，应同时检查 overview |
| `segments[].frames`、`visual_review/visual_identity` | `frame.frame_refs/frame_facts/frame_window` | 路径、选帧秒可转；核验过的可见事实才能转 facts。`caption`/`visual_focus` 含解释时不能直接当事实；实际 PTS、区域、read_by、verified、窗口范围需逐项有依据 |
| 模型来源、原始响应、校验、疑点及输入哈希 | `extraction.*`、`quality.*`、转换清单 | 保留上游证据包；`validation.status=passed` 不代表 `audio_verified`。仅看模型档位或画面纠名，也不能把整段转写升级成已听音 |
| 无对应字段 | `mention_id/revision/chain`、天气/题材后填项 | 转换器生成稳定 ID 与版本；修订追加。跨视频链条、历史标签和环境对齐由 Harry 后续做，不改写上游分析 |

依据：上游字段来自 [S3]–[S8]；目标字段以 07 第 16–24、30–180、182–196 行为准。07 第 223–240 行再把这些记录导出到 `RestorationRecord`；03b 第 78 行把批量出图入口列为试点需要时再做。本文没有实现转换器。

## 资料缺口与最小后续学习

已满足 08 的一部分需求：仓库交付了生成提示词、步骤和正式脚本。`references/workflow.md` 第 93 行记载“35 个目录、245 个内容文件”的工程整理快照，但这是上游自述历史快照，不是本次验证过的最新库存，也不是 35 份观点分析或 PPT。`references/video_analysis.md` 第 144 行提到三条样例验证，未提供对应素材。[S2][S8]

本次固定提交的文件树只有工具、说明、提示词、依赖和技能元数据：**没有真实 `analysis.json`、原字幕样本、关键帧、PPT 或媒体产物**。本文未见 08 所指 8/13、10/02 的 JSON 原件；总控“已经看过三份 PPT”是既有计划记载，不代表该新仓库包含它们。`yuncong-member-video-download` 未上传，与用户说明及上游“独立下载技能”的边界一致；不能据此恢复会员下载步骤或确认短视频公开可下载。[S1]

最小学习顺序如下，均为后续建议，本次只完成方法阅读与归档材料：

1. 保留这个固定提交的上游原样快照、提交号和文件清单；阅读本文后先看原提示词与引用/时钟校验代码。
2. 若李老师另行提供一份完整案例，收齐 `analysis.json`、源字幕 JSON、来源记录、clock 对齐/锚、视觉登记及所引用关键帧；PPT 可用于检查展示，但不代替上述证据。
3. 在一份冻结案例上做只读转换试点，输出独立 07 JSONL 和转换清单；先检对象身份、连续提及、原话、历史/当前/计划、排除和安全截止。结果要明确哪些字段直接搬运、哪些补抽、哪些未知。
4. 用有效 `ts_code + cutoff_bj` 接现有看图工具，核对原图与还原图。遇到当前阻塞再补适配；无需先迁移 MLX 或建全量 OCR/PPT 批处理。
5. 两三小时直播的覆盖、上下文容量、运行时间和稳定性尚无本次实测证据；不能由 ASR 支持连续分块推断观点模型已适合完整长直播。需要时用授权的单场冻结输入验证，再决定是否扩大。

上游包含 Qwen vLLM 与 Hermes MiniMax 的可选模型入口（`scripts/analyze_video_transcript.py` 第 491–500 行），本次仅作为方法资料保存，未调用；它们也不构成恢复 Project Rocket 已归档 Codex AgentPool 的授权。后续外部模型若需使用，仍遵守本项目 Hermes 的明确任务、工具/文件范围、时限、日志、停止与 Codex 验收约束。

## 源码证据索引（固定提交）

- **[S1] 入口、资产边界和七文件**：[README.md 第 3–7、47–55 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/README.md#L3)；[SKILL.md 第 24–48 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/SKILL.md#L24)。下载独立见 SKILL.md 第 3 行。
- **[S2] 批次与运行环境**：[references/workflow.md 第 7–29、42、64、89、93 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/references/workflow.md#L7)；[references/runtime.md 第 5–22、45–56 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/references/runtime.md#L5)。
- **[S3] 原始内容契约**：[prompts/video_analysis.md 第 9–23、34–86 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/prompts/video_analysis.md#L9)。
- **[S4] 实际引用和覆盖校验**：[scripts/analyze_video_transcript.py 第 83–165 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/scripts/analyze_video_transcript.py#L83)；名称/代码检查第 210–245 行；动作、重复提及、覆盖和语义限制第 250–340 行。
- **[S5] 组装后的实际字段**：[scripts/build_video_analysis.py 第 26–46、120–156、168–208、240–267 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/scripts/build_video_analysis.py#L26)。包含双时间、metadata、原始 cue 重算、单帧钟面优先、来源哈希与不覆盖保护。
- **[S6] 时间模型与精度**：[scripts/video_clock.py 第 224–274 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/scripts/video_clock.py#L224)（实际帧 PTS）；[第 376–490 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/scripts/video_clock.py#L376)（区间交集、置信、时区、剪辑假设和中点估计）。
- **[S7] 普通帧与片段资产**：[scripts/video_analysis_assets.py 第 91–125、141–173 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/scripts/video_analysis_assets.py#L91)（代表帧默认未核验、选帧秒数、前后 padding 与切片边界）。
- **[S8] 展示与媒体契约**：[prompts/video_ppt_outline.md 第 16–36、44–59、63–93 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/prompts/video_ppt_outline.md#L16)；[references/video_analysis.md 第 7–14、26–48、103–109、132–144 行](https://github.com/LiYiqiang0827/video_study/blob/003ff7fed1b03360dda6053c63f4e53dd033d001/references/video_analysis.md#L7)。

文件缺失判断另以固定提交的 `git ls-tree -r --name-only 003ff7fed1b03360dda6053c63f4e53dd033d001` 文件清单为依据；没有将上游说明里的工程外路径当作本机已取得的文件。
