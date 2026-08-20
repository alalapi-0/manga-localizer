import { useMemo, useState } from 'react';

import { api } from '../api/client';
import {
  activeImage,
  imageHasTypesetOverflow,
  latestPageProcessingActivity,
  latestPageProcessingError,
  overflowingRegionIds,
  AI_REDRAW_PREPROCESSING,
  preprocessingSettingsForProfile,
  preferredAiRedrawProvider,
  regionHasTypesetOverflow,
  useWorkbenchStore,
} from '../store/workbench';
import { clampRegionGeometry } from './canvasGeometry';
import type {
  ExportOptions,
  ImageAsset,
  JobKind,
  PreprocessingSettings,
  ProjectSettings,
  ProviderCapability,
  Region,
  RegionType,
  TextDirection,
  RegionDisposition,
} from '../types';
import { CreateLocalProjectButton, EmptyState, Field, ImportPhotosButton, ProviderBadge, Toggle } from './Primitives';

const regionTypeLabels: Record<RegionType, string> = {
  dialogue: '对白',
  narration: '旁白',
  sound_effect: '拟声词',
  title: '标题',
  ruby: '注音',
  background: '背景文字',
  unknown: '未知',
  thought: '内心',
  sign: '标牌',
  speech: '气泡对白',
  other: '其他',
};

const directionLabels: Record<TextDirection, string> = {
  auto: '自动',
  vertical: '竖排',
  horizontal: '横排',
};

function GeometryNumberField({
  ariaLabel,
  label,
  min,
  onCommit,
  step,
  value,
}: {
  ariaLabel: string;
  label: string;
  min?: number;
  onCommit: (next: number) => void;
  step?: number | string;
  value: number;
}) {
  const [draft, setDraft] = useState<string | null>(null);

  return (
    <Field label={label}>
      <input
        aria-label={ariaLabel}
        min={min}
        onBlur={() => {
          if (draft !== null) {
            const next = Number(draft);
            if (Number.isFinite(next)) onCommit(next);
          }
          setDraft(null);
        }}
        onChange={(event) => {
          const raw = event.target.value;
          setDraft(raw);
          const next = Number(raw);
          if (Number.isFinite(next)) onCommit(next);
        }}
        step={step}
        type="number"
        value={draft ?? value}
      />
    </Field>
  );
}

const preprocessingProfileLabels: Record<PreprocessingSettings['profile'], string> = {
  off: '关闭',
  'ocr-friendly': 'OCR 友好',
  balanced: '平衡',
  'visual-quality': '视觉质量',
};

const preprocessSuggestionReasonLabels: Record<string, string> = {
  'small-page': '短边偏小，超分可能有助于 OCR',
  'low-contrast': '对比度偏低',
  'soft-detail': '细节偏软',
  'high-res-sharp': '已是清晰大图，默认不必增强',
  'large-page': '短边已够大（仅按尺寸估计）',
};

const defaultExportOptions: ExportOptions = {
  format: 'both',
  imageVariant: 'typeset',
  conflict: 'rename',
  preserveTree: true,
};

const retryStageLabels: Partial<Record<JobKind, string>> = {
  preprocess: '重试本页增强',
  detect: '重试本页检测',
  ocr: '重试本页 OCR',
  translate: '重试本页翻译',
  inpaint: '重试本页修复',
  typeset: '重试本页排版',
  export: '重试本页导出',
};

const processingStageTitles: Record<string, string> = {
  preprocess: '图片增强失败',
  detect: '文字检测失败',
  ocr: '日文 OCR 失败',
  translate: '翻译失败',
  inpaint: '擦字修复失败',
  typeset: '嵌字排版失败',
  export: '导出失败',
  render: '图像渲染失败',
  processing: '本页处理失败',
};

const processingStageNouns: Record<string, string> = {
  preprocess: '图片增强',
  detect: '文字检测',
  ocr: '日文 OCR',
  translate: '翻译',
  inpaint: '擦字修复',
  typeset: '嵌字排版',
  export: '导出',
  processing: '本页处理',
};

const EMPTY_REGIONS: Region[] = [];

const dispositionLabels: Record<RegionDisposition, string> = {
  review: 'OCR 待信任',
  trusted: 'OCR 已信任',
  ignored: '人工已忽略',
};

const dispositionReasonLabels: Record<string, string> = {
  'automatic-proposal': '自动检测候选，等待人工确认',
  'automatic-ocr-complete': 'OCR 已完成，置信度不能代替人工确认',
  'manual-unconfirmed': '手动创建，等待人工确认',
  'human-confirmed': '已由人工明确确认',
  'human-ignored': '已由人工明确忽略',
  'trust-input-changed': '原文或识别范围已变化，需要重新确认',
  'legacy-confirmed': '兼容旧项目中的人工确认记录',
  'legacy-unverified': '旧记录没有明确的信任决定',
  'policy-version-changed': '信任策略已更新，需要重新确认',
};

function dispositionReason(region: Region): string {
  return dispositionReasonLabels[region.trustReason]
    ?? `原因：${region.trustReason || '未提供'}`;
}

function percent(value: number | null): string {
  if (value === null) return '未评分';
  const normalized = value <= 1 ? value * 100 : value;
  return `${Math.round(normalized * 10) / 10}%`;
}

function PageReviewControl({ regions }: { regions: Region[] }) {
  const image = useWorkbenchStore(activeImage);
  const reviewActiveImage = useWorkbenchStore((state) => state.reviewActiveImage);
  const setRightTab = useWorkbenchStore((state) => state.setRightTab);
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const regionsLoading = useWorkbenchStore((state) =>
    state.activeImageId ? Boolean(state.regionsLoading[state.activeImageId]) : false,
  );
  const [submitting, setSubmitting] = useState(false);
  if (!image) return null;
  const state = image.status.reviewState;
  const activeRegions = regions.filter((region) => !region.ignored);
  const loadedUnreadyCount = activeRegions.filter(
    (region) => !region.confirmed || region.trustDisposition !== 'trusted',
  ).length;
  const serverPendingCount = Math.max(0, Number(image.trustReviewCount ?? 0));
  const unreadyCount = Math.max(serverPendingCount, loadedUnreadyCount);
  const hasKnownActiveRegions = activeRegions.length > 0 || serverPendingCount > 0;
  const actionState = hasKnownActiveRegions ? 'reviewed' : 'no-text-reviewed';
  const reviewed = state !== 'pending';
  const label = state === 'reviewed'
    ? '本页已标记检查完毕'
    : state === 'no-text-reviewed'
      ? '本页已确认无文字'
      : unreadyCount > 0
        ? `还有 ${unreadyCount} 个活动文本框尚未确认并信任`
        : activeRegions.length === 0
          ? '本页没有活动文本框，可确认无文字'
          : `${activeRegions.length} 个活动文本框均已信任，可完成页面检查`;

  const firstUnready = regions.find(
    (region) => !region.ignored && (!region.confirmed || region.trustDisposition !== 'trusted'),
  );

  async function submit() {
    if (!reviewed && unreadyCount > 0) {
      setRightTab('text');
      if (firstUnready) selectRegion(firstUnready.id);
      return;
    }
    setSubmitting(true);
    await reviewActiveImage(reviewed ? 'pending' : actionState);
    setSubmitting(false);
  }

  return (
    <section className={`page-review page-review--${reviewed ? 'done' : 'pending'}`} aria-label="页面复核状态">
      <div>
        <span>{reviewed ? '已检查' : '待检查'}</span>
        <strong>{label}</strong>
      </div>
      <button
        className={`button button--compact ${reviewed ? '' : 'button--accent'}`}
        disabled={submitting || regionsLoading || (!reviewed && unreadyCount > 0 && !firstUnready)}
        onClick={() => void submit()}
        type="button"
      >
        {regionsLoading
          ? '正在读取文本框…'
          : submitting
          ? '正在保存…'
          : reviewed
            ? '撤回检查标记'
            : unreadyCount > 0
              ? `还需确认并信任 ${unreadyCount} 个文本框`
              : activeRegions.length === 0
                ? '确认本页无文字'
                : '标记本页已检查'}
      </button>
    </section>
  );
}

function ProcessingActivityNotice() {
  const image = useWorkbenchStore(activeImage);
  const openQueueForImage = useWorkbenchStore((state) => state.openQueueForImage);
  const failure = latestPageProcessingError(image);
  const activity = latestPageProcessingActivity(image);
  if (failure || !image || !activity) return null;
  const noun = processingStageNouns[activity.stage] ?? processingStageNouns.processing;
  const title = activity.status === 'running' ? `${noun} 处理中` : `${noun} 排队中`;
  return (
    <div className="notice notice--warning" role="status">
      <b>{title}</b>
      <span>本页已重新排队，不必打开批处理抽屉；完成后检查器会更新。</span>
      <div className="notice__actions">
        <button
          className="button button--compact"
          onClick={() => openQueueForImage(image.id, activity.kind)}
          type="button"
        >
          查看队列
        </button>
      </div>
    </div>
  );
}

function ProcessingErrorNotice() {
  const image = useWorkbenchStore(activeImage);
  const startBatch = useWorkbenchStore((state) => state.startBatch);
  const openQueueForImage = useWorkbenchStore((state) => state.openQueueForImage);
  const failure = latestPageProcessingError(image);
  if (!image || !failure) return null;
  const retryKind = failure.kind;
  const retryLabel = retryKind ? retryStageLabels[retryKind] : undefined;

  return (
    <div className="notice notice--error" role="alert">
      <b>{processingStageTitles[failure.stage] ?? processingStageTitles.processing}</b>
      <span>详情只保存在本机项目日志中。可重试这一页，或打开批处理抽屉查看队列。</span>
      <div className="notice__actions">
        {retryKind && retryLabel ? (
          <button
            className="button button--compact"
            onClick={() => {
              void startBatch([retryKind], [image.id], defaultExportOptions, 1);
            }}
            type="button"
          >
            {retryLabel}
          </button>
        ) : null}
        <button
          className="button button--compact"
          onClick={() => openQueueForImage(image.id, retryKind)}
          type="button"
        >
          查看队列
        </button>
      </div>
    </div>
  );
}

function TextInspector({ regions, selected }: { regions: Region[]; selected: Region[] }) {
  const image = useWorkbenchStore(activeImage);
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const focusRegions = useWorkbenchStore((state) => state.focusRegions);
  const updateRegion = useWorkbenchStore((state) => state.updateRegion);
  const nudgeSelectedRegions = useWorkbenchStore((state) => state.nudgeSelectedRegions);
  const setRegionConfirmed = useWorkbenchStore((state) => state.setRegionConfirmed);
  const deleteSelectedRegions = useWorkbenchStore((state) => state.deleteSelectedRegions);
  const mergeSelectedRegions = useWorkbenchStore((state) => state.mergeSelectedRegions);
  const splitSelectedRegion = useWorkbenchStore((state) => state.splitSelectedRegion);
  const setDrawerOpen = useWorkbenchStore((state) => state.setDrawerOpen);
  const startBatch = useWorkbenchStore((state) => state.startBatch);

  function rerunSelectedOcr() {
    if (!image || !selected.length) return;
    setDrawerOpen(true);
    void startBatch(
      ['ocr'],
      [image.id],
      defaultExportOptions,
      1,
      selected.map((region) => region.id),
    );
  }

  if (!selected.length) {
    if (!regions.length) {
      const ocrDone = image?.status.ocr === 'done';
      return (
        <EmptyState
          icon={ocrDone ? '✓' : '文'}
          title={ocrDone ? '本页未检测到文本' : '未选择文本框'}
          description={
            ocrDone
              ? '未检测到文字不等于完成检查；请在上方确认无文字，或在画布上手动框选。'
              : regions.length ? '在画布或下方列表中选择文本框。' : '运行 OCR 或在画布上绘制文本框。'
          }
        />
      );
    }
    return (
      <div className="region-index">
        <p className="panel-hint">选择一个文本框编辑内容；按住 Shift 可多选。</p>
        {regions.map((region) => (
          <button
            aria-label={`选择文本框 #${region.order}`}
            key={region.id}
            onClick={() => {
              selectRegion(region.id);
              focusRegions([region.id]);
            }}
            type="button"
          >
            <b>#{region.order}</b>
            <span>{region.sourceText || '（空文本）'}</span>
            <em>{regionHasTypesetOverflow(image, region.id) ? '排版溢出' : dispositionLabels[region.trustDisposition]}</em>
          </button>
        ))}
      </div>
    );
  }

  if (selected.length > 1) {
    const allTrusted = selected.every(
      (region) => region.confirmed && !region.ignored && region.trustDisposition === 'trusted',
    );
    const allIgnored = selected.every((region) => region.ignored);
    return (
      <div className="form-stack">
        <div className="multi-selection-summary">
          <strong>已选择 {selected.length} 个文本框</strong>
          <span>批量修改只影响当前选择。</span>
        </div>
        <Field label="文本类型">
          <select
            defaultValue=""
            onChange={(event) => selected.forEach((region) => updateRegion(region.id, { type: event.target.value as RegionType }))}
          >
            <option disabled value="">批量设置…</option>
            {Object.entries(regionTypeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Field>
        <Field label="排版方向">
          <select
            defaultValue=""
            onChange={(event) => selected.forEach((region) => updateRegion(region.id, { direction: event.target.value as TextDirection }))}
          >
            <option disabled value="">批量设置…</option>
            {Object.entries(directionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Field>
        <Toggle
          checked={allTrusted}
          label="全部确认"
          onChange={(event) => selected.forEach((region) => {
            void setRegionConfirmed(region.id, event.target.checked);
          })}
        />
        <Toggle
          checked={allIgnored}
          label="全部忽略"
          onChange={(event) => selected.forEach((region) => updateRegion(region.id, { ignored: event.target.checked }))}
        />
        <button className="button" onClick={rerunSelectedOcr} type="button">重新 OCR 选中区域</button>
        <button className="button" onClick={mergeSelectedRegions} type="button">合并为一个文本框</button>
        <button className="button button--danger" onClick={deleteSelectedRegions} type="button">删除所选文本框</button>
      </div>
    );
  }

  const region = selected[0];
  if (!region) return null;
  const confidencePercent = region.confidence === null
    ? ''
    : Math.round((region.confidence <= 1 ? region.confidence * 100 : region.confidence) * 10) / 10;

  return (
    <div className="form-stack text-inspector">
      <div className="region-heading">
        <div><span>文本框</span><strong>#{region.order}</strong></div>
        <span className={`review-state review-state--${region.ignored || region.trustDisposition === 'ignored' ? 'ignored' : region.confirmed && region.trustDisposition === 'trusted' ? 'confirmed' : 'pending'}`}>
          {dispositionLabels[region.trustDisposition]}
        </span>
      </div>
      <section className={`trust-summary trust-summary--${region.trustDisposition}`} aria-label="OCR 信任状态">
        <strong>{dispositionLabels[region.trustDisposition]}</strong>
        <span>{dispositionReason(region)}</span>
        <small>检测 {percent(region.detectorConfidence)} · OCR {percent(region.ocrConfidence)} · 策略 v{region.trustPolicyVersion}</small>
      </section>
      <Toggle checked={region.confirmed && !region.ignored && region.trustDisposition === 'trusted'} description={region.trustDisposition === 'trusted' ? 'OCR 已由人工信任；修改或翻译后还需再确认当前内容，才可完成页面复核' : '明确确认后才允许翻译和安全图像处理，并获得页面复核资格'} label="确认此文本框" onChange={(event) => {
        void setRegionConfirmed(region.id, event.target.checked);
      }} />
      <Toggle checked={region.ignored} description="图像处理会跳过；导出 JSON 仍保留此记录" label="忽略此文本框" onChange={(event) => updateRegion(region.id, { ignored: event.target.checked })} />
      <Field label="日文原文">
        <textarea
          aria-label="日文原文"
          onChange={(event) => updateRegion(region.id, { sourceText: event.target.value })}
          rows={5}
          spellCheck={false}
          value={region.sourceText}
        />
      </Field>
      <div className="text-meta"><span>{region.sourceText.length} 字符</span><span>兼容评分 {confidencePercent === '' ? '未评分' : `${confidencePercent}%`}</span></div>
      <Field label="中文译文">
        <textarea
          aria-label="中文译文"
          lang="zh-CN"
          onChange={(event) => updateRegion(region.id, { translationText: event.target.value })}
          rows={6}
          value={region.translationText}
        />
      </Field>
      <div className="text-meta"><span>{region.translationText.length} 字符</span><span>{region.style.autoFit ? '自动适配字号' : '固定字号'}</span></div>
      {image ? (
        <section className="form-stack" aria-label="选框几何">
          <div className="field-grid">
            <GeometryNumberField
              key={`${region.id}-x`}
              ariaLabel="选框 X"
              label="X"
              min={0}
              onCommit={(x) => updateRegion(region.id, clampRegionGeometry({ ...region, x }, image))}
              value={region.x}
            />
            <GeometryNumberField
              key={`${region.id}-y`}
              ariaLabel="选框 Y"
              label="Y"
              min={0}
              onCommit={(y) => updateRegion(region.id, clampRegionGeometry({ ...region, y }, image))}
              value={region.y}
            />
            <GeometryNumberField
              key={`${region.id}-width`}
              ariaLabel="选框宽度"
              label="宽"
              min={4}
              onCommit={(width) => updateRegion(region.id, clampRegionGeometry({ ...region, width }, image))}
              value={region.width}
            />
            <GeometryNumberField
              key={`${region.id}-height`}
              ariaLabel="选框高度"
              label="高"
              min={4}
              onCommit={(height) => updateRegion(region.id, clampRegionGeometry({ ...region, height }, image))}
              value={region.height}
            />
            <GeometryNumberField
              key={`${region.id}-rotation`}
              ariaLabel="选框旋转"
              label="旋转 °"
              onCommit={(rotation) => updateRegion(region.id, clampRegionGeometry({ ...region, rotation }, image))}
              step="0.1"
              value={region.rotation}
            />
          </div>
          <div className="nudge-actions" aria-label="微调选框">
            <button className="button button--compact" onClick={() => nudgeSelectedRegions(0, -1)} type="button">上移 1px</button>
            <button className="button button--compact" onClick={() => nudgeSelectedRegions(0, 1)} type="button">下移 1px</button>
            <button className="button button--compact" onClick={() => nudgeSelectedRegions(-1, 0)} type="button">左移 1px</button>
            <button className="button button--compact" onClick={() => nudgeSelectedRegions(1, 0)} type="button">右移 1px</button>
          </div>
        </section>
      ) : null}
      <div className="field-grid">
        <Field label="类型">
          <select aria-label="文本类型" onChange={(event) => updateRegion(region.id, { type: event.target.value as RegionType })} value={region.type}>
            {Object.entries(regionTypeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Field>
        <Field label="方向">
          <select aria-label="文本方向" onChange={(event) => updateRegion(region.id, { direction: event.target.value as TextDirection })} value={region.direction}>
            {Object.entries(directionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Field>
        <Field label="阅读顺序">
          <input
            aria-label="阅读顺序"
            min={0}
            onChange={(event) => updateRegion(region.id, { order: event.target.value === '' ? 0 : Number(event.target.value) })}
            type="number"
            value={region.order || ''}
          />
        </Field>
        <Field label="兼容评分 %" hint="仅供旧项目显示；不会自动建立 OCR 信任">
          <input
            aria-label="置信度"
            max={100}
            min={0}
            onChange={(event) => updateRegion(region.id, { confidence: event.target.value === '' ? null : Number(event.target.value) / 100 })}
            step="0.1"
            type="number"
            value={confidencePercent}
          />
        </Field>
      </div>
      <button className="button" onClick={rerunSelectedOcr} type="button">重新 OCR 选中区域</button>
      <div className="split-actions" aria-label="拆分文本框">
        <button className="button" onClick={() => splitSelectedRegion('horizontal')} type="button">水平中线拆分</button>
        <button className="button" onClick={() => splitSelectedRegion('vertical')} type="button">垂直中线拆分</button>
      </div>
      <button className="button button--danger" onClick={deleteSelectedRegions} type="button">删除文本框</button>
    </div>
  );
}

function TypesettingInspector({ region }: { region: Region | undefined }) {
  const image = useWorkbenchStore(activeImage);
  const updateRegion = useWorkbenchStore((state) => state.updateRegion);
  const setDrawerOpen = useWorkbenchStore((state) => state.setDrawerOpen);
  const startBatch = useWorkbenchStore((state) => state.startBatch);
  if (!region) return <EmptyState icon="字" title="选择一个文本框" description="排版参数会按文本框单独保存。" />;
  const style = region.style;
  const overflowing = regionHasTypesetOverflow(image, region.id);
  const updateStyle = (patch: Partial<Region['style']>) => updateRegion(region.id, { style: { ...style, ...patch } });
  return (
    <div className="form-stack">
      {overflowing ? (
        <div className="notice notice--warning" role="status">
          <b>当前文本框排版溢出</b>
          <span>缩小字号、加大文本框，或打开自动适配后再点「重排当前框」。</span>
        </div>
      ) : null}
      <Toggle checked={style.autoFit} description="缩小字号直到译文放入区域" label="自动适配字号" onChange={(event) => updateStyle({ autoFit: event.target.checked })} />
      <Field label="字体族" hint="使用本机已安装字体；字体文件不会被上传。">
        <input onChange={(event) => updateStyle({ fontFamily: event.target.value })} value={style.fontFamily} />
      </Field>
      <div className="field-grid">
        <Field label="字号 px"><input disabled={style.autoFit} min={6} onChange={(event) => updateStyle({ fontSize: Number(event.target.value) })} type="number" value={style.fontSize} /></Field>
        <Field label="行高"><input min={0.7} onChange={(event) => updateStyle({ lineHeight: Number(event.target.value) })} step="0.05" type="number" value={style.lineHeight} /></Field>
        <Field label="字间距 px"><input onChange={(event) => updateStyle({ letterSpacing: Number(event.target.value) })} step="0.5" type="number" value={style.letterSpacing} /></Field>
        <Field label="内边距 px"><input min={0} onChange={(event) => updateStyle({ padding: Number(event.target.value) })} type="number" value={style.padding} /></Field>
      </div>
      <div className="field-grid">
        <Field label="文字颜色"><input aria-label="文字颜色" onChange={(event) => updateStyle({ color: event.target.value })} type="color" value={style.color} /></Field>
        <Field label="描边颜色"><input aria-label="描边颜色" onChange={(event) => updateStyle({ strokeColor: event.target.value })} type="color" value={style.strokeColor} /></Field>
        <Field label="描边 px"><input min={0} onChange={(event) => updateStyle({ strokeWidth: Number(event.target.value) })} step="0.5" type="number" value={style.strokeWidth} /></Field>
        <Field label="对齐">
          <select onChange={(event) => updateStyle({ align: event.target.value as Region['style']['align'] })} value={style.align}>
            <option value="start">起始</option><option value="center">居中</option><option value="end">末端</option>
          </select>
        </Field>
      </div>
      <div className="typeset-preview" style={{ color: style.color, fontFamily: style.fontFamily, fontSize: `${Math.min(style.fontSize, 32)}px`, lineHeight: style.lineHeight, WebkitTextStroke: `${style.strokeWidth}px ${style.strokeColor}` }}>
        {region.translationText || '中文排版预览'}
      </div>
      {image ? (
        <button
          className="button"
          onClick={() => {
            setDrawerOpen(true);
            void startBatch(['typeset'], [image.id], defaultExportOptions, 1, [region.id]);
          }}
          title="T"
          type="button"
        >
          重排当前框
        </button>
      ) : null}
    </div>
  );
}

const inpaintAnomalyLabels: Record<string, string> = {
  'mask-outside-changed': '蒙版外像素变化',
  'chroma-introduced': '引入彩色',
  'possible-smear': '可能涂抹过重',
};

function InpaintCandidatePicker({ image }: { image: ImageAsset }) {
  const selectInpaintCandidate = useWorkbenchStore((state) => state.selectInpaintCandidate);
  const busy = useWorkbenchStore((state) => state.stageReviewSaving);
  const candidates = image.inpaintCandidates ?? [];
  if (image.status.inpaint !== 'done' || candidates.length < 2) return null;
  return (
    <Field
      label="修复候选"
      hint="比较后选择一个作为当前擦除结果。接受/拒绝仍绑定该结果的校验和。自动指标只标异常，不能代替目视。"
    >
      <div className="inpaint-candidates" role="radiogroup" aria-label="修复候选">
        {candidates.map((candidate) => (
          <label className="inpaint-candidate" key={candidate.id}>
            <input
              checked={image.inpaintCandidate === candidate.id}
              disabled={Boolean(busy)}
              name="inpaint-candidate"
              onChange={() => {
                void selectInpaintCandidate(candidate.id);
              }}
              type="radio"
              value={candidate.id}
            />
            <span>
              <span>{candidate.label}</span>
              {candidate.anomalies.length ? (
                <span className="inpaint-candidate__anomalies">
                  {candidate.anomalies.map((flag) => inpaintAnomalyLabels[flag] ?? flag).join(' · ')}
                </span>
              ) : null}
            </span>
          </label>
        ))}
      </div>
    </Field>
  );
}

function RepairInspector({ region }: { region: Region | undefined }) {
  const image = useWorkbenchStore(activeImage);
  const project = useWorkbenchStore((state) => state.currentProject);
  const showMask = useWorkbenchStore((state) => state.showMask);
  const setShowMask = useWorkbenchStore((state) => state.setShowMask);
  const providers = useWorkbenchStore((state) => state.capabilities.providers);
  const setDrawerOpen = useWorkbenchStore((state) => state.setDrawerOpen);
  const updateRegion = useWorkbenchStore((state) => state.updateRegion);
  const startBatch = useWorkbenchStore((state) => state.startBatch);
  const candidatePicker = image ? <InpaintCandidatePicker image={image} /> : null;
  if (!region) {
    return (
      <div className="form-stack">
        {candidatePicker}
        <EmptyState icon="◌" title="选择一个文本框" description="蒙版与修复参数会按区域保存。" />
      </div>
    );
  }
  const repair = region.repair;
  const updateRepair = (patch: Partial<Region['repair']>) => updateRegion(region.id, { repair: { ...repair, ...patch } });
  const inheritedProvider = project?.settings.inpainterProvider ?? 'opencv';
  const provider = repair.inpainterProvider || inheritedProvider;
  const isLama = provider === 'lama' || provider === 'lama-onnx';
  const inpainters = providers.filter((capability) => capability.kind === 'inpainter');
  const providerCapability = providers.find(
    (capability) => capability.kind === 'inpainter' && capability.id === provider,
  );
  const providerUnavailable = providerCapability?.available === false;
  return (
    <div className="form-stack">
      {candidatePicker}
      <div className="notice notice--local"><b>本地处理 · {provider}</b><span>图像、蒙版和修复结果只在本机处理；可在画布用蒙版画笔与橡皮擦精修选中区域。</span></div>
      <div className="notice notice--local"><b>安全修复策略</b><span>只处理已确认、可信自动识别或手工识别区域；完成后任务抽屉会显示实际修复与跳过数量。</span></div>
      {providerUnavailable ? <div className="notice notice--warning"><b>当前修复 Provider 不可用</b><span>{providerCapability?.reason}</span></div> : null}
      <Field label="区域修复 Provider" hint="继承使用项目默认；区域覆盖只影响当前文本框。">
        <select
          aria-label="区域修复 Provider"
          onChange={(event) => updateRepair({ inpainterProvider: event.target.value || undefined })}
          value={repair.inpainterProvider ?? ''}
        >
          <option value="">继承项目设置（{inheritedProvider}）</option>
          {repair.inpainterProvider
            && !inpainters.some((capability) => capability.id === repair.inpainterProvider) ? (
              <option value={repair.inpainterProvider}>{repair.inpainterProvider}（能力未报告）</option>
            ) : null}
          {inpainters.map((capability) => (
            <option disabled={!capability.available} key={capability.id} value={capability.id}>
              {capability.label}{capability.available ? '' : '（不可用）'}
            </option>
          ))}
        </select>
      </Field>
      <Field label="蒙版策略" hint="文本轮廓优先使用检测多边形并细化字形；区域模式会覆盖整个文本框。">
        <select onChange={(event) => updateRepair({ maskMode: event.target.value as Region['repair']['maskMode'] })} value={repair.maskMode}>
          <option value="text">文本轮廓（推荐）</option>
          <option value="region">完整区域</option>
        </select>
      </Field>
      <Field label="文字极性" hint="仅文本轮廓模式生效；描边字可只移除字芯并保留反色衬底。">
        <select
          aria-label="文字极性"
          disabled={repair.maskMode !== 'text'}
          onChange={(event) => updateRepair({ textPolarity: event.target.value as Region['repair']['textPolarity'] })}
          value={repair.textPolarity}
        >
          <option value="auto">自动（推荐）</option>
          <option value="dark">深色文字（保留浅色衬底）</option>
          <option value="light">浅色文字（保留深色衬底）</option>
        </select>
      </Field>
      {isLama ? (
        <div className="notice notice--local"><b>LaMa AI 背景修复</b><span>使用当前蒙版的局部上下文推理，并保持灰度页不被染色。复杂线稿可在修复完成后比较 Navier–Stokes 与线稿引导候选。</span></div>
      ) : (
        <Field label="修复方法">
          <select onChange={(event) => updateRepair({ method: event.target.value as Region['repair']['method'] })} value={repair.method}>
            <option value="telea">OpenCV Telea</option>
            <option value="navier_stokes">OpenCV Navier–Stokes</option>
            <option value="solid">纯色填充</option>
          </select>
        </Field>
      )}
      <div className="field-grid">
        <Field label="蒙版外扩 px"><input max={512} min={0} onChange={(event) => updateRepair({ maskPadding: Math.min(512, Math.max(0, Math.round(Number(event.target.value)))) })} type="number" value={repair.maskPadding} /></Field>
        <Field label="膨胀 px"><input max={128} min={0} onChange={(event) => updateRepair({ dilation: Math.min(128, Math.max(0, Math.round(Number(event.target.value)))) })} type="number" value={repair.dilation} /></Field>
        <Field label="羽化 px"><input max={128} min={0} onChange={(event) => updateRepair({ feather: Math.min(128, Math.max(0, Math.round(Number(event.target.value)))) })} type="number" value={repair.feather} /></Field>
        {!isLama ? <Field label="修复半径"><input max={256} min={1} onChange={(event) => updateRepair({ radius: Math.min(256, Math.max(1, Number(event.target.value))) })} type="number" value={repair.radius} /></Field> : null}
        {!isLama ? <Field label="填充色"><input aria-label="修复填充色" onChange={(event) => updateRepair({ fillColor: event.target.value })} type="color" value={repair.fillColor} /></Field> : null}
      </div>
      <Toggle checked={showMask} description="在画布上叠加上一次实际生成的蒙版；重新修复后会刷新。" label="显示实际蒙版" onChange={(event) => setShowMask(event.target.checked)} />
      <button
        className="button button--accent"
        disabled={!image || providerUnavailable}
        onClick={() => {
          if (!image) return;
          setDrawerOpen(true);
          void startBatch(['inpaint'], [image.id], defaultExportOptions);
        }}
        type="button"
      >
        重建当前页
      </button>
    </div>
  );
}

function ProviderSelect({
  label,
  kind,
  value,
  providers,
  onChange,
}: {
  label: string;
  kind: ProviderCapability['kind'];
  value: string;
  providers: ProviderCapability[];
  onChange: (value: string) => void;
}) {
  const choices = providers.filter((provider) => provider.kind === kind);
  const selected = choices.find((provider) => provider.id === value);
  return (
    <div className="provider-setting">
      <Field label={label}>
        <select onChange={(event) => onChange(event.target.value)} value={value}>
          {!selected && value ? <option value={value}>{value}（能力未报告）</option> : null}
          {!choices.length && !value ? <option value="">无可用 provider</option> : null}
          {choices.map((provider) => (
            <option disabled={!provider.available && !provider.configurable} key={provider.id} value={provider.id}>
              {provider.label}{provider.isMock ? ' [演示 MOCK]' : ''}{provider.available ? '' : provider.configurable ? ' [未配置]' : ' [不可用]'}
            </option>
          ))}
        </select>
      </Field>
      <ProviderBadge provider={selected} />
    </div>
  );
}

function ProjectInspector() {
  const project = useWorkbenchStore((state) => state.currentProject);
  const image = useWorkbenchStore(activeImage);
  const providers = useWorkbenchStore((state) => state.capabilities.providers);
  const updateProjectSettings = useWorkbenchStore((state) => state.updateProjectSettings);
  const [sessionKey, setSessionKey] = useState('');
  const [sessionConfigured, setSessionConfigured] = useState(project?.settings.apiKeyConfigured ?? false);
  const [credentialState, setCredentialState] = useState('');
  if (!project) return <EmptyState icon="⚙" title="未打开项目" />;
  const settings = project.settings;
  const update = (patch: Partial<ProjectSettings>) => updateProjectSettings(patch);
  const updatePreprocessing = (patch: Partial<ProjectSettings['preprocessing']>) => update({
    preprocessing: { ...settings.preprocessing, ...patch },
  });
  const translator = providers.find((provider) => provider.kind === 'translator' && provider.id === settings.translatorProvider);
  const isRemote = translator ? !translator.local : Boolean(settings.remoteEndpoint);

  async function saveSessionKey() {
    setCredentialState('正在写入当前服务会话…');
    try {
      const result = await api.setSessionCredential(
        settings.translatorProvider,
        sessionKey,
        settings.remoteEndpoint,
        settings.remoteModel,
      );
      setSessionConfigured(result.configured);
      useWorkbenchStore.setState({ capabilities: result.capabilities });
      setSessionKey('');
      setCredentialState(result.configured ? '已配置，仅当前后端会话有效。' : '凭据未配置。');
    } catch (error) {
      setCredentialState(error instanceof Error ? error.message : '凭据配置失败');
    }
  }

  return (
    <div className="form-stack project-inspector">
      <section className="inspector-section">
        <h3>语言与上下文</h3>
        <div className="field-grid">
          <Field label="源语言"><select onChange={(event) => update({ sourceLanguage: event.target.value })} value={settings.sourceLanguage}><option value="ja">日语</option><option value="zh-CN">简体中文</option><option value="en">英语</option></select></Field>
          <Field label="目标语言"><select onChange={(event) => update({ targetLanguage: event.target.value })} value={settings.targetLanguage}><option value="zh-CN">简体中文</option><option value="zh-TW">繁体中文</option><option value="en">英语</option></select></Field>
          <Field label="相邻文本层级" hint="仅发送当前页前后相邻文本块；0 表示不带上下文。"><input max={5} min={0} onChange={(event) => update({ contextPages: Number(event.target.value) })} type="number" value={settings.contextPages} /></Field>
        </div>
      </section>
      <section className="inspector-section">
        <h3>处理 Provider</h3>
        <ProviderSelect kind="preprocessor" label="图片增强" onChange={(preprocessorProvider) => update({ preprocessorProvider })} providers={providers} value={settings.preprocessorProvider} />
        <ProviderSelect kind="detector" label="文本检测" onChange={(detectorProvider) => update({ detectorProvider })} providers={providers} value={settings.detectorProvider} />
        <ProviderSelect kind="ocr" label="日文 OCR" onChange={(ocrProvider) => update({ ocrProvider })} providers={providers} value={settings.ocrProvider} />
        <ProviderSelect kind="translator" label="翻译" onChange={(translatorProvider) => update({ translatorProvider })} providers={providers} value={settings.translatorProvider} />
        <ProviderSelect kind="inpainter" label="图像修复" onChange={(inpainterProvider) => update({ inpainterProvider })} providers={providers} value={settings.inpainterProvider} />
      </section>
      <section className="inspector-section">
        <h3>OCR 前图片增强</h3>
        <Field label="预处理配置">
          <select onChange={(event) => {
            const profile = event.target.value as ProjectSettings['preprocessing']['profile'];
            updatePreprocessing(preprocessingSettingsForProfile(profile, settings.preprocessing));
          }} value={settings.preprocessing.profile}>
            <option value="off">关闭</option>
            <option value="ocr-friendly">OCR 友好</option>
            <option value="balanced">平衡</option>
            <option value="visual-quality">视觉质量</option>
          </select>
        </Field>
        <div className="field-grid">
          <Field label="超分倍数"><select disabled={!settings.preprocessing.enableUpscale} onChange={(event) => updatePreprocessing({ upscaleFactor: Number(event.target.value) as 2 | 3 | 4 })} value={settings.preprocessing.upscaleFactor}><option value={2}>2×</option><option value={3}>3×</option><option value={4}>4×</option></select></Field>
          <Field label="二值化阈值"><input disabled={!settings.preprocessing.enableBinarize} max={255} min={0} onChange={(event) => updatePreprocessing({ threshold: Number(event.target.value) })} type="number" value={settings.preprocessing.threshold} /></Field>
        </div>
        <Toggle checked={settings.preprocessing.enableUpscale} label="超分放大" onChange={(event) => updatePreprocessing({ enableUpscale: event.target.checked })} />
        <Toggle checked={settings.preprocessing.enableDenoise} label="去噪" onChange={(event) => updatePreprocessing({ enableDenoise: event.target.checked })} />
        <Toggle checked={settings.preprocessing.enableSharpen} label="锐化" onChange={(event) => updatePreprocessing({ enableSharpen: event.target.checked })} />
        <Toggle checked={settings.preprocessing.enableContrastEnhance} label="增强对比度" onChange={(event) => updatePreprocessing({ enableContrastEnhance: event.target.checked })} />
        <Toggle checked={settings.preprocessing.enableEdgeOptimize} label="边缘优化" onChange={(event) => updatePreprocessing({ enableEdgeOptimize: event.target.checked })} />
        <Toggle checked={settings.preprocessing.enableBinarize} label="OCR 二值化" onChange={(event) => updatePreprocessing({ enableBinarize: event.target.checked })} />
        {image ? (
          <p className="field-hint">
            当前页建议 {preprocessingProfileLabels[image.preprocessSuggestion.profile]}
            {image.preprocessSuggestion.profile === settings.preprocessing.profile
              ? '，与项目默认一致。建议不会自动套用到整本。'
              : '；这是本页提示，不会自动改项目默认。'}
          </p>
        ) : null}
      </section>
      {translator?.isMock ? (
        <div className="notice notice--mock"><b>演示 MOCK 翻译</b><span>输出是确定性演示文本，不代表真实翻译质量，导出前必须复核。</span></div>
      ) : null}
      <div className={`notice ${isRemote ? 'notice--remote' : 'notice--local'}`}>
        <b>{isRemote ? '远程文本翻译' : '本地 / 手动模式'}</b>
        <span>
          {isRemote
            ? '只会发送当前文本、当前页前后相邻文本块、术语表和角色名；原图、擦除图和项目路径绝不发送。请确认你有权向所选服务提交文本。'
            : '图像和文本留在本机。本地 Argos 日→中翻译经英语中转，不会把文本发到外部服务；手动模式也不会发起外部请求。'}
        </span>
      </div>
      {isRemote ? (
        <>
          <Field label="兼容 API 地址" hint="地址会写入项目配置；不要在 URL 中放凭据。"><input onChange={(event) => update({ remoteEndpoint: event.target.value })} placeholder="https://example.com/v1" type="url" value={settings.remoteEndpoint} /></Field>
          <Field label="模型名称"><input onChange={(event) => update({ remoteModel: event.target.value })} value={settings.remoteModel} /></Field>
          <Field label="API Key（仅当前会话）" hint="密钥不会写入 SQLite、project.json、浏览器存储或日志。">
            <div className="inline-input-button"><input autoComplete="off" onChange={(event) => setSessionKey(event.target.value)} placeholder={sessionConfigured ? '已在当前会话配置' : 'sk-…'} type="password" value={sessionKey} /><button className="button" disabled={!sessionKey} onClick={() => void saveSessionKey()} type="button">应用</button></div>
          </Field>
          {credentialState ? <p className="credential-state" role="status">{credentialState}</p> : null}
        </>
      ) : null}
      <Field label="术语表" hint="每行“日文 = 中文”。远程模式下这些条目会随翻译请求发送；本地 Argos 翻译把它们当作需保留的专名。"><textarea onChange={(event) => update({ glossary: event.target.value })} rows={5} value={settings.glossary} /></Field>
      <Field label="角色名" hint="每行一个名字或“日文 = 中文”。"><textarea onChange={(event) => update({ characterNames: event.target.value })} rows={4} value={settings.characterNames} /></Field>
      <Toggle checked={settings.preserveTree} description="导出时重建导入时的相对目录" label="保留目录结构" onChange={(event) => update({ preserveTree: event.target.checked })} />
    </div>
  );
}

function ReviewBoxTools() {
  const image = useWorkbenchStore(activeImage);
  const regionsByImage = useWorkbenchStore((state) => state.regionsByImage);
  const consolidateActiveImageRegions = useWorkbenchStore((state) => state.consolidateActiveImageRegions);
  const startBatch = useWorkbenchStore((state) => state.startBatch);
  const setDrawerOpen = useWorkbenchStore((state) => state.setDrawerOpen);
  const providers = useWorkbenchStore((state) => state.capabilities.providers);
  if (!image) return null;
  const regionCount = (regionsByImage[image.id] ?? []).length;
  const aiProvider = preferredAiRedrawProvider(providers);
  const lowRes = Math.min(image.width, image.height) < 1400;

  return (
    <div className={`notice ${lowRes ? 'notice--warning' : 'notice--local'}`} role="status">
      <b>{lowRes ? '低分辨率页可人工 AI 重绘' : '本页选框与画质'}</b>
      <span>
        选框过多或没包住字时，先整理本页重叠碎片并外扩。扫描糊、分辨率低时，基础增强往往不够，可用本地 Real-ESRGAN 动漫 4× 重绘当前页；不会自动跑。
      </span>
      <div className="notice__actions">
        <button
          className="button button--compact"
          disabled={!regionCount}
          onClick={() => consolidateActiveImageRegions()}
          type="button"
        >
          整理本页选框
        </button>
        <button
          className="button button--compact"
          disabled={!aiProvider}
          onClick={() => {
            if (!aiProvider) return;
            setDrawerOpen(true);
            void startBatch(
              ['preprocess'],
              [image.id],
              defaultExportOptions,
              1,
              undefined,
              AI_REDRAW_PREPROCESSING,
              aiProvider,
            );
          }}
          type="button"
        >
          AI 重绘本页
        </button>
      </div>
      {aiProvider ? null : (
        <span>本地 Real-ESRGAN 不可用。安装 `ai` extra 和动漫超分模型后即可重绘。</span>
      )}
    </div>
  );
}

function PreprocessSuggestionNotice() {
  const image = useWorkbenchStore(activeImage);
  const project = useWorkbenchStore((state) => state.currentProject);
  const startBatch = useWorkbenchStore((state) => state.startBatch);
  const setDrawerOpen = useWorkbenchStore((state) => state.setDrawerOpen);
  const updateProjectSettings = useWorkbenchStore((state) => state.updateProjectSettings);
  if (!image || !project) return null;
  const imageId = image.id;
  const suggestion = image.preprocessSuggestion;
  const matchesDefault = suggestion.profile === project.settings.preprocessing.profile;
  const suggested = preprocessingSettingsForProfile(suggestion.profile, project.settings.preprocessing);
  const reasons = suggestion.reasons
    .map((reason) => preprocessSuggestionReasonLabels[reason] ?? reason)
    .join('；');

  function applyToPage() {
    setDrawerOpen(true);
    void startBatch(['preprocess'], [imageId], defaultExportOptions, 1, undefined, suggested);
  }

  function adoptDefault() {
    updateProjectSettings({ preprocessing: suggested });
  }

  return (
    <div className={`notice ${matchesDefault ? 'notice--local' : 'notice--warning'}`} role="status">
      <b>本页建议预处理：{preprocessingProfileLabels[suggestion.profile]}</b>
      <span>
        {reasons || '按本页尺寸和采样统计给出，不会自动套用到整本。'}
        {suggestion.metrics.sampled ? '' : ' 完整对比度/锐度统计会在导入时写入。'}
        {' '}处理完成后画布会切到增强预览，便于对比原图。
      </span>
      <div className="notice__actions">
        <button className="button button--compact" onClick={applyToPage} type="button">
          按建议处理本页
        </button>
        {matchesDefault ? null : (
          <button className="button button--compact" onClick={adoptDefault} type="button">
            采用为项目默认
          </button>
        )}
      </div>
    </div>
  );
}

function TypesetOverflowNotice({ regions }: { regions: Region[] }) {
  const image = useWorkbenchStore(activeImage);
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const setRightTab = useWorkbenchStore((state) => state.setRightTab);
  const setDrawerOpen = useWorkbenchStore((state) => state.setDrawerOpen);
  const startBatch = useWorkbenchStore((state) => state.startBatch);
  const focusRegions = useWorkbenchStore((state) => state.focusRegions);
  if (!imageHasTypesetOverflow(image) || !image) return null;
  const overflowIds = overflowingRegionIds(image, regions);
  const overflowing = regions.find((region) => overflowIds.includes(region.id));
  return (
    <div className="notice notice--warning" role="status">
      <b>{image.typesetOverflowCount} 个文本框排版溢出</b>
      <span>溢出页仍可目视接受，但导出前应复核字号和框大小。只重排溢出框不会改动其他文本框。</span>
      <div className="notice__actions">
        {overflowing ? (
          <button
            className="text-button"
            onClick={() => {
              selectRegion(overflowing.id);
              setRightTab('typesetting');
              focusRegions([overflowing.id]);
            }}
            type="button"
          >
            打开 #{overflowing.order}
          </button>
        ) : null}
        {overflowIds.length ? (
          <button
            className="button button--compact"
            onClick={() => {
              overflowIds.forEach((regionId, index) => selectRegion(regionId, index > 0));
              setRightTab('typesetting');
              focusRegions(overflowIds);
            }}
            type="button"
          >
            选中溢出框
          </button>
        ) : null}
        {overflowIds.length ? (
          <button
            className="button button--compact"
            onClick={() => {
              setDrawerOpen(true);
              void startBatch(['typeset'], [image.id], defaultExportOptions, 1, overflowIds);
            }}
            title="⇧T"
            type="button"
          >
            只重排溢出框
          </button>
        ) : null}
      </div>
    </div>
  );
}

function EmptyLibraryState() {
  return (
    <EmptyState
      icon="▧"
      title="尚未导入图像"
      description="手机请用「多图」从相册导入；处理仍在 Mac 上运行。"
      action={<ImportPhotosButton />}
    />
  );
}

export function Inspector() {
  const project = useWorkbenchStore((state) => state.currentProject);
  const hasLibrary = useWorkbenchStore((state) => state.images.length > 0);
  const tab = useWorkbenchStore((state) => state.rightTab);
  const setRightTab = useWorkbenchStore((state) => state.setRightTab);
  const activeImageId = useWorkbenchStore((state) => state.activeImageId);
  const regionsByImage = useWorkbenchStore((state) => state.regionsByImage);
  const selectedRegionIds = useWorkbenchStore((state) => state.selectedRegionIds);
  const regions = activeImageId ? regionsByImage[activeImageId] ?? EMPTY_REGIONS : EMPTY_REGIONS;
  const selected = useMemo(() => {
    const ids = new Set(selectedRegionIds);
    return regions.filter((region) => ids.has(region.id));
  }, [regions, selectedRegionIds]);

  const helperNotices = hasLibrary ? (
    <>
      <ReviewBoxTools />
      <PreprocessSuggestionNotice />
      <TypesetOverflowNotice regions={regions} />
    </>
  ) : null;
  const editingSelectedText = tab === 'text' && selected.length > 0;

  return (
    <aside className="inspector panel" aria-label="属性检查器">
      <nav className="inspector-tabs" aria-label="属性标签">
        {([
          ['text', '文本'],
          ['typesetting', '排版'],
          ['repair', '修复'],
          ['project', '项目'],
        ] as const).map(([value, label]) => (
          <button aria-selected={tab === value} key={value} onClick={() => setRightTab(value)} role="tab" type="button">{label}</button>
        ))}
      </nav>
      <div className="inspector__content" role="tabpanel">
        {!project ? (
          <EmptyState
            icon="⚙"
            title="未打开项目"
            description="先创建本机项目，再用手机从相册导入。处理仍在 Mac 上运行。"
            action={<CreateLocalProjectButton />}
          />
        ) : (
          <>
            {hasLibrary ? (
              <>
                <PageReviewControl regions={regions} />
                <ProcessingErrorNotice />
                <ProcessingActivityNotice />
                {editingSelectedText ? null : helperNotices}
              </>
            ) : null}
            {tab === 'text' ? (hasLibrary ? <TextInspector regions={regions} selected={selected} /> : <EmptyLibraryState />) : null}
            {editingSelectedText ? helperNotices : null}
            {tab === 'typesetting' ? (
              hasLibrary
                ? <TypesettingInspector region={selected.length === 1 ? selected[0] : undefined} />
                : <EmptyLibraryState />
            ) : null}
            {tab === 'repair' ? (
              hasLibrary
                ? <RepairInspector region={selected.length === 1 ? selected[0] : undefined} />
                : <EmptyLibraryState />
            ) : null}
            {tab === 'project' ? <ProjectInspector /> : null}
          </>
        )}
      </div>
    </aside>
  );
}
