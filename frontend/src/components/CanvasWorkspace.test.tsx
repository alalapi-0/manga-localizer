import { act, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { resetWorkbenchStore, useWorkbenchStore } from '../store/workbench';
import { imageFixture, seedWorkbench } from '../test/fixtures';
import { CanvasWorkspace } from './CanvasWorkspace';

const NativeImage = window.Image;

describe('canvas generated-image refresh', () => {
  afterEach(() => {
    Object.defineProperty(window, 'Image', {
      configurable: true,
      value: NativeImage,
    });
    resetWorkbenchStore();
    vi.restoreAllMocks();
  });

  it('retries a failed generated preview and cache-busts after the image revision changes', async () => {
    const created: Array<{
      src: string;
      onload: (() => void) | null;
      onerror: (() => void) | null;
    }> = [];
    class ImageMock {
      src = '';
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;

      constructor() {
        created.push(this);
      }
    }
    Object.defineProperty(window, 'Image', {
      configurable: true,
      value: ImageMock,
    });
    const initial = imageFixture('image-1', { revision: 4 });
    seedWorkbench({ images: [initial] });
    useWorkbenchStore.setState({ canvasMode: 'typeset' });
    render(<CanvasWorkspace />);

    await waitFor(() => expect(created[0]?.src).toContain('typeset?v=4'));
    act(() => created[0]?.onerror?.());
    expect(screen.getByRole('alert')).toHaveTextContent('图像读取失败');

    useWorkbenchStore.setState({
      images: [imageFixture('image-1', { revision: 5 })],
    });
    await waitFor(() => expect(created[1]?.src).toContain('typeset?v=5'));
    act(() => created[1]?.onload?.());
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  });
});
