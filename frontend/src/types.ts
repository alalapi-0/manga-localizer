export type Theme = 'dark' | 'light';
export type CanvasMode = 'original' | 'preprocessed' | 'erased' | 'typeset';
export type CanvasTool = 'select' | 'region' | 'hand' | 'mask-brush' | 'mask-eraser';
export type RightPanelTab = 'text' | 'typesetting' | 'repair' | 'project';
export type ReviewState = 'pending' | 'reviewed' | 'no-text-reviewed';
export type ImageNavigationTarget = 'adjacent' | 'unreviewed' | 'overflow' | 'failed';
export type VisualStage = 'preprocess' | 'inpaint' | 'typeset';
export type StageReviewState = 'pending' | 'accepted' | 'rejected';
export type RegionDisposition = 'review' | 'trusted' | 'ignored';

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
  type: RegionType;
  direction: TextDirection;
  order: number;
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

export type JobKind = 'preprocess' | 'detect' | 'ocr' | 'translate' | 'inpaint' | 'typeset' | 'export';
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
