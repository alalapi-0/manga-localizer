import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, api } from '../api/client';
import App from '../App';
import { seedWorkbench } from '../test/fixtures';
import { resetWorkbenchStore, useWorkbenchStore } from '../store/workbench';
import { FinalReviewPage, RepairContextBanner } from './FinalReviewPage';
import { resetFinalReviewStore, useFinalReviewStore } from './store';
import { finalReviewBatchFixture, finalReviewItemFixture } from './testFixtures';

function renderPage() {
  const items = [
    finalReviewItemFixture('final-item-1'),
    finalReviewItemFixture('final-item-2', {
      verdict: 'approved', reviewedAt: '2026-08-25T01:00:00Z',
    }),
    finalReviewItemFixture('final-item-3', {
      verdict: 'issues', issueCodes: ['ai_inpaint', 'mask'], feedback: '背景有伪影',
      reviewedAt: '2026-08-25T01:30:00Z',
    }),
  ];
  const batch = finalReviewBatchFixture(items);
  useFinalReviewStore.setState({
    batches: [batch], batch, items, activeItemId: items[0]?.id,
    draft: { verdict: 'pending', issueCodes: [], feedback: '' },
  });
  const onOpenWorkbench = vi.fn();
  render(<FinalReviewPage onOpenWorkbench={onOpenWorkbench} />);
  return { items, batch, onOpenWorkbench };
}

const REPAIR_PARAMETER_SET_HASH = '9ede4cd795967a3ec5e3de3ba544b677aabb589b4490c2f8cecc655808bab338';

function repairResult(
  itemId = 'final-item-3',
  sourceImageId = 'image-1',
  patch: Partial<Awaited<ReturnType<typeof api.beginFinalReviewRepair>>> = {},
) {
  return {
    itemId, sourceProjectId: 'project-1', sourceImageId,
    repairProjectId: 'project-1', repairImageId: 'repair-image-new',
    pageGenerationId: 'generation-new', runId: `final-review-${itemId.slice(0, 8)}-r1`,
    finalReviewItemRevision: 1, batchRevision: 1, artifactRevision: 1, nextSequence: 2,
    parameterSetId: 'final-review-repair-v1', parameterSetHash: REPAIR_PARAMETER_SET_HASH,
    idempotent: false,
    ...patch,
  };
}

function saveResult(item: ReturnType<typeof finalReviewItemFixture>, batchRevision = 2) {
  return {
    item: item.verdict !== 'pending' && !item.reviewedAt
      ? { ...item, reviewedAt: '2026-08-25T02:00:00Z' }
      : item,
    batchRevision,
    historyCreated: true,
  };
}

describe('final review page', () => {
  afterEach(() => {
    cleanup();
    resetFinalReviewStore();
    resetWorkbenchStore();
    vi.restoreAllMocks();
  });

  it('shows counts, lazy snapshot thumbnails, the selected snapshot and three verdicts', () => {
    renderPage();
    expect(within(screen.getByLabelText('终审计数')).getByText('3')).toBeInTheDocument();
    const thumbnails = screen.getAllByRole('img').filter((image) => image.getAttribute('loading') === 'lazy');
    expect(thumbnails).toHaveLength(3);
    expect(thumbnails[0]).toHaveAttribute('src', '/api/final-review-items/final-item-1/thumbnail?artifactRevision=1');
    expect(screen.getByAltText('成品冻结证据：final-item-1.png')).toHaveAttribute('src', '/api/final-review-items/final-item-1/artifacts/final?artifactRevision=1');
    expect(screen.getAllByRole('radio')).toHaveLength(3);
    expect(screen.getByRole('button', { name: '保存并下一张' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '显式保存' })).toBeDisabled();
  });

  it('supports multiple issue labels and other validation', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('radio', { name: '有问题' }));
    await user.click(screen.getByRole('checkbox', { name: 'AI 补图' }));
    await user.click(screen.getByRole('checkbox', { name: '抠图蒙版' }));
    expect(useFinalReviewStore.getState().draft?.issueCodes).toEqual(['ai_inpaint', 'mask']);
    await user.click(screen.getByRole('checkbox', { name: '其他' }));
    expect(screen.getByText('选择“其他”时必须填写具体反馈')).toBeInTheDocument();
  });

  it('saves and advances while preserving a failed draft', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('radio', { name: '完全没问题' }));
    const update = vi.spyOn(api, 'updateFinalReviewItem').mockResolvedValue(saveResult(finalReviewItemFixture('final-item-1', { verdict: 'approved', revision: 2 })));
    await user.click(screen.getByRole('button', { name: '保存并下一张' }));
    expect(update).toHaveBeenCalledOnce();
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-2');
  });

  it('advances a clean item without PATCH and returns the mobile layout to preview', async () => {
    const user = userEvent.setup();
    renderPage();
    const update = vi.spyOn(api, 'updateFinalReviewItem');
    await user.click(screen.getByRole('button', { name: '审核与导出' }));

    await user.click(screen.getByRole('button', { name: '保存并下一张' }));

    expect(update).not.toHaveBeenCalled();
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-2');
    expect(screen.getByRole('main')).toHaveAttribute('data-mobile-pane', 'preview');
  });

  it('enables save-and-next for clean or dirty valid drafts, but not conflicts, invalid drafts, or the last visible item', async () => {
    const user = userEvent.setup();
    const { items } = renderPage();
    const saveNext = screen.getByRole('button', { name: '保存并下一张' });
    const explicitSave = screen.getByRole('button', { name: '显式保存' });
    expect(saveNext).toBeEnabled();
    expect(explicitSave).toBeDisabled();

    await user.click(screen.getByRole('radio', { name: '有问题' }));
    expect(saveNext).toBeDisabled();
    expect(explicitSave).toBeDisabled();
    await user.click(screen.getByRole('checkbox', { name: '翻译' }));
    expect(saveNext).toBeEnabled();
    expect(explicitSave).toBeEnabled();

    act(() => useFinalReviewStore.setState({ conflict: true }));
    expect(saveNext).toBeDisabled();
    expect(explicitSave).toBeDisabled();

    act(() => useFinalReviewStore.setState({
      activeItemId: items[2]!.id,
      draft: { verdict: 'issues', issueCodes: ['ai_inpaint', 'mask'], feedback: '背景有伪影' },
      conflict: false,
    }));
    expect(screen.getByRole('button', { name: '保存并下一张' })).toBeDisabled();
  });

  it('locks every draft control while a deferred save is in flight and preserves the submitted draft', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('radio', { name: '有问题' }));
    await user.click(screen.getByRole('checkbox', { name: '翻译' }));
    await user.type(screen.getByLabelText('具体反馈'), '提交时草稿');
    let resolveUpdate!: (item: Awaited<ReturnType<typeof api.updateFinalReviewItem>>) => void;
    vi.spyOn(api, 'updateFinalReviewItem').mockReturnValue(new Promise((resolve) => {
      resolveUpdate = resolve;
    }));

    await user.click(screen.getByRole('button', { name: '显式保存' }));
    expect(screen.getByRole('radio', { name: '未审核' })).toBeDisabled();
    expect(screen.getByRole('radio', { name: '完全没问题' })).toBeDisabled();
    expect(screen.getByRole('radio', { name: '有问题' })).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: '翻译' })).toBeDisabled();
    expect(screen.getByLabelText('具体反馈')).toBeDisabled();
    expect(screen.getByRole('button', { name: '显式保存' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '保存并下一张' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '上一张终审成品' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '下一张终审成品' })).toBeDisabled();
    expect(screen.getAllByRole('listitem').every((item) => item.hasAttribute('disabled'))).toBe(true);

    act(() => {
      useFinalReviewStore.getState().updateDraft({ feedback: '不应写入' });
      useFinalReviewStore.getState().toggleIssue('mask');
    });
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'issues', issueCodes: ['translation'], feedback: '提交时草稿',
    });

    resolveUpdate(saveResult(finalReviewItemFixture('final-item-1', {
      verdict: 'issues', issueCodes: ['translation'], feedback: '提交时草稿', revision: 2,
    })));
    await waitFor(() => expect(useFinalReviewStore.getState().saving).toBe(false));
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'issues', issueCodes: ['translation'], feedback: '提交时草稿',
    });
  });

  it('locks editing and switching after 409 while keeping newest reload and the draft available', async () => {
    const user = userEvent.setup();
    const { items } = renderPage();
    await user.type(screen.getByLabelText('选择一个尚不存在的新目录'), '/safe/conflict-export');
    await user.click(screen.getByRole('radio', { name: '完全没问题' }));
    await user.type(screen.getByLabelText('具体反馈'), '保留的冲突草稿');
    vi.spyOn(api, 'updateFinalReviewItem').mockRejectedValue(new ApiError('revision drift', 409));

    await user.click(screen.getByRole('button', { name: '显式保存' }));
    expect(screen.getByText('终审状态需要重新载入确认')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '载入最新版本并保留草稿' })).toBeEnabled();
    expect(screen.getAllByRole('radio').every((control) => control.hasAttribute('disabled'))).toBe(true);
    expect(screen.getByLabelText('具体反馈')).toBeDisabled();
    expect(screen.getByLabelText('选择终审批次')).toBeDisabled();
    expect(screen.getAllByRole('listitem').every((item) => item.hasAttribute('disabled'))).toBe(true);
    expect(screen.getByLabelText('选择一个尚不存在的新目录')).toBeDisabled();
    expect(screen.getByLabelText('终审导出同名文件处理')).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: '按来源项目保留目录层级' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '导出 1 张已通过成品' })).toBeDisabled();

    const newest = finalReviewItemFixture('final-item-1', {
      revision: 8, verdict: 'issues', issueCodes: ['mask'],
      reviewedAt: '2026-08-25T03:00:00Z',
    });
    const loadedItems = [newest, items[1]!, items[2]!];
    const loadedBatch = finalReviewBatchFixture(loadedItems);
    vi.spyOn(api, 'getFinalReviewBatch')
      .mockResolvedValueOnce({ ...loadedBatch, revision: 1 })
      .mockResolvedValueOnce({ ...loadedBatch, revision: 8 });

    await user.click(screen.getByRole('button', { name: '载入最新版本并保留草稿' }));
    expect(screen.getByText('终审状态需要重新载入确认')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '载入最新版本并保留草稿' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '导出 1 张已通过成品' })).toBeDisabled();
    expect(useFinalReviewStore.getState().items).toEqual(items);
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'approved', issueCodes: [], feedback: '保留的冲突草稿',
    });

    await user.click(screen.getByRole('button', { name: '载入最新版本并保留草稿' }));
    expect(useFinalReviewStore.getState().items[0]).toMatchObject({ revision: 8, verdict: 'issues' });
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'approved', issueCodes: [], feedback: '保留的冲突草稿',
    });
  });

  it('honestly reports an unknown save outcome and preserves reload plus the local draft', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('radio', { name: '完全没问题' }));
    await user.type(screen.getByLabelText('具体反馈'), '网络中断草稿');
    vi.spyOn(api, 'updateFinalReviewItem').mockRejectedValue(new Error('connection reset'));

    await user.click(screen.getByRole('button', { name: '显式保存' }));
    expect(screen.getByText('终审状态需要重新载入确认')).toBeInTheDocument();
    expect(screen.getByText(/操作结果未知.*请求可能已在服务端完成/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '载入最新版本并保留草稿' })).toBeEnabled();
    expect(screen.getByLabelText('具体反馈')).toBeDisabled();
    expect(useFinalReviewStore.getState().draft).toEqual({
      verdict: 'approved', issueCodes: [], feedback: '网络中断草稿',
    });
  });

  it('warns before losing a draft and does not let workbench shortcuts consume textarea arrows', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.type(screen.getByLabelText('具体反馈'), '草稿');
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    await user.click(screen.getByRole('listitem', { name: /第 2 张/ }));
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-1');
    fireEvent.keyDown(screen.getByLabelText('具体反馈'), { key: 'ArrowRight' });
    expect(useFinalReviewStore.getState().activeItemId).toBe('final-item-1');
  });

  it('keeps a partially reviewed batch non-exportable even after a path is entered', async () => {
    const user = userEvent.setup();
    renderPage();
    const exportSpy = vi.spyOn(api, 'exportApprovedFinalReviewItems');
    await user.type(screen.getByLabelText('选择一个尚不存在的新目录'), '/chosen/final');
    expect(screen.getByRole('button', { name: '导出 1 张已通过成品' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '导出 1 张已通过成品' }));
    expect(exportSpy).not.toHaveBeenCalled();
  });

  it('exports an exact all-approved batch with safe rename and no skip option', async () => {
    const user = userEvent.setup();
    const { items } = renderPage();
    const approvedItems = items.map((item, index) => ({
      ...item,
      verdict: 'approved' as const,
      issueCodes: [],
      feedback: '',
      reviewedAt: `2026-08-25T0${index + 1}:00:00Z`,
    }));
    const batch = finalReviewBatchFixture(approvedItems);
    act(() => useFinalReviewStore.setState({
      batch, items: approvedItems, activeItemId: approvedItems[0]!.id,
      draft: { verdict: 'approved', issueCodes: [], feedback: '' },
    }));
    const exportSpy = vi.spyOn(api, 'exportApprovedFinalReviewItems').mockResolvedValue({
      batchId: 'final-batch-1', outputPath: '/chosen/final', exportedCount: 3,
      skippedPendingCount: 0, skippedIssuesCount: 0, skippedCollisionCount: 0,
      manifestPath: '/chosen/final/manifest.json',
    });

    await user.type(screen.getByLabelText('选择一个尚不存在的新目录'), '/chosen/final');
    expect(screen.getByLabelText('终审导出同名文件处理')).toHaveTextContent('安全重命名（终态必需）');
    expect(screen.getByLabelText('终审导出同名文件处理')).not.toHaveTextContent('跳过');
    await user.click(screen.getByRole('button', { name: '导出 3 张已通过成品' }));

    expect(exportSpy).toHaveBeenCalledWith('final-batch-1', expect.objectContaining({
      outputPath: '/chosen/final', conflict: 'rename', expectedBatchRevision: 1,
    }));
    expect(await screen.findByText('导出完成：3 张')).toBeInTheDocument();
  });

  it('opens an issues item in its source workbench and preserves repair context', async () => {
    const user = userEvent.setup();
    const { items, onOpenWorkbench } = renderPage();
    seedWorkbench();
    const issue = { ...items[2]!, sourceImageId: 'image-1' };
    useFinalReviewStore.setState((state) => ({
      items: state.items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: [...issue.issueCodes], feedback: issue.feedback },
    }));
    vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue(repairResult(issue.id, issue.sourceImageId));
    const selectProject = vi.fn().mockResolvedValue(true);
    const selectImage = vi.fn().mockResolvedValue(true);
    useWorkbenchStore.setState({ selectProject, selectImage });

    await user.click(screen.getByRole('button', { name: '创建新 G0 并进入修复' }));
    expect(onOpenWorkbench).toHaveBeenCalledOnce();
    expect(selectProject).toHaveBeenCalledWith(issue.sourceProjectId, true);
    expect(selectImage).toHaveBeenCalledWith('repair-image-new');
    expect(useFinalReviewStore.getState().repairContext).toMatchObject({
      itemId: issue.id, issueCodes: ['ai_inpaint', 'mask'], feedback: '背景有伪影',
    });
  });

  it('keeps repair locked through delayed workbench navigation so rapid clicks send one request', async () => {
    const user = userEvent.setup();
    const { items, onOpenWorkbench } = renderPage();
    const issue = { ...items[2]!, sourceImageId: 'image-1' };
    useFinalReviewStore.setState((state) => ({
      items: state.items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: [...issue.issueCodes], feedback: issue.feedback },
    }));
    const repair = vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue(repairResult(issue.id, issue.sourceImageId));
    let resolveProject!: (value: boolean) => void;
    useWorkbenchStore.setState({
      selectProject: vi.fn().mockReturnValue(new Promise((resolve) => { resolveProject = resolve; })),
      selectImage: vi.fn().mockResolvedValue(true),
    });

    await user.click(screen.getByRole('button', { name: '创建新 G0 并进入修复' }));
    await waitFor(() => expect(useFinalReviewStore.getState().operation).toBe('repair'));
    const button = screen.getByRole('button', { name: '创建新 G0 并进入修复' });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(repair).toHaveBeenCalledOnce();
    act(() => resolveProject(true));
    await waitFor(() => expect(onOpenWorkbench).toHaveBeenCalledOnce());
    expect(useFinalReviewStore.getState().operation).toBeNull();
  });

  it.each([
    ['the original source image', { repairImageId: 'image-1' }],
    ['a different project', { repairProjectId: 'project-forged' }],
  ])('does not navigate for a complete repair handoff bound to %s', async (_label, patch) => {
    const user = userEvent.setup();
    const { items, onOpenWorkbench } = renderPage();
    const issue = { ...items[2]!, sourceImageId: 'image-1' };
    useFinalReviewStore.setState((state) => ({
      items: state.items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: [...issue.issueCodes], feedback: issue.feedback },
    }));
    vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue(
      repairResult(issue.id, issue.sourceImageId, patch),
    );
    const selectProject = vi.fn().mockResolvedValue(true);
    const selectImage = vi.fn().mockResolvedValue(true);
    useWorkbenchStore.setState({ selectProject, selectImage });

    await user.click(screen.getByRole('button', { name: '创建新 G0 并进入修复' }));

    expect(onOpenWorkbench).not.toHaveBeenCalled();
    expect(selectProject).not.toHaveBeenCalled();
    expect(selectImage).not.toHaveBeenCalled();
    expect(useFinalReviewStore.getState()).toMatchObject({
      conflict: true,
      operation: null,
      repairContext: null,
      draft: { verdict: 'issues', issueCodes: issue.issueCodes, feedback: issue.feedback },
    });
    expect(screen.getByRole('button', { name: '载入最新版本并保留草稿' })).toBeEnabled();
  });

  it('requires confirmation before refreshing a repaired snapshot', async () => {
    const user = userEvent.setup();
    const { items } = renderPage();
    const issue = { ...items[2]!, currentArtifactStale: true };
    useFinalReviewStore.setState({
      items: items.map((item) => item.id === issue.id ? issue : item),
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: [...issue.issueCodes], feedback: issue.feedback },
    });
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const refresh = vi.spyOn(api, 'refreshFinalReviewItem');

    await user.click(screen.getByRole('button', { name: '同步修复后的成品' }));
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('保留旧快照历史'));
    expect(refresh).not.toHaveBeenCalled();
  });

  it('enables snapshot refresh only when the source project has a newer final artifact', () => {
    const { items } = renderPage();
    const button = screen.getByRole('button', { name: '同步修复后的成品' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', '源项目暂无更新成品');

    act(() => useFinalReviewStore.setState({
      items: items.map((item) => item.id === 'final-item-1'
        ? { ...item, currentArtifactStale: true }
        : item),
    }));
    expect(screen.getByRole('button', { name: '同步修复后的成品' })).toBeEnabled();
  });

  it('clears repair session context when opening the source project fails', async () => {
    const user = userEvent.setup();
    const { items } = renderPage();
    seedWorkbench();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: [...issue.issueCodes], feedback: issue.feedback },
    });
    vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue(repairResult(issue.id, issue.sourceImageId));
    useWorkbenchStore.setState({ selectProject: vi.fn().mockResolvedValue(false) });

    await user.click(screen.getByRole('button', { name: '创建新 G0 并进入修复' }));
    expect(useFinalReviewStore.getState().repairContext).toBeNull();
    expect(useFinalReviewStore.getState().operation).toBeNull();
    expect(window.sessionStorage.getItem('manga-localizer-final-review-repair')).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent('无法打开终审项的来源项目');
    await user.click(screen.getByRole('listitem', { name: /第 1 张/ }));
    expect(screen.queryByText('无法进入工作台修复')).not.toBeInTheDocument();
  });

  it('clears repair session context when opening the source image fails', async () => {
    const user = userEvent.setup();
    const { items } = renderPage();
    seedWorkbench();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: [...issue.issueCodes], feedback: issue.feedback },
    });
    vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue(repairResult(issue.id, issue.sourceImageId));
    useWorkbenchStore.setState({
      selectProject: vi.fn().mockResolvedValue(true),
      selectImage: vi.fn().mockResolvedValue(false),
    });

    await user.click(screen.getByRole('button', { name: '创建新 G0 并进入修复' }));
    expect(useFinalReviewStore.getState().repairContext).toBeNull();
    expect(useFinalReviewStore.getState().operation).toBeNull();
    expect(window.sessionStorage.getItem('manga-localizer-final-review-repair')).toBeNull();
    expect(screen.getByRole('alert')).toHaveTextContent('无法打开终审项的来源图片');
  });

  it('disables approved-only export when no item is approved', async () => {
    const user = userEvent.setup();
    const item = finalReviewItemFixture();
    const batch = finalReviewBatchFixture([item]);
    useFinalReviewStore.setState({
      batches: [batch], batch, items: [item], activeItemId: item.id,
      draft: { verdict: 'pending', issueCodes: [], feedback: '' },
    });
    render(<FinalReviewPage onOpenWorkbench={vi.fn()} />);
    expect(screen.getByText('为防止覆盖已有文件，输出路径必须是一个尚不存在的新目录。')).toBeInTheDocument();
    await user.type(screen.getByLabelText('选择一个尚不存在的新目录'), '/chosen/empty');
    expect(screen.getByRole('button', { name: '导出 0 张已通过成品' })).toBeDisabled();
  });

  it('keeps the repair feedback visible with a return-to-review action', async () => {
    const user = userEvent.setup();
    const onReturn = vi.fn();
    useFinalReviewStore.setState({
      repairContext: {
        batchId: 'final-batch-1', itemId: 'final-item-3',
        issueCodes: ['translation', 'typesetting'], feedback: '第三个气泡需要重排',
        sourceProjectId: 'project-1', sourceImageId: 'image-3', pageGenerationId: 'generation-3',
        repairProjectId: 'project-1', repairImageId: 'image-3',
        runId: 'run-3', itemRevision: 2, batchRevision: 3, artifactRevision: 1,
        nextSequence: 1, parameterSetId: 'final-review-repair-v1', parameterSetHash: 'a'.repeat(64),
      },
    });
    render(<RepairContextBanner onReturn={onReturn} />);
    expect(screen.getByText(/翻译、嵌字排版.*第三个气泡需要重排/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '返回终审' }));
    expect(onReturn).toHaveBeenCalledOnce();
  });

  it('switches the mobile review pane without changing the batch', async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole('button', { name: '审核与导出' }));
    expect(document.querySelector('.final-review')).toHaveAttribute('data-mobile-pane', 'review');
  });

  it('is a top-level app view and disables workbench Delete and Enter shortcuts there', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    const item = finalReviewItemFixture();
    const batch = finalReviewBatchFixture([item]);
    useFinalReviewStore.setState({
      batches: [batch], batch, items: [item], activeItemId: item.id,
      draft: { verdict: 'pending', issueCodes: [], feedback: '' },
    });
    const deleteSelectedRegions = vi.fn();
    const setRegionConfirmed = vi.fn().mockResolvedValue(true);
    useWorkbenchStore.setState({ deleteSelectedRegions, setRegionConfirmed });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '最终验收' }));
    fireEvent.keyDown(window, { key: 'Delete' });
    fireEvent.keyDown(window, { key: 'Enter' });
    expect(deleteSelectedRegions).not.toHaveBeenCalled();
    expect(setRegionConfirmed).not.toHaveBeenCalled();
    expect(screen.getByRole('main')).toHaveClass('final-review');
  });

  it('keeps final review reachable when the workbench project fails to initialize', async () => {
    const user = userEvent.setup();
    const item = finalReviewItemFixture();
    const batch = finalReviewBatchFixture([item]);
    useWorkbenchStore.setState({ loadState: 'error', currentProject: null });
    useFinalReviewStore.setState({
      batches: [batch], batch, items: [item], activeItemId: item.id,
      draft: { verdict: 'pending', issueCodes: [], feedback: '' },
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '最终验收' }));
    expect(screen.getByRole('main')).toHaveClass('final-review');
  });

  it('blocks top-level view navigation while the unified operation lock is held', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    const item = finalReviewItemFixture();
    const batch = finalReviewBatchFixture([item]);
    useFinalReviewStore.setState({
      batches: [batch], batch, items: [item], activeItemId: item.id,
      draft: { verdict: 'pending', issueCodes: [], feedback: '' }, operation: 'refresh', refreshing: true,
    });
    render(<App />);
    await user.click(screen.getByRole('button', { name: '最终验收' }));
    expect(screen.getByRole('main')).toHaveClass('final-review');
    await user.click(screen.getByRole('button', { name: '工作台' }));
    expect(screen.getByRole('main')).toHaveClass('final-review');
    act(() => useFinalReviewStore.setState({ operation: null, refreshing: false, conflict: true }));
    await user.click(screen.getByRole('button', { name: '工作台' }));
    expect(screen.getByRole('main')).toHaveClass('final-review');
  });

  it('releases the repair lock only after navigation and then switches App to the workbench', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    const item = finalReviewItemFixture('final-item-3', {
      verdict: 'issues', issueCodes: ['mask'], feedback: '需要修复', sourceImageId: 'image-1',
    });
    const batch = finalReviewBatchFixture([item]);
    useFinalReviewStore.setState({
      batches: [batch], batch, items: [item], activeItemId: item.id,
      draft: { verdict: 'issues', issueCodes: ['mask'], feedback: '需要修复' },
    });
    vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue(repairResult(item.id, item.sourceImageId));
    useWorkbenchStore.setState({
      selectProject: vi.fn().mockResolvedValue(true),
      selectImage: vi.fn().mockResolvedValue(true),
    });
    render(<App />);

    await user.click(screen.getByRole('button', { name: '最终验收' }));
    await user.click(screen.getByRole('button', { name: '创建新 G0 并进入修复' }));
    await waitFor(() => expect(document.querySelector('.workbench-grid')).toBeInTheDocument());
    expect(useFinalReviewStore.getState().operation).toBeNull();
    expect(screen.getByText('正在处理终审反馈')).toBeInTheDocument();
  });

  it('shows all five authoritative evidence states and never falls back to a live image URL', async () => {
    const user = userEvent.setup();
    const base = finalReviewItemFixture('evidence-item', { artifactRevision: 7 });
    const item = finalReviewItemFixture('evidence-item', {
      artifactRevision: 7,
      evidence: {
        ...base.evidence,
        mask: { ...base.evidence.mask, artifactRevision: 7, availability: 'not-applicable', reason: 'no-text page' },
        clean: { ...base.evidence.clean, artifactRevision: 7, availability: 'unavailable', reason: 'legacy evidence absent', url: '/live/project/forbidden.png' },
      },
    });
    const batch = finalReviewBatchFixture([item]);
    useFinalReviewStore.setState({
      batch, batches: [batch], items: [item], activeItemId: item.id,
      draft: { verdict: 'pending', issueCodes: [], feedback: '' },
    });
    render(<FinalReviewPage onOpenWorkbench={vi.fn()} />);

    expect(screen.getAllByRole('tab')).toHaveLength(5);
    expect(screen.getByAltText('成品冻结证据：evidence-item.png')).toHaveAttribute(
      'src', '/api/final-review-items/evidence-item/artifacts/final?artifactRevision=7',
    );
    expect(screen.getByText('accept-revision-final-evidence-item')).toBeInTheDocument();
    await user.click(screen.getByRole('tab', { name: /蒙版/ }));
    expect(screen.getByText('此阶段不适用')).toBeInTheDocument();
    expect(document.querySelector('.final-review__image-stage img')).toBeNull();
    await user.click(screen.getByRole('tab', { name: /净图/ }));
    expect(screen.getByText('历史证据不可用')).toBeInTheDocument();
    expect(document.querySelector('img[src="/live/project/forbidden.png"]')).toBeNull();
  });

  it('locks every operation-sensitive control and keeps legacy approved evidence read-only', () => {
    const item = finalReviewItemFixture('legacy-approved', {
      formatVersion: 1, strictEvidence: false, verdict: 'approved', currentArtifactStale: true,
    });
    const batch = finalReviewBatchFixture([item]);
    useFinalReviewStore.setState({
      batch, batches: [batch], items: [item], activeItemId: item.id,
      draft: { verdict: 'approved', issueCodes: [], feedback: '' },
    });
    render(<FinalReviewPage onOpenWorkbench={vi.fn()} />);

    expect(screen.getByText(/旧版已通过项保持只读/)).toBeInTheDocument();
    expect(screen.getAllByRole('radio').every((control) => control.hasAttribute('disabled'))).toBe(true);
    expect(screen.getByRole('button', { name: '同步修复后的成品' })).toBeDisabled();
    act(() => useFinalReviewStore.setState({ operation: 'export', exporting: true }));
    expect(screen.getAllByRole('tab').every((tab) => tab.hasAttribute('disabled'))).toBe(true);
    expect(screen.getByLabelText('选择终审批次')).toBeDisabled();
  });

  it('keeps a legacy issues decision read-only but permits its explicit fresh-G0 transition', async () => {
    const user = userEvent.setup();
    seedWorkbench();
    const item = finalReviewItemFixture('legacy-issue', {
      formatVersion: 1, strictEvidence: false, verdict: 'issues', issueCodes: ['mask'], feedback: '旧反馈',
      sourceImageId: 'image-1',
    });
    const batch = finalReviewBatchFixture([item]);
    useFinalReviewStore.setState({
      batch, batches: [batch], items: [item], activeItemId: item.id,
      draft: { verdict: 'issues', issueCodes: ['mask'], feedback: '旧反馈' },
    });
    const repair = vi.spyOn(api, 'beginFinalReviewRepair').mockResolvedValue(repairResult(item.id, item.sourceImageId));
    useWorkbenchStore.setState({
      selectProject: vi.fn().mockResolvedValue(true),
      selectImage: vi.fn().mockResolvedValue(true),
    });
    const onOpenWorkbench = vi.fn();
    render(<FinalReviewPage onOpenWorkbench={onOpenWorkbench} />);

    expect(screen.getByText(/旧版问题项的既有 verdict 与反馈保持只读/)).toBeInTheDocument();
    expect(screen.getAllByRole('radio').every((control) => control.hasAttribute('disabled'))).toBe(true);
    expect(screen.getByLabelText('具体反馈')).toBeDisabled();
    const button = screen.getByRole('button', { name: '创建新 G0 并进入修复' });
    expect(button).toBeEnabled();
    await user.click(button);
    expect(repair).toHaveBeenCalledOnce();
    expect(onOpenWorkbench).toHaveBeenCalledOnce();
    expect(useFinalReviewStore.getState().items[0]?.verdict).toBe('issues');
  });

  it('does not navigate when the fresh-G0 repair handoff fails', async () => {
    const user = userEvent.setup();
    const { items, onOpenWorkbench } = renderPage();
    const issue = items[2]!;
    useFinalReviewStore.setState({
      activeItemId: issue.id,
      draft: { verdict: 'issues', issueCodes: [...issue.issueCodes], feedback: issue.feedback },
    });
    vi.spyOn(api, 'beginFinalReviewRepair').mockRejectedValue(new Error('repair unavailable'));

    await user.click(screen.getByRole('button', { name: '创建新 G0 并进入修复' }));
    expect(onOpenWorkbench).not.toHaveBeenCalled();
    expect(useFinalReviewStore.getState().repairContext).toBeNull();
    expect(useFinalReviewStore.getState().operation).toBeNull();
    expect(useFinalReviewStore.getState().items[2]?.verdict).toBe('issues');
    expect(screen.getByRole('alert')).toHaveTextContent('repair unavailable');
  });
});
