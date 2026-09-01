export interface CanonicalImageSize {
  width: number;
  height: number;
}

export interface LoadedCanvasImage {
  image: ImageBitmap;
  checksum: string;
  pixelWidth: number;
  pixelHeight: number;
}

export type RequiredImageFormat = 'png';

const PNG_SIGNATURE = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function requireImageFormat(
  bytes: ArrayBuffer,
  contentType: string | null,
  requiredFormat?: RequiredImageFormat,
): void {
  if (!requiredFormat) return;
  const mediaType = contentType?.split(';', 1)[0]?.trim().toLowerCase();
  const payload = new Uint8Array(bytes);
  if (requiredFormat === 'png' && (
    mediaType !== 'image/png'
    || payload.length < PNG_SIGNATURE.length
    || PNG_SIGNATURE.some((byte, index) => payload[index] !== byte)
  )) throw new Error('Actual mask is not a canonical PNG payload');
}

function checksumHex(bytes: ArrayBuffer): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new Error('当前浏览器不支持复核文件校验');
  return subtle.digest('SHA-256', bytes).then((digest) =>
    Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, '0')).join(''),
  );
}

function isSupportedGrid(
  width: number,
  height: number,
  expectedSize: CanonicalImageSize,
  allowCanonicalScale: boolean,
): boolean {
  if (width === expectedSize.width && height === expectedSize.height) return true;
  if (!allowCanonicalScale || expectedSize.width <= 0 || expectedSize.height <= 0) return false;
  const scale = width / expectedSize.width;
  return Number.isInteger(scale)
    && scale >= 1
    && scale <= 4
    && height === expectedSize.height * scale;
}

export async function loadCanonicalCanvasImage(
  src: string,
  expectedSize: CanonicalImageSize,
  signal?: AbortSignal,
  allowCanonicalScale = false,
  requiredFormat?: RequiredImageFormat,
): Promise<LoadedCanvasImage> {
  if (typeof globalThis.createImageBitmap !== 'function') {
    throw new Error('当前浏览器不支持原始像素方向解码');
  }
  const response = await fetch(src, { signal, cache: 'no-store' });
  if (!response.ok) throw new Error(`Image request failed (${response.status})`);
  const bytes = await response.arrayBuffer();
  requireImageFormat(bytes, response.headers.get('content-type'), requiredFormat);
  const blob = new Blob([bytes], {
    type: response.headers.get('content-type') || 'application/octet-stream',
  });
  const checksum = await checksumHex(bytes);
  const bitmap = await globalThis.createImageBitmap(blob, {
    // Backend regions, masks, and image dimensions all use the immutable file's raw pixel grid.
    imageOrientation: 'none',
  });
  if (!isSupportedGrid(bitmap.width, bitmap.height, expectedSize, allowCanonicalScale)) {
    bitmap.close();
    throw new Error(
      `Decoded image grid ${bitmap.width}×${bitmap.height} does not match canonical grid ${expectedSize.width}×${expectedSize.height}`,
    );
  }
  return {
    image: bitmap,
    checksum,
    pixelWidth: bitmap.width,
    pixelHeight: bitmap.height,
  };
}
