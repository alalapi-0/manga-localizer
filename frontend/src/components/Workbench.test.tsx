import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
    expect(screen.getByText('无文本（正常）')).toBeInTheDocument();
    await user.click(screen.getByRole('checkbox', { name: '批选 image-2.png' }));
    expect(useWorkbenchStore.getState().selectedImageIds).toEqual(['image-1', 'image-2']);

    await user.type(screen.getByRole('searchbox', { name: '搜索图像路径' }), '第二话');
    expect(screen.queryByText('image-1.png')).not.toBeInTheDocument();
    expect(screen.getByText('image-2.png')).toBeInTheDocument();

    await user.click(screen.getByText('image-2.png'));
    expect(useWorkbenchStore.getState().activeImageId).toBe('image-2');
  });

  it('edits source, translation, type, direction, order, and review flags', async () => {
    const user = userEvent.setup();
    seedWorkbench({ selectedRegionIds: ['region-1'] });
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

    expect(screen.getByText('图像处理会跳过；导出 JSON 仍保留此记录')).toBeInTheDocument();

    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[0]).toMatchObject({
      sourceText: '新しい台詞',
      translationText: '新的对白',
      type: 'ruby',
      direction: 'horizontal',
      order: 7,
      confirmed: true,
    });
    const typeOptions = within(screen.getByRole('combobox', { name: '文本类型' }))
      .getAllByRole('option')
      .map((option) => option.getAttribute('value'));
    expect(typeOptions).toEqual(expect.arrayContaining([
      'dialogue', 'narration', 'sound_effect', 'title', 'ruby', 'background', 'unknown',
    ]));
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
    await user.selectOptions(screen.getByRole('combobox', { name: '任务并发数' }), '4');
    await user.click(screen.getByRole('button', { name: /加入队列/ }));

    await waitFor(() => expect(exportProject).toHaveBeenCalledWith(
      'project-1',
      expect.objectContaining({
        imageIds: ['image-1'],
        options: expect.objectContaining({
          format: 'json',
          preserveTree: true,
          conflict: 'rename',
          concurrency: 1,
        }),
      }),
    ));
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
    expect(useWorkbenchStore.getState().compareMode).toBe(true);
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(useWorkbenchStore.getState().selectedRegionIds).toEqual(['region-2']);
    fireEvent.keyDown(window, { key: 'Enter' });
    expect(useWorkbenchStore.getState().regionsByImage['image-1']?.[1]?.confirmed).toBe(true);

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
});
