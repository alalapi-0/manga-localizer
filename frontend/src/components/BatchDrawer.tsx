import { useMemo, useState } from 'react';

import { activeImage, imageHasTypesetOverflow, useWorkbenchStore } from '../store/workbench';
import type { ExportOptions, Job, JobKind, ProviderCapability } from '../types';
import { Field, IconButton } from './Primitives';

const kindLabels: Record<JobKind, string> = {
  preprocess: '图片增强',
  detect: '文字检测',
  ocr: '日文 OCR',
  translate: '翻译',
  inpaint: '擦字修复',
  typeset: '嵌字排版',
  export: '安全导出',
};

const statusLabels: Record<Job['status'], string> = {
  queued: '排队中',
  running: '处理中',
  paused: '已暂停',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
  unavailable: '能力不可用',
};

function percent(job: Job): number {
  if (job.total > 0) return Math.round((job.completed / job.total) * 100);
  return Math.round(job.progress <= 1 ? job.progress * 100 : job.progress);
}

function capabilityForKind(
  kind: JobKind,
  providers: ProviderCapability[],
  settings: {
    preprocessorProvider: string;
    detectorProvider: string;
    ocrProvider: string;
    translatorProvider: string;
    inpainterProvider: string;
  },
): { available: boolean; message: string; mock: boolean } {
  if (kind === 'export') return { available: true, message: '本地安全导出', mock: false };
  if (kind === 'preprocess') {
    const preprocessor = providers.find((provider) => provider.kind === 'preprocessor' && provider.id === settings.preprocessorProvider);
    return {
      available: Boolean(preprocessor?.available),
      message: preprocessor?.available ? preprocessor.label : preprocessor?.reason || '图片增强能力未报告',
      mock: false,
    };
  }
  if (kind === 'detect') {
    const detector = providers.find((provider) => provider.kind === 'detector' && provider.id === settings.detectorProvider);
    return {
      available: Boolean(detector?.available),
      message: detector?.available ? detector.label : detector?.reason || '文字检测能力未报告',
      mock: Boolean(detector?.isMock),
    };
  }
  if (kind === 'ocr') {
    const ocr = providers.find((provider) => provider.kind === 'ocr' && provider.id === settings.ocrProvider);
    return {
      available: Boolean(ocr?.available),
      message: ocr?.available ? ocr.label : ocr?.reason || 'OCR 能力未报告',
      mock: Boolean(ocr?.isMock),
    };
  }
  const providerKind = kind === 'translate' ? 'translator' : kind === 'inpaint' ? 'inpainter' : 'typesetter';
  const providerId = kind === 'translate'
    ? settings.translatorProvider
    : kind === 'inpaint'
      ? settings.inpainterProvider
      : providers.find((provider) => provider.kind === 'typesetter' && provider.available)?.id;
  const provider = providers.find((entry) => entry.kind === providerKind && entry.id === providerId);
  return {
    available: Boolean(provider?.available),
    message: provider?.available ? provider.label : provider?.reason || `${kindLabels[kind]}能力未报告`,
    mock: Boolean(provider?.isMock),
  };
}

function JobCard({ job }: { job: Job }) {
  const runJobAction = useWorkbenchStore((state) => state.runJobAction);
  const images = useWorkbenchStore((state) => state.images);
  const value = Math.max(0, Math.min(100, percent(job)));
  const repaired = job.kind === 'inpaint'
    ? job.items.reduce((total, item) => total + Number(item.output?.repairedRegionCount ?? 0), 0)
    : null;
  const skipped = job.kind === 'inpaint'
    ? job.items.reduce((total, item) => total + Number(item.output?.skippedRegionCount ?? 0), 0)
    : null;
  const hasRepairMetrics = job.kind === 'inpaint'
    && job.items.some((item) => item.output?.repairedRegionCount !== undefined);
  const displayedItems = [
    ...job.items.slice(0, 20),
    ...job.items.slice(20).filter((item) => item.status === 'failed'),
  ];
  return (
    <article className={`job-card job-card--${job.status}`}>
      <header>
        <div><strong>{kindLabels[job.kind]}</strong><span>{statusLabels[job.status]}</span></div>
        <b>{value}%</b>
      </header>
      <progress aria-label={`${kindLabels[job.kind]}进度`} max={100} value={value} />
      <div className="job-card__meta">
        <span>{job.completed} / {job.total || '—'} 项</span>
        {hasRepairMetrics ? (
          <span className={repaired === 0 ? 'job-error' : undefined}>
            修复 {repaired} · 跳过 {skipped}{repaired === 0 ? '（未改动图像）' : ''}
          </span>
        ) : null}
        {job.error ? <span className="job-error">{job.error}</span> : null}
      </div>
      {job.items.length ? (
        <details>
          <summary>查看 {job.items.length} 个队列项{displayedItems.length < job.items.length ? `（显示 ${displayedItems.length}）` : ''}</summary>
          <div className="job-items">
            {displayedItems.map((item) => (
              <div key={item.id}><span>{images.find((image) => image.id === item.imageId)?.relativePath ?? item.label}</span><em>{statusLabels[item.status]}</em>{item.error ? <small>{item.error}</small> : null}</div>
            ))}
          </div>
        </details>
      ) : null}
      <footer>
        {job.status === 'running' || job.status === 'queued' ? (
          <button className="text-button" onClick={() => void runJobAction(job.id, 'pause')} type="button">暂停</button>
        ) : null}
        {job.status === 'paused' ? (
          <button className="text-button" onClick={() => void runJobAction(job.id, 'resume')} type="button">继续</button>
        ) : null}
        {['queued', 'running', 'paused'].includes(job.status) ? (
          <button className="text-button text-button--danger" onClick={() => void runJobAction(job.id, 'cancel')} type="button">取消</button>
        ) : null}
        {job.status === 'failed' || job.status === 'cancelled' ? (
          <button className="text-button" onClick={() => void runJobAction(job.id, 'retry')} type="button">重试失败项</button>
        ) : null}
      </footer>
    </article>
  );
}

export function BatchDrawer() {
  const open = useWorkbenchStore((state) => state.drawerOpen);
  const setOpen = useWorkbenchStore((state) => state.setDrawerOpen);
  const project = useWorkbenchStore((state) => state.currentProject);
  const images = useWorkbenchStore((state) => state.images);
  const regionsByImage = useWorkbenchStore((state) => state.regionsByImage);
  const selectedImageIds = useWorkbenchStore((state) => state.selectedImageIds);
  const providers = useWorkbenchStore((state) => state.capabilities.providers);
  const jobs = useWorkbenchStore((state) => state.jobs);
  const currentImage = useWorkbenchStore(activeImage);
  const startBatch = useWorkbenchStore((state) => state.startBatch);
  const [target, setTarget] = useState<'selected' | 'current' | 'all'>('selected');
  const [steps, setSteps] = useState<Record<JobKind, boolean>>({
    preprocess: false,
    detect: true,
    ocr: true,
    translate: false,
    inpaint: false,
    typeset: false,
    export: false,
  });
  const [exportOptions, setExportOptions] = useState<ExportOptions>({
    format: 'both',
    imageVariant: 'typeset',
    conflict: 'rename',
    preserveTree: true,
  });
  const [concurrency, setConcurrency] = useState(2);
  const [starting, setStarting] = useState(false);

  const imageIds = useMemo(() => {
    if (target === 'all') return images.map((image) => image.id);
    if (target === 'current') return currentImage ? [currentImage.id] : [];
    return selectedImageIds;
  }, [currentImage, images, selectedImageIds, target]);

  const unreviewedImageCount = imageIds.filter((imageId) => {
    const state = images.find((image) => image.id === imageId)?.status.reviewState;
    return state !== 'reviewed' && state !== 'no-text-reviewed';
  }).length;
  const imageExportBlocked = steps.export
    && exportOptions.format !== 'json'
    && unreviewedImageCount > 0;
  const missingTypesetCount = imageIds.filter((imageId) =>
    images.find((image) => image.id === imageId)?.status.typeset !== 'done'
  ).length;
  const missingInpaintCount = imageIds.filter((imageId) =>
    images.find((image) => image.id === imageId)?.status.inpaint !== 'done'
  ).length;
  const requiresTypeset = exportOptions.imageVariant === 'typeset'
    || exportOptions.imageVariant === 'both';
  // Typeset output is composited from the clean background, so it also requires
  // an accepted inpaint review even when the clean plate is not exported separately.
  const requiresInpaint = exportOptions.format !== 'json';
  const unacceptedTypesetCount = imageIds.filter((imageId) =>
    images.find((image) => image.id === imageId)?.stageReviews?.typeset?.state !== 'accepted'
  ).length;
  const unacceptedInpaintCount = imageIds.filter((imageId) =>
    images.find((image) => image.id === imageId)?.stageReviews?.inpaint?.state !== 'accepted'
  ).length;
  const overflowImageCount = imageIds.filter((imageId) =>
    imageHasTypesetOverflow(images.find((image) => image.id === imageId)),
  ).length;
  const generatedImageExportBlocked = steps.export
    && exportOptions.format !== 'json'
    && ((requiresTypeset && missingTypesetCount > 0)
      || (requiresInpaint && missingInpaintCount > 0));
  const stageReviewExportBlocked = steps.export
    && exportOptions.format !== 'json'
    && ((requiresTypeset && unacceptedTypesetCount > 0)
      || (requiresInpaint && unacceptedInpaintCount > 0));
  const pipelineExportBlocked = steps.export
    && (Object.keys(steps) as JobKind[]).some((kind) => kind !== 'export' && steps[kind]);
  const trustGatedKinds: JobKind[] = ['translate', 'inpaint', 'typeset'];
  const ocrDownstreamMixed = steps.ocr && trustGatedKinds.some((kind) => steps[kind]);
  const trustGateEnabled = trustGatedKinds.some((kind) => steps[kind]);
  const pendingTrustCount = trustGateEnabled
    ? imageIds.reduce((total, imageId) => {
        const image = images.find((entry) => entry.id === imageId);
        const serverPending = Math.max(0, Number(image?.trustReviewCount ?? 0));
        const loadedPending = (regionsByImage[imageId] ?? []).filter(
          (region) => !region.ignored && region.trustDisposition !== 'trusted',
        ).length;
        return total + Math.max(serverPending, loadedPending);
      }, 0)
    : 0;
  const trustGateBlocked = trustGateEnabled && pendingTrustCount > 0;
  const outputPathHint = exportOptions.format === 'json'
    ? '仅写入文本元数据；不会复制图像，也不会创建可重开的项目快照。'
    : exportOptions.format === 'images'
      ? '仅写入所选生成图像；不会创建可重开的项目快照。'
      : '同时写入图像与 JSON；自定义目录可包含完整、可重开的 project/ 项目副本及源图副本。';

  if (!open) return null;
  const selectedKinds = (Object.keys(steps) as JobKind[]).filter((kind) => steps[kind]);

  async function run() {
    if (imageExportBlocked || generatedImageExportBlocked || stageReviewExportBlocked || pipelineExportBlocked || ocrDownstreamMixed || trustGateBlocked) return;
    setStarting(true);
    const success = await startBatch(selectedKinds, imageIds, exportOptions, concurrency);
    setStarting(false);
    if (success) setSteps((current) => ({ ...current, preprocess: false, detect: false, ocr: false, translate: false, inpaint: false, typeset: false, export: false }));
  }

  return (
    <div className="drawer-backdrop" onMouseDown={() => setOpen(false)} role="presentation">
      <aside aria-label="批处理与导出" aria-modal="true" className="batch-drawer" onMouseDown={(event) => event.stopPropagation()} role="dialog">
        <header className="batch-drawer__header">
          <div><span className="section-kicker">后台队列</span><h2>批处理与导出</h2></div>
          <IconButton aria-label="关闭批处理抽屉" onClick={() => setOpen(false)}>×</IconButton>
        </header>
        <div className="batch-drawer__body">
          <section className="drawer-section">
            <h3>1. 处理范围</h3>
            <div className="choice-cards">
              <label><input checked={target === 'selected'} disabled={!selectedImageIds.length} name="target" onChange={() => setTarget('selected')} type="radio" /><span><b>批选图像</b><small>{selectedImageIds.length} 张</small></span></label>
              <label><input checked={target === 'current'} disabled={!currentImage} name="target" onChange={() => setTarget('current')} type="radio" /><span><b>当前页</b><small>{currentImage?.name ?? '未选择'}</small></span></label>
              <label><input checked={target === 'all'} disabled={!images.length} name="target" onChange={() => setTarget('all')} type="radio" /><span><b>全部图像</b><small>{images.length} 张</small></span></label>
            </div>
          </section>
          <section className="drawer-section">
            <h3>2. 处理步骤</h3>
            <div className="pipeline-steps">
              {(Object.keys(kindLabels) as JobKind[]).map((kind, index) => {
                const capability = project
                  ? capabilityForKind(kind, providers, project.settings)
                  : { available: false, message: '未打开项目', mock: false };
                return (
                  <label className={`${capability.available ? '' : 'is-unavailable'} ${capability.mock ? 'is-mock' : ''}`} key={kind}>
                    <span className="pipeline-steps__index">{index + 1}</span>
                    <input
                      checked={steps[kind]}
                      disabled={!capability.available}
                      onChange={(event) => setSteps((current) => ({ ...current, [kind]: event.target.checked }))}
                      type="checkbox"
                    />
                    <span><b>{kindLabels[kind]}</b><small>{capability.message}</small></span>
                    {capability.mock ? <em>演示 MOCK</em> : null}
                    {!capability.available ? <em>不可用</em> : null}
                  </label>
                );
              })}
            </div>
            {!steps.preprocess
              && (steps.detect || steps.ocr)
              && project?.settings.preprocessing.profile !== 'off' ? (
                <div className="notice notice--warning"><b>本批次不会执行图片增强</b><span>项目已配置 {project?.settings.preprocessing.profile}，勾选“图片增强”后检测与 OCR 才会使用新产物。</span></div>
              ) : null}
            {pipelineExportBlocked ? (
              <div className="notice notice--warning" role="status"><b>先处理→复核→再导出</b><span>处理阶段会使旧产物或复核结论失效，不能与导出一次性排队。请先完成处理，逐页复核后再单独导出。</span></div>
            ) : null}
            {ocrDownstreamMixed ? (
              <div className="notice notice--warning" role="status"><b>OCR 后必须先人工确认</b><span>OCR 不能与翻译、擦字修复或嵌字排版放进同一批次。请先单独完成 OCR，再确认每个候选框。</span></div>
            ) : null}
            {trustGateBlocked ? (
              <div className="notice notice--warning" role="status"><b>还有 {pendingTrustCount} 个 OCR 文本框待信任确认</b><span>置信度不是放行依据。请逐个确认或忽略后，再开始翻译和安全图像处理。</span></div>
            ) : null}
          </section>
          <section className="drawer-section">
            <h3>3. 资源限制</h3>
            <Field label="并发页数" hint="检测、OCR、翻译与渲染每批最多并行 1–8 页；为保证重名处理安全，导出固定串行。">
              <select
                aria-label="任务并发数"
                onChange={(event) => setConcurrency(Number(event.target.value))}
                value={concurrency}
              >
                <option value={1}>1（节省资源）</option>
                <option value={2}>2（推荐）</option>
                <option value={4}>4</option>
                <option value={8}>8</option>
              </select>
            </Field>
          </section>
          {steps.export ? (
            <section className="drawer-section">
              <h3>4. 导出选项</h3>
              <div className="field-grid">
                <Field label="内容">
                  <select aria-label="导出内容" onChange={(event) => setExportOptions((current) => ({ ...current, format: event.target.value as ExportOptions['format'] }))} value={exportOptions.format}>
                    <option value="both">成品图 + JSON</option><option value="images">仅成品图</option><option value="json">仅文本 JSON</option>
                  </select>
                </Field>
                <Field label="图像版本">
                  <select
                    aria-label="导出图像版本"
                    disabled={exportOptions.format === 'json'}
                    onChange={(event) => setExportOptions((current) => ({
                      ...current,
                      imageVariant: event.target.value as ExportOptions['imageVariant'],
                    }))}
                    value={exportOptions.imageVariant}
                  >
                    <option value="typeset">排版图（translated/）</option>
                    <option value="inpainted">无字底图（clean/）</option>
                    <option value="both">排版图 + 无字底图</option>
                  </select>
                </Field>
                <Field label="重名处理">
                  <select aria-label="导出重名处理" onChange={(event) => setExportOptions((current) => ({ ...current, conflict: event.target.value as ExportOptions['conflict'] }))} value={exportOptions.conflict}>
                    <option value="rename">自动重命名</option><option value="skip">跳过</option><option value="overwrite">覆盖生成文件</option>
                  </select>
                </Field>
              </div>
              <Field label="输出目录（可选）" hint={outputPathHint}><input onChange={(event) => setExportOptions((current) => ({ ...current, outputPath: event.target.value }))} placeholder="使用项目根目录" value={exportOptions.outputPath ?? ''} /></Field>
              <label className="check-row"><input checked={exportOptions.preserveTree} onChange={(event) => setExportOptions((current) => ({ ...current, preserveTree: event.target.checked }))} type="checkbox" />保留原始相对目录树</label>
              {imageExportBlocked ? (
                <div className="notice notice--warning" role="status"><b>还有 {unreviewedImageCount} 页未显式检查</b><span>包含图像的导出要求先逐页“标记已检查”或“确认无文字”；改为仅文本 JSON 可跳过此门禁。</span></div>
              ) : null}
              {generatedImageExportBlocked ? (
                <div className="notice notice--warning" role="status">
                  <b>所选图像版本尚未全部生成</b>
                  <span>
                    {requiresTypeset && missingTypesetCount > 0 ? `${missingTypesetCount} 页缺少排版图。` : ''}
                    {requiresInpaint && missingInpaintCount > 0 ? `${missingInpaintCount} 页缺少无字底图。` : ''}
                    请先单独处理，完成后逐页复核，再导出。
                  </span>
                </div>
              ) : null}
              {stageReviewExportBlocked ? (
                <div className="notice notice--warning" role="status">
                  <b>所选图像版本尚未全部通过视觉复核</b>
                  <span>
                    {requiresTypeset && unacceptedTypesetCount > 0 ? `${unacceptedTypesetCount} 页排版图未接受。` : ''}
                    {requiresInpaint && unacceptedInpaintCount > 0 ? `${unacceptedInpaintCount} 页无字底图未接受。` : ''}
                    请在画布切换到对应生成版本，逐页接受后再导出。
                  </span>
                </div>
              ) : null}
              {steps.export && exportOptions.format !== 'json' && requiresTypeset && overflowImageCount > 0 ? (
                <div className="notice notice--warning" role="status">
                  <b>还有 {overflowImageCount} 页排版溢出</b>
                  <span>这不是导出硬门禁。用侧栏“排版溢出”筛选，或在检查器里只重排溢出框。</span>
                </div>
              ) : null}
              {exportOptions.format === 'json' ? (
                <div className="notice notice--local"><b>仅文本 JSON 不受页面复核门禁</b><span>此导出不会写入排版图或无字底图。</span></div>
              ) : null}
              {exportOptions.conflict === 'overwrite' ? <div className="notice notice--warning"><b>覆盖仅限生成目录</b><span>本地服务仍不会修改导入的源图。</span></div> : null}
            </section>
          ) : null}
          <section className="drawer-section queue-section">
            <div className="section-title-row"><h3>任务队列</h3><span>{jobs.filter((job) => ['queued', 'running', 'paused'].includes(job.status)).length} 个活动任务</span></div>
            {!jobs.length ? <p className="panel-hint">尚无任务。队列持久化在项目数据库中，重启后可恢复。</p> : jobs.map((job) => <JobCard job={job} key={job.id} />)}
          </section>
        </div>
        <footer className="batch-drawer__footer">
          <button className="button button--accent button--block" disabled={starting || !imageIds.length || !selectedKinds.length || imageExportBlocked || generatedImageExportBlocked || stageReviewExportBlocked || pipelineExportBlocked || ocrDownstreamMixed || trustGateBlocked} onClick={() => void run()} type="button">
            {starting ? '正在创建队列…' : `加入队列 · ${imageIds.length} 张 · ${selectedKinds.length} 步`}
          </button>
        </footer>
      </aside>
    </div>
  );
}
