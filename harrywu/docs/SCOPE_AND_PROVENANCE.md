# 分享范围、来源和复现边界

日期：2026-10-10（JST）。本分支来自上游 main `003ff7fed1b03360dda6053c63f4e53dd033d001`，新增 `harrywu/`。李老师现有工具源文件保持原样，仅在根 README 增加导航，并为新增材料开放 Git 忽略规则。

## 如何核对

- `provenance/source_manifest.jsonl`：本次搬运的逐文件来源、大小、SHA256与转换方式。普通复制字节不变；媒体平台解包记录原ZIP及成员路径。
- `provenance/primary_20_streams.json`：20场的真实七文件路径与哈希；避免漏掉8/4试点。
- `provenance/SHA256SUMS.json`：交付快照所有文件的最终哈希。`python harrywu/tools/verify_handoff.py` 核对文件、140份交付及源码搬运。
- `provenance/drive_sources.json`：本次读取总控与参考文档的Drive ID、链接和时间。总控16份与本地按统一换行文本比较一致；Google Docs参考文档是连接器可读文本快照，排版或链接显示文字不等于原始文件导出。
- `provenance/excluded_assets.jsonl`：盘点范围内未复制的媒体、重复、缓存、日志、认证邻近文件、当前新版工具等元数据清单；没有读取凭证内容，也不包含凭证值。

原有报告的SHA指向其当时外部输入；本次SHA指向迁移副本。旧报告中“已push/未push”描述当时TradingAgent分支状态，新仓库的分享不改写旧事实。旧副本、归档、示例和未完成项保留其状态。

## 不在仓库中的大资产

| 内容 | 处理方式 |
|---|---|
| 54场源直播，约17.75GB | 原文件继续在 `D:/YuncongVideoAnalysis/2026-01-01_to_2026-10-03/`；仓库有来源链接、文件清单及已有哈希 |
| 5段课程源片，约3.29GB | 原片外置；课程完整转写、复核图片、时间线和研究文档已入库 |
| 旧批量抽帧、音轨 | 保留相关JSON/OCR/索引和关键案例，未把数千张完整旧帧与工作音轨复制入Git；复核图与联系表已选入 |
| 全市场分钟CSV、SQLite、Parquet、DuckDB | 数据盘继续保存；接口/处理代码与小规模日线样例已入库 |
| 模型权重、运行环境、缓存 | 记录版本与用法，新机器单独准备；不以依赖目录代替安装声明 |
| 登录、下载凭据、cookies、私钥 | 不纳入；李老师独立下载技能也未上传、未被补造 |

媒体清单中的文件大小是盘点值；原视频没有在本轮重读数十GB重新哈希，使用已有来源记录。原行情全量也没有在本轮重新验收。外部资产索引不等于云盘媒体备份或可公开访问授权。

## 版本与修改

看图源码来自 `chart-tool-v2-20261009 / 5bd2261a1a97da1a6cb56bead77652c2c6d54d87`；天气来自 `codex/sentiment-cycle-20261005 / d5ec591137d143d94cc1e3618c1c89629f80604b`。两者取自不同工作树，均按对应文件清单搬运。

课程媒体平台2.1.0与直播2.2.1分开存放。2.2.1只展开源码、安装声明、README、测试；其他项目的refine验收背景未分享，不把新机器当作已通过ASR运行验收。

新编文档是交接解释，旧设计原文仅作来源。没有修改原始转写、原库、源媒体或执行旧批次。李老师工具中的Qwen/MiniMax入口保留其源码身份，本轮未调用任何外部模型，也未改变本机已归档提供商策略。

## 李老师方法的云盘归档

[方法快照文件夹](https://drive.google.com/drive/folders/18YibvrGSf2OGPh0RH4D4YvKEbhMT91hz) 存放固定main版本的[原始ZIP](https://drive.google.com/file/d/1c_vJj4uNpodU5G7KWebAoXLyGWg33_AO/view?usp=drivesdk)、来源清单和[源码对照说明](https://drive.google.com/file/d/1mgALB7WQEeLZeuxHnLndPXSVIyjDEp5-/view?usp=drivesdk)。未来学习可从固定版本复查，不会把Harry本次新增材料误当李老师原实现。
