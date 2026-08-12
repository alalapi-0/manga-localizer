import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, api } from '../api/client';
import {
  imageFixture,
  jobFixture,
  projectFixture,
  regionFixture,
  seedWorkbench,
} from '../test/fixtures';
import { activeRegions, resetWorkbenchStore, useWorkbenchStore } from './workbench';

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((next) => {
    resolve = next;
  });
  return { promise, resolve };
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

  it('flushes a delayed substantive edit before sending a sparse reconfirmation', async () => {
    vi.useFakeTimers();
    const confirmed = regionFixture('region-1', { confirmed: true });
    seedWorkbench({ regions: [confirmed] });
    const firstSave = deferred<ReturnType<typeof regionFixture>>();
    let callCount = 0;
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => {
      callCount += 1;
      if (callCount === 1) return firstSave.promise;
      return {
        ...confirmed,
        translationText: '编辑后译文',
        confirmed: true,
        revision: 6,
        ...patch,
      };
    });

    useWorkbenchStore.getState().updateRegion('region-1', {
      translationText: '编辑后译文',
    });
    expect(vi.getTimerCount()).toBe(1);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.confirmed).toBe(false);

    const reconfirming = useWorkbenchStore.getState().setRegionConfirmed('region-1', true);
    await vi.advanceTimersByTimeAsync(0);
    expect(update).toHaveBeenCalledTimes(1);
    expect(update).toHaveBeenNthCalledWith(1, 'region-1', expect.objectContaining({
      translationText: '编辑后译文',
      confirmed: false,
      expectedRevision: 4,
    }));
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.confirmed).toBe(false);

    firstSave.resolve({
      ...confirmed,
      translationText: '编辑后译文',
      confirmed: false,
      revision: 5,
    });
    await vi.waitFor(() => expect(update).toHaveBeenCalledTimes(2));

    expect(update).toHaveBeenNthCalledWith(2, 'region-1', {
      confirmed: true,
      expectedRevision: 5,
    });
    expect(await reconfirming).toBe(true);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      translationText: '编辑后译文',
      confirmed: true,
      revision: 6,
    });
    expect(useWorkbenchStore.getState().pendingRegionMutations).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
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
        regionFixture('region-1', { confirmed: true }),
        regionFixture('region-2', { confirmed: false }),
      ],
    });
    const review = vi.spyOn(api, 'reviewImage');

    expect(await useWorkbenchStore.getState().reviewActiveImage('reviewed')).toBe(false);
    expect(useWorkbenchStore.getState().globalError).toBe('还有 1 个活动文本框尚未确认。');
    expect(await useWorkbenchStore.getState().reviewActiveImage('no-text-reviewed')).toBe(false);
    expect(review).not.toHaveBeenCalled();
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

  it('always queues pipeline stages in detect-to-export order', async () => {
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
    )).toBe(true);

    expect(calls).toEqual(['detect', 'ocr', 'translate', 'inpaint', 'typeset', 'export']);
    expect(useWorkbenchStore.getState().currentProject?.revision).toBe(12);
    expect(vi.mocked(api.startJob).mock.calls.find((call) => call[1] === 'translate')?.[2])
      .toEqual(expect.objectContaining({
        options: expect.objectContaining({
          targetLanguage: 'zh-CN',
          characterNames: { 桜: '桜', 太郎: '小明' },
          concurrency: 3,
        }),
      }));
    expect(vi.mocked(api.exportProject).mock.calls[0]?.[1].options.concurrency).toBe(1);
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
