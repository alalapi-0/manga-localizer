import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, api } from '../api/client';
import {
  filteredFinalReviewItems,
  finalReviewCanonicalDigest,
  finalReviewDraftDirty,
  finalReviewEvidenceDigest,
  finalReviewValidationError,
  resetFinalReviewStore,
  useFinalReviewStore,
} from './store';
import { finalReviewBatchFixture, finalReviewItemFixture } from './testFixtures';
import type { FinalReviewBatch, FinalReviewEvidenceDescriptor, FinalReviewItem } from './types';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function saveResult(item: ReturnType<typeof finalReviewItemFixture>, batchRevision = 2, historyCreated = true) {
  return {
    item: historyCreated && item.verdict !== 'pending' && !item.reviewedAt
      ? { ...item, reviewedAt: '2026-08-25T02:00:00Z' }
      : item,
    batchRevision,
    historyCreated,
  };
}

const REPAIR_PARAMETER_SET_HASH = '9ede4cd795967a3ec5e3de3ba544b677aabb589b4490c2f8cecc655808bab338';

function repairResult(
  item: FinalReviewItem,
  patch: Partial<Awaited<ReturnType<typeof api.beginFinalReviewRepair>>> = {},
): Awaited<ReturnType<typeof api.beginFinalReviewRepair>> {
  return {
    itemId: item.id,
    sourceProjectId: item.sourceProjectId,
    sourceImageId: item.sourceImageId,
    repairProjectId: item.sourceProjectId,
    repairImageId: `repair-${item.sourceImageId}`,
    pageGenerationId: `generation-${item.id}`,
    runId: `final-review-${item.id.slice(0, 8)}-r${item.revision}`,
    finalReviewItemRevision: item.revision,
    batchRevision: 1,
    artifactRevision: item.artifactRevision,
    nextSequence: 2,
    parameterSetId: 'final-review-repair-v1',
    parameterSetHash: REPAIR_PARAMETER_SET_HASH,
    idempotent: false,
    ...patch,
  };
}

function strictRefreshItem(id: string, patch: Partial<FinalReviewItem> = {}): FinalReviewItem {
  const item = finalReviewItemFixture(id, {
    revision: 2,
    artifactRevision: 2,
    verdict: 'pending',
    issueCodes: [],
    feedback: '',
    reviewedAt: null,
    currentArtifactStale: false,
    strictEvidence: true,
    formatVersion: 2,
    ...patch,
  });
  return {
    ...item,
    artifactChecksum: item.evidence.final.checksum ?? '',
    contentUrl: `/api/final-review-items/${id}/content?artifactRevision=${item.artifactRevision}`,
    thumbnailUrl: `/api/final-review-items/${id}/thumbnail?artifactRevision=${item.artifactRevision}`,
  } as FinalReviewItem;
}

function notApplicableDescriptor(
  item: FinalReviewItem,
  kind: 'mask' | 'clean',
): FinalReviewEvidenceDescriptor {
  const terminal = item.evidence[kind];
  return {
    kind,
    availability: 'not-applicable',
    artifactRevision: item.artifactRevision,
    generationId: terminal.generationId,
    producerId: null,
    producerRevisionId: null,
    terminalId: terminal.terminalId,
    terminalChecksum: terminal.terminalChecksum,
    terminalRevisionId: terminal.terminalRevisionId,
    checksum: null,
    grid: null,
    resolutionDigest: null,
    relativePath: null,
    url: null,
  };
}

function strictNotApplicableRefreshItem(id: string): FinalReviewItem {
  const item = strictRefreshItem(id);
  const evidence = {
    ...item.evidence,
    mask: notApplicableDescriptor(item, 'mask'),
    clean: notApplicableDescriptor(item, 'clean'),
  };
  return {
    ...item,
    evidence,
    evidenceDigest: finalReviewEvidenceDigest(evidence),
  };
}

function strictPreprocessRefreshItem(id: string): FinalReviewItem {
  const item = strictNotApplicableRefreshItem(id);
  const quality = item.evidence.quality;
  const evidence = {
    ...item.evidence,
    final: {
      ...item.evidence.final,
      checksum: quality.checksum,
      producerId: quality.producerId,
      producerRevisionId: null,
      terminalChecksum: quality.checksum,
    },
  };
  return {
    ...item,
    finalVariant: 'preprocess',
    artifactChecksum: quality.checksum ?? '',
    evidence,
    evidenceDigest: finalReviewEvidenceDigest(evidence),
  };
}

function legacyPublicItem(id: string, patch: Partial<FinalReviewItem> = {}): FinalReviewItem {
  const item = finalReviewItemFixture(id, {
    formatVersion: 1,
    strictEvidence: false,
    evidenceDigest: null,
    ...patch,
  });
  const evidence = Object.fromEntries(([
    'original', 'quality', 'mask', 'clean', 'final',
  ] as const).map((kind) => [kind, {
    kind,
    availability: kind === 'final' ? 'available' as const : 'unavailable' as const,
    artifactRevision: item.artifactRevision,
    generationId: null,
    producerId: null,
    producerRevisionId: null,
    terminalId: null,
    terminalChecksum: null,
    terminalRevisionId: null,
    checksum: kind === 'final' ? item.artifactChecksum : null,
    grid: null,
    resolutionDigest: null,
    relativePath: kind === 'final' ? `snapshots/${id}.png` : null,
    url: kind === 'final'
      ? `/api/final-review-items/${id}/artifacts/final?artifactRevision=${item.artifactRevision}`
      : null,
  }])) as FinalReviewItem['evidence'];
  return { ...item, evidence };
}

function seedFinalReview() {
  const items = [
    finalReviewItemFixture('final-item-1'),
    finalReviewItemFixture('final-item-2', { verdict: 'approved', reviewedAt: '2026-08-25T01:00:00Z' }),
    finalReviewItemFixture('final-item-3', {
      verdict: 'issues', issueCodes: ['translation'], feedback: '译意不准',
      reviewedAt: '2026-08-25T01:30:00Z',
    }),
  ];
  const batch = finalReviewBatchFixture(items);
  useFinalReviewStore.setState({
    batches: [batch], batch, items, activeItemId: items[0]?.id ?? null,
    draft: { verdict: 'pending', issueCodes: [], feedback: '' },
  });
  return { batch, items };
}

function seedAllApproved() {
  const items = [1, 2, 3].map((position) => finalReviewItemFixture(`final-item-${position}`, {
    verdict: 'approved',
    reviewedAt: `2026-08-25T0${position}:00:00Z`,
  }));
  const batch = finalReviewBatchFixture(items);
  useFinalReviewStore.setState({
    batches: [batch], batch, items, activeItemId: items[0]!.id,
    draft: { verdict: 'approved', issueCodes: [], feedback: '' },
  });
  return { batch, items };
}

function seedConflictRecovery() {
  const items = [
    finalReviewItemFixture('final-item-1', {
      revision: 3,
      artifactRevision: 2,
      verdict: 'issues',
      issueCodes: ['mask'],
      feedback: '保留的本地草稿',
      reviewedAt: '2026-08-25T01:00:00Z',
    }),
    finalReviewItemFixture('final-item-2', {
      verdict: 'approved', reviewedAt: '2026-08-25T01:30:00Z',
    }),
    finalReviewItemFixture('final-item-3'),
  ];
  const batch = { ...finalReviewBatchFixture(items), revision: 5 };
  const draft = { verdict: 'issues' as const, issueCodes: ['mask'] as const, feedback: '保留的本地草稿' };
  useFinalReviewStore.setState({
    batches: [batch], batch, items, activeItemId: items[0]!.id,
    draft: { ...draft, issueCodes: [...draft.issueCodes] },
    conflictDraft: { ...draft, issueCodes: [...draft.issueCodes] },
    conflict: true,
    error: '操作结果未知',
  });
  return { batch, items, draft };
}

function advancedStrictRecoveryBatch(
  batch: FinalReviewBatch,
  mutate: (item: FinalReviewItem) => FinalReviewItem,
): FinalReviewBatch {
  const current = batch.items[0]!;
  const advanced = finalReviewItemFixture(current.id, {
    revision: current.revision + 1,
    artifactRevision: current.artifactRevision + 1,
    verdict: current.verdict,
    issueCodes: [...current.issueCodes],
    feedback: current.feedback,
    reviewedAt: current.reviewedAt,
  });
  return {
    ...batch,
    revision: batch.revision + 1,
    items: [mutate(advanced), ...batch.items.slice(1)],
  };
}

describe('final review store', () => {
  afterEach(() => {
    resetFinalReviewStore();
    vi.restoreAllMocks();
  });

  it('matches backend canonical JSON SHA-256 vectors and the frozen evidence payload', () => {
    expect(finalReviewCanonicalDigest({ width: 1200, height: 1800 })).toBe(
      '29c4ae5c9f3bcb144453f34dcd1eb93e64218219e19da76e3ae49400c842a25c',
    );
    expect(finalReviewCanonicalDigest({ text: '漫画😀', grid: { width: 1200, height: 1800 } })).toBe(
      '4ede557ae0fb3fe5a2f960bb4729f83832ccfb9ec8b5879e42d6bf99e78eb324',
    );
    expect(finalReviewCanonicalDigest({ z: '\u007f', a: 'é/😀' })).toBe(
      'fb3533289ffc743b3904ee9742f20bed664f706edad892ad9eff08df05bf5129',
    );
    const item = finalReviewItemFixture('final-item-1');
    expect(item.evidence.original.resolutionDigest).toBe(
      '29c4ae5c9f3bcb144453f34dcd1eb93e64218219e19da76e3ae49400c842a25c',
    );
    expect(item.evidenceDigest).toBe(
      'fb761f96f5691847f01e9ac710a14b08037e006b052cf29bfc22ea9e575acf4b',
    );
    expect(finalReviewEvidenceDigest(item.evidence)).toBe(item.evidenceDigest);
  });

  it('loads a batch and its items when detail is compact', async () => {
    const batch = finalReviewBatchFixture([]);
    const items = [finalReviewItemFixture()];
    vi.spyOn(api, 'getFinalReviewBatch').mockResolvedValue(batch);
    vi.spyOn(api, 'listFinalReviewItems').mockResolvedValue(items);

    await expect(useFinalReviewStore.getState().loadBatch(batch.id)).resolves.toBe(true);
    expect(useFinalReviewStore.getState()).toMatchObject({
      activeItemId: items[0]?.id,
      items,
    });
  });

  it('filters independently by verdict, issue and search', () => {
    seedFinalReview();
    useFinalReviewStore.setState({ statusFilter: 'issues', issueFilter: 'translation', search: '真实项目' });
    expect(filteredFinalReviewItems(useFinalReviewStore.getState()).map((item) => item.id)).toEqual(['final-item-3']);
  });

  it('tracks drafts and validates issue and other feedback requirements', () => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'issues' });
    expect(finalReviewDraftDirty(useFinalReviewStore.getState())).toBe(true);
    expect(finalReviewValidationError(useFinalReviewStore.getState().draft)).toMatch('至少选择');
    useFinalReviewStore.getState().toggleIssue('other');
    expect(finalReviewValidationError(useFinalReviewStore.getState().draft)).toMatch('必须填写');
    useFinalReviewStore.getState().updateDraft({ feedback: '气泡外还有残字' });
    expect(finalReviewValidationError(useFinalReviewStore.getState().draft)).toBe('');
  });

  it('advances a clean draft locally without issuing a PATCH', async () => {
    seedFinalReview();
    const update = vi.spyOn(api, 'updateFinalReviewItem');

    await expect(useFinalReviewStore.getState().save(true)).resolves.toBe(true);

    expect(update).not.toHaveBeenCalled();
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-2');
  });

  it('persists once and advances to the trigger-time filtered successor', async () => {
    const items = [
      finalReviewItemFixture('final-item-1'),
      finalReviewItemFixture('final-item-2'),
      finalReviewItemFixture('final-item-3'),
    ];
    const batch = finalReviewBatchFixture(items);
    useFinalReviewStore.setState({
      batches: [batch], batch, items, activeItemId: items[0]!.id,
      draft: { verdict: 'pending', issueCodes: [], feedback: '' }, statusFilter: 'pending',
    });
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved', feedback: '终审通过' });
    const update = vi.spyOn(api, 'updateFinalReviewItem').mockResolvedValue(saveResult(finalReviewItemFixture('final-item-1', {
      verdict: 'approved', feedback: '终审通过', revision: 2, reviewedAt: '2026-08-25T02:00:00Z',
    })));

    await expect(useFinalReviewStore.getState().save(true)).resolves.toBe(true);

    expect(update).toHaveBeenCalledOnce();
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-2');
    expect(useFinalReviewStore.getState().batch?.counts.approved).toBe(1);
  });

  it('advances to the deterministic successor in the trigger-time search results', async () => {
    const items = [
      finalReviewItemFixture('final-item-1', { sourceRelativePath: '目标/第一页.png' }),
      finalReviewItemFixture('final-item-2', { sourceRelativePath: '其他/第二页.png' }),
      finalReviewItemFixture('final-item-3', { sourceRelativePath: '目标/第三页.png' }),
    ];
    const batch = finalReviewBatchFixture(items);
    useFinalReviewStore.setState({
      batches: [batch], batch, items, activeItemId: items[0]!.id,
      draft: { verdict: 'pending', issueCodes: [], feedback: '' }, search: '目标',
    });
    const update = vi.spyOn(api, 'updateFinalReviewItem');

    await expect(useFinalReviewStore.getState().save(true)).resolves.toBe(true);

    expect(update).not.toHaveBeenCalled();
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-3');
  });

  it('uses issue-filter order and blocks when the active item is outside that visible sequence', async () => {
    const items = [
      finalReviewItemFixture('final-item-1', { verdict: 'issues', issueCodes: ['translation'] }),
      finalReviewItemFixture('final-item-2', { verdict: 'issues', issueCodes: ['mask'] }),
      finalReviewItemFixture('final-item-3', { verdict: 'issues', issueCodes: ['translation'] }),
    ];
    const batch = finalReviewBatchFixture(items);
    useFinalReviewStore.setState({
      batches: [batch], batch, items, activeItemId: items[0]!.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '' },
      issueFilter: 'translation',
    });
    const update = vi.spyOn(api, 'updateFinalReviewItem');

    await expect(useFinalReviewStore.getState().save(true)).resolves.toBe(true);
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-3');

    useFinalReviewStore.setState({
      activeItemId: items[1]!.id,
      draft: { verdict: 'issues', issueCodes: ['mask'], feedback: '' },
      error: '',
    });
    await expect(useFinalReviewStore.getState().save(true)).resolves.toBe(false);
    expect(update).not.toHaveBeenCalled();
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-2');
    expect(useFinalReviewStore.getState().error).toContain('没有下一张');
  });

  it('blocks save-and-next on the final visible item but still allows explicit save', async () => {
    const { items } = seedFinalReview();
    useFinalReviewStore.setState({
      activeItemId: items[2]!.id,
      draft: { verdict: 'approved', issueCodes: [], feedback: '修订后通过' },
    });
    const update = vi.spyOn(api, 'updateFinalReviewItem').mockResolvedValue(saveResult({
      ...items[2]!, verdict: 'approved', issueCodes: [], feedback: '修订后通过', revision: 2,
    }));

    await expect(useFinalReviewStore.getState().save(true)).resolves.toBe(false);
    expect(update).not.toHaveBeenCalled();
    expect(useFinalReviewStore.getState().error).toContain('没有下一张');

    await expect(useFinalReviewStore.getState().save()).resolves.toBe(true);
    expect(update).toHaveBeenCalledOnce();
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-3');
  });

  it.each([
    ['request failure', new Error('network unavailable'), true],
    ['malformed successful response', new ApiError('invalid response', 502), true],
    ['revision conflict', new ApiError('revision conflict', 409), true],
    ['proven precondition rejection', new ApiError('invalid request', 400), false],
  ])('does not advance after a %s', async (_label, failure, conflict) => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved' });
    vi.spyOn(api, 'updateFinalReviewItem').mockRejectedValue(failure);

    await expect(useFinalReviewStore.getState().save(true)).resolves.toBe(false);

    expect(useFinalReviewStore.getState()).toMatchObject({ activeItemId: 'final-item-1', conflict });
  });

  it('coalesces rapid save-and-next attempts into one PATCH and one advance', async () => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved' });
    let resolveUpdate!: (item: Awaited<ReturnType<typeof api.updateFinalReviewItem>>) => void;
    const update = vi.spyOn(api, 'updateFinalReviewItem').mockReturnValue(new Promise((resolve) => {
      resolveUpdate = resolve;
    }));

    const first = useFinalReviewStore.getState().save(true);
    const repeated = useFinalReviewStore.getState().save(true);
    await expect(repeated).resolves.toBe(false);
    expect(update).toHaveBeenCalledOnce();
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-1');
    expect(useFinalReviewStore.getState().navigate(1)).toBe(false);

    resolveUpdate(saveResult(finalReviewItemFixture('final-item-1', { verdict: 'approved', revision: 2 })));
    await expect(first).resolves.toBe(true);
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-2');
  });

  it('does not replace a different active draft when an earlier save returns', async () => {
    const { items } = seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved' });
    let resolveUpdate!: (item: Awaited<ReturnType<typeof api.updateFinalReviewItem>>) => void;
    vi.spyOn(api, 'updateFinalReviewItem').mockReturnValue(new Promise((resolve) => {
      resolveUpdate = resolve;
    }));
    const saving = useFinalReviewStore.getState().save(true);
    useFinalReviewStore.setState({
      activeItemId: items[2]!.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '保留这个草稿' },
    });

    resolveUpdate(saveResult(finalReviewItemFixture('final-item-1', { verdict: 'approved', revision: 2 })));
    await expect(saving).resolves.toBe(true);

    expect(useFinalReviewStore.getState()).toMatchObject({
      activeItemId: 'final-item-3',
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '保留这个草稿' },
    });
    expect(useFinalReviewStore.getState().items[0]).toMatchObject({ verdict: 'approved', revision: 2 });
    expect(useFinalReviewStore.getState().batch?.revision).toBe(2);
  });

  it('globally locks a late unknown save outcome after active identity changes and preserves the current draft', async () => {
    const { items } = seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved', feedback: '原请求草稿' });
    let rejectUpdate!: (reason?: unknown) => void;
    vi.spyOn(api, 'updateFinalReviewItem').mockReturnValue(new Promise((_resolve, reject) => {
      rejectUpdate = reject;
    }));
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    const saving = useFinalReviewStore.getState().save();
    const currentDraft = { verdict: 'issues' as const, issueCodes: ['translation'] as const, feedback: '当前页草稿' };
    useFinalReviewStore.setState({
      activeItemId: items[2]!.id,
      draft: { ...currentDraft, issueCodes: [...currentDraft.issueCodes] },
    });
    rejectUpdate(new Error('save response lost after commit'));

    await expect(saving).resolves.toBe(false);
    expect(useFinalReviewStore.getState()).toMatchObject({
      activeItemId: items[2]!.id,
      conflict: true,
      operation: null,
      draft: currentDraft,
      conflictDraft: currentDraft,
    });
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-after-late-save', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it('fail-closes a committed save response when its requested item can no longer be merged', async () => {
    const { items } = seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved' });
    let resolveUpdate!: (value: Awaited<ReturnType<typeof api.updateFinalReviewItem>>) => void;
    vi.spyOn(api, 'updateFinalReviewItem').mockReturnValue(new Promise((resolve) => {
      resolveUpdate = resolve;
    }));

    const saving = useFinalReviewStore.getState().save();
    const currentDraft = { verdict: 'issues' as const, issueCodes: ['translation'] as const, feedback: '仍在当前页' };
    useFinalReviewStore.setState({
      items: [items[2]!],
      activeItemId: items[2]!.id,
      draft: { ...currentDraft, issueCodes: [...currentDraft.issueCodes] },
    });
    resolveUpdate(saveResult(finalReviewItemFixture('final-item-1', { verdict: 'approved', revision: 2 })));

    await expect(saving).resolves.toBe(false);
    expect(useFinalReviewStore.getState()).toMatchObject({
      conflict: true,
      operation: null,
      activeItemId: items[2]!.id,
      draft: currentDraft,
      conflictDraft: currentDraft,
    });
    expect(useFinalReviewStore.getState().error).toContain('已在服务端完成');
  });

  it('preserves the draft and exposes a 409 conflict', async () => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved' });
    const update = vi.spyOn(api, 'updateFinalReviewItem').mockRejectedValue(new ApiError('revision conflict', 409));

    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);
    expect(useFinalReviewStore.getState()).toMatchObject({ conflict: true, error: 'revision conflict' });
    expect(useFinalReviewStore.getState().draft?.verdict).toBe('approved');

    useFinalReviewStore.getState().updateDraft({ feedback: '不应覆盖冲突草稿' });
    useFinalReviewStore.getState().toggleIssue('mask');
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'approved', issueCodes: [], feedback: '',
    });
    expect(useFinalReviewStore.getState().selectItem('final-item-2')).toBe(false);
    expect(useFinalReviewStore.getState().navigate(1)).toBe(false);
    await expect(useFinalReviewStore.getState().loadBatch('final-batch-1')).resolves.toBe(false);
    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(false);
    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toBeNull();
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/must-not-export', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportApproved).not.toHaveBeenCalled();
    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);
    expect(update).toHaveBeenCalledOnce();
    expect(useFinalReviewStore.getState()).toMatchObject({ conflict: true, error: 'revision conflict' });
  });

  it('blocks refresh and repair for an issues item after a 409 until explicit reload', async () => {
    const { items } = seedFinalReview();
    const issue = { ...items[2]!, currentArtifactStale: true };
    useFinalReviewStore.setState((state) => ({
      items: state.items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '新的未保存反馈' },
    }));
    vi.spyOn(api, 'updateFinalReviewItem').mockRejectedValue(new ApiError('batch drift', 409));
    const refresh = vi.spyOn(api, 'refreshFinalReviewItem');
    const repair = vi.spyOn(api, 'beginFinalReviewRepair');

    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);
    expect(useFinalReviewStore.getState().conflict).toBe(true);
    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(false);
    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toBeNull();
    expect(refresh).not.toHaveBeenCalled();
    expect(repair).not.toHaveBeenCalled();
    expect(useFinalReviewStore.getState().draft?.feedback).toBe('新的未保存反馈');
  });

  it.each([
    ['network interruption', new Error('connection reset after request')],
    ['malformed successful response', new ApiError('missing mutation metadata', 502)],
  ])('fail-closes an unknown save outcome and blocks every downstream mutation: %s', async (_label, failure) => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved', feedback: '必须保留' });
    vi.spyOn(api, 'updateFinalReviewItem').mockRejectedValue(failure);
    const refresh = vi.spyOn(api, 'refreshFinalReviewItem');
    const repair = vi.spyOn(api, 'beginFinalReviewRepair');
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);
    expect(useFinalReviewStore.getState()).toMatchObject({ conflict: true });
    expect(useFinalReviewStore.getState().error).toContain('操作结果未知');
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'approved', issueCodes: [], feedback: '必须保留',
    });
    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(false);
    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toBeNull();
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-after-unknown-save', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(refresh).not.toHaveBeenCalled();
    expect(repair).not.toHaveBeenCalled();
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it('fail-closes a successful save response carrying the wrong item identity before applying it', async () => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved', feedback: 'wrong-id 草稿' });
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      item: finalReviewItemFixture('different-item', { revision: 2, verdict: 'approved' }),
      batchRevision: 2,
      historyCreated: true,
    }));
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);
    expect(useFinalReviewStore.getState().conflict).toBe(true);
    expect(useFinalReviewStore.getState().error).toContain('操作结果未知');
    expect(useFinalReviewStore.getState().draft?.feedback).toBe('wrong-id 草稿');
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-wrong-save-item', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it.each([
    ['wrong verdict', { verdict: 'approved', issueCodes: [] }],
    ['wrong issue codes', { issueCodes: ['translation'] }],
    ['wrong feedback', { feedback: '服务端返回了另一份反馈' }],
    ['frozen checksum drift', { artifactChecksum: 'forged-final-checksum' }],
    ['stale projection drift', { currentArtifactStale: true }],
  ] satisfies Array<[string, Partial<FinalReviewItem>]>)('fail-closes a structurally valid save response with %s', async (_label, responsePatch) => {
    const { items } = seedFinalReview();
    useFinalReviewStore.getState().updateDraft({
      verdict: 'issues',
      issueCodes: ['translation', 'mask', 'translation'],
      feedback: '  修复反馈  ',
    });
    const before = useFinalReviewStore.getState();
    const responseItem: FinalReviewItem = {
      ...items[0]!,
      revision: 2,
      verdict: 'issues',
      issueCodes: ['mask', 'translation'],
      feedback: '修复反馈',
      reviewedAt: '2026-08-25T02:00:00Z',
      ...responsePatch,
    };
    const fetch = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      item: responseItem,
      batchRevision: 2,
      historyCreated: true,
    }));
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);
    const requestBody = JSON.parse(String((fetch.mock.calls[0]?.[1] as RequestInit | undefined)?.body));
    expect(requestBody).toMatchObject({
      verdict: 'issues', issueCodes: ['mask', 'translation'], feedback: '修复反馈',
    });
    expect(useFinalReviewStore.getState()).toMatchObject({
      conflict: true,
      operation: null,
      items: before.items,
      batch: before.batch,
      draft: before.draft,
      conflictDraft: before.draft,
    });
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-wrong-save-semantics', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it('rejects a save response whose item revision jumps despite otherwise valid metadata', async () => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved' });
    vi.spyOn(api, 'updateFinalReviewItem').mockResolvedValue({
      item: finalReviewItemFixture('final-item-1', { revision: 3, verdict: 'approved' }),
      batchRevision: 2,
      historyCreated: true,
    });

    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);
    expect(useFinalReviewStore.getState().conflict).toBe(true);
    expect(useFinalReviewStore.getState().items[0]?.revision).toBe(1);
  });

  it('rejects a save response that changes the frozen artifact revision', async () => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved' });
    vi.spyOn(api, 'updateFinalReviewItem').mockResolvedValue({
      item: finalReviewItemFixture('final-item-1', {
        revision: 2, artifactRevision: 2, verdict: 'approved',
      }),
      batchRevision: 2,
      historyCreated: true,
    });

    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);
    expect(useFinalReviewStore.getState().conflict).toBe(true);
    expect(useFinalReviewStore.getState().items[0]?.artifactRevision).toBe(1);
  });

  it('locks draft mutations until a deferred save response has been applied', async () => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved', feedback: '提交时草稿' });
    let resolveUpdate!: (item: Awaited<ReturnType<typeof api.updateFinalReviewItem>>) => void;
    vi.spyOn(api, 'updateFinalReviewItem').mockReturnValue(new Promise((resolve) => {
      resolveUpdate = resolve;
    }));

    const saving = useFinalReviewStore.getState().save();
    expect(useFinalReviewStore.getState().saving).toBe(true);
    useFinalReviewStore.getState().updateDraft({ feedback: '响应前的新编辑' });
    useFinalReviewStore.getState().toggleIssue('translation');
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'approved', issueCodes: [], feedback: '提交时草稿',
    });

    resolveUpdate(saveResult(finalReviewItemFixture('final-item-1', {
      verdict: 'approved', feedback: '提交时草稿', revision: 2,
    })));
    await expect(saving).resolves.toBe(true);
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'approved', issueCodes: [], feedback: '提交时草稿',
    });
  });

  it('protects unsaved work during navigation', () => {
    seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ feedback: '未保存' });
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    expect(useFinalReviewStore.getState().navigate(1)).toBe(false);
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-1');
  });

  it('refreshes the snapshot, resets pending, and blocks export until every item is approved', async () => {
    const { items } = seedFinalReview();
    useFinalReviewStore.setState({
      activeItemId: items[2]?.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '译意不准' },
    });
    vi.spyOn(api, 'refreshFinalReviewItem').mockResolvedValue({
      item: strictRefreshItem('final-item-3', {
        thumbnailChecksum: 'e'.repeat(64),
      }),
      batchRevision: 2,
      historyCreated: true,
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(true);
    expect(useFinalReviewStore.getState().items[2]).toMatchObject({
      verdict: 'pending', revision: 2, thumbnailChecksum: 'e'.repeat(64),
    });

    const exportSpy = vi.spyOn(api, 'exportApprovedFinalReviewItems');
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/safe/new', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportSpy).not.toHaveBeenCalled();
  });

  it('exports only an exact all-approved authoritative batch with batch CAS and actor', async () => {
    seedAllApproved();
    const exportSpy = vi.spyOn(api, 'exportApprovedFinalReviewItems').mockResolvedValue({
      batchId: 'final-batch-1', outputPath: '/opt/manga/Final Output', exportedCount: 3,
      skippedPendingCount: 0, skippedIssuesCount: 0, skippedCollisionCount: 0,
      manifestPath: '/opt/manga/Final Output/manifest.json',
    });

    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/opt/manga/Exports/../Final Output/', conflict: 'skip', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportSpy).not.toHaveBeenCalled();

    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/opt/manga/Exports/../Final Output/', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(true);
    expect(exportSpy).toHaveBeenCalledWith('final-batch-1', expect.objectContaining({
      outputPath: '/opt/manga/Exports/../Final Output/', conflict: 'rename', preserveTree: true,
      expectedBatchRevision: 1,
      actor: expect.objectContaining({ actorKind: 'human', operationSource: 'ui' }),
    }));
  });

  it('fail-closes structurally valid export results not bound to the resolved target and fixed manifest', async () => {
    const invalidResults = [
      { outputPath: '/safe/other', manifestPath: '/safe/other/manifest.json' },
      { outputPath: '/safe/terminal', manifestPath: '/safe/terminal/nested/manifest.json' },
      { outputPath: '/safe/terminal', manifestPath: '/safe/terminal-copy/manifest.json' },
      { outputPath: '/safe/terminal', manifestPath: '/safe/terminal/export.json' },
      { outputPath: '/safe/terminal/', manifestPath: '/safe/terminal/manifest.json' },
    ];
    const exportSpy = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    for (const paths of invalidResults) {
      resetFinalReviewStore();
      const { batch, items } = seedAllApproved();
      const draft = useFinalReviewStore.getState().draft;
      exportSpy.mockResolvedValueOnce({
        batchId: batch.id,
        ...paths,
        exportedCount: items.length,
        skippedPendingCount: 0,
        skippedIssuesCount: 0,
        skippedCollisionCount: 0,
      });

      await expect(useFinalReviewStore.getState().exportApproved({
        outputPath: '/safe/terminal', conflict: 'rename', preserveTree: true,
      })).resolves.toBe(false);
      expect(useFinalReviewStore.getState()).toMatchObject({
        batch, items, draft, conflict: true, operation: null, exportResult: null,
      });
      expect(useFinalReviewStore.getState().error).toContain('操作结果未知');
    }
    expect(exportSpy).toHaveBeenCalledTimes(invalidResults.length);
  });

  it('does not request export for a relative target that cannot match backend absolute-path semantics', async () => {
    seedAllApproved();
    const exportSpy = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: 'relative/output', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportSpy).not.toHaveBeenCalled();
    expect(useFinalReviewStore.getState().conflict).toBe(false);
  });

  it('fail-closes terminal export results with forged batch, count, or skipped semantics', async () => {
    const invalidResults = [
      { batchId: 'different-batch' },
      { exportedCount: 2 },
      { skippedPendingCount: 1 },
      { skippedIssuesCount: 1 },
      { exportedCount: 2, skippedCollisionCount: 1 },
    ];
    const exportSpy = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    for (const patch of invalidResults) {
      resetFinalReviewStore();
      seedAllApproved();
      exportSpy.mockResolvedValueOnce({
        batchId: 'final-batch-1', outputPath: '/safe/terminal', exportedCount: 3,
        skippedPendingCount: 0, skippedIssuesCount: 0, skippedCollisionCount: 0,
        manifestPath: '/safe/terminal/manifest.json',
        ...patch,
      });

      await expect(useFinalReviewStore.getState().exportApproved({
        outputPath: '/safe/terminal', conflict: 'rename', preserveTree: true,
      })).resolves.toBe(false);
      expect(useFinalReviewStore.getState()).toMatchObject({ conflict: true, operation: null });
      expect(useFinalReviewStore.getState().error).toContain('操作结果未知');
    }
    expect(exportSpy).toHaveBeenCalledTimes(invalidResults.length);
  });

  it('blocks the coherent 41-approved/158-issues/0-pending batch from export', async () => {
    const items = Array.from({ length: 199 }, (_, index) => finalReviewItemFixture(`terminal-item-${index + 1}`, {
      verdict: index < 41 ? 'approved' : 'issues',
      issueCodes: index < 41 ? [] : ['mask'],
      feedback: index < 41 ? '' : '需要修复',
      reviewedAt: '2026-08-25T02:00:00Z',
    }));
    const batch = finalReviewBatchFixture(items);
    useFinalReviewStore.setState({
      batch, items, activeItemId: items[0]!.id,
      draft: { verdict: 'approved', issueCodes: [], feedback: '' },
    });
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-partial', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(batch.counts).toEqual({ approved: 41, issues: 158, pending: 0 });
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it('blocks count mismatch, conflict, dirty draft, and stale-item export preconditions', async () => {
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    const { batch, items } = seedAllApproved();
    useFinalReviewStore.setState({
      batch: { ...batch, itemCount: 4, counts: { approved: 4, pending: 0, issues: 0 } },
    });
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-counts', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);

    useFinalReviewStore.setState({ batch, conflict: true });
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-conflict', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);

    useFinalReviewStore.setState({ conflict: false, draft: { verdict: 'issues', issueCodes: ['mask'], feedback: '' } });
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-draft', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);

    const staleItems = items.map((item, index) => index === 0 ? { ...item, currentArtifactStale: true } : item);
    useFinalReviewStore.setState({
      items: staleItems,
      batch: { ...batch, items: staleItems },
      draft: { verdict: 'approved', issueCodes: [], feedback: '' },
    });
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-stale', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it('uses one operation lock and safely merges a late refresh without replacing the current draft', async () => {
    const { items } = seedFinalReview();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '刷新中的未保存草稿' },
    });
    let resolveRefresh!: (value: Awaited<ReturnType<typeof api.refreshFinalReviewItem>>) => void;
    vi.spyOn(api, 'refreshFinalReviewItem').mockReturnValue(new Promise((resolve) => {
      resolveRefresh = resolve;
    }));
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    const refreshing = useFinalReviewStore.getState().refreshActive();
    expect(useFinalReviewStore.getState().operation).toBe('refresh');
    const lockedDraft = useFinalReviewStore.getState().draft;
    useFinalReviewStore.getState().discardDraft();
    expect(useFinalReviewStore.getState().draft).toEqual(lockedDraft);
    expect(useFinalReviewStore.getState().selectItem('final-item-1')).toBe(false);
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/not-run', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);

    // Simulate an external route restore while the request is in flight. The
    // authoritative batch result must be merged without replacing the current page draft.
    const currentDraft = { verdict: 'pending' as const, issueCodes: [], feedback: '当前页草稿' };
    useFinalReviewStore.setState({ activeItemId: 'final-item-1', draft: currentDraft });
    resolveRefresh({
      item: strictRefreshItem('final-item-3'),
      batchRevision: 2,
      historyCreated: true,
    });
    await expect(refreshing).resolves.toBe(true);
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-1');
    expect(useFinalReviewStore.getState().draft).toEqual(currentDraft);
    expect(useFinalReviewStore.getState().items[2]).toMatchObject({ revision: 2, artifactRevision: 2 });
    expect(useFinalReviewStore.getState().batch?.revision).toBe(2);
    expect(useFinalReviewStore.getState().conflict).toBe(false);
    expect(useFinalReviewStore.getState().operation).toBeNull();
  });

  it('fail-closes a committed refresh response after the current batch identity changes', async () => {
    const { items } = seedFinalReview();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: issue.feedback },
    });
    let resolveRefresh!: (value: Awaited<ReturnType<typeof api.refreshFinalReviewItem>>) => void;
    vi.spyOn(api, 'refreshFinalReviewItem').mockReturnValue(new Promise((resolve) => {
      resolveRefresh = resolve;
    }));
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    const refreshing = useFinalReviewStore.getState().refreshActive();
    const otherItem = finalReviewItemFixture('other-item-1', { batchId: 'other-batch' });
    const otherBatch = { ...finalReviewBatchFixture([otherItem]), id: 'other-batch' };
    const currentDraft = { verdict: 'pending' as const, issueCodes: [], feedback: '另一批次草稿' };
    useFinalReviewStore.setState({
      batch: otherBatch,
      items: [otherItem],
      activeItemId: otherItem.id,
      draft: currentDraft,
    });
    resolveRefresh({
      item: strictRefreshItem(issue.id),
      batchRevision: 2,
      historyCreated: true,
    });

    await expect(refreshing).resolves.toBe(false);
    expect(useFinalReviewStore.getState()).toMatchObject({
      batch: { id: 'other-batch', revision: 1 },
      activeItemId: otherItem.id,
      conflict: true,
      draft: currentDraft,
      conflictDraft: currentDraft,
    });
    expect(useFinalReviewStore.getState().error).toContain('已在服务端完成');
  });

  it('globally locks a late unknown refresh outcome after active identity changes and preserves the current draft', async () => {
    const { items } = seedFinalReview();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: issue.feedback },
    });
    let rejectRefresh!: (reason?: unknown) => void;
    vi.spyOn(api, 'refreshFinalReviewItem').mockReturnValue(new Promise((_resolve, reject) => {
      rejectRefresh = reject;
    }));
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    const refreshing = useFinalReviewStore.getState().refreshActive();
    const currentDraft = { verdict: 'approved' as const, issueCodes: [], feedback: '切换后的当前草稿' };
    useFinalReviewStore.setState({ activeItemId: items[1]!.id, draft: currentDraft });
    rejectRefresh(new Error('refresh response lost after commit'));

    await expect(refreshing).resolves.toBe(false);
    expect(useFinalReviewStore.getState()).toMatchObject({
      activeItemId: items[1]!.id,
      conflict: true,
      operation: null,
      draft: currentDraft,
      conflictDraft: currentDraft,
    });
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-after-late-refresh', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it.each([
    ['network interruption', new Error('refresh response lost')],
    ['malformed successful response', new ApiError('invalid refresh wrapper', 502)],
  ])('fail-closes an unknown refresh outcome and preserves the review draft: %s', async (_label, failure) => {
    const { items } = seedFinalReview();
    const issue = { ...items[2]!, currentArtifactStale: true };
    useFinalReviewStore.setState((state) => ({
      items: state.items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '刷新前草稿' },
    }));
    const refresh = vi.spyOn(api, 'refreshFinalReviewItem').mockRejectedValue(failure);
    const repair = vi.spyOn(api, 'beginFinalReviewRepair');
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(false);
    expect(useFinalReviewStore.getState().conflict).toBe(true);
    expect(useFinalReviewStore.getState().error).toContain('操作结果未知');
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'issues', issueCodes: ['translation'], feedback: '刷新前草稿',
    });
    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(false);
    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toBeNull();
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-after-unknown-refresh', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(refresh).toHaveBeenCalledOnce();
    expect(repair).not.toHaveBeenCalled();
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it('fail-closes a malformed successful refresh wrapper before applying empty item revisions', async () => {
    const { items } = seedFinalReview();
    const issue = { ...items[2]!, currentArtifactStale: true };
    useFinalReviewStore.setState((state) => ({
      items: state.items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: 'malformed refresh 草稿' },
    }));
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      item: {},
      batchRevision: 2,
      historyCreated: true,
    }));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(false);
    expect(useFinalReviewStore.getState().conflict).toBe(true);
    expect(useFinalReviewStore.getState().error).toContain('操作结果未知');
    expect(useFinalReviewStore.getState().items[2]).toMatchObject({ id: issue.id, revision: 1 });
    expect(useFinalReviewStore.getState().draft?.feedback).toBe('malformed refresh 草稿');
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-malformed-refresh', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it.each([
    ['approved verdict', (item: FinalReviewItem): FinalReviewItem => ({
      ...item, verdict: 'approved', reviewedAt: '2026-08-25T02:00:00Z',
    })],
    ['non-empty issue codes', (item: FinalReviewItem): FinalReviewItem => ({
      ...item, issueCodes: ['mask'],
    })],
    ['non-empty feedback', (item: FinalReviewItem): FinalReviewItem => ({
      ...item, feedback: '没有被清空',
    })],
    ['stale projection', (item: FinalReviewItem): FinalReviewItem => ({
      ...item, currentArtifactStale: true,
    })],
    ['non-strict evidence flag', (item: FinalReviewItem): FinalReviewItem => ({
      ...item, strictEvidence: false,
    })],
    ['unavailable strict evidence', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        clean: { ...item.evidence.clean, availability: 'unavailable', url: null },
      },
    })],
    ['quality producer identity stripped', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        quality: { ...item.evidence.quality, producerId: null },
      },
    })],
    ['available terminal identity stripped', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, terminalId: null },
      },
    })],
    ['available terminal checksum stripped', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, terminalChecksum: null },
      },
    })],
    ['available terminal revision stripped', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, terminalRevisionId: null },
      },
    })],
    ['available generation binding stripped', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        clean: { ...item.evidence.clean, generationId: null },
      },
    })],
    ['non-canonical resolution digest with a matching evidence digest', (item: FinalReviewItem): FinalReviewItem => {
      const evidence = {
        ...item.evidence,
        final: { ...item.evidence.final, resolutionDigest: 'a'.repeat(64) },
      };
      return { ...item, evidence, evidenceDigest: finalReviewEvidenceDigest(evidence) };
    }],
    ['non-canonical evidence digest', (item: FinalReviewItem): FinalReviewItem => ({
      ...item, evidenceDigest: 'b'.repeat(64),
    })],
    ['required producer revision stripped', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        mask: { ...item.evidence.mask, producerRevisionId: null },
      },
    })],
    ['available relative path stripped', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        clean: { ...item.evidence.clean, relativePath: null },
      },
    })],
    ['typeset terminal checksum conflated with artifact', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, terminalChecksum: item.evidence.final.checksum },
      },
    })],
    ['original terminal checksum detached from artifact', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        original: { ...item.evidence.original, terminalChecksum: '9'.repeat(64) },
      },
    })],
    ['N/A artifact residue', (item: FinalReviewItem): FinalReviewItem => {
      const exact = strictNotApplicableRefreshItem(item.id);
      return {
        ...exact,
        evidence: {
          ...exact.evidence,
          mask: {
            ...exact.evidence.mask,
            checksum: item.evidence.mask.checksum,
            grid: item.evidence.mask.grid,
            resolutionDigest: item.evidence.mask.resolutionDigest,
            relativePath: item.evidence.mask.relativePath,
          },
        },
      };
    }],
    ['N/A producer residue', (item: FinalReviewItem): FinalReviewItem => {
      const exact = strictNotApplicableRefreshItem(item.id);
      return {
        ...exact,
        evidence: {
          ...exact.evidence,
          mask: {
            ...exact.evidence.mask,
            producerId: item.evidence.mask.producerId,
            producerRevisionId: item.evidence.mask.producerRevisionId,
          },
        },
      };
    }],
    ['N/A terminal revision stripped', (item: FinalReviewItem): FinalReviewItem => {
      const exact = strictNotApplicableRefreshItem(item.id);
      return {
        ...exact,
        evidence: {
          ...exact.evidence,
          clean: { ...exact.evidence.clean, terminalRevisionId: null },
        },
      };
    }],
    ['original marked N/A', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        original: {
          ...item.evidence.original,
          availability: 'not-applicable',
          producerId: null,
          producerRevisionId: null,
          checksum: null,
          grid: null,
          resolutionDigest: null,
          relativePath: null,
          url: null,
        },
      },
    })],
    ['mixed mask and clean availability', (item: FinalReviewItem): FinalReviewItem => ({
      ...item,
      evidence: {
        ...item.evidence,
        mask: notApplicableDescriptor(item, 'mask'),
      },
    })],
    ['preprocess final not bound to quality producer', (item: FinalReviewItem): FinalReviewItem => ({
      ...strictNotApplicableRefreshItem(item.id),
      finalVariant: 'preprocess',
    })],
  ] as Array<[string, (item: FinalReviewItem) => FinalReviewItem]>)('fail-closes a structurally valid refresh response with %s', async (_label, mutateResponse) => {
    const { items } = seedFinalReview();
    const issue = { ...items[2]!, currentArtifactStale: true };
    useFinalReviewStore.setState((state) => ({
      items: state.items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '刷新前草稿' },
    }));
    const before = useFinalReviewStore.getState();
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      item: mutateResponse(strictRefreshItem(issue.id)),
      batchRevision: 2,
      historyCreated: true,
    }));
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(false);
    expect(useFinalReviewStore.getState()).toMatchObject({
      conflict: true,
      operation: null,
      items: before.items,
      batch: before.batch,
      draft: before.draft,
      conflictDraft: before.draft,
    });
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/blocked-wrong-refresh-semantics', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it.each([
    ['typeset with paired G7/G8 N/A', strictNotApplicableRefreshItem],
    ['preprocess with G1-bound final and paired N/A', strictPreprocessRefreshItem],
  ] as Array<[string, (id: string) => FinalReviewItem]>)('accepts an exact strict refresh evidence grammar: %s', async (_label, refreshedItem) => {
    const { items } = seedFinalReview();
    const issue = { ...items[2]!, currentArtifactStale: true };
    useFinalReviewStore.setState((state) => ({
      items: state.items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: issue.feedback },
    }));
    vi.spyOn(api, 'refreshFinalReviewItem').mockResolvedValue({
      item: refreshedItem(issue.id),
      batchRevision: 2,
      historyCreated: true,
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(true);
    expect(useFinalReviewStore.getState()).toMatchObject({
      conflict: false,
      operation: null,
      batch: { revision: 2 },
      draft: { verdict: 'pending', issueCodes: [], feedback: '' },
    });
    expect(useFinalReviewStore.getState().items[2]?.evidence.mask.availability).toBe('not-applicable');
    expect(useFinalReviewStore.getState().items[2]?.evidence.clean.availability).toBe('not-applicable');
  });

  it('rejects a no-op refresh response because refresh must create history and a new artifact revision', async () => {
    const { items } = seedFinalReview();
    const issue = { ...items[2]!, currentArtifactStale: true };
    useFinalReviewStore.setState((state) => ({
      items: state.items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: issue.feedback },
    }));
    vi.spyOn(api, 'refreshFinalReviewItem').mockResolvedValue({
      item: issue,
      batchRevision: 1,
      historyCreated: false,
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(false);
    expect(useFinalReviewStore.getState().conflict).toBe(true);
    expect(useFinalReviewStore.getState().items[2]).toMatchObject({
      revision: 1, artifactRevision: 1, verdict: 'issues',
    });
  });

  it('preserves the unsaved draft across a 409 and explicit newest-version reload', async () => {
    const { batch, items } = seedFinalReview();
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved', feedback: '我的未保存判断' });
    vi.spyOn(api, 'updateFinalReviewItem').mockRejectedValue(new ApiError('revision drift', 409));
    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);

    const newest = finalReviewItemFixture('final-item-1', {
      revision: 7, verdict: 'issues', issueCodes: ['mask'],
      reviewedAt: '2026-08-25T03:00:00Z',
    });
    const loadedItems = [newest, items[1]!, items[2]!];
    vi.spyOn(api, 'getFinalReviewBatch').mockResolvedValue({
      ...finalReviewBatchFixture(loadedItems), revision: 7,
    });
    await expect(useFinalReviewStore.getState().reloadConflict()).resolves.toBe(true);

    expect(useFinalReviewStore.getState().items[0]).toMatchObject({ revision: 7, verdict: 'issues' });
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'approved', issueCodes: [], feedback: '我的未保存判断',
    });
    expect(useFinalReviewStore.getState().batch?.id).toBe(batch.id);
  });

  it('accepts the exact 199-item format-v1 public evidence shape and preserves the local draft', async () => {
    const items = Array.from({ length: 199 }, (_, index) => legacyPublicItem(`legacy-item-${index + 1}`, {
      verdict: index < 41 ? 'approved' : 'issues',
      issueCodes: index < 41 ? [] : ['mask'],
      feedback: index < 41 ? '' : '需要修复',
      reviewedAt: '2026-08-25T02:00:00Z',
    }));
    const batch = { ...finalReviewBatchFixture(items), formatVersion: 1 };
    const active = items[41]!;
    const draft = { verdict: 'issues' as const, issueCodes: ['mask'] as const, feedback: '我的本地未保存判断' };
    useFinalReviewStore.setState({
      batches: [batch], batch, items, activeItemId: active.id,
      draft: { ...draft, issueCodes: [...draft.issueCodes] },
      conflictDraft: { ...draft, issueCodes: [...draft.issueCodes] },
      conflict: true,
      error: '操作结果未知',
    });
    vi.spyOn(api, 'getFinalReviewBatch').mockResolvedValue(
      JSON.parse(JSON.stringify(batch)) as FinalReviewBatch,
    );

    await expect(useFinalReviewStore.getState().reloadConflict()).resolves.toBe(true);

    const recovered = useFinalReviewStore.getState();
    expect(recovered).toMatchObject({
      conflict: false,
      operation: null,
      activeItemId: active.id,
      batch: {
        formatVersion: 1,
        itemCount: 199,
        counts: { approved: 41, issues: 158, pending: 0 },
      },
    });
    expect(recovered.draft).toEqual(draft);
    expect(recovered.items[41]).toMatchObject({
      strictEvidence: false,
      formatVersion: 1,
      evidenceDigest: null,
      evidence: {
        original: { availability: 'unavailable', checksum: null, url: null },
        final: {
          availability: 'available',
          grid: null,
          resolutionDigest: null,
          checksum: active.artifactChecksum,
        },
      },
    });
  });

  it.each([
    ['generation residue', (item: FinalReviewItem) => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, generationId: 'fabricated-generation' },
      },
    })],
    ['producer residue', (item: FinalReviewItem) => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, producerId: 'fabricated-producer' },
      },
    })],
    ['terminal residue', (item: FinalReviewItem) => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, terminalId: 'fabricated-terminal' },
      },
    })],
    ['non-final artifact residue', (item: FinalReviewItem) => ({
      ...item,
      evidence: {
        ...item.evidence,
        original: { ...item.evidence.original, checksum: '1'.repeat(64) },
      },
    })],
    ['wrong final checksum', (item: FinalReviewItem) => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, checksum: '0'.repeat(64) },
      },
    })],
    ['wrong final url', (item: FinalReviewItem) => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, url: '/api/projects/live/final.png' },
      },
    })],
    ['missing final path', (item: FinalReviewItem) => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: { ...item.evidence.final, relativePath: null },
      },
    })],
    ['fabricated final resolution metadata', (item: FinalReviewItem) => ({
      ...item,
      evidence: {
        ...item.evidence,
        final: {
          ...item.evidence.final,
          grid: { width: 1200, height: 1800 },
          resolutionDigest: 'a'.repeat(64),
        },
      },
    })],
    ['wrong item format', (item: FinalReviewItem) => ({ ...item, formatVersion: 2 })],
    ['fabricated evidence digest', (item: FinalReviewItem) => ({ ...item, evidenceDigest: 'b'.repeat(64) })],
  ] as Array<[string, (item: FinalReviewItem) => FinalReviewItem]>)('keeps the conflict lock for contradictory v1 public evidence: %s', async (_label, contradict) => {
    const malformed = contradict(legacyPublicItem('legacy-item-1', {
      verdict: 'issues',
      issueCodes: ['mask'],
      feedback: '需要修复',
      reviewedAt: '2026-08-25T02:00:00Z',
    }));
    const batch = { ...finalReviewBatchFixture([malformed]), formatVersion: 1 };
    const draft = { verdict: 'issues' as const, issueCodes: ['mask'] as const, feedback: '本地草稿' };
    useFinalReviewStore.setState({
      batches: [batch], batch, items: [malformed], activeItemId: malformed.id,
      draft: { ...draft, issueCodes: [...draft.issueCodes] },
      conflictDraft: { ...draft, issueCodes: [...draft.issueCodes] },
      conflict: true,
      error: '操作结果未知',
    });
    vi.spyOn(api, 'getFinalReviewBatch').mockResolvedValue(
      JSON.parse(JSON.stringify(batch)) as FinalReviewBatch,
    );

    await expect(useFinalReviewStore.getState().reloadConflict()).resolves.toBe(false);

    expect(useFinalReviewStore.getState()).toMatchObject({
      conflict: true,
      operation: null,
      loading: false,
      draft,
      conflictDraft: draft,
    });
    expect(useFinalReviewStore.getState().error).toContain('无法证明');
  });

  it.each([
    ['stale batch revision', (batch: FinalReviewBatch) => ({ ...batch, revision: 4 })],
    ['wrong batch with the same item ids', (batch: FinalReviewBatch) => ({ ...batch, id: 'other-batch' })],
    ['duplicate item identity', (batch: FinalReviewBatch) => ({
      ...batch,
      items: [batch.items[0]!, batch.items[1]!, { ...batch.items[2]!, id: batch.items[1]!.id }],
    })],
    ['wrong-bound active item', (batch: FinalReviewBatch) => ({
      ...batch,
      items: batch.items.map((item, index) => index === 0 ? { ...item, batchId: 'other-batch' } : item),
    })],
    ['malformed strict item evidence', (batch: FinalReviewBatch) => ({
      ...batch,
      items: batch.items.map((item, index) => index === 0 ? {
        ...item,
        evidence: {
          ...item.evidence,
          final: { ...item.evidence.final, terminalId: null },
        },
      } : item),
    })],
    ['inconsistent counts', (batch: FinalReviewBatch) => ({
      ...batch, counts: { pending: 0, approved: 3, issues: 0 },
    })],
    ['missing active item', (batch: FinalReviewBatch) => {
      const items = batch.items.slice(1);
      return { ...finalReviewBatchFixture(items), revision: batch.revision };
    }],
    ['regressing item and artifact revisions', (batch: FinalReviewBatch) => ({
      ...batch,
      items: batch.items.map((item, index) => index === 0 ? finalReviewItemFixture(item.id, {
        revision: 2,
        artifactRevision: 1,
        verdict: 'issues',
        issueCodes: ['mask'],
        feedback: item.feedback,
        reviewedAt: item.reviewedAt,
      }) : item),
    })],
    ['wrong source binding', (batch: FinalReviewBatch) => ({
      ...batch,
      items: batch.items.map((item, index) => index === 0
        ? { ...item, sourceImageId: 'other-image' }
        : item),
    })],
    ['non-canonical strict resolution digest', (batch: FinalReviewBatch) => advancedStrictRecoveryBatch(
      batch,
      (item) => {
        const evidence = {
          ...item.evidence,
          final: { ...item.evidence.final, resolutionDigest: 'a'.repeat(64) },
        };
        return { ...item, evidence, evidenceDigest: finalReviewEvidenceDigest(evidence) };
      },
    )],
    ['non-canonical strict evidence digest', (batch: FinalReviewBatch) => advancedStrictRecoveryBatch(
      batch,
      (item) => ({ ...item, evidenceDigest: 'b'.repeat(64) }),
    )],
  ] as Array<[string, (batch: FinalReviewBatch) => FinalReviewBatch]>)('keeps the global lock for an invalid reload: %s', async (_label, malformed) => {
    const { batch, items, draft } = seedConflictRecovery();
    const getBatch = vi.spyOn(api, 'getFinalReviewBatch').mockResolvedValue(malformed(batch));
    const repair = vi.spyOn(api, 'beginFinalReviewRepair');
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems');

    await expect(useFinalReviewStore.getState().reloadConflict()).resolves.toBe(false);

    const state = useFinalReviewStore.getState();
    expect(getBatch).toHaveBeenCalledOnce();
    expect(state).toMatchObject({ conflict: true, operation: null, loading: false });
    expect(state.error).toContain('无法证明');
    expect(state.batch).toBe(batch);
    expect(state.items).toBe(items);
    expect(state.draft).toEqual(draft);
    expect(state.conflictDraft).toEqual(draft);
    await expect(state.beginRepair()).resolves.toBeNull();
    await expect(state.exportApproved({
      outputPath: '/blocked-reload', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(repair).not.toHaveBeenCalled();
    expect(exportApproved).not.toHaveBeenCalled();
  });

  it('clears the conflict only for a coherent non-regressing authoritative reload', async () => {
    const { batch, items, draft } = seedConflictRecovery();
    const newest = finalReviewItemFixture(items[0]!.id, {
      revision: 4,
      artifactRevision: 2,
      verdict: 'issues',
      issueCodes: ['mask'],
      feedback: '服务端并发后的新反馈',
      reviewedAt: '2026-08-25T04:00:00Z',
    });
    const loadedItems = [newest, items[1]!, items[2]!];
    const loadedBatch = { ...finalReviewBatchFixture(loadedItems), revision: batch.revision + 1 };
    vi.spyOn(api, 'getFinalReviewBatch').mockResolvedValue(loadedBatch);

    await expect(useFinalReviewStore.getState().reloadConflict()).resolves.toBe(true);

    const recovered = useFinalReviewStore.getState();
    expect(recovered).toMatchObject({
      conflict: false,
      operation: null,
      loading: false,
      batch: { revision: 6 },
    });
    expect(recovered.items[0]).toMatchObject({
      id: newest.id, revision: 4, feedback: '服务端并发后的新反馈',
    });
    expect(recovered.draft).toEqual(draft);
  });

  it('keeps the authoritative batch revision for a whitespace-normalized save no-op', async () => {
    const { items } = seedFinalReview();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '  译意不准  ' },
    });
    const update = vi.spyOn(api, 'updateFinalReviewItem').mockResolvedValue(
      saveResult(issue, 1, false),
    );

    await expect(useFinalReviewStore.getState().save()).resolves.toBe(true);
    expect(update).toHaveBeenCalledWith(issue.id, expect.objectContaining({ feedback: '译意不准' }));
    expect(useFinalReviewStore.getState().batch?.revision).toBe(1);
    expect(useFinalReviewStore.getState().draft?.feedback).toBe('译意不准');
  });

  it('creates exactly one fresh-G0 handoff for rapid repair calls without changing verdict', async () => {
    const { items } = seedFinalReview();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '译意不准' },
    });
    let resolveRepair!: (value: Awaited<ReturnType<typeof api.beginFinalReviewRepair>>) => void;
    const repair = vi.spyOn(api, 'beginFinalReviewRepair').mockReturnValue(new Promise((resolve) => {
      resolveRepair = resolve;
    }));
    const first = useFinalReviewStore.getState().beginRepair();
    const second = useFinalReviewStore.getState().beginRepair();
    await expect(second).resolves.toBeNull();
    expect(repair).toHaveBeenCalledOnce();
    expect(repair).toHaveBeenCalledWith(issue.id, {
      expectedRevision: issue.revision,
      expectedBatchRevision: 1,
      actor: expect.objectContaining({ actorKind: 'human', operationSource: 'ui' }),
    });
    resolveRepair(repairResult(issue, { pageGenerationId: 'generation-new' }));
    await expect(first).resolves.toMatchObject({
      pageGenerationId: 'generation-new',
      runId: `final-review-${issue.id.slice(0, 8)}-r${issue.revision}`,
      repairProjectId: issue.sourceProjectId,
      repairImageId: `repair-${issue.sourceImageId}`,
      nextSequence: 2,
      parameterSetHash: REPAIR_PARAMETER_SET_HASH,
    });
    expect(useFinalReviewStore.getState().operation).toBe('repair');
    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toBeNull();
    expect(repair).toHaveBeenCalledOnce();
    useFinalReviewStore.getState().finishRepairNavigation();
    expect(useFinalReviewStore.getState().operation).toBeNull();
    expect(useFinalReviewStore.getState().items[2]?.verdict).toBe('issues');
  });

  it('fail-closes unknown repair and export outcomes before they can be repeated', async () => {
    const { items } = seedFinalReview();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: '译意不准' },
    });
    const repair = vi.spyOn(api, 'beginFinalReviewRepair').mockRejectedValue(new Error('repair response lost'));
    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toBeNull();
    expect(useFinalReviewStore.getState().conflict).toBe(true);
    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toBeNull();
    expect(repair).toHaveBeenCalledOnce();

    resetFinalReviewStore();
    seedAllApproved();
    const exportApproved = vi.spyOn(api, 'exportApprovedFinalReviewItems').mockRejectedValue(
      new Error('export response lost'),
    );
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/unknown-export', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(useFinalReviewStore.getState().conflict).toBe(true);
    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/unknown-export', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(exportApproved).toHaveBeenCalledOnce();
  });

  it('fail-closes malformed successful repair and export responses as unknown outcomes', async () => {
    const { items } = seedFinalReview();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: issue.feedback },
    });
    vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue({
      itemId: issue.id,
    } as Awaited<ReturnType<typeof api.beginFinalReviewRepair>>);

    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toBeNull();
    expect(useFinalReviewStore.getState()).toMatchObject({ conflict: true, operation: null });
    expect(useFinalReviewStore.getState().error).toContain('操作结果未知');

    resetFinalReviewStore();
    seedAllApproved();
    vi.spyOn(api, 'exportApprovedFinalReviewItems').mockResolvedValue({
      batchId: 'final-batch-1',
    } as Awaited<ReturnType<typeof api.exportApprovedFinalReviewItems>>);

    await expect(useFinalReviewStore.getState().exportApproved({
      outputPath: '/malformed-export', conflict: 'rename', preserveTree: true,
    })).resolves.toBe(false);
    expect(useFinalReviewStore.getState()).toMatchObject({ conflict: true, operation: null });
    expect(useFinalReviewStore.getState().error).toContain('操作结果未知');
  });

  it('fail-closes complete repair handoffs with forged target or fresh-G0 semantics', async () => {
    const invalidPatches = [
      (_item: FinalReviewItem) => ({ repairProjectId: 'different-project' }),
      (item: FinalReviewItem) => ({ repairImageId: item.sourceImageId }),
      (item: FinalReviewItem) => ({ runId: `final-review-${item.id.slice(0, 8)}-r999` }),
      () => ({ nextSequence: 3 }),
      () => ({ parameterSetId: 'forged-repair-parameters' }),
      () => ({ parameterSetHash: 'a'.repeat(64) }),
    ];
    const repair = vi.spyOn(api, 'beginFinalReviewRepair');

    for (const invalidPatch of invalidPatches) {
      resetFinalReviewStore();
      const { batch, items } = seedFinalReview();
      const issue = items[2]!;
      const draft = { verdict: 'issues' as const, issueCodes: ['translation'] as const, feedback: issue.feedback };
      useFinalReviewStore.setState({
        activeItemId: issue.id,
        draft: { ...draft, issueCodes: [...draft.issueCodes] },
      });
      repair.mockResolvedValueOnce(repairResult(issue, invalidPatch(issue)));

      await expect(useFinalReviewStore.getState().beginRepair()).resolves.toBeNull();
      expect(useFinalReviewStore.getState()).toMatchObject({
        batch,
        items,
        draft: { ...draft, issueCodes: [...draft.issueCodes] },
        conflict: true,
        operation: null,
        repairContext: null,
      });
      expect(useFinalReviewStore.getState().items[2]?.verdict).toBe('issues');
      expect(useFinalReviewStore.getState().error).toContain('操作结果未知');
    }
    expect(repair).toHaveBeenCalledTimes(invalidPatches.length);
  });

  it('accepts an idempotent authoritative repair handoff after later lineage events', async () => {
    const { items } = seedFinalReview();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['translation'], feedback: issue.feedback },
    });
    vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue(repairResult(issue, {
      idempotent: true,
      nextSequence: 5,
    }));

    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toMatchObject({
      nextSequence: 5,
      repairProjectId: issue.sourceProjectId,
      repairImageId: `repair-${issue.sourceImageId}`,
    });
    expect(useFinalReviewStore.getState()).toMatchObject({ conflict: false, operation: 'repair' });
  });

  it('keeps legacy reviewed verdicts read-only while allowing a v1 issues item to start repair', async () => {
    const legacy = finalReviewItemFixture('legacy-approved', { formatVersion: 1, strictEvidence: false, verdict: 'approved' });
    const batch = finalReviewBatchFixture([legacy]);
    useFinalReviewStore.setState({
      batches: [batch], batch, items: [legacy], activeItemId: legacy.id,
      draft: { verdict: 'approved', issueCodes: [], feedback: '' },
    });
    useFinalReviewStore.getState().updateDraft({ verdict: 'issues' });
    expect(useFinalReviewStore.getState().draft?.verdict).toBe('approved');
    await expect(useFinalReviewStore.getState().save()).resolves.toBe(false);
    await expect(useFinalReviewStore.getState().refreshActive()).resolves.toBe(false);

    const issue = finalReviewItemFixture('legacy-issue', { formatVersion: 1, strictEvidence: false, verdict: 'issues', issueCodes: ['mask'] });
    const issueBatch = finalReviewBatchFixture([issue]);
    useFinalReviewStore.setState({
      batch: issueBatch, items: [issue], activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: ['mask'], feedback: '' },
    });
    useFinalReviewStore.getState().updateDraft({ verdict: 'approved', feedback: 'forbidden edit' });
    expect(useFinalReviewStore.getState().draft).toEqual({ verdict: 'issues', issueCodes: ['mask'], feedback: '' });
    vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue(repairResult(issue, {
      pageGenerationId: 'generation-v2',
    }));
    await expect(useFinalReviewStore.getState().beginRepair()).resolves.toMatchObject({
      pageGenerationId: 'generation-v2', itemRevision: 1,
    });
    expect(useFinalReviewStore.getState().items[0]?.verdict).toBe('issues');
  });
});
