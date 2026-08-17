import Konva from 'konva';
import type { KonvaEventObject } from 'konva/lib/Node';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Group, Image as KonvaImage, Label, Layer, Line, Rect, Stage, Tag, Text, Transformer } from 'react-konva';

import { api } from '../api/client';
import {
  activeImage,
  activeRegions,
  hasGeneratedPreview,
  regionHasTypesetOverflow,
  useWorkbenchStore,
} from '../store/workbench';
import type {
  CanvasMode,
  ImageAsset,
  MaskEditStroke,
  Region,
  StageReviewObservation,
  VisualStage,
} from '../types';
import {
  buildMaskStroke,
  canonicalPoint,
  centeredNodeToRegionGeometry,
  frameRegions,
  isKonvaRegionEditTarget,
  maskEditCapacity,
  regionToCenteredNodeGeometry,
} from './canvasGeometry';
import type { Point, Viewport } from './canvasGeometry';
import { loadCanonicalCanvasImage } from './canvasImage';
import { CreateLocalProjectButton, EmptyState, IconButton, ImportPhotosButton, LoadingState } from './Primitives';

function canvasModeAvailable(image: ImageAsset | null | undefined, mode: CanvasMode): boolean {
  if (mode === 'original') return Boolean(image);
  if (!image) return false;
  if (mode === 'preprocessed') return image.status.preprocess === 'done';
  if (mode === 'erased') return image.status.inpaint === 'done';
  return image.status.typeset === 'done';
}

function visualStageForMode(mode: CanvasMode): VisualStage | null {
  if (mode === 'preprocessed') return 'preprocess';
  if (mode === 'erased') return 'inpaint';
  if (mode === 'typeset') return 'typeset';
  return null;
}

function regionStatusStroke(
  region: Pick<Region, 'confirmed' | 'ignored' | 'trustDisposition'>,
): string {
  if (region.ignored || region.trustDisposition === 'ignored') return '#7b818a';
  return region.confirmed && region.trustDisposition === 'trusted' ? '#50c878' : '#f4b957';
}

type CanvasReviewObservation = Pick<StageReviewObservation, 'imageId' | 'stage' | 'revision'> & (
  | { state: 'loading' | 'error'; artifactChecksum?: undefined; maskChecksum?: undefined }
  | { state: 'ready'; artifactChecksum: string; maskChecksum?: string }
);

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
  allowCanonicalScale = false,
) {
  const expectedWidth = expectedSize.width;
  const expectedHeight = expectedSize.height;
  const [result, setResult] = useState<{
    src: string;
    image: ImageBitmap | null;
    checksum: string | null;
    state: 'loading' | 'ready' | 'error';
  }>({ src: '', image: null, checksum: null, state: 'loading' });

  useEffect(() => {
    if (!src) return;
    const controller = new AbortController();
    let disposed = false;
    let decoded: ImageBitmap | null = null;
    void loadCanonicalCanvasImage(src, {
      width: expectedWidth,
      height: expectedHeight,
    }, controller.signal, allowCanonicalScale)
      .then((loaded) => {
        decoded = loaded.image;
        if (disposed) loaded.image.close();
        else setResult({ src, image: loaded.image, checksum: loaded.checksum, state: 'ready' });
      })
      .catch(() => {
        if (!disposed) setResult({ src, image: null, checksum: null, state: 'error' });
      });
    return () => {
      disposed = true;
      controller.abort();
      decoded?.close();
    };
  }, [allowCanonicalScale, expectedHeight, expectedWidth, src]);

  if (!src) return { src: '', image: null, checksum: null, state: 'ready' as const };
  return result.src === src
    ? result
    : { src, image: null, checksum: null, state: 'loading' as const };
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
  }, [editable, region.height, region.rotation, region.width, region.x, region.y, selected]);

  const overflowing = regionHasTypesetOverflow(image, region.id);
  const stroke = selected
    ? '#5f9dff'
    : overflowing
      ? '#ff6b6b'
      : regionStatusStroke(region);
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
        dash={region.ignored ? [8 / viewportScale, 5 / viewportScale] : overflowing ? [6 / viewportScale, 4 / viewportScale] : undefined}
        draggable={editable}
        dragBoundFunc={(pos) => ({
          x: clamp(pos.x, region.width / 2, Math.max(region.width / 2, image.width - region.width / 2)),
          y: clamp(pos.y, region.height / 2, Math.max(region.height / 2, image.height - region.height / 2)),
        })}
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
          updateRegion(region.id, geometry);
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
      {(showOrder || showConfidence || overflowing) ? (
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
            text={`${showOrder ? `#${region.order}` : ''}${overflowing ? ' 溢出' : ''}${showOrder && showConfidence ? ' · ' : ''}${showConfidence ? confidence : ''}`}
          />
        </Label>
      ) : null}
      {selected && editable ? (
        <Transformer
          ref={transformerRef}
          rotateEnabled
          keepRatio={false}
          centeredScaling={false}
          flipEnabled={false}
          ignoreStroke
          enabledAnchors={[
            'top-left',
            'top-center',
            'top-right',
            'middle-right',
            'middle-left',
            'bottom-left',
            'bottom-center',
            'bottom-right',
          ]}
          anchorCornerRadius={2}
          anchorFill="#dce9ff"
          anchorStroke="#286fdd"
          borderStroke="#5f9dff"
          borderStrokeWidth={1 / viewportScale}
          rotateAnchorOffset={24 / viewportScale}
          anchorSize={Math.max(10, 12 / viewportScale)}
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
  observationStage,
  onReviewObservation,
}: {
  imageAsset: ImageAsset;
  mode: CanvasMode;
  editable: boolean;
  zoomSignal: { direction: -1 | 0 | 1; nonce: number };
  observationStage?: VisualStage | null;
  onReviewObservation?: (observation: CanvasReviewObservation) => void;
}) {
  const { ref: containerRef, size } = useElementSize();
  const source = api.contentUrl(imageAsset.id, mode, imageAsset.revision);
  const canonicalSize = { width: imageAsset.width, height: imageAsset.height };
  const {
    image,
    checksum: artifactChecksum,
    state: imageLoadState,
  } = useCanvasImage(source, canonicalSize, mode === 'preprocessed');
  const showMask = useWorkbenchStore((state) => state.showMask);
  const maskSource = mode === 'erased'
    && (showMask || observationStage === 'inpaint')
    && imageAsset.status.inpaint === 'done'
    ? api.maskUrl(imageAsset.id, imageAsset.revision)
    : null;
  const {
    image: maskImage,
    checksum: maskChecksum,
    state: maskLoadState,
  } = useCanvasImage(maskSource, canonicalSize);
  const regions = useWorkbenchStore(activeRegions);
  const selectedRegionIds = useWorkbenchStore((state) => state.selectedRegionIds);
  const tool = useWorkbenchStore((state) => state.canvasTool);
  const spacePressed = useWorkbenchStore((state) => state.spacePressed);
  const showRegions = useWorkbenchStore((state) => state.showRegions);
  const showOrder = useWorkbenchStore((state) => state.showOrder);
  const showConfidence = useWorkbenchStore((state) => state.showConfidence);
  const maskBrushRadius = useWorkbenchStore((state) => state.maskBrushRadius);
  const fitRequest = useWorkbenchStore((state) => state.fitRequest);
  const focusRequest = useWorkbenchStore((state) => state.focusRequest);
  const focusRegionIds = useWorkbenchStore((state) => state.focusRegionIds);
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
    if (!observationStage || !onReviewObservation) return;
    const identity = {
      imageId: imageAsset.id,
      stage: observationStage,
      revision: imageAsset.revision,
    };
    if (imageLoadState === 'error'
      || (observationStage === 'inpaint' && maskLoadState === 'error')) {
      onReviewObservation({ ...identity, state: 'error' });
      return;
    }
    if (imageLoadState !== 'ready'
      || !artifactChecksum
      || (observationStage === 'inpaint' && (maskLoadState !== 'ready' || !maskChecksum))) {
      onReviewObservation({ ...identity, state: 'loading' });
      return;
    }
    onReviewObservation({
      ...identity,
      state: 'ready',
      artifactChecksum,
      ...(observationStage === 'inpaint' ? { maskChecksum: maskChecksum! } : {}),
    });
  }, [
    artifactChecksum,
    imageAsset.id,
    imageAsset.revision,
    imageLoadState,
    maskChecksum,
    maskLoadState,
    observationStage,
    onReviewObservation,
  ]);

  useEffect(() => {
    // Canvas viewport follows measured container geometry, an explicit fit command,
    // or a post-typeset request to frame the selected boxes.
    const focused = focusRegionIds.length
      ? (useWorkbenchStore.getState().regionsByImage[imageAsset.id] ?? []).filter((region) =>
        focusRegionIds.includes(region.id))
      : [];
    const next = focused.length
      ? frameRegions(size, focused, { width: imageAsset.width, height: imageAsset.height })
      : null;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setViewport(next ?? fitViewport(size, imageAsset.width, imageAsset.height));
  }, [
    fitRequest,
    focusRegionIds,
    focusRequest,
    imageAsset.height,
    imageAsset.id,
    imageAsset.width,
    size,
  ]);

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
    if (!isKonvaRegionEditTarget(event.target)) clearRegionSelection();
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
      data-editable={editable ? 'true' : 'false'}
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
            {mode === 'erased'
              && showMask
              && maskImage
              && imageAsset.status.inpaint === 'done' ? (
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
  observation,
}: {
  onZoom: (direction: -1 | 1) => void;
  observation: CanvasReviewObservation | null;
}) {
  const image = useWorkbenchStore(activeImage);
  const mode = useWorkbenchStore((state) => state.canvasMode);
  const tool = useWorkbenchStore((state) => state.canvasTool);
  const compareMode = useWorkbenchStore((state) => state.compareMode);
  const showRegions = useWorkbenchStore((state) => state.showRegions);
  const showOrder = useWorkbenchStore((state) => state.showOrder);
  const showConfidence = useWorkbenchStore((state) => state.showConfidence);
  const showMask = useWorkbenchStore((state) => state.showMask);
  const maskBrushRadius = useWorkbenchStore((state) => state.maskBrushRadius);
  const selectedRegionIds = useWorkbenchStore((state) => state.selectedRegionIds);
  const setCanvasMode = useWorkbenchStore((state) => state.setCanvasMode);
  const setCanvasTool = useWorkbenchStore((state) => state.setCanvasTool);
  const toggleCompareMode = useWorkbenchStore((state) => state.toggleCompareMode);
  const setShowRegions = useWorkbenchStore((state) => state.setShowRegions);
  const setShowOrder = useWorkbenchStore((state) => state.setShowOrder);
  const setShowConfidence = useWorkbenchStore((state) => state.setShowConfidence);
  const setShowMask = useWorkbenchStore((state) => state.setShowMask);
  const setMaskBrushRadius = useWorkbenchStore((state) => state.setMaskBrushRadius);
  const requestFit = useWorkbenchStore((state) => state.requestFit);
  const focusSelectedRegions = useWorkbenchStore((state) => state.focusSelectedRegions);
  const createRegion = useWorkbenchStore((state) => state.createRegion);
  const reviewActiveImageStage = useWorkbenchStore((state) => state.reviewActiveImageStage);
  const selectInpaintCandidate = useWorkbenchStore((state) => state.selectInpaintCandidate);
  const stageReviewSaving = useWorkbenchStore((state) => state.stageReviewSaving);
  const compareAvailable = hasGeneratedPreview(image);
  const maskToolAvailable = Boolean(image && selectedRegionIds.length === 1);
  const maskToolActive = tool === 'mask-brush' || tool === 'mask-eraser';
  const reviewStage = visualStageForMode(mode);
  const stageReviewState = reviewStage
    ? image?.stageReviews?.[reviewStage]?.state ?? 'pending'
    : 'pending';
  const observationMatchesIdentity = Boolean(
    image
      && reviewStage
      && observation
      && observation.imageId === image.id
      && observation.stage === reviewStage
      && observation.revision === image.revision,
  );
  const observationReady = Boolean(
    observationMatchesIdentity
      && observation?.state === 'ready'
      && observation.artifactChecksum
      && (reviewStage !== 'inpaint' || observation.maskChecksum),
  );
  const stageReviewEnabled = Boolean(
    image
      && reviewStage
      && image.status[reviewStage] === 'done'
      && observationReady
      && (reviewStage !== 'inpaint' || showMask),
  );
  const stageReviewBusy = stageReviewSaving !== null;
  const submittedObservation: StageReviewObservation | undefined = observation?.state === 'ready'
    ? {
        imageId: observation.imageId,
        stage: observation.stage,
        revision: observation.revision,
        artifactChecksum: observation.artifactChecksum,
        ...(observation.maskChecksum ? { maskChecksum: observation.maskChecksum } : {}),
      }
    : undefined;
  const observationStatus = stageReviewBusy
    ? '保存中'
    : !image || !reviewStage || image.status[reviewStage] !== 'done'
      ? '尚未生成'
      : observationMatchesIdentity && observation?.state === 'error'
        ? '复核文件读取失败'
        : stageReviewState === 'accepted'
          ? '已接受'
          : stageReviewState === 'rejected'
            ? '已拒绝'
        : !observationReady
          ? '正在校验复核文件'
          : reviewStage === 'inpaint' && !showMask
            ? '请显示蒙版复核'
            : '待复核';

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
      {reviewStage ? (
        <div className="stage-review-controls" aria-busy={stageReviewBusy} aria-label="当前视觉阶段复核" role="group">
          <span aria-live="polite" className={`stage-review-state stage-review-state--${stageReviewState}`} role="status">
            {observationStatus}
          </span>
          {reviewStage === 'inpaint' ? (
            <label className="toolbar-check">
              <input
                checked={showMask}
                onChange={(event) => setShowMask(event.target.checked)}
                type="checkbox"
              />复核蒙版
            </label>
          ) : null}
          <button disabled={!stageReviewEnabled || stageReviewBusy || stageReviewState === 'accepted'} onClick={() => void reviewActiveImageStage(reviewStage, 'accepted', submittedObservation)} type="button">接受</button>
          <button disabled={!stageReviewEnabled || stageReviewBusy || stageReviewState === 'rejected'} onClick={() => void reviewActiveImageStage(reviewStage, 'rejected', submittedObservation)} type="button">拒绝</button>
          <button disabled={stageReviewBusy || stageReviewState === 'pending'} onClick={() => void reviewActiveImageStage(reviewStage, 'pending')} type="button">撤回复核</button>
        </div>
      ) : null}
      {mode === 'erased' && (image?.inpaintCandidates?.length ?? 0) > 1 ? (
        <label className="candidate-select">
          <span className="sr-only">修复候选</span>
          <select
            aria-label="修复候选"
            disabled={stageReviewBusy}
            onChange={(event) => {
              void selectInpaintCandidate(event.target.value);
            }}
            value={image?.inpaintCandidate ?? ''}
          >
            {image?.inpaintCandidates?.map((candidate) => (
              <option key={candidate.id} value={candidate.id}>{candidate.label}</option>
            ))}
          </select>
        </label>
      ) : null}
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
        <IconButton
          aria-label="框住所选"
          disabled={!selectedRegionIds.length}
          onClick={focusSelectedRegions}
          title="框住所选文本框 G"
        >
          框住
        </IconButton>
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
  const project = useWorkbenchStore((state) => state.currentProject);
  const hasLibrary = useWorkbenchStore((state) => state.images.length > 0);
  const compareMode = useWorkbenchStore((state) => state.compareMode);
  const requestedMode = useWorkbenchStore((state) => state.canvasMode);
  const setCanvasMode = useWorkbenchStore((state) => state.setCanvasMode);
  const toggleCompareMode = useWorkbenchStore((state) => state.toggleCompareMode);
  const regionsLoading = useWorkbenchStore((state) =>
    state.activeImageId ? state.regionsLoading[state.activeImageId] : false,
  );
  const [zoomSignal, setZoomSignal] = useState({ direction: 0 as -1 | 0 | 1, nonce: 0 });
  const [reviewObservation, setReviewObservation] = useState<CanvasReviewObservation | null>(null);

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
  const reviewStage = visualStageForMode(mode);
  const reviewIdentity = image && reviewStage
    ? `${image.id}:${image.revision}:${reviewStage}`
    : '';
  const reviewIdentityRef = useRef(reviewIdentity);
  useLayoutEffect(() => {
    reviewIdentityRef.current = reviewIdentity;
  }, [reviewIdentity]);
  const handleReviewObservation = useCallback((observation: CanvasReviewObservation) => {
    if (`${observation.imageId}:${observation.revision}:${observation.stage}` !== reviewIdentityRef.current) {
      return;
    }
    setReviewObservation((current) => {
      if (JSON.stringify(current) === JSON.stringify(observation)) return current;
      return observation;
    });
  }, []);

  useEffect(() => {
    if (requestedMode !== mode) setCanvasMode(mode);
  }, [mode, requestedMode, setCanvasMode]);

  useEffect(() => {
    if (compareMode && !compareAvailable) toggleCompareMode();
  }, [compareAvailable, compareMode, toggleCompareMode]);

  return (
    <main className="canvas-panel panel">
      <CanvasToolbar
        observation={reviewObservation}
        onZoom={(direction) => setZoomSignal((signal) => ({ direction, nonce: signal.nonce + 1 }))}
      />
      <div className={`canvas-area ${showCompare ? 'canvas-area--compare' : ''}`}>
        {!image ? (
          <EmptyState
            icon="▧"
            title={!project ? '先创建项目' : hasLibrary ? '选择一张图像开始' : '尚未导入图像'}
            description={
              !project
                ? '先创建本机项目，再用手机从相册导入。处理仍在 Mac 上运行。'
                : hasLibrary
                  ? '画布坐标始终使用原图像素，缩放和平移不会改变区域数据。'
                  : '手机请用「多图」从相册导入；处理仍在 Mac 上运行。'
            }
            action={!project ? <CreateLocalProjectButton /> : hasLibrary ? undefined : <ImportPhotosButton />}
          />
        ) : showCompare ? (
          <>
            <section className="compare-pane">
              <span className="compare-pane__label">原图</span>
              <CanvasViewport editable imageAsset={image} mode="original" zoomSignal={zoomSignal} />
            </section>
            <section className="compare-pane">
              <span className="compare-pane__label">{resultMode === 'preprocessed' ? '增强结果' : resultMode === 'erased' ? '擦除结果' : resultMode === 'typeset' ? '嵌字成品' : '原图'}</span>
              <CanvasViewport
                editable
                imageAsset={image}
                mode={resultMode}
                observationStage={reviewStage}
                onReviewObservation={reviewStage ? handleReviewObservation : undefined}
                zoomSignal={zoomSignal}
              />
            </section>
          </>
        ) : (
          <CanvasViewport
            editable
            imageAsset={image}
            mode={mode}
            observationStage={reviewStage}
            onReviewObservation={reviewStage ? handleReviewObservation : undefined}
            zoomSignal={zoomSignal}
          />
        )}
        {regionsLoading ? <div className="canvas-regions-loading"><LoadingState label="读取文本框…" /></div> : null}
      </div>
    </main>
  );
}
