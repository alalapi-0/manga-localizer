import type {
  AppCapabilities,
  BackgroundCategory,
  BackgroundGateContext,
  BackgroundRationaleCode,
  BatchRequest,
  CleanPlateCheck,
  CleanPlateGateContext,
  CleanPlateReviewReason,
  ExportOptions,
  ImageAsset,
  Job,
  MutationLineageContext,
  MaskCheckResult,
  MaskCollateralCheck,
  MaskCoverageCheck,
  MaskDraftRegion,
  MaskGateContext,
  OCRGateContext,
  OCRQCCheck,
  OCRSourceMode,
  PageGateResult,
  PageGeneration,
  PageLineageEvent,
  Project,
  ProjectSettings,
  ProjectSummary,
  Region,
  ReviewState,
  StageReviewState,
  StageReviewObservation,
  TranslationGateContext,
  TranslationOriginKind,
  TranslationQCCheck,
  TranslationQCFlag,
  TranslationReviewReason,
  TypesetCheck,
  TypesetGateContext,
  TypesetReviewReason,
  VisualStage,
  RevisionConflict,
} from '../types';
import type {
  FinalReviewBatch,
  FinalReviewBatchSummary,
  FinalReviewExportRequest,
  FinalReviewExportResult,
  FinalReviewItem,
  FinalReviewItemPatch,
  FinalReviewEvidenceKind,
  FinalReviewRefreshRequest,
  FinalReviewRefreshResult,
  FinalReviewRepairRequest,
  FinalReviewRepairResult,
  FinalReviewSaveResult,
} from '../finalReview/types';

const API_BASE = (import.meta.env.VITE_API_BASE || '/api').replace(/\/$/, '');

type JsonRecord = Record<string, unknown>;

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly details?: unknown;
  readonly conflict?: RevisionConflict;

  constructor(
    message: string,
    status: number,
    options: { code?: string; details?: unknown; conflict?: RevisionConflict } = {},
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = options.code;
    this.details = options.details;
    this.conflict = options.conflict;
  }
}

function snakeToCamelKey(key: string): string {
  return key.replace(/_([a-z])/g, (_, letter: string) => letter.toUpperCase());
}

function mapKeys(value: unknown, mapper: (key: string) => string): unknown {
  if (Array.isArray(value)) return value.map((entry) => mapKeys(entry, mapper));
  if (value === null || typeof value !== 'object' || value instanceof Blob || value instanceof File) {
    return value;
  }
  return Object.fromEntries(
    Object.entries(value as JsonRecord).map(([key, entry]) => [mapper(key), mapKeys(entry, mapper)]),
  );
}

function fromApi<T>(value: unknown): T {
  return mapKeys(value, snakeToCamelKey) as T;
}

function unwrap<T>(payload: unknown, collectionKey?: string): T {
  const normalized = fromApi<unknown>(payload);
  if (normalized && typeof normalized === 'object' && !Array.isArray(normalized)) {
    const record = normalized as JsonRecord;
    if ('data' in record) return record.data as T;
    if (collectionKey && collectionKey in record) return record[collectionKey] as T;
    if ('item' in record) return record.item as T;
  }
  return normalized as T;
}

interface RequestOptions extends Omit<RequestInit, 'body'> {
  body?: unknown;
  rawBody?: BodyInit;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = new Headers(options.headers);
  let body = options.rawBody;
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json');
    // The backend's public contract is camelCase. Nested generic settings/options must
    // remain camelCase as well, otherwise provider-specific keys become invisible.
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...options,
      headers,
      body,
      credentials: 'same-origin',
    });
  } catch (error) {
    throw new ApiError(
      error instanceof Error ? `无法连接本地服务：${error.message}` : '无法连接本地服务',
      0,
      { code: 'NETWORK_ERROR' },
    );
  }

  const contentType = response.headers.get('content-type') ?? '';
  const payload = response.status === 204
    ? undefined
    : contentType.includes('application/json')
      ? await response.json()
      : await response.text();

  if (!response.ok) {
    const normalized = fromApi<JsonRecord>(payload ?? {});
    const detail = typeof normalized.detail === 'object'
      ? (normalized.detail as JsonRecord)
      : undefined;
    const message =
      (typeof normalized.message === 'string' && normalized.message) ||
      (typeof normalized.detail === 'string' && normalized.detail) ||
      (detail && typeof detail.message === 'string' && detail.message) ||
      `请求失败（HTTP ${response.status}）`;
    const conflictSource = (detail ?? normalized) as JsonRecord;
    throw new ApiError(message, response.status, {
      code: typeof normalized.code === 'string' ? normalized.code : undefined,
      details: normalized,
      conflict:
        response.status === 409
          ? {
              expectedRevision: Number(conflictSource.expectedRevision) || undefined,
              actualRevision: Number(conflictSource.actualRevision) || undefined,
              resource: conflictSource.resource,
            }
          : undefined,
    });
  }

  return fromApi<T>(payload);
}

function listFrom<T>(payload: unknown, key: string): T[] {
  return unwrap<T[]>(payload, key) ?? [];
}

function authoritativeFinalReviewResult<T extends FinalReviewSaveResult | FinalReviewRefreshResult>(
  payload: unknown,
  operation: string,
): T {
  if (
    !payload || typeof payload !== 'object' || !('item' in payload)
    || !payload.item || typeof payload.item !== 'object'
    || !('batchRevision' in payload) || !Number.isInteger(payload.batchRevision)
    || Number(payload.batchRevision) < 1
    || !('historyCreated' in payload) || typeof payload.historyCreated !== 'boolean'
  ) {
    throw new ApiError(`${operation}响应缺少权威 batch revision/history 标记`, 502, {
      code: 'INVALID_FINAL_REVIEW_RESPONSE',
    });
  }
  return payload as T;
}

function capabilityRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function normalizeCapabilities(payload: unknown): AppCapabilities {
  const config = unwrap<Record<string, unknown>>(payload);
  const providerGroups = capabilityRecord(config.providers);
  const systemCapabilities = capabilityRecord(config.capabilities);
  const providers: AppCapabilities['providers'] = [];
  const labels: Record<string, string> = {
    tesseract: 'Tesseract',
    'opencv-pillow': 'OpenCV / Pillow 基础增强',
    'realesrgan-onnx': 'Real-ESRGAN ONNX 动漫超分',
    'realesrgan-ncnn': 'Real-ESRGAN NCNN',
    'ppocr-v3': 'PP-OCRv3',
    'ppocr-v3+tesseract': 'PP-OCRv3 + Tesseract 联合检测',
    'lama-onnx': 'LaMa ONNX',
    opencv: 'OpenCV',
    manual: '手动翻译',
    mock: '确定性演示翻译',
    dictionary: '本地词典',
    'argos-ja-zh': 'Argos 本地日→中',
    'openai-compatible': 'OpenAI 兼容接口',
    pillow: 'Pillow 本地排版',
  };

  const addGroup = (
    groupName: string,
    kind: AppCapabilities['providers'][number]['kind'],
  ) => {
    const group = capabilityRecord(providerGroups[groupName]);
    for (const [providerId, raw] of Object.entries(group)) {
      const detail = capabilityRecord(raw);
      const available = Boolean(detail.available);
      const textPolarities = Array.isArray(detail.textPolarities)
        ? detail.textPolarities.filter(
            (value): value is 'auto' | 'dark' | 'light' => (
              value === 'auto' || value === 'dark' || value === 'light'
            ),
          )
        : undefined;
      providers.push({
        id: providerId,
        label: labels[providerId] ?? providerId,
        kind,
        available,
        configurable: Boolean(detail.configurable),
        local: detail.remote !== true,
        isMock: providerId === 'mock',
        textPolarities,
        reason: available
          ? undefined
          : typeof detail.error === 'string'
            ? detail.error
            : providerId === 'argos-ja-zh'
              ? '未安装本地 Argos 日英/英中模型或 CTranslate2'
              : providerId === 'openai-compatible'
                ? '当前会话尚未配置 API Key'
                : '本地服务报告此能力不可用',
      });
    }
  };

  addGroup('preprocessing', 'preprocessor');
  addGroup('ocr', 'ocr');
  const detectionGroup = Object.keys(capabilityRecord(providerGroups.detection)).length
    ? capabilityRecord(providerGroups.detection)
    : capabilityRecord(providerGroups.ocr);
  for (const [providerId, raw] of Object.entries(detectionGroup)) {
    const detail = capabilityRecord(raw);
    providers.push({
      id: providerId,
      label: `${labels[providerId] ?? providerId} 文本检测`,
      kind: 'detector',
      available: Boolean(detail.available && detail.detectTextRegions !== false),
      local: true,
      isMock: false,
      reason: detail.available ? undefined : String(detail.error || '文本检测能力不可用'),
    });
  }
  addGroup('inpainting', 'inpainter');
  addGroup('translation', 'translator');

  const fonts = capabilityRecord(systemCapabilities.fonts);
  providers.push({
    id: 'pillow',
    label: labels.pillow ?? 'Pillow 本地排版',
    kind: 'typesetter',
    available: Boolean(fonts.available),
    local: true,
    isMock: false,
    reason: fonts.available ? undefined : String(fonts.error || '未发现可用的中日韩系统字体'),
  });

  const system = Object.fromEntries(
    Object.entries(systemCapabilities)
      .filter(([, value]) => ['string', 'number', 'boolean'].includes(typeof value))
      .map(([key, value]) => [key, value as string | number | boolean]),
  );
  return { providers, system };
}

function linesToMapping(value: string): Record<string, string> {
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

function serializeProjectSettings(settings: Partial<ProjectSettings>): Record<string, unknown> {
  const { glossary, characterNames, preserveTree } = settings;
  const portable: Record<string, unknown> = { ...settings };
  delete portable.apiKeyConfigured;
  delete portable.glossary;
  delete portable.characterNames;
  delete portable.preserveTree;
  return {
    ...portable,
    ...(typeof glossary === 'string' ? { glossary: linesToMapping(glossary) } : {}),
    ...(typeof characterNames === 'string' ? { characterNames: linesToMapping(characterNames) } : {}),
    ...(preserveTree === undefined ? {} : { export: { preserveTree } }),
  };
}

export interface CreateProjectInput {
  name: string;
  outputPath?: string;
}

export interface UpdateRegionInput extends Partial<Region> {
  expectedRevision: number;
}

export type G4RegionFields = Pick<
  Region,
  | 'x'
  | 'y'
  | 'width'
  | 'height'
  | 'rotation'
  | 'type'
  | 'direction'
  | 'order'
  | 'paragraphGroupId'
  | 'rubyParentId'
  | 'contentDisposition'
>;

export type G4RegionPatch = Partial<G4RegionFields>;

export const api = {
  baseUrl: API_BASE,

  async health(): Promise<{ status: string; version?: string }> {
    return unwrap(await request('/health'));
  },

  async getCapabilities(): Promise<AppCapabilities> {
    return normalizeCapabilities(await request('/config'));
  },

  async listProjects(): Promise<ProjectSummary[]> {
    return listFrom(await request('/projects'), 'projects');
  },

  async listFinalReviewBatches(): Promise<FinalReviewBatchSummary[]> {
    return listFrom(await request('/final-review-batches'), 'batches');
  },

  async createFinalReviewBatch(input: {
    name: string;
    sourceProjectIds: string[];
    outputPath?: string | null;
    expectedItemCount: number;
  }): Promise<FinalReviewBatch> {
    return unwrap(await request('/final-review-batches', { method: 'POST', body: input }));
  },

  async openFinalReviewBatch(manifestPath: string): Promise<FinalReviewBatch> {
    return unwrap(await request('/final-review-batches/open', {
      method: 'POST',
      body: { manifestPath },
    }));
  },

  async getFinalReviewBatch(batchId: string): Promise<FinalReviewBatch> {
    return unwrap(await request(`/final-review-batches/${encodeURIComponent(batchId)}`));
  },

  async listFinalReviewItems(batchId: string): Promise<FinalReviewItem[]> {
    return listFrom(
      await request(`/final-review-batches/${encodeURIComponent(batchId)}/items`),
      'items',
    );
  },

  async updateFinalReviewItem(
    itemId: string,
    patch: FinalReviewItemPatch,
  ): Promise<FinalReviewSaveResult> {
    const payload = await request(`/final-review-items/${encodeURIComponent(itemId)}`, {
      method: 'PATCH',
      headers: { 'If-Match': String(patch.expectedRevision) },
      body: patch,
    });
    return authoritativeFinalReviewResult<FinalReviewSaveResult>(payload, '终审保存');
  },

  finalReviewThumbnailUrl(itemId: string, artifactRevision: number): string {
    const query = `?artifactRevision=${encodeURIComponent(String(artifactRevision))}`;
    return `${API_BASE}/final-review-items/${encodeURIComponent(itemId)}/thumbnail${query}`;
  },

  finalReviewArtifactUrl(
    itemId: string,
    kind: FinalReviewEvidenceKind,
    artifactRevision: number,
  ): string {
    const query = `?artifactRevision=${encodeURIComponent(String(artifactRevision))}`;
    return `${API_BASE}/final-review-items/${encodeURIComponent(itemId)}/artifacts/${encodeURIComponent(kind)}${query}`;
  },

  finalReviewContentUrl(itemId: string, artifactRevision: number): string {
    return this.finalReviewArtifactUrl(itemId, 'final', artifactRevision);
  },

  async refreshFinalReviewItem(
    itemId: string,
    input: FinalReviewRefreshRequest,
  ): Promise<FinalReviewRefreshResult> {
    const payload = await request(`/final-review-items/${encodeURIComponent(itemId)}/refresh`, {
      method: 'POST',
      headers: { 'If-Match': String(input.expectedRevision) },
      body: input,
    });
    return authoritativeFinalReviewResult<FinalReviewRefreshResult>(payload, '终审刷新');
  },

  async beginFinalReviewRepair(
    itemId: string,
    input: FinalReviewRepairRequest,
  ): Promise<FinalReviewRepairResult> {
    return unwrap(await request(`/final-review-items/${encodeURIComponent(itemId)}/repair`, {
      method: 'POST',
      headers: { 'If-Match': String(input.expectedRevision) },
      body: input,
    }));
  },

  async exportApprovedFinalReviewItems(
    batchId: string,
    input: FinalReviewExportRequest,
  ): Promise<FinalReviewExportResult> {
    return unwrap(await request(`/final-review-batches/${encodeURIComponent(batchId)}/export`, {
      method: 'POST',
      body: input,
    }));
  },

  async createProject(input: CreateProjectInput): Promise<Project> {
    return unwrap(await request('/projects', { method: 'POST', body: input }));
  },

  async openProject(manifestPath: string): Promise<Project> {
    return unwrap(
      await request('/projects/open', { method: 'POST', body: { manifestPath } }),
    );
  },

  async getProject(projectId: string): Promise<Project> {
    return unwrap(await request(`/projects/${encodeURIComponent(projectId)}`));
  },

  async updateProject(
    projectId: string,
    patch: { settings?: Partial<ProjectSettings>; name?: string; expectedRevision: number },
  ): Promise<Project> {
    return unwrap(
      await request(`/projects/${encodeURIComponent(projectId)}`, {
        method: 'PATCH',
        headers: { 'If-Match': String(patch.expectedRevision) },
        body: {
          ...patch,
          settings: patch.settings ? serializeProjectSettings(patch.settings) : undefined,
        },
      }),
    );
  },

  async listImages(projectId: string): Promise<ImageAsset[]> {
    return listFrom(
      await request(`/projects/${encodeURIComponent(projectId)}/images`),
      'images',
    );
  },

  async uploadImages(projectId: string, files: File[]): Promise<ImageAsset[]> {
    const form = new FormData();
    const folderUpload = files.some(
      (file) => Boolean((file as File & { webkitRelativePath?: string }).webkitRelativePath),
    );
    files.forEach((file) => {
      const relativePath = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
      form.append('files', file, relativePath || file.name);
    });
    form.append(
      'relative_paths',
      JSON.stringify(
        files.map(
          (file) => (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name,
        ),
      ),
    );
    if (folderUpload) form.append('strip_common_root', 'true');
    return listFrom(
      await request(`/projects/${encodeURIComponent(projectId)}/images/upload`, {
        method: 'POST',
        rawBody: form,
      }),
      'images',
    );
  },

  async importLocalImages(projectId: string, paths: string[]): Promise<ImageAsset[]> {
    return listFrom(
      await request(`/projects/${encodeURIComponent(projectId)}/images/import-local`, {
        method: 'POST',
        body: { paths },
      }),
      'images',
    );
  },

  thumbnailUrl(imageId: string): string {
    return `${API_BASE}/images/${encodeURIComponent(imageId)}/thumbnail`;
  },

  contentUrl(
    imageId: string,
    variant: 'original' | 'preprocessed' | 'erased' | 'typeset' = 'original',
    revision?: number,
  ): string {
    if (variant === 'original') return `${API_BASE}/images/${encodeURIComponent(imageId)}/content`;
    const stage = variant === 'preprocessed'
      ? 'preprocessed'
      : variant === 'erased'
        ? 'inpainted'
        : 'typeset';
    const version = revision === undefined ? '' : `?v=${encodeURIComponent(String(revision))}`;
    return `${API_BASE}/images/${encodeURIComponent(imageId)}/generated/${stage}${version}`;
  },

  maskUrl(imageId: string, revision?: number): string {
    const version = revision === undefined ? '' : `?v=${encodeURIComponent(String(revision))}`;
    return `${API_BASE}/images/${encodeURIComponent(imageId)}/generated/mask${version}`;
  },

  async listRegions(imageId: string): Promise<Region[]> {
    return listFrom(
      await request(`/images/${encodeURIComponent(imageId)}/regions`),
      'regions',
    );
  },

  async listPageGenerations(imageId: string): Promise<PageGeneration[]> {
    return listFrom(
      await request(`/images/${encodeURIComponent(imageId)}/page-generations`),
      'pageGenerations',
    );
  },

  async listPageLineageEvents(generationId: string): Promise<PageLineageEvent[]> {
    return listFrom(
      await request(`/page-generations/${encodeURIComponent(generationId)}/events`),
      'events',
    );
  },

  async acceptRegionsGate(
    imageId: string,
    observedRegionChecksum: string,
    expectedRevision: number,
    lineage: MutationLineageContext,
  ): Promise<PageGateResult> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/regions`, {
        method: 'PATCH',
        body: {
          decision: 'accept',
          reason: 'all-region-decisions-reviewed',
          observedRegionChecksum,
          expectedRevision,
          lineage,
        },
      }),
    );
  },

  async getBackgroundGateContext(imageId: string): Promise<BackgroundGateContext> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/background`),
    );
  },

  async updateBackgroundClassification(
    regionId: string,
    classification: {
      category: BackgroundCategory;
      confidence: number;
      rationaleCodes: BackgroundRationaleCode[];
      expectedRevision: number;
      expectedImageRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<Region> {
    return unwrap(
      await request(`/regions/${encodeURIComponent(regionId)}/background-classification`, {
        method: 'PATCH',
        body: classification,
      }),
    );
  },

  async acceptBackgroundGate(
    imageId: string,
    reason: 'all-eligible-backgrounds-reviewed' | 'no-eligible-regions',
    observedBackgroundChecksum: string,
    expectedRevision: number,
    lineage: MutationLineageContext,
  ): Promise<PageGateResult> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/background`, {
        method: 'PATCH',
        body: {
          decision: 'accept',
          reason,
          observedBackgroundChecksum,
          expectedRevision,
          lineage,
        },
      }),
    );
  },

  async getOCRGateContext(imageId: string): Promise<OCRGateContext> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/ocr`),
    );
  },

  async updateOCRSourceReview(
    regionId: string,
    review: {
      sourceText: string;
      sourceMode: OCRSourceMode;
      selectedAttemptId: string;
      qcChecks: OCRQCCheck[];
      expectedRevision: number;
      expectedImageRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<Region> {
    return unwrap(
      await request(`/regions/${encodeURIComponent(regionId)}/ocr-source-review`, {
        method: 'PATCH',
        body: review,
      }),
    );
  },

  async acceptOCRGate(
    imageId: string,
    reason: 'all-translatable-source-text-reviewed' | 'no-translatable-regions',
    observedOCRChecksum: string,
    expectedRevision: number,
    lineage: MutationLineageContext,
  ): Promise<PageGateResult> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/ocr`, {
        method: 'PATCH',
        body: {
          decision: 'accept',
          reason,
          observedOCRChecksum,
          expectedRevision,
          lineage,
        },
      }),
    );
  },

  async getMaskGateContext(imageId: string): Promise<MaskGateContext> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/mask`),
    );
  },

  async updateMaskDraft(
    imageId: string,
    update: {
      regions: MaskDraftRegion[];
      expectedRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<MaskGateContext> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/mask/draft`, {
        method: 'PATCH',
        body: update,
      }),
    );
  },

  maskArtifactUrl(imageId: string, artifactId: string): string {
    return `${API_BASE}/images/${encodeURIComponent(imageId)}/page-gates/mask/artifacts/${encodeURIComponent(artifactId)}`;
  },

  async reviewMaskGate(
    imageId: string,
    review: {
      decision: 'accept' | 'reject' | 'not-applicable';
      reason:
        | 'complete-and-no-collateral'
        | 'coverage-incomplete'
        | 'collateral-damage'
        | 'coverage-and-collateral-failed'
        | 'no-eligible-regions';
      selectedArtifactId?: string;
      observedMaskChecksum?: string;
      coverageChecks: Array<MaskCheckResult<MaskCoverageCheck>>;
      collateralChecks: Array<MaskCheckResult<MaskCollateralCheck>>;
      expectedRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<PageGateResult> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/mask`, {
        method: 'PATCH',
        body: review,
      }),
    );
  },

  async getCleanPlateGateContext(imageId: string): Promise<CleanPlateGateContext> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/clean-plate`),
    );
  },

  cleanPlateCandidateUrl(imageId: string, candidateId: string): string {
    return `${API_BASE}/images/${encodeURIComponent(imageId)}/page-gates/clean-plate/candidates/${encodeURIComponent(candidateId)}`;
  },

  async reviewCleanPlateGate(
    imageId: string,
    review: {
      decision: 'accept' | 'reject' | 'not-applicable';
      reason: CleanPlateReviewReason;
      candidateId?: string;
      observedCandidateChecksum?: string;
      observedWidth?: number;
      observedHeight?: number;
      checks: Array<MaskCheckResult<CleanPlateCheck>>;
      expectedRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<PageGateResult> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/clean-plate`, {
        method: 'PATCH',
        body: review,
      }),
    );
  },

  async setCleanPlateFallback(
    imageId: string,
    update: {
      enabled: boolean;
      reason: 'all-ai-candidates-rejected' | 'resume-ai-candidates';
      expectedRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<CleanPlateGateContext> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/clean-plate/fallback`, {
        method: 'PATCH',
        body: update,
      }),
    );
  },

  async getTranslationGateContext(imageId: string): Promise<TranslationGateContext> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/translation`),
    );
  },

  async createTranslationCandidate(
    imageId: string,
    candidate: {
      regionId: string;
      translationText: string;
      originKind: Exclude<TranslationOriginKind, 'model'>;
      observedG8Checksum: string;
      observedSourceTextChecksum: string;
      observedContextChecksum: string;
      observedTranslationStateChecksum: string;
      expectedRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<TranslationGateContext> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/translation/candidates`, {
        method: 'POST',
        body: candidate,
      }),
    );
  },

  async reviewTranslationCandidate(
    imageId: string,
    candidateId: string,
    review: {
      decision: 'accept' | 'reject';
      reason: TranslationReviewReason;
      observedCandidateChecksum: string;
      observedSourceTextChecksum: string;
      observedContextChecksum: string;
      observedG8Checksum: string;
      checks: Array<MaskCheckResult<TranslationQCCheck>>;
      qcFlags: TranslationQCFlag[];
      expectedRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<PageGateResult> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/translation/candidates/${encodeURIComponent(candidateId)}`, {
        method: 'PATCH',
        body: review,
      }),
    );
  },

  async reviewTranslationGate(
    imageId: string,
    review: {
      decision: 'accept' | 'not-applicable';
      observedTranslationStateChecksum: string;
      expectedRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<PageGateResult> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/translation`, {
        method: 'PATCH',
        body: review,
      }),
    );
  },

  async getTypesetGateContext(imageId: string): Promise<TypesetGateContext> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/typeset`),
    );
  },

  typesetCandidateUrl(imageId: string, candidateId: string): string {
    return `${API_BASE}/images/${encodeURIComponent(imageId)}/page-gates/typeset/candidates/${encodeURIComponent(candidateId)}`;
  },

  async reviewTypesetCandidate(
    imageId: string,
    candidateId: string,
    review: {
      decision: 'accept' | 'reject';
      reason: TypesetReviewReason;
      observedCandidateChecksum: string;
      observedRouteChecksum: string;
      observedStyleChecksum: string;
      observedLayoutChecksum: string;
      observedTranslationTerminalChecksum: string;
      observedCleanPlateChecksum: string;
      observedWidth: number;
      observedHeight: number;
      observedRenderScale: number;
      checks: Array<MaskCheckResult<TypesetCheck>>;
      expectedRevision: number;
      lineage: MutationLineageContext;
    },
  ): Promise<PageGateResult> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/page-gates/typeset/candidates/${encodeURIComponent(candidateId)}`, {
        method: 'PATCH',
        body: review,
      }),
    );
  },

  async reviewImage(
    imageId: string,
    reviewState: ReviewState,
    expectedRevision: number,
  ): Promise<ImageAsset> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/review`, {
        method: 'PATCH',
        headers: { 'If-Match': String(expectedRevision) },
        body: { reviewState, expectedRevision },
      }),
    );
  },

  async reviewImageStage(
    imageId: string,
    stage: VisualStage,
    state: StageReviewState,
    expectedRevision: number,
    observation?: Pick<StageReviewObservation, 'artifactChecksum' | 'maskChecksum'>,
  ): Promise<ImageAsset> {
    return unwrap(
      await request(
        `/images/${encodeURIComponent(imageId)}/stage-reviews/${encodeURIComponent(stage)}`,
        {
          method: 'PATCH',
          headers: { 'If-Match': String(expectedRevision) },
          body: {
            state,
            expectedRevision,
            ...(observation ? {
              observedArtifactChecksum: observation.artifactChecksum,
              ...(observation.maskChecksum
                ? { observedMaskChecksum: observation.maskChecksum }
                : {}),
            } : {}),
          },
        },
      ),
    );
  },

  async selectInpaintCandidate(
    imageId: string,
    candidateId: string,
    expectedRevision: number,
  ): Promise<ImageAsset> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/inpaint-candidate`, {
        method: 'PATCH',
        headers: { 'If-Match': String(expectedRevision) },
        body: { candidateId, expectedRevision },
      }),
    );
  },

  async reviewSelectedInpaintAiCandidate(
    imageId: string,
    state: 'rejected' | 'pending',
    expectedRevision: number,
  ): Promise<ImageAsset> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/inpaint-ai-candidate-review`, {
        method: 'PATCH',
        headers: { 'If-Match': String(expectedRevision) },
        body: { state, expectedRevision },
      }),
    );
  },

  async setInpaintClassicalFallback(
    imageId: string,
    state: 'approved' | 'pending',
    expectedRevision: number,
    options: {
      reason?: 'ai-visible-artifacts';
    } = {},
  ): Promise<ImageAsset> {
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/inpaint-classical-fallback`, {
        method: 'PATCH',
        headers: { 'If-Match': String(expectedRevision) },
        body: {
          state,
          ...(options.reason ? { reason: options.reason } : {}),
          expectedRevision,
        },
      }),
    );
  },

  async createRegion(imageId: string, region: Region): Promise<Region> {
    const {
      x,
      y,
      width,
      height,
      rotation,
      sourceText,
      translationText,
      type,
      direction,
      order,
      paragraphGroupId,
      rubyParentId,
      contentDisposition,
      confidence,
      ignored,
      confirmed,
      style,
      repair,
    } = region;
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/regions`, {
        method: 'POST',
        body: {
          x,
          y,
          width,
          height,
          rotation,
          sourceText,
          translationText,
          type,
          direction,
          order,
          paragraphGroupId,
          rubyParentId,
          contentDisposition,
          confidence,
          ignored,
          confirmed,
          style,
          repair,
        },
      }),
    );
  },

  async createG4Region(
    imageId: string,
    region: G4RegionFields,
    expectedImageRevision: number,
    lineage: MutationLineageContext,
  ): Promise<Region> {
    const {
      x,
      y,
      width,
      height,
      rotation,
      type,
      direction,
      order,
      paragraphGroupId,
      rubyParentId,
      contentDisposition,
    } = region;
    return unwrap(
      await request(`/images/${encodeURIComponent(imageId)}/regions`, {
        method: 'POST',
        body: {
          x,
          y,
          width,
          height,
          rotation,
          type,
          direction,
          order,
          paragraphGroupId,
          rubyParentId,
          contentDisposition,
          expectedImageRevision,
          lineage,
        },
      }),
    );
  },

  async updateRegion(regionId: string, patch: UpdateRegionInput): Promise<Region> {
    const {
      expectedRevision,
      x,
      y,
      width,
      height,
      rotation,
      sourceText,
      translationText,
      type,
      direction,
      order,
      paragraphGroupId,
      rubyParentId,
      contentDisposition,
      confidence,
      ignored,
      confirmed,
      style,
      repair,
    } = patch;
    const allowedPatch = Object.fromEntries(
      Object.entries({
        x,
        y,
        width,
        height,
        rotation,
        sourceText,
        translationText,
        type,
        direction,
        order,
        paragraphGroupId,
        rubyParentId,
        contentDisposition,
        confidence,
        ignored,
        confirmed,
        style,
        repair,
        expectedRevision,
      }).filter(([, value]) => value !== undefined),
    );
    return unwrap(
      await request(`/regions/${encodeURIComponent(regionId)}`, {
        method: 'PATCH',
        headers: { 'If-Match': String(expectedRevision) },
        body: allowedPatch,
      }),
    );
  },

  async updateG4Region(
    regionId: string,
    patch: G4RegionPatch,
    expectedRevision: number,
    expectedImageRevision: number,
    lineage: MutationLineageContext,
  ): Promise<Region> {
    const allowedPatch = Object.fromEntries(
      Object.entries({
        x: patch.x,
        y: patch.y,
        width: patch.width,
        height: patch.height,
        rotation: patch.rotation,
        type: patch.type,
        direction: patch.direction,
        order: patch.order,
        paragraphGroupId: patch.paragraphGroupId,
        rubyParentId: patch.rubyParentId,
        contentDisposition: patch.contentDisposition,
      }).filter(([, value]) => value !== undefined),
    );
    return unwrap(
      await request(`/regions/${encodeURIComponent(regionId)}`, {
        method: 'PATCH',
        headers: { 'If-Match': String(expectedRevision) },
        body: {
          ...allowedPatch,
          expectedRevision,
          expectedImageRevision,
          lineage,
        },
      }),
    );
  },

  async deleteRegion(regionId: string, expectedRevision: number): Promise<void> {
    const query = new URLSearchParams({ expectedRevision: String(expectedRevision) });
    await request(`/regions/${encodeURIComponent(regionId)}?${query}`, {
      method: 'DELETE',
      headers: { 'If-Match': String(expectedRevision) },
    });
  },

  async deleteG4Region(
    regionId: string,
    expectedRevision: number,
    expectedImageRevision: number,
    lineage: MutationLineageContext,
  ): Promise<void> {
    await request(`/regions/${encodeURIComponent(regionId)}`, {
      method: 'DELETE',
      headers: { 'If-Match': String(expectedRevision) },
      body: { expectedRevision, expectedImageRevision, lineage },
    });
  },

  async reorderG4Regions(
    imageId: string,
    regionIds: string[],
    expectedImageRevision: number,
    lineage: MutationLineageContext,
  ): Promise<Region[]> {
    return listFrom(
      await request(`/images/${encodeURIComponent(imageId)}/regions/reorder`, {
        method: 'POST',
        body: {
          regionIds,
          expectedImageRevision,
          lineage,
        },
      }),
      'regions',
    );
  },

  async listJobs(projectId: string): Promise<Job[]> {
    const query = new URLSearchParams({ projectId });
    return listFrom(await request(`/jobs?${query}`), 'jobs');
  },

  async startJob(
    projectId: string,
    kind: Exclude<Job['kind'], 'export'>,
    input: BatchRequest,
  ): Promise<Job> {
    return unwrap(
      await request(`/projects/${encodeURIComponent(projectId)}/${kind}`, {
        method: 'POST',
        body: input,
      }),
    );
  },

  async exportProject(
    projectId: string,
    input: { imageIds: string[]; regionIds?: string[]; options: ExportOptions },
  ): Promise<Job> {
    return unwrap(
      await request(`/projects/${encodeURIComponent(projectId)}/export`, {
        method: 'POST',
        body: input,
      }),
    );
  },

  async jobAction(jobId: string, action: 'pause' | 'resume' | 'cancel' | 'retry'): Promise<Job> {
    return unwrap(
      await request(`/jobs/${encodeURIComponent(jobId)}/${action}`, { method: 'POST' }),
    );
  },

  async setSessionCredential(
    _provider: string,
    apiKey: string,
    baseUrl?: string,
    model?: string,
  ): Promise<{ configured: boolean; capabilities: AppCapabilities }> {
    const response = await request('/config/translation/openai-session', {
      method: 'PUT',
      body: { apiKey, baseUrl: baseUrl || undefined, model: model || undefined },
    });
    const config = unwrap<Record<string, unknown>>(response);
    const groups = capabilityRecord(config.providers);
    const translation = capabilityRecord(groups.translation);
    const openai = capabilityRecord(translation['openai-compatible']);
    return {
      configured: Boolean(openai.available),
      capabilities: normalizeCapabilities(response),
    };
  },
};

export type ApiClient = typeof api;
