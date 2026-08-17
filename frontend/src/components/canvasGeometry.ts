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

export function frameRegions(
  container: { width: number; height: number },
  regions: Array<{ x: number; y: number; width: number; height: number }>,
  imageSize?: { width: number; height: number },
): Viewport | null {
  if (!regions.length || container.width <= 1 || container.height <= 1) return null;
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const region of regions) {
    minX = Math.min(minX, region.x);
    minY = Math.min(minY, region.y);
    maxX = Math.max(maxX, region.x + region.width);
    maxY = Math.max(maxY, region.y + region.height);
  }
  const boxWidth = Math.max(1, maxX - minX);
  const boxHeight = Math.max(1, maxY - minY);
  const pad = Math.max(48, Math.max(boxWidth, boxHeight) * 0.4);
  minX -= pad;
  minY -= pad;
  maxX += pad;
  maxY += pad;
  if (imageSize) {
    minX = Math.max(0, minX);
    minY = Math.max(0, minY);
    maxX = Math.min(imageSize.width, maxX);
    maxY = Math.min(imageSize.height, maxY);
  }
  const width = Math.max(1, maxX - minX);
  const height = Math.max(1, maxY - minY);
  const inset = 24;
  const scale = clamp(
    Math.min((container.width - inset) / width, (container.height - inset) / height),
    0.02,
    8,
  );
  return {
    scale,
    x: (container.width - width * scale) / 2 - minX * scale,
    y: (container.height - height * scale) / 2 - minY * scale,
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

export function regionBoxIou(left: RegionGeometry, right: RegionGeometry): number {
  const intersectionWidth = Math.max(
    0,
    Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x),
  );
  const intersectionHeight = Math.max(
    0,
    Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y),
  );
  const intersection = intersectionWidth * intersectionHeight;
  if (!intersection) return 0;
  const union = left.width * left.height + right.width * right.height - intersection;
  return union ? intersection / union : 0;
}

export function regionOverlapOfSmaller(left: RegionGeometry, right: RegionGeometry): number {
  const intersectionWidth = Math.max(
    0,
    Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x),
  );
  const intersectionHeight = Math.max(
    0,
    Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y),
  );
  const smaller = Math.min(left.width * left.height, right.width * right.height);
  return smaller ? (intersectionWidth * intersectionHeight) / smaller : 0;
}

function inferredDirection(region: RegionGeometry & { direction?: string }): 'horizontal' | 'vertical' {
  if (region.direction === 'horizontal' || region.direction === 'vertical') return region.direction;
  return region.height >= region.width ? 'vertical' : 'horizontal';
}

function regionGap(left: RegionGeometry, right: RegionGeometry): number {
  const dx = Math.max(0, left.x - (right.x + right.width), right.x - (left.x + left.width));
  const dy = Math.max(0, left.y - (right.y + right.height), right.y - (left.y + left.height));
  if (dx === 0 && dy === 0) return 0;
  return Math.hypot(dx, dy);
}

function regionsAligned(
  left: RegionGeometry,
  right: RegionGeometry,
  direction: 'horizontal' | 'vertical',
): boolean {
  const overlap = direction === 'vertical'
    ? Math.min(left.x + left.width, right.x + right.width) - Math.max(left.x, right.x)
    : Math.min(left.y + left.height, right.y + right.height) - Math.max(left.y, right.y);
  const span = direction === 'vertical'
    ? Math.min(left.width, right.width)
    : Math.min(left.height, right.height);
  return span > 0 && overlap >= span * 0.4;
}

export function shouldMergeRegionGeometries(
  left: RegionGeometry & { direction?: string },
  right: RegionGeometry & { direction?: string },
  bounds: { width: number; height: number },
): boolean {
  const pageArea = Math.max(1, bounds.width * bounds.height);
  const unionWidth = Math.max(left.x + left.width, right.x + right.width) - Math.min(left.x, right.x);
  const unionHeight = Math.max(left.y + left.height, right.y + right.height) - Math.min(left.y, right.y);
  if (regionBoxIou(left, right) >= 0.22 || regionOverlapOfSmaller(left, right) >= 0.5) {
    return unionWidth * unionHeight <= pageArea * 0.22;
  }
  if (inferredDirection(left) !== inferredDirection(right)) return false;
  const short = Math.min(left.width, left.height, right.width, right.height);
  if (regionGap(left, right) > Math.max(8, short * 0.35)) return false;
  if (!regionsAligned(left, right, inferredDirection(left))) return false;
  return unionWidth * unionHeight <= pageArea * 0.18;
}

export function expandRegionGeometry(
  region: RegionGeometry,
  bounds: { width: number; height: number },
  direction?: string,
): RegionGeometry {
  let padX = Math.min(28, Math.max(6, Math.round(region.width * 0.08)));
  let padY = Math.min(28, Math.max(6, Math.round(region.height * 0.08)));
  if (inferredDirection({ ...region, direction }) === 'vertical') padX = Math.min(32, Math.max(padX, 8));
  else padY = Math.min(32, Math.max(padY, 8));
  return clampRegionGeometry({
    x: region.x - padX,
    y: region.y - padY,
    width: region.width + padX * 2,
    height: region.height + padY * 2,
    rotation: region.rotation,
  }, bounds);
}

export function clusterRegionIds(
  regions: Array<RegionGeometry & { id: string; direction?: string }>,
  bounds: { width: number; height: number },
): string[][] {
  const parent = regions.map((_, index) => index);
  const find = (index: number): number => {
    while (parent[index] !== index) {
      parent[index] = parent[parent[index] ?? index] ?? index;
      index = parent[index] ?? index;
    }
    return index;
  };
  const union = (left: number, right: number) => {
    const rootLeft = find(left);
    const rootRight = find(right);
    if (rootLeft !== rootRight) parent[rootRight] = rootLeft;
  };
  for (let left = 0; left < regions.length; left += 1) {
    const current = regions[left];
    if (!current) continue;
    for (let right = left + 1; right < regions.length; right += 1) {
      const other = regions[right];
      if (!other) continue;
      if (shouldMergeRegionGeometries(current, other, bounds)) union(left, right);
    }
  }
  const clusters = new Map<number, string[]>();
  const order: number[] = [];
  regions.forEach((region, index) => {
    const root = find(index);
    if (!clusters.has(root)) {
      clusters.set(root, []);
      order.push(root);
    }
    clusters.get(root)?.push(region.id);
  });
  return order.map((root) => clusters.get(root) ?? []);
}

export function clampRegionGeometry(
  geometry: RegionGeometry,
  bounds: { width: number; height: number },
): RegionGeometry {
  const width = Math.min(bounds.width, Math.max(4, Math.round(Math.abs(geometry.width))));
  const height = Math.min(bounds.height, Math.max(4, Math.round(Math.abs(geometry.height))));
  return {
    x: Math.round(clamp(geometry.x, 0, Math.max(0, bounds.width - width))),
    y: Math.round(clamp(geometry.y, 0, Math.max(0, bounds.height - height))),
    width,
    height,
    rotation: Math.round(geometry.rotation * 10) / 10,
  };
}

export function centeredNodeToRegionGeometry(
  node: CenteredRegionNodeGeometry,
  bounds: { width: number; height: number },
): RegionGeometry {
  const width = Math.abs(node.width * node.scaleX);
  const height = Math.abs(node.height * node.scaleY);
  return clampRegionGeometry(
    {
      x: node.x - width / 2,
      y: node.y - height / 2,
      width,
      height,
      rotation: node.rotation,
    },
    bounds,
  );
}

export function isKonvaRegionEditTarget(target: {
  name?: () => string;
  getClassName?: () => string;
  getParent?: () => unknown;
} | null | undefined): boolean {
  let node = target ?? null;
  for (let depth = 0; node && depth < 8; depth += 1) {
    const name = typeof node.name === 'function' ? node.name() : '';
    const className = typeof node.getClassName === 'function' ? node.getClassName() : '';
    if (name === 'region' || className === 'Transformer') return true;
    const parent = typeof node.getParent === 'function' ? node.getParent() : null;
    node = parent && typeof parent === 'object' ? parent as typeof node : null;
  }
  return false;
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
