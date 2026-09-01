import { useEffect, useState } from 'react';

import { api } from '../api/client';
import { EmptyState, LoadingState } from '../components/Primitives';
import { useWorkbenchStore } from '../store/workbench';
import {
  filteredFinalReviewItems,
  finalReviewDraftDirty,
  finalReviewExportReady,
  finalReviewLegacyApproved,
  finalReviewLegacyReviewed,
  finalReviewValidationError,
  useFinalReviewStore,
} from './store';
import {
  FINAL_REVIEW_EVIDENCE_KINDS,
  FINAL_REVIEW_ISSUES,
  finalReviewIssueLabel,
  type FinalReviewIssueCode,
  type FinalReviewEvidenceKind,
  type FinalReviewVerdict,
} from './types';

interface FinalReviewPageProps {
  onOpenWorkbench: () => void;
}

const VERDICT_LABELS: Record<FinalReviewVerdict, string> = {
  pending: '未审核',
  approved: '完全没问题',
  issues: '有问题',
};

const EVIDENCE_LABELS: Record<FinalReviewEvidenceKind, string> = {
  original: '原图', quality: '质量板', mask: '蒙版', clean: '净图', final: '成品',
};

function itemName(relativePath: string): string {
  return relativePath.split('/').filter(Boolean).at(-1) ?? relativePath;
}

function itemStatusClass(verdict: FinalReviewVerdict): string {
  return verdict === 'approved' ? 'is-approved' : verdict === 'issues' ? 'has-issues' : 'is-pending';
}

export function FinalReviewPage({ onOpenWorkbench }: FinalReviewPageProps) {
  const state = useFinalReviewStore();
  const [outputPath, setOutputPath] = useState('');
  const [preserveTree, setPreserveTree] = useState(true);
  const [mobilePane, setMobilePane] = useState<'list' | 'preview' | 'review'>('preview');
  const [repairError, setRepairError] = useState('');
  const [evidenceKind, setEvidenceKind] = useState<FinalReviewEvidenceKind>('final');
  const filtered = filteredFinalReviewItems(state);
  const active = state.items.find((item) => item.id === state.activeItemId) ?? null;
  const dirty = finalReviewDraftDirty(state);
  const validation = finalReviewValidationError(state.draft);
  const locked = state.operation !== null;
  const interactionLocked = locked || state.conflict;
  const legacyApproved = finalReviewLegacyApproved(active);
  const legacyReviewed = finalReviewLegacyReviewed(active);
  const evidence = active?.evidence[evidenceKind] ?? null;
  const exportReady = finalReviewExportReady(state);

  useEffect(() => {
    if (!useFinalReviewStore.getState().batch) void useFinalReviewStore.getState().loadBatches();
  }, []);

  useEffect(() => {
    function beforeUnload(event: BeforeUnloadEvent) {
      if (!finalReviewDraftDirty(useFinalReviewStore.getState())) return;
      event.preventDefault();
    }
    function keydown(event: KeyboardEvent) {
      const target = event.target;
      if (target instanceof HTMLElement
        && target.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]')) return;
      if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
      event.preventDefault();
      if (useFinalReviewStore.getState().navigate(event.key === 'ArrowLeft' ? -1 : 1)) {
        setRepairError('');
      }
    }
    window.addEventListener('beforeunload', beforeUnload);
    window.addEventListener('keydown', keydown);
    return () => {
      window.removeEventListener('beforeunload', beforeUnload);
      window.removeEventListener('keydown', keydown);
    };
  }, []);

  async function openInWorkbench() {
    if (!active || !state.draft) return;
    setRepairError('');
    if (dirty) {
      setRepairError('请先显式保存终审反馈，再创建新的 G0 修复 lineage。');
      return;
    }
    const context = await useFinalReviewStore.getState().beginRepair();
    if (!context) return;
    const workbench = useWorkbenchStore.getState();
    try {
      // Repair creates a new isolated image inside the source project, so even an
      // already-open source project must be reloaded before selecting that image.
      if (!await workbench.selectProject(context.repairProjectId, true)) {
        useFinalReviewStore.getState().finishRepairNavigation(true);
        setRepairError(useWorkbenchStore.getState().globalError || '无法打开终审项的来源项目。');
        return;
      }
      if (!await workbench.selectImage(context.repairImageId)) {
        useFinalReviewStore.getState().finishRepairNavigation(true);
        setRepairError(useWorkbenchStore.getState().globalError || '无法打开终审项的来源图片。');
        return;
      }
    } catch (error) {
      useFinalReviewStore.getState().finishRepairNavigation(true);
      setRepairError(error instanceof Error ? error.message : '打开来源页时发生未知错误。');
      return;
    }
    useFinalReviewStore.getState().finishRepairNavigation();
    onOpenWorkbench();
  }

  const activeFilteredIndex = filtered.findIndex((item) => item.id === active?.id);
  const hasNextFilteredItem = activeFilteredIndex >= 0 && activeFilteredIndex < filtered.length - 1;

  async function saveAndNext() {
    const previousItemId = useFinalReviewStore.getState().activeItemId;
    if (!await useFinalReviewStore.getState().save(true)) return;
    if (useFinalReviewStore.getState().activeItemId === previousItemId) return;
    setRepairError('');
    setMobilePane('preview');
  }

  return (
    <main className="final-review" data-mobile-pane={mobilePane}>
      <nav className="final-review__mobile-panes" aria-label="终审分区">
        {(['list', 'preview', 'review'] as const).map((pane) => (
          <button
            aria-pressed={mobilePane === pane}
            className="button button--compact"
            key={pane}
            onClick={() => setMobilePane(pane)}
            type="button"
          >
            {pane === 'list' ? '全部成品' : pane === 'preview' ? '大图' : '审核与导出'}
          </button>
        ))}
      </nav>

      <section className="final-review__list panel" aria-label="终审成品列表">
        <header className="final-review__header">
          <div>
            <span className="section-kicker">FINAL REVIEW</span>
            <h1>最终验收</h1>
          </div>
          <select
            aria-label="选择终审批次"
            disabled={interactionLocked || state.batches.length === 0}
            onChange={(event) => {
              setRepairError('');
              void state.loadBatch(event.target.value);
            }}
            value={state.batch?.id ?? ''}
          >
            {state.batches.length === 0 ? <option value="">暂无批次</option> : null}
            {state.batches.map((batch) => (
              <option key={batch.id} value={batch.id}>{batch.name}（{batch.itemCount}）</option>
            ))}
          </select>
        </header>

        {state.batch ? (
          <div className="final-review__stats" aria-label="终审计数">
            <button className={state.statusFilter === 'all' ? 'is-active' : ''} onClick={() => state.setStatusFilter('all')} type="button"><b>{state.batch.itemCount}</b><span>全部</span></button>
            <button className={state.statusFilter === 'pending' ? 'is-active' : ''} onClick={() => state.setStatusFilter('pending')} type="button"><b>{state.batch.counts.pending}</b><span>未审</span></button>
            <button className={state.statusFilter === 'approved' ? 'is-active' : ''} onClick={() => state.setStatusFilter('approved')} type="button"><b>{state.batch.counts.approved}</b><span>没问题</span></button>
            <button className={state.statusFilter === 'issues' ? 'is-active' : ''} onClick={() => state.setStatusFilter('issues')} type="button"><b>{state.batch.counts.issues}</b><span>有问题</span></button>
          </div>
        ) : null}

        <div className="final-review__filters">
          <input
            aria-label="搜索终审成品"
            onChange={(event) => state.setSearch(event.target.value)}
            placeholder="搜索序号、文件或来源项目"
            type="search"
            value={state.search}
          />
          <select
            aria-label="按问题类别筛选"
            onChange={(event) => state.setIssueFilter(event.target.value as FinalReviewIssueCode | 'all')}
            value={state.issueFilter}
          >
            <option value="all">全部问题类别</option>
            {FINAL_REVIEW_ISSUES.map((issue) => <option key={issue.code} value={issue.code}>{issue.label}</option>)}
          </select>
        </div>

        {state.loading ? <div className="final-review__loading"><LoadingState label="正在加载终审批次…" /></div> : null}
        {!state.loading && !state.batch ? (
          <EmptyState icon="审" title="还没有终审批次" description="终审批次属于项目工具，会把多个来源项目的最终成品做成不可变快照。" />
        ) : null}
        {!state.loading && state.batch && filtered.length === 0 ? (
          <EmptyState icon="0" title="没有符合筛选条件的成品" description="调整审核状态、问题类别或搜索条件。" />
        ) : null}
        <div className="final-review__grid" role="list">
          {filtered.map((item) => (
            <button
              aria-label={`第 ${item.position} 张 ${itemName(item.sourceRelativePath)}，${VERDICT_LABELS[item.verdict]}`}
              aria-current={item.id === active?.id ? 'true' : undefined}
              className={`final-review-card ${itemStatusClass(item.verdict)}`}
              disabled={interactionLocked}
              key={item.id}
              onClick={() => {
                if (state.selectItem(item.id)) {
                  setRepairError('');
                  setMobilePane('preview');
                }
              }}
              role="listitem"
              type="button"
            >
              <span className="final-review-card__image">
                <img
                  alt={`第 ${item.position} 张 ${itemName(item.sourceRelativePath)}`}
                  loading="lazy"
                  src={api.finalReviewThumbnailUrl(item.id, item.artifactRevision)}
                />
                <b>#{item.position}</b>
              </span>
              <span className="final-review-card__text">
                <strong>{itemName(item.sourceRelativePath)}</strong>
                <small>{item.sourceProjectName} · {VERDICT_LABELS[item.verdict]}</small>
              </span>
            </button>
          ))}
        </div>
      </section>

      <section className="final-review__preview panel" aria-label="终审成品大图">
        {active ? (
          <>
            <header>
              <div>
                <span>全局 #{active.position} / {state.batch?.itemCount ?? state.items.length}</span>
                <strong>{itemName(active.sourceRelativePath)}</strong>
                <small>{active.sourceProjectName} · {active.sourceRelativePath}</small>
              </div>
              <div className="final-review__nav">
                <button aria-label="上一张终审成品" className="button button--compact" disabled={interactionLocked || activeFilteredIndex <= 0} onClick={() => { if (state.navigate(-1)) { setRepairError(''); setEvidenceKind('final'); } }} type="button">← 上一张</button>
                <button aria-label="下一张终审成品" className="button button--compact" disabled={interactionLocked || activeFilteredIndex < 0 || activeFilteredIndex >= filtered.length - 1} onClick={() => { if (state.navigate(1)) { setRepairError(''); setEvidenceKind('final'); } }} type="button">下一张 →</button>
              </div>
            </header>
            <div className="final-review__evidence-tabs" role="tablist" aria-label="冻结阶段证据">
              {FINAL_REVIEW_EVIDENCE_KINDS.map((kind) => (
                <button
                  aria-selected={evidenceKind === kind}
                  disabled={interactionLocked}
                  key={kind}
                  onClick={() => setEvidenceKind(kind)}
                  role="tab"
                  type="button"
                >{EVIDENCE_LABELS[kind]}<small>{active.evidence[kind].availability === 'available' ? '可用' : active.evidence[kind].availability === 'not-applicable' ? 'N/A' : '缺失'}</small></button>
              ))}
            </div>
            <div className="final-review__image-stage">
              {evidence?.availability === 'available' ? (
                <img
                  alt={`${EVIDENCE_LABELS[evidenceKind]}冻结证据：${itemName(active.sourceRelativePath)}`}
                  src={api.finalReviewArtifactUrl(active.id, evidenceKind, evidence.artifactRevision)}
                />
              ) : (
                <div className="final-review__evidence-empty" role="status">
                  <strong>{evidence?.availability === 'not-applicable' ? '此阶段不适用' : '历史证据不可用'}</strong>
                  <span>{evidence?.reason || (!active.strictEvidence ? '旧版终审批次未冻结此阶段；不会回退读取当前工作台产物。' : '服务端未提供可校验的冻结证据。')}</span>
                </div>
              )}
            </div>
            <dl className="final-review__evidence-provenance" aria-label="冻结证据 provenance">
              <div><dt>Revision</dt><dd>{evidence?.artifactRevision ?? active.artifactRevision}</dd></div>
              <div><dt>Grid</dt><dd>{evidence?.grid ? `${evidence.grid.width}×${evidence.grid.height}` : 'N/A'}</dd></div>
              <div><dt>Resolution</dt><dd>{evidence?.resolutionDigest || 'N/A'}</dd></div>
              <div><dt>Generation</dt><dd>{evidence?.generationId || 'unknown'}</dd></div>
              <div><dt>Producer</dt><dd>{evidence?.producerId || 'unknown'}</dd></div>
              <div><dt>Producer revision</dt><dd>{evidence?.producerRevisionId || 'unknown'}</dd></div>
              <div><dt>Terminal</dt><dd>{evidence?.terminalId || 'unknown'}</dd></div>
              <div><dt>Terminal revision</dt><dd>{evidence?.terminalRevisionId || 'unknown'}</dd></div>
              <div><dt>Checksum</dt><dd>{evidence?.checksum || 'unavailable'}</dd></div>
            </dl>
            <footer>
              <span className={`final-review__verdict-pill ${itemStatusClass(active.verdict)}`}>{VERDICT_LABELS[active.verdict]}</span>
              {active.currentArtifactStale ? <span className="final-review__stale">源项目已有新成品，可同步</span> : null}
              <small>artifact r{active.artifactRevision} · {evidence?.checksum ? `sha256 ${evidence.checksum.slice(0, 12)}…` : '无 checksum'} · {evidence?.generationId || 'legacy/unknown generation'}</small>
            </footer>
          </>
        ) : <EmptyState icon="图" title="选择一张成品开始终审" description="左侧会按全局序号展示合并批次中的全部最终成品。" />}
      </section>

      <aside className="final-review__review panel" aria-label="终审反馈与导出">
        {active && state.draft ? (
          <>
            <section className="final-review__form">
              <header><span className="section-kicker">VERDICT</span><h2>审核结论</h2></header>
              <div className="final-review__verdicts" role="radiogroup" aria-label="审核结论">
                {(['pending', 'approved', 'issues'] as const).map((verdict) => (
                  <label key={verdict}>
                    <input checked={state.draft?.verdict === verdict} disabled={interactionLocked || legacyReviewed} name="final-verdict" onChange={() => state.updateDraft({ verdict })} type="radio" />
                    <span>{VERDICT_LABELS[verdict]}</span>
                  </label>
                ))}
              </div>

              <fieldset disabled={interactionLocked || legacyReviewed || state.draft.verdict !== 'issues'}>
                <legend>问题类别（可多选）</legend>
                <div className="final-review__issues">
                  {FINAL_REVIEW_ISSUES.map((issue) => (
                    <label key={issue.code}>
                      <input checked={state.draft?.issueCodes.includes(issue.code)} onChange={() => state.toggleIssue(issue.code)} type="checkbox" />
                      <span>{issue.label}</span>
                    </label>
                  ))}
                </div>
              </fieldset>

              <label className="field-label" htmlFor="final-review-feedback">具体反馈</label>
              <textarea
                disabled={interactionLocked || legacyReviewed}
                id="final-review-feedback"
                onChange={(event) => state.updateDraft({ feedback: event.target.value })}
                placeholder="描述位置、表现和期望修复结果；选择“其他”时必填。"
                rows={5}
                value={state.draft.feedback}
              />
              {validation ? <p className="final-review__validation">{validation}</p> : null}
              {legacyReviewed ? <div className="final-review__legacy-lock" role="status">{legacyApproved ? '旧版已通过项保持只读；缺失阶段证据如实标记为 unavailable，不允许 refresh 或改写 verdict。' : '旧版问题项的既有 verdict 与反馈保持只读；可用这些反馈创建新的 G0 修复 lineage。'}</div> : null}
              {state.error ? <div className={`final-review__error ${state.conflict ? 'is-conflict' : ''}`} role="alert"><strong>{state.conflict ? '终审状态需要重新载入确认' : '操作失败'}</strong><span>{state.error}</span>{state.conflict ? <button className="text-button" disabled={locked} onClick={() => void state.reloadConflict()} type="button">载入最新版本并保留草稿</button> : null}</div> : null}
              {repairError ? <div className="final-review__error" role="alert"><strong>无法进入工作台修复</strong><span>{repairError}</span></div> : null}
              <div className="final-review__save-status" aria-live="polite">
                {state.operation ? `正在执行：${state.operation}` : dirty ? '有未保存的终审草稿' : state.lastSavedAt ? `已保存 ${new Date(state.lastSavedAt).toLocaleTimeString()}` : '当前标注已同步'}
              </div>
              <div className="final-review__actions">
                <button className="button" disabled={locked || legacyReviewed || state.conflict || Boolean(validation) || !dirty} onClick={() => void state.save()} type="button">显式保存</button>
                <button
                  className="button button--accent"
                  disabled={locked || legacyReviewed || state.conflict || Boolean(validation) || !hasNextFilteredItem}
                  onClick={() => void saveAndNext()}
                  type="button"
                >保存并下一张</button>
              </div>
              <div className="final-review__repair-actions">
                <button className="button" disabled={state.draft.verdict !== 'issues' || Boolean(validation) || interactionLocked || dirty} onClick={() => void openInWorkbench()} type="button">创建新 G0 并进入修复</button>
                <button
                  className="button"
                  disabled={interactionLocked || legacyApproved || dirty || !active.currentArtifactStale}
                  onClick={() => void state.refreshActive()}
                  title={dirty ? '请先保存或放弃当前草稿' : !active.currentArtifactStale ? '源项目暂无更新成品' : '同步源项目中的最新成品快照'}
                  type="button"
                >
                  {state.refreshing ? '正在同步…' : '同步修复后的成品'}
                </button>
              </div>
              <p className="final-review__hint">同步会保留旧快照历史，并把本项重置为未审核，供你重新验收。</p>
            </section>

            <section className="final-review__export">
              <header><span className="section-kicker">FINAL BATCH ONLY</span><h2>导出完整终审批次</h2></header>
              <p>只有全部 <strong>{state.batch?.itemCount ?? 0}</strong> 张均已标记“完全没问题”，且没有过期证据、冲突或未保存草稿时，才能发起最终导出。</p>
              <label className="field-label" htmlFor="final-review-output">选择一个尚不存在的新目录</label>
              <input disabled={interactionLocked} id="final-review-output" aria-describedby="final-review-output-hint" onChange={(event) => setOutputPath(event.target.value)} placeholder="请输入尚不存在的新目录绝对路径" value={outputPath} />
              <p className="final-review__path-hint" id="final-review-output-hint">为防止覆盖已有文件，输出路径必须是一个尚不存在的新目录。</p>
              <div className="final-review__export-options">
                <label><span>同名文件</span><select aria-label="终审导出同名文件处理" disabled={interactionLocked}><option value="rename">安全重命名（终态必需）</option></select></label>
                <label className="check-row"><input checked={preserveTree} disabled={interactionLocked} onChange={(event) => setPreserveTree(event.target.checked)} type="checkbox" /><span>按来源项目保留目录层级</span></label>
              </div>
              <button
                className="button button--accent button--block"
                disabled={!outputPath.trim() || interactionLocked || !exportReady}
                onClick={() => void state.exportApproved({ outputPath: outputPath.trim(), conflict: 'rename', preserveTree })}
                type="button"
              >
                {state.exporting ? '正在安全导出…' : `导出 ${state.batch?.counts.approved ?? 0} 张已通过成品`}
              </button>
              {state.exportResult ? (
                <div className="final-review__export-success" role="status">
                  <strong>导出完成：{state.exportResult.exportedCount} 张</strong>
                  <span>{state.exportResult.outputPath}</span>
                  <small>未审未导出 {state.exportResult.skippedPendingCount} · 有问题未导出 {state.exportResult.skippedIssuesCount} · 同名跳过 {state.exportResult.skippedCollisionCount}</small>
                </div>
              ) : null}
            </section>
          </>
        ) : <EmptyState icon="审" title="等待选择成品" description="选择后可以给出三态结论、多项问题标签和可执行反馈。" />}
      </aside>
    </main>
  );
}

export function RepairContextBanner({ onReturn }: { onReturn: () => void }) {
  const context = useFinalReviewStore((state) => state.repairContext);
  if (!context) return null;
  return (
    <div className="repair-context-banner" role="status">
      <div><strong>正在处理终审反馈</strong><span>{context.issueCodes.map(finalReviewIssueLabel).join('、')} · {context.feedback || '无补充说明'}</span></div>
      <button className="button button--compact button--accent" onClick={onReturn} type="button">返回终审</button>
    </div>
  );
}
