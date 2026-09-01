import Konva from 'konva';
import type { KonvaEventObject } from 'konva/lib/Node';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import {
  Circle,
  Group,
  Image as KonvaImage,
  Label,
  Layer,
  Line,
  Rect,
  Stage,
  Tag,
  Text,
  Transformer,
} from 'react-konva';

import { api } from '../api/client';
import {
  activeImage,
  activeRegions,
  g4EditingLocked,
  g7EditingLocked,
  hasGeneratedPreview,
  regionHasTypesetOverflow,
  useWorkbenchStore,
  workflowPhase,
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
import type { RequiredImageFormat } from './canvasImage';
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

type G7ReviewViewKey = 'original-off' | 'quality-off' | 'original-on' | 'quality-on';
type G8ReviewViewKey = 'original' | 'quality' | 'quality-mask' | 'candidate';
type G10ReviewViewKey = 'original' | 'clean-plate' | 'candidate';

interface G7ReviewViewObservation {
  identity: string;
  key: G7ReviewViewKey;
  state: 'loading' | 'ready' | 'error';
}

interface G7ReviewViewExpectation {
  identity: string;
  key: G7ReviewViewKey;
  expectedBaseChecksum: string;
}

const G7_REVIEW_VIEW_KEYS: G7ReviewViewKey[] = [
  'original-off',
  'quality-off',
  'original-on',
  'quality-on',
];
const G8_REVIEW_VIEW_KEYS: G8ReviewViewKey[] = [
  'original', 'quality', 'quality-mask', 'candidate',
];
const G10_REVIEW_VIEW_KEYS: G10ReviewViewKey[] = ['original', 'clean-plate', 'candidate'];

interface G8ReviewViewObservation {
  identity: string;
  key: G8ReviewViewKey;
  state: 'loading' | 'ready' | 'error';
}

interface G8ReviewViewExpectation {
  identity: string;
  key: G8ReviewViewKey;
  expectedChecksum: string;
  expectedWidth: number;
  expectedHeight: number;
}

interface G10ReviewViewObservation {
  identity: string;
  key: G10ReviewViewKey;
  state: 'loading' | 'ready' | 'error';
}

interface G10ReviewViewExpectation {
  identity: string;
  key: G10ReviewViewKey;
  expectedChecksum: string;
  expectedWidth: number;
  expectedHeight: number;
}

function allG7ReviewViewsReady(
  observations: Partial<Record<G7ReviewViewKey, G7ReviewViewObservation>>,
  identity: string,
): boolean {
  return Boolean(identity) && G7_REVIEW_VIEW_KEYS.every((key) => {
    const observation = observations[key];
    return observation?.identity === identity && observation.state === 'ready';
  });
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
  allowCanonicalScale = false,
  requiredFormat?: RequiredImageFormat,
) {
  const expectedWidth = expectedSize.width;
  const expectedHeight = expectedSize.height;
  const [result, setResult] = useState<{
    src: string;
    image: ImageBitmap | null;
    checksum: string | null;
    pixelWidth: number | null;
    pixelHeight: number | null;
    state: 'loading' | 'ready' | 'error';
  }>({
    src: '',
    image: null,
    checksum: null,
    pixelWidth: null,
    pixelHeight: null,
    state: 'loading',
  });

  useEffect(() => {
    if (!src) return;
    const controller = new AbortController();
    let disposed = false;
    void loadCanonicalCanvasImage(src, {
      width: expectedWidth,
      height: expectedHeight,
    }, controller.signal, allowCanonicalScale, requiredFormat)
      .then((loaded) => {
        if (disposed) loaded.image.close();
        else {
          setResult({
            src,
            image: loaded.image,
            checksum: loaded.checksum,
            pixelWidth: loaded.pixelWidth,
            pixelHeight: loaded.pixelHeight,
            state: 'ready',
          });
        }
      })
      .catch(() => {
        if (!disposed) {
          setResult({
            src,
            image: null,
            checksum: null,
            pixelWidth: null,
            pixelHeight: null,
            state: 'error',
          });
        }
      });
    return () => {
      disposed = true;
      controller.abort();
    };
  }, [allowCanonicalScale, expectedHeight, expectedWidth, requiredFormat, src]);

  useEffect(() => {
    const loadedImage = result.image;
    return () => loadedImage?.close();
  }, [result.image]);

  if (!src) {
    return {
      src: '',
      image: null,
      checksum: null,
      pixelWidth: null,
      pixelHeight: null,
      state: 'ready' as const,
    };
  }
  return result.src === src
    ? result
    : {
        src,
        image: null,
        checksum: null,
        pixelWidth: null,
        pixelHeight: null,
        state: 'loading' as const,
      };
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
  selectable,
  editable,
  showOrder,
  showConfidence,
  viewportScale,
}: {
  region: Region;
  image: ImageAsset;
  selected: boolean;
  selectable: boolean;
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
    if (!selectable) return;
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
  selectable,
  zoomSignal,
  observationStage,
  onReviewObservation,
  g7Mask,
  g7ReviewView,
  onG7ReviewViewObservation,
  g8ReviewView,
  onG8ReviewViewObservation,
  g10ReviewView,
  onG10ReviewViewObservation,
  artifactOverride,
  maskOn = false,
  maskEditable = false,
}: {
  imageAsset: ImageAsset;
  mode: CanvasMode;
  editable: boolean;
  selectable: boolean;
  zoomSignal: { direction: -1 | 0 | 1; nonce: number };
  observationStage?: VisualStage | null;
  onReviewObservation?: (observation: CanvasReviewObservation) => void;
  g7Mask?: { artifactId: string; checksum: string; width: number; height: number };
  g7ReviewView?: G7ReviewViewExpectation;
  onG7ReviewViewObservation?: (observation: G7ReviewViewObservation) => void;
  g8ReviewView?: G8ReviewViewExpectation;
  onG8ReviewViewObservation?: (observation: G8ReviewViewObservation) => void;
  g10ReviewView?: G10ReviewViewExpectation;
  onG10ReviewViewObservation?: (observation: G10ReviewViewObservation) => void;
  artifactOverride?: { src: string };
  maskOn?: boolean;
  maskEditable?: boolean;
}) {
  const { ref: containerRef, size } = useElementSize();
  const source = artifactOverride?.src ?? api.contentUrl(imageAsset.id, mode, imageAsset.revision);
  const canonicalSize = { width: imageAsset.width, height: imageAsset.height };
  const {
    image,
    checksum: artifactChecksum,
    pixelWidth: artifactPixelWidth,
    pixelHeight: artifactPixelHeight,
    state: imageLoadState,
  } = useCanvasImage(
    source,
    canonicalSize,
    Boolean(artifactOverride) || mode !== 'original',
    artifactOverride ? 'png' : undefined,
  );
  const showMask = useWorkbenchStore((state) => state.showMask);
  const maskSource = g7Mask
    ? api.maskArtifactUrl(imageAsset.id, g7Mask.artifactId)
    : mode === 'erased'
    && (showMask || observationStage === 'inpaint')
    && imageAsset.status.inpaint === 'done'
    ? api.maskUrl(imageAsset.id, imageAsset.revision)
    : null;
  const {
    image: maskImage,
    checksum: maskChecksum,
    pixelWidth: maskPixelWidth,
    pixelHeight: maskPixelHeight,
    state: maskLoadState,
  } = useCanvasImage(maskSource, canonicalSize, true, 'png');
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
  const g7Draft = useWorkbenchStore((state) => state.maskContexts[imageAsset.id]?.draft);
  const appendG7MaskStroke = useWorkbenchStore((state) => state.appendG7MaskStroke);
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
  const g7DraftStrokes: MaskEditStroke[] = maskEditable && selectedRegion
    ? (g7Draft?.regions
      .find((recipe) => recipe.regionId === selectedRegion.id)?.maskEdits.strokes ?? [])
    : [];

  useEffect(() => {
    if (!g7ReviewView || !onG7ReviewViewObservation) return;
    const needsMask = g7ReviewView.key.endsWith('-on');
    const baseMismatch = imageLoadState === 'ready'
      && artifactChecksum !== g7ReviewView.expectedBaseChecksum;
    const maskMismatch = needsMask && maskLoadState === 'ready' && Boolean(
      !g7Mask
      || maskChecksum !== g7Mask.checksum
      || maskPixelWidth !== g7Mask.width
      || maskPixelHeight !== g7Mask.height
      || (g7ReviewView.key === 'quality-on'
        && (artifactPixelWidth !== maskPixelWidth || artifactPixelHeight !== maskPixelHeight)),
    );
    if (imageLoadState === 'error' || (needsMask && maskLoadState === 'error')
      || baseMismatch || maskMismatch) {
      onG7ReviewViewObservation({
        identity: g7ReviewView.identity,
        key: g7ReviewView.key,
        state: 'error',
      });
      return;
    }
    if (imageLoadState !== 'ready' || !artifactChecksum
      || (needsMask && (maskLoadState !== 'ready' || !maskChecksum))) {
      onG7ReviewViewObservation({
        identity: g7ReviewView.identity,
        key: g7ReviewView.key,
        state: 'loading',
      });
      return;
    }
    onG7ReviewViewObservation({
      identity: g7ReviewView.identity,
      key: g7ReviewView.key,
      state: 'ready',
    });
  }, [
    artifactChecksum,
    artifactPixelHeight,
    artifactPixelWidth,
    g7Mask,
    g7ReviewView,
    imageLoadState,
    maskChecksum,
    maskLoadState,
    maskPixelHeight,
    maskPixelWidth,
    onG7ReviewViewObservation,
  ]);

  useEffect(() => {
    if (!g10ReviewView || !onG10ReviewViewObservation) return;
    const mismatch = imageLoadState === 'ready' && Boolean(
      artifactChecksum !== g10ReviewView.expectedChecksum
      || artifactPixelWidth !== g10ReviewView.expectedWidth
      || artifactPixelHeight !== g10ReviewView.expectedHeight,
    );
    const state = imageLoadState === 'error' || mismatch
      ? 'error' : imageLoadState === 'ready' ? 'ready' : 'loading';
    onG10ReviewViewObservation({ identity: g10ReviewView.identity, key: g10ReviewView.key, state });
  }, [
    artifactChecksum,
    artifactPixelHeight,
    artifactPixelWidth,
    g10ReviewView,
    imageLoadState,
    onG10ReviewViewObservation,
  ]);

  useEffect(() => {
    if (!g8ReviewView || !onG8ReviewViewObservation) return;
    const needsMask = g8ReviewView.key === 'quality-mask';
    const baseMismatch = imageLoadState === 'ready' && Boolean(
      artifactChecksum !== g8ReviewView.expectedChecksum
      || artifactPixelWidth !== g8ReviewView.expectedWidth
      || artifactPixelHeight !== g8ReviewView.expectedHeight,
    );
    const maskMismatch = needsMask && maskLoadState === 'ready' && Boolean(
      !g7Mask || maskChecksum !== g7Mask.checksum
      || maskPixelWidth !== g7Mask.width || maskPixelHeight !== g7Mask.height
      || artifactPixelWidth !== maskPixelWidth || artifactPixelHeight !== maskPixelHeight,
    );
    const state = imageLoadState === 'error' || (needsMask && maskLoadState === 'error')
      || baseMismatch || maskMismatch
      ? 'error'
      : imageLoadState === 'ready' && (!needsMask || maskLoadState === 'ready')
        ? 'ready'
        : 'loading';
    onG8ReviewViewObservation({ identity: g8ReviewView.identity, key: g8ReviewView.key, state });
  }, [
    artifactChecksum,
    artifactPixelHeight,
    artifactPixelWidth,
    g7Mask,
    g8ReviewView,
    imageLoadState,
    maskChecksum,
    maskLoadState,
    maskPixelHeight,
    maskPixelWidth,
    onG8ReviewViewObservation,
  ]);

  useEffect(() => {
    if (!observationStage || !onReviewObservation) return;
    const identity = {
      imageId: imageAsset.id,
      stage: observationStage,
      revision: imageAsset.revision,
    };
    const rasterMismatch = observationStage === 'inpaint'
      && imageLoadState === 'ready'
      && maskLoadState === 'ready'
      && (artifactPixelWidth !== maskPixelWidth || artifactPixelHeight !== maskPixelHeight);
    if (imageLoadState === 'error'
      || (observationStage === 'inpaint' && (maskLoadState === 'error' || rasterMismatch))) {
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
    artifactPixelHeight,
    artifactPixelWidth,
    imageAsset.id,
    imageAsset.revision,
    imageLoadState,
    maskChecksum,
    maskLoadState,
    maskPixelHeight,
    maskPixelWidth,
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
    if (!editable && !(maskEditing && maskEditable)) return;
    if (maskEditing) {
      if (!selectedRegion) return;
      const strokes = maskEditable ? g7DraftStrokes : selectedRegion.repair.maskEdits?.strokes ?? [];
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
        if (maskEditable) {
          void appendG7MaskStroke(region.id, completed);
        } else updateRegion(region.id, {
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
      data-selectable={selectable ? 'true' : 'false'}
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
            {(g7Mask ? maskOn : mode === 'erased' && showMask)
              && maskImage
              && (g7Mask || imageAsset.status.inpaint === 'done') ? (
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
                selectable={selectable && tool === 'select' && !spacePressed}
                selected={selectedRegionIds.includes(region.id)}
                showConfidence={showConfidence}
                showOrder={showOrder}
                viewportScale={viewport.scale}
              />
            )) : null}
            {maskEditing && selectedRegion ? [
              ...(maskEditable ? g7DraftStrokes : selectedRegion.repair.maskEdits?.strokes ?? []),
              ...(maskDraft?.regionId === selectedRegion.id ? [maskDraft] : []),
            ].map((stroke, index) => {
              const color = stroke.mode === 'add' ? '#4ad7c8' : '#ff6b6b';
              const key = `${stroke.mode}-${index}`;
              const point = stroke.points.length === 1 ? stroke.points[0] : null;
              if (point) {
                return (
                  <Circle
                    fill={color}
                    key={key}
                    listening={false}
                    opacity={0.72}
                    radius={Math.max(0.5, stroke.radius)}
                    x={point[0]}
                    y={point[1]}
                  />
                );
              }
              return (
                <Line
                  key={key}
                  points={stroke.points.flat()}
                  stroke={color}
                  strokeWidth={Math.max(1, stroke.radius * 2)}
                  lineCap="round"
                  lineJoin="round"
                  listening={false}
                  opacity={0.72}
                />
              );
            }) : null}
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
  const g4Locked = useWorkbenchStore((state) => g4EditingLocked(state));
  const g7Locked = useWorkbenchStore((state) => g7EditingLocked(state));
  const g4ContextStatus = useWorkbenchStore((state) => state.activeImageId
    ? state.g4Contexts[state.activeImageId]?.status
    : undefined);
  const phase = useWorkbenchStore((state) => state.activeImageId
    ? workflowPhase(state.g4Contexts[state.activeImageId])
    : null);
  const g4Active = g4ContextStatus === 'active';
  const legacyStageControls = g4ContextStatus === 'legacy';
  const g7Context = useWorkbenchStore((state) => state.activeImageId ? state.maskContexts[state.activeImageId] : undefined);
  const backgroundReviewMode = phase === 'G5' || phase === 'G6' || phase === 'G7' || phase === 'G8' || phase === 'G9' || phase === 'G10';
  const modeAvailable = (value: CanvasMode) => canvasModeAvailable(image, value)
    && (!backgroundReviewMode || value === 'original' || value === 'preprocessed');
  const compareAvailable = backgroundReviewMode
    ? canvasModeAvailable(image, 'preprocessed')
    : hasGeneratedPreview(image);
  const maskToolAvailable = Boolean(
    image && selectedRegionIds.length === 1 && (legacyStageControls || (
      phase === 'G7' && !g7Locked && g7Context?.eligibleRegionIds.includes(selectedRegionIds[0]!)
    ))
  );
  const maskToolActive = maskToolAvailable
    && (tool === 'mask-brush' || tool === 'mask-eraser');
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
      && (reviewStage !== 'inpaint' || showMask)
      && !g4Active,
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
          <button aria-pressed={mode === value} disabled={!modeAvailable(value)} key={value} onClick={() => setCanvasMode(value)} title={!modeAvailable(value) && value !== 'original' ? backgroundReviewMode && (value === 'erased' || value === 'typeset') ? '严格门禁仅允许原图与已接受质量底板' : '尚未生成，请先运行对应步骤' : undefined} type="button">{label}</button>
        ))}
      </div>
      {reviewStage && legacyStageControls ? (
        <div className="stage-review-controls" aria-busy={stageReviewBusy} aria-label="当前视觉阶段复核" role="group">
          <span aria-live="polite" className={`stage-review-state stage-review-state--${stageReviewState}`} role="status">
            {observationStatus}
          </span>
          {reviewStage === 'inpaint' ? (
            <label className="stage-review-check">
              <input
                aria-label="复核蒙版"
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
      {legacyStageControls && mode === 'erased' && (image?.inpaintCandidates?.length ?? 0) > 1 ? (
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
        <IconButton aria-label="绘制文本框" className={tool === 'region' ? 'is-active' : ''} disabled={g4Locked} onClick={() => setCanvasTool('region')} title="绘制文本框 N">▢</IconButton>
        <IconButton aria-label="平移工具" className={tool === 'hand' ? 'is-active' : ''} onClick={() => setCanvasTool('hand')} title="平移 H">✋</IconButton>
        <IconButton aria-label="在中央快速新建文本框" disabled={!image || g4Locked} onClick={quickCreate} title="快速新建文本框">＋框</IconButton>
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
        title={compareAvailable ? undefined : backgroundReviewMode ? '尚无已接受质量底板可供对比' : '尚无增强、擦除或成品可供对比'}
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
  const editingLocked = useWorkbenchStore((state) => g4EditingLocked(state));
  const activeContext = useWorkbenchStore((state) => state.activeImageId
    ? state.g4Contexts[state.activeImageId]
    : undefined);
  const phase = workflowPhase(activeContext);
  const backgroundReviewMode = phase === 'G5' || phase === 'G6' || phase === 'G7' || phase === 'G8' || phase === 'G9' || phase === 'G10';
  const regionSelectable = activeContext?.status === 'legacy'
    || (activeContext?.status === 'active' && (
      phase === 'G4' || phase === 'G5' || phase === 'G6' || phase === 'G7' || phase === 'G10'
    ));
  const legacyMaskEditingAllowed = useWorkbenchStore((state) => Boolean(
    state.activeImageId && state.g4Contexts[state.activeImageId]?.status === 'legacy'
  ));
  const g7MaskEditingAllowed = useWorkbenchStore((state) => Boolean(
    state.activeImageId && workflowPhase(state.g4Contexts[state.activeImageId]) === 'G7'
      && !g7EditingLocked(state, state.activeImageId)
  ));
  const maskContext = useWorkbenchStore((state) => state.activeImageId ? state.maskContexts[state.activeImageId] : undefined);
  const selectedArtifactId = useWorkbenchStore((state) => state.activeImageId ? state.selectedMaskArtifactIds[state.activeImageId] : undefined);
  const selectedArtifact = maskContext?.artifacts.find((artifact) => artifact.artifactId === selectedArtifactId);
  const g7Mask = useMemo(() => selectedArtifact ? {
    artifactId: selectedArtifact.artifactId,
    checksum: selectedArtifact.maskChecksum,
    width: selectedArtifact.width,
    height: selectedArtifact.height,
  } : undefined, [selectedArtifact]);
  const cleanPlateContext = useWorkbenchStore((state) => state.activeImageId
    ? state.cleanPlateContexts[state.activeImageId] : undefined);
  const translationContext = useWorkbenchStore((state) => state.activeImageId
    ? state.translationContexts[state.activeImageId] : undefined);
  const typesetContext = useWorkbenchStore((state) => state.activeImageId
    ? state.typesetContexts[state.activeImageId] : undefined);
  const selectedTypesetCandidateId = useWorkbenchStore((state) => state.activeImageId
    ? state.selectedTypesetCandidateIds[state.activeImageId] : undefined);
  const selectedTypesetCandidate = typesetContext?.candidates.find((candidate) =>
    candidate.candidateId === selectedTypesetCandidateId);
  const selectedCleanPlateCandidateId = useWorkbenchStore((state) => state.activeImageId
    ? state.selectedCleanPlateCandidateIds[state.activeImageId] : undefined);
  const selectedCleanPlateCandidate = cleanPlateContext?.candidates.find((candidate) =>
    candidate.candidateId === selectedCleanPlateCandidateId);
  const g8MaskArtifact = maskContext?.artifacts.find((artifact) =>
    artifact.artifactId === cleanPlateContext?.maskArtifactId
    && artifact.maskChecksum === cleanPlateContext.maskChecksum);
  const g8AcceptedMask = useMemo(() => g8MaskArtifact ? {
    artifactId: g8MaskArtifact.artifactId,
    checksum: g8MaskArtifact.maskChecksum,
    width: g8MaskArtifact.width,
    height: g8MaskArtifact.height,
  } : undefined, [g8MaskArtifact]);
  const observeG8CleanPlateBitmap = useWorkbenchStore((state) => state.observeG8CleanPlateBitmap);
  const observeG7MaskBitmap = useWorkbenchStore((state) => state.observeG7MaskBitmap);
  const observeG10TypesetBitmap = useWorkbenchStore((state) => state.observeG10TypesetBitmap);
  const g7ReviewIdentity = image && activeContext?.status === 'active' && activeContext.generation
    && maskContext && selectedArtifact && phase === 'G7'
    ? [
        activeContext.generation.id,
        image.id,
        image.revision,
        maskContext.imageRevision,
        selectedArtifact.artifactId,
        activeContext.generation.sourceChecksum,
        maskContext.qualityChecksum,
        selectedArtifact.maskChecksum,
        selectedArtifact.width,
        selectedArtifact.height,
      ].join(':')
    : '';
  const [g7ReviewViews, setG7ReviewViews] = useState<Partial<
    Record<G7ReviewViewKey, G7ReviewViewObservation>
  >>({});
  const g7ReviewIdentityRef = useRef(g7ReviewIdentity);
  const g8ReviewIdentity = image && activeContext?.status === 'active' && activeContext.generation
    && cleanPlateContext && selectedCleanPlateCandidate && g8AcceptedMask && phase === 'G8'
    ? [
        activeContext.generation.id,
        activeContext.generation.nextSequence,
        image.id,
        image.revision,
        cleanPlateContext.cleanPlateStateChecksum,
        cleanPlateContext.g7Checksum,
        selectedCleanPlateCandidate.candidateId,
        selectedCleanPlateCandidate.candidateChecksum,
        selectedCleanPlateCandidate.routeChecksum,
        activeContext.generation.sourceChecksum,
        cleanPlateContext.qualityChecksum,
        g8AcceptedMask.artifactId,
        g8AcceptedMask.checksum,
        g8AcceptedMask.width,
        g8AcceptedMask.height,
        selectedCleanPlateCandidate.width,
        selectedCleanPlateCandidate.height,
      ].join(':')
    : '';
  const [g8ReviewViews, setG8ReviewViews] = useState<Partial<
    Record<G8ReviewViewKey, G8ReviewViewObservation>
  >>({});
  const g8ReviewIdentityRef = useRef(g8ReviewIdentity);
  const g10ReviewIdentity = image && activeContext?.status === 'active' && activeContext.generation
    && typesetContext && selectedTypesetCandidate && phase === 'G10'
    ? [
        activeContext.generation.id,
        activeContext.generation.nextSequence,
        image.id,
        image.revision,
        typesetContext.g9TerminalChecksum,
        typesetContext.cleanPlateChecksum,
        selectedTypesetCandidate.candidateId,
        selectedTypesetCandidate.candidateChecksum,
        selectedTypesetCandidate.routeChecksum,
        selectedTypesetCandidate.styleChecksum,
        selectedTypesetCandidate.layoutChecksum,
        activeContext.generation.sourceChecksum,
        selectedTypesetCandidate.width,
        selectedTypesetCandidate.height,
        selectedTypesetCandidate.renderScale,
      ].join(':')
    : '';
  const [g10ReviewViews, setG10ReviewViews] = useState<Partial<
    Record<G10ReviewViewKey, G10ReviewViewObservation>
  >>({});
  const g10ReviewIdentityRef = useRef(g10ReviewIdentity);
  const canvasTool = useWorkbenchStore((state) => state.canvasTool);
  const setCanvasTool = useWorkbenchStore((state) => state.setCanvasTool);
  const [zoomSignal, setZoomSignal] = useState({ direction: 0 as -1 | 0 | 1, nonce: 0 });
  const [reviewObservation, setReviewObservation] = useState<CanvasReviewObservation | null>(null);

  useLayoutEffect(() => {
    g7ReviewIdentityRef.current = g7ReviewIdentity;
  }, [g7ReviewIdentity]);

  useLayoutEffect(() => {
    g8ReviewIdentityRef.current = g8ReviewIdentity;
  }, [g8ReviewIdentity]);

  useLayoutEffect(() => {
    g10ReviewIdentityRef.current = g10ReviewIdentity;
  }, [g10ReviewIdentity]);

  const handleG7ReviewViewObservation = useCallback((observation: G7ReviewViewObservation) => {
    if (observation.identity !== g7ReviewIdentityRef.current) return;
    setG7ReviewViews((current) => {
      const previous = current[observation.key];
      if (previous?.identity === observation.identity && previous.state === observation.state) {
        return current;
      }
      return { ...current, [observation.key]: observation };
    });
  }, []);

  const handleG8ReviewViewObservation = useCallback((observation: G8ReviewViewObservation) => {
    if (observation.identity !== g8ReviewIdentityRef.current) return;
    setG8ReviewViews((current) => {
      const previous = current[observation.key];
      if (previous?.identity === observation.identity && previous.state === observation.state) {
        return current;
      }
      return { ...current, [observation.key]: observation };
    });
  }, []);

  const handleG10ReviewViewObservation = useCallback((observation: G10ReviewViewObservation) => {
    if (observation.identity !== g10ReviewIdentityRef.current) return;
    setG10ReviewViews((current) => {
      const previous = current[observation.key];
      if (previous?.identity === observation.identity && previous.state === observation.state) {
        return current;
      }
      return { ...current, [observation.key]: observation };
    });
  }, []);

  useEffect(() => {
    if (!image || !maskContext || !selectedArtifact || !g7ReviewIdentity) {
      observeG7MaskBitmap(null);
      return;
    }
    if (allG7ReviewViewsReady(g7ReviewViews, g7ReviewIdentity)) {
      observeG7MaskBitmap({
        imageId: image.id,
        artifactId: selectedArtifact.artifactId,
        imageRevision: image.revision,
        checksum: selectedArtifact.maskChecksum,
        width: selectedArtifact.width,
        height: selectedArtifact.height,
        state: 'ready',
      });
      return;
    }
    observeG7MaskBitmap(null);
    if (G7_REVIEW_VIEW_KEYS.some((key) => {
      const observation = g7ReviewViews[key];
      return observation?.identity === g7ReviewIdentity && observation.state === 'error';
    })) {
      useWorkbenchStore.setState({
        globalError: 'G7 四视图底图或实际蒙版的 checksum/像素网格读取失败；接受已锁定。',
      });
    }
  }, [
    g7ReviewIdentity,
    g7ReviewViews,
    image,
    maskContext,
    observeG7MaskBitmap,
    selectedArtifact,
  ]);

  useEffect(() => {
    if (!image || !activeContext?.generation || !cleanPlateContext || !g8AcceptedMask
      || !selectedCleanPlateCandidate || !g8ReviewIdentity) {
      observeG8CleanPlateBitmap(null);
      return;
    }
    const ready = G8_REVIEW_VIEW_KEYS.every((key) => {
      const observation = g8ReviewViews[key];
      return observation?.identity === g8ReviewIdentity && observation.state === 'ready';
    });
    if (ready) {
      observeG8CleanPlateBitmap({
        imageId: image.id,
        generationId: activeContext.generation.id,
        nextSequence: activeContext.generation.nextSequence,
        cleanPlateStateChecksum: cleanPlateContext.cleanPlateStateChecksum,
        candidateId: selectedCleanPlateCandidate.candidateId,
        imageRevision: image.revision,
        sourceChecksum: activeContext.generation.sourceChecksum,
        qualityChecksum: cleanPlateContext.qualityChecksum,
        maskArtifactId: g8AcceptedMask.artifactId,
        maskChecksum: g8AcceptedMask.checksum,
        maskWidth: g8AcceptedMask.width,
        maskHeight: g8AcceptedMask.height,
        checksum: selectedCleanPlateCandidate.candidateChecksum,
        width: selectedCleanPlateCandidate.width,
        height: selectedCleanPlateCandidate.height,
        state: 'ready',
      });
      return;
    }
    observeG8CleanPlateBitmap(null);
    if (G8_REVIEW_VIEW_KEYS.some((key) => {
      const observation = g8ReviewViews[key];
      return observation?.identity === g8ReviewIdentity && observation.state === 'error';
    })) {
      useWorkbenchStore.setState({
        globalError: 'G8 四视图底图、实际蒙版或候选 PNG 的 checksum/像素网格读取失败；复核已锁定。',
      });
    }
  }, [
    activeContext,
    cleanPlateContext,
    g8AcceptedMask,
    g8ReviewIdentity,
    g8ReviewViews,
    image,
    observeG8CleanPlateBitmap,
    selectedCleanPlateCandidate,
  ]);

  useEffect(() => {
    if (!image || !activeContext?.generation || !typesetContext
      || !selectedTypesetCandidate || !g10ReviewIdentity) {
      observeG10TypesetBitmap(null);
      return;
    }
    const ready = G10_REVIEW_VIEW_KEYS.every((key) => {
      const observation = g10ReviewViews[key];
      return observation?.identity === g10ReviewIdentity && observation.state === 'ready';
    });
    if (ready) {
      observeG10TypesetBitmap({
        imageId: image.id,
        generationId: activeContext.generation.id,
        nextSequence: activeContext.generation.nextSequence,
        candidateId: selectedTypesetCandidate.candidateId,
        imageRevision: image.revision,
        sourceChecksum: activeContext.generation.sourceChecksum,
        cleanPlateChecksum: typesetContext.cleanPlateChecksum,
        candidateChecksum: selectedTypesetCandidate.candidateChecksum,
        routeChecksum: selectedTypesetCandidate.routeChecksum,
        styleChecksum: selectedTypesetCandidate.styleChecksum,
        layoutChecksum: selectedTypesetCandidate.layoutChecksum,
        width: selectedTypesetCandidate.width,
        height: selectedTypesetCandidate.height,
        renderScale: selectedTypesetCandidate.renderScale,
        state: 'ready',
      });
      return;
    }
    observeG10TypesetBitmap(null);
    if (G10_REVIEW_VIEW_KEYS.some((key) => {
      const observation = g10ReviewViews[key];
      return observation?.identity === g10ReviewIdentity && observation.state === 'error';
    })) {
      useWorkbenchStore.setState({
        globalError: 'G10 三视图原图、accepted clean plate 或最终候选 checksum/像素网格读取失败；复核已锁定。',
      });
    }
  }, [
    activeContext,
    g10ReviewIdentity,
    g10ReviewViews,
    image,
    observeG10TypesetBitmap,
    selectedTypesetCandidate,
    typesetContext,
  ]);

  const requestedModeAllowed = canvasModeAvailable(image, requestedMode)
    && (!backgroundReviewMode || requestedMode === 'original' || requestedMode === 'preprocessed');
  const mode: CanvasMode = requestedModeAllowed ? requestedMode : 'original';
  const resultMode = useMemo<CanvasMode>(() => {
    if (backgroundReviewMode) {
      return canvasModeAvailable(image, 'preprocessed') ? 'preprocessed' : 'original';
    }
    if (mode !== 'original') return mode;
    if (canvasModeAvailable(image, 'typeset')) return 'typeset';
    if (canvasModeAvailable(image, 'erased')) return 'erased';
    if (canvasModeAvailable(image, 'preprocessed')) return 'preprocessed';
    return 'original';
  }, [backgroundReviewMode, image, mode]);
  const compareAvailable = backgroundReviewMode
    ? canvasModeAvailable(image, 'preprocessed')
    : hasGeneratedPreview(image);
  const showCompare = compareMode && compareAvailable;
  const reviewStage = backgroundReviewMode ? null : visualStageForMode(mode);
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
    if (!legacyMaskEditingAllowed && !g7MaskEditingAllowed && (canvasTool === 'mask-brush' || canvasTool === 'mask-eraser')) {
      setCanvasTool('select');
    }
  }, [canvasTool, g7MaskEditingAllowed, legacyMaskEditingAllowed, setCanvasTool]);

  useEffect(() => {
    if (activeContext?.status === 'active' && phase !== 'G4' && canvasTool === 'region') {
      setCanvasTool('select');
    }
  }, [activeContext?.status, canvasTool, phase, setCanvasTool]);

  useEffect(() => {
    if (compareMode && !compareAvailable) toggleCompareMode();
  }, [compareAvailable, compareMode, toggleCompareMode]);

  return (
    <main className="canvas-panel panel">
      <CanvasToolbar
        observation={reviewObservation}
        onZoom={(direction) => setZoomSignal((signal) => ({ direction, nonce: signal.nonce + 1 }))}
      />
      <div className={`canvas-area ${phase === 'G10' && selectedTypesetCandidate ? 'canvas-area--g10-grid' : (phase === 'G7' && g7Mask) || (phase === 'G8' && g8AcceptedMask && selectedCleanPlateCandidate) ? 'canvas-area--g7-grid' : phase === 'G9' ? 'canvas-area--compare' : showCompare ? 'canvas-area--compare' : ''}`}>
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
        ) : phase === 'G10' && typesetContext && selectedTypesetCandidate ? (
          <>
            <section className="compare-pane"><span className="compare-pane__label">不可变原图</span><CanvasViewport editable={false} g10ReviewView={{ identity: g10ReviewIdentity, key: 'original', expectedChecksum: activeContext?.generation?.sourceChecksum ?? '', expectedWidth: image.width, expectedHeight: image.height }} imageAsset={image} mode="original" onG10ReviewViewObservation={handleG10ReviewViewObservation} selectable={regionSelectable} zoomSignal={zoomSignal} /></section>
            <section className="compare-pane"><span className="compare-pane__label">G10 父项 · accepted clean plate</span><CanvasViewport artifactOverride={typesetContext.cleanPlateCandidateId ? { src: api.cleanPlateCandidateUrl(image.id, typesetContext.cleanPlateCandidateId) } : undefined} editable={false} g10ReviewView={{ identity: g10ReviewIdentity, key: 'clean-plate', expectedChecksum: typesetContext.cleanPlateChecksum, expectedWidth: selectedTypesetCandidate.width, expectedHeight: selectedTypesetCandidate.height }} imageAsset={image} mode="preprocessed" onG10ReviewViewObservation={handleG10ReviewViewObservation} selectable={regionSelectable} zoomSignal={zoomSignal} /></section>
            <section className="compare-pane"><span className="compare-pane__label">不可变最终候选</span><CanvasViewport artifactOverride={{ src: selectedTypesetCandidate.artifactUrl }} editable={false} g10ReviewView={{ identity: g10ReviewIdentity, key: 'candidate', expectedChecksum: selectedTypesetCandidate.candidateChecksum, expectedWidth: selectedTypesetCandidate.width, expectedHeight: selectedTypesetCandidate.height }} imageAsset={image} mode="preprocessed" onG10ReviewViewObservation={handleG10ReviewViewObservation} selectable={regionSelectable} zoomSignal={zoomSignal} /></section>
          </>
        ) : phase === 'G8' && g8AcceptedMask && selectedCleanPlateCandidate ? (
          <>
            <section className="compare-pane"><span className="compare-pane__label">原图</span><CanvasViewport editable={false} g8ReviewView={{ identity: g8ReviewIdentity, key: 'original', expectedChecksum: activeContext?.generation?.sourceChecksum ?? '', expectedWidth: image.width, expectedHeight: image.height }} imageAsset={image} mode="original" onG8ReviewViewObservation={handleG8ReviewViewObservation} selectable={false} zoomSignal={zoomSignal} /></section>
            <section className="compare-pane"><span className="compare-pane__label">质量底板</span><CanvasViewport editable={false} g8ReviewView={{ identity: g8ReviewIdentity, key: 'quality', expectedChecksum: cleanPlateContext?.qualityChecksum ?? '', expectedWidth: selectedCleanPlateCandidate.width, expectedHeight: selectedCleanPlateCandidate.height }} imageAsset={image} mode="preprocessed" onG8ReviewViewObservation={handleG8ReviewViewObservation} selectable={false} zoomSignal={zoomSignal} /></section>
            <section className="compare-pane"><span className="compare-pane__label">质量底板 · accepted mask</span><CanvasViewport editable={false} g7Mask={g8AcceptedMask} g8ReviewView={{ identity: g8ReviewIdentity, key: 'quality-mask', expectedChecksum: cleanPlateContext?.qualityChecksum ?? '', expectedWidth: selectedCleanPlateCandidate.width, expectedHeight: selectedCleanPlateCandidate.height }} imageAsset={image} maskOn mode="preprocessed" onG8ReviewViewObservation={handleG8ReviewViewObservation} selectable={false} zoomSignal={zoomSignal} /></section>
            <section className="compare-pane"><span className="compare-pane__label">不可变 clean plate 候选</span><CanvasViewport artifactOverride={{ src: api.cleanPlateCandidateUrl(image.id, selectedCleanPlateCandidate.candidateId) }} editable={false} g8ReviewView={{ identity: g8ReviewIdentity, key: 'candidate', expectedChecksum: selectedCleanPlateCandidate.candidateChecksum, expectedWidth: selectedCleanPlateCandidate.width, expectedHeight: selectedCleanPlateCandidate.height }} imageAsset={image} mode="preprocessed" onG8ReviewViewObservation={handleG8ReviewViewObservation} selectable={false} zoomSignal={zoomSignal} /></section>
          </>
        ) : phase === 'G9' && translationContext ? (
          <>
            <section className="compare-pane"><span className="compare-pane__label">不可变原图</span><CanvasViewport editable={false} imageAsset={image} mode="original" selectable={false} zoomSignal={zoomSignal} /></section>
            <section className="compare-pane"><span className="compare-pane__label">G9 父项 · accepted clean plate · {translationContext.cleanPlateChecksum.slice(0, 12)}</span><CanvasViewport artifactOverride={translationContext.cleanPlateCandidateId ? { src: api.cleanPlateCandidateUrl(image.id, translationContext.cleanPlateCandidateId) } : undefined} editable={false} imageAsset={image} mode="preprocessed" selectable={false} zoomSignal={zoomSignal} /></section>
          </>
        ) : phase === 'G7' && g7Mask ? (
          <>
            <section className="compare-pane"><span className="compare-pane__label">原图 · mask-off</span><CanvasViewport editable={false} g7ReviewView={{ identity: g7ReviewIdentity, key: 'original-off', expectedBaseChecksum: activeContext?.generation?.sourceChecksum ?? '' }} imageAsset={image} mode="original" onG7ReviewViewObservation={handleG7ReviewViewObservation} selectable={regionSelectable} zoomSignal={zoomSignal} /></section>
            <section className="compare-pane"><span className="compare-pane__label">质量底板 · mask-off</span><CanvasViewport editable={false} g7ReviewView={{ identity: g7ReviewIdentity, key: 'quality-off', expectedBaseChecksum: maskContext?.qualityChecksum ?? '' }} imageAsset={image} mode="preprocessed" onG7ReviewViewObservation={handleG7ReviewViewObservation} selectable={regionSelectable} zoomSignal={zoomSignal} /></section>
            <section className="compare-pane"><span className="compare-pane__label">原图 · mask-on</span><CanvasViewport editable={false} g7Mask={g7Mask} g7ReviewView={{ identity: g7ReviewIdentity, key: 'original-on', expectedBaseChecksum: activeContext?.generation?.sourceChecksum ?? '' }} imageAsset={image} maskEditable={g7MaskEditingAllowed} maskOn mode="original" onG7ReviewViewObservation={handleG7ReviewViewObservation} selectable={regionSelectable} zoomSignal={zoomSignal} /></section>
            <section className="compare-pane"><span className="compare-pane__label">质量底板 · mask-on</span><CanvasViewport editable={false} g7Mask={g7Mask} g7ReviewView={{ identity: g7ReviewIdentity, key: 'quality-on', expectedBaseChecksum: maskContext?.qualityChecksum ?? '' }} imageAsset={image} maskEditable={g7MaskEditingAllowed} maskOn mode="preprocessed" onG7ReviewViewObservation={handleG7ReviewViewObservation} selectable={regionSelectable} zoomSignal={zoomSignal} /></section>
          </>
        ) : showCompare ? (
          <>
            <section className="compare-pane">
              <span className="compare-pane__label">原图</span>
              <CanvasViewport editable={!editingLocked} imageAsset={image} maskEditable={g7MaskEditingAllowed} mode="original" selectable={regionSelectable} zoomSignal={zoomSignal} />
            </section>
            <section className="compare-pane">
              <span className="compare-pane__label">{backgroundReviewMode ? '已接受质量底板' : resultMode === 'preprocessed' ? '增强结果' : resultMode === 'erased' ? '擦除结果' : resultMode === 'typeset' ? '嵌字成品' : '原图'}</span>
              <CanvasViewport
                editable={!editingLocked}
                imageAsset={image}
                maskEditable={g7MaskEditingAllowed}
                mode={resultMode}
                observationStage={reviewStage}
                onReviewObservation={reviewStage ? handleReviewObservation : undefined}
                selectable={regionSelectable}
                zoomSignal={zoomSignal}
              />
            </section>
          </>
        ) : (
          <CanvasViewport
            editable={!editingLocked}
            imageAsset={image}
            maskEditable={g7MaskEditingAllowed}
            mode={mode}
            observationStage={reviewStage}
            onReviewObservation={reviewStage ? handleReviewObservation : undefined}
            selectable={regionSelectable}
            zoomSignal={zoomSignal}
          />
        )}
        {regionsLoading ? <div className="canvas-regions-loading"><LoadingState label="读取文本框…" /></div> : null}
      </div>
    </main>
  );
}
