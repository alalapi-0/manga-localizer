import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, api } from '../api/client';
import type {
  CleanPlateGateContext,
  MaskGateContext,
  OCRAttempt,
  PageGeneration,
  PageLineageEvent,
  TypesetGateContext,
  TypesetRegionStyle,
} from '../types';
import {
  CLEAN_PLATE_CHECKS,
  MASK_COLLATERAL_CHECKS,
  MASK_COVERAGE_CHECKS,
  OCR_QC_CHECKS,
  TRANSLATION_QC_CHECKS,
  TYPESET_CHECKS,
} from '../types';
import {
  imageFixture,
  jobFixture,
  projectFixture,
  regionFixture,
  seedWorkbench,
} from '../test/fixtures';
import {
  type G4PageContext,
  activeRegions,
  canNavigateAdjacent,
  deriveWorkflowPhase,
  g4EditingLocked,
  g7MaskDraftChecksum,
  imageReviewState,
  latestPageProcessingActivity,
  latestPageProcessingError,
  matchingQueueJob,
  ocrSourceReviewRequired,
  overflowingRegionIds,
  resetWorkbenchStore,
  useWorkbenchStore,
  visibleImagePosition,
  workflowPhase,
} from './workbench';

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((next, fail) => {
    resolve = next;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function stageObservation(
  stage: 'preprocess' | 'inpaint' | 'typeset',
  revision = 7,
) {
  return {
    imageId: 'image-1',
    stage,
    revision,
    artifactChecksum: 'a'.repeat(64),
    ...(stage === 'inpaint' ? { maskChecksum: 'b'.repeat(64) } : {}),
  };
}

function pageGeneration(nextSequence = 8, overrides: Partial<PageGeneration> = {}): PageGeneration {
  return {
    id: 'generation-1',
    runId: 'run-1',
    projectId: 'project-1',
    imageId: 'image-1',
    restartFromSource: true,
    parameterSetId: 'params-1',
    parameterSetHash: 'a'.repeat(64),
    sourceProjectId: 'project-1',
    sourceImageId: 'image-1',
    sourceChecksum: 'b'.repeat(64),
    state: 'active',
    nextSequence,
    actor: { actorKind: 'codex', taskId: 'task-1', operationSource: 'api' },
    createdAt: '2026-08-25T00:00:00Z',
    closedAt: null,
    ...overrides,
  };
}

function pageEvent(
  sequence = 7,
  operation = 'detect-job-completed',
  overrides: Partial<PageLineageEvent> = {},
): PageLineageEvent {
  return {
    id: `event-${sequence}`,
    generationId: 'generation-1',
    sequence,
    operation,
    gate: 'G4_regions',
    state: 'pending',
    actor: { actorKind: 'system', actorId: 'queue', operationSource: 'api' },
    inputChecksum: 'c'.repeat(64),
    outputChecksum: 'd'.repeat(64),
    parentChecksum: 'c'.repeat(64),
    stage: 'detection',
    provider: 'tesseract',
    modelVersion: null,
    parameterHash: 'a'.repeat(64),
    jobId: 'job-detect',
    jobItemId: 'item-detect',
    revisionId: null,
    decision: 'job-completed',
    reason: 'job-completed',
    gitCommit: null,
    evidence: { targetKind: 'region-set' },
    startedAt: '2026-08-25T00:00:00Z',
    finishedAt: '2026-08-25T00:00:01Z',
    createdAt: '2026-08-25T00:00:01Z',
    ...overrides,
  };
}

function seedActiveG4(nextSequence = 8) {
  const region = regionFixture('region-1', {
    order: 0,
    paragraphGroupId: 'paragraph-1',
    contentDisposition: 'translate',
    sourceText: '',
    translationText: '',
    confidence: null,
    ignored: false,
    confirmed: false,
  });
  seedWorkbench({
    images: [imageFixture('image-1', { revision: 10, regionCount: 1 })],
    regions: [region],
  });
  useWorkbenchStore.setState({
    g4Contexts: {
      'image-1': {
        status: 'active',
        generation: pageGeneration(nextSequence),
        events: [pageEvent(nextSequence - 1)],
        error: '',
        conflict: false,
      },
    },
  });
  return region;
}

function seedActiveG5(options: { eligible?: boolean; classified?: boolean } = {}) {
  const eligible = options.eligible ?? true;
  const classified = options.classified ?? false;
  const reviewer = {
    actorKind: 'human' as const,
    sessionId: 'review-session',
    operationSource: 'ui' as const,
  };
  const region = regionFixture('region-1', {
    order: 0,
    paragraphGroupId: 'paragraph-1',
    contentDisposition: eligible ? 'translate' : 'keep-art',
    sourceText: '',
    translationText: '',
    confidence: null,
    ...(classified
      ? {
          backgroundCategory: 'white-solid' as const,
          backgroundConfidence: 0,
          backgroundRationaleCodes: ['uniform-near-white' as const],
          backgroundReviewer: reviewer,
          backgroundGenerationId: 'generation-1',
        }
      : {}),
  });
  seedWorkbench({
    images: [imageFixture('image-1', { revision: 10, regionCount: 1 })],
    regions: [region],
  });
  useWorkbenchStore.setState({
    g4Contexts: {
      'image-1': {
        status: 'active',
        generation: pageGeneration(8),
        events: [pageEvent(7, 'regions-stage-review', {
          state: 'accepted',
          outputChecksum: 'e'.repeat(64),
        })],
        phase: 'G5',
        error: '',
        conflict: false,
      },
    },
    backgroundContexts: {
      'image-1': {
        imageId: 'image-1',
        imageRevision: 10,
        generationId: 'generation-1',
        nextSequence: 8,
        g4Checksum: 'e'.repeat(64),
        backgroundChecksum: 'f'.repeat(64),
        state: 'pending',
        eligibleRegionIds: eligible ? ['region-1'] : [],
        classifiedRegionIds: classified ? ['region-1'] : [],
      },
    },
  });
  return region;
}

function ocrAttempt(
  inputVariant: 'original' | 'quality',
  confidence: number | null = inputVariant === 'original' ? 0 : 0.8,
): OCRAttempt {
  return {
    id: `attempt-${inputVariant}`,
    regionId: 'region-1',
    generationId: 'generation-1',
    jobId: 'job-ocr',
    jobItemId: 'item-ocr',
    inputVariant,
    parentChecksum: (inputVariant === 'original' ? 'b' : 'c').repeat(64),
    cropChecksum: (inputVariant === 'original' ? 'd' : 'e').repeat(64),
    cropBox: { x: 10, y: 20, width: 120, height: 80 },
    provider: 'tesseract',
    modelVersion: 'tesseract-test-v1',
    parameterHash: 'a'.repeat(64),
    language: 'jpn_vert',
    direction: 'vertical',
    text: inputVariant === 'original' ? '原文' : '原文。',
    textChecksum: (inputVariant === 'original' ? '6' : '7').repeat(64),
    confidence,
    createdAt: '2026-08-25T00:00:01Z',
  };
}

function g6BaseEvents(includeAttempts = true, includeReview = false): PageLineageEvent[] {
  const g4 = pageEvent(7, 'regions-stage-review', {
    state: 'accepted', outputChecksum: 'e'.repeat(64),
  });
  const g5 = pageEvent(8, 'background-stage-review', {
    gate: 'G5_background',
    state: 'accepted',
    decision: 'backgrounds-accepted',
    inputChecksum: 'f'.repeat(64),
    outputChecksum: 'f'.repeat(64),
    parentChecksum: 'e'.repeat(64),
  });
  if (!includeAttempts) return [g4, g5];
  const enqueue = pageEvent(9, 'ocr-job-enqueued', {
    gate: 'G6_ocr',
    state: 'pending',
    stage: 'ocr',
    inputChecksum: '1'.repeat(64),
    outputChecksum: '1'.repeat(64),
    parentChecksum: 'f'.repeat(64),
    provider: 'tesseract',
    jobId: 'job-ocr',
    jobItemId: 'item-ocr',
    decision: null,
    reason: 'job-enqueued',
    evidence: {
      eventType: 'job-enqueued',
      qualityState: 'pending-review',
      targetKind: 'region-set',
      eligibleRegionCount: 1,
    },
  });
  const produced = pageEvent(10, 'ocr-attempts-produced', {
    gate: 'G6_ocr',
    state: 'pending',
    stage: 'ocr',
    inputChecksum: '1'.repeat(64),
    outputChecksum: '2'.repeat(64),
    parentChecksum: 'f'.repeat(64),
    provider: 'tesseract',
    jobId: 'job-ocr',
    jobItemId: 'item-ocr',
    decision: null,
    reason: 'source-review-required',
    evidence: {
      eventType: 'ocr-attempts-produced',
      qualityState: 'pending-review',
      targetKind: 'region-set',
      regionCount: 1,
      eligibleRegionCount: 1,
      attemptedRegionCount: 1,
      ocrAttemptCount: 2,
    },
  });
  const completed = pageEvent(11, 'ocr-job-completed', {
    gate: 'G6_ocr',
    state: 'pending',
    stage: 'ocr',
    inputChecksum: '1'.repeat(64),
    outputChecksum: '2'.repeat(64),
    parentChecksum: 'f'.repeat(64),
    provider: 'tesseract',
    jobId: 'job-ocr',
    jobItemId: 'item-ocr',
    decision: null,
    reason: 'review-required',
    evidence: {
      eventType: 'job-completed',
      qualityState: 'pending-review',
      targetKind: 'image',
      eligibleRegionCount: 1,
      ocrAttemptCount: 2,
    },
  });
  if (!includeReview) return [g4, g5, enqueue, produced, completed];
  return [g4, g5, enqueue, produced, completed, pageEvent(12, 'ocr-source-reviewed', {
    gate: 'G6_ocr',
    state: 'pending',
    stage: 'ocr',
    inputChecksum: '2'.repeat(64),
    outputChecksum: '3'.repeat(64),
    parentChecksum: 'f'.repeat(64),
    jobId: null,
    jobItemId: null,
    provider: 'tesseract',
    modelVersion: null,
    decision: 'source-text-trusted',
    reason: 'quality-attempt',
    evidence: {
      eventType: 'ocr-source-reviewed',
      qualityState: 'pending-review',
      targetKind: 'region',
      targetRegionId: 'region-1',
      selectedAttemptId: 'attempt-quality',
      regionCount: 1,
      eligibleRegionCount: 1,
      attemptedRegionCount: 1,
      reviewedRegionCount: 1,
    },
  })];
}

function seedActiveG6(options: {
  eligible?: boolean;
  attempts?: boolean;
  reviewed?: boolean;
} = {}) {
  const eligible = options.eligible ?? true;
  const attempts = eligible && (options.attempts ?? true);
  const reviewed = attempts && (options.reviewed ?? false);
  const reviewer = {
    actorKind: 'human' as const,
    sessionId: 'server-reviewer',
    operationSource: 'ui' as const,
  };
  const region = regionFixture('region-1', {
    order: 0,
    paragraphGroupId: 'paragraph-1',
    contentDisposition: eligible ? 'translate' : 'keep-art',
    sourceText: reviewed ? '原文。' : '',
    translationText: '',
    backgroundCategory: eligible ? 'white-solid' : null,
    backgroundConfidence: eligible ? 0 : null,
    backgroundRationaleCodes: eligible ? ['uniform-near-white'] : null,
    backgroundReviewer: eligible ? reviewer : null,
    backgroundGenerationId: eligible ? 'generation-1' : null,
    ...(reviewed
      ? {
          ocrReview: {
            sourceMode: 'quality-attempt' as const,
            selectedAttemptId: 'attempt-quality',
            sourceTextChecksum: '8'.repeat(64),
            qcChecks: [...OCR_QC_CHECKS],
            qcFlags: ['original-quality-disagree' as const],
          },
          ocrReviewer: reviewer,
          ocrGenerationId: 'generation-1',
        }
      : {}),
  });
  const nextSequence = reviewed ? 13 : attempts ? 12 : 9;
  const events = eligible
    ? g6BaseEvents(attempts, reviewed)
    : [
        pageEvent(7, 'regions-stage-review', {
          state: 'accepted', outputChecksum: 'e'.repeat(64),
        }),
        pageEvent(8, 'background-stage-review', {
          gate: 'G5_background',
          state: 'not-applicable',
          decision: 'background-not-applicable',
          inputChecksum: 'f'.repeat(64),
          outputChecksum: 'f'.repeat(64),
          parentChecksum: 'e'.repeat(64),
        }),
      ];
  seedWorkbench({
    images: [imageFixture('image-1', { revision: 10, regionCount: 1 })],
    regions: [region],
  });
  useWorkbenchStore.setState({
    g4Contexts: {
      'image-1': {
        status: 'active',
        generation: pageGeneration(nextSequence),
        events,
        error: '',
        conflict: false,
      },
    },
    ocrContexts: {
      'image-1': {
        imageId: 'image-1',
        imageRevision: 10,
        generationId: 'generation-1',
        nextSequence,
        g5Checksum: 'f'.repeat(64),
        ocrChecksum: reviewed ? '3'.repeat(64) : attempts ? '2'.repeat(64) : '1'.repeat(64),
        state: 'pending',
        eligibleRegionIds: eligible ? ['region-1'] : [],
        attemptedRegionIds: attempts ? ['region-1'] : [],
        reviewedRegionIds: reviewed ? ['region-1'] : [],
        attempts: attempts ? [ocrAttempt('original', 0), ocrAttempt('quality', 0.2)] : [],
      },
    },
  });
  return region;
}

describe('workbench store', () => {
  beforeEach(() => {
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture(),
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images,
    );
    vi.spyOn(api, 'listJobs').mockImplementation(async () =>
      useWorkbenchStore.getState().jobs,
    );
    vi.spyOn(api, 'listPageGenerations').mockResolvedValue([]);
  });

  afterEach(() => {
    resetWorkbenchStore();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('creates a region in canonical image pixels and autosaves it after the debounce', async () => {
    vi.useFakeTimers();
    seedWorkbench({ regions: [] });
    const create = vi.spyOn(api, 'createRegion').mockImplementation(async (_imageId, region) => ({
      ...region,
      id: 'region-server',
      revision: 1,
    }));

    const localId = useWorkbenchStore.getState().createRegion({ x: 101.4, y: 202.6, width: 303.2, height: 99.8 });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      id: localId,
      x: 101,
      y: 203,
      width: 303,
      height: 100,
    });

    await vi.advanceTimersByTimeAsync(650);

    expect(create).toHaveBeenCalledOnce();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.id).toBe('region-server');
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
  });

  it('rebases an edit made while a new region is being created on the server', async () => {
    seedWorkbench({ regions: [] });
    const created = deferred<ReturnType<typeof regionFixture>>();
    const create = vi.spyOn(api, 'createRegion').mockReturnValue(created.promise);
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-server'),
      ...patch,
      revision: 2,
    }));

    const localId = useWorkbenchStore.getState().createRegion({ x: 10, y: 20, width: 120, height: 80 });
    const saving = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(create).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().updateRegion(localId!, { sourceText: '保存中补写' });
    created.resolve({
      ...regionFixture('region-server'),
      imageId: 'image-1',
      sourceText: '',
      translationText: '',
      revision: 1,
    });

    expect(await saving).toBe(true);
    expect(update).toHaveBeenCalledWith('region-server', expect.objectContaining({
      sourceText: '保存中补写',
      expectedRevision: 1,
    }));
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      id: 'region-server',
      sourceText: '保存中补写',
      revision: 2,
    });
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
  });

  it('deletes the server region when a local create is deleted in flight', async () => {
    seedWorkbench({ regions: [] });
    const created = deferred<ReturnType<typeof regionFixture>>();
    vi.spyOn(api, 'createRegion').mockReturnValue(created.promise);
    const remove = vi.spyOn(api, 'deleteRegion').mockResolvedValue();

    useWorkbenchStore.getState().createRegion({ x: 10, y: 20, width: 120, height: 80 });
    const saving = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(api.createRegion).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().deleteSelectedRegions();
    created.resolve({
      ...regionFixture('region-server'),
      imageId: 'image-1',
      revision: 1,
    });

    expect(await saving).toBe(true);
    expect(remove).toHaveBeenCalledWith('region-server', 1);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([]);
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
  });

  it('undoes an in-flight create by deleting the created server region', async () => {
    seedWorkbench({ regions: [] });
    const created = deferred<ReturnType<typeof regionFixture>>();
    vi.spyOn(api, 'createRegion').mockReturnValue(created.promise);
    const remove = vi.spyOn(api, 'deleteRegion').mockResolvedValue();

    useWorkbenchStore.getState().createRegion({ x: 10, y: 20, width: 120, height: 80 });
    const saving = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(api.createRegion).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().undo();
    created.resolve({
      ...regionFixture('region-server'),
      imageId: 'image-1',
      revision: 1,
    });

    expect(await saving).toBe(true);
    expect(remove).toHaveBeenCalledWith('region-server', 1);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([]);
  });

  it('redoing an in-flight create undo keeps exactly one server region', async () => {
    seedWorkbench({ regions: [] });
    const created = deferred<ReturnType<typeof regionFixture>>();
    const create = vi.spyOn(api, 'createRegion').mockReturnValue(created.promise);
    const remove = vi.spyOn(api, 'deleteRegion').mockResolvedValue();

    useWorkbenchStore.getState().createRegion({ x: 10, y: 20, width: 120, height: 80 });
    const localRegion = structuredClone(
      useWorkbenchStore.getState().regionsByImage['image-1']?.[0],
    );
    const saving = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(create).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().undo();
    useWorkbenchStore.getState().redo();
    created.resolve({
      ...localRegion!,
      id: 'region-server',
      revision: 1,
    });

    expect(await saving).toBe(true);
    expect(create).toHaveBeenCalledOnce();
    expect(remove).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([
      expect.objectContaining({ id: 'region-server' }),
    ]);
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
  });

  it('skips a queued create removed while an earlier create is in flight', async () => {
    seedWorkbench({ regions: [] });
    const firstResponse = deferred<ReturnType<typeof regionFixture>>();
    let call = 0;
    const create = vi.spyOn(api, 'createRegion').mockImplementation(async (_imageId, region) => {
      call += 1;
      if (call === 1) return firstResponse.promise;
      return { ...region, id: 'region-server-b', revision: 1 };
    });
    const remove = vi.spyOn(api, 'deleteRegion').mockResolvedValue();

    const firstId = useWorkbenchStore.getState().createRegion({ x: 10, y: 20, width: 120, height: 80 });
    useWorkbenchStore.getState().createRegion({ x: 200, y: 20, width: 120, height: 80 });
    const saving = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    useWorkbenchStore.getState().deleteSelectedRegions();
    firstResponse.resolve({
      ...regionFixture('region-server-a'),
      imageId: 'image-1',
      revision: 1,
    });

    expect(await saving).toBe(true);
    expect(create).toHaveBeenCalledOnce();
    expect(remove).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([
      expect.objectContaining({ id: 'region-server-a' }),
    ]);
    expect(firstId).toMatch(/^local-/);
  });

  it('does not retain a local delete for a queued create after an earlier create fails', async () => {
    seedWorkbench({ regions: [] });
    const firstResponse = deferred<ReturnType<typeof regionFixture>>();
    let call = 0;
    const create = vi.spyOn(api, 'createRegion').mockImplementation(async (_imageId, region) => {
      call += 1;
      if (call === 1) return firstResponse.promise;
      return { ...region, id: 'region-server-a', revision: 1 };
    });
    const remove = vi.spyOn(api, 'deleteRegion').mockResolvedValue();

    const firstId = useWorkbenchStore.getState().createRegion({
      x: 10,
      y: 20,
      width: 120,
      height: 80,
    });
    const secondId = useWorkbenchStore.getState().createRegion({
      x: 200,
      y: 20,
      width: 120,
      height: 80,
    });
    const firstSave = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(create).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().undo();
    firstResponse.reject(new Error('第一个创建失败'));

    expect(await firstSave).toBe(false);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([
      expect.objectContaining({ id: firstId }),
    ]);
    expect(useWorkbenchStore.getState().pendingRegionMutations).toEqual([
      expect.objectContaining({ kind: 'create', region: expect.objectContaining({ id: firstId }) }),
    ]);
    expect(
      useWorkbenchStore.getState().pendingRegionMutations.some(
        (mutation) => mutation.region.id === secondId && mutation.kind === 'delete',
      ),
    ).toBe(false);

    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(create).toHaveBeenCalledTimes(2);
    expect(remove).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([
      expect.objectContaining({ id: 'region-server-a' }),
    ]);
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
  });

  it('returns a stable empty region snapshot for React external-store selectors', () => {
    seedWorkbench({ regions: [] });

    const first = activeRegions(useWorkbenchStore.getState());
    const second = activeRegions(useWorkbenchStore.getState());

    expect(first).toBe(second);
    expect(first).toEqual([]);
  });

  it('hydrates legacy repair aliases back into the editor model', async () => {
    seedWorkbench({ regions: [] });
    vi.spyOn(api, 'listRegions').mockResolvedValue([{
      ...regionFixture('region-1'),
      repair: {
        method: 'navier-stokes',
        padding: 7,
        dilation: 3,
        radius: 5,
        fillColor: '#eeeeee',
        textPolarity: 'unsupported',
      },
    } as unknown as ReturnType<typeof regionFixture>]);

    await useWorkbenchStore.getState().loadRegions('image-1', true);

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair).toMatchObject({
      method: 'navier_stokes',
      maskPadding: 7,
      dilation: 3,
      radius: 5,
      textPolarity: 'auto',
    });
  });

  it('hydrates the explicit screentone repair method without making it the default', async () => {
    seedWorkbench({ regions: [] });
    vi.spyOn(api, 'listRegions').mockResolvedValue([{
      ...regionFixture('region-1'),
      repair: {
        ...regionFixture('region-1').repair,
        method: 'screentone',
      },
    }]);

    await useWorkbenchStore.getState().loadRegions('image-1', true);

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair.method).toBe('screentone');
  });

  it('hydrates manual-only mask mode while unknown modes fail back to text', async () => {
    seedWorkbench({ regions: [] });
    vi.spyOn(api, 'listRegions').mockResolvedValue([
      {
        ...regionFixture('region-manual'),
        repair: {
          ...regionFixture('region-manual').repair,
          maskMode: 'manual',
        },
      },
      {
        ...regionFixture('region-unknown'),
        repair: {
          ...regionFixture('region-unknown').repair,
          maskMode: 'unknown',
        },
      } as unknown as ReturnType<typeof regionFixture>,
    ]);

    await useWorkbenchStore.getState().loadRegions('image-1', true);

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair.maskMode).toBe('manual');
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[1]?.repair.maskMode).toBe('text');
  });

  it('hydrates separate recognition evidence and fails legacy trust state closed', async () => {
    seedWorkbench({ regions: [] });
    const legacy = regionFixture('region-1', {
      detectorConfidence: undefined as unknown as null,
      ocrConfidence: undefined as unknown as null,
      trustDisposition: undefined as unknown as 'review',
      trustReason: '',
      trustPolicyVersion: undefined as unknown as number,
      recognition: undefined as unknown as Record<string, unknown>,
    });
    vi.spyOn(api, 'listRegions').mockResolvedValue([legacy]);

    await useWorkbenchStore.getState().loadRegions('image-1', true);

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      detectorConfidence: null,
      ocrConfidence: null,
      trustDisposition: 'review',
      trustReason: 'legacy-unverified',
      trustPolicyVersion: 1,
      recognition: {},
    });
  });

  it('lets an explicit ignore win over a corrupt simultaneous confirmation', async () => {
    seedWorkbench({ regions: [] });
    vi.spyOn(api, 'listRegions').mockResolvedValue([
      regionFixture('region-1', {
        confirmed: true,
        ignored: true,
        trustDisposition: 'trusted',
        trustReason: 'human-confirmed',
      }),
    ]);

    await useWorkbenchStore.getState().loadRegions('image-1', true);

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      confirmed: false,
      ignored: true,
      trustDisposition: 'ignored',
    });
  });

  it('fails contradictory and stale trust evidence closed during hydration', async () => {
    seedWorkbench({ regions: [] });
    vi.spyOn(api, 'listRegions').mockResolvedValue([
      regionFixture('bad-pair', {
        confirmed: true,
        trustDisposition: 'trusted',
        trustReason: 'automatic-proposal',
      }),
      regionFixture('ignored-mismatch', {
        confirmed: true,
        ignored: false,
        trustDisposition: 'ignored',
        trustReason: 'human-ignored',
      }),
      regionFixture('old-policy', {
        confirmed: true,
        trustDisposition: 'trusted',
        trustReason: 'human-confirmed',
        trustPolicyVersion: 2,
      }),
      regionFixture('valid-trust', {
        confirmed: true,
        trustDisposition: 'trusted',
        trustReason: 'human-confirmed',
        trustPolicyVersion: 1,
      }),
    ]);

    await useWorkbenchStore.getState().loadRegions('image-1', true);

    const regions = useWorkbenchStore.getState().regionsByImage['image-1'] ?? [];
    for (const regionId of ['bad-pair', 'ignored-mismatch', 'old-policy']) {
      expect(regions.find((region) => region.id === regionId)).toMatchObject({
        trustDisposition: 'review',
        trustReason: 'policy-version-changed',
        trustPolicyVersion: 1,
      });
    }
    expect(regions.find((region) => region.id === 'valid-trust')).toMatchObject({
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      trustPolicyVersion: 1,
    });
  });

  it('blocks trust-gated work until every active region is explicitly trusted', async () => {
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 1 })],
      regions: [
        regionFixture('region-1', { trustDisposition: 'trusted', trustReason: 'human-confirmed' }),
        regionFixture('region-2', { trustDisposition: 'review' }),
      ],
    });
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({ kind: 'translate' }));

    expect(await useWorkbenchStore.getState().startBatch(
      ['translate'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
    )).toBe(false);
    expect(startJob).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain('1 个 OCR 文本框待信任确认');
  });

  it('fails closed when the page trust aggregate is newer than loaded regions', async () => {
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 2 })],
      regions: [],
    });
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({ kind: 'translate' }));

    expect(await useWorkbenchStore.getState().startBatch(
      ['translate'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
    )).toBe(false);

    expect(startJob).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain('2 个 OCR 文本框待信任确认');
  });

  it('refreshes the authoritative page trust aggregate before downstream enqueue', async () => {
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 0 })],
      regions: [regionFixture('region-1', {
        trustDisposition: 'trusted',
        trustReason: 'human-confirmed',
      })],
    });
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', { revision: 8, trustReviewCount: 2 }),
    ]);
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({ kind: 'translate' }));

    expect(await useWorkbenchStore.getState().startBatch(
      ['translate'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
    )).toBe(false);

    expect(api.listImages).toHaveBeenCalledWith('project-1');
    expect(startJob).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().images[0]?.trustReviewCount).toBe(2);
    expect(useWorkbenchStore.getState().globalError).toContain('2 个 OCR 文本框需要重新确认');
  });

  it('aborts selected-region downstream work when its trust refresh fails', async () => {
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 0 })],
      regions: [regionFixture('region-1', {
        trustDisposition: 'trusted',
        trustReason: 'human-confirmed',
      })],
    });
    vi.spyOn(api, 'listRegions').mockRejectedValue(new Error('region refresh unavailable'));
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({ kind: 'translate' }));

    expect(await useWorkbenchStore.getState().startBatch(
      ['translate'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
      1,
      ['region-1'],
    )).toBe(false);

    expect(api.listRegions).toHaveBeenCalledWith('image-1');
    expect(startJob).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain('无法刷新所选文本框');
  });

  it('keeps fresh server trust counts when a forced region refresh fails', async () => {
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 0 })],
      regions: [regionFixture('region-1', {
        trustDisposition: 'trusted',
        trustReason: 'human-confirmed',
      })],
    });
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', { revision: 9, trustReviewCount: 3 }),
    ]);
    vi.spyOn(api, 'listRegions').mockRejectedValue(new Error('region refresh unavailable'));

    await useWorkbenchStore.getState().reloadActiveImage();

    expect(useWorkbenchStore.getState().images[0]?.trustReviewCount).toBe(3);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.trustDisposition).toBe(
      'trusted',
    );
    expect(useWorkbenchStore.getState().globalError).toBe('region refresh unavailable');
  });

  it('rechecks the trust gate after saving authoritative server state', async () => {
    seedWorkbench({
      regions: [regionFixture('region-1', {
        trustDisposition: 'trusted',
        trustReason: 'human-confirmed',
      })],
    });
    vi.spyOn(api, 'updateRegion').mockImplementation(async (_regionId, patch) => ({
      ...regionFixture('region-1'),
      ...patch,
      trustDisposition: 'review',
      trustReason: 'trust-input-changed',
      confirmed: false,
      revision: 5,
    }));
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({ kind: 'translate' }));
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '保存后由后端降级' });

    expect(await useWorkbenchStore.getState().startBatch(
      ['translate'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
    )).toBe(false);

    expect(api.updateRegion).toHaveBeenCalledOnce();
    expect(startJob).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain('保存后所选范围有 1 个');
  });

  it('sends expected revision on edit and delete', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      ...patch,
      revision: 5,
    }));
    const remove = vi.spyOn(api, 'deleteRegion').mockResolvedValue();

    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '新译文' });
    await useWorkbenchStore.getState().flushAutosave();
    expect(update).toHaveBeenCalledWith('region-1', expect.objectContaining({
      translationText: '新译文',
      expectedRevision: 4,
    }));

    useWorkbenchStore.getState().selectRegion('region-1');
    useWorkbenchStore.getState().deleteSelectedRegions();
    await useWorkbenchStore.getState().flushAutosave();
    expect(remove).toHaveBeenCalledWith('region-1', 5);
  });

  it('keeps legacy hydration defaults out of ordinary sparse updates', async () => {
    seedWorkbench({ regions: [] });
    vi.spyOn(api, 'listRegions').mockResolvedValue([{
      ...regionFixture('region-1'),
      style: {},
      repair: {},
    } as ReturnType<typeof regionFixture>]);
    await useWorkbenchStore.getState().loadRegions('image-1', true);
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      translationText: patch.translationText ?? regionFixture('region-1').translationText,
      revision: 5,
    }));

    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '仅保存这个字段' });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(update).toHaveBeenCalledWith('region-1', {
      translationText: '仅保存这个字段',
      expectedRevision: 4,
    });
  });

  it('serializes an optional repair override deletion as a JSON null tombstone', async () => {
    const overridden = regionFixture('region-1', {
      repair: { ...regionFixture('region-1').repair, inpainterProvider: 'lama-onnx' },
    });
    seedWorkbench({ regions: [overridden] });
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => {
      const repair = { ...overridden.repair };
      if (patch.repair?.inpainterProvider === null) delete repair.inpainterProvider;
      return { ...overridden, repair, revision: 5 };
    });

    useWorkbenchStore.getState().updateRegion('region-1', {
      repair: { inpainterProvider: undefined },
    });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(update).toHaveBeenCalledWith('region-1', {
      repair: { inpainterProvider: null },
      expectedRevision: 4,
    });
    expect(
      useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair.inpainterProvider,
    ).toBeUndefined();
  });

  it('uses a tombstone when undo removes a newly added optional repair override', async () => {
    seedWorkbench();
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => {
      const repair = { ...regionFixture('region-1').repair };
      if (patch.repair?.inpainterProvider !== null && patch.repair?.inpainterProvider) {
        repair.inpainterProvider = patch.repair.inpainterProvider;
      }
      return { ...regionFixture('region-1'), repair, revision: 5 };
    });

    useWorkbenchStore.getState().updateRegion('region-1', {
      repair: { inpainterProvider: 'lama-onnx' },
    });
    useWorkbenchStore.getState().undo();
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(update).toHaveBeenCalledWith('region-1', {
      repair: { inpainterProvider: null },
      expectedRevision: 4,
    });
  });

  it('merges consecutive nested edits into one sparse update without regressing create or confirm', async () => {
    seedWorkbench({ regions: [regionFixture('region-1', {
      confirmed: true,
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
    })] });
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      style: { ...regionFixture('region-1').style, ...patch.style },
      repair: { ...regionFixture('region-1').repair, ...patch.repair },
      confirmed: patch.confirmed ?? false,
      revision: 5,
    }));

    useWorkbenchStore.getState().updateRegion('region-1', { style: { fontSize: 32 } });
    useWorkbenchStore.getState().updateRegion('region-1', { repair: { textPolarity: 'dark' } });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(update).toHaveBeenCalledWith('region-1', {
      style: { fontSize: 32 },
      repair: { textPolarity: 'dark' },
      confirmed: false,
      expectedRevision: 4,
    });

    seedWorkbench({ regions: [] });
    const create = vi.spyOn(api, 'createRegion').mockImplementation(async (_imageId, region) => ({
      ...region,
      id: 'created-region',
      revision: 1,
    }));
    useWorkbenchStore.getState().createRegion({ x: 10, y: 20, width: 80, height: 60 });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(create).toHaveBeenCalledOnce();

    seedWorkbench({ regions: [regionFixture('region-1', {
      confirmed: false,
      trustDisposition: 'review',
      trustReason: 'trust-input-changed',
    })] });
    update.mockResolvedValue({
      ...regionFixture('region-1'),
      confirmed: true,
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      revision: 5,
    });
    expect(await useWorkbenchStore.getState().setRegionConfirmed('region-1', true)).toBe(true);
    expect(update).toHaveBeenLastCalledWith('region-1', {
      confirmed: true,
      expectedRevision: 4,
    });
  });

  it('consolidates overlapping page boxes and expands the survivor', () => {
    seedWorkbench({
      regions: [
        regionFixture('region-1'),
        regionFixture('region-2', { x: 140, y: 140, width: 200, height: 100 }),
      ],
    });
    expect(useWorkbenchStore.getState().consolidateActiveImageRegions()).toBeGreaterThan(0);
    const regions = useWorkbenchStore.getState().regionsByImage['image-1'] ?? [];
    expect(regions).toHaveLength(1);
    expect(regions[0]?.width).toBeGreaterThan(220);
    expect(regions[0]?.height).toBeGreaterThan(120);
  });

  it('queues a manual AI redraw with the local 4x upscaler', async () => {
    seedWorkbench();
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-ai-redraw',
      kind: 'preprocess',
    }));
    expect(await useWorkbenchStore.getState().startBatch(
      ['preprocess'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
      1,
      undefined,
      {
        profile: 'visual-quality',
        enableUpscale: true,
        upscaleFactor: 4,
        enableDenoise: true,
        enableSharpen: true,
        enableContrastEnhance: true,
        enableEdgeOptimize: false,
        enableBinarize: false,
        threshold: 180,
      },
      'realesrgan-onnx',
    )).toBe(true);
    expect(startJob).toHaveBeenCalledWith('project-1', 'preprocess', {
      imageIds: ['image-1'],
      options: {
        provider: 'realesrgan-onnx',
        preprocessing: expect.objectContaining({
          profile: 'visual-quality',
          upscaleFactor: 4,
          enableUpscale: true,
        }),
        concurrency: 1,
      },
    });
  });

  it('nudges selected boxes in image pixels and clamps to the page', () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    useWorkbenchStore.getState().nudgeSelectedRegions(12, -8);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      x: regionFixture('region-1').x + 12,
      y: regionFixture('region-1').y - 8,
    });
    useWorkbenchStore.getState().nudgeSelectedRegions(-10_000, 10_000);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      x: 0,
      y: 1800 - regionFixture('region-1').height,
    });
  });

  it('immediately clears stale confirmation for every substantive region edit and autosaves false', async () => {
    const confirmed = regionFixture('region-1', { confirmed: true });
    const cases: Array<[string, Partial<ReturnType<typeof regionFixture>>]> = [
      ['geometry', { x: confirmed.x + 5 }],
      ['text', { sourceText: '修改后的原文' }],
      ['style', { style: { ...confirmed.style, fontSize: confirmed.style.fontSize + 2 } }],
      ['repair', {
        repair: {
          ...confirmed.repair,
          maskEdits: {
            version: 1,
            strokes: [{ mode: 'add', radius: 9, points: [[130, 150], [150, 170]] }],
          },
        },
      }],
      ['ignored', { ignored: true }],
    ];

    for (const [label, patch] of cases) {
      seedWorkbench({
        regions: [confirmed],
        images: [imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, reviewState: 'reviewed' },
        })],
      });
      useWorkbenchStore.getState().updateRegion('region-1', patch);

      expect(
        useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.confirmed,
        label,
      ).toBe(false);
      expect(useWorkbenchStore.getState().images[0]?.status.reviewState, label).toBe('pending');
      expect(
        useWorkbenchStore.getState().pendingRegionMutations[0]?.region.confirmed,
        label,
      ).toBe(false);
    }

    seedWorkbench({ regions: [confirmed] });
    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '需要撤销的编辑' });
    useWorkbenchStore.getState().undo();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      sourceText: confirmed.sourceText,
      confirmed: false,
    });

    seedWorkbench({ regions: [confirmed] });
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...confirmed,
      ...patch,
      revision: 5,
    }));
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '新的译文' });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(update).toHaveBeenCalledWith('region-1', expect.objectContaining({
      translationText: '新的译文',
      confirmed: false,
      expectedRevision: 4,
    }));
  });

  it('reconfirms a stale page flag with a sparse write and requires trusted server state', async () => {
    vi.useFakeTimers();
    const confirmed = regionFixture('region-1', {
      confirmed: true,
      trustDisposition: 'review',
      trustReason: 'trust-input-changed',
    });
    seedWorkbench({ regions: [confirmed] });
    const update = vi.spyOn(api, 'updateRegion').mockResolvedValue({
      ...confirmed,
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      revision: 5,
    });

    expect(await useWorkbenchStore.getState().setRegionConfirmed('region-1', true)).toBe(true);
    expect(update).toHaveBeenCalledOnce();
    expect(update).toHaveBeenCalledWith('region-1', {
      confirmed: true,
      expectedRevision: 4,
    });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      confirmed: true,
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      revision: 5,
    });
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('treats a semantic type edit as style-only for trust while staling confirmation', () => {
    const confirmed = regionFixture('region-1', {
      confirmed: true,
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      type: 'dialogue',
    });
    seedWorkbench({
      regions: [confirmed],
      images: [imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, reviewState: 'reviewed' },
      })],
    });

    useWorkbenchStore.getState().updateRegion('region-1', { type: 'sound_effect' });

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      type: 'sound_effect',
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      confirmed: false,
    });
    expect(useWorkbenchStore.getState().images[0]?.status.reviewState).toBe('pending');
    expect(useWorkbenchStore.getState().pendingRegionMutations[0]?.region).toMatchObject({
      trustDisposition: 'trusted',
      confirmed: false,
    });
  });

  it('does not report reconfirmation success when the server keeps the region in review', async () => {
    const stale = regionFixture('region-1', {
      confirmed: true,
      trustDisposition: 'review',
    });
    seedWorkbench({ regions: [stale] });
    vi.spyOn(api, 'updateRegion').mockResolvedValue({ ...stale, revision: 5 });

    expect(await useWorkbenchStore.getState().setRegionConfirmed('region-1', true)).toBe(false);
    expect(api.updateRegion).toHaveBeenCalledWith('region-1', {
      confirmed: true,
      expectedRevision: 4,
    });
  });

  it('restores page confirmation with a sparse write when OCR trust already remains valid', async () => {
    const trusted = regionFixture('region-1', {
      confirmed: false,
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
    });
    seedWorkbench({ regions: [trusted] });
    const update = vi.spyOn(api, 'updateRegion').mockResolvedValue({
      ...trusted,
      confirmed: true,
      revision: 5,
    });

    expect(await useWorkbenchStore.getState().setRegionConfirmed('region-1', true)).toBe(true);
    expect(update).toHaveBeenCalledWith('region-1', {
      confirmed: true,
      expectedRevision: 4,
    });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      confirmed: true,
      trustDisposition: 'trusted',
    });
  });

  it('keeps a region unconfirmed and surfaces the error when sparse reconfirmation fails', async () => {
    seedWorkbench({ regions: [regionFixture('region-1', { confirmed: false })] });
    vi.spyOn(api, 'updateRegion').mockRejectedValue(new Error('确认写入失败'));

    expect(await useWorkbenchStore.getState().setRegionConfirmed('region-1', true)).toBe(false);

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.confirmed).toBe(false);
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
    expect(useWorkbenchStore.getState().saveError).toBe('确认写入失败');
  });

  it('rejects page review states that do not match active region eligibility', async () => {
    seedWorkbench({
      regions: [
        regionFixture('region-1', { confirmed: false, trustDisposition: 'trusted' }),
        regionFixture('region-2', { confirmed: true, trustDisposition: 'review' }),
      ],
    });
    const review = vi.spyOn(api, 'reviewImage');

    expect(await useWorkbenchStore.getState().reviewActiveImage('reviewed')).toBe(false);
    expect(useWorkbenchStore.getState().globalError).toBe(
      '还有 2 个活动文本框尚未确认并信任。',
    );
    expect(await useWorkbenchStore.getState().reviewActiveImage('no-text-reviewed')).toBe(false);
    expect(review).not.toHaveBeenCalled();
  });

  it('rejects page review when the server trust aggregate is newer than loaded regions', async () => {
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 2 })],
      regions: [],
    });
    const review = vi.spyOn(api, 'reviewImage');

    expect(await useWorkbenchStore.getState().reviewActiveImage('reviewed')).toBe(false);
    expect(useWorkbenchStore.getState().globalError).toBe(
      '还有 2 个活动文本框尚未确认并信任。',
    );
    expect(await useWorkbenchStore.getState().reviewActiveImage('no-text-reviewed')).toBe(false);
    expect(useWorkbenchStore.getState().globalError).toBe(
      '当前页仍有活动文本框，不能标记为“确认无文字”。',
    );
    expect(review).not.toHaveBeenCalled();
  });

  it('refuses a viewed artifact after autosave advances the image revision', async () => {
    const initial = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
    });
    seedWorkbench({ images: [initial], selectedRegionIds: ['region-1'] });
    const calls: string[] = [];
    vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => {
      calls.push('save');
      return { ...regionFixture('region-1'), ...patch, revision: 5 };
    });
    const afterSave = imageFixture('image-1', {
      revision: 8,
      status: { ...initial.status, inpaint: 'not_started' },
    });
    vi.mocked(api.listImages).mockResolvedValue([afterSave]);
    const review = vi.spyOn(api, 'reviewImageStage').mockImplementation(async () => {
      calls.push('review');
      return {
        ...afterSave,
        revision: 9,
        stageReviews: {
          inpaint: { state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 8, artifactChecksum: 'a'.repeat(64), maskChecksum: 'b'.repeat(64) },
        },
      };
    });
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '复核前保存' });

    expect(await useWorkbenchStore.getState().reviewActiveImageStage(
      'inpaint',
      'accepted',
      stageObservation('inpaint'),
    )).toBe(false);
    expect(calls).toEqual(['save']);
    expect(review).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().images[0]).toMatchObject({
      revision: 8,
      status: { inpaint: 'not_started' },
    });
    expect(useWorkbenchStore.getState().globalError).toContain('已过期');
  });

  it('reloads authoritative stage-review state after a revision conflict', async () => {
    const initial = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, typeset: 'done' },
    });
    const authoritative = imageFixture('image-1', {
      revision: 8,
      status: { ...initial.status, typeset: 'done' },
      stageReviews: {
        typeset: { state: 'rejected', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'a'.repeat(64) },
      },
    });
    seedWorkbench({ images: [initial] });
    vi.spyOn(api, 'reviewImageStage').mockRejectedValueOnce(new ApiError('revision mismatch', 409));

    expect(await useWorkbenchStore.getState().reviewActiveImageStage(
      'typeset',
      'accepted',
      stageObservation('typeset'),
    )).toBe(false);
    expect(useWorkbenchStore.getState()).toMatchObject({
      revisionConflict: true,
      globalError: 'revision mismatch',
    });

    vi.mocked(api.listImages).mockResolvedValue([authoritative]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([]);
    await useWorkbenchStore.getState().reloadActiveImage();
    expect(useWorkbenchStore.getState()).toMatchObject({
      revisionConflict: false,
      globalError: '',
    });
    expect(useWorkbenchStore.getState().images[0]).toMatchObject({
      revision: 8,
      stageReviews: { typeset: { state: 'rejected' } },
    });
  });

  it('serializes visual-stage review mutations across stages', async () => {
    const initial = imageFixture('image-1', {
      revision: 7,
      status: {
        ...imageFixture('image-1').status,
        inpaint: 'done',
        typeset: 'done',
      },
    });
    seedWorkbench({ images: [initial] });
    let resolveReview: ((value: typeof initial) => void) | undefined;
    const firstReview = new Promise<typeof initial>((resolve) => {
      resolveReview = resolve;
    });
    const review = vi.spyOn(api, 'reviewImageStage').mockReturnValue(firstReview);

    const inpaintReview = useWorkbenchStore
      .getState()
      .reviewActiveImageStage('inpaint', 'accepted', stageObservation('inpaint'));
    await vi.waitFor(() => expect(review).toHaveBeenCalledTimes(1));
    expect(
      await useWorkbenchStore.getState().reviewActiveImageStage(
        'typeset',
        'accepted',
        stageObservation('typeset'),
      ),
    ).toBe(false);
    expect(review).toHaveBeenCalledTimes(1);

    resolveReview?.({
      ...initial,
      revision: 8,
      stageReviews: {
        inpaint: {
          state: 'accepted',
          reviewedAt: '2026-08-13T10:00:00Z',
          resultRevision: 7,
          artifactChecksum: 'a'.repeat(64),
          maskChecksum: 'b'.repeat(64),
        },
      },
    });
    expect(await inpaintReview).toBe(true);
    expect(useWorkbenchStore.getState().stageReviewSaving).toBeNull();
  });

  it('selects an inpaint candidate and replaces the current plate identity', async () => {
    const initial = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
      inpaintCandidate: 'primary',
      inpaintCandidates: [
        { id: 'primary', label: '当前 Provider 结果', anomalies: [], originKind: 'direct-ai' },
        { id: 'lineart-guided', label: '线稿引导(结构+纹理)', anomalies: [], originKind: 'ai-derived' },
      ],
    });
    seedWorkbench({ images: [initial] });
    const select = vi.spyOn(api, 'selectInpaintCandidate').mockResolvedValue({
      ...initial,
      revision: 8,
      inpaintCandidate: 'lineart-guided',
      stageReviews: {},
    });

    expect(await useWorkbenchStore.getState().selectInpaintCandidate('lineart-guided')).toBe(true);
    expect(select).toHaveBeenCalledWith('image-1', 'lineart-guided', 7);
    expect(useWorkbenchStore.getState().images[0]).toMatchObject({
      revision: 8,
      inpaintCandidate: 'lineart-guided',
    });
  });

  it('flushes pending edits and approves a classical fallback with the image revision guard', async () => {
    const initial = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
      inpaintCandidate: 'classical-clean',
      inpaintCandidateGenerationId: 'generation-a',
      inpaintCandidates: [
        { id: 'ai-a', label: 'AI A', anomalies: [], originKind: 'direct-ai' },
        { id: 'classical-clean', label: '传统算法', anomalies: [], originKind: 'classical' },
      ],
    });
    seedWorkbench({ images: [initial] });
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      ...patch,
      revision: 5,
    }));
    const fallback = vi.spyOn(api, 'setInpaintClassicalFallback').mockResolvedValue({
      ...initial,
      revision: 8,
      inpaintFallback: {
        state: 'approved',
        reason: 'ai-visible-artifacts',
        rejectedAiCandidateIds: ['ai-a'],
      },
    });
    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '待保存修改' });

    expect(await useWorkbenchStore.getState().setActiveImageInpaintFallback('approved', {
      reason: 'ai-visible-artifacts',
    })).toBe(true);

    expect(update).toHaveBeenCalledOnce();
    expect(fallback).toHaveBeenCalledWith('image-1', 'approved', 7, {
      reason: 'ai-visible-artifacts',
    });
    expect(update.mock.invocationCallOrder[0]).toBeLessThan(fallback.mock.invocationCallOrder[0]!);
    expect(useWorkbenchStore.getState().images[0]).toMatchObject({
      revision: 8,
      inpaintFallback: { state: 'approved' },
    });
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
    expect(useWorkbenchStore.getState().stageReviewSaving).toBeNull();
  });

  it('flushes edits before persisting the selected AI candidate review', async () => {
    const initial = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
      inpaintCandidate: 'ai-a',
      inpaintCandidateGenerationId: 'generation-a',
      inpaintCandidates: [
        { id: 'ai-a', label: 'AI A', anomalies: [], originKind: 'direct-ai' },
        { id: 'classical-clean', label: '传统算法', anomalies: [], originKind: 'classical' },
      ],
    });
    seedWorkbench({ images: [initial] });
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      ...patch,
      revision: 5,
    }));
    const review = vi.spyOn(api, 'reviewSelectedInpaintAiCandidate').mockResolvedValue({
      ...initial,
      revision: 8,
      inpaintAiRejectedCandidateIds: ['ai-a'],
    });
    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '待保存修改' });

    expect(await useWorkbenchStore.getState().reviewSelectedInpaintAiCandidate('rejected')).toBe(true);

    expect(update).toHaveBeenCalledOnce();
    expect(review).toHaveBeenCalledWith('image-1', 'rejected', 7);
    expect(update.mock.invocationCallOrder[0]).toBeLessThan(review.mock.invocationCallOrder[0]!);
    expect(useWorkbenchStore.getState().images[0]?.inpaintAiRejectedCandidateIds).toEqual(['ai-a']);
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
    expect(useWorkbenchStore.getState().stageReviewSaving).toBeNull();
  });

  it('keeps selected AI review conflicts recoverable and serializes review mutations', async () => {
    seedWorkbench();
    const review = vi.spyOn(api, 'reviewSelectedInpaintAiCandidate').mockRejectedValue(
      new ApiError('页面版本已变化', 409),
    );

    expect(await useWorkbenchStore.getState().reviewSelectedInpaintAiCandidate('rejected')).toBe(false);
    expect(useWorkbenchStore.getState()).toMatchObject({
      globalError: '页面版本已变化',
      revisionConflict: true,
      stageReviewSaving: null,
    });

    useWorkbenchStore.setState({ stageReviewSaving: 'image-1:inpaint' });
    expect(await useWorkbenchStore.getState().reviewSelectedInpaintAiCandidate('pending')).toBe(false);
    expect(review).toHaveBeenCalledOnce();
  });

  it('keeps a classical fallback conflict recoverable and serializes it with visual mutations', async () => {
    seedWorkbench();
    const fallback = vi.spyOn(api, 'setInpaintClassicalFallback').mockRejectedValue(
      new ApiError('页面版本已变化', 409),
    );

    expect(await useWorkbenchStore.getState().setActiveImageInpaintFallback('pending')).toBe(false);
    expect(useWorkbenchStore.getState()).toMatchObject({
      globalError: '页面版本已变化',
      revisionConflict: true,
      stageReviewSaving: null,
    });

    useWorkbenchStore.setState({ stageReviewSaving: 'image-1:inpaint' });
    expect(await useWorkbenchStore.getState().setActiveImageInpaintFallback('pending')).toBe(false);
    expect(fallback).toHaveBeenCalledOnce();
  });

  it('clears optimistic visual reviews when project settings invalidate their artifacts', () => {
    const initial = imageFixture('image-1', {
      stageReviews: {
        preprocess: {
          state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'a'.repeat(64),
        },
        inpaint: {
          state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'b'.repeat(64), maskChecksum: 'c'.repeat(64),
        },
        typeset: {
          state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'd'.repeat(64),
        },
      },
    });
    seedWorkbench({ images: [initial] });

    useWorkbenchStore.getState().updateProjectSettings({ inpainterProvider: 'lama-onnx' });

    expect(useWorkbenchStore.getState().images[0]?.stageReviews).toEqual({
      preprocess: initial.stageReviews.preprocess,
    });
  });

  it('enables the strict AI clean-plate gate without invalidating an accepted artifact', () => {
    const initial = imageFixture('image-1', {
      stageReviews: {
        inpaint: {
          state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'b'.repeat(64), maskChecksum: 'c'.repeat(64),
        },
      },
    });
    seedWorkbench({ images: [initial] });

    useWorkbenchStore.getState().updateProjectSettings({
      requireAIInpaintBeforeDownstream: true,
    });

    expect(useWorkbenchStore.getState().images[0]?.stageReviews).toEqual(
      initial.stageReviews,
    );
  });

  it('does not wipe page pipeline status when only the detector default changes', () => {
    const initial = imageFixture('image-1', {
      status: {
        ...imageFixture('image-1').status,
        detection: 'done',
        ocr: 'done',
        inpaint: 'done',
        typeset: 'done',
      },
      stageReviews: {
        inpaint: {
          state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'b'.repeat(64), maskChecksum: 'c'.repeat(64),
        },
      },
    });
    seedWorkbench({ images: [initial] });

    useWorkbenchStore.getState().updateProjectSettings({ detectorProvider: 'ppocr-v3' });

    expect(useWorkbenchStore.getState().images[0]?.status).toMatchObject({
      detection: 'done',
      ocr: 'done',
      inpaint: 'done',
      typeset: 'done',
    });
    expect(useWorkbenchStore.getState().images[0]?.stageReviews).toEqual(initial.stageReviews);
  });

  it('reconciles image revision and invalidated render status after a region save', async () => {
    const rendered = imageFixture('image-1', {
      revision: 10,
      status: {
        ...imageFixture('image-1').status,
        inpaint: 'done',
        typeset: 'done',
        export: 'done',
      },
    });
    seedWorkbench({ images: [rendered], selectedRegionIds: ['region-1'] });
    vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      ...patch,
      revision: 5,
    }));
    vi.mocked(api.listImages).mockResolvedValue([imageFixture('image-1', {
      revision: 11,
      status: {
        ...rendered.status,
        inpaint: 'done',
        typeset: 'not_started',
        export: 'not_started',
      },
    })]);

    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '更新成品' });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(api.listImages).toHaveBeenCalledWith('project-1');
    expect(useWorkbenchStore.getState().images[0]).toMatchObject({
      revision: 11,
      status: { inpaint: 'done', typeset: 'not_started', export: 'not_started' },
    });
  });

  it('syncs the project revision after a saved region mutation', async () => {
    seedWorkbench({ regions: [regionFixture('region-1')] });
    vi.mocked(api.getProject).mockResolvedValue(projectFixture({ revision: 8 }));
    vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      ...patch,
      revision: 5,
    }));

    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '项目版本同步' });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(useWorkbenchStore.getState().currentProject?.revision).toBe(8);
    expect(useWorkbenchStore.getState().serverRegionRevisions['region-1']).toBe(5);
  });

  it('rebases pending project settings after upload without overwriting local values', async () => {
    seedWorkbench();
    useWorkbenchStore.getState().updateProjectSettings({ targetLanguage: 'ko' });
    vi.spyOn(api, 'uploadImages').mockResolvedValue([imageFixture('image-3')]);
    vi.mocked(api.getProject).mockResolvedValue(projectFixture({
      revision: 8,
      settings: { ...projectFixture().settings, targetLanguage: 'fr' },
    }));
    const updateProject = vi.spyOn(api, 'updateProject').mockImplementation(async (_id, patch) =>
      projectFixture({
        revision: 9,
        settings: { ...projectFixture().settings, ...patch.settings },
      }),
    );

    expect(await useWorkbenchStore.getState().importFiles([
      new File(['image'], 'image-3.png', { type: 'image/png' }),
    ])).toBe(true);
    expect(useWorkbenchStore.getState().currentProject).toMatchObject({
      revision: 8,
      settings: { targetLanguage: 'ko' },
    });
    expect(useWorkbenchStore.getState().pendingProjectMutation).toMatchObject({
      expectedRevision: 8,
      settings: { targetLanguage: 'ko' },
    });

    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(updateProject).toHaveBeenCalledWith('project-1', expect.objectContaining({
      expectedRevision: 8,
      settings: expect.objectContaining({ targetLanguage: 'ko' }),
    }));
    expect(useWorkbenchStore.getState().currentProject).toMatchObject({
      revision: 9,
      settings: { targetLanguage: 'ko' },
    });
  });

  it('rechecks every page and blocks image import when any project page has lineage', async () => {
    seedWorkbench();
    vi.mocked(api.listPageGenerations).mockImplementation(async (imageId) =>
      imageId === 'image-1' ? [pageGeneration()] : []
    );
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([pageEvent()]);
    const upload = vi.spyOn(api, 'uploadImages');

    expect(await useWorkbenchStore.getState().importFiles([
      new File(['image'], 'blocked.png', { type: 'image/png' }),
    ])).toBe(false);

    expect(upload).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain('不能再导入图像');
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.status).toBe('active');
  });

  it('keeps image import fail closed when lineage authority cannot be loaded', async () => {
    seedWorkbench();
    useWorkbenchStore.setState((state) => ({
      g4Contexts: {
        ...state.g4Contexts,
        'image-1': {
          status: 'error',
          generation: null,
          events: [],
          error: 'lineage unavailable',
          conflict: false,
        },
      },
    }));
    const upload = vi.spyOn(api, 'uploadImages');

    expect(await useWorkbenchStore.getState().importFiles([
      new File(['image'], 'blocked.png', { type: 'image/png' }),
    ])).toBe(false);

    expect(upload).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain('图像导入已锁定');
  });

  it('refreshes and retries a project settings save once when a background revision wins the race', async () => {
    seedWorkbench();
    useWorkbenchStore.getState().updateProjectSettings({ contextPages: 3 });
    vi.mocked(api.getProject)
      .mockResolvedValueOnce(projectFixture({ revision: 8 }))
      .mockResolvedValueOnce(projectFixture({ revision: 9 }));
    const updateProject = vi.spyOn(api, 'updateProject')
      .mockRejectedValueOnce(new ApiError('revision mismatch', 409))
      .mockImplementationOnce(async (_id, patch) => projectFixture({
        revision: 10,
        settings: { ...projectFixture().settings, ...patch.settings },
      }));

    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(updateProject).toHaveBeenNthCalledWith(1, 'project-1', expect.objectContaining({
      expectedRevision: 8,
    }));
    expect(updateProject).toHaveBeenNthCalledWith(2, 'project-1', expect.objectContaining({
      expectedRevision: 9,
      settings: expect.objectContaining({ contextPages: 3 }),
    }));
    expect(useWorkbenchStore.getState()).toMatchObject({
      revisionConflict: false,
      currentProject: { revision: 10, settings: { contextPages: 3 } },
    });
  });

  it('refreshes invalidated image stages after a settings-only save', async () => {
    const rendered = imageFixture('image-1', {
      revision: 10,
      status: {
        ...imageFixture('image-1').status,
        translation: 'done',
        inpaint: 'done',
        typeset: 'done',
        export: 'done',
        reviewState: 'reviewed',
        reviewedAt: '2026-08-10T10:00:00Z',
      },
    });
    seedWorkbench({ images: [rendered] });
    vi.spyOn(api, 'updateProject').mockImplementation(async (_id, patch) => projectFixture({
      revision: 4,
      settings: { ...projectFixture().settings, ...patch.settings },
    }));
    vi.mocked(api.listImages).mockResolvedValue([imageFixture('image-1', {
      revision: 11,
      status: {
        ...rendered.status,
        translation: 'not_started',
        typeset: 'not_started',
        export: 'not_started',
        reviewState: 'pending',
        reviewedAt: null,
      },
    })]);

    useWorkbenchStore.getState().updateProjectSettings({ targetLanguage: 'zh-TW' });
    expect(useWorkbenchStore.getState().images[0]).toMatchObject({
      status: {
        reviewState: 'pending',
        translation: 'not_started',
        typeset: 'not_started',
        export: 'not_started',
      },
    });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(api.listImages).toHaveBeenCalledWith('project-1');
    expect(useWorkbenchStore.getState().images[0]).toMatchObject({
      revision: 11,
      status: {
        reviewState: 'pending',
        translation: 'not_started',
        typeset: 'not_started',
        export: 'not_started',
      },
    });
  });

  it('flushes pending autosave before changing pages', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    const calls: string[] = [];
    vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => {
      calls.push('save');
      return { ...regionFixture('region-1'), ...patch, revision: 5 };
    });
    vi.spyOn(api, 'listRegions').mockImplementation(async () => {
      calls.push('load-next');
      return [];
    });
    useWorkbenchStore.setState((state) => ({
      regionsByImage: { ...state.regionsByImage, 'image-2': undefined as unknown as never },
    }));
    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '切页前保存' });

    const changed = await useWorkbenchStore.getState().selectImage('image-2');

    expect(changed).toBe(true);
    expect(calls).toEqual(['save', 'load-next']);
    expect(useWorkbenchStore.getState().activeImageId).toBe('image-2');
  });

  it('skips reviewed pages and jumps to overflowing pages', async () => {
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, reviewState: 'pending' },
        }),
        imageFixture('image-2', {
          status: {
            ...imageFixture('image-2').status,
            ocr: 'done',
            typeset: 'done',
            reviewState: 'reviewed',
          },
        }),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: {
            ...imageFixture('image-1').status,
            typeset: 'done',
            reviewState: 'pending',
          },
          typesetOverflowCount: 2,
          typesetOverflowRegionIds: ['region-9'],
        }),
      ],
    });
    useWorkbenchStore.setState((state) => ({
      regionsByImage: { ...state.regionsByImage, 'image-3': [] },
    }));
    vi.spyOn(api, 'listRegions').mockResolvedValue([]);

    expect(await useWorkbenchStore.getState().navigateImage(1, 'unreviewed')).toBe(true);
    expect(useWorkbenchStore.getState().activeImageId).toBe('image-3');

    useWorkbenchStore.setState({ activeImageId: 'image-1' });
    expect(await useWorkbenchStore.getState().navigateImage(1, 'overflow')).toBe(true);
    expect(useWorkbenchStore.getState().activeImageId).toBe('image-3');
  });

  it('frames overflow boxes when jumping to an overflowing page', async () => {
    const overflowRegion = regionFixture('region-9', { imageId: 'image-2' });
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-9'],
        }),
      ],
      regions: [regionFixture('region-1')],
    });
    useWorkbenchStore.setState((state) => ({
      regionsByImage: { ...state.regionsByImage, 'image-2': undefined as unknown as never },
    }));
    vi.spyOn(api, 'listRegions').mockResolvedValue([overflowRegion]);

    expect(await useWorkbenchStore.getState().navigateImage(1, 'overflow')).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-2',
      selectedRegionIds: ['region-9'],
      rightTab: 'typesetting',
      canvasMode: 'typeset',
      focusRegionIds: ['region-9'],
    });
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
  });

  it('frames overflow boxes when focusing overflow on the current page', async () => {
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-1'],
        }),
        imageFixture('image-2'),
      ],
    });
    useWorkbenchStore.setState({ canvasMode: 'original', rightTab: 'text' });

    expect(await useWorkbenchStore.getState().selectImage('image-1', { focusOverflow: true })).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-1',
      selectedRegionIds: ['region-1'],
      rightTab: 'typesetting',
      canvasMode: 'typeset',
      focusRegionIds: ['region-1'],
    });
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
  });

  it('opens the matching inspector when selecting a failed page', async () => {
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, inpaint: 'failed' },
          processingErrors: [{ stage: 'inpaint', error: 'Image rendering failed; inspect the private project log' }],
        }),
      ],
    });
    useWorkbenchStore.setState({ rightTab: 'text' });
    expect(await useWorkbenchStore.getState().selectImage('image-2', { focusFailure: true })).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-2',
      rightTab: 'repair',
    });
  });

  it('jumps to failed pages and opens the matching inspector', async () => {
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, ocr: 'failed' },
          processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
        }),
        imageFixture('image-2'),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: { ...imageFixture('image-3').status, inpaint: 'failed' },
          processingErrors: [{ stage: 'inpaint', error: 'Image rendering failed; inspect the private project log' }],
        }),
      ],
    });
    useWorkbenchStore.setState({ rightTab: 'project' });
    vi.spyOn(api, 'listRegions').mockResolvedValue([]);

    expect(await useWorkbenchStore.getState().navigateImage(1, 'failed')).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-3',
      rightTab: 'repair',
    });
    expect(await useWorkbenchStore.getState().navigateImage(1, 'failed')).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-1',
      rightTab: 'text',
    });
    expect(await useWorkbenchStore.getState().navigateImage(-1, 'failed')).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-3',
      rightTab: 'repair',
    });
  });

  it('steps through visible overflowing pages with adjacent navigation', async () => {
    const overflow = regionFixture('region-9', { imageId: 'image-3' });
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-1'],
        }),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, typeset: 'done' },
        }),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-9'],
        }),
      ],
    });
    useWorkbenchStore.setState((state) => ({
      imageFilter: 'overflow',
      canvasMode: 'original',
      rightTab: 'text',
      regionsByImage: { ...state.regionsByImage, 'image-3': [overflow] },
    }));

    expect(await useWorkbenchStore.getState().navigateImage(1)).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-3',
      selectedRegionIds: ['region-9'],
      rightTab: 'typesetting',
      canvasMode: 'typeset',
      focusRegionIds: ['region-9'],
    });
    expect(await useWorkbenchStore.getState().navigateImage(1)).toBe(false);
  });

  it('opens the matching inspector when using next-image under the failed filter', async () => {
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, ocr: 'failed' },
          processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
        }),
        imageFixture('image-2'),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: { ...imageFixture('image-3').status, inpaint: 'failed' },
          processingErrors: [{ stage: 'inpaint', error: 'Image rendering failed; inspect the private project log' }],
        }),
      ],
    });
    useWorkbenchStore.setState({ imageFilter: 'failed', rightTab: 'project' });
    expect(await useWorkbenchStore.getState().navigateImage(1)).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-3',
      rightTab: 'repair',
    });
    expect(await useWorkbenchStore.getState().navigateImage(1)).toBe(false);
  });

  it('keeps the active page visible after it leaves the failed filter', () => {
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, ocr: 'queued' },
          error: 'OCR failed; inspect the private project log',
          processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
        }),
        imageFixture('image-2'),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: { ...imageFixture('image-3').status, inpaint: 'failed' },
          processingErrors: [{ stage: 'inpaint', error: 'Image rendering failed; inspect the private project log' }],
        }),
      ],
    });
    useWorkbenchStore.setState({ imageFilter: 'failed', activeImageId: 'image-1' });
    expect(visibleImagePosition(useWorkbenchStore.getState())).toEqual({ current: 1, total: 2 });
    expect(canNavigateAdjacent(useWorkbenchStore.getState(), 1)).toBe(true);
    useWorkbenchStore.setState({ activeImageId: 'image-3' });
    expect(visibleImagePosition(useWorkbenchStore.getState())).toEqual({ current: 1, total: 1 });
    expect(canNavigateAdjacent(useWorkbenchStore.getState(), -1)).toBe(false);
  });

  it('counts visible pages for adjacent navigation', () => {
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-1'],
        }),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, typeset: 'done' },
        }),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-9'],
        }),
      ],
    });
    useWorkbenchStore.setState({ imageFilter: 'overflow' });
    expect(visibleImagePosition(useWorkbenchStore.getState())).toEqual({ current: 1, total: 2 });
    expect(canNavigateAdjacent(useWorkbenchStore.getState(), -1)).toBe(false);
    expect(canNavigateAdjacent(useWorkbenchStore.getState(), 1)).toBe(true);
    useWorkbenchStore.setState({ activeImageId: 'image-3' });
    expect(visibleImagePosition(useWorkbenchStore.getState())).toEqual({ current: 2, total: 2 });
    expect(canNavigateAdjacent(useWorkbenchStore.getState(), 1)).toBe(false);
  });

  it('opens a typeset queue item and frames overlay boxes', async () => {
    const overlay = regionFixture('region-9', { imageId: 'image-2' });
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-8'],
        }),
      ],
    });
    useWorkbenchStore.setState((state) => ({
      canvasMode: 'original',
      rightTab: 'text',
      drawerOpen: true,
      regionsByImage: { ...state.regionsByImage, 'image-2': [overlay] },
      jobs: [jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'completed',
        items: [{
          id: 'item-typeset',
          imageId: 'image-2',
          label: 'opaque-id',
          status: 'completed',
          progress: 1,
          output: {
            partialTypeset: true,
            overlayRegionCount: 1,
            overlayRegionIds: ['region-9'],
          },
        }],
      })],
    }));

    expect(await useWorkbenchStore.getState().openJobItem('job-typeset', 'item-typeset')).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-2',
      selectedRegionIds: ['region-9'],
      rightTab: 'typesetting',
      canvasMode: 'typeset',
      focusRegionIds: ['region-9'],
      drawerOpen: false,
    });
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
  });

  it('opens a full-page typeset queue item and frames leftover overflow', async () => {
    const overflow = regionFixture('region-8', { imageId: 'image-2' });
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-8'],
        }),
      ],
    });
    useWorkbenchStore.setState((state) => ({
      canvasMode: 'original',
      regionsByImage: { ...state.regionsByImage, 'image-2': [overflow] },
      jobs: [jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'completed',
        items: [{
          id: 'item-typeset',
          imageId: 'image-2',
          label: 'opaque-id',
          status: 'completed',
          progress: 1,
          output: { partialTypeset: false, overlayRegionCount: 0, overlayRegionIds: [] },
        }],
      })],
    }));

    expect(await useWorkbenchStore.getState().openJobItem('job-typeset', 'item-typeset')).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-2',
      selectedRegionIds: ['region-8'],
      rightTab: 'typesetting',
      canvasMode: 'typeset',
      focusRegionIds: ['region-8'],
    });
  });

  it('opens an inpaint queue item on the erased preview', async () => {
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, inpaint: 'done' },
        }),
      ],
    });
    useWorkbenchStore.setState({
      canvasMode: 'original',
      rightTab: 'text',
      showMask: false,
      drawerOpen: true,
      jobs: [jobFixture({
        id: 'job-inpaint',
        kind: 'inpaint',
        status: 'completed',
        items: [{
          id: 'item-inpaint',
          imageId: 'image-2',
          label: 'opaque-id',
          status: 'completed',
          progress: 1,
        }],
      })],
    });

    expect(await useWorkbenchStore.getState().openJobItem('job-inpaint', 'item-inpaint')).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-2',
      canvasMode: 'erased',
      showMask: true,
      rightTab: 'repair',
      drawerOpen: false,
    });
  });

  it('opens a failed queue item on the matching inspector', async () => {
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, inpaint: 'failed' },
        }),
      ],
    });
    useWorkbenchStore.setState({
      canvasMode: 'original',
      rightTab: 'project',
      drawerOpen: true,
      jobs: [jobFixture({
        id: 'job-ocr',
        kind: 'ocr',
        status: 'failed',
        items: [{
          id: 'item-ocr',
          imageId: 'image-2',
          label: 'opaque-id',
          status: 'failed',
          progress: 0,
          error: 'tesseract unavailable',
        }],
      })],
    });

    expect(await useWorkbenchStore.getState().openJobItem('job-ocr', 'item-ocr')).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-2',
      canvasMode: 'original',
      rightTab: 'text',
      drawerOpen: false,
    });

    useWorkbenchStore.setState({
      activeImageId: 'image-1',
      rightTab: 'text',
      drawerOpen: true,
      jobs: [jobFixture({
        id: 'job-inpaint',
        kind: 'inpaint',
        status: 'failed',
        items: [{
          id: 'item-inpaint',
          imageId: 'image-2',
          label: 'opaque-id',
          status: 'failed',
          progress: 0,
          error: 'mask empty',
        }],
      })],
    });
    expect(await useWorkbenchStore.getState().openJobItem('job-inpaint', 'item-inpaint')).toBe(true);
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-2',
      canvasMode: 'original',
      rightTab: 'repair',
      drawerOpen: false,
    });
  });

  it('lists overflowing region ids that still exist on the page', () => {
    const image = imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, typeset: 'done' },
      typesetOverflowCount: 2,
      typesetOverflowRegionIds: ['region-1', 'gone'],
    });
    expect(overflowingRegionIds(image, [regionFixture('region-1')])).toEqual(['region-1']);
    expect(overflowingRegionIds(image, [])).toEqual([]);
  });

  it('exposes the latest page processing error for inspector retry', () => {
    const image = imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, ocr: 'failed' },
      error: 'OCR failed; inspect the private project log',
      processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
    });
    expect(latestPageProcessingError(image)).toEqual({
      stage: 'ocr',
      error: 'OCR failed; inspect the private project log',
      kind: 'ocr',
    });
    expect(latestPageProcessingError(imageFixture('image-2', {
      status: { ...imageFixture('image-2').status, inpaint: 'failed' },
    }))).toEqual({
      stage: 'inpaint',
      error: '',
      kind: 'inpaint',
    });
    expect(latestPageProcessingError(imageFixture('image-1'))).toBeNull();
  });

  it('hides a stale processing error after that stage is requeued', () => {
    const queued = imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, ocr: 'queued' },
      error: 'OCR failed; inspect the private project log',
      processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
    });
    expect(latestPageProcessingError(queued)).toBeNull();
    expect(latestPageProcessingActivity(queued)).toEqual({
      stage: 'ocr',
      status: 'queued',
      kind: 'ocr',
    });
    expect(imageReviewState(queued)).toBe('queued');
    expect(latestPageProcessingError(imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, ocr: 'queued', inpaint: 'failed' },
      processingErrors: [
        { stage: 'ocr', error: 'OCR failed; inspect the private project log' },
        { stage: 'inpaint', error: 'Image rendering failed; inspect the private project log' },
      ],
    }))).toEqual({
      stage: 'inpaint',
      error: 'Image rendering failed; inspect the private project log',
      kind: 'inpaint',
    });
  });

  it('matches the newest same-kind job for the current page when opening the queue', () => {
    const ocrJob = jobFixture({
      id: 'job-ocr',
      kind: 'ocr',
      items: [{
        id: 'item-ocr-1',
        imageId: 'image-1',
        label: '第一话/image-1.png',
        status: 'queued',
        progress: 0,
      }],
    });
    const inpaintJob = jobFixture({
      id: 'job-inpaint',
      kind: 'inpaint',
      items: [{
        id: 'item-inpaint-2',
        imageId: 'image-2',
        label: '第二话/image-2.png',
        status: 'queued',
        progress: 0,
      }],
    });
    expect(matchingQueueJob([ocrJob, inpaintJob], 'image-1', 'ocr')?.id).toBe('job-ocr');
    expect(matchingQueueJob([inpaintJob], 'image-1', 'ocr')?.id).toBeUndefined();
    expect(matchingQueueJob([jobFixture({ id: 'job-ocr-empty', kind: 'ocr' })], 'image-1', 'ocr')?.id).toBe('job-ocr-empty');
    seedWorkbench();
    useWorkbenchStore.setState({ jobs: [ocrJob, inpaintJob] });
    useWorkbenchStore.getState().openQueueForImage('image-1', 'ocr');
    expect(useWorkbenchStore.getState()).toMatchObject({
      drawerOpen: true,
      queueRevealJobId: 'job-ocr',
      queueRevealItemId: 'item-ocr-1',
    });
    useWorkbenchStore.getState().setDrawerOpen(false);
    expect(useWorkbenchStore.getState()).toMatchObject({
      drawerOpen: false,
      queueRevealJobId: null,
      queueRevealItemId: null,
    });
  });

  it('frames requested region ids until fit-to-window clears them', () => {
    seedWorkbench();
    useWorkbenchStore.getState().focusRegions(['region-2', 'region-2', '']);
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual(['region-2']);
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
    useWorkbenchStore.getState().requestFit();
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual([]);
  });

  it('frames the current selection', () => {
    seedWorkbench({ selectedRegionIds: ['region-2'] });
    useWorkbenchStore.getState().focusSelectedRegions();
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual(['region-2']);
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
    useWorkbenchStore.getState().focusSelectedRegions();
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(1);
  });

  it('merges selected regions, then undo restores the original boxes', () => {
    const polygon = [[100, 120], [320, 120], [320, 240], [100, 240]] as Array<[number, number]>;
    seedWorkbench({
      regions: [
        regionFixture('region-1', {
          repair: { ...regionFixture('region-1').repair, detectorGenerated: true, maskPolygon: polygon },
        }),
        regionFixture('region-2', {
          repair: { ...regionFixture('region-2').repair, detectorGenerated: true, maskPolygon: polygon },
        }),
      ],
      selectedRegionIds: ['region-1', 'region-2'],
    });

    useWorkbenchStore.getState().mergeSelectedRegions();

    const merged = useWorkbenchStore.getState().regionsByImage['image-1'];
    expect(merged).toHaveLength(1);
    expect(merged?.[0]).toMatchObject({
      x: 100,
      y: 120,
      width: 480,
      height: 400,
      sourceText: 'こんにちは\nありがとう',
      detectorConfidence: null,
      ocrConfidence: null,
      recognition: {},
      trustDisposition: 'review',
      trustReason: 'manual-unconfirmed',
      trustPolicyVersion: 1,
      confirmed: false,
      ignored: false,
    });
    expect(merged?.[0]?.repair).not.toHaveProperty('maskPolygon');
    expect(useWorkbenchStore.getState().pendingRegionMutations.map((entry) => entry.kind)).toEqual([
      'delete', 'delete', 'create',
    ]);
    expect(
      useWorkbenchStore.getState().pendingRegionMutations.find((entry) => entry.kind === 'create')
        ?.region.repair,
    ).not.toHaveProperty('maskPolygon');

    useWorkbenchStore.getState().undo();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toHaveLength(2);
  });

  it('splits one region at a real midpoint and supports undo/redo', () => {
    const original = regionFixture('region-1');
    seedWorkbench({
      regions: [{
        ...original,
        repair: {
          ...original.repair,
          detectorGenerated: true,
          maskPolygon: [[100, 120], [320, 120], [320, 240], [100, 240]],
        },
      }],
      selectedRegionIds: ['region-1'],
    });

    useWorkbenchStore.getState().splitSelectedRegion('vertical');
    const split = useWorkbenchStore.getState().regionsByImage['image-1'];
    expect(split).toHaveLength(2);
    expect(split?.[0]?.width).toBe(110);
    expect(split?.[1]).toMatchObject({ x: 210, width: 110 });
    expect(split?.every((region) =>
      region.detectorConfidence === null
      && region.ocrConfidence === null
      && Object.keys(region.recognition).length === 0
      && region.trustDisposition === 'review'
      && region.trustReason === 'manual-unconfirmed'
      && region.trustPolicyVersion === 1
      && !region.confirmed
      && !region.ignored
    )).toBe(true);
    expect(split?.every((region) => region.repair.maskPolygon === undefined)).toBe(true);
    expect(
      useWorkbenchStore.getState().pendingRegionMutations
        .filter((entry) => entry.kind === 'create')
        .every((entry) => entry.region.repair.maskPolygon === undefined),
    ).toBe(true);

    useWorkbenchStore.getState().undo();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toHaveLength(1);
    useWorkbenchStore.getState().redo();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toHaveLength(2);
  });

  it('uses the live server region revision when undoing a saved edit', async () => {
    seedWorkbench({ regions: [regionFixture('region-1')] });
    let revision = 4;
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      ...patch,
      revision: ++revision,
    }));

    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '已保存的新译文' });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    useWorkbenchStore.getState().undo();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      translationText: '你好',
      revision: 5,
    });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(update).toHaveBeenNthCalledWith(2, 'region-1', expect.objectContaining({
      translationText: '你好',
      expectedRevision: 5,
    }));

    useWorkbenchStore.getState().redo();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      translationText: '已保存的新译文',
      revision: 6,
    });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(update).toHaveBeenNthCalledWith(3, 'region-1', expect.objectContaining({
      translationText: '已保存的新译文',
      expectedRevision: 6,
    }));
  });

  it('preserves an earlier unsaved edit when undoing and redoing a later edit', async () => {
    seedWorkbench({ regions: [regionFixture('region-1')] });
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      sourceText: patch.sourceText ?? regionFixture('region-1').sourceText,
      translationText: patch.translationText ?? regionFixture('region-1').translationText,
      revision: 5,
    }));

    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '编辑 A' });
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '编辑 B' });
    useWorkbenchStore.getState().undo();
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(update).toHaveBeenCalledWith('region-1', {
      sourceText: '编辑 A',
      translationText: regionFixture('region-1').translationText,
      expectedRevision: 4,
    });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      sourceText: '编辑 A',
      translationText: regionFixture('region-1').translationText,
    });

    seedWorkbench({ regions: [regionFixture('region-1')] });
    update.mockClear();
    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '编辑 A' });
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '编辑 B' });
    useWorkbenchStore.getState().undo();
    useWorkbenchStore.getState().redo();
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(update).toHaveBeenCalledWith('region-1', {
      sourceText: '编辑 A',
      translationText: '编辑 B',
      expectedRevision: 4,
    });
  });

  it('restores an in-flight delete on undo and avoids recreation after redo', async () => {
    seedWorkbench({ regions: [regionFixture('region-1')], selectedRegionIds: ['region-1'] });
    const deleted = deferred<void>();
    vi.spyOn(api, 'deleteRegion').mockReturnValue(deleted.promise);
    const create = vi.spyOn(api, 'createRegion').mockImplementation(async (_imageId, region) => ({
      ...region,
      id: 'region-restored',
      revision: 1,
    }));

    useWorkbenchStore.getState().deleteSelectedRegions();
    const restoring = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(api.deleteRegion).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().undo();
    deleted.resolve(undefined);
    expect(await restoring).toBe(true);
    expect(create).toHaveBeenCalledOnce();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([
      expect.objectContaining({ id: 'region-restored' }),
    ]);

    seedWorkbench({ regions: [regionFixture('region-1')], selectedRegionIds: ['region-1'] });
    const deletedAgain = deferred<void>();
    vi.mocked(api.deleteRegion).mockReturnValue(deletedAgain.promise);
    create.mockClear();
    useWorkbenchStore.getState().deleteSelectedRegions();
    const deleting = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(api.deleteRegion).toHaveBeenCalledTimes(2));
    useWorkbenchStore.getState().undo();
    useWorkbenchStore.getState().redo();
    deletedAgain.resolve(undefined);
    expect(await deleting).toBe(true);
    expect(create).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([]);
  });

  it('drops full recovery intent when an in-flight delete fails after undo', async () => {
    seedWorkbench({ regions: [regionFixture('region-1')], selectedRegionIds: ['region-1'] });
    const deleted = deferred<void>();
    vi.spyOn(api, 'deleteRegion').mockReturnValue(deleted.promise);
    const update = vi.spyOn(api, 'updateRegion');
    const create = vi.spyOn(api, 'createRegion');

    useWorkbenchStore.getState().deleteSelectedRegions();
    const deleting = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(api.deleteRegion).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().undo();
    deleted.reject(new Error('删除未执行'));

    expect(await deleting).toBe(false);
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(update).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([
      expect.objectContaining({ id: 'region-1' }),
    ]);
  });

  it('keeps only genuine sparse edits when an in-flight delete fails after undo', async () => {
    seedWorkbench({ regions: [regionFixture('region-1')], selectedRegionIds: ['region-1'] });
    const deleted = deferred<void>();
    vi.spyOn(api, 'deleteRegion').mockReturnValue(deleted.promise);
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      translationText: patch.translationText ?? regionFixture('region-1').translationText,
      revision: 5,
    }));

    useWorkbenchStore.getState().deleteSelectedRegions();
    const deleting = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(api.deleteRegion).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().undo();
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '删除失败后的编辑' });
    deleted.reject(new Error('删除未执行'));

    expect(await deleting).toBe(false);
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(update).toHaveBeenCalledWith('region-1', {
      translationText: '删除失败后的编辑',
      expectedRevision: 4,
    });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      translationText: '删除失败后的编辑',
      revision: 5,
    });
  });

  it('recreates a region when undoing a delete that already reached the server', async () => {
    seedWorkbench({
      regions: [regionFixture('region-1')],
      selectedRegionIds: ['region-1'],
    });
    vi.spyOn(api, 'deleteRegion').mockResolvedValue();
    const create = vi.spyOn(api, 'createRegion').mockImplementation(async (_imageId, region) => ({
      ...region,
      id: 'region-restored',
      revision: 1,
    }));
    const update = vi.spyOn(api, 'updateRegion');

    useWorkbenchStore.getState().deleteSelectedRegions();
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    useWorkbenchStore.getState().undo();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.id).toMatch(/^local-/);
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(create).toHaveBeenCalledOnce();
    expect(update).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.id).toBe('region-restored');
  });

  it('reloads an explicitly opened project even when its id is already active', async () => {
    seedWorkbench();
    vi.spyOn(api, 'openProject').mockResolvedValue(projectFixture({ revision: 7 }));
    vi.mocked(api.getProject).mockResolvedValue(projectFixture({ revision: 7 }));
    vi.spyOn(api, 'listImages').mockResolvedValue([imageFixture('image-fresh')]);
    vi.spyOn(api, 'listJobs').mockResolvedValue([]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([]);

    expect(await useWorkbenchStore.getState().openProjectPath('/tmp/project.json')).toBe(true);

    expect(api.getProject).toHaveBeenCalledWith('project-1');
    expect(api.listImages).toHaveBeenCalledWith('project-1');
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-fresh',
      images: [expect.objectContaining({ id: 'image-fresh' })],
    });
  });

  it('flushes dirty edits before opening a portable clone with the same project id', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    const calls: string[] = [];
    vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => {
      calls.push('save');
      return { ...regionFixture('region-1'), ...patch, revision: 5 };
    });
    vi.spyOn(api, 'openProject').mockImplementation(async () => {
      calls.push('open');
      return projectFixture({ revision: 7 });
    });
    vi.mocked(api.getProject).mockResolvedValue(projectFixture({ revision: 7 }));
    vi.mocked(api.listImages).mockResolvedValue([imageFixture('image-fresh')]);
    vi.spyOn(api, 'listJobs').mockResolvedValue([]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([]);
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '克隆前保存' });

    expect(await useWorkbenchStore.getState().openProjectPath('/tmp/portable/project.json')).toBe(true);

    expect(calls).toEqual(['save', 'open']);
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
  });

  it('does not switch pages when a save conflict occurs', async () => {
    seedWorkbench({
      images: [imageFixture('image-1'), imageFixture('image-2')],
      selectedRegionIds: ['region-1'],
    });
    vi.spyOn(api, 'updateRegion').mockRejectedValue(new Error('revision mismatch'));
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '冲突内容' });

    const changed = await useWorkbenchStore.getState().selectImage('image-2');

    expect(changed).toBe(false);
    expect(useWorkbenchStore.getState().activeImageId).toBe('image-1');
    expect(useWorkbenchStore.getState().saveError).toContain('revision mismatch');
  });

  it('blocks OCR from sharing a batch with trust-gated downstream stages', async () => {
    seedWorkbench({
      project: projectFixture({
        settings: {
          ...projectFixture().settings,
          characterNames: '桜\n太郎 = 小明',
        },
      }),
    });
    const calls: string[] = [];
    vi.mocked(api.getProject).mockResolvedValue(projectFixture({ revision: 12 }));
    vi.spyOn(api, 'startJob').mockImplementation(async (_projectId, kind) => {
      calls.push(kind);
      return jobFixture({ id: `job-${kind}`, kind });
    });
    vi.spyOn(api, 'exportProject').mockImplementation(async () => {
      calls.push('export');
      return jobFixture({ id: 'job-export', kind: 'export' });
    });

    expect(await useWorkbenchStore.getState().startBatch(
      ['export', 'typeset', 'detect', 'translate', 'ocr', 'inpaint'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
      3,
    )).toBe(false);

    expect(calls).toEqual([]);
    expect(useWorkbenchStore.getState().globalError).toContain('OCR 与翻译');
  });

  it('resolves every batch target lineage and blocks the legacy entry when any page is active', async () => {
    seedWorkbench();
    useWorkbenchStore.setState((state) => ({
      g4Contexts: { 'image-1': state.g4Contexts['image-1']! },
    }));
    vi.mocked(api.listPageGenerations).mockImplementation(async (imageId) =>
      imageId === 'image-2'
        ? [pageGeneration(4, { id: 'generation-2', imageId: 'image-2' })]
        : []
    );
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([pageEvent(3)]);
    const startJob = vi.spyOn(api, 'startJob');

    expect(await useWorkbenchStore.getState().startBatch(
      ['ocr'],
      ['image-1', 'image-2'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
    )).toBe(false);

    expect(api.listPageGenerations).toHaveBeenCalledWith('image-2');
    expect(startJob).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain('血缘尚未全部确认为旧版页面');
  });

  it('resolves an unclassified batch target as legacy before sending the old job request', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({ g4Contexts: {} });
    vi.mocked(api.listPageGenerations).mockResolvedValue([]);
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-detect', kind: 'detect', status: 'queued',
    }));

    expect(await useWorkbenchStore.getState().startBatch(
      ['detect'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
    )).toBe(true);

    expect(api.listPageGenerations).toHaveBeenCalledWith('image-1');
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.status).toBe('legacy');
    expect(startJob).toHaveBeenCalledOnce();
  });

  it.each([
    {
      kinds: ['preprocess', 'detect'] as const,
      error: '请先验收增强结果',
    },
    {
      kinds: ['preprocess', 'inpaint'] as const,
      error: '请先验收增强结果',
    },
    {
      kinds: ['detect', 'inpaint'] as const,
      error: '请先完成 OCR 并人工确认文本框',
    },
    {
      kinds: ['detect', 'translate'] as const,
      error: '请先完成 OCR 并人工确认文本框',
    },
    {
      kinds: ['inpaint', 'translate'] as const,
      error: '请先验收净版',
    },
    {
      kinds: ['inpaint', 'typeset'] as const,
      error: '请先验收净版',
    },
    {
      kinds: ['translate', 'typeset'] as const,
      error: '请先核对并确认译文',
    },
    {
      kinds: ['typeset', 'export'] as const,
      error: '请先验收成品',
    },
  ])('blocks a batch that crosses an explicit acceptance boundary: $kinds', async ({
    kinds,
    error,
  }) => {
    seedWorkbench();
    const startJob = vi.spyOn(api, 'startJob');
    const exportProject = vi.spyOn(api, 'exportProject');

    expect(await useWorkbenchStore.getState().startBatch(
      [...kinds],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
    )).toBe(false);

    expect(startJob).not.toHaveBeenCalled();
    expect(exportProject).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain(error);
  });

  it('serializes preprocessing settings and marks the selected images as queued', async () => {
    const preprocessing = {
      profile: 'visual-quality' as const,
      enableUpscale: true,
      upscaleFactor: 4 as const,
      enableDenoise: false,
      enableSharpen: true,
      enableContrastEnhance: false,
      enableEdgeOptimize: true,
      enableBinarize: true,
      threshold: 203,
    };
    seedWorkbench({
      project: projectFixture({
        settings: {
          ...projectFixture().settings,
          preprocessorProvider: 'realesrgan-ncnn',
          preprocessing,
        },
      }),
    });
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-preprocess',
      kind: 'preprocess',
    }));

    expect(await useWorkbenchStore.getState().startBatch(
      ['preprocess'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
      4,
    )).toBe(true);

    expect(startJob).toHaveBeenCalledWith('project-1', 'preprocess', {
      imageIds: ['image-1'],
      options: {
        provider: 'realesrgan-ncnn',
        preprocessing,
        concurrency: 4,
      },
    });
    expect(useWorkbenchStore.getState()).toMatchObject({
      jobs: [expect.objectContaining({ id: 'job-preprocess', kind: 'preprocess' })],
      images: [
        expect.objectContaining({
          id: 'image-1',
          preprocessingProvider: 'realesrgan-ncnn',
          status: expect.objectContaining({ preprocess: 'queued' }),
        }),
        expect.objectContaining({
          id: 'image-2',
          status: expect.objectContaining({ preprocess: 'not_started' }),
        }),
      ],
    });
  });

  it('can queue preprocess with a per-page profile override', async () => {
    seedWorkbench();
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-page-preprocess',
      kind: 'preprocess',
    }));
    const override = {
      profile: 'off' as const,
      enableUpscale: false,
      upscaleFactor: 2 as const,
      enableDenoise: false,
      enableSharpen: false,
      enableContrastEnhance: false,
      enableEdgeOptimize: false,
      enableBinarize: false,
      threshold: 180,
    };

    expect(await useWorkbenchStore.getState().startBatch(
      ['preprocess'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
      1,
      undefined,
      override,
    )).toBe(true);

    expect(startJob).toHaveBeenCalledWith('project-1', 'preprocess', {
      imageIds: ['image-1'],
      options: {
        provider: 'opencv-pillow',
        preprocessing: override,
        concurrency: 1,
      },
    });
  });

  it('reuses the OCR job endpoint for only the selected region ids', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-region-ocr',
      kind: 'ocr',
    }));

    expect(await useWorkbenchStore.getState().startBatch(
      ['ocr'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
      1,
      ['region-1'],
    )).toBe(true);

    expect(startJob).toHaveBeenCalledWith('project-1', 'ocr', {
      imageIds: ['image-1'],
      regionIds: ['region-1'],
      options: expect.objectContaining({ provider: 'tesseract', concurrency: 1 }),
    });
  });

  it('keeps an already-created discovery stage visible when OCR creation fails', async () => {
    seedWorkbench();
    const startJob = vi.spyOn(api, 'startJob')
      .mockResolvedValueOnce(jobFixture({ id: 'job-detect', kind: 'detect' }))
      .mockRejectedValueOnce(new Error('OCR unavailable'));

    expect(await useWorkbenchStore.getState().startBatch(
      ['ocr', 'detect'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
    )).toBe(false);

    expect(startJob.mock.calls.map((call) => call[1])).toEqual(['detect', 'ocr']);
    expect(useWorkbenchStore.getState()).toMatchObject({
      globalError: 'OCR unavailable',
      jobs: [expect.objectContaining({ id: 'job-detect', kind: 'detect' })],
      images: [
        expect.objectContaining({
          id: 'image-1',
          status: expect.objectContaining({
            detection: 'queued',
          }),
        }),
        expect.any(Object),
      ],
    });
  });

  it('does not overwrite a pending active-image edit while refreshing completed jobs', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({ id: 'job-ocr', kind: 'ocr', status: 'completed' }),
    ]);
    const listRegions = vi.spyOn(api, 'listRegions').mockResolvedValue([
      regionFixture('region-1', { sourceText: '服务器旧文本' }),
    ]);

    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '尚未提交的本地编辑' });
    await useWorkbenchStore.getState().refreshJobs();

    expect(listRegions).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      id: 'region-1',
      sourceText: '尚未提交的本地编辑',
    });
    expect(useWorkbenchStore.getState().pendingRegionMutations).toEqual([
      expect.objectContaining({ imageId: 'image-1', kind: 'update' }),
    ]);
  });

  it('switches to the typeset preview when a typeset job for the active page completes', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      canvasMode: 'original',
      jobs: [jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'running',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'running',
          progress: 0.4,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'completed',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, typeset: 'done' },
      }),
      imageFixture('image-2'),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([regionFixture('region-1')]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('typeset');
    expect(useWorkbenchStore.getState().compareMode).toBe(true);
    expect(useWorkbenchStore.getState().selectedRegionIds).toEqual([]);
    expect(useWorkbenchStore.getState().rightTab).toBe('text');
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual([]);
  });

  it('opens the text inspector when a detect or OCR job for the active page completes', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      rightTab: 'project',
      jobs: [jobFixture({
        id: 'job-ocr',
        kind: 'ocr',
        status: 'running',
        items: [{
          id: 'item-ocr',
          imageId: 'image-1',
          label: 'page',
          status: 'running',
          progress: 0.4,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-ocr',
        kind: 'ocr',
        status: 'completed',
        items: [{
          id: 'item-ocr',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, ocr: 'done', detection: 'done' },
      }),
      imageFixture('image-2'),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([regionFixture('region-1')]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().rightTab).toBe('text');
  });

  it('opens the text inspector when a fast detect or OCR job is first seen already completed', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      rightTab: 'project',
      jobs: [jobFixture({
        id: 'job-older',
        kind: 'inpaint',
        status: 'completed',
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-older',
        kind: 'inpaint',
        status: 'completed',
      }),
      jobFixture({
        id: 'job-ocr-fast',
        kind: 'ocr',
        status: 'completed',
        items: [{
          id: 'item-ocr-fast',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, ocr: 'done', detection: 'done' },
      }),
      imageFixture('image-2'),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([regionFixture('region-1')]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().rightTab).toBe('text');
  });

  it('selects overflowing boxes when a typeset job for the active page completes', async () => {
    seedWorkbench({ selectedRegionIds: ['region-2'] });
    useWorkbenchStore.setState({
      canvasMode: 'original',
      rightTab: 'text',
      jobs: [jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'running',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'running',
          progress: 0.4,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'completed',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, typeset: 'done' },
        typesetOverflowCount: 1,
        typesetOverflowRegionIds: ['region-1'],
      }),
      imageFixture('image-2'),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([
      regionFixture('region-1'),
      regionFixture('region-2'),
    ]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('typeset');
    expect(useWorkbenchStore.getState().compareMode).toBe(true);
    expect(useWorkbenchStore.getState().selectedRegionIds).toEqual(['region-1']);
    expect(useWorkbenchStore.getState().rightTab).toBe('typesetting');
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual(['region-1']);
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
  });

  it('selects overlay boxes instead of leftover overflow when a partial typeset completes', async () => {
    seedWorkbench({ selectedRegionIds: ['region-2'] });
    useWorkbenchStore.setState({
      canvasMode: 'original',
      rightTab: 'text',
      jobs: [jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'running',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'running',
          progress: 0.4,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'completed',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
          output: {
            partialTypeset: true,
            overlayRegionCount: 1,
            overlayRegionIds: ['region-2'],
          },
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, typeset: 'done' },
        typesetOverflowCount: 1,
        typesetOverflowRegionIds: ['region-1'],
      }),
      imageFixture('image-2'),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([
      regionFixture('region-1'),
      regionFixture('region-2'),
    ]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('typeset');
    expect(useWorkbenchStore.getState().compareMode).toBe(true);
    expect(useWorkbenchStore.getState().selectedRegionIds).toEqual(['region-2']);
    expect(useWorkbenchStore.getState().rightTab).toBe('typesetting');
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual(['region-2']);
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
  });

  it('selects overlay boxes when a partial typeset completes without leftover overflow', async () => {
    seedWorkbench({ selectedRegionIds: ['region-2'] });
    useWorkbenchStore.setState({
      canvasMode: 'original',
      rightTab: 'text',
      jobs: [jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'running',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'running',
          progress: 0.4,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'completed',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
          output: {
            partialTypeset: true,
            overlayRegionCount: 1,
            overlayRegionIds: ['region-2'],
          },
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, typeset: 'done' },
      }),
      imageFixture('image-2'),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([
      regionFixture('region-1'),
      regionFixture('region-2'),
    ]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('typeset');
    expect(useWorkbenchStore.getState().compareMode).toBe(true);
    expect(useWorkbenchStore.getState().selectedRegionIds).toEqual(['region-2']);
    expect(useWorkbenchStore.getState().rightTab).toBe('typesetting');
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual(['region-2']);
    useWorkbenchStore.getState().requestFit();
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual([]);
  });

  it('shows the inpaint review mask when switching to the erased canvas', () => {
    seedWorkbench();
    useWorkbenchStore.setState({ canvasMode: 'original', showMask: false });
    useWorkbenchStore.getState().setCanvasMode('erased');
    expect(useWorkbenchStore.getState().canvasMode).toBe('erased');
    expect(useWorkbenchStore.getState().showMask).toBe(true);
    useWorkbenchStore.getState().setCanvasMode('typeset');
    expect(useWorkbenchStore.getState().showMask).toBe(true);
  });

  it('switches to the erased preview and mask when an inpaint job for the active page completes', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      canvasMode: 'original',
      showMask: false,
      rightTab: 'text',
      jobs: [jobFixture({
        id: 'job-inpaint',
        kind: 'inpaint',
        status: 'running',
        items: [{
          id: 'item-inpaint',
          imageId: 'image-1',
          label: 'page',
          status: 'running',
          progress: 0.4,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-inpaint',
        kind: 'inpaint',
        status: 'completed',
        items: [{
          id: 'item-inpaint',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, inpaint: 'done' },
      }),
      imageFixture('image-2'),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([regionFixture('region-1')]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('erased');
    expect(useWorkbenchStore.getState().compareMode).toBe(true);
    expect(useWorkbenchStore.getState().showMask).toBe(true);
    expect(useWorkbenchStore.getState().rightTab).toBe('repair');
  });

  it('does not change the canvas when an inpaint job was already complete', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      canvasMode: 'original',
      showMask: false,
      jobs: [jobFixture({
        id: 'job-inpaint',
        kind: 'inpaint',
        status: 'completed',
        items: [{
          id: 'item-inpaint',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-inpaint',
        kind: 'inpaint',
        status: 'completed',
        items: [{
          id: 'item-inpaint',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, inpaint: 'done' },
      }),
      imageFixture('image-2'),
    ]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('original');
    expect(useWorkbenchStore.getState().showMask).toBe(false);
  });

  it('switches to the enhanced preview when a preprocess job for the active page completes', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      canvasMode: 'original',
      jobs: [jobFixture({
        id: 'job-preprocess',
        kind: 'preprocess',
        status: 'running',
        items: [{
          id: 'item-preprocess',
          imageId: 'image-1',
          label: 'page',
          status: 'running',
          progress: 0.4,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-preprocess',
        kind: 'preprocess',
        status: 'completed',
        items: [{
          id: 'item-preprocess',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, preprocess: 'done' },
      }),
      imageFixture('image-2'),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([regionFixture('region-1')]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('preprocessed');
    expect(useWorkbenchStore.getState().compareMode).toBe(true);
  });

  it('does not change the canvas when a preprocess job was already complete', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      canvasMode: 'original',
      jobs: [jobFixture({
        id: 'job-preprocess',
        kind: 'preprocess',
        status: 'completed',
        items: [{
          id: 'item-preprocess',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-preprocess',
        kind: 'preprocess',
        status: 'completed',
        items: [{
          id: 'item-preprocess',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, preprocess: 'done' },
      }),
      imageFixture('image-2'),
    ]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('original');
  });

  it('does not change the canvas when a typeset job was already complete', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      canvasMode: 'original',
      jobs: [jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'completed',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'completed',
        items: [{
          id: 'item-typeset',
          imageId: 'image-1',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, typeset: 'done' },
      }),
      imageFixture('image-2'),
    ]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('original');
  });

  it('does not change the canvas when an OCR job completes', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      canvasMode: 'original',
      jobs: [jobFixture({
        id: 'job-ocr',
        kind: 'ocr',
        status: 'running',
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-ocr',
        kind: 'ocr',
        status: 'completed',
      }),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([regionFixture('region-1')]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('original');
  });

  it('does not change the canvas when typesetting completes on another page', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({
      canvasMode: 'original',
      jobs: [jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'running',
        items: [{
          id: 'item-typeset',
          imageId: 'image-2',
          label: 'page',
          status: 'running',
          progress: 0.4,
        }],
      })],
    });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({
        id: 'job-typeset',
        kind: 'typeset',
        status: 'completed',
        items: [{
          id: 'item-typeset',
          imageId: 'image-2',
          label: 'page',
          status: 'completed',
          progress: 1,
        }],
      }),
    ]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1'),
      imageFixture('image-2', {
        status: { ...imageFixture('image-2').status, typeset: 'done' },
      }),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([regionFixture('region-1')]);

    await useWorkbenchStore.getState().refreshJobs();
    expect(useWorkbenchStore.getState().canvasMode).toBe('original');
  });

  it('does not apply a region response that became stale during its request', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    vi.spyOn(api, 'listJobs').mockResolvedValue([
      jobFixture({ id: 'job-ocr', kind: 'ocr', status: 'completed' }),
    ]);
    const response = deferred<ReturnType<typeof regionFixture>[]>();
    const listRegions = vi.spyOn(api, 'listRegions').mockReturnValue(response.promise);

    const refreshing = useWorkbenchStore.getState().refreshJobs();
    await vi.waitFor(() => expect(listRegions).toHaveBeenCalledWith('image-1'));
    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '请求期间的本地编辑' });
    response.resolve([regionFixture('region-1', { sourceText: '服务器旧文本' })]);
    await refreshing;

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      id: 'region-1',
      sourceText: '请求期间的本地编辑',
    });
    expect(useWorkbenchStore.getState().pendingRegionMutations).toEqual([
      expect.objectContaining({ imageId: 'image-1', kind: 'update' }),
    ]);
  });

  it('drains edits made during an active save before starting a batch', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    const firstResponse = deferred<ReturnType<typeof regionFixture>>();
    const calls: string[] = [];
    let updateCall = 0;
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => {
      updateCall += 1;
      calls.push(`save-${updateCall}`);
      if (updateCall === 1) return firstResponse.promise;
      return { ...regionFixture('region-1'), ...patch, revision: 6 };
    });
    vi.spyOn(api, 'startJob').mockImplementation(async (_projectId, kind) => {
      calls.push(`job-${kind}`);
      return jobFixture({ id: `job-${kind}`, kind });
    });

    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '第一次编辑' });
    const firstSave = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '第二次编辑' });
    const batch = useWorkbenchStore.getState().startBatch(['ocr'], ['image-1'], {
      format: 'both',
      imageVariant: 'typeset',
      conflict: 'rename',
      preserveTree: true,
    });
    firstResponse.resolve({
      ...regionFixture('region-1'),
      sourceText: '第一次编辑',
      revision: 5,
    });

    expect(await firstSave).toBe(true);
    expect(await batch).toBe(true);
    expect(calls).toEqual(['save-1', 'save-2', 'job-ocr']);
    expect(update).toHaveBeenNthCalledWith(2, 'region-1', expect.objectContaining({
      sourceText: '第二次编辑',
      expectedRevision: 5,
    }));
  });

  it('retains cumulative sparse intent when an in-flight update fails before retry', async () => {
    const overridden = regionFixture('region-1', {
      repair: { ...regionFixture('region-1').repair, inpainterProvider: 'lama-onnx' },
    });
    seedWorkbench({ selectedRegionIds: ['region-1'], regions: [overridden] });
    const firstResponse = deferred<ReturnType<typeof regionFixture>>();
    let updateCall = 0;
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => {
      updateCall += 1;
      if (updateCall === 1) return firstResponse.promise;
      return {
        ...regionFixture('region-1'),
        sourceText: patch.sourceText ?? regionFixture('region-1').sourceText,
        revision: 5,
      };
    });

    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '第一项编辑' });
    const firstSave = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    useWorkbenchStore.getState().updateRegion('region-1', {
      repair: { inpainterProvider: undefined },
    });
    firstResponse.reject(new Error('第一次请求失败'));

    expect(await firstSave).toBe(false);
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(update).toHaveBeenNthCalledWith(2, 'region-1', {
      sourceText: '第一项编辑',
      repair: { inpainterProvider: null },
      expectedRevision: 4,
    });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      sourceText: '第一项编辑',
      repair: expect.not.objectContaining({ inpainterProvider: expect.anything() }),
      revision: 5,
    });
  });

  it('rebases an in-flight update success without resending its applied fields', async () => {
    const overridden = regionFixture('region-1', {
      repair: { ...regionFixture('region-1').repair, inpainterProvider: 'lama-onnx' },
    });
    seedWorkbench({ selectedRegionIds: ['region-1'], regions: [overridden] });
    const firstResponse = deferred<ReturnType<typeof regionFixture>>();
    let updateCall = 0;
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, _patch) => {
      updateCall += 1;
      if (updateCall === 1) return firstResponse.promise;
      return {
        ...regionFixture('region-1'),
        sourceText: '第一项编辑',
        revision: 6,
      };
    });

    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '第一项编辑' });
    const saving = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(update).toHaveBeenCalledTimes(1));
    useWorkbenchStore.getState().updateRegion('region-1', {
      repair: { inpainterProvider: undefined },
    });
    firstResponse.resolve({
      ...overridden,
      sourceText: '第一项编辑',
      revision: 5,
    });

    expect(await saving).toBe(true);
    expect(update).toHaveBeenNthCalledWith(2, 'region-1', {
      repair: { inpainterProvider: null },
      expectedRevision: 5,
    });
  });

  it('keeps the earlier server-relative edit when an in-flight request fails after undo', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    const firstResponse = deferred<ReturnType<typeof regionFixture>>();
    let updateCall = 0;
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => {
      updateCall += 1;
      if (updateCall === 1) return firstResponse.promise;
      return {
        ...regionFixture('region-1'),
        sourceText: patch.sourceText ?? regionFixture('region-1').sourceText,
        translationText: patch.translationText ?? regionFixture('region-1').translationText,
        revision: 5,
      };
    });

    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '编辑 A' });
    const firstSave = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(update).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '编辑 B' });
    useWorkbenchStore.getState().undo();
    firstResponse.reject(new Error('第一次请求失败'));

    expect(await firstSave).toBe(false);
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    expect(update).toHaveBeenNthCalledWith(2, 'region-1', {
      sourceText: '编辑 A',
      translationText: regionFixture('region-1').translationText,
      expectedRevision: 4,
    });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      sourceText: '编辑 A',
      translationText: regionFixture('region-1').translationText,
    });
  });

  it('drops an empty rebased mutation when an in-flight request succeeds after undo', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    const firstResponse = deferred<ReturnType<typeof regionFixture>>();
    const update = vi.spyOn(api, 'updateRegion').mockReturnValue(firstResponse.promise);

    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: '编辑 A' });
    const saving = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(update).toHaveBeenCalledOnce());
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '编辑 B' });
    useWorkbenchStore.getState().undo();
    firstResponse.resolve({
      ...regionFixture('region-1'),
      sourceText: '编辑 A',
      revision: 5,
    });

    expect(await saving).toBe(true);
    expect(update).toHaveBeenCalledOnce();
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      sourceText: '编辑 A',
      translationText: regionFixture('region-1').translationText,
      revision: 5,
    });
  });

  it('loads no generation as legacy and fails closed on multiple active generations', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({ g4Contexts: {} });

    expect(await useWorkbenchStore.getState().loadG4Context('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'legacy',
      generation: null,
    });

    vi.mocked(api.listPageGenerations).mockResolvedValue([
      pageGeneration(8),
      pageGeneration(9, { id: 'generation-2' }),
    ]);
    expect(await useWorkbenchStore.getState().loadG4Context('image-1', true)).toBe(false);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'error',
      conflict: false,
    });
  });

  it.each(['completed', 'superseded'] as const)(
    'fails closed when page lineage is historical with no active generation: %s',
    async (generationState) => {
      seedWorkbench();
      useWorkbenchStore.setState({ g4Contexts: {} });
      vi.mocked(api.listPageGenerations).mockResolvedValue([
        pageGeneration(8, {
          state: generationState,
          closedAt: '2026-08-25T00:00:01Z',
        }),
      ]);
      const startJob = vi.spyOn(api, 'startJob');

      expect(await useWorkbenchStore.getState().loadG4Context('image-1', true)).toBe(false);
      expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
        status: 'error',
        error: expect.stringContaining('历史代次'),
      });
      expect(await useWorkbenchStore.getState().startBatch(
        ['ocr'],
        ['image-1'],
        { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
      )).toBe(false);
      expect(startJob).not.toHaveBeenCalled();
    },
  );

  it('does not let a stale G4 context request overwrite a newer result', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({ g4Contexts: {} });
    const stale = deferred<PageGeneration[]>();
    vi.mocked(api.listPageGenerations)
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValueOnce([]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([pageEvent()]);

    const first = useWorkbenchStore.getState().loadG4Context('image-1', true);
    const second = useWorkbenchStore.getState().loadG4Context('image-1', true);
    expect(await second).toBe(true);
    stale.resolve([pageGeneration()]);
    expect(await first).toBe(false);

    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'legacy', generation: null,
    });
  });

  it('retries one torn generation/event snapshot and locks on continuous lineage drift', async () => {
    seedWorkbench();
    useWorkbenchStore.setState({ g4Contexts: {} });
    vi.mocked(api.listPageGenerations)
      .mockResolvedValueOnce([pageGeneration(8)])
      .mockResolvedValueOnce([pageGeneration(9)])
      .mockResolvedValueOnce([pageGeneration(9)])
      .mockResolvedValueOnce([pageGeneration(9)]);
    vi.spyOn(api, 'listPageLineageEvents')
      .mockResolvedValueOnce([pageEvent(7)])
      .mockResolvedValueOnce([pageEvent(8)]);

    expect(await useWorkbenchStore.getState().loadG4Context('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'active',
      generation: { nextSequence: 9 },
      phase: 'G4',
    });

    useWorkbenchStore.setState({ g4Contexts: {} });
    vi.mocked(api.listPageGenerations)
      .mockReset()
      .mockResolvedValueOnce([pageGeneration(8)])
      .mockResolvedValueOnce([pageGeneration(9)])
      .mockResolvedValueOnce([pageGeneration(10)])
      .mockResolvedValueOnce([pageGeneration(11)]);
    vi.mocked(api.listPageLineageEvents)
      .mockReset()
      .mockResolvedValueOnce([pageEvent(7)])
      .mockResolvedValueOnce([pageEvent(9)]);

    expect(await useWorkbenchStore.getState().loadG4Context('image-1', true)).toBe(false);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'error',
      error: expect.stringContaining('持续变化'),
    });
  });

  it('does not let a background G4 context refresh clear a later conflict lock', async () => {
    seedActiveG4(8);
    const response = deferred<PageGeneration[]>();
    vi.mocked(api.listPageGenerations).mockReturnValue(response.promise);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([pageEvent(8)]);

    const loading = useWorkbenchStore.getState().loadG4Context('image-1', true);
    useWorkbenchStore.setState((state) => ({
      g4Contexts: {
        ...state.g4Contexts,
        'image-1': {
          status: 'active',
          generation: pageGeneration(8),
          events: [pageEvent(7)],
          error: 'sequence changed',
          conflict: true,
        },
      },
    }));
    response.resolve([pageGeneration(9)]);

    expect(await loading).toBe(false);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'active',
      generation: { nextSequence: 8 },
      error: 'sequence changed',
      conflict: true,
    });
  });

  it.each(['missing', 'loading', 'error'] as const)(
    'blocks all legacy region and project entry points while lineage is %s',
    async (lineageState) => {
      const first = regionFixture('region-1', { x: 10, y: 10, width: 100, height: 80 });
      const second = regionFixture('region-2', { x: 20, y: 20, width: 100, height: 80 });
      seedWorkbench({ regions: [first, second], selectedRegionIds: ['region-1', 'region-2'] });
      if (lineageState === 'missing') {
        useWorkbenchStore.setState({ g4Contexts: {} });
      } else {
        useWorkbenchStore.setState({
          g4Contexts: {
            'image-1': {
              status: lineageState,
              generation: null,
              events: [],
              error: lineageState === 'error' ? 'lineage unavailable' : '',
              conflict: false,
            },
          },
        });
      }
      const beforeSettings = useWorkbenchStore.getState().currentProject?.settings.targetLanguage;

      useWorkbenchStore.getState().mergeSelectedRegions();
      useWorkbenchStore.setState({ selectedRegionIds: ['region-1'] });
      useWorkbenchStore.getState().splitSelectedRegion('vertical');
      expect(useWorkbenchStore.getState().consolidateActiveImageRegions()).toBe(0);
      expect(await useWorkbenchStore.getState().setRegionConfirmed('region-1', true)).toBe(false);
      useWorkbenchStore.getState().undo();
      useWorkbenchStore.getState().redo();
      useWorkbenchStore.getState().updateProjectSettings({ targetLanguage: 'blocked-change' });

      expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([first, second]);
      expect(useWorkbenchStore.getState().pendingRegionMutations).toEqual([]);
      expect(useWorkbenchStore.getState().pendingProjectMutation).toBeNull();
      expect(useWorkbenchStore.getState().currentProject?.settings.targetLanguage).toBe(beforeSettings);
      expect(useWorkbenchStore.getState().globalError).toContain('血缘');
    },
  );

  it('stops a queued legacy region write if lineage becomes unclassified before flush', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    const update = vi.spyOn(api, 'updateRegion');
    useWorkbenchStore.getState().updateRegion('region-1', { sourceText: 'queued legacy edit' });
    useWorkbenchStore.setState({
      g4Contexts: {
        'image-1': {
          status: 'loading', generation: null, events: [], error: '', conflict: false,
        },
      },
    });

    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(false);
    expect(update).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(1);
    expect(useWorkbenchStore.getState().saveError).toContain('旧版文本框写入已停止');
  });

  it('stops a queued project mutation if the active page lineage becomes unclassified', async () => {
    seedWorkbench();
    const updateProject = vi.spyOn(api, 'updateProject');
    useWorkbenchStore.getState().updateProjectSettings({ targetLanguage: 'queued-change' });
    useWorkbenchStore.setState({
      g4Contexts: {
        'image-1': {
          status: 'error', generation: null, events: [], error: 'lineage unavailable', conflict: false,
        },
      },
    });

    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(false);
    expect(updateProject).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().pendingProjectMutation).not.toBeNull();
    expect(useWorkbenchStore.getState().saveError).toContain('不能保存旧版项目参数');
  });

  it.each(['missing', 'loading', 'error', 'active'] as const)(
    'blocks a project setting change when a non-current page lineage is %s',
    (lineageState) => {
      seedWorkbench();
      if (lineageState === 'missing') {
        const firstContext = useWorkbenchStore.getState().g4Contexts['image-1']!;
        useWorkbenchStore.setState({ g4Contexts: { 'image-1': firstContext } });
      } else if (lineageState === 'active') {
        useWorkbenchStore.setState((state) => ({
          g4Contexts: {
            ...state.g4Contexts,
            'image-2': {
              status: 'active',
              generation: pageGeneration(8, { id: 'generation-2', imageId: 'image-2' }),
              events: [pageEvent(7)],
              error: '',
              conflict: false,
            },
          },
        }));
      } else {
        useWorkbenchStore.setState((state) => ({
          g4Contexts: {
            ...state.g4Contexts,
            'image-2': {
              status: lineageState,
              generation: null,
              events: [],
              error: lineageState === 'error' ? 'lineage unavailable' : '',
              conflict: false,
            },
          },
        }));
      }
      const before = useWorkbenchStore.getState().currentProject?.settings.targetLanguage;

      useWorkbenchStore.getState().updateProjectSettings({ targetLanguage: 'blocked-project-change' });

      expect(useWorkbenchStore.getState().pendingProjectMutation).toBeNull();
      expect(useWorkbenchStore.getState().currentProject?.settings.targetLanguage).toBe(before);
      expect(useWorkbenchStore.getState().globalError).toContain('项目内页面血缘尚未全部确认为旧版');
    },
  );

  it('rechecks every project page lineage immediately after settings synchronization', async () => {
    seedWorkbench();
    useWorkbenchStore.getState().updateProjectSettings({ targetLanguage: 'queued-project-change' });
    const projectResponse = deferred<ReturnType<typeof projectFixture>>();
    vi.mocked(api.getProject).mockReturnValue(projectResponse.promise);
    const updateProject = vi.spyOn(api, 'updateProject');

    const saving = useWorkbenchStore.getState().flushAutosave();
    await vi.waitFor(() => expect(api.getProject).toHaveBeenCalledWith('project-1'));
    useWorkbenchStore.setState((state) => ({
      g4Contexts: {
        ...state.g4Contexts,
        'image-2': {
          status: 'active',
          generation: pageGeneration(8, { id: 'generation-2', imageId: 'image-2' }),
          events: [pageEvent(7)],
          error: '',
          conflict: false,
        },
      },
    }));
    projectResponse.resolve(projectFixture({ revision: 9 }));

    expect(await saving).toBe(false);
    expect(updateProject).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().pendingProjectMutation).not.toBeNull();
    expect(useWorkbenchStore.getState().saveError).toContain('状态已变化');
  });

  it('creates a manual G4 draft with resolved defaults and the active image authority', async () => {
    seedActiveG4(8);
    useWorkbenchStore.setState({
      regionsByImage: { 'image-1': [] },
      serverRegionRevisions: {},
      images: [imageFixture('image-1', { revision: 10, regionCount: 0 })],
    });
    const saved = regionFixture('region-server', {
      order: 0,
      direction: 'vertical',
      paragraphGroupId: 'paragraph-server',
      contentDisposition: 'translate',
      sourceText: '',
      translationText: '',
      confidence: null,
      revision: 1,
    });
    const create = vi.spyOn(api, 'createG4Region').mockImplementation(async (_imageId, region) => ({
      ...saved,
      paragraphGroupId: region.paragraphGroupId,
    }));
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', { revision: 11, regionCount: 1 }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(9)]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([pageEvent(8, 'regions-created')]);
    vi.spyOn(api, 'listRegions').mockImplementation(async () => [
      { ...saved, paragraphGroupId: create.mock.calls[0]?.[1].paragraphGroupId ?? null },
    ]);

    const localId = useWorkbenchStore.getState().createRegion({ x: 10, y: 20, width: 100, height: 60 });
    expect(localId).toMatch(/^local-/);
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(create).toHaveBeenCalledWith(
      'image-1',
      expect.objectContaining({
        order: 0,
        direction: 'vertical',
        contentDisposition: 'translate',
        paragraphGroupId: expect.stringMatching(/^paragraph-/),
      }),
      10,
      expect.objectContaining({ expectedSequence: 8 }),
    );
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.id).toBe('region-server');
  });

  it('serializes G4 mutations against refreshed image revisions and lineage sequences', async () => {
    const region = seedActiveG4(8);
    const firstSaved = { ...region, x: 110, revision: 5 };
    const secondSaved = { ...firstSaved, x: 120, revision: 6 };
    const update = vi.spyOn(api, 'updateG4Region')
      .mockResolvedValueOnce(firstSaved)
      .mockResolvedValueOnce(secondSaved);
    vi.mocked(api.listImages)
      .mockResolvedValueOnce([imageFixture('image-1', { revision: 11, regionCount: 1 })])
      .mockResolvedValueOnce([imageFixture('image-1', { revision: 12, regionCount: 1 })]);
    vi.mocked(api.listPageGenerations)
      .mockResolvedValueOnce([pageGeneration(9)])
      .mockResolvedValueOnce([pageGeneration(9)])
      .mockResolvedValueOnce([pageGeneration(10)])
      .mockResolvedValueOnce([pageGeneration(10)]);
    vi.spyOn(api, 'listPageLineageEvents')
      .mockResolvedValueOnce([pageEvent(8, 'regions-updated')])
      .mockResolvedValueOnce([pageEvent(9, 'regions-updated')]);
    vi.spyOn(api, 'listRegions')
      .mockResolvedValueOnce([firstSaved])
      .mockResolvedValueOnce([secondSaved]);

    useWorkbenchStore.getState().updateRegion('region-1', { x: 110 });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    useWorkbenchStore.getState().updateRegion('region-1', { x: 120 });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);

    expect(update).toHaveBeenNthCalledWith(
      1,
      'region-1',
      { x: 110 },
      4,
      10,
      expect.objectContaining({
        runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 8,
      }),
    );
    expect(update).toHaveBeenNthCalledWith(
      2,
      'region-1',
      { x: 120 },
      5,
      11,
      expect.objectContaining({
        runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 9,
      }),
    );
    const firstActor = update.mock.calls[0]?.[4]?.actor;
    const secondActor = update.mock.calls[1]?.[4]?.actor;
    expect(secondActor).toEqual(firstActor);
    expect(useWorkbenchStore.getState().pendingG4Mutations).toHaveLength(0);
  });

  it('stops a G4 mutation after one 409 and requires a manual reload', async () => {
    seedActiveG4(8);
    const update = vi.spyOn(api, 'updateG4Region').mockRejectedValue(
      new ApiError('sequence changed', 409),
    );

    useWorkbenchStore.getState().updateRegion('region-1', { x: 111 });
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(false);
    expect(await useWorkbenchStore.getState().flushAutosave()).toBe(false);

    expect(update).toHaveBeenCalledOnce();
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'active',
      conflict: true,
      error: 'sequence changed',
    });
    expect(useWorkbenchStore.getState().pendingG4Mutations).toHaveLength(1);
  });

  it('starts active-generation detection with exact job lineage and locks the page', async () => {
    seedActiveG4(8);
    const start = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-detect',
      kind: 'detect',
      status: 'queued',
      total: 1,
      items: [{
        id: 'item-detect', imageId: 'image-1', label: 'image-1', status: 'queued', progress: 0,
      }],
    }));
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        revision: 10,
        status: { ...imageFixture('image-1').status, detection: 'queued' },
      }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(9)]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([
      pageEvent(8, 'detect-job-enqueued', { outputChecksum: null }),
    ]);

    expect(await useWorkbenchStore.getState().startG4Detection()).toBe(true);

    expect(start).toHaveBeenCalledWith('project-1', 'detect', expect.objectContaining({
      imageIds: ['image-1'],
      lineage: expect.objectContaining({
        runId: 'run-1',
        pages: [{ imageId: 'image-1', pageGenerationId: 'generation-1', expectedSequence: 8 }],
      }),
    }));
    expect(useWorkbenchStore.getState().jobs[0]).toMatchObject({
      id: 'job-detect', status: 'queued',
    });
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.generation?.nextSequence).toBe(9);
  });

  it('locks the page after an uncertain detect submission and reconciles jobs on manual reload', async () => {
    seedActiveG4(8);
    const start = vi.spyOn(api, 'startJob').mockRejectedValueOnce(new Error('connection lost'));

    expect(await useWorkbenchStore.getState().startG4Detection()).toBe(false);
    expect(await useWorkbenchStore.getState().startG4Detection()).toBe(false);
    expect(start).toHaveBeenCalledOnce();
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'active',
      conflict: false,
      error: expect.stringContaining('结果不确定'),
    });

    const queued = jobFixture({
      id: 'job-detect',
      kind: 'detect',
      status: 'queued',
      total: 1,
      items: [{
        id: 'item-detect', imageId: 'image-1', label: 'image-1', status: 'queued', progress: 0,
      }],
    });
    vi.mocked(api.listJobs).mockResolvedValue([queued]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        revision: 10,
        status: { ...imageFixture('image-1').status, detection: 'queued' },
      }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(9)]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([
      pageEvent(8, 'detect-job-enqueued', { outputChecksum: null }),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([
      useWorkbenchStore.getState().regionsByImage['image-1']![0]!,
    ]);

    await useWorkbenchStore.getState().reloadActiveImage();

    expect(useWorkbenchStore.getState().jobs).toEqual([
      expect.objectContaining({ id: 'job-detect', status: 'queued' }),
    ]);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'active',
      generation: { nextSequence: 9 },
      error: '',
      conflict: false,
    });
    expect(g4EditingLocked(useWorkbenchStore.getState(), 'image-1')).toBe(true);
  });

  it('locks the page after an uncertain G4 acceptance and never submits it twice', async () => {
    seedActiveG4(8);
    const accept = vi.spyOn(api, 'acceptRegionsGate').mockRejectedValueOnce(
      new Error('connection lost'),
    );

    expect(await useWorkbenchStore.getState().acceptG4Regions()).toBe(false);
    expect(await useWorkbenchStore.getState().acceptG4Regions()).toBe(false);

    expect(accept).toHaveBeenCalledOnce();
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      status: 'active',
      conflict: false,
      error: expect.stringContaining('结果不确定'),
    });
    expect(g4EditingLocked(useWorkbenchStore.getState(), 'image-1')).toBe(true);
  });

  it('refreshes regions and lineage when an active-page detect job becomes terminal', async () => {
    const region = seedActiveG4(9);
    const queued = jobFixture({
      id: 'job-detect',
      kind: 'detect',
      status: 'queued',
      total: 1,
      items: [{
        id: 'item-detect', imageId: 'image-1', label: 'image-1', status: 'queued', progress: 0,
      }],
    });
    const completed = jobFixture({
      ...queued,
      status: 'completed',
      completed: 1,
      progress: 1,
      items: [{
        id: 'item-detect', imageId: 'image-1', label: 'image-1', status: 'completed', progress: 1,
      }],
    });
    useWorkbenchStore.setState({ jobs: [queued] });
    vi.spyOn(api, 'listJobs').mockResolvedValue([completed]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        revision: 11,
        regionCount: 1,
        status: { ...imageFixture('image-1').status, detection: 'done' },
      }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(11)]);
    const listEvents = vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([
      pageEvent(10, 'detect-job-completed', { outputChecksum: 'f'.repeat(64) }),
    ]);
    const refreshedRegion = { ...region, x: 130, revision: 5 };
    const listRegions = vi.spyOn(api, 'listRegions').mockResolvedValue([refreshedRegion]);

    await useWorkbenchStore.getState().refreshJobs();

    expect(listRegions).toHaveBeenCalledWith('image-1');
    expect(listEvents).toHaveBeenCalledWith('generation-1');
    expect(useWorkbenchStore.getState().jobs[0]?.status).toBe('completed');
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      x: 130, revision: 5,
    });
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.generation?.nextSequence).toBe(11);
  });

  it('refreshes regions and lineage when a non-current page detect job becomes terminal', async () => {
    const region = seedActiveG4(9);
    const secondImage = imageFixture('image-2');
    const queued = jobFixture({
      id: 'job-detect',
      kind: 'detect',
      status: 'queued',
      total: 1,
      items: [{
        id: 'item-detect', imageId: 'image-1', label: 'image-1', status: 'queued', progress: 0,
      }],
    });
    const completed = jobFixture({
      ...queued,
      status: 'completed',
      completed: 1,
      progress: 1,
      items: [{
        id: 'item-detect', imageId: 'image-1', label: 'image-1', status: 'completed', progress: 1,
      }],
    });
    useWorkbenchStore.setState((state) => ({
      activeImageId: 'image-2',
      images: [...state.images, secondImage],
      jobs: [queued],
      g4Contexts: {
        ...state.g4Contexts,
        'image-2': {
          status: 'legacy', generation: null, events: [], error: '', conflict: false,
        },
      },
    }));
    vi.mocked(api.listJobs).mockResolvedValue([completed]);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', {
        revision: 11,
        regionCount: 1,
        status: { ...imageFixture('image-1').status, detection: 'done' },
      }),
      secondImage,
    ]);
    vi.mocked(api.listPageGenerations).mockImplementation(async (imageId) =>
      imageId === 'image-1' ? [pageGeneration(11)] : []
    );
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([
      pageEvent(10, 'detect-job-completed', { outputChecksum: 'f'.repeat(64) }),
    ]);
    const refreshedRegion = { ...region, x: 140, revision: 6 };
    const listRegions = vi.spyOn(api, 'listRegions').mockImplementation(async (imageId) =>
      imageId === 'image-1' ? [refreshedRegion] : []
    );

    await useWorkbenchStore.getState().refreshJobs();

    expect(listRegions).toHaveBeenCalledWith('image-1');
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      x: 140, revision: 6,
    });
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.generation?.nextSequence).toBe(11);
    expect(await useWorkbenchStore.getState().selectImage('image-1')).toBe(true);
    expect(listRegions.mock.calls.filter(([imageId]) => imageId === 'image-1')).toHaveLength(1);
  });

  it('blocks every later-stage review mutation for an active G4 generation', async () => {
    seedActiveG4(8);
    const reviewImage = vi.spyOn(api, 'reviewImage');
    const reviewStage = vi.spyOn(api, 'reviewImageStage');
    const selectCandidate = vi.spyOn(api, 'selectInpaintCandidate');
    const reviewCandidate = vi.spyOn(api, 'reviewSelectedInpaintAiCandidate');
    const setFallback = vi.spyOn(api, 'setInpaintClassicalFallback');

    expect(await useWorkbenchStore.getState().reviewActiveImage('reviewed')).toBe(false);
    expect(await useWorkbenchStore.getState().reviewActiveImageStage(
      'inpaint', 'accepted', stageObservation('inpaint', 10),
    )).toBe(false);
    expect(await useWorkbenchStore.getState().selectInpaintCandidate('candidate-b')).toBe(false);
    expect(await useWorkbenchStore.getState().reviewSelectedInpaintAiCandidate('rejected')).toBe(false);
    expect(await useWorkbenchStore.getState().setActiveImageInpaintFallback(
      'approved', { reason: 'ai-visible-artifacts' },
    )).toBe(false);

    expect(reviewImage).not.toHaveBeenCalled();
    expect(reviewStage).not.toHaveBeenCalled();
    expect(selectCandidate).not.toHaveBeenCalled();
    expect(reviewCandidate).not.toHaveBeenCalled();
    expect(setFallback).not.toHaveBeenCalled();
  });

  it('requires detector candidates to keep their identity and receive a disposition', () => {
    const region = seedActiveG4(8);
    const detectorRegion = {
      ...region,
      detectorJobItemId: 'item-detect',
      detectorCandidateIndex: 0,
      contentDisposition: null,
    };
    useWorkbenchStore.setState((state) => ({
      selectedRegionIds: [detectorRegion.id],
      regionsByImage: { ...state.regionsByImage, 'image-1': [detectorRegion] },
    }));

    useWorkbenchStore.getState().deleteSelectedRegions();

    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([detectorRegion]);
    expect(useWorkbenchStore.getState().pendingG4Mutations).toEqual([]);
    expect(useWorkbenchStore.getState().globalError).toContain('标记为“误检”');
  });

  it('blocks generic resume and retry actions for jobs targeting a non-legacy page', async () => {
    seedActiveG4(8);
    const failed = jobFixture({
      id: 'job-ocr',
      kind: 'ocr',
      status: 'failed',
      items: [{
        id: 'item-ocr', imageId: 'image-1', label: 'image-1', status: 'failed', progress: 0,
      }],
    });
    useWorkbenchStore.setState({ jobs: [failed] });
    const action = vi.spyOn(api, 'jobAction');

    await useWorkbenchStore.getState().runJobAction('job-ocr', 'retry');
    await useWorkbenchStore.getState().runJobAction('job-ocr', 'resume');

    expect(action).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain('血缘尚未全部确认为旧版页面');
  });

  it('uses the full region set for G4 reorder and the latest draft checksum for acceptance', async () => {
    const first = seedActiveG4(8);
    const second = regionFixture('region-2', {
      order: 1,
      paragraphGroupId: 'paragraph-2',
      contentDisposition: 'translate',
      sourceText: '',
      translationText: '',
      confidence: null,
    });
    useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => ({ ...image, regionCount: 2 })),
      regionsByImage: { ...state.regionsByImage, 'image-1': [first, second] },
      serverRegionRevisions: { 'region-1': 4, 'region-2': second.revision },
    }));
    const reordered = [
      { ...second, order: 0, revision: 5 },
      { ...first, order: 1, revision: 5 },
    ];
    const reorder = vi.spyOn(api, 'reorderG4Regions').mockResolvedValue(reordered);
    vi.mocked(api.listImages)
      .mockResolvedValueOnce([imageFixture('image-1', { revision: 11, regionCount: 2 })])
      .mockResolvedValueOnce([imageFixture('image-1', { revision: 12, regionCount: 2 })]);
    vi.mocked(api.listPageGenerations)
      .mockResolvedValueOnce([pageGeneration(9)])
      .mockResolvedValueOnce([pageGeneration(9)])
      .mockResolvedValueOnce([pageGeneration(10)])
      .mockResolvedValueOnce([pageGeneration(10)]);
    vi.spyOn(api, 'listPageLineageEvents')
      .mockResolvedValueOnce([pageEvent(8, 'regions-reordered', { outputChecksum: 'e'.repeat(64) })])
      .mockResolvedValueOnce([
        pageEvent(8, 'regions-reordered', { outputChecksum: 'e'.repeat(64) }),
        pageEvent(9, 'regions-stage-review', {
          state: 'accepted', outputChecksum: 'e'.repeat(64),
        }),
      ]);
    vi.spyOn(api, 'listRegions')
      .mockResolvedValueOnce(reordered)
      .mockResolvedValueOnce(reordered);
    const accept = vi.spyOn(api, 'acceptRegionsGate').mockResolvedValue({
      imageId: 'image-1', imageRevision: 12, generationId: 'generation-1', nextSequence: 10,
      event: pageEvent(9, 'regions-stage-review', { state: 'accepted', outputChecksum: 'e'.repeat(64) }),
    });

    expect(await useWorkbenchStore.getState().moveG4Region('region-1', 1)).toBe(true);
    expect(reorder).toHaveBeenCalledWith(
      'image-1', ['region-2', 'region-1'], 10,
      expect.objectContaining({ expectedSequence: 8 }),
    );
    expect(await useWorkbenchStore.getState().acceptG4Regions()).toBe(true);
    expect(accept).toHaveBeenCalledWith(
      'image-1', 'e'.repeat(64), 11,
      expect.objectContaining({ expectedSequence: 9 }),
    );
  });

  it('derives G4 through G7, no-text, and fail-closed workflow phases', () => {
    const acceptedG4 = pageEvent(7, 'regions-stage-review', {
      state: 'accepted', outputChecksum: 'e'.repeat(64),
    });
    const classified = pageEvent(8, 'background-classification-reviewed', {
      gate: 'G5_background', state: 'pending', outputChecksum: 'f'.repeat(64),
    });
    const acceptedG5 = pageEvent(9, 'background-stage-review', {
      gate: 'G5_background', state: 'accepted', outputChecksum: 'f'.repeat(64),
    });
    const notApplicableG5 = pageEvent(8, 'background-stage-review', {
      gate: 'G5_background',
      state: 'not-applicable',
      decision: 'background-not-applicable',
      outputChecksum: 'f'.repeat(64),
    });
    const noText = pageEvent(3, 'text-presence-decision', {
      gate: 'G3_textPresence', state: 'accepted', decision: 'no-text',
    });

    expect(deriveWorkflowPhase(pageGeneration(8), [pageEvent(7)])).toBe('G4');
    expect(deriveWorkflowPhase(pageGeneration(8), [acceptedG4])).toBe('G5');
    expect(deriveWorkflowPhase(pageGeneration(9), [acceptedG4, classified])).toBe('G5');
    expect(deriveWorkflowPhase(pageGeneration(10), [acceptedG4, classified, acceptedG5])).toBe('G6');
    expect(deriveWorkflowPhase(pageGeneration(9), [acceptedG4, notApplicableG5])).toBe('G6');
    const reviewedG6 = g6BaseEvents(true, true);
    expect(deriveWorkflowPhase(pageGeneration(13), reviewedG6)).toBe('G6');
    const acceptedG6 = pageEvent(13, 'ocr-stage-review', {
      gate: 'G6_ocr',
      state: 'accepted',
      stage: 'ocr',
      decision: 'ocr-trust-accepted',
      reason: 'all-translatable-source-text-reviewed',
      inputChecksum: '3'.repeat(64),
      outputChecksum: '3'.repeat(64),
      parentChecksum: 'f'.repeat(64),
      provider: null,
      modelVersion: null,
      jobId: null,
      jobItemId: null,
      evidence: {
        eventType: 'ocr-stage-review',
        qualityState: 'accepted',
        targetKind: 'region-set',
        regionCount: 1,
        eligibleRegionCount: 1,
        attemptedRegionCount: 1,
        reviewedRegionCount: 1,
        ocrAttemptCount: 2,
      },
    });
    expect(deriveWorkflowPhase(pageGeneration(14), [...reviewedG6, acceptedG6])).toBe('G7');
    const g7Draft = pageEvent(14, 'mask-draft-updated', {
      gate: 'G7_mask', state: 'pending', stage: 'mask', decision: null,
      reason: 'mask-recipe-updated', inputChecksum: '3'.repeat(64), outputChecksum: '5'.repeat(64),
      parentChecksum: '3'.repeat(64), provider: 'deterministic-mask', modelVersion: 'create-mask-v1',
      parameterHash: '6'.repeat(64), jobId: null, jobItemId: null, revisionId: 'revision-mask-draft-1',
      evidence: { eventType: 'mask-draft-updated', qualityState: 'pending-review',
        eligibleRegionCount: 1, recipeRegionCount: 1, recipeChecksum: '6'.repeat(64),
        qualityChecksum: 'a'.repeat(64), rubyRegionCount: 0,
        rubyRegionIdsByPrimary: { 'region-1': [] }, imageRevision: 20 },
    });
    const g7Enqueue = pageEvent(15, 'mask-job-enqueued', {
      gate: 'G7_mask', state: 'pending', stage: 'mask', decision: null, reason: 'job-enqueued',
      inputChecksum: '5'.repeat(64), outputChecksum: '5'.repeat(64), parentChecksum: '3'.repeat(64),
      provider: 'deterministic-mask', modelVersion: 'create-mask-v1', parameterHash: '6'.repeat(64),
      jobId: 'job-mask', jobItemId: 'item-mask',
      evidence: { eventType: 'job-enqueued', qualityState: 'pending-review', targetKind: 'image', eligibleRegionCount: 1,
        rubyRegionCount: 0, recipeChecksum: '6'.repeat(64), qualityChecksum: 'a'.repeat(64),
        rubyRegionIdsByPrimary: { 'region-1': [] } },
    });
    const g7Produced = pageEvent(16, 'mask-artifact-produced', {
      gate: 'G7_mask', state: 'pending', stage: 'mask', decision: null, reason: 'mask-review-required',
      inputChecksum: '5'.repeat(64), outputChecksum: '7'.repeat(64), parentChecksum: '3'.repeat(64),
      provider: 'deterministic-mask', modelVersion: 'create-mask-v1', jobId: 'job-mask', jobItemId: 'item-mask',
      parameterHash: '6'.repeat(64), revisionId: 'revision-mask-artifact-1',
      evidence: { eventType: 'mask-artifact-produced', qualityState: 'pending-review', targetKind: 'page-mask',
        artifactId: 'artifact-1', recipeChecksum: '6'.repeat(64), maskChecksum: '8'.repeat(64),
        qualityChecksum: 'a'.repeat(64), rubyRegionIdsByPrimary: { 'region-1': [] },
        provider: 'deterministic-mask', modelVersion: 'create-mask-v1', parameterHash: '6'.repeat(64),
        eligibleRegionCount: 1, rubyRegionCount: 0, nonzeroPixelCount: 42,
        width: 1200, height: 1800, renderScale: 2, bbox: { x: 20, y: 30, width: 100, height: 120 },
        imageRevision: 21 },
    });
    const g7Completed = pageEvent(17, 'mask-job-completed', {
      gate: 'G7_mask', state: 'pending', stage: 'mask', decision: null, reason: 'review-required',
      inputChecksum: '5'.repeat(64), outputChecksum: '7'.repeat(64), parentChecksum: '3'.repeat(64),
      provider: 'deterministic-mask', modelVersion: 'create-mask-v1', jobId: 'job-mask', jobItemId: 'item-mask',
      parameterHash: '6'.repeat(64),
      evidence: { eventType: 'job-completed', qualityState: 'pending-review', targetKind: 'image',
        artifactId: 'artifact-1', recipeChecksum: '6'.repeat(64), maskChecksum: '8'.repeat(64),
        qualityChecksum: 'a'.repeat(64), rubyRegionIdsByPrimary: { 'region-1': [] },
        eligibleRegionCount: 1, rubyRegionCount: 0, nonzeroPixelCount: 42,
        width: 1200, height: 1800, renderScale: 2, bbox: { x: 20, y: 30, width: 100, height: 120 },
        provider: 'deterministic-mask', modelVersion: 'create-mask-v1', parameterHash: '6'.repeat(64) },
    });
    const coverageChecks = MASK_COVERAGE_CHECKS.map((check) => ({ check, passed: true }));
    const collateralChecks = MASK_COLLATERAL_CHECKS.map((check) => ({ check, passed: true }));
    const g7Accepted = pageEvent(18, 'mask-stage-review', {
      gate: 'G7_mask', state: 'accepted', stage: 'mask', decision: 'mask-accepted',
      reason: 'complete-and-no-collateral', inputChecksum: '7'.repeat(64), outputChecksum: '9'.repeat(64),
      parentChecksum: '3'.repeat(64), provider: 'deterministic-mask', modelVersion: 'create-mask-v1',
      parameterHash: '6'.repeat(64), jobId: null, jobItemId: null,
      revisionId: 'revision-mask-review-1',
      evidence: { eventType: 'mask-stage-review', qualityState: 'accepted', artifactId: 'artifact-1',
        maskChecksum: '8'.repeat(64), recipeChecksum: '6'.repeat(64), eligibleRegionCount: 1,
        qualityChecksum: 'a'.repeat(64), rubyRegionCount: 0, rubyRegionIdsByPrimary: { 'region-1': [] },
        coverageChecks, collateralChecks, imageRevision: 22 },
    });
    const g7Base = [...reviewedG6, acceptedG6, g7Draft, g7Enqueue, g7Produced, g7Completed];
    expect(deriveWorkflowPhase(pageGeneration(18), g7Base)).toBe('G7');
    expect(deriveWorkflowPhase(pageGeneration(18), [
      ...reviewedG6, acceptedG6, { ...g7Draft, revisionId: null },
      g7Enqueue, g7Produced, g7Completed,
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(18), [
      ...reviewedG6, acceptedG6, g7Draft, g7Enqueue,
      { ...g7Produced, revisionId: null }, g7Completed,
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(18), [
      ...reviewedG6, acceptedG6, g7Draft,
      { ...g7Enqueue, revisionId: 'unexpected-job-revision' }, g7Produced, g7Completed,
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(18), [
      ...reviewedG6, acceptedG6, g7Draft,
      { ...g7Enqueue, id: g7Draft.id }, g7Produced, g7Completed,
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(18), [
      ...reviewedG6, acceptedG6, g7Draft, g7Enqueue,
      { ...g7Produced, revisionId: g7Draft.revisionId }, g7Completed,
    ])).toBe('locked');
    const draftDuringActiveJob = {
      ...g7Draft,
      id: 'event-16-draft',
      revisionId: 'revision-mask-draft-active-job',
      sequence: 16,
      inputChecksum: '5'.repeat(64),
      outputChecksum: '4'.repeat(64),
    };
    expect(deriveWorkflowPhase(pageGeneration(17), [
      ...reviewedG6,
      acceptedG6,
      g7Draft,
      g7Enqueue,
      draftDuringActiveJob,
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(18), [
      ...reviewedG6, acceptedG6, g7Draft, g7Enqueue, g7Produced,
      { ...g7Completed, evidence: { ...g7Completed.evidence, rubyRegionCount: 1 } },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(19), [...g7Base, g7Accepted])).toBe('G8');
    expect(deriveWorkflowPhase(pageGeneration(19), [
      ...g7Base, { ...g7Accepted, revisionId: null },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(19), [...g7Base, {
      ...g7Accepted, evidence: { ...g7Accepted.evidence, maskChecksum: '0'.repeat(64) },
    }])).toBe('locked');
    const aiRouteManifest = [{
      regionId: 'region-1', backgroundCategory: 'complex-lineart',
      route: 'ai-inpaint-redraw', originKind: 'ai', provider: 'lama',
      modelVersion: 'lama-onnx-local-v1', parameterHash: '6'.repeat(64),
    }];
    const g8Enqueue = pageEvent(19, 'inpaint-job-enqueued', {
      gate: 'G8_cleanPlate', state: 'pending', stage: 'inpaint', decision: null,
      reason: 'job-enqueued', inputChecksum: '9'.repeat(64), outputChecksum: '9'.repeat(64),
      parentChecksum: '9'.repeat(64), provider: 'lama', modelVersion: 'route-manifest-v1',
      parameterHash: 'd'.repeat(64), jobId: 'job-inpaint', jobItemId: 'item-inpaint',
      revisionId: null, evidence: {
        eventType: 'job-enqueued', qualityState: 'pending-review', targetKind: 'image',
        g7Checksum: '9'.repeat(64), backgroundChecksum: 'a'.repeat(64),
        qualityChecksum: 'b'.repeat(64), maskArtifactId: 'artifact-1',
        maskChecksum: '8'.repeat(64), routeManifest: aiRouteManifest,
        routeChecksum: 'd'.repeat(64),
      },
    });
    const g8Produced = pageEvent(20, 'clean-plate-candidate-produced', {
      gate: 'G8_cleanPlate', state: 'pending', stage: 'inpaint', decision: null,
      reason: 'clean-plate-review-required', inputChecksum: '9'.repeat(64),
      outputChecksum: 'f'.repeat(64), parentChecksum: '9'.repeat(64), provider: 'lama',
      modelVersion: 'route-manifest-v1', parameterHash: 'd'.repeat(64),
      jobId: 'job-inpaint', jobItemId: 'item-inpaint', revisionId: 'revision-clean-1',
      evidence: {
        eventType: 'clean-plate-candidate-produced', qualityState: 'pending-review',
        targetKind: 'clean-plate-candidate', candidateId: 'candidate-1',
        candidateChecksum: 'e'.repeat(64), g7Checksum: '9'.repeat(64),
        backgroundChecksum: 'a'.repeat(64), qualityChecksum: 'b'.repeat(64),
        maskArtifactId: 'artifact-1', maskChecksum: '8'.repeat(64),
        routeManifest: aiRouteManifest, routeChecksum: 'd'.repeat(64), originKind: 'ai',
        providerIds: ['lama'], modelVersions: ['lama-onnx-local-v1'],
        parameterHash: 'd'.repeat(64), width: 1200, height: 1800, renderScale: 1,
        outsideMaskChangeCount: 0, anomalies: [], imageRevision: 23,
      },
    });
    const g8Completed = pageEvent(21, 'inpaint-job-completed', {
      gate: 'G8_cleanPlate', state: 'pending', stage: 'inpaint', decision: null,
      reason: 'review-required', inputChecksum: '9'.repeat(64), outputChecksum: 'f'.repeat(64),
      parentChecksum: '9'.repeat(64), provider: 'lama', modelVersion: 'route-manifest-v1',
      parameterHash: 'd'.repeat(64), jobId: 'job-inpaint', jobItemId: 'item-inpaint',
      revisionId: null, evidence: {
        eventType: 'job-completed', qualityState: 'pending-review', targetKind: 'image',
        candidateId: 'candidate-1', candidateChecksum: 'e'.repeat(64),
        maskArtifactId: 'artifact-1', maskChecksum: '8'.repeat(64),
        routeChecksum: 'd'.repeat(64), outsideMaskChangeCount: 0,
      },
    });
    const rejectedChecks = CLEAN_PLATE_CHECKS.map((check, index) => ({
      check, passed: index !== 0,
    }));
    const g8Rejected = pageEvent(22, 'clean-plate-stage-review', {
      gate: 'G8_cleanPlate', state: 'rejected', stage: 'inpaint',
      decision: 'clean-plate-rejected', reason: 'outside-mask-changed',
      inputChecksum: 'f'.repeat(64), outputChecksum: '0'.repeat(64),
      parentChecksum: '9'.repeat(64), provider: 'lama', modelVersion: 'route-manifest-v1',
      parameterHash: 'd'.repeat(64), jobId: null, jobItemId: null,
      revisionId: 'revision-clean-review-1', evidence: {
        eventType: 'clean-plate-stage-review', qualityState: 'rejected',
        candidateId: 'candidate-1', candidateChecksum: 'e'.repeat(64),
        g7Checksum: '9'.repeat(64), backgroundChecksum: 'a'.repeat(64),
        qualityChecksum: 'b'.repeat(64), maskArtifactId: 'artifact-1',
        maskChecksum: '8'.repeat(64), routeChecksum: 'd'.repeat(64),
        originKind: 'ai', checks: rejectedChecks, imageRevision: 24,
      },
    });
    const g8Base = [...g7Base, g7Accepted, g8Enqueue, g8Produced, g8Completed];
    expect(deriveWorkflowPhase(pageGeneration(22), g8Base)).toBe('G8');
    expect(deriveWorkflowPhase(pageGeneration(23), [...g8Base, g8Rejected])).toBe('G8');
    const tamperedG8Events = [
      { ...g8Enqueue, evidence: { ...g8Enqueue.evidence, unexpected: true } },
      { ...g8Produced, actor: { ...g8Produced.actor, actorId: 'different-worker' } },
      { ...g8Produced, evidence: { ...g8Produced.evidence, qualityChecksum: 'c'.repeat(64) } },
      { ...g8Produced, evidence: { ...g8Produced.evidence, outsideMaskChangeCount: 1 } },
      { ...g8Produced, evidence: { ...g8Produced.evidence, imageRevision: 22 } },
      { ...g8Completed, evidence: { ...g8Completed.evidence, unexpected: true } },
    ];
    for (const tampered of tamperedG8Events) {
      const suffix = tampered.operation === 'inpaint-job-enqueued' ? [tampered]
        : tampered.operation === 'clean-plate-candidate-produced'
          ? [g8Enqueue, tampered] : [g8Enqueue, g8Produced, tampered];
      expect(deriveWorkflowPhase(
        pageGeneration(19 + suffix.length), [...g7Base, g7Accepted, ...suffix],
      )).toBe('locked');
    }
    const classicalWithoutFallback = {
      ...g8Enqueue,
      evidence: { ...g8Enqueue.evidence, routeManifest: [{
        ...aiRouteManifest[0]!, route: 'classical-fallback', originKind: 'classical',
        provider: 'opencv', modelVersion: 'telea-v1',
      }] },
      provider: 'opencv',
    };
    expect(deriveWorkflowPhase(
      pageGeneration(20), [...g7Base, g7Accepted, classicalWithoutFallback],
    )).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(19), [
      ...g7Base,
      { ...g8Enqueue, id: 'event-18-g8-early', sequence: 18 },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(23), [
      ...g8Base,
      { ...g8Rejected, revisionId: g8Produced.revisionId },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(24), [
      ...g8Base, g8Rejected,
      { ...g8Rejected, id: 'event-23-review-duplicate', sequence: 23,
        inputChecksum: '0'.repeat(64), outputChecksum: '1'.repeat(64),
        revisionId: 'revision-clean-review-duplicate',
        evidence: { ...g8Rejected.evidence, imageRevision: 25 } },
    ])).toBe('locked');
    const fallbackEnabled = pageEvent(23, 'clean-plate-fallback-enabled', {
      gate: 'G8_cleanPlate', state: 'pending', stage: 'inpaint',
      decision: 'classical-fallback-enabled', reason: 'all-ai-candidates-rejected',
      inputChecksum: '0'.repeat(64), outputChecksum: '1'.repeat(64),
      parentChecksum: '9'.repeat(64), provider: 'operator',
      modelVersion: 'page-scoped-fallback-v1', parameterHash: '7'.repeat(64),
      jobId: null, jobItemId: null, revisionId: 'revision-fallback-1', evidence: {
        eventType: 'clean-plate-fallback-enabled', qualityState: 'pending-review',
        enabled: true, candidateCount: 1, aiCandidateCount: 1, imageRevision: 25,
      },
    });
    expect(deriveWorkflowPhase(pageGeneration(24), [
      ...g8Base, g8Rejected, fallbackEnabled,
    ])).toBe('G8');
    const classicalRouteManifest = [{
      ...aiRouteManifest[0]!, route: 'classical-fallback', originKind: 'classical',
      provider: 'opencv', modelVersion: 'telea-v1', parameterHash: '8'.repeat(64),
    }];
    const classicalEnqueue = pageEvent(24, 'inpaint-job-enqueued', {
      ...g8Enqueue, id: 'event-24-classical-enqueue', sequence: 24,
      inputChecksum: '1'.repeat(64), outputChecksum: '1'.repeat(64),
      provider: 'opencv', parameterHash: '2'.repeat(64),
      jobId: 'job-classical', jobItemId: 'item-classical', evidence: {
        ...g8Enqueue.evidence, routeManifest: classicalRouteManifest,
        routeChecksum: '2'.repeat(64),
      },
    });
    const classicalProduced = pageEvent(25, 'clean-plate-candidate-produced', {
      ...g8Produced, id: 'event-25-classical-produced', sequence: 25,
      inputChecksum: '1'.repeat(64), outputChecksum: '4'.repeat(64),
      provider: 'opencv', parameterHash: '2'.repeat(64),
      jobId: 'job-classical', jobItemId: 'item-classical',
      revisionId: 'revision-clean-2', evidence: {
        ...g8Produced.evidence, candidateId: 'candidate-2', candidateChecksum: '3'.repeat(64),
        routeManifest: classicalRouteManifest, routeChecksum: '2'.repeat(64),
        originKind: 'classical', providerIds: ['opencv'], modelVersions: ['telea-v1'],
        parameterHash: '2'.repeat(64), imageRevision: 26,
      },
    });
    const classicalCompleted = pageEvent(26, 'inpaint-job-completed', {
      ...g8Completed, id: 'event-26-classical-completed', sequence: 26,
      inputChecksum: '1'.repeat(64), outputChecksum: '4'.repeat(64),
      provider: 'opencv', parameterHash: '2'.repeat(64),
      jobId: 'job-classical', jobItemId: 'item-classical', evidence: {
        ...g8Completed.evidence, candidateId: 'candidate-2',
        candidateChecksum: '3'.repeat(64), routeChecksum: '2'.repeat(64),
      },
    });
    const acceptedChecks = CLEAN_PLATE_CHECKS.map((check) => ({ check, passed: true }));
    const classicalAccepted = pageEvent(27, 'clean-plate-stage-review', {
      ...g8Rejected, id: 'event-27-classical-accepted', sequence: 27,
      state: 'accepted', decision: 'clean-plate-accepted', reason: 'clean-plate-complete',
      inputChecksum: '4'.repeat(64), outputChecksum: '5'.repeat(64),
      provider: 'opencv', parameterHash: '2'.repeat(64),
      revisionId: 'revision-clean-review-2', evidence: {
        ...g8Rejected.evidence, qualityState: 'accepted', candidateId: 'candidate-2',
        candidateChecksum: '3'.repeat(64), routeChecksum: '2'.repeat(64),
        originKind: 'classical', checks: acceptedChecks, imageRevision: 27,
      },
    });
    const acceptedG8 = [
      ...g8Base, g8Rejected, fallbackEnabled,
      classicalEnqueue, classicalProduced, classicalCompleted, classicalAccepted,
    ];
    expect(deriveWorkflowPhase(pageGeneration(28), acceptedG8)).toBe('G9');
    const g9Revision = pageEvent(28, 'translation-candidate-revised', {
      gate: 'G9_translation', stage: 'translation', state: 'pending', decision: 'candidate-revised',
      inputChecksum: '5'.repeat(64), outputChecksum: '6'.repeat(64), parentChecksum: '5'.repeat(64),
      jobId: null, jobItemId: null, evidence: { candidateId: 'translation-candidate-1', regionId: 'region-1' },
    });
    const g9Review = pageEvent(29, 'translation-candidate-reviewed', {
      gate: 'G9_translation', stage: 'translation', state: 'accepted', decision: 'candidate-accepted',
      reason: 'translation-reviewed', inputChecksum: '6'.repeat(64), outputChecksum: '7'.repeat(64),
      parentChecksum: '5'.repeat(64), jobId: null, jobItemId: null,
      evidence: { candidateId: 'translation-candidate-1', regionId: 'region-1', qcFlagCount: 1 },
    });
    const g9Accepted = pageEvent(30, 'translation-stage-review', {
      gate: 'G9_translation', stage: 'translation', state: 'accepted', decision: 'translations-accepted',
      reason: 'all-translations-reviewed', inputChecksum: '7'.repeat(64), outputChecksum: '8'.repeat(64),
      parentChecksum: '5'.repeat(64), jobId: null, jobItemId: null,
      evidence: { eligibleRegionCount: 1, reviewedCandidateCount: 1, acceptedRegionCount: 1 },
    });
    expect(deriveWorkflowPhase(pageGeneration(29), [...acceptedG8, g9Revision])).toBe('G9');
    expect(deriveWorkflowPhase(pageGeneration(30), [...acceptedG8, g9Revision, g9Review])).toBe('G9');
    expect(deriveWorkflowPhase(pageGeneration(31), [...acceptedG8, g9Revision, g9Review, g9Accepted])).toBe('G10');
    const g10Enqueue = pageEvent(31, 'typeset-job-enqueued', {
      gate: 'G10_typeset', stage: 'typeset', state: 'pending', decision: null,
      reason: 'job-enqueued', inputChecksum: '8'.repeat(64), outputChecksum: '8'.repeat(64),
      parentChecksum: '8'.repeat(64), provider: 'pillow-g10', modelVersion: 'g10-typeset-v1',
      parameterHash: '1'.repeat(64), jobId: 'job-typeset', jobItemId: 'item-typeset',
      revisionId: null, finishedAt: null, evidence: {
        eventType: 'job-enqueued', qualityState: 'pending-review', targetKind: 'image',
        regionCount: 2, renderRegionCount: 1, g9TerminalChecksum: '8'.repeat(64),
        cleanPlateChecksum: '9'.repeat(64), routeChecksum: 'a'.repeat(64),
        styleChecksum: 'b'.repeat(64),
      },
    });
    const g10Produced = pageEvent(32, 'typeset-candidate-produced', {
      gate: 'G10_typeset', stage: 'typeset', state: 'pending', decision: 'candidate-produced',
      reason: 'typeset-review-required', inputChecksum: '8'.repeat(64),
      outputChecksum: 'c'.repeat(64), parentChecksum: '8'.repeat(64),
      provider: 'pillow-g10', modelVersion: 'g10-typeset-v1', parameterHash: '1'.repeat(64),
      jobId: 'job-typeset', jobItemId: 'item-typeset', revisionId: 'revision-typeset-1',
      evidence: {
        eventType: 'typeset-candidate-produced', qualityState: 'pending-review',
        targetKind: 'typeset-candidate', candidateId: 'typeset-candidate-1',
        candidateChecksum: 'd'.repeat(64), regionCount: 2, renderRegionCount: 1,
        g9TerminalChecksum: '8'.repeat(64), cleanPlateChecksum: '9'.repeat(64),
        routeChecksum: 'a'.repeat(64), styleChecksum: 'b'.repeat(64),
        layoutChecksum: 'e'.repeat(64), width: 1200, height: 1800, renderScale: 1,
        overflowRegionIds: [], anomalies: [],
      },
    });
    const g10Completed = pageEvent(33, 'typeset-job-completed', {
      gate: 'G10_typeset', stage: 'typeset', state: 'pending', decision: null,
      reason: 'review-required', inputChecksum: '8'.repeat(64), outputChecksum: 'c'.repeat(64),
      parentChecksum: '8'.repeat(64), provider: 'pillow-g10', modelVersion: 'g10-typeset-v1',
      parameterHash: '1'.repeat(64), jobId: 'job-typeset', jobItemId: 'item-typeset',
      revisionId: null, evidence: {
        eventType: 'job-completed', qualityState: 'pending-review', targetKind: 'image',
        candidateId: 'typeset-candidate-1', candidateChecksum: 'd'.repeat(64),
        g9TerminalChecksum: '8'.repeat(64), cleanPlateChecksum: '9'.repeat(64),
        routeChecksum: 'a'.repeat(64), styleChecksum: 'b'.repeat(64),
        layoutChecksum: 'e'.repeat(64), width: 1200, height: 1800, renderScale: 1,
        overflowRegionIds: [], anomalies: [],
      },
    });
    const g10Checks = TYPESET_CHECKS.map((check) => ({ check, passed: true }));
    const g10Accepted = pageEvent(34, 'typeset-candidate-reviewed', {
      gate: 'G10_typeset', stage: 'typeset', state: 'accepted', decision: 'candidate-accepted',
      reason: 'typeset-reviewed', inputChecksum: 'c'.repeat(64), outputChecksum: 'f'.repeat(64),
      parentChecksum: '8'.repeat(64), provider: 'pillow-g10', modelVersion: 'g10-typeset-v1',
      parameterHash: '1'.repeat(64), jobId: null, jobItemId: null,
      revisionId: 'revision-typeset-review-1', startedAt: null, finishedAt: null, evidence: {
        eventType: 'typeset-candidate-reviewed', qualityState: 'accepted',
        targetKind: 'typeset-candidate', candidateId: 'typeset-candidate-1',
        candidateChecksum: 'd'.repeat(64), g9TerminalChecksum: '8'.repeat(64),
        cleanPlateChecksum: '9'.repeat(64), routeChecksum: 'a'.repeat(64),
        styleChecksum: 'b'.repeat(64), layoutChecksum: 'e'.repeat(64),
        width: 1200, height: 1800, renderScale: 1, overflowRegionIds: [], anomalies: [],
        checks: g10Checks,
      },
    });
    const throughG9 = [...acceptedG8, g9Revision, g9Review, g9Accepted];
    expect(deriveWorkflowPhase(pageGeneration(32), [...throughG9, g10Enqueue])).toBe('G10');
    expect(deriveWorkflowPhase(pageGeneration(33), [...throughG9, g10Enqueue, g10Produced])).toBe('G10');
    expect(deriveWorkflowPhase(pageGeneration(34), [
      ...throughG9, g10Enqueue, g10Produced, g10Completed,
    ])).toBe('G10');
    expect(deriveWorkflowPhase(pageGeneration(35), [
      ...throughG9, g10Enqueue, g10Produced, g10Completed, g10Accepted,
    ])).toBe('G10');
    const uncachedContext: G4PageContext = {
      status: 'active', generation: pageGeneration(35),
      events: [...throughG9, g10Enqueue, g10Produced, g10Completed, g10Accepted],
      error: '', conflict: false,
    };
    expect(uncachedContext).not.toHaveProperty('phase');
    expect(workflowPhase(uncachedContext)).toBe('G10');
    expect(deriveWorkflowPhase(pageGeneration(34), [
      ...throughG9, g10Enqueue, g10Produced,
      { ...g10Completed, inputChecksum: 'c'.repeat(64) },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(33), [
      ...throughG9, g10Enqueue, { ...g10Produced, revisionId: null },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(33), [
      ...throughG9, g10Enqueue,
      { ...g10Produced, actor: { ...g10Produced.actor, actorId: 'other-worker' } },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(36), [
      ...throughG9, g10Enqueue, g10Produced, g10Completed, g10Accepted,
      { ...g10Enqueue, id: 'event-35-after-terminal', sequence: 35,
        inputChecksum: 'f'.repeat(64), outputChecksum: 'f'.repeat(64),
        jobId: 'job-typeset-2', jobItemId: 'item-typeset-2' },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(29), [...acceptedG8, {
      ...g9Revision, evidence: { ...g9Revision.evidence, translationText: '不能出现在事件中' },
    }])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(29), [
      ...acceptedG8,
      { ...fallbackEnabled, id: 'event-28-after-terminal', sequence: 28,
        inputChecksum: '5'.repeat(64), outputChecksum: '6'.repeat(64),
        revisionId: 'revision-after-terminal', evidence: {
          ...fallbackEnabled.evidence, candidateCount: 2, aiCandidateCount: 1,
          imageRevision: 28,
        } },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(28), [
      ...acceptedG8.slice(0, -1),
      { ...classicalAccepted, evidence: {
        ...classicalAccepted.evidence, checks: acceptedChecks.slice(0, -1),
      } },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(21), [
      ...g7Base, g7Accepted, g8Enqueue,
      { ...fallbackEnabled, id: 'event-20-fallback-early', sequence: 20,
        inputChecksum: '9'.repeat(64),
        evidence: { ...fallbackEnabled.evidence, candidateCount: 0, aiCandidateCount: 0,
          imageRevision: 23 } },
    ])).toBe('locked');
    const g7Failed = pageEvent(16, 'mask-job-failed', {
      gate: 'G7_mask', state: 'blocked', stage: 'mask', decision: null, reason: 'job-execution-failed',
      inputChecksum: '5'.repeat(64), outputChecksum: null, parentChecksum: '3'.repeat(64),
      provider: 'deterministic-mask', modelVersion: 'create-mask-v1', parameterHash: '6'.repeat(64),
      jobId: 'job-mask', jobItemId: 'item-mask',
      evidence: { eventType: 'job-failed', qualityState: 'blocked', targetKind: 'image',
        recipeChecksum: '6'.repeat(64), qualityChecksum: 'a'.repeat(64), eligibleRegionCount: 1,
        rubyRegionCount: 0, rubyRegionIdsByPrimary: { 'region-1': [] },
        provider: 'deterministic-mask', modelVersion: 'create-mask-v1', parameterHash: '6'.repeat(64) },
    });
    const retryEnqueue = { ...g7Enqueue, id: 'event-17', sequence: 17,
      jobId: 'job-mask-retry', jobItemId: 'item-mask-retry' };
    const retryProduced = { ...g7Produced, id: 'event-18', sequence: 18,
      revisionId: 'revision-mask-artifact-retry',
      inputChecksum: '5'.repeat(64), outputChecksum: '7'.repeat(64),
      jobId: 'job-mask-retry', jobItemId: 'item-mask-retry',
      evidence: { ...g7Produced.evidence, artifactId: 'artifact-retry' } };
    const retryCompleted = { ...g7Completed, id: 'event-19', sequence: 19,
      inputChecksum: '5'.repeat(64), outputChecksum: '7'.repeat(64),
      jobId: 'job-mask-retry', jobItemId: 'item-mask-retry',
      evidence: { ...g7Completed.evidence, artifactId: 'artifact-retry' } };
    expect(deriveWorkflowPhase(pageGeneration(20), [
      ...reviewedG6, acceptedG6, g7Draft, g7Enqueue, g7Failed,
      retryEnqueue, retryProduced, retryCompleted,
    ])).toBe('G7');
    expect(deriveWorkflowPhase(pageGeneration(17), [
      ...reviewedG6, acceptedG6, g7Draft, g7Enqueue,
      { ...g7Failed, modelVersion: null },
    ])).toBe('locked');
    const secondEnqueue = { ...g7Enqueue, id: 'event-18', sequence: 18,
      inputChecksum: '7'.repeat(64), outputChecksum: '7'.repeat(64),
      jobId: 'job-mask-2', jobItemId: 'item-mask-2' };
    const secondProduced = { ...g7Produced, id: 'event-19', sequence: 19,
      revisionId: 'revision-mask-artifact-2',
      inputChecksum: '7'.repeat(64), outputChecksum: '0'.repeat(64),
      jobId: 'job-mask-2', jobItemId: 'item-mask-2',
      evidence: { ...g7Produced.evidence, artifactId: 'artifact-2', maskChecksum: '1'.repeat(64), imageRevision: 22 } };
    const secondCompleted = { ...g7Completed, id: 'event-20', sequence: 20,
      inputChecksum: '7'.repeat(64), outputChecksum: '0'.repeat(64),
      jobId: 'job-mask-2', jobItemId: 'item-mask-2',
      evidence: { ...g7Completed.evidence, artifactId: 'artifact-2', maskChecksum: '1'.repeat(64) } };
    expect(deriveWorkflowPhase(pageGeneration(21), [...g7Base, secondEnqueue, secondProduced, secondCompleted])).toBe('G7');
    const duplicateArtifactProduced = {
      ...secondProduced,
      evidence: {
        ...secondProduced.evidence,
        artifactId: 'artifact-1',
        maskChecksum: '8'.repeat(64),
      },
    };
    const duplicateArtifactCompleted = {
      ...secondCompleted,
      evidence: {
        ...secondCompleted.evidence,
        artifactId: 'artifact-1',
        maskChecksum: '8'.repeat(64),
      },
    };
    expect(deriveWorkflowPhase(pageGeneration(21), [
      ...g7Base,
      secondEnqueue,
      duplicateArtifactProduced,
      duplicateArtifactCompleted,
    ])).toBe('locked');
    const rejectedFirst = {
      ...g7Accepted,
      id: 'event-21',
      sequence: 21,
      revisionId: 'revision-mask-review-rejected',
      state: 'rejected' as const,
      decision: 'mask-rejected',
      reason: 'coverage-incomplete',
      inputChecksum: '0'.repeat(64),
      outputChecksum: '2'.repeat(64),
      evidence: {
        ...g7Accepted.evidence,
        qualityState: 'rejected',
        imageRevision: 23,
        coverageChecks: coverageChecks.map((entry, index) => ({
          ...entry,
          passed: index === 0 ? false : entry.passed,
        })),
      },
    };
    const laterDraft = {
      ...g7Draft,
      id: 'event-22',
      sequence: 22,
      revisionId: 'revision-mask-draft-2',
      inputChecksum: '2'.repeat(64),
      outputChecksum: '4'.repeat(64),
      evidence: { ...g7Draft.evidence, imageRevision: 24 },
    };
    const staleSecondAccepted = {
      ...g7Accepted,
      id: 'event-23',
      sequence: 23,
      revisionId: 'revision-mask-review-stale',
      inputChecksum: '4'.repeat(64),
      outputChecksum: '9'.repeat(64),
      evidence: {
        ...g7Accepted.evidence,
        artifactId: 'artifact-2',
        maskChecksum: '1'.repeat(64),
        imageRevision: 25,
      },
    };
    expect(deriveWorkflowPhase(pageGeneration(24), [
      ...g7Base,
      secondEnqueue,
      secondProduced,
      secondCompleted,
      rejectedFirst,
      laterDraft,
      staleSecondAccepted,
    ])).toBe('locked');
    const g7NA = pageEvent(14, 'mask-stage-review', {
      gate: 'G7_mask', state: 'not-applicable', stage: 'mask', decision: 'mask-not-applicable',
      reason: 'no-eligible-regions', inputChecksum: '3'.repeat(64), outputChecksum: '4'.repeat(64),
      parentChecksum: '3'.repeat(64), provider: 'deterministic-mask', modelVersion: 'create-mask-v1',
      parameterHash: '5'.repeat(64),
      jobId: null, jobItemId: null, revisionId: 'revision-mask-review-na', evidence: { eventType: 'mask-stage-review', qualityState: 'not-applicable',
        artifactId: null, maskChecksum: null, recipeChecksum: '5'.repeat(64), qualityChecksum: 'a'.repeat(64),
        eligibleRegionCount: 0, rubyRegionCount: 0, rubyRegionIdsByPrimary: {}, coverageChecks: [],
        collateralChecks: [], imageRevision: 20 },
    });
    expect(deriveWorkflowPhase(pageGeneration(15), [...reviewedG6, acceptedG6, g7NA])).toBe('G8');
    const g8NA = pageEvent(15, 'clean-plate-stage-review', {
      gate: 'G8_cleanPlate', state: 'not-applicable', stage: 'inpaint',
      decision: 'clean-plate-not-applicable', reason: 'no-clean-plate-required',
      inputChecksum: '4'.repeat(64), outputChecksum: '6'.repeat(64),
      parentChecksum: '4'.repeat(64), provider: 'none',
      modelVersion: 'quality-plate-pass-through-v1', parameterHash: 'b'.repeat(64),
      jobId: null, jobItemId: null, revisionId: 'revision-clean-review-na', evidence: {
        eventType: 'clean-plate-stage-review', qualityState: 'not-applicable',
        candidateId: null, candidateChecksum: null, g7Checksum: '4'.repeat(64),
        backgroundChecksum: 'b'.repeat(64), qualityChecksum: 'a'.repeat(64),
        maskArtifactId: null, maskChecksum: null, routeChecksum: null,
        originKind: 'no-op', checks: [], imageRevision: 21,
      },
    });
    expect(deriveWorkflowPhase(pageGeneration(16), [
      ...reviewedG6, acceptedG6, g7NA, g8NA,
    ])).toBe('G9');
    expect(deriveWorkflowPhase(pageGeneration(20), [
      ...g7Base, g7Accepted,
      { ...g8NA, id: 'event-19-na-after-mask', sequence: 19,
        inputChecksum: '9'.repeat(64), outputChecksum: '6'.repeat(64),
        parentChecksum: '9'.repeat(64),
        evidence: { ...g8NA.evidence, g7Checksum: '9'.repeat(64), imageRevision: 23 } },
    ])).toBe('locked');
    const corruptTerminals = [
      { ...acceptedG6, decision: 'ocr-source-text-accepted' },
      { ...acceptedG6, reason: 'all-source-text-reviewed' },
      {
        ...acceptedG6,
        evidence: { ...acceptedG6.evidence, qualityState: 'pending-review' },
      },
      {
        ...acceptedG6,
        evidence: { ...acceptedG6.evidence, reviewedRegionCount: 0 },
      },
    ];
    for (const terminal of corruptTerminals) {
      expect(deriveWorkflowPhase(pageGeneration(14), [...reviewedG6, terminal])).toBe('locked');
    }
    const malformedReview = reviewedG6.map((event) => event.operation === 'ocr-source-reviewed'
      ? { ...event, evidence: { ...event.evidence, selectedAttemptId: '' } }
      : event);
    expect(deriveWorkflowPhase(pageGeneration(13), malformedReview)).toBe('locked');
    const danglingEnqueue = pageEvent(13, 'ocr-job-enqueued', {
      gate: 'G6_ocr', state: 'pending', stage: 'ocr', decision: null, reason: 'job-enqueued',
      inputChecksum: '3'.repeat(64), outputChecksum: '3'.repeat(64),
      parentChecksum: 'f'.repeat(64), provider: 'tesseract',
      jobId: 'job-active', jobItemId: 'item-active',
      evidence: {
        eventType: 'job-enqueued', qualityState: 'pending-review',
        targetKind: 'region-set', eligibleRegionCount: 1,
      },
    });
    expect(deriveWorkflowPhase(pageGeneration(15), [
      ...reviewedG6,
      danglingEnqueue,
      { ...acceptedG6, id: 'event-14', sequence: 14 },
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(15), [
      ...reviewedG6,
      acceptedG6,
      pageEvent(14, 'ocr-source-reviewed', {
        gate: 'G6_ocr',
        state: 'pending',
        inputChecksum: '3'.repeat(64),
        outputChecksum: '4'.repeat(64),
        parentChecksum: 'f'.repeat(64),
        evidence: { targetRegionId: 'region-1' },
      }),
    ])).toBe('locked');
    const failedG6 = [
      ...g6BaseEvents(false),
      pageEvent(9, 'ocr-job-enqueued', {
        gate: 'G6_ocr', state: 'pending',
        stage: 'ocr', decision: null, reason: 'job-enqueued',
        inputChecksum: '1'.repeat(64), outputChecksum: '1'.repeat(64),
        parentChecksum: 'f'.repeat(64), provider: 'tesseract',
        jobId: 'job-failed', jobItemId: 'item-failed',
        evidence: {
          eventType: 'job-enqueued', qualityState: 'pending-review',
          targetKind: 'region-set', eligibleRegionCount: 1,
        },
      }),
      pageEvent(10, 'ocr-job-failed', {
        gate: 'G6_ocr', state: 'blocked',
        stage: 'ocr', decision: null, reason: 'job-execution-failed',
        inputChecksum: '1'.repeat(64), outputChecksum: null,
        parentChecksum: 'f'.repeat(64), provider: 'tesseract',
        jobId: 'job-failed', jobItemId: 'item-failed',
        evidence: {
          eventType: 'job-failed', qualityState: 'blocked', targetKind: 'image',
        },
      }),
    ];
    expect(deriveWorkflowPhase(pageGeneration(11), failedG6)).toBe('G6');
    expect(deriveWorkflowPhase(pageGeneration(4), [noText])).toBe('no-text');
    expect(deriveWorkflowPhase(pageGeneration(3), [pageEvent(2, 'preprocess-stage-review', {
      gate: 'G1_baselineUpscale', state: 'accepted',
    })])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(9), [classified])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(8), [{ ...acceptedG4, generationId: 'wrong' }])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(10), [
      acceptedG4,
      classified,
      pageEvent(9, 'regions-updated'),
    ])).toBe('locked');
    expect(deriveWorkflowPhase(pageGeneration(11), [acceptedG4, {
      ...classified, sequence: 10,
    }])).toBe('locked');
  });

  it('cross-binds cold G7 context to lineage and prefers the newest current-recipe artifact', async () => {
    seedWorkbench({ images: [imageFixture('image-1', { revision: 10 })], regions: [
      regionFixture('region-1', { contentDisposition: 'translate', type: 'dialogue' }),
    ] });
    const reviewer = {
      actorKind: 'human' as const, sessionId: 'reviewer', operationSource: 'ui' as const,
    };
    const rejectedCoverage = MASK_COVERAGE_CHECKS.map((check, index) => ({ check, passed: index > 0 }));
    const acceptedCoverage = MASK_COVERAGE_CHECKS.map((check) => ({ check, passed: true }));
    const acceptedCollateral = MASK_COLLATERAL_CHECKS.map((check) => ({ check, passed: true }));
    const currentDraftRegions = [{ regionId: 'region-1',
      maskMode: 'text' as const, polygon: null, padding: 4, dilation: 2, feather: 1,
      polarity: 'auto' as const, maskEdits: { version: 1 as const, strokes: [] } }];
    const currentDraftChecksum = await g7MaskDraftChecksum(
      'a'.repeat(64), 'b'.repeat(64), { 'region-1': [] }, currentDraftRegions,
    );
    expect(currentDraftChecksum).toBe('31f17311d6524da0e32d4106ef8a86428d3f20a98c2cb552cb05d13f3831c558');
    expect(await g7MaskDraftChecksum(
      'a'.repeat(64),
      'b'.repeat(64),
      { 'region-1': [] },
      [{
        regionId: 'region-1', maskMode: 'manual',
        polygon: [[0, 1.5], [2, 3.25], [4, 5]],
        padding: 4, dilation: 2, feather: 1, polarity: 'auto',
        maskEdits: { version: 1, strokes: [{
          mode: 'add', radius: 1, points: [[0, 1.5], [2, 3.25]],
        }] },
      }],
    )).toBe('96fad7d6a7dca0386b8544d04c53c377575c89f4b946ecf8f839f82e4eb700b0');
    const artifactEvidence = (
      artifactId: string,
      maskChecksum: string,
      recipeChecksum: string,
      imageRevision: number,
    ) => ({
      artifactId, maskChecksum, recipeChecksum, qualityChecksum: 'b'.repeat(64),
      width: 1200, height: 1800, renderScale: 1, nonzeroPixelCount: 42,
      bbox: { x: 1, y: 2, width: 3, height: 4 }, eligibleRegionCount: 1,
      rubyRegionCount: 0, rubyRegionIdsByPrimary: { 'region-1': [] },
      provider: 'deterministic-mask', modelVersion: 'create-mask-v1', parameterHash: recipeChecksum,
      imageRevision,
    });
    const g6Terminal = pageEvent(12, 'ocr-stage-review', {
      gate: 'G6_ocr', state: 'accepted', outputChecksum: 'a'.repeat(64),
    });
    const oldDraft = pageEvent(13, 'mask-draft-updated', {
      gate: 'G7_mask', stage: 'mask', state: 'pending', parentChecksum: 'a'.repeat(64),
      outputChecksum: '0'.repeat(64), evidence: {
        recipeChecksum: '3'.repeat(64), qualityChecksum: 'b'.repeat(64),
        eligibleRegionCount: 1, recipeRegionCount: 1, rubyRegionCount: 0,
        rubyRegionIdsByPrimary: { 'region-1': [] }, imageRevision: 6,
      },
    });
    const oldProduced = pageEvent(14, 'mask-artifact-produced', {
      gate: 'G7_mask', stage: 'mask', state: 'pending', outputChecksum: '1'.repeat(64),
      parentChecksum: 'a'.repeat(64), provider: 'deterministic-mask', modelVersion: 'create-mask-v1',
      parameterHash: '3'.repeat(64), jobId: 'job-1', jobItemId: 'item-1',
      evidence: artifactEvidence('artifact-old', '2'.repeat(64), '3'.repeat(64), 7),
    });
    const rejected = pageEvent(15, 'mask-stage-review', {
      gate: 'G7_mask', stage: 'mask', state: 'rejected', outputChecksum: '4'.repeat(64),
      parentChecksum: 'a'.repeat(64), actor: reviewer, reason: 'coverage-incomplete', evidence: {
        artifactId: 'artifact-old', maskChecksum: '2'.repeat(64),
        recipeChecksum: '3'.repeat(64), qualityChecksum: 'b'.repeat(64),
        eligibleRegionCount: 1, rubyRegionCount: 0, rubyRegionIdsByPrimary: { 'region-1': [] },
        coverageChecks: rejectedCoverage, collateralChecks: acceptedCollateral,
        imageRevision: 8,
      },
    });
    const draftEvent = pageEvent(16, 'mask-draft-updated', {
      gate: 'G7_mask', stage: 'mask', state: 'pending', outputChecksum: '5'.repeat(64),
      parentChecksum: 'a'.repeat(64), evidence: {
        recipeChecksum: currentDraftChecksum, qualityChecksum: 'b'.repeat(64),
        eligibleRegionCount: 1, recipeRegionCount: 1, rubyRegionCount: 0,
        rubyRegionIdsByPrimary: { 'region-1': [] }, imageRevision: 9,
      },
    });
    const newProduced = pageEvent(17, 'mask-artifact-produced', {
      gate: 'G7_mask', stage: 'mask', state: 'pending', outputChecksum: '7'.repeat(64),
      parentChecksum: 'a'.repeat(64), provider: 'deterministic-mask', modelVersion: 'create-mask-v1',
      parameterHash: currentDraftChecksum, jobId: 'job-2', jobItemId: 'item-2',
      evidence: artifactEvidence('artifact-new', '8'.repeat(64), currentDraftChecksum, 10),
    });
    const setActiveG7 = (
      currentDraftEvent = draftEvent,
      currentProducedEvent = newProduced,
    ) => useWorkbenchStore.setState({ g4Contexts: { 'image-1': {
      status: 'active', generation: pageGeneration(18),
      events: [g6Terminal, oldDraft, oldProduced, rejected, currentDraftEvent, currentProducedEvent],
      phase: 'G7', error: '', conflict: false,
    } } });
    const resetActiveG7 = () => setActiveG7();
    resetActiveG7();
    const artifact = (artifactId: string, recipeChecksum: string, maskChecksum: string, sequence: number) => ({
      artifactId, sequence, jobId: `job-${sequence}`, jobItemId: `item-${sequence}`,
      parentChecksum: 'a'.repeat(64), qualityChecksum: 'b'.repeat(64), recipeChecksum, maskChecksum,
      width: 1200, height: 1800, renderScale: 1, provider: 'deterministic-mask',
      modelVersion: 'create-mask-v1', parameterHash: recipeChecksum, nonzeroPixelCount: 42,
      bbox: { x: 1, y: 2, width: 3, height: 4 }, createdAt: '2026-08-25T00:00:00Z',
    });
    const maskContext: MaskGateContext = {
      imageId: 'image-1', imageRevision: 10, generationId: 'generation-1', nextSequence: 18,
      g6Checksum: 'a'.repeat(64), qualityChecksum: 'b'.repeat(64), maskStateChecksum: '7'.repeat(64),
      state: 'rejected' as const, eligibleRegionIds: ['region-1'], rubyRegionIdsByPrimary: { 'region-1': [] },
      draft: { revision: 2, stateChecksum: currentDraftChecksum, regions: currentDraftRegions },
      artifacts: [artifact('artifact-old', '3'.repeat(64), '2'.repeat(64), 1),
        artifact('artifact-new', currentDraftChecksum, '8'.repeat(64), 2)],
      selectedArtifactId: 'artifact-old',
      review: { id: 'review-1', state: 'rejected' as const, reason: 'coverage-incomplete' as const,
        artifactId: 'artifact-old', maskChecksum: '2'.repeat(64),
        coverageChecks: rejectedCoverage,
        collateralChecks: acceptedCollateral,
        reviewer,
        createdAt: '2026-08-25T00:00:00Z' },
    };
    vi.spyOn(api, 'getMaskGateContext').mockResolvedValue(maskContext);
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().selectedMaskArtifactIds['image-1']).toBe('artifact-new');

    const acceptedReviewEvent = pageEvent(18, 'mask-stage-review', {
      gate: 'G7_mask', stage: 'mask', state: 'accepted', inputChecksum: '7'.repeat(64),
      outputChecksum: '9'.repeat(64), parentChecksum: 'a'.repeat(64), actor: reviewer,
      provider: 'deterministic-mask', modelVersion: 'create-mask-v1',
      parameterHash: currentDraftChecksum, revisionId: 'revision-mask-review-accepted',
      decision: 'mask-accepted', reason: 'complete-and-no-collateral', evidence: {
        eventType: 'mask-stage-review', qualityState: 'accepted',
        artifactId: 'artifact-new', maskChecksum: '8'.repeat(64),
        recipeChecksum: currentDraftChecksum, qualityChecksum: 'b'.repeat(64),
        eligibleRegionCount: 1, rubyRegionCount: 0,
        rubyRegionIdsByPrimary: { 'region-1': [] },
        coverageChecks: acceptedCoverage, collateralChecks: acceptedCollateral,
        imageRevision: 11,
      },
    });
    useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => ({ ...image, revision: 12 })),
      g4Contexts: { 'image-1': {
        status: 'active', generation: pageGeneration(19),
        events: [g6Terminal, oldDraft, oldProduced, rejected, draftEvent, newProduced,
          acceptedReviewEvent],
        phase: 'G8', error: '', conflict: false,
      } },
    }));
    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext, imageRevision: 12, nextSequence: 19,
      maskStateChecksum: '9'.repeat(64), state: 'accepted',
      selectedArtifactId: 'artifact-new', review: {
        id: 'review-accepted', state: 'accepted', reason: 'complete-and-no-collateral',
        artifactId: 'artifact-new', maskChecksum: '8'.repeat(64),
        coverageChecks: acceptedCoverage, collateralChecks: acceptedCollateral,
        reviewer, createdAt: '2026-08-25T00:00:00Z',
      },
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(true);

    useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => ({ ...image, revision: 10 })),
    }));
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      draft: { ...maskContext.draft, regions: [{ ...maskContext.draft.regions[0]!, padding: 512 }] },
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      draft: { ...maskContext.draft, regions: [{
        ...maskContext.draft.regions[0]!,
        polygon: [[10, 20, 30] as unknown as [number, number], [30, 40], [50, 60]],
      }] },
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      artifacts: maskContext.artifacts.map((entry) => entry.artifactId === 'artifact-new'
        ? { ...entry, sequence: 99 } : entry),
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      artifacts: [maskContext.artifacts[1]!, maskContext.artifacts[0]!],
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      draft: { ...maskContext.draft, stateChecksum: 'f'.repeat(64) },
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      draft: { ...maskContext.draft, revision: 3 },
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetActiveG7();

    const boundaryRegions = [{
      ...maskContext.draft.regions[0]!, padding: 512, feather: 128,
      polygon: Array.from({ length: 4096 }, (_, index): [number, number] => [index % 1200, index % 1800]),
      maskEdits: { version: 1 as const, strokes: [{
        mode: 'add' as const, radius: 512, points: [[0, 0], [1200, 1800]] as Array<[number, number]>,
      }] },
    }];
    const boundaryChecksum = await g7MaskDraftChecksum(
      maskContext.g6Checksum,
      maskContext.qualityChecksum,
      maskContext.rubyRegionIdsByPrimary,
      boundaryRegions,
    );
    const boundaryDraftEvent = {
      ...draftEvent,
      parameterHash: boundaryChecksum,
      evidence: { ...draftEvent.evidence, recipeChecksum: boundaryChecksum },
    };
    const boundaryProducedEvent = {
      ...newProduced,
      parameterHash: boundaryChecksum,
      evidence: artifactEvidence('artifact-new', '8'.repeat(64), boundaryChecksum, 10),
    };
    const canonicalBoundaryContext: MaskGateContext = {
      ...maskContext,
      draft: { ...maskContext.draft, stateChecksum: boundaryChecksum, regions: boundaryRegions },
      artifacts: maskContext.artifacts.map((entry) => entry.artifactId === 'artifact-new'
        ? { ...entry, recipeChecksum: boundaryChecksum, parameterHash: boundaryChecksum } : entry),
    };
    setActiveG7(boundaryDraftEvent, boundaryProducedEvent);
    vi.mocked(api.getMaskGateContext).mockResolvedValue(canonicalBoundaryContext);
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(true);

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...canonicalBoundaryContext,
      artifacts: canonicalBoundaryContext.artifacts.map((entry) => entry.artifactId === 'artifact-new'
        ? { ...entry, bbox: { x: 2, y: 2, width: 3, height: 4 } } : entry),
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetActiveG7();

    const numericRadiusRegions = [{
      ...maskContext.draft.regions[0]!,
      maskEdits: { version: 1 as const, strokes: [{
        mode: 'add' as const, radius: 1, points: [[20, 20]] as Array<[number, number]>,
      }] },
    }];
    const numericRadiusChecksum = await g7MaskDraftChecksum(
      maskContext.g6Checksum,
      maskContext.qualityChecksum,
      maskContext.rubyRegionIdsByPrimary,
      numericRadiusRegions,
    );
    setActiveG7(
      { ...draftEvent, parameterHash: numericRadiusChecksum,
        evidence: { ...draftEvent.evidence, recipeChecksum: numericRadiusChecksum } },
      { ...newProduced, parameterHash: numericRadiusChecksum,
        evidence: artifactEvidence('artifact-new', '8'.repeat(64), numericRadiusChecksum, 10) },
    );
    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      draft: { ...maskContext.draft, stateChecksum: numericRadiusChecksum, regions: [{
        ...numericRadiusRegions[0]!,
        maskEdits: { version: 1, strokes: [{
          ...numericRadiusRegions[0]!.maskEdits.strokes[0]!,
          radius: true as unknown as number,
        }] },
      }] },
      artifacts: maskContext.artifacts.map((entry) => entry.artifactId === 'artifact-new'
        ? { ...entry, recipeChecksum: numericRadiusChecksum, parameterHash: numericRadiusChecksum }
        : entry),
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      review: maskContext.review ? {
        ...maskContext.review,
        coverageChecks: MASK_COVERAGE_CHECKS.map((check) => ({ check, passed: true })),
      } : null,
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      draft: { ...maskContext.draft, regions: [{
        ...maskContext.draft.regions[0]!,
        maskEdits: { version: 1, strokes: [{ mode: 'add', radius: 1, points: [[1201, 20]] }] },
      }] },
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.error).toContain('G7 上下文');
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      draft: { ...maskContext.draft, regions: [{
        ...maskContext.draft.regions[0]!,
        maskEdits: { version: 1, strokes: [{
          mode: 'xor' as 'add', radius: 1, points: [[20, 20]],
        }] },
      }] },
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.error).toContain('G7 上下文');
    resetActiveG7();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({ ...maskContext, maskStateChecksum: '0'.repeat(64) });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.error).toContain('G7 上下文');
  });

  it('cross-binds an artifact-free G7 N/A context to its exact G6 and review evidence', async () => {
    const reviewer = { actorKind: 'human' as const, sessionId: 'reviewer-na', operationSource: 'ui' as const };
    seedWorkbench({ images: [imageFixture('image-1', { revision: 10 })], regions: [] });
    const emptyDraftChecksum = await g7MaskDraftChecksum(
      'a'.repeat(64), 'b'.repeat(64), {}, [],
    );
    expect(emptyDraftChecksum).toBe('1639a196279487cd4b93b1581e728c29c4825325736a1b5c98bd08a5f6377f86');
    const g6Terminal = pageEvent(12, 'ocr-stage-review', {
      gate: 'G6_ocr', state: 'not-applicable', outputChecksum: 'a'.repeat(64),
    });
    const naReview = pageEvent(13, 'mask-stage-review', {
      gate: 'G7_mask', stage: 'mask', state: 'not-applicable', actor: reviewer,
      parentChecksum: 'a'.repeat(64), inputChecksum: 'a'.repeat(64), outputChecksum: 'c'.repeat(64),
      provider: 'deterministic-mask', modelVersion: 'create-mask-v1', parameterHash: emptyDraftChecksum,
      jobId: null, jobItemId: null, decision: 'mask-not-applicable', reason: 'no-eligible-regions',
      revisionId: 'revision-mask-review-na-context',
      evidence: {
        eventType: 'mask-stage-review', qualityState: 'not-applicable', artifactId: null,
        maskChecksum: null, recipeChecksum: emptyDraftChecksum, qualityChecksum: 'b'.repeat(64),
        eligibleRegionCount: 0, rubyRegionCount: 0, rubyRegionIdsByPrimary: {},
        coverageChecks: [], collateralChecks: [], imageRevision: 10,
      },
    });
    const resetContext = () => useWorkbenchStore.setState({ g4Contexts: { 'image-1': {
      status: 'active', generation: pageGeneration(14), events: [g6Terminal, naReview],
      phase: 'G8', error: '', conflict: false,
    } } });
    resetContext();
    const maskContext: MaskGateContext = {
      imageId: 'image-1', imageRevision: 10, generationId: 'generation-1', nextSequence: 14,
      g6Checksum: 'a'.repeat(64), qualityChecksum: 'b'.repeat(64), maskStateChecksum: 'c'.repeat(64),
      state: 'not-applicable', eligibleRegionIds: [], rubyRegionIdsByPrimary: {},
      draft: { revision: 0, stateChecksum: emptyDraftChecksum, regions: [] },
      artifacts: [], selectedArtifactId: null,
      review: { id: 'review-na', state: 'not-applicable', reason: 'no-eligible-regions',
        artifactId: null, maskChecksum: null, coverageChecks: [], collateralChecks: [], reviewer,
        createdAt: '2026-08-25T00:00:00Z' },
    };
    vi.spyOn(api, 'getMaskGateContext').mockResolvedValue(maskContext);
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(true);

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      qualityChecksum: 'e'.repeat(64),
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
    resetContext();

    vi.mocked(api.getMaskGateContext).mockResolvedValue({
      ...maskContext,
      g6Checksum: 'f'.repeat(64),
    });
    expect(await useWorkbenchStore.getState().loadMaskContext('image-1', true)).toBe(false);
  });

  it('cross-binds G8 context and bitmap evidence to the exact accepted mask', async () => {
    const reviewer = {
      actorKind: 'human' as const, sessionId: 'reviewer-g8', operationSource: 'ui' as const,
    };
    const region = regionFixture('region-1', {
      contentDisposition: 'translate', type: 'dialogue',
      backgroundCategory: 'complex-lineart', backgroundGenerationId: 'generation-1',
    });
    seedWorkbench({
      images: [imageFixture('image-1', { revision: 11, width: 1200, height: 1800 })],
      regions: [region],
    });
    const routeManifest = [{
      regionId: 'region-1', backgroundCategory: 'complex-lineart' as const,
      route: 'ai-inpaint-redraw' as const, originKind: 'ai' as const,
      provider: 'lama', modelVersion: 'lama-onnx-local-v1', parameterHash: '6'.repeat(64),
    }];
    const g7Terminal = pageEvent(18, 'mask-stage-review', {
      gate: 'G7_mask', state: 'accepted', stage: 'mask', outputChecksum: '9'.repeat(64),
    });
    const enqueue = pageEvent(19, 'inpaint-job-enqueued', {
      gate: 'G8_cleanPlate', stage: 'inpaint', parentChecksum: '9'.repeat(64),
      inputChecksum: '9'.repeat(64), outputChecksum: '9'.repeat(64),
      provider: 'lama', modelVersion: 'route-manifest-v1', parameterHash: 'd'.repeat(64),
      jobId: 'job-inpaint', jobItemId: 'item-inpaint', evidence: {
        eventType: 'job-enqueued', qualityState: 'pending-review', targetKind: 'image',
        g7Checksum: '9'.repeat(64), backgroundChecksum: 'a'.repeat(64),
        qualityChecksum: 'b'.repeat(64), maskArtifactId: 'mask-accepted',
        maskChecksum: '8'.repeat(64), routeManifest, routeChecksum: 'd'.repeat(64),
      },
    });
    const produced = pageEvent(20, 'clean-plate-candidate-produced', {
      gate: 'G8_cleanPlate', stage: 'inpaint', parentChecksum: '9'.repeat(64),
      inputChecksum: '9'.repeat(64), outputChecksum: 'f'.repeat(64),
      provider: 'lama', modelVersion: 'route-manifest-v1', parameterHash: 'd'.repeat(64),
      jobId: 'job-inpaint', jobItemId: 'item-inpaint', revisionId: 'revision-clean-1',
      evidence: {
        eventType: 'clean-plate-candidate-produced', qualityState: 'pending-review',
        targetKind: 'clean-plate-candidate', candidateId: 'candidate-1',
        candidateChecksum: 'e'.repeat(64), g7Checksum: '9'.repeat(64),
        backgroundChecksum: 'a'.repeat(64), qualityChecksum: 'b'.repeat(64),
        maskArtifactId: 'mask-accepted', maskChecksum: '8'.repeat(64), routeManifest,
        routeChecksum: 'd'.repeat(64), originKind: 'ai', providerIds: ['lama'],
        modelVersions: ['lama-onnx-local-v1'], parameterHash: 'd'.repeat(64),
        width: 1200, height: 1800, renderScale: 1, outsideMaskChangeCount: 0,
        anomalies: [], imageRevision: 11,
      },
    });
    const completed = pageEvent(21, 'inpaint-job-completed', {
      gate: 'G8_cleanPlate', stage: 'inpaint', parentChecksum: '9'.repeat(64),
      inputChecksum: '9'.repeat(64), outputChecksum: 'f'.repeat(64),
      provider: 'lama', modelVersion: 'route-manifest-v1', parameterHash: 'd'.repeat(64),
      jobId: 'job-inpaint', jobItemId: 'item-inpaint', evidence: {
        eventType: 'job-completed', qualityState: 'pending-review', targetKind: 'image',
        candidateId: 'candidate-1', candidateChecksum: 'e'.repeat(64),
        maskArtifactId: 'mask-accepted', maskChecksum: '8'.repeat(64),
        routeChecksum: 'd'.repeat(64), outsideMaskChangeCount: 0,
      },
    });
    const generation = pageGeneration(22, { sourceChecksum: 'c'.repeat(64) });
    const setLineage = (nextGeneration = generation, events = [g7Terminal, enqueue, produced, completed]) => {
      useWorkbenchStore.setState({ g4Contexts: { 'image-1': {
        status: 'active', generation: nextGeneration, events,
        phase: 'G8', error: '', conflict: false,
      } } });
    };
    setLineage();
    const acceptedMask = {
      artifactId: 'mask-accepted', sequence: 1, jobId: 'job-mask', jobItemId: 'item-mask',
      parentChecksum: '3'.repeat(64), qualityChecksum: 'b'.repeat(64),
      recipeChecksum: '7'.repeat(64), maskChecksum: '8'.repeat(64), width: 1200,
      height: 1800, renderScale: 1, provider: 'deterministic-mask',
      modelVersion: 'create-mask-v1', parameterHash: '7'.repeat(64),
      nonzeroPixelCount: 42, bbox: { x: 1, y: 2, width: 3, height: 4 },
      createdAt: '2026-08-25T00:00:00Z',
    };
    const maskContext: MaskGateContext = {
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 22,
      g6Checksum: '3'.repeat(64), qualityChecksum: 'b'.repeat(64),
      maskStateChecksum: '9'.repeat(64), state: 'accepted', eligibleRegionIds: ['region-1'],
      rubyRegionIdsByPrimary: { 'region-1': [] },
      draft: { revision: 1, stateChecksum: '7'.repeat(64), regions: [] },
      artifacts: [acceptedMask], selectedArtifactId: 'mask-accepted',
      review: { id: 'mask-review', state: 'accepted', reason: 'complete-and-no-collateral',
        artifactId: 'mask-accepted', maskChecksum: '8'.repeat(64),
        coverageChecks: MASK_COVERAGE_CHECKS.map((check) => ({ check, passed: true })),
        collateralChecks: MASK_COLLATERAL_CHECKS.map((check) => ({ check, passed: true })),
        reviewer, createdAt: '2026-08-25T00:00:00Z' },
    };
    useWorkbenchStore.setState({ maskContexts: { 'image-1': maskContext } });
    const candidate = {
      candidateId: 'candidate-1', sequence: 1, jobId: 'job-inpaint',
      jobItemId: 'item-inpaint', parentChecksum: '9'.repeat(64),
      qualityChecksum: 'b'.repeat(64), backgroundChecksum: 'a'.repeat(64),
      maskArtifactId: 'mask-accepted', maskChecksum: '8'.repeat(64), routeManifest,
      routeChecksum: 'd'.repeat(64), originKind: 'ai' as const, providerIds: ['lama'],
      modelVersions: ['lama-onnx-local-v1'], parameterHash: 'd'.repeat(64),
      candidateChecksum: 'e'.repeat(64), width: 1200, height: 1800, renderScale: 1,
      outsideMaskChangeCount: 0, anomalies: [], completed: true, review: null,
      createdAt: '2026-08-25T00:00:00Z',
    };
    const cleanPlateContext: CleanPlateGateContext = {
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 22,
      g7Checksum: '9'.repeat(64), qualityChecksum: 'b'.repeat(64),
      backgroundChecksum: 'a'.repeat(64), maskArtifactId: 'mask-accepted',
      maskChecksum: '8'.repeat(64), cleanPlateStateChecksum: 'f'.repeat(64), state: 'pending',
      routes: [{ regionId: 'region-1', backgroundCategory: 'complex-lineart',
        defaultRoute: 'ai-inpaint-redraw' }],
      candidates: [candidate], acceptedCandidateId: null,
      fallbackEnabled: false, fallbackAllowed: false,
    };
    const getCleanPlate = vi.spyOn(api, 'getCleanPlateGateContext')
      .mockResolvedValue(cleanPlateContext);
    expect(await useWorkbenchStore.getState().loadCleanPlateContext('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().selectedCleanPlateCandidateIds['image-1'])
      .toBe('candidate-1');

    const exactObservation = {
      imageId: 'image-1', generationId: 'generation-1', nextSequence: 22,
      cleanPlateStateChecksum: 'f'.repeat(64), candidateId: 'candidate-1',
      imageRevision: 11, sourceChecksum: 'c'.repeat(64), qualityChecksum: 'b'.repeat(64),
      maskArtifactId: 'mask-accepted', maskChecksum: '8'.repeat(64),
      maskWidth: 1200, maskHeight: 1800, checksum: 'e'.repeat(64),
      width: 1200, height: 1800, state: 'ready' as const,
    };
    useWorkbenchStore.getState().observeG8CleanPlateBitmap({
      ...exactObservation, maskArtifactId: 'mask-old',
    });
    expect(useWorkbenchStore.getState().cleanPlateBitmapObservations['image-1']).toBeUndefined();
    useWorkbenchStore.getState().observeG8CleanPlateBitmap(exactObservation);
    expect(useWorkbenchStore.getState().cleanPlateBitmapObservations['image-1'])
      .toEqual(exactObservation);

    useWorkbenchStore.setState({ cleanPlateBitmapObservations: {
      'image-1': { ...exactObservation, maskChecksum: '0'.repeat(64) },
    } });
    const review = vi.spyOn(api, 'reviewCleanPlateGate');
    expect(await useWorkbenchStore.getState().reviewG8CleanPlate(
      'accept', CLEAN_PLATE_CHECKS.map((check) => ({ check, passed: true })),
    )).toBe(false);
    expect(review).not.toHaveBeenCalled();

    setLineage();
    vi.mocked(api.getCleanPlateGateContext).mockResolvedValue({
      ...cleanPlateContext,
      candidates: [{ ...candidate, width: 2400 }],
    });
    expect(await useWorkbenchStore.getState().loadCleanPlateContext('image-1', true)).toBe(false);

    const fallbackEvent = pageEvent(22, 'clean-plate-fallback-enabled', {
      gate: 'G8_cleanPlate', stage: 'inpaint', parentChecksum: '9'.repeat(64),
      inputChecksum: 'f'.repeat(64), outputChecksum: '0'.repeat(64),
      evidence: { imageRevision: 12 },
    });
    useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => ({ ...image, revision: 12 })),
      maskContexts: { 'image-1': { ...maskContext, imageRevision: 12, nextSequence: 23 } },
    }));
    setLineage(pageGeneration(23, { sourceChecksum: 'c'.repeat(64) }),
      [g7Terminal, enqueue, produced, completed, fallbackEvent]);
    getCleanPlate.mockResolvedValue({
      ...cleanPlateContext, imageRevision: 12, nextSequence: 23,
      cleanPlateStateChecksum: '0'.repeat(64), fallbackEnabled: true, fallbackAllowed: false,
    });
    expect(await useWorkbenchStore.getState().loadCleanPlateContext('image-1', true)).toBe(false);

    setLineage(pageGeneration(23, { sourceChecksum: 'c'.repeat(64) }),
      [g7Terminal, enqueue, produced, completed, fallbackEvent]);
    useWorkbenchStore.setState({ cleanPlateContexts: { 'image-1': {
      ...cleanPlateContext, imageRevision: 12, nextSequence: 23,
      cleanPlateStateChecksum: '0'.repeat(64), fallbackEnabled: true, fallbackAllowed: true,
    } } });
    const start = vi.spyOn(api, 'startJob');
    expect(await useWorkbenchStore.getState().startG8CleanPlate(false)).toBe(false);
    expect(start).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().globalError).toContain('明确关闭');
  });

  it('blocks every G4 write entry once the page advances to G5', async () => {
    seedActiveG5();
    const update = vi.spyOn(api, 'updateG4Region');
    const detect = vi.spyOn(api, 'startJob');

    expect(useWorkbenchStore.getState().createRegion({ x: 1, y: 2, width: 30, height: 40 })).toBeNull();
    useWorkbenchStore.getState().updateRegion('region-1', { x: 120 });
    expect(await useWorkbenchStore.getState().startG4Detection()).toBe(false);

    expect(useWorkbenchStore.getState().pendingG4Mutations).toEqual([]);
    expect(update).not.toHaveBeenCalled();
    expect(detect).not.toHaveBeenCalled();
    expect(g4EditingLocked(useWorkbenchStore.getState(), 'image-1')).toBe(true);
  });

  it('saves a confidence-zero G5 classification with CAS and reloads server reviewer evidence', async () => {
    const region = seedActiveG5();
    const reviewer = {
      actorKind: 'human' as const,
      sessionId: 'server-reviewer',
      operationSource: 'ui' as const,
    };
    const saved = {
      ...region,
      backgroundCategory: 'white-solid' as const,
      backgroundConfidence: 0,
      backgroundRationaleCodes: ['uniform-near-white' as const],
      backgroundReviewer: reviewer,
      backgroundGenerationId: 'generation-1',
      revision: 5,
    };
    const update = vi.spyOn(api, 'updateBackgroundClassification').mockResolvedValue(saved);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', { revision: 11, regionCount: 1 }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(9)]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([
      pageEvent(7, 'regions-stage-review', { state: 'accepted', outputChecksum: 'e'.repeat(64) }),
      pageEvent(8, 'background-classification-reviewed', {
        gate: 'G5_background', state: 'pending', outputChecksum: 'f'.repeat(64),
      }),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([saved]);
    vi.spyOn(api, 'getBackgroundGateContext').mockResolvedValue({
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 9,
      g4Checksum: 'e'.repeat(64), backgroundChecksum: 'f'.repeat(64), state: 'pending',
      eligibleRegionIds: ['region-1'], classifiedRegionIds: ['region-1'],
    });

    expect(await useWorkbenchStore.getState().saveG5Background(
      'region-1', 'white-solid', 0, ['uniform-near-white'],
    )).toBe(true);

    expect(update).toHaveBeenCalledWith('region-1', {
      category: 'white-solid',
      confidence: 0,
      rationaleCodes: ['uniform-near-white'],
      expectedRevision: 4,
      expectedImageRevision: 10,
      lineage: expect.objectContaining({
        runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 8,
      }),
    });
    expect(update.mock.calls[0]?.[1]).not.toHaveProperty('reviewer');
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      backgroundConfidence: 0,
      backgroundReviewer: reviewer,
      backgroundGenerationId: 'generation-1',
    });
    expect(workflowPhase(useWorkbenchStore.getState().g4Contexts['image-1'])).toBe('G5');
  });

  it('rejects invalid G5 drafts locally and keeps an uncertain conflict sticky', async () => {
    seedActiveG5();
    const update = vi.spyOn(api, 'updateBackgroundClassification')
      .mockRejectedValue(new ApiError('revision mismatch', 409));

    expect(await useWorkbenchStore.getState().saveG5Background(
      'region-1', 'white-solid', Number.NaN, ['uniform-near-white'],
    )).toBe(false);
    expect(update).not.toHaveBeenCalled();
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.error).toBe('');

    expect(await useWorkbenchStore.getState().saveG5Background(
      'region-1', 'white-solid', 0.05, ['uniform-near-white'],
    )).toBe(false);
    expect(await useWorkbenchStore.getState().saveG5Background(
      'region-1', 'white-solid', 0.05, ['uniform-near-white'],
    )).toBe(false);
    expect(update).toHaveBeenCalledOnce();
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      conflict: true,
      error: 'revision mismatch',
    });
  });

  it('does not accept incomplete G5 evidence and records zero-eligible pages as not-applicable', async () => {
    seedActiveG5();
    const accept = vi.spyOn(api, 'acceptBackgroundGate');
    expect(await useWorkbenchStore.getState().acceptG5Background()).toBe(false);
    expect(accept).not.toHaveBeenCalled();

    const region = seedActiveG5({ eligible: false });
    accept.mockResolvedValue({
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 9,
      event: pageEvent(8, 'background-stage-review', {
        gate: 'G5_background', state: 'not-applicable', decision: 'background-not-applicable',
      }),
    });
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', { revision: 11, regionCount: 1 }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(9)]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([
      pageEvent(7, 'regions-stage-review', { state: 'accepted', outputChecksum: 'e'.repeat(64) }),
      pageEvent(8, 'background-stage-review', {
        gate: 'G5_background', state: 'not-applicable', decision: 'background-not-applicable',
        outputChecksum: 'f'.repeat(64),
      }),
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([region]);
    vi.spyOn(api, 'getBackgroundGateContext').mockResolvedValue({
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 9,
      g4Checksum: 'e'.repeat(64), backgroundChecksum: 'f'.repeat(64), state: 'not-applicable',
      eligibleRegionIds: [], classifiedRegionIds: [],
    });
    vi.spyOn(api, 'getOCRGateContext').mockResolvedValue({
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 9,
      g5Checksum: 'f'.repeat(64), ocrChecksum: '1'.repeat(64), state: 'pending',
      eligibleRegionIds: [], attemptedRegionIds: [], reviewedRegionIds: [], attempts: [],
    });

    expect(await useWorkbenchStore.getState().acceptG5Background()).toBe(true);
    expect(accept).toHaveBeenLastCalledWith(
      'image-1', 'no-eligible-regions', 'f'.repeat(64), 10,
      expect.objectContaining({ expectedSequence: 8 }),
    );
    expect(workflowPhase(useWorkbenchStore.getState().g4Contexts['image-1'])).toBe('G6');
  });

  it('starts strict whole-page G6 OCR with current lineage and no client region list', async () => {
    seedActiveG6({ attempts: false });
    const start = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-ocr',
      kind: 'ocr',
      status: 'queued',
      total: 1,
      items: [{
        id: 'item-ocr', imageId: 'image-1', label: 'image-1', status: 'queued', progress: 0,
      }],
    }));
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', { revision: 10, regionCount: 1 }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(10)]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([
      ...g6BaseEvents(false),
      pageEvent(9, 'ocr-job-enqueued', {
        gate: 'G6_ocr', state: 'pending',
        stage: 'ocr', decision: null, reason: 'job-enqueued',
        inputChecksum: '1'.repeat(64), outputChecksum: '1'.repeat(64),
        parentChecksum: 'f'.repeat(64), provider: 'tesseract',
        jobId: 'job-ocr', jobItemId: 'item-ocr',
        evidence: {
          eventType: 'job-enqueued', qualityState: 'pending-review',
          targetKind: 'region-set', eligibleRegionCount: 1,
        },
      }),
    ]);
    vi.spyOn(api, 'getOCRGateContext').mockResolvedValue({
      imageId: 'image-1', imageRevision: 10, generationId: 'generation-1', nextSequence: 10,
      g5Checksum: 'f'.repeat(64), ocrChecksum: '1'.repeat(64), state: 'pending',
      eligibleRegionIds: ['region-1'], attemptedRegionIds: [], reviewedRegionIds: [], attempts: [],
    });

    expect(await useWorkbenchStore.getState().startG6OCR()).toBe(true);
    expect(start).toHaveBeenCalledWith('project-1', 'ocr', {
      imageIds: ['image-1'],
      options: { provider: 'tesseract', language: 'ja', concurrency: 1 },
      lineage: {
        runId: 'run-1',
        actor: expect.objectContaining({ actorKind: 'human', operationSource: 'ui' }),
        pages: [{
          imageId: 'image-1', pageGenerationId: 'generation-1', expectedSequence: 9,
        }],
      },
    });
    expect(start.mock.calls[0]?.[2]).not.toHaveProperty('regionIds');
  });

  it('saves confidence-zero G6 dual-attempt review with CAS and reloads server evidence', async () => {
    const region = seedActiveG6();
    const reviewer = {
      actorKind: 'human' as const,
      sessionId: 'server-reviewer',
      operationSource: 'ui' as const,
    };
    const saved = regionFixture('region-1', {
      ...region,
      sourceText: '原文。',
      ocrReview: {
        sourceMode: 'quality-attempt',
        selectedAttemptId: 'attempt-quality',
        sourceTextChecksum: '8'.repeat(64),
        qcChecks: [...OCR_QC_CHECKS],
        qcFlags: ['original-quality-disagree'],
      },
      ocrReviewer: reviewer,
      ocrGenerationId: 'generation-1',
      revision: 5,
    });
    const update = vi.spyOn(api, 'updateOCRSourceReview').mockResolvedValue(saved);
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', { revision: 11, regionCount: 1 }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(13)]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue(g6BaseEvents(true, true));
    vi.spyOn(api, 'listRegions').mockResolvedValue([saved]);
    vi.spyOn(api, 'getOCRGateContext').mockResolvedValue({
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 13,
      g5Checksum: 'f'.repeat(64), ocrChecksum: '3'.repeat(64), state: 'pending',
      eligibleRegionIds: ['region-1'], attemptedRegionIds: ['region-1'],
      reviewedRegionIds: ['region-1'], attempts: [ocrAttempt('original', 0), ocrAttempt('quality', 0.2)],
    });

    expect(await useWorkbenchStore.getState().saveG6SourceReview(
      'region-1', '原文。', 'quality-attempt', 'attempt-quality', [...OCR_QC_CHECKS],
    )).toBe(true);
    expect(update).toHaveBeenCalledWith('region-1', {
      sourceText: '原文。',
      sourceMode: 'quality-attempt',
      selectedAttemptId: 'attempt-quality',
      qcChecks: [...OCR_QC_CHECKS],
      expectedRevision: 4,
      expectedImageRevision: 10,
      lineage: expect.objectContaining({
        runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 12,
      }),
    });
    expect(update.mock.calls[0]?.[1]).not.toHaveProperty('reviewer');
    expect(update.mock.calls[0]?.[1]).not.toHaveProperty('generationId');
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      sourceText: '原文。',
      ocrReviewer: reviewer,
      ocrGenerationId: 'generation-1',
    });
  });

  it('rejects incomplete G6 attempts locally and keeps an uncertain review conflict sticky', async () => {
    seedActiveG6();
    useWorkbenchStore.setState((state) => ({
      ocrContexts: {
        ...state.ocrContexts,
        'image-1': {
          ...state.ocrContexts['image-1']!,
          attempts: [ocrAttempt('original', 0)],
        },
      },
    }));
    const update = vi.spyOn(api, 'updateOCRSourceReview');
    expect(await useWorkbenchStore.getState().saveG6SourceReview(
      'region-1', '原文', 'original-attempt', 'attempt-original', [...OCR_QC_CHECKS],
    )).toBe(false);
    expect(update).not.toHaveBeenCalled();

    useWorkbenchStore.setState((state) => ({
      ocrContexts: {
        ...state.ocrContexts,
        'image-1': {
          ...state.ocrContexts['image-1']!,
          attempts: [ocrAttempt('original', 0), ocrAttempt('quality', 0.2)],
        },
      },
    }));
    update.mockRejectedValue(new ApiError('revision mismatch', 409));
    expect(await useWorkbenchStore.getState().saveG6SourceReview(
      'region-1', '原文', 'original-attempt', 'attempt-original', [...OCR_QC_CHECKS],
    )).toBe(false);
    expect(await useWorkbenchStore.getState().saveG6SourceReview(
      'region-1', '原文', 'original-attempt', 'attempt-original', [...OCR_QC_CHECKS],
    )).toBe(false);
    expect(update).toHaveBeenCalledOnce();
    expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      conflict: true,
      error: 'revision mismatch',
    });
  });

  it('accepts reviewed low-confidence G6 evidence and advances to G7', async () => {
    const region = seedActiveG6({ reviewed: true });
    const secondOriginal = {
      ...ocrAttempt('original', 0.1),
      id: 'attempt-original-2',
      jobId: 'job-ocr-2',
      jobItemId: 'item-ocr-2',
    };
    const secondQuality = {
      ...ocrAttempt('quality', 0.9),
      id: 'attempt-quality-2',
      jobId: 'job-ocr-2',
      jobItemId: 'item-ocr-2',
    };
    useWorkbenchStore.setState((state) => ({
      ocrContexts: {
        ...state.ocrContexts,
        'image-1': {
          ...state.ocrContexts['image-1']!,
          attempts: [
            ocrAttempt('original', 0),
            ocrAttempt('quality', 0.2),
            secondOriginal,
            secondQuality,
          ],
        },
      },
    }));
    const terminal = pageEvent(13, 'ocr-stage-review', {
      gate: 'G6_ocr', state: 'accepted', stage: 'ocr',
      decision: 'ocr-trust-accepted', reason: 'all-translatable-source-text-reviewed',
      inputChecksum: '3'.repeat(64), outputChecksum: '3'.repeat(64),
      parentChecksum: 'f'.repeat(64), provider: null, modelVersion: null,
      jobId: null, jobItemId: null,
      evidence: {
        eventType: 'ocr-stage-review', qualityState: 'accepted',
        targetKind: 'region-set', regionCount: 1, eligibleRegionCount: 1,
        attemptedRegionCount: 1, reviewedRegionCount: 1, ocrAttemptCount: 2,
      },
    });
    const accept = vi.spyOn(api, 'acceptOCRGate').mockResolvedValue({
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 14,
      event: terminal,
    });
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', { revision: 11, regionCount: 1 }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(14)]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([...g6BaseEvents(true, true), terminal]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([region]);
    vi.spyOn(api, 'getOCRGateContext').mockResolvedValue({
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 14,
      g5Checksum: 'f'.repeat(64), ocrChecksum: '3'.repeat(64), state: 'accepted',
      eligibleRegionIds: ['region-1'], attemptedRegionIds: ['region-1'],
      reviewedRegionIds: ['region-1'], attempts: [ocrAttempt('original', 0), ocrAttempt('quality', 0.2)],
    });

    expect(await useWorkbenchStore.getState().acceptG6OCR()).toBe(true);
    expect(accept).toHaveBeenCalledWith(
      'image-1', 'all-translatable-source-text-reviewed', '3'.repeat(64), 10,
      expect.objectContaining({ expectedSequence: 13 }),
    );
    expect(workflowPhase(useWorkbenchStore.getState().g4Contexts['image-1'])).toBe('G7');
  });

  it('rejects G6 acceptance when the selected attempt pair is incomplete', async () => {
    seedActiveG6({ reviewed: true });
    const completeOtherPair = [
      {
        ...ocrAttempt('original', 0.1),
        id: 'attempt-original-2',
        jobId: 'job-ocr-2',
        jobItemId: 'item-ocr-2',
      },
      {
        ...ocrAttempt('quality', 0.9),
        id: 'attempt-quality-2',
        jobId: 'job-ocr-2',
        jobItemId: 'item-ocr-2',
      },
    ];
    useWorkbenchStore.setState((state) => ({
      ocrContexts: {
        ...state.ocrContexts,
        'image-1': {
          ...state.ocrContexts['image-1']!,
          attempts: [ocrAttempt('quality', 0.2), ...completeOtherPair],
        },
      },
    }));
    const accept = vi.spyOn(api, 'acceptOCRGate');

    expect(await useWorkbenchStore.getState().acceptG6OCR()).toBe(false);
    expect(accept).not.toHaveBeenCalled();
  });

  it('blocks incomplete G6 acceptance and records zero-eligible pages as not-applicable', async () => {
    seedActiveG6();
    const accept = vi.spyOn(api, 'acceptOCRGate');
    expect(await useWorkbenchStore.getState().acceptG6OCR()).toBe(false);
    expect(accept).not.toHaveBeenCalled();

    seedActiveG6({ eligible: false, attempts: false });
    const terminal = pageEvent(9, 'ocr-stage-review', {
      gate: 'G6_ocr', state: 'not-applicable', stage: 'ocr',
      decision: 'ocr-not-applicable', reason: 'no-translatable-regions',
      inputChecksum: '1'.repeat(64), outputChecksum: '1'.repeat(64),
      parentChecksum: 'f'.repeat(64), provider: null, modelVersion: null,
      jobId: null, jobItemId: null,
      evidence: {
        eventType: 'ocr-stage-review', qualityState: 'not-applicable',
        targetKind: 'region-set', regionCount: 1, eligibleRegionCount: 0,
        attemptedRegionCount: 0, reviewedRegionCount: 0, ocrAttemptCount: 0,
      },
    });
    accept.mockResolvedValue({
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 10,
      event: terminal,
    });
    vi.mocked(api.listImages).mockResolvedValue([
      imageFixture('image-1', { revision: 11, regionCount: 1 }),
    ]);
    vi.mocked(api.listPageGenerations).mockResolvedValue([pageGeneration(10)]);
    vi.spyOn(api, 'listPageLineageEvents').mockResolvedValue([
      ...useWorkbenchStore.getState().g4Contexts['image-1']!.events,
      terminal,
    ]);
    vi.spyOn(api, 'listRegions').mockResolvedValue([
      useWorkbenchStore.getState().regionsByImage['image-1']![0]!,
    ]);
    vi.spyOn(api, 'getOCRGateContext').mockResolvedValue({
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 10,
      g5Checksum: 'f'.repeat(64), ocrChecksum: '1'.repeat(64), state: 'not-applicable',
      eligibleRegionIds: [], attemptedRegionIds: [], reviewedRegionIds: [], attempts: [],
    });

    expect(await useWorkbenchStore.getState().acceptG6OCR()).toBe(true);
    expect(accept).toHaveBeenLastCalledWith(
      'image-1', 'no-translatable-regions', '1'.repeat(64), 10,
      expect.objectContaining({ expectedSequence: 9 }),
    );
    expect(workflowPhase(useWorkbenchStore.getState().g4Contexts['image-1'])).toBe('G7');
  });

  it('requires trusted G6 source text for non-ruby translate and redraw-art regions only', () => {
    expect(ocrSourceReviewRequired(regionFixture('sfx-redraw', {
      type: 'sound_effect', contentDisposition: 'redraw-art',
    }))).toBe(true);
    expect(ocrSourceReviewRequired(regionFixture('dialogue-translate', {
      type: 'dialogue', contentDisposition: 'translate',
    }))).toBe(true);
    expect(ocrSourceReviewRequired(regionFixture('kept-art', {
      type: 'sound_effect', contentDisposition: 'keep-art',
    }))).toBe(false);
    expect(ocrSourceReviewRequired(regionFixture('ignored-art', {
      type: 'sound_effect', contentDisposition: 'ignore',
    }))).toBe(false);
    expect(ocrSourceReviewRequired(regionFixture('ruby-redraw', {
      type: 'ruby', contentDisposition: 'redraw-art',
    }))).toBe(false);
  });
});

describe('strict G9 frontend', () => {
  beforeEach(() => {
    resetWorkbenchStore();
    seedWorkbench();
  });
  afterEach(() => vi.restoreAllMocks());

  async function coldLoadPendingEligibleRegions(
    regions: ReturnType<typeof regionFixture>[],
    eligibleRegionIds: string[],
  ) {
    const generation = pageGeneration(2);
    const g8Terminal = pageEvent(1, 'clean-plate-stage-review', {
      gate: 'G8_cleanPlate', stage: 'inpaint', state: 'accepted',
      decision: 'clean-plate-accepted', reason: 'clean-plate-complete',
      inputChecksum: '7'.repeat(64), outputChecksum: '8'.repeat(64), parentChecksum: '7'.repeat(64),
      jobId: null, jobItemId: null, evidence: {
        candidateId: 'clean-1', candidateChecksum: '9'.repeat(64),
        qualityChecksum: '5'.repeat(64), imageRevision: 7,
      },
    });
    useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => image.id === 'image-1' ? { ...image, revision: 7 } : image),
      regionsByImage: { ...state.regionsByImage, 'image-1': regions },
      g4Contexts: { 'image-1': { status: 'active', generation, events: [g8Terminal],
        phase: 'G9', error: '', conflict: false } },
    }));
    const eligibleRegions = eligibleRegionIds.map((regionId) => {
      const region = regions.find((entry) => entry.id === regionId)!;
      return {
        regionId, readingOrder: region.order, regionType: region.type,
        direction: region.direction === 'vertical' ? 'vertical' as const : 'horizontal' as const,
        paragraphGroupId: region.paragraphGroupId,
        sourceText: region.sourceText, sourceTextChecksum: region.ocrReview!.sourceTextChecksum,
        contextRegionIds: [], contextChecksum: '7'.repeat(64), rubyExcluded: true as const,
      };
    });
    const context = {
      imageId: 'image-1', imageRevision: 7, generationId: generation.id, nextSequence: 2,
      g8Checksum: '8'.repeat(64), cleanPlateCandidateId: 'clean-1',
      cleanPlateChecksum: '9'.repeat(64), translationStateChecksum: '8'.repeat(64),
      targetLanguage: 'zh-CN', terminalChecksum: null, state: 'pending' as const,
      eligibleRegions, candidates: [], acceptedCandidateIdsByRegion: {}, reviewedRegionCount: 0,
    };
    vi.spyOn(api, 'getTranslationGateContext').mockResolvedValue(context);
    const loaded = await useWorkbenchStore.getState().loadTranslationContext('image-1', true);
    return { context, loaded };
  }

  function trustedRegion(
    id: string,
    order: number,
    contentDisposition: 'translate' | 'redraw-art' | 'keep-art' | 'ignore',
    type: 'dialogue' | 'sound_effect' = 'sound_effect',
  ) {
    return regionFixture(id, {
      order, type, direction: 'vertical', contentDisposition, sourceText: `原文-${id}`,
      ocrReview: { sourceMode: 'manual-correction', selectedAttemptId: `attempt-${id}`,
        sourceTextChecksum: order.toString(16).repeat(64).slice(0, 64),
        qcChecks: OCR_QC_CHECKS, qcFlags: ['none'] },
      ocrReviewer: { actorKind: 'human', sessionId: 'reviewer', operationSource: 'ui' },
      ocrGenerationId: 'generation-1',
    });
  }

  it('cold-loads a redraw-art-only G9 context after trusted G6 source review', async () => {
    const redraw = trustedRegion('sfx-redraw', 1, 'redraw-art');
    const { context, loaded } = await coldLoadPendingEligibleRegions([redraw], ['sfx-redraw']);

    expect(loaded).toBe(true);
    expect(useWorkbenchStore.getState().translationContexts['image-1']).toEqual(context);
  });

  it('cold-loads mixed translate and redraw-art eligibility while excluding keep-art and ignore', async () => {
    const regions = [
      trustedRegion('dialogue', 1, 'translate', 'dialogue'),
      trustedRegion('sfx-redraw', 2, 'redraw-art'),
      trustedRegion('sfx-kept', 3, 'keep-art'),
      trustedRegion('sfx-ignored', 4, 'ignore'),
      regionFixture('ruby', { order: 5, type: 'ruby', contentDisposition: 'redraw-art' }),
    ];
    const { loaded } = await coldLoadPendingEligibleRegions(regions, ['dialogue', 'sfx-redraw']);

    expect(loaded).toBe(true);
    expect(useWorkbenchStore.getState().translationContexts['image-1']?.eligibleRegions
      .map((region) => region.regionId)).toEqual(['dialogue', 'sfx-redraw']);
  });

  it('cold-loads zero-eligible G9 only when it is bound to the exact G8 terminal', async () => {
    const generation = pageGeneration(2);
    const g8Terminal = pageEvent(1, 'clean-plate-stage-review', {
      gate: 'G8_cleanPlate', stage: 'inpaint', state: 'not-applicable',
      decision: 'clean-plate-not-applicable', reason: 'no-clean-plate-required',
      inputChecksum: '7'.repeat(64), outputChecksum: '8'.repeat(64), parentChecksum: '7'.repeat(64),
      jobId: null, jobItemId: null, evidence: {
        candidateId: null, candidateChecksum: null, qualityChecksum: '9'.repeat(64), imageRevision: 7,
      },
    });
    useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => image.id === 'image-1' ? { ...image, revision: 7 } : image),
      regionsByImage: { ...state.regionsByImage, 'image-1': [regionFixture('region-1', {
        contentDisposition: 'ignore', type: 'ruby',
      })] },
      g4Contexts: { 'image-1': { status: 'active', generation, events: [g8Terminal],
        phase: 'G9', error: '', conflict: false } },
    }));
    const context = {
      imageId: 'image-1', imageRevision: 7, generationId: generation.id, nextSequence: 2,
      g8Checksum: '8'.repeat(64), cleanPlateCandidateId: null,
      cleanPlateChecksum: '9'.repeat(64), translationStateChecksum: '8'.repeat(64),
      targetLanguage: 'zh-CN', terminalChecksum: null, state: 'pending' as const, eligibleRegions: [], candidates: [],
      acceptedCandidateIdsByRegion: {}, reviewedRegionCount: 0,
    };
    vi.spyOn(api, 'getTranslationGateContext').mockResolvedValue(context);
    expect(await useWorkbenchStore.getState().loadTranslationContext('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().translationContexts['image-1']).toEqual(context);

    vi.mocked(api.getTranslationGateContext).mockResolvedValue({ ...context, imageRevision: 8 });
    expect(await useWorkbenchStore.getState().loadTranslationContext('image-1', true)).toBe(false);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.error).toContain('G9 上下文');
  });

  it('keeps generic translationText and confirmed writes closed for active G9', async () => {
    const region = regionFixture('region-1', { translationText: '', confirmed: false });
    useWorkbenchStore.setState((state) => ({
      regionsByImage: { ...state.regionsByImage, 'image-1': [region] },
      g4Contexts: { 'image-1': { status: 'active', generation: pageGeneration(2), events: [],
        phase: 'G9', error: '', conflict: false } },
    }));
    useWorkbenchStore.getState().updateRegion('region-1', { translationText: '不能走旧入口' });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.translationText).toBe('');
    expect(await useWorkbenchStore.getState().setRegionConfirmed('region-1', true)).toBe(false);
    expect(useWorkbenchStore.getState().globalError).toContain('旧版');
  });

  it('dispatches a nonempty parent-null first revision for an eligible region', async () => {
    const generation = pageGeneration(2);
    useWorkbenchStore.setState((state) => ({
      g4Contexts: { 'image-1': { status: 'active', generation, events: [],
        phase: 'G9', error: '', conflict: false } },
      translationContexts: { 'image-1': {
        imageId: 'image-1', imageRevision: 1, generationId: generation.id, nextSequence: 2,
        g8Checksum: '8'.repeat(64), cleanPlateCandidateId: 'clean-1',
        cleanPlateChecksum: '9'.repeat(64), targetLanguage: 'zh-CN',
        translationStateChecksum: '8'.repeat(64), terminalChecksum: null, state: 'pending',
        eligibleRegions: [{ regionId: 'region-1', readingOrder: 1, regionType: 'dialogue',
          direction: 'vertical', paragraphGroupId: null, sourceText: '待って！',
          sourceTextChecksum: '6'.repeat(64), contextRegionIds: [],
          contextChecksum: '7'.repeat(64), rubyExcluded: true }],
        candidates: [], acceptedCandidateIdsByRegion: {}, reviewedRegionCount: 0,
      } },
      regionsByImage: { ...state.regionsByImage, 'image-1': [regionFixture('region-1', {
        type: 'dialogue', contentDisposition: 'translate', sourceText: '待って！',
      })] },
    }));
    const create = vi.spyOn(api, 'createTranslationCandidate')
      .mockRejectedValue(new Error('stop after dispatch'));
    expect(await useWorkbenchStore.getState().reviseG9Translation(
      'region-1', '等等！', 'manual',
    )).toBe(false);
    expect(create).toHaveBeenCalledWith('image-1', expect.objectContaining({
      regionId: 'region-1', translationText: '等等！', originKind: 'manual',
      observedG8Checksum: '8'.repeat(64), observedTranslationStateChecksum: '8'.repeat(64),
    }));
    expect(create.mock.calls[0]?.[1]).not.toHaveProperty('supersedesCandidateId');
  });

  it('cold-loads an empty model candidate so empty-output can be rejected or revised', async () => {
    const generation = pageGeneration(5);
    const source = regionFixture('region-1', {
      revision: 4, order: 1, type: 'dialogue', direction: 'vertical',
      contentDisposition: 'translate', sourceText: '待って！',
      ocrReview: { sourceMode: 'manual-correction', selectedAttemptId: 'attempt-1',
        sourceTextChecksum: '6'.repeat(64), qcChecks: OCR_QC_CHECKS, qcFlags: ['none'] },
      ocrReviewer: { actorKind: 'human', sessionId: 'reviewer', operationSource: 'ui' },
      ocrGenerationId: generation.id,
    });
    const events = [
      pageEvent(1, 'clean-plate-stage-review', { gate: 'G8_cleanPlate', stage: 'inpaint',
        state: 'accepted', decision: 'clean-plate-accepted', inputChecksum: '7'.repeat(64),
        outputChecksum: '8'.repeat(64), parentChecksum: '7'.repeat(64), jobId: null,
        jobItemId: null, evidence: { candidateId: 'clean-1', candidateChecksum: '9'.repeat(64),
          qualityChecksum: '5'.repeat(64), imageRevision: 1 } }),
      pageEvent(2, 'translate-job-enqueued', { gate: 'G9_translation', stage: 'translation',
        state: 'pending', decision: null, inputChecksum: '8'.repeat(64), outputChecksum: '8'.repeat(64),
        parentChecksum: '8'.repeat(64), jobId: 'job-translate', jobItemId: 'item-translate',
        evidence: { eligibleRegionCount: 1 } }),
      pageEvent(3, 'translation-candidates-produced', { gate: 'G9_translation', stage: 'translation',
        state: 'pending', decision: 'candidates-produced', inputChecksum: '8'.repeat(64),
        outputChecksum: 'a'.repeat(64), parentChecksum: '8'.repeat(64),
        jobId: 'job-translate', jobItemId: 'item-translate', evidence: { candidateCount: 1 } }),
      pageEvent(4, 'translate-job-completed', { gate: 'G9_translation', stage: 'translation',
        state: 'pending', decision: null, inputChecksum: '8'.repeat(64), outputChecksum: 'a'.repeat(64),
        parentChecksum: '8'.repeat(64), jobId: 'job-translate', jobItemId: 'item-translate',
        evidence: { candidateCount: 1 } }),
    ];
    useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => image.id === 'image-1' ? { ...image, revision: 4 } : image),
      regionsByImage: { ...state.regionsByImage, 'image-1': [source] },
      g4Contexts: { 'image-1': { status: 'active', generation, events,
        phase: 'G9', error: '', conflict: false } },
    }));
    const context = {
      imageId: 'image-1', imageRevision: 4, generationId: generation.id, nextSequence: 5,
      g8Checksum: '8'.repeat(64), cleanPlateCandidateId: 'clean-1',
      cleanPlateChecksum: '9'.repeat(64), targetLanguage: 'zh-CN',
      translationStateChecksum: 'a'.repeat(64), terminalChecksum: null, state: 'pending' as const,
      eligibleRegions: [{ regionId: 'region-1', readingOrder: 1, regionType: 'dialogue' as const,
        direction: 'vertical' as const, paragraphGroupId: null, sourceText: '待って！',
        sourceTextChecksum: '6'.repeat(64), contextRegionIds: [],
        contextChecksum: '7'.repeat(64), rubyExcluded: true as const }],
      candidates: [{ candidateId: 'translation-1', sequence: 3, regionId: 'region-1',
        revisionNumber: 1, supersedesCandidateId: null, originKind: 'model' as const,
        provider: 'argos-ja-zh', modelVersion: 'argos-local-v1', parameterHash: 'b'.repeat(64),
        targetLanguage: 'zh-CN', g8Checksum: '8'.repeat(64), cleanPlateChecksum: '9'.repeat(64),
        sourceTextChecksum: '6'.repeat(64), sourceRegionRevision: 4,
        contextChecksum: '7'.repeat(64), translationText: '', candidateChecksum: 'c'.repeat(64),
        computedQcFlags: ['empty-output' as const], jobId: 'job-translate',
        jobItemId: 'item-translate', revisionId: 'revision-1', review: null,
        createdAt: '2026-08-25T00:00:00Z' }],
      acceptedCandidateIdsByRegion: {}, reviewedRegionCount: 0,
    };
    vi.spyOn(api, 'getTranslationGateContext').mockResolvedValue(context);
    expect(await useWorkbenchStore.getState().loadTranslationContext('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().translationContexts['image-1']?.candidates[0]).toMatchObject({
      translationText: '', computedQcFlags: ['empty-output'],
    });
  });

  it('cold-loads an accepted candidate only when the current compatibility projection matches exactly', async () => {
    const generation = pageGeneration(7);
    const acceptedText = '等等！';
    const projected = regionFixture('region-1', {
      revision: 5, order: 1, type: 'dialogue', direction: 'vertical',
      contentDisposition: 'translate', sourceText: '待って！', translationText: acceptedText,
      translationProvider: 'manual',
      ocrReview: { sourceMode: 'manual-correction', selectedAttemptId: 'attempt-1',
        sourceTextChecksum: '6'.repeat(64), qcChecks: OCR_QC_CHECKS, qcFlags: ['none'] },
      ocrReviewer: { actorKind: 'human', sessionId: 'reviewer', operationSource: 'ui' },
      ocrGenerationId: generation.id,
    });
    const events = [
      pageEvent(1, 'clean-plate-stage-review', { gate: 'G8_cleanPlate', stage: 'inpaint',
        state: 'accepted', decision: 'clean-plate-accepted', inputChecksum: '7'.repeat(64),
        outputChecksum: '8'.repeat(64), parentChecksum: '7'.repeat(64), jobId: null,
        jobItemId: null, evidence: { candidateId: 'clean-1', candidateChecksum: '9'.repeat(64),
          qualityChecksum: '5'.repeat(64), imageRevision: 4 } }),
      pageEvent(2, 'translation-candidate-revised', { gate: 'G9_translation', stage: 'translation',
        state: 'pending', decision: 'candidate-revised', inputChecksum: '8'.repeat(64),
        outputChecksum: 'a'.repeat(64), parentChecksum: '8'.repeat(64), jobId: null,
        jobItemId: null, evidence: { candidateId: 'translation-1', regionId: 'region-1',
          candidateChecksum: 'c'.repeat(64) } }),
      pageEvent(3, 'translation-candidate-reviewed', { gate: 'G9_translation', stage: 'translation',
        state: 'rejected', decision: 'candidate-rejected', reason: 'source-copy',
        inputChecksum: 'a'.repeat(64), outputChecksum: 'b'.repeat(64),
        parentChecksum: '8'.repeat(64), jobId: null, jobItemId: null,
        evidence: { candidateId: 'translation-1', regionId: 'region-1',
          candidateChecksum: 'c'.repeat(64), qcFlagCount: 1 } }),
      pageEvent(4, 'translation-candidate-revised', { gate: 'G9_translation', stage: 'translation',
        state: 'pending', decision: 'candidate-revised', inputChecksum: 'b'.repeat(64),
        outputChecksum: 'f'.repeat(64), parentChecksum: '8'.repeat(64), jobId: null,
        jobItemId: null, evidence: { candidateId: 'translation-2', regionId: 'region-1',
          candidateChecksum: '1'.repeat(64) } }),
      pageEvent(5, 'translation-candidate-reviewed', { gate: 'G9_translation', stage: 'translation',
        state: 'accepted', decision: 'candidate-accepted', reason: 'translation-reviewed',
        inputChecksum: 'f'.repeat(64), outputChecksum: 'd'.repeat(64),
        parentChecksum: '8'.repeat(64), jobId: null, jobItemId: null,
        evidence: { candidateId: 'translation-2', regionId: 'region-1',
          candidateChecksum: '1'.repeat(64), qcFlagCount: 1 } }),
      pageEvent(6, 'translation-stage-review', { gate: 'G9_translation', stage: 'translation',
        state: 'accepted', decision: 'translations-accepted', reason: 'all-translations-reviewed',
        inputChecksum: 'd'.repeat(64), outputChecksum: 'e'.repeat(64),
        parentChecksum: '8'.repeat(64), jobId: null, jobItemId: null,
        evidence: { eligibleRegionCount: 1, reviewedCandidateCount: 2, acceptedRegionCount: 1 } }),
    ];
    const context = {
      imageId: 'image-1', imageRevision: 5, generationId: generation.id, nextSequence: 7,
      g8Checksum: '8'.repeat(64), cleanPlateCandidateId: 'clean-1',
      cleanPlateChecksum: '9'.repeat(64), targetLanguage: 'zh-CN',
      translationStateChecksum: 'd'.repeat(64), terminalChecksum: 'e'.repeat(64), state: 'accepted' as const,
      eligibleRegions: [{ regionId: 'region-1', readingOrder: 1, regionType: 'dialogue' as const,
        direction: 'vertical' as const, paragraphGroupId: null, sourceText: '待って！',
        sourceTextChecksum: '6'.repeat(64), contextRegionIds: [],
        contextChecksum: '7'.repeat(64), rubyExcluded: true as const }],
      candidates: [{ candidateId: 'translation-1', sequence: 2, regionId: 'region-1',
        revisionNumber: 1, supersedesCandidateId: null, originKind: 'manual' as const,
        provider: 'manual', modelVersion: 'manual-review-v1', parameterHash: 'e'.repeat(64),
        targetLanguage: 'zh-CN', g8Checksum: '8'.repeat(64), cleanPlateChecksum: '9'.repeat(64),
        sourceTextChecksum: '6'.repeat(64), sourceRegionRevision: 4,
        contextChecksum: '7'.repeat(64), translationText: '待って！',
        candidateChecksum: 'c'.repeat(64), computedQcFlags: ['source-copy' as const],
        jobId: null, jobItemId: null, revisionId: 'revision-1',
        review: { id: 'review-1', state: 'rejected' as const, reason: 'source-copy' as const,
          checks: TRANSLATION_QC_CHECKS.map((check) => ({ check, passed: check !== 'source-copy-checked' })),
          qcFlags: ['source-copy' as const],
          reviewer: { actorKind: 'human' as const, sessionId: 'reviewer', operationSource: 'ui' as const },
          createdAt: '2026-08-25T00:01:00Z' }, createdAt: '2026-08-25T00:00:00Z' },
      { candidateId: 'translation-2', sequence: 4, regionId: 'region-1',
        revisionNumber: 2, supersedesCandidateId: 'translation-1', originKind: 'manual' as const,
        provider: 'manual', modelVersion: 'manual-review-v1', parameterHash: 'e'.repeat(64),
        targetLanguage: 'zh-CN', g8Checksum: '8'.repeat(64), cleanPlateChecksum: '9'.repeat(64),
        sourceTextChecksum: '6'.repeat(64), sourceRegionRevision: 4,
        contextChecksum: '7'.repeat(64), translationText: acceptedText,
        candidateChecksum: '1'.repeat(64), computedQcFlags: ['none' as const],
        jobId: null, jobItemId: null, revisionId: 'revision-2',
        review: { id: 'review-2', state: 'accepted' as const, reason: 'translation-reviewed' as const,
          checks: TRANSLATION_QC_CHECKS.map((check) => ({ check, passed: true })), qcFlags: ['none' as const],
          reviewer: { actorKind: 'human' as const, sessionId: 'reviewer', operationSource: 'ui' as const },
          createdAt: '2026-08-25T00:03:00Z' }, createdAt: '2026-08-25T00:02:00Z' }],
      acceptedCandidateIdsByRegion: { 'region-1': 'translation-2' }, reviewedRegionCount: 1,
    };
    const seedProjection = (region: typeof projected) => useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => image.id === 'image-1' ? { ...image, revision: 5 } : image),
      regionsByImage: { ...state.regionsByImage, 'image-1': [region] },
      translationContexts: {},
      g4Contexts: { 'image-1': { status: 'active', generation, events,
        phase: 'G9', error: '', conflict: false } },
    }));
    vi.spyOn(api, 'getTranslationGateContext').mockResolvedValue(context);

    seedProjection(projected);
    expect(await useWorkbenchStore.getState().loadTranslationContext('image-1', true)).toBe(true);

    for (const tampered of [
      { ...projected, revision: 4 },
      { ...projected, translationText: '被篡改' },
      { ...projected, translationProvider: 'dictionary' },
    ]) {
      seedProjection(tampered);
      expect(await useWorkbenchStore.getState().loadTranslationContext('image-1', true)).toBe(false);
      expect(useWorkbenchStore.getState().g4Contexts['image-1']?.error).toContain('G9 上下文');
    }
  });
});

describe('strict G10 frontend', () => {
  const fontChecksum = '1'.repeat(64);
  const displayChecksum = '2'.repeat(64);
  const regularFont = { token: 'installed-font-111111111111111111111111', label: 'Regular CJK',
    fontChecksum,
    capabilityChecksum: 'b97efff04be2306e70546a4f0fd12bc0002036bbbde375e993e0803b11e3a965',
    role: 'regular' as const };
  const displayFont = { token: 'installed-font-222222222222222222222222', label: 'Display CJK',
    fontChecksum: displayChecksum,
    capabilityChecksum: 'ea9ff7ffb2ce937fdd45f3b7da86c339976ed562e5fc7e438f6e2a6e24ee9db8',
    role: 'display' as const };
  const frozenStyle = (display = false): TypesetRegionStyle => ({
    fontToken: display ? displayFont.token : regularFont.token,
    fontChecksum: display ? displayFont.fontChecksum : regularFont.fontChecksum,
    fontSize: 32, minFontSize: 12, padding: 4, fill: '#FFFFFF',
    strokeColor: '#000000', strokeWidth: 2, rotation: 0, scaleX: 1, scaleY: 1,
    shearX: 0, shearY: 0, opacity: 1, visualCenterX: 0.5, visualCenterY: 0.5,
    align: 'center', lineSpacing: 1, letterSpacing: 0, autoFit: true,
    fontSource: display ? 'server-display-default' : 'server-regular-default',
  });
  const styleInput = (style: TypesetRegionStyle) => {
    const { fontChecksum: _fontChecksum, fontSource: _fontSource, ...input } = style;
    void _fontChecksum;
    void _fontSource;
    return input;
  };
  const capability = { available: true, contractVersion: 'g10-art-lettering-v1',
    features: ['explicit-installed-chinese-display-font', 'fill-stroke', 'rotation',
      'nonuniform-scale', 'shear-affine', 'opacity', 'visual-center',
      'alignment', 'line-spacing'], reason: null };

  const manifestFloatKeys = new Set([
    'rotation', 'scaleX', 'scaleY', 'shearX', 'shearY', 'opacity',
    'visualCenterX', 'visualCenterY', 'lineSpacing', 'letterSpacing',
  ]);

  function pythonFloatJson(value: number): string {
    if (Object.is(value, -0)) return '-0.0';
    if (Number.isInteger(value)) return `${value}.0`;
    const absolute = Math.abs(value);
    let encoded = absolute > 0 && absolute < 1e-4 ? value.toExponential() : value.toString();
    if (encoded.includes('e')) {
      const [mantissa, rawExponent] = encoded.split('e');
      const exponent = Number(rawExponent);
      encoded = `${mantissa}e${exponent >= 0 ? '+' : '-'}${Math.abs(exponent).toString().padStart(2, '0')}`;
    }
    return encoded;
  }

  async function strictDigest(
    value: unknown,
    floatKeys: ReadonlySet<string> = new Set(),
  ): Promise<string> {
    const canonical = (entry: unknown, currentKey: string | null = null): string => {
      const value = entry;
      if (value === null) return 'null';
      if (Array.isArray(value)) return `[${value.map((item) => canonical(item, currentKey)).join(',')}]`;
      if (typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
      if (typeof value === 'number') {
        return currentKey && floatKeys.has(currentKey) ? pythonFloatJson(value) : JSON.stringify(value);
      }
      return `{${Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => left.localeCompare(right))
        .map(([key, item]) => `${JSON.stringify(key)}:${canonical(item, key)}`).join(',')}}`;
    };
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(canonical(value)));
    return Array.from(new Uint8Array(digest), (entry) => entry.toString(16).padStart(2, '0')).join('');
  }

  async function routeDigest(routes: TypesetGateContext['routeManifest']): Promise<string> {
    return strictDigest(routes);
  }

  async function g10Context(routes: TypesetGateContext['routeManifest']): Promise<TypesetGateContext> {
    return {
      imageId: 'image-1', imageRevision: 7, generationId: 'generation-1', nextSequence: 3,
      g9TerminalChecksum: '9'.repeat(64), translationStateChecksum: '8'.repeat(64),
      cleanPlateCandidateId: 'clean-1', cleanPlateChecksum: '7'.repeat(64),
      state: 'pending', terminalChecksum: null, candidates: [], reviews: [],
      routeManifest: routes, routeChecksum: await routeDigest(routes),
      styleDefaults: { bubble: frozenStyle(), ordinary: frozenStyle(),
        artLettering: frozenStyle(true) },
      availableFonts: [regularFont, displayFont], availableDisplayFonts: [displayFont],
      artLetteringCapability: capability, retryRegionStyles: {},
    };
  }

  function seedG10(context: TypesetGateContext) {
    const generation = pageGeneration(context.nextSequence);
    const g8Terminal = pageEvent(1, 'clean-plate-stage-review', {
      gate: 'G8_cleanPlate', stage: 'inpaint', state: 'accepted',
      decision: 'clean-plate-accepted', reason: 'clean-plate-complete',
      inputChecksum: '4'.repeat(64), outputChecksum: '5'.repeat(64),
      parentChecksum: '3'.repeat(64), jobId: null, jobItemId: null,
      evidence: {
        candidateId: context.cleanPlateCandidateId,
        candidateChecksum: context.cleanPlateChecksum,
        qualityChecksum: '6'.repeat(64),
      },
    });
    const g9Terminal = pageEvent(2, 'translation-stage-review', {
      gate: 'G9_translation', stage: 'translation', state: 'accepted',
      decision: 'translations-accepted', reason: 'all-translations-reviewed',
      inputChecksum: context.translationStateChecksum,
      outputChecksum: context.g9TerminalChecksum, parentChecksum: '5'.repeat(64),
      jobId: null, jobItemId: null,
      evidence: { eligibleRegionCount: 1, reviewedCandidateCount: 1, acceptedRegionCount: 1 },
    });
    useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => image.id === 'image-1' ? { ...image, revision: 7 } : image),
      regionsByImage: { ...state.regionsByImage, 'image-1': context.routeManifest.map((route) => {
        const frozen = context.candidates.at(-1)?.regionManifest.find((entry) =>
          entry.regionId === route.regionId);
        return regionFixture(route.regionId, {
          revision: 4,
          order: route.readingOrder,
          type: route.route === 'bubble' ? 'dialogue'
            : route.route === 'art-lettering' || route.route === 'keep' ? 'sound_effect' : 'other',
          contentDisposition: route.route === 'art-lettering' ? 'redraw-art'
            : route.route === 'keep' ? 'keep-art'
              : route.route === 'ignore' ? 'ignore' : 'translate',
          ...(frozen ? {
            revision: frozen.regionRevision, order: frozen.readingOrder,
            x: frozen.geometry.x, y: frozen.geometry.y,
            width: frozen.geometry.width, height: frozen.geometry.height,
            rotation: frozen.geometry.rotation,
            type: frozen.regionType,
            direction: frozen.direction,
            paragraphGroupId: frozen.paragraphGroupId,
            contentDisposition: frozen.contentDisposition,
          } : {}),
        });
      }) },
      g4Contexts: { 'image-1': { status: 'active', generation, events: [g8Terminal, g9Terminal],
        phase: 'G10', error: '', conflict: false } },
      typesetContexts: {},
    }));
  }

  beforeEach(() => {
    resetWorkbenchStore();
    seedWorkbench();
  });
  afterEach(() => vi.restoreAllMocks());

  it('cold-loads redraw-art-only and mixed explicit routes with server display defaults', async () => {
    const redraw = await g10Context([{ regionId: 'sfx-redraw', readingOrder: 1,
      route: 'art-lettering', renderRequired: true,
      translationCandidateId: 'translation-1', translationCandidateChecksum: '5'.repeat(64) }]);
    seedG10(redraw);
    vi.spyOn(api, 'getTypesetGateContext').mockResolvedValue(redraw);
    expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().typesetStyleDrafts['image-1']?.['sfx-redraw'])
      .toMatchObject({ fontToken: displayFont.token, fill: '#FFFFFF', lineSpacing: 1 });
    expect(useWorkbenchStore.getState().typesetStyleDrafts['image-1']?.['sfx-redraw'])
      .not.toHaveProperty('fontChecksum');

    for (const invalidDisplayFont of [
      { ...displayFont, token: 'installed-font-ffffffffffffffffffffffff' },
      { ...displayFont, capabilityChecksum: 'f'.repeat(64) },
    ]) {
      const tamperedFontContext: TypesetGateContext = {
        ...redraw,
        availableFonts: redraw.availableFonts.map((font) =>
          font.role === 'display' ? invalidDisplayFont : font),
        availableDisplayFonts: [invalidDisplayFont],
        styleDefaults: {
          ...redraw.styleDefaults,
          artLettering: redraw.styleDefaults.artLettering ? {
            ...redraw.styleDefaults.artLettering,
            fontToken: invalidDisplayFont.token,
            fontChecksum: invalidDisplayFont.fontChecksum,
          } : null,
        },
      };
      seedG10(tamperedFontContext);
      vi.mocked(api.getTypesetGateContext).mockResolvedValue(tamperedFontContext);
      expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(false);
    }

    for (const invalidFeatures of [
      capability.features.filter((feature) => feature !== 'alignment'),
      [...capability.features, 'unadvertised-art-feature'],
    ]) {
      const tamperedCapabilityContext: TypesetGateContext = {
        ...redraw,
        artLetteringCapability: { ...redraw.artLetteringCapability, features: invalidFeatures },
      };
      seedG10(tamperedCapabilityContext);
      vi.mocked(api.getTypesetGateContext).mockResolvedValue(tamperedCapabilityContext);
      expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(false);
    }

    seedG10(redraw);
    vi.mocked(api.getTypesetGateContext).mockResolvedValue({
      ...redraw, cleanPlateChecksum: '6'.repeat(64),
    });
    expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(false);

    const mixed = await g10Context([
      { regionId: 'dialogue', readingOrder: 1, route: 'bubble', renderRequired: true,
        translationCandidateId: 'translation-1', translationCandidateChecksum: '5'.repeat(64) },
      { regionId: 'sfx-redraw', readingOrder: 2, route: 'art-lettering', renderRequired: true,
        translationCandidateId: 'translation-2', translationCandidateChecksum: 'a'.repeat(64) },
      { regionId: 'sfx-kept', readingOrder: 3, route: 'keep', renderRequired: false,
        translationCandidateId: null, translationCandidateChecksum: null },
      { regionId: 'ignored', readingOrder: 4, route: 'ignore', renderRequired: false,
        translationCandidateId: null, translationCandidateChecksum: null },
    ]);
    seedG10(mixed);
    vi.mocked(api.getTypesetGateContext).mockResolvedValue(mixed);
    expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(true);
    expect(Object.keys(useWorkbenchStore.getState().typesetStyleDrafts['image-1'] ?? {}))
      .toEqual(['dialogue', 'sfx-redraw']);

    const nonRendering = { ...await g10Context([
      { regionId: 'sfx-kept', readingOrder: 1, route: 'keep', renderRequired: false,
        translationCandidateId: null, translationCandidateChecksum: null },
      { regionId: 'ignored', readingOrder: 2, route: 'ignore', renderRequired: false,
        translationCandidateId: null, translationCandidateChecksum: null },
    ]), availableFonts: [], availableDisplayFonts: [],
      styleDefaults: { bubble: null, ordinary: null, artLettering: null },
      artLetteringCapability: { ...capability, available: false,
        reason: 'g10-art-lettering-capability-required' } };
    seedG10(nonRendering);
    vi.mocked(api.getTypesetGateContext).mockResolvedValue(nonRendering);
    expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().typesetStyleDrafts['image-1']).toEqual({});

    const unknownTranslate = await g10Context([{ regionId: 'unknown-text', readingOrder: 1,
      route: 'ordinary', renderRequired: true,
      translationCandidateId: 'translation-unknown',
      translationCandidateChecksum: '6'.repeat(64) }]);
    seedG10(unknownTranslate);
    useWorkbenchStore.setState((state) => ({ regionsByImage: {
      ...state.regionsByImage,
      'image-1': [regionFixture('unknown-text', {
        revision: 4, order: 1, type: 'unknown', contentDisposition: 'translate',
      })],
    } }));
    vi.mocked(api.getTypesetGateContext).mockResolvedValue(unknownTranslate);
    expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(false);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.error).toContain('G10 上下文');
  });

  it('locks a tampered art style that points at a regular font and excludes legacy batch bypass', async () => {
    const context = await g10Context([{ regionId: 'sfx-redraw', readingOrder: 1,
      route: 'art-lettering', renderRequired: true,
      translationCandidateId: 'translation-1', translationCandidateChecksum: '5'.repeat(64) }]);
    const tampered = { ...context, styleDefaults: { ...context.styleDefaults,
      artLettering: { ...frozenStyle(), fontSource: 'region-override' as const } } };
    seedG10(tampered);
    vi.spyOn(api, 'getTypesetGateContext').mockResolvedValue(tampered);
    expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(false);
    expect(useWorkbenchStore.getState().g4Contexts['image-1']?.error).toContain('G10 上下文');

    seedG10(context);
    expect(await useWorkbenchStore.getState().startBatch(
      ['typeset'], ['image-1'], { format: 'images', imageVariant: 'typeset',
        conflict: 'rename', preserveTree: true },
    )).toBe(false);
    expect(useWorkbenchStore.getState().globalError).toContain('旧版批处理入口');
  });

  it('rejects route-unsupported styles locally and sends only exact G10 job options', async () => {
    const context = await g10Context([
      { regionId: 'dialogue', readingOrder: 1, route: 'bubble', renderRequired: true,
        translationCandidateId: 'translation-1', translationCandidateChecksum: '5'.repeat(64) },
      { regionId: 'sfx-redraw', readingOrder: 2, route: 'art-lettering', renderRequired: true,
        translationCandidateId: 'translation-2', translationCandidateChecksum: 'a'.repeat(64) },
    ]);
    seedG10(context);
    const validStyles = {
      dialogue: styleInput(frozenStyle()),
      'sfx-redraw': styleInput(frozenStyle(true)),
    };
    useWorkbenchStore.setState({ typesetContexts: { 'image-1': context },
      typesetStyleDrafts: { 'image-1': validStyles } });
    const startJob = vi.spyOn(api, 'startJob').mockRejectedValue(new Error('stop after dispatch'));
    const reload = vi.fn(async () => undefined);
    useWorkbenchStore.setState({ reloadActiveImage: reload });

    for (const invalidStyles of [
      { ...validStyles, dialogue: { ...validStyles.dialogue, scaleX: 1.2 } },
      { ...validStyles, 'sfx-redraw': { ...validStyles['sfx-redraw'], letterSpacing: 1 } },
      { ...validStyles, dialogue: { ...validStyles.dialogue,
        align: 'sideways' as unknown as 'center' } },
      { ...validStyles, dialogue: { ...validStyles.dialogue,
        autoFit: 'true' as unknown as boolean } },
      { ...validStyles, dialogue: { ...validStyles.dialogue, fontSize: 32.5 } },
    ]) {
      expect(await useWorkbenchStore.getState().startG10Typeset(invalidStyles)).toBe(false);
    }
    expect(startJob).not.toHaveBeenCalled();

    expect(await useWorkbenchStore.getState().startG10Typeset(validStyles)).toBe(false);
    expect(startJob).toHaveBeenCalledWith('project-1', 'typeset', expect.objectContaining({
      imageIds: ['image-1'], regionIds: [], options: { regionStyles: validStyles },
      lineage: expect.objectContaining({ pages: [{ imageId: 'image-1',
        pageGenerationId: 'generation-1', expectedSequence: 3 }] }),
    }));
    const dispatchedStyles = vi.mocked(startJob).mock.calls[0]?.[2].options?.regionStyles as
      Record<string, Record<string, unknown>>;
    expect(dispatchedStyles.dialogue).not.toHaveProperty('fontChecksum');
    expect(dispatchedStyles['sfx-redraw']).not.toHaveProperty('fontSource');
    expect(reload).toHaveBeenCalledOnce();
  });

  it('cold-loads only a completed candidate exactly bound to publication evidence', async () => {
    const routes: TypesetGateContext['routeManifest'] = [{
      regionId: 'sfx-redraw', readingOrder: 1, route: 'art-lettering', renderRequired: true,
      translationCandidateId: 'translation-1', translationCandidateChecksum: '5'.repeat(64),
    }];
    const base = await g10Context(routes);
    const style = { ...frozenStyle(true), rotation: 10 };
    const candidate: TypesetGateContext['candidates'][number] = {
      candidateId: 'typeset-candidate-1', sequence: 3, jobId: 'job-typeset',
      jobItemId: 'item-typeset', parentChecksum: base.g9TerminalChecksum,
      g9TerminalChecksum: base.g9TerminalChecksum,
      translationStateChecksum: base.translationStateChecksum,
      cleanPlateCandidateId: base.cleanPlateCandidateId,
      cleanPlateChecksum: base.cleanPlateChecksum,
      regionManifest: [{ regionId: 'sfx-redraw', regionRevision: 4,
        geometry: { x: 360, y: 400, width: 220, height: 120, rotation: 5 },
        readingOrder: 1, regionType: 'sound_effect', direction: 'vertical',
        paragraphGroupId: null, contentDisposition: 'redraw-art',
        acceptedTranslationCandidateId: 'translation-1',
        acceptedTranslationCandidateChecksum: '5'.repeat(64) }],
      routeManifest: routes, routeChecksum: base.routeChecksum,
      styleManifest: [{ regionId: 'sfx-redraw', route: 'art-lettering', style }],
      styleChecksum: 'a'.repeat(64),
      layoutManifest: [{ regionId: 'sfx-redraw', route: 'art-lettering',
        bounds: { x: 720, y: 800, width: 440, height: 240 }, fontSize: 64,
        overflow: false, direction: 'vertical', rotation: 15, scaleX: 1, scaleY: 1,
        shearX: 0, shearY: 0, opacity: 1, visualCenterX: 0.5,
        visualCenterY: 0.5, align: 'center' }],
      layoutChecksum: 'b'.repeat(64), provider: 'pillow-g10',
      modelVersion: 'g10-typeset-v1', parameterHash: 'c'.repeat(64),
      candidateChecksum: 'd'.repeat(64), width: 2400, height: 3600, renderScale: 2,
      overflowRegionIds: [], anomalies: [], revisionId: 'revision-typeset-1',
      completed: true, artifactUrl: api.typesetCandidateUrl('image-1', 'typeset-candidate-1'),
      review: null, createdAt: '2026-08-25T00:00:00Z',
    };
    candidate.styleChecksum = await strictDigest(candidate.styleManifest, manifestFloatKeys);
    candidate.layoutChecksum = await strictDigest(candidate.layoutManifest, manifestFloatKeys);
    expect(candidate.styleChecksum)
      .toBe('c8d099fda4979d1ad15966fec0b3a8856998b0318161856dac5881297eefb5c9');
    expect(candidate.layoutChecksum)
      .toBe('99f9473b1521ae5cdbba1dd6d29e4e0eff9e28b04b83a3536286029802050121');
    const produced = pageEvent(3, 'typeset-candidate-produced', {
      gate: 'G10_typeset', stage: 'typeset', jobId: candidate.jobId,
      jobItemId: candidate.jobItemId, revisionId: candidate.revisionId,
      provider: candidate.provider, modelVersion: candidate.modelVersion,
      parameterHash: candidate.parameterHash, evidence: {
        candidateId: candidate.candidateId, candidateChecksum: candidate.candidateChecksum,
        g9TerminalChecksum: candidate.g9TerminalChecksum,
        cleanPlateChecksum: candidate.cleanPlateChecksum,
        routeChecksum: candidate.routeChecksum, styleChecksum: candidate.styleChecksum,
        layoutChecksum: candidate.layoutChecksum, width: candidate.width, height: candidate.height,
        renderScale: candidate.renderScale, overflowRegionIds: [], anomalies: [],
      },
    });
    const completed = pageEvent(4, 'typeset-job-completed', {
      gate: 'G10_typeset', stage: 'typeset', jobId: candidate.jobId,
      jobItemId: candidate.jobItemId, revisionId: null,
      provider: candidate.provider, modelVersion: candidate.modelVersion,
      parameterHash: candidate.parameterHash, evidence: {
        candidateId: candidate.candidateId, candidateChecksum: candidate.candidateChecksum,
        g9TerminalChecksum: candidate.g9TerminalChecksum,
        cleanPlateChecksum: candidate.cleanPlateChecksum,
        routeChecksum: candidate.routeChecksum, styleChecksum: candidate.styleChecksum,
        layoutChecksum: candidate.layoutChecksum, width: candidate.width, height: candidate.height,
        renderScale: candidate.renderScale, overflowRegionIds: [], anomalies: [],
      },
    });
    const context: TypesetGateContext = { ...base, nextSequence: 5, candidates: [candidate] };
    seedG10(context);
    useWorkbenchStore.setState((state) => ({
      g4Contexts: { 'image-1': { ...state.g4Contexts['image-1']!,
        generation: pageGeneration(5),
        events: [...state.g4Contexts['image-1']!.events.slice(0, 2), produced, completed] } },
    }));
    vi.spyOn(api, 'getTypesetGateContext').mockResolvedValue(context);
    expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().selectedTypesetCandidateIds['image-1'])
      .toBe(candidate.candidateId);

    for (const tamperedCandidate of [
      { ...candidate, completed: false },
      { ...candidate, provider: 'forged-provider' },
      { ...candidate, styleManifest: [{ ...candidate.styleManifest[0]!,
        style: { ...style, fontChecksum: displayFont.capabilityChecksum } }] },
      { ...candidate, styleManifest: [{ ...candidate.styleManifest[0]!,
        style: { ...style, autoFit: 'true' as unknown as boolean } }] },
      { ...candidate, styleManifest: [{ ...candidate.styleManifest[0]!,
        style: { ...style, fill: '#123456' } }] },
      { ...candidate, layoutManifest: [{ ...candidate.layoutManifest[0]!,
        bounds: { ...candidate.layoutManifest[0]!.bounds, x: 721 } }] },
      { ...candidate, layoutManifest: [{ ...candidate.layoutManifest[0]!, rotation: style.rotation }] },
      { ...candidate, layoutManifest: [{ ...candidate.layoutManifest[0]!, fontSize: 12 }] },
      { ...candidate, renderScale: 3 },
    ]) {
      const tampered = { ...context, candidates: [tamperedCandidate] };
      seedG10(tampered);
      useWorkbenchStore.setState((state) => ({
        g4Contexts: { 'image-1': { ...state.g4Contexts['image-1']!,
          generation: pageGeneration(5),
          events: [...state.g4Contexts['image-1']!.events.slice(0, 2), produced, completed] } },
      }));
      vi.mocked(api.getTypesetGateContext).mockResolvedValue(tampered);
      expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(false);
    }

    const retryStyle = styleInput(style);
    const rejectedReview: TypesetGateContext['reviews'][number] = {
      id: 'typeset-review-1', sequence: 5, candidateId: candidate.candidateId,
      state: 'rejected', reason: 'overflow-free', parentChecksum: base.g9TerminalChecksum,
      candidateChecksum: candidate.candidateChecksum, routeChecksum: candidate.routeChecksum,
      styleChecksum: candidate.styleChecksum, layoutChecksum: candidate.layoutChecksum,
      g9TerminalChecksum: base.g9TerminalChecksum, cleanPlateChecksum: base.cleanPlateChecksum,
      observedWidth: candidate.width, observedHeight: candidate.height,
      observedRenderScale: candidate.renderScale,
      checks: TYPESET_CHECKS.map((check) => ({ check, passed: check !== 'overflow-free' })),
      reviewer: { actorKind: 'system', actorId: 'queue', operationSource: 'api' },
      terminalChecksum: 'e'.repeat(64), revisionId: 'revision-typeset-review-1',
      createdAt: '2026-08-25T00:01:00Z',
    };
    const rejectedCandidate = { ...candidate, review: rejectedReview };
    const reviewedEvent = pageEvent(5, 'typeset-candidate-reviewed', {
      gate: 'G10_typeset', stage: 'typeset', state: 'rejected',
      decision: 'candidate-rejected', reason: 'overflow-free',
      jobId: null, jobItemId: null, revisionId: rejectedReview.revisionId,
      outputChecksum: rejectedReview.terminalChecksum, startedAt: null, finishedAt: null,
      evidence: { candidateId: candidate.candidateId },
    });
    const rejectedContext: TypesetGateContext = {
      ...base, nextSequence: 6, candidates: [rejectedCandidate], reviews: [rejectedReview],
      retryRegionStyles: { 'sfx-redraw': retryStyle },
    };
    seedG10(rejectedContext);
    useWorkbenchStore.setState((state) => ({
      g4Contexts: { 'image-1': { ...state.g4Contexts['image-1']!, generation: pageGeneration(6),
        events: [...state.g4Contexts['image-1']!.events.slice(0, 2),
          produced, completed, reviewedEvent] } },
    }));
    vi.mocked(api.getTypesetGateContext).mockResolvedValue(rejectedContext);
    expect(await useWorkbenchStore.getState().loadTypesetContext('image-1', true)).toBe(true);
    expect(useWorkbenchStore.getState().typesetStyleDrafts['image-1']?.['sfx-redraw'])
      .toEqual(retryStyle);
    expect(useWorkbenchStore.getState().typesetStyleDrafts['image-1']?.['sfx-redraw'])
      .not.toHaveProperty('fontChecksum');

    seedG10(context);
    useWorkbenchStore.setState((state) => ({
      g4Contexts: { 'image-1': { ...state.g4Contexts['image-1']!, generation: pageGeneration(5),
        events: [...state.g4Contexts['image-1']!.events.slice(0, 2), produced, completed] } },
      typesetContexts: { 'image-1': context },
      typesetBitmapObservations: { 'image-1': {
        imageId: 'image-1', generationId: 'generation-1', nextSequence: 5,
        candidateId: candidate.candidateId, imageRevision: 7,
        sourceChecksum: pageGeneration().sourceChecksum,
        cleanPlateChecksum: candidate.cleanPlateChecksum,
        candidateChecksum: candidate.candidateChecksum, routeChecksum: candidate.routeChecksum,
        styleChecksum: candidate.styleChecksum, layoutChecksum: candidate.layoutChecksum,
        width: candidate.width, height: candidate.height, renderScale: candidate.renderScale,
        state: 'ready',
      } },
    }));
    const reviewWrite = vi.spyOn(api, 'reviewTypesetCandidate')
      .mockRejectedValue(new Error('connection dropped after write'));
    const reload = vi.fn(async () => undefined);
    useWorkbenchStore.setState({ reloadActiveImage: reload });
    expect(await useWorkbenchStore.getState().reviewG10TypesetCandidate(
      candidate.candidateId,
      'reject',
      TYPESET_CHECKS.map((check) => ({
        check, passed: check !== 'typography-source-matched',
      })),
      'typography-source-matched',
      ['typography-source-matched'],
    )).toBe(false);
    expect(reviewWrite).not.toHaveBeenCalled();

    const defectCandidate = {
      ...candidate,
      overflowRegionIds: ['sfx-redraw'],
      layoutManifest: candidate.layoutManifest.map((entry) => ({ ...entry, overflow: true })),
    };
    useWorkbenchStore.setState({
      typesetContexts: { 'image-1': { ...context, candidates: [defectCandidate] } },
    });
    expect(await useWorkbenchStore.getState().reviewG10TypesetCandidate(
      candidate.candidateId,
      'reject',
      TYPESET_CHECKS.map((check) => ({
        check, passed: check !== 'typography-source-matched',
      })),
      'typography-source-matched',
      [...TYPESET_CHECKS],
    )).toBe(false);
    expect(reviewWrite).not.toHaveBeenCalled();

    useWorkbenchStore.setState({ typesetContexts: { 'image-1': context } });
    expect(await useWorkbenchStore.getState().reviewG10TypesetCandidate(
      candidate.candidateId,
      'accept',
      TYPESET_CHECKS.map((check) => ({ check, passed: true })),
      'typeset-reviewed',
      [...TYPESET_CHECKS],
    )).toBe(false);
    expect(reviewWrite).toHaveBeenCalledOnce();
    expect(reload).toHaveBeenCalledOnce();
  });
});
