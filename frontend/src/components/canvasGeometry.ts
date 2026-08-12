import type { MaskEditStroke } from '../types';

export const MAX_MASK_EDIT_STROKES = 256;
export const MAX_MASK_POINTS_PER_STROKE = 4096;
export const MAX_MASK_EDIT_POINTS = 16384;
export const MAX_MASK_BRUSH_RADIUS = 512;

export interface Viewport {
  x: number;
  y: number;
  scale: number;
}

export interface Point {
  x: number;
  y: number;
}

export interface RegionGeometry {
  x: number;
  y: number;
  width: number;
  height: number;
  rotation: number;
}

export interface CenteredRegionNodeGeometry {
  x: number;
  y: number;
  width: number;
  height: number;
  scaleX: number;
  scaleY: number;
  rotation: number;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

export function canonicalPoint(pointer: Point, viewport: Viewport): Point {
  return {
    x: (pointer.x - viewport.x) / viewport.scale,
    y: (pointer.y - viewport.y) / viewport.scale,
  };
}

export function regionToCenteredNodeGeometry(region: RegionGeometry) {
  return {
    x: region.x + region.width / 2,
    y: region.y + region.height / 2,
    offsetX: region.width / 2,
    offsetY: region.height / 2,
  };
}

export function centeredNodeToRegionGeometry(
  node: CenteredRegionNodeGeometry,
  bounds: { width: number; height: number },
): RegionGeometry {
  const width = Math.min(
    bounds.width,
    Math.max(4, Math.round(Math.abs(node.width * node.scaleX))),
  );
  const height = Math.min(
    bounds.height,
    Math.max(4, Math.round(Math.abs(node.height * node.scaleY))),
  );
  return {
    x: Math.round(clamp(node.x - width / 2, 0, Math.max(0, bounds.width - width))),
    y: Math.round(clamp(node.y - height / 2, 0, Math.max(0, bounds.height - height))),
    width,
    height,
    rotation: Math.round(node.rotation * 10) / 10,
  };
}

export function buildMaskStroke(
  mode: MaskEditStroke['mode'],
  radius: number,
  pointers: Point[],
  viewport: Viewport,
  bounds: { width: number; height: number },
): MaskEditStroke {
  return {
    mode,
    radius: clamp(Math.round(radius), 1, MAX_MASK_BRUSH_RADIUS),
    points: pointers.slice(0, MAX_MASK_POINTS_PER_STROKE).map((pointer) => {
      const point = canonicalPoint(pointer, viewport);
      return [
        Math.round(clamp(point.x, 0, bounds.width) * 100) / 100,
        Math.round(clamp(point.y, 0, bounds.height) * 100) / 100,
      ];
    }),
  };
}

export function maskEditCapacity(strokes: MaskEditStroke[]): {
  canAddStroke: boolean;
  remainingPoints: number;
} {
  const usedPoints = strokes.reduce((total, stroke) => total + stroke.points.length, 0);
  return {
    canAddStroke: strokes.length < MAX_MASK_EDIT_STROKES && usedPoints < MAX_MASK_EDIT_POINTS,
    remainingPoints: Math.max(
      0,
      Math.min(MAX_MASK_POINTS_PER_STROKE, MAX_MASK_EDIT_POINTS - usedPoints),
    ),
  };
}
