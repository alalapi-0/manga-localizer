import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { resetWorkbenchStore, useWorkbenchStore } from '../store/workbench';
import { imageFixture, regionFixture, seedWorkbench } from '../test/fixtures';
import type { TypesetGateContext } from '../types';
import {
  buildMaskStroke,
  centeredNodeToRegionGeometry,
  clampRegionGeometry,
  clusterRegionIds,
  frameRegions,
  isKonvaRegionEditTarget,
  maskEditCapacity,
  MAX_MASK_BRUSH_RADIUS,
  MAX_MASK_EDIT_POINTS,
  MAX_MASK_EDIT_STROKES,
  MAX_MASK_POINTS_PER_STROKE,
  regionToCenteredNodeGeometry,
} from './canvasGeometry';
import { loadCanonicalCanvasImage } from './canvasImage';
import { CanvasWorkspace } from './CanvasWorkspace';

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

function pngPayload(label = ''): ArrayBuffer {
  return new Uint8Array([...PNG_SIGNATURE, ...new TextEncoder().encode(label)]).buffer;
}

describe('canvas generated-image refresh', () => {
  beforeEach(() => {
    vi.stubGlobal('crypto', {
      randomUUID: () => '00000000-0000-4000-8000-000000000000',
      subtle: {
        digest: vi.fn(async () => new Uint8Array(32).fill(0xaa).buffer),
      },
    });
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'image/png' }),
      arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer,
    } as unknown as Response)));
    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({
      width: 1200,
      height: 1800,
      close: vi.fn(),
    } as unknown as ImageBitmap)));
  });

  afterEach(() => {
    resetWorkbenchStore();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('decodes the raw pixel grid without EXIF orientation and retries by image revision', async () => {
    const requested: string[] = [];
    let requestedSource = '';
    const decode = vi.fn(async (_blob: Blob, options?: ImageBitmapOptions) => {
      if (options?.imageOrientation !== 'none') throw new Error('EXIF orientation was not disabled');
      if (requestedSource.includes('v=4')) throw new Error('preview missing');
      return {
        width: 1200,
        height: 1800,
        close: vi.fn(),
      } as unknown as ImageBitmap;
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const source = String(input);
      requestedSource = source;
      requested.push(source);
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'image/png' }),
        arrayBuffer: async () => new TextEncoder().encode(source).buffer,
      } as unknown as Response;
    }));
    vi.stubGlobal('createImageBitmap', decode);
    const initial = imageFixture('image-1', {
      revision: 4,
      status: { ...imageFixture('image-1').status, typeset: 'done' },
    });
    seedWorkbench({ images: [initial] });
    useWorkbenchStore.setState({ canvasMode: 'typeset' });
    render(<CanvasWorkspace />);

    await waitFor(() => expect(requested[0]).toContain('typeset?v=4'));
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('图像读取失败'));

    useWorkbenchStore.setState({
      images: [
        imageFixture('image-1', {
          revision: 5,
          status: { ...imageFixture('image-1').status, typeset: 'done' },
        }),
      ],
    });
    await waitFor(() => expect(requested[1]).toContain('typeset?v=5'));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(decode).toHaveBeenCalledWith(expect.any(Blob), { imageOrientation: 'none' });
  });

  it('bypasses HTTP cache when loading a generated preview', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'image/png' }),
      arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer,
    } as unknown as Response));
    vi.stubGlobal('fetch', fetchMock);

    await loadCanonicalCanvasImage('/generated/typeset?v=5', { width: 1200, height: 1800 });

    expect(fetchMock).toHaveBeenCalledWith(
      '/generated/typeset?v=5',
      expect.objectContaining({ cache: 'no-store' }),
    );
  });

  it('frames selected boxes inside the viewport instead of fitting the whole page', () => {
    expect(frameRegions({ width: 0, height: 400 }, [{ x: 100, y: 100, width: 50, height: 50 }])).toBeNull();
    expect(frameRegions({ width: 400, height: 400 }, [])).toBeNull();

    const viewport = frameRegions(
      { width: 400, height: 400 },
      [{ x: 200, y: 300, width: 40, height: 60 }],
      { width: 1200, height: 1800 },
    );
    expect(viewport).not.toBeNull();
    expect(viewport?.scale).toBeGreaterThan(0.5);
    const left = (0 - (viewport?.x ?? 0)) / (viewport?.scale ?? 1);
    const top = (0 - (viewport?.y ?? 0)) / (viewport?.scale ?? 1);
    const right = (400 - (viewport?.x ?? 0)) / (viewport?.scale ?? 1);
    const bottom = (400 - (viewport?.y ?? 0)) / (viewport?.scale ?? 1);
    expect(left).toBeLessThan(200);
    expect(top).toBeLessThan(300);
    expect(right).toBeGreaterThan(240);
    expect(bottom).toBeGreaterThan(360);
  });

  it('rejects a decoded image whose dimensions do not match the canonical backend grid', async () => {
    const close = vi.fn();
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'image/jpeg' }),
      arrayBuffer: async () => new Uint8Array([1]).buffer,
    } as unknown as Response)));
    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({
      width: 1800,
      height: 1200,
      close,
    } as unknown as ImageBitmap)));

    await expect(loadCanonicalCanvasImage('/oriented.jpg', { width: 1200, height: 1800 }))
      .rejects.toThrow('does not match canonical grid');
    expect(close).toHaveBeenCalledOnce();
  });

  it('rejects a mask unless both its media type and byte signature are PNG', async () => {
    const decode = vi.mocked(globalThis.createImageBitmap);
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'image/jpeg' }),
        arrayBuffer: async () => pngPayload('jpeg-header-lie'),
      } as unknown as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'image/png; charset=binary' }),
        arrayBuffer: async () => new Uint8Array([0xff, 0xd8, 0xff, 0xe0]).buffer,
      } as unknown as Response));

    await expect(loadCanonicalCanvasImage(
      '/mask-as-jpeg', { width: 1200, height: 1800 }, undefined, true, 'png',
    )).rejects.toThrow('canonical PNG');
    await expect(loadCanonicalCanvasImage(
      '/jpeg-bytes-as-mask', { width: 1200, height: 1800 }, undefined, true, 'png',
    )).rejects.toThrow('canonical PNG');
    expect(decode).not.toHaveBeenCalled();
  });

  it('accepts only supported integer upscale grids for preprocessing previews', async () => {
    const close = vi.fn();
    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({
      width: 2400,
      height: 3600,
      close,
    } as unknown as ImageBitmap)));

    const loaded = await loadCanonicalCanvasImage(
      '/preprocessed.png',
      { width: 1200, height: 1800 },
      undefined,
      true,
    );

    expect(loaded).toMatchObject({
      checksum: 'aa'.repeat(32),
      pixelWidth: 2400,
      pixelHeight: 3600,
    });
    expect(close).not.toHaveBeenCalled();

    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({
      width: 2400,
      height: 3500,
      close,
    } as unknown as ImageBitmap)));
    await expect(loadCanonicalCanvasImage(
      '/bad-preprocessed.png',
      { width: 1200, height: 1800 },
      undefined,
      true,
    )).rejects.toThrow('does not match canonical grid');
    expect(close).toHaveBeenCalledOnce();
  });

  it('binds the G8 four-view observation to the context accepted mask, not an old selection', async () => {
    const requested: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requested.push(String(input));
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'image/png' }),
        arrayBuffer: async () => pngPayload(String(input)),
      } as unknown as Response;
    }));
    const checksum = 'aa'.repeat(32);
    const image = imageFixture('image-1', { revision: 11, width: 1200, height: 1800 });
    seedWorkbench({ images: [image], regions: [regionFixture('region-1', {
      contentDisposition: 'translate', backgroundCategory: 'complex-lineart',
      backgroundGenerationId: 'generation-1',
    })] });
    const maskArtifact = (artifactId: string, sequence: number) => ({
      artifactId, sequence, jobId: `job-mask-${sequence}`, jobItemId: `item-mask-${sequence}`,
      parentChecksum: checksum, qualityChecksum: checksum, recipeChecksum: checksum,
      maskChecksum: checksum, width: 1200, height: 1800, renderScale: 1,
      provider: 'deterministic-mask', modelVersion: 'create-mask-v1', parameterHash: checksum,
      nonzeroPixelCount: 42, bbox: { x: 1, y: 2, width: 3, height: 4 },
      createdAt: '2026-08-25T00:00:00Z',
    });
    const acceptedMask = maskArtifact('mask-accepted', 2);
    const oldMask = maskArtifact('mask-old', 1);
    const routeManifest = [{
      regionId: 'region-1', backgroundCategory: 'complex-lineart' as const,
      route: 'ai-inpaint-redraw' as const, originKind: 'ai' as const,
      provider: 'lama', modelVersion: 'lama-onnx-local-v1', parameterHash: checksum,
    }];
    const candidate = {
      candidateId: 'candidate-1', sequence: 1, jobId: 'job-inpaint',
      jobItemId: 'item-inpaint', parentChecksum: checksum, qualityChecksum: checksum,
      backgroundChecksum: checksum, maskArtifactId: acceptedMask.artifactId,
      maskChecksum: checksum, routeManifest, routeChecksum: checksum,
      originKind: 'ai' as const, providerIds: ['lama'], modelVersions: ['lama-onnx-local-v1'],
      parameterHash: checksum, candidateChecksum: checksum, width: 1200, height: 1800,
      renderScale: 1, outsideMaskChangeCount: 0, anomalies: [], completed: true,
      review: null, createdAt: '2026-08-25T00:00:00Z',
    };
    useWorkbenchStore.setState({
      g4Contexts: { 'image-1': {
        status: 'active', phase: 'G8', error: '', conflict: false,
        generation: {
          id: 'generation-1', runId: 'run-1', projectId: 'project-1', imageId: 'image-1',
          restartFromSource: true, parameterSetId: 'params-1', parameterSetHash: checksum,
          sourceProjectId: 'project-1', sourceImageId: 'image-1', sourceChecksum: checksum,
          state: 'active', nextSequence: 22,
          actor: { actorKind: 'codex', taskId: 'task-1', operationSource: 'api' },
          createdAt: '2026-08-25T00:00:00Z', closedAt: null,
        },
        events: [],
      } },
      maskContexts: { 'image-1': {
        imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 22,
        g6Checksum: checksum, qualityChecksum: checksum, maskStateChecksum: checksum,
        state: 'accepted', eligibleRegionIds: ['region-1'],
        rubyRegionIdsByPrimary: { 'region-1': [] },
        draft: { revision: 1, stateChecksum: checksum, regions: [] },
        artifacts: [oldMask, acceptedMask], selectedArtifactId: acceptedMask.artifactId,
        review: {
          id: 'mask-review', state: 'accepted', reason: 'complete-and-no-collateral',
          artifactId: acceptedMask.artifactId, maskChecksum: checksum,
          coverageChecks: [], collateralChecks: [],
          reviewer: { actorKind: 'human', sessionId: 'reviewer', operationSource: 'ui' },
          createdAt: '2026-08-25T00:00:00Z',
        },
      } },
      selectedMaskArtifactIds: { 'image-1': oldMask.artifactId },
      cleanPlateContexts: { 'image-1': {
        imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 22,
        g7Checksum: checksum, qualityChecksum: checksum, backgroundChecksum: checksum,
        maskArtifactId: acceptedMask.artifactId, maskChecksum: checksum,
        cleanPlateStateChecksum: checksum, state: 'pending',
        routes: [{ regionId: 'region-1', backgroundCategory: 'complex-lineart',
          defaultRoute: 'ai-inpaint-redraw' }],
        candidates: [candidate], acceptedCandidateId: null,
        fallbackEnabled: false, fallbackAllowed: false,
      } },
      selectedCleanPlateCandidateIds: { 'image-1': candidate.candidateId },
    });

    render(<CanvasWorkspace />);

    expect(await screen.findByText('质量底板 · accepted mask')).toBeInTheDocument();
    await waitFor(() => expect(
      useWorkbenchStore.getState().cleanPlateBitmapObservations['image-1'],
    ).toMatchObject({
      generationId: 'generation-1', nextSequence: 22,
      candidateId: 'candidate-1', maskArtifactId: 'mask-accepted',
      maskChecksum: checksum, checksum, state: 'ready',
    }));
    expect(requested.some((url) => url.includes('/page-gates/mask/artifacts/mask-accepted')))
      .toBe(true);
    expect(requested.some((url) => url.includes('/page-gates/mask/artifacts/mask-old')))
      .toBe(false);
    expect(requested.some((url) => url.includes('/page-gates/clean-plate/candidates/candidate-1')))
      .toBe(true);

    act(() => useWorkbenchStore.setState((state) => ({
      cleanPlateContexts: {
        ...state.cleanPlateContexts,
        'image-1': {
          ...state.cleanPlateContexts['image-1']!,
          candidates: [{ ...candidate, candidateChecksum: 'bb'.repeat(32) }],
        },
      },
    })));
    await waitFor(() => expect(
      useWorkbenchStore.getState().cleanPlateBitmapObservations['image-1'],
    ).toBeUndefined());
    await waitFor(() => expect(useWorkbenchStore.getState().globalError)
      .toContain('G8 四视图'));
  });

  it('observes a scaled clean parent and candidate, and rejects a base-grid clean parent', async () => {
    const requested: string[] = [];
    const decodedGrids: Array<{ payload: string; width: number; height: number }> = [];
    let returnBaseGridCleanPlate = false;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requested.push(String(input));
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'image/png' }),
        arrayBuffer: async () => pngPayload(String(input)),
      } as unknown as Response;
    }));
    vi.stubGlobal('createImageBitmap', vi.fn(async (blob: Blob) => {
      const payload = await blob.text();
      const scaled = payload.includes('/page-gates/typeset/candidates/')
        || (!returnBaseGridCleanPlate
          && payload.includes('/page-gates/clean-plate/candidates/'));
      decodedGrids.push({ payload, width: scaled ? 2400 : 1200, height: scaled ? 3600 : 1800 });
      return {
        width: scaled ? 2400 : 1200,
        height: scaled ? 3600 : 1800,
        close: vi.fn(),
      } as unknown as ImageBitmap;
    }));
    const checksum = 'aa'.repeat(32);
    const style = {
      fontToken: 'installed-font-aaaaaaaaaaaaaaaaaaaaaaaa', fontChecksum: checksum,
      fontSize: 48, minFontSize: 12,
      padding: 4, fill: '#111111', strokeColor: '#FFFFFF', strokeWidth: 2, rotation: 0,
      scaleX: 1, scaleY: 1, shearX: 0, shearY: 0, opacity: 1,
      visualCenterX: 0.5, visualCenterY: 0.5, align: 'center' as const,
      lineSpacing: 0.15, letterSpacing: 0, autoFit: true,
      fontSource: 'server-display-default' as const,
    };
    const route = { regionId: 'sfx-redraw', readingOrder: 1, route: 'art-lettering' as const,
      renderRequired: true, translationCandidateId: 'translation-1',
      translationCandidateChecksum: checksum };
    const candidate: TypesetGateContext['candidates'][number] = {
      candidateId: 'typeset-candidate-1', sequence: 22, jobId: 'job-typeset',
      jobItemId: 'item-typeset', parentChecksum: checksum, g9TerminalChecksum: checksum,
      translationStateChecksum: checksum, cleanPlateCandidateId: 'clean-1',
      cleanPlateChecksum: checksum, regionManifest: [{
        regionId: route.regionId, regionRevision: 4,
        geometry: { x: 10, y: 20, width: 200, height: 100, rotation: 0 },
        readingOrder: 1, regionType: 'sound_effect', direction: 'vertical',
        paragraphGroupId: null, contentDisposition: 'redraw-art',
        acceptedTranslationCandidateId: 'translation-1',
        acceptedTranslationCandidateChecksum: checksum,
      }], routeManifest: [route], routeChecksum: checksum,
      styleManifest: [{ regionId: route.regionId, route: route.route, style }],
      styleChecksum: checksum, layoutManifest: [{
        regionId: route.regionId, route: route.route,
        bounds: { x: 10, y: 20, width: 200, height: 100 }, fontSize: 48,
        overflow: false, direction: 'vertical', rotation: 0, scaleX: 1, scaleY: 1,
        shearX: 0, shearY: 0, opacity: 1, visualCenterX: 0.5, visualCenterY: 0.5,
        align: 'center',
      }], layoutChecksum: checksum, provider: 'pillow-g10',
      modelVersion: 'g10-typeset-v1', parameterHash: checksum, candidateChecksum: checksum,
      width: 2400, height: 3600, renderScale: 2, overflowRegionIds: [], anomalies: [],
      revisionId: 'revision-typeset-1', completed: true,
      artifactUrl: '/images/image-1/page-gates/typeset/candidates/typeset-candidate-1',
      review: null, createdAt: '2026-08-25T00:00:00Z',
    };
    const context: TypesetGateContext = {
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 22,
      g9TerminalChecksum: checksum, translationStateChecksum: checksum,
      cleanPlateCandidateId: 'clean-1', cleanPlateChecksum: checksum, state: 'pending',
      terminalChecksum: null, candidates: [candidate], reviews: [], routeManifest: [route],
      routeChecksum: checksum, styleDefaults: { bubble: style, ordinary: style,
        artLettering: style }, availableFonts: [{
        token: 'installed-font-aaaaaaaaaaaaaaaaaaaaaaaa', label: 'Display CJK',
        fontChecksum: checksum,
        capabilityChecksum: '8f2d75c0332a268c23ecec4b770aacc53437ea108382dc399645141653cde365',
        role: 'display' }],
      availableDisplayFonts: [{ token: 'installed-font-aaaaaaaaaaaaaaaaaaaaaaaa',
        label: 'Display CJK', fontChecksum: checksum,
        capabilityChecksum: '8f2d75c0332a268c23ecec4b770aacc53437ea108382dc399645141653cde365',
        role: 'display' }],
      artLetteringCapability: { available: true, contractVersion: 'g10-art-lettering-v1',
        features: ['explicit-installed-chinese-display-font', 'fill-stroke', 'rotation',
          'nonuniform-scale', 'shear-affine', 'opacity', 'visual-center',
          'alignment', 'line-spacing'], reason: null },
      retryRegionStyles: {},
    };
    const image = imageFixture('image-1', { revision: 11, width: 1200, height: 1800 });
    seedWorkbench({ images: [image], regions: [regionFixture('sfx-redraw', {
      order: 1, type: 'sound_effect', contentDisposition: 'redraw-art',
    })] });
    useWorkbenchStore.setState({
      g4Contexts: { 'image-1': { status: 'active', phase: 'G10', error: '', conflict: false,
        generation: {
          id: 'generation-1', runId: 'run-1', projectId: 'project-1', imageId: 'image-1',
          restartFromSource: true, parameterSetId: 'params-1', parameterSetHash: checksum,
          sourceProjectId: 'project-1', sourceImageId: 'image-1', sourceChecksum: checksum,
          state: 'active', nextSequence: 22,
          actor: { actorKind: 'codex', taskId: 'task-1', operationSource: 'api' },
          createdAt: '2026-08-25T00:00:00Z', closedAt: null,
        }, events: [] } },
      typesetContexts: { 'image-1': context },
      selectedTypesetCandidateIds: { 'image-1': candidate.candidateId },
    });

    const mounted = render(<CanvasWorkspace />);

    expect(await screen.findByText('不可变原图')).toBeInTheDocument();
    expect(screen.getByText('G10 父项 · accepted clean plate')).toBeInTheDocument();
    expect(screen.getByText('不可变最终候选')).toBeInTheDocument();
    await waitFor(() => expect(
      useWorkbenchStore.getState().typesetBitmapObservations['image-1'],
    ).toMatchObject({ candidateId: candidate.candidateId, sourceChecksum: checksum,
      cleanPlateChecksum: checksum, candidateChecksum: checksum, width: 2400, height: 3600,
      renderScale: 2, state: 'ready' }));
    expect(requested.some((url) => url.includes('/images/image-1/content'))).toBe(true);
    expect(requested.some((url) => url.includes('/page-gates/clean-plate/candidates/clean-1')))
      .toBe(true);
    expect(requested.some((url) => url.includes('/page-gates/typeset/candidates/typeset-candidate-1')))
      .toBe(true);
    expect(decodedGrids).toEqual(expect.arrayContaining([
      expect.objectContaining({ payload: expect.stringContaining('/images/image-1/content'),
        width: 1200, height: 1800 }),
      expect.objectContaining({ payload: expect.stringContaining('/page-gates/clean-plate/candidates/clean-1'),
        width: 2400, height: 3600 }),
      expect.objectContaining({ payload: expect.stringContaining('/page-gates/typeset/candidates/typeset-candidate-1'),
        width: 2400, height: 3600 }),
    ]));

    mounted.unmount();
    returnBaseGridCleanPlate = true;
    act(() => useWorkbenchStore.setState({
      typesetBitmapObservations: {},
      globalError: '',
    }));
    render(<CanvasWorkspace />);
    await waitFor(() => expect(
      useWorkbenchStore.getState().typesetBitmapObservations['image-1'],
    ).toBeUndefined());
    await waitFor(() => expect(useWorkbenchStore.getState().globalError)
      .toContain('G10 三视图'));
    expect(decodedGrids).toContainEqual(expect.objectContaining({
      payload: expect.stringContaining('/page-gates/clean-plate/candidates/clean-1'),
      width: 1200,
      height: 1800,
    }));
  });

  it('records add and erase strokes in canonical image coordinates with undo support', () => {
    seedWorkbench({
      regions: [regionFixture('region-1')],
      selectedRegionIds: ['region-1'],
    });
    const addStroke = buildMaskStroke(
      'add',
      13.4,
      [{ x: 110, y: 70 }, { x: 210, y: 170 }],
      { x: 10, y: 20, scale: 2 },
      { width: 1200, height: 1800 },
    );
    const eraseStroke = buildMaskStroke(
      'erase',
      6,
      [{ x: -20, y: 4000 }],
      { x: 10, y: 20, scale: 2 },
      { width: 1200, height: 1800 },
    );

    expect(addStroke).toEqual({
      mode: 'add',
      radius: 13,
      points: [[50, 25], [100, 75]],
    });
    expect(eraseStroke.points).toEqual([[0, 1800]]);

    const region = useWorkbenchStore.getState().regionsByImage['image-1']?.[0];
    useWorkbenchStore.getState().updateRegion('region-1', {
      repair: {
        ...region!.repair,
        maskEdits: { version: 1, strokes: [addStroke, eraseStroke] },
      },
    });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair.maskEdits)
      .toEqual({ version: 1, strokes: [addStroke, eraseStroke] });

    useWorkbenchStore.getState().undo();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair.maskEdits)
      .toBeUndefined();
  });

  it('renders a persisted single-point mask stroke as a visible circle', () => {
    const region = regionFixture('region-1');
    region.repair.maskEdits = {
      version: 1,
      strokes: [
        { mode: 'add', radius: 13, points: [[120, 140]] },
        { mode: 'erase', radius: 6, points: [[150, 160], [150, 180]] },
      ],
    };
    seedWorkbench({ regions: [region], selectedRegionIds: ['region-1'] });
    useWorkbenchStore.setState({ canvasTool: 'mask-brush' });

    const { container } = render(<CanvasWorkspace />);

    expect(container.querySelectorAll('[data-konva="Circle"]')).toHaveLength(1);
    expect(container.querySelectorAll('[data-konva="Line"]')).toHaveLength(1);
  });

  it('caps mask strokes at the backend resource contract before autosave', () => {
    const oversizedStroke = buildMaskStroke(
      'add',
      MAX_MASK_BRUSH_RADIUS + 100,
      Array.from({ length: MAX_MASK_POINTS_PER_STROKE + 10 }, (_, index) => ({
        x: index,
        y: index,
      })),
      { x: 0, y: 0, scale: 1 },
      { width: 20000, height: 20000 },
    );
    expect(oversizedStroke.radius).toBe(MAX_MASK_BRUSH_RADIUS);
    expect(oversizedStroke.points).toHaveLength(MAX_MASK_POINTS_PER_STROKE);

    const fullPointBudget = Array.from({ length: MAX_MASK_EDIT_POINTS / 4096 }, () => ({
      mode: 'add' as const,
      radius: 1,
      points: Array.from({ length: 4096 }, () => [0, 0] as [number, number]),
    }));
    expect(maskEditCapacity(fullPointBudget)).toEqual({
      canAddStroke: false,
      remainingPoints: 0,
    });
    expect(maskEditCapacity(Array.from({ length: MAX_MASK_EDIT_STROKES }, () => ({
      mode: 'erase' as const,
      radius: 1,
      points: [[0, 0] as [number, number]],
    })))).toMatchObject({ canAddStroke: false });
  });

  it('anchors rotated Konva regions at the same canonical center used by OpenCV', () => {
    const region = { x: 100, y: 120, width: 220, height: 120, rotation: 32.25 };

    expect(regionToCenteredNodeGeometry(region)).toEqual({
      x: 210,
      y: 180,
      offsetX: 110,
      offsetY: 60,
    });
    expect(centeredNodeToRegionGeometry({
      x: 250,
      y: 210,
      width: region.width,
      height: region.height,
      scaleX: 1.5,
      scaleY: 0.5,
      rotation: region.rotation,
    }, { width: 1200, height: 1800 })).toEqual({
      x: 85,
      y: 180,
      width: 330,
      height: 60,
      rotation: 32.3,
    });
    expect(centeredNodeToRegionGeometry({
      x: 210,
      y: 180,
      width: region.width,
      height: region.height,
      scaleX: 1,
      scaleY: 1,
      rotation: region.rotation,
    }, { width: 1200, height: 1800 })).toEqual({
      ...region,
      rotation: 32.3,
    });
  });

  it('enables mask tools for one selected region and controls the canonical brush radius', () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    render(<CanvasWorkspace />);

    fireEvent.click(screen.getByRole('button', { name: '蒙版画笔' }));
    fireEvent.change(screen.getByRole('slider', { name: '蒙版画笔半径' }), {
      target: { value: '24' },
    });

    expect(useWorkbenchStore.getState()).toMatchObject({
      canvasTool: 'mask-brush',
      maskBrushRadius: 24,
    });
    expect(screen.getByRole('button', { name: '蒙版橡皮擦' })).toBeEnabled();
  });

  it('reviews only the explicitly selected visual stage and hydrates the returned state', async () => {
    const image = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, preprocess: 'done' },
    });
    seedWorkbench({ images: [image] });
    useWorkbenchStore.setState({ canvasMode: 'preprocessed' });
    vi.stubGlobal('createImageBitmap', vi.fn(async () => ({
      width: 2400,
      height: 3600,
      close: vi.fn(),
    } as unknown as ImageBitmap)));
    const review = vi.spyOn(api, 'reviewImageStage').mockResolvedValue({
      ...image,
      revision: 8,
      stageReviews: {
        preprocess: {
          state: 'accepted',
          reviewedAt: '2026-08-13T10:00:00Z',
          resultRevision: 7,
          artifactChecksum: 'a'.repeat(64),
        },
      },
    });
    render(<CanvasWorkspace />);

    expect(screen.getByRole('button', { name: '接受' })).toBeDisabled();
    await waitFor(() => expect(screen.getByLabelText('当前视觉阶段复核')).toHaveTextContent('待复核'));
    expect(screen.getByRole('button', { name: '接受' })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: '接受' }));

    await waitFor(() => expect(review).toHaveBeenCalledWith(
      'image-1',
      'preprocess',
      'accepted',
      7,
      {
        imageId: 'image-1',
        stage: 'preprocess',
        revision: 7,
        artifactChecksum: 'aa'.repeat(32),
      },
    ));
    await waitFor(() => expect(screen.getByLabelText('当前视觉阶段复核')).toHaveTextContent('已接受'));
  });

  it('loads and visibly presents the current inpaint mask before review', async () => {
    const image = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
    });
    seedWorkbench({ images: [image] });
    useWorkbenchStore.setState({ canvasMode: 'erased', showMask: false });
    const requested: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requested.push(String(input));
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'image/png' }),
        arrayBuffer: async () => pngPayload(String(input)),
      } as unknown as Response;
    }));
    const review = vi.spyOn(api, 'reviewImageStage').mockResolvedValue({
      ...image,
      revision: 8,
      stageReviews: {
        inpaint: {
          state: 'accepted',
          reviewedAt: '2026-08-13T10:00:00Z',
          resultRevision: 7,
          artifactChecksum: 'aa'.repeat(32),
          maskChecksum: 'aa'.repeat(32),
        },
      },
    });

    render(<CanvasWorkspace />);

    await waitFor(() => {
      expect(requested.some((url) => url.includes('/generated/inpainted'))).toBe(true);
      expect(requested.some((url) => url.includes('/generated/mask'))).toBe(true);
    });
    await waitFor(() => expect(screen.getByLabelText('当前视觉阶段复核')).toHaveTextContent('请显示蒙版复核'));
    expect(screen.getByRole('button', { name: '接受' })).toBeDisabled();

    fireEvent.click(screen.getByRole('checkbox', { name: '复核蒙版' }));
    await waitFor(() => expect(screen.getByLabelText('当前视觉阶段复核')).toHaveTextContent('待复核'));
    fireEvent.click(screen.getByRole('button', { name: '接受' }));

    await waitFor(() => expect(review).toHaveBeenCalledWith(
      'image-1',
      'inpaint',
      'accepted',
      7,
      expect.objectContaining({
        artifactChecksum: 'aa'.repeat(32),
        maskChecksum: 'aa'.repeat(32),
      }),
    ));
  });

  it('accepts a high-resolution inpaint grid only when its mask matches', async () => {
    const image = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
    });
    seedWorkbench({ images: [image] });
    useWorkbenchStore.setState({ canvasMode: 'erased', showMask: true });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'image/png' }),
      arrayBuffer: async () => pngPayload(String(input)),
    }) as unknown as Response));
    vi.stubGlobal('createImageBitmap', vi.fn(async (blob: Blob) => {
      const source = await blob.text();
      return {
        width: source.includes('/generated/mask') ? 1200 : 2400,
        height: source.includes('/generated/mask') ? 1800 : 3600,
        close: vi.fn(),
      } as unknown as ImageBitmap;
    }));

    render(<CanvasWorkspace />);

    await waitFor(() => expect(
      screen.getByLabelText('当前视觉阶段复核'),
    ).toHaveTextContent('复核文件读取失败'));
    expect(screen.getByRole('button', { name: '接受' })).toBeDisabled();
  });

  it('shows the review mask when the operator opens the erased preview', async () => {
    const image = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
    });
    seedWorkbench({ images: [image] });
    useWorkbenchStore.setState({ canvasMode: 'original', showMask: false });
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      headers: new Headers({ 'content-type': 'image/png' }),
      arrayBuffer: async () => pngPayload('mask'),
    }) as unknown as Response));

    render(<CanvasWorkspace />);
    fireEvent.click(screen.getByRole('button', { name: '擦除' }));

    expect(useWorkbenchStore.getState().showMask).toBe(true);
    expect(screen.getByRole('checkbox', { name: '复核蒙版' })).toBeChecked();
    await waitFor(() => expect(screen.getByLabelText('当前视觉阶段复核')).toHaveTextContent('待复核'));
    expect(screen.getByRole('button', { name: '接受' })).toBeEnabled();
  });

  it('keeps a reusable mask bitmap alive while preview modes cycle', async () => {
    const image = imageFixture('image-1', {
      revision: 7,
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
    });
    seedWorkbench({ images: [image] });
    useWorkbenchStore.setState({ canvasMode: 'erased', showMask: true });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const source = String(input);
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'image/png' }),
        arrayBuffer: async () => pngPayload(source),
      } as unknown as Response;
    }));
    const decoded = new Map<string, Array<ImageBitmap & { close: ReturnType<typeof vi.fn> }>>();
    const pending: Array<{
      resolve: (bitmap: ImageBitmap) => void;
      source: string;
    }> = [];
    let deferGeneratedReload = false;
    function bitmapFor(source: string) {
      const bitmap = {
        width: 1200,
        height: 1800,
        close: vi.fn(),
      } as unknown as ImageBitmap & { close: ReturnType<typeof vi.fn> };
      decoded.set(source, [...(decoded.get(source) ?? []), bitmap]);
      return bitmap;
    }
    vi.stubGlobal('createImageBitmap', vi.fn(async (blob: Blob) => {
      const source = await blob.text();
      if (deferGeneratedReload && source.includes('/generated/')) {
        return await new Promise<ImageBitmap>((resolve) => pending.push({ resolve, source }));
      }
      return bitmapFor(source);
    }));

    const { container } = render(<CanvasWorkspace />);
    await waitFor(() => expect(container.querySelectorAll('[data-konva="Image"]')).toHaveLength(2));
    const maskSource = [...decoded.keys()].find((source) => source.includes('/generated/mask'));
    expect(maskSource).toBeDefined();
    const initialMask = maskSource ? decoded.get(maskSource)?.[0] : undefined;
    expect(initialMask).toBeDefined();
    if (!initialMask) throw new Error('Expected the initial mask bitmap to be decoded');

    fireEvent.click(screen.getByRole('button', { name: '原图' }));
    await waitFor(() => expect(screen.getByRole('application', { name: '原图画布' })).toBeVisible());
    await waitFor(() => expect(container.querySelectorAll('[data-konva="Image"]')).toHaveLength(1));
    expect(initialMask.close).not.toHaveBeenCalled();

    deferGeneratedReload = true;
    fireEvent.click(screen.getByRole('button', { name: '擦除' }));
    expect(initialMask.close).not.toHaveBeenCalled();
    await waitFor(() => expect(pending).toHaveLength(2));

    await act(async () => {
      for (const request of pending.splice(0)) request.resolve(bitmapFor(request.source));
    });
    await waitFor(() => expect(container.querySelectorAll('[data-konva="Image"]')).toHaveLength(2));
    await waitFor(() => expect(initialMask.close).toHaveBeenCalledOnce());
  });

  it('removes the mask overlay before presenting a typeset result for review', async () => {
    const image = imageFixture('image-1', {
      revision: 7,
      status: {
        ...imageFixture('image-1').status,
        inpaint: 'done',
        typeset: 'done',
      },
    });
    seedWorkbench({ images: [image] });
    useWorkbenchStore.setState({ canvasMode: 'erased', showMask: true });
    const requested: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requested.push(String(input));
      return {
        ok: true,
        status: 200,
        headers: new Headers({ 'content-type': 'image/png' }),
        arrayBuffer: async () => pngPayload(String(input)),
      } as unknown as Response;
    }));

    const { container } = render(<CanvasWorkspace />);

    await waitFor(() => expect(
      container.querySelectorAll('[data-konva="Image"]'),
    ).toHaveLength(2));
    const maskRequests = () => requested.filter((url) => url.includes('/generated/mask'));
    expect(maskRequests()).toHaveLength(1);

    fireEvent.click(screen.getByRole('button', { name: '成品' }));

    await waitFor(() => expect(
      requested.some((url) => url.includes('/generated/typeset')),
    ).toBe(true));
    await waitFor(() => expect(
      container.querySelectorAll('[data-konva="Image"]'),
    ).toHaveLength(1));
    expect(maskRequests()).toHaveLength(1);
    expect(useWorkbenchStore.getState().showMask).toBe(true);
  });

  it('keeps transformer handles and region boxes as edit targets', () => {
    const region = { name: () => 'region' };
    const transformer = { name: () => '', getClassName: () => 'Transformer' };
    const handle = {
      name: () => 'top-left',
      getClassName: () => 'Rect',
      getParent: () => transformer,
    };
    const image = { name: () => '', getClassName: () => 'Image', getParent: () => null };
    expect(isKonvaRegionEditTarget(region)).toBe(true);
    expect(isKonvaRegionEditTarget(handle)).toBe(true);
    expect(isKonvaRegionEditTarget(image)).toBe(false);
    expect(clampRegionGeometry({
      x: -20,
      y: 1900,
      width: 80,
      height: 40,
      rotation: 12.34,
    }, { width: 1200, height: 1800 })).toEqual({
      x: 0,
      y: 1760,
      width: 80,
      height: 40,
      rotation: 12.3,
    });
    expect(clusterRegionIds([
      { id: 'a', x: 10, y: 10, width: 40, height: 40, rotation: 0, direction: 'horizontal' },
      { id: 'b', x: 18, y: 16, width: 40, height: 40, rotation: 0, direction: 'horizontal' },
      { id: 'c', x: 400, y: 400, width: 30, height: 30, rotation: 0, direction: 'vertical' },
    ], { width: 1200, height: 1800 })).toEqual([['a', 'b'], ['c']]);
  });

  it('lets the original compare pane keep the same box editor', () => {
    const image = imageFixture('image-1', {
      status: {
        ...imageFixture('image-1').status,
        inpaint: 'done',
        typeset: 'done',
      },
    });
    seedWorkbench({
      images: [image],
      selectedRegionIds: ['region-1'],
    });
    useWorkbenchStore.setState({ canvasMode: 'typeset', compareMode: true });

    render(<CanvasWorkspace />);

    const surfaces = screen.getAllByTestId('canvas-surface');
    expect(surfaces).toHaveLength(2);
    expect(surfaces.every((surface) => surface.getAttribute('data-editable') === 'true')).toBe(true);
  });

  it('does not infer a review stage from original compare mode', () => {
    const image = imageFixture('image-1', {
      status: {
        ...imageFixture('image-1').status,
        inpaint: 'done',
        typeset: 'done',
      },
    });
    seedWorkbench({ images: [image] });
    useWorkbenchStore.setState({ canvasMode: 'original', compareMode: true });

    render(<CanvasWorkspace />);

    expect(screen.queryByLabelText('当前视觉阶段复核')).not.toBeInTheDocument();
  });
});
