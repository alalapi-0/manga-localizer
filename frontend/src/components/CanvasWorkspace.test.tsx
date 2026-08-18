import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { resetWorkbenchStore, useWorkbenchStore } from '../store/workbench';
import { imageFixture, regionFixture, seedWorkbench } from '../test/fixtures';
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
        arrayBuffer: async () => new Uint8Array([requested.length]).buffer,
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
      arrayBuffer: async () => new Uint8Array([1]).buffer,
    }) as unknown as Response));

    render(<CanvasWorkspace />);
    fireEvent.click(screen.getByRole('button', { name: '擦除' }));

    expect(useWorkbenchStore.getState().showMask).toBe(true);
    expect(screen.getByRole('checkbox', { name: '复核蒙版' })).toBeChecked();
    await waitFor(() => expect(screen.getByLabelText('当前视觉阶段复核')).toHaveTextContent('待复核'));
    expect(screen.getByRole('button', { name: '接受' })).toBeEnabled();
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
        arrayBuffer: async () => new Uint8Array([requested.length]).buffer,
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
