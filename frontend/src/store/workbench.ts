import { create } from 'zustand';

import { ApiError, api } from '../api/client';
import type {
  AppCapabilities,
  CanvasMode,
  CanvasTool,
  ExportOptions,
  ImageAsset,
  Job,
  JobKind,
  ImageNavigationTarget,
  PreprocessSuggestion,
  PreprocessingSettings,
  Project,
  ProjectSettings,
  ProjectSummary,
  Region,
  ReviewState,
  StageReviewState,
  StageReviewObservation,
  VisualStage,
  RightPanelTab,
  StageState,
  Theme,
} from '../types';
import {
  DEFAULT_PROJECT_SETTINGS,
  DEFAULT_REGION_STYLE,
  DEFAULT_REPAIR_SETTINGS,
  EMPTY_PIPELINE_STATUS,
} from '../types';

type LoadState = 'idle' | 'loading' | 'ready' | 'error';

const TRUST_POLICY_VERSION = 1;
const TRUSTED_REASON_PAIRS = new Set(['human-confirmed', 'legacy-confirmed']);
const REVIEW_REASON_PAIRS = new Set([
  'automatic-ocr-complete',
  'automatic-proposal',
  'legacy-unverified',
  'manual-unconfirmed',
  'policy-version-changed',
  'trust-input-changed',
]);

interface HistoryFrame {
  regionsByImage: Record<string, Region[]>;
}

interface RegionMutation {
  mutationId: string;
  kind: 'create' | 'update' | 'confirm' | 'delete';
  imageId: string;
  region: Region;
  expectedRevision: number;
}

interface ProjectMutation {
  mutationId: string;
  settings: ProjectSettings;
  expectedRevision: number;
}

interface WorkbenchState {
  loadState: LoadState;
  loadMessage: string;
  globalError: string;
  capabilities: AppCapabilities;
  projects: ProjectSummary[];
  currentProject: Project | null;
  images: ImageAsset[];
  regionsByImage: Record<string, Region[]>;
  serverRegionRevisions: Record<string, number>;
  regionsLoading: Record<string, boolean>;
  activeImageId: string | null;
  selectedImageIds: string[];
  selectedRegionIds: string[];
  imageSearch: string;
  imageFilter: 'all' | 'needs_review' | 'failed' | 'complete' | 'no_text' | 'overflow';
  canvasMode: CanvasMode;
  canvasTool: CanvasTool;
  compareMode: boolean;
  showRegions: boolean;
  showOrder: boolean;
  showConfidence: boolean;
  showMask: boolean;
  maskBrushRadius: number;
  fitRequest: number;
  rightTab: RightPanelTab;
  theme: Theme;
  drawerOpen: boolean;
  shortcutsOpen: boolean;
  spacePressed: boolean;
  jobs: Job[];
  pendingRegionMutations: RegionMutation[];
  pendingProjectMutation: ProjectMutation | null;
  saving: boolean;
  saveError: string;
  lastSavedAt: string | null;
  revisionConflict: boolean;
  stageReviewSaving: string | null;
  past: HistoryFrame[];
  future: HistoryFrame[];

  initialize: () => Promise<void>;
  retryInitialize: () => Promise<void>;
  createProject: (name: string, outputPath?: string) => Promise<boolean>;
  openProjectPath: (manifestPath: string) => Promise<boolean>;
  selectProject: (projectId: string, forceReload?: boolean) => Promise<boolean>;
  importFiles: (files: File[]) => Promise<boolean>;
  loadRegions: (imageId: string, force?: boolean) => Promise<boolean>;
  reloadActiveImage: () => Promise<void>;
  selectImage: (imageId: string) => Promise<boolean>;
  navigateImage: (direction: -1 | 1, target?: ImageNavigationTarget) => Promise<boolean>;
  toggleImageSelection: (imageId: string, additive?: boolean) => void;
  selectAllVisibleImages: (imageIds: string[]) => void;
  clearImageSelection: () => void;
  setImageSearch: (value: string) => void;
  setImageFilter: (value: WorkbenchState['imageFilter']) => void;
  selectRegion: (regionId: string, additive?: boolean) => void;
  clearRegionSelection: () => void;
  createRegion: (geometry: Pick<Region, 'x' | 'y' | 'width' | 'height'>) => string | null;
  updateRegion: (regionId: string, patch: Partial<Region>, recordHistory?: boolean) => void;
  setRegionConfirmed: (regionId: string, confirmed: boolean) => Promise<boolean>;
  deleteSelectedRegions: () => void;
  mergeSelectedRegions: () => void;
  splitSelectedRegion: (axis: 'horizontal' | 'vertical') => void;
  undo: () => void;
  redo: () => void;
  flushAutosave: () => Promise<boolean>;
  updateProjectSettings: (patch: Partial<ProjectSettings>) => void;
  reviewActiveImage: (reviewState: ReviewState) => Promise<boolean>;
  reviewActiveImageStage: (
    stage: VisualStage,
    state: StageReviewState,
    observation?: StageReviewObservation,
  ) => Promise<boolean>;
  selectInpaintCandidate: (candidateId: string) => Promise<boolean>;
  setCanvasMode: (mode: CanvasMode) => void;
  setCanvasTool: (tool: CanvasTool) => void;
  toggleCompareMode: () => void;
  setShowRegions: (value: boolean) => void;
  setShowOrder: (value: boolean) => void;
  setShowConfidence: (value: boolean) => void;
  setShowMask: (value: boolean) => void;
  setMaskBrushRadius: (value: number) => void;
  requestFit: () => void;
  setRightTab: (tab: RightPanelTab) => void;
  setTheme: (theme: Theme) => void;
  setDrawerOpen: (value: boolean) => void;
  setShortcutsOpen: (value: boolean) => void;
  setSpacePressed: (value: boolean) => void;
  startBatch: (
    kinds: JobKind[],
    imageIds: string[],
    exportOptions: ExportOptions,
    concurrency?: number,
    regionIds?: string[],
    preprocessing?: PreprocessingSettings,
  ) => Promise<boolean>;
  refreshJobs: () => Promise<void>;
  runJobAction: (jobId: string, action: 'pause' | 'resume' | 'cancel' | 'retry') => Promise<void>;
  dismissError: () => void;
}

const emptyCapabilities: AppCapabilities = { providers: [] };
let autosaveTimer: ReturnType<typeof setTimeout> | null = null;
let activeSave: Promise<boolean> | null = null;
const inFlightRegionMutationIds = new Set<string>();
const regionLoadTokens = new Map<string, symbol>();
const savedRegionIdAliases = new Map<string, string>();

function resolvedRegionId(regionId: string): string {
  let resolved = regionId;
  const visited = new Set<string>();
  while (savedRegionIdAliases.has(resolved) && !visited.has(resolved)) {
    visited.add(resolved);
    resolved = savedRegionIdAliases.get(resolved) ?? resolved;
  }
  return resolved;
}

function storedTheme(): Theme {
  try {
    return typeof window !== 'undefined' && window.localStorage?.getItem('manga-localizer-theme') === 'light'
      ? 'light'
      : 'dark';
  } catch {
    return 'dark';
  }
}

function id(prefix: string): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `${prefix}-${random}`;
}

function cloneRegions(regionsByImage: Record<string, Region[]>): Record<string, Region[]> {
  return structuredClone(regionsByImage);
}

function makeHistoryFrame(regionsByImage: Record<string, Region[]>): HistoryFrame {
  return { regionsByImage: cloneRegions(regionsByImage) };
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return '发生未知错误';
}

function hydrateProject(project: Project): Project {
  const rawSettings = (project.settings ?? {}) as ProjectSettings & Record<string, unknown>;
  const exportSettings = rawSettings.export && typeof rawSettings.export === 'object'
    ? rawSettings.export as Record<string, unknown>
    : {};

  function mappingToLines(value: unknown): string {
    if (typeof value === 'string') return value;
    if (!value || typeof value !== 'object' || Array.isArray(value)) return '';
    return Object.entries(value as Record<string, unknown>)
      .map(([source, target]) => `${source} = ${String(target)}`)
      .join('\n');
  }

  return {
    ...project,
    revision: Number(project.revision ?? 0),
    imageCount: Number(project.imageCount ?? 0),
    settings: {
      ...DEFAULT_PROJECT_SETTINGS,
      ...rawSettings,
      preprocessing: {
        ...DEFAULT_PROJECT_SETTINGS.preprocessing,
        ...(rawSettings.preprocessing ?? {}),
      },
      glossary: mappingToLines(rawSettings.glossary),
      characterNames: mappingToLines(rawSettings.characterNames),
      preserveTree: typeof exportSettings.preserveTree === 'boolean'
        ? exportSettings.preserveTree
        : Boolean(rawSettings.preserveTree ?? DEFAULT_PROJECT_SETTINGS.preserveTree),
      apiKeyConfigured: false,
    },
  };
}

function stageState(value: unknown): StageState {
  const allowed: StageState[] = [
    'not_started',
    'queued',
    'running',
    'done',
    'failed',
    'unavailable',
  ];
  if (value === 'complete' || value === 'completed') return 'done';
  if (value === 'pending') return 'not_started';
  if (value === 'error') return 'failed';
  return allowed.includes(value as StageState) ? (value as StageState) : 'not_started';
}

function reviewState(value: unknown): ReviewState {
  return value === 'reviewed' || value === 'no-text-reviewed' ? value : 'pending';
}

const PREPROCESS_PROFILES: PreprocessingSettings['profile'][] = [
  'off',
  'ocr-friendly',
  'balanced',
  'visual-quality',
];
const PREPROCESS_SUGGESTION_REASONS = new Set([
  'small-page',
  'low-contrast',
  'soft-detail',
  'high-res-sharp',
  'large-page',
]);

export function preprocessingSettingsForProfile(
  profile: PreprocessingSettings['profile'],
  base: PreprocessingSettings,
): PreprocessingSettings {
  const switches: Record<PreprocessingSettings['profile'], Pick<
    PreprocessingSettings,
    | 'enableUpscale'
    | 'enableDenoise'
    | 'enableSharpen'
    | 'enableContrastEnhance'
    | 'enableEdgeOptimize'
    | 'enableBinarize'
  >> = {
    off: {
      enableUpscale: false,
      enableDenoise: false,
      enableSharpen: false,
      enableContrastEnhance: false,
      enableEdgeOptimize: false,
      enableBinarize: false,
    },
    'ocr-friendly': {
      enableUpscale: true,
      enableDenoise: true,
      enableSharpen: true,
      enableContrastEnhance: true,
      enableEdgeOptimize: false,
      enableBinarize: false,
    },
    balanced: {
      enableUpscale: false,
      enableDenoise: true,
      enableSharpen: true,
      enableContrastEnhance: true,
      enableEdgeOptimize: false,
      enableBinarize: false,
    },
    'visual-quality': {
      enableUpscale: true,
      enableDenoise: true,
      enableSharpen: true,
      enableContrastEnhance: true,
      enableEdgeOptimize: false,
      enableBinarize: false,
    },
  };
  return {
    ...base,
    profile,
    ...switches[profile],
  };
}

function hydratePreprocessSuggestion(
  raw: ImageAsset['preprocessSuggestion'] | undefined,
  width: number,
  height: number,
): PreprocessSuggestion {
  const minSide = Math.min(width, height);
  if (raw && PREPROCESS_PROFILES.includes(raw.profile)) {
    const metrics = raw.metrics ?? { width, height, minSide, sampled: false };
    return {
      profile: raw.profile,
      reasons: Array.isArray(raw.reasons)
        ? raw.reasons.filter((reason) => PREPROCESS_SUGGESTION_REASONS.has(reason))
        : [],
      metrics: {
        width,
        height,
        minSide,
        sampled: Boolean(metrics.sampled),
        ...(typeof metrics.laplacianVar === 'number' ? { laplacianVar: metrics.laplacianVar } : {}),
        ...(typeof metrics.luminanceStd === 'number' ? { luminanceStd: metrics.luminanceStd } : {}),
        ...(typeof metrics.uniqueGray === 'number' ? { uniqueGray: metrics.uniqueGray } : {}),
        ...(typeof metrics.grayscale === 'boolean' ? { grayscale: metrics.grayscale } : {}),
      },
    };
  }
  return {
    profile: minSide < 1200 ? 'ocr-friendly' : 'off',
    reasons: [minSide < 1200 ? 'small-page' : 'large-page'],
    metrics: { width, height, minSide, sampled: false },
  };
}

function hydrateImage(image: ImageAsset, settings?: ProjectSettings): ImageAsset {
  const rawStatus = (image.status ?? EMPTY_PIPELINE_STATUS) as ImageAsset['status'] & Record<string, unknown>;
  const legacyImage = image as ImageAsset & {
    reviewState?: ReviewState;
    reviewedAt?: string | null;
  };
  const ocrState = stageState(rawStatus.ocr);
  const translationState = stageState(rawStatus.translation);
  const typesetState = stageState(rawStatus.typeset);
  const overflowRegionIds = typesetState === 'done' && Array.isArray(image.typesetOverflowRegionIds)
    ? [...new Set(image.typesetOverflowRegionIds.filter((regionId): regionId is string =>
      typeof regionId === 'string' && regionId.length > 0))]
    : [];
  return {
    ...image,
    name: image.name || image.relativePath?.split('/').at(-1) || '未命名图像',
    relativePath: image.relativePath || image.name,
    width: Number(image.width ?? 1),
    height: Number(image.height ?? 1),
    regionCount: Number(image.regionCount ?? 0),
    confirmedCount: Number(image.confirmedCount ?? 0),
    ignoredCount: Number(image.ignoredCount ?? 0),
    trustedCount: Number(image.trustedCount ?? image.confirmedCount ?? 0),
    trustReviewCount: Number(
      image.trustReviewCount
        ?? Math.max(0, Number(image.regionCount ?? 0)
          - Number(image.confirmedCount ?? 0)
          - Number(image.ignoredCount ?? 0)),
    ),
    revision: Number(image.revision ?? 0),
    stageReviews: image.stageReviews && typeof image.stageReviews === 'object'
      ? image.stageReviews
      : {},
    inpaintCandidate: typeof image.inpaintCandidate === 'string' ? image.inpaintCandidate : undefined,
    inpaintCandidates: Array.isArray(image.inpaintCandidates) ? image.inpaintCandidates : [],
    typesetOverflowCount: overflowRegionIds.length,
    typesetOverflowRegionIds: overflowRegionIds,
    preprocessSuggestion: hydratePreprocessSuggestion(
      image.preprocessSuggestion,
      Number(image.width ?? 1),
      Number(image.height ?? 1),
    ),
    status: {
      import: stageState(rawStatus.import ?? 'done'),
      preprocess: stageState(rawStatus.preprocess),
      detection: stageState(rawStatus.detection),
      ocr: ocrState,
      translation: translationState,
      inpaint: stageState(rawStatus.inpaint),
      typeset: typesetState,
      export: stageState(rawStatus.export),
      reviewState: reviewState(rawStatus.reviewState ?? legacyImage.reviewState),
      reviewedAt: typeof (rawStatus.reviewedAt ?? legacyImage.reviewedAt) === 'string'
        ? String(rawStatus.reviewedAt ?? legacyImage.reviewedAt)
        : null,
    },
    preprocessingProvider: image.preprocessingProvider
      ?? (stageState(rawStatus.preprocess) === 'done' ? settings?.preprocessorProvider : undefined),
    detectorProvider: image.detectorProvider ?? (ocrState === 'done' ? settings?.detectorProvider ?? 'tesseract' : undefined),
    ocrProvider: image.ocrProvider ?? (ocrState === 'done' ? settings?.ocrProvider ?? 'tesseract' : undefined),
    translatorProvider: image.translatorProvider ?? (translationState === 'done' ? settings?.translatorProvider : undefined),
  };
}

function hydrateJob(job: Job): Job {
  return {
    ...job,
    progress: Number(job.progress ?? 0),
    total: Number(job.total ?? 0),
    completed: Number(job.completed ?? 0),
    items: (job.items ?? []).map((item) => ({
      ...item,
      label: item.label || item.imageId || item.id,
      progress: Number(item.progress ?? 0),
    })),
  };
}

function parseKeyValueLines(value: string): Record<string, string> {
  const entries: Array<[string, string]> = [];
  for (const line of value.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const separator = trimmed.includes('=') ? '=' : trimmed.includes('：') ? '：' : ':';
    const [source, ...rest] = trimmed.split(separator);
    const target = rest.join(separator).trim();
    if (source?.trim()) entries.push([source.trim(), target || source.trim()]);
  }
  return Object.fromEntries(entries);
}

function hydrateRegion(region: Region): Region {
  const rawRepair = (region.repair ?? {}) as Region['repair'] & {
    padding?: number;
  };
  const repairMethod = String(rawRepair.method ?? DEFAULT_REPAIR_SETTINGS.method)
    .toLowerCase()
    .replaceAll('-', '_');
  const ignored = Boolean(region.ignored);
  const confirmed = ignored ? false : Boolean(region.confirmed);
  const rawDisposition = region.trustDisposition;
  const rawReason = region.trustReason;
  const rawPolicyVersion = Number(region.trustPolicyVersion);
  const hasCurrentPolicy = Number.isInteger(rawPolicyVersion)
    && rawPolicyVersion === TRUST_POLICY_VERSION;
  const trustedPairIsValid = rawDisposition === 'trusted'
    && TRUSTED_REASON_PAIRS.has(rawReason)
    && hasCurrentPolicy;
  const reviewPairIsValid = rawDisposition === 'review'
    && REVIEW_REASON_PAIRS.has(rawReason)
    && hasCurrentPolicy;
  const disposition = ignored ? 'ignored' : trustedPairIsValid ? 'trusted' : 'review';
  const trustReason = ignored
    ? 'human-ignored'
    : trustedPairIsValid || reviewPairIsValid
      ? rawReason
      : rawDisposition === undefined && !rawReason && region.trustPolicyVersion === undefined
        ? 'legacy-unverified'
        : 'policy-version-changed';
  return {
    ...region,
    x: Number(region.x ?? 0),
    y: Number(region.y ?? 0),
    width: Math.max(1, Number(region.width ?? 1)),
    height: Math.max(1, Number(region.height ?? 1)),
    rotation: Number(region.rotation ?? 0),
    sourceText: region.sourceText ?? '',
    translationText: region.translationText ?? '',
    type: region.type ?? 'dialogue',
    direction: region.direction ?? 'auto',
    order: Number(region.order ?? 0),
    confidence: region.confidence === null || region.confidence === undefined
      ? null
      : Number(region.confidence),
    detectorConfidence: region.detectorConfidence === null || region.detectorConfidence === undefined
      ? null
      : Number(region.detectorConfidence),
    ocrConfidence: region.ocrConfidence === null || region.ocrConfidence === undefined
      ? null
      : Number(region.ocrConfidence),
    trustDisposition: disposition,
    trustReason,
    trustPolicyVersion: TRUST_POLICY_VERSION,
    recognition: region.recognition && typeof region.recognition === 'object'
      ? region.recognition
      : {},
    ignored,
    confirmed,
    style: { ...DEFAULT_REGION_STYLE, ...(region.style ?? {}) },
    repair: {
      ...DEFAULT_REPAIR_SETTINGS,
      ...rawRepair,
      method: repairMethod === 'navier_stokes'
        ? 'navier_stokes'
        : repairMethod === 'solid'
          ? 'solid'
          : 'telea',
      maskPadding: Number(rawRepair.maskPadding ?? rawRepair.padding ?? DEFAULT_REPAIR_SETTINGS.maskPadding),
      maskMode: rawRepair.maskMode === 'region' ? 'region' : 'text',
      feather: Number(rawRepair.feather ?? DEFAULT_REPAIR_SETTINGS.feather),
    },
    revision: Number(region.revision ?? 0),
  };
}

function hasSubstantiveRegionChange(before: Region, after: Region): boolean {
  const scalarKeys: Array<keyof Region> = [
    'x',
    'y',
    'width',
    'height',
    'rotation',
    'sourceText',
    'translationText',
    'type',
    'direction',
    'order',
    'confidence',
    'ignored',
  ];
  return scalarKeys.some((key) => before[key] !== after[key])
    || JSON.stringify(before.style) !== JSON.stringify(after.style)
    || JSON.stringify(before.repair) !== JSON.stringify(after.repair);
}

function hasTrustInputChange(before: Region, after: Region): boolean {
  const keys: Array<keyof Region> = [
    'x',
    'y',
    'width',
    'height',
    'rotation',
    'sourceText',
    'type',
    'direction',
    'confidence',
  ];
  return keys.some((key) => before[key] !== after[key]);
}

function pendingTrustCount(
  state: Pick<WorkbenchState, 'images' | 'regionsByImage'>,
  imageIds: string[],
  regionIds?: string[],
): number {
  const selectedRegions = regionIds?.length ? new Set(regionIds.map(resolvedRegionId)) : null;
  if (selectedRegions) {
    const loadedRegions = imageIds.flatMap((imageId) => state.regionsByImage[imageId] ?? []);
    return [...selectedRegions].filter((regionId) => {
      const region = loadedRegions.find((entry) => entry.id === regionId);
      return !region || (!region.ignored && region.trustDisposition !== 'trusted');
    }).length;
  }
  return imageIds.reduce((total, imageId) => {
    const image = state.images.find((entry) => entry.id === imageId);
    const serverPending = Math.max(0, Number(image?.trustReviewCount ?? 0));
    const loadedPending = (state.regionsByImage[imageId] ?? []).filter(
      (region) => !region.ignored && region.trustDisposition !== 'trusted',
    ).length;
    // A just-completed OCR job can update the page aggregate before the region
    // refresh lands. Fail closed against whichever authoritative view is newer.
    return total + Math.max(serverPending, loadedPending);
  }, 0);
}

function repairWithoutMaskPolygon(repair: Region['repair']): Region['repair'] {
  const normalized = { ...repair };
  delete normalized.maskPolygon;
  delete normalized.maskEdits;
  return normalized;
}

function updateImageCounts(
  images: ImageAsset[],
  imageId: string,
  regions: Region[],
  resetReview = false,
): ImageAsset[] {
  return images.map((image) =>
    image.id === imageId
      ? {
          ...image,
          regionCount: regions.length,
          confirmedCount: regions.filter((region) => region.confirmed).length,
          ignoredCount: regions.filter((region) => region.ignored).length,
          trustedCount: regions.filter((region) =>
            !region.ignored && region.trustDisposition === 'trusted'
          ).length,
          trustReviewCount: regions.filter((region) =>
            !region.ignored && region.trustDisposition !== 'trusted'
          ).length,
          status: resetReview
            ? { ...image.status, reviewState: 'pending', reviewedAt: null }
            : image.status,
        }
      : image,
  );
}

function updateAllImageCounts(
  images: ImageAsset[],
  regionsByImage: Record<string, Region[]>,
  resetReview = false,
): ImageAsset[] {
  return images.map((image) => {
    const regions = regionsByImage[image.id];
    return regions
      ? updateImageCounts([image], image.id, regions, resetReview)[0] ?? image
      : image;
  });
}

type InvalidatedImageStage =
  | 'preprocess'
  | 'detection'
  | 'ocr'
  | 'translation'
  | 'inpaint'
  | 'typeset'
  | 'export';

function projectSettingsInvalidatedStages(
  before: ProjectSettings,
  after: ProjectSettings,
): Set<InvalidatedImageStage> {
  const changed = (keys: Array<keyof ProjectSettings>) => keys.some(
    (key) => JSON.stringify(before[key]) !== JSON.stringify(after[key]),
  );
  const stages = new Set<InvalidatedImageStage>();
  if (changed(['preprocessorProvider', 'preprocessing'])) {
    stages.add('preprocess');
    stages.add('detection');
    stages.add('ocr');
    stages.add('translation');
    stages.add('inpaint');
    stages.add('typeset');
    stages.add('export');
  }
  if (changed(['detectorProvider'])) {
    stages.add('detection');
    stages.add('ocr');
    stages.add('translation');
    stages.add('inpaint');
    stages.add('typeset');
    stages.add('export');
  }
  if (changed(['ocrProvider', 'sourceLanguage'])) {
    stages.add('ocr');
    stages.add('translation');
    stages.add('inpaint');
    stages.add('typeset');
    stages.add('export');
  }
  if (changed([
    'translatorProvider',
    'targetLanguage',
    'glossary',
    'characterNames',
    'contextPages',
    'remoteEndpoint',
    'remoteModel',
  ])) {
    stages.add('translation');
    stages.add('typeset');
    stages.add('export');
  }
  if (changed(['inpainterProvider'])) {
    stages.add('inpaint');
    stages.add('typeset');
    stages.add('export');
  }
  return stages;
}

function invalidateImagesForSettings(
  images: ImageAsset[],
  stages: Set<InvalidatedImageStage>,
): ImageAsset[] {
  if (!stages.size) return images;
  const resetReview = [...stages].some((stage) => stage !== 'preprocess' && stage !== 'export');
  const invalidatedVisualStages = new Set<VisualStage>();
  if (stages.has('preprocess')) invalidatedVisualStages.add('preprocess');
  if (stages.has('inpaint')) invalidatedVisualStages.add('inpaint');
  if (stages.has('typeset')) invalidatedVisualStages.add('typeset');
  return images.map((image) => {
    const status = { ...image.status };
    for (const stage of stages) status[stage] = 'not_started';
    if (resetReview) {
      status.reviewState = 'pending';
      status.reviewedAt = null;
    }
    return {
      ...image,
      status,
      stageReviews: Object.fromEntries(
        Object.entries(image.stageReviews).filter(
          ([stage]) => !invalidatedVisualStages.has(stage as VisualStage),
        ),
      ) as ImageAsset['stageReviews'],
      preprocessingProvider: stages.has('preprocess') ? undefined : image.preprocessingProvider,
      detectorProvider: stages.has('detection') ? undefined : image.detectorProvider,
      ocrProvider: stages.has('ocr') ? undefined : image.ocrProvider,
      translatorProvider: stages.has('translation') ? undefined : image.translatorProvider,
      inpaintingProvider: stages.has('inpaint') ? undefined : image.inpaintingProvider,
      typesettingProvider: stages.has('typeset') ? undefined : image.typesettingProvider,
    };
  });
}

function scheduleAutosave(): void {
  if (autosaveTimer) clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => {
    autosaveTimer = null;
    void useWorkbenchStore.getState().flushAutosave();
  }, 650);
}

function replaceRegionMutation(
  pending: RegionMutation[],
  next: RegionMutation,
): RegionMutation[] {
  const existing = pending.find((mutation) => mutation.region.id === next.region.id);
  if (!existing) return [...pending, next];
  if (existing.kind === 'create' && (next.kind === 'update' || next.kind === 'confirm')) {
    return pending.map((mutation) =>
      mutation.region.id === next.region.id
        ? { ...next, kind: 'create', expectedRevision: 0 }
        : mutation,
    );
  }
  if (existing.kind === 'create' && next.kind === 'delete') {
    if (inFlightRegionMutationIds.has(existing.mutationId)) {
      return pending.map((mutation) =>
        mutation.region.id === next.region.id ? next : mutation,
      );
    }
    return pending.filter((mutation) => mutation.region.id !== next.region.id);
  }
  return pending.map((mutation) => (mutation.region.id === next.region.id ? next : mutation));
}

function syncMutationsForFrame(
  current: Record<string, Region[]>,
  target: Record<string, Region[]>,
  pending: RegionMutation[],
  serverRegionRevisions: Record<string, number>,
): {
  regionsByImage: Record<string, Region[]>;
  pendingRegionMutations: RegionMutation[];
} {
  const imageIds = new Set([...Object.keys(current), ...Object.keys(target)]);
  const regionsByImage = cloneRegions(target);
  let result = [...pending];
  for (const imageId of imageIds) {
    const before = new Map((current[imageId] ?? []).map((region) => [region.id, region]));
    const historicalAfter = target[imageId] ?? [];
    const normalizedAfter = historicalAfter.map((region) => {
      const currentRegion = before.get(region.id);
      const serverRevision = serverRegionRevisions[region.id];
      const normalized = serverRevision !== undefined
        ? { ...region, revision: serverRevision }
        : region.id.startsWith('local-')
          ? { ...region, revision: 0 }
          : {
              ...region,
              id: id('local'),
              revision: 0,
              createdAt: undefined,
              updatedAt: undefined,
            };
      return normalized.confirmed
        && (!currentRegion || hasSubstantiveRegionChange(currentRegion, normalized))
        ? { ...normalized, confirmed: false }
        : normalized;
    });
    regionsByImage[imageId] = normalizedAfter;
    const after = new Map(normalizedAfter.map((region) => [region.id, region]));
    const involved = new Set([
      ...before.keys(),
      ...historicalAfter.map((region) => region.id),
      ...after.keys(),
    ]);
    result = result.filter((mutation) => !involved.has(mutation.region.id));
    for (const [regionId, region] of after) {
      const serverRevision = serverRegionRevisions[regionId];
      result.push({
        mutationId: id('mutation'),
        kind: serverRevision === undefined ? 'create' : 'update',
        imageId,
        region,
        expectedRevision: serverRevision ?? 0,
      });
    }
    for (const [regionId, region] of before) {
      const serverRevision = serverRegionRevisions[regionId];
      if (!after.has(regionId) && serverRevision !== undefined) {
        result.push({
          mutationId: id('mutation'),
          kind: 'delete',
          imageId,
          region: { ...region, revision: serverRevision },
          expectedRevision: serverRevision,
        });
      }
    }
  }
  return { regionsByImage, pendingRegionMutations: result };
}

function mergeProjectSnapshot(state: WorkbenchState, snapshot: Project): Partial<WorkbenchState> {
  const projects = state.projects.map((project) =>
    project.id === snapshot.id ? snapshot : project,
  );
  if (state.currentProject?.id !== snapshot.id) return { projects };
  if (snapshot.revision < state.currentProject.revision) return {};

  const pending = state.pendingProjectMutation;
  const currentProject = pending
    ? { ...snapshot, settings: pending.settings }
    : snapshot;
  return {
    currentProject,
    pendingProjectMutation: pending
      ? { ...pending, expectedRevision: snapshot.revision }
      : null,
    projects,
  };
}

async function synchronizeProject(projectId: string): Promise<Project> {
  const snapshot = hydrateProject(await api.getProject(projectId));
  useWorkbenchStore.setState((state) => mergeProjectSnapshot(state, snapshot));
  return snapshot;
}

async function synchronizeImages(projectId: string): Promise<void> {
  const current = useWorkbenchStore.getState().currentProject;
  if (!current || current.id !== projectId) return;
  const response = await api.listImages(projectId);
  useWorkbenchStore.setState((state) => {
    if (state.currentProject?.id !== projectId) return {};
    const images = response
      .map((image) => hydrateImage(image, state.currentProject?.settings))
      .sort((left, right) => left.relativePath.localeCompare(right.relativePath));
    return {
      images: images.map((image) => {
        const currentImage = state.images.find((entry) => entry.id === image.id);
        if (currentImage && currentImage.revision > image.revision) return currentImage;
        const hasPendingEdit = state.pendingRegionMutations.some(
          (mutation) => mutation.imageId === image.id,
        );
        const localRegions = state.regionsByImage[image.id];
        return hasPendingEdit && localRegions
          ? updateImageCounts([image], image.id, localRegions, true)[0] ?? image
          : image;
      }),
    };
  });
}

function applySavedProject(savedProject: Project, mutationId: string): void {
  useWorkbenchStore.setState((state) => {
    const pending = state.pendingProjectMutation;
    const savedMutationIsCurrent = pending?.mutationId === mutationId;
    const newerPending = pending && !savedMutationIsCurrent ? pending : null;
    const currentProject = state.currentProject?.id === savedProject.id
      ? newerPending
        ? { ...savedProject, settings: newerPending.settings }
        : savedProject
      : state.currentProject;
    return {
      currentProject,
      pendingProjectMutation: newerPending
        ? { ...newerPending, expectedRevision: savedProject.revision }
        : savedMutationIsCurrent
          ? null
          : pending,
      projects: state.projects.map((project) =>
        project.id === savedProject.id ? savedProject : project,
      ),
    };
  });
}

async function flushProjectMutation(): Promise<void> {
  const maxAttempts = 3;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const project = useWorkbenchStore.getState().currentProject;
    if (!project || !useWorkbenchStore.getState().pendingProjectMutation) return;
    await synchronizeProject(project.id);
    const latest = useWorkbenchStore.getState();
    const mutation = latest.pendingProjectMutation;
    if (!mutation || latest.currentProject?.id !== project.id) return;
    try {
      const savedProject = hydrateProject(
        await api.updateProject(project.id, {
          settings: mutation.settings,
          expectedRevision: mutation.expectedRevision,
        }),
      );
      applySavedProject(savedProject, mutation.mutationId);
      return;
    } catch (error) {
      if (!(error instanceof ApiError && error.status === 409 && attempt < maxAttempts - 1)) {
        throw error;
      }
    }
  }
}

async function performFlush(): Promise<boolean> {
  if (autosaveTimer) {
    clearTimeout(autosaveTimer);
    autosaveTimer = null;
  }
  if (activeSave) {
    const activeResult = await activeSave;
    if (!activeResult) return false;
    const remaining = useWorkbenchStore.getState();
    return remaining.pendingRegionMutations.length || remaining.pendingProjectMutation
      ? performFlush()
      : true;
  }

  activeSave = (async () => {
    const beforeSave = useWorkbenchStore.getState();
    if (!beforeSave.pendingRegionMutations.length && !beforeSave.pendingProjectMutation) return true;
    const hadProjectMutation = Boolean(beforeSave.pendingProjectMutation);
    useWorkbenchStore.setState({ saving: true, saveError: '', revisionConflict: false });

    try {
      await flushProjectMutation();

      const pendingRegions = [...useWorkbenchStore.getState().pendingRegionMutations];
      for (const mutation of pendingRegions) {
        inFlightRegionMutationIds.add(mutation.mutationId);
      }
      for (const mutation of pendingRegions) {
        let saved: Region | null = null;
        try {
          if (mutation.kind === 'create') {
            saved = hydrateRegion(await api.createRegion(mutation.imageId, mutation.region));
          } else if (mutation.kind === 'update') {
            saved = hydrateRegion(
              await api.updateRegion(mutation.region.id, {
                ...mutation.region,
                expectedRevision: mutation.expectedRevision,
              }),
            );
          } else if (mutation.kind === 'confirm') {
            saved = hydrateRegion(
              await api.updateRegion(mutation.region.id, {
                confirmed: true,
                expectedRevision: mutation.expectedRevision,
              }),
            );
          } else {
            await api.deleteRegion(mutation.region.id, mutation.expectedRevision);
          }
        } catch (error) {
          if (mutation.kind === 'create') {
            useWorkbenchStore.setState((state) => ({
              pendingRegionMutations: state.pendingRegionMutations.filter((entry) =>
                !(entry.region.id === mutation.region.id && entry.kind === 'delete')
              ),
            }));
          } else if (mutation.kind === 'confirm') {
            useWorkbenchStore.setState((state) => ({
              pendingRegionMutations: state.pendingRegionMutations.filter(
                (entry) => entry.mutationId !== mutation.mutationId,
              ),
            }));
          }
          throw error;
        } finally {
          inFlightRegionMutationIds.delete(mutation.mutationId);
        }

        if (saved && saved.id !== mutation.region.id) {
          savedRegionIdAliases.set(mutation.region.id, saved.id);
        }
        useWorkbenchStore.setState((state) => {
          const oldId = mutation.region.id;
          const withoutCompleted = state.pendingRegionMutations.filter(
            (entry) => entry.mutationId !== mutation.mutationId,
          );
          const newer = withoutCompleted.find((entry) => entry.region.id === oldId);
          let pendingRegionMutations = withoutCompleted;
          const serverRegionRevisions = { ...state.serverRegionRevisions };
          if (!saved) {
            delete serverRegionRevisions[oldId];
            if (newer?.kind === 'delete') {
              pendingRegionMutations = withoutCompleted.filter(
                (entry) => entry.mutationId !== newer.mutationId,
              );
            } else if (newer) {
              pendingRegionMutations = withoutCompleted.map((entry) =>
                entry.mutationId === newer.mutationId
                  ? {
                      ...entry,
                      kind: 'create',
                      expectedRevision: 0,
                      region: { ...entry.region, revision: 0 },
                    }
                  : entry,
              );
            }
            return { pendingRegionMutations, serverRegionRevisions };
          }

          delete serverRegionRevisions[oldId];
          serverRegionRevisions[saved.id] = saved.revision;
          let replacement = saved as Region;
          if (newer) {
            const rebasedRegion = {
              ...newer.region,
              id: saved.id,
              revision: saved.revision,
              createdAt: saved.createdAt,
              updatedAt: saved.updatedAt,
            };
            replacement = rebasedRegion;
            pendingRegionMutations = withoutCompleted.map((entry) =>
              entry.mutationId === newer.mutationId
                ? {
                    ...entry,
                    kind: newer.kind === 'delete'
                      ? 'delete'
                      : newer.kind === 'confirm'
                        ? 'confirm'
                        : 'update',
                    expectedRevision: saved.revision,
                    region: rebasedRegion,
                  }
                : entry,
            );
          }
          const currentRegions = state.regionsByImage[mutation.imageId] ?? [];
          let regions = currentRegions.map((region) =>
            region.id === oldId ? replacement : region,
          );
          if (newer && newer.kind !== 'delete' && !currentRegions.some((region) => region.id === oldId)) {
            regions = [...regions, replacement].sort((left, right) => left.order - right.order);
          }
          return {
            pendingRegionMutations,
            serverRegionRevisions,
            regionsByImage: { ...state.regionsByImage, [mutation.imageId]: regions },
            selectedRegionIds: state.selectedRegionIds.map((regionId) =>
              regionId === oldId ? (saved as Region).id : regionId,
            ),
            images: updateImageCounts(state.images, mutation.imageId, regions),
            past: mutation.kind === 'confirm' && !newer
              ? [...state.past.slice(-49), makeHistoryFrame(state.regionsByImage)]
              : state.past,
            future: mutation.kind === 'confirm' && !newer ? [] : state.future,
          };
        });
      }

      const projectId = useWorkbenchStore.getState().currentProject?.id;
      if (projectId && (hadProjectMutation || pendingRegions.length)) {
        await Promise.all([
          synchronizeProject(projectId),
          synchronizeImages(projectId),
        ]);
      }

      useWorkbenchStore.setState({
        saving: false,
        saveError: '',
        lastSavedAt: new Date().toISOString(),
      });
      return true;
    } catch (error) {
      inFlightRegionMutationIds.clear();
      useWorkbenchStore.setState({
        saving: false,
        saveError: errorMessage(error),
        revisionConflict: error instanceof ApiError && error.status === 409,
      });
      return false;
    }
  })().finally(() => {
    activeSave = null;
  });

  const result = await activeSave;
  if (result) {
    const remaining = useWorkbenchStore.getState();
    if (remaining.pendingRegionMutations.length || remaining.pendingProjectMutation) {
      return performFlush();
    }
  }
  return result;
}

const initialUiState = {
  loadState: 'idle' as LoadState,
  loadMessage: '正在连接本地服务…',
  globalError: '',
  capabilities: emptyCapabilities,
  projects: [] as ProjectSummary[],
  currentProject: null as Project | null,
  images: [] as ImageAsset[],
  regionsByImage: {} as Record<string, Region[]>,
  serverRegionRevisions: {} as Record<string, number>,
  regionsLoading: {} as Record<string, boolean>,
  activeImageId: null as string | null,
  selectedImageIds: [] as string[],
  selectedRegionIds: [] as string[],
  imageSearch: '',
  imageFilter: 'all' as const,
  canvasMode: 'original' as CanvasMode,
  canvasTool: 'select' as CanvasTool,
  compareMode: false,
  showRegions: true,
  showOrder: true,
  showConfidence: true,
  showMask: false,
  maskBrushRadius: 12,
  fitRequest: 0,
  rightTab: 'text' as RightPanelTab,
  theme: storedTheme(),
  drawerOpen: false,
  shortcutsOpen: false,
  spacePressed: false,
  jobs: [] as Job[],
  pendingRegionMutations: [] as RegionMutation[],
  pendingProjectMutation: null as ProjectMutation | null,
  saving: false,
  saveError: '',
  lastSavedAt: null as string | null,
  revisionConflict: false,
  stageReviewSaving: null as string | null,
  past: [] as HistoryFrame[],
  future: [] as HistoryFrame[],
};

export const useWorkbenchStore = create<WorkbenchState>((set, get) => ({
  ...initialUiState,

  initialize: async () => {
    set({ loadState: 'loading', loadMessage: '正在读取本地项目…', globalError: '' });
    try {
      const [capabilities, projects] = await Promise.all([
        api.getCapabilities(),
        api.listProjects(),
      ]);
      set({
        capabilities: { ...capabilities, providers: capabilities.providers ?? [] },
        projects,
        loadState: 'ready',
      });
      if (projects[0]) await get().selectProject(projects[0].id);
    } catch (error) {
      set({
        loadState: 'error',
        loadMessage: '本地服务不可用',
        globalError: errorMessage(error),
      });
    }
  },

  retryInitialize: async () => get().initialize(),

  createProject: async (name, outputPath) => {
    set({ globalError: '' });
    try {
      const project = hydrateProject(await api.createProject({ name, outputPath }));
      set((state) => ({ projects: [project, ...state.projects] }));
      return get().selectProject(project.id);
    } catch (error) {
      set({ globalError: errorMessage(error) });
      return false;
    }
  },

  openProjectPath: async (manifestPath) => {
    set({ globalError: '' });
    try {
      if (!(await get().flushAutosave())) return false;
      const project = hydrateProject(await api.openProject(manifestPath));
      set((state) => ({
        projects: [project, ...state.projects.filter((entry) => entry.id !== project.id)],
      }));
      return get().selectProject(project.id, true);
    } catch (error) {
      set({ globalError: errorMessage(error) });
      return false;
    }
  },

  selectProject: async (projectId, forceReload = false) => {
    if (!forceReload && get().currentProject?.id === projectId) return true;
    if (!(await get().flushAutosave())) return false;
    set({ loadState: 'loading', loadMessage: '正在打开项目…', globalError: '' });
    try {
      const [projectResponse, imageResponse, jobs] = await Promise.all([
        api.getProject(projectId),
        api.listImages(projectId),
        api.listJobs(projectId),
      ]);
      const project = hydrateProject(projectResponse);
      const images = imageResponse.map((image) => hydrateImage(image, project.settings)).sort((a, b) =>
        a.relativePath.localeCompare(b.relativePath, 'zh-CN', { numeric: true }),
      );
      const firstImageId = images[0]?.id ?? null;
      set({
        loadState: 'ready',
        currentProject: project,
        projects: get().projects.map((entry) => (entry.id === project.id ? project : entry)),
        images,
        jobs: jobs.map(hydrateJob),
        activeImageId: firstImageId,
        selectedImageIds: firstImageId ? [firstImageId] : [],
        selectedRegionIds: [],
        regionsByImage: {},
        serverRegionRevisions: {},
        pendingRegionMutations: [],
        pendingProjectMutation: null,
        past: [],
        future: [],
      });
      if (firstImageId) await get().loadRegions(firstImageId);
      return true;
    } catch (error) {
      set({ loadState: 'error', loadMessage: '项目打开失败', globalError: errorMessage(error) });
      return false;
    }
  },

  importFiles: async (files) => {
    const project = get().currentProject;
    if (!project || files.length === 0) return false;
    set({ globalError: '' });
    try {
      const imported = (await api.uploadImages(project.id, files)).map((image) => hydrateImage(image, project.settings));
      set((state) => {
        const merged = new Map(state.images.map((image) => [image.id, image]));
        imported.forEach((image) => merged.set(image.id, image));
        return {
          images: [...merged.values()].sort((a, b) =>
            a.relativePath.localeCompare(b.relativePath, 'zh-CN', { numeric: true }),
          ),
          activeImageId: state.activeImageId ?? imported[0]?.id ?? null,
          selectedImageIds: imported.map((image) => image.id),
          currentProject: state.currentProject
            ? {
                ...state.currentProject,
                imageCount: merged.size,
              }
            : null,
        };
      });
      await synchronizeProject(project.id);
      const activeImageId = get().activeImageId;
      if (activeImageId) await get().loadRegions(activeImageId);
      return true;
    } catch (error) {
      set({ globalError: errorMessage(error) });
      return false;
    }
  },

  loadRegions: async (imageId, force = false) => {
    if (!force && get().regionsByImage[imageId]) return true;
    const requestToken = Symbol(imageId);
    const regionsAtRequest = get().regionsByImage[imageId];
    regionLoadTokens.set(imageId, requestToken);
    set((state) => ({
      regionsLoading: { ...state.regionsLoading, [imageId]: true },
      globalError: '',
    }));
    try {
      const regions = (await api.listRegions(imageId)).map(hydrateRegion).sort((a, b) => a.order - b.order);
      let applied = false;
      set((state) => {
        if (regionLoadTokens.get(imageId) !== requestToken) return {};
        const localStateChanged = state.regionsByImage[imageId] !== regionsAtRequest;
        const hasPendingEdit = state.pendingRegionMutations.some(
          (mutation) => mutation.imageId === imageId,
        );
        if (localStateChanged || hasPendingEdit) {
          return {
            regionsLoading: { ...state.regionsLoading, [imageId]: false },
          };
        }
        const serverRegionRevisions = { ...state.serverRegionRevisions };
        for (const previous of state.regionsByImage[imageId] ?? []) {
          delete serverRegionRevisions[previous.id];
        }
        for (const region of regions) serverRegionRevisions[region.id] = region.revision;
        applied = true;
        return {
          regionsByImage: { ...state.regionsByImage, [imageId]: regions },
          serverRegionRevisions,
          regionsLoading: { ...state.regionsLoading, [imageId]: false },
          images: updateImageCounts(state.images, imageId, regions),
        };
      });
      return applied;
    } catch (error) {
      if (regionLoadTokens.get(imageId) !== requestToken) return false;
      set((state) => ({
        regionsLoading: { ...state.regionsLoading, [imageId]: false },
        globalError: errorMessage(error),
      }));
      return false;
    } finally {
      if (regionLoadTokens.get(imageId) === requestToken) {
        regionLoadTokens.delete(imageId);
      }
    }
  },

  reloadActiveImage: async () => {
    const imageId = get().activeImageId;
    const projectId = get().currentProject?.id;
    if (!imageId || !projectId) return;
    set((state) => ({
      pendingRegionMutations: state.pendingRegionMutations.filter(
        (mutation) => mutation.imageId !== imageId,
      ),
      saveError: '',
      globalError: '',
      revisionConflict: false,
      selectedRegionIds: [],
    }));
    await synchronizeImages(projectId);
    await get().loadRegions(imageId, true);
  },

  selectImage: async (imageId) => {
    if (imageId === get().activeImageId) return true;
    if (!(await get().flushAutosave())) return false;
    set({ activeImageId: imageId, selectedRegionIds: [] });
    await get().loadRegions(imageId);
    return true;
  },

  navigateImage: async (direction, target = 'adjacent') => {
    const { images, activeImageId } = get();
    if (!images.length) return false;
    const currentIndex = Math.max(0, images.findIndex((image) => image.id === activeImageId));
    const step = direction < 0 ? -1 : 1;
    if (target === 'adjacent') {
      const nextIndex = Math.min(images.length - 1, Math.max(0, currentIndex + step));
      const nextImage = images[nextIndex];
      if (!nextImage || nextImage.id === activeImageId) return false;
      return get().selectImage(nextImage.id);
    }
    const matches = target === 'overflow' ? imageHasTypesetOverflow : imagePageReviewPending;
    for (let seen = 0; seen < images.length - 1; seen += 1) {
      const index = (currentIndex + step * (seen + 1) + images.length * (seen + 1)) % images.length;
      const nextImage = images[index];
      if (nextImage && matches(nextImage)) return get().selectImage(nextImage.id);
    }
    return false;
  },

  toggleImageSelection: (imageId, additive = true) => {
    set((state) => {
      if (!additive) return { selectedImageIds: [imageId] };
      return {
        selectedImageIds: state.selectedImageIds.includes(imageId)
          ? state.selectedImageIds.filter((entry) => entry !== imageId)
          : [...state.selectedImageIds, imageId],
      };
    });
  },

  selectAllVisibleImages: (imageIds) => set({ selectedImageIds: imageIds }),
  clearImageSelection: () => set({ selectedImageIds: [] }),
  setImageSearch: (imageSearch) => set({ imageSearch }),
  setImageFilter: (imageFilter) => set({ imageFilter }),

  selectRegion: (regionId, additive = false) => {
    set((state) => ({
      selectedRegionIds: additive
        ? state.selectedRegionIds.includes(regionId)
          ? state.selectedRegionIds.filter((entry) => entry !== regionId)
          : [...state.selectedRegionIds, regionId]
        : [regionId],
    }));
  },

  clearRegionSelection: () => set({ selectedRegionIds: [] }),

  createRegion: (geometry) => {
    const imageId = get().activeImageId;
    if (!imageId) return null;
    const current = get().regionsByImage[imageId] ?? [];
    const region: Region = {
      id: id('local'),
      imageId,
      x: Math.max(0, Math.round(geometry.x)),
      y: Math.max(0, Math.round(geometry.y)),
      width: Math.max(4, Math.round(geometry.width)),
      height: Math.max(4, Math.round(geometry.height)),
      rotation: 0,
      sourceText: '',
      translationText: '',
      type: 'dialogue',
      direction: 'auto',
      order: current.reduce((max, entry) => Math.max(max, entry.order), 0) + 1,
      confidence: null,
      detectorConfidence: null,
      ocrConfidence: null,
      trustDisposition: 'review',
      trustReason: 'manual-unconfirmed',
      trustPolicyVersion: 1,
      recognition: {},
      ignored: false,
      confirmed: false,
      style: { ...DEFAULT_REGION_STYLE },
      repair: { ...DEFAULT_REPAIR_SETTINGS },
      revision: 0,
    };
    const next = [...current, region];
    set((state) => ({
      regionsByImage: { ...state.regionsByImage, [imageId]: next },
      images: updateImageCounts(state.images, imageId, next, true),
      selectedRegionIds: [region.id],
      past: [...state.past.slice(-49), makeHistoryFrame(state.regionsByImage)],
      future: [],
      pendingRegionMutations: replaceRegionMutation(state.pendingRegionMutations, {
        mutationId: id('mutation'),
        kind: 'create',
        imageId,
        region,
        expectedRevision: 0,
      }),
    }));
    scheduleAutosave();
    return region.id;
  },

  updateRegion: (regionId, patch, recordHistory = true) => {
    const state = get();
    const imageId = state.activeImageId;
    if (!imageId) return;
    const current = state.regionsByImage[imageId] ?? [];
    const original = current.find((region) => region.id === regionId);
    if (!original) return;
    const exclusivePatch = patch.confirmed === true
      ? { ...patch, ignored: false }
      : patch.ignored === true
        ? { ...patch, confirmed: false }
        : patch;
    let updated = hydrateRegion({
      ...original,
      ...exclusivePatch,
      style: exclusivePatch.style
        ? { ...original.style, ...exclusivePatch.style }
        : original.style,
      repair: exclusivePatch.repair
        ? { ...original.repair, ...exclusivePatch.repair }
        : original.repair,
    });
    if (original.confirmed && hasSubstantiveRegionChange(original, updated)) {
      updated = { ...updated, confirmed: false };
    }
    if (exclusivePatch.ignored === true) {
      updated = { ...updated, trustDisposition: 'ignored', trustReason: 'human-ignored' };
    } else if (exclusivePatch.ignored === false && original.trustDisposition === 'ignored') {
      updated = { ...updated, trustDisposition: 'review', trustReason: 'trust-input-changed' };
    } else if (original.trustDisposition === 'trusted' && hasTrustInputChange(original, updated)) {
      updated = { ...updated, trustDisposition: 'review', trustReason: 'trust-input-changed' };
    }
    const next = current.map((region) => (region.id === regionId ? updated : region));
    set((currentState) => ({
      regionsByImage: { ...currentState.regionsByImage, [imageId]: next },
      images: updateImageCounts(currentState.images, imageId, next, true),
      past: recordHistory
        ? [...currentState.past.slice(-49), makeHistoryFrame(currentState.regionsByImage)]
        : currentState.past,
      future: recordHistory ? [] : currentState.future,
      pendingRegionMutations: replaceRegionMutation(currentState.pendingRegionMutations, {
        mutationId: id('mutation'),
        kind: 'update',
        imageId,
        region: updated,
        expectedRevision: currentState.serverRegionRevisions[original.id] ?? original.revision,
      }),
    }));
    scheduleAutosave();
  },

  setRegionConfirmed: async (regionId, confirmed) => {
    const state = get();
    const imageId = state.activeImageId;
    const region = imageId
      ? (state.regionsByImage[imageId] ?? []).find((entry) => entry.id === regionId)
      : undefined;
    if (!imageId || !region) return false;
    if (!confirmed) {
      get().updateRegion(regionId, { confirmed: false });
      return true;
    }
    if (region.confirmed && !region.ignored && region.trustDisposition === 'trusted') return true;

    // Unignoring is itself substantive and must reach the server before the sparse reconfirmation.
    if (region.ignored) get().updateRegion(regionId, { ignored: false });
    if (!(await get().flushAutosave())) return false;

    const savedId = resolvedRegionId(regionId);
    const refreshedState = get();
    const refreshed = (refreshedState.regionsByImage[imageId] ?? []).find(
      (entry) => entry.id === savedId,
    );
    if (!refreshed) {
      set({
        saveError: '文本框在确认前已被删除或重载。',
        revisionConflict: false,
      });
      return false;
    }
    if (
      refreshed.confirmed
      && !refreshed.ignored
      && refreshed.trustDisposition === 'trusted'
    ) return true;

    set((currentState) => ({
      pendingRegionMutations: replaceRegionMutation(currentState.pendingRegionMutations, {
        mutationId: id('mutation'),
        kind: 'confirm',
        imageId,
        region: { ...refreshed, confirmed: true, ignored: false },
        expectedRevision: currentState.serverRegionRevisions[savedId] ?? refreshed.revision,
      }),
      saveError: '',
      revisionConflict: false,
    }));
    const saved = await get().flushAutosave();
    if (!saved) return false;
    const confirmedRegion = (get().regionsByImage[imageId] ?? []).find(
      (entry) => entry.id === savedId,
    );
    return Boolean(
      confirmedRegion
      && confirmedRegion.confirmed
      && !confirmedRegion.ignored
      && confirmedRegion.trustDisposition === 'trusted',
    );
  },

  deleteSelectedRegions: () => {
    const state = get();
    const imageId = state.activeImageId;
    if (!imageId || !state.selectedRegionIds.length) return;
    const selected = new Set(state.selectedRegionIds);
    const current = state.regionsByImage[imageId] ?? [];
    const removed = current.filter((region) => selected.has(region.id));
    const next = current.filter((region) => !selected.has(region.id));
    let mutations = state.pendingRegionMutations;
    for (const region of removed) {
      mutations = replaceRegionMutation(mutations, {
        mutationId: id('mutation'),
        kind: 'delete',
        imageId,
        region,
        expectedRevision: state.serverRegionRevisions[region.id] ?? region.revision,
      });
    }
    set((currentState) => ({
      regionsByImage: { ...currentState.regionsByImage, [imageId]: next },
      images: updateImageCounts(currentState.images, imageId, next, true),
      selectedRegionIds: [],
      past: [...currentState.past.slice(-49), makeHistoryFrame(currentState.regionsByImage)],
      future: [],
      pendingRegionMutations: mutations,
    }));
    scheduleAutosave();
  },

  mergeSelectedRegions: () => {
    const state = get();
    const imageId = state.activeImageId;
    if (!imageId || state.selectedRegionIds.length < 2) return;
    const selectedIds = new Set(state.selectedRegionIds);
    const current = state.regionsByImage[imageId] ?? [];
    const selected = current.filter((region) => selectedIds.has(region.id));
    if (selected.length < 2) return;
    const first = [...selected].sort((a, b) => a.order - b.order)[0];
    if (!first) return;
    const x = Math.min(...selected.map((region) => region.x));
    const y = Math.min(...selected.map((region) => region.y));
    const right = Math.max(...selected.map((region) => region.x + region.width));
    const bottom = Math.max(...selected.map((region) => region.y + region.height));
    const confidenceValues = selected
      .map((region) => region.confidence)
      .filter((value): value is number => value !== null);
    const merged: Region = {
      ...first,
      id: id('local'),
      x: Math.round(x),
      y: Math.round(y),
      width: Math.max(4, Math.round(right - x)),
      height: Math.max(4, Math.round(bottom - y)),
      rotation: 0,
      sourceText: [...selected]
        .sort((a, b) => a.order - b.order)
        .map((region) => region.sourceText)
        .filter(Boolean)
        .join('\n'),
      translationText: [...selected]
        .sort((a, b) => a.order - b.order)
        .map((region) => region.translationText)
        .filter(Boolean)
        .join('\n'),
      order: Math.min(...selected.map((region) => region.order)),
      confidence: confidenceValues.length
        ? confidenceValues.reduce((sum, value) => sum + value, 0) / confidenceValues.length
        : null,
      detectorConfidence: null,
      ocrConfidence: null,
      trustDisposition: 'review',
      trustReason: 'manual-unconfirmed',
      trustPolicyVersion: 1,
      recognition: {},
      ignored: false,
      confirmed: false,
      repair: repairWithoutMaskPolygon(first.repair),
      revision: 0,
      createdAt: undefined,
      updatedAt: undefined,
    };
    const next = [...current.filter((region) => !selectedIds.has(region.id)), merged].sort(
      (a, b) => a.order - b.order,
    );
    let mutations = state.pendingRegionMutations;
    for (const region of selected) {
      mutations = replaceRegionMutation(mutations, {
        mutationId: id('mutation'),
        kind: 'delete',
        imageId,
        region,
        expectedRevision: state.serverRegionRevisions[region.id] ?? region.revision,
      });
    }
    mutations = replaceRegionMutation(mutations, {
      mutationId: id('mutation'),
      kind: 'create',
      imageId,
      region: merged,
      expectedRevision: 0,
    });
    set({
      regionsByImage: { ...state.regionsByImage, [imageId]: next },
      images: updateImageCounts(state.images, imageId, next, true),
      selectedRegionIds: [merged.id],
      past: [...state.past.slice(-49), makeHistoryFrame(state.regionsByImage)],
      future: [],
      pendingRegionMutations: mutations,
    });
    scheduleAutosave();
  },

  splitSelectedRegion: (axis) => {
    const state = get();
    const imageId = state.activeImageId;
    if (!imageId || state.selectedRegionIds.length !== 1) return;
    const current = state.regionsByImage[imageId] ?? [];
    const original = current.find((region) => region.id === state.selectedRegionIds[0]);
    if (!original) return;

    function splitText(text: string): [string, string] {
      if (!text) return ['', ''];
      const lines = text.split('\n');
      if (lines.length > 1) {
        const midpoint = Math.ceil(lines.length / 2);
        return [lines.slice(0, midpoint).join('\n'), lines.slice(midpoint).join('\n')];
      }
      const midpoint = Math.ceil([...text].length / 2);
      return [[...text].slice(0, midpoint).join(''), [...text].slice(midpoint).join('')];
    }

    const [sourceA, sourceB] = splitText(original.sourceText);
    const [translationA, translationB] = splitText(original.translationText);
    const first: Region = {
      ...original,
      id: id('local'),
      width: axis === 'vertical' ? Math.max(4, Math.round(original.width / 2)) : original.width,
      height: axis === 'horizontal' ? Math.max(4, Math.round(original.height / 2)) : original.height,
      rotation: 0,
      sourceText: sourceA,
      translationText: translationA,
      detectorConfidence: null,
      ocrConfidence: null,
      trustDisposition: 'review',
      trustReason: 'manual-unconfirmed',
      trustPolicyVersion: 1,
      recognition: {},
      ignored: false,
      confirmed: false,
      repair: repairWithoutMaskPolygon(original.repair),
      revision: 0,
      createdAt: undefined,
      updatedAt: undefined,
    };
    const second: Region = {
      ...first,
      id: id('local'),
      x: axis === 'vertical' ? original.x + first.width : original.x,
      y: axis === 'horizontal' ? original.y + first.height : original.y,
      width: axis === 'vertical' ? Math.max(4, original.width - first.width) : original.width,
      height: axis === 'horizontal' ? Math.max(4, original.height - first.height) : original.height,
      sourceText: sourceB,
      translationText: translationB,
      order: original.order + 1,
    };
    const next = [
      ...current.filter((region) => region.id !== original.id),
      first,
      second,
    ].sort((a, b) => a.order - b.order);
    let mutations = replaceRegionMutation(state.pendingRegionMutations, {
      mutationId: id('mutation'),
      kind: 'delete',
      imageId,
      region: original,
      expectedRevision: state.serverRegionRevisions[original.id] ?? original.revision,
    });
    for (const region of [first, second]) {
      mutations = replaceRegionMutation(mutations, {
        mutationId: id('mutation'),
        kind: 'create',
        imageId,
        region,
        expectedRevision: 0,
      });
    }
    set({
      regionsByImage: { ...state.regionsByImage, [imageId]: next },
      images: updateImageCounts(state.images, imageId, next, true),
      selectedRegionIds: [first.id, second.id],
      past: [...state.past.slice(-49), makeHistoryFrame(state.regionsByImage)],
      future: [],
      pendingRegionMutations: mutations,
    });
    scheduleAutosave();
  },

  undo: () => {
    const state = get();
    const previous = state.past.at(-1);
    if (!previous) return;
    const restored = syncMutationsForFrame(
      state.regionsByImage,
      previous.regionsByImage,
      state.pendingRegionMutations,
      state.serverRegionRevisions,
    );
    set({
      regionsByImage: restored.regionsByImage,
      images: updateAllImageCounts(state.images, restored.regionsByImage, true),
      past: state.past.slice(0, -1),
      future: [makeHistoryFrame(state.regionsByImage), ...state.future.slice(0, 49)],
      pendingRegionMutations: restored.pendingRegionMutations,
      selectedRegionIds: [],
    });
    scheduleAutosave();
  },

  redo: () => {
    const state = get();
    const next = state.future[0];
    if (!next) return;
    const restored = syncMutationsForFrame(
      state.regionsByImage,
      next.regionsByImage,
      state.pendingRegionMutations,
      state.serverRegionRevisions,
    );
    set({
      regionsByImage: restored.regionsByImage,
      images: updateAllImageCounts(state.images, restored.regionsByImage, true),
      past: [...state.past.slice(-49), makeHistoryFrame(state.regionsByImage)],
      future: state.future.slice(1),
      pendingRegionMutations: restored.pendingRegionMutations,
      selectedRegionIds: [],
    });
    scheduleAutosave();
  },

  flushAutosave: performFlush,

  updateProjectSettings: (patch) => {
    const project = get().currentProject;
    if (!project) return;
    const settings = { ...project.settings, ...patch };
    const invalidatedStages = projectSettingsInvalidatedStages(project.settings, settings);
    set({
      currentProject: { ...project, settings },
      images: invalidateImagesForSettings(get().images, invalidatedStages),
      pendingProjectMutation: {
        mutationId: id('project-mutation'),
        settings,
        expectedRevision: project.revision,
      },
    });
    scheduleAutosave();
  },

  reviewActiveImage: async (nextReviewState) => {
    if (!(await get().flushAutosave())) return false;
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const reviewRegions = state.regionsByImage[image.id];
    if (!reviewRegions) {
      set({ globalError: '当前页文本框尚未加载，请稍后再复核。' });
      return false;
    }
    const activeRegionsForReview = reviewRegions.filter(
      (region) => !region.ignored,
    );
    const loadedUnreadyCount = activeRegionsForReview.filter(
      (region) => !region.confirmed || region.trustDisposition !== 'trusted',
    ).length;
    const serverPendingCount = Math.max(0, Number(image.trustReviewCount ?? 0));
    const unreadyCount = Math.max(serverPendingCount, loadedUnreadyCount);
    if (
      nextReviewState === 'no-text-reviewed'
      && (activeRegionsForReview.length > 0 || serverPendingCount > 0)
    ) {
      set({ globalError: '当前页仍有活动文本框，不能标记为“确认无文字”。' });
      return false;
    }
    if (nextReviewState === 'reviewed' && unreadyCount > 0) {
      set({ globalError: `还有 ${unreadyCount} 个活动文本框尚未确认并信任。` });
      return false;
    }
    if (nextReviewState === 'reviewed' && activeRegionsForReview.length === 0) {
      set({ globalError: '当前页没有活动文本框，请使用“确认无文字”。' });
      return false;
    }
    set({ globalError: '' });
    try {
      const response = await api.reviewImage(image.id, nextReviewState, image.revision);
      const merged = hydrateImage({
        ...image,
        ...response,
        status: { ...image.status, ...(response.status ?? {}) },
      }, state.currentProject?.settings);
      set((currentState) => ({
        images: currentState.images.map((entry) =>
          entry.id === merged.id && entry.revision <= merged.revision ? merged : entry,
        ),
      }));
      return true;
    } catch (error) {
      set({ globalError: errorMessage(error) });
      return false;
    }
  },

  reviewActiveImageStage: async (stage, nextState, observation) => {
    if (!(await get().flushAutosave())) return false;
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    if (nextState !== 'pending') {
      const observationMatches = observation
        && observation.imageId === image.id
        && observation.stage === stage
        && observation.revision === image.revision
        && Boolean(observation.artifactChecksum)
        && (stage !== 'inpaint' || Boolean(observation.maskChecksum));
      if (!observationMatches) {
        set({
          globalError: '画布中的复核文件已过期或尚未完成校验，请重新加载当前阶段后再复核。',
        });
        return false;
      }
    }
    const mutationKey = `${image.id}:${stage}`;
    if (state.stageReviewSaving !== null) return false;
    set({ globalError: '', revisionConflict: false, stageReviewSaving: mutationKey });
    try {
      const response = await api.reviewImageStage(
        image.id,
        stage,
        nextState,
        image.revision,
        nextState === 'pending' ? undefined : observation,
      );
      const merged = hydrateImage({
        ...image,
        ...response,
        status: { ...image.status, ...(response.status ?? {}) },
        stageReviews: response.stageReviews ?? {},
      }, state.currentProject?.settings);
      set((currentState) => ({
        images: currentState.images.map((entry) =>
          entry.id === merged.id && entry.revision <= merged.revision ? merged : entry,
        ),
      }));
      return true;
    } catch (error) {
      set({
        globalError: errorMessage(error),
        revisionConflict: error instanceof ApiError && error.status === 409,
      });
      return false;
    } finally {
      set((currentState) => ({
        stageReviewSaving: currentState.stageReviewSaving === mutationKey
          ? null
          : currentState.stageReviewSaving,
      }));
    }
  },

  selectInpaintCandidate: async (candidateId) => {
    if (!(await get().flushAutosave())) return false;
    const state = get();
    const image = activeImage(state);
    if (!image || image.inpaintCandidate === candidateId) return false;
    const mutationKey = `${image.id}:inpaint-candidate`;
    if (state.stageReviewSaving !== null) return false;
    set({ globalError: '', revisionConflict: false, stageReviewSaving: mutationKey });
    try {
      const response = await api.selectInpaintCandidate(image.id, candidateId, image.revision);
      const merged = hydrateImage({
        ...image,
        ...response,
        status: { ...image.status, ...(response.status ?? {}) },
        stageReviews: response.stageReviews ?? {},
        inpaintCandidate: response.inpaintCandidate,
        inpaintCandidates: response.inpaintCandidates ?? image.inpaintCandidates,
      }, state.currentProject?.settings);
      set((currentState) => ({
        images: currentState.images.map((entry) =>
          entry.id === merged.id && entry.revision <= merged.revision ? merged : entry,
        ),
      }));
      return true;
    } catch (error) {
      set({
        globalError: errorMessage(error),
        revisionConflict: error instanceof ApiError && error.status === 409,
      });
      return false;
    } finally {
      set((currentState) => ({
        stageReviewSaving: currentState.stageReviewSaving === mutationKey
          ? null
          : currentState.stageReviewSaving,
      }));
    }
  },

  setCanvasMode: (canvasMode) => set({ canvasMode }),
  setCanvasTool: (canvasTool) => set({ canvasTool }),
  toggleCompareMode: () => set((state) => ({
    compareMode: hasGeneratedPreview(activeImage(state)) ? !state.compareMode : false,
  })),
  setShowRegions: (showRegions) => set({ showRegions }),
  setShowOrder: (showOrder) => set({ showOrder }),
  setShowConfidence: (showConfidence) => set({ showConfidence }),
  setShowMask: (showMask) => set({ showMask }),
  setMaskBrushRadius: (maskBrushRadius) => set({
    maskBrushRadius: Math.max(1, Math.min(200, Math.round(maskBrushRadius))),
  }),
  requestFit: () => set((state) => ({ fitRequest: state.fitRequest + 1 })),
  setRightTab: (rightTab) => set({ rightTab }),
  setTheme: (theme) => {
    try {
      window.localStorage?.setItem('manga-localizer-theme', theme);
    } catch {
      // Theme persistence is optional in privacy-restricted browser contexts.
    }
    set({ theme });
  },
  setDrawerOpen: (drawerOpen) => set({ drawerOpen }),
  setShortcutsOpen: (shortcutsOpen) => set({ shortcutsOpen }),
  setSpacePressed: (spacePressed) => set({ spacePressed }),

  startBatch: async (kinds, imageIds, exportOptions, concurrency = 1, regionIds, preprocessing) => {
    if (!get().currentProject || !imageIds.length || !kinds.length) return false;
    const hasOcr = kinds.includes('ocr');
    const trustGatedKinds = kinds.filter((kind) =>
      kind === 'translate' || kind === 'inpaint' || kind === 'typeset'
    );
    if (hasOcr && trustGatedKinds.length) {
      set({
        globalError: 'OCR 与翻译、擦字修复或嵌字排版不能放在同一批次；请先完成 OCR 并人工确认文本框。',
      });
      return false;
    }
    if (trustGatedKinds.length) {
      const pending = pendingTrustCount(get(), imageIds, regionIds);
      if (pending > 0) {
        set({
          globalError: `所选范围还有 ${pending} 个 OCR 文本框待信任确认，不能开始翻译或安全图像处理。`,
        });
        return false;
      }
    }
    if (!(await get().flushAutosave())) return false;
    if (trustGatedKinds.length) {
      const projectAfterSave = get().currentProject;
      if (!projectAfterSave) return false;
      try {
        await synchronizeImages(projectAfterSave.id);
      } catch (error) {
        set({
          globalError: `无法刷新服务端信任状态，未创建下游任务：${errorMessage(error)}`,
        });
        return false;
      }
      if (regionIds?.length) {
        const refreshed = await Promise.all(
          imageIds.map((imageId) => get().loadRegions(imageId, true)),
        );
        if (refreshed.some((loaded) => !loaded)) {
          set({ globalError: '无法刷新所选文本框的服务端信任状态，未创建下游任务。' });
          return false;
        }
      }
      const pending = regionIds?.length
        ? pendingTrustCount(get(), imageIds, regionIds)
        : imageIds.reduce((total, imageId) => {
            const image = get().images.find((entry) => entry.id === imageId);
            return total + Math.max(0, Number(image?.trustReviewCount ?? 0));
          }, 0);
      if (pending > 0) {
        set({
          globalError: `保存后所选范围有 ${pending} 个 OCR 文本框需要重新确认，未创建下游任务。`,
        });
        return false;
      }
    }
    const project = get().currentProject;
    if (!project) return false;
    set({ globalError: '' });
    try {
      const created: Job[] = [];
      const operationOrder: JobKind[] = [
        'preprocess',
        'detect',
        'ocr',
        'translate',
        'inpaint',
        'typeset',
        'export',
      ];
      const orderedKinds = operationOrder.filter((kind) => kinds.includes(kind));
      for (const kind of orderedKinds) {
        const options: Record<string, unknown> = kind === 'preprocess'
          ? {
              provider: project.settings.preprocessorProvider,
              preprocessing: preprocessing ?? project.settings.preprocessing,
            }
          : kind === 'detect'
          ? {
              provider: project.settings.detectorProvider,
              direction: 'auto',
            }
          : kind === 'ocr'
            ? {
                provider: project.settings.ocrProvider,
                direction: 'auto',
              }
          : kind === 'translate'
            ? {
                provider: project.settings.translatorProvider,
                glossary: parseKeyValueLines(project.settings.glossary),
                characterNames: parseKeyValueLines(project.settings.characterNames),
                contextPages: project.settings.contextPages,
                targetLanguage: project.settings.targetLanguage,
                baseUrl: project.settings.remoteEndpoint || undefined,
                model: project.settings.remoteModel || undefined,
              }
            : kind === 'inpaint'
              ? { provider: project.settings.inpainterProvider, repairPolicy: 'safe' }
              : kind === 'typeset'
                ? { provider: 'pillow', repairPolicy: 'safe' }
                : {};
        const request = {
          imageIds,
          ...(regionIds?.length ? { regionIds } : {}),
          options: { ...options, concurrency },
        };
        const job = kind === 'export'
          ? await api.exportProject(project.id, {
              ...request,
              options: { ...exportOptions, concurrency: 1 },
            })
          : await api.startJob(project.id, kind, request);
        const hydrated = hydrateJob(job);
        created.push(hydrated);
        set((state) => ({
          jobs: [hydrated, ...state.jobs.filter((entry) => entry.id !== hydrated.id)],
          images: state.images.map((image) => {
            if (!imageIds.includes(image.id)) return image;
            const status = { ...image.status };
            if (kind === 'preprocess') status.preprocess = 'queued';
            else if (kind === 'detect') status.detection = 'queued';
            else if (kind === 'ocr') status.ocr = 'queued';
            else if (kind === 'translate') status.translation = 'queued';
            else if (kind === 'inpaint') status.inpaint = 'queued';
            else if (kind === 'typeset') status.typeset = 'queued';
            else status.export = 'queued';
            return {
              ...image,
              status,
              preprocessingProvider: kind === 'preprocess'
                ? project.settings.preprocessorProvider
                : image.preprocessingProvider,
              detectorProvider: kind === 'detect'
                ? project.settings.detectorProvider
                : image.detectorProvider,
              ocrProvider: kind === 'ocr' ? project.settings.ocrProvider : image.ocrProvider,
              translatorProvider: kind === 'translate'
                ? project.settings.translatorProvider
                : image.translatorProvider,
              inpaintingProvider: kind === 'inpaint'
                ? project.settings.inpainterProvider
                : image.inpaintingProvider,
              typesettingProvider: kind === 'typeset' ? 'pillow' : image.typesettingProvider,
            };
          }),
        }));
      }
      set((state) => ({
        jobs: [...created, ...state.jobs.filter((job) => !created.some((entry) => entry.id === job.id))],
        images: state.images.map((image) => {
          if (!imageIds.includes(image.id)) return image;
          const status = { ...image.status };
          for (const kind of orderedKinds) {
            if (kind === 'preprocess') status.preprocess = 'queued';
            else if (kind === 'detect') status.detection = 'queued';
            else if (kind === 'ocr') status.ocr = 'queued';
            else if (kind === 'translate') status.translation = 'queued';
            else if (kind === 'inpaint') status.inpaint = 'queued';
            else if (kind === 'typeset') status.typeset = 'queued';
            else status.export = 'queued';
          }
          return {
            ...image,
            status,
            preprocessingProvider: orderedKinds.includes('preprocess') ? project.settings.preprocessorProvider : image.preprocessingProvider,
            detectorProvider: orderedKinds.includes('detect') ? project.settings.detectorProvider : image.detectorProvider,
            ocrProvider: orderedKinds.includes('ocr') ? project.settings.ocrProvider : image.ocrProvider,
            translatorProvider: orderedKinds.includes('translate') ? project.settings.translatorProvider : image.translatorProvider,
            inpaintingProvider: orderedKinds.includes('inpaint') ? project.settings.inpainterProvider : image.inpaintingProvider,
            typesettingProvider: orderedKinds.includes('typeset') ? 'pillow' : image.typesettingProvider,
          };
        }),
      }));
      await synchronizeProject(project.id);
      return true;
    } catch (error) {
      set({ globalError: errorMessage(error) });
      return false;
    }
  },

  refreshJobs: async () => {
    const project = get().currentProject;
    if (!project) return;
    try {
      const [jobResponse, imageResponse, projectResponse] = await Promise.all([
        api.listJobs(project.id),
        api.listImages(project.id),
        api.getProject(project.id),
      ]);
      const jobs = jobResponse.map(hydrateJob);
      const previousJobs = get().jobs;
      const newlyCompleted = jobs.some(
        (job) => job.status === 'completed'
          && previousJobs.find((previous) => previous.id === job.id)?.status !== 'completed',
      );
      const newlyCompletedImageIds = (kind: JobKind) => new Set(
        jobs.flatMap((job) => {
          if (job.kind !== kind || job.status !== 'completed') return [];
          const previous = previousJobs.find((entry) => entry.id === job.id);
          if (!previous || previous.status === 'completed') return [];
          return job.items
            .map((item) => item.imageId)
            .filter((imageId): imageId is string => Boolean(imageId));
        }),
      );
      const completedTypesetImageIds = newlyCompletedImageIds('typeset');
      const completedInpaintImageIds = newlyCompletedImageIds('inpaint');
      const completedPreprocessImageIds = newlyCompletedImageIds('preprocess');
      const refreshedImages = imageResponse.map((image) =>
        hydrateImage(image, project.settings),
      );
      const projectSnapshot = hydrateProject(projectResponse);
      set((state) => ({
        ...mergeProjectSnapshot(state, projectSnapshot),
        jobs,
        images: refreshedImages.map((image) => {
          const previous = state.images.find((entry) => entry.id === image.id);
          if (previous && previous.revision > image.revision) return previous;
          return {
            ...image,
            preprocessingProvider: image.preprocessingProvider ?? previous?.preprocessingProvider,
            detectorProvider: image.detectorProvider ?? previous?.detectorProvider,
            ocrProvider: image.ocrProvider ?? previous?.ocrProvider,
            translatorProvider: image.translatorProvider ?? previous?.translatorProvider,
            inpaintingProvider: image.inpaintingProvider ?? previous?.inpaintingProvider,
            typesettingProvider: image.typesettingProvider ?? previous?.typesettingProvider,
          };
        }),
      }));
      const activeImageId = get().activeImageId;
      const hasPendingActiveEdits = activeImageId
        ? get().pendingRegionMutations.some((mutation) => mutation.imageId === activeImageId)
        : false;
      if (
        activeImageId
        && !hasPendingActiveEdits
        && newlyCompleted
      ) {
        await get().loadRegions(activeImageId, true);
      }
      if (activeImageId && completedTypesetImageIds.has(activeImageId)) {
        const image = get().images.find((entry) => entry.id === activeImageId);
        if (image?.status.typeset === 'done') {
          const regions = get().regionsByImage[activeImageId] ?? [];
          const overlayIds = overlayRegionIdsFromCompletedTypeset(
            jobs,
            previousJobs,
            activeImageId,
            regions,
          );
          const overflowIds = overflowingRegionIds(image, regions);
          const focusIds = overlayIds.length ? overlayIds : overflowIds;
          set({
            canvasMode: 'typeset',
            compareMode: true,
            ...(focusIds.length
              ? { selectedRegionIds: focusIds, rightTab: 'typesetting' as const }
              : {}),
          });
        }
      } else if (
        activeImageId
        && completedInpaintImageIds.has(activeImageId)
        && get().images.find((entry) => entry.id === activeImageId)?.status.inpaint === 'done'
      ) {
        set({ canvasMode: 'erased', showMask: true, rightTab: 'repair', compareMode: true });
      } else if (
        activeImageId
        && completedPreprocessImageIds.has(activeImageId)
        && get().images.find((entry) => entry.id === activeImageId)?.status.preprocess === 'done'
      ) {
        set({ canvasMode: 'preprocessed', compareMode: true });
      }
    } catch (error) {
      set({ globalError: errorMessage(error) });
    }
  },

  runJobAction: async (jobId, action) => {
    try {
      const updated = hydrateJob(await api.jobAction(jobId, action));
      set((state) => ({
        jobs: state.jobs.map((job) => (job.id === updated.id ? updated : job)),
      }));
      const projectId = get().currentProject?.id;
      if (projectId) await synchronizeProject(projectId);
    } catch (error) {
      set({ globalError: errorMessage(error) });
    }
  },

  dismissError: () => set({ globalError: '' }),
}));

export function resetWorkbenchStore(): void {
  if (autosaveTimer) clearTimeout(autosaveTimer);
  autosaveTimer = null;
  activeSave = null;
  inFlightRegionMutationIds.clear();
  regionLoadTokens.clear();
  savedRegionIdAliases.clear();
  useWorkbenchStore.setState({ ...initialUiState, theme: 'dark' });
}

export function activeImage(state: WorkbenchState): ImageAsset | null {
  return state.images.find((image) => image.id === state.activeImageId) ?? null;
}

export function imageHasTypesetOverflow(image: ImageAsset | null | undefined): boolean {
  return Boolean(
    image
    && image.status.typeset === 'done'
    && (image.typesetOverflowCount ?? 0) > 0,
  );
}

export function imagePageReviewPending(image: ImageAsset | null | undefined): boolean {
  const state = image?.status.reviewState;
  return Boolean(image) && state !== 'reviewed' && state !== 'no-text-reviewed';
}

export function regionHasTypesetOverflow(
  image: ImageAsset | null | undefined,
  regionId: string,
): boolean {
  return Boolean(
    imageHasTypesetOverflow(image)
    && image?.typesetOverflowRegionIds?.includes(regionId),
  );
}

export function overflowingRegionIds(
  image: ImageAsset | null | undefined,
  regions: Region[],
): string[] {
  if (!imageHasTypesetOverflow(image) || !image) return [];
  const present = new Set(regions.map((region) => region.id));
  return image.typesetOverflowRegionIds.filter((regionId) => present.has(regionId));
}

function overlayRegionIdsFromCompletedTypeset(
  jobs: Job[],
  previousJobs: Job[],
  imageId: string,
  regions: Region[],
): string[] {
  const present = new Set(regions.map((region) => region.id));
  const selected: string[] = [];
  for (const job of jobs) {
    if (job.kind !== 'typeset' || job.status !== 'completed') continue;
    const previous = previousJobs.find((entry) => entry.id === job.id);
    if (!previous || previous.status === 'completed') continue;
    for (const item of job.items) {
      if (item.imageId !== imageId || item.output?.partialTypeset !== true) continue;
      const overlayIds = item.output.overlayRegionIds;
      if (!Array.isArray(overlayIds)) continue;
      for (const regionId of overlayIds) {
        if (typeof regionId === 'string' && present.has(regionId)) selected.push(regionId);
      }
    }
  }
  return [...new Set(selected)];
}

export function hasGeneratedPreview(image: ImageAsset | null | undefined): boolean {
  return Boolean(
    image
      && (
        image.status.preprocess === 'done'
        || image.status.inpaint === 'done'
        || image.status.typeset === 'done'
      ),
  );
}

const STABLE_EMPTY_REGIONS: Region[] = [];

export function activeRegions(state: WorkbenchState): Region[] {
  return state.activeImageId
    ? state.regionsByImage[state.activeImageId] ?? STABLE_EMPTY_REGIONS
    : STABLE_EMPTY_REGIONS;
}

export function selectedRegions(state: WorkbenchState): Region[] {
  const ids = new Set(state.selectedRegionIds);
  return activeRegions(state).filter((region) => ids.has(region.id));
}

export function hasPendingChanges(state: WorkbenchState): boolean {
  return Boolean(state.pendingProjectMutation || state.pendingRegionMutations.length);
}

export function providerAvailable(
  state: WorkbenchState,
  kind: AppCapabilities['providers'][number]['kind'],
  providerId: string | undefined,
): boolean {
  if (!providerId) return false;
  return Boolean(
    state.capabilities.providers.find(
      (provider) => provider.kind === kind && provider.id === providerId && provider.available,
    ),
  );
}
