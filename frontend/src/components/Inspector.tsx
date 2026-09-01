import { useEffect, useMemo, useState } from 'react';

import { api } from '../api/client';
import {
  activeImage,
  backgroundClassificationComplete,
  backgroundClassificationRequired,
  BACKGROUND_CATEGORIES,
  BACKGROUND_RATIONALE_ANCHOR,
  g4EditingLocked,
  g5EditingLocked,
  g6EditingLocked,
  g7EditingLocked,
  g8EditingLocked,
  g4RegionsAccepted,
  imageHasActiveDetectJob,
  imageHasTypesetOverflow,
  latestG4RegionChecksum,
  latestPageProcessingActivity,
  latestPageProcessingError,
  ocrSourceReviewRequired,
  ocrSourceReviewComplete,
  maskRegionRequired,
  overflowingRegionIds,
  AI_REDRAW_PREPROCESSING,
  preprocessingSettingsForProfile,
  preferredAiRedrawProvider,
  regionHasTypesetOverflow,
  useWorkbenchStore,
  workflowPhase,
} from '../store/workbench';
import { clampRegionGeometry } from './canvasGeometry';
import type {
  ExportOptions,
  BackgroundCategory,
  BackgroundRationaleCode,
  CleanPlateCheck,
  OCRAttempt,
  OCRQCCheck,
  OCRSourceMode,
  MaskCheckResult,
  MaskCollateralCheck,
  MaskCoverageCheck,
  MaskDraftRegion,
  ImageAsset,
  InpaintCandidate,
  JobKind,
  PreprocessingSettings,
  ProjectSettings,
  ProviderCapability,
  Region,
  RegionContentDisposition,
  RegionType,
  TextDirection,
  RegionDisposition,
  TranslationOriginKind,
  TranslationQCCheck,
  TranslationQCFlag,
  TranslationReviewReason,
  TypesetCheck,
  TypesetRegionStyleInput,
  TypesetReviewReason,
} from '../types';
import {
  CLEAN_PLATE_CHECKS,
  MASK_COLLATERAL_CHECKS,
  MASK_COVERAGE_CHECKS,
  OCR_QC_CHECKS,
  TRANSLATION_QC_CHECKS,
  TYPESET_CHECKS,
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
  commitOnChange = true,
  disabled = false,
  label,
  min,
  onCommit,
  step,
  value,
}: {
  ariaLabel: string;
  commitOnChange?: boolean;
  disabled?: boolean;
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
        disabled={disabled}
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
          if (commitOnChange && Number.isFinite(next)) onCommit(next);
        }}
        step={step}
        type="number"
        value={draft ?? value}
      />
    </Field>
  );
}

function CommitTextField({
  ariaLabel,
  disabled,
  label,
  onCommit,
  placeholder,
  value,
}: {
  ariaLabel: string;
  disabled: boolean;
  label: string;
  onCommit: (next: string) => void;
  placeholder?: string;
  value: string;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  return (
    <Field label={label}>
      <input
        aria-label={ariaLabel}
        disabled={disabled}
        onBlur={() => {
          if (draft !== null && draft !== value) onCommit(draft);
          setDraft(null);
        }}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.currentTarget.blur();
        }}
        placeholder={placeholder}
        type="text"
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
  mask: '重试本页蒙版',
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

const g4DispositionLabels: Record<RegionContentDisposition, string> = {
  translate: '翻译并处理文字',
  ignore: '忽略文字处理',
  'keep-art': '保留美术字',
  'redraw-art': '重绘美术字',
  'false-positive': '误检',
};

function g4RegionValidationIssues(regions: Region[], image: ImageAsset): string[] {
  const issues = new Set<string>();
  if (!regions.length) issues.add('至少需要一个区域');
  if (!regions.some((region) => region.contentDisposition !== 'false-positive')) {
    issues.add('至少需要一个真实文字区域');
  }
  if (
    [...regions].map((region) => region.order).sort((left, right) => left - right)
      .some((order, index) => order !== index)
  ) issues.add('阅读顺序必须连续');
  const byId = new Map(regions.map((region) => [region.id, region]));
  for (const region of regions) {
    if (!region.contentDisposition) issues.add('仍有区域未选择处理决定');
    if (
      ![region.x, region.y, region.width, region.height, region.rotation].every(Number.isFinite)
      || region.x < 0
      || region.y < 0
      || region.width <= 0
      || region.height <= 0
      || region.x + region.width > image.width + 0.001
      || region.y + region.height > image.height + 0.001
    ) issues.add('仍有区域几何无效');
    if (
      (region.detectorJobItemId === null) !== (region.detectorCandidateIndex === null)
      || (region.detectorCandidateIndex !== null && region.detectorCandidateIndex < 0)
    ) issues.add('检测候选身份不完整');
    if (region.contentDisposition === 'false-positive') {
      if (region.rubyParentId) issues.add('误检框不能保留注音父框');
      continue;
    }
    if (region.direction !== 'horizontal' && region.direction !== 'vertical') {
      issues.add('仍有区域方向未确认');
    }
    if (region.type === 'unknown') issues.add('仍有区域类型未确认');
    if (!region.paragraphGroupId) issues.add('仍有区域缺少段落组');
    if (
      region.type === 'sound_effect'
      && region.contentDisposition !== 'ignore'
      && region.contentDisposition !== 'keep-art'
      && region.contentDisposition !== 'redraw-art'
    ) issues.add('拟声词处理决定无效');
    if (region.type !== 'ruby') {
      if (region.rubyParentId) issues.add('非注音框不能保留注音父框');
      continue;
    }
    const parent = region.rubyParentId ? byId.get(region.rubyParentId) : undefined;
    if (region.contentDisposition !== 'ignore') issues.add('注音框必须忽略独立文字处理');
    if (!parent) issues.add('注音框缺少正文父框');
    else if (parent.type === 'ruby' || parent.contentDisposition === 'false-positive') {
      issues.add('注音父框无效');
    } else if (parent.paragraphGroupId !== region.paragraphGroupId) {
      issues.add('注音框与正文框段落组不一致');
    }
  }
  return [...issues];
}

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

function evidenceRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
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
  const startG4Detection = useWorkbenchStore((state) => state.startG4Detection);
  const openQueueForImage = useWorkbenchStore((state) => state.openQueueForImage);
  const g4Context = useWorkbenchStore((state) =>
    state.activeImageId ? state.g4Contexts[state.activeImageId] : undefined,
  );
  const failure = latestPageProcessingError(image);
  if (!image || !failure) return null;
  const retryKind = failure.kind;
  const retryLabel = retryKind ? retryStageLabels[retryKind] : undefined;

  return (
    <div className="notice notice--error" role="alert">
      <b>{processingStageTitles[failure.stage] ?? processingStageTitles.processing}</b>
      <span>详情只保存在本机项目日志中。可重试这一页，或打开批处理抽屉查看队列。</span>
      <div className="notice__actions">
        {retryKind && retryLabel && (
          g4Context?.status === 'legacy'
          || (
            g4Context?.status === 'active'
            && workflowPhase(g4Context) === 'G4'
            && retryKind === 'detect'
          )
        ) ? (
          <button
            className="button button--compact"
            onClick={() => {
              if (
                g4Context?.status === 'active'
                && workflowPhase(g4Context) === 'G4'
                && retryKind === 'detect'
              ) {
                void startG4Detection();
              } else {
                void startBatch([retryKind], [image.id], defaultExportOptions, 1);
              }
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

function G4RegionsControl({ regions }: { regions: Region[] }) {
  const image = useWorkbenchStore(activeImage);
  const context = useWorkbenchStore((state) =>
    state.activeImageId ? state.g4Contexts[state.activeImageId] : undefined,
  );
  const locked = useWorkbenchStore((state) => g4EditingLocked(state));
  const detectBusy = useWorkbenchStore((state) =>
    state.activeImageId ? imageHasActiveDetectJob(state, state.activeImageId) : false,
  );
  const pendingMutation = useWorkbenchStore((state) =>
    state.activeImageId
      ? state.pendingG4Mutations.some((mutation) => mutation.imageId === state.activeImageId)
      : false,
  );
  const saving = useWorkbenchStore((state) =>
    Boolean(state.activeImageId && (
      state.g4SavingImageId === state.activeImageId
      || state.g4GateSavingImageId === state.activeImageId
    )),
  );
  const startG4Detection = useWorkbenchStore((state) => state.startG4Detection);
  const acceptG4Regions = useWorkbenchStore((state) => state.acceptG4Regions);
  const reloadActiveImage = useWorkbenchStore((state) => state.reloadActiveImage);

  if (!image || !context || context.status === 'loading') {
    return (
      <div className="notice notice--warning" aria-busy="true" role="status">
        <b>正在读取 G4 血缘</b>
        <span>确认活动页代次前，本页编辑保持锁定。</span>
      </div>
    );
  }
  if (context.status === 'error' || context.error) {
    return (
      <div className="notice notice--error" role="alert">
        <b>{context.conflict ? 'G4 版本冲突' : 'G4 血缘不可用'}</b>
        <span>{context.error || '无法确认本页唯一活动代次。'}</span>
        <div className="notice__actions">
          <button className="button button--compact" onClick={() => void reloadActiveImage()} type="button">
            重载本页
          </button>
        </div>
      </div>
    );
  }
  if (context.status !== 'active' || workflowPhase(context) !== 'G4') return null;

  const accepted = g4RegionsAccepted(context);
  const checksum = latestG4RegionChecksum(context);
  const validationIssues = g4RegionValidationIssues(regions, image);
  const latest = context.events.at(-1);
  const stageLabel = detectBusy
    ? '检测运行中'
    : saving || pendingMutation
      ? '正在保存区域证据'
      : accepted
        ? 'G4 已接受'
        : validationIssues.length
          ? `还有 ${validationIssues.length} 类决定待修正`
        : checksum
          ? 'G4 待人工接受'
          : '尚无可接受的检测草稿';
  return (
    <section className={`page-review page-review--${accepted ? 'done' : 'pending'}`} aria-label="G4 区域门禁">
      <div>
        <span>G4 区域</span>
        <strong>{stageLabel}</strong>
        <small>
          {regions.length} 个框 · 序号 {context.generation?.nextSequence ?? '—'}
          {latest ? ` · ${latest.operation}` : ''}
        </small>
        {validationIssues.length ? <small>{validationIssues[0]}</small> : null}
      </div>
      <div className="notice__actions">
        <button
          className="button button--compact"
          disabled={locked}
          onClick={() => void startG4Detection()}
          type="button"
        >
          {detectBusy ? '检测进行中…' : checksum ? '重新检测本页' : '检测本页'}
        </button>
        <button
          className="button button--compact button--accent"
          disabled={locked || accepted || !checksum || validationIssues.length > 0}
          onClick={() => void acceptG4Regions()}
          type="button"
        >
          {accepted ? '区域已接受' : '接受全部区域决定'}
        </button>
      </div>
    </section>
  );
}

function G4TextInspector({ regions, selected }: { regions: Region[]; selected: Region[] }) {
  const image = useWorkbenchStore(activeImage);
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const focusRegions = useWorkbenchStore((state) => state.focusRegions);
  const updateRegion = useWorkbenchStore((state) => state.updateRegion);
  const nudgeSelectedRegions = useWorkbenchStore((state) => state.nudgeSelectedRegions);
  const deleteSelectedRegions = useWorkbenchStore((state) => state.deleteSelectedRegions);
  const moveG4Region = useWorkbenchStore((state) => state.moveG4Region);
  const locked = useWorkbenchStore((state) => g4EditingLocked(state));

  if (!selected.length) {
    if (!regions.length) {
      return (
        <EmptyState
          icon="框"
          title="尚无区域草稿"
          description="先运行检测；若检测结果为空，可在画布上手动绘制至少一个真实文字区域。"
        />
      );
    }
    return (
      <div className="region-index" aria-label="G4 区域列表">
        <p className="panel-hint">逐个选择并确认类型、方向、段落组及处理决定。</p>
        {[...regions].sort((left, right) => left.order - right.order).map((region) => (
          <button
            aria-label={`选择 G4 文本框 #${region.order}`}
            key={region.id}
            onClick={() => {
              selectRegion(region.id);
              focusRegions([region.id]);
            }}
            type="button"
          >
            <b>#{region.order}</b>
            <span>{regionTypeLabels[region.type]}</span>
            <em>{region.contentDisposition ? g4DispositionLabels[region.contentDisposition] : '决定缺失'}</em>
          </button>
        ))}
      </div>
    );
  }

  if (selected.length !== 1) {
    return (
      <div className="notice notice--warning" role="status">
        <b>G4 请逐框编辑</b>
        <span>取消多选后逐个保存，确保每次区域变更都有独立、连续的血缘事件。</span>
      </div>
    );
  }

  const region = selected[0]!;
  const falsePositive = region.contentDisposition === 'false-positive';
  const detectorDerived = region.detectorJobItemId !== null
    || region.detectorCandidateIndex !== null;
  const detectionEvidence = evidenceRecord(region.recognition.detection);
  const detectorProvider = typeof detectionEvidence?.provider === 'string'
    ? detectionEvidence.provider
    : '未记录';
  const detectorInput = typeof detectionEvidence?.inputVariant === 'string'
    ? detectionEvidence.inputVariant
    : '未记录';
  const detectorLanguage = typeof detectionEvidence?.language === 'string'
    ? detectionEvidence.language
    : '未记录';
  const detectedTextCandidate = typeof region.repair.detectedTextCandidate === 'string'
    && region.repair.detectedTextCandidate.trim()
    ? region.repair.detectedTextCandidate
    : '（检测器未返回文字候选）';
  const sorted = [...regions].sort((left, right) => left.order - right.order);
  const orderIndex = sorted.findIndex((entry) => entry.id === region.id);
  const rubyParents = regions.filter((entry) =>
    entry.id !== region.id
    && entry.type !== 'ruby'
    && entry.contentDisposition !== 'false-positive'
    && Boolean(entry.paragraphGroupId),
  );
  const dispositionOptions: RegionContentDisposition[] = region.type === 'ruby'
    ? ['ignore', 'false-positive']
    : region.type === 'sound_effect'
      ? ['ignore', 'keep-art', 'redraw-art', 'false-positive']
      : ['translate', 'ignore', 'keep-art', 'redraw-art', 'false-positive'];

  return (
    <div className="form-stack text-inspector" aria-busy={locked}>
      <div className="region-heading">
        <div><span>G4 文本框</span><strong>#{region.order}</strong></div>
        <span className={`review-state review-state--${region.contentDisposition ? 'confirmed' : 'pending'}`}>
          {region.contentDisposition ? g4DispositionLabels[region.contentDisposition] : '待决定'}
        </span>
      </div>
      <Field label="内容处理决定">
        <select
          aria-label="G4 内容处理决定"
          disabled={locked}
          onChange={(event) => {
            const contentDisposition = event.target.value as RegionContentDisposition;
            updateRegion(region.id, {
              contentDisposition,
              ...(contentDisposition === 'false-positive' ? { rubyParentId: null } : {}),
            });
          }}
          value={region.contentDisposition ?? ''}
        >
          <option disabled value="">请选择…</option>
          {dispositionOptions.map((value) => (
            <option key={value} value={value}>{g4DispositionLabels[value]}</option>
          ))}
        </select>
      </Field>
      {!falsePositive ? (
        <>
          <div className="field-grid">
            <Field label="类型">
              <select
                aria-label="G4 文本类型"
                disabled={locked}
                onChange={(event) => {
                  const type = event.target.value as RegionType;
                  updateRegion(region.id, {
                    type,
                    ...(type === 'ruby'
                      ? { contentDisposition: 'ignore' as const, rubyParentId: null }
                      : region.type === 'ruby'
                        ? { rubyParentId: null }
                        : {}),
                  });
                }}
                value={region.type}
              >
                {Object.entries(regionTypeLabels).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
            </Field>
            <Field label="方向">
              <select
                aria-label="G4 文本方向"
                disabled={locked}
                onChange={(event) => updateRegion(region.id, {
                  direction: event.target.value as TextDirection,
                })}
                value={region.direction}
              >
                <option value="auto">待确认（自动）</option>
                <option value="vertical">竖排</option>
                <option value="horizontal">横排</option>
              </select>
            </Field>
          </div>
          <CommitTextField
            ariaLabel="G4 段落组"
            disabled={locked}
            label="段落组"
            onCommit={(value) => updateRegion(region.id, {
              paragraphGroupId: value.trim() || null,
            })}
            placeholder="例如 paragraph-01"
            value={region.paragraphGroupId ?? ''}
          />
          {region.type === 'ruby' ? (
            <Field label="注音所属正文框">
              <select
                aria-label="G4 注音父框"
                disabled={locked}
                onChange={(event) => {
                  const parent = rubyParents.find((entry) => entry.id === event.target.value);
                  updateRegion(region.id, {
                    rubyParentId: parent?.id ?? null,
                    paragraphGroupId: parent?.paragraphGroupId ?? region.paragraphGroupId,
                    contentDisposition: 'ignore',
                  });
                }}
                value={region.rubyParentId ?? ''}
              >
                <option value="">请选择正文框…</option>
                {rubyParents.map((parent) => (
                  <option key={parent.id} value={parent.id}>
                    #{parent.order} · {parent.paragraphGroupId}
                  </option>
                ))}
              </select>
            </Field>
          ) : null}
        </>
      ) : (
        <div className="notice" role="status">
          <b>已标记为误检</b>
          <span>误检框不要求类型、方向或段落关系，但仍保留几何与检测身份作为审计证据。</span>
        </div>
      )}
      {image ? (
        <section className="form-stack" aria-label="G4 选框几何">
          <div className="field-grid">
            {([
              ['x', 'X', 'G4 选框 X', 0],
              ['y', 'Y', 'G4 选框 Y', 0],
              ['width', '宽', 'G4 选框宽度', 4],
              ['height', '高', 'G4 选框高度', 4],
              ['rotation', '旋转 °', 'G4 选框旋转', undefined],
            ] as const).map(([key, label, ariaLabel, min]) => (
              <GeometryNumberField
                ariaLabel={ariaLabel}
                commitOnChange={false}
                disabled={locked}
                key={`${region.id}-${key}`}
                label={label}
                min={min}
                onCommit={(value) => updateRegion(
                  region.id,
                  clampRegionGeometry({ ...region, [key]: value }, image),
                )}
                step={key === 'rotation' ? '0.1' : undefined}
                value={region[key]}
              />
            ))}
          </div>
          <div className="nudge-actions" aria-label="G4 微调选框">
            <button className="button button--compact" disabled={locked} onClick={() => nudgeSelectedRegions(0, -1)} type="button">上移 1px</button>
            <button className="button button--compact" disabled={locked} onClick={() => nudgeSelectedRegions(0, 1)} type="button">下移 1px</button>
            <button className="button button--compact" disabled={locked} onClick={() => nudgeSelectedRegions(-1, 0)} type="button">左移 1px</button>
            <button className="button button--compact" disabled={locked} onClick={() => nudgeSelectedRegions(1, 0)} type="button">右移 1px</button>
          </div>
        </section>
      ) : null}
      <div className="split-actions" aria-label="G4 阅读顺序">
        <button className="button" disabled={locked || orderIndex <= 0} onClick={() => void moveG4Region(region.id, -1)} type="button">顺序上移</button>
        <button className="button" disabled={locked || orderIndex < 0 || orderIndex >= sorted.length - 1} onClick={() => void moveG4Region(region.id, 1)} type="button">顺序下移</button>
      </div>
      {detectorDerived ? (
        <div aria-label="G4 检测候选证据" className="notice" role="status">
          <b>检测候选证据</b>
          <span>任务项：{region.detectorJobItemId ?? '缺失'} · 候选序号：{region.detectorCandidateIndex ?? '缺失'}</span>
          <span>Provider：{detectorProvider} · 检测置信度：{percent(region.detectorConfidence)}</span>
          <span>输入：{detectorInput} · 语言：{detectorLanguage}</span>
          <span>文字候选：{detectedTextCandidate}</span>
          <span>请选择处理决定；若不是文字，请标记为“误检”，不能直接删除。</span>
        </div>
      ) : (
        <button className="button button--danger" disabled={locked} onClick={deleteSelectedRegions} type="button">删除这个 G4 文本框</button>
      )}
    </div>
  );
}

const backgroundCategoryLabels: Record<BackgroundCategory, string> = {
  'white-solid': '纯白 / 近白',
  'black-solid': '纯黑 / 近黑',
  'other-solid': '其他纯色',
  'simple-gradient': '简单渐变',
  screentone: '网点',
  'complex-lineart': '复杂线稿',
  'illustration/character': '插画 / 人物',
};

const backgroundRationaleLabels: Record<BackgroundRationaleCode, string> = {
  'uniform-near-white': '区域近白且均匀',
  'uniform-near-black': '区域近黑且均匀',
  'uniform-other-color': '区域为其他均匀色',
  'smooth-gradient-continuity': '存在连续平滑渐变',
  'periodic-screentone': '存在周期性网点纹理',
  'structural-lines-cross-region': '结构线穿过文字区域',
  'character-or-illustration-detail': '包含人物或插画细节',
  'mixed-visual-signals': '同时存在混合视觉信号',
};

function backgroundReviewerLabel(region: Region): string {
  const reviewer = region.backgroundReviewer;
  if (!reviewer) return '尚未由服务端记录';
  return reviewer.actorId
    || reviewer.taskId
    || reviewer.threadId
    || reviewer.sessionId
    || reviewer.actorKind;
}

function G5BackgroundControl({ regions }: { regions: Region[] }) {
  const image = useWorkbenchStore(activeImage);
  const context = useWorkbenchStore((state) =>
    state.activeImageId ? state.g4Contexts[state.activeImageId] : undefined,
  );
  const background = useWorkbenchStore((state) =>
    state.activeImageId ? state.backgroundContexts[state.activeImageId] : undefined,
  );
  const loading = useWorkbenchStore((state) =>
    state.activeImageId ? Boolean(state.backgroundLoading[state.activeImageId]) : false,
  );
  const locked = useWorkbenchStore((state) => g5EditingLocked(state));
  const saving = useWorkbenchStore((state) => Boolean(
    state.g5SavingRegionId
    || (state.activeImageId && state.g5GateSavingImageId === state.activeImageId),
  ));
  const acceptG5Background = useWorkbenchStore((state) => state.acceptG5Background);
  const reloadActiveImage = useWorkbenchStore((state) => state.reloadActiveImage);

  if (!image || !context || !context.generation || workflowPhase(context) !== 'G5') return null;
  if (context.error) {
    return (
      <div className="notice notice--error" role="alert">
        <b>{context.conflict ? 'G5 版本冲突' : 'G5 血缘不可用'}</b>
        <span>{context.error}</span>
        <div className="notice__actions">
          <button className="button button--compact" onClick={() => void reloadActiveImage()} type="button">重载本页</button>
        </div>
      </div>
    );
  }
  if (loading || !background) {
    return (
      <div className="notice notice--warning" aria-busy="true" role="status">
        <b>正在读取 G5 背景门禁</b>
        <span>权威校验和与区域集合就绪前，分类和接受保持锁定。</span>
      </div>
    );
  }
  if (
    background.generationId !== context.generation.id
    || background.nextSequence !== context.generation.nextSequence
    || background.imageRevision !== image.revision
  ) {
    return (
      <div className="notice notice--warning" aria-busy="true" role="status">
        <b>正在核对新的 G5 权威上下文</b>
        <span>页代次、序号或图像版本变化期间，分类和接受保持锁定。</span>
      </div>
    );
  }

  const localEligible = regions.filter(backgroundClassificationRequired);
  const localEligibleIds = localEligible.map((region) => region.id).sort();
  const authoritativeEligibleIds = [...background.eligibleRegionIds].sort();
  const classifiedIds = [...background.classifiedRegionIds].sort();
  const allComplete = localEligible.every((region) =>
    context.generation
      ? backgroundClassificationComplete(region, context.generation.id)
      : false
  );
  const ready = background.state === 'pending'
    && localEligibleIds.join('\0') === authoritativeEligibleIds.join('\0')
    && localEligibleIds.join('\0') === classifiedIds.join('\0')
    && allComplete;

  return (
    <section className="page-review page-review--pending" aria-label="G5 背景门禁">
      <div>
        <span>G5 背景</span>
        <strong>
          {background.classifiedRegionIds.length} / {background.eligibleRegionIds.length} 个适用区域已分类
        </strong>
        <small>置信度只作证据，不设自动阈值；0 与低分仍由人工决定。</small>
      </div>
      <button
        className="button button--compact button--accent"
        disabled={locked || saving || !ready}
        onClick={() => void acceptG5Background()}
        type="button"
      >
        {saving
          ? '正在保存…'
          : background.eligibleRegionIds.length
            ? '接受全部背景分类'
            : '确认本页 G5 不适用'}
      </button>
    </section>
  );
}

function G5BackgroundInspector({ regions, selected }: { regions: Region[]; selected: Region[] }) {
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const focusRegions = useWorkbenchStore((state) => state.focusRegions);
  const region = selected.length === 1 ? selected[0] : undefined;

  if (!selected.length) {
    return (
      <div className="region-index" aria-label="G5 背景分类列表">
        <p className="panel-hint">在原图与已接受的质量底板之间对照，逐个记录背景类别。</p>
        {[...regions].sort((left, right) => left.order - right.order).map((entry) => {
          const eligible = backgroundClassificationRequired(entry);
          return (
            <button
              aria-label={`选择 G5 文本框 #${entry.order}`}
              key={entry.id}
              onClick={() => {
                selectRegion(entry.id);
                focusRegions([entry.id]);
              }}
              type="button"
            >
              <b>#{entry.order}</b>
              <span>
                {regionTypeLabels[entry.type]} · {entry.contentDisposition
                  ? g4DispositionLabels[entry.contentDisposition]
                  : '处置缺失'}
              </span>
              <em>
                {eligible
                  ? entry.backgroundCategory
                    ? [
                        backgroundCategoryLabels[entry.backgroundCategory],
                        String(entry.backgroundConfidence),
                        backgroundReviewerLabel(entry),
                      ].join(' · ')
                    : '待分类'
                  : entry.type === 'ruby' ? '随正文父框' : '不适用'}
              </em>
            </button>
          );
        })}
      </div>
    );
  }
  if (selected.length !== 1 || !region) {
    return (
      <div className="notice notice--warning" role="status">
        <b>G5 请逐框分类</b>
        <span>一次只保存一个区域，确保每个分类都有独立且连续的血缘事件。</span>
      </div>
    );
  }
  if (!backgroundClassificationRequired(region)) {
    return (
      <div className="notice" role="status">
        <b>此区域不需要独立背景分类</b>
        <span>
          {region.type === 'ruby'
            ? '注音区域沿用正文父框的背景判断。'
            : `G4 处置为“${region.contentDisposition ?? '未决定'}”，不属于 translate / redraw-art。`}
        </span>
      </div>
    );
  }

  const draftIdentity = [
    region.id,
    region.revision,
    region.backgroundCategory ?? '',
    region.backgroundConfidence ?? '',
    region.backgroundRationaleCodes?.join('|') ?? '',
  ].join(':');
  return <G5BackgroundForm key={draftIdentity} region={region} />;
}

function G5BackgroundForm({ region }: { region: Region }) {
  const saveG5Background = useWorkbenchStore((state) => state.saveG5Background);
  const locked = useWorkbenchStore((state) => g5EditingLocked(state));
  const savingRegionId = useWorkbenchStore((state) => state.g5SavingRegionId);
  const [category, setCategory] = useState<BackgroundCategory | ''>(
    region.backgroundCategory ?? '',
  );
  const [confidence, setConfidence] = useState(
    region.backgroundConfidence === null ? '' : String(region.backgroundConfidence),
  );
  const [rationaleCodes, setRationaleCodes] = useState<BackgroundRationaleCode[]>(
    region.backgroundRationaleCodes ?? [],
  );

  const parsedConfidence = Number(confidence);
  const anchor = category ? BACKGROUND_RATIONALE_ANCHOR[category] : null;
  const valid = Boolean(
    category
    && confidence.trim() !== ''
    && Number.isFinite(parsedConfidence)
    && parsedConfidence >= 0
    && parsedConfidence <= 1
    && anchor
    && rationaleCodes.includes(anchor),
  );
  const mixed = rationaleCodes.includes('mixed-visual-signals');

  return (
    <div className="form-stack text-inspector" aria-busy={savingRegionId === region.id}>
      <div className="region-heading">
        <div><span>G5 背景区域</span><strong>#{region.order}</strong></div>
        <span className={`review-state review-state--${region.backgroundCategory ? 'confirmed' : 'pending'}`}>
          {region.backgroundCategory ? '已保存' : '待分类'}
        </span>
      </div>
      <div className="notice" role="status">
        <b>只比较 immutable original 与已接受质量底板</b>
        <span>旧擦字图和旧排版图不会作为本门禁的主判断依据。</span>
      </div>
      <Field label="背景类别">
        <select
          aria-label="G5 背景类别"
          disabled={locked}
          onChange={(event) => {
            const next = event.target.value as BackgroundCategory;
            setCategory(next);
            setRationaleCodes([
              BACKGROUND_RATIONALE_ANCHOR[next],
              ...(mixed ? ['mixed-visual-signals' as const] : []),
            ]);
          }}
          value={category}
        >
          <option disabled value="">请选择…</option>
          {BACKGROUND_CATEGORIES.map((value) => (
            <option key={value} value={value}>{backgroundCategoryLabels[value]}</option>
          ))}
        </select>
      </Field>
      <Field label="置信度（0–1）" hint="仅记录判断把握，不会自动放行或阻断。">
        <input
          aria-label="G5 背景置信度"
          disabled={locked}
          max="1"
          min="0"
          onChange={(event) => setConfidence(event.target.value)}
          step="0.01"
          type="number"
          value={confidence}
        />
      </Field>
      <Field label="受控理由">
        <div className="form-stack">
          <span>{anchor ? backgroundRationaleLabels[anchor] : '选择类别后自动绑定主理由'}</span>
          <Toggle
            checked={mixed}
            description="存在第二类视觉信号时追加；主理由仍必须匹配所选类别。"
            disabled={locked || !category}
            label={backgroundRationaleLabels['mixed-visual-signals']}
            onChange={(event) => setRationaleCodes(() => {
              if (!anchor) return [];
              return event.target.checked ? [anchor, 'mixed-visual-signals'] : [anchor];
            })}
          />
        </div>
      </Field>
      <div className="notice" role="status">
        <b>服务端 Reviewer</b>
        <span>{backgroundReviewerLabel(region)}</span>
      </div>
      <button
        className="button button--accent"
        disabled={locked || savingRegionId === region.id || !valid || !category}
        onClick={() => {
          if (category && valid) {
            void saveG5Background(region.id, category, parsedConfidence, rationaleCodes);
          }
        }}
        type="button"
      >
        {savingRegionId === region.id ? '正在保存分类证据…' : '保存分类证据'}
      </button>
    </div>
  );
}

const ocrSourceModeLabels: Record<OCRSourceMode, string> = {
  'original-attempt': '采用原图 OCR',
  'quality-attempt': '采用增强图 OCR',
  'manual-correction': '手工修正',
};

const ocrQCCheckLabels: Record<OCRQCCheck, string> = {
  'original-and-quality-compared': '已对照原图与增强图 OCR',
  'source-text-characters-checked': '已逐字核对日文原文',
  'punctuation-checked': '已核对标点与符号',
  'direction-checked': '已核对横排 / 竖排方向',
  'reading-order-checked': '已核对阅读顺序',
  'empty-or-garbled-checked': '已排除空文本与乱码',
  'duplicate-fragment-checked': '已排除重复片段',
  'template-contamination-checked': '已排除模板污染',
  'page-text-consistency-checked': '已核对本页文本一致性',
};

const ocrQCFlagLabels: Record<string, string> = {
  none: '未发现额外风险',
  'original-quality-disagree': '原图与增强图结果不一致',
  'low-japanese-character-ratio': '日文字符占比较低',
  'ocr-empty-attempt': 'OCR 尝试包含空文本',
  'ocr-garbled-attempt': 'OCR 尝试疑似乱码',
  'duplicate-fragment': '疑似重复片段',
  'template-contamination': '疑似模板污染',
  'manual-correction': '使用了手工修正',
};

function ocrReviewerLabel(region: Region): string {
  const reviewer = region.ocrReviewer;
  if (!reviewer) return '尚未由服务端记录';
  return reviewer.actorId
    || reviewer.taskId
    || reviewer.threadId
    || reviewer.sessionId
    || reviewer.actorKind;
}

function activeOCRAttempts(attempts: OCRAttempt[], region: Region): OCRAttempt[] {
  const matching = attempts.filter((attempt) => attempt.regionId === region.id);
  const selected = region.ocrReview
    ? matching.find((attempt) => attempt.id === region.ocrReview?.selectedAttemptId)
    : undefined;
  const jobItemId = selected?.jobItemId ?? matching.at(-1)?.jobItemId;
  return matching.filter((attempt) => attempt.jobItemId === jobItemId);
}

function OCRAttemptEvidence({ attempt, label }: { attempt?: OCRAttempt; label: string }) {
  return (
    <article className="ocr-attempt">
      <strong>{label}</strong>
      {attempt ? (
        <>
          <p>{attempt.text || '（空文本）'}</p>
          <small>置信度：{attempt.confidence === null ? '—' : String(attempt.confidence)}</small>
          <small>Provider：{attempt.provider} · 模型：{attempt.modelVersion ?? '未记录'}</small>
          <small title={attempt.cropChecksum}>裁剪校验和：{attempt.cropChecksum}</small>
        </>
      ) : <small>尚无不可变 OCR 尝试</small>}
    </article>
  );
}

function G6OCRControl({ regions }: { regions: Region[] }) {
  const image = useWorkbenchStore(activeImage);
  const context = useWorkbenchStore((state) =>
    state.activeImageId ? state.g4Contexts[state.activeImageId] : undefined,
  );
  const ocr = useWorkbenchStore((state) =>
    state.activeImageId ? state.ocrContexts?.[state.activeImageId] : undefined,
  );
  const loading = useWorkbenchStore((state) =>
    state.activeImageId ? Boolean(state.ocrLoading?.[state.activeImageId]) : false,
  );
  const saving = useWorkbenchStore((state) => Boolean(
    state.g6SavingRegionId
    || (state.activeImageId && state.g6GateSavingImageId === state.activeImageId),
  ));
  const ocrRunning = useWorkbenchStore((state) => state.jobs.some((job) =>
    job.kind === 'ocr'
    && (job.status === 'queued' || job.status === 'running')
    && job.items.some((item) => item.imageId === state.activeImageId
      && (item.status === 'queued' || item.status === 'running'))
  ));
  const startG6OCR = useWorkbenchStore((state) => state.startG6OCR);
  const acceptG6OCR = useWorkbenchStore((state) => state.acceptG6OCR);
  const reloadActiveImage = useWorkbenchStore((state) => state.reloadActiveImage);

  if (!image || !context || !context.generation || workflowPhase(context) !== 'G6') return null;
  if (!ocr && typeof startG6OCR !== 'function') {
    return <section className="page-review page-review--done" aria-label="G6 OCR 门禁"><div><span>G5 已接受</span><strong>等待 G6 OCR 门禁</strong></div></section>;
  }
  if (context.error) {
    return (
      <div className="notice notice--error" role="alert">
        <b>{context.conflict ? 'G6 版本冲突' : 'G6 血缘不可用'}</b>
        <span>{context.error}</span>
        <div className="notice__actions">
          <button className="button button--compact" onClick={() => void reloadActiveImage()} type="button">重载本页</button>
        </div>
      </div>
    );
  }
  if (loading || !ocr) {
    return (
      <div className="notice notice--warning" aria-busy="true" role="status">
        <b>正在读取 G6 OCR 门禁</b>
        <span>双路 OCR 证据和权威校验和就绪前，本页保持锁定。</span>
      </div>
    );
  }
  if (
    ocr.generationId !== context.generation.id
    || ocr.nextSequence !== context.generation.nextSequence
    || ocr.imageRevision !== image.revision
  ) {
    return (
      <div className="notice notice--warning" aria-busy="true" role="status">
        <b>正在核对新的 G6 权威上下文</b>
        <span>页代次、序号或图像版本变化期间，OCR 与接受保持锁定。</span>
      </div>
    );
  }

  const eligibleRegions = regions.filter(ocrSourceReviewRequired);
  const eligibleIds = eligibleRegions.map((region) => region.id).sort();
  const authoritativeIds = [...ocr.eligibleRegionIds].sort();
  const attemptedIds = [...ocr.attemptedRegionIds].sort();
  const reviewedIds = [...ocr.reviewedRegionIds].sort();
  const sameEligible = eligibleIds.join('\0') === authoritativeIds.join('\0');
  const allAttempted = sameEligible
    && eligibleIds.join('\0') === attemptedIds.join('\0')
    && eligibleRegions.every((region) => {
      const variants = new Set(activeOCRAttempts(ocr.attempts, region).map((attempt) => attempt.inputVariant));
      return variants.has('original') && variants.has('quality');
    });
  const allReviewed = sameEligible
    && eligibleIds.join('\0') === reviewedIds.join('\0')
    && eligibleRegions.every((region) => Boolean(
      context.generation && ocrSourceReviewComplete(region, context.generation.id),
    ));
  const ready = ocr.state === 'pending' && allAttempted && allReviewed;

  return (
    <section className="page-review page-review--pending" aria-label="G6 OCR 门禁">
      <div>
        <span>G6 OCR</span>
        <strong>{ocr.reviewedRegionIds.length} / {ocr.eligibleRegionIds.length} 个需本地化区域已复核</strong>
        <small>原图与增强图都会运行；置信度（包括 0）只展示，不自动放行或阻断。</small>
      </div>
      {ocr.eligibleRegionIds.length > 0 && !allAttempted ? (
        <button className="button button--compact button--accent" disabled={saving || ocrRunning || !sameEligible} onClick={() => void startG6OCR()} type="button">{ocrRunning ? 'G6 OCR 正在运行…' : '运行 G6 双路 OCR'}</button>
      ) : (
        <button className="button button--compact button--accent" disabled={saving || ocrRunning || !ready} onClick={() => void acceptG6OCR()} type="button">
          {saving ? '正在保存…' : ocr.eligibleRegionIds.length ? '接受全部原文复核' : '确认本页 G6 不适用'}
        </button>
      )}
    </section>
  );
}

function G6OCRInspector({ regions, selected }: { regions: Region[]; selected: Region[] }) {
  const context = useWorkbenchStore((state) =>
    state.activeImageId ? state.ocrContexts?.[state.activeImageId] : undefined,
  );
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const focusRegions = useWorkbenchStore((state) => state.focusRegions);
  const region = selected.length === 1 ? selected[0] : undefined;

  if (!context) return <EmptyState icon="✓" title="G5 已接受" description="当前版本尚未开放 G6 OCR 写入口，本页保持只读。" />;
  if (!selected.length) {
    return (
      <div className="region-index" aria-label="G6 OCR 原文复核列表">
        <p className="panel-hint">选择 translate / redraw-art 区域，对照原图与已接受质量底板的两次 OCR。</p>
        {[...regions].sort((left, right) => left.order - right.order).map((entry) => {
          const eligible = ocrSourceReviewRequired(entry);
          const attempts = activeOCRAttempts(context.attempts, entry);
          return (
            <button aria-label={`选择 G6 文本框 #${entry.order}`} key={entry.id} onClick={() => { selectRegion(entry.id); focusRegions([entry.id]); }} type="button">
              <b>#{entry.order}</b>
              <span>{entry.sourceText || '（尚无已信任原文）'}</span>
              <em>{eligible ? entry.ocrReview ? '已复核' : attempts.length === 2 ? '待复核' : '待 OCR' : '不适用'}</em>
            </button>
          );
        })}
      </div>
    );
  }
  if (selected.length !== 1 || !region) {
    return <div className="notice notice--warning" role="status"><b>G6 请逐框复核</b><span>一次只保存一个区域，确保原文选择具有独立血缘。</span></div>;
  }
  if (!ocrSourceReviewRequired(region)) {
    return (
      <div className="notice" role="status">
        <b>此区域不进入 G6 OCR</b>
        <span>{region.type === 'ruby' ? '注音区域不独立 OCR。' : '只有 G4 处置为 translate / redraw-art 的非注音区域适用。'}</span>
      </div>
    );
  }
  const attempts = activeOCRAttempts(context.attempts, region);
  const identity = `${region.id}:${region.revision}:${attempts.map((attempt) => attempt.id).join(':')}`;
  return <G6OCRForm attempts={attempts} key={identity} region={region} />;
}

function G6OCRForm({ attempts, region }: { attempts: OCRAttempt[]; region: Region }) {
  const saveG6SourceReview = useWorkbenchStore((state) => state.saveG6SourceReview);
  const savingRegionId = useWorkbenchStore((state) => state.g6SavingRegionId);
  const original = attempts.find((attempt) => attempt.inputVariant === 'original');
  const quality = attempts.find((attempt) => attempt.inputVariant === 'quality');
  const initialSelected = attempts.find((attempt) => attempt.id === region.ocrReview?.selectedAttemptId) ?? quality ?? original;
  const [sourceMode, setSourceMode] = useState<OCRSourceMode>(region.ocrReview?.sourceMode ?? (quality ? 'quality-attempt' : 'original-attempt'));
  const [selectedAttemptId, setSelectedAttemptId] = useState(initialSelected?.id ?? '');
  const [sourceText, setSourceText] = useState(region.ocrReview ? region.sourceText : initialSelected?.text ?? '');
  const [qcChecks, setQCChecks] = useState<OCRQCCheck[]>(region.ocrReview?.qcChecks ?? []);
  const locked = useWorkbenchStore((state) => g6EditingLocked(state));
  const complete = Boolean(original && quality);
  const valid = complete && Boolean(selectedAttemptId && sourceText.trim()) && qcChecks.length === OCR_QC_CHECKS.length && OCR_QC_CHECKS.every((check) => qcChecks.includes(check));

  function selectMode(next: OCRSourceMode) {
    setSourceMode(next);
    const attempt = next === 'original-attempt' ? original : next === 'quality-attempt' ? quality : initialSelected;
    if (attempt) {
      setSelectedAttemptId(attempt.id);
      if (next !== 'manual-correction') setSourceText(attempt.text);
    }
  }

  return (
    <div className="form-stack text-inspector" aria-busy={savingRegionId === region.id}>
      <div className="region-heading"><div><span>G6 OCR 区域</span><strong>#{region.order}</strong></div><span className={`review-state review-state--${region.ocrReview ? 'confirmed' : 'pending'}`}>{region.ocrReview ? '已复核' : '待复核'}</span></div>
      <div className="ocr-attempt-grid" aria-label="G6 双路 OCR 尝试"><OCRAttemptEvidence attempt={original} label="原图 OCR" /><OCRAttemptEvidence attempt={quality} label="增强图 OCR" /></div>
      {!complete ? <div className="notice notice--warning" role="status"><b>双路证据尚未齐全</b><span>先运行整页 G6 OCR；不能用单路结果建立原文信任。</span></div> : null}
      <Field label="原文来源模式">
        <select aria-label="G6 原文来源模式" disabled={locked || !complete} onChange={(event) => selectMode(event.target.value as OCRSourceMode)} value={sourceMode}>
          {(Object.keys(ocrSourceModeLabels) as OCRSourceMode[]).map((mode) => <option key={mode} value={mode}>{ocrSourceModeLabels[mode]}</option>)}
        </select>
      </Field>
      {sourceMode === 'manual-correction' ? (
        <Field label="手工修正依据"><select aria-label="G6 手工修正依据" disabled={locked || !complete} onChange={(event) => setSelectedAttemptId(event.target.value)} value={selectedAttemptId}>{attempts.map((attempt) => <option key={attempt.id} value={attempt.id}>{attempt.inputVariant === 'original' ? '原图 OCR' : '增强图 OCR'}</option>)}</select></Field>
      ) : null}
      <Field label="已核准日文原文" hint="选择 OCR 尝试时必须与该结果完全一致；只有“手工修正”可编辑。"><textarea aria-label="G6 已核准日文原文" disabled={locked || !complete || sourceMode !== 'manual-correction'} onChange={(event) => setSourceText(event.target.value)} rows={5} value={sourceText} /></Field>
      <div className="field" role="group" aria-label="必做 QC（9 / 9）">
        <span className="field__label">必做 QC（9 / 9）</span>
        <div className="ocr-qc-list">{OCR_QC_CHECKS.map((check) => <Toggle checked={qcChecks.includes(check)} disabled={locked || !complete} key={check} label={ocrQCCheckLabels[check]} onChange={(event) => setQCChecks((current) => event.target.checked ? [...current, check] : current.filter((entry) => entry !== check))} />)}</div>
      </div>
      <div className="notice" role="status"><b>服务端 QC flags / Reviewer</b><span>{region.ocrReview?.qcFlags.map((flag) => ocrQCFlagLabels[flag] ?? flag).join('；') || '保存后由服务端计算'}</span><span>{ocrReviewerLabel(region)}</span></div>
      <button className="button button--accent" disabled={locked || !valid} onClick={() => void saveG6SourceReview(region.id, sourceText.trim(), sourceMode, selectedAttemptId, qcChecks)} type="button">{savingRegionId === region.id ? '正在保存原文证据…' : '保存原文复核证据'}</button>
    </div>
  );
}

const maskCoverageLabels: Record<MaskCoverageCheck, string> = {
  'body-glyphs-covered': '正文笔画全部覆盖',
  'punctuation-covered': '标点全部覆盖',
  'strokes-and-shadows-covered': '描边与阴影全部覆盖',
  'ruby-covered': '所属注音全部覆盖',
  'antialias-edges-covered': '抗锯齿边缘全部覆盖',
};
const maskCollateralLabels: Record<MaskCollateralCheck, string> = {
  'bubble-borders-protected': '气泡边线未受损',
  'characters-protected': '人物未受损',
  'speed-lines-protected': '速度线未受损',
  'screentone-protected': '网点未受损',
  'nearby-art-protected': '邻近画面未受损',
};
const cleanPlateCheckLabels: Record<CleanPlateCheck, string> = {
  'outside-mask-unchanged': 'mask 外像素完全未变化',
  'source-text-unreadable': '原日文已不可读',
  'no-white-or-gray-hole': '无白洞、灰块或硬色块',
  'no-blur-band': '无模糊带',
  'no-repeated-texture': '无重复纹理',
  'background-continuous': '背景、渐变与网点连续',
  'structure-preserved': '线条、人物与结构未受损',
};

function G7MaskControl() {
  const image = useWorkbenchStore(activeImage);
  const context = useWorkbenchStore((state) => state.activeImageId ? state.g4Contexts[state.activeImageId] : undefined);
  const mask = useWorkbenchStore((state) => state.activeImageId ? state.maskContexts[state.activeImageId] : undefined);
  const loading = useWorkbenchStore((state) => Boolean(state.activeImageId && state.maskLoading[state.activeImageId]));
  const busy = useWorkbenchStore((state) => Boolean(state.g7DraftSavingImageId || state.g7GateSavingImageId));
  const running = useWorkbenchStore((state) => state.jobs.some((job) => job.kind === 'mask'
    && (job.status === 'queued' || job.status === 'running')
    && job.items.some((item) => item.imageId === state.activeImageId && (item.status === 'queued' || item.status === 'running'))));
  const start = useWorkbenchStore((state) => state.startG7Mask);
  const saveDraft = useWorkbenchStore((state) => state.saveG7MaskDraft);
  const review = useWorkbenchStore((state) => state.reviewG7Mask);
  if (!image || !context?.generation || workflowPhase(context) !== 'G7') return null;
  if (loading || !mask) return <div className="notice notice--warning" aria-busy="true" role="status"><b>正在读取 G7 蒙版门禁</b><span>配方、不可变实际 PNG 和复核证据就绪前保持锁定。</span></div>;
  const current = mask.generationId === context.generation.id
    && mask.nextSequence === context.generation.nextSequence
    && mask.imageRevision === image.revision;
  const rejectedSequence = [...context.events].reverse().find((event) => event.operation === 'mask-stage-review' && event.state === 'rejected')?.sequence;
  const revisedAfterReject = rejectedSequence === undefined || context.events.some((event) => event.operation === 'mask-draft-updated' && event.sequence > rejectedSequence);
  if (!current) return <div className="notice notice--warning" role="status"><b>G7 权威版本已变化</b><span>正在等待页代次、序号和图像版本重新对齐。</span></div>;
  return (
    <section className="page-review page-review--pending" aria-label="G7 蒙版门禁">
      <div><span>G7 完整蒙版</span><strong>{mask.artifacts.length} 个不可变实际蒙版</strong><small>配方变更会使先前产物失效；只有客户端实际读取并校验的 PNG 可复核。</small></div>
      {mask.eligibleRegionIds.length === 0
        ? <button className="button button--compact button--accent" disabled={busy || running} onClick={() => void review('not-applicable', [], [])} type="button">确认 G7 不适用</button>
        : mask.draft.revision === 0
          ? <button className="button button--compact button--accent" disabled={busy || running || mask.draft.regions.length !== mask.eligibleRegionIds.length} onClick={() => void saveDraft(mask.draft.regions)} type="button">确认并保存初始配方</button>
          : <button className="button button--compact button--accent" disabled={busy || running || !revisedAfterReject || mask.draft.regions.length !== mask.eligibleRegionIds.length} onClick={() => void start()} type="button">{running ? '蒙版正在生成…' : mask.state === 'rejected' ? revisedAfterReject ? '按修正配方重试' : '先修正配方再重试' : '生成不可变实际蒙版'}</button>}
    </section>
  );
}

function G7MaskInspector({ regions, selected }: { regions: Region[]; selected: Region[] }) {
  const image = useWorkbenchStore(activeImage);
  const imageId = useWorkbenchStore((state) => state.activeImageId);
  const lineage = useWorkbenchStore((state) => imageId ? state.g4Contexts[imageId] : undefined);
  const mask = useWorkbenchStore((state) => imageId ? state.maskContexts[imageId] : undefined);
  const selectedArtifactId = useWorkbenchStore((state) => imageId ? state.selectedMaskArtifactIds[imageId] : undefined);
  const observation = useWorkbenchStore((state) => imageId ? state.maskBitmapObservations[imageId] : undefined);
  const saveDraft = useWorkbenchStore((state) => state.saveG7MaskDraft);
  const selectArtifact = useWorkbenchStore((state) => state.selectG7MaskArtifact);
  const review = useWorkbenchStore((state) => state.reviewG7Mask);
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const focusRegions = useWorkbenchStore((state) => state.focusRegions);
  const locked = useWorkbenchStore((state) => g7EditingLocked(state));
  const checkIdentity = `${mask?.generationId ?? ''}:${mask?.imageRevision ?? ''}:${mask?.draft.revision ?? ''}:${mask?.draft.stateChecksum ?? ''}:${selectedArtifactId ?? ''}`;
  const [reviewDraft, setReviewDraft] = useState<{
    identity: string;
    coverage: Array<MaskCheckResult<MaskCoverageCheck>>;
    collateral: Array<MaskCheckResult<MaskCollateralCheck>>;
  }>({ identity: '', coverage: [], collateral: [] });
  const coverage = reviewDraft.identity === checkIdentity ? reviewDraft.coverage
    : MASK_COVERAGE_CHECKS.map((check) => ({ check, passed: false }));
  const collateral = reviewDraft.identity === checkIdentity ? reviewDraft.collateral
    : MASK_COLLATERAL_CHECKS.map((check) => ({ check, passed: false }));
  if (!mask) return <EmptyState icon="锁" title="正在读取 G7" description="权威蒙版上下文未就绪。" />;
  const eligible = regions.filter(maskRegionRequired);
  const region = selected.length === 1 ? selected[0] : undefined;
  const recipe = region ? mask.draft.regions.find((entry) => entry.regionId === region.id) : undefined;
  const artifact = mask.artifacts.find((entry) => entry.artifactId === selectedArtifactId);
  const observed = Boolean(artifact && observation
    && observation.artifactId === artifact.artifactId
    && observation.checksum === artifact.maskChecksum
    && observation.width === artifact.width && observation.height === artifact.height
    && observation.imageRevision === image?.revision);
  const rejectedEvent = lineage?.status === 'active' ? [...lineage.events].reverse().find((event) =>
    event.operation === 'mask-stage-review' && event.state === 'rejected') : undefined;
  const revisedEvent = rejectedEvent && lineage?.status === 'active' ? [...lineage.events].reverse().find((event) =>
    event.operation === 'mask-draft-updated' && event.sequence > rejectedEvent.sequence) : undefined;
  const producedEvent = artifact && lineage?.status === 'active' ? lineage.events.find((event) =>
    event.operation === 'mask-artifact-produced' && event.evidence.artifactId === artifact.artifactId) : undefined;
  const acceptanceIsFresh = mask.state !== 'rejected' || Boolean(
    artifact && producedEvent && revisedEvent
    && artifact.artifactId !== mask.review?.artifactId
    && producedEvent.sequence > revisedEvent.sequence,
  );
  const allPass = coverage.every((entry) => entry.passed) && collateral.every((entry) => entry.passed);
  const anyFail = coverage.some((entry) => !entry.passed) || collateral.some((entry) => !entry.passed);
  const updateRecipe = (patch: Partial<MaskDraftRegion>) => {
    if (!recipe) return;
    void saveDraft(mask.draft.regions.map((entry) => entry.regionId === recipe.regionId ? { ...entry, ...patch } : entry));
  };
  return (
    <div className="form-stack g7-mask-inspector">
      <div className="notice" role="status"><b>四视图核对</b><span>画布同时展示 mask-off 原图/质量底板与 mask-on 实际位图；“框住所选”和工具栏缩放会同时定位四幅视图。</span></div>
      <Field label="不可变实际蒙版">
        <select aria-label="G7 不可变实际蒙版" disabled={locked || !mask.artifacts.length} onChange={(event) => selectArtifact(event.target.value)} value={selectedArtifactId ?? ''}>
          {!mask.artifacts.length ? <option value="">尚未生成</option> : null}
          {mask.artifacts.map((entry) => <option key={entry.artifactId} value={entry.artifactId}>#{entry.sequence} · {entry.maskChecksum.slice(0, 12)}</option>)}
        </select>
      </Field>
      {artifact ? <div className={`notice ${observed ? '' : 'notice--warning'}`} role="status"><b>{observed ? '实际 PNG 已校验' : '正在读取实际 PNG'}</b><span>{artifact.width}×{artifact.height} · 非零像素 {artifact.nonzeroPixelCount} · checksum {artifact.maskChecksum.slice(0, 16)}</span></div> : null}
      {!selected.length ? <div className="region-index" aria-label="G7 eligible 主区域列表">{eligible.map((entry) => {
        const ruby = mask.rubyRegionIdsByPrimary[entry.id] ?? [];
        return <button key={entry.id} onClick={() => { selectRegion(entry.id); focusRegions([entry.id, ...ruby]); }} type="button"><b>#{entry.order}</b><span>{entry.sourceText || '需清除视觉文字'}</span><em>{ruby.length ? `关联 ${ruby.length} 个注音` : '无关联注音'}</em></button>;
      })}</div> : null}
      {region && !maskRegionRequired(region) ? <div className="notice notice--warning" role="status"><b>不可独立编辑</b><span>{region.type === 'ruby' ? '注音由所属 primary 自动纳入并高亮。' : '该区域不属于 G7 eligible primary。'}</span></div> : null}
      {recipe ? <div className="form-stack"><div className="region-heading"><div><span>G7 主区域配方</span><strong>#{region?.order}</strong></div><span>{(mask.rubyRegionIdsByPrimary[recipe.regionId] ?? []).length} 个关联注音</span></div>
        <Field label="蒙版模式"><select disabled={locked} onChange={(event) => updateRecipe({ maskMode: event.target.value as MaskDraftRegion['maskMode'] })} value={recipe.maskMode}><option value="text">文字像素</option><option value="region">全区域</option><option value="manual">手工笔迹</option></select></Field>
        <Field label="文字极性"><select disabled={locked} onChange={(event) => updateRecipe({ polarity: event.target.value as MaskDraftRegion['polarity'] })} value={recipe.polarity}><option value="auto">自动</option><option value="dark">深色文字</option><option value="light">浅色文字</option></select></Field>
        <GeometryNumberField ariaLabel="G7 padding" commitOnChange={false} disabled={locked} label="Padding" min={0} onCommit={(value) => updateRecipe({ padding: Math.min(512, Math.max(0, Math.round(value))) })} value={recipe.padding} />
        <GeometryNumberField ariaLabel="G7 dilation" commitOnChange={false} disabled={locked} label="Dilation" min={0} onCommit={(value) => updateRecipe({ dilation: Math.min(128, Math.max(0, Math.round(value))) })} value={recipe.dilation} />
        <GeometryNumberField ariaLabel="G7 feather" commitOnChange={false} disabled={locked} label="Feather" min={0} onCommit={(value) => updateRecipe({ feather: Math.min(128, Math.max(0, Math.round(value))) })} value={recipe.feather} />
        <small>更改画笔/橡皮并保存会生成新配方 checksum，使旧产物不可用于接受。</small>
      </div> : null}
      {artifact ? <><div className="field" role="group" aria-label="G7 覆盖检查">{coverage.map((entry) => <Toggle checked={entry.passed} disabled={locked || !observed} key={entry.check} label={maskCoverageLabels[entry.check]} onChange={(event) => setReviewDraft({ identity: checkIdentity, coverage: coverage.map((item) => item.check === entry.check ? { ...item, passed: event.target.checked } : item), collateral })} />)}</div>
        <div className="field" role="group" aria-label="G7 误伤检查">{collateral.map((entry) => <Toggle checked={entry.passed} disabled={locked || !observed} key={entry.check} label={maskCollateralLabels[entry.check]} onChange={(event) => setReviewDraft({ identity: checkIdentity, coverage, collateral: collateral.map((item) => item.check === entry.check ? { ...item, passed: event.target.checked } : item) })} />)}</div>
        <div className="notice__actions"><button className="button button--accent" disabled={locked || !observed || !allPass || !acceptanceIsFresh} onClick={() => void review('accept', coverage, collateral)} type="button">接受当前实际蒙版</button><button className="button" disabled={locked || !observed || !anyFail} onClick={() => void review('reject', coverage, collateral)} type="button">拒绝并修正</button></div></> : null}
    </div>
  );
}

function G8CleanPlateControl() {
  const image = useWorkbenchStore(activeImage);
  const context = useWorkbenchStore((state) => state.activeImageId
    ? state.g4Contexts[state.activeImageId] : undefined);
  const cleanPlate = useWorkbenchStore((state) => state.activeImageId
    ? state.cleanPlateContexts[state.activeImageId] : undefined);
  const loading = useWorkbenchStore((state) => Boolean(
    state.activeImageId && state.cleanPlateLoading[state.activeImageId]));
  const busy = useWorkbenchStore((state) => state.g8GateSavingImageId === state.activeImageId);
  const running = useWorkbenchStore((state) => state.jobs.some((job) => job.kind === 'inpaint'
    && (job.status === 'queued' || job.status === 'running')
    && job.items.some((item) => item.imageId === state.activeImageId
      && (item.status === 'queued' || item.status === 'running'))));
  const start = useWorkbenchStore((state) => state.startG8CleanPlate);
  const review = useWorkbenchStore((state) => state.reviewG8CleanPlate);
  if (!image || !context?.generation || workflowPhase(context) !== 'G8') return null;
  if (loading || !cleanPlate) return <div className="notice notice--warning" aria-busy="true" role="status"><b>正在读取 G8 净版门禁</b><span>route、不可变候选与实际 PNG 校验就绪前保持锁定。</span></div>;
  const current = cleanPlate.generationId === context.generation.id
    && cleanPlate.nextSequence === context.generation.nextSequence
    && cleanPlate.imageRevision === image.revision;
  if (!current) return <div className="notice notice--warning" role="status"><b>G8 权威版本已变化</b><span>正在等待页代次、序号和图像版本重新对齐。</span></div>;
  if (cleanPlate.state === 'accepted' || cleanPlate.state === 'not-applicable') {
    return <section className="page-review page-review--done" aria-label="G8 净版门禁"><div><span>G8 已终结</span><strong>{cleanPlate.state === 'accepted' ? 'clean plate 已接受' : '本页无需净版'}</strong><small>后续只能消费这条不可变接受证据。</small></div></section>;
  }
  return (
    <section className="page-review page-review--pending" aria-label="G8 净版门禁">
      <div><span>G8 clean plate</span><strong>{cleanPlate.candidates.length} 个不可变候选</strong><small>按 G5 背景类别路由；mask 外像素变化必须为 0。</small></div>
      {cleanPlate.routes.length === 0
        ? <button className="button button--compact button--accent" disabled={busy || running} onClick={() => void review('not-applicable', [])} type="button">确认 G8 不适用</button>
        : cleanPlate.fallbackEnabled
          ? <button className="button button--compact" disabled type="button">传统回退已开启，请使用下方专用操作</button>
          : <button className="button button--compact button--accent" disabled={busy || running} onClick={() => void start(false)} type="button">{running ? '候选正在生成…' : cleanPlate.candidates.length ? '生成新的 AI / 确定性候选' : '生成 clean plate 候选'}</button>}
    </section>
  );
}

function G8CleanPlateInspector() {
  const image = useWorkbenchStore(activeImage);
  const imageId = useWorkbenchStore((state) => state.activeImageId);
  const lineage = useWorkbenchStore((state) => imageId
    ? state.g4Contexts[imageId] : undefined);
  const cleanPlate = useWorkbenchStore((state) => imageId
    ? state.cleanPlateContexts[imageId] : undefined);
  const selectedCandidateId = useWorkbenchStore((state) => imageId
    ? state.selectedCleanPlateCandidateIds[imageId] : undefined);
  const observation = useWorkbenchStore((state) => imageId
    ? state.cleanPlateBitmapObservations[imageId] : undefined);
  const selectCandidate = useWorkbenchStore((state) => state.selectG8CleanPlateCandidate);
  const review = useWorkbenchStore((state) => state.reviewG8CleanPlate);
  const setFallback = useWorkbenchStore((state) => state.setG8ClassicalFallback);
  const start = useWorkbenchStore((state) => state.startG8CleanPlate);
  const locked = useWorkbenchStore((state) => g8EditingLocked(state))
    || cleanPlate?.state === 'accepted'
    || cleanPlate?.state === 'not-applicable';
  const candidate = cleanPlate?.candidates.find((entry) =>
    entry.candidateId === selectedCandidateId);
  const identity = [
    cleanPlate?.generationId ?? '',
    cleanPlate?.nextSequence ?? '',
    cleanPlate?.imageRevision ?? '',
    cleanPlate?.cleanPlateStateChecksum ?? '',
    cleanPlate?.maskArtifactId ?? '',
    cleanPlate?.maskChecksum ?? '',
    selectedCandidateId ?? '',
    candidate?.candidateChecksum ?? '',
    candidate?.routeChecksum ?? '',
  ].join(':');
  const [draft, setDraft] = useState<{
    identity: string;
    checks: Array<MaskCheckResult<CleanPlateCheck>>;
  }>({ identity: '', checks: [] });
  const checks = draft.identity === identity ? draft.checks
    : CLEAN_PLATE_CHECKS.map((check) => ({ check, passed: false }));
  if (!cleanPlate) {
    return <EmptyState icon="锁" title="正在读取 G8" description="权威 clean plate 上下文未就绪。" />;
  }
  const observed = Boolean(candidate && observation
    && observation.state === 'ready'
    && observation.generationId === cleanPlate.generationId
    && observation.nextSequence === cleanPlate.nextSequence
    && observation.cleanPlateStateChecksum === cleanPlate.cleanPlateStateChecksum
    && observation.candidateId === candidate.candidateId
    && observation.sourceChecksum === lineage?.generation?.sourceChecksum
    && observation.qualityChecksum === cleanPlate.qualityChecksum
    && observation.maskArtifactId === cleanPlate.maskArtifactId
    && observation.maskChecksum === cleanPlate.maskChecksum
    && observation.maskWidth === candidate.width
    && observation.maskHeight === candidate.height
    && observation.checksum === candidate.candidateChecksum
    && observation.width === candidate.width && observation.height === candidate.height
    && observation.imageRevision === image?.revision);
  const allPass = checks.every((entry) => entry.passed);
  const anyFail = checks.some((entry) => !entry.passed);
  const complex = cleanPlate.routes.some((route) =>
    route.backgroundCategory === 'complex-lineart'
    || route.backgroundCategory === 'illustration/character');
  return (
    <div className="form-stack g7-mask-inspector">
      <div className="notice" role="status"><b>四视图核对</b><span>画布同步展示原图、质量底板、质量底板 + accepted mask 与所选不可变候选；四幅实际字节全部校验后才可复核。</span></div>
      <Field label="不可变 clean plate 候选">
        <select aria-label="G8 不可变 clean plate 候选" disabled={locked || !cleanPlate.candidates.length} onChange={(event) => selectCandidate(event.target.value)} value={selectedCandidateId ?? ''}>
          {!cleanPlate.candidates.length ? <option value="">尚未生成</option> : null}
          {cleanPlate.candidates.map((entry) => <option key={entry.candidateId} value={entry.candidateId}>#{entry.sequence} · {entry.originKind} · {entry.review?.state ?? (entry.completed ? '待复核' : '生成中')}</option>)}
        </select>
      </Field>
      {candidate ? <>
        <div className={`notice ${observed ? '' : 'notice--warning'}`} role="status"><b>{observed ? '四视图与候选实际 PNG 已校验' : '正在读取四视图实际字节'}</b><span>{candidate.width}×{candidate.height} · checksum {candidate.candidateChecksum.slice(0, 16)} · mask 外变化 {candidate.outsideMaskChangeCount}</span></div>
        <div className="notice" role="status"><b>Provenance：{candidate.originKind}</b><span>{candidate.providerIds.join(' + ')} · {candidate.modelVersions.join(' + ')}</span><span>parent {candidate.parentChecksum.slice(0, 12)} · route {candidate.routeChecksum.slice(0, 12)} · parameter {candidate.parameterHash.slice(0, 12)}</span><span>accepted mask {candidate.maskArtifactId.slice(0, 12)} · {candidate.maskChecksum.slice(0, 12)}</span></div>
        <div className="region-index" aria-label="G8 route manifest">{candidate.routeManifest.map((route) => <div key={route.regionId}><b>{backgroundCategoryLabels[route.backgroundCategory]}</b><span>{route.route}</span><em>{route.originKind} · {route.provider}</em></div>)}</div>
        {candidate.anomalies.length ? <div className="notice notice--warning" role="status"><b>候选异常标记</b><span>{candidate.anomalies.join('；')}</span></div> : null}
        {candidate.review ? <div className={`notice ${candidate.review.state === 'accepted' ? '' : 'notice--warning'}`} role="status"><b>不可变结论：{candidate.review.state}</b><span>{candidate.review.reason}</span></div> : (
          <>
            <div className="field" role="group" aria-label="G8 净版检查（7 / 7）">{checks.map((entry) => <Toggle checked={entry.passed} disabled={locked || !observed || !candidate.completed} key={entry.check} label={cleanPlateCheckLabels[entry.check]} onChange={(event) => setDraft({ identity, checks: checks.map((item) => item.check === entry.check ? { ...item, passed: event.target.checked } : item) })} />)}</div>
            <div className="notice__actions"><button className="button button--accent" disabled={locked || !observed || !candidate.completed || !allPass} onClick={() => void review('accept', checks)} type="button">接受当前 clean plate</button><button className="button" disabled={locked || !observed || !candidate.completed || !anyFail} onClick={() => void review('reject', checks)} type="button">拒绝当前候选</button></div>
          </>
        )}
      </> : <div className="notice notice--warning" role="status"><b>尚无候选</b><span>{cleanPlate.routes.length ? '先生成候选；传统回退默认关闭。' : '零 eligible 页面请在上方确认 G8 不适用。'}</span></div>}
      {complex ? <div className="form-stack"><div className="notice" role="status"><b>页级传统回退：{cleanPlate.fallbackEnabled ? '已开启' : '关闭'}</b><span>只有同代次全部适用 AI 候选逐一拒绝后才允许开启；provenance 始终记录为 classical。</span></div><div className="notice__actions">{cleanPlate.fallbackEnabled ? <><button className="button button--accent" disabled={locked} onClick={() => void start(true)} type="button">生成 classical 候选</button><button className="button" disabled={locked} onClick={() => void setFallback(false)} type="button">关闭回退并恢复 AI</button></> : <button className="button" disabled={locked || !cleanPlate.fallbackAllowed} onClick={() => void setFallback(true)} type="button">开启本页 classical fallback</button>}</div></div> : null}
    </div>
  );
}

const translationCheckLabels: Record<TranslationQCCheck, string> = {
  'target-chinese-checked': '目标中文已核对',
  'forbidden-template-checked': '禁用模板话术已核对',
  'nonempty-checked': '非空输出已核对',
  'source-copy-checked': '未机械复制原文',
  'japanese-residual-checked': '无日文残留',
  'generic-duplicate-checked': '页内通用重复已核对',
  'source-consistency-checked': '与可信原文一致',
  'context-consistency-checked': '与相邻语境一致',
  'tone-and-type-checked': '语气与文字类型一致',
  'source-noise-checked': '未把 OCR 噪声润色成译文',
};
const translationFlagLabels: Record<Exclude<TranslationQCFlag, 'none'>, string> = {
  'empty-output': '空输出',
  'non-chinese-output': '非中文输出',
  'forbidden-template': '模板污染',
  'source-copy': '复制原文',
  'japanese-residual': '日文残留',
  'generic-duplicate': '通用重复',
  'source-inconsistent': '源文不一致',
  'context-inconsistent': '上下文不一致',
  'source-noise-hallucination': '将源噪声幻译为流畅文本',
};

function G9TranslationControl() {
  const imageId = useWorkbenchStore((state) => state.activeImageId);
  const project = useWorkbenchStore((state) => state.currentProject);
  const context = useWorkbenchStore((state) => imageId ? state.translationContexts[imageId] : undefined);
  const loading = useWorkbenchStore((state) => Boolean(imageId && state.translationLoading[imageId]));
  const busy = useWorkbenchStore((state) => state.g9GateSavingImageId === imageId);
  const running = useWorkbenchStore((state) => state.jobs.some((job) => job.kind === 'translate'
    && (job.status === 'queued' || job.status === 'running')
    && job.items.some((item) => item.imageId === imageId && (item.status === 'queued' || item.status === 'running'))));
  const start = useWorkbenchStore((state) => state.startG9Translation);
  const accept = useWorkbenchStore((state) => state.acceptG9Translation);
  const [remoteAuthorized, setRemoteAuthorized] = useState(false);
  if (loading || !context) return <div className="notice notice--warning" aria-busy="true" role="status"><b>正在读取 G9 翻译门禁</b><span>候选 revision、QC 与 accepted clean plate 绑定就绪前保持锁定。</span></div>;
  if (context.state !== 'pending') return <section className="page-review page-review--done" aria-label="G9 翻译门禁"><div><span>G9 已终结</span><strong>{context.state === 'accepted' ? '译文已显式接受' : '本页无翻译 eligible region'}</strong><small>候选与复核历史只读保留。</small></div></section>;
  const latest = new Map<string, typeof context.candidates[number]>();
  for (const candidate of context.candidates) latest.set(candidate.regionId, candidate);
  const ready = context.eligibleRegions.length > 0
    && context.eligibleRegions.every((region) => latest.get(region.regionId)?.review?.state === 'accepted')
    && context.candidates.every((candidate) => candidate.review !== null);
  return <section className="page-review page-review--pending" aria-label="G9 翻译门禁">
    <div><span>G9 translation</span><strong>{context.reviewedRegionCount} / {context.eligibleRegions.length} region 最新候选已接受</strong><small>{context.targetLanguage} · ruby 已由服务端结构性排除 · clean plate {context.cleanPlateChecksum.slice(0, 12)}</small></div>
    {context.eligibleRegions.length === 0
      ? <button className="button button--compact button--accent" disabled={busy} onClick={() => void accept()} type="button">确认 G9 不适用</button>
      : project?.settings.translatorProvider === 'manual' || project?.settings.translatorProvider === 'dictionary'
        ? <><div className="notice notice--warning" role="status"><b>当前 provider 仅支持 revision</b><span>manual / dictionary 不创建自动整页任务；请在下方逐个 eligible region 创建首个 revision。</span></div><button className="button button--compact button--accent" disabled={busy || !ready} onClick={() => void accept()} type="button">接受 G9 并进入排版</button></>
        : <><label className="stage-review-check"><input checked={remoteAuthorized} onChange={(event) => setRemoteAuthorized(event.target.checked)} type="checkbox" />本次明确授权远端翻译（仅远端 provider 需要）</label><div className="notice__actions"><button className="button button--compact" disabled={busy || running} onClick={() => void start(remoteAuthorized)} type="button">{running ? '整页翻译运行中…' : '生成整页翻译候选'}</button><button className="button button--compact button--accent" disabled={busy || !ready} onClick={() => void accept()} type="button">接受 G9 并进入排版</button></div></>}
  </section>;
}

function G9TranslationInspector() {
  const imageId = useWorkbenchStore((state) => state.activeImageId);
  const context = useWorkbenchStore((state) => imageId ? state.translationContexts[imageId] : undefined);
  const selectedId = useWorkbenchStore((state) => imageId ? state.selectedTranslationCandidateIds[imageId] : undefined);
  const select = useWorkbenchStore((state) => state.selectG9TranslationCandidate);
  const revise = useWorkbenchStore((state) => state.reviseG9Translation);
  const review = useWorkbenchStore((state) => state.reviewG9TranslationCandidate);
  const busy = useWorkbenchStore((state) => state.g9GateSavingImageId === imageId);
  const candidate = context?.candidates.find((entry) => entry.candidateId === selectedId);
  const eligible = context?.eligibleRegions.find((entry) => entry.regionId === candidate?.regionId);
  const identity = `${context?.translationStateChecksum ?? ''}:${candidate?.candidateId ?? ''}:${candidate?.candidateChecksum ?? ''}`;
  const [draft, setDraft] = useState<{ identity: string; text: string; origin: Exclude<TranslationOriginKind, 'model'>; checks: Array<MaskCheckResult<TranslationQCCheck>>; flags: Array<Exclude<TranslationQCFlag, 'none'>> }>({ identity: '', text: '', origin: 'manual', checks: [], flags: [] });
  const [firstDraft, setFirstDraft] = useState<{ regionId: string; text: string; origin: Exclude<TranslationOriginKind, 'model'> }>({ regionId: '', text: '', origin: 'manual' });
  const text = draft.identity === identity ? draft.text : candidate?.translationText ?? '';
  const checks = draft.identity === identity ? draft.checks : TRANSLATION_QC_CHECKS.map((check) => ({ check, passed: false }));
  const flags = draft.identity === identity ? draft.flags : [];
  const patchDraft = (patch: Partial<typeof draft>) => setDraft({ identity, text, origin: draft.identity === identity ? draft.origin : 'manual', checks, flags, ...patch });
  if (!context) return <EmptyState icon="锁" title="正在读取 G9" description="权威 translation context 未就绪。" />;
  const locked = busy || context.state !== 'pending';
  const latestByRegion = new Map<string, typeof context.candidates[number]>();
  for (const entry of context.candidates) latestByRegion.set(entry.regionId, entry);
  const missingRegions = context.eligibleRegions.filter((region) => !latestByRegion.has(region.regionId));
  const firstRegionId = missingRegions.some((region) => region.regionId === firstDraft.regionId)
    ? firstDraft.regionId : missingRegions[0]?.regionId ?? '';
  const firstText = firstDraft.regionId === firstRegionId ? firstDraft.text : '';
  const rejectReason: TranslationReviewReason = flags.length > 1 ? 'multiple-qc-failures' : flags[0] ?? 'multiple-qc-failures';
  return <div className="form-stack g9-translation-inspector">
    <div className="notice" role="status"><b>Accepted clean plate 是唯一图像父项</b><span>{context.cleanPlateCandidateId ?? 'G8 not-applicable'} · {context.cleanPlateChecksum.slice(0, 16)}</span><span>target {context.targetLanguage} · state {context.translationStateChecksum.slice(0, 16)}</span></div>
    <div className="region-index" aria-label="G9 eligible 最新候选状态">{context.eligibleRegions.map((region) => {
      const latest = latestByRegion.get(region.regionId);
      const passed = latest?.review?.checks.filter((entry) => entry.passed).length ?? 0;
      return <div key={region.regionId}><b>order {region.readingOrder} · {region.regionType}</b><span>{latest ? `r${latest.revisionNumber} · ${latest.review?.state ?? '待复核'}` : '缺候选'}</span><em>{latest ? `${passed}/10 checks · ${latest.computedQcFlags.join(', ')}` : 'ruby excluded · 等待整页候选'}</em></div>;
    })}</div>
    {missingRegions.length ? <div className="form-stack" aria-label="G9 创建首个 revision">
      <div className="notice notice--warning"><b>eligible region 尚无候选</b><span>选择一个非 ruby region，通过专用 revision API 创建 parent=null 的首候选；不会写旧 region.translationText。</span></div>
      <Field label="缺候选 region"><select aria-label="G9 首候选 region" disabled={locked} onChange={(event) => setFirstDraft({ ...firstDraft, regionId: event.target.value, text: '' })} value={firstRegionId}>{missingRegions.map((region) => <option key={region.regionId} value={region.regionId}>order {region.readingOrder} · {region.regionType} · {region.sourceText}</option>)}</select></Field>
      <Field label="首候选译文"><textarea aria-label="G9 首候选译文" disabled={locked} onChange={(event) => setFirstDraft({ ...firstDraft, regionId: firstRegionId, text: event.target.value })} rows={4} value={firstText} /></Field>
      <Field label="首候选来源"><select aria-label="G9 首候选来源" disabled={locked} onChange={(event) => setFirstDraft({ ...firstDraft, regionId: firstRegionId, origin: event.target.value as Exclude<TranslationOriginKind, 'model'> })} value={firstDraft.origin}><option value="manual">human manual</option><option value="agent">agent（需 Codex/Cursor actor）</option><option value="dictionary">dictionary</option></select></Field>
      <button className="button button--accent" disabled={locked || !firstRegionId || !firstText.trim()} onClick={() => void revise(firstRegionId, firstText, firstDraft.origin)} type="button">创建首个 revision</button>
    </div> : null}
    <Field label="不可变译文候选"><select aria-label="G9 不可变译文候选" disabled={locked || !context.candidates.length} onChange={(event) => select(event.target.value)} value={selectedId ?? ''}>{!context.candidates.length ? <option value="">尚未生成</option> : null}{context.candidates.map((entry) => <option key={entry.candidateId} value={entry.candidateId}>#{entry.sequence} · region {entry.regionId.slice(0, 8)} · r{entry.revisionNumber} · {entry.review?.state ?? '待复核'}</option>)}</select></Field>
    {candidate && eligible ? <>
      <div className="notice"><b>可信完整段落 · order {eligible.readingOrder}</b><span>{eligible.sourceText}</span><span>{eligible.regionType} · {eligible.direction} · paragraph {eligible.paragraphGroupId ?? '—'} · ruby 不在 eligible 集合</span><span>context regions: {eligible.contextRegionIds.join(', ') || '无'}</span></div>
      <Field label="译文 revision"><textarea aria-label="G9 译文修订" disabled={locked || candidate.review?.state === 'accepted'} onChange={(event) => patchDraft({ text: event.target.value })} rows={4} value={text} /></Field>
      <Field label="修订来源"><select disabled={locked || candidate.review?.state === 'accepted'} onChange={(event) => patchDraft({ origin: event.target.value as Exclude<TranslationOriginKind, 'model'> })} value={draft.identity === identity ? draft.origin : 'manual'}><option value="manual">human manual</option><option value="agent">agent（需 Codex/Cursor actor）</option><option value="dictionary">dictionary</option></select></Field>
      <div className="notice"><b>Provenance：{candidate.originKind}</b><span>{candidate.provider} · {candidate.modelVersion} · target {candidate.targetLanguage}</span><span>parent {candidate.supersedesCandidateId ?? 'initial'} · parameter {candidate.parameterHash.slice(0, 12)}</span><span>server QC: {candidate.computedQcFlags.join(', ')}</span></div>
      {candidate.review ? <div className={`notice ${candidate.review.state === 'accepted' ? '' : 'notice--warning'}`}><b>不可变结论：{candidate.review.state}</b><span>{candidate.review.reason} · {candidate.review.qcFlags.join(', ')}</span></div> : <>
        <button className="button" disabled={locked || !text.trim() || text.trim() === candidate.translationText} onClick={() => void revise(candidate.regionId, text, draft.identity === identity ? draft.origin : 'manual')} type="button">创建新 revision（当前候选须先拒绝）</button>
        <div className="field" role="group" aria-label="G9 翻译 QC（10 / 10）">{checks.map((entry) => <Toggle checked={entry.passed} disabled={locked} key={entry.check} label={translationCheckLabels[entry.check]} onChange={(event) => patchDraft({ checks: checks.map((item) => item.check === entry.check ? { ...item, passed: event.target.checked } : item) })} />)}</div>
        <div className="field" role="group" aria-label="G9 QC flags">{(Object.keys(translationFlagLabels) as Array<Exclude<TranslationQCFlag, 'none'>>).map((flag) => <Toggle checked={flags.includes(flag)} disabled={locked} key={flag} label={translationFlagLabels[flag]} onChange={(event) => patchDraft({ flags: event.target.checked ? [...flags, flag] : flags.filter((entry) => entry !== flag) })} />)}</div>
        <div className="notice__actions"><button className="button button--accent" disabled={locked || !checks.every((entry) => entry.passed) || flags.length > 0 || candidate.computedQcFlags.length !== 1 || candidate.computedQcFlags[0] !== 'none'} onClick={() => void review(candidate.candidateId, 'accept', checks, ['none'], 'translation-reviewed')} type="button">接受当前译文</button><button className="button" disabled={locked || flags.length === 0 || (checks.every((entry) => entry.passed) && flags.length === 0)} onClick={() => void review(candidate.candidateId, 'reject', checks, flags, rejectReason)} type="button">拒绝当前译文</button></div>
      </>}
    </> : <div className="notice notice--warning"><b>尚无已生成候选</b><span>{missingRegions.length ? '请先用上方专用入口创建首个 revision。' : '请重载 G9 权威上下文。'}</span></div>}
  </div>;
}

const typesetRouteLabels = {
  bubble: '气泡文字',
  ordinary: '普通非气泡文字',
  'art-lettering': '艺术字 / SFX 绘图式路线',
  keep: '保留原艺术字',
  ignore: '明确忽略',
} as const;

const typesetCheckLabels: Record<TypesetCheck, string> = {
  'original-clean-final-compared': '已同时对照不可变原图、accepted clean plate 与最终候选',
  'translation-complete': '所有需渲染区域都有已接受译文',
  'hierarchy-reading-order-preserved': '层级与阅读顺序保持正确',
  'key-art-unobstructed': '人物与关键画面未被遮挡',
  'typography-source-matched': '字体、字号、粗细、填色、描边与原文视觉重量匹配',
  'bubble-contained': '气泡文字行数、对齐、方向与留白均正确，且完整位于气泡内',
  'art-lettering-composition-matched': '艺术字描边、倾斜、对齐、重心与构图关系匹配',
  'overflow-free': '无溢出或服务端布局异常',
};

function G10TypesetControl() {
  const imageId = useWorkbenchStore((state) => state.activeImageId);
  const context = useWorkbenchStore((state) => imageId ? state.typesetContexts[imageId] : undefined);
  const loading = useWorkbenchStore((state) => Boolean(imageId && state.typesetLoading[imageId]));
  const busy = useWorkbenchStore((state) => state.g10GateSavingImageId === imageId);
  const running = useWorkbenchStore((state) => state.jobs.some((job) => job.kind === 'typeset'
    && (job.status === 'queued' || job.status === 'running')
    && job.items.some((item) => item.imageId === imageId
      && (item.status === 'queued' || item.status === 'running'))));
  const start = useWorkbenchStore((state) => state.startG10Typeset);
  if (loading || !context) return <div className="notice notice--warning" aria-busy="true" role="status"><b>正在读取 G10 排版门禁</b><span>route/style/layout manifest 与不可变候选就绪前保持锁定。</span></div>;
  if (context.state === 'accepted') return <section className="page-review page-review--done" aria-label="G10 排版门禁"><div><span>G10 已终结</span><strong>最终候选已显式接受</strong><small>{context.terminalChecksum?.slice(0, 16)} · 所有样式和复核证据只读。</small></div></section>;
  const artRequired = context.routeManifest.some((entry) => entry.route === 'art-lettering');
  const blocked = artRequired && !context.artLetteringCapability.available;
  const incomplete = context.candidates.some((candidate) => !candidate.completed);
  const pendingReview = context.candidates.length !== context.reviews.length;
  return <section className="page-review page-review--pending" aria-label="G10 排版门禁">
    <div><span>G10 typeset</span><strong>{context.candidates.length} 个不可变整页候选 · {context.reviews.length} 个结论</strong><small>G9 {context.g9TerminalChecksum.slice(0, 12)} · clean {context.cleanPlateChecksum.slice(0, 12)}</small></div>
    {blocked ? <div className="notice notice--error" role="alert"><b>艺术字能力硬阻断</b><span>{context.artLetteringCapability.reason || '服务端未提供完整艺术字 capability。'}；禁止退化为普通系统字体。</span></div> : null}
    <button className="button button--compact button--accent" disabled={busy || running || incomplete || pendingReview || blocked} onClick={() => void start()} type="button">{running || incomplete ? '整页 G10 候选生成中…' : pendingReview ? '请先复核当前 G10 候选' : context.reviews.some((review) => review.state === 'rejected') ? '按拒绝候选样式重试' : '生成整页 G10 候选'}</button>
  </section>;
}

function G10StyleEditor({ regionId }: { regionId: string }) {
  const imageId = useWorkbenchStore((state) => state.activeImageId);
  const context = useWorkbenchStore((state) => imageId ? state.typesetContexts[imageId] : undefined);
  const style = useWorkbenchStore((state) => imageId ? state.typesetStyleDrafts[imageId]?.[regionId] : undefined);
  const setStyle = useWorkbenchStore((state) => state.setG10RegionStyle);
  const route = context?.routeManifest.find((entry) => entry.regionId === regionId);
  const locked = context?.state !== 'pending';
  if (!context || !route || !route.renderRequired || !style) return null;
  const patch = (next: Partial<TypesetRegionStyleInput>) => setStyle(regionId, { ...style, ...next });
  const number = (key: keyof TypesetRegionStyleInput, value: string) => {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) patch({ [key]: parsed });
  };
  const art = route.route === 'art-lettering';
  return <div className="form-stack" aria-label={`G10 ${route.route} 样式`}>
    <div className={`notice ${art ? 'notice--warning' : ''}`} role="status"><b>{typesetRouteLabels[route.route]}</b><span>{art ? '只使用服务端明确登记的中文展示字体；绝不回落 ordinary/default_cjk_font。' : '样式只进入 G10 job options，不修改锁定的 G4 region。'}</span></div>
    {art ? <div className="notice notice--warning"><b>曲线 / 局部 AI lettering 未宣告</b><span>当前能力只覆盖明确字体、描填、旋转、非均匀缩放、shear、透明度、视觉中心、对齐与行距；缺失能力时必须阻断，不会静默普通排版。</span></div> : null}
    <Field label={art ? '中文展示字体' : '服务端字体'}><select aria-label="G10 font token" disabled={locked} onChange={(event) => patch({ fontToken: event.target.value })} value={style.fontToken}>{(art ? context.availableDisplayFonts : context.availableFonts).map((font) => <option key={font.token} value={font.token}>{font.label} · {font.role}</option>)}</select></Field>
    <div className="form-grid form-grid--two">
      <GeometryNumberField ariaLabel="G10 font size" disabled={locked} label="字号" min={6} onCommit={(value) => patch({ fontSize: Math.min(512, Math.max(6, value)) })} value={style.fontSize} />
      <GeometryNumberField ariaLabel="G10 min font size" disabled={locked} label="最小字号" min={6} onCommit={(value) => patch({ minFontSize: Math.min(style.fontSize, Math.max(6, value)) })} value={style.minFontSize} />
      <GeometryNumberField ariaLabel="G10 padding" disabled={locked} label="Padding" min={0} onCommit={(value) => patch({ padding: Math.min(128, Math.max(0, value)) })} value={style.padding} />
      <GeometryNumberField ariaLabel="G10 stroke width" disabled={locked} label="描边宽度" min={0} onCommit={(value) => patch({ strokeWidth: Math.min(32, Math.max(0, value)) })} value={style.strokeWidth} />
    </div>
    <Field label="填充色"><input aria-label="G10 fill" disabled={locked} onChange={(event) => patch({ fill: event.target.value.toUpperCase() })} pattern="#[0-9A-Fa-f]{6}" value={style.fill} /></Field>
    <Field label="描边色"><input aria-label="G10 stroke color" disabled={locked} onChange={(event) => patch({ strokeColor: event.target.value.toUpperCase() })} pattern="#[0-9A-Fa-f]{6}" value={style.strokeColor} /></Field>
    <div className="form-grid form-grid--two">
      <Field label="旋转 -180…180"><input aria-label="G10 rotation" disabled={locked} max={180} min={-180} onChange={(event) => number('rotation', event.target.value)} step="0.1" type="number" value={style.rotation} /></Field>
      <Field label="透明度 .05…1"><input aria-label="G10 opacity" disabled={locked} max={1} min={0.05} onChange={(event) => number('opacity', event.target.value)} step="0.05" type="number" value={style.opacity} /></Field>
      {art ? <>
        <Field label="Scale X .25…4"><input aria-label="G10 scale X" disabled={locked} max={4} min={0.25} onChange={(event) => number('scaleX', event.target.value)} step="0.05" type="number" value={style.scaleX} /></Field>
        <Field label="Scale Y .25…4"><input aria-label="G10 scale Y" disabled={locked} max={4} min={0.25} onChange={(event) => number('scaleY', event.target.value)} step="0.05" type="number" value={style.scaleY} /></Field>
        <Field label="Shear X -1…1"><input aria-label="G10 shear X" disabled={locked} max={1} min={-1} onChange={(event) => number('shearX', event.target.value)} step="0.05" type="number" value={style.shearX} /></Field>
        <Field label="Shear Y -1…1"><input aria-label="G10 shear Y" disabled={locked} max={1} min={-1} onChange={(event) => number('shearY', event.target.value)} step="0.05" type="number" value={style.shearY} /></Field>
        <Field label="视觉中心 X 0…1"><input aria-label="G10 visual center X" disabled={locked} max={1} min={0} onChange={(event) => number('visualCenterX', event.target.value)} step="0.05" type="number" value={style.visualCenterX} /></Field>
        <Field label="视觉中心 Y 0…1"><input aria-label="G10 visual center Y" disabled={locked} max={1} min={0} onChange={(event) => number('visualCenterY', event.target.value)} step="0.05" type="number" value={style.visualCenterY} /></Field>
      </> : null}
      <Field label="行距 0…3"><input aria-label="G10 line spacing" disabled={locked} max={3} min={0} onChange={(event) => number('lineSpacing', event.target.value)} step="0.05" type="number" value={style.lineSpacing} /></Field>
      {!art ? <Field label="字距 -10…50"><input aria-label="G10 letter spacing" disabled={locked} max={50} min={-10} onChange={(event) => number('letterSpacing', event.target.value)} step="0.1" type="number" value={style.letterSpacing} /></Field> : null}
    </div>
    <Field label="对齐"><select aria-label="G10 align" disabled={locked} onChange={(event) => patch({ align: event.target.value as TypesetRegionStyleInput['align'] })} value={style.align}><option value="start">start</option><option value="center">center</option><option value="end">end</option></select></Field>
    <Toggle checked={style.autoFit} disabled={locked} label="允许在 minFontSize 范围内 auto-fit" onChange={(event) => patch({ autoFit: event.target.checked })} />
  </div>;
}

function G10TypesetInspector() {
  const imageId = useWorkbenchStore((state) => state.activeImageId);
  const image = useWorkbenchStore(activeImage);
  const lineage = useWorkbenchStore((state) => imageId ? state.g4Contexts[imageId] : undefined);
  const context = useWorkbenchStore((state) => imageId ? state.typesetContexts[imageId] : undefined);
  const selectedCandidateId = useWorkbenchStore((state) => imageId ? state.selectedTypesetCandidateIds[imageId] : undefined);
  const observation = useWorkbenchStore((state) => imageId ? state.typesetBitmapObservations[imageId] : undefined);
  const selectedRegionIds = useWorkbenchStore((state) => state.selectedRegionIds);
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const focusRegions = useWorkbenchStore((state) => state.focusRegions);
  const selectCandidate = useWorkbenchStore((state) => state.selectG10TypesetCandidate);
  const reviewCandidate = useWorkbenchStore((state) => state.reviewG10TypesetCandidate);
  const busy = useWorkbenchStore((state) => state.g10GateSavingImageId === imageId);
  const [draft, setDraft] = useState<{
    identity: string;
    checks: Array<MaskCheckResult<TypesetCheck>>;
    touched: TypesetCheck[];
  }>({ identity: '', checks: [], touched: [] });
  if (!context) return <EmptyState icon="锁" title="正在读取 G10" description="权威 typeset context 未就绪。" />;
  const selectedRoute = context.routeManifest.find((entry) => entry.regionId === selectedRegionIds[0])
    ?? context.routeManifest[0];
  const candidate = context.candidates.find((entry) => entry.candidateId === selectedCandidateId);
  const candidateReview = context.reviews.find((entry) => entry.candidateId === candidate?.candidateId);
  const identity = `${context.nextSequence}:${candidate?.candidateId ?? ''}:${candidate?.candidateChecksum ?? ''}`;
  const serverLayoutFailure = Boolean(candidate?.overflowRegionIds.length || candidate?.anomalies.length);
  const draftChecks = draft.identity === identity ? draft.checks
    : TYPESET_CHECKS.map((check) => ({ check, passed: false }));
  const checks = serverLayoutFailure
    ? draftChecks.map((entry) => entry.check === 'overflow-free'
      ? { ...entry, passed: false } : entry)
    : draftChecks;
  const touched = draft.identity === identity ? draft.touched : [];
  const patchCheck = (check: TypesetCheck, passed: boolean) => setDraft({
    identity,
    checks: checks.map((entry) => entry.check === check ? { ...entry, passed } : entry),
    touched: touched.includes(check) ? touched : [...touched, check],
  });
  const observed = Boolean(candidate && observation
    && observation.state === 'ready'
    && observation.imageId === imageId
    && observation.generationId === context.generationId
    && observation.nextSequence === context.nextSequence
    && observation.imageRevision === image?.revision
    && observation.sourceChecksum === lineage?.generation?.sourceChecksum
    && observation.candidateId === candidate.candidateId
    && observation.candidateChecksum === candidate.candidateChecksum
    && observation.routeChecksum === candidate.routeChecksum
    && observation.styleChecksum === candidate.styleChecksum
    && observation.layoutChecksum === candidate.layoutChecksum
    && observation.cleanPlateChecksum === candidate.cleanPlateChecksum
    && observation.width === candidate.width && observation.height === candidate.height
    && observation.renderScale === candidate.renderScale);
  const allPass = touched.length === TYPESET_CHECKS.length && checks.every((entry) => entry.passed);
  const allTouched = touched.length === TYPESET_CHECKS.length;
  const failed = checks.filter((entry) => !entry.passed).map((entry) => entry.check);
  const rejectReason: TypesetReviewReason = failed.length > 1
    ? 'multiple-visual-failures' : failed[0] ?? 'multiple-visual-failures';
  const locked = busy || context.state === 'accepted' || !candidate?.completed || Boolean(candidateReview);
  return <div className="form-stack g10-typeset-inspector">
    <div className="notice" role="status"><b>G10 immutable parents</b><span>G9 terminal {context.g9TerminalChecksum.slice(0, 16)}</span><span>accepted clean plate {context.cleanPlateCandidateId ?? 'not-applicable'} · {context.cleanPlateChecksum.slice(0, 16)}</span></div>
    <div className="region-index" aria-label="G10 route manifest">{context.routeManifest.map((entry) => <button aria-label={`选择 G10 route ${entry.regionId}`} key={entry.regionId} onClick={() => { selectRegion(entry.regionId); focusRegions([entry.regionId]); }} type="button"><b>order {entry.readingOrder} · {typesetRouteLabels[entry.route]}</b><span>{entry.renderRequired ? '需要渲染' : '不渲染'}</span><em>{entry.translationCandidateId ? `translation ${entry.translationCandidateId.slice(0, 8)}` : '无译文消费'}</em></button>)}</div>
    {selectedRoute ? <G10StyleEditor regionId={selectedRoute.regionId} /> : null}
    <Field label="不可变 G10 候选"><select aria-label="G10 不可变候选" disabled={!context.candidates.length || context.state === 'accepted'} onChange={(event) => selectCandidate(event.target.value)} value={selectedCandidateId ?? ''}>{!context.candidates.length ? <option value="">尚未生成</option> : null}{context.candidates.map((entry) => { const review = context.reviews.find((item) => item.candidateId === entry.candidateId); return <option key={entry.candidateId} value={entry.candidateId}>#{entry.sequence} · {review?.state ?? (entry.completed ? '待复核' : '生成中')} · {entry.candidateChecksum.slice(0, 10)}</option>; })}</select></Field>
    {candidate ? <>
      <div className={`notice ${observed ? '' : 'notice--warning'}`} role="status"><b>{observed ? '三视图 checksum / 像素网格已全部校验' : '等待三视图精确观察'}</b><span>不可变原图 + accepted clean plate + selected final candidate 缺一不可复核。</span><span>{candidate.width}×{candidate.height} @ {candidate.renderScale} · route {candidate.routeChecksum.slice(0, 12)} · style {candidate.styleChecksum.slice(0, 12)} · layout {candidate.layoutChecksum.slice(0, 12)}</span></div>
      {candidate.overflowRegionIds.length || candidate.anomalies.length ? <div className="notice notice--error" role="alert"><b>服务端 raster 硬失败</b><span>overflow: {candidate.overflowRegionIds.join(', ') || '无'} · anomalies: {candidate.anomalies.join(', ') || '无'}</span></div> : null}
      <div className="region-index" aria-label="G10 candidate route manifest">{candidate.routeManifest.map((entry) => <div key={entry.regionId}><b>{typesetRouteLabels[entry.route]}</b><span>{entry.regionId}</span><em>{entry.renderRequired ? 'renderRequired' : 'preserved / ignored'}</em></div>)}</div>
      {candidateReview ? <div className={`notice ${candidateReview.state === 'accepted' ? '' : 'notice--warning'}`}><b>不可变结论：{candidateReview.state}</b><span>{candidateReview.reason} · terminal {candidateReview.terminalChecksum.slice(0, 16)}</span></div> : <>
        <div className="field" role="group" aria-label="G10 视觉检查（8 / 8）">{checks.map((entry) => {
          const hardFailure = serverLayoutFailure && entry.check === 'overflow-free';
          const explicitlyAcknowledged = touched.includes(entry.check);
          return <Toggle
            checked={hardFailure ? explicitlyAcknowledged : entry.passed}
            description={hardFailure ? '此操作只确认已检查；结果仍固定为失败，候选不能接受。' : undefined}
            disabled={locked || !observed || (hardFailure && explicitlyAcknowledged)}
            key={entry.check}
            label={hardFailure ? '确认服务端硬失败：存在溢出或布局异常' : typesetCheckLabels[entry.check]}
            onChange={(event) => patchCheck(entry.check, hardFailure ? false : event.target.checked)}
          />;
        })}</div>
        <div className="notice__actions"><button className="button button--accent" disabled={locked || !observed || !allPass || serverLayoutFailure} onClick={() => void reviewCandidate(candidate.candidateId, 'accept', checks, 'typeset-reviewed', touched)} type="button">接受最终候选</button><button className="button" disabled={locked || !observed || !allTouched || failed.length === 0} onClick={() => void reviewCandidate(candidate.candidateId, 'reject', checks, rejectReason, touched)} type="button">拒绝并按样式重试</button></div>
      </>}
    </> : <div className="notice notice--warning"><b>尚无 G10 候选</b><span>先确认每条 route 的服务端样式，再从上方创建严格整页任务。</span></div>}
  </div>;
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

const inpaintOriginLabels: Record<InpaintCandidate['originKind'], string> = {
  'direct-ai': '来源：AI 直接修复',
  'ai-derived': '来源：AI 派生修复',
  classical: '来源：传统算法（Classical）',
  'deterministic-postprocess': '来源：确定性后处理',
  mixed: '来源：混合处理',
};

function inpaintCandidateSetIdentity(image: ImageAsset): string {
  const candidates = (image.inpaintCandidates ?? [])
    .map((candidate) => `${candidate.id}:${candidate.originKind}`)
    .sort()
    .join('|');
  return `${image.id}:${image.inpaintCandidateGenerationId ?? 'missing-generation'}:${candidates}`;
}

function InpaintCandidatePicker({ image }: { image: ImageAsset }) {
  const selectInpaintCandidate = useWorkbenchStore((state) => state.selectInpaintCandidate);
  const reviewSelectedAiCandidate = useWorkbenchStore(
    (state) => state.reviewSelectedInpaintAiCandidate,
  );
  const setInpaintFallback = useWorkbenchStore((state) => state.setActiveImageInpaintFallback);
  const busy = useWorkbenchStore((state) => state.stageReviewSaving);
  const strictAiRequired = useWorkbenchStore(
    (state) => state.currentProject?.settings.requireAIInpaintBeforeDownstream ?? false,
  );
  const candidates = image.inpaintCandidates ?? [];
  const currentIdentity = inpaintCandidateSetIdentity(image);
  const [evaluation, setEvaluation] = useState<{
    identity: string;
    reason: '' | 'ai-visible-artifacts';
  }>(() => ({ identity: currentIdentity, reason: '' }));
  const reason = evaluation.identity === currentIdentity ? evaluation.reason : '';
  if (image.status.inpaint !== 'done' || candidates.length < 2) return null;
  const selectedCandidate = candidates.find((candidate) => candidate.id === image.inpaintCandidate);
  const aiCandidates = candidates.filter(
    (candidate) => candidate.originKind === 'direct-ai' || candidate.originKind === 'ai-derived',
  );
  const hasClassicalCandidate = candidates.some((candidate) => candidate.originKind === 'classical');
  const rejectedAiCandidateIds = image.inpaintAiRejectedCandidateIds;
  const allAiCandidatesRejected = aiCandidates.length > 0
    && aiCandidates.every((candidate) => rejectedAiCandidateIds.includes(candidate.id));
  const fallbackApproved = image.inpaintFallback?.state === 'approved';
  const canApproveFallback = Boolean(
    strictAiRequired
      && selectedCandidate?.originKind === 'classical'
      && image.stageReviews.inpaint?.state === 'accepted'
      && reason === 'ai-visible-artifacts'
      && allAiCandidatesRejected
      && !busy,
  );

  return (
    <div className="form-stack">
      <Field
        label="修复候选"
        hint="逐个选择并查看后决定。接受/拒绝仍绑定该结果的校验和；来源标签来自持久化证据。"
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
                <span className="inpaint-candidate__origin">
                  {inpaintOriginLabels[candidate.originKind]}
                </span>
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
      {selectedCandidate
        && (selectedCandidate.originKind === 'direct-ai' || selectedCandidate.originKind === 'ai-derived') ? (
          <button
            className="button"
            disabled={Boolean(busy)}
            onClick={() => void reviewSelectedAiCandidate(
              rejectedAiCandidateIds.includes(selectedCandidate.id) ? 'pending' : 'rejected',
            )}
            type="button"
          >
            {rejectedAiCandidateIds.includes(selectedCandidate.id)
              ? '撤销此 AI 候选不可接受'
              : '标记此 AI 候选不可接受'}
          </button>
        ) : null}
      {strictAiRequired && hasClassicalCandidate ? (
        <div className="inpaint-fallback" aria-label="传统算法逐页兜底" role="group">
          <span
            aria-label="本页修复授权"
            className={`stage-review-state stage-review-state--${fallbackApproved ? 'accepted' : 'pending'}`}
            role="status"
          >
            {fallbackApproved ? '已批准：传统算法兜底' : '未批准'}
          </span>
          {fallbackApproved && image.inpaintFallback?.reason === 'ai-visible-artifacts' ? (
            <p>原因：AI 候选存在明显伪影</p>
          ) : null}
          <p>仅对当前候选、蒙版和文件有效；任一内容或候选代次变化都会自动失效。</p>
          {fallbackApproved ? (
            <button
              className="button"
              disabled={Boolean(busy)}
              onClick={async () => {
                if (await setInpaintFallback('pending')) {
                  setEvaluation({
                    identity: currentIdentity,
                    reason: '',
                  });
                }
              }}
              type="button"
            >
              撤销本页传统算法兜底
            </button>
          ) : (
            <>
              <label>
                <span>兜底原因</span>
                <select
                  aria-label="传统算法兜底原因"
                  disabled={Boolean(busy)}
                  onChange={(event) => setEvaluation({
                    identity: currentIdentity,
                    reason: event.target.value as typeof reason,
                  })}
                  value={reason}
                >
                  <option value="">请选择</option>
                  <option value="ai-visible-artifacts">AI 候选存在明显伪影</option>
                </select>
              </label>
              <button
                className="button button--accent"
                disabled={!canApproveFallback}
                onClick={async () => {
                  if (await setInpaintFallback('approved', {
                    reason: 'ai-visible-artifacts',
                  })) {
                    setEvaluation({
                      identity: currentIdentity,
                      reason: '',
                    });
                  }
                }}
                type="button"
              >
                批准本页传统算法兜底
              </button>
            </>
          )}
        </div>
      ) : null}
    </div>
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
  const updateRepair = (patch: Partial<Region['repair']>) => updateRegion(region.id, { repair: patch });
  const inheritedProvider = project?.settings.inpainterProvider ?? 'opencv';
  const provider = repair.inpainterProvider || inheritedProvider;
  const isLama = provider === 'lama' || provider === 'lama-onnx';
  const inpainters = providers.filter((capability) => capability.kind === 'inpainter');
  const providerCapability = providers.find(
    (capability) => capability.kind === 'inpainter' && capability.id === provider,
  );
  const providerUnavailable = providerCapability?.available === false;
  const manualAddStrokeCount = repair.maskEdits?.strokes.filter(
    (stroke) => stroke.mode === 'add',
  ).length ?? 0;
  return (
    <div className="form-stack">
      {candidatePicker}
      <div className="notice notice--local"><b>本地处理 · {provider}</b><span>图像、蒙版和修复结果只在本机处理；可在画布用蒙版画笔与橡皮擦精修选中区域。</span></div>
      <div className="notice notice--local"><b>安全修复策略</b><span>只处理已确认、可信自动识别或手工识别区域；完成后任务抽屉会显示实际修复与跳过数量。</span></div>
      <button
        aria-label="清除当前区域蒙版笔迹"
        className="button button--compact"
        disabled={!repair.maskEdits?.strokes.length}
        onClick={() => {
          if (!window.confirm('清除当前区域的全部蒙版画笔和橡皮擦笔迹？')) return;
          updateRepair({ maskEdits: { version: 1, strokes: [] } });
        }}
        type="button"
      >
        清除蒙版笔迹{repair.maskEdits?.strokes.length ? `（${repair.maskEdits.strokes.length}）` : ''}
      </button>
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
      <Field label="蒙版策略" hint="文本轮廓会自动细化字形；完整区域覆盖整个文本框；仅手工模式从空蒙版开始，只使用持久画笔笔迹。">
        <select aria-label="蒙版策略" onChange={(event) => updateRepair({ maskMode: event.target.value as Region['repair']['maskMode'] })} value={repair.maskMode}>
          <option value="text">文本轮廓（推荐）</option>
          <option value="region">完整区域</option>
          <option value="manual">仅手工蒙版（空白起步）</option>
        </select>
      </Field>
      {repair.maskMode === 'manual' ? (
        <div className={`notice ${manualAddStrokeCount ? 'notice--local' : 'notice--warning'}`}>
          <b>{manualAddStrokeCount ? `仅手工蒙版 · ${manualAddStrokeCount} 条添加笔迹` : '仅手工蒙版尚未添加范围'}</b>
          <span>
            自动字形、文本框、蒙版外扩、膨胀和羽化均不产生修改范围；请用画笔明确扣出文字，橡皮擦仍为最终权威。
          </span>
        </div>
      ) : null}
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
          <select aria-label="修复方法" onChange={(event) => updateRepair({ method: event.target.value as Region['repair']['method'] })} value={repair.method}>
            <option value="telea">OpenCV Telea</option>
            <option value="navier_stokes">OpenCV Navier–Stokes</option>
            <option value="solid">纯色填充</option>
            <option value="screentone">规则网点 / 底纹修复</option>
          </select>
        </Field>
      )}
      <div className="field-grid">
        <Field label="蒙版外扩 px"><input disabled={repair.maskMode === 'manual'} max={512} min={0} onChange={(event) => updateRepair({ maskPadding: Math.min(512, Math.max(0, Math.round(Number(event.target.value)))) })} type="number" value={repair.maskPadding} /></Field>
        <Field label="膨胀 px"><input disabled={repair.maskMode === 'manual'} max={128} min={0} onChange={(event) => updateRepair({ dilation: Math.min(128, Math.max(0, Math.round(Number(event.target.value)))) })} type="number" value={repair.dilation} /></Field>
        <Field label="羽化 px"><input disabled={repair.maskMode === 'manual'} max={128} min={0} onChange={(event) => updateRepair({ feather: Math.min(128, Math.max(0, Math.round(Number(event.target.value)))) })} type="number" value={repair.feather} /></Field>
        {!isLama ? <Field label="修复半径"><input max={256} min={1} onChange={(event) => updateRepair({ radius: Math.min(256, Math.max(1, Number(event.target.value))) })} type="number" value={repair.radius} /></Field> : null}
        {!isLama ? <Field label="填充色"><input aria-label="修复填充色" onInput={(event) => updateRepair({ fillColor: event.currentTarget.value })} type="color" value={repair.fillColor} /></Field> : null}
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
        <Toggle
          checked={settings.requireAIInpaintBeforeDownstream}
          label="翻译/嵌字前必须验收 AI 补图"
          onChange={(event) => update({ requireAIInpaintBeforeDownstream: event.target.checked })}
        />
        <p className="field-hint">
          开启后，非 AI 修复候选即使已接受也不会解锁翻译或嵌字；空蒙版页不受影响。
        </p>
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
  const g4Context = useWorkbenchStore((state) =>
    state.activeImageId ? state.g4Contexts[state.activeImageId] : undefined,
  );
  const backgroundContext = useWorkbenchStore((state) =>
    state.activeImageId ? state.backgroundContexts[state.activeImageId] : undefined,
  );
  const ocrContext = useWorkbenchStore((state) =>
    state.activeImageId ? state.ocrContexts?.[state.activeImageId] : undefined,
  );
  const maskContext = useWorkbenchStore((state) =>
    state.activeImageId ? state.maskContexts?.[state.activeImageId] : undefined,
  );
  const cleanPlateContext = useWorkbenchStore((state) =>
    state.activeImageId ? state.cleanPlateContexts?.[state.activeImageId] : undefined,
  );
  const translationContext = useWorkbenchStore((state) =>
    state.activeImageId ? state.translationContexts?.[state.activeImageId] : undefined,
  );
  const typesetContext = useWorkbenchStore((state) =>
    state.activeImageId ? state.typesetContexts?.[state.activeImageId] : undefined,
  );
  const loadBackgroundContext = useWorkbenchStore((state) => state.loadBackgroundContext);
  const loadOCRContext = useWorkbenchStore((state) => state.loadOCRContext);
  const loadMaskContext = useWorkbenchStore((state) => state.loadMaskContext);
  const loadCleanPlateContext = useWorkbenchStore((state) => state.loadCleanPlateContext);
  const loadTranslationContext = useWorkbenchStore((state) => state.loadTranslationContext);
  const loadTypesetContext = useWorkbenchStore((state) => state.loadTypesetContext);
  const reloadActiveImage = useWorkbenchStore((state) => state.reloadActiveImage);
  const regionsByImage = useWorkbenchStore((state) => state.regionsByImage);
  const selectedRegionIds = useWorkbenchStore((state) => state.selectedRegionIds);
  const regions = activeImageId ? regionsByImage[activeImageId] ?? EMPTY_REGIONS : EMPTY_REGIONS;
  const selected = useMemo(() => {
    const ids = new Set(selectedRegionIds);
    return regions.filter((region) => ids.has(region.id));
  }, [regions, selectedRegionIds]);
  const derivedPhase = workflowPhase(g4Context);
  const phase = (derivedPhase === 'G7' || derivedPhase === 'G8' || derivedPhase === 'G9' || derivedPhase === 'G10') && (g4Context?.conflict || g4Context?.error)
    ? 'locked'
    : derivedPhase;

  useEffect(() => {
    const backgroundIsCurrent = Boolean(
      backgroundContext
      && g4Context?.generation
      && backgroundContext.generationId === g4Context.generation.id
      && backgroundContext.nextSequence === g4Context.generation.nextSequence,
    );
    if (
      activeImageId
      && phase === 'G5'
      && !backgroundIsCurrent
      && !g4Context?.error
    ) {
      void loadBackgroundContext(activeImageId, Boolean(backgroundContext));
    }
  }, [
    activeImageId,
    backgroundContext,
    g4Context?.error,
    g4Context?.generation,
    loadBackgroundContext,
    phase,
  ]);

  const ocrIsCurrent = Boolean(ocrContext && g4Context?.generation
    && ocrContext.generationId === g4Context.generation.id
    && ocrContext.nextSequence === g4Context.generation.nextSequence);
  useEffect(() => {
    if (activeImageId && (phase === 'G6' || phase === 'G7' || phase === 'G8') && !ocrIsCurrent && !g4Context?.error && typeof loadOCRContext === 'function') {
      void loadOCRContext(activeImageId, Boolean(ocrContext));
    }
  }, [activeImageId, g4Context?.error, g4Context?.generation, loadOCRContext, ocrContext, ocrIsCurrent, phase]);

  const maskIsCurrent = Boolean(maskContext && g4Context?.generation
    && maskContext.generationId === g4Context.generation.id
    && maskContext.nextSequence === g4Context.generation.nextSequence);
  useEffect(() => {
    if (activeImageId && (phase === 'G7' || phase === 'G8') && ocrIsCurrent && !maskIsCurrent && !g4Context?.error) {
      void loadMaskContext(activeImageId, Boolean(maskContext));
    }
  }, [activeImageId, g4Context?.error, loadMaskContext, maskContext, maskIsCurrent, ocrIsCurrent, phase]);

  const cleanPlateIsCurrent = Boolean(cleanPlateContext && g4Context?.generation
    && cleanPlateContext.generationId === g4Context.generation.id
    && cleanPlateContext.nextSequence === g4Context.generation.nextSequence);
  useEffect(() => {
    if (activeImageId && phase === 'G8' && maskIsCurrent && !cleanPlateIsCurrent
      && !g4Context?.error) {
      void loadCleanPlateContext(activeImageId, Boolean(cleanPlateContext));
    }
  }, [
    activeImageId,
    cleanPlateContext,
    cleanPlateIsCurrent,
    g4Context?.error,
    loadCleanPlateContext,
    maskIsCurrent,
    phase,
  ]);

  const translationIsCurrent = Boolean(translationContext && g4Context?.generation
    && translationContext.generationId === g4Context.generation.id
    && translationContext.nextSequence === g4Context.generation.nextSequence);
  useEffect(() => {
    if (activeImageId && phase === 'G9' && !translationIsCurrent && !g4Context?.error) {
      void loadTranslationContext(activeImageId, Boolean(translationContext));
    }
  }, [activeImageId, g4Context?.error, loadTranslationContext, phase, translationContext, translationIsCurrent]);

  const typesetIsCurrent = Boolean(typesetContext && g4Context?.generation
    && typesetContext.generationId === g4Context.generation.id
    && typesetContext.nextSequence === g4Context.generation.nextSequence);
  useEffect(() => {
    if (activeImageId && phase === 'G10' && !typesetIsCurrent && !g4Context?.error) {
      void loadTypesetContext(activeImageId, Boolean(typesetContext));
    }
  }, [activeImageId, g4Context?.error, loadTypesetContext, phase, typesetContext, typesetIsCurrent]);

  const lineageMode = hasLibrary && g4Context?.status !== 'legacy';
  const effectiveTab = lineageMode && tab !== 'text' ? 'text' : tab;
  const helperNotices = hasLibrary && !lineageMode ? (
    <>
      <ReviewBoxTools />
      <PreprocessSuggestionNotice />
      <TypesetOverflowNotice regions={regions} />
    </>
  ) : null;
  const editingSelectedText = effectiveTab === 'text' && selected.length > 0;

  return (
    <aside className="inspector panel" aria-label="属性检查器">
      <nav className="inspector-tabs" aria-label="属性标签">
        {([
          ['text', '文本'],
          ['typesetting', '排版'],
          ['repair', '修复'],
          ['project', '项目'],
        ] as const).map(([value, label]) => (
          <button
            aria-selected={effectiveTab === value}
            disabled={lineageMode && value !== 'text'}
            key={value}
            onClick={() => setRightTab(value)}
            role="tab"
            type="button"
          >{label}</button>
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
                {g4Context?.status === 'legacy'
                  ? <PageReviewControl regions={regions} />
                  : phase === 'G4'
                    ? <G4RegionsControl regions={regions} />
                    : phase === 'G5'
                      ? <G5BackgroundControl regions={regions} />
                    : phase === 'G6'
                        ? <G6OCRControl regions={regions} />
                        : phase === 'G7'
                          ? <G7MaskControl />
                          : phase === 'G8'
                            ? <G8CleanPlateControl />
                            : phase === 'G9'
                              ? <G9TranslationControl />
                              : phase === 'G10'
                                ? <G10TypesetControl />
                        : phase === 'no-text'
                          ? (
                              <section className="page-review page-review--done" aria-label="无文字终态">
                                <div><span>G3 已接受</span><strong>本页无文字，文字处理链已终止</strong></div>
                              </section>
                            )
                          : (
                              <div className="notice notice--error" role="alert">
                                <b>本页工作流已锁定</b>
                                <span>{g4Context?.error || '正在读取或校验唯一活动页代次。'}</span>
                                <div className="notice__actions">
                                  <button className="button button--compact" onClick={() => void reloadActiveImage()} type="button">重载本页</button>
                                </div>
                              </div>
                            )}
                <ProcessingErrorNotice />
                <ProcessingActivityNotice />
                {editingSelectedText ? null : helperNotices}
              </>
            ) : null}
            {effectiveTab === 'text' ? (
              hasLibrary
                ? g4Context?.status === 'active' && phase === 'G4'
                  ? <G4TextInspector regions={regions} selected={selected} />
                  : g4Context?.status === 'active' && phase === 'G5'
                    ? <G5BackgroundInspector regions={regions} selected={selected} />
                    : g4Context?.status === 'active' && phase === 'G6'
                      ? <G6OCRInspector regions={regions} selected={selected} />
                      : g4Context?.status === 'active' && phase === 'G7'
                        ? <G7MaskInspector regions={regions} selected={selected} />
                        : g4Context?.status === 'active' && phase === 'G8'
                          ? <G8CleanPlateInspector />
                        : g4Context?.status === 'active' && phase === 'G9'
                          ? <G9TranslationInspector />
                          : g4Context?.status === 'active' && phase === 'G10'
                            ? <G10TypesetInspector />
                      : g4Context?.status === 'active' && phase === 'no-text'
                        ? <EmptyState icon="✓" title="本页无文字" description="G3 已终止后续文字处理，不再创建 G4/G5 区域证据。" />
                  : g4Context?.status === 'legacy'
                    ? <TextInspector regions={regions} selected={selected} />
                    : <EmptyState icon="锁" title="本页编辑已锁定" description="正在读取或校验唯一活动页代次。" />
                : <EmptyLibraryState />
            ) : null}
            {editingSelectedText ? helperNotices : null}
            {effectiveTab === 'typesetting' ? (
              hasLibrary
                ? <TypesettingInspector region={selected.length === 1 ? selected[0] : undefined} />
                : <EmptyLibraryState />
            ) : null}
            {effectiveTab === 'repair' ? (
              hasLibrary
                ? <RepairInspector region={selected.length === 1 ? selected[0] : undefined} />
                : <EmptyLibraryState />
            ) : null}
            {effectiveTab === 'project' ? <ProjectInspector /> : null}
          </>
        )}
      </div>
    </aside>
  );
}
