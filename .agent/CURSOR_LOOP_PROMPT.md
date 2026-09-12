# Manga Localizer — Cursor native `/loop` 恢复稿（2026-09-07 晨停点）

- **Reader：**用户（复制到新对话）与收到该复制块的执行 Agent。
- **Update trigger：**现场 checkpoint、复用规则、页槽调度或 `/loop` 用法变化。
- **Purpose：**给新对话一份可粘贴的 native `/loop` + Goal Mode 正文。本文件、定时 tick、旧 sentinel 和 Git/Cursor 历史都**不能**激活或恢复项目 Goal。
- **Protocol revision：**`final-corpus-rebuild/2026-09-04.2`
- **Resume token：**`final-corpus-rebuild/2026-09-06-parallel-slots`
- **Live overlay at draft：**2026-09-07T05:44+08:00。业主在旧对话要求停全部任务，到新对话重启。现场 batch `a734c596-faae-4875-ae61-f694a3c26d4a` **r482**：`199 = 140 approved / 59 issues / 0 pending`。Page68/112/123 新 G7 已 accept，待第一窗 cover-crop。
- **Supersedes：**`2026-09-04-salvage`、`2026-09-04-speed`、`2026-09-04-ramp8`、`2026-09-05-cursor-cloud-first-max8`、过热暂停复制块、以及本文件此前所有草稿 overlay。只有用户在**新对话**发出本复制块才恢复。

官方 Cursor `/loop`：输入 `/loop` 再跟自然语言。不要写 `5m` / `25m`。不许自建 `sleep` / `AGENT_LOOP_WAKE_*`。Goal Mode 必须由用户显式写 `/goal-mode` 或「激活目标模式」。

## 重启后怎么拉起

1. 外置盘已挂载，工作区是本 Git 根 `manga-localizer`。
2. **新开一条 Agent 对话**（不要在已停机对话里续跑）。
3. 先输入 `/loop`（无固定间隔），再把下面「复制块」原样粘贴后发送。
4. 只开终审页 `http://127.0.0.1:5173/#final-review`。不要打开 source A/B 工作台。
5. 需要停时说 `pause` / `stop` / `cancel`。

不要把本文件路径丢给 Agent 代替复制块。

---

## 复制块（从下一行到文末分隔线，全部粘贴）

/goal-mode
激活目标模式

/goal 在当前 Manga Localizer Git 根恢复并完成 199 页真实图片终审与语料重建。先读 `AGENTS.md` 和 `.agent/STATE.md`，按其路由使用 `$manga-final-review-loop`，从唯一 checkpoint 跨轮推进；每轮持久化证据、blocker 和唯一 next action，不重复加载或复述完整协议。持续完成所有可执行工作，直到 `199 approved` 且重建、导出、校验全部通过；仅在需要用户判定或真实外部条件时，保存精确停点后等待。

OWNER_EXPLICIT_RESUME_AND_LOOP=final-corpus-rebuild/2026-09-06-parallel-slots

你已在用户发起的 Cursor native `/loop` 内，并且用户已显式激活 Goal Mode。本条消息同时构成：项目 Goal 显式恢复、业主按并行页槽调度重开。禁止把自动 Goal 续轮、旧 Prompt、已停对话、`2026-09-04-salvage`、`2026-09-04-speed`、`2026-09-04-ramp8`、`2026-09-05-cursor-cloud-first-max8`、过热暂停复制块、本文件静态存在或任意 tick 当成恢复。此后每个 `/loop` tick 只从 `.agent/STATE.md` 的唯一 next action 推进，不要重放完整协议。

### 现场停点（以 list API 覆盖，禁止 GET `/{id}` 与 `/items`）

- batch `a734c596-faae-4875-ae61-f694a3c26d4a` **r482**，`199 = 140 approved / 59 issues / 0 pending`。`expectedBatchRevision=482`。
- 先 `GET /api/final-review-batches`（列表）核对 counts/revision。禁止 `GET /api/final-review-batches/{id}`，禁止 `/items` 全量（会卡死事件循环）。定位 issues 只用 FR SQLite。
- Unique next action 起步：`PAGE68_FIRST_G8_COVERCROP`。新对话 execute 配额重置，硬限仍 **=1**。
- 权威始终是 `.agent/STATE.md`。本复制块只是入口；冲突以现场库 + STATE 为准。

**已 approved、禁止 refresh / 改 verdict：**  
4, 28, 31, 32, 33, 41, 61, 62, **64**, 66, 67, 71, 74, 76, 86, 90, 92, 93, 95, 96, 99, 102, 111, 113, 114, 115, 116, 121, 129, 136, 142, 144, 145, 147, 149, 150, 152, 154, 155, 160, 164, 174, 186, 189。以现场为准。Page64 item `008e71f9-e852-4a32-a5af-1a4025463278`。

**本档可执行（先 G8，再 remask）：**
1. **Page68** 第一窗 **3:4 cover-crop**。item `04b9ada4-dedc-4867-be72-fb9e88798c64` / repair `ed664fb8-4f33-4ecd-9b59-2ee208c3f2b7` / gen `7e719d5f-52c2-4348-a320-28487e077285` / run `final-review-04b9ada4-r2` / 新 G7 `fb17fc97-7253-51e0-bab3-8cb384c2abc4` / mask SHA `edba90f20e08b7f452fa456e1d5445662decb2c668c1bd8813d55f5f2bf3440f` / G1 1044×1252 SHA `e06d04b8f8cf1088e378f1ebf44d915a5a1f5bce610b0994590bd4f18ac05820`。manga01 project `63ac150e-b071-41d9-af18-6d31d10f4590`。勿烧旧 G7 `4b2008fe`。
2. **Page112** 第一窗 **4:3 cover-crop**。item `b9dcfedb-4f02-4d01-9cf7-96e4844f6016` / repair `9439659b-695a-48a9-9fd5-f197b62cbc2e` / gen `2859a8bd-fc90-4f40-8d98-9e87ef77988b` / run `final-review-b9dcfedb-r2` / 新 G7 `8d8ec8df-f969-5647-91e3-720997b9fbfe` / mask SHA `5abe66a9a10e02d856236937c2238e29a4a965bde37859ac4235161b2e5f0d71` / G1 2352×1716 SHA `70200af606171cb420e6017f849713ef3e58381de8110f2fca9345f8919031f3`。勿烧旧 G7 `2a3ba601`。
3. **Page123** 第一窗 **4:3 cover-crop**。item `8fe86307-8aea-4f2c-b6cc-6404b8901a3b` / repair `08cb1247-9548-4ea4-969b-54cef81789b6` / gen `30ccb421-3c02-4fb2-8b04-d7d90b061709` / run `final-review-8fe86307-r2` / 新 G7 `2fe37386-0c72-5af1-abe3-22df4e02645f` / mask SHA `ded6a80055b04bd42a5dd25736cb9eebf303a3cd4d344a84673bf601527594b7` / G1 **5132×3634** SHA `6ab525f73b554baccd931a08d9c3523460d13104e833ce41ea9be8b7f3a4de11`。勿烧旧 G7 `95bb8cfc`。超大页：GET quality/mask artifact 前后必须 `%cpu < 25`。

**新 mask 两窗已尽、禁止 a3：**  
7, 24, 45, 52, 54, 69, 146, 151, 161, 166, 167, 169, 172, 173, 175, 176, 177, 178, 181, 182, 184, 187, 188, 191, 192, 194, 196, 197, 198。  
**190 仅 a1、禁止 a2。**  
勿空烧已列 raw / candidate。

**禁止 leftover 折线 / 密点 dump / leftoverDark q&lt;80 对插图网点：**  
53, 134, 135, 162。未 accept 的喷雾 draft 不要再烧。

**其余 issues（可 reopen G7，先核 `backgroundCategory`）：**  
5, 8, 10, 18, 20, 25, 47, 79, 85, 87, 88, 89, 91, 103, 125, 127, 128, 130, 138, 139, 157, 159。  
先 `GET /api/images/{repair}/regions`。全 illustration/character 或 screentone / 超高黑底框：跳过，不要 leftoverDark dump。优先全白实心底。

### 必须遵守、不要再复现

**打回不是推倒重来。** 业主 issues / 视觉 fail 默认门禁局部。复用已 accepted 的 G0–G(n-1)。禁止「又是 issues 就 G0 / `restartFromSource`」。
- G8 失败 → 保留 G0–G7；只换窗或补 mask。
- G10 失败 → 保留 accepted G8。
- 漏框 / 漏标点 → 只回 G4/G7。
- 只有反馈指向 source/preprocess，或当前 accepted quality/mask 真的不能用，才 `restartFromSource`。
- 无新 G0。Agent 不得写 `approved`。refresh 只能到严格 pending。

**HTTP / 进程（旧对话反复踩过）。**
- 只开 `http://127.0.0.1:5173/#final-review`。禁止 source A/B 工作台。禁止 `npm run dev`。
- 8000/5173 活着且 health=200：**禁止仪式性重启**（含「Relaunch guarded uvicorn」）。旧对话有一条重启 wrapper 已被用户终止；不要再拉第二条 8000。
- 禁止 GET：`/api/final-review-batches/{id}`、`/items` 全量、`GET /api/images/{id}`、整份 `cloud-full-page` 上下文。
- 允许：list `GET /api/final-review-batches`、`.../revisions`、`.../regions`、`.../page-gates/mask`、`.../page-generations/{id}/events`、`GET /api/images/{id}/generated/quality`、mask/candidate artifact、`GET /api/final-review-items/{id}/revisions`。
- loopback：`NO_PROXY=127.0.0.1,localhost`，httpx `trust_env=False`，curl `--noproxy '*'`。
- PATCH/GET 后 uvicorn 常冲到 80–99% CPU；health=200 不要重启。等 `%cpu < 25` 再发下一条重请求。
- Auto-review 常把 loopback remask / G8 review PATCH / `--execute` 误判为「重启/停进程」。必须用**完全相同命令** + `request_smart_mode_approval=true` + **原文 block reason** 重试。不要换 host，不要跳过 mutation。
- 禁止 `git status` 无忽略地扫 `.agent/audits/`。禁止往那里写 `.py` 或整页 quality/mask/raw 副本（checksum-bound `--prepare-dir` 除外）。
- Detect/OCR 必须显式 `tesseract`。Git delivery `none`。
- mask job 的 project id 必须是该 repair image 所属项目。manga01 = `63ac150e-b071-41d9-af18-6d31d10f4590`；manga02 = `b1b85f3e-4d72-4956-b519-6b30fee02fcc`。用错会 400。

**调度。**
- 页槽阶梯 `1→2→3→4→6→8`，硬顶 8；当前可保持 `pageSlots=4`。pending 不占槽。
- 同时 `repair+refresh ≤ 2`；`--execute` = **1**；mask/ocr/detect enqueue ≤ 3。
- 禁止在 live `--execute` 上叠另一条 ingest 或 `--prepare-dir`。新 tick 或同一 tick 上一条 execute 已完成后才可再 ingest。
- 禁止整读 `STATE.md`。每 tick 只读 Execution control 前约 25 行 + Unique next action 点名的那一条。
- 唯一 writer = 每个 image/generation 一个。Root 写 STATE / 控制面。

**G7 remask。**
- `POST .../mask/reopen` body 只需 `expectedRevision` + `lineage`（`reopen-for-owner-issues`）。
- 新配方惯例：白实心底用 **text pad1（或 pad2）dilate1 feather0 dark** + 少量 leftover **单点心**（半径约 2.4–4.2，夹在源图框内）。
- Stroke schema 恰好 `{mode:"add"|"erase", radius:float, points:[[x,y],...]}`，源图坐标；服务端 × renderScale=2。连续点走 `cv2.line`——**禁止折线/密点 dump leftover**（会喷出框外两万 px）。
- leftoverDark-in-boxes q&lt;80 只对**白实心底**。illustration/character / 网点 / 黑底：禁止 leftoverDark dump。
- Job：`POST /api/projects/{proj}/mask`，`regionIds: []`，带 `JobLineageContext`。
- Accept：恰好 5 coverage + 5 collateral 全过，`complete-and-no-collateral`，带 `observedMaskChecksum`。
- Lineage actor：`{actorKind:cursor, actorId:cursor-agent, taskId:manga-native-image, threadId:manga-localizer, sessionId:<page-session>, operationSource:script}`。

**G8。**
```
npm run cloud:image -- --api-base http://127.0.0.1:8000 --runtime cursor \
  --image-id <id> --session-id <same> --prepare-dir <new-absolute-dir-under-.agent/audits>
# 再同一 session + --raw-image <abs.png> --execute
```
- prepare-dir **不得已存在**。`--execute` **不能同时**带 `--prepare-dir`。
- Prompt 必须是 CLI `PROMPT`；SHA 必须是 `e22ee77bb48ec171a10507c88b285e729dd4c2e43804db9ec7eb9dd7549821a3`。
- Cursor `GenerateImage` + 当前默认/Auto。禁止 Gemini / 禁止索要 API key。
- 第一窗：只 cover-crop（refs = prepare-dir 原尺寸 quality+mask）。
- 第二窗 letterbox：**先**把 quality/mask pad 到与目标 GI 画布同尺寸，**再**用 padded 图做 GenerateImage。quality: Pillow LANCZOS + 边条 replicate；mask: NEAREST + constant 0。**禁止 cv2.copyMakeBorder**（曾挂死约 100s）。
- JPEG/JFIF 按字节导入有效（`.png` 后缀 JPEG 记 `image/jpeg`）。
- Review PATCH：恰好 10 项 `CLOUD_FULL_PAGE_CHECKS`；必须带 `observedChecksum` = normalized SHA。reject `reason` 为唯一失败 check 名，多于一项则 `multiple-visual-failures`。用 mask-gate 的 `imageRevision`/`nextSequence`，不要 GET 整份 cloud-full-page。
- 硬门禁 `outsideMaskChangedPixelCount == 0`。不得自动 accept / 写 approved。
- 两窗同质失败后 **禁止 a3**。

G8 checks 精确顺序：  
`full-page-fidelity, no-new-text, no-new-objects, unrelated-content-preserved, target-source-text-unreadable, no-white-or-gray-hole, no-blur-band, no-repeated-texture, background-continuous, structure-preserved`

G7 coverage：`body-glyphs-covered, punctuation-covered, strokes-and-shadows-covered, ruby-covered, antialias-edges-covered`  
G7 collateral：`bubble-borders-protected, characters-protected, speed-lines-protected, screentone-protected, nearby-art-protected`

G10 字体：STHeiti Medium `installed-font-f8fa4a63e2cf500e98e64d4c`。

### 0. 产品 `/loop` 纪律

- 用户已经用 `/loop`（无固定间隔）启动。一个可验证 checkpoint 结束后等产品再唤醒。不要自建 `while sleep`、watcher、queue、后台 mega-worker。
- 旧 salvage/speed/ramp8/cursor-cloud 复制块与已删除 LOOP_PROMPT 全部作废。
- `pause` / `stop` / `cancel`：立刻停派发，写入 STATE，不要再要下一 tick。
- 本单元默认 **Cursor**：`GenerateImage` + 当前默认/Auto。CLI `--api-base http://127.0.0.1:8000`。

### 1. 本单元规则

1. 工作队列 = 现场仍为 `issues` 且仍有未用合法下一窗（或其它局部门禁动作）的页。pending 等业主。已拒窗 / 同质失败不占槽。
2. 业主新反馈每个 tick 插队，且按门禁局部复用。
3. 新 `approved` 立即出队，禁止再加工。
4. 自由文本优先于 issue codes。
5. 重复程序缺陷先修程序再套页。修 backend 前权衡重启成本；健康则不要重启。
6. 修复只 refresh 成 **strict pending**。执行 Agent **绝不能**写 `approved`。

### 2. 恢复 tick

1. 只读 `AGENTS.md`、`.agent/STATE.md` 的 Execution control 前约 25 行。按 `$manga-final-review-loop` 进入；protocol `final-corpus-rebuild/2026-09-04.2`。
2. 写前证明：控制面只有 Root；每个 image/generation 最多一个页 writer。保护已有 dirty worktree。
3. 不要重复 `storage:check`，除非路径失效。禁止把卷绝对路径写进 Prompt。
4. 探活 loopback。health=200 就用现进程（现场曾是 uvicorn PID `88367` / Vite `57612`，以现场为准）。禁止 `npm run dev`。
5. 用 list API 覆盖暂停快照。authority → `owner-r2-review`。立刻量 CPU。然后做 Page68 第一窗 3:4 cover-crop。
6. 若 68 拒：letterbox 第二窗（下一 tick 或本 tick execute 已空闲）。112 / 123 第一窗随后。两窗尽则换另一张白实心底 issues 页 reopen G7。

**后续 tick：**只重读 `AGENTS.md`、`STATE.md` 头 25 行、live batch 列表 delta、`cpuRamp` 和 next action 的直接证据。`--execute` 永远只开 1 条。

### 3. 每个 tick

1. 只读 batch 列表 + 必要 events/regions。
2. 量 `cpuRamp`。`%cpu < 25` 且 health=200 再发重请求。
3. 按优先级：已 accept 新 G7 的第一/第二窗 → 白实心底 remask → 跳过喷雾/插图页。
4. Refresh 重读 `expectedBatchRevision`，409 只重试同一 item 一次。不要并行堆超过 2 条 refresh。
5. 连续两次同质无进展必须改方法。两窗同质失败禁止 a3。
6. 无可执行 issues 时立即停，等业主审 pending。不要为「有事做」空烧。

### 4. 完成

`199 approved / 0 issues / 0 pending`，全链路一致，受治理 artifacts 路线导出到尚不存在的新目录且校验通过。在此之前不要 UpdateGoal complete。重建/导出尚未开始。

### 5. 硬禁止

覆盖用户 verdict、已 approved 页（尤其 28/64）；关 1% / outside-mask 门禁；凭据入文件；把本地拼贴伪称 GenerateImage raw；仪式性重启仍活着的 8000；打开 source A/B 工作台；GET `/items` 或整份 cloud-full-page；一个 worker 包多页重学协议；自制 letterbox 当第一窗；cv2 letterbox pad；issues 就 `restartFromSource`；往 `.agent/audits/` 写 `.py` 或整页 PNG；并行超过 1 条 `--execute`；`npm run dev` / `--reload`；整读 STATE；空烧已拒窗 / leftover 折线 dump / Page190 a2 / 两窗页 a3。

---

复制块结束。
