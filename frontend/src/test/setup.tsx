import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { createElement, forwardRef, useImperativeHandle } from 'react';
import { afterEach, vi } from 'vitest';

afterEach(() => cleanup());

class TestResizeObserver implements ResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

Object.defineProperty(window, 'ResizeObserver', { configurable: true, value: TestResizeObserver });
const localStorageMemory = new Map<string, string>();
Object.defineProperty(window, 'localStorage', {
  configurable: true,
  value: {
    getItem: (key: string) => localStorageMemory.get(key) ?? null,
    setItem: (key: string, value: string) => localStorageMemory.set(key, value),
    removeItem: (key: string) => localStorageMemory.delete(key),
    clear: () => localStorageMemory.clear(),
    key: (index: number) => [...localStorageMemory.keys()][index] ?? null,
    get length() { return localStorageMemory.size; },
  },
});
Object.defineProperty(window, 'matchMedia', {
  configurable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })),
});

vi.mock('react-konva', () => {
  const container = (name: string) => ({ children }: { children?: React.ReactNode }) =>
    createElement('div', { 'data-konva': name }, children);

  const Stage = ({ children }: { children?: React.ReactNode }) =>
    createElement('div', { 'data-testid': 'konva-stage' }, children);

  const Rect = forwardRef<unknown, Record<string, unknown>>((props, ref) => {
    useImperativeHandle(ref, () => {
      let x = Number(props.x ?? 0);
      let y = Number(props.y ?? 0);
      let rotation = Number(props.rotation ?? 0);
      return {
        x(value?: number) { if (value !== undefined) x = value; return x; },
        y(value?: number) { if (value !== undefined) y = value; return y; },
        rotation(value?: number) { if (value !== undefined) rotation = value; return rotation; },
        scaleX() { return 1; },
        scaleY() { return 1; },
      };
    }, [props.rotation, props.x, props.y]);
    return createElement('div', { 'data-konva': 'Rect' });
  });

  const Transformer = forwardRef<unknown>((_props, ref) => {
    useImperativeHandle(ref, () => ({
      nodes() {},
      getLayer() { return { batchDraw() {} }; },
    }), []);
    return null;
  });

  const Text = ({ text }: { text?: string }) => createElement('span', null, text);
  const Image = () => createElement('div', { 'data-konva': 'Image' });
  const Tag = () => null;
  return {
    Stage,
    Layer: container('Layer'),
    Group: container('Group'),
    Label: container('Label'),
    Line: container('Line'),
    Rect,
    Transformer,
    Text,
    Image,
    Tag,
  };
});
