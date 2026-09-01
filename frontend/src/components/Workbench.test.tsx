import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../App';
import { ApiError, api } from '../api/client';
import { OCR_QC_CHECKS } from '../types';
import type { OCRAttempt, PageGeneration, PageLineageEvent } from '../types';
import {
  capabilitiesFixture,
  imageFixture,
  jobFixture,
  projectFixture,
  regionFixture,
  seedWorkbench,
} from '../test/fixtures';
import { resetWorkbenchStore, useWorkbenchStore } from '../store/workbench';

function activeG4Generation(nextSequence = 8): PageGeneration {
  return {
    id: 'generation-1',
    runId: 'run-1',
    projectId: 'project-1',
    imageId: 'image-1',
    restartFromSource: true,
    parameterSetId: 'params-1',
    parameterSetHash: 'a'.repeat(64),
    sourceProjectId: 'project-1',
    sourceImageId: 'image-1',
    sourceChecksum: 'b'.repeat(64),
    state: 'active',
    nextSequence,
    actor: { actorKind: 'codex', taskId: 'task-1', operationSource: 'api' },
    createdAt: '2026-08-25T00:00:00Z',
    closedAt: null,
  };
}

function activeG4Event(sequence = 7): PageLineageEvent {
  return {
    id: `event-${sequence}`,
    generationId: 'generation-1',
    sequence,
    operation: 'detect-job-completed',
    gate: 'G4_regions',
    state: 'pending',
    actor: { actorKind: 'system', actorId: 'queue', operationSource: 'api' },
    inputChecksum: 'c'.repeat(64),
    outputChecksum: 'd'.repeat(64),
    parentChecksum: 'c'.repeat(64),
    stage: 'detection',
    provider: 'tesseract',
    modelVersion: null,
    parameterHash: 'a'.repeat(64),
    jobId: 'job-detect',
    jobItemId: 'item-detect',
    revisionId: null,
    decision: 'job-completed',
    reason: 'job-completed',
    gitCommit: null,
    evidence: { targetKind: 'region-set' },
    startedAt: '2026-08-25T00:00:00Z',
    finishedAt: '2026-08-25T00:00:01Z',
    createdAt: '2026-08-25T00:00:01Z',
  };
}

function ocrAttemptFixture(
  inputVariant: OCRAttempt['inputVariant'],
  confidence: number,
): OCRAttempt {
  return {
    id: `attempt-${inputVariant}`,
    regionId: 'region-1',
    generationId: 'generation-1',
    jobId: 'job-ocr',
    jobItemId: 'item-ocr',
    inputVariant,
    parentChecksum: 'f'.repeat(64),
    cropChecksum: (inputVariant === 'original' ? '1' : '2').repeat(64),
    cropBox: { x: 10, y: 20, width: 80, height: 40 },
    provider: 'tesseract',
    modelVersion: 'tesseract-5',
    parameterHash: '3'.repeat(64),
    language: 'ja',
    direction: 'vertical',
    text: inputVariant === 'original' ? '原図の文' : '品質の文',
    textChecksum: (inputVariant === 'original' ? '4' : '5').repeat(64),
    confidence,
    createdAt: '2026-08-25T00:00:02Z',
  };
}

const ocrQCCheckLabelsForTest = {
  'original-and-quality-compared': '已对照原图与增强图 OCR',
  'source-text-characters-checked': '已逐字核对日文原文',
  'punctuation-checked': '已核对标点与符号',
  'direction-checked': '已核对横排 / 竖排方向',
  'reading-order-checked': '已核对阅读顺序',
  'empty-or-garbled-checked': '已排除空文本与乱码',
  'duplicate-fragment-checked': '已排除重复片段',
  'template-contamination-checked': '已排除模板污染',
  'page-text-consistency-checked': '已核对本页文本一致性',
} as const;

describe('desktop workbench interactions', () => {
  afterEach(() => {
    cleanup();
    resetWorkbenchStore();
    vi.restoreAllMocks();
  });

  it('lists, searches, filters, batch-selects, and opens an image', async () => {
    const user = userEvent.setup();
    const second = imageFixture('image-2', {
      status: { ...imageFixture('image-2').status, ocr: 'done' },
    });
    seedWorkbench({ images: [imageFixture('image-1'), second] });
    render(<App />);

    expect(screen.getByText('image-1.png')).toBeInTheDocument();
    expect(screen.getByText('待确认无文字')).toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: '批选 image-2.png' }));
    expect(useWorkbenchStore.getState().selectedImageIds).toEqual(['image-1', 'image-2']);

    await user.type(screen.getByRole('searchbox', { name: '搜索图像路径' }), '第二话');
    expect(screen.queryByText('image-1.png')).not.toBeInTheDocument();
    expect(screen.getByText('image-2.png')).toBeInTheDocument();

    await user.click(screen.getByText('image-2.png'));
    expect(useWorkbenchStore.getState().activeImageId).toBe('image-2');
  });

  it('disables every image import entry when project lineage is active or unresolved', () => {
    seedWorkbench();
    useWorkbenchStore.setState((state) => ({
      g4Contexts: {
        ...state.g4Contexts,
        'image-1': {
          status: 'active',
          generation: activeG4Generation(),
          events: [activeG4Event()],
          error: '',
          conflict: false,
        },
        'image-2': {
          status: 'error',
          generation: null,
          events: [],
          error: 'lineage unavailable',
          conflict: false,
        },
      },
    }));
    render(<App />);

    expect(screen.getByRole('button', { name: '单图' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '多图' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '文件夹' })).toBeDisabled();
    expect(screen.getByText('项目页面已有或尚未核清血缘，图像导入已锁定。')).toBeInTheDocument();
  });

  it('opens the matching inspector when clicking a failed sidebar page', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, inpaint: 'failed' },
          processingErrors: [{ stage: 'inpaint', error: 'Image rendering failed; inspect the private project log' }],
        }),
      ],
    });
    useWorkbenchStore.setState({ rightTab: 'text' });
    render(<App />);

    await user.click(screen.getByText('image-2.png'));
    await waitFor(() => {
      expect(useWorkbenchStore.getState()).toMatchObject({
        activeImageId: 'image-2',
        rightTab: 'repair',
      });
    });
    expect(screen.getByRole('tab', { name: '修复' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('擦字修复失败')).toBeInTheDocument();
  });

  it('jumps to the next failed page with Option+ArrowRight', async () => {
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, inpaint: 'failed' },
          processingErrors: [{ stage: 'inpaint', error: 'Image rendering failed; inspect the private project log' }],
        }),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: { ...imageFixture('image-3').status, ocr: 'failed' },
          processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
        }),
      ],
    });
    useWorkbenchStore.setState({ rightTab: 'project' });
    vi.spyOn(api, 'listRegions').mockResolvedValue([]);
    render(<App />);

    fireEvent.keyDown(window, { key: 'ArrowRight', altKey: true });
    await waitFor(() => {
      expect(useWorkbenchStore.getState()).toMatchObject({
        activeImageId: 'image-2',
        rightTab: 'repair',
      });
    });
    expect(screen.getByRole('tab', { name: '修复' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('擦字修复失败')).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'ArrowRight', altKey: true });
    await waitFor(() => {
      expect(useWorkbenchStore.getState()).toMatchObject({
        activeImageId: 'image-3',
        rightTab: 'text',
      });
    });
    expect(screen.getByRole('tab', { name: '文本' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('日文 OCR 失败')).toBeInTheDocument();
  });

  it('filters overflowing pages and shows a page-level overflow warning', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-1'],
        }),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, ocr: 'done', typeset: 'done' },
        }),
      ],
    });
    render(<App />);

    expect(screen.getByText('1 个文本框排版溢出')).toBeInTheDocument();
    expect(screen.getByText('排版溢出 1')).toBeInTheDocument();

    await user.selectOptions(screen.getByRole('combobox', { name: '按状态筛选' }), 'overflow');
    expect(screen.getByText('image-1.png')).toBeInTheDocument();
    expect(screen.queryByText('image-2.png')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '下一张排版溢出' })).toBeEnabled();

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    await user.click(screen.getByRole('checkbox', { name: /安全导出/ }));
    expect(screen.getByText('还有 1 页排版溢出')).toBeInTheDocument();
  });

  it('switches compact workbench panes used on phone-sized layouts', () => {
    seedWorkbench();
    render(<App />);

    const pages = screen.getByRole('button', { name: '图像', hidden: true });
    fireEvent.click(pages);
    expect(pages).toHaveAttribute('aria-pressed', 'true');
    expect(document.querySelector('.workbench-grid')).toHaveAttribute('data-shell-pane', 'pages');
    fireEvent.click(screen.getByRole('button', { name: '检查', hidden: true }));
    expect(document.querySelector('.workbench-grid')).toHaveAttribute('data-shell-pane', 'inspect');
  });

  it('opens batch processing from the compact phone panes', () => {
    seedWorkbench();
    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: '打开批处理与导出', hidden: true }));
    expect(screen.getByRole('dialog', { name: '批处理与导出' })).toBeInTheDocument();
  });

  it('opens create-project from the empty sidebar', async () => {
    const user = userEvent.setup();
    resetWorkbenchStore();
    useWorkbenchStore.setState({
      loadState: 'ready',
      capabilities: capabilitiesFixture(),
      projects: [],
      currentProject: null,
      images: [],
    });
    render(<App />);

    const sidebar = document.querySelector('.image-tree');
    expect(sidebar).not.toBeNull();
    await user.click(within(sidebar as HTMLElement).getByRole('button', { name: '创建本机项目' }));
    expect(screen.getByRole('dialog', { name: '新建本地项目' })).toBeInTheDocument();
  });

  it('opens create-project from the empty canvas', async () => {
    const user = userEvent.setup();
    resetWorkbenchStore();
    useWorkbenchStore.setState({
      loadState: 'ready',
      capabilities: capabilitiesFixture(),
      projects: [],
      currentProject: null,
      images: [],
    });
    render(<App />);

    const canvas = document.querySelector('.canvas-panel');
    expect(canvas).not.toBeNull();
    await user.click(within(canvas as HTMLElement).getByRole('button', { name: '创建本机项目' }));
    expect(screen.getByRole('dialog', { name: '新建本地项目' })).toBeInTheDocument();
  });

  it('opens create-project from the empty inspector pane', async () => {
    const user = userEvent.setup();
    resetWorkbenchStore();
    useWorkbenchStore.setState({
      loadState: 'ready',
      capabilities: capabilitiesFixture(),
      projects: [],
      currentProject: null,
      images: [],
    });
    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: '检查', hidden: true }));
    const inspector = document.querySelector('.inspector');
    expect(inspector).not.toBeNull();
    await user.click(within(inspector as HTMLElement).getByRole('button', { name: '创建本机项目' }));
    expect(screen.getByRole('dialog', { name: '新建本地项目' })).toBeInTheDocument();
  });

  it('opens photo import from the empty canvas', async () => {
    const user = userEvent.setup();
    seedWorkbench({ images: [] });
    const { container } = render(<App />);

    const canvas = document.querySelector('.canvas-panel');
    expect(canvas).not.toBeNull();
    const multiInput = container.querySelector<HTMLInputElement>('input[type="file"][multiple]:not([webkitdirectory])');
    expect(multiInput).not.toBeNull();
    const click = vi.spyOn(multiInput as HTMLInputElement, 'click');
    await user.click(within(canvas as HTMLElement).getByRole('button', { name: '从相册导入' }));
    expect(click).toHaveBeenCalledOnce();
  });

  it('opens photo import from the empty inspector pane', async () => {
    const user = userEvent.setup();
    seedWorkbench({ images: [] });
    const { container } = render(<App />);

    fireEvent.click(screen.getByRole('button', { name: '检查', hidden: true }));
    const inspector = document.querySelector('.inspector');
    expect(inspector).not.toBeNull();
    const multiInput = container.querySelector<HTMLInputElement>('input[type="file"][multiple]:not([webkitdirectory])');
    expect(multiInput).not.toBeNull();
    const click = vi.spyOn(multiInput as HTMLInputElement, 'click');
    await user.click(within(inspector as HTMLElement).getByRole('button', { name: '从相册导入' }));
    expect(click).toHaveBeenCalledOnce();
  });

  it('keeps project settings available before images are imported', () => {
    seedWorkbench({ images: [] });
    useWorkbenchStore.setState({ rightTab: 'project' });
    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: '检查', hidden: true }));
    const inspector = document.querySelector('.inspector');
    expect(inspector).not.toBeNull();
    expect(within(inspector as HTMLElement).getByRole('tab', { name: '项目' })).toHaveAttribute('aria-selected', 'true');
    expect(within(inspector as HTMLElement).getByRole('combobox', { name: '翻译' })).toBeInTheDocument();
  });

  it('shows the same-LAN companion URL when the Mac app exposes one', async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText },
    });
    seedWorkbench();
    useWorkbenchStore.setState({
      capabilities: {
        ...capabilitiesFixture(),
        system: { companionUrl: 'http://192.168.1.20:8000', lanAccess: true },
      },
    });
    render(<App />);

    expect(screen.getByRole('status', { name: '手机入口' })).toHaveTextContent(
      '同一 Wi-Fi 的手机请在 Safari 打开 http://192.168.1.20:8000，用「多图」从相册导入。',
    );
    await user.click(screen.getByRole('button', { name: '复制手机入口地址' }));
    expect(writeText).toHaveBeenCalledWith('http://192.168.1.20:8000');
    expect(screen.getByRole('button', { name: '复制手机入口地址' })).toHaveTextContent('已复制');
  });

  it('frames overflow boxes from the sidebar overflow pill', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-1'],
        }),
        imageFixture('image-2'),
      ],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '框住 image-1.png 的排版溢出' }));
    await waitFor(() => {
      expect(useWorkbenchStore.getState()).toMatchObject({
        activeImageId: 'image-1',
        selectedRegionIds: ['region-1'],
        rightTab: 'typesetting',
        canvasMode: 'typeset',
        focusRegionIds: ['region-1'],
      });
    });
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
  });

  it('retries a page processing failure from the inspector without opening the batch drawer', async () => {
    const user = userEvent.setup();
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-ocr-retry',
      kind: 'ocr',
      status: 'queued',
    }));
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture(),
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images,
    );
    vi.spyOn(api, 'listJobs').mockImplementation(async () =>
      useWorkbenchStore.getState().jobs,
    );
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, ocr: 'failed' },
          error: 'OCR failed; inspect the private project log',
          processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
        }),
        imageFixture('image-2'),
      ],
    });
    render(<App />);

    expect(screen.getByText('日文 OCR 失败')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重试本页 OCR' }));
    await waitFor(() => expect(startJob).toHaveBeenCalled());
    expect(startJob).toHaveBeenCalledWith('project-1', 'ocr', expect.objectContaining({
      imageIds: ['image-1'],
    }));
    expect(useWorkbenchStore.getState().drawerOpen).toBe(false);
    expect(screen.queryByRole('dialog', { name: '批处理与导出' })).not.toBeInTheDocument();
    await waitFor(() => {
      expect(useWorkbenchStore.getState().images[0]?.status.ocr).toBe('queued');
      expect(screen.queryByText('日文 OCR 失败')).not.toBeInTheDocument();
      expect(screen.getByText('日文 OCR 排队中')).toBeInTheDocument();
    });
    expect(screen.getByText('本页已重新排队，不必打开批处理抽屉；完成后检查器会更新。')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看队列' }));
    expect(useWorkbenchStore.getState().drawerOpen).toBe(true);
    expect(screen.getByRole('dialog', { name: '批处理与导出' })).toBeInTheDocument();
    expect(screen.getByRole('article', { current: true })).toHaveTextContent('日文 OCR');
  });

  it('does not offer a legacy processing retry while page lineage is unavailable', () => {
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, ocr: 'failed' },
          error: 'OCR failed',
          processingErrors: [{ stage: 'ocr', error: 'OCR failed' }],
        }),
      ],
    });
    useWorkbenchStore.setState({
      g4Contexts: {
        'image-1': {
          status: 'error',
          generation: null,
          events: [],
          error: 'lineage unavailable',
          conflict: false,
        },
      },
    });
    render(<App />);

    expect(screen.getByText('日文 OCR 失败')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重试本页 OCR' })).not.toBeInTheDocument();
  });

  it('opens the matching queue job from a page processing failure notice', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, ocr: 'failed' },
          error: 'OCR failed; inspect the private project log',
          processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
        }),
        imageFixture('image-2'),
      ],
    });
    useWorkbenchStore.setState({
      jobs: [
        jobFixture({
          id: 'job-ocr-failed',
          kind: 'ocr',
          status: 'failed',
          items: [{
            id: 'item-ocr-1',
            imageId: 'image-1',
            label: '第一话/image-1.png',
            status: 'failed',
            progress: 0,
          }],
        }),
      ],
    });
    render(<App />);

    expect(screen.getByText('日文 OCR 失败')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '查看队列' }));
    expect(useWorkbenchStore.getState()).toMatchObject({
      drawerOpen: true,
      queueRevealJobId: 'job-ocr-failed',
      queueRevealItemId: 'item-ocr-1',
    });
    expect(screen.getByRole('article', { current: true })).toHaveTextContent('日文 OCR');
    expect(screen.getByRole('button', { name: '打开队列项 第一话/image-1.png' })).toHaveAttribute('aria-current', 'true');
  });

  it('keeps the retried page in the failed sidebar until you leave it', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-ocr-retry-filter',
      kind: 'ocr',
      status: 'queued',
    }));
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture(),
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images,
    );
    vi.spyOn(api, 'listJobs').mockImplementation(async () =>
      useWorkbenchStore.getState().jobs,
    );
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, ocr: 'failed' },
          error: 'OCR failed; inspect the private project log',
          processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
        }),
        imageFixture('image-2'),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: { ...imageFixture('image-3').status, inpaint: 'failed' },
          processingErrors: [{ stage: 'inpaint', error: 'Image rendering failed; inspect the private project log' }],
        }),
      ],
    });
    useWorkbenchStore.setState({ imageFilter: 'failed', rightTab: 'text' });
    render(<App />);

    expect(screen.queryByText('image-2.png')).not.toBeInTheDocument();
    expect(screen.getByLabelText('可见列表 1 / 2')).toHaveTextContent('1 / 2');
    await user.click(screen.getByRole('button', { name: '重试本页 OCR' }));
    await waitFor(() => {
      expect(useWorkbenchStore.getState().images[0]?.status.ocr).toBe('queued');
      expect(screen.getByText('日文 OCR 排队中')).toBeInTheDocument();
    });
    expect(screen.getByText('image-1.png')).toBeInTheDocument();
    expect(screen.getByText('image-3.png')).toBeInTheDocument();
    expect(screen.getByLabelText('可见列表 1 / 2')).toHaveTextContent('1 / 2');

    await user.click(screen.getByRole('button', { name: '下一张图' }));
    await waitFor(() => {
      expect(useWorkbenchStore.getState().activeImageId).toBe('image-3');
    });
    expect(screen.queryByText('image-1.png')).not.toBeInTheDocument();
    expect(screen.getByText('image-3.png')).toBeInTheDocument();
    expect(screen.getByLabelText('可见列表 1 / 1')).toHaveTextContent('1 / 1');
  });

  it('frames a box from the inspector region list', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    render(<App />);

    await user.click(screen.getByRole('button', { name: '选择文本框 #2' }));
    expect(useWorkbenchStore.getState()).toMatchObject({
      selectedRegionIds: ['region-2'],
      focusRegionIds: ['region-2'],
    });
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
  });

  it('frames the selection from G and the canvas toolbar', async () => {
    const user = userEvent.setup();
    seedWorkbench({ selectedRegionIds: ['region-2'] });
    render(<App />);

    fireEvent.keyDown(window, { key: 'g' });
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual(['region-2']);
    const firstFocus = useWorkbenchStore.getState().focusRequest;
    expect(firstFocus).toBeGreaterThan(0);

    fireEvent.keyDown(window, { key: 'f' });
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual([]);

    await user.click(screen.getByRole('button', { name: '框住所选' }));
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual(['region-2']);
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(firstFocus);
  });

  it('skips hidden pages when using next-image under the overflow filter', async () => {
    const user = userEvent.setup();
    const overflow = regionFixture('region-9', { imageId: 'image-3' });
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-1'],
        }),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, typeset: 'done' },
        }),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-9'],
        }),
      ],
    });
    useWorkbenchStore.setState((state) => ({
      imageFilter: 'overflow',
      regionsByImage: { ...state.regionsByImage, 'image-3': [overflow] },
    }));
    render(<App />);

    expect(screen.queryByText('image-2.png')).not.toBeInTheDocument();
    expect(screen.getByLabelText('可见列表 1 / 2')).toHaveTextContent('1 / 2');
    expect(screen.getByRole('button', { name: '上一张图' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '下一张图' }));
    await waitFor(() => {
      expect(useWorkbenchStore.getState()).toMatchObject({
        activeImageId: 'image-3',
        selectedRegionIds: ['region-9'],
        focusRegionIds: ['region-9'],
        canvasMode: 'typeset',
      });
    });
    expect(screen.getByLabelText('可见列表 2 / 2')).toHaveTextContent('2 / 2');
    expect(screen.getByRole('button', { name: '下一张图' })).toBeDisabled();
  });

  it('opens the matching inspector when using next-image under the failed filter', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, ocr: 'failed' },
          processingErrors: [{ stage: 'ocr', error: 'OCR failed; inspect the private project log' }],
        }),
        imageFixture('image-2'),
        imageFixture('image-3', {
          name: 'image-3.png',
          relativePath: '第三话/image-3.png',
          status: { ...imageFixture('image-3').status, inpaint: 'failed' },
          processingErrors: [{ stage: 'inpaint', error: 'Image rendering failed; inspect the private project log' }],
        }),
      ],
    });
    useWorkbenchStore.setState({ imageFilter: 'failed', rightTab: 'text' });
    render(<App />);

    expect(screen.queryByText('image-2.png')).not.toBeInTheDocument();
    expect(screen.getByText('日文 OCR 失败')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '下一张图' }));
    await waitFor(() => {
      expect(useWorkbenchStore.getState()).toMatchObject({
        activeImageId: 'image-3',
        rightTab: 'repair',
      });
    });
    expect(screen.getByRole('tab', { name: '修复' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('擦字修复失败')).toBeInTheDocument();
  });

  it('selects overflowing boxes and queues typesetting for those region ids only', async () => {
    const user = userEvent.setup();
    const overflowing = regionFixture('region-1', {
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      confirmed: true,
    });
    const other = regionFixture('region-2', {
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      confirmed: true,
    });
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-1'],
          trustReviewCount: 0,
          trustedCount: 2,
        }),
      ],
      regions: [overflowing, other],
    });
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture(),
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images,
    );
    vi.spyOn(api, 'listRegions').mockResolvedValue([overflowing, other]);
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-overflow-typeset',
      kind: 'typeset',
    }));
    render(<App />);

    await user.click(screen.getByRole('button', { name: '选中溢出框' }));
    expect(useWorkbenchStore.getState().selectedRegionIds).toEqual(['region-1']);
    expect(useWorkbenchStore.getState().rightTab).toBe('typesetting');
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual(['region-1']);
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);

    await user.click(screen.getByRole('button', { name: '只重排溢出框' }));
    expect(startJob).toHaveBeenCalledWith('project-1', 'typeset', {
      imageIds: ['image-1'],
      regionIds: ['region-1'],
      options: expect.objectContaining({ provider: 'pillow', concurrency: 1 }),
    });
  });

  it('queues typesetting for the selected region from the typesetting inspector', async () => {
    const user = userEvent.setup();
    const region = regionFixture('region-1', {
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      confirmed: true,
    });
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 0, trustedCount: 1 })],
      regions: [region],
      selectedRegionIds: ['region-1'],
    });
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture(),
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images,
    );
    vi.spyOn(api, 'listRegions').mockResolvedValue([region]);
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-current-typeset',
      kind: 'typeset',
    }));
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '排版' }));
    await user.click(screen.getByRole('button', { name: '重排当前框' }));
    expect(startJob).toHaveBeenCalledWith('project-1', 'typeset', {
      imageIds: ['image-1'],
      regionIds: ['region-1'],
      options: expect.objectContaining({ provider: 'pillow', concurrency: 1 }),
    });
  });

  it('queues typesetting for the selected region with the T shortcut', async () => {
    const region = regionFixture('region-1', {
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      confirmed: true,
    });
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 0, trustedCount: 1 })],
      regions: [region],
      selectedRegionIds: ['region-1'],
    });
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture(),
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images,
    );
    vi.spyOn(api, 'listRegions').mockResolvedValue([region]);
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-shortcut-typeset',
      kind: 'typeset',
    }));
    render(<App />);

    fireEvent.keyDown(window, { key: 't' });
    await waitFor(() => expect(startJob).toHaveBeenCalledWith('project-1', 'typeset', {
      imageIds: ['image-1'],
      regionIds: ['region-1'],
      options: expect.objectContaining({ provider: 'pillow', concurrency: 1 }),
    }));
  });

  it('queues overflow-only typesetting with Shift+T', async () => {
    const overflowing = regionFixture('region-1', {
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      confirmed: true,
    });
    const other = regionFixture('region-2', {
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      confirmed: true,
    });
    seedWorkbench({
      images: [
        imageFixture('image-1', {
          status: { ...imageFixture('image-1').status, typeset: 'done' },
          typesetOverflowCount: 1,
          typesetOverflowRegionIds: ['region-1'],
          trustReviewCount: 0,
          trustedCount: 2,
        }),
      ],
      regions: [overflowing, other],
    });
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture(),
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images,
    );
    vi.spyOn(api, 'listRegions').mockResolvedValue([overflowing, other]);
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-shortcut-overflow',
      kind: 'typeset',
    }));
    render(<App />);

    fireEvent.keyDown(window, { key: 'T', shiftKey: true });
    await waitFor(() => expect(startJob).toHaveBeenCalledWith('project-1', 'typeset', {
      imageIds: ['image-1'],
      regionIds: ['region-1'],
      options: expect.objectContaining({ provider: 'pillow', concurrency: 1 }),
    }));
  });

  it('requires an explicit confirmation before a zero-region page is treated as reviewed', async () => {
    const user = userEvent.setup();
    const zeroText = imageFixture('image-1', {
      regionCount: 0,
      trustReviewCount: 0,
      status: {
        ...imageFixture('image-1').status,
        ocr: 'done',
        reviewState: 'pending',
      },
      revision: 7,
    });
    seedWorkbench({ images: [zeroText], regions: [] });
    const reviewImage = vi.spyOn(api, 'reviewImage').mockResolvedValue({
      ...zeroText,
      status: {
        ...zeroText.status,
        reviewState: 'no-text-reviewed',
        reviewedAt: '2026-08-10T10:00:00Z',
      },
      revision: 8,
    });
    render(<App />);

    expect(screen.getByText('待确认无文字')).toBeInTheDocument();
    expect(document.querySelector('.status-pill--no-text')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '确认本页无文字' }));

    await waitFor(() => expect(reviewImage).toHaveBeenCalledWith(
      'image-1',
      'no-text-reviewed',
      7,
    ));
    expect(document.querySelector('.status-pill--no-text')).toHaveTextContent('已确认无文字');
    expect(useWorkbenchStore.getState().images[0]?.status.reviewState).toBe('no-text-reviewed');
  });

  it('only enables page review after every active region is confirmed and trusted', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 2 })],
      regions: [
        regionFixture('region-1', { confirmed: false, trustDisposition: 'trusted' }),
        regionFixture('region-2', { confirmed: true, trustDisposition: 'review' }),
        regionFixture('region-3', { ignored: true }),
      ],
    });
    useWorkbenchStore.setState({ rightTab: 'project', selectedRegionIds: [] });
    render(<App />);

    expect(screen.getByText('还有 2 个活动文本框尚未确认并信任')).toBeInTheDocument();
    const focusUnready = screen.getByRole('button', { name: '还需确认并信任 2 个文本框' });
    expect(focusUnready).toBeEnabled();
    await user.click(focusUnready);
    expect(useWorkbenchStore.getState().rightTab).toBe('text');
    expect(useWorkbenchStore.getState().selectedRegionIds).toEqual(['region-1']);

    act(() => useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => image.id === 'image-1'
        ? { ...image, trustReviewCount: 0, trustedCount: 2 }
        : image),
      regionsByImage: {
        ...state.regionsByImage,
        'image-1': (state.regionsByImage['image-1'] ?? []).map((region) =>
          region.id === 'region-2'
            ? { ...region, trustDisposition: 'trusted', trustReason: 'human-confirmed' }
            : region.id === 'region-1'
              ? { ...region, confirmed: true }
              : region
        ),
      },
    })));

    expect(screen.getByRole('button', { name: '标记本页已检查' })).toBeEnabled();

    act(() => {
      useWorkbenchStore.getState().updateRegion('region-1', { ignored: true });
      useWorkbenchStore.getState().updateRegion('region-2', { ignored: true });
    });
    expect(screen.getByText('本页没有活动文本框，可确认无文字')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '确认本页无文字' })).toBeEnabled();
  });

  it('keeps page review disabled when the server trust aggregate is ahead of loaded regions', () => {
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 2 })],
      regions: [],
    });
    render(<App />);

    expect(screen.getByText('还有 2 个活动文本框尚未确认并信任')).toBeInTheDocument();
    expect(screen.getByRole('button', {
      name: '还需确认并信任 2 个文本框',
    })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '确认本页无文字' })).not.toBeInTheDocument();
  });

  it('keeps translation above helper notices when a box is selected', () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    render(<App />);

    const translation = screen.getByRole('textbox', { name: '中文译文' });
    const helper = screen.getByRole('button', { name: '整理本页选框' });
    expect(translation.compareDocumentPosition(helper) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('edits source, translation, type, direction, order, and keeps review flags exclusive', async () => {
    const user = userEvent.setup();
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    let revision = 4;
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture()
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images
    );
    vi.spyOn(api, 'updateRegion').mockImplementation(async (regionId, patch) => {
      revision += 1;
      const current = useWorkbenchStore.getState().regionsByImage['image-1']?.find(
        (region) => region.id === regionId,
      ) ?? regionFixture(regionId);
      return {
        ...current,
        ...patch,
        ...(patch.confirmed === true
          ? { trustDisposition: 'trusted' as const, trustReason: 'human-confirmed' }
          : {}),
        revision,
      };
    });
    render(<App />);

    const source = screen.getByRole('textbox', { name: '日文原文' });
    const translation = screen.getByRole('textbox', { name: '中文译文' });
    await user.clear(source);
    await user.type(source, '新しい台詞');
    await user.clear(translation);
    await user.type(translation, '新的对白');
    await user.selectOptions(screen.getByRole('combobox', { name: '文本类型' }), 'ruby');
    await user.selectOptions(screen.getByRole('combobox', { name: '文本方向' }), 'horizontal');
    await user.clear(screen.getByRole('spinbutton', { name: '选框 X' }));
    await user.type(screen.getByRole('spinbutton', { name: '选框 X' }), '140');
    await user.clear(screen.getByRole('spinbutton', { name: '选框宽度' }));
    await user.type(screen.getByRole('spinbutton', { name: '选框宽度' }), '260');
    await user.click(screen.getByRole('button', { name: '右移 1px' }));
    await user.clear(screen.getByRole('spinbutton', { name: '阅读顺序' }));
    await user.type(screen.getByRole('spinbutton', { name: '阅读顺序' }), '7');
    await user.click(screen.getByRole('checkbox', { name: /确认此文本框/ }));
    await waitFor(() => expect(
      useWorkbenchStore.getState().regionsByImage['image-1']?.[0],
    ).toMatchObject({ confirmed: true, ignored: false }));
    await user.click(screen.getByRole('checkbox', { name: /忽略此文本框/ }));

    expect(screen.getByText('图像处理会跳过；导出 JSON 仍保留此记录')).toBeInTheDocument();

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      sourceText: '新しい台詞',
      translationText: '新的对白',
      x: 141,
      width: 260,
      type: 'ruby',
      direction: 'horizontal',
      order: 7,
      confirmed: false,
      ignored: true,
      trustDisposition: 'ignored',
      trustReason: 'human-ignored',
    });
    const typeOptions = within(screen.getByRole('combobox', { name: '文本类型' }))
      .getAllByRole('option')
      .map((option) => option.getAttribute('value'));
    expect(typeOptions).toEqual(expect.arrayContaining([
      'dialogue', 'narration', 'sound_effect', 'title', 'ruby', 'background', 'unknown', 'speech',
    ]));
  });

  it('nudges the selected box from the inspector and modifier arrows', async () => {
    const user = userEvent.setup();
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '右移 1px' }));
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.x).toBe(101);

    fireEvent.keyDown(window, { key: 'ArrowDown', ctrlKey: true, shiftKey: true });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      x: 101,
      y: 130,
    });
  });

  it('presents OCR trust separately from page review and explains the batch gate accessibly', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 1 })],
      selectedRegionIds: ['region-1'],
      regions: [regionFixture('region-1', {
        confirmed: true,
        detectorConfidence: 0.41,
        ocrConfidence: 0.99,
        trustDisposition: 'review',
        trustReason: 'automatic-ocr-complete',
      })],
    });
    render(<App />);

    const trustStatus = screen.getByRole('region', { name: 'OCR 信任状态' });
    expect(trustStatus).toHaveTextContent('OCR 待信任');
    expect(trustStatus).toHaveTextContent('置信度不能代替人工确认');
    expect(trustStatus).toHaveTextContent('检测 41% · OCR 99%');
    expect(screen.getByRole('checkbox', { name: /确认此文本框/ })).not.toBeChecked();

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    const drawer = screen.getByRole('dialog', { name: '批处理与导出' });
    await user.click(within(drawer).getByRole('checkbox', { name: /日文 OCR/ }));
    await user.click(within(drawer).getByRole('checkbox', { name: /翻译/ }));
    const batchWarnings = within(drawer).getAllByRole('status')
      .map((notice) => notice.textContent)
      .join(' ');
    expect(batchWarnings).toContain('OCR 后必须先人工确认');
    expect(batchWarnings).toContain('1 个 OCR 文本框待信任确认');
    expect(within(drawer).getByRole('button', { name: /加入队列/ })).toBeDisabled();
  });

  it('reconfirms a stale confirmed flag in one click and checks the switch only after trust returns', async () => {
    const user = userEvent.setup();
    const stale = regionFixture('region-1', {
      confirmed: true,
      trustDisposition: 'review',
      trustReason: 'trust-input-changed',
    });
    seedWorkbench({ selectedRegionIds: ['region-1'], regions: [stale] });
    const update = vi.spyOn(api, 'updateRegion').mockResolvedValue({
      ...stale,
      trustDisposition: 'trusted',
      trustReason: 'human-confirmed',
      revision: 5,
    });
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture()
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images
    );
    render(<App />);

    const confirm = screen.getByRole('checkbox', { name: /确认此文本框/ });
    expect(confirm).not.toBeChecked();
    await user.click(confirm);

    await waitFor(() => expect(update).toHaveBeenCalledWith('region-1', {
      confirmed: true,
      expectedRevision: 4,
    }));
    await waitFor(() => expect(confirm).toBeChecked());
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      confirmed: true,
      trustDisposition: 'trusted',
    });
  });

  it('derives batch confirmation from trust and lets ignored state take precedence', () => {
    seedWorkbench({
      selectedRegionIds: ['region-1', 'region-2'],
      regions: [
        regionFixture('region-1', { confirmed: true, trustDisposition: 'review' }),
        regionFixture('region-2', { confirmed: true, trustDisposition: 'trusted' }),
      ],
    });
    const { rerender } = render(<App />);

    expect(screen.getByRole('checkbox', { name: '全部确认' })).not.toBeChecked();

    act(() => useWorkbenchStore.setState((state) => ({
      regionsByImage: {
        ...state.regionsByImage,
        'image-1': (state.regionsByImage['image-1'] ?? []).map((region) => ({
          ...region,
          trustDisposition: 'trusted',
          ...(region.id === 'region-2' ? { ignored: true } : {}),
        })),
      },
    })));
    rerender(<App />);

    expect(screen.getByRole('checkbox', { name: '全部确认' })).not.toBeChecked();
  });

  it('creates and deletes a real box from the canvas toolbar', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    render(<App />);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toHaveLength(2);

    await user.click(screen.getByRole('button', { name: '在中央快速新建文本框' }));
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toHaveLength(3);
    expect(useWorkbenchStore.getState().selectedRegionIds[0]).toMatch(/^local-/);

    await user.click(screen.getByRole('button', { name: '删除文本框' }));
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toHaveLength(2);
  });

  it('manually saves an edit and queues an export with safe options', async () => {
    const user = userEvent.setup();
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (_id, patch) => ({
      ...regionFixture('region-1'),
      ...patch,
      revision: 5,
    }));
    vi.spyOn(api, 'getProject').mockResolvedValue(projectFixture({ revision: 4 }));
    vi.spyOn(api, 'listImages').mockResolvedValue([
      imageFixture('image-1'),
      imageFixture('image-2'),
    ]);
    const exportProject = vi.spyOn(api, 'exportProject').mockResolvedValue(jobFixture());
    render(<App />);

    await user.type(screen.getByRole('textbox', { name: '中文译文' }), '！');
    await user.click(screen.getByRole('button', { name: /有未保存更改/ }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(screen.getByText(/已保存/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    await user.click(screen.getByRole('checkbox', { name: /文字检测/ }));
    await user.click(screen.getByRole('checkbox', { name: /日文 OCR/ }));
    await user.click(screen.getByRole('checkbox', { name: /安全导出/ }));
    await user.selectOptions(screen.getByRole('combobox', { name: '导出内容' }), 'json');
    expect(screen.getByText('仅写入文本元数据；不会复制图像，也不会创建可重开的项目快照。')).toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox', { name: '任务并发数' }), '4');
    await user.click(screen.getByRole('button', { name: /加入队列/ }));

    await waitFor(() => expect(exportProject).toHaveBeenCalledWith(
      'project-1',
      expect.objectContaining({
        imageIds: ['image-1'],
        options: expect.objectContaining({
          format: 'json',
          imageVariant: 'typeset',
          preserveTree: true,
          conflict: 'rename',
          concurrency: 1,
        }),
      }),
    ));
  });

  it('queues both the typeset page and clean background export variants', async () => {
    const user = userEvent.setup();
    const reviewed = imageFixture('image-1', {
      status: {
        ...imageFixture('image-1').status,
        inpaint: 'done',
        typeset: 'done',
        reviewState: 'reviewed',
      },
      stageReviews: {
        inpaint: { state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'a'.repeat(64) },
        typeset: { state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'a'.repeat(64) },
      },
    });
    seedWorkbench({ images: [reviewed] });
    vi.spyOn(api, 'getProject').mockResolvedValue(projectFixture({ revision: 4 }));
    vi.spyOn(api, 'listImages').mockResolvedValue([reviewed]);
    const exportProject = vi.spyOn(api, 'exportProject').mockResolvedValue(jobFixture());
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    await user.click(screen.getByRole('checkbox', { name: /文字检测/ }));
    await user.click(screen.getByRole('checkbox', { name: /日文 OCR/ }));
    await user.click(screen.getByRole('checkbox', { name: /安全导出/ }));
    expect(screen.getByText('同时写入图像与 JSON；自定义目录可包含完整、可重开的 project/ 项目副本及源图副本。')).toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox', { name: '导出内容' }), 'images');
    expect(screen.getByText('仅写入所选生成图像；不会创建可重开的项目快照。')).toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox', { name: '导出内容' }), 'both');
    await user.selectOptions(screen.getByRole('combobox', { name: '导出图像版本' }), 'both');
    await user.click(screen.getByRole('button', { name: /加入队列/ }));

    await waitFor(() => expect(exportProject).toHaveBeenCalledWith(
      'project-1',
      expect.objectContaining({
        options: expect.objectContaining({
          format: 'both',
          imageVariant: 'both',
        }),
      }),
    ));
  });

  it('requires the generated artifact selected by the export image variant', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [imageFixture('image-1', {
        status: {
          ...imageFixture('image-1').status,
          inpaint: 'done',
          typeset: 'not_started',
          reviewState: 'reviewed',
        },
        stageReviews: {
          inpaint: { state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'a'.repeat(64) },
        },
      })],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    await user.click(screen.getByRole('checkbox', { name: /文字检测/ }));
    await user.click(screen.getByRole('checkbox', { name: /日文 OCR/ }));
    await user.click(screen.getByRole('checkbox', { name: /安全导出/ }));

    expect(screen.getByText('所选图像版本尚未全部生成')).toBeInTheDocument();
    expect(screen.getByText(/缺少排版图/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /加入队列/ })).toBeDisabled();

    await user.selectOptions(screen.getByRole('combobox', { name: '导出图像版本' }), 'inpainted');
    expect(screen.queryByText('所选图像版本尚未全部生成')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /加入队列/ })).toBeEnabled();

    await user.selectOptions(screen.getByRole('combobox', { name: '导出图像版本' }), 'both');
    expect(screen.getByRole('button', { name: /加入队列/ })).toBeDisabled();
  });

  it('requires explicit visual-stage acceptance before queuing image export', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [imageFixture('image-1', {
        status: {
          ...imageFixture('image-1').status,
          inpaint: 'done',
          typeset: 'done',
          reviewState: 'reviewed',
        },
        stageReviews: {
          inpaint: { state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'a'.repeat(64) },
          typeset: { state: 'rejected', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'a'.repeat(64) },
        },
      })],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    await user.click(screen.getByRole('checkbox', { name: /文字检测/ }));
    await user.click(screen.getByRole('checkbox', { name: /日文 OCR/ }));
    await user.click(screen.getByRole('checkbox', { name: /安全导出/ }));

    expect(screen.getByText('所选图像版本尚未全部通过视觉复核')).toBeInTheDocument();
    expect(screen.getByText(/1 页排版图未接受/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /加入队列/ })).toBeDisabled();

    await user.selectOptions(screen.getByRole('combobox', { name: '导出内容' }), 'json');
    expect(screen.queryByText('所选图像版本尚未全部通过视觉复核')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /加入队列/ })).toBeEnabled();
  });

  it('blocks combining processing and export until the user processes, reviews, then exports', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [imageFixture('image-1', {
        status: { ...imageFixture('image-1').status, reviewState: 'reviewed' },
      })],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    await user.click(screen.getByRole('checkbox', { name: /安全导出/ }));

    expect(screen.getByText('先处理→复核→再导出')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /加入队列/ })).toBeDisabled();

    await user.selectOptions(screen.getByRole('combobox', { name: '导出内容' }), 'json');
    expect(screen.getByRole('button', { name: /加入队列/ })).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: /文字检测/ }));
    await user.click(screen.getByRole('checkbox', { name: /日文 OCR/ }));

    expect(screen.queryByText('先处理→复核→再导出')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /加入队列/ })).toBeEnabled();
  });

  it('shows actionable errors instead of a successful-looking state', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    useWorkbenchStore.setState({ globalError: '导出目录不可写' });
    render(<App />);

    expect(screen.getByRole('alert')).toHaveTextContent('导出目录不可写');
    await user.click(screen.getByRole('button', { name: '关闭错误提示' }));
    expect(screen.queryByText('导出目录不可写')).not.toBeInTheDocument();
  });

  it('guards editing fields from global shortcuts while preserving the explicit global map', async () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture()
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images
    );
    vi.spyOn(api, 'updateRegion').mockImplementation(async (regionId, patch) => ({
      ...(useWorkbenchStore.getState().regionsByImage['image-1']?.find(
        (region) => region.id === regionId,
      ) ?? regionFixture(regionId)),
      ...patch,
      revision: 5,
    }));
    const { container } = render(<App />);
    const translation = screen.getByRole('textbox', { name: '中文译文' });
    translation.focus();

    expect(fireEvent.keyDown(translation, { key: 'Delete' })).toBe(true);
    fireEvent.keyDown(translation, { key: 'b' });
    fireEvent.keyDown(translation, { key: 'Enter' });
    fireEvent.keyDown(translation, { key: 'ArrowRight' });
    expect(useWorkbenchStore.getState()).toMatchObject({
      activeImageId: 'image-1',
      compareMode: false,
    });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.confirmed).toBe(false);
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toHaveLength(2);

    fireEvent.keyDown(window, { key: 'b' });
    expect(useWorkbenchStore.getState().compareMode).toBe(false);
    act(() => useWorkbenchStore.setState((state) => ({
      images: state.images.map((image) => image.id === 'image-1'
        ? { ...image, status: { ...image.status, preprocess: 'done' } }
        : image),
    })));
    fireEvent.keyDown(window, { key: 'b' });
    expect(useWorkbenchStore.getState().compareMode).toBe(true);
    expect(fireEvent.keyDown(window, { key: 'Tab' })).toBe(true);
    expect(useWorkbenchStore.getState().selectedRegionIds).toEqual(['region-1']);
    fireEvent.keyDown(window, { key: 'ArrowDown', altKey: true });
    expect(useWorkbenchStore.getState().selectedRegionIds).toEqual(['region-2']);
    expect(useWorkbenchStore.getState().focusRegionIds).toEqual(['region-2']);
    fireEvent.keyDown(window, { key: 'Enter' });
    await waitFor(() => expect(
      useWorkbenchStore.getState().regionsByImage['image-1']?.[1]?.confirmed,
    ).toBe(true));

    const multiInput = container.querySelector<HTMLInputElement>('input[type="file"][multiple]:not([webkitdirectory])');
    expect(multiInput).not.toBeNull();
    const click = vi.spyOn(multiInput as HTMLInputElement, 'click');
    fireEvent.keyDown(window, { key: 'o', ctrlKey: true });
    expect(click).toHaveBeenCalledOnce();

    fireEvent.keyDown(window, { key: 'Delete' });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toHaveLength(1);
  });

  it('configures an unavailable remote translator without enabling its batch step early', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    const unavailableRemote = {
      id: 'openai-compatible',
      label: 'OpenAI 兼容接口',
      kind: 'translator' as const,
      available: false,
      configurable: true,
      local: false,
      isMock: false,
      reason: '当前会话尚未配置 API Key',
    };
    useWorkbenchStore.setState({
      capabilities: {
        providers: [...capabilitiesFixture().providers, unavailableRemote],
      },
    });
    const configuredCapabilities = capabilitiesFixture();
    configuredCapabilities.providers.push({
      ...unavailableRemote,
      available: true,
      reason: undefined,
    });
    const setSessionCredential = vi.spyOn(api, 'setSessionCredential').mockResolvedValue({
      configured: true,
      capabilities: configuredCapabilities,
    });
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '项目' }));
    const translator = screen.getByRole('combobox', { name: '翻译' });
    const remoteOption = within(translator).getByRole('option', {
      name: 'OpenAI 兼容接口 [未配置]',
    });
    expect(remoteOption).toBeEnabled();
    await user.selectOptions(translator, 'openai-compatible');

    expect(screen.getByText('远程文本翻译')).toBeInTheDocument();
    const keyInput = screen.getByLabelText(/API Key（仅当前会话）/);
    expect(keyInput).toHaveValue('');

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    const drawer = screen.getByRole('dialog', { name: '批处理与导出' });
    expect(within(drawer).getByRole('checkbox', { name: /翻译/ })).toBeDisabled();
    await user.click(within(drawer).getByRole('button', { name: '关闭批处理抽屉' }));

    const secret = 'ui-session-test-value';
    await user.type(keyInput, secret);
    await user.click(screen.getByRole('button', { name: '应用' }));

    await waitFor(() => expect(setSessionCredential).toHaveBeenCalledWith(
      'openai-compatible',
      secret,
      '',
      '',
    ));
    expect(keyInput).toHaveValue('');
    expect(screen.getByText('已配置，仅当前后端会话有效。')).toBeInTheDocument();
    expect(screen.queryByDisplayValue(secret)).not.toBeInTheDocument();
    expect(JSON.stringify(useWorkbenchStore.getState().currentProject?.settings)).not.toContain(secret);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    const configuredDrawer = screen.getByRole('dialog', { name: '批处理与导出' });
    expect(within(configuredDrawer).getByRole('checkbox', { name: /翻译/ })).toBeEnabled();
  });

  it('applies preprocessing profile defaults while keeping every switch editable', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '项目' }));
    await user.selectOptions(screen.getByRole('combobox', { name: '预处理配置' }), 'off');

    expect(useWorkbenchStore.getState().currentProject?.settings.preprocessing).toMatchObject({
      profile: 'off',
      enableUpscale: false,
      enableDenoise: false,
      enableSharpen: false,
      enableContrastEnhance: false,
      enableEdgeOptimize: false,
      enableBinarize: false,
    });

    await user.click(screen.getByRole('checkbox', { name: '锐化' }));
    expect(
      useWorkbenchStore.getState().currentProject?.settings.preprocessing.enableSharpen,
    ).toBe(true);
  });

  it('lets the project require accepted AI inpainting before downstream work', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '项目' }));
    const gate = screen.getByRole('checkbox', {
      name: '翻译/嵌字前必须验收 AI 补图',
    });
    expect(gate).not.toBeChecked();
    await user.click(gate);

    expect(
      useWorkbenchStore.getState().currentProject?.settings
        .requireAIInpaintBeforeDownstream,
    ).toBe(true);
    expect(screen.getByText(/非 AI 修复候选即使已接受也不会解锁/)).toBeInTheDocument();
  });

  it('queues the current page with its preprocess suggestion without changing project defaults', async () => {
    const user = userEvent.setup();
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-page-suggest',
      kind: 'preprocess',
    }));
    seedWorkbench();
    render(<App />);

    expect(screen.getByText('本页建议预处理：关闭')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '采用为项目默认' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '按建议处理本页' }));
    expect(startJob).toHaveBeenCalledWith('project-1', 'preprocess', {
      imageIds: ['image-1'],
      options: {
        provider: 'opencv-pillow',
        preprocessing: expect.objectContaining({
          profile: 'off',
          enableUpscale: false,
          enableDenoise: false,
        }),
        concurrency: 1,
      },
    });
    expect(useWorkbenchStore.getState().currentProject?.settings.preprocessing.profile).toBe(
      'ocr-friendly',
    );

    await user.click(screen.getByRole('button', { name: '采用为项目默认' }));
    expect(useWorkbenchStore.getState().currentProject?.settings.preprocessing).toMatchObject({
      profile: 'off',
      enableUpscale: false,
      enableDenoise: false,
    });
  });

  it('lets the reviewer tidy overlapping boxes and queue a local AI redraw', async () => {
    const user = userEvent.setup();
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-ai-redraw',
      kind: 'preprocess',
    }));
    seedWorkbench({
      regions: [
        regionFixture('region-1'),
        regionFixture('region-2', { x: 140, y: 140, width: 200, height: 100 }),
      ],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: 'AI 重绘本页' }));
    expect(startJob).toHaveBeenCalledWith('project-1', 'preprocess', {
      imageIds: ['image-1'],
      options: {
        provider: 'realesrgan-onnx',
        preprocessing: expect.objectContaining({
          profile: 'visual-quality',
          upscaleFactor: 4,
        }),
        concurrency: 1,
      },
    });

    vi.spyOn(api, 'updateRegion').mockImplementation(async (regionId, patch) => ({
      ...regionFixture(regionId),
      ...patch,
      revision: 6,
    }));
    vi.spyOn(api, 'createRegion').mockResolvedValue(regionFixture('region-merged'));
    vi.spyOn(api, 'deleteRegion').mockResolvedValue();
    await user.click(screen.getByRole('button', { name: '整理本页选框' }));
    expect(useWorkbenchStore.getState().regionsByImage['image-1']).toHaveLength(1);
  });

  it('stores a per-region inpainting provider override and exposes an explicit rebuild action', async () => {
    const user = userEvent.setup();
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '修复' }));
    const provider = screen.getByRole('combobox', { name: '区域修复 Provider' });
    expect(provider).toHaveValue('');
    expect(within(provider).getByRole('option', { name: /继承项目设置/ })).toBeInTheDocument();

    const method = screen.getByRole('combobox', { name: '修复方法' });
    await user.selectOptions(method, 'screentone');
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair.method).toBe('screentone');

    await user.selectOptions(provider, 'lama-onnx');
    const textPolarity = screen.getByRole('combobox', { name: '文字极性' });
    expect(textPolarity).toHaveValue('auto');
    await user.selectOptions(textPolarity, 'dark');

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair).toMatchObject({
      inpainterProvider: 'lama-onnx',
      textPolarity: 'dark',
    });
    const maskMode = screen.getByRole('combobox', { name: '蒙版策略' });
    await user.selectOptions(maskMode, 'manual');
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair.maskMode).toBe('manual');
    expect(textPolarity).toBeDisabled();
    expect(screen.getByText('仅手工蒙版尚未添加范围')).toBeInTheDocument();
    expect(screen.getByRole('spinbutton', { name: '蒙版外扩 px' })).toBeDisabled();
    expect(screen.getByRole('spinbutton', { name: '膨胀 px' })).toBeDisabled();
    expect(screen.getByRole('spinbutton', { name: '羽化 px' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '重建当前页' })).toBeEnabled();
    expect(screen.getByText('LaMa AI 背景修复')).toBeInTheDocument();
  });

  it('persists a solid fill color across region selection without losing sibling repair settings', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      regions: [
        regionFixture('region-1', { order: 10 }),
        regionFixture('region-2', { order: 20, x: 360 }),
      ],
      selectedRegionIds: ['region-1'],
    });
    const update = vi.spyOn(api, 'updateRegion').mockImplementation(async (regionId, patch) => ({
      ...regionFixture(regionId),
      ...patch,
      repair: {
        ...regionFixture(regionId).repair,
        ...patch.repair,
      },
      revision: 6,
    }));
    vi.spyOn(api, 'getProject').mockResolvedValue(projectFixture({ revision: 4 }));
    vi.spyOn(api, 'listImages').mockResolvedValue([
      imageFixture('image-1'),
      imageFixture('image-2'),
    ]);
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '修复' }));
    fireEvent.change(screen.getByRole('combobox', { name: '修复方法' }), {
      target: { value: 'solid' },
    });
    fireEvent.input(screen.getByLabelText('修复填充色'), {
      target: { value: '#000000' },
    });
    fireEvent.change(screen.getByRole('spinbutton', { name: '蒙版外扩 px' }), {
      target: { value: '3' },
    });

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair).toMatchObject({
      method: 'solid',
      fillColor: '#000000',
      maskPadding: 3,
    });

    act(() => useWorkbenchStore.getState().selectRegion('region-2'));
    act(() => useWorkbenchStore.getState().selectRegion('region-1'));
    expect(screen.getByLabelText('修复填充色')).toHaveValue('#000000');

    await act(async () => {
      expect(await useWorkbenchStore.getState().flushAutosave()).toBe(true);
    });
    expect(update).toHaveBeenCalledWith(
      'region-1',
      expect.objectContaining({
        repair: expect.objectContaining({
          method: 'solid',
          fillColor: '#000000',
          maskPadding: 3,
        }),
      }),
    );
  });

  it('clears persisted mask strokes for only the selected region after confirmation', async () => {
    const user = userEvent.setup();
    const first = regionFixture('region-1', { order: 10 });
    first.repair.maskEdits = {
      version: 1,
      strokes: [
        { mode: 'add', radius: 8, points: [[120, 140], [120, 180]] },
        { mode: 'erase', radius: 3, points: [[120, 160]] },
      ],
    };
    const second = regionFixture('region-2', { order: 20, x: 360 });
    second.repair.maskEdits = {
      version: 1,
      strokes: [{ mode: 'add', radius: 5, points: [[370, 140]] }],
    };
    seedWorkbench({
      regions: [first, second],
      selectedRegionIds: ['region-1'],
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '修复' }));
    await user.click(screen.getByRole('button', { name: '清除当前区域蒙版笔迹' }));

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair.maskEdits).toEqual({
      version: 1,
      strokes: [],
    });
    expect(
      useWorkbenchStore.getState().regionsByImage['image-1']?.[1]?.repair.maskEdits?.strokes,
    ).toHaveLength(1);
  });

  it('lets the editor choose among inpaint candidates from the repair inspector', async () => {
    const user = userEvent.setup();
    const image = imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
      inpaintCandidate: 'primary',
      inpaintCandidates: [
        { id: 'primary', label: '当前 Provider 结果', anomalies: [], originKind: 'direct-ai' },
        { id: 'lineart-guided', label: '线稿引导(结构+纹理)', anomalies: ['possible-smear'], originKind: 'ai-derived' },
      ],
    });
    seedWorkbench({ images: [image] });
    const select = vi.spyOn(api, 'selectInpaintCandidate').mockResolvedValue({
      ...image,
      revision: 2,
      inpaintCandidate: 'lineart-guided',
      stageReviews: {},
    });
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '修复' }));
    expect(screen.getByRole('radiogroup', { name: '修复候选' })).toBeInTheDocument();
    expect(screen.getByText('可能涂抹过重')).toBeInTheDocument();
    expect(screen.getByText('来源：AI 直接修复')).toBeInTheDocument();
    expect(screen.getByText('来源：AI 派生修复')).toBeInTheDocument();
    expect(screen.queryByRole('group', { name: '传统算法逐页兜底' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: /线稿引导/ }));
    expect(select).toHaveBeenCalledWith('image-1', 'lineart-guided', 1);
  });

  it('requires current-candidate AI rejection before approving and revoking one classical page', async () => {
    const user = userEvent.setup();
    const review = {
      state: 'accepted' as const,
      reviewedAt: '2026-08-23T12:00:00Z',
      resultRevision: 3,
      artifactChecksum: 'a'.repeat(64),
      maskChecksum: 'b'.repeat(64),
    };
    const candidates = [
      { id: 'ai-a', label: 'AI 候选 A', anomalies: ['possible-smear'], originKind: 'direct-ai' as const },
      { id: 'ai-b', label: 'AI 候选 B', anomalies: [], originKind: 'ai-derived' as const },
      { id: 'classical-clean', label: '纯净传统候选', anomalies: [], originKind: 'classical' as const },
    ];
    const image = imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
      stageReviews: {},
      inpaintCandidate: 'ai-a',
      inpaintCandidateGenerationId: 'generation-a',
      inpaintCandidates: candidates,
      inpaintFallback: { state: 'pending' },
    });
    seedWorkbench({
      images: [image],
      project: projectFixture({
        settings: {
          ...projectFixture().settings,
          requireAIInpaintBeforeDownstream: true,
        },
      }),
    });
    let revision = 1;
    const rejectedAiCandidateIds = new Set<string>();
    vi.spyOn(api, 'selectInpaintCandidate').mockImplementation(async (_imageId, candidateId) => {
      revision += 1;
      return {
        ...image,
        revision,
        inpaintCandidate: candidateId,
        inpaintAiRejectedCandidateIds: [...rejectedAiCandidateIds],
        stageReviews: candidateId === 'classical-clean' ? { inpaint: review } : {},
      };
    });
    const reviewAi = vi.spyOn(api, 'reviewSelectedInpaintAiCandidate').mockImplementation(
      async (_imageId, state) => {
        const current = useWorkbenchStore.getState().images[0]!;
        const selectedId = current.inpaintCandidate!;
        if (state === 'rejected') rejectedAiCandidateIds.add(selectedId);
        else rejectedAiCandidateIds.delete(selectedId);
        return {
          ...current,
          revision: ++revision,
          inpaintAiRejectedCandidateIds: [...rejectedAiCandidateIds],
        };
      },
    );
    const fallback = vi.spyOn(api, 'setInpaintClassicalFallback').mockImplementation(
      async (_imageId, state) => ({
        ...image,
        revision: ++revision,
        inpaintCandidate: 'classical-clean',
        inpaintAiRejectedCandidateIds: [...rejectedAiCandidateIds],
        stageReviews: { inpaint: review },
        inpaintFallback: state === 'approved'
          ? { state, reason: 'ai-visible-artifacts' }
          : { state },
      }),
    );
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '修复' }));
    expect(screen.getByText('来源：传统算法（Classical）')).toBeInTheDocument();
    expect(screen.getByRole('status', { name: '本页修复授权' })).toHaveTextContent('未批准');
    const approve = screen.getByRole('button', { name: '批准本页传统算法兜底' });
    expect(approve).toBeDisabled();
    expect(reviewAi).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '标记此 AI 候选不可接受' }));
    expect(reviewAi).toHaveBeenLastCalledWith('image-1', 'rejected', 1);
    expect(useWorkbenchStore.getState().images[0]?.inpaintAiRejectedCandidateIds).toEqual(['ai-a']);
    await user.click(screen.getByRole('button', { name: '撤销此 AI 候选不可接受' }));
    expect(reviewAi).toHaveBeenLastCalledWith('image-1', 'pending', 2);
    expect(useWorkbenchStore.getState().images[0]?.inpaintAiRejectedCandidateIds).toEqual([]);
    await user.click(screen.getByRole('button', { name: '标记此 AI 候选不可接受' }));
    await user.click(screen.getByRole('radio', { name: /纯净传统候选/ }));
    await user.selectOptions(
      screen.getByRole('combobox', { name: '传统算法兜底原因' }),
      'ai-visible-artifacts',
    );
    expect(approve).toBeDisabled();

    await user.click(screen.getByRole('radio', { name: /AI 候选 B/ }));
    await user.click(screen.getByRole('button', { name: '标记此 AI 候选不可接受' }));
    await user.click(screen.getByRole('radio', { name: /纯净传统候选/ }));
    expect(approve).toBeEnabled();
    await user.click(approve);

    expect(fallback).toHaveBeenCalledWith('image-1', 'approved', 8, {
      reason: 'ai-visible-artifacts',
    });
    expect(screen.getByRole('status', { name: '本页修复授权' })).toHaveTextContent(
      '已批准：传统算法兜底',
    );
    expect(screen.getByText('原因：AI 候选存在明显伪影')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '撤销本页传统算法兜底' }));
    expect(fallback).toHaveBeenLastCalledWith('image-1', 'pending', 9, {});
    expect(screen.getByRole('status', { name: '本页修复授权' })).toHaveTextContent('未批准');
  });

  it('fails closed when the server clears untrusted candidate evidence', async () => {
    const user = userEvent.setup();
    const candidates = [
      { id: 'ai-a', label: 'AI 候选 A', anomalies: [], originKind: 'direct-ai' as const },
      { id: 'classical-clean', label: '传统候选', anomalies: [], originKind: 'classical' as const },
    ];
    const image = imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
      inpaintCandidate: 'ai-a',
      inpaintCandidateGenerationId: 'generation-a',
      inpaintCandidates: candidates,
      inpaintAiRejectedCandidateIds: ['ai-a'],
    });
    seedWorkbench({
      images: [image],
      project: projectFixture({
        settings: {
          ...projectFixture().settings,
          requireAIInpaintBeforeDownstream: true,
        },
      }),
    });
    render(<App />);
    await user.click(screen.getByRole('tab', { name: '修复' }));
    expect(screen.getByRole('radiogroup', { name: '修复候选' })).toBeInTheDocument();

    act(() => {
      useWorkbenchStore.setState((state) => ({
        images: state.images.map((entry) => entry.id === image.id ? {
          ...entry,
          revision: 2,
          inpaintCandidate: undefined,
          inpaintCandidateGenerationId: null,
          inpaintCandidates: [],
          inpaintAiRejectedCandidateIds: [],
          inpaintFallback: { state: 'pending' },
        } : entry),
      }));
    });

    expect(screen.queryByRole('radiogroup', { name: '修复候选' })).not.toBeInTheDocument();
    expect(screen.queryByRole('group', { name: '传统算法逐页兜底' })).not.toBeInTheDocument();
    expect(screen.queryByText('来源：传统算法（Classical）')).not.toBeInTheDocument();
  });

  it('surfaces a successful safe-repair job that changed no image pixels', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    useWorkbenchStore.setState({
      jobs: [jobFixture({
        id: 'job-inpaint',
        kind: 'inpaint',
        status: 'completed',
        total: 1,
        completed: 1,
        progress: 1,
        items: [{
          id: 'item-inpaint',
          imageId: 'image-1',
          label: 'opaque-id',
          status: 'completed',
          progress: 1,
          output: { repairedRegionCount: 0, skippedRegionCount: 3 },
        }],
      })],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));

    expect(screen.getByText('修复 0 · 跳过 3（未改动图像）')).toBeInTheDocument();
  });

  it('surfaces an overlay typeset job in the queue card', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    useWorkbenchStore.setState({
      jobs: [jobFixture({
        id: 'job-typeset-overlay',
        kind: 'typeset',
        status: 'completed',
        total: 1,
        completed: 1,
        progress: 1,
        items: [{
          id: 'item-typeset-overlay',
          imageId: 'image-1',
          label: 'opaque-id',
          status: 'completed',
          progress: 1,
          output: { partialTypeset: true, overlayRegionCount: 1, overlayRegionIds: ['region-1'] },
        }],
      })],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    expect(screen.getByText('叠绘 1 框')).toBeInTheDocument();
  });

  it('surfaces a full-page typeset fallback in the queue card', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    useWorkbenchStore.setState({
      jobs: [jobFixture({
        id: 'job-typeset-full',
        kind: 'typeset',
        status: 'completed',
        total: 1,
        completed: 1,
        progress: 1,
        items: [{
          id: 'item-typeset-full',
          imageId: 'image-1',
          label: 'opaque-id',
          status: 'completed',
          progress: 1,
          output: { partialTypeset: false, overlayRegionCount: 0, overlayRegionIds: [] },
        }],
      })],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    expect(screen.getByText('整页重排 1 页')).toBeInTheDocument();
  });

  it('opens a queue item page from the job card', async () => {
    const user = userEvent.setup();
    const overlay = regionFixture('region-9', { imageId: 'image-2' });
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2', {
          status: { ...imageFixture('image-2').status, typeset: 'done' },
        }),
      ],
    });
    useWorkbenchStore.setState((state) => ({
      regionsByImage: { ...state.regionsByImage, 'image-2': [overlay] },
      jobs: [jobFixture({
        id: 'job-typeset-overlay',
        kind: 'typeset',
        status: 'completed',
        total: 1,
        completed: 1,
        progress: 1,
        items: [{
          id: 'item-typeset-page2',
          imageId: 'image-2',
          label: 'opaque-id',
          status: 'completed',
          progress: 1,
          output: { partialTypeset: true, overlayRegionCount: 1, overlayRegionIds: ['region-9'] },
        }],
      })],
    }));
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    await user.click(screen.getByText('查看 1 个队列项'));
    await user.click(screen.getByRole('button', { name: '打开队列项 第二话/image-2.png' }));
    await waitFor(() => {
      expect(useWorkbenchStore.getState()).toMatchObject({
        activeImageId: 'image-2',
        selectedRegionIds: ['region-9'],
        rightTab: 'typesetting',
        canvasMode: 'typeset',
        focusRegionIds: ['region-9'],
        drawerOpen: false,
      });
    });
    expect(useWorkbenchStore.getState().focusRequest).toBeGreaterThan(0);
    expect(screen.queryByRole('dialog', { name: '批处理与导出' })).not.toBeInTheDocument();
  });

  it('opens a failed queue item onto the matching inspector', async () => {
    const user = userEvent.setup();
    seedWorkbench({
      images: [
        imageFixture('image-1'),
        imageFixture('image-2'),
      ],
    });
    useWorkbenchStore.setState({
      canvasMode: 'original',
      rightTab: 'project',
      jobs: [jobFixture({
        id: 'job-ocr-failed',
        kind: 'ocr',
        status: 'failed',
        total: 1,
        completed: 0,
        progress: 0,
        items: [{
          id: 'item-ocr-page2',
          imageId: 'image-2',
          label: 'opaque-id',
          status: 'failed',
          progress: 0,
          error: 'tesseract unavailable',
        }],
      })],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    await user.click(screen.getByText('查看 1 个队列项'));
    await user.click(screen.getByRole('button', { name: '打开队列项 第二话/image-2.png' }));
    await waitFor(() => {
      expect(useWorkbenchStore.getState()).toMatchObject({
        activeImageId: 'image-2',
        rightTab: 'text',
        canvasMode: 'original',
        drawerOpen: false,
      });
    });
    expect(screen.queryByRole('dialog', { name: '批处理与导出' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '文本' })).toHaveAttribute('aria-selected', 'true');
  });

  it('keeps unavailable generated previews disabled and falls back to the original', () => {
    seedWorkbench();
    useWorkbenchStore.setState({ canvasMode: 'typeset' });

    render(<App />);

    expect(screen.getByRole('button', { name: /^增强$/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^擦除$/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /^成品$/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: '对比' })).toBeDisabled();
    fireEvent.keyDown(window, { key: 'b' });
    expect(useWorkbenchStore.getState().compareMode).toBe(false);
    expect(screen.getByRole('application', { name: '原图画布' })).toBeVisible();
  });

  it('turns comparison off when switching to a page without generated artifacts', async () => {
    const generated = imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, typeset: 'done' },
    });
    seedWorkbench({ images: [generated, imageFixture('image-2')] });
    render(<App />);

    fireEvent.click(screen.getByRole('button', { name: '对比' }));
    expect(useWorkbenchStore.getState().compareMode).toBe(true);
    expect(screen.getByText('嵌字成品')).toBeInTheDocument();

    act(() => useWorkbenchStore.setState({
      activeImageId: 'image-2',
      selectedImageIds: ['image-2'],
    }));

    await waitFor(() => expect(useWorkbenchStore.getState().compareMode).toBe(false));
    expect(screen.getByRole('button', { name: '对比' })).toBeDisabled();
    expect(screen.queryByText('嵌字成品')).not.toBeInTheDocument();
    expect(screen.getByRole('application', { name: '原图画布' })).toBeVisible();
  });

  it('queues the viewed page from the batch drawer even when another page stays checkbox-selected', async () => {
    const user = userEvent.setup();
    const startJob = vi.spyOn(api, 'startJob').mockResolvedValue(jobFixture({
      id: 'job-current-page',
      kind: 'detect',
    }));
    vi.spyOn(api, 'getProject').mockImplementation(async () =>
      useWorkbenchStore.getState().currentProject ?? projectFixture(),
    );
    vi.spyOn(api, 'listImages').mockImplementation(async () =>
      useWorkbenchStore.getState().images,
    );
    vi.spyOn(api, 'listJobs').mockImplementation(async () =>
      useWorkbenchStore.getState().jobs,
    );
    seedWorkbench({ images: [imageFixture('image-1'), imageFixture('image-2')] });
    useWorkbenchStore.setState({
      activeImageId: 'image-2',
      selectedImageIds: ['image-1'],
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    const drawer = screen.getByRole('dialog', { name: '批处理与导出' });
    expect(within(drawer).getByRole('radio', { name: /当前页/ })).toBeChecked();
    expect(within(drawer).getByRole('button', { name: /加入队列 · 1 张 · 2 步/ })).toBeEnabled();
    await user.click(within(drawer).getByRole('button', { name: /加入队列/ }));

    await waitFor(() => expect(startJob).toHaveBeenCalled());
    expect(startJob).toHaveBeenCalledWith(
      'project-1',
      'detect',
      expect.objectContaining({ imageIds: ['image-2'] }),
    );
    expect(startJob).toHaveBeenCalledWith(
      'project-1',
      'ocr',
      expect.objectContaining({ imageIds: ['image-2'] }),
    );
    expect(startJob).not.toHaveBeenCalledWith(
      'project-1',
      expect.anything(),
      expect.objectContaining({ imageIds: ['image-1'] }),
    );
  });

  it('resets the batch drawer to the current page when it is reopened', async () => {
    const user = userEvent.setup();
    seedWorkbench({ images: [imageFixture('image-1'), imageFixture('image-2')] });
    useWorkbenchStore.setState({ activeImageId: 'image-2' });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    const firstDrawer = screen.getByRole('dialog', { name: '批处理与导出' });
    await user.click(within(firstDrawer).getByRole('radio', { name: /全部图像/ }));
    expect(within(firstDrawer).getByRole('radio', { name: /全部图像/ })).toBeChecked();
    await user.click(within(firstDrawer).getByRole('button', { name: '关闭批处理抽屉' }));

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    const drawer = screen.getByRole('dialog', { name: '批处理与导出' });
    expect(within(drawer).getByRole('radio', { name: /当前页/ })).toBeChecked();
    expect(within(drawer).getByRole('button', { name: /加入队列 · 1 张 · 2 步/ })).toBeEnabled();
  });

  it('shows only G4-owned editor fields for an active page generation', async () => {
    const region = regionFixture('region-1', {
      order: 0,
      paragraphGroupId: 'paragraph-1',
      contentDisposition: 'translate',
      sourceText: '后续阶段原文',
      translationText: '后续阶段译文',
    });
    seedWorkbench({ regions: [region], selectedRegionIds: ['region-1'] });
    useWorkbenchStore.setState({
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(),
          events: [activeG4Event()],
          error: '',
          conflict: false,
        },
      },
    });
    render(<App />);

    expect(screen.getByRole('region', { name: 'G4 区域门禁' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'G4 内容处理决定' })).toHaveValue('translate');
    expect(screen.getByRole('combobox', { name: 'G4 文本类型' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'G4 文本方向' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'G4 段落组' })).toHaveValue('paragraph-1');
    expect(screen.queryByRole('textbox', { name: '日文原文' })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: '中文译文' })).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: '确认此文本框' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '排版' })).toBeDisabled();
    expect(screen.getByRole('tab', { name: '修复' })).toBeDisabled();
    expect(screen.getByRole('tab', { name: '项目' })).toBeDisabled();
  });

  it.each(['active', 'loading', 'error'] as const)(
    'disables mask editing and exits a stale mask tool while lineage is %s',
    async (status) => {
      seedWorkbench({ selectedRegionIds: ['region-1'] });
      useWorkbenchStore.setState({
        canvasTool: 'mask-brush',
        g4Contexts: {
          'image-1': {
            status,
            generation: status === 'active' ? activeG4Generation() : null,
            events: status === 'active' ? [activeG4Event()] : [],
            error: status === 'error' ? 'historical lineage has no active generation' : '',
            conflict: false,
          },
        },
      });
      render(<App />);

      expect(screen.getByRole('button', { name: '蒙版画笔' })).toBeDisabled();
      expect(screen.getByRole('button', { name: '蒙版橡皮擦' })).toBeDisabled();
      expect(screen.queryByRole('slider', { name: '蒙版画笔半径' })).not.toBeInTheDocument();
      await waitFor(() => {
        expect(useWorkbenchStore.getState().canvasTool).toBe('select');
      });
    },
  );

  it('keeps mask editing available for an explicitly legacy page', () => {
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    render(<App />);

    expect(screen.getByRole('button', { name: '蒙版画笔' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '蒙版橡皮擦' })).toBeEnabled();
  });

  it('keeps detector candidates in the G4 audit trail instead of offering deletion', () => {
    const region = regionFixture('region-1', {
      order: 0,
      paragraphGroupId: null,
      contentDisposition: null,
      detectorJobItemId: 'item-detect',
      detectorCandidateIndex: 0,
      detectorConfidence: 0.37,
      recognition: {
        version: 1,
        detection: {
          provider: 'ppocr-v3', inputVariant: 'preprocessed', language: 'ja', confidence: 0.37,
        },
      },
      repair: { ...regionFixture('region-1').repair, detectedTextCandidate: '候補' },
    });
    seedWorkbench({ regions: [region], selectedRegionIds: ['region-1'] });
    useWorkbenchStore.setState({
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(),
          events: [activeG4Event()],
          error: '',
          conflict: false,
        },
      },
    });
    render(<App />);

    const evidence = screen.getByRole('status', { name: 'G4 检测候选证据' });
    expect(evidence).toHaveTextContent('任务项：item-detect · 候选序号：0');
    expect(evidence).toHaveTextContent('Provider：ppocr-v3 · 检测置信度：37%');
    expect(evidence).toHaveTextContent('输入：preprocessed · 语言：ja');
    expect(evidence).toHaveTextContent('文字候选：候補');
    expect(screen.queryByRole('button', { name: '删除这个 G4 文本框' })).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'G4 内容处理决定' })).toHaveValue('');
  });

  it('routes G4 decisions, detect, acceptance, and full-order actions to dedicated store commands', async () => {
    const user = userEvent.setup();
    const first = regionFixture('region-1', {
      order: 0,
      paragraphGroupId: 'paragraph-1',
      contentDisposition: 'translate',
    });
    const second = regionFixture('region-2', {
      order: 1,
      paragraphGroupId: 'paragraph-2',
      contentDisposition: 'translate',
    });
    seedWorkbench({ regions: [first, second], selectedRegionIds: ['region-1'] });
    const updateRegion = vi.fn();
    const moveG4Region = vi.fn(async () => true);
    const startG4Detection = vi.fn(async () => true);
    const acceptG4Regions = vi.fn(async () => true);
    useWorkbenchStore.setState({
      updateRegion,
      moveG4Region,
      startG4Detection,
      acceptG4Regions,
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(),
          events: [activeG4Event()],
          error: '',
          conflict: false,
        },
      },
    });
    render(<App />);

    await user.selectOptions(
      screen.getByRole('combobox', { name: 'G4 内容处理决定' }),
      'false-positive',
    );
    await user.click(screen.getByRole('button', { name: '顺序下移' }));
    await user.click(screen.getByRole('button', { name: '重新检测本页' }));
    await user.click(screen.getByRole('button', { name: '接受全部区域决定' }));

    expect(updateRegion).toHaveBeenCalledWith('region-1', {
      contentDisposition: 'false-positive', rubyParentId: null,
    });
    expect(moveG4Region).toHaveBeenCalledWith('region-1', 1);
    expect(startG4Detection).toHaveBeenCalledOnce();
    expect(acceptG4Regions).toHaveBeenCalledOnce();
  });

  it('locks G4 editing and acceptance while detection is queued', () => {
    const region = regionFixture('region-1', {
      order: 0,
      paragraphGroupId: 'paragraph-1',
      contentDisposition: 'translate',
    });
    seedWorkbench({ regions: [region], selectedRegionIds: ['region-1'] });
    useWorkbenchStore.setState({
      jobs: [jobFixture({
        id: 'job-detect',
        kind: 'detect',
        status: 'queued',
        items: [{
          id: 'item-detect', imageId: 'image-1', label: 'image-1', status: 'queued', progress: 0,
        }],
      })],
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(),
          events: [activeG4Event()],
          error: '',
          conflict: false,
        },
      },
    });
    render(<App />);

    expect(screen.getByText('检测运行中')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '检测进行中…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '接受全部区域决定' })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'G4 内容处理决定' })).toBeDisabled();
  });

  it('disables every legacy batch step when the selected page has active lineage', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    useWorkbenchStore.setState({
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(),
          events: [activeG4Event()],
          error: '',
          conflict: false,
        },
        'image-2': {
          status: 'legacy', generation: null, events: [], error: '', conflict: false,
        },
      },
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    const drawer = screen.getByRole('dialog', { name: '批处理与导出' });
    expect(within(drawer).getByText('血缘页面请使用阶段专用入口')).toBeInTheDocument();
    for (const label of ['图片增强', '文字检测', '日文 OCR', '擦字修复', '翻译', '嵌字排版', '安全导出']) {
      expect(within(drawer).getByRole('checkbox', { name: new RegExp(label) })).toBeDisabled();
    }
    expect(within(drawer).getByRole('button', { name: /加入队列/ })).toBeDisabled();
  });

  it('hides visual-stage review and repair-candidate writes for an active G4 generation', () => {
    const image = imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
      stageReviews: {
        inpaint: {
          state: 'accepted',
          reviewedAt: '2026-08-25T00:00:00Z',
          resultRevision: 7,
          artifactChecksum: 'a'.repeat(64),
          maskChecksum: 'b'.repeat(64),
        },
      },
      inpaintCandidate: 'candidate-a',
      inpaintCandidates: [
        { id: 'candidate-a', label: '候选 A', anomalies: [], originKind: 'direct-ai' },
        { id: 'candidate-b', label: '候选 B', anomalies: [], originKind: 'direct-ai' },
      ],
    });
    seedWorkbench({ images: [image] });
    useWorkbenchStore.setState({
      canvasMode: 'erased',
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(),
          events: [activeG4Event()],
          error: '',
          conflict: false,
        },
      },
    });
    render(<App />);

    expect(screen.queryByRole('group', { name: '当前视觉阶段复核' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: '修复候选' })).not.toBeInTheDocument();
  });

  it('shows a manual reload action after a G4 sequence conflict', async () => {
    const user = userEvent.setup();
    seedWorkbench({ regions: [regionFixture('region-1')] });
    const reloadActiveImage = vi.fn(async () => undefined);
    useWorkbenchStore.setState({
      reloadActiveImage,
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(),
          events: [activeG4Event()],
          error: 'Page lineage changed after the mutation was prepared',
          conflict: true,
        },
      },
    });
    render(<App />);

    expect(screen.getByText('G4 版本冲突')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重载本页' }));
    expect(reloadActiveImage).toHaveBeenCalledOnce();
  });

  it('renders G5 as selectable read-only geometry and saves confidence-zero evidence explicitly', async () => {
    const user = userEvent.setup();
    const image = imageFixture('image-1', {
      revision: 10,
      status: {
        ...imageFixture('image-1').status,
        preprocess: 'done',
        inpaint: 'done',
        typeset: 'done',
      },
    });
    const region = regionFixture('region-1', {
      order: 0,
      paragraphGroupId: 'paragraph-1',
      contentDisposition: 'translate',
    });
    seedWorkbench({ images: [image], regions: [region], selectedRegionIds: ['region-1'] });
    const saveG5Background = vi.fn(async () => true);
    const contentUrl = vi.spyOn(api, 'contentUrl');
    useWorkbenchStore.setState({
      canvasMode: 'typeset',
      saveG5Background,
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(8),
          events: [{
            ...activeG4Event(7),
            operation: 'regions-stage-review',
            state: 'accepted',
            outputChecksum: 'e'.repeat(64),
          }],
          phase: 'G5',
          error: '',
          conflict: false,
        },
      },
      backgroundContexts: {
        'image-1': {
          imageId: 'image-1', imageRevision: 10, generationId: 'generation-1', nextSequence: 8,
          g4Checksum: 'e'.repeat(64), backgroundChecksum: 'f'.repeat(64), state: 'pending',
          eligibleRegionIds: ['region-1'], classifiedRegionIds: [],
        },
      },
    });
    render(<App />);

    expect(screen.getByRole('region', { name: 'G5 背景门禁' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'G4 区域门禁' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '蒙版画笔' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '擦除' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '成品' })).toBeDisabled();
    await waitFor(() => expect(useWorkbenchStore.getState().canvasMode).toBe('original'));
    expect(screen.getByTestId('canvas-surface')).toHaveAttribute('data-editable', 'false');
    expect(screen.getByTestId('canvas-surface')).toHaveAttribute('data-selectable', 'true');
    await user.click(screen.getByRole('button', { name: '对比' }));
    await waitFor(() => {
      expect(contentUrl.mock.calls.some((call) => call[1] === 'preprocessed')).toBe(true);
    });
    expect(contentUrl.mock.calls.some((call) => call[1] === 'erased' || call[1] === 'typeset')).toBe(false);
    expect(screen.getByText('已接受质量底板')).toBeInTheDocument();

    await user.selectOptions(screen.getByRole('combobox', { name: 'G5 背景类别' }), 'white-solid');
    await user.type(screen.getByRole('spinbutton', { name: 'G5 背景置信度' }), '0');
    await user.click(screen.getByRole('button', { name: '保存分类证据' }));
    expect(saveG5Background).toHaveBeenCalledWith(
      'region-1', 'white-solid', 0, ['uniform-near-white'],
    );
  });

  it('keeps low-confidence G5 acceptance enabled and blocks mutation shortcuts', async () => {
    const reviewer = {
      actorKind: 'human' as const,
      sessionId: 'reviewer-1',
      operationSource: 'ui' as const,
    };
    const region = regionFixture('region-1', {
      contentDisposition: 'translate',
      backgroundCategory: 'complex-lineart',
      backgroundConfidence: 0,
      backgroundRationaleCodes: ['structural-lines-cross-region'],
      backgroundReviewer: reviewer,
      backgroundGenerationId: 'generation-1',
    });
    seedWorkbench({
      images: [imageFixture('image-1', { revision: 10 })],
      regions: [region],
      selectedRegionIds: ['region-1'],
    });
    const acceptG5Background = vi.fn(async () => true);
    const deleteSelectedRegions = vi.fn();
    const nudgeSelectedRegions = vi.fn();
    const startBatch = vi.fn(async () => true);
    useWorkbenchStore.setState({
      acceptG5Background,
      deleteSelectedRegions,
      nudgeSelectedRegions,
      startBatch,
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(8),
          events: [{
            ...activeG4Event(7),
            operation: 'regions-stage-review',
            state: 'accepted',
            outputChecksum: 'e'.repeat(64),
          }],
          phase: 'G5',
          error: '',
          conflict: false,
        },
      },
      backgroundContexts: {
        'image-1': {
          imageId: 'image-1', imageRevision: 10, generationId: 'generation-1', nextSequence: 8,
          g4Checksum: 'e'.repeat(64), backgroundChecksum: 'f'.repeat(64), state: 'pending',
          eligibleRegionIds: ['region-1'], classifiedRegionIds: ['region-1'],
        },
      },
    });
    render(<App />);

    const accept = screen.getByRole('button', { name: '接受全部背景分类' });
    expect(accept).toBeEnabled();
    await userEvent.click(accept);
    expect(acceptG5Background).toHaveBeenCalledOnce();

    for (const key of ['r', 'n', 'm', 'e', 't', '3', '4', 'Delete']) {
      fireEvent.keyDown(window, { key });
    }
    fireEvent.keyDown(window, { key: 'ArrowRight', metaKey: true });
    expect(useWorkbenchStore.getState().canvasTool).toBe('select');
    expect(useWorkbenchStore.getState().canvasMode).toBe('original');
    expect(deleteSelectedRegions).not.toHaveBeenCalled();
    expect(nudgeSelectedRegions).not.toHaveBeenCalled();
    expect(startBatch).not.toHaveBeenCalled();
  });

  it('switches a terminal G5 event to the read-only G6 gate', () => {
    seedWorkbench({
      images: [imageFixture('image-1', { revision: 11 })],
      regions: [regionFixture('region-1', { contentDisposition: 'translate' })],
    });
    useWorkbenchStore.setState({
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(9),
          events: [
            {
              ...activeG4Event(7),
              operation: 'regions-stage-review',
              state: 'accepted',
              outputChecksum: 'e'.repeat(64),
            },
            {
              ...activeG4Event(8),
              operation: 'background-stage-review',
              gate: 'G5_background',
              state: 'accepted',
              decision: 'backgrounds-accepted',
              outputChecksum: 'f'.repeat(64),
            },
          ],
          phase: 'G6',
          error: '',
          conflict: false,
        },
      },
      backgroundContexts: {
        'image-1': {
          imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 9,
          g4Checksum: 'e'.repeat(64), backgroundChecksum: 'f'.repeat(64), state: 'accepted',
          eligibleRegionIds: ['region-1'], classifiedRegionIds: ['region-1'],
        },
      },
      ocrContexts: {
        'image-1': {
          imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 9,
          g5Checksum: 'f'.repeat(64), ocrChecksum: '6'.repeat(64), state: 'pending',
          eligibleRegionIds: ['region-1'], attemptedRegionIds: [], reviewedRegionIds: [], attempts: [],
        },
      },
    });
    render(<App />);

    expect(screen.getByRole('region', { name: 'G6 OCR 门禁' })).toHaveTextContent('G6 OCR');
    expect(screen.getByRole('button', { name: '运行 G6 双路 OCR' })).toBeEnabled();
    expect(screen.queryByRole('combobox', { name: 'G5 背景类别' })).not.toBeInTheDocument();
  });

  it('reviews dual G6 OCR attempts explicitly and never treats confidence zero as a blocker', () => {
    const region = regionFixture('region-1', {
      sourceText: '',
      contentDisposition: 'translate',
      type: 'dialogue',
    });
    const attempts = [ocrAttemptFixture('original', 0), ocrAttemptFixture('quality', 0.42)];
    seedWorkbench({
      images: [imageFixture('image-1', { revision: 12 })],
      regions: [region],
      selectedRegionIds: ['region-1'],
    });
    useWorkbenchStore.setState({
      g4Contexts: {
        'image-1': {
          status: 'active', generation: activeG4Generation(10), events: [], phase: 'G6', error: '', conflict: false,
        },
      },
      ocrContexts: {
        'image-1': {
          imageId: 'image-1', imageRevision: 12, generationId: 'generation-1', nextSequence: 10,
          g5Checksum: 'f'.repeat(64), ocrChecksum: '6'.repeat(64), state: 'pending',
          eligibleRegionIds: ['region-1'], attemptedRegionIds: ['region-1'], reviewedRegionIds: [], attempts,
        },
      },
    });
    render(<App />);

    expect(screen.getByText('原图 OCR')).toBeInTheDocument();
    expect(screen.getByText('增强图 OCR')).toBeInTheDocument();
    expect(screen.getByText('置信度：0')).toBeInTheDocument();
    expect(screen.getAllByText('Provider：tesseract · 模型：tesseract-5')).toHaveLength(2);
    expect(screen.getByTitle('1'.repeat(64))).toHaveTextContent('裁剪校验和');
    expect(screen.getByRole('button', { name: '保存原文复核证据' })).toBeDisabled();

    expect(OCR_QC_CHECKS.map((check) => ocrQCCheckLabelsForTest[check])).toHaveLength(9);
    document.querySelectorAll<HTMLInputElement>('.ocr-qc-list input').forEach((input) => {
      fireEvent.click(input);
    });
    expect(screen.getByRole('button', { name: '保存原文复核证据' })).toBeEnabled();
    expect(screen.getByRole('textbox', { name: 'G6 已核准日文原文' })).toHaveValue('品質の文');

    fireEvent.change(screen.getByRole('combobox', { name: 'G6 原文来源模式' }), { target: { value: 'manual-correction' } });
    expect(screen.getByRole('combobox', { name: 'G6 原文来源模式' })).toHaveValue('manual-correction');
    expect(screen.getByRole('textbox', { name: 'G6 已核准日文原文' })).toBeEnabled();
    expect(screen.getByRole('tab', { name: '排版' })).toBeDisabled();
    expect(screen.getByRole('tab', { name: '修复' })).toBeDisabled();
  });

  it('accepts reviewed G6 pages, exposes zero-eligible N/A, and opens the strict G7 gate', () => {
    const acceptG6OCR = vi.fn(async () => true);
    const reviewer = { actorKind: 'human' as const, sessionId: 'reviewer-1', operationSource: 'ui' as const };
    const reviewed = regionFixture('region-1', {
      sourceText: '品質の文', contentDisposition: 'translate', type: 'dialogue',
      ocrReview: {
        sourceMode: 'quality-attempt', selectedAttemptId: 'attempt-quality',
        sourceTextChecksum: '5'.repeat(64), qcChecks: OCR_QC_CHECKS, qcFlags: ['none'],
      },
      ocrReviewer: reviewer,
      ocrGenerationId: 'generation-1',
    });
    seedWorkbench({ images: [imageFixture('image-1', { revision: 12 })], regions: [reviewed] });
    useWorkbenchStore.setState({
      acceptG6OCR,
      g4Contexts: { 'image-1': { status: 'active', generation: activeG4Generation(11), events: [], phase: 'G6', error: '', conflict: false } },
      ocrContexts: { 'image-1': {
        imageId: 'image-1', imageRevision: 12, generationId: 'generation-1', nextSequence: 11,
        g5Checksum: 'f'.repeat(64), ocrChecksum: '6'.repeat(64), state: 'pending',
        eligibleRegionIds: ['region-1'], attemptedRegionIds: ['region-1'], reviewedRegionIds: ['region-1'],
        attempts: [ocrAttemptFixture('original', 0), ocrAttemptFixture('quality', 0.42)],
      } },
    });
    render(<App />);
    const accept = screen.getByRole('button', { name: '接受全部原文复核' });
    expect(accept).toBeEnabled();
    expect(useWorkbenchStore.getState().acceptG6OCR).toBe(acceptG6OCR);

    const currentOCR = useWorkbenchStore.getState().ocrContexts['image-1'];
    expect(currentOCR).toBeDefined();
    act(() => useWorkbenchStore.setState((state) => ({
      regionsByImage: { ...state.regionsByImage, 'image-1': [regionFixture('region-2', { contentDisposition: 'ignore' })] },
      ocrContexts: { ...state.ocrContexts, 'image-1': { ...currentOCR!, eligibleRegionIds: [], attemptedRegionIds: [], reviewedRegionIds: [] } },
    })));
    expect(screen.getByRole('button', { name: '确认本页 G6 不适用' })).toBeEnabled();

    const currentPage = useWorkbenchStore.getState().g4Contexts['image-1'];
    expect(currentPage).toBeDefined();
    act(() => useWorkbenchStore.setState((state) => ({
      g4Contexts: { ...state.g4Contexts, 'image-1': { ...currentPage!, phase: 'G7' } },
      maskContexts: { 'image-1': {
        imageId: 'image-1', imageRevision: 12, generationId: 'generation-1', nextSequence: 11,
        g6Checksum: '6'.repeat(64), qualityChecksum: '7'.repeat(64), maskStateChecksum: '8'.repeat(64),
        state: 'pending', eligibleRegionIds: [], rubyRegionIdsByPrimary: {},
        draft: { revision: 0, stateChecksum: '9'.repeat(64), regions: [] },
        artifacts: [], selectedArtifactId: null, review: null,
      } },
    })));
    expect(screen.getByRole('region', { name: 'G7 蒙版门禁' })).toHaveTextContent('0 个不可变实际蒙版');
    expect(screen.getByRole('button', { name: '确认 G7 不适用' })).toBeEnabled();
  });

  it('revalidates G6 trust on a cold G7 render and locks on a server conflict', async () => {
    seedWorkbench({
      images: [imageFixture('image-1', { revision: 12 })],
      regions: [regionFixture('region-1', { contentDisposition: 'translate' })],
    });
    useWorkbenchStore.setState({
      g4Contexts: {
        'image-1': {
          status: 'active',
          generation: activeG4Generation(14),
          events: [],
          phase: 'G7',
          error: '',
          conflict: false,
        },
      },
      ocrContexts: {},
    });
    const getOCR = vi.spyOn(api, 'getOCRGateContext').mockRejectedValue(
      new ApiError('G6 terminal evidence is no longer current', 409),
    );
    render(<App />);

    await waitFor(() => expect(getOCR).toHaveBeenCalledWith('image-1'));
    await waitFor(() => expect(useWorkbenchStore.getState().g4Contexts['image-1']).toMatchObject({
      error: 'G6 terminal evidence is no longer current',
      conflict: true,
    }));
    expect(screen.getByText('本页工作流已锁定')).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'G7 蒙版门禁' })).not.toBeInTheDocument();
  });

  it('requires all four checksum-bound G7 views before retaining a mask observation', async () => {
    const image = imageFixture('image-1', { revision: 12, status: {
      ...imageFixture('image-1').status, preprocess: 'done',
    } });
    const region = regionFixture('region-1', { contentDisposition: 'translate', type: 'dialogue' });
    seedWorkbench({ images: [image], regions: [region], selectedRegionIds: ['region-1'] });
    useWorkbenchStore.setState({
      g4Contexts: { 'image-1': { status: 'active', generation: activeG4Generation(18), events: [], phase: 'G7', error: '', conflict: false } },
      ocrContexts: { 'image-1': { imageId: 'image-1', imageRevision: 12, generationId: 'generation-1', nextSequence: 18,
        g5Checksum: '1'.repeat(64), ocrChecksum: '2'.repeat(64), state: 'accepted', eligibleRegionIds: ['region-1'],
        attemptedRegionIds: ['region-1'], reviewedRegionIds: ['region-1'], attempts: [] } },
      maskContexts: { 'image-1': { imageId: 'image-1', imageRevision: 12, generationId: 'generation-1', nextSequence: 18,
        g6Checksum: '2'.repeat(64), qualityChecksum: '3'.repeat(64), maskStateChecksum: '4'.repeat(64),
        state: 'pending', eligibleRegionIds: ['region-1'], rubyRegionIdsByPrimary: { 'region-1': [] },
        draft: { revision: 1, stateChecksum: '5'.repeat(64), regions: [{ regionId: 'region-1', maskMode: 'text',
          polygon: null, padding: 4, dilation: 2, feather: 1, polarity: 'auto', maskEdits: { version: 1, strokes: [] } }] },
        artifacts: [{ artifactId: 'artifact-1', sequence: 1, jobId: 'job-mask', jobItemId: 'item-mask',
          parentChecksum: '2'.repeat(64), qualityChecksum: '3'.repeat(64), recipeChecksum: '5'.repeat(64),
          maskChecksum: '6'.repeat(64), width: 1200, height: 1800, renderScale: 1,
          provider: 'deterministic-mask', modelVersion: 'create-mask-v1', parameterHash: '7'.repeat(64),
          nonzeroPixelCount: 42, bbox: { x: 1, y: 2, width: 3, height: 4 }, createdAt: '2026-08-25T00:00:00Z' }],
        selectedArtifactId: null, review: null } },
      selectedMaskArtifactIds: { 'image-1': 'artifact-1' },
      maskBitmapObservations: { 'image-1': {
        imageId: 'image-1', artifactId: 'artifact-1', imageRevision: 12,
        checksum: '6'.repeat(64), width: 1200, height: 1800, state: 'ready',
      } },
    });
    render(<App />);
    expect(screen.getByText('原图 · mask-off')).toBeInTheDocument();
    expect(screen.getByText('质量底板 · mask-off')).toBeInTheDocument();
    expect(screen.getByText('原图 · mask-on')).toBeInTheDocument();
    expect(screen.getByText('质量底板 · mask-on')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'G7 不可变实际蒙版' })).toHaveValue('artifact-1');
    expect(screen.getByRole('button', { name: '蒙版画笔' })).toBeEnabled();
    expect(within(screen.getByRole('group', { name: 'G7 覆盖检查' })).getAllByRole('checkbox')).toHaveLength(5);
    expect(within(screen.getByRole('group', { name: 'G7 误伤检查' })).getAllByRole('checkbox')).toHaveLength(5);
    expect(screen.getByRole('button', { name: '接受当前实际蒙版' })).toBeDisabled();
    await waitFor(() => expect(useWorkbenchStore.getState().maskBitmapObservations['image-1']).toBeUndefined());
  });
});
