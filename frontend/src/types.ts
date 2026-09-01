export type Theme = 'dark' | 'light';
export type CanvasMode = 'original' | 'preprocessed' | 'erased' | 'typeset';
export type CanvasTool = 'select' | 'region' | 'hand' | 'mask-brush' | 'mask-eraser';
export type RightPanelTab = 'text' | 'typesetting' | 'repair' | 'project';
export type ReviewState = 'pending' | 'reviewed' | 'no-text-reviewed';
export type ImageNavigationTarget = 'adjacent' | 'unreviewed' | 'overflow' | 'failed';
export type VisualStage = 'preprocess' | 'inpaint' | 'typeset';
export type StageReviewState = 'pending' | 'accepted' | 'rejected';
export type RegionDisposition = 'review' | 'trusted' | 'ignored';
export type RegionContentDisposition =
  | 'translate'
  | 'ignore'
  | 'keep-art'
  | 'redraw-art'
  | 'false-positive';

export type BackgroundCategory =
  | 'white-solid'
  | 'black-solid'
  | 'other-solid'
  | 'simple-gradient'
  | 'screentone'
  | 'complex-lineart'
  | 'illustration/character';

export type BackgroundRationaleCode =
  | 'uniform-near-white'
  | 'uniform-near-black'
  | 'uniform-other-color'
  | 'smooth-gradient-continuity'
  | 'periodic-screentone'
  | 'structural-lines-cross-region'
  | 'character-or-illustration-detail'
  | 'mixed-visual-signals';

export type OCRSourceMode = 'original-attempt' | 'quality-attempt' | 'manual-correction';
export type OCRInputVariant = 'original' | 'quality';
export type OCRQCCheck =
  | 'original-and-quality-compared'
  | 'source-text-characters-checked'
  | 'punctuation-checked'
  | 'direction-checked'
  | 'reading-order-checked'
  | 'empty-or-garbled-checked'
  | 'duplicate-fragment-checked'
  | 'template-contamination-checked'
  | 'page-text-consistency-checked';
export type OCRQCFlag =
  | 'original-quality-disagree'
  | 'low-japanese-character-ratio'
  | 'ocr-empty-attempt'
  | 'ocr-garbled-attempt'
  | 'duplicate-fragment'
  | 'template-contamination'
  | 'manual-correction'
  | 'none';

export const OCR_QC_CHECKS: OCRQCCheck[] = [
  'original-and-quality-compared',
  'source-text-characters-checked',
  'punctuation-checked',
  'direction-checked',
  'reading-order-checked',
  'empty-or-garbled-checked',
  'duplicate-fragment-checked',
  'template-contamination-checked',
  'page-text-consistency-checked',
];

export type MaskCoverageCheck =
  | 'body-glyphs-covered'
  | 'punctuation-covered'
  | 'strokes-and-shadows-covered'
  | 'ruby-covered'
  | 'antialias-edges-covered';
export type MaskCollateralCheck =
  | 'bubble-borders-protected'
  | 'characters-protected'
  | 'speed-lines-protected'
  | 'screentone-protected'
  | 'nearby-art-protected';
export interface MaskCheckResult<T extends string> {
  check: T;
  passed: boolean;
}
export const MASK_COVERAGE_CHECKS: MaskCoverageCheck[] = [
  'body-glyphs-covered',
  'punctuation-covered',
  'strokes-and-shadows-covered',
  'ruby-covered',
  'antialias-edges-covered',
];
export const MASK_COLLATERAL_CHECKS: MaskCollateralCheck[] = [
  'bubble-borders-protected',
  'characters-protected',
  'speed-lines-protected',
  'screentone-protected',
  'nearby-art-protected',
];

export type CleanPlateRoute =
  | 'deterministic-solid'
  | 'controlled-gradient'
  | 'screentone-preserving'
  | 'ai-inpaint-redraw'
  | 'classical-fallback';
export type CleanPlateOriginKind = 'deterministic' | 'ai' | 'classical' | 'mixed';
export type CleanPlateCheck =
  | 'outside-mask-unchanged'
  | 'source-text-unreadable'
  | 'no-white-or-gray-hole'
  | 'no-blur-band'
  | 'no-repeated-texture'
  | 'background-continuous'
  | 'structure-preserved';
export type CleanPlateReviewReason =
  | 'clean-plate-complete'
  | 'residual-text-readable'
  | 'hole-or-block'
  | 'blur-band'
  | 'repeated-texture'
  | 'background-discontinuous'
  | 'structure-damaged'
  | 'outside-mask-changed'
  | 'multiple-visual-failures'
  | 'no-clean-plate-required';
export const CLEAN_PLATE_CHECKS: CleanPlateCheck[] = [
  'outside-mask-unchanged',
  'source-text-unreadable',
  'no-white-or-gray-hole',
  'no-blur-band',
  'no-repeated-texture',
  'background-continuous',
  'structure-preserved',
];

export type TranslationOriginKind = 'model' | 'manual' | 'agent' | 'dictionary';
export type TranslationQCCheck =
  | 'target-chinese-checked'
  | 'forbidden-template-checked'
  | 'nonempty-checked'
  | 'source-copy-checked'
  | 'japanese-residual-checked'
  | 'generic-duplicate-checked'
  | 'source-consistency-checked'
  | 'context-consistency-checked'
  | 'tone-and-type-checked'
  | 'source-noise-checked';
export type TranslationQCFlag =
  | 'none'
  | 'empty-output'
  | 'non-chinese-output'
  | 'forbidden-template'
  | 'source-copy'
  | 'japanese-residual'
  | 'generic-duplicate'
  | 'source-inconsistent'
  | 'context-inconsistent'
  | 'source-noise-hallucination';
export type TranslationReviewReason =
  | 'translation-reviewed'
  | Exclude<TranslationQCFlag, 'none'>
  | 'multiple-qc-failures';
export const TRANSLATION_QC_CHECKS: TranslationQCCheck[] = [
  'target-chinese-checked',
  'forbidden-template-checked',
  'nonempty-checked',
  'source-copy-checked',
  'japanese-residual-checked',
  'generic-duplicate-checked',
  'source-consistency-checked',
  'context-consistency-checked',
  'tone-and-type-checked',
  'source-noise-checked',
];

export interface LineageActor {
  actorKind: 'codex' | 'cursor' | 'human' | 'system';
  actorId?: string;
  taskId?: string;
  threadId?: string;
  sessionId?: string;
  operationSource: 'ui' | 'api' | 'script';
}

export interface MutationLineageContext {
  runId: string;
  pageGenerationId: string;
  expectedSequence: number;
  actor: LineageActor;
}

export interface JobLineageContext {
  runId: string;
  actor: LineageActor;
  pages: Array<{
    imageId: string;
    pageGenerationId: string;
    expectedSequence: number;
  }>;
}

export interface PageGeneration {
  id: string;
  runId: string;
  projectId: string;
  imageId: string;
  restartFromSource: boolean;
  parameterSetId: string;
  parameterSetHash: string;
  sourceProjectId: string;
  sourceImageId: string;
  sourceChecksum: string;
  state: 'active' | 'completed' | 'superseded';
  nextSequence: number;
  actor: LineageActor;
  createdAt: string;
  closedAt: string | null;
}

export interface PageLineageEvent {
  id: string;
  generationId: string;
  sequence: number;
  operation: string;
  gate: string | null;
  state: 'pending' | 'accepted' | 'rejected' | 'blocked' | 'not-applicable';
  actor: LineageActor;
  inputChecksum: string | null;
  outputChecksum: string | null;
  parentChecksum: string | null;
  stage: string | null;
  provider: string | null;
  modelVersion: string | null;
  parameterHash: string | null;
  jobId: string | null;
  jobItemId: string | null;
  revisionId: string | null;
  decision: string | null;
  reason: string | null;
  gitCommit: string | null;
  evidence: Record<string, unknown>;
  startedAt: string | null;
  finishedAt: string | null;
  createdAt: string;
}

export interface PageGateResult {
  imageId: string;
  imageRevision: number;
  generationId: string;
  nextSequence: number;
  event: PageLineageEvent;
}

export interface BackgroundGateContext {
  imageId: string;
  imageRevision: number;
  generationId: string;
  nextSequence: number;
  g4Checksum: string;
  backgroundChecksum: string;
  state: 'pending' | 'accepted' | 'not-applicable';
  eligibleRegionIds: string[];
  classifiedRegionIds: string[];
}

export interface OCRReviewEvidence {
  sourceMode: OCRSourceMode;
  selectedAttemptId: string;
  sourceTextChecksum: string;
  qcChecks: OCRQCCheck[];
  qcFlags: OCRQCFlag[];
}

export interface OCRAttempt {
  id: string;
  regionId: string;
  generationId: string;
  jobId: string;
  jobItemId: string;
  inputVariant: OCRInputVariant;
  parentChecksum: string;
  cropChecksum: string;
  cropBox: { x: number; y: number; width: number; height: number };
  provider: string;
  modelVersion: string | null;
  parameterHash: string;
  language: string | null;
  direction: Exclude<TextDirection, 'auto'>;
  text: string;
  textChecksum: string;
  confidence: number | null;
  createdAt: string;
}

export interface OCRGateContext {
  imageId: string;
  imageRevision: number;
  generationId: string;
  nextSequence: number;
  g5Checksum: string;
  ocrChecksum: string;
  state: 'pending' | 'accepted' | 'not-applicable';
  eligibleRegionIds: string[];
  attemptedRegionIds: string[];
  reviewedRegionIds: string[];
  attempts: OCRAttempt[];
}

export interface MaskDraftRegion {
  regionId: string;
  polygon?: Array<[number, number]> | null;
  maskMode: 'region' | 'text' | 'manual';
  polarity: 'auto' | 'dark' | 'light';
  padding: number;
  dilation: number;
  feather: number;
  maskEdits: MaskEdits;
}

export interface MaskDraft {
  revision: number;
  stateChecksum: string;
  regions: MaskDraftRegion[];
}

export interface MaskArtifact {
  artifactId: string;
  sequence: number;
  jobId: string;
  jobItemId: string;
  parentChecksum: string;
  maskChecksum: string;
  recipeChecksum: string;
  qualityChecksum: string;
  renderScale: number;
  provider: string;
  modelVersion: string;
  parameterHash: string;
  width: number;
  height: number;
  nonzeroPixelCount: number;
  bbox: { x: number; y: number; width: number; height: number };
  createdAt: string;
}

export interface MaskGateReview {
  id: string;
  state: 'accepted' | 'rejected' | 'not-applicable';
  reason:
    | 'complete-and-no-collateral'
    | 'coverage-incomplete'
    | 'collateral-damage'
    | 'coverage-and-collateral-failed'
    | 'no-eligible-regions';
  artifactId: string | null;
  maskChecksum: string | null;
  coverageChecks: Array<MaskCheckResult<MaskCoverageCheck>>;
  collateralChecks: Array<MaskCheckResult<MaskCollateralCheck>>;
  reviewer: LineageActor;
  createdAt: string;
}

export interface MaskGateContext {
  imageId: string;
  imageRevision: number;
  generationId: string;
  nextSequence: number;
  g6Checksum: string;
  qualityChecksum: string;
  maskStateChecksum: string;
  state: 'pending' | 'accepted' | 'rejected' | 'not-applicable';
  eligibleRegionIds: string[];
  rubyRegionIdsByPrimary: Record<string, string[]>;
  draft: MaskDraft;
  artifacts: MaskArtifact[];
  selectedArtifactId: string | null;
  review: MaskGateReview | null;
}

export interface CleanPlateRouteEntry {
  regionId: string;
  backgroundCategory: BackgroundCategory;
  route: CleanPlateRoute;
  originKind: Exclude<CleanPlateOriginKind, 'mixed'>;
  provider: string;
  modelVersion: string;
  parameterHash: string;
}

export interface CleanPlateRouteSummary {
  regionId: string;
  backgroundCategory: BackgroundCategory;
  defaultRoute: Exclude<CleanPlateRoute, 'classical-fallback'>;
}

export interface CleanPlateCandidateReview {
  id: string;
  state: 'accepted' | 'rejected';
  reason: CleanPlateReviewReason;
  checks: Array<MaskCheckResult<CleanPlateCheck>>;
  reviewer: LineageActor;
  createdAt: string;
}

export interface CleanPlateCandidate {
  candidateId: string;
  sequence: number;
  jobId: string;
  jobItemId: string;
  parentChecksum: string;
  qualityChecksum: string;
  backgroundChecksum: string;
  maskArtifactId: string;
  maskChecksum: string;
  routeManifest: CleanPlateRouteEntry[];
  routeChecksum: string;
  originKind: CleanPlateOriginKind;
  providerIds: string[];
  modelVersions: string[];
  parameterHash: string;
  candidateChecksum: string;
  width: number;
  height: number;
  renderScale: number;
  outsideMaskChangeCount: number;
  anomalies: string[];
  completed: boolean;
  review: CleanPlateCandidateReview | null;
  createdAt: string;
}

export interface CleanPlateGateContext {
  imageId: string;
  imageRevision: number;
  generationId: string;
  nextSequence: number;
  g7Checksum: string;
  qualityChecksum: string;
  backgroundChecksum: string;
  maskArtifactId: string | null;
  maskChecksum: string | null;
  cleanPlateStateChecksum: string;
  state: 'pending' | 'accepted' | 'rejected' | 'not-applicable';
  routes: CleanPlateRouteSummary[];
  candidates: CleanPlateCandidate[];
  acceptedCandidateId: string | null;
  fallbackEnabled: boolean;
  fallbackAllowed: boolean;
}

export interface CleanPlateBitmapObservation {
  imageId: string;
  generationId: string;
  nextSequence: number;
  cleanPlateStateChecksum: string;
  candidateId: string;
  imageRevision: number;
  sourceChecksum: string;
  qualityChecksum: string;
  maskArtifactId: string;
  maskChecksum: string;
  maskWidth: number;
  maskHeight: number;
  checksum: string;
  width: number;
  height: number;
  state: 'ready';
}

export interface TranslationEligibleRegion {
  regionId: string;
  readingOrder: number;
  regionType: RegionType;
  direction: Exclude<TextDirection, 'auto'>;
  paragraphGroupId: string | null;
  sourceText: string;
  sourceTextChecksum: string;
  contextRegionIds: string[];
  contextChecksum: string;
  rubyExcluded: true;
}

export interface TranslationCandidateReview {
  id: string;
  state: 'accepted' | 'rejected';
  reason: TranslationReviewReason;
  checks: Array<MaskCheckResult<TranslationQCCheck>>;
  qcFlags: TranslationQCFlag[];
  reviewer: LineageActor;
  createdAt: string;
}

export interface TranslationCandidate {
  candidateId: string;
  sequence: number;
  regionId: string;
  revisionNumber: number;
  supersedesCandidateId: string | null;
  originKind: TranslationOriginKind;
  targetLanguage: string;
  provider: string;
  modelVersion: string;
  parameterHash: string;
  g8Checksum: string;
  cleanPlateChecksum: string;
  sourceTextChecksum: string;
  sourceRegionRevision: number;
  contextChecksum: string;
  translationText: string;
  candidateChecksum: string;
  computedQcFlags: TranslationQCFlag[];
  jobId: string | null;
  jobItemId: string | null;
  revisionId: string;
  review: TranslationCandidateReview | null;
  createdAt: string;
}

export interface TranslationGateContext {
  imageId: string;
  imageRevision: number;
  generationId: string;
  nextSequence: number;
  g8Checksum: string;
  cleanPlateCandidateId: string | null;
  cleanPlateChecksum: string;
  translationStateChecksum: string;
  targetLanguage: string;
  terminalChecksum: string | null;
  state: 'pending' | 'accepted' | 'not-applicable';
  eligibleRegions: TranslationEligibleRegion[];
  candidates: TranslationCandidate[];
  acceptedCandidateIdsByRegion: Record<string, string>;
  reviewedRegionCount: number;
}

export type TypesetRoute = 'bubble' | 'ordinary' | 'art-lettering' | 'keep' | 'ignore';

export type TypesetCheck =
  | 'original-clean-final-compared'
  | 'translation-complete'
  | 'hierarchy-reading-order-preserved'
  | 'key-art-unobstructed'
  | 'typography-source-matched'
  | 'bubble-contained'
  | 'art-lettering-composition-matched'
  | 'overflow-free';

export const TYPESET_CHECKS: TypesetCheck[] = [
  'original-clean-final-compared',
  'translation-complete',
  'hierarchy-reading-order-preserved',
  'key-art-unobstructed',
  'typography-source-matched',
  'bubble-contained',
  'art-lettering-composition-matched',
  'overflow-free',
];

export type TypesetReviewReason =
  | 'typeset-reviewed'
  | TypesetCheck
  | 'multiple-visual-failures';

export interface TypesetDisplayFont {
  token: string;
  label: string;
  fontChecksum: string;
  capabilityChecksum: string;
  role: 'regular' | 'display';
}

export interface ArtLetteringCapability {
  available: boolean;
  contractVersion: string;
  features: string[];
  reason: string | null;
}

export interface TypesetRegionStyle {
  fontToken: string;
  fontChecksum: string;
  fontSize: number;
  minFontSize: number;
  autoFit: boolean;
  fill: string;
  strokeColor: string;
  strokeWidth: number;
  lineSpacing: number;
  letterSpacing: number;
  align: 'start' | 'center' | 'end';
  padding: number;
  rotation: number;
  scaleX: number;
  scaleY: number;
  shearX: number;
  shearY: number;
  opacity: number;
  visualCenterX: number;
  visualCenterY: number;
  fontSource: 'server-regular-default' | 'server-display-default' | 'region-override';
}

export type TypesetRegionStyleInput = Omit<TypesetRegionStyle, 'fontChecksum' | 'fontSource'>;

export interface TypesetRegionManifestEntry {
  regionId: string;
  regionRevision: number;
  geometry: { x: number; y: number; width: number; height: number; rotation: number };
  readingOrder: number;
  regionType: RegionType;
  direction: Exclude<TextDirection, 'auto'>;
  paragraphGroupId: string | null;
  contentDisposition: RegionContentDisposition;
  acceptedTranslationCandidateId: string | null;
  acceptedTranslationCandidateChecksum: string | null;
}

export interface TypesetRouteEntry {
  regionId: string;
  readingOrder: number;
  route: TypesetRoute;
  renderRequired: boolean;
  translationCandidateId: string | null;
  translationCandidateChecksum: string | null;
}

export interface TypesetStyleEntry {
  regionId: string;
  route: TypesetRoute;
  style: TypesetRegionStyle | null;
}

export interface TypesetLayoutEntry {
  regionId: string;
  route: Exclude<TypesetRoute, 'keep' | 'ignore'>;
  bounds: { x: number; y: number; width: number; height: number };
  fontSize: number;
  overflow: boolean;
  direction: Exclude<TextDirection, 'auto'>;
  rotation: number;
  scaleX: number;
  scaleY: number;
  shearX: number;
  shearY: number;
  opacity: number;
  visualCenterX: number;
  visualCenterY: number;
  align: 'start' | 'center' | 'end';
}

export interface TypesetCandidate {
  candidateId: string;
  sequence: number;
  jobId: string;
  jobItemId: string;
  parentChecksum: string;
  g9TerminalChecksum: string;
  translationStateChecksum: string;
  cleanPlateCandidateId: string | null;
  cleanPlateChecksum: string;
  regionManifest: TypesetRegionManifestEntry[];
  routeManifest: TypesetRouteEntry[];
  routeChecksum: string;
  styleManifest: TypesetStyleEntry[];
  styleChecksum: string;
  layoutManifest: TypesetLayoutEntry[];
  layoutChecksum: string;
  provider: string;
  modelVersion: string;
  parameterHash: string;
  candidateChecksum: string;
  width: number;
  height: number;
  renderScale: number;
  overflowRegionIds: string[];
  anomalies: string[];
  revisionId: string;
  completed: boolean;
  artifactUrl: string;
  review: TypesetCandidateReview | null;
  createdAt: string;
}

export interface TypesetCandidateReview {
  id: string;
  candidateId: string;
  sequence: number;
  state: 'accepted' | 'rejected';
  reason: TypesetReviewReason;
  parentChecksum: string;
  candidateChecksum: string;
  routeChecksum: string;
  styleChecksum: string;
  layoutChecksum: string;
  g9TerminalChecksum: string;
  cleanPlateChecksum: string;
  observedWidth: number;
  observedHeight: number;
  observedRenderScale: number;
  checks: Array<MaskCheckResult<TypesetCheck>>;
  reviewer: LineageActor;
  terminalChecksum: string;
  revisionId: string;
  createdAt: string;
}

export interface TypesetGateContext {
  imageId: string;
  imageRevision: number;
  generationId: string;
  nextSequence: number;
  g9TerminalChecksum: string;
  translationStateChecksum: string;
  cleanPlateCandidateId: string | null;
  cleanPlateChecksum: string;
  state: 'pending' | 'accepted';
  terminalChecksum: string | null;
  candidates: TypesetCandidate[];
  reviews: TypesetCandidateReview[];
  routeManifest: TypesetRouteEntry[];
  routeChecksum: string;
  styleDefaults: {
    bubble: TypesetRegionStyle | null;
    ordinary: TypesetRegionStyle | null;
    artLettering: TypesetRegionStyle | null;
  };
  availableFonts: TypesetDisplayFont[];
  availableDisplayFonts: TypesetDisplayFont[];
  artLetteringCapability: ArtLetteringCapability;
  retryRegionStyles: Record<string, TypesetRegionStyleInput>;
}

export interface TypesetBitmapObservation {
  imageId: string;
  generationId: string;
  nextSequence: number;
  candidateId: string;
  imageRevision: number;
  sourceChecksum: string;
  cleanPlateChecksum: string;
  candidateChecksum: string;
  routeChecksum: string;
  styleChecksum: string;
  layoutChecksum: string;
  width: number;
  height: number;
  renderScale: number;
  state: 'ready';
}

export interface StageReview {
  state: Exclude<StageReviewState, 'pending'>;
  reviewedAt: string;
  resultRevision: number;
  artifactChecksum: string;
  maskChecksum?: string;
}

export interface StageReviewObservation {
  imageId: string;
  stage: VisualStage;
  revision: number;
  artifactChecksum: string;
  maskChecksum?: string;
}

export type StageState =
  | 'not_started'
  | 'queued'
  | 'running'
  | 'done'
  | 'failed'
  | 'unavailable';

export type RegionType =
  | 'dialogue'
  | 'narration'
  | 'sound_effect'
  | 'title'
  | 'ruby'
  | 'background'
  | 'unknown'
  | 'thought'
  | 'sign'
  | 'speech'
  | 'other';

export type TextDirection = 'vertical' | 'horizontal' | 'auto';

export interface ProviderCapability {
  id: string;
  label: string;
  kind: 'preprocessor' | 'detector' | 'ocr' | 'translator' | 'inpainter' | 'typesetter';
  available: boolean;
  configurable?: boolean;
  local: boolean;
  isMock: boolean;
  reason?: string;
  textPolarities?: Array<'auto' | 'dark' | 'light'>;
}

export interface AppCapabilities {
  providers: ProviderCapability[];
  version?: string;
  system?: Record<string, string | boolean | number>;
}

export interface ProjectSettings {
  sourceLanguage: string;
  targetLanguage: string;
  preprocessorProvider: string;
  preprocessing: PreprocessingSettings;
  detectorProvider: string;
  ocrProvider: string;
  translatorProvider: string;
  inpainterProvider: string;
  requireAIInpaintBeforeDownstream: boolean;
  contextPages: number;
  glossary: string;
  characterNames: string;
  remoteEndpoint: string;
  remoteModel: string;
  apiKeyConfigured: boolean;
  preserveTree: boolean;
}

export interface PreprocessingSettings {
  profile: 'off' | 'ocr-friendly' | 'balanced' | 'visual-quality';
  enableUpscale: boolean;
  upscaleFactor: 2 | 3 | 4;
  enableDenoise: boolean;
  enableSharpen: boolean;
  enableContrastEnhance: boolean;
  enableEdgeOptimize: boolean;
  enableBinarize: boolean;
  threshold: number;
}

export interface ProjectSummary {
  id: string;
  name: string;
  rootPath?: string;
  manifestPath?: string;
  imageCount: number;
  updatedAt?: string;
  revision: number;
}

export interface Project extends ProjectSummary {
  settings: ProjectSettings;
  createdAt?: string;
}

export interface PipelineStatus {
  import: StageState;
  preprocess: StageState;
  detection: StageState;
  ocr: StageState;
  translation: StageState;
  inpaint: StageState;
  typeset: StageState;
  export: StageState;
  reviewState: ReviewState;
  reviewedAt?: string | null;
}

export interface InpaintCandidate {
  id: string;
  label: string;
  anomalies: string[];
  originKind: 'direct-ai' | 'ai-derived' | 'classical' | 'deterministic-postprocess' | 'mixed';
}

export interface InpaintFallback {
  state: 'pending' | 'approved';
  kind?: 'classical-page-fallback' | null;
  reason?: 'ai-visible-artifacts' | null;
  originKind?: 'classical' | null;
  rejectedAiCandidateIds?: string[];
  candidateId?: string | null;
}

export interface PreprocessSuggestion {
  profile: PreprocessingSettings['profile'];
  reasons: string[];
  metrics: {
    width: number;
    height: number;
    minSide: number;
    sampled: boolean;
    laplacianVar?: number;
    luminanceStd?: number;
    uniqueGray?: number;
    grayscale?: boolean;
  };
}

export interface ProcessingError {
  stage: string;
  error: string;
}

export interface ImageAsset {
  id: string;
  projectId: string;
  name: string;
  relativePath: string;
  width: number;
  height: number;
  regionCount: number;
  confirmedCount: number;
  ignoredCount: number;
  trustedCount: number;
  trustReviewCount: number;
  status: PipelineStatus;
  stageReviews: Partial<Record<VisualStage, StageReview>>;
  preprocessingProvider?: string;
  detectorProvider?: string;
  ocrProvider?: string;
  translatorProvider?: string;
  inpaintingProvider?: string;
  typesettingProvider?: string;
  renderInputVariant?: 'original' | 'preprocessed';
  renderScale?: [number, number];
  renderedSize?: [number, number];
  inpaintCandidate?: string;
  inpaintCandidates?: InpaintCandidate[];
  inpaintCandidateGenerationId?: string | null;
  inpaintAiRejectedCandidateIds: string[];
  inpaintFallback?: InpaintFallback;
  typesetOverflowCount: number;
  typesetOverflowRegionIds: string[];
  preprocessSuggestion: PreprocessSuggestion;
  processingErrors: ProcessingError[];
  revision: number;
  error?: string;
}

export interface RegionStyle {
  fontFamily: string;
  fontSize: number;
  autoFit: boolean;
  color: string;
  strokeColor: string;
  strokeWidth: number;
  lineHeight: number;
  letterSpacing: number;
  align: 'start' | 'center' | 'end';
  padding: number;
}

export interface RepairSettings {
  method: 'telea' | 'navier_stokes' | 'solid' | 'screentone';
  maskMode: 'region' | 'text' | 'manual';
  textPolarity: 'auto' | 'dark' | 'light';
  maskPadding: number;
  dilation: number;
  feather: number;
  radius: number;
  fillColor: string;
  detectorGenerated?: boolean;
  detectedTextCandidate?: string;
  maskPolygon?: Array<[number, number]>;
  ocrAttemptCount?: number;
  ocrInputVariant?: 'original' | 'preprocessed';
  inpainterProvider?: string;
  maskEdits?: MaskEdits;
}

export interface MaskEditStroke {
  mode: 'add' | 'erase';
  radius: number;
  points: Array<[number, number]>;
}

export interface MaskEdits {
  version: 1;
  strokes: MaskEditStroke[];
}

export interface Region {
  id: string;
  imageId: string;
  x: number;
  y: number;
  width: number;
  height: number;
  rotation: number;
  sourceText: string;
  translationText: string;
  translationProvider: string | null;
  type: RegionType;
  direction: TextDirection;
  order: number;
  paragraphGroupId: string | null;
  rubyParentId: string | null;
  contentDisposition: RegionContentDisposition | null;
  backgroundCategory: BackgroundCategory | null;
  backgroundConfidence: number | null;
  backgroundRationaleCodes: BackgroundRationaleCode[] | null;
  backgroundReviewer: LineageActor | null;
  backgroundGenerationId: string | null;
  ocrReview: OCRReviewEvidence | null;
  ocrReviewer: LineageActor | null;
  ocrGenerationId: string | null;
  detectorJobItemId: string | null;
  detectorCandidateIndex: number | null;
  confidence: number | null;
  detectorConfidence: number | null;
  ocrConfidence: number | null;
  trustDisposition: RegionDisposition;
  trustReason: string;
  trustPolicyVersion: number;
  recognition: Record<string, unknown>;
  ignored: boolean;
  confirmed: boolean;
  style: RegionStyle;
  repair: RepairSettings;
  revision: number;
  createdAt?: string;
  updatedAt?: string;
}

export type JobKind = 'preprocess' | 'detect' | 'ocr' | 'mask' | 'translate' | 'inpaint' | 'typeset' | 'export';
export type JobStatus =
  | 'queued'
  | 'running'
  | 'paused'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'unavailable';

export interface JobItem {
  id: string;
  imageId?: string;
  label: string;
  status: JobStatus;
  progress: number;
  error?: string;
  output?: Record<string, unknown>;
}

export interface Job {
  id: string;
  projectId: string;
  kind: JobKind;
  status: JobStatus;
  progress: number;
  total: number;
  completed: number;
  error?: string;
  items: JobItem[];
  createdAt?: string;
  updatedAt?: string;
}

export interface ExportOptions {
  format: 'images' | 'json' | 'both';
  imageVariant: 'typeset' | 'inpainted' | 'both';
  outputPath?: string;
  conflict: 'rename' | 'overwrite' | 'skip';
  preserveTree: boolean;
  concurrency?: number;
}

export interface BatchRequest {
  imageIds: string[];
  regionIds?: string[];
  options?: Record<string, unknown>;
  lineage?: JobLineageContext;
}

export interface RevisionConflict {
  expectedRevision?: number;
  actualRevision?: number;
  resource?: unknown;
}

export const DEFAULT_PROJECT_SETTINGS: ProjectSettings = {
  sourceLanguage: 'ja',
  targetLanguage: 'zh-CN',
  preprocessorProvider: 'opencv-pillow',
  preprocessing: {
    profile: 'ocr-friendly',
    enableUpscale: true,
    upscaleFactor: 2,
    enableDenoise: true,
    enableSharpen: true,
    enableContrastEnhance: true,
    enableEdgeOptimize: false,
    enableBinarize: false,
    threshold: 180,
  },
  detectorProvider: 'tesseract',
  ocrProvider: 'tesseract',
  translatorProvider: 'manual',
  inpainterProvider: 'opencv',
  requireAIInpaintBeforeDownstream: false,
  contextPages: 1,
  glossary: '',
  characterNames: '',
  remoteEndpoint: '',
  remoteModel: '',
  apiKeyConfigured: false,
  preserveTree: true,
};

export const DEFAULT_REGION_STYLE: RegionStyle = {
  fontFamily: 'system-ui',
  fontSize: 28,
  autoFit: true,
  color: '#171717',
  strokeColor: '#ffffff',
  strokeWidth: 1,
  lineHeight: 1.15,
  letterSpacing: 0,
  align: 'center',
  padding: 8,
};

export const DEFAULT_REPAIR_SETTINGS: RepairSettings = {
  method: 'telea',
  maskMode: 'text',
  textPolarity: 'auto',
  maskPadding: 4,
  dilation: 2,
  feather: 2,
  radius: 3,
  fillColor: '#ffffff',
};

export const EMPTY_PIPELINE_STATUS: PipelineStatus = {
  import: 'done',
  preprocess: 'not_started',
  detection: 'not_started',
  ocr: 'not_started',
  translation: 'not_started',
  inpaint: 'not_started',
  typeset: 'not_started',
  export: 'not_started',
  reviewState: 'pending',
  reviewedAt: null,
};
