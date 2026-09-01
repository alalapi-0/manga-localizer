import { create } from 'zustand';

import { ApiError, api } from '../api/client';
import type { G4RegionPatch } from '../api/client';
import type {
  AppCapabilities,
  BackgroundCategory,
  BackgroundGateContext,
  BackgroundRationaleCode,
  CanvasMode,
  CanvasTool,
  CleanPlateBitmapObservation,
  CleanPlateCheck,
  CleanPlateGateContext,
  CleanPlateReviewReason,
  ExportOptions,
  ImageAsset,
  Job,
  JobKind,
  LineageActor,
  MutationLineageContext,
  MaskCheckResult,
  MaskCollateralCheck,
  MaskCoverageCheck,
  MaskDraftRegion,
  MaskEditStroke,
  MaskGateContext,
  MaskGateReview,
  OCRGateContext,
  OCRQCCheck,
  OCRSourceMode,
  PageGeneration,
  PageLineageEvent,
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
  TranslationCandidate,
  TranslationGateContext,
  TranslationOriginKind,
  TranslationQCCheck,
  TranslationQCFlag,
  TranslationReviewReason,
  TypesetBitmapObservation,
  TypesetCandidate,
  TypesetCheck,
  TypesetGateContext,
  TypesetRoute,
  TypesetRegionStyle,
  TypesetRegionStyleInput,
  TypesetReviewReason,
} from '../types';
import {
  DEFAULT_PROJECT_SETTINGS,
  DEFAULT_REGION_STYLE,
  DEFAULT_REPAIR_SETTINGS,
  EMPTY_PIPELINE_STATUS,
  CLEAN_PLATE_CHECKS,
  OCR_QC_CHECKS,
  MASK_COLLATERAL_CHECKS,
  MASK_COVERAGE_CHECKS,
  TRANSLATION_QC_CHECKS,
  TYPESET_CHECKS,
} from '../types';
import { clusterRegionIds, expandRegionGeometry } from '../components/canvasGeometry';

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

function canonicalJson(value: unknown): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) return `[${value.map((entry) => canonicalJson(entry)).join(',')}]`;
  if (typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) return JSON.stringify(value);
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, entry]) => entry !== undefined)
      .sort(([left], [right]) => left.localeCompare(right));
    return `{${entries.map(([key, entry]) => `${JSON.stringify(key)}:${canonicalJson(entry)}`).join(',')}}`;
  }
  throw new Error('无法规范化 G7 蒙版配方');
}

function pythonFloatJson(value: number): string {
  if (Object.is(value, -0)) return '-0.0';
  if (Number.isInteger(value)) return `${value}.0`;
  const absolute = Math.abs(value);
  let encoded = absolute > 0 && absolute < 1e-4 ? value.toExponential() : value.toString();
  if (encoded.includes('e')) {
    const [mantissa, rawExponent] = encoded.split('e');
    const exponent = Number(rawExponent);
    encoded = `${mantissa}e${exponent >= 0 ? '+' : '-'}${Math.abs(exponent).toString().padStart(2, '0')}`;
  }
  return encoded;
}

function pythonCanonicalJson(
  value: unknown,
  floatKeys: ReadonlySet<string>,
  currentKey: string | null = null,
): string {
  if (value === null) return 'null';
  if (Array.isArray(value)) {
    return `[${value.map((entry) => pythonCanonicalJson(entry, floatKeys, currentKey)).join(',')}]`;
  }
  if (typeof value === 'string' || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number' && Number.isFinite(value)) {
    return currentKey && floatKeys.has(currentKey) ? pythonFloatJson(value) : JSON.stringify(value);
  }
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, entry]) => entry !== undefined)
      .sort(([left], [right]) => left < right ? -1 : left > right ? 1 : 0);
    return `{${entries.map(([key, entry]) =>
      `${JSON.stringify(key)}:${pythonCanonicalJson(entry, floatKeys, key)}`).join(',')}}`;
  }
  throw new Error('无法规范化严格服务端证据');
}

async function canonicalSha256(
  value: unknown,
  pythonFloatKeys?: ReadonlySet<string>,
): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) throw new Error('当前浏览器不支持严格证据校验');
  const canonical = pythonFloatKeys
    ? pythonCanonicalJson(value, pythonFloatKeys)
    : canonicalJson(value);
  const bytes = new TextEncoder().encode(canonical);
  const digest = await subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (entry) => entry.toString(16).padStart(2, '0')).join('');
}

const TYPESET_PYTHON_FLOAT_KEYS = new Set([
  'rotation', 'scaleX', 'scaleY', 'shearX', 'shearY', 'opacity',
  'visualCenterX', 'visualCenterY', 'lineSpacing', 'letterSpacing',
]);

function float64Token(value: number): string {
  const bytes = new ArrayBuffer(8);
  new DataView(bytes).setFloat64(0, value, false);
  return Array.from(new Uint8Array(bytes), (entry) => entry.toString(16).padStart(2, '0')).join('');
}

export function g7MaskDraftChecksum(
  parentChecksum: string,
  qualityChecksum: string,
  rubyRegionIdsByPrimary: Record<string, string[]>,
  regions: MaskDraftRegion[],
): Promise<string> {
  const digestRegions = regions.map((region) => ({
    regionId: region.regionId,
    maskMode: region.maskMode,
    polygon: region.polygon == null
      ? null
      : region.polygon.map((point) => [float64Token(point[0]), float64Token(point[1])]),
    padding: region.padding,
    dilation: region.dilation,
    feather: region.feather,
    polarity: region.polarity,
    maskEdits: {
      version: region.maskEdits.version,
      strokes: region.maskEdits.strokes.map((stroke) => ({
        mode: stroke.mode,
        radius: float64Token(stroke.radius),
        points: stroke.points.map((point) => [float64Token(point[0]), float64Token(point[1])]),
      })),
    },
  }));
  return canonicalSha256({
    parentChecksum,
    qualityChecksum,
    rubyRegionIdsByPrimary,
    regions: digestRegions,
  });
}

export const BACKGROUND_CATEGORIES: BackgroundCategory[] = [
  'white-solid',
  'black-solid',
  'other-solid',
  'simple-gradient',
  'screentone',
  'complex-lineart',
  'illustration/character',
];
export const BACKGROUND_RATIONALE_CODES: BackgroundRationaleCode[] = [
  'uniform-near-white',
  'uniform-near-black',
  'uniform-other-color',
  'smooth-gradient-continuity',
  'periodic-screentone',
  'structural-lines-cross-region',
  'character-or-illustration-detail',
  'mixed-visual-signals',
];
export const BACKGROUND_RATIONALE_ANCHOR: Record<
  BackgroundCategory,
  BackgroundRationaleCode
> = {
  'white-solid': 'uniform-near-white',
  'black-solid': 'uniform-near-black',
  'other-solid': 'uniform-other-color',
  'simple-gradient': 'smooth-gradient-continuity',
  screentone: 'periodic-screentone',
  'complex-lineart': 'structural-lines-cross-region',
  'illustration/character': 'character-or-illustration-detail',
};

interface HistoryFrame {
  regionsByImage: Record<string, Region[]>;
}

interface RegionMutation {
  mutationId: string;
  kind: 'create' | 'update' | 'confirm' | 'delete';
  imageId: string;
  region: Region;
  patch?: RegionUpdatePatch;
  expectedRevision: number;
}

interface ProjectMutation {
  mutationId: string;
  settings: ProjectSettings;
  expectedRevision: number;
}

export interface G4PageContext {
  status: 'loading' | 'legacy' | 'active' | 'error';
  generation: PageGeneration | null;
  events: PageLineageEvent[];
  phase?: WorkflowPhase;
  error: string;
  conflict: boolean;
}

export type WorkflowPhase = 'G4' | 'G5' | 'G6' | 'G7' | 'G8' | 'G9' | 'G10' | 'no-text' | 'locked';

export interface MaskBitmapObservation {
  imageId: string;
  artifactId: string;
  imageRevision: number;
  checksum: string;
  width: number;
  height: number;
  state: 'ready';
}

interface G4RegionMutation {
  mutationId: string;
  kind: 'create' | 'update' | 'delete';
  imageId: string;
  region: Region;
  patch?: G4RegionPatch;
  expectedRevision: number;
}

type NestedRegionPatch<T> = {
  [Key in keyof T]?: T[Key] | null;
};

type RegionUpdatePatch = Omit<Partial<Region>, 'repair' | 'style'> & {
  repair?: NestedRegionPatch<Region['repair']>;
  style?: NestedRegionPatch<Region['style']>;
};

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
  focusRequest: number;
  focusRegionIds: string[];
  rightTab: RightPanelTab;
  theme: Theme;
  drawerOpen: boolean;
  queueRevealJobId: string | null;
  queueRevealItemId: string | null;
  shortcutsOpen: boolean;
  spacePressed: boolean;
  jobs: Job[];
  g4Contexts: Record<string, G4PageContext>;
  backgroundContexts: Record<string, BackgroundGateContext>;
  backgroundLoading: Record<string, boolean>;
  ocrContexts: Record<string, OCRGateContext>;
  ocrLoading: Record<string, boolean>;
  maskContexts: Record<string, MaskGateContext>;
  maskLoading: Record<string, boolean>;
  selectedMaskArtifactIds: Record<string, string>;
  maskBitmapObservations: Record<string, MaskBitmapObservation>;
  cleanPlateContexts: Record<string, CleanPlateGateContext>;
  cleanPlateLoading: Record<string, boolean>;
  selectedCleanPlateCandidateIds: Record<string, string>;
  cleanPlateBitmapObservations: Record<string, CleanPlateBitmapObservation>;
  translationContexts: Record<string, TranslationGateContext>;
  translationLoading: Record<string, boolean>;
  selectedTranslationCandidateIds: Record<string, string>;
  typesetContexts: Record<string, TypesetGateContext>;
  typesetLoading: Record<string, boolean>;
  selectedTypesetCandidateIds: Record<string, string>;
  typesetBitmapObservations: Record<string, TypesetBitmapObservation>;
  typesetStyleDrafts: Record<string, Record<string, TypesetRegionStyleInput>>;
  pendingG4Mutations: G4RegionMutation[];
  g4SavingImageId: string | null;
  g4GateSavingImageId: string | null;
  g5SavingRegionId: string | null;
  g5GateSavingImageId: string | null;
  g6SavingRegionId: string | null;
  g6GateSavingImageId: string | null;
  g7DraftSavingImageId: string | null;
  g7GateSavingImageId: string | null;
  g8GateSavingImageId: string | null;
  g9GateSavingImageId: string | null;
  g10GateSavingImageId: string | null;
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
  loadG4Context: (imageId: string, force?: boolean) => Promise<boolean>;
  loadBackgroundContext: (imageId: string, force?: boolean) => Promise<boolean>;
  loadOCRContext: (imageId: string, force?: boolean) => Promise<boolean>;
  loadMaskContext: (imageId: string, force?: boolean) => Promise<boolean>;
  loadCleanPlateContext: (imageId: string, force?: boolean) => Promise<boolean>;
  loadTranslationContext: (imageId: string, force?: boolean) => Promise<boolean>;
  loadTypesetContext: (imageId: string, force?: boolean) => Promise<boolean>;
  reloadActiveImage: () => Promise<void>;
  selectImage: (imageId: string, options?: { focusOverflow?: boolean; focusFailure?: boolean }) => Promise<boolean>;
  navigateImage: (direction: -1 | 1, target?: ImageNavigationTarget) => Promise<boolean>;
  toggleImageSelection: (imageId: string, additive?: boolean) => void;
  selectAllVisibleImages: (imageIds: string[]) => void;
  clearImageSelection: () => void;
  setImageSearch: (value: string) => void;
  setImageFilter: (value: WorkbenchState['imageFilter']) => void;
  selectRegion: (regionId: string, additive?: boolean) => void;
  clearRegionSelection: () => void;
  createRegion: (geometry: Pick<Region, 'x' | 'y' | 'width' | 'height'>) => string | null;
  updateRegion: (regionId: string, patch: RegionUpdatePatch, recordHistory?: boolean) => void;
  nudgeSelectedRegions: (dx: number, dy: number) => void;
  setRegionConfirmed: (regionId: string, confirmed: boolean) => Promise<boolean>;
  deleteSelectedRegions: () => void;
  mergeSelectedRegions: () => void;
  consolidateActiveImageRegions: () => number;
  splitSelectedRegion: (axis: 'horizontal' | 'vertical') => void;
  moveG4Region: (regionId: string, direction: -1 | 1) => Promise<boolean>;
  startG4Detection: () => Promise<boolean>;
  acceptG4Regions: () => Promise<boolean>;
  saveG5Background: (
    regionId: string,
    category: BackgroundCategory,
    confidence: number,
    rationaleCodes: BackgroundRationaleCode[],
  ) => Promise<boolean>;
  acceptG5Background: () => Promise<boolean>;
  startG6OCR: () => Promise<boolean>;
  saveG6SourceReview: (
    regionId: string,
    sourceText: string,
    sourceMode: OCRSourceMode,
    selectedAttemptId: string,
    qcChecks: OCRQCCheck[],
  ) => Promise<boolean>;
  acceptG6OCR: () => Promise<boolean>;
  saveG7MaskDraft: (regions: MaskDraftRegion[]) => Promise<boolean>;
  appendG7MaskStroke: (regionId: string, stroke: MaskEditStroke) => Promise<boolean>;
  startG7Mask: () => Promise<boolean>;
  selectG7MaskArtifact: (artifactId: string) => void;
  observeG7MaskBitmap: (observation: MaskBitmapObservation | null) => void;
  reviewG7Mask: (
    decision: 'accept' | 'reject' | 'not-applicable',
    coverageChecks: Array<MaskCheckResult<MaskCoverageCheck>>,
    collateralChecks: Array<MaskCheckResult<MaskCollateralCheck>>,
  ) => Promise<boolean>;
  startG8CleanPlate: (classicalFallback?: boolean) => Promise<boolean>;
  selectG8CleanPlateCandidate: (candidateId: string) => void;
  observeG8CleanPlateBitmap: (observation: CleanPlateBitmapObservation | null) => void;
  reviewG8CleanPlate: (
    decision: 'accept' | 'reject' | 'not-applicable',
    checks: Array<MaskCheckResult<CleanPlateCheck>>,
  ) => Promise<boolean>;
  setG8ClassicalFallback: (enabled: boolean) => Promise<boolean>;
  startG9Translation: (remoteAuthorized?: boolean) => Promise<boolean>;
  selectG9TranslationCandidate: (candidateId: string) => void;
  reviseG9Translation: (
    regionId: string,
    translationText: string,
    originKind: Exclude<TranslationOriginKind, 'model'>,
  ) => Promise<boolean>;
  reviewG9TranslationCandidate: (
    candidateId: string,
    decision: 'accept' | 'reject',
    checks: Array<MaskCheckResult<TranslationQCCheck>>,
    qcFlags: TranslationQCFlag[],
    reason: TranslationReviewReason,
  ) => Promise<boolean>;
  acceptG9Translation: () => Promise<boolean>;
  setG10RegionStyle: (regionId: string, style: TypesetRegionStyleInput) => void;
  startG10Typeset: (regionStyles?: Record<string, TypesetRegionStyleInput>) => Promise<boolean>;
  selectG10TypesetCandidate: (candidateId: string) => void;
  observeG10TypesetBitmap: (observation: TypesetBitmapObservation | null) => void;
  reviewG10TypesetCandidate: (
    candidateId: string,
    decision: 'accept' | 'reject',
    checks: Array<MaskCheckResult<TypesetCheck>>,
    reason: TypesetReviewReason,
    touchedChecks: TypesetCheck[],
  ) => Promise<boolean>;
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
  reviewSelectedInpaintAiCandidate: (state: 'rejected' | 'pending') => Promise<boolean>;
  setActiveImageInpaintFallback: (
    state: 'approved' | 'pending',
    options?: {
      reason?: 'ai-visible-artifacts';
    },
  ) => Promise<boolean>;
  setCanvasMode: (mode: CanvasMode) => void;
  setCanvasTool: (tool: CanvasTool) => void;
  toggleCompareMode: () => void;
  setShowRegions: (value: boolean) => void;
  setShowOrder: (value: boolean) => void;
  setShowConfidence: (value: boolean) => void;
  setShowMask: (value: boolean) => void;
  setMaskBrushRadius: (value: number) => void;
  requestFit: () => void;
  focusRegions: (regionIds: string[]) => void;
  focusSelectedRegions: () => void;
  focusActiveOverflow: () => void;
  focusActiveFailure: () => void;
  setRightTab: (tab: RightPanelTab) => void;
  setTheme: (theme: Theme) => void;
  setDrawerOpen: (value: boolean) => void;
  openQueueForImage: (imageId: string, kind?: JobKind | null) => void;
  setShortcutsOpen: (value: boolean) => void;
  setSpacePressed: (value: boolean) => void;
  startBatch: (
    kinds: JobKind[],
    imageIds: string[],
    exportOptions: ExportOptions,
    concurrency?: number,
    regionIds?: string[],
    preprocessing?: PreprocessingSettings,
    provider?: string,
  ) => Promise<boolean>;
  refreshJobs: () => Promise<void>;
  runJobAction: (jobId: string, action: 'pause' | 'resume' | 'cancel' | 'retry') => Promise<void>;
  openJobItem: (jobId: string, itemId: string) => Promise<boolean>;
  dismissError: () => void;
}

const emptyCapabilities: AppCapabilities = { providers: [] };
let autosaveTimer: ReturnType<typeof setTimeout> | null = null;
let activeSave: Promise<boolean> | null = null;
const inFlightRegionIds = new Set<string>();
const regionLoadTokens = new Map<string, symbol>();
const g4LoadTokens = new Map<string, symbol>();
const backgroundLoadTokens = new Map<string, symbol>();
const ocrLoadTokens = new Map<string, symbol>();
const maskLoadTokens = new Map<string, symbol>();
const cleanPlateLoadTokens = new Map<string, symbol>();
const translationLoadTokens = new Map<string, symbol>();
const typesetLoadTokens = new Map<string, symbol>();
const savedRegionIdAliases = new Map<string, string>();
let fallbackG4SessionId: string | null = null;

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

function uiLineageActor(): LineageActor {
  if (!fallbackG4SessionId) fallbackG4SessionId = id('ui-session');
  let sessionId = fallbackG4SessionId;
  try {
    if (typeof window !== 'undefined' && window.sessionStorage) {
      const key = 'manga-localizer-lineage-session';
      sessionId = window.sessionStorage.getItem(key) || fallbackG4SessionId;
      if (!window.sessionStorage.getItem(key)) window.sessionStorage.setItem(key, sessionId);
    }
  } catch {
    sessionId = fallbackG4SessionId;
  }
  return {
    actorKind: 'human',
    sessionId,
    operationSource: 'ui',
  };
}

function mutationLineage(context: G4PageContext): MutationLineageContext | null {
  if (context.status !== 'active' || !context.generation) return null;
  return {
    runId: context.generation.runId,
    pageGenerationId: context.generation.id,
    expectedSequence: context.generation.nextSequence,
    actor: uiLineageActor(),
  };
}

function deriveG10Phase(events: PageLineageEvent[], g9TerminalChecksum: string): WorkflowPhase {
  const sha256 = /^[0-9a-f]{64}$/;
  const exactKeys = (value: Record<string, unknown>, keys: readonly string[]) =>
    Object.keys(value).sort().join('\0') === [...keys].sort().join('\0');
  const containsPrivateText = (value: unknown): boolean => {
    if (Array.isArray(value)) return value.some(containsPrivateText);
    if (!value || typeof value !== 'object') return false;
    return Object.entries(value as Record<string, unknown>).some(([key, entry]) =>
      ['translationText', 'sourceText', 'text', 'artifactUrl', 'relativePath'].includes(key)
      || containsPrivateText(entry));
  };
  const count = (value: unknown): number | null => typeof value === 'number'
    && Number.isInteger(value) && value >= 0 ? value : null;
  const exactChecks = (value: unknown): value is Array<MaskCheckResult<TypesetCheck>> => Array.isArray(value)
    && value.length === TYPESET_CHECKS.length
    && value.every((entry) => entry && typeof entry === 'object' && !Array.isArray(entry)
      && exactKeys(entry as unknown as Record<string, unknown>, ['check', 'passed'])
      && TYPESET_CHECKS.includes((entry as { check: TypesetCheck }).check)
      && typeof (entry as { passed?: unknown }).passed === 'boolean')
    && value.every((entry, index) => entry.check === TYPESET_CHECKS[index]);
  const stringList = (value: unknown) => Array.isArray(value)
    && value.every((entry) => typeof entry === 'string' && entry.length > 0)
    && new Set(value).size === value.length;
  const actorKey = (value: unknown): string | null => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const actor = value as LineageActor;
    const allowed = new Set([
      'actorKind', 'actorId', 'taskId', 'threadId', 'sessionId', 'operationSource',
    ]);
    const identity = [actor.actorId, actor.taskId, actor.threadId, actor.sessionId];
    const opaque = (entry: unknown) => entry === undefined || entry === null
      || (typeof entry === 'string' && entry.length >= 1 && entry.length <= 128
        && !/[\\/\0\r\n]/.test(entry));
    if (Object.keys(value as Record<string, unknown>).some((key) => !allowed.has(key))
      || !['codex', 'cursor', 'human', 'system'].includes(actor.actorKind)
      || !['ui', 'api', 'script'].includes(actor.operationSource)
      || !identity.every(opaque)
      || !identity.some((entry) => typeof entry === 'string' && entry.length > 0)) return null;
    return canonicalJson({
      actorKind: actor.actorKind,
      actorId: actor.actorId ?? null,
      taskId: actor.taskId ?? null,
      threadId: actor.threadId ?? null,
      sessionId: actor.sessionId ?? null,
      operationSource: actor.operationSource,
    });
  };
  const same = (left: unknown, right: unknown) => canonicalJson(left) === canonicalJson(right);
  type OpenTypeset = {
    itemId: string;
    jobId: string;
    actorKey: string;
    enqueue: PageLineageEvent;
    produced: PageLineageEvent | null;
    completed: boolean;
  };
  let open: OpenTypeset | null = null;
  const reviewed = new Set<string>();
  const revisionIds = new Set<string>();
  const jobIds = new Set<string>();
  const jobItemIds = new Set<string>();
  let currentChecksum = g9TerminalChecksum;
  let accepted = false;
  for (const event of events) {
    const rawEvidence: unknown = event.evidence;
    if (!rawEvidence || typeof rawEvidence !== 'object' || Array.isArray(rawEvidence)) return 'locked';
    const evidence = rawEvidence as Record<string, unknown>;
    const itemId = event.jobItemId;
    const candidateId = evidence.candidateId;
    const eventActorKey = actorKey(event.actor);
    if (accepted || event.gate !== 'G10_typeset' || event.stage !== 'typeset'
      || event.parentChecksum !== g9TerminalChecksum || event.gitCommit !== null
      || !eventActorKey || containsPrivateText(evidence)) return 'locked';
    if (event.operation === 'typeset-job-enqueued') {
      if (event.state !== 'pending' || event.decision !== null || event.reason !== 'job-enqueued'
        || !event.jobId || !itemId || open
        || jobIds.has(event.jobId) || jobItemIds.has(itemId)
        || event.inputChecksum !== currentChecksum || event.outputChecksum !== event.inputChecksum
        || event.provider !== 'pillow-g10' || event.modelVersion !== 'g10-typeset-v1'
        || !event.parameterHash || !sha256.test(event.parameterHash)
        || event.revisionId !== null || event.startedAt === null || event.finishedAt !== null
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'targetKind', 'regionCount', 'renderRegionCount',
          'g9TerminalChecksum', 'cleanPlateChecksum', 'routeChecksum', 'styleChecksum',
        ])
        || evidence.eventType !== 'job-enqueued' || evidence.qualityState !== 'pending-review'
        || evidence.targetKind !== 'image' || evidence.g9TerminalChecksum !== g9TerminalChecksum
        || !sha256.test(String(evidence.cleanPlateChecksum))
        || !sha256.test(String(evidence.routeChecksum))
        || !sha256.test(String(evidence.styleChecksum))
        || count(evidence.regionCount) === null || count(evidence.regionCount)! < 1
        || count(evidence.renderRegionCount) === null
        || count(evidence.renderRegionCount)! > count(evidence.regionCount)!) return 'locked';
      open = {
        itemId, jobId: event.jobId, actorKey: eventActorKey,
        enqueue: event, produced: null, completed: false,
      };
      jobIds.add(event.jobId);
      jobItemIds.add(itemId);
      continue;
    }
    if (event.operation === 'typeset-candidate-produced') {
      const enqueue = open?.enqueue;
      const width = count(evidence.width);
      const height = count(evidence.height);
      if (event.state !== 'pending' || event.decision !== 'candidate-produced'
        || event.reason !== 'typeset-review-required' || !open || !enqueue
        || itemId !== open.itemId || event.jobId !== open.jobId || open.produced
        || eventActorKey !== open.actorKey
        || typeof candidateId !== 'string' || !candidateId || reviewed.has(candidateId)
        || event.inputChecksum !== currentChecksum
        || !event.outputChecksum || !sha256.test(event.outputChecksum)
        || event.outputChecksum === currentChecksum
        || event.provider !== enqueue.provider || event.modelVersion !== enqueue.modelVersion
        || event.parameterHash !== enqueue.parameterHash
        || typeof event.revisionId !== 'string' || !event.revisionId
        || revisionIds.has(event.revisionId) || event.startedAt === null || event.finishedAt === null
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'targetKind', 'candidateId', 'candidateChecksum',
          'regionCount', 'renderRegionCount', 'g9TerminalChecksum', 'cleanPlateChecksum',
          'routeChecksum', 'styleChecksum', 'layoutChecksum', 'width', 'height',
          'renderScale', 'overflowRegionIds', 'anomalies',
        ])
        || evidence.eventType !== 'typeset-candidate-produced'
        || evidence.qualityState !== 'pending-review' || evidence.targetKind !== 'typeset-candidate'
        || evidence.g9TerminalChecksum !== g9TerminalChecksum
        || !['candidateChecksum', 'routeChecksum', 'styleChecksum', 'layoutChecksum',
          'cleanPlateChecksum'].every((key) =>
          typeof evidence[key] === 'string' && sha256.test(evidence[key] as string))
        || evidence.cleanPlateChecksum !== enqueue.evidence.cleanPlateChecksum
        || evidence.routeChecksum !== enqueue.evidence.routeChecksum
        || evidence.styleChecksum !== enqueue.evidence.styleChecksum
        || count(evidence.regionCount) !== count(enqueue.evidence.regionCount)
        || count(evidence.renderRegionCount) !== count(enqueue.evidence.renderRegionCount)
        || width === null || width < 1 || height === null || height < 1
        || typeof evidence.renderScale !== 'number' || !Number.isFinite(evidence.renderScale)
        || evidence.renderScale <= 0 || evidence.renderScale > 4
        || !stringList(evidence.overflowRegionIds) || !stringList(evidence.anomalies)) return 'locked';
      revisionIds.add(event.revisionId);
      open.produced = event;
      currentChecksum = event.outputChecksum;
      continue;
    }
    if (event.operation === 'typeset-job-completed') {
      const enqueue = open?.enqueue;
      const produced = open?.produced;
      if (event.state !== 'pending' || event.decision !== null || event.reason !== 'review-required'
        || !open || !enqueue || !produced || open.completed
        || itemId !== open.itemId || event.jobId !== open.jobId
        || eventActorKey !== open.actorKey
        || event.inputChecksum !== enqueue.inputChecksum || event.outputChecksum !== currentChecksum
        || event.provider !== produced.provider || event.modelVersion !== produced.modelVersion
        || event.parameterHash !== produced.parameterHash || event.revisionId !== null
        || event.startedAt === null || event.finishedAt === null
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'targetKind', 'candidateId', 'candidateChecksum',
          'g9TerminalChecksum', 'cleanPlateChecksum', 'routeChecksum', 'styleChecksum',
          'layoutChecksum', 'width', 'height', 'renderScale', 'overflowRegionIds', 'anomalies',
        ])
        || evidence.eventType !== 'job-completed' || evidence.qualityState !== 'pending-review'
        || evidence.targetKind !== 'image'
        || !same(evidence, {
          eventType: 'job-completed', qualityState: 'pending-review', targetKind: 'image',
          candidateId: produced.evidence.candidateId,
          candidateChecksum: produced.evidence.candidateChecksum,
          g9TerminalChecksum: produced.evidence.g9TerminalChecksum,
          cleanPlateChecksum: produced.evidence.cleanPlateChecksum,
          routeChecksum: produced.evidence.routeChecksum,
          styleChecksum: produced.evidence.styleChecksum,
          layoutChecksum: produced.evidence.layoutChecksum,
          width: produced.evidence.width, height: produced.evidence.height,
          renderScale: produced.evidence.renderScale,
          overflowRegionIds: produced.evidence.overflowRegionIds,
          anomalies: produced.evidence.anomalies,
        })) return 'locked';
      open.completed = true;
      continue;
    }
    if (event.operation === 'typeset-job-failed') {
      const enqueue = open?.enqueue;
      if (event.state !== 'blocked' || event.decision !== null
        || event.reason !== 'job-execution-failed' || !open || !enqueue || open.produced
        || itemId !== open.itemId || event.jobId !== open.jobId
        || eventActorKey !== open.actorKey
        || event.inputChecksum !== enqueue.inputChecksum || event.outputChecksum !== null
        || event.provider !== enqueue.provider || event.modelVersion !== enqueue.modelVersion
        || event.parameterHash !== enqueue.parameterHash || event.revisionId !== null
        || event.startedAt === null || event.finishedAt === null
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'targetKind', 'g9TerminalChecksum',
          'cleanPlateChecksum', 'routeChecksum', 'styleChecksum',
        ])
        || !same(evidence, {
          eventType: 'job-failed', qualityState: 'blocked', targetKind: 'image',
          g9TerminalChecksum, cleanPlateChecksum: enqueue.evidence.cleanPlateChecksum,
          routeChecksum: enqueue.evidence.routeChecksum,
          styleChecksum: enqueue.evidence.styleChecksum,
        })) return 'locked';
      open = null;
      continue;
    }
    if (event.operation === 'typeset-candidate-reviewed') {
      const produced = open?.produced;
      const checks = evidence.checks;
      const overflow = evidence.overflowRegionIds;
      const anomalies = evidence.anomalies;
      const failedChecks = exactChecks(checks) ? checks.filter((entry) => !entry.passed) : [];
      const knownDefects = Array.isArray(overflow) && Array.isArray(anomalies)
        && (overflow.length > 0 || anomalies.length > 0);
      const overflowFailed = exactChecks(checks)
        && checks.find((entry) => entry.check === 'overflow-free')?.passed === false;
      const isAccepted = event.state === 'accepted' && event.decision === 'candidate-accepted'
        && event.reason === 'typeset-reviewed' && failedChecks.length === 0
        && Array.isArray(overflow) && overflow.length === 0
        && Array.isArray(anomalies) && anomalies.length === 0;
      const isRejected = event.state === 'rejected' && event.decision === 'candidate-rejected'
        && failedChecks.length > 0 && (!knownDefects || overflowFailed)
        && (event.reason === 'multiple-visual-failures'
          ? failedChecks.length > 1 : failedChecks.some((entry) => entry.check === event.reason));
      if ((!isAccepted && !isRejected) || !open || !open.completed || !produced
        || typeof candidateId !== 'string' || candidateId !== produced.evidence.candidateId
        || reviewed.has(candidateId) || event.jobId !== null
        || event.jobItemId !== null || event.inputChecksum !== currentChecksum
        || !event.outputChecksum || !sha256.test(event.outputChecksum)
        || event.outputChecksum === currentChecksum
        || event.provider !== produced.provider || event.modelVersion !== produced.modelVersion
        || event.parameterHash !== produced.parameterHash
        || typeof event.revisionId !== 'string' || !event.revisionId
        || revisionIds.has(event.revisionId) || event.startedAt !== null || event.finishedAt !== null
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'targetKind', 'candidateId', 'candidateChecksum',
          'g9TerminalChecksum', 'cleanPlateChecksum', 'routeChecksum', 'styleChecksum',
          'layoutChecksum', 'width', 'height', 'renderScale', 'overflowRegionIds',
          'anomalies', 'checks',
        ])
        || evidence.eventType !== 'typeset-candidate-reviewed'
        || evidence.qualityState !== event.state || evidence.targetKind !== 'typeset-candidate'
        || !exactChecks(checks)
        || evidence.candidateChecksum !== produced.evidence.candidateChecksum
        || evidence.routeChecksum !== produced.evidence.routeChecksum
        || evidence.styleChecksum !== produced.evidence.styleChecksum
        || evidence.layoutChecksum !== produced.evidence.layoutChecksum
        || evidence.g9TerminalChecksum !== g9TerminalChecksum
        || evidence.cleanPlateChecksum !== produced.evidence.cleanPlateChecksum
        || evidence.width !== produced.evidence.width || evidence.height !== produced.evidence.height
        || evidence.renderScale !== produced.evidence.renderScale
        || !same(overflow, produced.evidence.overflowRegionIds)
        || !same(anomalies, produced.evidence.anomalies)) return 'locked';
      revisionIds.add(event.revisionId);
      reviewed.add(candidateId);
      currentChecksum = event.outputChecksum;
      accepted = isAccepted;
      open = null;
      continue;
    }
    return 'locked';
  }
  return 'G10';
}

function deriveG9Phase(
  events: PageLineageEvent[],
  g8Checksum: string,
): WorkflowPhase {
  const sha256 = /^[0-9a-f]{64}$/;
  const allowed = new Set([
    'translate-job-enqueued',
    'translation-candidates-produced',
    'translate-job-completed',
    'translate-job-failed',
    'translation-candidate-revised',
    'translation-candidate-reviewed',
    'translation-stage-review',
  ]);
  const containsPrivateText = (value: unknown): boolean => {
    if (Array.isArray(value)) return value.some(containsPrivateText);
    if (!value || typeof value !== 'object') return false;
    return Object.entries(value as Record<string, unknown>).some(([key, entry]) =>
      ['translationText', 'sourceText', 'text'].includes(key) || containsPrivateText(entry));
  };
  const openJobs = new Map<string, string>();
  const producedJobs = new Set<string>();
  const candidateIds = new Set<string>();
  const reviewedIds = new Set<string>();
  let currentChecksum = g8Checksum;
  let terminal = false;
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index]!;
    const evidence = event.evidence;
    const candidateId = evidence?.candidateId;
    if (terminal) return deriveG10Phase(events.slice(index), currentChecksum);
    if (event.gate !== 'G9_translation' || event.stage !== 'translation'
      || event.parentChecksum !== g8Checksum || !allowed.has(event.operation)
      || !event.inputChecksum || !sha256.test(event.inputChecksum)
      || containsPrivateText(evidence)) return 'locked';
    if (event.operation === 'translate-job-enqueued') {
      if (event.state !== 'pending' || event.decision !== null || !event.jobId || !event.jobItemId
        || openJobs.size > 0 || event.inputChecksum !== currentChecksum
        || event.outputChecksum !== event.inputChecksum) return 'locked';
      openJobs.set(event.jobItemId, event.inputChecksum);
      continue;
    }
    if (event.operation === 'translation-candidates-produced') {
      if (event.state !== 'pending' || event.decision !== 'candidates-produced' || !event.jobItemId
        || openJobs.get(event.jobItemId) !== event.inputChecksum || event.inputChecksum !== currentChecksum
        || producedJobs.has(event.jobItemId)
        || !event.outputChecksum || !sha256.test(event.outputChecksum)) return 'locked';
      producedJobs.add(event.jobItemId);
      currentChecksum = event.outputChecksum;
      continue;
    }
    if (event.operation === 'translate-job-completed') {
      if (event.state !== 'pending' || event.decision !== null || !event.jobItemId
        || openJobs.get(event.jobItemId) !== event.inputChecksum || !openJobs.delete(event.jobItemId)
        || !producedJobs.has(event.jobItemId)
        || event.outputChecksum !== currentChecksum) return 'locked';
      continue;
    }
    if (event.operation === 'translate-job-failed') {
      if (event.state !== 'blocked' || event.decision !== null || !event.jobItemId
        || openJobs.get(event.jobItemId) !== event.inputChecksum || !openJobs.delete(event.jobItemId)
        || event.inputChecksum !== currentChecksum || producedJobs.has(event.jobItemId)
        || event.outputChecksum !== null) return 'locked';
      continue;
    }
    if (event.operation === 'translation-candidate-revised') {
      if (event.state !== 'pending' || event.decision !== 'candidate-revised' || event.jobId !== null
        || event.jobItemId !== null || typeof candidateId !== 'string' || !candidateId
        || candidateIds.has(candidateId) || !event.outputChecksum
        || event.inputChecksum !== currentChecksum || !sha256.test(event.outputChecksum)) return 'locked';
      candidateIds.add(candidateId);
      currentChecksum = event.outputChecksum;
      continue;
    }
    if (event.operation === 'translation-candidate-reviewed') {
      if (!['accepted', 'rejected'].includes(event.state) || !['candidate-accepted', 'candidate-rejected'].includes(event.decision ?? '')
        || typeof candidateId !== 'string' || !candidateId || reviewedIds.has(candidateId)
        || (!candidateIds.has(candidateId) && producedJobs.size === 0)
        || event.inputChecksum !== currentChecksum || !event.outputChecksum
        || !sha256.test(event.outputChecksum)) return 'locked';
      reviewedIds.add(candidateId);
      candidateIds.add(candidateId);
      currentChecksum = event.outputChecksum;
      continue;
    }
    if (event.operation === 'translation-stage-review') {
      const accepted = event.state === 'accepted' && event.decision === 'translations-accepted';
      const na = event.state === 'not-applicable' && event.decision === 'translation-not-applicable';
      if ((!accepted && !na) || openJobs.size || event.inputChecksum !== currentChecksum
        || event.jobId !== null || event.jobItemId !== null
        || !event.outputChecksum || !sha256.test(event.outputChecksum)
        || event.outputChecksum === event.inputChecksum) return 'locked';
      currentChecksum = event.outputChecksum;
      terminal = true;
      continue;
    }
  }
  return terminal ? 'G10' : 'G9';
}

function deriveG8Phase(
  generation: PageGeneration,
  events: PageLineageEvent[],
  g7Checksum: string,
  g7NotApplicable: boolean,
  g7ImageRevision: number | null,
  priorRevisionIds: ReadonlySet<string>,
): WorkflowPhase {
  const sha256 = /^[0-9a-f]{64}$/;
  const exactKeys = (value: Record<string, unknown>, keys: readonly string[]) => (
    Object.keys(value).sort().join('\0') === [...keys].sort().join('\0')
  );
  const integer = (value: unknown, minimum = 0) => (
    typeof value === 'number' && Number.isInteger(value) && value >= minimum
  );
  const actorKey = (value: unknown): string | null => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const actor = value as LineageActor;
    const identity = [actor.actorId, actor.taskId, actor.threadId, actor.sessionId];
    const opaque = (value: unknown) => value === undefined || value === null
      || (typeof value === 'string' && value.length >= 1 && value.length <= 128
        && !/[\\/\0\r\n]/.test(value));
    if (!['codex', 'cursor', 'human', 'system'].includes(actor.actorKind)
      || !['ui', 'api', 'script'].includes(actor.operationSource)
      || !identity.every(opaque)
      || !identity.some((value) => typeof value === 'string' && value.length > 0)) return null;
    return JSON.stringify({
      actorKind: actor.actorKind,
      actorId: actor.actorId ?? null,
      taskId: actor.taskId ?? null,
      threadId: actor.threadId ?? null,
      sessionId: actor.sessionId ?? null,
      operationSource: actor.operationSource,
    });
  };
  type RuntimeRoute = {
    regionId: string;
    backgroundCategory: BackgroundCategory;
    route: string;
    originKind: string;
    provider: string;
    modelVersion: string;
    parameterHash: string;
  };
  const routeManifest = (value: unknown, fallbackEnabled: boolean): RuntimeRoute[] | null => {
    if (!Array.isArray(value) || value.length === 0) return null;
    const defaultRoutes: Record<BackgroundCategory, string> = {
      'white-solid': 'deterministic-solid',
      'black-solid': 'deterministic-solid',
      'other-solid': 'deterministic-solid',
      'simple-gradient': 'controlled-gradient',
      screentone: 'screentone-preserving',
      'complex-lineart': 'ai-inpaint-redraw',
      'illustration/character': 'ai-inpaint-redraw',
    };
    const rows: RuntimeRoute[] = [];
    for (const raw of value) {
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)
        || !exactKeys(raw as Record<string, unknown>, [
          'regionId', 'backgroundCategory', 'route', 'originKind',
          'provider', 'modelVersion', 'parameterHash',
        ])) return null;
      const row = raw as RuntimeRoute;
      const expected = defaultRoutes[row.backgroundCategory];
      const complex = row.backgroundCategory === 'complex-lineart'
        || row.backgroundCategory === 'illustration/character';
      const routeIsValid = complex
        ? row.route === 'ai-inpaint-redraw'
          || (fallbackEnabled && row.route === 'classical-fallback')
        : row.route === expected;
      const expectedOrigin = row.route === 'ai-inpaint-redraw' ? 'ai'
        : row.route === 'classical-fallback' ? 'classical' : 'deterministic';
      if (typeof row.regionId !== 'string' || !row.regionId
        || typeof row.provider !== 'string' || !row.provider
        || typeof row.modelVersion !== 'string' || !row.modelVersion
        || !expected || !routeIsValid || row.originKind !== expectedOrigin
        || !sha256.test(row.parameterHash)) return null;
      rows.push(row);
    }
    return new Set(rows.map((row) => row.regionId)).size === rows.length ? rows : null;
  };
  const exactChecks = (value: unknown) => Array.isArray(value)
    && value.length === CLEAN_PLATE_CHECKS.length
    && value.every((entry) => entry && typeof entry === 'object' && !Array.isArray(entry)
      && exactKeys(entry as Record<string, unknown>, ['check', 'passed'])
      && typeof (entry as { passed?: unknown }).passed === 'boolean')
    && new Set(value.map((entry) => (entry as { check: unknown }).check)).size === CLEAN_PLATE_CHECKS.length
    && CLEAN_PLATE_CHECKS.every((check) => value.some((entry) =>
      (entry as { check: unknown }).check === check));
  const failedReason = (checks: Array<{ check: string; passed: boolean }>) => {
    const failed = checks.filter((entry) => !entry.passed).map((entry) => entry.check);
    if (failed.length > 1) return 'multiple-visual-failures';
    return ({
      'outside-mask-unchanged': 'outside-mask-changed',
      'source-text-unreadable': 'residual-text-readable',
      'no-white-or-gray-hole': 'hole-or-block',
      'no-blur-band': 'blur-band',
      'no-repeated-texture': 'repeated-texture',
      'background-continuous': 'background-discontinuous',
      'structure-preserved': 'structure-damaged',
    } as Record<string, string>)[failed[0] ?? ''];
  };
  const enqueued = new Map<string, PageLineageEvent>();
  const produced = new Map<string, PageLineageEvent>();
  const completed = new Set<string>();
  const failed = new Set<string>();
  const candidateIds = new Set<string>();
  const reviewedCandidateIds = new Set<string>();
  const revisionIds = new Set<string>(priorRevisionIds);
  const candidateManifests = new Map<string, RuntimeRoute[]>();
  const candidateReviewStates = new Map<string, 'accepted' | 'rejected'>();
  const complexRegionIds = new Set<string>();
  let backgroundChecksum: string | null = null;
  let qualityChecksum: string | null = null;
  let maskArtifactId: string | null = null;
  let maskChecksum: string | null = null;
  let latestStateImageRevision: number | null = g7ImageRevision;
  let currentState = g7Checksum;
  let openItemId: string | null = null;
  let fallbackEnabled = false;
  let terminal = false;

  for (let eventIndex = 0; eventIndex < events.length; eventIndex += 1) {
    const event = events[eventIndex]!;
    if (terminal) return deriveG9Phase(events.slice(eventIndex), currentState);
    if (event.gate !== 'G8_cleanPlate' || event.stage !== 'inpaint'
      || event.parentChecksum !== g7Checksum) return 'locked';
    if (!event.evidence || typeof event.evidence !== 'object' || Array.isArray(event.evidence)
      || actorKey(event.actor) === null) return 'locked';
    const evidence = event.evidence;
    if (event.operation === 'clean-plate-fallback-enabled'
      || event.operation === 'clean-plate-fallback-disabled') {
      const enabled = event.operation === 'clean-plate-fallback-enabled';
      const imageRevision = integer(evidence.imageRevision, 1)
        ? evidence.imageRevision as number : null;
      const aiCandidateEntries = [...candidateManifests.entries()].filter(([, manifest]) =>
        manifest.some((route) => route.originKind === 'ai'));
      const coveredAIRegionIds = new Set(aiCandidateEntries.flatMap(([, manifest]) =>
        manifest.filter((route) => route.originKind === 'ai').map((route) => route.regionId)));
      const fallbackAllowed = complexRegionIds.size > 0 && aiCandidateEntries.length > 0
        && [...complexRegionIds].every((regionId) => coveredAIRegionIds.has(regionId))
        && aiCandidateEntries.every(([candidateId]) =>
          candidateReviewStates.get(candidateId) === 'rejected');
      if (openItemId || fallbackEnabled === enabled
        || event.state !== 'pending' || event.inputChecksum !== currentState
        || !event.outputChecksum || !sha256.test(event.outputChecksum)
        || event.outputChecksum === currentState || event.jobId !== null || event.jobItemId !== null
        || typeof event.revisionId !== 'string' || !event.revisionId
        || revisionIds.has(event.revisionId)
        || event.provider !== 'operator' || event.modelVersion !== 'page-scoped-fallback-v1'
        || !event.parameterHash || !sha256.test(event.parameterHash)
        || event.decision !== (enabled ? 'classical-fallback-enabled' : 'classical-fallback-disabled')
        || event.reason !== (enabled ? 'all-ai-candidates-rejected' : 'resume-ai-candidates')
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'enabled', 'candidateCount', 'aiCandidateCount', 'imageRevision',
        ])
        || evidence.eventType !== event.operation || evidence.qualityState !== 'pending-review'
        || evidence.enabled !== enabled || evidence.candidateCount !== candidateIds.size
        || evidence.aiCandidateCount !== aiCandidateEntries.length
        || imageRevision === null
        || (latestStateImageRevision !== null && imageRevision <= latestStateImageRevision)
        || (enabled && !fallbackAllowed)) return 'locked';
      revisionIds.add(event.revisionId);
      latestStateImageRevision = imageRevision;
      fallbackEnabled = enabled;
      currentState = event.outputChecksum;
      continue;
    }

    const itemId = event.jobItemId;
    if (event.operation === 'inpaint-job-enqueued') {
      const manifest = routeManifest(evidence.routeManifest, fallbackEnabled);
      const providers = manifest
        ? [...new Set(manifest.map((route) => route.provider))].sort() : [];
      const nextBackgroundChecksum = typeof evidence.backgroundChecksum === 'string'
        ? evidence.backgroundChecksum : '';
      const nextQualityChecksum = typeof evidence.qualityChecksum === 'string'
        ? evidence.qualityChecksum : '';
      const nextMaskArtifactId = typeof evidence.maskArtifactId === 'string'
        ? evidence.maskArtifactId : '';
      const nextMaskChecksum = typeof evidence.maskChecksum === 'string'
        ? evidence.maskChecksum : '';
      if (g7NotApplicable || openItemId || !event.jobId || !itemId || enqueued.has(itemId)
        || event.state !== 'pending' || event.decision !== null || event.reason !== 'job-enqueued'
        || event.revisionId !== null || event.inputChecksum !== currentState
        || event.outputChecksum !== currentState || !manifest
        || event.provider !== (providers.length === 1 ? providers[0] : 'mixed')
        || event.modelVersion !== 'route-manifest-v1'
        || !event.parameterHash || !sha256.test(event.parameterHash)
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'targetKind', 'g7Checksum',
          'backgroundChecksum', 'qualityChecksum', 'maskArtifactId',
          'maskChecksum', 'routeManifest', 'routeChecksum',
        ])
        || evidence.eventType !== 'job-enqueued' || evidence.qualityState !== 'pending-review'
        || evidence.targetKind !== 'image' || evidence.g7Checksum !== g7Checksum
        || !sha256.test(nextBackgroundChecksum)
        || !sha256.test(nextQualityChecksum)
        || !nextMaskArtifactId
        || !sha256.test(nextMaskChecksum)
        || !sha256.test(String(evidence.routeChecksum ?? ''))
        || event.parameterHash !== evidence.routeChecksum
        || (backgroundChecksum !== null && backgroundChecksum !== nextBackgroundChecksum)
        || (qualityChecksum !== null && qualityChecksum !== nextQualityChecksum)
        || (maskArtifactId !== null && maskArtifactId !== nextMaskArtifactId)
        || (maskChecksum !== null && maskChecksum !== nextMaskChecksum)) return 'locked';
      backgroundChecksum = nextBackgroundChecksum;
      qualityChecksum = nextQualityChecksum;
      maskArtifactId = nextMaskArtifactId;
      maskChecksum = nextMaskChecksum;
      for (const route of manifest) {
        if (route.backgroundCategory === 'complex-lineart'
          || route.backgroundCategory === 'illustration/character') {
          complexRegionIds.add(route.regionId);
        }
      }
      enqueued.set(itemId, event);
      openItemId = itemId;
      continue;
    }
    if (event.operation === 'clean-plate-candidate-produced') {
      const enqueue = itemId ? enqueued.get(itemId) : undefined;
      const candidateId = evidence.candidateId;
      const manifest = routeManifest(evidence.routeManifest, fallbackEnabled);
      const providers = manifest
        ? [...new Set(manifest.map((route) => route.provider))].sort() : [];
      const models = manifest
        ? [...new Set(manifest.map((route) => route.modelVersion))].sort() : [];
      const origins = manifest ? new Set(manifest.map((route) => route.originKind)) : new Set<string>();
      const expectedOrigin = origins.size === 1 ? [...origins][0] : 'mixed';
      const imageRevision = integer(evidence.imageRevision, 1)
        ? evidence.imageRevision as number : null;
      if (!itemId || itemId !== openItemId || !enqueue || produced.has(itemId)
        || event.jobId !== enqueue.jobId || actorKey(event.actor) !== actorKey(enqueue.actor)
        || event.state !== 'pending' || event.decision !== null
        || event.reason !== 'clean-plate-review-required'
        || typeof event.revisionId !== 'string' || !event.revisionId
        || revisionIds.has(event.revisionId)
        || event.inputChecksum !== currentState || !event.outputChecksum
        || !sha256.test(event.outputChecksum) || event.outputChecksum === currentState
        || !manifest || canonicalJson(manifest) !== canonicalJson(enqueue.evidence.routeManifest)
        || event.provider !== (providers.length === 1 ? providers[0] : 'mixed')
        || event.modelVersion !== 'route-manifest-v1'
        || !event.parameterHash || event.parameterHash !== evidence.parameterHash
        || event.parameterHash !== evidence.routeChecksum
        || event.parameterHash !== enqueue.parameterHash
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'targetKind', 'candidateId', 'candidateChecksum',
          'g7Checksum', 'backgroundChecksum', 'qualityChecksum', 'maskArtifactId',
          'maskChecksum', 'routeManifest', 'routeChecksum', 'originKind', 'providerIds',
          'modelVersions', 'parameterHash', 'width', 'height', 'renderScale',
          'outsideMaskChangeCount', 'anomalies', 'imageRevision',
        ])
        || evidence.eventType !== 'clean-plate-candidate-produced'
        || evidence.qualityState !== 'pending-review' || evidence.targetKind !== 'clean-plate-candidate'
        || typeof candidateId !== 'string' || !candidateId || candidateIds.has(candidateId)
        || !sha256.test(String(evidence.candidateChecksum ?? ''))
        || evidence.g7Checksum !== g7Checksum
        || evidence.backgroundChecksum !== backgroundChecksum
        || evidence.qualityChecksum !== qualityChecksum
        || evidence.maskArtifactId !== maskArtifactId
        || evidence.maskChecksum !== maskChecksum
        || !sha256.test(String(evidence.routeChecksum ?? ''))
        || evidence.routeChecksum !== enqueue.evidence.routeChecksum
        || evidence.originKind !== expectedOrigin
        || !Array.isArray(evidence.providerIds) || !Array.isArray(evidence.modelVersions)
        || canonicalJson(evidence.providerIds) !== canonicalJson(providers)
        || canonicalJson(evidence.modelVersions) !== canonicalJson(models)
        || !integer(evidence.width, 1) || !integer(evidence.height, 1)
        || !integer(evidence.renderScale, 1) || (evidence.renderScale as number) > 4
        || evidence.outsideMaskChangeCount !== 0
        || !Array.isArray(evidence.anomalies)
        || evidence.anomalies.some((entry) => typeof entry !== 'string')
        || imageRevision === null
        || (latestStateImageRevision !== null && imageRevision <= latestStateImageRevision)) return 'locked';
      candidateIds.add(candidateId);
      candidateManifests.set(candidateId, manifest);
      revisionIds.add(event.revisionId);
      latestStateImageRevision = imageRevision;
      produced.set(itemId, event);
      currentState = event.outputChecksum;
      continue;
    }
    if (event.operation === 'inpaint-job-completed') {
      const enqueue = itemId ? enqueued.get(itemId) : undefined;
      const publication = itemId ? produced.get(itemId) : undefined;
      if (!itemId || itemId !== openItemId || !enqueue || !publication
        || completed.has(itemId) || failed.has(itemId) || event.jobId !== enqueue.jobId
        || actorKey(event.actor) !== actorKey(enqueue.actor)
        || event.state !== 'pending' || event.decision !== null || event.reason !== 'review-required'
        || event.revisionId !== null || event.inputChecksum !== enqueue.inputChecksum
        || event.outputChecksum !== currentState || event.outputChecksum !== publication.outputChecksum
        || event.provider !== publication.provider || event.modelVersion !== publication.modelVersion
        || event.parameterHash !== publication.parameterHash
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'targetKind', 'candidateId', 'candidateChecksum',
          'maskArtifactId', 'maskChecksum', 'routeChecksum', 'outsideMaskChangeCount',
        ])
        || evidence.eventType !== 'job-completed' || evidence.qualityState !== 'pending-review'
        || evidence.targetKind !== 'image'
        || evidence.candidateId !== publication.evidence.candidateId
        || evidence.candidateChecksum !== publication.evidence.candidateChecksum
        || evidence.maskArtifactId !== publication.evidence.maskArtifactId
        || evidence.maskChecksum !== publication.evidence.maskChecksum
        || evidence.routeChecksum !== publication.evidence.routeChecksum
        || evidence.outsideMaskChangeCount !== 0) return 'locked';
      completed.add(itemId);
      openItemId = null;
      continue;
    }
    if (event.operation === 'inpaint-job-failed') {
      const enqueue = itemId ? enqueued.get(itemId) : undefined;
      if (!itemId || itemId !== openItemId || !enqueue || produced.has(itemId)
        || completed.has(itemId) || failed.has(itemId) || event.jobId !== enqueue.jobId
        || actorKey(event.actor) !== actorKey(enqueue.actor)
        || event.state !== 'blocked' || event.decision !== null
        || event.reason !== 'job-execution-failed' || event.revisionId !== null
        || event.inputChecksum !== enqueue.inputChecksum || event.outputChecksum !== null
        || event.provider !== enqueue.provider || event.modelVersion !== enqueue.modelVersion
        || event.parameterHash !== enqueue.parameterHash
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'targetKind', 'routeChecksum',
        ])
        || evidence.eventType !== 'job-failed' || evidence.qualityState !== 'blocked'
        || evidence.targetKind !== 'image'
        || evidence.routeChecksum !== enqueue.evidence.routeChecksum) return 'locked';
      failed.add(itemId);
      openItemId = null;
      continue;
    }
    if (event.operation === 'clean-plate-stage-review') {
      const candidateId = evidence.candidateId;
      const checks = evidence.checks;
      const imageRevision = integer(evidence.imageRevision, 1)
        ? evidence.imageRevision as number : null;
      const isNA = event.state === 'not-applicable'
        && event.decision === 'clean-plate-not-applicable'
        && event.reason === 'no-clean-plate-required'
        && candidateId === null && evidence.candidateChecksum === null
        && evidence.maskArtifactId === null && evidence.maskChecksum === null
        && evidence.routeChecksum === null && evidence.originKind === 'no-op'
        && Array.isArray(checks) && checks.length === 0
        && candidateIds.size === 0 && enqueued.size === 0 && g7NotApplicable;
      const publication = typeof candidateId === 'string'
        ? [...produced.values()].find((entry) => entry.evidence.candidateId === candidateId)
        : undefined;
      const completedCandidate = publication ? completed.has(publication.jobItemId ?? '') : false;
      const validChecks = exactChecks(checks);
      const typedChecks = validChecks ? checks as Array<{ check: string; passed: boolean }> : [];
      const allPassed = validChecks && typedChecks.every((entry) => entry.passed);
      const accepted = event.state === 'accepted' && event.decision === 'clean-plate-accepted'
        && event.reason === 'clean-plate-complete' && allPassed;
      const rejected = event.state === 'rejected' && event.decision === 'clean-plate-rejected'
        && validChecks && !allPassed && event.reason === failedReason(typedChecks);
      if (openItemId || event.jobId !== null || event.jobItemId !== null
        || typeof event.revisionId !== 'string' || !event.revisionId
        || revisionIds.has(event.revisionId)
        || event.inputChecksum !== currentState || !event.outputChecksum
        || !sha256.test(event.outputChecksum) || event.outputChecksum === currentState
        || event.provider !== (isNA ? 'none' : publication?.provider)
        || event.modelVersion !== (isNA ? 'quality-plate-pass-through-v1' : 'route-manifest-v1')
        || event.parameterHash !== (isNA ? evidence.backgroundChecksum : publication?.parameterHash)
        || !exactKeys(evidence, [
          'eventType', 'qualityState', 'candidateId', 'candidateChecksum', 'g7Checksum',
          'backgroundChecksum', 'qualityChecksum', 'maskArtifactId', 'maskChecksum',
          'routeChecksum', 'originKind', 'checks', 'imageRevision',
        ])
        || evidence.eventType !== 'clean-plate-stage-review'
        || evidence.qualityState !== event.state || evidence.g7Checksum !== g7Checksum
        || !sha256.test(String(evidence.backgroundChecksum ?? ''))
        || !sha256.test(String(evidence.qualityChecksum ?? ''))
        || (backgroundChecksum !== null && evidence.backgroundChecksum !== backgroundChecksum)
        || (qualityChecksum !== null && evidence.qualityChecksum !== qualityChecksum)
        || imageRevision === null
        || (latestStateImageRevision !== null && imageRevision <= latestStateImageRevision)
        || (!isNA && !accepted && !rejected)
        || (isNA && (!g7NotApplicable || backgroundChecksum !== null || qualityChecksum !== null))
        || (!isNA && g7NotApplicable)
        || (!isNA && (typeof candidateId !== 'string' || !publication || !completedCandidate
          || reviewedCandidateIds.has(candidateId)
          || evidence.candidateChecksum !== publication.evidence.candidateChecksum
          || evidence.maskArtifactId !== publication.evidence.maskArtifactId
          || evidence.maskChecksum !== publication.evidence.maskChecksum
          || evidence.routeChecksum !== publication.evidence.routeChecksum
          || evidence.originKind !== publication.evidence.originKind
          || event.parameterHash !== publication.parameterHash))) return 'locked';
      if (typeof candidateId === 'string') {
        reviewedCandidateIds.add(candidateId);
        candidateReviewStates.set(candidateId, event.state as 'accepted' | 'rejected');
      }
      revisionIds.add(event.revisionId);
      latestStateImageRevision = imageRevision;
      if (isNA) {
        backgroundChecksum = evidence.backgroundChecksum as string;
        qualityChecksum = evidence.qualityChecksum as string;
      }
      currentState = event.outputChecksum;
      terminal = isNA || accepted;
      continue;
    }
    return 'locked';
  }
  return terminal ? 'G9' : 'G8';
}

function deriveG7Phase(
  generation: PageGeneration,
  events: PageLineageEvent[],
  g6Checksum: string,
): WorkflowPhase {
  const sha256 = /^[0-9a-f]{64}$/;
  const count = (event: PageLineageEvent, key: string) => {
    const value = event.evidence[key];
    return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
  };
  const exactEvidenceKeys = (event: PageLineageEvent, keys: readonly string[]) => (
    Object.keys(event.evidence).sort().join('\0') === [...keys].sort().join('\0')
  );
  const canonicalRubyMapping = (value: unknown): string | null => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => a.localeCompare(b));
    if (entries.some(([, ids]) => !Array.isArray(ids)
      || ids.some((id) => typeof id !== 'string')
      || ids.length !== new Set(ids).size)) return null;
    return JSON.stringify(Object.fromEntries(entries.map(([key, ids]) => [key, [...(ids as string[])].sort()])));
  };
  const maskBBox = (value: unknown): { x: number; y: number; width: number; height: number } | null => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    const record = value as Record<string, unknown>;
    if (Object.keys(record).sort().join(',') !== 'height,width,x,y') return null;
    const { x, y, width, height } = record;
    if (![x, y, width, height].every((entry) => typeof entry === 'number' && Number.isInteger(entry))
      || (x as number) < 0 || (y as number) < 0 || (width as number) < 1 || (height as number) < 1) return null;
    return { x: x as number, y: y as number, width: width as number, height: height as number };
  };
  const jobs = new Map<string, PageLineageEvent>();
  const artifacts = new Map<string, PageLineageEvent>();
  const artifactIds = new Set<string>();
  const revisionIds = new Set<string>();
  const completed = new Set<string>();
  const failed = new Set<string>();
  let eligibleCount: number | null = null;
  let currentRecipeChecksum: string | null = null;
  let currentStateChecksum: string = g6Checksum;
  let qualityChecksum: string | null = null;
  let rubyMapping: string | null = null;
  let terminal = false;
  let g7NotApplicable = false;
  let latestStateImageRevision: number | null = null;
  let lastRejectedArtifact: string | null = null;
  let latestDraftAfterRejectSequence: number | null = null;
  for (let eventIndex = 0; eventIndex < events.length; eventIndex += 1) {
    const event = events[eventIndex]!;
    if (event.gate === 'G8_cleanPlate') {
      return terminal
        ? deriveG8Phase(
          generation,
          events.slice(eventIndex),
          currentStateChecksum,
          g7NotApplicable,
          latestStateImageRevision,
          revisionIds,
        )
        : 'locked';
    }
    if (event.gate !== 'G7_mask' || terminal || event.stage !== 'mask'
      || event.parentChecksum !== g6Checksum) return 'locked';
    const itemId = event.jobItemId;
    if (event.operation === 'mask-draft-updated') {
      if ([...jobs.keys()].some((id) => !completed.has(id) && !failed.has(id))) return 'locked';
      const nextEligible = count(event, 'eligibleRegionCount');
      const recipeCount = count(event, 'recipeRegionCount');
      const recipeChecksum = event.evidence.recipeChecksum;
      const nextQuality = event.evidence.qualityChecksum;
      const nextRubyMapping = canonicalRubyMapping(event.evidence.rubyRegionIdsByPrimary);
      const rubyCount = count(event, 'rubyRegionCount');
      const imageRevision = count(event, 'imageRevision');
      const linkedRubyCount = nextRubyMapping === null
        ? -1
        : Object.values(JSON.parse(nextRubyMapping) as Record<string, string[]>).reduce((sum, ids) => sum + ids.length, 0);
      if (event.state !== 'pending' || event.decision !== null || event.reason !== 'mask-recipe-updated'
        || event.provider !== 'deterministic-mask' || event.modelVersion !== 'create-mask-v1'
        || event.jobId !== null || itemId !== null
        || typeof event.revisionId !== 'string' || !event.revisionId
        || revisionIds.has(event.revisionId)
        || !event.inputChecksum || !sha256.test(event.inputChecksum)
        || event.inputChecksum !== currentStateChecksum
        || !event.outputChecksum || !sha256.test(event.outputChecksum)
        || event.evidence.eventType !== 'mask-draft-updated'
        || event.evidence.qualityState !== 'pending-review'
        || !exactEvidenceKeys(event, [
          'eventType', 'qualityState', 'eligibleRegionCount', 'recipeRegionCount',
          'recipeChecksum', 'qualityChecksum', 'rubyRegionCount',
          'rubyRegionIdsByPrimary', 'imageRevision',
        ])
        || typeof recipeChecksum !== 'string' || !sha256.test(recipeChecksum)
        || event.parameterHash !== recipeChecksum
        || typeof nextQuality !== 'string' || !sha256.test(nextQuality)
        || (qualityChecksum !== null && nextQuality !== qualityChecksum)
        || nextRubyMapping === null || (rubyMapping !== null && nextRubyMapping !== rubyMapping)
        || rubyCount === null || rubyCount !== linkedRubyCount
        || imageRevision === null || imageRevision < 1
        || (latestStateImageRevision !== null && imageRevision <= latestStateImageRevision)
        || nextEligible === null || recipeCount !== nextEligible
        || (eligibleCount !== null && eligibleCount !== nextEligible)) return 'locked';
      eligibleCount = nextEligible;
      currentRecipeChecksum = recipeChecksum;
      currentStateChecksum = event.outputChecksum;
      qualityChecksum = nextQuality;
      rubyMapping = nextRubyMapping;
      latestStateImageRevision = imageRevision;
      revisionIds.add(event.revisionId);
      if (lastRejectedArtifact) latestDraftAfterRejectSequence = event.sequence;
      continue;
    }
    if (event.operation === 'mask-job-enqueued') {
      const nextEligible = count(event, 'eligibleRegionCount');
      const nextRubyCount = count(event, 'rubyRegionCount');
      const nextRubyMapping = canonicalRubyMapping(event.evidence.rubyRegionIdsByPrimary);
      const linkedRubyCount = nextRubyMapping === null
        ? -1
        : Object.values(JSON.parse(nextRubyMapping) as Record<string, string[]>).reduce((sum, ids) => sum + ids.length, 0);
      if (event.state !== 'pending' || event.decision !== null || event.reason !== 'job-enqueued'
        || !event.jobId || !itemId || jobs.has(itemId)
        || event.revisionId !== null
        || [...jobs.keys()].some((id) => !completed.has(id) && !failed.has(id))
        || event.provider !== 'deterministic-mask' || event.modelVersion !== 'create-mask-v1'
        || event.parameterHash !== currentRecipeChecksum || event.outputChecksum !== event.inputChecksum
        || !event.inputChecksum || !sha256.test(event.inputChecksum)
        || event.inputChecksum !== currentStateChecksum
        || event.evidence.eventType !== 'job-enqueued'
        || event.evidence.qualityState !== 'pending-review'
        || event.evidence.targetKind !== 'image' || event.evidence.recipeChecksum !== currentRecipeChecksum
        || event.evidence.qualityChecksum !== qualityChecksum
        || !exactEvidenceKeys(event, [
          'eventType', 'qualityState', 'targetKind', 'eligibleRegionCount',
          'rubyRegionCount', 'rubyRegionIdsByPrimary', 'recipeChecksum', 'qualityChecksum',
        ])
        || nextRubyMapping !== rubyMapping
        || nextRubyCount === null || nextRubyCount !== linkedRubyCount
        || nextEligible === null || nextEligible < 1
        || (eligibleCount !== null && eligibleCount !== nextEligible)) return 'locked';
      jobs.set(itemId, event); eligibleCount = nextEligible;
      continue;
    }
    if (event.operation === 'mask-artifact-produced') {
      const enqueue = itemId ? jobs.get(itemId) : undefined;
      const artifactId = event.evidence.artifactId;
      const recipeChecksum = event.evidence.recipeChecksum;
      const maskChecksum = event.evidence.maskChecksum;
      const nextEligible = count(event, 'eligibleRegionCount');
      const nextQuality = event.evidence.qualityChecksum;
      const nextRubyMapping = canonicalRubyMapping(event.evidence.rubyRegionIdsByPrimary);
      const rubyCount = count(event, 'rubyRegionCount');
      const width = count(event, 'width');
      const height = count(event, 'height');
      const renderScale = count(event, 'renderScale');
      const nonzeroPixelCount = count(event, 'nonzeroPixelCount');
      const imageRevision = count(event, 'imageRevision');
      const bbox = maskBBox(event.evidence.bbox);
      const linkedRubyCount = nextRubyMapping === null
        ? -1
        : Object.values(JSON.parse(nextRubyMapping) as Record<string, string[]>).reduce((sum, ids) => sum + ids.length, 0);
      if (!enqueue || !itemId || artifacts.has(itemId) || failed.has(itemId)
        || event.state !== 'pending' || event.decision !== null || event.reason !== 'mask-review-required'
        || event.jobId !== enqueue.jobId || !event.inputChecksum || !sha256.test(event.inputChecksum)
        || typeof event.revisionId !== 'string' || !event.revisionId
        || revisionIds.has(event.revisionId)
        || event.inputChecksum !== enqueue.inputChecksum
        || event.inputChecksum !== currentStateChecksum
        || !event.outputChecksum || !sha256.test(event.outputChecksum)
        || event.evidence.eventType !== 'mask-artifact-produced'
        || event.evidence.qualityState !== 'pending-review' || event.evidence.targetKind !== 'page-mask'
        || typeof artifactId !== 'string' || !artifactId || artifactIds.has(artifactId)
        || typeof recipeChecksum !== 'string'
        || recipeChecksum !== currentRecipeChecksum
        || typeof maskChecksum !== 'string' || !sha256.test(maskChecksum)
        || typeof nextQuality !== 'string' || nextQuality !== qualityChecksum
        || nextRubyMapping !== rubyMapping
        || event.provider !== 'deterministic-mask' || event.modelVersion !== 'create-mask-v1'
        || event.parameterHash !== currentRecipeChecksum
        || event.provider !== enqueue.provider || event.modelVersion !== enqueue.modelVersion
        || event.parameterHash !== enqueue.parameterHash
        || event.parentChecksum !== enqueue.parentChecksum
        || recipeChecksum !== enqueue.evidence.recipeChecksum
        || nextQuality !== enqueue.evidence.qualityChecksum
        || nextRubyMapping !== canonicalRubyMapping(enqueue.evidence.rubyRegionIdsByPrimary)
        || nextEligible !== count(enqueue, 'eligibleRegionCount')
        || event.evidence.provider !== event.provider || event.evidence.modelVersion !== event.modelVersion
        || event.evidence.parameterHash !== event.parameterHash
        || !exactEvidenceKeys(event, [
          'eventType', 'qualityState', 'targetKind', 'artifactId', 'recipeChecksum',
          'maskChecksum', 'qualityChecksum', 'width', 'height', 'renderScale',
          'nonzeroPixelCount', 'bbox', 'eligibleRegionCount', 'rubyRegionCount',
          'rubyRegionIdsByPrimary', 'imageRevision', 'provider', 'modelVersion',
          'parameterHash',
        ])
        || nextEligible === null || nextEligible !== eligibleCount
        || rubyCount === null || rubyCount !== linkedRubyCount
        || nonzeroPixelCount === null || nonzeroPixelCount < 1
        || width === null || width < 1 || height === null || height < 1
        || renderScale === null || renderScale < 1 || renderScale > 4
        || imageRevision === null || imageRevision < 1
        || (latestStateImageRevision !== null && imageRevision <= latestStateImageRevision)
        || bbox === null || bbox.x + bbox.width > width || bbox.y + bbox.height > height) return 'locked';
      artifacts.set(itemId, event);
      artifactIds.add(artifactId);
      currentStateChecksum = event.outputChecksum;
      latestStateImageRevision = imageRevision;
      revisionIds.add(event.revisionId);
      continue;
    }
    if (event.operation === 'mask-job-completed') {
      const enqueue = itemId ? jobs.get(itemId) : undefined;
      const produced = itemId ? artifacts.get(itemId) : undefined;
      const completionKeys = [
        'artifactId', 'maskChecksum', 'recipeChecksum', 'qualityChecksum', 'width', 'height',
        'renderScale', 'nonzeroPixelCount', 'bbox', 'eligibleRegionCount', 'rubyRegionCount',
        'provider', 'modelVersion', 'parameterHash',
      ] as const;
      const completionEvidenceMatches = produced !== undefined
        && completionKeys.every((key) => JSON.stringify(event.evidence[key]) === JSON.stringify(produced.evidence[key]))
        && canonicalRubyMapping(event.evidence.rubyRegionIdsByPrimary)
          === canonicalRubyMapping(produced.evidence.rubyRegionIdsByPrimary);
      if (!enqueue || !produced || !itemId || completed.has(itemId) || failed.has(itemId)
        || event.state !== 'pending' || event.decision !== null || event.reason !== 'review-required'
        || event.jobId !== enqueue.jobId || event.inputChecksum !== enqueue.inputChecksum
        || event.revisionId !== null
        || event.outputChecksum !== currentStateChecksum || event.outputChecksum !== produced.outputChecksum || event.provider !== produced.provider
        || event.modelVersion !== produced.modelVersion || event.parameterHash !== produced.parameterHash
        || event.evidence.eventType !== 'job-completed'
        || event.evidence.qualityState !== 'pending-review' || event.evidence.targetKind !== 'image'
        || !exactEvidenceKeys(event, [
          'eventType', 'qualityState', 'targetKind', 'artifactId', 'maskChecksum',
          'recipeChecksum', 'qualityChecksum', 'width', 'height', 'renderScale',
          'nonzeroPixelCount', 'bbox', 'eligibleRegionCount', 'rubyRegionCount',
          'rubyRegionIdsByPrimary', 'provider', 'modelVersion', 'parameterHash',
        ])
        || !completionEvidenceMatches) return 'locked';
      completed.add(itemId);
      continue;
    }
    if (event.operation === 'mask-job-failed') {
      const enqueue = itemId ? jobs.get(itemId) : undefined;
      const failureAnchorKeys = [
        'recipeChecksum', 'qualityChecksum', 'eligibleRegionCount', 'rubyRegionCount',
        'rubyRegionIdsByPrimary',
      ] as const;
      if (!enqueue || !itemId || artifacts.has(itemId) || completed.has(itemId) || failed.has(itemId)
        || event.state !== 'blocked' || event.decision !== null || event.reason !== 'job-execution-failed'
        || event.jobId !== enqueue.jobId || event.inputChecksum !== currentStateChecksum
        || event.revisionId !== null
        || event.provider !== enqueue.provider || event.modelVersion !== enqueue.modelVersion
        || event.parameterHash !== enqueue.parameterHash
        || event.outputChecksum !== null || event.evidence.eventType !== 'job-failed'
        || event.evidence.qualityState !== 'blocked' || event.evidence.targetKind !== 'image'
        || !exactEvidenceKeys(event, [
          'eventType', 'qualityState', 'targetKind', 'recipeChecksum', 'qualityChecksum',
          'eligibleRegionCount', 'rubyRegionCount', 'rubyRegionIdsByPrimary',
          'provider', 'modelVersion', 'parameterHash',
        ])
        || failureAnchorKeys.some((key) => JSON.stringify(event.evidence[key])
          !== JSON.stringify(enqueue.evidence[key]))
        || event.evidence.provider !== enqueue.provider
        || event.evidence.modelVersion !== enqueue.modelVersion
        || event.evidence.parameterHash !== enqueue.parameterHash) return 'locked';
      failed.add(itemId);
      continue;
    }
    if (event.operation === 'mask-stage-review') {
      const active = [...jobs.keys()].some((id) => !completed.has(id) && !failed.has(id));
      const artifactId = event.evidence.artifactId;
      const coverage = event.evidence.coverageChecks;
      const collateral = event.evidence.collateralChecks;
      const nextEligible = count(event, 'eligibleRegionCount');
      const nextQuality = event.evidence.qualityChecksum;
      const nextRubyMapping = canonicalRubyMapping(event.evidence.rubyRegionIdsByPrimary);
      const nextRubyCount = count(event, 'rubyRegionCount');
      const imageRevision = count(event, 'imageRevision');
      const linkedRubyCount = nextRubyMapping === null
        ? -1
        : Object.values(JSON.parse(nextRubyMapping) as Record<string, string[]>).reduce((sum, ids) => sum + ids.length, 0);
      const reviewRecipeChecksum = event.evidence.recipeChecksum;
      const exactChecks = (value: unknown, expected: readonly string[]) => Array.isArray(value)
        && value.length === expected.length
        && value.every((entry) => typeof entry === 'object' && entry !== null && !Array.isArray(entry)
          && Object.keys(entry).sort().join(',') === 'check,passed')
        && new Set(value.map((entry) => typeof entry === 'object' && entry ? (entry as { check?: unknown }).check : null)).size === expected.length
        && expected.every((check) => value.some((entry) => typeof entry === 'object' && entry
          && (entry as { check?: unknown }).check === check && typeof (entry as { passed?: unknown }).passed === 'boolean'));
      const na = event.state === 'not-applicable' && event.decision === 'mask-not-applicable'
        && event.reason === 'no-eligible-regions' && nextEligible === 0
        && artifactId === null && Array.isArray(coverage) && coverage.length === 0
        && Array.isArray(collateral) && collateral.length === 0
        && jobs.size === 0 && artifacts.size === 0 && lastRejectedArtifact === null && currentRecipeChecksum === null;
      const artifactEntry = [...artifacts.entries()].find(([, entry]) => entry.evidence.artifactId === artifactId);
      const artifact = artifactEntry?.[1];
      const covExact = exactChecks(coverage, MASK_COVERAGE_CHECKS);
      const colExact = exactChecks(collateral, MASK_COLLATERAL_CHECKS);
      const covPass = covExact && (coverage as Array<{ passed: boolean }>).every((entry) => entry.passed);
      const colPass = colExact && (collateral as Array<{ passed: boolean }>).every((entry) => entry.passed);
      const accepted = event.state === 'accepted' && event.decision === 'mask-accepted'
        && event.reason === 'complete-and-no-collateral' && covPass && colPass;
      const rejectedReason = !covPass && !colPass ? 'coverage-and-collateral-failed'
        : !covPass ? 'coverage-incomplete' : 'collateral-damage';
      const rejected = event.state === 'rejected' && event.decision === 'mask-rejected'
        && covExact && colExact && (!covPass || !colPass) && event.reason === rejectedReason;
      if (active || !event.inputChecksum || !sha256.test(event.inputChecksum)
        || event.inputChecksum !== currentStateChecksum
        || !event.outputChecksum || !sha256.test(event.outputChecksum)
        || event.provider !== 'deterministic-mask' || event.modelVersion !== 'create-mask-v1'
        || typeof reviewRecipeChecksum !== 'string' || !sha256.test(reviewRecipeChecksum)
        || event.parameterHash !== reviewRecipeChecksum || event.jobId !== null || itemId !== null
        || typeof event.revisionId !== 'string' || !event.revisionId
        || revisionIds.has(event.revisionId)
        || event.evidence.eventType !== 'mask-stage-review'
        || event.evidence.qualityState !== event.state || nextEligible === null
        || !exactEvidenceKeys(event, [
          'eventType', 'qualityState', 'artifactId', 'maskChecksum', 'recipeChecksum',
          'qualityChecksum', 'eligibleRegionCount', 'rubyRegionCount',
          'rubyRegionIdsByPrimary', 'coverageChecks', 'collateralChecks', 'imageRevision',
        ])
        || typeof nextQuality !== 'string' || !sha256.test(nextQuality)
        || (qualityChecksum !== null && nextQuality !== qualityChecksum)
        || nextRubyMapping === null || (rubyMapping !== null && nextRubyMapping !== rubyMapping)
        || nextRubyCount === null || nextRubyCount !== linkedRubyCount
        || imageRevision === null || imageRevision < 1
        || (latestStateImageRevision !== null && imageRevision <= latestStateImageRevision)
        || (eligibleCount !== null && nextEligible !== eligibleCount)
        || (!na && !accepted && !rejected)
        || (!na && (!artifact || !artifactEntry || !completed.has(artifactEntry[0])
          || artifact.evidence.recipeChecksum !== currentRecipeChecksum
          || reviewRecipeChecksum !== currentRecipeChecksum
          || typeof event.evidence.maskChecksum !== 'string'
          || event.evidence.maskChecksum !== artifact.evidence.maskChecksum
          || event.evidence.recipeChecksum !== artifact.evidence.recipeChecksum))
        || (accepted && lastRejectedArtifact !== null && (
          latestDraftAfterRejectSequence === null
          || artifactId === lastRejectedArtifact
          || !artifact
          || artifact.sequence <= latestDraftAfterRejectSequence
        ))) return 'locked';
      if (rejected) {
        lastRejectedArtifact = artifactId as string;
        latestDraftAfterRejectSequence = null;
      }
      else {
        terminal = true;
        g7NotApplicable = na;
      }
      currentStateChecksum = event.outputChecksum;
      qualityChecksum = nextQuality;
      rubyMapping = nextRubyMapping;
      latestStateImageRevision = imageRevision;
      revisionIds.add(event.revisionId);
      continue;
    }
    return 'locked';
  }
  return terminal ? 'G8' : 'G7';
}

export function deriveWorkflowPhase(
  generation: PageGeneration,
  rawEvents: PageLineageEvent[],
): WorkflowPhase {
  const events = [...rawEvents].sort((left, right) => left.sequence - right.sequence);
  if (
    !events.length
    || events.some((event) => typeof event.id !== 'string' || !event.id)
    || new Set(events.map((event) => event.id)).size !== events.length
    || events.some((event) => event.generationId !== generation.id)
    || events.some((event) => !Number.isInteger(event.sequence) || event.sequence < 1)
    || events.some((event, index) => index > 0 && event.sequence <= events[index - 1]!.sequence)
    || events.some((event, index) => index > 0 && event.sequence !== events[index - 1]!.sequence + 1)
    || events.at(-1)!.sequence !== generation.nextSequence - 1
  ) return 'locked';

  let noTextIndex = -1;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]!;
    if (
      event.gate === 'G3_textPresence'
      && event.operation === 'text-presence-decision'
      && event.state === 'accepted'
      && event.decision === 'no-text'
    ) {
      noTextIndex = index;
      break;
    }
  }
  if (noTextIndex >= 0) {
    return events.slice(noTextIndex + 1).some((event) =>
      event.gate === 'G4_regions'
      || event.gate === 'G5_background'
      || event.gate === 'G6_ocr'
      || event.gate === 'G7_mask'
      || event.gate === 'G8_cleanPlate'
      || event.gate === 'G9_translation'
      || event.gate === 'G10_typeset'
    ) ? 'locked' : 'no-text';
  }

  const firstG5Index = events.findIndex((event) => event.gate === 'G5_background');
  if (firstG5Index >= 0) {
    const previousG4 = events
      .slice(0, firstG5Index)
      .filter((event) => event.gate === 'G4_regions')
      .at(-1);
    if (
      previousG4?.operation !== 'regions-stage-review'
      || previousG4.state !== 'accepted'
      || events.slice(firstG5Index + 1).some((event) => event.gate === 'G4_regions')
    ) return 'locked';

    let g5Terminal: PageLineageEvent | null = null;
    let g6StartIndex = events.length;
    for (let index = firstG5Index; index < events.length; index += 1) {
      const event = events[index]!;
      if (event.gate === 'G6_ocr') {
        if (!g5Terminal) return 'locked';
        g6StartIndex = index;
        break;
      }
      if (event.gate !== 'G5_background' || g5Terminal) return 'locked';
      if (event.operation === 'background-classification-reviewed' && event.state === 'pending') {
        continue;
      }
      if (
        event.operation === 'background-stage-review'
        && (event.state === 'accepted' || event.state === 'not-applicable')
      ) {
        g5Terminal = event;
        continue;
      }
      return 'locked';
    }
    if (!g5Terminal) return 'G5';
    if (g6StartIndex === events.length) return 'G6';

    const sha256 = /^[0-9a-f]{64}$/;
    let expectedChecksum: string | null = null;
    const enqueued = new Map<string, PageLineageEvent>();
    const produced = new Map<string, PageLineageEvent>();
    const completed = new Set<string>();
    const failed = new Set<string>();
    const reviewedTargets = new Set<string>();
    let expectedRegionCount: number | null = null;
    let expectedEligibleCount: number | null = null;
    let publishedAttemptCount = 0;
    let g6Terminal = false;
    const evidenceCount = (event: PageLineageEvent, key: string): number | null => {
      const value = event.evidence[key];
      return typeof value === 'number' && Number.isInteger(value) && value >= 0
        ? value
        : null;
    };
    for (let index = g6StartIndex; index < events.length; index += 1) {
      const event = events[index]!;
      if (event.gate === 'G7_mask') {
        if (!g6Terminal || !expectedChecksum) return 'locked';
        return deriveG7Phase(generation, events.slice(index), expectedChecksum);
      }
      if (
        event.gate !== 'G6_ocr'
        || g6Terminal
        || event.parentChecksum !== g5Terminal.outputChecksum
      ) return 'locked';
      const itemId = event.jobItemId;
      if (event.operation === 'ocr-job-enqueued') {
        const eligibleCount = evidenceCount(event, 'eligibleRegionCount');
        if (
          event.state !== 'pending'
          || event.stage !== 'ocr'
          || event.decision !== null
          || event.reason !== 'job-enqueued'
          || event.parameterHash !== generation.parameterSetHash
          || !event.jobId
          || !itemId
          || enqueued.has(itemId)
          || [...enqueued.keys()].some((key) => !completed.has(key) && !failed.has(key))
          || !event.inputChecksum
          || !sha256.test(event.inputChecksum)
          || event.outputChecksum !== event.inputChecksum
          || (expectedChecksum !== null && event.inputChecksum !== expectedChecksum)
          || !event.provider
          || event.evidence.eventType !== 'job-enqueued'
          || event.evidence.qualityState !== 'pending-review'
          || event.evidence.targetKind !== 'region-set'
          || eligibleCount === null
          || eligibleCount < 1
          || (expectedEligibleCount !== null && eligibleCount !== expectedEligibleCount)
        ) return 'locked';
        enqueued.set(itemId, event);
        expectedEligibleCount = eligibleCount;
        expectedChecksum = event.inputChecksum;
        continue;
      }
      if (event.operation === 'ocr-attempts-produced') {
        const enqueue = itemId ? enqueued.get(itemId) : undefined;
        const regionCount = evidenceCount(event, 'regionCount');
        const eligibleCount = evidenceCount(event, 'eligibleRegionCount');
        const attemptedCount = evidenceCount(event, 'attemptedRegionCount');
        const attemptCount = evidenceCount(event, 'ocrAttemptCount');
        if (
          event.state !== 'pending'
          || event.stage !== 'ocr'
          || event.decision !== null
          || event.reason !== 'source-review-required'
          || !itemId
          || !enqueue
          || event.jobId !== enqueue.jobId
          || produced.has(itemId)
          || failed.has(itemId)
          || event.inputChecksum !== expectedChecksum
          || event.inputChecksum !== enqueue.inputChecksum
          || !event.outputChecksum
          || !sha256.test(event.outputChecksum)
          || event.provider !== enqueue.provider
          || !event.parameterHash
          || !sha256.test(event.parameterHash)
          || event.evidence.eventType !== 'ocr-attempts-produced'
          || event.evidence.qualityState !== 'pending-review'
          || event.evidence.targetKind !== 'region-set'
          || regionCount === null
          || eligibleCount === null
          || attemptedCount === null
          || attemptCount === null
          || eligibleCount < 1
          || regionCount < eligibleCount
          || attemptedCount !== eligibleCount
          || attemptCount !== eligibleCount * 2
          || (expectedRegionCount !== null && regionCount !== expectedRegionCount)
          || (expectedEligibleCount !== null && eligibleCount !== expectedEligibleCount)
        ) return 'locked';
        produced.set(itemId, event);
        expectedRegionCount = regionCount;
        expectedEligibleCount = eligibleCount;
        publishedAttemptCount += attemptCount;
        expectedChecksum = event.outputChecksum;
        continue;
      }
      if (event.operation === 'ocr-job-completed') {
        const enqueue = itemId ? enqueued.get(itemId) : undefined;
        const publication = itemId ? produced.get(itemId) : undefined;
        const eligibleCount = evidenceCount(event, 'eligibleRegionCount');
        const attemptCount = evidenceCount(event, 'ocrAttemptCount');
        const publishedEligibleCount = publication
          ? evidenceCount(publication, 'eligibleRegionCount')
          : null;
        const publishedItemAttemptCount = publication
          ? evidenceCount(publication, 'ocrAttemptCount')
          : null;
        if (
          event.state !== 'pending'
          || event.stage !== 'ocr'
          || event.decision !== null
          || event.reason !== 'review-required'
          || !itemId
          || !enqueue
          || !publication
          || event.jobId !== enqueue.jobId
          || completed.has(itemId)
          || failed.has(itemId)
          || event.inputChecksum !== enqueue.inputChecksum
          || event.outputChecksum !== publication.outputChecksum
          || event.outputChecksum !== expectedChecksum
          || event.provider !== publication.provider
          || event.modelVersion !== publication.modelVersion
          || event.parameterHash !== publication.parameterHash
          || event.evidence.eventType !== 'job-completed'
          || event.evidence.qualityState !== 'pending-review'
          || event.evidence.targetKind !== 'image'
          || eligibleCount === null
          || attemptCount === null
          || eligibleCount !== publishedEligibleCount
          || attemptCount !== publishedItemAttemptCount
        ) return 'locked';
        completed.add(itemId);
        continue;
      }
      if (event.operation === 'ocr-job-failed') {
        const enqueue = itemId ? enqueued.get(itemId) : undefined;
        if (
          event.state !== 'blocked'
          || event.stage !== 'ocr'
          || event.decision !== null
          || event.reason !== 'job-execution-failed'
          || event.parameterHash !== generation.parameterSetHash
          || !itemId
          || !enqueue
          || event.jobId !== enqueue.jobId
          || produced.has(itemId)
          || completed.has(itemId)
          || failed.has(itemId)
          || event.inputChecksum !== enqueue.inputChecksum
          || event.outputChecksum !== null
          || event.provider !== enqueue.provider
          || event.evidence.eventType !== 'job-failed'
          || event.evidence.qualityState !== 'blocked'
          || event.evidence.targetKind !== 'image'
        ) return 'locked';
        failed.add(itemId);
        continue;
      }
      if (event.operation === 'ocr-source-reviewed') {
        const targetRegionId = event.evidence.targetRegionId;
        const selectedAttemptId = event.evidence.selectedAttemptId;
        const regionCount = evidenceCount(event, 'regionCount');
        const eligibleCount = evidenceCount(event, 'eligibleRegionCount');
        const attemptedCount = evidenceCount(event, 'attemptedRegionCount');
        const reviewedCount = evidenceCount(event, 'reviewedRegionCount');
        const activeJobExists = [...enqueued.keys()].some(
          (key) => !completed.has(key) && !failed.has(key),
        );
        const nextReviewedTargets = new Set(reviewedTargets);
        if (typeof targetRegionId === 'string' && targetRegionId.trim()) {
          nextReviewedTargets.add(targetRegionId);
        }
        if (
          event.state !== 'pending'
          || event.stage !== 'ocr'
          || !event.provider
          || event.modelVersion !== null
          || event.parameterHash !== generation.parameterSetHash
          || event.jobId !== null
          || event.jobItemId !== null
          || event.decision !== 'source-text-trusted'
          || !['original-attempt', 'quality-attempt', 'manual-correction'].includes(
            event.reason ?? '',
          )
          || event.evidence.eventType !== 'ocr-source-reviewed'
          || event.evidence.qualityState !== 'pending-review'
          || event.evidence.targetKind !== 'region'
          || typeof targetRegionId !== 'string'
          || !targetRegionId.trim()
          || typeof selectedAttemptId !== 'string'
          || !selectedAttemptId.trim()
          || event.inputChecksum !== expectedChecksum
          || !event.outputChecksum
          || !sha256.test(event.outputChecksum)
          || completed.size === 0
          || activeJobExists
          || regionCount === null
          || eligibleCount === null
          || attemptedCount === null
          || reviewedCount === null
          || eligibleCount < 1
          || regionCount < eligibleCount
          || attemptedCount !== eligibleCount
          || reviewedCount < 1
          || reviewedCount > eligibleCount
          || reviewedCount !== nextReviewedTargets.size
          || (expectedRegionCount !== null && regionCount !== expectedRegionCount)
          || (expectedEligibleCount !== null && eligibleCount !== expectedEligibleCount)
        ) return 'locked';
        reviewedTargets.add(targetRegionId);
        expectedRegionCount = regionCount;
        expectedEligibleCount = eligibleCount;
        expectedChecksum = event.outputChecksum;
        continue;
      }
      if (event.operation === 'ocr-stage-review') {
        const terminalChecksum: string | null = expectedChecksum ?? event.inputChecksum;
        const regionCount = evidenceCount(event, 'regionCount');
        const eligibleCount = evidenceCount(event, 'eligibleRegionCount');
        const attemptedCount = evidenceCount(event, 'attemptedRegionCount');
        const reviewedCount = evidenceCount(event, 'reviewedRegionCount');
        const attemptCount = evidenceCount(event, 'ocrAttemptCount');
        const activeJobExists = [...enqueued.keys()].some(
          (key) => !completed.has(key) && !failed.has(key),
        );
        const acceptedTerminal = event.state === 'accepted'
          && event.decision === 'ocr-trust-accepted'
          && event.reason === 'all-translatable-source-text-reviewed'
          && event.evidence.qualityState === 'accepted'
          && eligibleCount !== null
          && eligibleCount > 0
          && attemptedCount === eligibleCount
          && reviewedCount === eligibleCount
          && reviewedTargets.size === eligibleCount
          && completed.size > 0
          && attemptCount !== null
          && attemptCount === publishedAttemptCount
          && attemptCount >= eligibleCount * 2
          && attemptCount % 2 === 0;
        const notApplicableTerminal = event.state === 'not-applicable'
          && event.decision === 'ocr-not-applicable'
          && event.reason === 'no-translatable-regions'
          && event.evidence.qualityState === 'not-applicable'
          && eligibleCount === 0
          && attemptedCount === 0
          && reviewedCount === 0
          && attemptCount === 0
          && reviewedTargets.size === 0
          && enqueued.size === 0
          && produced.size === 0
          && completed.size === 0
          && failed.size === 0
          && publishedAttemptCount === 0;
        if (
          event.stage !== 'ocr'
          || event.provider !== null
          || event.modelVersion !== null
          || event.parameterHash !== generation.parameterSetHash
          || event.jobId !== null
          || event.jobItemId !== null
          || event.evidence.eventType !== 'ocr-stage-review'
          || event.evidence.targetKind !== 'region-set'
          || regionCount === null
          || eligibleCount === null
          || attemptedCount === null
          || reviewedCount === null
          || attemptCount === null
          || regionCount < eligibleCount
          || (expectedRegionCount !== null && regionCount !== expectedRegionCount)
          || (expectedEligibleCount !== null && eligibleCount !== expectedEligibleCount)
          || (!acceptedTerminal && !notApplicableTerminal)
          || !terminalChecksum
          || !sha256.test(terminalChecksum)
          || event.inputChecksum !== terminalChecksum
          || event.outputChecksum !== terminalChecksum
          || activeJobExists
        ) return 'locked';
        expectedChecksum = terminalChecksum;
        g6Terminal = true;
        continue;
      }
      return 'locked';
    }
    return g6Terminal ? 'G7' : 'G6';
  }

  if (events.some((event) => event.gate === 'G6_ocr'
    || event.gate === 'G7_mask' || event.gate === 'G8_cleanPlate'
    || event.gate === 'G9_translation' || event.gate === 'G10_typeset')) return 'locked';

  const g4 = events.filter((event) => event.gate === 'G4_regions');
  const acceptedG4Index = g4.findIndex((event) =>
    event.operation === 'regions-stage-review' && event.state === 'accepted'
  );
  if (acceptedG4Index >= 0 && acceptedG4Index !== g4.length - 1) return 'locked';
  const latestG4 = g4.at(-1);
  if (latestG4) {
    return latestG4.operation === 'regions-stage-review' && latestG4.state === 'accepted'
      ? 'G5'
      : 'G4';
  }
  const latestG3 = events.filter((event) => event.gate === 'G3_textPresence').at(-1);
  return latestG3?.operation === 'text-presence-decision'
    && latestG3.state === 'accepted'
    && latestG3.decision === 'text-present'
    ? 'G4'
    : 'locked';
}

export function workflowPhase(context: G4PageContext | undefined): WorkflowPhase | null {
  if (!context || context.status !== 'active' || !context.generation) return null;
  return context.phase ?? deriveWorkflowPhase(context.generation, context.events);
}

export function backgroundClassificationRequired(region: Region): boolean {
  return region.type !== 'ruby'
    && (region.contentDisposition === 'translate' || region.contentDisposition === 'redraw-art');
}

export function backgroundClassificationComplete(
  region: Region,
  generationId: string,
): boolean {
  if (!backgroundClassificationRequired(region) || !region.backgroundCategory) return false;
  const confidence = region.backgroundConfidence;
  const rationaleCodes = region.backgroundRationaleCodes;
  return BACKGROUND_CATEGORIES.includes(region.backgroundCategory)
    && typeof confidence === 'number'
    && Number.isFinite(confidence)
    && confidence >= 0
    && confidence <= 1
    && Array.isArray(rationaleCodes)
    && rationaleCodes.length > 0
    && new Set(rationaleCodes).size === rationaleCodes.length
    && rationaleCodes.every((code) => BACKGROUND_RATIONALE_CODES.includes(code))
    && rationaleCodes.includes(BACKGROUND_RATIONALE_ANCHOR[region.backgroundCategory])
    && Boolean(region.backgroundReviewer)
    && region.backgroundGenerationId === generationId;
}

const OCR_QC_FLAGS = new Set([
  'none',
  'original-quality-disagree',
  'low-japanese-character-ratio',
  'ocr-empty-attempt',
  'ocr-garbled-attempt',
  'duplicate-fragment',
  'template-contamination',
  'manual-correction',
]);

export function ocrSourceReviewRequired(region: Region): boolean {
  return region.type !== 'ruby'
    && (region.contentDisposition === 'translate' || region.contentDisposition === 'redraw-art');
}

export function ocrSourceReviewComplete(region: Region, generationId: string): boolean {
  const review = region.ocrReview;
  if (!ocrSourceReviewRequired(region) || !review || !region.sourceText.trim()) return false;
  const checks = review.qcChecks;
  const flags = review.qcFlags;
  return (
    (review.sourceMode === 'original-attempt'
      || review.sourceMode === 'quality-attempt'
      || review.sourceMode === 'manual-correction')
    && Boolean(review.selectedAttemptId)
    && /^[0-9a-f]{64}$/.test(review.sourceTextChecksum)
    && Array.isArray(checks)
    && checks.length === OCR_QC_CHECKS.length
    && new Set(checks).size === checks.length
    && OCR_QC_CHECKS.every((check) => checks.includes(check))
    && Array.isArray(flags)
    && flags.length > 0
    && new Set(flags).size === flags.length
    && flags.every((flag) => OCR_QC_FLAGS.has(flag))
    && (flags.includes('none') ? flags.length === 1 : !flags.includes('none'))
    && Boolean(region.ocrReviewer)
    && region.ocrGenerationId === generationId
  );
}

export function imageHasActiveDetectJob(
  state: Pick<WorkbenchState, 'jobs'>,
  imageId: string,
): boolean {
  return state.jobs.some((job) =>
    job.kind === 'detect'
    && (job.status === 'queued' || job.status === 'running')
    && job.items.some((item) => item.imageId === imageId
      && (item.status === 'queued' || item.status === 'running'))
  );
}

export function imageHasActiveOCRJob(
  state: Pick<WorkbenchState, 'jobs'>,
  imageId: string,
): boolean {
  return state.jobs.some((job) =>
    job.kind === 'ocr'
    && (job.status === 'queued' || job.status === 'running')
    && job.items.some((item) => item.imageId === imageId
      && (item.status === 'queued' || item.status === 'running'))
  );
}

export function imageHasActiveMaskJob(
  state: Pick<WorkbenchState, 'jobs'>,
  imageId: string,
): boolean {
  return state.jobs.some((job) =>
    job.kind === 'mask'
    && (job.status === 'queued' || job.status === 'running')
    && job.items.some((item) => item.imageId === imageId
      && (item.status === 'queued' || item.status === 'running'))
  );
}

export function imageHasActiveCleanPlateJob(
  state: Pick<WorkbenchState, 'jobs'>,
  imageId: string,
): boolean {
  return state.jobs.some((job) =>
    job.kind === 'inpaint'
    && (job.status === 'queued' || job.status === 'running')
    && job.items.some((item) => item.imageId === imageId
      && (item.status === 'queued' || item.status === 'running'))
  );
}

export function maskRegionRequired(region: Region): boolean {
  return region.type !== 'ruby'
    && (region.contentDisposition === 'translate' || region.contentDisposition === 'redraw-art');
}

export function g7EditingLocked(
  state: Pick<
    WorkbenchState,
    | 'activeImageId'
    | 'g4Contexts'
    | 'g7DraftSavingImageId'
    | 'g7GateSavingImageId'
    | 'jobs'
  >,
  imageId = state.activeImageId,
): boolean {
  if (!imageId) return true;
  const context = state.g4Contexts[imageId];
  return !context
    || context.status !== 'active'
    || workflowPhase(context) !== 'G7'
    || Boolean(context.conflict || context.error)
    || state.g7DraftSavingImageId === imageId
    || state.g7GateSavingImageId === imageId
    || imageHasActiveMaskJob(state, imageId);
}

export function g8EditingLocked(
  state: Pick<
    WorkbenchState,
    'activeImageId' | 'g4Contexts' | 'g8GateSavingImageId' | 'jobs'
  >,
  imageId = state.activeImageId,
): boolean {
  if (!imageId) return true;
  const context = state.g4Contexts[imageId];
  return !context
    || context.status !== 'active'
    || workflowPhase(context) !== 'G8'
    || Boolean(context.conflict || context.error)
    || state.g8GateSavingImageId === imageId
    || imageHasActiveCleanPlateJob(state, imageId);
}

export function g4EditingLocked(
  state: Pick<
    WorkbenchState,
    | 'activeImageId'
    | 'g4Contexts'
    | 'pendingG4Mutations'
    | 'g4SavingImageId'
    | 'g4GateSavingImageId'
    | 'jobs'
  >,
  imageId = state.activeImageId,
): boolean {
  if (!imageId) return false;
  const context = state.g4Contexts[imageId];
  if (!context || context.status === 'loading' || context.status === 'error') return true;
  if (context.status === 'legacy') return false;
  return Boolean(
    workflowPhase(context) !== 'G4'
    || context.conflict
    || context.error
    || state.g4SavingImageId === imageId
    || state.g4GateSavingImageId === imageId
    || state.pendingG4Mutations.some((mutation) => mutation.imageId === imageId)
    || imageHasActiveDetectJob(state, imageId)
  );
}

export function g5EditingLocked(
  state: Pick<
    WorkbenchState,
    | 'activeImageId'
    | 'g4Contexts'
    | 'g5SavingRegionId'
    | 'g5GateSavingImageId'
  >,
  imageId = state.activeImageId,
): boolean {
  if (!imageId) return true;
  const context = state.g4Contexts[imageId];
  return !context
    || context.status !== 'active'
    || workflowPhase(context) !== 'G5'
    || Boolean(context.conflict || context.error)
    || Boolean(state.g5SavingRegionId)
    || state.g5GateSavingImageId === imageId;
}

export function g6EditingLocked(
  state: Pick<
    WorkbenchState,
    | 'activeImageId'
    | 'g4Contexts'
    | 'g6SavingRegionId'
    | 'g6GateSavingImageId'
    | 'jobs'
  >,
  imageId = state.activeImageId,
): boolean {
  if (!imageId) return true;
  const context = state.g4Contexts[imageId];
  return !context
    || context.status !== 'active'
    || workflowPhase(context) !== 'G6'
    || Boolean(context.conflict || context.error)
    || Boolean(state.g6SavingRegionId)
    || state.g6GateSavingImageId === imageId
    || imageHasActiveOCRJob(state, imageId);
}

export function projectPagesAreLegacy(
  state: Pick<WorkbenchState, 'images' | 'g4Contexts'>,
): boolean {
  return state.images.every((image) => state.g4Contexts[image.id]?.status === 'legacy');
}

export function latestG4RegionChecksum(context: G4PageContext | undefined): string | null {
  if (!context || context.status !== 'active') return null;
  const event = [...context.events]
    .reverse()
    .find((entry) => entry.gate === 'G4_regions' && Boolean(entry.outputChecksum));
  return event?.outputChecksum ?? null;
}

export function g4RegionsAccepted(context: G4PageContext | undefined): boolean {
  if (!context || context.status !== 'active') return false;
  const latest = [...context.events].reverse().find((event) => event.gate === 'G4_regions');
  return latest?.operation === 'regions-stage-review' && latest.state === 'accepted';
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
      requireAIInpaintBeforeDownstream:
        typeof rawSettings.requireAIInpaintBeforeDownstream === 'boolean'
          ? rawSettings.requireAIInpaintBeforeDownstream
          : DEFAULT_PROJECT_SETTINGS.requireAIInpaintBeforeDownstream,
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

export const AI_REDRAW_PREPROCESSING: PreprocessingSettings = {
  profile: 'visual-quality',
  enableUpscale: true,
  upscaleFactor: 4,
  enableDenoise: true,
  enableSharpen: true,
  enableContrastEnhance: true,
  enableEdgeOptimize: false,
  enableBinarize: false,
  threshold: 180,
};

export function preferredAiRedrawProvider(
  providers: AppCapabilities['providers'],
): 'realesrgan-onnx' | 'realesrgan-ncnn' | null {
  const onnx = providers.find((provider) => (
    provider.kind === 'preprocessor' && provider.id === 'realesrgan-onnx'
  ));
  const ncnn = providers.find((provider) => (
    provider.kind === 'preprocessor' && provider.id === 'realesrgan-ncnn'
  ));
  if (onnx?.available) return 'realesrgan-onnx';
  if (ncnn?.available) return 'realesrgan-ncnn';
  return null;
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

function hydrateProcessingErrors(value: ImageAsset['processingErrors'] | undefined): ImageAsset['processingErrors'] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry) =>
    Boolean(
      entry
      && typeof entry === 'object'
      && typeof entry.stage === 'string'
      && typeof entry.error === 'string'
      && entry.stage.length > 0
      && entry.error.length > 0
    ),
  );
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
    inpaintCandidateGenerationId: typeof image.inpaintCandidateGenerationId === 'string'
      ? image.inpaintCandidateGenerationId
      : null,
    inpaintAiRejectedCandidateIds: Array.isArray(image.inpaintAiRejectedCandidateIds)
      ? [...new Set(image.inpaintAiRejectedCandidateIds.filter(
        (candidateId): candidateId is string => typeof candidateId === 'string',
      ))]
      : [],
    inpaintFallback: image.inpaintFallback?.state === 'approved'
      ? image.inpaintFallback
      : { state: 'pending' },
    typesetOverflowCount: overflowRegionIds.length,
    typesetOverflowRegionIds: overflowRegionIds,
    processingErrors: hydrateProcessingErrors(image.processingErrors),
    error: typeof image.error === 'string' && image.error.length > 0 ? image.error : undefined,
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

function hydrateOCRReview(value: Region['ocrReview'] | undefined): Region['ocrReview'] {
  if (!value || typeof value !== 'object') return null;
  const sourceMode = value.sourceMode;
  const checks = value.qcChecks;
  const flags = value.qcFlags;
  if (
    (sourceMode !== 'original-attempt'
      && sourceMode !== 'quality-attempt'
      && sourceMode !== 'manual-correction')
    || typeof value.selectedAttemptId !== 'string'
    || !value.selectedAttemptId
    || typeof value.sourceTextChecksum !== 'string'
    || !/^[0-9a-f]{64}$/.test(value.sourceTextChecksum)
    || !Array.isArray(checks)
    || checks.length !== OCR_QC_CHECKS.length
    || new Set(checks).size !== checks.length
    || !OCR_QC_CHECKS.every((check) => checks.includes(check))
    || !Array.isArray(flags)
    || flags.length === 0
    || new Set(flags).size !== flags.length
    || !flags.every((flag) => OCR_QC_FLAGS.has(flag))
    || (flags.includes('none') && flags.length !== 1)
  ) return null;
  return {
    sourceMode,
    selectedAttemptId: value.selectedAttemptId,
    sourceTextChecksum: value.sourceTextChecksum,
    qcChecks: [...checks],
    qcFlags: [...flags],
  };
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
  const ocrReview = hydrateOCRReview(region.ocrReview);
  return {
    ...region,
    x: Number(region.x ?? 0),
    y: Number(region.y ?? 0),
    width: Math.max(1, Number(region.width ?? 1)),
    height: Math.max(1, Number(region.height ?? 1)),
    rotation: Number(region.rotation ?? 0),
    sourceText: region.sourceText ?? '',
    translationText: region.translationText ?? '',
    translationProvider: region.translationProvider ?? null,
    type: region.type ?? 'dialogue',
    direction: region.direction ?? 'auto',
    order: Number(region.order ?? 0),
    paragraphGroupId: region.paragraphGroupId ?? null,
    rubyParentId: region.rubyParentId ?? null,
    contentDisposition: region.contentDisposition ?? null,
    backgroundCategory: region.backgroundCategory ?? null,
    backgroundConfidence: region.backgroundConfidence === null
      || region.backgroundConfidence === undefined
      || !Number.isFinite(Number(region.backgroundConfidence))
      ? null
      : Number(region.backgroundConfidence),
    backgroundRationaleCodes: Array.isArray(region.backgroundRationaleCodes)
      ? region.backgroundRationaleCodes.filter(
          (code): code is BackgroundRationaleCode => BACKGROUND_RATIONALE_CODES.includes(code),
        )
      : null,
    backgroundReviewer: region.backgroundReviewer ?? null,
    backgroundGenerationId: region.backgroundGenerationId ?? null,
    ocrReview,
    ocrReviewer: ocrReview ? region.ocrReviewer ?? null : null,
    ocrGenerationId: ocrReview ? region.ocrGenerationId ?? null : null,
    detectorJobItemId: region.detectorJobItemId ?? null,
    detectorCandidateIndex: region.detectorCandidateIndex === null
      || region.detectorCandidateIndex === undefined
      ? null
      : Number(region.detectorCandidateIndex),
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
          : repairMethod === 'screentone'
            ? 'screentone'
            : 'telea',
      maskPadding: Number(rawRepair.maskPadding ?? rawRepair.padding ?? DEFAULT_REPAIR_SETTINGS.maskPadding),
      maskMode: rawRepair.maskMode === 'region'
        ? 'region'
        : rawRepair.maskMode === 'manual'
          ? 'manual'
          : 'text',
      textPolarity: rawRepair.textPolarity === 'dark' || rawRepair.textPolarity === 'light'
        ? rawRepair.textPolarity
        : 'auto',
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

const REGION_PATCH_SCALAR_KEYS = [
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
  'paragraphGroupId',
  'rubyParentId',
  'contentDisposition',
  'confidence',
  'ignored',
  'confirmed',
] as const satisfies ReadonlyArray<keyof Region>;

const G4_REGION_UPDATE_KEYS = new Set<keyof Region>([
  'x',
  'y',
  'width',
  'height',
  'rotation',
  'type',
  'direction',
  'paragraphGroupId',
  'rubyParentId',
  'contentDisposition',
]);

function activeG4Patch(patch: RegionUpdatePatch): G4RegionPatch | null {
  const keys = Object.keys(patch) as Array<keyof RegionUpdatePatch>;
  if (keys.some((key) => !G4_REGION_UPDATE_KEYS.has(key as keyof Region))) return null;
  return Object.fromEntries(
    keys.map((key) => [key, patch[key]]),
  ) as G4RegionPatch;
}

function replaceG4Mutation(
  pending: G4RegionMutation[],
  next: G4RegionMutation,
): G4RegionMutation[] {
  const existing = pending.find((mutation) => mutation.region.id === next.region.id);
  if (!existing) return [...pending, next];
  if (existing.kind === 'create' && next.kind === 'update') {
    return pending.map((mutation) => mutation.region.id === next.region.id
      ? { ...existing, mutationId: next.mutationId, region: next.region }
      : mutation);
  }
  if (existing.kind === 'create' && next.kind === 'delete') {
    return pending.filter((mutation) => mutation.region.id !== next.region.id);
  }
  if (existing.kind === 'update' && next.kind === 'update') {
    return pending.map((mutation) => mutation.region.id === next.region.id
      ? {
          ...next,
          patch: { ...(existing.patch ?? {}), ...(next.patch ?? {}) },
        }
      : mutation);
  }
  return pending.map((mutation) => mutation.region.id === next.region.id ? next : mutation);
}

function sparseRegionPatch(before: Region, after: Region): RegionUpdatePatch {
  const patch: RegionUpdatePatch = {};
  for (const key of REGION_PATCH_SCALAR_KEYS) {
    if (before[key] !== after[key]) Object.assign(patch, { [key]: after[key] });
  }
  const style = sparseNestedRegionPatch(before.style, after.style);
  if (Object.keys(style).length) patch.style = style;
  const repair = sparseNestedRegionPatch(before.repair, after.repair);
  if (Object.keys(repair).length) patch.repair = repair;
  return patch;
}

function sparseNestedRegionPatch<T extends object>(before: T, after: T): NestedRegionPatch<T> {
  const patch: Record<string, unknown> = {};
  const beforeRecord = before as Record<string, unknown>;
  const afterRecord = after as Record<string, unknown>;
  const keys = new Set([...Object.keys(beforeRecord), ...Object.keys(afterRecord)]);
  for (const key of keys) {
    const beforeValue = beforeRecord[key];
    const afterValue = afterRecord[key];
    if (beforeValue === undefined && afterValue === undefined) continue;
    if (afterValue === undefined) {
      patch[key] = null;
    } else if (JSON.stringify(beforeValue) !== JSON.stringify(afterValue)) {
      patch[key] = afterValue;
    }
  }
  return patch as NestedRegionPatch<T>;
}

function applyNestedRegionPatch<T extends object>(current: T, patch: NestedRegionPatch<T>): T {
  const merged = { ...current } as Record<string, unknown>;
  for (const [key, value] of Object.entries(patch)) {
    if (value === null || value === undefined) delete merged[key];
    else merged[key] = value;
  }
  return merged as T;
}

function fullRegionPatch(region: Region): RegionUpdatePatch {
  const patch: RegionUpdatePatch = {
    style: { ...region.style },
    repair: { ...region.repair },
  };
  for (const key of REGION_PATCH_SCALAR_KEYS) {
    Object.assign(patch, { [key]: region[key] });
  }
  return patch;
}

function mergeRegionPatches(
  previous: RegionUpdatePatch | undefined,
  next: RegionUpdatePatch | undefined,
): RegionUpdatePatch | undefined {
  if (!previous) return next;
  if (!next) return previous;
  return {
    ...previous,
    ...next,
    style: previous.style || next.style
      ? { ...previous.style, ...next.style }
      : undefined,
    repair: previous.repair || next.repair
      ? { ...previous.repair, ...next.repair }
      : undefined,
  };
}

function hasTrustInputChange(before: Region, after: Region): boolean {
  const keys: Array<keyof Region> = [
    'x',
    'y',
    'width',
    'height',
    'rotation',
    'sourceText',
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
        ? {
            mutationId: next.mutationId,
            kind: 'create',
            imageId: next.imageId,
            region: next.region,
            patch: next.patch,
            expectedRevision: 0,
          }
        : mutation,
    );
  }
  if (existing.kind === 'create' && next.kind === 'delete') {
    if (inFlightRegionIds.has(existing.region.id)) {
      return pending.map((mutation) =>
        mutation.region.id === next.region.id ? next : mutation,
      );
    }
    return pending.filter((mutation) => mutation.region.id !== next.region.id);
  }
  if (existing.kind === 'update' && next.kind === 'update') {
    // Keep the cumulative intent until the active request succeeds. If it
    // fails, this mutation is the only retry authority and must still contain
    // fields from both edits. The success path rebases it against the saved
    // response below so already-applied fields are not sent again.
    const patch = mergeRegionPatches(existing.patch, next.patch);
    return pending.map((mutation) => (
      mutation.region.id === next.region.id ? { ...next, patch } : mutation
    ));
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
  const existingByRegion = new Map(
    pending.map((mutation) => [mutation.region.id, mutation]),
  );
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
      const currentRegion = before.get(regionId);
      const existing = existingByRegion.get(regionId);
      if (
        serverRevision !== undefined
        && !currentRegion
        && existing?.kind === 'delete'
        && !inFlightRegionIds.has(regionId)
      ) continue;
      const delta = currentRegion
        ? sparseRegionPatch(currentRegion, region)
        : fullRegionPatch(region);
      if (currentRegion && !Object.keys(delta).length && !existing) continue;
      const patch = serverRevision !== undefined && existing?.kind === 'update'
        ? mergeRegionPatches(existing.patch, delta)
        : delta;
      result.push({
        mutationId: id('mutation'),
        kind: serverRevision === undefined ? 'create' : 'update',
        imageId,
        region,
        ...(serverRevision === undefined ? {} : {
          patch,
        }),
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
      } else if (
        !after.has(regionId)
        && serverRevision === undefined
        && existingByRegion.get(regionId)?.kind === 'create'
        && inFlightRegionIds.has(regionId)
      ) {
        result.push({
          mutationId: id('mutation'),
          kind: 'delete',
          imageId,
          region,
          expectedRevision: 0,
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
        ) || state.pendingG4Mutations.some((mutation) => mutation.imageId === image.id);
        const localRegions = state.regionsByImage[image.id];
        return hasPendingEdit && localRegions
          ? updateImageCounts([image], image.id, localRegions, true)[0] ?? image
          : image;
      }),
    };
  });
}

async function loadProjectG4Contexts(imageIds: string[], force = false): Promise<boolean> {
  const uniqueImageIds = [...new Set(imageIds)];
  let loaded = true;
  const concurrency = 8;
  for (let offset = 0; offset < uniqueImageIds.length; offset += concurrency) {
    const batch = uniqueImageIds.slice(offset, offset + concurrency);
    const results = await Promise.all(batch.map((imageId) =>
      useWorkbenchStore.getState().loadG4Context(imageId, force)
    ));
    if (results.some((result) => !result)) loaded = false;
  }
  return loaded;
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
    const initial = useWorkbenchStore.getState();
    const project = initial.currentProject;
    if (!project || !initial.pendingProjectMutation) return;
    if (!projectPagesAreLegacy(initial)) {
      throw new Error('项目内页面血缘尚未全部确认为旧版，不能保存旧版项目参数。');
    }
    await synchronizeProject(project.id);
    const latest = useWorkbenchStore.getState();
    const mutation = latest.pendingProjectMutation;
    if (!mutation || latest.currentProject?.id !== project.id) return;
    if (!projectPagesAreLegacy(latest)) {
      throw new Error('项目内页面血缘状态已变化，旧版项目参数写入已停止。');
    }
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

async function flushG4Mutations(): Promise<void> {
  while (true) {
    const state = useWorkbenchStore.getState();
    const mutation = state.pendingG4Mutations[0];
    if (!mutation) return;
    const context = state.g4Contexts[mutation.imageId];
    const image = state.images.find((entry) => entry.id === mutation.imageId);
    const lineage = context ? mutationLineage(context) : null;
    if (
      !context
      || context.status !== 'active'
      || !context.generation
      || workflowPhase(context) !== 'G4'
      || !image
      || !lineage
    ) {
      throw new Error('当前页的 G4 血缘上下文不可用，请重载本页后再编辑。');
    }
    if (context.conflict || context.error || imageHasActiveDetectJob(state, mutation.imageId)) {
      throw new Error(context.error || '当前页正在检测或存在版本冲突，请重载后再编辑。');
    }

    useWorkbenchStore.setState({ g4SavingImageId: mutation.imageId });
    let saved: Region | null = null;
    try {
      if (mutation.kind === 'create') {
        saved = hydrateRegion(await api.createG4Region(
          mutation.imageId,
          mutation.region,
          image.revision,
          lineage,
        ));
      } else if (mutation.kind === 'update') {
        saved = hydrateRegion(await api.updateG4Region(
          mutation.region.id,
          mutation.patch ?? {},
          mutation.expectedRevision,
          image.revision,
          lineage,
        ));
      } else {
        await api.deleteG4Region(
          mutation.region.id,
          mutation.expectedRevision,
          image.revision,
          lineage,
        );
      }
    } catch (error) {
      const message = errorMessage(error);
      useWorkbenchStore.setState((current) => ({
        g4SavingImageId: null,
        saveError: message,
        revisionConflict: error instanceof ApiError && error.status === 409,
        g4Contexts: {
          ...current.g4Contexts,
          [mutation.imageId]: {
            ...context,
            error: message,
            conflict: error instanceof ApiError && error.status === 409,
          },
        },
      }));
      throw error;
    }

    useWorkbenchStore.setState((current) => {
      const oldId = mutation.region.id;
      const pendingG4Mutations = current.pendingG4Mutations.filter(
        (entry) => entry.mutationId !== mutation.mutationId,
      );
      const serverRegionRevisions = { ...current.serverRegionRevisions };
      delete serverRegionRevisions[oldId];
      if (!saved) return { pendingG4Mutations, serverRegionRevisions };
      serverRegionRevisions[saved.id] = saved.revision;
      const regions = (current.regionsByImage[mutation.imageId] ?? []).map((region) =>
        region.id === oldId ? saved as Region : region
      );
      return {
        pendingG4Mutations,
        serverRegionRevisions,
        regionsByImage: { ...current.regionsByImage, [mutation.imageId]: regions },
        selectedRegionIds: current.selectedRegionIds.map((regionId) =>
          regionId === oldId ? (saved as Region).id : regionId
        ),
        images: updateImageCounts(current.images, mutation.imageId, regions, true),
      };
    });

    try {
      const projectId = useWorkbenchStore.getState().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭，无法刷新 G4 权威状态。');
      await synchronizeImages(projectId);
      const contextLoaded = await useWorkbenchStore.getState().loadG4Context(mutation.imageId, true);
      if (!contextLoaded) throw new Error('G4 写入已提交，但无法读取新的血缘序号；请重载本页。');
      const regionsLoaded = await useWorkbenchStore.getState().loadRegions(mutation.imageId, true);
      if (!regionsLoaded) throw new Error('G4 写入已提交，但无法刷新权威文本框；请重载本页。');
      useWorkbenchStore.setState({ g4SavingImageId: null });
    } catch (error) {
      const message = errorMessage(error);
      useWorkbenchStore.setState((current) => {
        const refreshed = current.g4Contexts[mutation.imageId] ?? context;
        return {
          g4SavingImageId: null,
          saveError: message,
          g4Contexts: {
            ...current.g4Contexts,
            [mutation.imageId]: { ...refreshed, error: message },
          },
        };
      });
      throw error;
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
    return remaining.pendingRegionMutations.length
      || remaining.pendingG4Mutations.length
      || remaining.pendingProjectMutation
      ? performFlush()
      : true;
  }

  activeSave = (async () => {
    const beforeSave = useWorkbenchStore.getState();
    if (
      !beforeSave.pendingRegionMutations.length
      && !beforeSave.pendingG4Mutations.length
      && !beforeSave.pendingProjectMutation
    ) return true;
    const hadProjectMutation = Boolean(beforeSave.pendingProjectMutation);
    useWorkbenchStore.setState({ saving: true, saveError: '', revisionConflict: false });

    try {
      await flushProjectMutation();

      const pendingRegions = [...useWorkbenchStore.getState().pendingRegionMutations];
      for (const mutation of pendingRegions) {
        const currentAuthority = useWorkbenchStore.getState();
        const isCurrentAuthority = currentAuthority.pendingRegionMutations.some(
          (entry) => entry.mutationId === mutation.mutationId,
        );
        if (!isCurrentAuthority) continue;
        if (currentAuthority.g4Contexts[mutation.imageId]?.status !== 'legacy') {
          throw new Error('本页血缘尚未确认为旧版页面，旧版文本框写入已停止。');
        }
        inFlightRegionIds.add(mutation.region.id);
        let saved: Region | null = null;
        try {
          if (mutation.kind === 'create') {
            saved = hydrateRegion(await api.createRegion(mutation.imageId, mutation.region));
          } else if (mutation.kind === 'update') {
            saved = hydrateRegion(
              await api.updateRegion(mutation.region.id, {
                ...mutation.patch,
                expectedRevision: mutation.expectedRevision,
              } as Parameters<typeof api.updateRegion>[1]),
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
          } else if (mutation.kind === 'delete') {
            // An undo issued while delete is in flight must be a full create
            // recovery only if the delete succeeds. On failure the original
            // row still exists, so rebase the desired local region against the
            // delete snapshot and retain only genuine operator edits.
            useWorkbenchStore.setState((state) => {
              const recovery = state.pendingRegionMutations.find((entry) => (
                entry.mutationId !== mutation.mutationId
                && entry.region.id === mutation.region.id
                && entry.kind === 'update'
              ));
              if (!recovery) return {};
              const patch = sparseRegionPatch(mutation.region, recovery.region);
              return {
                pendingRegionMutations: Object.keys(patch).length
                  ? state.pendingRegionMutations.map((entry) => (
                      entry.mutationId === recovery.mutationId
                        ? {
                            ...entry,
                            patch,
                            expectedRevision: state.serverRegionRevisions[mutation.region.id]
                              ?? mutation.expectedRevision,
                          }
                        : entry
                    ))
                  : state.pendingRegionMutations.filter(
                      (entry) => entry.mutationId !== recovery.mutationId,
                    ),
              };
            });
          }
          throw error;
        } finally {
          inFlightRegionIds.delete(mutation.region.id);
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
            const rebasedPatch = newer.kind === 'create' || newer.kind === 'update'
              ? sparseRegionPatch(saved, rebasedRegion)
              : newer.patch;
            pendingRegionMutations = withoutCompleted.flatMap((entry) => {
              if (entry.mutationId !== newer.mutationId) return [entry];
              const nextKind = newer.kind === 'delete'
                ? 'delete'
                : newer.kind === 'confirm'
                  ? 'confirm'
                  : 'update';
              if (nextKind === 'update' && !Object.keys(rebasedPatch ?? {}).length) return [];
              return [{
                ...entry,
                kind: nextKind,
                expectedRevision: saved.revision,
                region: rebasedRegion,
                patch: rebasedPatch,
              }];
            });
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

      await flushG4Mutations();

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
      inFlightRegionIds.clear();
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
    if (
      remaining.pendingRegionMutations.length
      || remaining.pendingG4Mutations.length
      || remaining.pendingProjectMutation
    ) {
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
  focusRequest: 0,
  focusRegionIds: [] as string[],
  rightTab: 'text' as RightPanelTab,
  theme: storedTheme(),
  drawerOpen: false,
  queueRevealJobId: null as string | null,
  queueRevealItemId: null as string | null,
  shortcutsOpen: false,
  spacePressed: false,
  jobs: [] as Job[],
  g4Contexts: {} as Record<string, G4PageContext>,
  backgroundContexts: {} as Record<string, BackgroundGateContext>,
  backgroundLoading: {} as Record<string, boolean>,
  ocrContexts: {} as Record<string, OCRGateContext>,
  ocrLoading: {} as Record<string, boolean>,
  maskContexts: {} as Record<string, MaskGateContext>,
  maskLoading: {} as Record<string, boolean>,
  selectedMaskArtifactIds: {} as Record<string, string>,
  maskBitmapObservations: {} as Record<string, MaskBitmapObservation>,
  cleanPlateContexts: {} as Record<string, CleanPlateGateContext>,
  cleanPlateLoading: {} as Record<string, boolean>,
  selectedCleanPlateCandidateIds: {} as Record<string, string>,
  cleanPlateBitmapObservations: {} as Record<string, CleanPlateBitmapObservation>,
  translationContexts: {} as Record<string, TranslationGateContext>,
  translationLoading: {} as Record<string, boolean>,
  selectedTranslationCandidateIds: {} as Record<string, string>,
  typesetContexts: {} as Record<string, TypesetGateContext>,
  typesetLoading: {} as Record<string, boolean>,
  selectedTypesetCandidateIds: {} as Record<string, string>,
  typesetBitmapObservations: {} as Record<string, TypesetBitmapObservation>,
  typesetStyleDrafts: {} as Record<string, Record<string, TypesetRegionStyleInput>>,
  pendingG4Mutations: [] as G4RegionMutation[],
  g4SavingImageId: null as string | null,
  g4GateSavingImageId: null as string | null,
  g5SavingRegionId: null as string | null,
  g5GateSavingImageId: null as string | null,
  g6SavingRegionId: null as string | null,
  g6GateSavingImageId: null as string | null,
  g7DraftSavingImageId: null as string | null,
  g7GateSavingImageId: null as string | null,
  g8GateSavingImageId: null as string | null,
  g9GateSavingImageId: null as string | null,
  g10GateSavingImageId: null as string | null,
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
      g4LoadTokens.clear();
      backgroundLoadTokens.clear();
      ocrLoadTokens.clear();
      maskLoadTokens.clear();
      cleanPlateLoadTokens.clear();
      translationLoadTokens.clear();
      typesetLoadTokens.clear();
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
        g4Contexts: {},
        backgroundContexts: {},
        backgroundLoading: {},
        ocrContexts: {},
        ocrLoading: {},
        maskContexts: {},
        maskLoading: {},
        selectedMaskArtifactIds: {},
        maskBitmapObservations: {},
        cleanPlateContexts: {},
        cleanPlateLoading: {},
        selectedCleanPlateCandidateIds: {},
        cleanPlateBitmapObservations: {},
        translationContexts: {},
        translationLoading: {},
        selectedTranslationCandidateIds: {},
        typesetContexts: {},
        typesetLoading: {},
        selectedTypesetCandidateIds: {},
        typesetBitmapObservations: {},
        typesetStyleDrafts: {},
        pendingG4Mutations: [],
        g4SavingImageId: null,
        g4GateSavingImageId: null,
        g5SavingRegionId: null,
        g5GateSavingImageId: null,
        g6SavingRegionId: null,
        g6GateSavingImageId: null,
        g7DraftSavingImageId: null,
        g7GateSavingImageId: null,
        g8GateSavingImageId: null,
        g9GateSavingImageId: null,
        g10GateSavingImageId: null,
        pendingRegionMutations: [],
        pendingProjectMutation: null,
        past: [],
        future: [],
      });
      const lineageLoad = loadProjectG4Contexts(images.map((image) => image.id));
      if (firstImageId) await Promise.all([get().loadRegions(firstImageId), lineageLoad]);
      else await lineageLoad;
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
      const imageIds = get().images.map((image) => image.id);
      if (!(await loadProjectG4Contexts(imageIds, true))) {
        throw new Error('无法核对项目页面血缘，图像导入已锁定；请重载项目后再试。');
      }
      const authority = get();
      if (authority.currentProject?.id !== project.id || !projectPagesAreLegacy(authority)) {
        throw new Error('项目已存在活动或历史页面血缘，不能再导入图像。');
      }
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
      const lineageLoad = loadProjectG4Contexts(imported.map((image) => image.id));
      if (activeImageId) await Promise.all([get().loadRegions(activeImageId), lineageLoad]);
      else await lineageLoad;
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

  loadG4Context: async (imageId, force = false) => {
    const existing = get().g4Contexts[imageId];
    // A conflict or an uncertain write outcome is a manual-reconciliation
    // boundary. Background polling must never make the page editable again.
    if (existing?.conflict || existing?.error) return false;
    if (!force && existing && existing.status !== 'loading') return existing.status !== 'error';
    const requestToken = Symbol(imageId);
    g4LoadTokens.set(imageId, requestToken);
    set((state) => ({
      g4Contexts: {
        ...state.g4Contexts,
        [imageId]: {
          status: 'loading',
          generation: null,
          events: [],
          error: '',
          conflict: false,
        },
      },
    }));
    try {
      const readActiveGeneration = async (): Promise<PageGeneration | null> => {
        const generations = await api.listPageGenerations(imageId);
        const active = generations.filter((generation) => generation.state === 'active');
        if (active.length > 1) {
          throw new Error('服务端返回多个活动页代次；为防止血缘错绑，本页已锁定。');
        }
        if (active.length === 0 && generations.length > 0) {
          throw new Error('本页存在历史代次但没有唯一活动代次；请创建新代次后再操作。');
        }
        return active[0] ?? null;
      };
      let generation: PageGeneration | null = null;
      let events: PageLineageEvent[] = [];
      let stable = false;
      for (let attempt = 0; attempt < 2; attempt += 1) {
        const before = await readActiveGeneration();
        const observedEvents = before
          ? await api.listPageLineageEvents(before.id)
          : [];
        const after = await readActiveGeneration();
        if (
          before?.id === after?.id
          && before?.state === after?.state
          && before?.nextSequence === after?.nextSequence
        ) {
          generation = after;
          events = observedEvents;
          stable = true;
          break;
        }
      }
      if (!stable) {
        throw new Error('读取页代次时服务端血缘持续变化；为避免错绑，本页已锁定，请重载。');
      }
      if (g4LoadTokens.get(imageId) !== requestToken) return false;
      const sortedEvents = [...events].sort((left, right) => left.sequence - right.sequence);
      const phase = generation ? deriveWorkflowPhase(generation, sortedEvents) : undefined;
      const phaseError = phase === 'locked'
        ? '页代次事件序列不一致或阶段越界；本页已锁定，请核对服务端血缘。'
        : '';
      let applied = false;
      set((state) => {
        const current = state.g4Contexts[imageId];
        if (current?.conflict || current?.error) return {};
        applied = true;
        const backgroundContexts = { ...state.backgroundContexts };
        const background = backgroundContexts[imageId];
        if (
          !generation
          || (phase !== 'G5' && phase !== 'G6' && phase !== 'G7')
          || !background
          || background.generationId !== generation.id
          || background.nextSequence !== generation.nextSequence
        ) {
          delete backgroundContexts[imageId];
        }
        const ocrContexts = { ...state.ocrContexts };
        const ocr = ocrContexts[imageId];
        if (
          !generation
    || (phase !== 'G6' && phase !== 'G7' && phase !== 'G8')
          || !ocr
          || ocr.generationId !== generation.id
          || ocr.nextSequence !== generation.nextSequence
        ) {
          delete ocrContexts[imageId];
        }
        const maskContexts = { ...state.maskContexts };
        const mask = maskContexts[imageId];
        if (
          !generation
          || (phase !== 'G7' && phase !== 'G8')
          || !mask
          || mask.generationId !== generation.id
          || mask.nextSequence !== generation.nextSequence
        ) {
          delete maskContexts[imageId];
        }
        const cleanPlateContexts = { ...state.cleanPlateContexts };
        const cleanPlate = cleanPlateContexts[imageId];
        if (
          !generation
          || phase !== 'G8'
          || !cleanPlate
          || cleanPlate.generationId !== generation.id
          || cleanPlate.nextSequence !== generation.nextSequence
        ) {
          delete cleanPlateContexts[imageId];
        }
        const translationContexts = { ...state.translationContexts };
        const translation = translationContexts[imageId];
        if (
          !generation
          || phase !== 'G9'
          || !translation
          || translation.generationId !== generation.id
          || translation.nextSequence !== generation.nextSequence
        ) {
          delete translationContexts[imageId];
        }
        const typesetContexts = { ...state.typesetContexts };
        const typeset = typesetContexts[imageId];
        if (
          !generation
          || phase !== 'G10'
          || !typeset
          || typeset.generationId !== generation.id
          || typeset.nextSequence !== generation.nextSequence
        ) {
          delete typesetContexts[imageId];
        }
        return {
          backgroundContexts,
          ocrContexts,
          maskContexts,
          cleanPlateContexts,
          translationContexts,
          typesetContexts,
          g4Contexts: {
            ...state.g4Contexts,
            [imageId]: generation
              ? {
                  status: 'active',
                  generation,
                  events: sortedEvents,
                  phase,
                  error: phaseError,
                  conflict: false,
                }
              : {
                  status: 'legacy',
                  generation: null,
                  events: [],
                  error: '',
                  conflict: false,
                },
          },
        };
      });
      return applied && phase !== 'locked';
    } catch (error) {
      if (g4LoadTokens.get(imageId) !== requestToken) return false;
      set((state) => {
        const current = state.g4Contexts[imageId];
        if (current?.conflict || current?.error) return {};
        return {
          g4Contexts: {
            ...state.g4Contexts,
            [imageId]: {
              status: 'error',
              generation: null,
              events: [],
              error: errorMessage(error),
              conflict: error instanceof ApiError && error.status === 409,
            },
          },
        };
      });
      return false;
    } finally {
      if (g4LoadTokens.get(imageId) === requestToken) g4LoadTokens.delete(imageId);
    }
  },

  loadBackgroundContext: async (imageId, force = false) => {
    const context = get().g4Contexts[imageId];
    if (
      !context
      || context.status !== 'active'
      || (workflowPhase(context) !== 'G5' && workflowPhase(context) !== 'G6')
      || context.conflict
      || context.error
    ) return false;
    if (!force && get().backgroundContexts[imageId]) return true;
    const requestToken = Symbol(imageId);
    backgroundLoadTokens.set(imageId, requestToken);
    set((state) => ({
      backgroundLoading: { ...state.backgroundLoading, [imageId]: true },
    }));
    try {
      const background = await api.getBackgroundGateContext(imageId);
      if (backgroundLoadTokens.get(imageId) !== requestToken) return false;
      const current = get().g4Contexts[imageId];
      const image = get().images.find((entry) => entry.id === imageId);
      if (
        !current
        || current.status !== 'active'
        || !current.generation
        || background.generationId !== current.generation.id
        || background.nextSequence !== current.generation.nextSequence
        || !image
        || background.imageRevision !== image.revision
      ) {
        throw new Error('G5 上下文与当前页代次或图像版本不一致，请重载本页。');
      }
      set((state) => ({
        backgroundContexts: { ...state.backgroundContexts, [imageId]: background },
        backgroundLoading: { ...state.backgroundLoading, [imageId]: false },
      }));
      return true;
    } catch (error) {
      if (backgroundLoadTokens.get(imageId) !== requestToken) return false;
      const conflict = error instanceof ApiError && error.status === 409;
      const message = errorMessage(error);
      set((state) => {
        const current = state.g4Contexts[imageId];
        return {
          globalError: message,
          revisionConflict: conflict,
          backgroundLoading: { ...state.backgroundLoading, [imageId]: false },
          g4Contexts: current
            ? {
                ...state.g4Contexts,
                [imageId]: { ...current, error: message, conflict },
              }
            : state.g4Contexts,
        };
      });
      return false;
    } finally {
      if (backgroundLoadTokens.get(imageId) === requestToken) {
        backgroundLoadTokens.delete(imageId);
      }
    }
  },

  loadOCRContext: async (imageId, force = false) => {
    const context = get().g4Contexts[imageId];
    const phase = workflowPhase(context);
    if (
      !context
      || context.status !== 'active'
      || (phase !== 'G6' && phase !== 'G7' && phase !== 'G8')
      || context.conflict
      || context.error
    ) return false;
    if (!force && get().ocrContexts[imageId]) return true;
    const requestToken = Symbol(imageId);
    ocrLoadTokens.set(imageId, requestToken);
    set((state) => ({
      ocrLoading: { ...state.ocrLoading, [imageId]: true },
    }));
    try {
      const ocr = await api.getOCRGateContext(imageId);
      if (ocrLoadTokens.get(imageId) !== requestToken) return false;
      const current = get().g4Contexts[imageId];
      const currentPhase = workflowPhase(current);
      const image = get().images.find((entry) => entry.id === imageId);
      if (
        !current
        || current.status !== 'active'
        || !current.generation
        || (currentPhase !== 'G6' && currentPhase !== 'G7' && currentPhase !== 'G8')
        || ocr.generationId !== current.generation.id
        || ocr.nextSequence !== current.generation.nextSequence
        || !image
        || ocr.imageRevision !== image.revision
      ) {
        throw new Error('G6 上下文与当前页代次或图像版本不一致，请重载本页。');
      }
      set((state) => ({
        ocrContexts: { ...state.ocrContexts, [imageId]: ocr },
        ocrLoading: { ...state.ocrLoading, [imageId]: false },
      }));
      return true;
    } catch (error) {
      if (ocrLoadTokens.get(imageId) !== requestToken) return false;
      const conflict = error instanceof ApiError && error.status === 409;
      const message = errorMessage(error);
      set((state) => {
        const current = state.g4Contexts[imageId];
        return {
          globalError: message,
          revisionConflict: conflict,
          ocrLoading: { ...state.ocrLoading, [imageId]: false },
          g4Contexts: current
            ? {
                ...state.g4Contexts,
                [imageId]: { ...current, error: message, conflict },
              }
            : state.g4Contexts,
        };
      });
      return false;
    } finally {
      if (ocrLoadTokens.get(imageId) === requestToken) ocrLoadTokens.delete(imageId);
    }
  },

  loadMaskContext: async (imageId, force = false) => {
    const context = get().g4Contexts[imageId];
    const phase = workflowPhase(context);
    if (
      !context
      || context.status !== 'active'
      || (phase !== 'G7' && phase !== 'G8')
      || context.conflict
      || context.error
    ) return false;
    if (!force && get().maskContexts[imageId]) return true;
    const requestToken = Symbol(imageId);
    maskLoadTokens.set(imageId, requestToken);
    set((state) => ({ maskLoading: { ...state.maskLoading, [imageId]: true } }));
    try {
      const mask = await api.getMaskGateContext(imageId);
      if (maskLoadTokens.get(imageId) !== requestToken) return false;
      const current = get().g4Contexts[imageId];
      const image = get().images.find((entry) => entry.id === imageId);
      const regions = get().regionsByImage[imageId] ?? [];
      const currentPhase = workflowPhase(current);
      const sha256 = /^[0-9a-f]{64}$/;
      const canonicalRubyMapping = (value: unknown): string | null => {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
        const entries = Object.entries(value as Record<string, unknown>).sort(([left], [right]) => left.localeCompare(right));
        if (entries.some(([, ids]) => !Array.isArray(ids)
          || ids.some((id) => typeof id !== 'string')
          || ids.length !== new Set(ids).size)) return null;
        return JSON.stringify(Object.fromEntries(entries.map(([key, ids]) => [key, [...(ids as string[])].sort()])));
      };
      const sameChecks = (left: unknown, right: Array<{ check: string; passed: boolean }>) => Array.isArray(left)
        && JSON.stringify(left) === JSON.stringify(right);
      const sameActor = (event: PageLineageEvent, review: MaskGateReview) => (
        event.actor.actorKind === review.reviewer.actorKind
        && (event.actor.actorId ?? null) === (review.reviewer.actorId ?? null)
        && (event.actor.taskId ?? null) === (review.reviewer.taskId ?? null)
        && (event.actor.threadId ?? null) === (review.reviewer.threadId ?? null)
        && (event.actor.sessionId ?? null) === (review.reviewer.sessionId ?? null)
        && event.actor.operationSource === review.reviewer.operationSource
      );
      const exactObjectKeys = (value: object, keys: readonly string[]) => (
        Object.keys(value).sort().join('\0') === [...keys].sort().join('\0')
      );
      const expectedDraftStateChecksum = await g7MaskDraftChecksum(
        mask.g6Checksum,
        mask.qualityChecksum,
        mask.rubyRegionIdsByPrimary,
        mask.draft.regions,
      );
      if (maskLoadTokens.get(imageId) !== requestToken) return false;
      const artifactIds = new Set(mask.artifacts.map((artifact) => artifact.artifactId));
      const lineageEvents = current?.status === 'active'
        ? [...current.events].sort((left, right) => left.sequence - right.sequence) : [];
      const g7Events = lineageEvents.filter((event) => event.gate === 'G7_mask');
      const g6TerminalEvent = [...lineageEvents].reverse().find((event) =>
        event.gate === 'G6_ocr' && event.operation === 'ocr-stage-review'
        && (event.state === 'accepted' || event.state === 'not-applicable'));
      const draftEvents = g7Events.filter((event) => event.operation === 'mask-draft-updated');
      const producedEvents = g7Events.filter((event) => event.operation === 'mask-artifact-produced');
      const reviewEvents = g7Events.filter((event) => event.operation === 'mask-stage-review');
      const stateBearingEvents = g7Events.filter((event) =>
        event.operation === 'mask-draft-updated'
        || event.operation === 'mask-artifact-produced'
        || event.operation === 'mask-stage-review');
      const latestDraftEvent = draftEvents.at(-1);
      const latestReviewEvent = reviewEvents.at(-1);
      const latestStateBearingEvent = stateBearingEvents.at(-1);
      const latestStateEvent = [...g7Events].reverse().find((event) => event.outputChecksum !== null);
      const lastG7Event = g7Events.at(-1);
      const eventArtifactIds = new Set(producedEvents.map((event) => event.evidence.artifactId));
      const eligibleSet = new Set(mask.eligibleRegionIds);
      const rubyEntries = Object.entries(mask.rubyRegionIdsByPrimary);
      const rubyIds = rubyEntries.flatMap(([, ids]) => ids);
      const canonicalContextRuby = canonicalRubyMapping(mask.rubyRegionIdsByPrimary);
      const reviewArtifact = mask.review?.artifactId
        ? mask.artifacts.find((artifact) => artifact.artifactId === mask.review?.artifactId)
        : undefined;
      const allEventAnchorsBound = g7Events.every((event) =>
        event.parentChecksum === mask.g6Checksum
        && event.evidence.qualityChecksum === mask.qualityChecksum
        && event.evidence.eligibleRegionCount === mask.eligibleRegionIds.length
        && event.evidence.rubyRegionCount === rubyIds.length
        && canonicalRubyMapping(event.evidence.rubyRegionIdsByPrimary) === canonicalContextRuby);
      const draftEventBound = mask.draft.revision === draftEvents.length
        && (mask.draft.revision === 0
          ? latestDraftEvent === undefined
          : Boolean(latestDraftEvent
            && latestDraftEvent.evidence.recipeChecksum === mask.draft.stateChecksum
            && latestDraftEvent.evidence.recipeRegionCount === mask.draft.regions.length));
      const stateImageRevisionBound = g7Events.length === 0
        ? latestStateBearingEvent === undefined
        : Boolean(latestStateBearingEvent
          && Number.isInteger(latestStateBearingEvent.evidence.imageRevision)
          && (currentPhase === 'G8'
            ? (latestStateBearingEvent.evidence.imageRevision as number) <= mask.imageRevision
            : latestStateBearingEvent.evidence.imageRevision === mask.imageRevision));
      const draftRecipesValid = mask.draft.regions.every((recipe) =>
        exactObjectKeys(recipe, [
          'regionId', 'maskMode', 'polygon', 'padding', 'dilation', 'feather',
          'polarity', 'maskEdits',
        ])
        && ['region', 'text', 'manual'].includes(recipe.maskMode)
        && ['auto', 'dark', 'light'].includes(recipe.polarity)
        && Number.isInteger(recipe.padding) && recipe.padding >= 0 && recipe.padding <= 512
        && Number.isInteger(recipe.dilation) && recipe.dilation >= 0 && recipe.dilation <= 128
        && Number.isInteger(recipe.feather) && recipe.feather >= 0 && recipe.feather <= 128
        && (recipe.polygon == null || (Array.isArray(recipe.polygon)
          && recipe.polygon.length >= 3 && recipe.polygon.length <= 4096
          && recipe.polygon.every((point) => Array.isArray(point) && point.length === 2
            && Number.isFinite(point[0]) && Number.isFinite(point[1])
            && point[0] >= 0 && point[1] >= 0
            && point[0] <= (image?.width ?? 0) && point[1] <= (image?.height ?? 0))))
        && exactObjectKeys(recipe.maskEdits, ['version', 'strokes'])
        && recipe.maskEdits.version === 1 && recipe.maskEdits.strokes.length <= 256
        && recipe.maskEdits.strokes.reduce((total, stroke) => total + stroke.points.length, 0) <= 16384
        && (recipe.maskMode !== 'manual' || recipe.maskEdits.strokes.length > 0)
        && recipe.maskEdits.strokes.every((stroke) => exactObjectKeys(stroke, ['mode', 'radius', 'points'])
          && (stroke.mode === 'add' || stroke.mode === 'erase')
          && Number.isFinite(stroke.radius) && stroke.radius > 0 && stroke.radius <= 512
          && stroke.points.length > 0 && stroke.points.length <= 4096
          && stroke.points.every((point) => Array.isArray(point) && point.length === 2
            && Number.isFinite(point[0]) && Number.isFinite(point[1])
            && point[0] >= 0 && point[1] >= 0
            && point[0] <= (image?.width ?? 0) && point[1] <= (image?.height ?? 0)))
      );
      const eventContextBound = g6TerminalEvent?.outputChecksum === mask.g6Checksum
        && canonicalContextRuby !== null
        && draftEventBound
        && stateImageRevisionBound
        && (g7Events.length === 0
          ? mask.state === 'pending' && mask.draft.revision === 0
          && mask.artifacts.length === 0 && mask.review === null && mask.selectedArtifactId === null
          : allEventAnchorsBound
          && latestStateEvent?.outputChecksum === mask.maskStateChecksum
          && (lastG7Event?.outputChecksum !== null || lastG7Event.inputChecksum === mask.maskStateChecksum)
          && producedEvents.length === mask.artifacts.length
          && eventArtifactIds.size === producedEvents.length
          && mask.artifacts.every((artifact, index) => {
            const event = producedEvents[index];
            return Boolean(event
            && event.evidence.artifactId === artifact.artifactId
            && event.evidence.maskChecksum === artifact.maskChecksum
            && event.evidence.recipeChecksum === artifact.recipeChecksum
            && event.evidence.qualityChecksum === artifact.qualityChecksum
            && event.evidence.width === artifact.width
            && event.evidence.height === artifact.height
            && event.evidence.renderScale === artifact.renderScale
            && event.evidence.nonzeroPixelCount === artifact.nonzeroPixelCount
            && JSON.stringify(event.evidence.bbox) === JSON.stringify(artifact.bbox)
            && event.evidence.eligibleRegionCount === mask.eligibleRegionIds.length
            && event.evidence.rubyRegionCount === rubyIds.length
            && canonicalRubyMapping(event.evidence.rubyRegionIdsByPrimary) === canonicalContextRuby
            && event.evidence.provider === artifact.provider
            && event.evidence.modelVersion === artifact.modelVersion
            && event.evidence.parameterHash === artifact.parameterHash
            && event.jobId === artifact.jobId && event.jobItemId === artifact.jobItemId
            && event.parentChecksum === artifact.parentChecksum
            && event.provider === artifact.provider && event.modelVersion === artifact.modelVersion
            && event.parameterHash === artifact.parameterHash);
          })
          && (latestReviewEvent
            ? Boolean(mask.review
              && mask.state === mask.review.state
              && mask.review.state === latestReviewEvent.state
              && mask.review.reason === latestReviewEvent.reason
              && mask.review.artifactId === latestReviewEvent.evidence.artifactId
              && mask.review.maskChecksum === latestReviewEvent.evidence.maskChecksum
              && mask.selectedArtifactId === mask.review.artifactId
              && latestReviewEvent.evidence.recipeChecksum === (mask.review.state === 'not-applicable'
                ? mask.draft.stateChecksum : reviewArtifact?.recipeChecksum)
              && sameChecks(latestReviewEvent.evidence.coverageChecks, mask.review.coverageChecks)
              && sameChecks(latestReviewEvent.evidence.collateralChecks, mask.review.collateralChecks)
              && sameActor(latestReviewEvent, mask.review))
            : mask.review === null && mask.selectedArtifactId === null && mask.state === 'pending'));
      const structurallyValid = sha256.test(mask.g6Checksum)
        && sha256.test(mask.qualityChecksum)
        && sha256.test(mask.maskStateChecksum)
        && sha256.test(mask.draft.stateChecksum)
        && mask.draft.stateChecksum === expectedDraftStateChecksum
        && Number.isInteger(mask.draft.revision)
        && mask.draft.revision >= 0
        && artifactIds.size === mask.artifacts.length
        && mask.artifacts.every((artifact, index) =>
          artifact.sequence === index + 1
          && artifact.parentChecksum === mask.g6Checksum
          && sha256.test(artifact.recipeChecksum)
          && sha256.test(artifact.maskChecksum)
          && sha256.test(artifact.qualityChecksum)
          && artifact.qualityChecksum === mask.qualityChecksum
          && Number.isInteger(artifact.renderScale) && artifact.renderScale >= 1 && artifact.renderScale <= 4
          && artifact.width === Math.round((image?.width ?? 0) * artifact.renderScale)
          && artifact.height === Math.round((image?.height ?? 0) * artifact.renderScale)
          && artifact.provider === 'deterministic-mask'
          && artifact.modelVersion === 'create-mask-v1'
          && artifact.parameterHash === artifact.recipeChecksum
          && Number.isInteger(artifact.nonzeroPixelCount) && artifact.nonzeroPixelCount > 0
          && Number.isInteger(artifact.bbox.x) && artifact.bbox.x >= 0
          && Number.isInteger(artifact.bbox.y) && artifact.bbox.y >= 0
          && Number.isInteger(artifact.bbox.width) && artifact.bbox.width > 0
          && Number.isInteger(artifact.bbox.height) && artifact.bbox.height > 0
          && artifact.bbox.x + artifact.bbox.width <= artifact.width
          && artifact.bbox.y + artifact.bbox.height <= artifact.height
          && Boolean(artifact.jobId && artifact.jobItemId)
        )
        && (!mask.selectedArtifactId || artifactIds.has(mask.selectedArtifactId))
        && eligibleSet.size === mask.eligibleRegionIds.length
        && [...mask.eligibleRegionIds].sort().join('\0') === regions.filter(maskRegionRequired).map((region) => region.id).sort().join('\0')
        && mask.draft.regions.map((recipe) => recipe.regionId).sort().join('\0') === [...mask.eligibleRegionIds].sort().join('\0')
        && draftRecipesValid
        && rubyEntries.map(([primaryId]) => primaryId).sort().join('\0') === [...mask.eligibleRegionIds].sort().join('\0')
        && new Set(rubyIds).size === rubyIds.length
        && Object.entries(mask.rubyRegionIdsByPrimary).every(([primaryId, rubyIds]) =>
          rubyIds.every((rubyId) => regions.some((region) => region.id === rubyId && region.type === 'ruby' && region.rubyParentId === primaryId))
          && [...rubyIds].sort().join('\0') === regions.filter((region) => region.type === 'ruby' && region.rubyParentId === primaryId)
            .map((region) => region.id).sort().join('\0')
        ) && eventContextBound;
      if (
        !current
        || current.status !== 'active'
        || !current.generation
        || (currentPhase !== 'G7' && currentPhase !== 'G8')
        || mask.generationId !== current.generation.id
        || mask.nextSequence !== current.generation.nextSequence
        || !image
        || mask.imageRevision !== image.revision
        || !structurallyValid
      ) throw new Error('G7 上下文、实际蒙版网格或页血缘不一致，请重载本页。');
      const selected = mask.eligibleRegionIds.length ? (mask.state === 'accepted'
        ? mask.review?.artifactId ?? ''
        : [...mask.artifacts].reverse().find((artifact) => artifact.recipeChecksum === mask.draft.stateChecksum)?.artifactId ?? '') : '';
      const selectedArtifact = mask.artifacts.find((artifact) => artifact.artifactId === selected);
      set((state) => ({
        maskContexts: { ...state.maskContexts, [imageId]: mask },
        maskLoading: { ...state.maskLoading, [imageId]: false },
        selectedMaskArtifactIds: selected
          ? { ...state.selectedMaskArtifactIds, [imageId]: selected }
          : Object.fromEntries(Object.entries(state.selectedMaskArtifactIds).filter(([id]) => id !== imageId)),
        maskBitmapObservations: Object.fromEntries(
          Object.entries(state.maskBitmapObservations).filter(([id, observation]) =>
            id !== imageId || Boolean(selectedArtifact
              && observation.artifactId === selectedArtifact.artifactId
              && observation.imageRevision === mask.imageRevision
              && observation.checksum === selectedArtifact.maskChecksum
              && observation.width === selectedArtifact.width
              && observation.height === selectedArtifact.height)),
        ),
      }));
      return true;
    } catch (error) {
      if (maskLoadTokens.get(imageId) !== requestToken) return false;
      const conflict = error instanceof ApiError && error.status === 409;
      const message = errorMessage(error);
      set((state) => {
        const current = state.g4Contexts[imageId];
        return {
          globalError: message,
          revisionConflict: conflict,
          maskLoading: { ...state.maskLoading, [imageId]: false },
          g4Contexts: current ? {
            ...state.g4Contexts,
            [imageId]: { ...current, error: message, conflict },
          } : state.g4Contexts,
        };
      });
      return false;
    } finally {
      if (maskLoadTokens.get(imageId) === requestToken) maskLoadTokens.delete(imageId);
    }
  },

  loadCleanPlateContext: async (imageId, force = false) => {
    const lineage = get().g4Contexts[imageId];
    if (!lineage || lineage.status !== 'active' || workflowPhase(lineage) !== 'G8'
      || lineage.conflict || lineage.error) return false;
    if (!force && get().cleanPlateContexts[imageId]) return true;
    const requestToken = Symbol(imageId);
    cleanPlateLoadTokens.set(imageId, requestToken);
    set((state) => ({
      cleanPlateLoading: { ...state.cleanPlateLoading, [imageId]: true },
    }));
    try {
      const cleanPlate = await api.getCleanPlateGateContext(imageId);
      if (cleanPlateLoadTokens.get(imageId) !== requestToken) return false;
      const current = get().g4Contexts[imageId];
      const image = get().images.find((entry) => entry.id === imageId);
      const mask = get().maskContexts[imageId];
      const regions = get().regionsByImage[imageId] ?? [];
      const sha256 = /^[0-9a-f]{64}$/;
      const events = current?.status === 'active'
        ? [...current.events].sort((left, right) => left.sequence - right.sequence) : [];
      const g7Terminal = [...events].reverse().find((event) =>
        event.gate === 'G7_mask' && event.operation === 'mask-stage-review'
        && (event.state === 'accepted' || event.state === 'not-applicable'));
      const g8Events = events.filter((event) => event.gate === 'G8_cleanPlate');
      const producedEvents = g8Events.filter((event) =>
        event.operation === 'clean-plate-candidate-produced');
      const completedItems = new Set(g8Events.filter((event) =>
        event.operation === 'inpaint-job-completed').map((event) => event.jobItemId));
      const reviewEvents = g8Events.filter((event) => event.operation === 'clean-plate-stage-review');
      const latestStateEvent = [...g8Events].reverse().find((event) => event.outputChecksum !== null);
      const latestG8ImageRevision = [...g8Events].reverse().find((event) =>
        typeof event.evidence.imageRevision === 'number')?.evidence.imageRevision;
      const latestFallback = [...g8Events].reverse().find((event) =>
        event.operation === 'clean-plate-fallback-enabled'
        || event.operation === 'clean-plate-fallback-disabled');
      const candidateIds = new Set(cleanPlate.candidates.map((candidate) => candidate.candidateId));
      const routeIds = cleanPlate.routes.map((route) => route.regionId);
      const eligibleIds = regions.filter(maskRegionRequired).map((region) => region.id).sort();
      const expectedRoutes: Record<string, string> = {
        'white-solid': 'deterministic-solid',
        'black-solid': 'deterministic-solid',
        'other-solid': 'deterministic-solid',
        'simple-gradient': 'controlled-gradient',
        screentone: 'screentone-preserving',
        'complex-lineart': 'ai-inpaint-redraw',
        'illustration/character': 'ai-inpaint-redraw',
      };
      const exactChecks = (checks: Array<{ check: string; passed: boolean }>) =>
        checks.length === CLEAN_PLATE_CHECKS.length
        && new Set(checks.map((entry) => entry.check)).size === CLEAN_PLATE_CHECKS.length
        && CLEAN_PLATE_CHECKS.every((check) => checks.some((entry) => entry.check === check))
        && checks.every((entry) => typeof entry.passed === 'boolean');
      const sameActor = (event: PageLineageEvent, actor: LineageActor) => (
        event.actor.actorKind === actor.actorKind
        && (event.actor.actorId ?? null) === (actor.actorId ?? null)
        && (event.actor.taskId ?? null) === (actor.taskId ?? null)
        && (event.actor.threadId ?? null) === (actor.threadId ?? null)
        && (event.actor.sessionId ?? null) === (actor.sessionId ?? null)
        && event.actor.operationSource === actor.operationSource
      );
      const acceptedMaskArtifact = cleanPlate.maskArtifactId
        ? mask?.artifacts.find((artifact) =>
          artifact.artifactId === cleanPlate.maskArtifactId
          && artifact.maskChecksum === cleanPlate.maskChecksum)
        : undefined;
      const candidatesValid = candidateIds.size === cleanPlate.candidates.length
        && producedEvents.length === cleanPlate.candidates.length
        && cleanPlate.candidates.every((candidate, index) => {
          const produced = producedEvents[index];
          const reviewEvent = candidate.review ? reviewEvents.find((event) =>
            event.evidence.candidateId === candidate.candidateId) : undefined;
          const providers = [...new Set(candidate.routeManifest.map((route) => route.provider))].sort();
          const models = [...new Set(candidate.routeManifest.map((route) => route.modelVersion))].sort();
          const origins = new Set(candidate.routeManifest.map((route) => route.originKind));
          const expectedOrigin = origins.size === 1 ? [...origins][0] : 'mixed';
          const reviewValid = candidate.review === null
            ? reviewEvent === undefined
            : Boolean(reviewEvent
              && reviewEvent.state === candidate.review.state
              && reviewEvent.reason === candidate.review.reason
              && reviewEvent.evidence.candidateChecksum === candidate.candidateChecksum
              && JSON.stringify(reviewEvent.evidence.checks) === JSON.stringify(candidate.review.checks)
              && sameActor(reviewEvent, candidate.review.reviewer)
              && exactChecks(candidate.review.checks)
              && (candidate.review.state === 'accepted'
                ? candidate.review.checks.every((entry) => entry.passed)
                : candidate.review.checks.some((entry) => !entry.passed)));
          return candidate.sequence === index + 1
            && candidate.parentChecksum === cleanPlate.g7Checksum
            && candidate.qualityChecksum === cleanPlate.qualityChecksum
            && candidate.backgroundChecksum === cleanPlate.backgroundChecksum
            && candidate.maskArtifactId === cleanPlate.maskArtifactId
            && candidate.maskChecksum === cleanPlate.maskChecksum
            && sha256.test(candidate.routeChecksum) && sha256.test(candidate.parameterHash)
            && candidate.parameterHash === candidate.routeChecksum
            && sha256.test(candidate.candidateChecksum)
            && Number.isInteger(candidate.renderScale) && candidate.renderScale >= 1
            && candidate.renderScale <= 4
            && candidate.width === Math.round((image?.width ?? 0) * candidate.renderScale)
            && candidate.height === Math.round((image?.height ?? 0) * candidate.renderScale)
            && candidate.width === acceptedMaskArtifact?.width
            && candidate.height === acceptedMaskArtifact?.height
            && candidate.outsideMaskChangeCount === 0
            && Array.isArray(candidate.anomalies)
            && candidate.anomalies.every((entry) => typeof entry === 'string')
            && candidate.routeManifest.length === cleanPlate.routes.length
            && candidate.routeManifest.every((route, routeIndex) => {
              const summary = cleanPlate.routes[routeIndex];
              return Boolean(summary
                && route.regionId === summary.regionId
                && route.backgroundCategory === summary.backgroundCategory
                && sha256.test(route.parameterHash)
                && (route.route === summary.defaultRoute
                  || (route.route === 'classical-fallback'
                    && (route.backgroundCategory === 'complex-lineart'
                      || route.backgroundCategory === 'illustration/character'))));
            })
            && candidate.originKind === expectedOrigin
            && candidate.providerIds.length > 0
            && candidate.modelVersions.length > 0
            && JSON.stringify(candidate.providerIds) === JSON.stringify(providers)
            && JSON.stringify(candidate.modelVersions) === JSON.stringify(models)
            && Boolean(produced
              && produced.jobId === candidate.jobId
              && produced.jobItemId === candidate.jobItemId
              && produced.evidence.candidateId === candidate.candidateId
              && produced.evidence.candidateChecksum === candidate.candidateChecksum
              && produced.evidence.routeChecksum === candidate.routeChecksum)
            && candidate.completed === completedItems.has(candidate.jobItemId)
            && reviewValid;
        });
      const acceptedReviews = cleanPlate.candidates.filter((candidate) =>
        candidate.review?.state === 'accepted');
      const latestReview = reviewEvents.at(-1);
      const complexIds = new Set(cleanPlate.routes.filter((route) =>
        route.backgroundCategory === 'complex-lineart'
        || route.backgroundCategory === 'illustration/character').map((route) => route.regionId));
      const aiCandidates = cleanPlate.candidates.filter((candidate) =>
        candidate.routeManifest.some((route) => route.originKind === 'ai'));
      const aiIds = new Set(aiCandidates.flatMap((candidate) => candidate.routeManifest
        .filter((route) => route.originKind === 'ai').map((route) => route.regionId)));
      const computedFallbackAllowed = complexIds.size > 0 && aiCandidates.length > 0
        && [...complexIds].every((regionId) => aiIds.has(regionId))
        && aiCandidates.every((candidate) => candidate.review?.state === 'rejected');
      const structurallyValid = Boolean(
        current && current.status === 'active' && current.generation && image && mask
        && workflowPhase(current) === 'G8'
        && cleanPlate.generationId === current.generation.id
        && cleanPlate.nextSequence === current.generation.nextSequence
        && cleanPlate.imageRevision === image.revision
        && sha256.test(cleanPlate.g7Checksum)
        && sha256.test(cleanPlate.qualityChecksum)
        && sha256.test(cleanPlate.backgroundChecksum)
        && sha256.test(cleanPlate.cleanPlateStateChecksum)
        && g7Terminal?.outputChecksum === cleanPlate.g7Checksum
        && mask.generationId === cleanPlate.generationId
        && mask.qualityChecksum === cleanPlate.qualityChecksum
        && (mask.state === 'accepted' || mask.state === 'not-applicable')
        && (mask.state === 'accepted'
          ? mask.review?.artifactId === cleanPlate.maskArtifactId
            && mask.review.maskChecksum === cleanPlate.maskChecksum
            && Boolean(acceptedMaskArtifact)
          : cleanPlate.maskArtifactId === null && cleanPlate.maskChecksum === null)
        && routeIds.length === new Set(routeIds).size
        && [...routeIds].sort().join('\0') === eligibleIds.join('\0')
        && cleanPlate.routes.every((route) => {
          const region = regions.find((entry) => entry.id === route.regionId);
          return Boolean(region && region.backgroundCategory === route.backgroundCategory
            && route.defaultRoute === expectedRoutes[route.backgroundCategory]);
        })
        && candidatesValid
        && cleanPlate.cleanPlateStateChecksum === (latestStateEvent?.outputChecksum
          ?? cleanPlate.g7Checksum)
        && cleanPlate.fallbackEnabled === (latestFallback?.operation
          === 'clean-plate-fallback-enabled')
        && cleanPlate.fallbackAllowed === computedFallbackAllowed
        && (!cleanPlate.fallbackEnabled || cleanPlate.fallbackAllowed)
        && (typeof latestG8ImageRevision === 'number'
          ? latestG8ImageRevision === cleanPlate.imageRevision
          : mask.imageRevision === cleanPlate.imageRevision)
        && acceptedReviews.length <= 1
        && cleanPlate.acceptedCandidateId === (acceptedReviews[0]?.candidateId ?? null)
        && (latestReview
          ? cleanPlate.state === latestReview.state
          : cleanPlate.state === 'pending')
      );
      if (!structurallyValid) {
        throw new Error('G8 上下文、候选 provenance 或页血缘不一致，请重载本页。');
      }
      const selected = cleanPlate.acceptedCandidateId
        ?? [...cleanPlate.candidates].reverse().find((candidate) =>
          candidate.completed && candidate.review === null)?.candidateId
        ?? cleanPlate.candidates.at(-1)?.candidateId ?? '';
      const selectedCandidate = cleanPlate.candidates.find((candidate) =>
        candidate.candidateId === selected);
      set((state) => ({
        cleanPlateContexts: { ...state.cleanPlateContexts, [imageId]: cleanPlate },
        cleanPlateLoading: { ...state.cleanPlateLoading, [imageId]: false },
        selectedCleanPlateCandidateIds: selected
          ? { ...state.selectedCleanPlateCandidateIds, [imageId]: selected }
          : Object.fromEntries(Object.entries(state.selectedCleanPlateCandidateIds)
            .filter(([id]) => id !== imageId)),
        cleanPlateBitmapObservations: Object.fromEntries(
          Object.entries(state.cleanPlateBitmapObservations).filter(([id, observation]) =>
            id !== imageId || Boolean(selectedCandidate
              && observation.candidateId === selectedCandidate.candidateId
              && observation.generationId === cleanPlate.generationId
              && observation.nextSequence === cleanPlate.nextSequence
              && observation.cleanPlateStateChecksum === cleanPlate.cleanPlateStateChecksum
              && observation.imageRevision === cleanPlate.imageRevision
              && observation.sourceChecksum === current?.generation?.sourceChecksum
              && observation.qualityChecksum === cleanPlate.qualityChecksum
              && observation.maskArtifactId === cleanPlate.maskArtifactId
              && observation.maskChecksum === cleanPlate.maskChecksum
              && observation.maskWidth === acceptedMaskArtifact?.width
              && observation.maskHeight === acceptedMaskArtifact?.height
              && observation.checksum === selectedCandidate.candidateChecksum
              && observation.width === selectedCandidate.width
              && observation.height === selectedCandidate.height)),
        ),
      }));
      return true;
    } catch (error) {
      if (cleanPlateLoadTokens.get(imageId) !== requestToken) return false;
      const conflict = error instanceof ApiError && error.status === 409;
      const message = errorMessage(error);
      set((state) => {
        const current = state.g4Contexts[imageId];
        return {
          globalError: message,
          revisionConflict: conflict,
          cleanPlateLoading: { ...state.cleanPlateLoading, [imageId]: false },
          g4Contexts: current ? {
            ...state.g4Contexts,
            [imageId]: { ...current, error: message, conflict },
          } : state.g4Contexts,
        };
      });
      return false;
    } finally {
      if (cleanPlateLoadTokens.get(imageId) === requestToken) {
        cleanPlateLoadTokens.delete(imageId);
      }
    }
  },

  loadTranslationContext: async (imageId, force = false) => {
    const lineage = get().g4Contexts[imageId];
    if (!lineage || lineage.status !== 'active' || workflowPhase(lineage) !== 'G9'
      || lineage.conflict || lineage.error) return false;
    if (!force && get().translationContexts[imageId]) return true;
    const requestToken = Symbol(imageId);
    translationLoadTokens.set(imageId, requestToken);
    set((state) => ({ translationLoading: { ...state.translationLoading, [imageId]: true } }));
    try {
      const translation = await api.getTranslationGateContext(imageId);
      if (translationLoadTokens.get(imageId) !== requestToken) return false;
      const state = get();
      const current = state.g4Contexts[imageId];
      const image = state.images.find((entry) => entry.id === imageId);
      const regions = state.regionsByImage[imageId] ?? [];
      const project = state.currentProject;
      const events = current?.status === 'active' ? [...current.events].sort((a, b) => a.sequence - b.sequence) : [];
      const g8Terminal = [...events].reverse().find((event) => event.gate === 'G8_cleanPlate'
        && event.operation === 'clean-plate-stage-review'
        && (event.state === 'accepted' || event.state === 'not-applicable'));
      const g9Events = events.filter((event) => event.gate === 'G9_translation');
      const g9Terminal = [...g9Events].reverse().find((event) => event.operation === 'translation-stage-review');
      const latestG9StateEvent = [...g9Events].reverse().find((event) =>
        event.operation !== 'translation-stage-review' && event.outputChecksum !== null);
      const sha256 = /^[0-9a-f]{64}$/;
      const allowedFlags = new Set<TranslationQCFlag>([
        'none', 'empty-output', 'non-chinese-output', 'forbidden-template', 'source-copy',
        'japanese-residual', 'generic-duplicate', 'source-inconsistent', 'context-inconsistent',
        'source-noise-hallucination',
      ]);
      const exactChecks = (checks: Array<MaskCheckResult<TranslationQCCheck>>) =>
        checks.length === TRANSLATION_QC_CHECKS.length
        && new Set(checks.map((entry) => entry.check)).size === TRANSLATION_QC_CHECKS.length
        && TRANSLATION_QC_CHECKS.every((check) => checks.some((entry) => entry.check === check))
        && checks.every((entry) => typeof entry.passed === 'boolean');
      const eligibleRegions = [...regions].filter((region) => region.type !== 'ruby'
        && (region.contentDisposition === 'translate' || region.contentDisposition === 'redraw-art')
        && current?.generation && ocrSourceReviewComplete(region, current.generation.id))
        .sort((a, b) => a.order - b.order);
      const eligibleIds = eligibleRegions.map((region) => region.id);
      const candidateIds = new Set<string>();
      const revisionIds = new Set<string>();
      const latestByRegion = new Map<string, TranslationCandidate>();
      const finalCandidateByRegion = new Map<string, TranslationCandidate>();
      for (const candidate of translation.candidates) {
        finalCandidateByRegion.set(candidate.regionId, candidate);
      }
      let candidatesValid = true;
      for (const candidate of translation.candidates) {
        const eligible = translation.eligibleRegions.find((entry) => entry.regionId === candidate.regionId);
        const region = regions.find((entry) => entry.id === candidate.regionId);
        const previous = latestByRegion.get(candidate.regionId);
        const isLatest = finalCandidateByRegion.get(candidate.regionId)?.candidateId === candidate.candidateId;
        const currentProjectionValid = !isLatest
          ? candidate.review?.state === 'rejected'
          : candidate.review?.state === 'accepted'
            ? Boolean(region
              && region.revision === candidate.sourceRegionRevision + 1
              && region.translationText === candidate.translationText
              && region.translationProvider === candidate.provider)
            : region?.revision === candidate.sourceRegionRevision;
        const flagsValid = candidate.computedQcFlags.length > 0
          && candidate.computedQcFlags.every((flag) => allowedFlags.has(flag))
          && new Set(candidate.computedQcFlags).size === candidate.computedQcFlags.length
          && (!candidate.computedQcFlags.includes('none') || candidate.computedQcFlags.length === 1);
        const reviewValid = candidate.review === null || Boolean(
          exactChecks(candidate.review.checks)
          && candidate.review.qcFlags.length > 0
          && candidate.review.qcFlags.every((flag) => allowedFlags.has(flag))
          && new Set(candidate.review.qcFlags).size === candidate.review.qcFlags.length
          && (candidate.review.state === 'accepted'
            ? candidate.review.reason === 'translation-reviewed'
              && candidate.review.checks.every((entry) => entry.passed)
              && candidate.review.qcFlags.length === 1 && candidate.review.qcFlags[0] === 'none'
            : candidate.review.reason !== 'translation-reviewed'
              && (candidate.review.checks.some((entry) => !entry.passed)
                || candidate.review.qcFlags.some((flag) => flag !== 'none')))
        );
        const producedEvent = candidate.jobItemId ? g9Events.find((event) =>
          event.operation === 'translation-candidates-produced'
          && event.jobId === candidate.jobId && event.jobItemId === candidate.jobItemId) : undefined;
        const revisedEvent = g9Events.find((event) => event.operation === 'translation-candidate-revised'
          && event.evidence.candidateId === candidate.candidateId
          && event.evidence.candidateChecksum === candidate.candidateChecksum);
        const reviewEvent = candidate.review ? g9Events.find((event) =>
          event.operation === 'translation-candidate-reviewed'
          && event.evidence.candidateId === candidate.candidateId
          && event.evidence.candidateChecksum === candidate.candidateChecksum
          && event.state === candidate.review?.state) : undefined;
        const provenanceValid = candidate.originKind === 'model'
          ? Boolean(candidate.jobId && candidate.jobItemId && producedEvent
            && candidate.provider && candidate.modelVersion)
          : Boolean(!candidate.jobId && !candidate.jobItemId && revisedEvent
            && (candidate.originKind === 'manual'
              ? candidate.provider === 'manual' && candidate.modelVersion === 'manual-review-v1'
              : candidate.originKind === 'agent'
                ? ['codex', 'cursor'].includes(candidate.provider) && candidate.modelVersion === 'agent-revision-v1'
                : candidate.provider === 'dictionary' && candidate.modelVersion === 'dictionary-revision-v1'));
        candidatesValid = candidatesValid && Boolean(eligible
          && !candidateIds.has(candidate.candidateId) && !revisionIds.has(candidate.revisionId)
          && candidate.sequence >= 1 && candidate.revisionNumber === (previous?.revisionNumber ?? 0) + 1
          && candidate.supersedesCandidateId === (previous?.candidateId ?? null)
          && (!previous || previous.review?.state === 'rejected')
          && ['model', 'manual', 'agent', 'dictionary'].includes(candidate.originKind)
          && candidate.targetLanguage === translation.targetLanguage
          && sha256.test(candidate.parameterHash)
          && candidate.g8Checksum === translation.g8Checksum
          && candidate.cleanPlateChecksum === translation.cleanPlateChecksum
          && candidate.sourceTextChecksum === eligible.sourceTextChecksum
          && Number.isInteger(candidate.sourceRegionRevision) && candidate.sourceRegionRevision >= 1
          && (!previous || candidate.sourceRegionRevision === previous.sourceRegionRevision)
          && currentProjectionValid
          && candidate.contextChecksum === eligible.contextChecksum
          && sha256.test(candidate.candidateChecksum)
          && flagsValid && reviewValid && provenanceValid
          && (!candidate.review || reviewEvent)
          && (candidate.review?.state !== 'accepted'
            || (candidate.computedQcFlags.length === 1 && candidate.computedQcFlags[0] === 'none')));
        candidateIds.add(candidate.candidateId);
        revisionIds.add(candidate.revisionId);
        latestByRegion.set(candidate.regionId, candidate);
      }
      const acceptedMap = Object.fromEntries([...latestByRegion.entries()]
        .filter(([, candidate]) => candidate.review?.state === 'accepted')
        .map(([regionId, candidate]) => [regionId, candidate.candidateId]));
      const contextValid = Boolean(current?.generation && image && project && g8Terminal
        && workflowPhase(current) === 'G9'
        && translation.imageId === imageId
        && translation.generationId === current.generation.id
        && translation.nextSequence === current.generation.nextSequence
        && translation.imageRevision === image.revision
        && translation.targetLanguage === project.settings.targetLanguage
        && sha256.test(translation.g8Checksum)
        && sha256.test(translation.cleanPlateChecksum)
        && sha256.test(translation.translationStateChecksum)
        && translation.translationStateChecksum === (latestG9StateEvent?.outputChecksum
          ?? translation.g8Checksum)
        && g8Terminal.outputChecksum === translation.g8Checksum
        && translation.cleanPlateCandidateId === (g8Terminal.evidence.candidateId ?? null)
        && translation.cleanPlateChecksum === (g8Terminal.evidence.candidateChecksum
          ?? g8Terminal.evidence.qualityChecksum)
        && translation.eligibleRegions.map((entry) => entry.regionId).join('\0') === eligibleIds.join('\0')
        && new Set(translation.eligibleRegions.map((entry) => entry.regionId)).size === eligibleIds.length
        && translation.eligibleRegions.every((entry, index) => {
          const region = eligibleRegions[index];
          return Boolean(region && entry.readingOrder === region.order && entry.regionType === region.type
            && entry.direction === region.direction && entry.paragraphGroupId === region.paragraphGroupId
            && entry.sourceText === region.sourceText && sha256.test(entry.sourceTextChecksum)
            && sha256.test(entry.contextChecksum) && entry.rubyExcluded === true
            && !entry.contextRegionIds.includes(entry.regionId));
        })
        && candidatesValid
        && Object.keys(translation.acceptedCandidateIdsByRegion).length === Object.keys(acceptedMap).length
        && Object.entries(acceptedMap).every(([regionId, candidateId]) =>
          translation.acceptedCandidateIdsByRegion[regionId] === candidateId)
        && translation.reviewedRegionCount === Object.keys(acceptedMap).length
        && (translation.state === 'pending'
          ? translation.terminalChecksum === null && !g9Terminal
          : Boolean(g9Terminal && translation.terminalChecksum
            && sha256.test(translation.terminalChecksum)
            && g9Terminal.inputChecksum === translation.translationStateChecksum
            && g9Terminal.outputChecksum === translation.terminalChecksum
            && g9Terminal.state === translation.state))
        && (translation.state === 'pending'
          || (translation.state === 'not-applicable'
            && eligibleIds.length === 0 && translation.candidates.length === 0)
          || (translation.state === 'accepted'
            && eligibleIds.length > 0 && eligibleIds.every((id) => id in acceptedMap)
            && translation.candidates.every((candidate) => candidate.review !== null))));
      if (!contextValid) throw new Error('G9 上下文、候选 revision 链、QC 或 accepted clean plate 绑定不一致。');
      const selected = translation.candidates.find((candidate) => candidate.review === null)?.candidateId
        ?? translation.candidates.at(-1)?.candidateId ?? '';
      set((currentState) => ({
        translationContexts: { ...currentState.translationContexts, [imageId]: translation },
        translationLoading: { ...currentState.translationLoading, [imageId]: false },
        selectedTranslationCandidateIds: selected
          ? { ...currentState.selectedTranslationCandidateIds, [imageId]: selected }
          : Object.fromEntries(Object.entries(currentState.selectedTranslationCandidateIds)
            .filter(([id]) => id !== imageId)),
      }));
      return true;
    } catch (error) {
      if (translationLoadTokens.get(imageId) !== requestToken) return false;
      const conflict = error instanceof ApiError && error.status === 409;
      const message = errorMessage(error);
      set((state) => {
        const current = state.g4Contexts[imageId];
        return {
          globalError: message,
          revisionConflict: conflict,
          translationLoading: { ...state.translationLoading, [imageId]: false },
          g4Contexts: current ? { ...state.g4Contexts, [imageId]: { ...current, error: message, conflict } } : state.g4Contexts,
        };
      });
      return false;
    } finally {
      if (translationLoadTokens.get(imageId) === requestToken) translationLoadTokens.delete(imageId);
    }
  },

  loadTypesetContext: async (imageId, force = false) => {
    const lineage = get().g4Contexts[imageId];
    if (!lineage || lineage.status !== 'active' || workflowPhase(lineage) !== 'G10'
      || lineage.conflict || lineage.error) return false;
    if (!force && get().typesetContexts[imageId]) return true;
    const requestToken = Symbol(imageId);
    typesetLoadTokens.set(imageId, requestToken);
    set((state) => ({ typesetLoading: { ...state.typesetLoading, [imageId]: true } }));
    try {
      const typeset = await api.getTypesetGateContext(imageId);
      if (typesetLoadTokens.get(imageId) !== requestToken) return false;
      const state = get();
      const current = state.g4Contexts[imageId];
      const generation = current?.status === 'active' ? current.generation : null;
      const image = state.images.find((entry) => entry.id === imageId);
      const events = current?.status === 'active'
        ? [...current.events].sort((left, right) => left.sequence - right.sequence) : [];
      const g9Terminal = [...events].reverse().find((event) =>
        event.gate === 'G9_translation' && event.operation === 'translation-stage-review'
        && (event.state === 'accepted' || event.state === 'not-applicable'));
      const g8Terminal = [...events].reverse().find((event) =>
        event.gate === 'G8_cleanPlate' && event.operation === 'clean-plate-stage-review'
        && (event.state === 'accepted' || event.state === 'not-applicable'));
      const sha256 = /^[0-9a-f]{64}$/;
      const exactKeys = (value: object, keys: readonly string[]) =>
        Object.keys(value).sort().join('\0') === [...keys].sort().join('\0');
      const uniqueStrings = (value: unknown): value is string[] => Array.isArray(value)
        && value.every((entry) => typeof entry === 'string' && entry.length > 0)
        && new Set(value).size === value.length;
      const validActor = (actor: LineageActor) => {
        if (!actor || typeof actor !== 'object' || Array.isArray(actor)) return false;
        const allowed = new Set([
          'actorKind', 'actorId', 'taskId', 'threadId', 'sessionId', 'operationSource',
        ]);
        const identities = [actor.actorId, actor.taskId, actor.threadId, actor.sessionId];
        return Object.keys(actor).every((key) => allowed.has(key))
          && ['codex', 'cursor', 'human', 'system'].includes(actor.actorKind)
          && ['ui', 'api', 'script'].includes(actor.operationSource)
          && identities.every((value) => value === undefined || value === null
            || (typeof value === 'string' && value.length >= 1 && value.length <= 128
              && !/[\\/\0\r\n]/.test(value)))
          && identities.some((value) => typeof value === 'string' && value.length > 0);
      };
      const validStyle = (style: TypesetRegionStyle, route?: string) => {
        const font = typeset.availableFonts.find((entry) => entry.token === style.fontToken);
        return exactKeys(style, [
          'fontToken', 'fontChecksum', 'fontSize', 'minFontSize', 'padding', 'fill',
          'strokeColor', 'strokeWidth', 'rotation', 'scaleX', 'scaleY', 'shearX',
          'shearY', 'opacity', 'visualCenterX', 'visualCenterY', 'align', 'lineSpacing',
          'letterSpacing', 'autoFit', 'fontSource',
        ])
          && Boolean(font && font.fontChecksum === style.fontChecksum
            && sha256.test(font.fontChecksum) && sha256.test(font.capabilityChecksum)
            && (route !== 'art-lettering' || font.role === 'display'))
          && /^#[0-9A-F]{6}$/.test(style.fill) && /^#[0-9A-F]{6}$/.test(style.strokeColor)
          && Number.isInteger(style.fontSize) && style.fontSize >= 6 && style.fontSize <= 512
          && Number.isInteger(style.minFontSize) && style.minFontSize >= 6
          && style.minFontSize <= style.fontSize
          && Number.isInteger(style.padding) && style.padding >= 0 && style.padding <= 128
          && Number.isInteger(style.strokeWidth) && style.strokeWidth >= 0 && style.strokeWidth <= 32
          && Number.isFinite(style.rotation) && style.rotation >= -180 && style.rotation <= 180
          && Number.isFinite(style.scaleX) && style.scaleX >= 0.25 && style.scaleX <= 4
          && Number.isFinite(style.scaleY) && style.scaleY >= 0.25 && style.scaleY <= 4
          && Number.isFinite(style.shearX) && style.shearX >= -1 && style.shearX <= 1
          && Number.isFinite(style.shearY) && style.shearY >= -1 && style.shearY <= 1
          && Number.isFinite(style.opacity) && style.opacity >= 0.05 && style.opacity <= 1
          && Number.isFinite(style.visualCenterX) && style.visualCenterX >= 0 && style.visualCenterX <= 1
          && Number.isFinite(style.visualCenterY) && style.visualCenterY >= 0 && style.visualCenterY <= 1
          && Number.isFinite(style.lineSpacing) && style.lineSpacing >= 0 && style.lineSpacing <= 3
          && Number.isFinite(style.letterSpacing) && style.letterSpacing >= -10 && style.letterSpacing <= 50
          && ['start', 'center', 'end'].includes(style.align)
          && ['server-regular-default', 'server-display-default', 'region-override'].includes(style.fontSource)
          && typeof style.autoFit === 'boolean'
          && (route !== 'art-lettering' || style.letterSpacing === 0)
          && (!['bubble', 'ordinary'].includes(route ?? '') || (
            style.scaleX === 1 && style.scaleY === 1 && style.shearX === 0 && style.shearY === 0
            && style.visualCenterX === 0.5 && style.visualCenterY === 0.5
          ));
      };
      const validStyleInput = (style: TypesetRegionStyleInput, route?: string) => {
        const font = typeset.availableFonts.find((entry) => entry.token === style.fontToken);
        return exactKeys(style, [
          'fontToken', 'fontSize', 'minFontSize', 'padding', 'fill', 'strokeColor',
          'strokeWidth', 'rotation', 'scaleX', 'scaleY', 'shearX', 'shearY', 'opacity',
          'visualCenterX', 'visualCenterY', 'align', 'lineSpacing', 'letterSpacing', 'autoFit',
        ]) && Boolean(font && (route !== 'art-lettering' || font.role === 'display'))
          && /^#[0-9A-F]{6}$/.test(style.fill) && /^#[0-9A-F]{6}$/.test(style.strokeColor)
          && Number.isInteger(style.fontSize) && style.fontSize >= 6 && style.fontSize <= 512
          && Number.isInteger(style.minFontSize) && style.minFontSize >= 6 && style.minFontSize <= style.fontSize
          && Number.isInteger(style.padding) && style.padding >= 0 && style.padding <= 128
          && Number.isInteger(style.strokeWidth) && style.strokeWidth >= 0 && style.strokeWidth <= 32
          && Number.isFinite(style.rotation) && style.rotation >= -180 && style.rotation <= 180
          && Number.isFinite(style.scaleX) && style.scaleX >= 0.25 && style.scaleX <= 4
          && Number.isFinite(style.scaleY) && style.scaleY >= 0.25 && style.scaleY <= 4
          && Number.isFinite(style.shearX) && style.shearX >= -1 && style.shearX <= 1
          && Number.isFinite(style.shearY) && style.shearY >= -1 && style.shearY <= 1
          && Number.isFinite(style.opacity) && style.opacity >= 0.05 && style.opacity <= 1
          && Number.isFinite(style.visualCenterX) && style.visualCenterX >= 0 && style.visualCenterX <= 1
          && Number.isFinite(style.visualCenterY) && style.visualCenterY >= 0 && style.visualCenterY <= 1
          && Number.isFinite(style.lineSpacing) && style.lineSpacing >= 0 && style.lineSpacing <= 3
          && Number.isFinite(style.letterSpacing) && style.letterSpacing >= -10 && style.letterSpacing <= 50
          && ['start', 'center', 'end'].includes(style.align)
          && typeof style.autoFit === 'boolean'
          && (route !== 'art-lettering' || style.letterSpacing === 0)
          && (!['bubble', 'ordinary'].includes(route ?? '') || (
            style.scaleX === 1 && style.scaleY === 1 && style.shearX === 0 && style.shearY === 0
            && style.visualCenterX === 0.5 && style.visualCenterY === 0.5
          ));
      };
      const styleInputFromFrozen = (style: TypesetRegionStyle): TypesetRegionStyleInput => ({
        fontToken: style.fontToken, fontSize: style.fontSize, minFontSize: style.minFontSize,
        padding: style.padding, fill: style.fill, strokeColor: style.strokeColor,
        strokeWidth: style.strokeWidth, rotation: style.rotation, scaleX: style.scaleX,
        scaleY: style.scaleY, shearX: style.shearX, shearY: style.shearY,
        opacity: style.opacity, visualCenterX: style.visualCenterX,
        visualCenterY: style.visualCenterY, align: style.align, lineSpacing: style.lineSpacing,
        letterSpacing: style.letterSpacing, autoFit: style.autoFit,
      });
      const validRouteManifest = (manifest: TypesetGateContext['routeManifest']) => Array.isArray(manifest)
        && manifest.length > 0
        && new Set(manifest.map((entry) => entry.regionId)).size === manifest.length
        && new Set(manifest.map((entry) => entry.readingOrder)).size === manifest.length
        && manifest.every((entry, index) => exactKeys(entry, [
          'regionId', 'readingOrder', 'route', 'renderRequired',
          'translationCandidateId', 'translationCandidateChecksum',
        ]) && typeof entry.regionId === 'string' && entry.regionId
          && Number.isInteger(entry.readingOrder) && entry.readingOrder >= 0
          && (index === 0 || entry.readingOrder > manifest[index - 1]!.readingOrder)
          && ['bubble', 'ordinary', 'art-lettering', 'keep', 'ignore'].includes(entry.route)
          && entry.renderRequired === ['bubble', 'ordinary', 'art-lettering'].includes(entry.route)
          && (entry.renderRequired
            ? typeof entry.translationCandidateId === 'string' && entry.translationCandidateId
              && typeof entry.translationCandidateChecksum === 'string'
              && sha256.test(entry.translationCandidateChecksum)
            : entry.translationCandidateId === null && entry.translationCandidateChecksum === null));
      const routeIds = typeset.routeManifest.map((entry) => entry.regionId);
      const renderRouteIds = typeset.routeManifest.filter((entry) => entry.renderRequired)
        .map((entry) => entry.regionId);
      const bubbleTypes = new Set(['dialogue', 'speech', 'thought']);
      const ordinaryTypes = new Set(['narration', 'title', 'background', 'sign', 'other']);
      const routeForRegion = (region: Pick<Region, 'type' | 'contentDisposition'>): TypesetRoute | null => {
        if (region.contentDisposition === 'redraw-art') return 'art-lettering';
        if (region.contentDisposition === 'keep-art') return 'keep';
        if (region.contentDisposition === 'ignore') return 'ignore';
        if (region.contentDisposition !== 'translate') return null;
        if (bubbleTypes.has(region.type)) return 'bubble';
        if (ordinaryTypes.has(region.type)) return 'ordinary';
        return null;
      };
      const currentRouteRegions = [...(state.regionsByImage[imageId] ?? [])]
        .filter((region) => region.type !== 'ruby'
          && region.contentDisposition !== 'false-positive'
          && region.contentDisposition !== null)
        .sort((left, right) => left.order - right.order || left.id.localeCompare(right.id));
      const currentRegionGridValid = Boolean(image && image.width > 0 && image.height > 0
        && currentRouteRegions.every((region) => [
          region.x, region.y, region.width, region.height, region.rotation,
        ].every((value) => Number.isFinite(value))
          && region.x >= 0 && region.y >= 0 && region.width > 0 && region.height > 0
          && region.x + region.width <= image.width && region.y + region.height <= image.height));
      const routeMatchesCurrentRegions = currentRegionGridValid
        && currentRouteRegions.length === typeset.routeManifest.length
        && currentRouteRegions.every((region, index) => {
          const route = typeset.routeManifest[index];
          const expected = routeForRegion(region);
          return Boolean(route && route.regionId === region.id && route.readingOrder === region.order
            && expected && route.route === expected);
        });
      const expectedFeatures = [
        'explicit-installed-chinese-display-font', 'fill-stroke', 'rotation',
        'nonuniform-scale', 'shear-affine', 'opacity', 'visual-center',
        'alignment', 'line-spacing',
      ];
      const routeChecksum = await canonicalSha256(typeset.routeManifest);
      const hasRegularRoute = typeset.routeManifest.some((entry) =>
        entry.route === 'bubble' || entry.route === 'ordinary');
      const hasArtRoute = typeset.routeManifest.some((entry) => entry.route === 'art-lettering');
      const fontCatalogValid = (await Promise.all(typeset.availableFonts.map(async (font) => {
        if (!exactKeys(font, ['token', 'label', 'fontChecksum', 'capabilityChecksum', 'role'])
          || typeof font.token !== 'string' || !font.token
          || typeof font.label !== 'string' || !font.label
          || !sha256.test(font.fontChecksum) || !sha256.test(font.capabilityChecksum)
          || !['regular', 'display'].includes(font.role)) return false;
        const capabilityChecksum = await canonicalSha256({
          fileChecksum: font.fontChecksum,
          role: font.role,
          contractVersion: 'g10-art-lettering-v1',
          chineseGlyphProbe: '中文永',
        });
        return font.token === `installed-font-${font.fontChecksum.slice(0, 24)}`
          && font.capabilityChecksum === capabilityChecksum;
      }))).every(Boolean);
      let valid = Boolean(exactKeys(typeset, [
        'imageId', 'imageRevision', 'generationId', 'nextSequence', 'g9TerminalChecksum',
        'translationStateChecksum', 'cleanPlateCandidateId', 'cleanPlateChecksum', 'state',
        'terminalChecksum', 'candidates', 'reviews', 'routeManifest', 'routeChecksum',
        'styleDefaults', 'availableFonts', 'availableDisplayFonts',
        'artLetteringCapability', 'retryRegionStyles',
      ]) && generation && image && g8Terminal && g9Terminal
        && workflowPhase(current) === 'G10'
        && typeset.imageId === imageId && typeset.imageRevision === image.revision
        && typeset.generationId === generation.id && typeset.nextSequence === generation.nextSequence
        && ['pending', 'accepted'].includes(typeset.state)
        && typeset.g9TerminalChecksum === g9Terminal.outputChecksum
        && typeset.translationStateChecksum === g9Terminal.inputChecksum
        && g8Terminal.sequence < g9Terminal.sequence
        && g9Terminal.parentChecksum === g8Terminal.outputChecksum
        && typeset.cleanPlateCandidateId === (g8Terminal.evidence.candidateId ?? null)
        && typeset.cleanPlateChecksum === (g8Terminal.evidence.candidateChecksum
          ?? g8Terminal.evidence.qualityChecksum)
        && (typeset.cleanPlateCandidateId === null
          || (typeof typeset.cleanPlateCandidateId === 'string'
            && typeset.cleanPlateCandidateId.length > 0))
        && sha256.test(typeset.g9TerminalChecksum) && sha256.test(typeset.translationStateChecksum)
        && sha256.test(typeset.cleanPlateChecksum) && sha256.test(typeset.routeChecksum)
        && typeset.routeChecksum === routeChecksum
        && validRouteManifest(typeset.routeManifest) && routeMatchesCurrentRegions
        && (!hasRegularRoute || typeset.availableFonts.length > 0)
        && new Set(typeset.availableFonts.map((font) => font.token)).size === typeset.availableFonts.length
        && fontCatalogValid
        && new Set(typeset.availableDisplayFonts.map((font) => font.token)).size
          === typeset.availableDisplayFonts.length
        && typeset.availableDisplayFonts.every((font) => font.role === 'display'
          && typeset.availableFonts.some((entry) => canonicalJson(entry) === canonicalJson(font)))
        && canonicalJson(typeset.availableDisplayFonts)
          === canonicalJson(typeset.availableFonts.filter((font) => font.role === 'display'))
        && exactKeys(typeset.artLetteringCapability, ['available', 'contractVersion', 'features', 'reason'])
        && typeset.artLetteringCapability.contractVersion === 'g10-art-lettering-v1'
        && uniqueStrings(typeset.artLetteringCapability.features)
        && canonicalJson([...typeset.artLetteringCapability.features].sort())
          === canonicalJson([...expectedFeatures].sort())
        && typeof typeset.artLetteringCapability.available === 'boolean'
        && typeset.artLetteringCapability.available === (typeset.availableDisplayFonts.length > 0)
        && (typeset.artLetteringCapability.available
          ? typeset.artLetteringCapability.reason === null
          : typeof typeset.artLetteringCapability.reason === 'string'
            && typeset.artLetteringCapability.reason.length > 0)
        && (!hasArtRoute || !typeset.artLetteringCapability.available
          || typeset.availableDisplayFonts.length > 0));
      const candidateIds = new Set<string>();
      const jobItemIds = new Set<string>();
      const revisionIds = new Set<string>();
      const candidateSequences = new Set<number>();
      const validRegionTypes = new Set([
        'dialogue', 'narration', 'sound_effect', 'title', 'background', 'unknown',
        'thought', 'sign', 'speech', 'other',
      ]);
      const expectedRoute = (entry: TypesetCandidate['regionManifest'][number]) => {
        return routeForRegion({ type: entry.regionType, contentDisposition: entry.contentDisposition });
      };
      for (const [candidateIndex, candidate] of typeset.candidates.entries()) {
        const [styleManifestChecksum, layoutManifestChecksum] = await Promise.all([
          canonicalSha256(candidate.styleManifest, TYPESET_PYTHON_FLOAT_KEYS),
          canonicalSha256(candidate.layoutManifest, TYPESET_PYTHON_FLOAT_KEYS),
        ]);
        const produced = events.find((event) => event.gate === 'G10_typeset'
          && event.operation === 'typeset-candidate-produced'
          && event.jobId === candidate.jobId && event.jobItemId === candidate.jobItemId
          && event.evidence.candidateId === candidate.candidateId);
        const completion = events.find((event) => event.gate === 'G10_typeset'
          && event.operation === 'typeset-job-completed'
          && event.jobId === candidate.jobId && event.jobItemId === candidate.jobItemId
          && event.evidence.candidateId === candidate.candidateId);
        const completionValid = !completion || Boolean(produced
          && completion.sequence > produced.sequence
          && completion.provider === candidate.provider
          && completion.modelVersion === candidate.modelVersion
          && completion.parameterHash === candidate.parameterHash
          && completion.revisionId === null
          && completion.evidence.candidateId === candidate.candidateId
          && completion.evidence.candidateChecksum === candidate.candidateChecksum
          && completion.evidence.g9TerminalChecksum === candidate.g9TerminalChecksum
          && completion.evidence.cleanPlateChecksum === candidate.cleanPlateChecksum
          && completion.evidence.routeChecksum === candidate.routeChecksum
          && completion.evidence.styleChecksum === candidate.styleChecksum
          && completion.evidence.layoutChecksum === candidate.layoutChecksum
          && completion.evidence.width === candidate.width
          && completion.evidence.height === candidate.height
          && completion.evidence.renderScale === candidate.renderScale
          && canonicalJson(completion.evidence.overflowRegionIds)
            === canonicalJson(candidate.overflowRegionIds)
          && canonicalJson(completion.evidence.anomalies) === canonicalJson(candidate.anomalies));
        const routesValid = validRouteManifest(candidate.routeManifest)
          && canonicalJson(candidate.routeManifest) === canonicalJson(typeset.routeManifest)
          && candidate.routeChecksum === typeset.routeChecksum;
        const regionIds = candidate.regionManifest.map((entry) => entry.regionId);
        const regionsValid = candidate.regionManifest.length === routeIds.length
          && candidate.regionManifest.every((entry, index) => {
            const route = candidate.routeManifest[index];
            const currentRegion = currentRouteRegions[index];
            const geometry = entry.geometry;
            return Boolean(route && currentRegion && exactKeys(entry, [
              'regionId', 'regionRevision', 'geometry', 'readingOrder', 'regionType',
              'direction', 'paragraphGroupId', 'contentDisposition',
              'acceptedTranslationCandidateId', 'acceptedTranslationCandidateChecksum',
            ]) && exactKeys(geometry, ['x', 'y', 'width', 'height', 'rotation'])
              && entry.regionId === route.regionId && entry.readingOrder === route.readingOrder
              && entry.regionId === currentRegion.id
              && entry.regionRevision === currentRegion.revision
              && entry.readingOrder === currentRegion.order
              && geometry.x === currentRegion.x && geometry.y === currentRegion.y
              && geometry.width === currentRegion.width && geometry.height === currentRegion.height
              && geometry.rotation === currentRegion.rotation
              && [geometry.x, geometry.y, geometry.width, geometry.height, geometry.rotation]
                .every((value) => typeof value === 'number' && Number.isFinite(value))
              && geometry.width > 0 && geometry.height > 0
              && validRegionTypes.has(entry.regionType) && entry.regionType === currentRegion.type
              && entry.regionType !== 'ruby' && entry.direction === currentRegion.direction
              && ['horizontal', 'vertical'].includes(entry.direction)
              && entry.paragraphGroupId === currentRegion.paragraphGroupId
              && entry.contentDisposition === currentRegion.contentDisposition
              && (entry.paragraphGroupId === null
                || (typeof entry.paragraphGroupId === 'string' && entry.paragraphGroupId.length > 0))
              && ['translate', 'redraw-art', 'keep-art', 'ignore'].includes(entry.contentDisposition)
              && route.route === expectedRoute(entry)
              && entry.acceptedTranslationCandidateId === route.translationCandidateId
              && entry.acceptedTranslationCandidateChecksum === route.translationCandidateChecksum);
          });
        const stylesValid = candidate.styleManifest.length === routeIds.length
          && candidate.styleManifest.every((entry, index) => {
            const route = candidate.routeManifest[index];
            return Boolean(route && exactKeys(entry, ['regionId', 'route', 'style'])
              && entry.regionId === route.regionId && entry.route === route.route
              && (route.renderRequired ? entry.style && validStyle(entry.style, route.route) : entry.style === null)
              && (route.route !== 'art-lettering' || entry.style?.fontSource !== 'server-regular-default'));
          });
        const layoutsValid = candidate.layoutManifest.length === renderRouteIds.length
          && candidate.layoutManifest.every((entry, index) => {
            const route = candidate.routeManifest.filter((item) => item.renderRequired)[index];
            const style = candidate.styleManifest.find((item) => item.regionId === entry.regionId)?.style;
            const region = candidate.regionManifest.find((item) => item.regionId === entry.regionId);
            return Boolean(route && exactKeys(entry, [
              'regionId', 'route', 'bounds', 'fontSize', 'overflow', 'direction', 'rotation',
              'scaleX', 'scaleY', 'shearX', 'shearY', 'opacity', 'visualCenterX',
              'visualCenterY', 'align',
            ]) && exactKeys(entry.bounds, ['x', 'y', 'width', 'height'])
              && entry.regionId === route.regionId && entry.route === route.route
              && [entry.bounds.x, entry.bounds.y, entry.bounds.width, entry.bounds.height]
                .every((value) => typeof value === 'number' && Number.isFinite(value))
              && entry.bounds.width > 0 && entry.bounds.height > 0
              && Number.isInteger(entry.fontSize) && entry.fontSize >= 1
              && ['horizontal', 'vertical'].includes(entry.direction)
              && typeof entry.overflow === 'boolean' && style && region
              && entry.fontSize >= Math.max(1, Math.round(style.minFontSize * candidate.renderScale))
              && entry.fontSize <= Math.max(1, Math.round(style.fontSize * candidate.renderScale))
              && entry.direction === region.direction
              && entry.rotation === region.geometry.rotation + style.rotation
              && entry.scaleX === style.scaleX && entry.scaleY === style.scaleY
              && entry.shearX === style.shearX && entry.shearY === style.shearY
              && entry.opacity === style.opacity
              && entry.visualCenterX === style.visualCenterX
              && entry.visualCenterY === style.visualCenterY && entry.align === style.align);
          });
        valid = valid && Boolean(
          !candidateIds.has(candidate.candidateId) && !jobItemIds.has(candidate.jobItemId)
          && !candidateSequences.has(candidate.sequence) && !revisionIds.has(candidate.revisionId)
          && exactKeys(candidate, [
            'candidateId', 'sequence', 'jobId', 'jobItemId', 'parentChecksum',
            'g9TerminalChecksum', 'translationStateChecksum', 'cleanPlateCandidateId',
            'cleanPlateChecksum', 'regionManifest', 'routeManifest', 'routeChecksum',
            'styleManifest', 'styleChecksum', 'layoutManifest', 'layoutChecksum', 'provider',
            'modelVersion', 'parameterHash', 'candidateChecksum', 'width', 'height',
            'renderScale', 'overflowRegionIds', 'anomalies', 'revisionId', 'completed',
            'artifactUrl', 'review', 'createdAt',
          ])
          && typeof candidate.candidateId === 'string' && candidate.candidateId
          && typeof candidate.jobId === 'string' && candidate.jobId
          && typeof candidate.jobItemId === 'string' && candidate.jobItemId
          && typeof candidate.revisionId === 'string' && candidate.revisionId
          && typeof candidate.createdAt === 'string' && candidate.createdAt
          && Number.isInteger(candidate.sequence) && candidate.sequence >= 1
          && (candidateIndex === 0
            || candidate.sequence > typeset.candidates[candidateIndex - 1]!.sequence)
          && produced && completionValid && candidate.completed === Boolean(completion)
          && candidate.sequence === produced.sequence
          && candidate.revisionId === produced.revisionId
          && candidate.provider === produced.provider
          && candidate.modelVersion === produced.modelVersion
          && candidate.parameterHash === produced.parameterHash
          && candidate.provider === 'pillow-g10'
          && candidate.modelVersion === 'g10-typeset-v1'
          && candidate.candidateChecksum === produced.evidence.candidateChecksum
          && candidate.routeChecksum === produced.evidence.routeChecksum
          && candidate.styleChecksum === produced.evidence.styleChecksum
          && candidate.layoutChecksum === produced.evidence.layoutChecksum
          && candidate.styleChecksum === styleManifestChecksum
          && candidate.layoutChecksum === layoutManifestChecksum
          && candidate.width === produced.evidence.width && candidate.height === produced.evidence.height
          && candidate.renderScale === produced.evidence.renderScale
          && canonicalJson(candidate.overflowRegionIds) === canonicalJson(produced.evidence.overflowRegionIds)
          && canonicalJson(candidate.anomalies) === canonicalJson(produced.evidence.anomalies)
          && candidate.parentChecksum === typeset.g9TerminalChecksum
          && candidate.g9TerminalChecksum === typeset.g9TerminalChecksum
          && candidate.translationStateChecksum === typeset.translationStateChecksum
          && candidate.cleanPlateCandidateId === typeset.cleanPlateCandidateId
          && candidate.cleanPlateChecksum === typeset.cleanPlateChecksum
          && regionsValid
          && new Set(regionIds).size === regionIds.length
          && canonicalJson(regionIds) === canonicalJson(routeIds)
          && candidate.regionManifest.every((entry) => entry.regionType !== 'ruby'
            && entry.contentDisposition !== 'false-positive')
          && routesValid && stylesValid && layoutsValid
          && [candidate.styleChecksum, candidate.layoutChecksum, candidate.parameterHash,
            candidate.candidateChecksum].every((checksum) => sha256.test(checksum))
          && Number.isInteger(candidate.width) && candidate.width > 0
          && Number.isInteger(candidate.height) && candidate.height > 0
          && [1, 2, 3, 4].includes(candidate.renderScale)
          && candidate.width === (image?.width ?? -1) * candidate.renderScale
          && candidate.height === (image?.height ?? -1) * candidate.renderScale
          && uniqueStrings(candidate.overflowRegionIds) && uniqueStrings(candidate.anomalies)
          && canonicalJson(candidate.overflowRegionIds)
            === canonicalJson(candidate.layoutManifest.filter((entry) => entry.overflow)
              .map((entry) => entry.regionId))
          && candidate.artifactUrl === api.typesetCandidateUrl(imageId, candidate.candidateId),
        );
        candidateIds.add(candidate.candidateId);
        jobItemIds.add(candidate.jobItemId);
        candidateSequences.add(candidate.sequence);
        revisionIds.add(candidate.revisionId);
      }
      const acceptedReviews = typeset.reviews.filter((review) => review.state === 'accepted');
      const reviewIds = new Set<string>();
      const reviewSequences = new Set<number>();
      for (const [reviewIndex, review] of typeset.reviews.entries()) {
        const candidate = typeset.candidates.find((entry) => entry.candidateId === review.candidateId);
        const event = events.find((entry) => entry.gate === 'G10_typeset'
          && entry.operation === 'typeset-candidate-reviewed'
          && entry.evidence.candidateId === review.candidateId);
        const exactChecks = review.checks.length === TYPESET_CHECKS.length
          && new Set(review.checks.map((entry) => entry.check)).size === TYPESET_CHECKS.length
          && review.checks.every((entry, index) => exactKeys(entry, ['check', 'passed'])
            && entry.check === TYPESET_CHECKS[index] && typeof entry.passed === 'boolean');
        const failed = review.checks.filter((entry) => !entry.passed).map((entry) => entry.check);
        const knownDefects = Boolean(candidate
          && (candidate.overflowRegionIds.length || candidate.anomalies.length));
        const overflowFailed = review.checks.find((entry) => entry.check === 'overflow-free')
          ?.passed === false;
        const verdictValid = review.state === 'accepted'
          ? review.reason === 'typeset-reviewed' && failed.length === 0
            && candidate?.overflowRegionIds.length === 0 && candidate.anomalies.length === 0
          : failed.length > 0 && (!knownDefects || overflowFailed)
            && (review.reason === 'multiple-visual-failures'
            ? failed.length > 1 : failed.includes(review.reason as TypesetCheck));
        valid = valid && Boolean(candidate && event && exactChecks && verdictValid
          && exactKeys(review, [
            'id', 'sequence', 'candidateId', 'state', 'reason', 'parentChecksum',
            'candidateChecksum', 'routeChecksum', 'styleChecksum', 'layoutChecksum',
            'g9TerminalChecksum', 'cleanPlateChecksum', 'observedWidth', 'observedHeight',
            'observedRenderScale', 'checks', 'reviewer', 'terminalChecksum', 'revisionId',
            'createdAt',
          ])
          && typeof review.id === 'string' && review.id && !reviewIds.has(review.id)
          && ['accepted', 'rejected'].includes(review.state)
          && typeof review.revisionId === 'string' && review.revisionId
          && !revisionIds.has(review.revisionId)
          && Number.isInteger(review.sequence) && review.sequence >= 1
          && (reviewIndex === 0
            || review.sequence > typeset.reviews[reviewIndex - 1]!.sequence)
          && !reviewSequences.has(review.sequence) && validActor(review.reviewer)
          && typeof review.createdAt === 'string' && review.createdAt
          && review.sequence === event.sequence && review.candidateChecksum === candidate.candidateChecksum
          && review.revisionId === event.revisionId
          && canonicalJson(review.reviewer) === canonicalJson(event.actor)
          && review.parentChecksum === typeset.g9TerminalChecksum
          && review.routeChecksum === candidate.routeChecksum
          && review.styleChecksum === candidate.styleChecksum
          && review.layoutChecksum === candidate.layoutChecksum
          && review.g9TerminalChecksum === candidate.g9TerminalChecksum
          && review.cleanPlateChecksum === candidate.cleanPlateChecksum
          && review.observedWidth === candidate.width && review.observedHeight === candidate.height
          && review.observedRenderScale === candidate.renderScale
          && review.terminalChecksum === event.outputChecksum && sha256.test(review.terminalChecksum)
          && canonicalJson(candidate.review) === canonicalJson(review));
        reviewIds.add(review.id);
        reviewSequences.add(review.sequence);
        revisionIds.add(review.revisionId);
      }
      const latestRejectedCandidate = [...typeset.candidates].reverse().find((candidate) =>
        typeset.reviews.some((review) => review.candidateId === candidate.candidateId
          && review.state === 'rejected'));
      const expectedRetryStyles = latestRejectedCandidate
        ? Object.fromEntries(latestRejectedCandidate.styleManifest.flatMap((entry) =>
          entry.style ? [[entry.regionId, styleInputFromFrozen(entry.style)] as const] : []))
        : {};
      valid = valid && typeset.reviews.length <= typeset.candidates.length
        && new Set(typeset.reviews.map((review) => review.candidateId)).size === typeset.reviews.length
        && typeset.candidates.every((candidate) => {
          const review = typeset.reviews.find((entry) => entry.candidateId === candidate.candidateId);
          return canonicalJson(candidate.review) === canonicalJson(review ?? null);
        })
        && exactKeys(typeset.styleDefaults, ['bubble', 'ordinary', 'artLettering'])
        && (typeset.styleDefaults.bubble === null || validStyle(typeset.styleDefaults.bubble, 'bubble'))
        && (typeset.styleDefaults.ordinary === null || validStyle(typeset.styleDefaults.ordinary, 'ordinary'))
        && (typeset.styleDefaults.artLettering === null
          || validStyle(typeset.styleDefaults.artLettering, 'art-lettering'))
        && (typeset.styleDefaults.bubble !== null) === (typeset.availableFonts.length > 0)
        && (typeset.styleDefaults.ordinary !== null) === (typeset.availableFonts.length > 0)
        && (typeset.styleDefaults.artLettering !== null)
          === typeset.artLetteringCapability.available
        && (!typeset.routeManifest.some((entry) => entry.route === 'bubble')
          || Boolean(typeset.styleDefaults.bubble))
        && (!typeset.routeManifest.some((entry) => entry.route === 'ordinary')
          || Boolean(typeset.styleDefaults.ordinary))
        && (!hasArtRoute || (typeset.artLetteringCapability.available
          ? Boolean(typeset.styleDefaults.artLettering
            && typeset.styleDefaults.artLettering.fontSource === 'server-display-default')
          : typeset.styleDefaults.artLettering === null))
        && Object.keys(typeset.retryRegionStyles).every((id) => renderRouteIds.includes(id))
        && Object.entries(typeset.retryRegionStyles).every(([id, style]) => validStyleInput(
          style, typeset.routeManifest.find((entry) => entry.regionId === id)?.route,
        ))
        && canonicalJson(typeset.retryRegionStyles) === canonicalJson(expectedRetryStyles)
        && (typeset.state === 'pending'
          ? acceptedReviews.length === 0 && typeset.terminalChecksum === null
          : acceptedReviews.length === 1 && typeset.terminalChecksum === acceptedReviews[0]!.terminalChecksum
            && typeset.candidates.at(-1)?.candidateId === acceptedReviews[0]!.candidateId);
      if (!valid) throw new Error('G10 上下文、manifest、候选/review 血缘或 raster 事实不一致。');
      const defaultStyles = Object.fromEntries(typeset.routeManifest
        .filter((entry) => entry.renderRequired)
        .flatMap((entry) => {
          const frozen = entry.route === 'bubble' ? typeset.styleDefaults.bubble
            : entry.route === 'ordinary' ? typeset.styleDefaults.ordinary
              : typeset.styleDefaults.artLettering;
          return frozen ? [[entry.regionId, styleInputFromFrozen(frozen)] as const] : [];
        }));
      const draftStyles = Object.keys(typeset.retryRegionStyles).length
        ? typeset.retryRegionStyles : defaultStyles;
      const selected = typeset.candidates.find((candidate) =>
        !typeset.reviews.some((review) => review.candidateId === candidate.candidateId))?.candidateId
        ?? typeset.candidates.at(-1)?.candidateId ?? '';
      set((currentState) => ({
        typesetContexts: { ...currentState.typesetContexts, [imageId]: typeset },
        typesetLoading: { ...currentState.typesetLoading, [imageId]: false },
        selectedTypesetCandidateIds: selected
          ? { ...currentState.selectedTypesetCandidateIds, [imageId]: selected }
          : Object.fromEntries(Object.entries(currentState.selectedTypesetCandidateIds)
            .filter(([id]) => id !== imageId)),
        typesetBitmapObservations: Object.fromEntries(Object.entries(currentState.typesetBitmapObservations)
          .filter(([id]) => id !== imageId)),
        typesetStyleDrafts: { ...currentState.typesetStyleDrafts, [imageId]: draftStyles },
      }));
      return true;
    } catch (error) {
      if (typesetLoadTokens.get(imageId) !== requestToken) return false;
      const conflict = error instanceof ApiError && error.status === 409;
      const message = errorMessage(error);
      set((state) => {
        const current = state.g4Contexts[imageId];
        return {
          globalError: message,
          revisionConflict: conflict,
          typesetLoading: { ...state.typesetLoading, [imageId]: false },
          g4Contexts: current
            ? { ...state.g4Contexts, [imageId]: { ...current, error: message, conflict } }
            : state.g4Contexts,
        };
      });
      return false;
    } finally {
      if (typesetLoadTokens.get(imageId) === requestToken) typesetLoadTokens.delete(imageId);
    }
  },

  reloadActiveImage: async () => {
    const imageId = get().activeImageId;
    const projectId = get().currentProject?.id;
    if (!imageId || !projectId) return;
    ocrLoadTokens.delete(imageId);
    maskLoadTokens.delete(imageId);
    cleanPlateLoadTokens.delete(imageId);
    translationLoadTokens.delete(imageId);
    typesetLoadTokens.delete(imageId);
    set((state) => ({
      pendingRegionMutations: state.pendingRegionMutations.filter(
        (mutation) => mutation.imageId !== imageId,
      ),
      pendingG4Mutations: state.pendingG4Mutations.filter(
        (mutation) => mutation.imageId !== imageId,
      ),
      g4Contexts: Object.fromEntries(
        Object.entries(state.g4Contexts).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      backgroundContexts: Object.fromEntries(
        Object.entries(state.backgroundContexts).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      backgroundLoading: Object.fromEntries(
        Object.entries(state.backgroundLoading).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      ocrContexts: Object.fromEntries(
        Object.entries(state.ocrContexts).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      ocrLoading: Object.fromEntries(
        Object.entries(state.ocrLoading).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      maskContexts: Object.fromEntries(
        Object.entries(state.maskContexts).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      maskLoading: Object.fromEntries(
        Object.entries(state.maskLoading).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      selectedMaskArtifactIds: Object.fromEntries(
        Object.entries(state.selectedMaskArtifactIds).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      maskBitmapObservations: Object.fromEntries(
        Object.entries(state.maskBitmapObservations).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      cleanPlateContexts: Object.fromEntries(
        Object.entries(state.cleanPlateContexts).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      cleanPlateLoading: Object.fromEntries(
        Object.entries(state.cleanPlateLoading).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      selectedCleanPlateCandidateIds: Object.fromEntries(
        Object.entries(state.selectedCleanPlateCandidateIds)
          .filter(([entryImageId]) => entryImageId !== imageId),
      ),
      cleanPlateBitmapObservations: Object.fromEntries(
        Object.entries(state.cleanPlateBitmapObservations)
          .filter(([entryImageId]) => entryImageId !== imageId),
      ),
      translationContexts: Object.fromEntries(
        Object.entries(state.translationContexts).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      translationLoading: Object.fromEntries(
        Object.entries(state.translationLoading).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      selectedTranslationCandidateIds: Object.fromEntries(
        Object.entries(state.selectedTranslationCandidateIds).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      typesetContexts: Object.fromEntries(
        Object.entries(state.typesetContexts).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      typesetLoading: Object.fromEntries(
        Object.entries(state.typesetLoading).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      selectedTypesetCandidateIds: Object.fromEntries(
        Object.entries(state.selectedTypesetCandidateIds).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      typesetBitmapObservations: Object.fromEntries(
        Object.entries(state.typesetBitmapObservations).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      typesetStyleDrafts: Object.fromEntries(
        Object.entries(state.typesetStyleDrafts).filter(([entryImageId]) => entryImageId !== imageId),
      ),
      g4SavingImageId: state.g4SavingImageId === imageId ? null : state.g4SavingImageId,
      g4GateSavingImageId: state.g4GateSavingImageId === imageId
        ? null
        : state.g4GateSavingImageId,
      g5SavingRegionId: null,
      g5GateSavingImageId: state.g5GateSavingImageId === imageId
        ? null
        : state.g5GateSavingImageId,
      g6SavingRegionId: null,
      g6GateSavingImageId: state.g6GateSavingImageId === imageId
        ? null
        : state.g6GateSavingImageId,
      g7DraftSavingImageId: state.g7DraftSavingImageId === imageId
        ? null
        : state.g7DraftSavingImageId,
      g7GateSavingImageId: state.g7GateSavingImageId === imageId
        ? null
        : state.g7GateSavingImageId,
      g8GateSavingImageId: state.g8GateSavingImageId === imageId
        ? null
        : state.g8GateSavingImageId,
      g9GateSavingImageId: state.g9GateSavingImageId === imageId
        ? null
        : state.g9GateSavingImageId,
      g10GateSavingImageId: state.g10GateSavingImageId === imageId
        ? null
        : state.g10GateSavingImageId,
      saveError: '',
      globalError: '',
      revisionConflict: false,
      selectedRegionIds: [],
    }));
    try {
      const jobs = (await api.listJobs(projectId)).map(hydrateJob);
      set((state) => state.currentProject?.id === projectId ? { jobs } : {});
      await synchronizeImages(projectId);
      const [regionsLoaded, contextLoaded] = await Promise.all([
        get().loadRegions(imageId, true),
        get().loadG4Context(imageId, true),
      ]);
      const refreshedContext = get().g4Contexts[imageId];
      const refreshedPhase = workflowPhase(refreshedContext);
      if (
        regionsLoaded
        && contextLoaded
        && refreshedPhase === 'G5'
      ) {
        await get().loadBackgroundContext(imageId, true);
      }
      if (
        regionsLoaded
        && contextLoaded
        && (refreshedPhase === 'G6' || refreshedPhase === 'G7' || refreshedPhase === 'G8')
      ) {
        await get().loadOCRContext(imageId, true);
      }
      if (regionsLoaded && contextLoaded && (refreshedPhase === 'G7' || refreshedPhase === 'G8')) {
        await get().loadMaskContext(imageId, true);
      }
      if (regionsLoaded && contextLoaded && refreshedPhase === 'G8') {
        await get().loadCleanPlateContext(imageId, true);
      }
      if (regionsLoaded && contextLoaded && refreshedPhase === 'G9') {
        await get().loadTranslationContext(imageId, true);
      }
      if (regionsLoaded && contextLoaded && refreshedPhase === 'G10') {
        await get().loadTypesetContext(imageId, true);
      }
      if (!regionsLoaded && contextLoaded) {
        const message = get().globalError || '无法刷新本页权威文本框，请再次重载。';
        set((state) => {
          const context = state.g4Contexts[imageId];
          return {
            globalError: message,
            g4Contexts: {
              ...state.g4Contexts,
              [imageId]: context?.status === 'active'
                ? { ...context, error: message, conflict: false }
                : {
                    status: 'error',
                    generation: null,
                    events: [],
                    error: message,
                    conflict: false,
                  },
            },
          };
        });
      }
    } catch (error) {
      const message = `重载本页失败：${errorMessage(error)}`;
      set((state) => ({
        globalError: message,
        g4Contexts: {
          ...state.g4Contexts,
          [imageId]: {
            status: 'error',
            generation: null,
            events: [],
            error: message,
            conflict: false,
          },
        },
      }));
    }
  },

  selectImage: async (imageId, options) => {
    if (imageId !== get().activeImageId) {
      if (!(await get().flushAutosave())) return false;
      set({ activeImageId: imageId, selectedRegionIds: [] });
      await Promise.all([
        get().loadRegions(imageId),
        get().loadG4Context(imageId),
      ]);
    }
    if (options?.focusOverflow) get().focusActiveOverflow();
    if (options?.focusFailure) get().focusActiveFailure();
    return true;
  },

  navigateImage: async (direction, target = 'adjacent') => {
    const state = get();
    const { images, activeImageId } = state;
    if (!images.length) return false;
    const currentIndex = Math.max(0, images.findIndex((image) => image.id === activeImageId));
    const step = direction < 0 ? -1 : 1;
    if (target === 'adjacent') {
      const nextImage = adjacentVisibleImage(images, visibleWorkbenchImages(state), activeImageId, step);
      if (!nextImage || nextImage.id === activeImageId) return false;
      return get().selectImage(nextImage.id, {
        focusOverflow: state.imageFilter === 'overflow',
        focusFailure: state.imageFilter === 'failed',
      });
    }
    const matches = target === 'overflow'
      ? imageHasTypesetOverflow
      : target === 'failed'
        ? imageHasProcessingFailure
        : imagePageReviewPending;
    for (let seen = 0; seen < images.length - 1; seen += 1) {
      const index = (currentIndex + step * (seen + 1) + images.length * (seen + 1)) % images.length;
      const nextImage = images[index];
      if (nextImage && matches(nextImage)) {
        return get().selectImage(nextImage.id, {
          focusOverflow: target === 'overflow',
          focusFailure: target === 'failed',
        });
      }
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
    const state = get();
    const imageId = state.activeImageId;
    if (!imageId) return null;
    const context = state.g4Contexts[imageId];
    if (!context || context.status === 'loading' || context.status === 'error') {
      set({ globalError: context?.error || '正在确认本页血缘状态，请稍后再创建文本框。' });
      return null;
    }
    const activeG4 = context.status === 'active';
    if (activeG4 && g4EditingLocked(state, imageId)) {
      set({ globalError: '当前 G4 操作尚未完成；请等待或重载本页。' });
      return null;
    }
    const current = state.regionsByImage[imageId] ?? [];
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
      translationProvider: null,
      type: 'dialogue',
      direction: activeG4 ? 'vertical' : 'auto',
      order: activeG4
        ? current.length
        : current.reduce((max, entry) => Math.max(max, entry.order), 0) + 1,
      paragraphGroupId: activeG4 ? id('paragraph') : null,
      rubyParentId: null,
      contentDisposition: activeG4 ? 'translate' : null,
      backgroundCategory: null,
      backgroundConfidence: null,
      backgroundRationaleCodes: null,
      backgroundReviewer: null,
      backgroundGenerationId: null,
      ocrReview: null,
      ocrReviewer: null,
      ocrGenerationId: null,
      detectorJobItemId: null,
      detectorCandidateIndex: null,
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
    set((currentState) => ({
      regionsByImage: { ...currentState.regionsByImage, [imageId]: next },
      images: updateImageCounts(currentState.images, imageId, next, true),
      selectedRegionIds: [region.id],
      ...(activeG4
        ? {
            pendingG4Mutations: replaceG4Mutation(currentState.pendingG4Mutations, {
              mutationId: id('g4-mutation'),
              kind: 'create',
              imageId,
              region,
              expectedRevision: 0,
            }),
          }
        : {
            past: [...currentState.past.slice(-49), makeHistoryFrame(currentState.regionsByImage)],
            future: [],
            pendingRegionMutations: replaceRegionMutation(currentState.pendingRegionMutations, {
              mutationId: id('mutation'),
              kind: 'create',
              imageId,
              region,
              expectedRevision: 0,
            }),
          }),
    }));
    scheduleAutosave();
    return region.id;
  },

  updateRegion: (regionId, patch, recordHistory = true) => {
    const state = get();
    const imageId = state.activeImageId;
    if (!imageId) return;
    const context = state.g4Contexts[imageId];
    if (!context || context.status === 'loading' || context.status === 'error') {
      set({ globalError: context?.error || '正在确认本页血缘状态，请稍后再编辑。' });
      return;
    }
    const current = state.regionsByImage[imageId] ?? [];
    const original = current.find((region) => region.id === regionId);
    if (!original) return;
    if (context.status === 'active') {
      if (g4EditingLocked(state, imageId)) {
        set({ globalError: '当前 G4 操作尚未完成；请等待或重载本页。' });
        return;
      }
      const g4Patch = activeG4Patch(patch);
      if (!g4Patch) {
        set({ globalError: '活动页代次当前只允许修改 G4 区域几何与语义字段。' });
        return;
      }
      const updated = hydrateRegion({ ...original, ...g4Patch });
      const mutationPatch = activeG4Patch(sparseRegionPatch(original, updated));
      if (!mutationPatch || !Object.keys(mutationPatch).length) return;
      const next = current.map((region) => region.id === regionId ? updated : region);
      set((currentState) => ({
        regionsByImage: { ...currentState.regionsByImage, [imageId]: next },
        images: updateImageCounts(currentState.images, imageId, next, true),
        pendingG4Mutations: replaceG4Mutation(currentState.pendingG4Mutations, {
          mutationId: id('g4-mutation'),
          kind: 'update',
          imageId,
          region: updated,
          patch: mutationPatch,
          expectedRevision: currentState.serverRegionRevisions[original.id] ?? original.revision,
        }),
      }));
      scheduleAutosave();
      return;
    }
    const exclusivePatch = patch.confirmed === true
      ? { ...patch, ignored: false }
      : patch.ignored === true
        ? { ...patch, confirmed: false }
        : patch;
    let updated = hydrateRegion({
      ...original,
      ...exclusivePatch,
      style: exclusivePatch.style
        ? applyNestedRegionPatch(original.style, exclusivePatch.style)
        : original.style,
      repair: exclusivePatch.repair
        ? applyNestedRegionPatch(original.repair, exclusivePatch.repair)
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
    const mutationPatch = sparseRegionPatch(original, updated);
    if (!Object.keys(mutationPatch).length) return;
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
        patch: mutationPatch,
        expectedRevision: currentState.serverRegionRevisions[original.id] ?? original.revision,
      }),
    }));
    scheduleAutosave();
  },

  nudgeSelectedRegions: (dx, dy) => {
    const state = get();
    const image = activeImage(state);
    if (!image || !state.selectedRegionIds.length || (!dx && !dy)) return;
    if (
      state.g4Contexts[image.id]?.status === 'active'
      && state.selectedRegionIds.length !== 1
    ) {
      set({ globalError: 'G4 阶段请逐框微调，以保持每次变更的独立血缘证据。' });
      return;
    }
    for (const regionId of state.selectedRegionIds) {
      const region = (state.regionsByImage[image.id] ?? []).find((entry) => entry.id === regionId);
      if (!region) continue;
      get().updateRegion(regionId, {
        x: Math.max(0, Math.min(image.width - region.width, region.x + dx)),
        y: Math.max(0, Math.min(image.height - region.height, region.y + dy)),
      });
    }
  },

  setRegionConfirmed: async (regionId, confirmed) => {
    const state = get();
    const imageId = state.activeImageId;
    if (imageId && state.g4Contexts[imageId]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能确认 OCR 文本。' });
      return false;
    }
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
    const context = state.g4Contexts[imageId];
    if (!context || context.status === 'loading' || context.status === 'error') {
      set({ globalError: context?.error || '正在确认本页血缘状态，请稍后再删除。' });
      return;
    }
    if (context.status === 'active' && g4EditingLocked(state, imageId)) {
      set({ globalError: '当前 G4 操作尚未完成；请等待或重载本页。' });
      return;
    }
    if (context.status === 'active' && state.selectedRegionIds.length !== 1) {
      set({ globalError: 'G4 阶段请逐个删除文本框，以保持每次血缘变更可核对。' });
      return;
    }
    const selected = new Set(state.selectedRegionIds);
    const current = state.regionsByImage[imageId] ?? [];
    const removed = current.filter((region) => selected.has(region.id));
    if (
      context.status === 'active'
      && removed.some((region) =>
        region.detectorJobItemId !== null || region.detectorCandidateIndex !== null
      )
    ) {
      set({ globalError: '检测候选必须选择明确处置；如属误检，请保留并标记为“误检”。' });
      return;
    }
    const next = current.filter((region) => !selected.has(region.id));
    let mutations = state.pendingRegionMutations;
    let g4Mutations = state.pendingG4Mutations;
    for (const region of removed) {
      if (context.status === 'active') {
        g4Mutations = replaceG4Mutation(g4Mutations, {
          mutationId: id('g4-mutation'),
          kind: 'delete',
          imageId,
          region,
          expectedRevision: state.serverRegionRevisions[region.id] ?? region.revision,
        });
      } else {
        mutations = replaceRegionMutation(mutations, {
          mutationId: id('mutation'),
          kind: 'delete',
          imageId,
          region,
          expectedRevision: state.serverRegionRevisions[region.id] ?? region.revision,
        });
      }
    }
    set((currentState) => ({
      regionsByImage: { ...currentState.regionsByImage, [imageId]: next },
      images: updateImageCounts(currentState.images, imageId, next, true),
      selectedRegionIds: [],
      ...(context.status === 'active'
        ? { pendingG4Mutations: g4Mutations }
        : {
            past: [...currentState.past.slice(-49), makeHistoryFrame(currentState.regionsByImage)],
            future: [],
            pendingRegionMutations: mutations,
          }),
    }));
    scheduleAutosave();
  },

  mergeSelectedRegions: () => {
    const state = get();
    const imageId = state.activeImageId;
    if (!imageId || state.selectedRegionIds.length < 2) return;
    if (state.g4Contexts[imageId]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能使用旧版文本框合并。' });
      return;
    }
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
      paragraphGroupId: null,
      rubyParentId: null,
      contentDisposition: null,
      detectorJobItemId: null,
      detectorCandidateIndex: null,
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

  consolidateActiveImageRegions: () => {
    const state = get();
    const image = activeImage(state);
    if (!image) return 0;
    if (state.g4Contexts[image.id]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能执行旧版自动合并。' });
      return 0;
    }
    const current = get().regionsByImage[image.id] ?? [];
    if (!current.length) return 0;
    const clusters = clusterRegionIds(current, image).filter((ids) => ids.length > 1);
    let changed = 0;
    for (const ids of clusters) {
      set({ selectedRegionIds: ids });
      get().mergeSelectedRegions();
      changed += 1;
    }
    const afterMerge = get().regionsByImage[image.id] ?? [];
    for (const region of afterMerge) {
      const geometry = expandRegionGeometry(region, image, region.direction);
      if (
        geometry.x === region.x
        && geometry.y === region.y
        && geometry.width === region.width
        && geometry.height === region.height
      ) continue;
      get().updateRegion(region.id, geometry);
      changed += 1;
    }
    return changed;
  },

  splitSelectedRegion: (axis) => {
    const state = get();
    const imageId = state.activeImageId;
    if (!imageId || state.selectedRegionIds.length !== 1) return;
    if (state.g4Contexts[imageId]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能使用旧版文本框拆分。' });
      return;
    }
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
      paragraphGroupId: null,
      rubyParentId: null,
      contentDisposition: null,
      detectorJobItemId: null,
      detectorCandidateIndex: null,
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

  moveG4Region: async (regionId, direction) => {
    if (!(await get().flushAutosave())) return false;
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    if (
      !context
      || context.status !== 'active'
      || !lineage
      || g4EditingLocked(state, image.id)
    ) {
      set({ globalError: context?.error || '当前 G4 血缘上下文不可写，请重载本页。' });
      return false;
    }
    const ordered = [...(state.regionsByImage[image.id] ?? [])]
      .sort((left, right) => left.order - right.order);
    const index = ordered.findIndex((region) => region.id === regionId);
    const target = index + direction;
    if (index < 0 || target < 0 || target >= ordered.length) return false;
    [ordered[index], ordered[target]] = [ordered[target]!, ordered[index]!];
    set({ g4GateSavingImageId: image.id, globalError: '' });
    try {
      const regions = (await api.reorderG4Regions(
        image.id,
        ordered.map((region) => region.id),
        image.revision,
        lineage,
      )).map(hydrateRegion).sort((left, right) => left.order - right.order);
      set((current) => {
        const serverRegionRevisions = { ...current.serverRegionRevisions };
        for (const region of regions) serverRegionRevisions[region.id] = region.revision;
        return {
          regionsByImage: { ...current.regionsByImage, [image.id]: regions },
          serverRegionRevisions,
          images: updateImageCounts(current.images, image.id, regions, true),
        };
      });
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭，无法刷新 G4 权威状态。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true))) {
        throw new Error('阅读顺序已保存，但无法读取新的血缘序号；请重载本页。');
      }
      if (!(await get().loadRegions(image.id, true))) {
        throw new Error('阅读顺序已保存，但无法刷新权威文本框；请重载本页。');
      }
      set({ g4GateSavingImageId: null });
      return true;
    } catch (error) {
      const message = errorMessage(error);
      set((current) => ({
        g4GateSavingImageId: null,
        globalError: message,
        revisionConflict: error instanceof ApiError && error.status === 409,
        g4Contexts: {
          ...current.g4Contexts,
          [image.id]: {
            ...context,
            error: message,
            conflict: error instanceof ApiError && error.status === 409,
          },
        },
      }));
      return false;
    }
  },

  startG4Detection: async () => {
    if (!(await get().flushAutosave())) return false;
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const context = state.g4Contexts[image.id];
    if (
      !context
      || context.status !== 'active'
      || !context.generation
      || g4EditingLocked(state, image.id)
    ) {
      set({ globalError: context?.error || '当前 G4 血缘上下文不可用于检测，请重载本页。' });
      return false;
    }
    set({ g4GateSavingImageId: image.id, globalError: '' });
    try {
      const job = hydrateJob(await api.startJob(project.id, 'detect', {
        imageIds: [image.id],
        options: {
          provider: project.settings.detectorProvider,
          direction: 'auto',
          concurrency: 1,
        },
        lineage: {
          runId: context.generation.runId,
          actor: uiLineageActor(),
          pages: [{
            imageId: image.id,
            pageGenerationId: context.generation.id,
            expectedSequence: context.generation.nextSequence,
          }],
        },
      }));
      set((current) => ({
        jobs: [job, ...current.jobs.filter((entry) => entry.id !== job.id)],
        images: current.images.map((entry) => entry.id === image.id
          ? {
              ...entry,
              detectorProvider: project.settings.detectorProvider,
              status: { ...entry.status, detection: 'queued' },
            }
          : entry),
      }));
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true))) {
        throw new Error('检测任务已排队，但无法读取新的血缘序号；请重载本页。');
      }
      set({ g4GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const detail = errorMessage(error);
      const message = conflict
        ? detail
        : `检测任务提交或核对结果不确定：${detail}。请重载本页核对任务与血缘。`;
      set((current) => ({
        g4GateSavingImageId: null,
        globalError: message,
        revisionConflict: conflict,
        g4Contexts: {
          ...current.g4Contexts,
          [image.id]: { ...context, error: message, conflict },
        },
      }));
      return false;
    }
  },

  acceptG4Regions: async () => {
    if (!(await get().flushAutosave())) return false;
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    const observedChecksum = latestG4RegionChecksum(context);
    if (
      !context
      || context.status !== 'active'
      || !lineage
      || !observedChecksum
      || g4EditingLocked(state, image.id)
    ) {
      set({ globalError: context?.error || '当前 G4 草稿证据不完整，暂不能接受。' });
      return false;
    }
    if (g4RegionsAccepted(context)) return true;
    set({ g4GateSavingImageId: image.id, globalError: '' });
    try {
      await api.acceptRegionsGate(image.id, observedChecksum, image.revision, lineage);
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭，无法刷新 G4 权威状态。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true))) {
        throw new Error('G4 已接受，但无法读取新的血缘序号；请重载本页。');
      }
      if (!(await get().loadRegions(image.id, true))) {
        throw new Error('G4 已接受，但无法刷新权威文本框；请重载本页。');
      }
      set({ g4GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const detail = errorMessage(error);
      const message = conflict
        ? detail
        : `G4 接受提交或核对结果不确定：${detail}。请重载本页核对门禁与血缘。`;
      set((current) => ({
        g4GateSavingImageId: null,
        globalError: message,
        revisionConflict: conflict,
        g4Contexts: {
          ...current.g4Contexts,
          [image.id]: { ...context, error: message, conflict },
        },
      }));
      return false;
    }
  },

  saveG5Background: async (regionId, category, confidence, rationaleCodes) => {
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const background = state.backgroundContexts[image.id];
    const region = (state.regionsByImage[image.id] ?? []).find((entry) => entry.id === regionId);
    const lineage = context ? mutationLineage(context) : null;
    const uniqueRationales = [...new Set(rationaleCodes)];
    const draftIsValid = BACKGROUND_CATEGORIES.includes(category)
      && typeof confidence === 'number'
      && Number.isFinite(confidence)
      && confidence >= 0
      && confidence <= 1
      && uniqueRationales.length === rationaleCodes.length
      && uniqueRationales.length > 0
      && uniqueRationales.every((code) => BACKGROUND_RATIONALE_CODES.includes(code))
      && uniqueRationales.includes(BACKGROUND_RATIONALE_ANCHOR[category]);
    if (!draftIsValid) {
      set({ globalError: 'G5 分类证据无效：请填写 0–1 的置信度，并保留与所选类别匹配的受控理由。' });
      return false;
    }
    if (
      !context
      || context.status !== 'active'
      || !context.generation
      || workflowPhase(context) !== 'G5'
      || !lineage
      || !background
      || background.state !== 'pending'
      || background.generationId !== context.generation?.id
      || background.nextSequence !== context.generation.nextSequence
      || background.imageRevision !== image.revision
      || !region
      || !backgroundClassificationRequired(region)
      || !background.eligibleRegionIds.includes(region.id)
      || g5EditingLocked(state, image.id)
      || state.pendingG4Mutations.length > 0
    ) {
      set({ globalError: context?.error || '当前 G5 权威上下文不可写，请重载本页后再分类。' });
      return false;
    }

    set({ g5SavingRegionId: region.id, globalError: '' });
    try {
      const saved = hydrateRegion(await api.updateBackgroundClassification(region.id, {
        category,
        confidence,
        rationaleCodes: uniqueRationales,
        expectedRevision: state.serverRegionRevisions[region.id] ?? region.revision,
        expectedImageRevision: image.revision,
        lineage,
      }));
      set((current) => ({
        regionsByImage: {
          ...current.regionsByImage,
          [image.id]: (current.regionsByImage[image.id] ?? []).map((entry) =>
            entry.id === saved.id ? saved : entry
          ),
        },
        serverRegionRevisions: {
          ...current.serverRegionRevisions,
          [saved.id]: saved.revision,
        },
      }));
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭，无法刷新 G5 权威状态。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true))) {
        throw new Error('G5 分类已提交，但无法读取新的血缘序号；请重载本页。');
      }
      if (!(await get().loadRegions(image.id, true))) {
        throw new Error('G5 分类已提交，但无法刷新权威文本框；请重载本页。');
      }
      if (!(await get().loadBackgroundContext(image.id, true))) {
        throw new Error('G5 分类已提交，但无法刷新权威门禁上下文；请重载本页。');
      }
      set({ g5SavingRegionId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const detail = errorMessage(error);
      const message = conflict
        ? detail
        : `G5 分类提交或核对结果不确定：${detail}。请重载本页核对分类与血缘。`;
      set((current) => {
        const latest = current.g4Contexts[image.id] ?? context;
        return {
          g5SavingRegionId: null,
          globalError: message,
          revisionConflict: conflict,
          g4Contexts: {
            ...current.g4Contexts,
            [image.id]: { ...latest, error: message, conflict },
          },
        };
      });
      return false;
    }
  },

  acceptG5Background: async () => {
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const background = state.backgroundContexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    const regions = state.regionsByImage[image.id] ?? [];
    const locallyEligible = regions.filter(backgroundClassificationRequired);
    const eligibleIds = [...locallyEligible.map((region) => region.id)].sort();
    const authoritativeEligibleIds = [...(background?.eligibleRegionIds ?? [])].sort();
    const classifiedIds = [...(background?.classifiedRegionIds ?? [])].sort();
    const allComplete = locallyEligible.every((region) =>
      context?.generation
        ? backgroundClassificationComplete(region, context.generation.id)
        : false
    );
    if (
      !context
      || context.status !== 'active'
      || !context.generation
      || workflowPhase(context) !== 'G5'
      || !lineage
      || !background
      || background.state !== 'pending'
      || background.generationId !== context.generation?.id
      || background.nextSequence !== context.generation.nextSequence
      || background.imageRevision !== image.revision
      || eligibleIds.join('\0') !== authoritativeEligibleIds.join('\0')
      || eligibleIds.join('\0') !== classifiedIds.join('\0')
      || !allComplete
      || g5EditingLocked(state, image.id)
      || state.pendingG4Mutations.length > 0
    ) {
      set({ globalError: context?.error || 'G5 尚有未分类区域，或权威上下文已变化；请完成分类或重载本页。' });
      return false;
    }

    set({ g5GateSavingImageId: image.id, globalError: '' });
    try {
      await api.acceptBackgroundGate(
        image.id,
        eligibleIds.length
          ? 'all-eligible-backgrounds-reviewed'
          : 'no-eligible-regions',
        background.backgroundChecksum,
        image.revision,
        lineage,
      );
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭，无法刷新 G5 权威状态。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true))) {
        throw new Error('G5 已接受，但无法读取新的血缘序号；请重载本页。');
      }
      if (!(await get().loadRegions(image.id, true))) {
        throw new Error('G5 已接受，但无法刷新权威文本框；请重载本页。');
      }
      if (!(await get().loadOCRContext(image.id, true))) {
        throw new Error('G5 已接受，但无法读取新的 G6 OCR 上下文；请重载本页。');
      }
      set({ g5GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const detail = errorMessage(error);
      const message = conflict
        ? detail
        : `G5 接受提交或核对结果不确定：${detail}。请重载本页核对门禁与血缘。`;
      set((current) => {
        const latest = current.g4Contexts[image.id] ?? context;
        return {
          g5GateSavingImageId: null,
          globalError: message,
          revisionConflict: conflict,
          g4Contexts: {
            ...current.g4Contexts,
            [image.id]: { ...latest, error: message, conflict },
          },
        };
      });
      return false;
    }
  },

  startG6OCR: async () => {
    if (!(await get().flushAutosave())) return false;
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const context = state.g4Contexts[image.id];
    const ocr = state.ocrContexts[image.id];
    const regions = state.regionsByImage[image.id] ?? [];
    const eligibleIds = regions.filter(ocrSourceReviewRequired).map((region) => region.id).sort();
    const authoritativeIds = [...(ocr?.eligibleRegionIds ?? [])].sort();
    const provider = state.capabilities.providers.find((entry) =>
      entry.kind === 'ocr' && entry.id === project.settings.ocrProvider
    );
    if (
      !context
      || context.status !== 'active'
      || !context.generation
      || workflowPhase(context) !== 'G6'
      || !ocr
      || ocr.state !== 'pending'
      || ocr.generationId !== context.generation.id
      || ocr.nextSequence !== context.generation.nextSequence
      || ocr.imageRevision !== image.revision
      || eligibleIds.join('\0') !== authoritativeIds.join('\0')
      || eligibleIds.length === 0
      || ocr.attemptedRegionIds.length > 0
      || ocr.attempts.length > 0
      || g6EditingLocked(state, image.id)
    ) {
      set({
        globalError: eligibleIds.length === 0
          ? '本页没有需本地化的 translate / redraw-art 区域；请使用 G6 不适用门禁，不要创建 OCR 任务。'
          : context?.error || '当前 G6 权威上下文不可运行 OCR，请重载本页。',
      });
      return false;
    }
    if (!provider?.available || !provider.local || provider.isMock) {
      set({ globalError: 'G6 只允许可用的本地真实 OCR provider；未创建任务。' });
      return false;
    }

    set({ g6GateSavingImageId: image.id, globalError: '' });
    try {
      const job = hydrateJob(await api.startJob(project.id, 'ocr', {
        imageIds: [image.id],
        options: {
          provider: project.settings.ocrProvider,
          language: project.settings.sourceLanguage,
          concurrency: 1,
        },
        lineage: {
          runId: context.generation.runId,
          actor: uiLineageActor(),
          pages: [{
            imageId: image.id,
            pageGenerationId: context.generation.id,
            expectedSequence: context.generation.nextSequence,
          }],
        },
      }));
      set((current) => ({
        jobs: [job, ...current.jobs.filter((entry) => entry.id !== job.id)],
        images: current.images.map((entry) => entry.id === image.id
          ? {
              ...entry,
              ocrProvider: project.settings.ocrProvider,
              status: { ...entry.status, ocr: 'queued' },
            }
          : entry),
      }));
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true))) {
        throw new Error('OCR 任务已排队，但无法读取新的血缘序号；请重载本页。');
      }
      if (!(await get().loadOCRContext(image.id, true))) {
        throw new Error('OCR 任务已排队，但无法刷新 G6 权威上下文；请重载本页。');
      }
      set({ g6GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const detail = errorMessage(error);
      const message = conflict
        ? detail
        : `OCR 任务提交或核对结果不确定：${detail}。请重载本页核对任务与血缘。`;
      set((current) => {
        const latest = current.g4Contexts[image.id] ?? context;
        return {
          g6GateSavingImageId: null,
          globalError: message,
          revisionConflict: conflict,
          g4Contexts: {
            ...current.g4Contexts,
            [image.id]: { ...latest, error: message, conflict },
          },
        };
      });
      return false;
    }
  },

  saveG6SourceReview: async (
    regionId,
    sourceText,
    sourceMode,
    selectedAttemptId,
    qcChecks,
  ) => {
    const cleanedSource = sourceText.trim();
    const uniqueChecks = [...new Set(qcChecks)];
    const localDraftValid = Boolean(cleanedSource)
      && !cleanedSource.includes('\ufffd')
      && ![...cleanedSource].some((character) => {
        const code = character.charCodeAt(0);
        return code < 32 && character !== '\n' && character !== '\t';
      })
      && (sourceMode === 'original-attempt'
        || sourceMode === 'quality-attempt'
        || sourceMode === 'manual-correction')
      && Boolean(selectedAttemptId)
      && uniqueChecks.length === qcChecks.length
      && uniqueChecks.length === OCR_QC_CHECKS.length
      && OCR_QC_CHECKS.every((check) => uniqueChecks.includes(check));
    if (!localDraftValid) {
      set({ globalError: 'G6 原文核对证据无效：原文不能为空，并须逐项确认全部 9 项 QC。' });
      return false;
    }

    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const ocr = state.ocrContexts[image.id];
    const region = (state.regionsByImage[image.id] ?? []).find((entry) => entry.id === regionId);
    const lineage = context ? mutationLineage(context) : null;
    const attempts = (ocr?.attempts ?? []).filter((attempt) =>
      attempt.regionId === regionId
      && attempt.generationId === context?.generation?.id
    );
    const selectedAttempt = attempts.find((attempt) => attempt.id === selectedAttemptId);
    const selectedPair = selectedAttempt
      ? attempts.filter((attempt) => attempt.jobItemId === selectedAttempt.jobItemId)
      : [];
    const pairVariants = new Set(selectedPair.map((attempt) => attempt.inputVariant));
    const sourceMatches = sourceMode === 'manual-correction'
      || cleanedSource === selectedAttempt?.text.trim();
    const modeMatches = sourceMode === 'manual-correction'
      || selectedAttempt?.inputVariant === (sourceMode === 'original-attempt' ? 'original' : 'quality');
    if (
      !context
      || context.status !== 'active'
      || !context.generation
      || workflowPhase(context) !== 'G6'
      || !lineage
      || !ocr
      || ocr.state !== 'pending'
      || ocr.generationId !== context.generation.id
      || ocr.nextSequence !== context.generation.nextSequence
      || ocr.imageRevision !== image.revision
      || !region
      || !ocrSourceReviewRequired(region)
      || !ocr.eligibleRegionIds.includes(region.id)
      || !selectedAttempt
      || pairVariants.size !== 2
      || !pairVariants.has('original')
      || !pairVariants.has('quality')
      || !sourceMatches
      || !modeMatches
      || g6EditingLocked(state, image.id)
      || state.pendingG4Mutations.length > 0
    ) {
      set({ globalError: context?.error || '当前 G6 权威上下文或双路 OCR 证据不可写，请重载本页。' });
      return false;
    }

    set({ g6SavingRegionId: region.id, globalError: '' });
    try {
      const saved = hydrateRegion(await api.updateOCRSourceReview(region.id, {
        sourceText: cleanedSource,
        sourceMode,
        selectedAttemptId,
        qcChecks: uniqueChecks,
        expectedRevision: state.serverRegionRevisions[region.id] ?? region.revision,
        expectedImageRevision: image.revision,
        lineage,
      }));
      set((current) => ({
        regionsByImage: {
          ...current.regionsByImage,
          [image.id]: (current.regionsByImage[image.id] ?? []).map((entry) =>
            entry.id === saved.id ? saved : entry
          ),
        },
        serverRegionRevisions: {
          ...current.serverRegionRevisions,
          [saved.id]: saved.revision,
        },
      }));
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭，无法刷新 G6 权威状态。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true))) {
        throw new Error('G6 原文核对已提交，但无法读取新的血缘序号；请重载本页。');
      }
      if (!(await get().loadRegions(image.id, true))) {
        throw new Error('G6 原文核对已提交，但无法刷新权威文本框；请重载本页。');
      }
      if (!(await get().loadOCRContext(image.id, true))) {
        throw new Error('G6 原文核对已提交，但无法刷新权威门禁上下文；请重载本页。');
      }
      set({ g6SavingRegionId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const detail = errorMessage(error);
      const message = conflict
        ? detail
        : `G6 原文核对提交或结果不确定：${detail}。请重载本页核对原文与血缘。`;
      set((current) => {
        const latest = current.g4Contexts[image.id] ?? context;
        return {
          g6SavingRegionId: null,
          globalError: message,
          revisionConflict: conflict,
          g4Contexts: {
            ...current.g4Contexts,
            [image.id]: { ...latest, error: message, conflict },
          },
        };
      });
      return false;
    }
  },

  acceptG6OCR: async () => {
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const ocr = state.ocrContexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    const regions = state.regionsByImage[image.id] ?? [];
    const eligible = regions.filter(ocrSourceReviewRequired);
    const eligibleIds = eligible.map((region) => region.id).sort();
    const authoritativeIds = [...(ocr?.eligibleRegionIds ?? [])].sort();
    const attemptedIds = [...(ocr?.attemptedRegionIds ?? [])].sort();
    const reviewedIds = [...(ocr?.reviewedRegionIds ?? [])].sort();
    const attemptsComplete = eligible.every((region) => {
      const selectedAttemptId = region.ocrReview?.selectedAttemptId;
      const attempts = ocr?.attempts.filter((attempt) => attempt.regionId === region.id) ?? [];
      const selected = attempts.find((attempt) => attempt.id === selectedAttemptId);
      if (!selected) return false;
      const selectedPair = attempts.filter(
        (attempt) => attempt.jobItemId === selected.jobItemId,
      );
      return selectedPair.length === 2
        && new Set(selectedPair.map((attempt) => attempt.inputVariant)).size === 2
        && selectedPair.every(
          (attempt) => attempt.generationId === context?.generation?.id,
        );
    });
    const reviewsComplete = eligible.every((region) =>
      context?.generation
        ? (() => {
            if (!ocrSourceReviewComplete(region, context.generation.id)) return false;
            const selected = ocr?.attempts.find((attempt) =>
              attempt.regionId === region.id && attempt.id === region.ocrReview?.selectedAttemptId
            );
            if (!selected || !region.ocrReview) return false;
            if (region.ocrReview.sourceMode === 'original-attempt') {
              return selected.inputVariant === 'original'
                && region.sourceText.trim() === selected.text.trim();
            }
            if (region.ocrReview.sourceMode === 'quality-attempt') {
              return selected.inputVariant === 'quality'
                && region.sourceText.trim() === selected.text.trim();
            }
            return region.ocrReview.sourceMode === 'manual-correction';
          })()
        : false
    );
    if (
      !context
      || context.status !== 'active'
      || !context.generation
      || workflowPhase(context) !== 'G6'
      || !lineage
      || !ocr
      || ocr.state !== 'pending'
      || ocr.generationId !== context.generation.id
      || ocr.nextSequence !== context.generation.nextSequence
      || ocr.imageRevision !== image.revision
      || eligibleIds.join('\0') !== authoritativeIds.join('\0')
      || eligibleIds.join('\0') !== attemptedIds.join('\0')
      || eligibleIds.join('\0') !== reviewedIds.join('\0')
      || !attemptsComplete
      || !reviewsComplete
      || g6EditingLocked(state, image.id)
      || state.pendingG4Mutations.length > 0
    ) {
      set({
        globalError: context?.error
          || 'G6 尚有未完成的双路 OCR 或可信原文核对，或权威上下文已变化；请完成核对或重载本页。',
      });
      return false;
    }

    set({ g6GateSavingImageId: image.id, globalError: '' });
    try {
      await api.acceptOCRGate(
        image.id,
        eligibleIds.length
          ? 'all-translatable-source-text-reviewed'
          : 'no-translatable-regions',
        ocr.ocrChecksum,
        image.revision,
        lineage,
      );
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭，无法刷新 G6 权威状态。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true))) {
        throw new Error('G6 已接受，但无法读取新的血缘序号；请重载本页。');
      }
      if (!(await get().loadRegions(image.id, true))) {
        throw new Error('G6 已接受，但无法刷新权威文本框；请重载本页。');
      }
      if (!(await get().loadOCRContext(image.id, true))) {
        throw new Error('G6 已接受，但无法刷新权威门禁上下文；请重载本页。');
      }
      set({ g6GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const detail = errorMessage(error);
      const message = conflict
        ? detail
        : `G6 接受提交或核对结果不确定：${detail}。请重载本页核对门禁与血缘。`;
      set((current) => {
        const latest = current.g4Contexts[image.id] ?? context;
        return {
          g6GateSavingImageId: null,
          globalError: message,
          revisionConflict: conflict,
          g4Contexts: {
            ...current.g4Contexts,
            [image.id]: { ...latest, error: message, conflict },
          },
        };
      });
      return false;
    }
  },

  saveG7MaskDraft: async (regions) => {
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const mask = state.maskContexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    const eligibleIds = (state.regionsByImage[image.id] ?? [])
      .filter(maskRegionRequired).map((region) => region.id).sort();
    const recipeIds = regions.map((recipe) => recipe.regionId).sort();
    const validRecipe = regions.every((recipe) =>
      ['region', 'text', 'manual'].includes(recipe.maskMode)
      && ['auto', 'dark', 'light'].includes(recipe.polarity)
      && Number.isInteger(recipe.padding) && recipe.padding >= 0 && recipe.padding <= 512
      && Number.isInteger(recipe.dilation) && recipe.dilation >= 0 && recipe.dilation <= 128
      && Number.isInteger(recipe.feather) && recipe.feather >= 0 && recipe.feather <= 128
      && (recipe.polygon == null || (recipe.polygon.length >= 3 && recipe.polygon.length <= 4096
        && recipe.polygon.every(([x, y]) =>
        Number.isFinite(x) && Number.isFinite(y)
        && x >= 0 && y >= 0 && x <= image.width && y <= image.height
      )))
      && recipe.maskEdits.version === 1 && recipe.maskEdits.strokes.length <= 256
      && recipe.maskEdits.strokes.reduce((total, stroke) => total + stroke.points.length, 0) <= 16384
      && (recipe.maskMode !== 'manual' || recipe.maskEdits.strokes.length > 0)
      && recipe.maskEdits.strokes.every((stroke) =>
        (stroke.mode === 'add' || stroke.mode === 'erase')
        && Number.isFinite(stroke.radius) && stroke.radius > 0 && stroke.radius <= 512
        && stroke.points.length > 0 && stroke.points.length <= 4096
        && stroke.points.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y)
          && x >= 0 && y >= 0 && x <= image.width && y <= image.height)
      )
    );
    if (
      !context || context.status !== 'active' || !context.generation
      || workflowPhase(context) !== 'G7' || !lineage || !mask
      || (mask.state !== 'pending' && mask.state !== 'rejected')
      || mask.generationId !== context.generation.id
      || mask.nextSequence !== context.generation.nextSequence
      || mask.imageRevision !== image.revision
      || eligibleIds.join('\0') !== [...mask.eligibleRegionIds].sort().join('\0')
      || eligibleIds.join('\0') !== recipeIds.join('\0')
      || new Set(recipeIds).size !== recipeIds.length
      || !validRecipe || g7EditingLocked(state, image.id)
    ) {
      set({ globalError: context?.error || 'G7 蒙版配方、页版本或权威 eligible 集合已变化；请重载本页。' });
      return false;
    }
    set({ g7DraftSavingImageId: image.id, globalError: '' });
    try {
      await api.updateMaskDraft(image.id, {
        regions,
        expectedRevision: image.revision,
        lineage,
      });
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true)) || !(await get().loadMaskContext(image.id, true))) {
        throw new Error('G7 配方已提交，但无法重新确认权威血缘。');
      }
      set((current) => ({
        g7DraftSavingImageId: null,
        maskBitmapObservations: Object.fromEntries(
          Object.entries(current.maskBitmapObservations).filter(([id]) => id !== image.id),
        ),
      }));
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error) : `G7 配方提交结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({
        g7DraftSavingImageId: null,
        globalError: message,
        revisionConflict: conflict,
        g4Contexts: { ...current.g4Contexts, [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict } },
      }));
      return false;
    }
  },

  appendG7MaskStroke: async (regionId, stroke) => {
    const state = get();
    const imageId = state.activeImageId;
    const draft = imageId ? state.maskContexts[imageId]?.draft : undefined;
    if (!imageId || !draft) return false;
    const recipe = draft.regions.find((entry) => entry.regionId === regionId);
    if (!recipe || !stroke.points.length) return false;
    return get().saveG7MaskDraft(draft.regions.map((entry) => entry.regionId === regionId ? {
      ...entry,
      maskMode: 'manual',
      maskEdits: { version: 1, strokes: [...entry.maskEdits.strokes, stroke] },
    } : entry));
  },

  startG7Mask: async () => {
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const context = state.g4Contexts[image.id];
    const mask = state.maskContexts[image.id];
    const eligibleIds = (state.regionsByImage[image.id] ?? []).filter(maskRegionRequired)
      .map((region) => region.id).sort();
    const recipeIds = mask?.draft.regions.map((region) => region.regionId).sort() ?? [];
    const lastRejectedSequence = context?.status === 'active'
      ? [...context.events].reverse().find((event) => event.operation === 'mask-stage-review' && event.state === 'rejected')?.sequence
      : undefined;
    const revisedAfterReject = lastRejectedSequence === undefined || Boolean(context?.status === 'active'
      && context.events.some((event) => event.operation === 'mask-draft-updated' && event.sequence > lastRejectedSequence));
    if (
      !context || context.status !== 'active' || !context.generation
      || workflowPhase(context) !== 'G7' || !mask
      || (mask.state !== 'pending' && mask.state !== 'rejected')
      || mask.generationId !== context.generation.id
      || mask.nextSequence !== context.generation.nextSequence
      || mask.imageRevision !== image.revision
      || eligibleIds.length === 0
      || mask.draft.revision < 1
      || !revisedAfterReject
      || eligibleIds.join('\0') !== [...mask.eligibleRegionIds].sort().join('\0')
      || eligibleIds.join('\0') !== recipeIds.join('\0')
      || g7EditingLocked(state, image.id)
    ) {
      set({ globalError: eligibleIds.length ? 'G7 配方或权威上下文不可运行，请重载本页。' : '零 eligible 页面只能提交 G7 不适用。' });
      return false;
    }
    set({ g7GateSavingImageId: image.id, globalError: '' });
    try {
      const job = hydrateJob(await api.startJob(project.id, 'mask', {
        imageIds: [image.id], regionIds: [], options: {},
        lineage: { runId: context.generation.runId, actor: uiLineageActor(), pages: [{
          imageId: image.id, pageGenerationId: context.generation.id,
          expectedSequence: context.generation.nextSequence,
        }] },
      }));
      set((current) => ({ jobs: [job, ...current.jobs.filter((entry) => entry.id !== job.id)] }));
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true)) || !(await get().loadMaskContext(image.id, true))) {
        throw new Error('G7 任务已排队，但无法确认权威血缘。');
      }
      set({ g7GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error) : `G7 任务提交结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({
        g7GateSavingImageId: null, globalError: message, revisionConflict: conflict,
        g4Contexts: { ...current.g4Contexts, [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict } },
      }));
      return false;
    }
  },

  selectG7MaskArtifact: (artifactId) => set((state) => {
    const imageId = state.activeImageId;
    const mask = imageId ? state.maskContexts[imageId] : undefined;
    if (!imageId || !mask?.artifacts.some((artifact) => artifact.artifactId === artifactId)) return {};
    return {
      selectedMaskArtifactIds: { ...state.selectedMaskArtifactIds, [imageId]: artifactId },
      maskBitmapObservations: Object.fromEntries(
        Object.entries(state.maskBitmapObservations).filter(([id]) => id !== imageId),
      ),
    };
  }),

  observeG7MaskBitmap: (observation) => set((state) => {
    const imageId = observation?.imageId ?? state.activeImageId;
    if (!imageId) return {};
    if (!observation) return { maskBitmapObservations: Object.fromEntries(
      Object.entries(state.maskBitmapObservations).filter(([id]) => id !== imageId),
    ) };
    const selected = state.selectedMaskArtifactIds[imageId];
    const image = state.images.find((entry) => entry.id === imageId);
    if (selected !== observation.artifactId || image?.revision !== observation.imageRevision) return {};
    return { maskBitmapObservations: { ...state.maskBitmapObservations, [imageId]: observation } };
  }),

  reviewG7Mask: async (decision, coverageChecks, collateralChecks) => {
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const mask = state.maskContexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    const artifactId = state.selectedMaskArtifactIds[image.id];
    const artifact = mask?.artifacts.find((entry) => entry.artifactId === artifactId);
    const observation = state.maskBitmapObservations[image.id];
    const producedEvent = context?.status === 'active' ? context.events.find((event) =>
      event.operation === 'mask-artifact-produced'
      && event.evidence.artifactId === artifact?.artifactId
      && event.jobItemId === artifact?.jobItemId) : undefined;
    const completedEvent = context?.status === 'active' ? context.events.find((event) =>
      event.operation === 'mask-job-completed'
      && event.jobItemId === artifact?.jobItemId
      && event.outputChecksum === producedEvent?.outputChecksum) : undefined;
    const coverageExact = coverageChecks.length === MASK_COVERAGE_CHECKS.length
      && new Set(coverageChecks.map((entry) => entry.check)).size === MASK_COVERAGE_CHECKS.length
      && MASK_COVERAGE_CHECKS.every((check) => coverageChecks.some((entry) => entry.check === check));
    const collateralExact = collateralChecks.length === MASK_COLLATERAL_CHECKS.length
      && new Set(collateralChecks.map((entry) => entry.check)).size === MASK_COLLATERAL_CHECKS.length
      && MASK_COLLATERAL_CHECKS.every((check) => collateralChecks.some((entry) => entry.check === check));
    const allCoverage = coverageExact && coverageChecks.every((entry) => entry.passed);
    const allCollateral = collateralExact && collateralChecks.every((entry) => entry.passed);
    const noEligible = mask?.eligibleRegionIds.length === 0;
    const rejectedEvent = context?.status === 'active' ? [...context.events].reverse().find((event) =>
      event.operation === 'mask-stage-review' && event.state === 'rejected') : undefined;
    const revisedEvent = rejectedEvent && context?.status === 'active' ? [...context.events].reverse().find((event) =>
      event.operation === 'mask-draft-updated' && event.sequence > rejectedEvent.sequence) : undefined;
    const acceptanceIsFresh = decision !== 'accept' || mask?.state !== 'rejected' || Boolean(
      artifact && producedEvent && revisedEvent
      && artifact.artifactId !== mask.review?.artifactId
      && producedEvent.sequence > revisedEvent.sequence,
    );
    const validDecision = decision === 'not-applicable'
      ? noEligible && coverageChecks.length === 0 && collateralChecks.length === 0 && !artifactId
      : Boolean(!noEligible && artifact && observation
        && producedEvent && completedEvent
        && artifact.recipeChecksum === mask?.draft.stateChecksum
        && artifact.parentChecksum === mask?.g6Checksum
        && artifact.qualityChecksum === mask?.qualityChecksum
        && observation.artifactId === artifact.artifactId
        && observation.checksum === artifact.maskChecksum
        && observation.width === artifact.width && observation.height === artifact.height
        && observation.imageRevision === image.revision
        && acceptanceIsFresh
        && coverageExact && collateralExact
        && (decision === 'accept' ? allCoverage && allCollateral : !allCoverage || !allCollateral));
    if (
      !context || context.status !== 'active' || !context.generation
      || workflowPhase(context) !== 'G7' || !lineage || !mask
      || (mask.state !== 'pending' && mask.state !== 'rejected')
      || mask.generationId !== context.generation.id
      || mask.nextSequence !== context.generation.nextSequence
      || mask.imageRevision !== image.revision
      || !validDecision || g7EditingLocked(state, image.id)
    ) {
      set({ globalError: 'G7 仅接受当前实际 PNG 的精确 checksum/网格与完整 10 项检查；上下文变化时请重载。' });
      return false;
    }
    const reason = decision === 'not-applicable' ? 'no-eligible-regions'
      : decision === 'accept' ? 'complete-and-no-collateral'
        : !allCoverage && !allCollateral ? 'coverage-and-collateral-failed'
          : !allCoverage ? 'coverage-incomplete' : 'collateral-damage';
    set({ g7GateSavingImageId: image.id, globalError: '' });
    try {
      await api.reviewMaskGate(image.id, {
        decision, reason,
        ...(artifact ? { selectedArtifactId: artifact.artifactId, observedMaskChecksum: observation!.checksum } : {}),
        coverageChecks, collateralChecks, expectedRevision: image.revision, lineage,
      });
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true)) || !(await get().loadMaskContext(image.id, true))) {
        throw new Error('G7 复核已提交，但无法确认新血缘。');
      }
      set({ g7GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error) : `G7 复核结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({
        g7GateSavingImageId: null, globalError: message, revisionConflict: conflict,
        g4Contexts: { ...current.g4Contexts, [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict } },
      }));
      return false;
    }
  },

  startG8CleanPlate: async (classicalFallback = false) => {
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const context = state.g4Contexts[image.id];
    const mask = state.maskContexts[image.id];
    const cleanPlate = state.cleanPlateContexts[image.id];
    if (!context || context.status !== 'active' || !context.generation
      || workflowPhase(context) !== 'G8' || !mask || !cleanPlate
      || (mask.state !== 'accepted' && mask.state !== 'not-applicable')
      || cleanPlate.state === 'accepted' || cleanPlate.state === 'not-applicable'
      || cleanPlate.maskArtifactId === null
      || cleanPlate.generationId !== context.generation.id
      || cleanPlate.nextSequence !== context.generation.nextSequence
      || cleanPlate.imageRevision !== image.revision
      || (!classicalFallback && cleanPlate.fallbackEnabled)
      || (classicalFallback && (!cleanPlate.fallbackEnabled || !cleanPlate.fallbackAllowed))
      || g8EditingLocked(state, image.id)) {
      set({ globalError: classicalFallback
        ? 'G8 传统回退仅可在本页全部适用 AI 候选明确拒绝并已授权后运行。'
        : cleanPlate?.fallbackEnabled
          ? '本页传统回退已开启；请明确关闭后再恢复 AI 候选。'
          : 'G8 权威上下文、G7 接受证据或页版本已变化；请重载本页。' });
      return false;
    }
    set({ g8GateSavingImageId: image.id, globalError: '' });
    try {
      const job = hydrateJob(await api.startJob(project.id, 'inpaint', {
        imageIds: [image.id],
        regionIds: [],
        options: { classicalFallback },
        lineage: {
          runId: context.generation.runId,
          actor: uiLineageActor(),
          pages: [{
            imageId: image.id,
            pageGenerationId: context.generation.id,
            expectedSequence: context.generation.nextSequence,
          }],
        },
      }));
      set((current) => ({ jobs: [job, ...current.jobs.filter((entry) => entry.id !== job.id)] }));
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true))
        || !(await get().loadMaskContext(image.id, true))
        || !(await get().loadCleanPlateContext(image.id, true))) {
        throw new Error('G8 任务已排队，但无法重新确认权威血缘。');
      }
      set({ g8GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error)
        : `G8 任务提交结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({
        g8GateSavingImageId: null,
        globalError: message,
        revisionConflict: conflict,
        g4Contexts: {
          ...current.g4Contexts,
          [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict },
        },
      }));
      return false;
    }
  },

  selectG8CleanPlateCandidate: (candidateId) => set((state) => {
    const imageId = state.activeImageId;
    const cleanPlate = imageId ? state.cleanPlateContexts[imageId] : undefined;
    if (!imageId || !cleanPlate?.candidates.some((candidate) =>
      candidate.candidateId === candidateId)) return {};
    return {
      selectedCleanPlateCandidateIds: {
        ...state.selectedCleanPlateCandidateIds,
        [imageId]: candidateId,
      },
      cleanPlateBitmapObservations: Object.fromEntries(
        Object.entries(state.cleanPlateBitmapObservations).filter(([id]) => id !== imageId),
      ),
    };
  }),

  observeG8CleanPlateBitmap: (observation) => set((state) => {
    const imageId = observation?.imageId ?? state.activeImageId;
    if (!imageId) return {};
    if (!observation) return {
      cleanPlateBitmapObservations: Object.fromEntries(
        Object.entries(state.cleanPlateBitmapObservations).filter(([id]) => id !== imageId),
      ),
    };
    const selected = state.selectedCleanPlateCandidateIds[imageId];
    const image = state.images.find((entry) => entry.id === imageId);
    const lineage = state.g4Contexts[imageId];
    const cleanPlate = state.cleanPlateContexts[imageId];
    const candidate = cleanPlate?.candidates.find((entry) =>
      entry.candidateId === observation.candidateId);
    const mask = state.maskContexts[imageId]?.artifacts.find((artifact) =>
      artifact.artifactId === cleanPlate?.maskArtifactId
      && artifact.maskChecksum === cleanPlate.maskChecksum);
    if (observation.state !== 'ready'
      || selected !== observation.candidateId
      || image?.revision !== observation.imageRevision
      || lineage?.status !== 'active'
      || !lineage.generation
      || observation.generationId !== lineage.generation.id
      || observation.nextSequence !== lineage.generation.nextSequence
      || observation.sourceChecksum !== lineage.generation.sourceChecksum
      || !cleanPlate
      || observation.cleanPlateStateChecksum !== cleanPlate.cleanPlateStateChecksum
      || observation.qualityChecksum !== cleanPlate.qualityChecksum
      || observation.maskArtifactId !== cleanPlate.maskArtifactId
      || observation.maskChecksum !== cleanPlate.maskChecksum
      || !mask
      || observation.maskWidth !== mask.width
      || observation.maskHeight !== mask.height
      || !candidate
      || observation.checksum !== candidate.candidateChecksum
      || observation.width !== candidate.width
      || observation.height !== candidate.height) return {};
    return {
      cleanPlateBitmapObservations: {
        ...state.cleanPlateBitmapObservations,
        [imageId]: observation,
      },
    };
  }),

  reviewG8CleanPlate: async (decision, checks) => {
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const cleanPlate = state.cleanPlateContexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    const candidateId = state.selectedCleanPlateCandidateIds[image.id];
    const candidate = cleanPlate?.candidates.find((entry) => entry.candidateId === candidateId);
    const observation = state.cleanPlateBitmapObservations[image.id];
    const produced = context?.status === 'active' ? context.events.find((event) =>
      event.gate === 'G8_cleanPlate' && event.operation === 'clean-plate-candidate-produced'
      && event.evidence.candidateId === candidate?.candidateId
      && event.jobItemId === candidate?.jobItemId) : undefined;
    const completed = context?.status === 'active' ? context.events.find((event) =>
      event.gate === 'G8_cleanPlate' && event.operation === 'inpaint-job-completed'
      && event.jobItemId === candidate?.jobItemId
      && event.outputChecksum === produced?.outputChecksum) : undefined;
    const exactChecks = checks.length === CLEAN_PLATE_CHECKS.length
      && new Set(checks.map((entry) => entry.check)).size === CLEAN_PLATE_CHECKS.length
      && CLEAN_PLATE_CHECKS.every((check) => checks.some((entry) => entry.check === check));
    const allPassed = exactChecks && checks.every((entry) => entry.passed);
    const notApplicable = decision === 'not-applicable'
      && cleanPlate?.maskArtifactId === null && cleanPlate.candidates.length === 0
      && checks.length === 0 && !candidateId;
    const candidateDecision = decision !== 'not-applicable' && Boolean(
      candidate && candidate.review === null && candidate.completed
      && observation && produced && completed
      && observation.state === 'ready'
      && observation.generationId === context?.generation?.id
      && observation.nextSequence === context?.generation?.nextSequence
      && observation.cleanPlateStateChecksum === cleanPlate?.cleanPlateStateChecksum
      && observation.candidateId === candidate.candidateId
      && observation.imageRevision === image.revision
      && observation.sourceChecksum === context?.generation?.sourceChecksum
      && observation.qualityChecksum === cleanPlate?.qualityChecksum
      && observation.maskArtifactId === cleanPlate?.maskArtifactId
      && observation.maskChecksum === cleanPlate?.maskChecksum
      && observation.maskWidth === candidate.width
      && observation.maskHeight === candidate.height
      && observation.checksum === candidate.candidateChecksum
      && observation.width === candidate.width && observation.height === candidate.height
      && candidate.parentChecksum === cleanPlate?.g7Checksum
      && candidate.qualityChecksum === cleanPlate?.qualityChecksum
      && candidate.backgroundChecksum === cleanPlate?.backgroundChecksum
      && candidate.maskArtifactId === cleanPlate?.maskArtifactId
      && candidate.maskChecksum === cleanPlate?.maskChecksum
      && candidate.outsideMaskChangeCount === 0
      && exactChecks
      && checks.every((entry) => typeof entry.passed === 'boolean')
      && (decision === 'accept' ? allPassed : !allPassed)
    );
    if (!context || context.status !== 'active' || !context.generation || !lineage || !cleanPlate
      || workflowPhase(context) !== 'G8'
      || cleanPlate.generationId !== context.generation.id
      || cleanPlate.nextSequence !== context.generation.nextSequence
      || cleanPlate.imageRevision !== image.revision
      || cleanPlate.state === 'accepted' || cleanPlate.state === 'not-applicable'
      || (!notApplicable && !candidateDecision)
      || g8EditingLocked(state, image.id)) {
      set({ globalError: 'G8 仅接受当前实际 PNG 的精确 checksum/网格、完整 7 项检查和未复核候选。' });
      return false;
    }
    let reason: CleanPlateReviewReason = 'no-clean-plate-required';
    if (!notApplicable) {
      const failed = checks.filter((entry) => !entry.passed).map((entry) => entry.check);
      const reasons: Record<CleanPlateCheck, CleanPlateReviewReason> = {
        'outside-mask-unchanged': 'outside-mask-changed',
        'source-text-unreadable': 'residual-text-readable',
        'no-white-or-gray-hole': 'hole-or-block',
        'no-blur-band': 'blur-band',
        'no-repeated-texture': 'repeated-texture',
        'background-continuous': 'background-discontinuous',
        'structure-preserved': 'structure-damaged',
      };
      reason = decision === 'accept' ? 'clean-plate-complete'
        : failed.length > 1 ? 'multiple-visual-failures' : reasons[failed[0]!];
    }
    set({ g8GateSavingImageId: image.id, globalError: '' });
    try {
      await api.reviewCleanPlateGate(image.id, {
        decision,
        reason,
        ...(candidate ? {
          candidateId: candidate.candidateId,
          observedCandidateChecksum: observation!.checksum,
          observedWidth: observation!.width,
          observedHeight: observation!.height,
        } : {}),
        checks,
        expectedRevision: image.revision,
        lineage,
      });
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true))
        || !(await get().loadMaskContext(image.id, true))
        || !(await get().loadCleanPlateContext(image.id, true))) {
        throw new Error('G8 复核已提交，但无法确认新血缘。');
      }
      set({ g8GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error)
        : `G8 复核结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({
        g8GateSavingImageId: null,
        globalError: message,
        revisionConflict: conflict,
        g4Contexts: {
          ...current.g4Contexts,
          [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict },
        },
      }));
      return false;
    }
  },

  setG8ClassicalFallback: async (enabled) => {
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    const context = state.g4Contexts[image.id];
    const cleanPlate = state.cleanPlateContexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    if (!context || context.status !== 'active' || !context.generation || !lineage || !cleanPlate
      || workflowPhase(context) !== 'G8'
      || cleanPlate.generationId !== context.generation.id
      || cleanPlate.nextSequence !== context.generation.nextSequence
      || cleanPlate.imageRevision !== image.revision
      || cleanPlate.state === 'accepted' || cleanPlate.state === 'not-applicable'
      || cleanPlate.fallbackEnabled === enabled
      || (enabled && !cleanPlate.fallbackAllowed)
      || g8EditingLocked(state, image.id)) {
      set({ globalError: enabled
        ? '必须先逐一拒绝本页同代次全部适用 AI 候选，才能开启传统回退。'
        : 'G8 传统回退状态或页版本已变化；请重载本页。' });
      return false;
    }
    set({ g8GateSavingImageId: image.id, globalError: '' });
    try {
      await api.setCleanPlateFallback(image.id, {
        enabled,
        reason: enabled ? 'all-ai-candidates-rejected' : 'resume-ai-candidates',
        expectedRevision: image.revision,
        lineage,
      });
      const projectId = get().currentProject?.id;
      if (!projectId) throw new Error('当前项目已关闭。');
      await synchronizeImages(projectId);
      if (!(await get().loadG4Context(image.id, true))
        || !(await get().loadMaskContext(image.id, true))
        || !(await get().loadCleanPlateContext(image.id, true))) {
        throw new Error('G8 回退授权已提交，但无法确认新血缘。');
      }
      set({ g8GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error)
        : `G8 回退授权结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({
        g8GateSavingImageId: null,
        globalError: message,
        revisionConflict: conflict,
        g4Contexts: {
          ...current.g4Contexts,
          [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict },
        },
      }));
      return false;
    }
  },

  startG9Translation: async (remoteAuthorized = false) => {
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const context = state.g4Contexts[image.id];
    const translation = state.translationContexts[image.id];
    if (!context || context.status !== 'active' || !context.generation
      || workflowPhase(context) !== 'G9' || !translation || translation.state !== 'pending'
      || translation.eligibleRegions.length === 0
      || translation.generationId !== context.generation.id
      || translation.nextSequence !== context.generation.nextSequence
      || translation.imageRevision !== image.revision
      || state.g9GateSavingImageId !== null) {
      set({ globalError: 'G9 权威上下文、eligible 集合或页版本已变化；请重载本页。' });
      return false;
    }
    set({ g9GateSavingImageId: image.id, globalError: '' });
    try {
      const job = hydrateJob(await api.startJob(project.id, 'translate', {
        imageIds: [image.id],
        regionIds: [],
        options: { remoteAuthorized },
        lineage: {
          runId: context.generation.runId,
          actor: uiLineageActor(),
          pages: [{ imageId: image.id, pageGenerationId: context.generation.id,
            expectedSequence: context.generation.nextSequence }],
        },
      }));
      set((current) => ({ jobs: [job, ...current.jobs.filter((entry) => entry.id !== job.id)] }));
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true))
        || !(await get().loadTranslationContext(image.id, true))) {
        throw new Error('G9 翻译任务已排队，但无法重新确认权威血缘。');
      }
      set({ g9GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error)
        : `G9 翻译任务提交结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({
        g9GateSavingImageId: null, globalError: message, revisionConflict: conflict,
        g4Contexts: { ...current.g4Contexts, [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict } },
      }));
      return false;
    }
  },

  selectG9TranslationCandidate: (candidateId) => set((state) => {
    const imageId = state.activeImageId;
    if (!imageId || !state.translationContexts[imageId]?.candidates.some((candidate) =>
      candidate.candidateId === candidateId)) return {};
    return { selectedTranslationCandidateIds: { ...state.selectedTranslationCandidateIds, [imageId]: candidateId } };
  }),

  reviseG9Translation: async (regionId, translationText, originKind) => {
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const context = state.g4Contexts[image.id];
    const translation = state.translationContexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    const eligible = translation?.eligibleRegions.find((region) => region.regionId === regionId);
    const latest = translation?.candidates.filter((candidate) => candidate.regionId === regionId).at(-1);
    if (!context || context.status !== 'active' || !context.generation || !lineage
      || workflowPhase(context) !== 'G9' || !translation || translation.state !== 'pending'
      || !eligible || !translationText.trim()
      || (latest?.review?.state === 'accepted')
      || (latest && latest.review?.state !== 'rejected')
      || translation.generationId !== context.generation.id
      || translation.nextSequence !== context.generation.nextSequence
      || translation.imageRevision !== image.revision || state.g9GateSavingImageId !== null) {
      set({ globalError: 'G9 修订只能基于当前 region 的最新已拒绝候选；接受候选不可变。' });
      return false;
    }
    set({ g9GateSavingImageId: image.id, globalError: '' });
    try {
      await api.createTranslationCandidate(image.id, {
        regionId, translationText: translationText.trim(), originKind,
        observedG8Checksum: translation.g8Checksum,
        observedSourceTextChecksum: eligible.sourceTextChecksum,
        observedContextChecksum: eligible.contextChecksum,
        observedTranslationStateChecksum: translation.translationStateChecksum,
        expectedRevision: image.revision, lineage,
      });
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true)) || !(await get().loadTranslationContext(image.id, true))) {
        throw new Error('G9 修订已提交，但无法重新确认权威血缘。');
      }
      set({ g9GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error) : `G9 修订结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({ g9GateSavingImageId: null, globalError: message, revisionConflict: conflict,
        g4Contexts: { ...current.g4Contexts, [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict } } }));
      return false;
    }
  },

  reviewG9TranslationCandidate: async (candidateId, decision, checks, qcFlags, reason) => {
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const context = state.g4Contexts[image.id];
    const translation = state.translationContexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    const candidate = translation?.candidates.find((entry) => entry.candidateId === candidateId);
    const eligible = translation?.eligibleRegions.find((entry) => entry.regionId === candidate?.regionId);
    const latest = candidate && translation?.candidates.filter((entry) => entry.regionId === candidate.regionId).at(-1);
    const exactChecks = checks.length === TRANSLATION_QC_CHECKS.length
      && new Set(checks.map((entry) => entry.check)).size === TRANSLATION_QC_CHECKS.length
      && TRANSLATION_QC_CHECKS.every((check) => checks.some((entry) => entry.check === check));
    const exactFlags = qcFlags.length > 0 && new Set(qcFlags).size === qcFlags.length
      && (!qcFlags.includes('none') || qcFlags.length === 1);
    const verdictValid = decision === 'accept'
      ? reason === 'translation-reviewed' && checks.every((entry) => entry.passed)
        && qcFlags.length === 1 && qcFlags[0] === 'none'
      : reason !== 'translation-reviewed'
        && (checks.some((entry) => !entry.passed) || qcFlags.some((flag) => flag !== 'none'));
    if (!context || context.status !== 'active' || !context.generation || !lineage
      || workflowPhase(context) !== 'G9' || !translation || translation.state !== 'pending'
      || !candidate || candidate !== latest || candidate.review !== null || !eligible
      || !exactChecks || !exactFlags || !verdictValid
      || translation.generationId !== context.generation.id
      || translation.nextSequence !== context.generation.nextSequence
      || translation.imageRevision !== image.revision || state.g9GateSavingImageId !== null) {
      set({ globalError: 'G9 只能复核当前 region 最新未处置候选，且必须提交精确 10 项 QC。' });
      return false;
    }
    set({ g9GateSavingImageId: image.id, globalError: '' });
    try {
      await api.reviewTranslationCandidate(image.id, candidate.candidateId, {
        decision, reason, observedCandidateChecksum: candidate.candidateChecksum,
        observedSourceTextChecksum: eligible.sourceTextChecksum, observedContextChecksum: eligible.contextChecksum,
        observedG8Checksum: translation.g8Checksum, checks, qcFlags,
        expectedRevision: image.revision, lineage,
      });
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true)) || !(await get().loadTranslationContext(image.id, true))) {
        throw new Error('G9 候选复核已提交，但无法重新确认权威血缘。');
      }
      set({ g9GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error) : `G9 候选复核结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({ g9GateSavingImageId: null, globalError: message, revisionConflict: conflict,
        g4Contexts: { ...current.g4Contexts, [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict } } }));
      return false;
    }
  },

  acceptG9Translation: async () => {
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const context = state.g4Contexts[image.id];
    const translation = state.translationContexts[image.id];
    const lineage = context ? mutationLineage(context) : null;
    const latestByRegion = new Map<string, TranslationCandidate>();
    for (const candidate of translation?.candidates ?? []) latestByRegion.set(candidate.regionId, candidate);
    const noEligible = translation?.eligibleRegions.length === 0;
    const ready = noEligible
      ? translation?.candidates.length === 0
      : Boolean(translation?.eligibleRegions.every((region) =>
        latestByRegion.get(region.regionId)?.review?.state === 'accepted')
        && translation.candidates.every((candidate) => candidate.review !== null));
    if (!context || context.status !== 'active' || !context.generation || !lineage
      || workflowPhase(context) !== 'G9' || !translation || translation.state !== 'pending' || !ready
      || translation.generationId !== context.generation.id
      || translation.nextSequence !== context.generation.nextSequence
      || translation.imageRevision !== image.revision || state.g9GateSavingImageId !== null) {
      set({ globalError: 'G9 终结前必须处置完整候选前缀，并让每个 eligible region 的最新候选唯一接受。' });
      return false;
    }
    set({ g9GateSavingImageId: image.id, globalError: '' });
    try {
      await api.reviewTranslationGate(image.id, {
        decision: noEligible ? 'not-applicable' : 'accept',
        observedTranslationStateChecksum: translation.translationStateChecksum,
        expectedRevision: image.revision, lineage,
      });
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true)) || !(await get().loadTranslationContext(image.id, true))) {
        throw new Error('G9 终结已提交，但无法重新确认权威血缘。');
      }
      set({ g9GateSavingImageId: null });
      return true;
    } catch (error) {
      const conflict = error instanceof ApiError && error.status === 409;
      const message = conflict ? errorMessage(error) : `G9 终结结果不确定：${errorMessage(error)}。请重载本页。`;
      set((current) => ({ g9GateSavingImageId: null, globalError: message, revisionConflict: conflict,
        g4Contexts: { ...current.g4Contexts, [image.id]: { ...(current.g4Contexts[image.id] ?? context), error: message, conflict } } }));
      return false;
    }
  },

  setG10RegionStyle: (regionId, style) => set((state) => {
    const imageId = state.activeImageId;
    const context = imageId ? state.typesetContexts[imageId] : undefined;
    if (!imageId || !context?.routeManifest.some((entry) =>
      entry.regionId === regionId && entry.renderRequired) || context.state !== 'pending') return {};
    return { typesetStyleDrafts: {
      ...state.typesetStyleDrafts,
      [imageId]: { ...(state.typesetStyleDrafts[imageId] ?? {}), [regionId]: style },
    } };
  }),

  startG10Typeset: async (providedRegionStyles) => {
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const lineageContext = state.g4Contexts[image.id];
    const typeset = state.typesetContexts[image.id];
    const regionStyles = providedRegionStyles ?? state.typesetStyleDrafts[image.id] ?? {};
    const renderRouteIds = new Set(typeset?.routeManifest
      .filter((entry) => entry.renderRequired)
      .map((entry) => entry.regionId));
    const styleIds = Object.keys(regionStyles);
    const styleKeys = [
      'fontToken', 'fontSize', 'minFontSize', 'padding', 'fill', 'strokeColor',
      'strokeWidth', 'rotation', 'scaleX', 'scaleY', 'shearX', 'shearY', 'opacity',
      'visualCenterX', 'visualCenterY', 'align', 'lineSpacing', 'letterSpacing', 'autoFit',
    ].sort().join('\0');
    const stylesValid = Boolean(typeset && styleIds.length === renderRouteIds.size
      && styleIds.every((regionId) => renderRouteIds.has(regionId))
      && Object.entries(regionStyles).every(([regionId, style]) => {
        const route = typeset.routeManifest.find((entry) => entry.regionId === regionId)?.route;
        const advertised = typeset.availableFonts.find((font) => font.token === style.fontToken);
        return Object.keys(style).sort().join('\0') === styleKeys
          && Boolean(advertised && (route !== 'art-lettering' || advertised.role === 'display'))
          && /^#[0-9A-F]{6}$/.test(style.fill) && /^#[0-9A-F]{6}$/.test(style.strokeColor)
          && Number.isInteger(style.fontSize) && style.fontSize >= 6 && style.fontSize <= 512
          && Number.isInteger(style.minFontSize) && style.minFontSize >= 6
          && style.minFontSize <= style.fontSize
          && Number.isInteger(style.padding) && style.padding >= 0 && style.padding <= 128
          && Number.isInteger(style.strokeWidth) && style.strokeWidth >= 0 && style.strokeWidth <= 32
          && Number.isFinite(style.rotation) && style.rotation >= -180 && style.rotation <= 180
          && Number.isFinite(style.scaleX) && style.scaleX >= 0.25 && style.scaleX <= 4
          && Number.isFinite(style.scaleY) && style.scaleY >= 0.25 && style.scaleY <= 4
          && Number.isFinite(style.shearX) && style.shearX >= -1 && style.shearX <= 1
          && Number.isFinite(style.shearY) && style.shearY >= -1 && style.shearY <= 1
          && Number.isFinite(style.opacity) && style.opacity >= 0.05 && style.opacity <= 1
          && Number.isFinite(style.visualCenterX) && style.visualCenterX >= 0 && style.visualCenterX <= 1
          && Number.isFinite(style.visualCenterY) && style.visualCenterY >= 0 && style.visualCenterY <= 1
          && Number.isFinite(style.lineSpacing) && style.lineSpacing >= 0 && style.lineSpacing <= 3
          && Number.isFinite(style.letterSpacing) && style.letterSpacing >= -10 && style.letterSpacing <= 50
          && ['start', 'center', 'end'].includes(style.align)
          && typeof style.autoFit === 'boolean'
          && (route !== 'art-lettering' || style.letterSpacing === 0)
          && (!['bubble', 'ordinary'].includes(route ?? '') || (
            style.scaleX === 1 && style.scaleY === 1 && style.shearX === 0 && style.shearY === 0
            && style.visualCenterX === 0.5 && style.visualCenterY === 0.5
          ));
      }));
    const activeJob = state.jobs.some((job) => job.kind === 'typeset'
      && (job.status === 'queued' || job.status === 'running')
      && job.items.some((item) => item.imageId === image.id
        && (item.status === 'queued' || item.status === 'running')));
    if (!lineageContext || lineageContext.status !== 'active' || !lineageContext.generation
      || workflowPhase(lineageContext) !== 'G10' || !typeset || typeset.state !== 'pending'
      || typeset.generationId !== lineageContext.generation.id
      || typeset.nextSequence !== lineageContext.generation.nextSequence
      || typeset.imageRevision !== image.revision || !stylesValid || activeJob
      || typeset.candidates.length !== typeset.reviews.length
      || (typeset.routeManifest.some((entry) => entry.route === 'art-lettering')
        && !typeset.artLetteringCapability.available)
      || state.g10GateSavingImageId !== null) {
      set({ globalError: 'G10 权威上下文、route/style 集合或页版本已变化；未创建嵌字任务。' });
      return false;
    }
    set({ g10GateSavingImageId: image.id, globalError: '' });
    try {
      const job = hydrateJob(await api.startJob(project.id, 'typeset', {
        imageIds: [image.id],
        regionIds: [],
        options: { regionStyles },
        lineage: {
          runId: lineageContext.generation.runId,
          actor: uiLineageActor(),
          pages: [{ imageId: image.id, pageGenerationId: lineageContext.generation.id,
            expectedSequence: lineageContext.generation.nextSequence }],
        },
      }));
      set((current) => ({ jobs: [job, ...current.jobs.filter((entry) => entry.id !== job.id)] }));
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true))
        || !(await get().loadTypesetContext(image.id, true))) {
        throw new Error('G10 嵌字任务已排队，但无法重新确认权威血缘。');
      }
      set({ g10GateSavingImageId: null });
      return true;
    } catch (error) {
      const message = `G10 嵌字任务结果不确定：${errorMessage(error)}。已强制重载本页。`;
      set({ g10GateSavingImageId: null });
      await get().reloadActiveImage();
      set({ globalError: message, revisionConflict: error instanceof ApiError && error.status === 409 });
      return false;
    }
  },

  selectG10TypesetCandidate: (candidateId) => set((state) => {
    const imageId = state.activeImageId;
    if (!imageId || !state.typesetContexts[imageId]?.candidates.some((candidate) =>
      candidate.candidateId === candidateId)) return {};
    const observations = { ...state.typesetBitmapObservations };
    delete observations[imageId];
    return {
      selectedTypesetCandidateIds: { ...state.selectedTypesetCandidateIds, [imageId]: candidateId },
      typesetBitmapObservations: observations,
    };
  }),

  observeG10TypesetBitmap: (observation) => set((state) => {
    const imageId = state.activeImageId;
    if (!imageId) return {};
    const observations = { ...state.typesetBitmapObservations };
    if (!observation) {
      delete observations[imageId];
      return { typesetBitmapObservations: observations };
    }
    const context = state.typesetContexts[imageId];
    const lineage = state.g4Contexts[imageId];
    const image = state.images.find((entry) => entry.id === imageId);
    const candidate = context?.candidates.find((entry) => entry.candidateId === observation.candidateId);
    if (!context || !lineage?.generation || !image || !candidate
      || observation.imageId !== imageId || observation.generationId !== context.generationId
      || observation.nextSequence !== context.nextSequence
      || observation.imageRevision !== image.revision
      || observation.sourceChecksum !== lineage.generation.sourceChecksum
      || observation.cleanPlateChecksum !== context.cleanPlateChecksum
      || observation.candidateChecksum !== candidate.candidateChecksum
      || observation.routeChecksum !== candidate.routeChecksum
      || observation.styleChecksum !== candidate.styleChecksum
      || observation.layoutChecksum !== candidate.layoutChecksum
      || observation.width !== candidate.width || observation.height !== candidate.height
      || observation.renderScale !== candidate.renderScale) {
      delete observations[imageId];
      return { typesetBitmapObservations: observations };
    }
    return { typesetBitmapObservations: { ...observations, [imageId]: observation } };
  }),

  reviewG10TypesetCandidate: async (candidateId, decision, checks, reason, touchedChecks) => {
    const state = get();
    const project = state.currentProject;
    const image = activeImage(state);
    if (!project || !image) return false;
    const lineageContext = state.g4Contexts[image.id];
    const lineage = lineageContext ? mutationLineage(lineageContext) : null;
    const typeset = state.typesetContexts[image.id];
    const candidate = typeset?.candidates.find((entry) => entry.candidateId === candidateId);
    const review = typeset?.reviews.find((entry) => entry.candidateId === candidateId);
    const latest = typeset?.candidates.at(-1);
    const observation = state.typesetBitmapObservations[image.id];
    const exactChecks = checks.length === TYPESET_CHECKS.length
      && TYPESET_CHECKS.every((check, index) => {
        const entry = checks[index];
        return Boolean(entry && entry.check === check && typeof entry.passed === 'boolean'
          && Object.keys(entry).sort().join('\0') === ['check', 'passed'].sort().join('\0'));
      });
    const allChecksTouched = Array.isArray(touchedChecks)
      && touchedChecks.length === TYPESET_CHECKS.length
      && new Set(touchedChecks).size === TYPESET_CHECKS.length
      && touchedChecks.every((check) => TYPESET_CHECKS.includes(check));
    const failed = checks.filter((entry) => !entry.passed).map((entry) => entry.check);
    const knownDefects = Boolean(candidate
      && (candidate.overflowRegionIds.length || candidate.anomalies.length));
    const overflowFailed = checks.find((entry) => entry.check === 'overflow-free')
      ?.passed === false;
    const verdictValid = decision === 'accept'
      ? reason === 'typeset-reviewed' && failed.length === 0
        && candidate?.overflowRegionIds.length === 0 && candidate.anomalies.length === 0
      : failed.length > 0 && (!knownDefects || overflowFailed)
        && (reason === 'multiple-visual-failures'
        ? failed.length > 1 : failed.includes(reason as TypesetCheck));
    const observationValid = Boolean(observation && candidate
      && observation.state === 'ready'
      && observation.imageId === image.id
      && observation.generationId === typeset?.generationId
      && observation.nextSequence === typeset.nextSequence
      && observation.imageRevision === image.revision
      && observation.sourceChecksum === lineageContext?.generation?.sourceChecksum
      && observation.candidateId === candidate.candidateId
      && observation.candidateChecksum === candidate.candidateChecksum
      && observation.routeChecksum === candidate.routeChecksum
      && observation.styleChecksum === candidate.styleChecksum
      && observation.layoutChecksum === candidate.layoutChecksum
      && observation.cleanPlateChecksum === candidate.cleanPlateChecksum
      && observation.width === candidate.width && observation.height === candidate.height
      && observation.renderScale === candidate.renderScale);
    if (!lineageContext || lineageContext.status !== 'active' || !lineageContext.generation
      || !lineage || workflowPhase(lineageContext) !== 'G10' || !typeset
      || typeset.state !== 'pending' || !candidate || !candidate.completed
      || candidate !== latest || review
      || !exactChecks || !allChecksTouched || !verdictValid || !observationValid
      || typeset.generationId !== lineageContext.generation.id
      || typeset.nextSequence !== lineageContext.generation.nextSequence
      || typeset.imageRevision !== image.revision || state.g10GateSavingImageId !== null) {
      set({ globalError: 'G10 只能复核最新未处置候选；三视图、精确 8 项检查及服务端 raster 事实必须一致。' });
      return false;
    }
    set({ g10GateSavingImageId: image.id, globalError: '' });
    try {
      await api.reviewTypesetCandidate(image.id, candidate.candidateId, {
        decision, reason, observedCandidateChecksum: candidate.candidateChecksum,
        observedRouteChecksum: candidate.routeChecksum,
        observedStyleChecksum: candidate.styleChecksum,
        observedLayoutChecksum: candidate.layoutChecksum,
        observedTranslationTerminalChecksum: candidate.g9TerminalChecksum,
        observedCleanPlateChecksum: candidate.cleanPlateChecksum,
        observedWidth: candidate.width, observedHeight: candidate.height,
        observedRenderScale: candidate.renderScale, checks,
        expectedRevision: image.revision, lineage,
      });
      await synchronizeImages(project.id);
      if (!(await get().loadG4Context(image.id, true))
        || !(await get().loadTypesetContext(image.id, true))) {
        throw new Error('G10 候选复核已提交，但无法重新确认权威血缘。');
      }
      set({ g10GateSavingImageId: null });
      return true;
    } catch (error) {
      const message = `G10 候选复核结果不确定：${errorMessage(error)}。已强制重载本页。`;
      set({ g10GateSavingImageId: null });
      await get().reloadActiveImage();
      set({ globalError: message, revisionConflict: error instanceof ApiError && error.status === 409 });
      return false;
    }
  },

  undo: () => {
    const state = get();
    if (state.activeImageId && state.g4Contexts[state.activeImageId]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能使用旧版本地撤销。' });
      return;
    }
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
    if (state.activeImageId && state.g4Contexts[state.activeImageId]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能使用旧版本地重做。' });
      return;
    }
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
    const state = get();
    if (!projectPagesAreLegacy(state)) {
      set({ globalError: '项目内页面血缘尚未全部确认为旧版，项目参数保持冻结。' });
      return;
    }
    const project = state.currentProject;
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
    if (state.g4Contexts[image.id]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能使用旧版页面复核。' });
      return false;
    }
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
    if (state.g4Contexts[image.id]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能修改后续视觉阶段复核。' });
      return false;
    }
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
    if (state.g4Contexts[image.id]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能切换修复候选。' });
      return false;
    }
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

  reviewSelectedInpaintAiCandidate: async (reviewState) => {
    if (!(await get().flushAutosave())) return false;
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    if (state.g4Contexts[image.id]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能复核修复候选。' });
      return false;
    }
    const mutationKey = `${image.id}:inpaint-ai-candidate-review`;
    if (state.stageReviewSaving !== null) return false;
    set({ globalError: '', revisionConflict: false, stageReviewSaving: mutationKey });
    try {
      const response = await api.reviewSelectedInpaintAiCandidate(
        image.id,
        reviewState,
        image.revision,
      );
      const merged = hydrateImage({
        ...image,
        ...response,
        status: { ...image.status, ...(response.status ?? {}) },
        stageReviews: response.stageReviews ?? image.stageReviews,
        inpaintCandidate: response.inpaintCandidate,
        inpaintCandidates: response.inpaintCandidates ?? [],
        inpaintCandidateGenerationId: response.inpaintCandidateGenerationId,
        inpaintAiRejectedCandidateIds: response.inpaintAiRejectedCandidateIds ?? [],
        inpaintFallback: response.inpaintFallback ?? { state: 'pending' },
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

  setActiveImageInpaintFallback: async (fallbackState, options = {}) => {
    if (!(await get().flushAutosave())) return false;
    const state = get();
    const image = activeImage(state);
    if (!image) return false;
    if (state.g4Contexts[image.id]?.status !== 'legacy') {
      set({ globalError: '本页血缘尚未确认为旧版页面，不能修改传统算法兜底。' });
      return false;
    }
    const mutationKey = `${image.id}:inpaint-classical-fallback`;
    if (state.stageReviewSaving !== null) return false;
    set({ globalError: '', revisionConflict: false, stageReviewSaving: mutationKey });
    try {
      const response = await api.setInpaintClassicalFallback(
        image.id,
        fallbackState,
        image.revision,
        options,
      );
      const merged = hydrateImage({
        ...image,
        ...response,
        status: { ...image.status, ...(response.status ?? {}) },
        stageReviews: response.stageReviews ?? image.stageReviews,
        inpaintCandidate: response.inpaintCandidate ?? image.inpaintCandidate,
        inpaintCandidates: response.inpaintCandidates ?? image.inpaintCandidates,
        inpaintCandidateGenerationId: 'inpaintCandidateGenerationId' in response
          ? response.inpaintCandidateGenerationId
          : image.inpaintCandidateGenerationId,
        inpaintAiRejectedCandidateIds: response.inpaintAiRejectedCandidateIds ?? [],
        inpaintFallback: response.inpaintFallback ?? { state: 'pending' },
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

  setCanvasMode: (canvasMode) => set((state) => ({
    canvasMode,
    showMask: canvasMode === 'erased' ? true : state.showMask,
  })),
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
  requestFit: () => set((state) => ({
    fitRequest: state.fitRequest + 1,
    focusRegionIds: [],
  })),
  focusRegions: (regionIds) => {
    const ids = [...new Set(regionIds.filter((regionId) => regionId.length > 0))];
    if (!ids.length) return;
    set((state) => ({
      focusRegionIds: ids,
      focusRequest: state.focusRequest + 1,
    }));
  },
  focusSelectedRegions: () => {
    get().focusRegions(get().selectedRegionIds);
  },
  focusActiveOverflow: () => {
    const state = get();
    const image = activeImage(state);
    const overflowIds = overflowingRegionIds(
      image,
      image ? state.regionsByImage[image.id] ?? [] : [],
    );
    if (!overflowIds.length) return;
    set({
      selectedRegionIds: overflowIds,
      rightTab: 'typesetting',
      canvasMode: image?.status.typeset === 'done' ? 'typeset' : state.canvasMode,
      focusRegionIds: overflowIds,
      focusRequest: state.focusRequest + 1,
    });
  },
  focusActiveFailure: () => {
    const failure = latestPageProcessingError(activeImage(get()));
    if (!failure) return;
    set({
      rightTab: failure.kind ? inspectorTabForJobKind(failure.kind) : 'text',
    });
  },
  setRightTab: (rightTab) => set({ rightTab }),
  setTheme: (theme) => {
    try {
      window.localStorage?.setItem('manga-localizer-theme', theme);
    } catch {
      // Theme persistence is optional in privacy-restricted browser contexts.
    }
    set({ theme });
  },
  setDrawerOpen: (drawerOpen) => set(
    drawerOpen
      ? { drawerOpen }
      : { drawerOpen, queueRevealJobId: null, queueRevealItemId: null },
  ),
  openQueueForImage: (imageId, kind) => {
    const job = matchingQueueJob(get().jobs, imageId, kind);
    const item = job?.items.find((entry) => entry.imageId === imageId);
    set({
      drawerOpen: true,
      queueRevealJobId: job?.id ?? null,
      queueRevealItemId: item?.id ?? null,
    });
  },
  setShortcutsOpen: (shortcutsOpen) => set({ shortcutsOpen }),
  setSpacePressed: (spacePressed) => set({ spacePressed }),

  startBatch: async (
    kinds,
    imageIds,
    exportOptions,
    concurrency = 1,
    regionIds,
    preprocessing,
    provider,
  ) => {
    if (!get().currentProject || !imageIds.length || !kinds.length) return false;
    const targetImageIds = [...new Set(imageIds)];
    const contextsLoaded = await Promise.all(targetImageIds.map((imageId) => {
      const context = get().g4Contexts[imageId];
      return context?.status === 'legacy' ? Promise.resolve(true) : get().loadG4Context(imageId);
    }));
    if (
      contextsLoaded.some((loaded) => !loaded)
      || targetImageIds.some((imageId) => get().g4Contexts[imageId]?.status !== 'legacy')
    ) {
      set({
        globalError: '所选页面血缘尚未全部确认为旧版页面，不能使用旧版批处理入口。',
      });
      return false;
    }
    const hasPreprocess = kinds.includes('preprocess');
    if (hasPreprocess && kinds.some((kind) => kind !== 'preprocess')) {
      set({
        globalError: '预处理不能与后续阶段放在同一批次；请先验收增强结果，再开始文本检测。',
      });
      return false;
    }
    const hasDetect = kinds.includes('detect');
    const hasOcr = kinds.includes('ocr');
    const trustGatedKinds = kinds.filter((kind) =>
      kind === 'translate' || kind === 'inpaint' || kind === 'typeset'
    );
    if ((hasDetect || hasOcr) && trustGatedKinds.length) {
      set({
        globalError: '文本检测/OCR 与翻译、擦字修复或嵌字排版不能放在同一批次；请先完成 OCR 并人工确认文本框。',
      });
      return false;
    }
    const hasInpaint = kinds.includes('inpaint');
    const hasTranslate = kinds.includes('translate');
    const hasTypeset = kinds.includes('typeset');
    const hasExport = kinds.includes('export');
    if (hasInpaint && (hasTranslate || hasTypeset)) {
      set({
        globalError: '擦字修复不能与翻译或嵌字排版放在同一批次；请先验收净版，再开始翻译。',
      });
      return false;
    }
    if (hasTranslate && hasTypeset) {
      set({
        globalError: '翻译与嵌字排版不能放在同一批次；请先核对并确认译文。',
      });
      return false;
    }
    if (hasExport && kinds.some((kind) => kind !== 'export')) {
      set({
        globalError: '导出不能与处理阶段放在同一批次；请先验收成品，再单独导出。',
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
        'inpaint',
        'translate',
        'typeset',
        'export',
      ];
      const orderedKinds = operationOrder.filter((kind) => kinds.includes(kind));
      const preprocessProvider = provider ?? project.settings.preprocessorProvider;
      for (const kind of orderedKinds) {
        if (imageIds.some((imageId) => get().g4Contexts[imageId]?.status !== 'legacy')) {
          throw new Error('所选页面血缘状态已变化，旧版批处理已停止。');
        }
        const options: Record<string, unknown> = kind === 'preprocess'
          ? {
              provider: preprocessProvider,
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
                ? preprocessProvider
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
            preprocessingProvider: orderedKinds.includes('preprocess') ? preprocessProvider : image.preprocessingProvider,
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
          if (previous?.status === 'completed') return [];
          // First poll after load sees the whole history as completed; ignore
          // those. A job that appears already-done on a later poll still counts
          // — detect/OCR can finish between two refreshes.
          if (!previous && previousJobs.length === 0) return [];
          return job.items
            .map((item) => item.imageId)
            .filter((imageId): imageId is string => Boolean(imageId));
        }),
      );
      const completedTypesetImageIds = newlyCompletedImageIds('typeset');
      const completedInpaintImageIds = newlyCompletedImageIds('inpaint');
      const completedOcrImageIds = newlyCompletedImageIds('ocr');
      const completedDetectImageIds = newlyCompletedImageIds('detect');
      const completedPreprocessImageIds = newlyCompletedImageIds('preprocess');
      const terminalDetectImageIds = new Set(
        jobs.flatMap((job) => {
          if (job.kind !== 'detect' || job.status === 'queued' || job.status === 'running') return [];
          const previous = previousJobs.find((entry) => entry.id === job.id);
          if (!previous || (previous.status !== 'queued' && previous.status !== 'running')) return [];
          return job.items
            .map((item) => item.imageId)
            .filter((imageId): imageId is string => Boolean(imageId));
        }),
      );
      const terminalOCRImageIds = new Set(
        jobs.flatMap((job) => {
          if (job.kind !== 'ocr' || job.status === 'queued' || job.status === 'running') return [];
          const previous = previousJobs.find((entry) => entry.id === job.id);
          if (!previous || (previous.status !== 'queued' && previous.status !== 'running')) return [];
          return job.items
            .map((item) => item.imageId)
            .filter((imageId): imageId is string => Boolean(imageId));
        }),
      );
      const terminalMaskImageIds = new Set(
        jobs.flatMap((job) => {
          if (job.kind !== 'mask' || job.status === 'queued' || job.status === 'running') return [];
          const previous = previousJobs.find((entry) => entry.id === job.id);
          if (!previous || (previous.status !== 'queued' && previous.status !== 'running')) return [];
          return job.items.map((item) => item.imageId)
            .filter((imageId): imageId is string => Boolean(imageId));
        }),
      );
      const terminalCleanPlateImageIds = new Set(
        jobs.flatMap((job) => {
          if (job.kind !== 'inpaint'
            || job.status === 'queued' || job.status === 'running') return [];
          const previous = previousJobs.find((entry) => entry.id === job.id);
          if (!previous || (previous.status !== 'queued' && previous.status !== 'running')) return [];
          return job.items.map((item) => item.imageId)
            .filter((imageId): imageId is string => Boolean(imageId));
        }),
      );
      const terminalTranslationImageIds = new Set(
        jobs.flatMap((job) => {
          if (job.kind !== 'translate' || job.status === 'queued' || job.status === 'running') return [];
          const previous = previousJobs.find((entry) => entry.id === job.id);
          if (!previous || (previous.status !== 'queued' && previous.status !== 'running')) return [];
          return job.items.map((item) => item.imageId)
            .filter((imageId): imageId is string => Boolean(imageId));
        }),
      );
      const terminalTypesetImageIds = new Set(
        jobs.flatMap((job) => {
          if (job.kind !== 'typeset' || job.status === 'queued' || job.status === 'running') return [];
          const previous = previousJobs.find((entry) => entry.id === job.id);
          if (!previous || (previous.status !== 'queued' && previous.status !== 'running')) return [];
          return job.items.map((item) => item.imageId)
            .filter((imageId): imageId is string => Boolean(imageId));
        }),
      );
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
      const stateAfterRefresh = get();
      const terminalLineageImageIds = new Set([
        ...terminalDetectImageIds,
        ...terminalOCRImageIds,
        ...terminalMaskImageIds,
        ...terminalCleanPlateImageIds,
        ...terminalTranslationImageIds,
        ...terminalTypesetImageIds,
      ]);
      const terminalRefreshImageIds = [...terminalLineageImageIds].filter((imageId) =>
        !stateAfterRefresh.pendingRegionMutations.some((mutation) => mutation.imageId === imageId)
        && !stateAfterRefresh.pendingG4Mutations.some((mutation) => mutation.imageId === imageId)
      );
      await Promise.all(terminalRefreshImageIds.map(async (imageId) => {
        await Promise.all([
          get().loadRegions(imageId, true),
          get().loadG4Context(imageId, true),
        ]);
        const phase = workflowPhase(get().g4Contexts[imageId]);
        if (phase === 'G6' || phase === 'G7' || phase === 'G8') {
          await get().loadOCRContext(imageId, true);
        }
        if (phase === 'G7' || phase === 'G8') await get().loadMaskContext(imageId, true);
        if (phase === 'G8') await get().loadCleanPlateContext(imageId, true);
        if (phase === 'G9') await get().loadTranslationContext(imageId, true);
        if (phase === 'G10') await get().loadTypesetContext(imageId, true);
      }));
      const activeImageId = get().activeImageId;
      const hasPendingActiveEdits = activeImageId
        ? get().pendingRegionMutations.some((mutation) => mutation.imageId === activeImageId)
          || get().pendingG4Mutations.some((mutation) => mutation.imageId === activeImageId)
        : false;
      if (
        activeImageId
        && !hasPendingActiveEdits
        && newlyCompleted
        && !terminalLineageImageIds.has(activeImageId)
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
          set((state) => ({
            canvasMode: 'typeset',
            compareMode: true,
            ...(focusIds.length
              ? {
                selectedRegionIds: focusIds,
                rightTab: 'typesetting' as const,
                focusRegionIds: focusIds,
                focusRequest: state.focusRequest + 1,
              }
              : {}),
          }));
        }
      } else if (
        activeImageId
        && completedInpaintImageIds.has(activeImageId)
        && get().images.find((entry) => entry.id === activeImageId)?.status.inpaint === 'done'
      ) {
        set({ canvasMode: 'erased', showMask: true, rightTab: 'repair', compareMode: true });
      } else if (
        activeImageId
        && (completedOcrImageIds.has(activeImageId) || completedDetectImageIds.has(activeImageId))
      ) {
        set({ rightTab: 'text' });
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
      if (action === 'resume' || action === 'retry') {
        const job = get().jobs.find((entry) => entry.id === jobId);
        if (!job || !job.items.length || job.items.some((item) => !item.imageId)) {
          set({ globalError: '无法确认任务的完整目标页血缘，继续或重试已停止。' });
          return;
        }
        const imageIds = [...new Set(
          job.items
            .map((item) => item.imageId)
            .filter((imageId): imageId is string => Boolean(imageId)),
        )];
        const contextsLoaded = await Promise.all(imageIds.map((imageId) => {
          const context = get().g4Contexts[imageId];
          return context?.status === 'legacy' ? Promise.resolve(true) : get().loadG4Context(imageId);
        }));
        if (
          contextsLoaded.some((loaded) => !loaded)
          || imageIds.some((imageId) => get().g4Contexts[imageId]?.status !== 'legacy')
        ) {
          set({
            globalError: '任务目标页血缘尚未全部确认为旧版页面；请使用对应血缘阶段入口。',
          });
          return;
        }
      }
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

  openJobItem: async (jobId, itemId) => {
    const job = get().jobs.find((entry) => entry.id === jobId);
    const item = job?.items.find((entry) => entry.id === itemId);
    const imageId = item?.imageId;
    if (!job || !item || !imageId) return false;
    if (!(await get().selectImage(imageId))) return false;
    const image = get().images.find((entry) => entry.id === imageId);
    if (!image) return false;
    const regions = get().regionsByImage[imageId] ?? [];
    const rightTab = inspectorTabForJobKind(job.kind);
    if (job.kind === 'typeset') {
      const overlayIds = overlayRegionIdsFromJobItem(item, regions);
      const overflowIds = overflowingRegionIds(image, regions);
      const focusIds = overlayIds.length ? overlayIds : overflowIds;
      set((state) => ({
        canvasMode: image.status.typeset === 'done' ? 'typeset' : state.canvasMode,
        rightTab,
        drawerOpen: false,
        ...(focusIds.length
          ? {
            selectedRegionIds: focusIds,
            focusRegionIds: focusIds,
            focusRequest: state.focusRequest + 1,
          }
          : {}),
      }));
      return true;
    }
    if (job.kind === 'inpaint' && image.status.inpaint === 'done') {
      set({ canvasMode: 'erased', showMask: true, rightTab, drawerOpen: false });
      return true;
    }
    if (job.kind === 'preprocess' && image.status.preprocess === 'done') {
      set({ canvasMode: 'preprocessed', rightTab, drawerOpen: false });
      return true;
    }
    set({ rightTab, drawerOpen: false });
    return true;
  },

  dismissError: () => set({ globalError: '' }),
}));

export function resetWorkbenchStore(): void {
  if (autosaveTimer) clearTimeout(autosaveTimer);
  autosaveTimer = null;
  activeSave = null;
  inFlightRegionIds.clear();
  regionLoadTokens.clear();
  g4LoadTokens.clear();
  backgroundLoadTokens.clear();
  ocrLoadTokens.clear();
  maskLoadTokens.clear();
  cleanPlateLoadTokens.clear();
  translationLoadTokens.clear();
  typesetLoadTokens.clear();
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

export function imageHasProcessingFailure(image: ImageAsset | null | undefined): boolean {
  if (!image) return false;
  const state = imageReviewState(image);
  return state === 'failed' || state === 'unavailable';
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

const STATUS_STAGE_KIND = [
  ['preprocess', 'preprocess'],
  ['detection', 'detect'],
  ['ocr', 'ocr'],
  ['translation', 'translate'],
  ['inpaint', 'inpaint'],
  ['typeset', 'typeset'],
  ['export', 'export'],
] as const;

function jobKindForProcessingStage(image: ImageAsset, stage: string): JobKind | null {
  if (stage === 'detect') return 'detect';
  if (
    stage === 'preprocess'
    || stage === 'ocr'
    || stage === 'translate'
    || stage === 'inpaint'
    || stage === 'typeset'
    || stage === 'export'
  ) {
    return stage;
  }
  if (stage === 'render') {
    if (image.status.inpaint === 'failed' || image.status.inpaint === 'unavailable') return 'inpaint';
    if (image.status.typeset === 'failed' || image.status.typeset === 'unavailable') return 'typeset';
  }
  return null;
}

function pipelineStatusForStage(image: ImageAsset, stage: string): StageState | undefined {
  const mapped = STATUS_STAGE_KIND.find(([, kind]) => kind === stage || (stage === 'detection' && kind === 'detect'));
  if (mapped) return image.status[mapped[0]];
  if (stage === 'render') {
    if (image.status.inpaint === 'failed' || image.status.inpaint === 'unavailable') return image.status.inpaint;
    if (image.status.typeset === 'failed' || image.status.typeset === 'unavailable') return image.status.typeset;
    if (image.status.inpaint === 'queued' || image.status.inpaint === 'running' || image.status.inpaint === 'done') {
      return image.status.inpaint;
    }
    return image.status.typeset;
  }
  return undefined;
}

function isResolvedProcessingStatus(state: StageState | undefined): boolean {
  return state === 'queued' || state === 'running' || state === 'done';
}

export function latestPageProcessingError(
  image: ImageAsset | null | undefined,
): { stage: string; error: string; kind: JobKind | null } | null {
  if (!image) return null;
  const recorded = (image.processingErrors ?? []).filter((entry) => entry.stage && entry.error);
  for (const entry of [...recorded].reverse()) {
    if (!isResolvedProcessingStatus(pipelineStatusForStage(image, entry.stage))) {
      return {
        stage: entry.stage,
        error: entry.error,
        kind: jobKindForProcessingStage(image, entry.stage),
      };
    }
  }
  const failed = [...STATUS_STAGE_KIND].reverse().find(([statusKey]) => {
    const state = image.status[statusKey];
    return state === 'failed' || state === 'unavailable';
  });
  if (failed) {
    return {
      stage: failed[1],
      error: image.error ?? '',
      kind: jobKindForProcessingStage(image, failed[1]),
    };
  }
  if (!image.error) return null;
  const busy = STATUS_STAGE_KIND.some(([statusKey]) => {
    const state = image.status[statusKey];
    return state === 'queued' || state === 'running';
  });
  if (busy) return null;
  return {
    stage: 'processing',
    error: image.error,
    kind: null,
  };
}

export function matchingQueueJob(
  jobs: Job[],
  imageId: string,
  kind?: JobKind | null,
): Job | undefined {
  const byItem = jobs.find((job) =>
    (!kind || job.kind === kind)
    && job.items.some((item) => item.imageId === imageId),
  );
  if (byItem) return byItem;
  if (kind) return jobs.find((job) => job.kind === kind);
  return undefined;
}

export function latestPageProcessingActivity(
  image: ImageAsset | null | undefined,
): { stage: string; status: 'queued' | 'running'; kind: JobKind | null } | null {
  if (!image) return null;
  const active = [...STATUS_STAGE_KIND].reverse().find(([statusKey]) => {
    const state = image.status[statusKey];
    return state === 'queued' || state === 'running';
  });
  if (!active) return null;
  const [statusKey, stage] = active;
  const status = image.status[statusKey];
  if (status !== 'queued' && status !== 'running') return null;
  return {
    stage,
    status,
    kind: jobKindForProcessingStage(image, stage),
  };
}

export function imageReviewState(
  image: ImageAsset,
): StageState | 'no_text_reviewed' | 'no_text_pending' | 'needs_review' {
  const stages = [
    image.status.import,
    image.status.preprocess,
    image.status.detection,
    image.status.ocr,
    image.status.translation,
    image.status.inpaint,
    image.status.typeset,
    image.status.export,
  ];
  if (stages.includes('running')) return 'running';
  if (stages.includes('queued')) return 'queued';
  if (image.error || stages.includes('failed')) return 'failed';
  if (image.status.ocr === 'unavailable' || image.status.detection === 'unavailable') return 'unavailable';
  if (image.status.reviewState === 'no-text-reviewed') return 'no_text_reviewed';
  if (image.status.reviewState === 'reviewed') return 'done';
  if (image.regionCount === image.ignoredCount && image.status.ocr === 'done') {
    return 'no_text_pending';
  }
  return 'needs_review';
}

export function imageMatchesFilter(
  image: ImageAsset,
  filter: WorkbenchState['imageFilter'],
): boolean {
  const state = imageReviewState(image);
  if (filter === 'all') return true;
  if (filter === 'failed') return imageHasProcessingFailure(image);
  if (filter === 'complete') return state === 'done';
  if (filter === 'no_text') return state === 'no_text_reviewed';
  if (filter === 'overflow') return imageHasTypesetOverflow(image);
  return state === 'needs_review'
    || state === 'no_text_pending'
    || state === 'not_started'
    || state === 'running'
    || state === 'queued';
}

export function visibleWorkbenchImages(
  state: Pick<WorkbenchState, 'images' | 'imageFilter' | 'imageSearch' | 'activeImageId'>,
): ImageAsset[] {
  const query = state.imageSearch.trim().toLocaleLowerCase();
  const matchesSearch = (image: ImageAsset) =>
    !query || image.relativePath.toLocaleLowerCase().includes(query);
  const matched = state.images.filter((image) =>
    matchesSearch(image) && imageMatchesFilter(image, state.imageFilter)
  );
  const active = state.images.find((image) => image.id === state.activeImageId);
  if (!active || matched.some((image) => image.id === active.id) || !matchesSearch(active)) {
    return matched;
  }
  const fullIndex = state.images.findIndex((image) => image.id === active.id);
  const insertAt = matched.findIndex((image) =>
    state.images.findIndex((entry) => entry.id === image.id) > fullIndex
  );
  if (insertAt < 0) return [...matched, active];
  return [...matched.slice(0, insertAt), active, ...matched.slice(insertAt)];
}

export function visibleImagePosition(
  state: Pick<WorkbenchState, 'images' | 'imageFilter' | 'imageSearch' | 'activeImageId'>,
): { current: number | null; total: number } {
  const visible = visibleWorkbenchImages(state);
  const index = visible.findIndex((image) => image.id === state.activeImageId);
  return {
    current: index >= 0 ? index + 1 : null,
    total: visible.length,
  };
}

export function canNavigateAdjacent(
  state: Pick<WorkbenchState, 'images' | 'imageFilter' | 'imageSearch' | 'activeImageId'>,
  direction: -1 | 1,
): boolean {
  return Boolean(
    adjacentVisibleImage(
      state.images,
      visibleWorkbenchImages(state),
      state.activeImageId,
      direction,
    ),
  );
}

function adjacentVisibleImage(
  images: ImageAsset[],
  visible: ImageAsset[],
  activeImageId: string | null,
  step: number,
): ImageAsset | undefined {
  if (!visible.length) return undefined;
  const visibleIndex = visible.findIndex((image) => image.id === activeImageId);
  if (visibleIndex >= 0) return visible[visibleIndex + step];
  const fullIndex = images.findIndex((image) => image.id === activeImageId);
  if (step > 0) {
    return visible.find((image) => images.findIndex((entry) => entry.id === image.id) > fullIndex);
  }
  return [...visible].reverse().find((image) =>
    images.findIndex((entry) => entry.id === image.id) < fullIndex
  );
}

function inspectorTabForJobKind(kind: JobKind): RightPanelTab {
  if (kind === 'typeset') return 'typesetting';
  if (kind === 'inpaint') return 'repair';
  if (kind === 'preprocess' || kind === 'export') return 'project';
  return 'text';
}

function overlayRegionIdsFromJobItem(item: Job['items'][number], regions: Region[]): string[] {
  if (item.output?.partialTypeset !== true) return [];
  const overlayIds = item.output.overlayRegionIds;
  if (!Array.isArray(overlayIds)) return [];
  const present = new Set(regions.map((region) => region.id));
  return [...new Set(
    overlayIds.filter((regionId): regionId is string =>
      typeof regionId === 'string' && present.has(regionId)
    ),
  )];
}

function overlayRegionIdsFromCompletedTypeset(
  jobs: Job[],
  previousJobs: Job[],
  imageId: string,
  regions: Region[],
): string[] {
  const selected: string[] = [];
  for (const job of jobs) {
    if (job.kind !== 'typeset' || job.status !== 'completed') continue;
    const previous = previousJobs.find((entry) => entry.id === job.id);
    if (!previous || previous.status === 'completed') continue;
    for (const item of job.items) {
      if (item.imageId !== imageId) continue;
      selected.push(...overlayRegionIdsFromJobItem(item, regions));
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
  return Boolean(
    state.pendingProjectMutation
    || state.pendingRegionMutations.length
    || state.pendingG4Mutations.length,
  );
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
