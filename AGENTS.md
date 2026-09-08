# Manga Localizer 路由

Reader：项目 Root、按需执行者、审查者与 Hub。更新条件：权威、状态格式、激活或验证入口改变。用途：用最小上下文识别当前事实与执行边界。

默认只读本文件及 `.agent/STATE.yaml`。后者是唯一当前状态，格式为严格 YAML；旧 Markdown 状态已迁入 Git 历史；禁止重复键、alias/anchor。读取 `execution_control`、`current_work`、`blockers` 与本任务 `all_projects_governance`；不要默认载入历史。

原项目状态为 `OWNER_R2_REVIEW / owner-r2-review`。全项目治理只做当前合约内的仓库维护，`project_execution: not_active_in_this_governance_goal`；它不启动终审 Goal、旧 loop、worker、queue 或后台恢复。静态 Prompt、skill、Git 历史和原生任务旧状态都不产生执行授权。

只在所有者明确激活项目执行后按共享 skill `manga-final-review-loop` 与状态中的精确页历史进入检查点；每页的禁止项和失败证据仍有效。历史权威字节保留在 STATE 的 `history.state_checkpoint_commit` 中；只读所需页，不把旧指令当新授权。

## 本治理单元

先读 STATE 指向的 contract/baseline。Root 独占控制面、状态、证据、Git 和原仓安装；Repair 仅写分配的产品代码/测试。原仓 Stage A 只读，Stage B 逐文件或逐 hunk CAS；不更新原仓 Git。保护全部既有内容，已交付相同文件复用，未交付内容逐项处置。

不操作 Cursor；不读取真实环境配置或秘密。不用应用入口打开真实数据库，不写真库、图像、用户 verdict 或 lineage，不启动真实服务、生成、付费调用或导出。仅隔离数据库/媒体/浏览器用于本轮验证。真实数据保持既有存储路由，不迁移、不清理。

首次进入当前运行时用 `npm run storage:check`；未变路由复用。原项目执行仍须显式授权并使用既有受保护存储入口。文件能力不扩大外部效果权限。

检查：`npm run check:backend`、`npm run check:frontend`、相关 `npm run test:e2e`；本状态校验为 `node scripts/external-uv.mjs run --frozen --offline --no-sync python -B scripts/check_state.py`。Hub 从根 `hub.connection.yaml` 读取同一 YAML 状态，声明不复制动态业务值，也不执行 validation_entry。

正常交付必须运行 `npm run audit:ci -- --base <完整验证前基线> --head <完整候选提交>`；本单元审查整个 checkpoint→最终候选系列。fresh Judge 与 Governor 通过后才正常推主分支并验证远端和 CI。禁止强推、绕过保护或重写历史。`audit:release` 另管产品发布，普通 CI 不解除历史发布阻塞。
