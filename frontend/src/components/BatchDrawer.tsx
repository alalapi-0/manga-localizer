import { useMemo, useState } from 'react';

import { activeImage, useWorkbenchStore } from '../store/workbench';
import type { ExportOptions, Job, JobKind, ProviderCapability } from '../types';
import { Field, IconButton } from './Primitives';

const kindLabels: Record<JobKind, string> = {
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
    detectorProvider: string;
    ocrProvider: string;
    translatorProvider: string;
    inpainterProvider: string;
  },
): { available: boolean; message: string; mock: boolean } {
  if (kind === 'export') return { available: true, message: '本地安全导出', mock: false };
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
  const value = Math.max(0, Math.min(100, percent(job)));
  return (
    <article className={`job-card job-card--${job.status}`}>
      <header>
        <div><strong>{kindLabels[job.kind]}</strong><span>{statusLabels[job.status]}</span></div>
        <b>{value}%</b>
      </header>
      <progress aria-label={`${kindLabels[job.kind]}进度`} max={100} value={value} />
      <div className="job-card__meta">
        <span>{job.completed} / {job.total || '—'} 项</span>
        {job.error ? <span className="job-error">{job.error}</span> : null}
      </div>
      {job.items.length ? (
        <details>
          <summary>查看 {job.items.length} 个队列项</summary>
          <div className="job-items">
            {job.items.slice(0, 20).map((item) => (
              <div key={item.id}><span>{item.label}</span><em>{statusLabels[item.status]}</em>{item.error ? <small>{item.error}</small> : null}</div>
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
  const selectedImageIds = useWorkbenchStore((state) => state.selectedImageIds);
  const providers = useWorkbenchStore((state) => state.capabilities.providers);
  const jobs = useWorkbenchStore((state) => state.jobs);
  const currentImage = useWorkbenchStore(activeImage);
  const startBatch = useWorkbenchStore((state) => state.startBatch);
  const [target, setTarget] = useState<'selected' | 'current' | 'all'>('selected');
  const [steps, setSteps] = useState<Record<JobKind, boolean>>({
    detect: true,
    ocr: true,
    translate: false,
    inpaint: false,
    typeset: false,
    export: false,
  });
  const [exportOptions, setExportOptions] = useState<ExportOptions>({
    format: 'both',
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

  if (!open) return null;
  const selectedKinds = (Object.keys(steps) as JobKind[]).filter((kind) => steps[kind]);

  async function run() {
    setStarting(true);
    const success = await startBatch(selectedKinds, imageIds, exportOptions, concurrency);
    setStarting(false);
    if (success) setSteps((current) => ({ ...current, detect: false, ocr: false, translate: false, inpaint: false, typeset: false, export: false }));
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
                <Field label="重名处理">
                  <select aria-label="导出重名处理" onChange={(event) => setExportOptions((current) => ({ ...current, conflict: event.target.value as ExportOptions['conflict'] }))} value={exportOptions.conflict}>
                    <option value="rename">自动重命名</option><option value="skip">跳过</option><option value="overwrite">覆盖生成文件</option>
                  </select>
                </Field>
              </div>
              <Field label="输出目录（可选）" hint="自定义目录会生成可重开的项目快照，包含源图副本；只分享成品时请仅取 translated/。"><input onChange={(event) => setExportOptions((current) => ({ ...current, outputPath: event.target.value }))} placeholder="使用项目默认 output/" value={exportOptions.outputPath ?? ''} /></Field>
              <label className="check-row"><input checked={exportOptions.preserveTree} onChange={(event) => setExportOptions((current) => ({ ...current, preserveTree: event.target.checked }))} type="checkbox" />保留原始相对目录树</label>
              {exportOptions.conflict === 'overwrite' ? <div className="notice notice--warning"><b>覆盖仅限生成目录</b><span>本地服务仍不会修改导入的源图。</span></div> : null}
            </section>
          ) : null}
          <button className="button button--accent button--block" disabled={starting || !imageIds.length || !selectedKinds.length} onClick={() => void run()} type="button">
            {starting ? '正在创建队列…' : `加入队列 · ${imageIds.length} 张 · ${selectedKinds.length} 步`}
          </button>
          <section className="drawer-section queue-section">
            <div className="section-title-row"><h3>任务队列</h3><span>{jobs.filter((job) => ['queued', 'running', 'paused'].includes(job.status)).length} 个活动任务</span></div>
            {!jobs.length ? <p className="panel-hint">尚无任务。队列持久化在项目数据库中，重启后可恢复。</p> : jobs.map((job) => <JobCard job={job} key={job.id} />)}
          </section>
        </div>
      </aside>
    </div>
  );
}
