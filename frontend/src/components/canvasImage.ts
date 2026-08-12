export interface CanonicalImageSize {
  width: number;
  height: number;
}

export async function loadCanonicalCanvasImage(
  src: string,
  expectedSize: CanonicalImageSize,
  signal?: AbortSignal,
): Promise<ImageBitmap> {
  if (typeof globalThis.createImageBitmap !== 'function') {
    throw new Error('当前浏览器不支持原始像素方向解码');
  }
  const response = await fetch(src, { signal });
  if (!response.ok) throw new Error(`Image request failed (${response.status})`);
  const bitmap = await globalThis.createImageBitmap(await response.blob(), {
    // Backend regions, masks, and image dimensions all use the immutable file's raw pixel grid.
    imageOrientation: 'none',
  });
  if (bitmap.width !== expectedSize.width || bitmap.height !== expectedSize.height) {
    bitmap.close();
    throw new Error(
      `Decoded image grid ${bitmap.width}×${bitmap.height} does not match canonical grid ${expectedSize.width}×${expectedSize.height}`,
    );
  }
  return bitmap;
}
