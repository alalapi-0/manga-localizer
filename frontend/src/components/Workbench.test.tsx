import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../App';
import { api } from '../api/client';
import {
  capabilitiesFixture,
  imageFixture,
  jobFixture,
  projectFixture,
  regionFixture,
  seedWorkbench,
} from '../test/fixtures';
import { resetWorkbenchStore, useWorkbenchStore } from '../store/workbench';

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

    await user.click(screen.getByRole('button', { name: '批处理与导出' }));
    await user.click(screen.getByRole('checkbox', { name: /安全导出/ }));
    expect(screen.getByText('还有 1 页排版溢出')).toBeInTheDocument();
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

  it('only enables page review after every active region is confirmed and trusted', () => {
    seedWorkbench({
      images: [imageFixture('image-1', { trustReviewCount: 2 })],
      regions: [
        regionFixture('region-1', { confirmed: false, trustDisposition: 'trusted' }),
        regionFixture('region-2', { confirmed: true, trustDisposition: 'review' }),
        regionFixture('region-3', { ignored: true }),
      ],
    });
    render(<App />);

    expect(screen.getByText('还有 2 个活动文本框尚未确认并信任')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '还需确认并信任 2 个文本框' })).toBeDisabled();

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
    expect(within(drawer).getByRole('status')).toHaveTextContent('1 个 OCR 文本框待信任确认');
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

  it('stores a per-region inpainting provider override and exposes an explicit rebuild action', async () => {
    const user = userEvent.setup();
    seedWorkbench({ selectedRegionIds: ['region-1'] });
    render(<App />);

    await user.click(screen.getByRole('tab', { name: '修复' }));
    const provider = screen.getByRole('combobox', { name: '区域修复 Provider' });
    expect(provider).toHaveValue('');
    expect(within(provider).getByRole('option', { name: /继承项目设置/ })).toBeInTheDocument();

    await user.selectOptions(provider, 'lama-onnx');

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]?.repair).toMatchObject({
      inpainterProvider: 'lama-onnx',
    });
    expect(screen.getByRole('button', { name: '重建当前页' })).toBeEnabled();
    expect(screen.getByText('LaMa AI 背景修复')).toBeInTheDocument();
  });

  it('lets the editor choose among inpaint candidates from the repair inspector', async () => {
    const user = userEvent.setup();
    const image = imageFixture('image-1', {
      status: { ...imageFixture('image-1').status, inpaint: 'done' },
      inpaintCandidate: 'primary',
      inpaintCandidates: [
        { id: 'primary', label: '当前 Provider 结果', anomalies: [] },
        { id: 'lineart-guided', label: '线稿引导(结构+纹理)', anomalies: ['possible-smear'] },
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
    await user.click(screen.getByRole('radio', { name: /线稿引导/ }));
    expect(select).toHaveBeenCalledWith('image-1', 'lineart-guided', 1);
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
});
