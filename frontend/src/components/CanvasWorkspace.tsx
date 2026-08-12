import Konva from 'konva';
import type { KonvaEventObject } from 'konva/lib/Node';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Group, Image as KonvaImage, Label, Layer, Line, Rect, Stage, Tag, Text, Transformer } from 'react-konva';

import { api } from '../api/client';
import {
  activeImage,
  activeRegions,
  hasGeneratedPreview,
  useWorkbenchStore,
} from '../store/workbench';
import type { CanvasMode, ImageAsset, MaskEditStroke, Region } from '../types';
import {
  buildMaskStroke,
  canonicalPoint,
  centeredNodeToRegionGeometry,
  maskEditCapacity,
  regionToCenteredNodeGeometry,
} from './canvasGeometry';
import type { Point, Viewport } from './canvasGeometry';
import { loadCanonicalCanvasImage } from './canvasImage';
import { EmptyState, IconButton, LoadingState } from './Primitives';

function canvasModeAvailable(image: ImageAsset | null | undefined, mode: CanvasMode): boolean {
  if (mode === 'original') return Boolean(image);
  if (!image) return false;
  if (mode === 'preprocessed') return image.status.preprocess === 'done';
  if (mode === 'erased') return image.status.inpaint === 'done';
  return image.status.typeset === 'done';
}

function useElementSize() {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 800, height: 600 });

  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const update = () => {
      const rect = element.getBoundingClientRect();
      setSize({ width: Math.max(240, rect.width), height: Math.max(240, rect.height) });
    };
    update();
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(update);
      observer.observe(element);
      return () => observer.disconnect();
    }
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, []);
  return { ref, size };
}

function useCanvasImage(
  src: string | null,
  expectedSize: { width: number; height: number },
) {
  const expectedWidth = expectedSize.width;
  const expectedHeight = expectedSize.height;
  const [result, setResult] = useState<{
    src: string;
    image: ImageBitmap | null;
    state: 'loading' | 'ready' | 'error';
  }>({ src: '', image: null, state: 'loading' });

  useEffect(() => {
    if (!src) return;
    const controller = new AbortController();
    let disposed = false;
    let decoded: ImageBitmap | null = null;
    void loadCanonicalCanvasImage(src, {
      width: expectedWidth,
      height: expectedHeight,
    }, controller.signal)
      .then((image) => {
        decoded = image;
        if (disposed) image.close();
        else setResult({ src, image, state: 'ready' });
      })
      .catch(() => {
        if (!disposed) setResult({ src, image: null, state: 'error' });
      });
    return () => {
      disposed = true;
      controller.abort();
      decoded?.close();
    };
  }, [expectedHeight, expectedWidth, src]);

  if (!src) return { src: '', image: null, state: 'ready' as const };
  return result.src === src ? result : { src, image: null, state: 'loading' as const };
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}


function fitViewport(
  container: { width: number; height: number },
  imageWidth: number,
  imageHeight: number,
): Viewport {
  const padding = 48;
  const scale = clamp(
    Math.min((container.width - padding) / imageWidth, (container.height - padding) / imageHeight),
    0.02,
    8,
  );
  return {
    scale,
    x: (container.width - imageWidth * scale) / 2,
    y: (container.height - imageHeight * scale) / 2,
  };
}

function RegionShape({
  region,
  image,
  selected,
  editable,
  showOrder,
  showConfidence,
  viewportScale,
}: {
  region: Region;
  image: ImageAsset;
  selected: boolean;
  editable: boolean;
  showOrder: boolean;
  showConfidence: boolean;
  viewportScale: number;
}) {
  const shapeRef = useRef<Konva.Rect>(null);
  const transformerRef = useRef<Konva.Transformer>(null);
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const updateRegion = useWorkbenchStore((state) => state.updateRegion);

  useEffect(() => {
    if (selected && editable && shapeRef.current && transformerRef.current) {
      transformerRef.current.nodes([shapeRef.current]);
      transformerRef.current.getLayer()?.batchDraw();
    }
  }, [editable, selected]);

  const stroke = selected
    ? '#5f9dff'
    : region.ignored
      ? '#7b818a'
      : region.confirmed
        ? '#50c878'
        : '#f4b957';
  const confidence = region.confidence === null
    ? '—'
    : `${Math.round((region.confidence <= 1 ? region.confidence * 100 : region.confidence))}%`;
  const centeredGeometry = regionToCenteredNodeGeometry(region);

  function select(event: KonvaEventObject<MouseEvent | TouchEvent>) {
    if (!editable) return;
    event.cancelBubble = true;
    const mouseEvent = event.evt as MouseEvent;
    selectRegion(region.id, mouseEvent.shiftKey || mouseEvent.metaKey || mouseEvent.ctrlKey);
  }

  return (
    <>
      <Rect
        ref={shapeRef}
        name="region"
        x={centeredGeometry.x}
        y={centeredGeometry.y}
        offsetX={centeredGeometry.offsetX}
        offsetY={centeredGeometry.offsetY}
        width={region.width}
        height={region.height}
        rotation={region.rotation}
        fill={selected ? 'rgba(95, 157, 255, 0.14)' : 'rgba(244, 185, 87, 0.06)'}
        stroke={stroke}
        strokeWidth={Math.max(1, 1.5 / viewportScale)}
        dash={region.ignored ? [8 / viewportScale, 5 / viewportScale] : undefined}
        draggable={editable}
        onClick={select}
        onTap={select}
        onDragEnd={(event) => {
          const geometry = centeredNodeToRegionGeometry({
            x: event.target.x(),
            y: event.target.y(),
            width: region.width,
            height: region.height,
            scaleX: 1,
            scaleY: 1,
            rotation: event.target.rotation(),
          }, image);
          updateRegion(region.id, {
            x: geometry.x,
            y: geometry.y,
          });
        }}
        onTransformEnd={() => {
          const node = shapeRef.current;
          if (!node) return;
          const geometry = centeredNodeToRegionGeometry({
            x: node.x(),
            y: node.y(),
            width: region.width,
            height: region.height,
            scaleX: node.scaleX(),
            scaleY: node.scaleY(),
            rotation: node.rotation(),
          }, image);
          node.scaleX(1);
          node.scaleY(1);
          updateRegion(region.id, geometry);
        }}
      />
      {(showOrder || showConfidence) ? (
        <Label
          x={region.x}
          y={region.y}
          listening={false}
          scaleX={1 / viewportScale}
          scaleY={1 / viewportScale}
        >
          <Tag fill={stroke} cornerRadius={4} />
          <Text
            fill="#0b0e12"
            fontFamily="system-ui"
            fontSize={11}
            fontStyle="bold"
            padding={4}
            text={`${showOrder ? `#${region.order}` : ''}${showOrder && showConfidence ? ' · ' : ''}${showConfidence ? confidence : ''}`}
          />
        </Label>
      ) : null}
      {selected && editable ? (
        <Transformer
          ref={transformerRef}
          rotateEnabled
          flipEnabled={false}
          anchorCornerRadius={2}
          anchorFill="#dce9ff"
          anchorStroke="#286fdd"
          borderStroke="#5f9dff"
          borderStrokeWidth={1 / viewportScale}
          rotateAnchorOffset={24 / viewportScale}
          anchorSize={8 / viewportScale}
          boundBoxFunc={(oldBox, newBox) =>
            Math.abs(newBox.width) < 6 || Math.abs(newBox.height) < 6 ? oldBox : newBox
          }
        />
      ) : null}
    </>
  );
}

function CanvasViewport({
  imageAsset,
  mode,
  editable,
  zoomSignal,
}: {
  imageAsset: ImageAsset;
  mode: CanvasMode;
  editable: boolean;
  zoomSignal: { direction: -1 | 0 | 1; nonce: number };
}) {
  const { ref: containerRef, size } = useElementSize();
  const source = api.contentUrl(imageAsset.id, mode, imageAsset.revision);
  const canonicalSize = { width: imageAsset.width, height: imageAsset.height };
  const { image, state: imageLoadState } = useCanvasImage(source, canonicalSize);
  const showMask = useWorkbenchStore((state) => state.showMask);
  const maskSource = showMask && imageAsset.status.inpaint === 'done'
    ? api.maskUrl(imageAsset.id, imageAsset.revision)
    : null;
  const { image: maskImage } = useCanvasImage(maskSource, canonicalSize);
  const regions = useWorkbenchStore(activeRegions);
  const selectedRegionIds = useWorkbenchStore((state) => state.selectedRegionIds);
  const tool = useWorkbenchStore((state) => state.canvasTool);
  const spacePressed = useWorkbenchStore((state) => state.spacePressed);
  const showRegions = useWorkbenchStore((state) => state.showRegions);
  const showOrder = useWorkbenchStore((state) => state.showOrder);
  const showConfidence = useWorkbenchStore((state) => state.showConfidence);
  const maskBrushRadius = useWorkbenchStore((state) => state.maskBrushRadius);
  const fitRequest = useWorkbenchStore((state) => state.fitRequest);
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const clearRegionSelection = useWorkbenchStore((state) => state.clearRegionSelection);
  const createRegion = useWorkbenchStore((state) => state.createRegion);
  const updateRegion = useWorkbenchStore((state) => state.updateRegion);
  const [viewport, setViewport] = useState<Viewport>(() =>
    fitViewport(size, imageAsset.width, imageAsset.height),
  );
  const [draft, setDraft] = useState<{ start: Point; end: Point } | null>(null);
  const [maskDraft, setMaskDraft] = useState<(
    MaskEditStroke & { regionId: string; maxPoints: number }
  ) | null>(null);
  const panStart = useRef<{ pointer: Point; viewport: Viewport } | null>(null);
  const selectedRegion = selectedRegionIds.length === 1
    ? regions.find((region) => region.id === selectedRegionIds[0])
    : undefined;
  const maskEditing = tool === 'mask-brush' || tool === 'mask-eraser';

  useEffect(() => {
    // Canvas viewport follows measured container geometry and an explicit fit command.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setViewport(fitViewport(size, imageAsset.width, imageAsset.height));
  }, [fitRequest, imageAsset.height, imageAsset.width, size]);

  useEffect(() => {
    if (!zoomSignal.nonce || zoomSignal.direction === 0) return;
    // Apply the explicit toolbar zoom command to local viewport state.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setViewport((current) => {
      const scale = clamp(current.scale * (zoomSignal.direction > 0 ? 1.2 : 1 / 1.2), 0.02, 8);
      const center = { x: size.width / 2, y: size.height / 2 };
      const anchor = canonicalPoint(center, current);
      return { scale, x: center.x - anchor.x * scale, y: center.y - anchor.y * scale };
    });
  }, [size.height, size.width, zoomSignal]);

  function pointerFromEvent(event: KonvaEventObject<MouseEvent | TouchEvent | WheelEvent>): Point | null {
    return event.target.getStage()?.getPointerPosition() ?? null;
  }

  function handlePointerDown(event: KonvaEventObject<MouseEvent | TouchEvent>) {
    const pointer = pointerFromEvent(event);
    if (!pointer) return;
    const native = event.evt as MouseEvent;
    if (tool === 'hand' || spacePressed || native.button === 1) {
      panStart.current = { pointer, viewport };
      return;
    }
    if (!editable) return;
    if (maskEditing) {
      if (!selectedRegion) return;
      const strokes = selectedRegion.repair.maskEdits?.strokes ?? [];
      const capacity = maskEditCapacity(strokes);
      if (!capacity.canAddStroke) {
        useWorkbenchStore.setState({
          globalError: '当前文本框的蒙版笔迹已达上限（256 笔 / 16384 个采样点），请先撤销或清除部分笔迹。',
        });
        return;
      }
      event.cancelBubble = true;
      setMaskDraft({
        regionId: selectedRegion.id,
        maxPoints: capacity.remainingPoints,
        ...buildMaskStroke(
          tool === 'mask-brush' ? 'add' : 'erase',
          maskBrushRadius,
          [pointer],
          viewport,
          imageAsset,
        ),
      });
      return;
    }
    if (tool === 'region') {
      const point = canonicalPoint(pointer, viewport);
      if (point.x < 0 || point.y < 0 || point.x > imageAsset.width || point.y > imageAsset.height) return;
      clearRegionSelection();
      setDraft({ start: point, end: point });
      return;
    }
    if (event.target.name() !== 'region') clearRegionSelection();
  }

  function handlePointerMove(event: KonvaEventObject<MouseEvent | TouchEvent>) {
    const pointer = pointerFromEvent(event);
    if (!pointer) return;
    if (panStart.current) {
      const start = panStart.current;
      setViewport({
        ...start.viewport,
        x: start.viewport.x + pointer.x - start.pointer.x,
        y: start.viewport.y + pointer.y - start.pointer.y,
      });
      return;
    }
    if (maskDraft) {
      if (maskDraft.points.length >= maskDraft.maxPoints) {
        useWorkbenchStore.setState({
          globalError: '当前蒙版笔迹已达采样点上限，多余轨迹未记录。',
        });
        return;
      }
      const nextPoint = buildMaskStroke(
        maskDraft.mode,
        maskDraft.radius,
        [pointer],
        viewport,
        imageAsset,
      ).points[0];
      if (!nextPoint) return;
      setMaskDraft((current) => {
        if (!current) return null;
        const previous = current.points.at(-1);
        if (previous && Math.hypot(nextPoint[0] - previous[0], nextPoint[1] - previous[1]) < 0.5) {
          return current;
        }
        return { ...current, points: [...current.points, nextPoint] };
      });
      return;
    }
    if (draft) {
      const point = canonicalPoint(pointer, viewport);
      setDraft({
        ...draft,
        end: {
          x: clamp(point.x, 0, imageAsset.width),
          y: clamp(point.y, 0, imageAsset.height),
        },
      });
    }
  }

  function handlePointerUp() {
    panStart.current = null;
    if (maskDraft) {
      const completed = maskDraft;
      setMaskDraft(null);
      const latest = useWorkbenchStore.getState();
      const region = latest.activeImageId
        ? (latest.regionsByImage[latest.activeImageId] ?? []).find(
            (entry) => entry.id === completed.regionId,
          )
        : undefined;
      if (region && completed.points.length) {
        updateRegion(region.id, {
          repair: {
            ...region.repair,
            maskEdits: {
              version: 1,
              strokes: [...(region.repair.maskEdits?.strokes ?? []), {
                mode: completed.mode,
                radius: completed.radius,
                points: completed.points,
              }],
            },
          },
        });
      }
      return;
    }
    if (!draft) return;
    const x = Math.min(draft.start.x, draft.end.x);
    const y = Math.min(draft.start.y, draft.end.y);
    const width = Math.abs(draft.end.x - draft.start.x);
    const height = Math.abs(draft.end.y - draft.start.y);
    setDraft(null);
    if (width >= 6 && height >= 6) {
      const regionId = createRegion({ x, y, width, height });
      if (regionId) selectRegion(regionId);
    }
  }

  function handleWheel(event: KonvaEventObject<WheelEvent>) {
    event.evt.preventDefault();
    if (!event.evt.metaKey && !event.evt.ctrlKey) {
      setViewport((current) => ({
        ...current,
        x: current.x - event.evt.deltaX,
        y: current.y - event.evt.deltaY,
      }));
      return;
    }
    const pointer = pointerFromEvent(event);
    if (!pointer) return;
    const anchor = canonicalPoint(pointer, viewport);
    const direction = event.evt.deltaY > 0 ? -1 : 1;
    const scale = clamp(viewport.scale * (direction > 0 ? 1.1 : 1 / 1.1), 0.02, 8);
    setViewport({ scale, x: pointer.x - anchor.x * scale, y: pointer.y - anchor.y * scale });
  }

  const draftRect = draft
    ? {
        x: Math.min(draft.start.x, draft.end.x),
        y: Math.min(draft.start.y, draft.end.y),
        width: Math.abs(draft.end.x - draft.start.x),
        height: Math.abs(draft.end.y - draft.start.y),
      }
    : null;

  return (
    <div
      aria-label={`${mode === 'original' ? '原图' : mode === 'preprocessed' ? '增强' : mode === 'erased' ? '擦除' : '成品'}画布`}
      className={`canvas-viewport canvas-viewport--${spacePressed ? 'hand' : tool}`}
      data-testid="canvas-surface"
      ref={containerRef}
      role="application"
    >
      <Stage
        height={size.height}
        width={size.width}
        onDblClick={(event) => {
          if (!editable || tool !== 'select') return;
          const pointer = pointerFromEvent(event);
          if (!pointer) return;
          const point = canonicalPoint(pointer, viewport);
          createRegion({
            x: clamp(point.x - 80, 0, Math.max(0, imageAsset.width - 160)),
            y: clamp(point.y - 45, 0, Math.max(0, imageAsset.height - 90)),
            width: Math.min(160, imageAsset.width),
            height: Math.min(90, imageAsset.height),
          });
        }}
        onMouseDown={handlePointerDown}
        onMouseMove={handlePointerMove}
        onMouseUp={handlePointerUp}
        onMouseLeave={handlePointerUp}
        onTouchStart={handlePointerDown}
        onTouchMove={handlePointerMove}
        onTouchEnd={handlePointerUp}
        onWheel={handleWheel}
      >
        <Layer>
          <Group x={viewport.x} y={viewport.y} scaleX={viewport.scale} scaleY={viewport.scale}>
            <Rect
              fill="#f5f5f2"
              height={imageAsset.height}
              listening={false}
              shadowBlur={28 / viewport.scale}
              shadowColor="rgba(0,0,0,.48)"
              shadowOpacity={0.9}
              width={imageAsset.width}
            />
            {image ? (
              <KonvaImage
                height={imageAsset.height}
                image={image}
                listening={tool !== 'hand'}
                width={imageAsset.width}
              />
            ) : null}
            {showMask && maskImage && imageAsset.status.inpaint === 'done' ? (
              <KonvaImage
                globalCompositeOperation="difference"
                height={imageAsset.height}
                image={maskImage}
                listening={false}
                opacity={0.72}
                width={imageAsset.width}
              />
            ) : null}
            {showRegions ? regions.map((region) => (
              <RegionShape
                editable={editable && tool === 'select' && !spacePressed}
                image={imageAsset}
                key={region.id}
                region={region}
                selected={selectedRegionIds.includes(region.id)}
                showConfidence={showConfidence}
                showOrder={showOrder}
                viewportScale={viewport.scale}
              />
            )) : null}
            {maskEditing && selectedRegion ? [
              ...(selectedRegion.repair.maskEdits?.strokes ?? []),
              ...(maskDraft?.regionId === selectedRegion.id ? [maskDraft] : []),
            ].map((stroke, index) => (
              <Line
                key={`${stroke.mode}-${index}`}
                points={stroke.points.flat()}
                stroke={stroke.mode === 'add' ? '#4ad7c8' : '#ff6b6b'}
                strokeWidth={Math.max(1, stroke.radius * 2)}
                lineCap="round"
                lineJoin="round"
                listening={false}
                opacity={0.72}
              />
            )) : null}
            {draftRect ? (
              <Rect
                {...draftRect}
                dash={[8 / viewport.scale, 5 / viewport.scale]}
                fill="rgba(95,157,255,.12)"
                listening={false}
                stroke="#5f9dff"
                strokeWidth={1.5 / viewport.scale}
              />
            ) : null}
          </Group>
        </Layer>
      </Stage>
      <span className="canvas-zoom">{Math.round(viewport.scale * 100)}%</span>
      {imageLoadState === 'loading' ? <div className="canvas-overlay"><LoadingState label="正在读取图像…" /></div> : null}
      {imageLoadState === 'error' ? (
        <div className="canvas-overlay canvas-overlay--error" role="alert">
          <strong>图像读取失败</strong>
          <span>检查本地服务和项目源文件是否仍可访问。</span>
        </div>
      ) : null}
    </div>
  );
}

function CanvasToolbar({
  onZoom,
}: {
  onZoom: (direction: -1 | 1) => void;
}) {
  const image = useWorkbenchStore(activeImage);
  const mode = useWorkbenchStore((state) => state.canvasMode);
  const tool = useWorkbenchStore((state) => state.canvasTool);
  const compareMode = useWorkbenchStore((state) => state.compareMode);
  const showRegions = useWorkbenchStore((state) => state.showRegions);
  const showOrder = useWorkbenchStore((state) => state.showOrder);
  const showConfidence = useWorkbenchStore((state) => state.showConfidence);
  const maskBrushRadius = useWorkbenchStore((state) => state.maskBrushRadius);
  const selectedRegionIds = useWorkbenchStore((state) => state.selectedRegionIds);
  const setCanvasMode = useWorkbenchStore((state) => state.setCanvasMode);
  const setCanvasTool = useWorkbenchStore((state) => state.setCanvasTool);
  const toggleCompareMode = useWorkbenchStore((state) => state.toggleCompareMode);
  const setShowRegions = useWorkbenchStore((state) => state.setShowRegions);
  const setShowOrder = useWorkbenchStore((state) => state.setShowOrder);
  const setShowConfidence = useWorkbenchStore((state) => state.setShowConfidence);
  const setMaskBrushRadius = useWorkbenchStore((state) => state.setMaskBrushRadius);
  const requestFit = useWorkbenchStore((state) => state.requestFit);
  const createRegion = useWorkbenchStore((state) => state.createRegion);
  const compareAvailable = hasGeneratedPreview(image);
  const maskToolAvailable = Boolean(image && selectedRegionIds.length === 1);
  const maskToolActive = tool === 'mask-brush' || tool === 'mask-eraser';

  function quickCreate() {
    if (!image) return;
    const width = Math.max(80, Math.round(image.width * 0.22));
    const height = Math.max(50, Math.round(image.height * 0.1));
    createRegion({
      x: Math.round((image.width - width) / 2),
      y: Math.round((image.height - height) / 2),
      width,
      height,
    });
    setCanvasTool('select');
  }

  return (
    <div className="canvas-toolbar" aria-label="画布工具栏">
      <div className="segmented" aria-label="预览模式">
        {([
          ['original', '原图'],
          ['preprocessed', '增强'],
          ['erased', '擦除'],
          ['typeset', '成品'],
        ] as const).map(([value, label]) => (
          <button aria-pressed={mode === value} disabled={!canvasModeAvailable(image, value)} key={value} onClick={() => setCanvasMode(value)} title={!canvasModeAvailable(image, value) && value !== 'original' ? '尚未生成，请先运行对应步骤' : undefined} type="button">{label}</button>
        ))}
      </div>
      <span className="toolbar-divider" />
      <div className="tool-buttons" aria-label="编辑工具">
        <IconButton aria-label="选择工具" className={tool === 'select' ? 'is-active' : ''} onClick={() => setCanvasTool('select')} title="选择 V">↖</IconButton>
        <IconButton aria-label="绘制文本框" className={tool === 'region' ? 'is-active' : ''} onClick={() => setCanvasTool('region')} title="绘制文本框 N">▢</IconButton>
        <IconButton aria-label="平移工具" className={tool === 'hand' ? 'is-active' : ''} onClick={() => setCanvasTool('hand')} title="平移 H">✋</IconButton>
        <IconButton aria-label="在中央快速新建文本框" disabled={!image} onClick={quickCreate} title="快速新建文本框">＋框</IconButton>
        <IconButton aria-label="蒙版画笔" className={tool === 'mask-brush' ? 'is-active' : ''} disabled={!maskToolAvailable} onClick={() => setCanvasTool('mask-brush')} title="向选中区域蒙版添加笔迹 M">画</IconButton>
        <IconButton aria-label="蒙版橡皮擦" className={tool === 'mask-eraser' ? 'is-active' : ''} disabled={!maskToolAvailable} onClick={() => setCanvasTool('mask-eraser')} title="从选中区域蒙版擦除笔迹 E">擦</IconButton>
      </div>
      {maskToolActive ? (
        <label className="brush-radius">
          <span>半径 {maskBrushRadius}px</span>
          <input
            aria-label="蒙版画笔半径"
            max={100}
            min={1}
            onChange={(event) => setMaskBrushRadius(Number(event.target.value))}
            type="range"
            value={maskBrushRadius}
          />
        </label>
      ) : null}
      <span className="toolbar-divider" />
      <div className="tool-buttons" aria-label="缩放">
        <IconButton aria-label="缩小" onClick={() => onZoom(-1)}>−</IconButton>
        <IconButton aria-label="适合窗口" onClick={requestFit}>适窗</IconButton>
        <IconButton aria-label="放大" onClick={() => onZoom(1)}>＋</IconButton>
      </div>
      <span className="toolbar-spacer" />
      <label className="toolbar-check"><input checked={showRegions} onChange={(event) => setShowRegions(event.target.checked)} type="checkbox" />框</label>
      <label className="toolbar-check"><input checked={showOrder} onChange={(event) => setShowOrder(event.target.checked)} type="checkbox" />编号</label>
      <label className="toolbar-check"><input checked={showConfidence} onChange={(event) => setShowConfidence(event.target.checked)} type="checkbox" />置信度</label>
      <button
        aria-pressed={compareMode && compareAvailable}
        className={`button button--compact ${compareMode && compareAvailable ? 'is-active' : ''}`}
        disabled={!compareAvailable}
        onClick={toggleCompareMode}
        title={compareAvailable ? undefined : '尚无增强、擦除或成品可供对比'}
        type="button"
      >
        对比
      </button>
    </div>
  );
}

export function CanvasWorkspace() {
  const image = useWorkbenchStore(activeImage);
  const compareMode = useWorkbenchStore((state) => state.compareMode);
  const requestedMode = useWorkbenchStore((state) => state.canvasMode);
  const setCanvasMode = useWorkbenchStore((state) => state.setCanvasMode);
  const toggleCompareMode = useWorkbenchStore((state) => state.toggleCompareMode);
  const regionsLoading = useWorkbenchStore((state) =>
    state.activeImageId ? state.regionsLoading[state.activeImageId] : false,
  );
  const [zoomSignal, setZoomSignal] = useState({ direction: 0 as -1 | 0 | 1, nonce: 0 });

  const mode: CanvasMode = canvasModeAvailable(image, requestedMode) ? requestedMode : 'original';
  const resultMode = useMemo<CanvasMode>(() => {
    if (mode !== 'original') return mode;
    if (canvasModeAvailable(image, 'typeset')) return 'typeset';
    if (canvasModeAvailable(image, 'erased')) return 'erased';
    if (canvasModeAvailable(image, 'preprocessed')) return 'preprocessed';
    return 'original';
  }, [image, mode]);
  const compareAvailable = hasGeneratedPreview(image);
  const showCompare = compareMode && compareAvailable;

  useEffect(() => {
    if (requestedMode !== mode) setCanvasMode(mode);
  }, [mode, requestedMode, setCanvasMode]);

  useEffect(() => {
    if (compareMode && !compareAvailable) toggleCompareMode();
  }, [compareAvailable, compareMode, toggleCompareMode]);

  return (
    <main className="canvas-panel panel">
      <CanvasToolbar
        onZoom={(direction) => setZoomSignal((signal) => ({ direction, nonce: signal.nonce + 1 }))}
      />
      <div className={`canvas-area ${showCompare ? 'canvas-area--compare' : ''}`}>
        {!image ? (
          <EmptyState
            icon="▧"
            title="选择一张图像开始"
            description="画布坐标始终使用原图像素，缩放和平移不会改变区域数据。"
          />
        ) : showCompare ? (
          <>
            <section className="compare-pane">
              <span className="compare-pane__label">原图</span>
              <CanvasViewport editable={false} imageAsset={image} mode="original" zoomSignal={zoomSignal} />
            </section>
            <section className="compare-pane">
              <span className="compare-pane__label">{resultMode === 'preprocessed' ? '增强结果' : resultMode === 'erased' ? '擦除结果' : resultMode === 'typeset' ? '嵌字成品' : '原图'}</span>
              <CanvasViewport editable imageAsset={image} mode={resultMode} zoomSignal={zoomSignal} />
            </section>
          </>
        ) : (
          <CanvasViewport editable imageAsset={image} mode={mode} zoomSignal={zoomSignal} />
        )}
        {regionsLoading ? <div className="canvas-regions-loading"><LoadingState label="读取文本框…" /></div> : null}
      </div>
    </main>
  );
}
