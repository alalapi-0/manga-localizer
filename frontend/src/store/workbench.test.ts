import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, api } from '../api/client';
import {
  imageFixture,
  jobFixture,
  projectFixture,
  regionFixture,
  seedWorkbench,
} from '../test/fixtures';
import { activeRegions, canNavigateAdjacent, imageReviewState, latestPageProcessingActivity, latestPageProcessingError, matchingQueueJob, overflowingRegionIds, resetWorkbenchStore, useWorkbenchStore, visibleImagePosition } from './workbench';

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
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

describe('workbench store', () => {
  beforeEach(() => {
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture(),
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images,
    );
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

  it('preserves a delete issued for the second of two snapshotted creates', async () => {
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
    expect(create).toHaveBeenCalledTimes(2);
    expect(remove).toHaveBeenCalledWith('region-server-b', 1);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toEqual([
      expect.objectContaining({ id: 'region-server-a' }),
    ]);
    expect(firstId).toMatch(/^local-/);
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
      },
    } as unknown as ReturnType<typeof regionFixture>]);

    await useWorkbenchStore.getState().loadRegions('image-1', true);

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair).toMatchObject({
      method: 'navier_stokes',
      maskPadding: 7,
      dilation: 3,
      radius: 5,
    });
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
        { id: 'primary', label: '当前 Provider 结果', anomalies: [] },
        { id: 'lineart-guided', label: '线稿引导(结构+纹理)', anomalies: [] },
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

  it('keeps an already-created stage visible when the next stage creation fails', async () => {
    seedWorkbench();
    const startJob = vi.spyOn(api, 'startJob')
      .mockResolvedValueOnce(jobFixture({ id: 'job-preprocess', kind: 'preprocess' }))
      .mockRejectedValueOnce(new Error('detector unavailable'));

    expect(await useWorkbenchStore.getState().startBatch(
      ['detect', 'preprocess'],
      ['image-1'],
      { format: 'both', imageVariant: 'typeset', conflict: 'rename', preserveTree: true },
    )).toBe(false);

    expect(startJob.mock.calls.map((call) => call[1])).toEqual(['preprocess', 'detect']);
    expect(useWorkbenchStore.getState()).toMatchObject({
      globalError: 'detector unavailable',
      jobs: [expect.objectContaining({ id: 'job-preprocess', kind: 'preprocess' })],
      images: [
        expect.objectContaining({
          id: 'image-1',
          status: expect.objectContaining({
            preprocess: 'queued',
            detection: 'not_started',
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
});
