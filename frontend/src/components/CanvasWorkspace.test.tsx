import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { resetWorkbenchStore, useWorkbenchStore } from '../store/workbench';
import { imageFixture, regionFixture, seedWorkbench } from '../test/fixtures';
import {
  buildMaskStroke,
  centeredNodeToRegionGeometry,
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
  afterEach(() => {
    resetWorkbenchStore();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('decodes the raw pixel grid without EXIF orientation and retries by image revision', async () => {
    const requested: string[] = [];
    const decode = vi.fn(async (blob: Blob, options?: ImageBitmapOptions) => {
      if (options?.imageOrientation !== 'none') throw new Error('EXIF orientation was not disabled');
      const source = (blob as Blob & { source: string }).source;
      if (source.includes('v=4')) throw new Error('preview missing');
      return {
        width: 1200,
        height: 1800,
        close: vi.fn(),
      } as unknown as ImageBitmap;
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const source = String(input);
      requested.push(source);
      return {
        ok: true,
        status: 200,
        blob: async () => Object.assign(new Blob(), { source }),
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

  it('rejects a decoded image whose dimensions do not match the canonical backend grid', async () => {
    const close = vi.fn();
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      blob: async () => new Blob(),
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
});
