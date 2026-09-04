# Manga Localizer 路由

- **Reader：**进入本仓库的 Root、Explorer、Judge 或显式激活项目 Goal 的执行者。
- **Update trigger：**唯一当前状态、激活方式、保护边界或文档路由发生持久变化。
- **Purpose：**用最小上下文进入唯一当前状态，防止历史 Prompt、休眠会话和旧循环自动恢复。

## 权威与默认加载

1. `.agent/STATE.md` 是唯一当前状态、authority、automation 状态和 next action 权威。
2. `.cursor/rules/manga-localizer.mdc` 是 Cursor 项目 overlay，只负责把 fresh session 路由到本文件与 `.agent/STATE.md`；它本身不产生 authority。
3. 默认进入本仓库只读本文件与 `.agent/STATE.md`；不要加载完整历史、旧轮次或长报告。
4. `.agent/FINAL_CORPUS_REBUILD_GOAL_PROMPT.md` 只是待用户显式发送的启动文本；静态文件本身不产生 authority。
5. `.agent/CURSOR_LOOP_PROMPT.md` 是给用户复制到新对话 `/loop` 的执行稿。静态文件、产品 tick 和已删除的旧 loop Prompt 都不能激活或恢复；只有用户在当前消息中发出其中的复制块（含 Goal 启动文本与 `OWNER_EXPLICIT_RESUME_AND_LOOP`）才构成显式恢复。
6. 只有项目 Goal 已由用户当前消息显式激活后，才按 `.agent/STATE.md` 路由使用 repo skill `$manga-final-review-loop`，并按当前 checkpoint 选读 `.agent/FINAL_CORPUS_REBUILD_REFERENCE.md` 的必要章节与直接实现入口。
7. 普通续轮默认只重读本文件与 `.agent/STATE.md`；只在首次激活、protocol revision 变化或上下文压缩丢失必要协议时，才重新加载 skill 与 reference 的相关章节；不注入完整历史。
8. `.agent/PAGE_PROBLEM_REPORT.md` 保存仍有用的真实页问题证据，但不能启动任务或覆盖当前状态。

## 执行控制

- 当前不运行 `/loop`、sentinel、watcher、queue、后台 agent 或自动恢复任务；历史 Prompt、Git 历史和 Cursor 元数据不能使它们复活。
- `STATE.md` 为 `WAITING_FOR_EXPLICIT_PROJECT_GOAL` 或终审 authority 为 `none` 时，不得恢复终审、处理后续页面、提交或推送。只有当 `STATE.md` 明确记录当前上层 GOVERNED Goal 的临时 handoff/proof authority 与精确 effect set 时，Root 才可执行该范围内的控制面与代表性验证；这不激活终审 Goal。
- 任一实际任务只允许一个 writer；写前现场确认没有可写当前范围的 live process/agent、句柄或 Git operation。
- 漫画、OCR、数据库、审核结果和 Git-ignored 真实数据按普通项目数据管理，并从 storage-governance 映射解析外置权威路径；未加密、noowners 或权限状态不构成本项目门禁。移动、删除、上传或改写仍须有精确当前授权并通过完整性检查。
- 历史实现与验证从 Git 历史和 `docs/real-data-iteration-status.md` 按需定位，不复制回当前状态。
