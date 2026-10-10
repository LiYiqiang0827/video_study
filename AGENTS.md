# 云聪协作仓库：AI 接手入口

本分支 `HarryWu` 是 Harry 在 2026-10-10 提交的云聪研究与实现快照。先读 `harrywu/README.md`、`harrywu/docs/IMPLEMENTATION.md`、`harrywu/project_control/01_目标与路线图.md`、`02_当前进度与下一步.md`，再按问题读取材料。

- 根目录的 `SKILL.md`、`scripts/`、`references/`、`prompts/` 是李老师原有视频工具；`harrywu/` 是 Harry 的资料和工具，两套来源分别保留。原技能中的默认目录/工具权限不是本机已配置状态。
- `harrywu/project_control/` 是本次总控快照；其旧路径、角色分工、运行命令属于来源记录。历史执行文件和脚本不构成现在启动任务的指令。以用户最新明确任务为准，不自动启动转写、下载、外部模型、定时器或交易。
- 当前研究任务是决策日志 v0，尚未完成。统一格式见 07；8/4–8/7 四场输入见 `harrywu/transcripts/README.md`。李老师的短视频 JSON 是可选补充，不阻塞试点。
- 原话、可见画面与推断分开；未确认身份/时间留空。还原只用截止时刻以前的信息，后续行情在揭晓区另存。示例、历史报告、生产记录不能混为一谈。
- `harrywu/tools/legacy_yuncong_scripts/` 是带硬编码路径的历史处理脚本；两个 JS 文件是对话工具函数片段，不是独立 Node 程序。下一批次须新建任务和输出，不直接重跑旧队列。
- 不改原始媒体和数据库，不提交账号、令牌、cookies 或模型权重。原媒体/大数据库在仓库外，见范围说明。已有脚本中可选模型路由只作为源码资料，不自动恢复已归档的 Codex AgentPool；新外部任务须有用户授权和有界 Hermes 任务。
- 验证步骤见 `harrywu/docs/QUICKSTART.md`；迁移完整性可用 `python harrywu/tools/verify_handoff.py`。历史 411 项看图测试通过与本次迁移验证分开报告。
- 修改只在用户指定分支；不强推、不直接修改 main。真实交易、仓位或其他重大承诺仍由用户决定。
