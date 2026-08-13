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
): Promise<LoadedCanvasImage> {
  if (typeof globalThis.createImageBitmap !== 'function') {
    throw new Error('当前浏览器不支持原始像素方向解码');
  }
  const response = await fetch(src, { signal });
  if (!response.ok) throw new Error(`Image request failed (${response.status})`);
  const bytes = await response.arrayBuffer();
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
