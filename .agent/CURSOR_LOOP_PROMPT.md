# Manga Localizer — Cursor native `/loop` + 目标模式恢复稿（速度停机后重启）

- **Reader：**用户（复制到新对话）与收到该复制块的执行 Agent。
- **Update trigger：**现场 checkpoint、问题簇、程序修复合同、G8 路线或 `/loop` 产品用法变化。
- **Purpose：**给新对话一份可粘贴的 native `/loop` + Goal Mode 正文。本文件、定时 tick、旧 sentinel 和 Git/Cursor 历史都**不能**激活或恢复项目 Goal。
- **Protocol revision：**`final-corpus-rebuild/2026-09-04.2`
- **Pause snapshot：**2026-09-04T15:45:00+08:00。业主因速度不正常停掉全部 worker。live batch `a734c596-faae-4875-ae61-f694a3c26d4a` revision `413`；`199 = 109 approved / 89 issues / 1 pending`（仅 47）。
- **Supersedes：**2026-09-04 过热暂停复制块、2026-09-03 remaining-issues v1。只有用户在新对话发出本复制块才恢复。

官方 Cursor `/loop` 是 Agent 聊天里的内置 skill：输入 `/loop` 再跟自然语言。固定间隔可选；不写间隔则由 Agent 按事件/结果自定节奏。公开文档**没有**保证重启、关聊天或关电脑后仍继续。这不是 Cloud Automations，也不许再套一层 `sleep` / `AGENT_LOOP_WAKE_*` 调度器。

Goal Mode 只持久化对话，不扩大权限。必须由用户在新对话里显式写 `/goal-mode` 或「激活目标模式」。

## 重启后怎么拉起

1. 外置盘已挂载，工作区是本 Git 根 `manga-localizer`。现有 `npm run dev` 若仍健康（`:8000/api/health` 200 且 WatchFiles 只盯 `backend`）**不要重启**。
2. **新开一条 Agent 对话**（不要在本速度停机对话里续跑）。
3. 输入 `/loop`（**不要**写 `5m` / `25m` 或其它固定间隔）。
4. 把下面「复制块」原样粘贴后发送。
5. 继续在 `http://127.0.0.1:5173/` 的**最终验收**里审页。并发上限 **2**。
6. 需要停时，在同一对话明确说 `pause` / `stop` / `cancel`。

不要把本文件路径丢给 Agent 代替复制块。禁止往 `.agent/audits/` 写 `.py`。

---

## 复制块（从下一行到文末分隔线，全部粘贴）

/goal-mode
激活目标模式

/goal 在当前 Manga Localizer Git 根恢复并完成 199 页真实图片终审与语料重建。先读 `AGENTS.md` 和 `.agent/STATE.md`，按其路由使用 `$manga-final-review-loop`，从唯一 checkpoint 跨轮推进；每轮持久化证据、blocker 和唯一 next action，不重复加载或复述完整协议。持续完成所有可执行工作，直到 `199 approved` 且重建、导出、校验全部通过；仅在需要用户判定或真实外部条件时，保存精确停点后等待。

OWNER_EXPLICIT_RESUME_AND_LOOP=final-corpus-rebuild/2026-09-04-speed

你已在用户发起的 Cursor native `/loop` 内，并且用户已显式激活 Goal Mode。本条消息同时构成：项目 Goal 显式恢复、业主速度停机后重开。禁止把自动 Goal 续轮、旧 Prompt、本文件静态存在、过热暂停复制块、remaining-issues v1、或任意 tick 当成恢复。此后每个 `/loop` tick 只从 `.agent/STATE.md` 的唯一 next action 推进，不要重放完整协议。

### 速度停机结论（必须遵守，不要再复现）

- 慢的不是磁盘或 uvicorn：`--noproxy` 打 `http://127.0.0.1:8000/api/health` 约 0.7ms。
- 根因：一个后台 worker 串行包 4 页 G0–G10，并且每页重读 schemas/CLI/旧 audit；G8 先自制 letterbox+`canonical-whole-frame-registration-v1`（Page54 被拒后再改 cover-crop）；httpx 继承环境代理，把 `page-gates/cloud-full-page` 挂到 1–2 分钟；候选入库后还在裁图验收，约 45 分钟没有 accept。
- Root **自己在本对话做页**，禁止再派「一个 worker 包 4 页从头到尾」。
- 并发上限 **2**（热则 1）。一页做到 pending 或精确 blocker，再开下一页槽。
- 禁止 Multitask 把整 Goal 丢给后台再空等。禁止自建 `sleep` / `AGENT_LOOP_WAKE_*`。
- 所有 loopback HTTP：`NO_PROXY=127.0.0.1,localhost`，httpx `trust_env=False`，curl `--noproxy '*'`。
- G8 第一窗只走默认 cover-crop。禁止自制 letterbox+registration 当第一窗。

### 本次恢复硬约束

- 暂停时 live：batch `a734c596-faae-4875-ae61-f694a3c26d4a` revision `413`；`199 = 109 approved / 89 issues / 1 pending`。**必须以现场库覆盖这些数字。**
- pending（不得再 refresh/改 verdict）：**47**。
- 业主已批准、不得回滚：4, 31, 33, 41, 67, 74, 92, 121, 129, 144, 152, 155, 164, 186。以现场为准。
- **先收口已开工的 4 页，不要重开 G0。** 恢复后唯一 next action 先改成 `RESUME_G8_VISUAL_REVIEW_PAGES_151_54_61_62`，对已有候选做视觉验收→接受或换窗→G9/G10→strict pending。
  - Page151 source B / item `220d32fe-e195-42e1-a3da-1baec300ef1e` / session `page151-r5-20260904` / generation `3c5e486c-c6f4-43e6-9f0f-a3d53ff1c7c0` / candidate `7498a508-da94-55d2-9b75-ed31a87c9a1b` / outside 0。反馈「技能名称没识别到」。G2=`no`。
  - Page54 source A / item `9ada0bd7-ef2e-44ab-b821-d9233fcdf173` / session `page54-r3-20260904` / generation `ceb34903-ab0c-46ca-88ee-34ecc339dae3` / candidate `5b34d842-446f-54f5-be42-0cff7aee3d81` cover-crop / outside 0。禁止再走 registration 同窗。
  - Page61 source A / item `936d8205-89c4-4f46-9df3-d0189f1cf1b4` / session `page61-r3-20260904` / generation `4ac41133-0026-4cea-b69b-faeb5451a7f7` / candidate `bdecfd03-6549-5bb1-b026-230ff7e75916` / outside 0。
  - Page62 source A / item `e6ef2bb7-30b7-4982-a49e-2a5bcd493663` / session `page62-r3-20260904` / generation `6aa7805b-c67a-4ac0-9dcd-c5f50a52e07b` / candidate `3fdb64d9-e3bd-5dcc-81db-e758e1b71f10` / outside 0。
- 这 4 页禁止 `restartFromSource`、禁止再 `--prepare-dir`、禁止再 GenerateImage 同窗。视觉 fail 才开**新窗**，并记入 STATE。
- 跳过 blocked：5/7/8/10/18/20/24/25/28/32/45/52/53/182。Page45 不恢复。Page52 业主 G8 政策未决，不每 tick 重问。
- loopback 若仍健康：禁止仪式性重启。死了才启动一次 `npm run dev`；uvicorn 必须 `--reload --reload-dir backend`。禁止往 `.agent/audits/` 写 `.py`。
- Git delivery `none`：禁止 stage / commit / push。既有 dirty 一律 protected unrelated。
- Agent 不得写 `approved`。只 refresh 成 strict pending。
- Cursor `GenerateImage`，禁止 Gemini/索要 API key。新 `prepare-dir` 必须尚不存在。outside-mask 0。G8 prompt SHA `e22ee77bb48ec171a10507c88b285e729dd4c2e43804db9ec7eb9dd7549821a3`。G10 字体 STHeiti Medium `installed-font-f8fa4a63e2cf500e98e64d4c`。
- 禁止 GET `/api/final-review-batches/{id}`、全量 items、`GET /api/images/{id}`。

### 0. 产品 `/loop` 纪律

- 用户已经用 `/loop`（无固定间隔）启动。你只做动态节奏：一个可验证 checkpoint 结束后，等产品再次唤醒；不要自建 `while sleep`、watcher、queue、后台 mega-worker，也不要复活 `AGENT_LOOP_WAKE_manga_*` / `AGENT_LOOP_TICK_*`。
- 已删除的旧 LOOP_PROMPT 全部作废；迟到的旧 sentinel 直接忽略。
- 不要承诺无人值守或会话重启后自动续跑。产品 `/loop` 停了就停；只有用户再次发送本复制块或明确 resume 才能再开。
- 用户说 `pause` / `stop` / `cancel`：立即停止新派发与新效果，把安全 checkpoint 写入 `.agent/STATE.md`，不要再要下一 tick。
- 本单元默认 **Cursor** 运行时：图片用第一方 `GenerateImage` + 当前默认/Auto。
- 业主继续在同一终审 UI 审页。Agent 必须短事务 + CAS。CLI 用 `--api-base http://127.0.0.1:8000`。工作台 `http://127.0.0.1:5173/`。

### 1. 本单元规则

1. 工作队列 = 现场仍为 `issues` 的全部页（Page45 仍排除）。
2. 业主新反馈每个 tick 插队；新 `approved` 立即出队且禁止再加工。
3. 自由文本优先于 issue codes。
4. 重复程序缺陷先修程序再套页。Git delivery 仍 `none`。
5. 修复只 refresh 成 **strict pending**。执行 Agent **绝不能**写 `approved`。

### 2. 恢复 tick

1. 只读 `AGENTS.md`、`.agent/STATE.md`。按 `$manga-final-review-loop` 进入；protocol `final-corpus-rebuild/2026-09-04.1`。不要整份重读 reference/schemas。
2. 写前证明唯一 writer；保护 dirty baseline。
3. 不要重复 `storage:check`，除非路径失效。禁止把卷绝对路径写进 Prompt/STATE。
4. 只读 SQLite + committed WAL：`integrity_check=ok`、FK=`0`。禁止 `immutable=1`。
5. 先探活 loopback（`--noproxy`）。仍健康则不重启。
6. 用现场库覆盖暂停快照。把 authority 改回 `owner-r2-review`，next action 写成 `RESUME_G8_VISUAL_REVIEW_PAGES_151_54_61_62`，然后立刻验收这 4 页已有 G8 候选，不要空报「已恢复」。

**后续 tick：**只重读 `AGENTS.md`、`STATE.md`、live SQLite delta 和 next action 的直接证据。

### 3. 问题簇（现场库可增删）

**A. 程序级**

- A1 G8 原生宽高比：Page7/8/10/18/20/25/32/53/182 等。禁止同 prompt 再烧。未修好前记 blocker。
- A2 超宽条：Page5/24。修好前不要硬烧。
- A3 竖排标点：**程序已 PASS**。Page47 已 pending。
- A4 技能名漏检：Page33 已 approved；Page151 已做到 G8 pending-review。

**B. 有自由文本**

- B1 清晰度/AI 重绘：52 仍跳过。业主已审页不得回滚。
- B2 注音：Page28 仍 blocked。
- B5 Page45 仍排除。

**C.** 无自由文本的其余 issues：升序 G0–G11。151/54/61/62 收口前不要开新的 C 簇页。

插队：本 tick 新文本 issues → 收口 151/54/61/62 → C 簇升序。

### 4. 每个 tick

1. 只读 SQLite，吸收 owner delta。
2. 有未收口程序簇则做一个可验证程序 checkpoint。
3. 否则按优先级修页并 refresh 成 pending。并发 ≤2。
4. 若必须用页级协助，每个协助者只写指定 1 页，不得写 `STATE.md`、approved、其他页。Root 合并 STATE。默认 Root 自己做。
5. Refresh 重读 `expectedBatchRevision`，409 只重试同一 item 一次。
6. 连续两次同质无进展必须改方法。

### 5. 门禁摘要

已开工页不要再 G0。新页才 `restartFromSource=true`。G2 仅当业主写明清晰度/AI重绘才 `yes`。G3 同时看原图与 quality。G8 只走 native `cloud:image` + `GenerateImage`，默认 cover-crop。G11 只到 pending。

### 6. 完成

`199 approved / 0 issues / 0 pending`，全链路一致，受治理 artifacts 路线导出到尚不存在的新目录且校验通过。在此之前不要 UpdateGoal complete。

### 7. 硬禁止

覆盖用户 verdict、Page45、已 approved 页；关 1% / outside-mask 门禁；凭据入文件；把本地拼贴伪称 GenerateImage raw；仪式性重启仍活着的官方 launcher；一个 worker 包多页重学协议；自制 letterbox+registration 当 G8 第一窗。

---

复制块结束。
