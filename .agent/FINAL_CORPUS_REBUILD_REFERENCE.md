# 199 张真实数据终审、溯源与重做参考规范

- **Reader：**已由用户当前消息显式激活 Manga Localizer 项目 Goal 的 Root 与审查者。
- **Update trigger：**领域门禁、受治理存储路由、验收标准或按需读取协议发生持久变化。当前计数、checkpoint、blocker 和 next action 只更新 `.agent/STATE.md`。
- **Purpose：**保存稳定领域规范与事实底稿，由 repo skill 按当前 checkpoint 渐进式读取，不作为第二套当前状态。
- **Protocol revision：**`final-corpus-rebuild/2026-09-04.2`

本文档、repo skill、静态 Prompt、历史记录或文件存在本身都不授权启动服务、导出图片、改写审核结果或重做页面。只有用户在当前消息中显式激活配套 Goal 后，才按 `.agent/STATE.md` 进入执行。

## 0. 渐进式读取路由

- 首次激活、protocol revision 变化或上下文压缩丢失必要协议时，先读章节标题，再只读当前 checkpoint 需要的章节。
- 普通续轮只重读 `AGENTS.md`、`.agent/STATE.md` 和 next action 的直接证据；不整份重读本文档，不把它复制进 Prompt、STATE 或进度消息。
- 启动、权限、事实优先级与存储前置：第 1–3 节。
- 历史归因或具体问题族：第 4–5 节及 `.agent/PAGE_PROBLEM_REPORT.md`。
- 单页重做门禁：第 6 节；产品能力核验：第 7 节；多轮 checkpoint：第 8 节。
- ledger 与参数：第 9–10 节；人工终审和终态导出：第 11 节；代码入口：第 12 节。

## 1. 目标、边界和权威来源

后续长任务的目标是：对 199 张全部完成同字段、可审计的原图—中间产物—冻结成品—数据库链路溯源，比较无问题和有问题两组流程差异；保留并导出用户已经判定无问题的页面；对所有有问题页面从项目持有的不可变原图重新开始，按严格的逐页状态机重做；每一轮修复结果都回到 Manga Localizer 内置的最终审核页，由用户继续给出终审结论，直到 199 张全部闭环。

以下规则是硬边界：

- 最终审核页属于 Manga Localizer 项目工具，不能被独立脚本、外部相册或一次性网页替代。
- 统计表、SQL 查询、对比接触表和临时对比图只能作为证据辅助；最终 verdict、问题反馈、修复回到工作台、快照刷新和 approved-only 导出必须继续走项目工具。
- 当前用户终审判断是权威质量标签。执行 Agent 不得把用户标记为 `issues` 的页面擅自改成 `approved`，也不得覆盖用户反馈。
- 2026-08-25 历史基线中的 41 张初始 `approved` 和 158 张初始 `issues` 都要完成同字段全链路溯源，用于比较流程差异；这些数字是历史 cohort，不是当前执行计数。
- 历史 cohort 中的初始 `approved` 页只读溯源，不重新加工、不 refresh、不改变 verdict；初始 `issues` 页不得以错误终审 PNG 或旧派生结果作为新输入，必须从对应 immutable source 建立新 lineage。
- 所有真实图片、OCR 文本、译文、审核反馈和审计产物按普通项目数据存放在受治理外置 ProjectData，并保持 Git-ignored。未加密、noowners 或权限状态不构成本项目门禁；上传到远端 AI 服务仍须由用户另行明确授权。
- 不允许 Codex、Cursor、后台批处理器或其他会话同时写同一个源项目、审核库或导出目录。开始写入前必须确认唯一写入者。
- 用户发出暂停、停止或取消时，立即停止新任务派发，保存当前页面和审计 ledger，停止服务，并保持 Goal 未完成状态供以后恢复。

控制 authority 与产品事实分开：`.agent/STATE.md` 是唯一当前控制权威，决定 activation、authority、automation 状态和 next action；其中的产品计数只是需由现场数据库覆盖的快照，不能代替数据证据。

产品事实优先级如下：

1. 当前三个 SQLite 数据库及其 manifest/checksum。
2. 两个源项目的 portable `project.json`、run manifest、作业和 revision 记录。
3. 用户最终审核 verdict、issue codes 和自由文本反馈。
4. 当前代码、测试和文档描述的行为。
5. Git、Codex/Cursor 任务记录和历史日志。

执行时必须重新查询审核库；不得用 `STATE.md` 的快照、Git/Codex/Cursor 历史或旧日志覆盖当前数据库与用户 verdict。

## 2. 数据路径和固定输出约定

执行 Agent 先把仓库绝对路径解析为 `REPO_ROOT`，运行 `npm run storage:check`，再以 `npm run -s storage:data -- --print-real-data` 的唯一输出作为逻辑 `REAL_DATA_ROOT`。以下路径都相对该外置根；Prompt、STATE 和本文档不固化卷绝对路径。

### 2.1 最终审核批次

- 批次根：`<REAL_DATA_ROOT>/final-review/all-199-pages`
- 审核 manifest：`<REAL_DATA_ROOT>/final-review/all-199-pages/final-review/manifest.json`
- 审核数据库：`<REAL_DATA_ROOT>/final-review/all-199-pages/final-review/final-review.sqlite3`
- 冻结终审图：`<REAL_DATA_ROOT>/final-review/all-199-pages/images/<final_item_id>.png`
- 缩略图：`<REAL_DATA_ROOT>/final-review/all-199-pages/thumbnails/<final_item_id>.jpg`
- batch id：`a734c596-faae-4875-ae61-f694a3c26d4a`

### 2.2 源项目 A：manga01，终审位置 1–130

- project id：`63ac150e-b071-41d9-af18-6d31d10f4590`
- project name：`round8-clean-plate-final`
- workspace：`<REAL_DATA_ROOT>/manga01/runs/round8-clean-plate-final/workspace`
- 源项目 DB：`<REAL_DATA_ROOT>/manga01/runs/round8-clean-plate-final/workspace/project/project.sqlite3`
- portable 状态：`<REAL_DATA_ROOT>/manga01/runs/round8-clean-plate-final/workspace/project/project.json`
- 项目持有的不可变 source：`<REAL_DATA_ROOT>/manga01/runs/round8-clean-plate-final/workspace/source`
- 初始真实输入：`<REAL_DATA_ROOT>/manga01/input`
- run 证据：同一 run 目录中的 `run-manifest.json`、`summary.json` 和 `catalog/catalog.json`

### 2.3 源项目 B：manga02，终审位置 131–199

- project id：`b1b85f3e-4d72-4956-b519-6b30fee02fcc`
- project name：`rd-r04-manga02-slice`
- workspace：`<REAL_DATA_ROOT>/manga02/runs/rd-r04-ppocr-lama-json/workspace`
- 源项目 DB：`<REAL_DATA_ROOT>/manga02/runs/rd-r04-ppocr-lama-json/workspace/project/project.sqlite3`
- portable 状态：`<REAL_DATA_ROOT>/manga02/runs/rd-r04-ppocr-lama-json/workspace/project/project.json`
- 项目持有的不可变 source：`<REAL_DATA_ROOT>/manga02/runs/rd-r04-ppocr-lama-json/workspace/source`
- 初始真实输入：`<REAL_DATA_ROOT>/manga02/input`
- run 证据：同一 run 目录中的 `report.json`、`report.md`、`catalog/catalog.json` 和 `export-bundle/export.json`

### 2.4 每个源项目需要对照的中间产物

对全部 199 个 final-review item 进行只读溯源时，至少同时读取：

- `source/<relative_path>`：项目持有的不可变原图。
- `generated/preprocessed/<relative_stem>.png`：基础增强/超分结果。
- `generated/lineage-masks/<page-generation-id>/<artifact-id>.png`：严格 G7 的 immutable mask authority。
- `generated/lineage-clean-plates/<page-generation-id>/<candidate-id>.png`：严格 G8 净版候选。
- `generated/lineage-typesets/<page-generation-id>/<candidate-id>.png`：严格 G10 嵌字候选。
- `generated/masks/<relative_stem>.png`、`generated/inpainted/<relative_stem>.png`、`generated/typeset/<relative_stem>.png`：仅在历史链路实际存在时作为 legacy 证据，不能授权当前严格 generation。
- `original-text/<relative_stem>.json`：导出的原文记录（若存在）。
- `translated-text/<relative_stem>.json`：导出的译文记录（若存在）。
- `project/project.sqlite3` 中对应 image、regions、jobs、revisions、stage reviews 和 provider 记录。
- 最终审核批次中对应 frozen snapshot、issue codes、反馈和 revision history。

任何路径不存在都必须记录为 `missing_artifact`，不得用相邻页面或同名旧 run 的文件替代。历史基线的 41 张初始 approved 也必须逐项完成这份证据清单；“已通过”只禁止重处理，不是跳过历史流程审计的理由。

### 2.5 已激活 Goal 的工作产物根

下列 Git-ignored 目录只由已激活 Goal 使用。首次写入前先检查是否已存在：现有内容是待核验状态，不得删除、覆盖或在无证据时从头重建。

- 审计与重做 ledger 根：`<REAL_DATA_ROOT>/final-review/rebuild-r1`
- 参数决策：`<REAL_DATA_ROOT>/final-review/rebuild-r1/config/resolved-parameters.md`
- 机器可读参数：`<REAL_DATA_ROOT>/final-review/rebuild-r1/config/resolved-parameters.json`
- 逐页溯源表：`<REAL_DATA_ROOT>/final-review/rebuild-r1/audit/provenance-ledger.jsonl`
- 逐页状态机 ledger：`<REAL_DATA_ROOT>/final-review/rebuild-r1/audit/page-ledger.jsonl`
- 代表性对比证据：`<REAL_DATA_ROOT>/final-review/rebuild-r1/evidence/<position>/`
- 阶段进度报告：`<REAL_DATA_ROOT>/final-review/rebuild-r1/reports/progress.md`

以下是 2026-08-25 基线时的早期导出约定，仅作历史证据，不再驱动当前计数、next action 或终态目录：

`<REAL_DATA_ROOT>/final-review/approved-exports/review-r204-approved-41`

当前 Goal 不得假设该目录仍不存在或该 revision/count 仍有效。需要调查早期导出时：

- 不要求当前审核库仍停在 revision 204/41 approved，也不根据当前计数重算这个历史目录名。只验证该快照自身的 `manifest.json` 确实声明对应 revision/count，并能绑定到当时的 frozen evidence。
- 若目标已经存在，不删除、不覆盖、不重复导出。验证其中 `manifest.json`、41 个文件和 checksum；完全一致则登记为历史证据，否则报告路径冲突。
- 已存在且全量一致的历史快照只登记为证据；路径冲突、缺失或 checksum 不一致时暂停该导出分支，不删除或覆盖。

### 2.6 受治理运行时、模型与终态导出

- 每次激活、恢复 Goal 或进入新进程后，任何数据/runtime/model 使用前先运行 `npm run storage:check`。挂载卷缺失、UUID/映射漂移、运行时 marker 过期或 guard 拒绝时 fail closed，不回退到仓库内项目数据，不自建本地 `.venv`、`node_modules` 或模型目录。
- backend 命令统一走 `node scripts/external-uv.mjs ...`；需要受治理模型时显式使用 `--with-guarded-models`。不直接运行裸 `uv`，不覆盖 `UV_PROJECT_ENVIRONMENT`、`UV_CACHE_DIR` 或 `MANGA_LOCALIZER_MODEL_BUNDLE`。
- frontend 不得替换已登记的 `frontend/node_modules` 路由；只用 `node scripts/external-frontend.mjs install` 修复或同步依赖。
- 外置路径每次都从当前 storage-governance guard 现场解析；不在 Prompt、STATE 或本规范中固化 `/Volumes/...` 路径。`MANGA_LOCALIZER_DATA_DIR` 不控制模型包，也不能改写受治理模型目的地。
- 当前没有专用 `final_export_root` 映射。终态导出前从 guard 解析唯一、绝对且 canonical 的 `roots.artifacts`，将目标派生为它的严格子目录 `manga-localizer/final-review/review-r<batch_revision>-approved-199`。这是受验证 artifacts root 下的项目约定，不是 macOS app 的 `package_dest`；未来若 guard 提供正式终审导出映射，优先使用新映射。
- 终态目标必须不存在，不删除、覆盖、合并或复用冲突目录。导出仍必须走 Manga Localizer 内置 final-review export；成功后验证 resolved output directory 精确等于请求目标、`manifest.json` 正好位于其根、199 项无缺失/重复，且所有 checksum 一致。

## 3. 2026-08-25 历史审核基线

本节只是历史 cohort 与根因比较证据，不是当前 checkpoint。当前计数只由 `.agent/STATE.md` 保存为待现场数据库覆盖的快照。当时的只读检查点：

- 199 items。
- 41 `approved`（20.6%）。
- 158 `issues`（79.4%）。
- 0 `pending`。
- batch revision 204。
- 402 history rows：199 条 `create`，203 条 `review`。
- SQLite `PRAGMA integrity_check` 为 `ok`。
- 29 个问题页有自由文本反馈；其余 129 个问题页只有结构化 issue codes。因此不能只修 29 个有文字备注的页面。

按来源项目：

| 来源 | 总数 | approved | issues | 通过率 |
|---|---:|---:|---:|---:|
| manga01 / `round8-clean-plate-final` | 130 | 36 | 94 | 27.7% |
| manga02 / `rd-r04-manga02-slice` | 69 | 5 | 64 | 7.2% |

按终审产物类型：

| final variant | 总数 | approved | issues | 通过率 |
|---|---:|---:|---:|---:|
| `preprocess`（已审无字页） | 43 | 33 | 10 | 76.7% |
| `typeset`（进入文字链） | 156 | 8 | 148 | 5.1% |

这比单纯按项目比较更有解释力：问题高度集中在文字处理链，而不是均匀分布在两本书或简单清晰度增强上。后续分析必须至少按 `preprocess/typeset`、有字/无字、页面复杂度、处理时期分层，不能把页面难度差异误写成 Agent 责任差异。

终审 issue code 的边际勾选次数（同页可多选）：

| issue code | 页面数 |
|---|---:|
| `mask` | 132 |
| `typesetting` | 131 |
| `ai_inpaint` | 125 |
| `translation` | 123 |
| `preprocess` | 119 |
| `missing_text` | 110 |

96 页同时勾选了上述六项。这里的数字是用户在终审页选择的标签，不等于已经完成逐阶段根因诊断；未来 Agent 必须找出每页“第一个失败门”和由它引起的下游失败。

### 3.1 冻结快照与源项目当前状态

- 199 个 final item 都可以通过 `(source_project_id, source_image_id)` 唯一连回源 DB。
- 源身份分布为 130 + 69，无缺链、无重复 source identity。
- 198/199 个冻结终审 checksum 与源项目当前 accepted final stage checksum 一致。
- 唯一 stale 项是终审位置 131：
  - final item：`6ae5a23c-bc53-4c6a-9c4b-74eb15ae85bc`
  - source image：`b6f5dc2e-a7e7-4b9b-bcda-fcec7a0adc03`
  - source relative path：`IMG_3895.jpg`
  - frozen variant：`typeset`
  - 冻结图仍有原 checksum，但源 typeset stage review 已因后续修改/反馈失效。

位置 131 必须标记 `source_final_stale=true`。冻结图仍可作为失败证据，但绝不能被当成当前 accepted source artifact。

## 4. Codex / Cursor 溯源结论与归因规则

### 4.1 可以证明的事实

- Git 提交 `182a533`、`d60bae1`、`d9eb053` 含 `Co-authored-by: Cursor <cursoragent@cursor.com>`，可以证明 Cursor 参与了早期 evaluator/流程代码和相关真实数据流程修订。
- sidebar 1–27 的历史记录提交含 Cursor trailer，可以证明这些位置存在 Cursor 参与的早期轮次证据。
- 后续历史账本记录了 Native Goal final corpus 完成 199/199，也记录了用户授权 Codex 通过 UI 做逐页 classical fallback 比较与点击。
- 当前源 DB 的 jobs、revisions、images、regions 和最终审核 DB 都没有 `actor`、`client`、`task/thread/session` 字段。
- sidebar 1–27 中绝大多数源 image 后来又在 2026-08-23 更新，早期轮次贡献不能直接等同于最终冻结 PNG 的作者。

仅作描述性观察，不能用于判责：

| 终审位置段 | 总数 | approved | issues |
|---|---:|---:|---:|
| 1–27（有 Cursor 早期轮次记录） | 27 | 12 | 15 |
| 28–58 | 31 | 15 | 16 |
| 59–130 | 72 | 9 | 63 |
| 131–199 | 69 | 5 | 64 |

后两个区段的通过率明显更低，但页面难度、文字页比例、批量执行方式和用户勾选方式都是混杂变量。它只能提示“后续批量阶段需要重点审计”，不能证明是 Codex 或 Cursor 造成。

### 4.2 不得声称的内容

现有证据不能可靠回答：

- 任意一张最终冻结 PNG 的最终操作者到底是 Codex 还是 Cursor。
- 某个具体框、OCR、mask、补图、译文或嵌字错误由哪个 Agent 引入。
- `round8-clean-plate-final` 或 `rd-r04-manga02-slice` 项目名代表某个 Agent。
- Git author/trailer 能证明 Git-ignored 的项目图片由同一工具生成。

### 4.3 归因分类

逐页 `actor_attribution` 只能取：

- `codex-confirmed`：精确 source image ID、artifact checksum、时间和 Codex task/session 记录四项吻合。
- `cursor-confirmed`：精确 source image ID、artifact checksum、时间和 Cursor task/chat/checkpoint 记录四项吻合。
- `mixed-confirmed`：能够证明两个工具先后改变了同一 lineage，并能给出各阶段证据。
- `unknown`：缺任意关键锚点，或只有 Git trailer、时间接近、命名风格、页面位置等弱证据。

历史审计不得把 `unknown` 强行分摊给某一方。报告应分别列出“可证明事实”“高风险相关性”“无法判断”。

### 4.4 新一轮必须补齐的 provenance

每次新 mutation 至少记录：

- `actorKind`: `codex | cursor | human | system | unknown`
- `taskId/threadId/sessionId`
- `operationSource`: `ui | api | script`
- `runId`、`pageGenerationId`
- `sourceProjectId`、`sourceImageId`、`sourceChecksum`
- `inputArtifactChecksum`、`outputArtifactChecksum`、`parentArtifactChecksum`
- `stage`、`provider`、`model/version`、参数文档 hash
- `jobId`、`revisionId`、开始/结束时间
- 人工/Agent 的 decision、reason、candidate acceptance/rejection
- 对应 Git commit；没有 commit 时显式为 `null`

如果现有数据模型不能保存这些字段，先在项目工具中加入可持久化 lineage/audit 支持及回归测试，再开始批量重做。

## 5. 已核实的质量问题

### 5.1 “联系我们”不是单一路径问题

两个源项目当前共发现精确译文 `联系我们`：

- 35 个 region。
- 分布在 27 页。
- 27 页全部是终审 `issues`，approved 中为 0。
- provider 组合：
  - `tesseract -> manual`：22 个 region。
  - `tesseract -> argos-ja-zh`：8 个 region。
  - `manual -> manual`：3 个 region。
  - `manual -> argos-ja-zh`：2 个 region。

因此不能简单归结为 Argos 单点故障。Argos 会把短句/噪声退化成模板短语；同时，标为 `manual` 的内容也可能只是 Agent/操作者覆写后未进行语义核对。`translation_provider=manual` 不是通过证据。

后续翻译质量门必须硬失败以下情况：

- `联系我们`、`联系人`、免责声明、客服/系统提示、模型话术等与漫画语境无关的模板文本。
- 无字页、空 source、乱码或单一噪声字符产生译文。
- 多个不相邻 source 重复得到同一通用短语。
- 目标中文比例、语义、语气或角色上下文明显不符。
- `manual` 结果没有原文对照和 reviewer decision。

### 5.2 代表性真实页面

这些页面是后续校准种子，不是允许只修样本页：

| 位置 / 文件 | 代表问题 | 必须核对的证据 |
|---|---|---|
| #4 `IMG_3979.jpg` | 主竖排和假名注音被拆碎，译文乱码，补图污迹 | 原图、regions、ruby 归属、mask、inpaint、typeset |
| #97 `IMG_4086.jpg` | 原图无应翻文字，却一路误识别并嵌入中文 | 原图/增强双看、text-presence gate、假阳性 region |
| #27 `IMG_4007.jpg` | mask 漏掉日文标点和边缘，原文残留 | 完整段落框、mask overlay、clean plate |
| #60 `IMG_4047.jpg` | 速度线/网点背景补图失败，拟声词误翻，排版混乱 | background class、复杂补图、translation QC、art text |
| #47 `IMG_4030.jpg` | 艺术字被当普通系统字体，画面结构被破坏 | keep/ignore/redraw 决策、art-lettering 路径 |
| #33 `IMG_4016.jpg` | 普通超分只放大/锐化，细线和网点仍不足 | original/upscaled 对比、进一步 AI reconstruction 决策 |
| #13 `IMG_3989.jpg` | 用户已通过的简单白气泡短句 | 只读质量基准，不修改、不作为复杂页面通用参数 |

明确反馈为“无字却嵌字”的同类页还包括 #78、#124、#155、#182、#199。执行 Agent 必须从全量 ledger 重新发现所有同类项，不能只依赖这几个编号。

### 5.3 当前流程和实现的主要断点

1. 历史 Prompt 的阶段顺序自相矛盾：一处写 `detect -> OCR -> inpaint -> translate -> typeset`，另一处写 `detect -> OCR -> confirm -> translate -> inpaint -> typeset`。
2. 后端强制 translation 前必须有 current accepted inpaint；前端批处理排序却是 `preprocess -> detect -> ocr -> translate -> inpaint -> typeset`。选择 translate+inpaint 时会让 translation 先撞后端门禁。
3. 所有页基础超分不是当前强制规则；已有 preprocess suggestion 只是非绑定提示。
4. “基础超分后是否仍需生成式重绘/复原”没有独立持久化判定和质量门。
5. 有/无文字有部分人工门禁，但没有独立的 page-level `textPresence` 证据对象；线稿/人物假阳性仍可能一路进入下游。
6. 现有框合并主要是几何启发式，不能保证一个语义段落完整覆盖，普通 PP-OCR 路径仍可能碎框或漏边。
7. 横排/竖排基础能力存在；`ruby` 类型字段也存在，但没有 furigana 与主文字的父子关系、自动忽略策略和回归门禁。
8. 没有 white-solid、black-solid、simple-tone、complex-lineart、illustration 等背景分类字段，也没有由背景类别驱动修复路线的硬门。
9. OCR 有 provider/attempt/input variant/trust 记录，但没有日文合法性、无字页异常、ruby、模板污染或广告/系统话术 QC。
10. 翻译有上下文、术语和人工确认基础，但没有模板短语、目标语言、重复/空洞输出和源文一致性 QC。
11. mask 有 polygon、padding/dilation/feather、画笔/橡皮、checksum 和 mask 外像素保护，但没有“完整包住正文、标点、描边、阴影、抗锯齿且不误伤画面”的独立验收字段。
12. solid、Telea/NS、screentone、LaMa 和 AI candidate 均存在，但没有按背景类别选择路线；复杂背景失败仍可能进入下游。
13. 普通 Pillow 排版、横竖方向、auto-fit、overflow 基础能力存在，但没有气泡检测、原文字号/字体/颜色/视觉重量匹配。
14. 艺术字/SFX 的绘图式重建当前明确缺失；把 `sound_effect` 类型交给普通系统字体不等于实现艺术字。
15. 最终审核页能 frozen preview、repair handoff、refresh、CAS/history 和 approved-only export，但当前无法同页证据化查看 immutable original、quality plate、mask、clean plate 和 final，也缺少更细的 OCR/ruby/background/art-text 问题类别。

在大规模重做前，执行 Agent 必须统一权威顺序并补齐阻断性门禁及测试。不能只靠改 Prompt 绕过前后端顺序冲突。

## 6. 唯一允许的逐页状态机

199 张首先都要完成只读溯源；2026-08-25 历史基线的 158 张初始问题页随后各自进入重做闭环。任何阶段的“job completed”只说明程序运行结束，不代表质量通过。只有显式 accepted gate 及其 checksum 才能进入下一阶段。

### G0：全量只读溯源、身份和问题页新 lineage

输入：任意 final-review item。

必须完成：

- 读取 position、final item ID、source project/image ID、relative path、variant、verdict、issues、feedback 和 frozen checksum。
- 在源 DB 中唯一找到 image；验证项目持有 source 的 checksum 与 image 记录一致。
- 对该页的 immutable original、所有可用 preprocessed/mask/inpainted/typeset、frozen final、stage review、job、revision、provider、参数和 Codex/Cursor 证据逐项登记；缺失项显式记录。
- 对 approved/issues 使用同一字段做分层比较：处理顺序、产物齐全度、页面难度、active region、provider、人工门禁和第一个异常点。不得只比较最终通过率。
- approved 页到此进入只读终态并按导出规则交付；不建立重做 generation，不改变任何项目或审核状态。
- 仅对 issues 页保存旧 frozen final 和中间产物为只读负面参照。
- `restartFromSource=true` 只在业主反馈指向 source/preprocess 身份，或当前 accepted quality/mask 实际不可用时建立新 lineage。业主打回或视觉失败默认门禁局部：G8 失败保留 G0–G7；G10 失败保留 accepted G8；漏框/漏标点只回 G4/G7。不得因为「又是 issues」就从 G0 重跑已 accepted 的超分、OCR、mask 或净版。
- 若必须新建 lineage，第一个像素输入只能是项目持有的 immutable source。

失败条件：缺源、checksum 不符、身份多匹配、已有未知写入者、全量只读证据清单不完整，或无法证明 issues 的新输入不是旧结果。approved 的历史缺失证据作为审计发现记录，但不得擅自重做或推翻用户 verdict。

### G1：所有问题页的基础清晰度增强

用户要求所有图片都先做基础超分/清晰度增强。对历史基线的 158 个初始问题页：

- 在原图上运行已登记的 baseline upscale，而不是在旧增强图上重复放大。
- 同屏比较 original 与 baseline result，检查人物轮廓、网点、细线、文字边缘和灰度层次。
- 检查过锐、断线、摩尔纹、光晕、结构改变和尺寸虚增。
- 保存 provider/model、scale、完整参数、输入输出 checksum 和视觉结论。

只有 `baseline_upscale_accepted=true` 才能进入 G2。历史基线的 41 张初始 approved 不重跑；其历史 baseline/source/stage evidence 在 G0 全量只读核对，已有导出只按第 11.1 节验证。

### G2：是否需要进一步 AI 重绘/复原

基础超分与生成式重绘是两个不同阶段：

- 如果基础超分已保留并清楚呈现原结构，记录 `further_reconstruction=no`。
- 如果细线、网点、烟雾、人物细节或原本模糊结构仍不满足质量要求，记录原因并进入生成式 reconstruction/redraw candidate 路径。
- 普通插值、锐化或 Real-ESRGAN 放大不能冒充“进一步 AI 重绘”。
- 生成候选必须与 original 和 baseline 同屏比较，拒绝改变人物身份、表情、构图、文字内容或新增物体的候选。
- 只有显式接受的 quality plate checksum 才能作为之后的检测输入。

需要远端模型上传真实图时，先停在权限边界并请求用户明确授权；不得默认为已授权。

### G3：页面级有字/无字判断

在 accepted quality plate 上先判断是否真的存在需要翻译/处理的文字：

- 同时查看 original 和 quality plate；detector/OCR 只作为证据，不能替代视觉判断。
- 区分对话、旁白、标题、说明、技能名、拟声词、艺术字、装饰性文字、品牌/环境文字和纯线稿纹理。
- 记录 `yes | no | uncertain`、判断理由和证据。
- `no` 必须经过显式人工/Agent visual confirmation；不得因为 OCR 空就自动无字，也不得因为 detector 有框就自动有字。
- 无字页立即停止 OCR 之后的文字链，确认没有 active region、mask、inpaint 或 typeset 叠加，输出 accepted quality plate 作为 no-text final。

无字页出现任何译文、中文叠字或非零文字 mask 都是硬失败。

### G4：段落级区域、方向和语义处置

仅对 G3=`yes` 的页面：

- 框选语义完整的一段文字，而不是逐字符碎框。
- 矩形/多边形必须完整包住正文、标点、描边和应处理的附属部分，同时不越界伤及人物或气泡边线。
- 明确横排/竖排、reading order、region type、paragraph group。
- 日文 furigana/ruby 必须关联主文字并在 OCR/翻译输入中忽略，不能作为独立译文。
- 每个检测候选都要得到 `translate | ignore | keep-art | redraw-art | false-positive` 的处置。
- 非必要环境字/装饰字可以在有依据时保留或忽略；不得因为“识别到了”就强制翻译。
- 艺术字/SFX 必须先决定保留、忽略或绘图式重建，不能直接落到普通文本排版。

所有候选都有明确处置且语义框完整后，G4 才可接受。

### G5：逐区域背景分类

每个需要移除/替换文字的 region 必须先分类：

- `white-solid`
- `black-solid`
- `other-solid`
- `simple-gradient`
- `screentone`
- `complex-lineart`
- `illustration/character`

记录 confidence、视觉依据和 reviewer。背景类别用于 G7 mask 与 G8 云端提示、结构保真及视觉验收，不再用来选择本地 clean-plate 算法。

### G6：OCR、原文核对和信任门

- 在 original crop 与 quality crop 上保留 OCR attempt 证据。
- 逐字核对日文、标点、方向和 reading order。
- ruby 不单独进入翻译；噪声、线条、人物细节和空白框标记 false-positive/ignored。
- 检查日文字符比例、空/乱码、重复片段、模板污染和无字页异常。
- OCR confidence 只提供证据，不能自动授予 trust。
- 每个 translatable region 必须有人/Agent显式确认可信 source text。

没有可信 source text 就不得进入 mask、翻译或嵌字。

### G7：完整 mask 验收

- mask 必须覆盖应删除的正文、标点、描边、阴影、注音（若它属于需要一起清除的原文视觉）和抗锯齿边缘。
- mask 不能误伤气泡边线、人物、速度线、网点或邻近画面。
- 同屏检查 mask-on、mask-off、original crop 和放大图。
- 保存实际 mask checksum；不能只保存参数或预览。
- 自动 mask 不完整时，允许执行 Agent 在项目 UI 使用画笔/橡皮修正；这属于 UI 操作，不等于让用户纯手工处理。

完整性或边界任一不通过，都回到 G4/G7，禁止用后续中文遮住残留日文。

### G8：直接使用原生云端模型生成并验收 clean plate

- 按 2026-09-03 用户明确决策，所有新 G8 候选直接走执行 Agent 的原生云端图片模型；当前 Codex 使用 built-in `image_gen`。不再生成、尝试、对比或评审 LaMa、本地 AI、纯色填充、Telea/NS 或其他 classical clean-plate 候选，也不要求先失败一次本地路线。
- 先跑通云端流程，成本优化以后再做。使用已授予的必要页图上传范围，不逐页重复询问；这不授权另购额度、付费 API、账号或全局配置变更。
- 背景分类用于提示与验收：纯色/渐变连续，网点频率与相位连续，线稿/人物结构保真。云端失败保留真实 blocker，不静默回退本地方法。
- G1 基础增强、检测、OCR、mask 计算、几何规范化、mask 内外严格合成及排版仍可本地执行；这些操作不能绕回本地净版生成。
- 每个云端候选保留真实调用、raw、provider/model（未知则如实记录）、参数、parent/candidate checksum、视觉结论和拒绝原因。
- mask 外像素变化必须为 0；净版必须残字不可读、无白洞/灰块/模糊带/重复纹理且背景连续。云端调用成功不等于质量通过。
- 无需净版的页面仍须精确、无产物的 N/A；只有 accepted clean plate 或合法 N/A 才能进入后续阶段。
- 历史本地候选、审查、参数、模型和成果原样保留，仅供按需读取与 replay；不重新生成、重新评审或批量查看。该规则替代旧 classical fallback 授权，不删除历史证据。

### G9：翻译与语义质量门

- 翻译输入是完整语义段落、可信 source text、正确 reading order，并排除 ruby。
- 使用漫画上下文、角色、语气、拟声词/技能名类型和相邻 region context。
- 所有模型输出都执行目标中文、禁用模板、重复/空洞、源文一致性和上下文一致性 QC。
- `联系我们`、`联系人`、客服/免责声明/模型话术等直接硬失败并回查 OCR 与翻译两端。
- `manual`、Agent覆写或字典结果仍需原文对照；provider 名称不构成接受证据。
- 拟声词、语气词和短句不能机械套通用句；不能把无意义 OCR 噪声翻成流畅中文。
- 保存初始候选、修订结果、reviewer 和接受理由。

译文未显式确认，不得进入 G10。

### G10：气泡文字、普通非气泡文字和艺术字分流

- 气泡文字：对照原文的字面面积、字号、粗细、颜色、描边、方向、行数、对齐和留白；中文必须完整位于气泡内。
- 普通非气泡文字：保持原位置层级和阅读关系，不能遮挡人物或关键画面。
- 非必要装饰字：可根据 G4 的处置保留/忽略。
- 艺术字/SFX：使用专门的 art-lettering/绘图式路线，匹配轮廓、描边、倾斜、形变、视觉重心和与构图的关系；普通系统字体不算完成。
- 艺术字的优先顺序是：非必要时保留原文或明确忽略；必须本地化时先在局部独立图层使用合适的中文展示字形、轮廓/描边、仿射或曲线形变重建；仍无法匹配时才生成受原构图约束的局部 AI lettering 候选。不得用整页生成式重绘来换一个拟声字。
- 如果项目工具尚无艺术字路线，先实现项目内能力与测试，或保留原艺术字并取得明确决策；不得用外部一次性编辑绕开项目 lineage。
- `overflow=0` 只是必要条件，不是视觉通过。必须和 original、clean plate、final 同屏比较。

### G11：项目内最终审核和导出

- 在最终审核模块中提供/调用同页 original、quality plate、mask、clean plate、final 的对比证据。
- 新 accepted final 生成后，显式 refresh 对应 frozen snapshot；refresh 必须写 history 并把该项重置为 `pending`。
- 用户在最终审核页重新判定。执行 Agent不得与用户并发保存、自动覆盖或偷偷改 verdict。
- `approved` 才能进入 approved-only export；`issues` 携带新反馈回到 G0，开始下一 generation。
- 导出前再次验证 frozen checksum、batch revision 和目标新目录；导出 manifest 不包含 OCR 文本或审核反馈正文。

## 7. 处理真实问题页前必须现场核验的工具能力

以下是能力验收清单，不是每轮重做的固定 backlog。执行 Agent 先依据当前代码、测试和最小真实校准证据逐项核验：已通过的能力只登记证据并继续，不重复实现；未通过且实际阻断当前 checkpoint 的项才进入任务范围。

1. 统一前后端权威阶段顺序为：`preprocess -> detect -> OCR + trust -> mask/inpaint + clean-plate accept -> translate + translation accept -> typeset + typeset accept -> final review`。
2. 修正前端批处理顺序与后端 translation-before-inpaint 门禁冲突，并增加回归测试。
3. 增加持久化 page generation/run lineage 和 actor/session provenance；旧记录缺失时保持 `unknown`。
4. 增加 page-level text-presence gate，确保 no-text 不会产生下游文字产物。
5. 增加 paragraph grouping、ruby parent/ignore、region disposition 和背景分类的可持久化字段或等价结构。
6. 增加 OCR/translation QC，至少覆盖无字误报、ruby、空/乱码、`联系我们` 等模板污染和 `manual` 二次验证。
7. 增加 mask 完整性、背景路由和 clean-plate acceptance 的结构化证据。
8. 为气泡参照排版和艺术字/SFX 提供项目内分流；艺术字能力不能假装由普通 Pillow 排版满足。
9. 扩展最终审核页面，使原图和各阶段证据可在项目工具内联动查看；保留 repair handoff、refresh、CAS/history 和 approved-only export。
10. 为上述门禁增加合成回归和代表性真实页面只读/视觉验证。真实终审 verdict 不能作为自动化测试写入目标。

实现时优先使用现有数据模型和 UI 交互，不建立与项目工具竞争的第二套审核系统。

## 8. `STATE.md` 驱动的多轮迭代协议

本 Goal 不维护一套与 `STATE.md` 并行的固定 Round 0–6，也不在每轮重放启动流程。历史轮次只是证据；当前 `checkpoint` 和 `next action` 只存于 `.agent/STATE.md`。

### 首次激活或协议变化

- 确认用户当前消息已显式激活 Goal；未激活时保持 waiting，不产生执行效果。
- 只读核对唯一 writer、live process/agent、句柄、Git operation、dirty worktree、受治理存储健康、三个 SQLite、manifest/checksum、revision/counts、source mapping 和用户最新 verdict。
- 将现场事实、已加载 protocol revision、当前 checkpoint、证据位置和精确 next action 写入 `STATE.md`。不把本文档、完整历史或大段日志复制进 STATE。

### 普通续轮

1. 只读 `AGENTS.md`、`STATE.md` 和 next action 的直接证据。只有 loaded revision 不匹配或必要协议已丢失时，才按第 0 节重新选读。
2. 选择一个能产生可验证证据的最小闭环。在该 checkpoint 内连续处理所有可安全推进的独立页或 cohort，不默认每页等一次用户 verdict。
3. 单页遇到可复现 blocker 时，登记证据、假设与恢复条件，然后继续其他独立工作。连续两次同质无进展后禁止原样重试，必须先诊断并改变方法。
4. 只在 material change 时更新 `STATE.md`：保留当前计数、checkpoint、blocker/证据、loaded revision 和精确 next action。计划、首个失败、单页完成或进度汇报不是停止条件；仍有可安全推进的独立工作就继续。

### 统一人工终审 checkpoint

- 当当前批次的可执行 `issues` 都已完成新 lineage 并 refresh 为 `pending`，或留下可复现的真实 blocker 时，汇总现场计数、待审位置、blocker 和 next action，然后结束当前执行轮次。
- 此时 Goal 保持 active，等待用户一次性批量 verdict；不后台轮询、空转或重复生成，也不标记 complete/blocked。
- 收到用户 verdict 后重查现场 DB 与 revision，把退回项设为新 checkpoint，再按普通续轮协议继续。

### 终态

只有第 11.3 节的全部完成条件与第 2.6 节的受治理新目录导出校验都成立，才将 Goal 标记 complete。用户发出 pause、stop 或 cancel 时，立即停止新调度与效果，安全持久化当前 checkpoint 后回应。

## 9. 每页 ledger 的最低字段

机器可读 ledger 每页一条、追加式更新；不要把大段 OCR/译文写入公开文档。

```json
{
  "position": 0,
  "finalItemId": "",
  "sourceProjectId": "",
  "sourceImageId": "",
  "sourceRelativePath": "",
  "immutableSourceChecksum": "",
  "frozenFinalChecksum": "",
  "sourceFinalChecksum": null,
  "sourceFinalStale": false,
  "originalVerdict": "issues",
  "originalIssueCodes": [],
  "originalFeedbackPresent": false,
  "actorAttribution": "unknown",
  "attributionEvidence": [],
  "reworkRequired": true,
  "runId": "",
  "pageGenerationId": "",
  "restartFromSource": true,
  "parameterSetId": "",
  "gates": {
    "G0_identity": {"state": "pending", "inputChecksum": "", "outputChecksum": "", "evidence": []},
    "G1_baselineUpscale": {"state": "pending", "inputChecksum": "", "outputChecksum": "", "decision": ""},
    "G2_reconstruction": {"state": "pending", "decision": "", "reason": "", "candidateChecksums": []},
    "G3_textPresence": {"state": "pending", "decision": "uncertain", "evidence": []},
    "G4_regions": {"state": "pending", "regionDecisions": []},
    "G5_background": {"state": "pending", "regionClasses": []},
    "G6_ocr": {"state": "pending", "trustedRegionIds": [], "qcFlags": []},
    "G7_mask": {"state": "pending", "maskChecksum": "", "coverageReviewed": false},
    "G8_cleanPlate": {"state": "pending", "route": "", "outputChecksum": "", "outsideMaskChangeCount": null},
    "G9_translation": {"state": "pending", "confirmedRegionIds": [], "qcFlags": []},
    "G10_typeset": {"state": "pending", "routeCounts": {}, "overflowCount": null, "outputChecksum": ""},
    "G11_finalReview": {"state": "pending", "refreshedRevision": null, "userVerdict": "pending", "exportPath": null}
  },
  "firstFailedGate": null,
  "derivedFailures": [],
  "nextAction": "",
  "updatedAt": ""
}
```

上例是 issues 页。approved 页必须写 `reworkRequired=false`、`runId=null`、`pageGenerationId=null`、`restartFromSource=false`；G0 保存完整只读证据，其余新处理 gate 为 `not-applicable`，历史 stage 证据留在 provenance 部分。每个 gate state 只能取 `pending | accepted | rejected | blocked | not-applicable`。`accepted` 必须绑定证据和 checksum；不能只写自然语言“看起来没问题”。

## 10. 参数决策登记

Goal Prompt 不携带这些参数。执行 Agent 只在当前 checkpoint 需要时，依据当前代码、provider health、代表性原图和用户反馈现场决定，并写入 `resolved-parameters.md/json`。任何参数改变都创建新的 `parameterSetId`，旧页面不悄悄继承新参数。

至少登记：

| 参数组 | 必须决定的内容 | 决策证据 |
|---|---|---|
| 基础增强 | provider/model/version、scale、去噪/锐化/对比度、适用输入 | original/result 对比、线条/网点评估 |
| 进一步重绘 | local/remote provider、模型、seed、强度、候选数、结构保护 | 代表性候选与拒绝原因；远端权限 |
| 检测 | provider、输入 variant、合并/外扩规则、最小区域 | ruby/碎框/无字负样本 |
| OCR | provider/language/direction、attempt variant、trust/QC 规则 | 日文逐字核对、无字负样本 |
| 背景分类 | 类别定义、confidence、是否必须人工复核 | 白/黑/网点/复杂背景样本 |
| mask | mode、padding/dilation/feather、polarity、人工修正规则 | glyph coverage、边界、mask 外像素 |
| clean plate | 原生云端 route、背景保真提示、candidate 参数；无本地 fallback | 背景连续性、残字、结构保持 |
| 翻译 | provider/model/prompt、context、术语、禁用模板、二次 QC | 源文/译文对照和漫画语境 |
| 普通排版 | font、字号估计、方向、行距/字距、描边、auto-fit | 原文视觉面积、气泡留白、overflow |
| 艺术字 | keep/ignore/redraw 路由、绘图/生成方法、视觉验收 | #47/#60 等艺术字样本 |
| 批量执行 | cohort、并发数、连续通过后扩容条件 | 工具稳定性和用户终审结果 |
| 导出 | batch revision、approved count、目标新目录、collision policy | DB checkpoint、目标不存在、manifest |

参数选择不得只依据平均指标；必须保留失败样本和用户终审反馈作为反例。

## 11. 验收标准和完成定义

### 11.1 历史 cohort 与已有 `approved` 保护

- 当前 `approved` 计数、batch revision 和已有导出只从现场审核库、manifest 与 checksum 证据确定，不从 2026-08-25 历史数字推断。
- 历史初始 `approved` cohort 完成同字段只读 provenance：immutable original、可用中间产物、frozen final、stage/job/revision/provider、缺失项、checksum 和 actor evidence；不重处理、不 refresh、不改 verdict。
- 已有早期 immutable export 只在目录名、manifest、revision/count、文件集和 checksum 全部一致时登记为有效证据。不因本规范的历史记载重复创建、删除或覆盖它。

### 11.2 每个重做页

- 身份和 immutable source checksum 已确认。
- `restartFromSource=true`，新 lineage 没有使用旧失败成品作为输入。
- G0–G11 顺序完整；不适用 gate 有明确原因。
- 无字页没有文字下游产物。
- 有字页所有候选都有处置，ruby 不单独翻译。
- mask 完整且不越界，clean plate 无残字和结构性伪影。
- 翻译没有模板污染，manual 也有原文复核。
- 普通排版与原文视觉重量相符；艺术字走专门分支或有明确保留/忽略决策。
- 新 final 在项目内 refresh，用户重新终审。

### 11.3 总任务完成

- 历史初始 `approved` cohort 已完成只读溯源，任何有效的早期 immutable export 已校验并保留。
- 全部 199 张都完成同字段、可复现的原图—中间产物—冻结成品—数据库链路审计，且 approved/issues 的流程差异已分层报告。
- 158 张初始 issues 都有从原图开始的新 lineage 和全阶段 ledger。
- 最终审核库为 199 approved、0 issues、0 pending。
- 按第 2.6 节从受治理 `roots.artifacts` 现场派生不存在的新 `approved-199` 目录，通过项目内置 final-review export 原子导出并逐文件校验。
- provenance 报告诚实区分 confirmed/mixed/unknown，不对 Codex/Cursor 无证据判责。
- 所有新增项目工具能力有相称的自动化测试和真实样本视觉证据。
- 服务状态、输出路径、参数集、剩余风险和用户终审结果已记录。
- 未经用户明确授权，不提交、不 push、不发布真实数据或项目产物。

如果用户尚未完成某一轮终审，Goal 保持进行中，并把最终审核页、待审位置和精确下一动作交给用户；不得用 Agent 自评代替用户 verdict，也不得把“程序跑完”报告为总任务完成。

## 12. 相关仓库文档和代码入口

项目 Goal 显式激活后，始终只读 `AGENTS.md` 与 `.agent/STATE.md`；其余入口只在当前 checkpoint 直接需要时选读：

- 具体问题族或恢复证据：`.agent/PAGE_PROBLEM_REPORT.md`。
- 历史 pipeline 证据：`docs/real-data-iteration-status.md` 和必要的 Git 历史。已删除的旧入口只作证据，不产生 authority。
- 数据、lineage、stage review、refresh 或 export 语义：`docs/data-model.md`、`docs/architecture.md` 和 `README.md`。
- 存储 runtime/model 路由与命令：`docs/development.md`、`scripts/external-uv.mjs`、`scripts/external-frontend.mjs` 和 `scripts/storage-*-route.mjs`。
- 后端阶段、最终审核或页面修复：`backend/src/manga_localizer/queue.py`、`backend/src/manga_localizer/services/final_reviews.py` 以及当前动作直接调用的 service/provider。
- 前端批处理或人工终审：`frontend/src/store/workbench.ts` 或 `frontend/src/finalReview/` 中当前动作直接涉及的文件。

本文档中的 2026-08-25 数字只是历史基线。执行 Agent 启动或恢复时必须以现场数据库覆盖当前快照；只把新快照写入 `STATE.md` 或 Git-ignored 项目进度证据，不悄改历史 cohort。
