import { create } from 'zustand';

import { ApiError, api } from '../api/client';
import type {
  FinalReviewBatch,
  FinalReviewBatchSummary,
  FinalReviewExportRequest,
  FinalReviewExportResult,
  FinalReviewIssueCode,
  FinalReviewItem,
  FinalReviewRepairResult,
  FinalReviewStats,
  FinalReviewVerdict,
} from './types';
import type { LineageActor } from '../types';

export type FinalReviewStatusFilter = 'all' | FinalReviewVerdict;

interface FinalReviewDraft {
  verdict: FinalReviewVerdict;
  issueCodes: FinalReviewIssueCode[];
  feedback: string;
}

export interface FinalReviewRepairContext {
  batchId: string;
  itemId: string;
  issueCodes: FinalReviewIssueCode[];
  feedback: string;
  sourceProjectId: string;
  sourceImageId: string;
  repairProjectId: string;
  repairImageId: string;
  pageGenerationId: string;
  runId: string;
  itemRevision: number;
  batchRevision: number;
  artifactRevision: number;
  nextSequence: number;
  parameterSetId: string;
  parameterSetHash: string;
}

export type FinalReviewOperation = 'load' | 'save' | 'refresh' | 'repair' | 'export' | null;

interface FinalReviewState {
  batches: FinalReviewBatchSummary[];
  batch: FinalReviewBatch | null;
  items: FinalReviewItem[];
  activeItemId: string | null;
  draft: FinalReviewDraft | null;
  statusFilter: FinalReviewStatusFilter;
  issueFilter: FinalReviewIssueCode | 'all';
  search: string;
  loading: boolean;
  saving: boolean;
  refreshing: boolean;
  error: string;
  conflict: boolean;
  lastSavedAt: string | null;
  exportResult: FinalReviewExportResult | null;
  exporting: boolean;
  repairContext: FinalReviewRepairContext | null;
  operation: FinalReviewOperation;
  conflictDraft: FinalReviewDraft | null;

  loadBatches: () => Promise<void>;
  loadBatch: (batchId: string, preferredItemId?: string) => Promise<boolean>;
  setStatusFilter: (value: FinalReviewStatusFilter) => void;
  setIssueFilter: (value: FinalReviewState['issueFilter']) => void;
  setSearch: (value: string) => void;
  selectItem: (itemId: string, confirmDiscard?: boolean) => boolean;
  navigate: (direction: -1 | 1) => boolean;
  updateDraft: (patch: Partial<FinalReviewDraft>) => void;
  toggleIssue: (code: FinalReviewIssueCode) => void;
  save: (moveNext?: boolean) => Promise<boolean>;
  refreshActive: () => Promise<boolean>;
  exportApproved: (input: Omit<FinalReviewExportRequest, 'expectedBatchRevision' | 'actor'>) => Promise<boolean>;
  discardDraft: () => void;
  beginRepair: () => Promise<FinalReviewRepairContext | null>;
  finishRepairNavigation: (clearContext?: boolean) => void;
  reloadConflict: () => Promise<boolean>;
  clearRepairContext: () => void;
}

const REPAIR_STORAGE_KEY = 'manga-localizer-final-review-repair';
const REPAIR_PARAMETER_SET_ID = 'final-review-repair-v1';
const REPAIR_PARAMETER_SET_HASH = '9ede4cd795967a3ec5e3de3ba544b677aabb589b4490c2f8cecc655808bab338';
let requestSequence = 0;
let fallbackSessionId = `final-review-${Date.now().toString(36)}`;

export function finalReviewActor(): LineageActor {
  try {
    const key = 'manga-localizer-lineage-session';
    const stored = window.sessionStorage.getItem(key);
    if (stored) fallbackSessionId = stored;
    else window.sessionStorage.setItem(key, fallbackSessionId);
  } catch {
    // The stable in-memory id still identifies this UI session.
  }
  return { actorKind: 'human', sessionId: fallbackSessionId, operationSource: 'ui' };
}

export function finalReviewLegacyReviewed(item: FinalReviewItem | undefined | null): boolean {
  return Boolean(item && item.verdict !== 'pending' && (
    item.strictEvidence === false || (item.strictEvidence === undefined && (item.formatVersion ?? 1) < 2)
  ));
}

export function finalReviewLegacyApproved(item: FinalReviewItem | undefined | null): boolean {
  return Boolean(item?.verdict === 'approved' && finalReviewLegacyReviewed(item));
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return '发生未知错误';
}

function mutationOutcomeRequiresReload(error: unknown): boolean {
  if (!(error instanceof ApiError)) return true;
  return error.status === 0 || error.status === 408 || error.status === 409 || error.status >= 500;
}

function mutationFailureMessage(error: unknown): string {
  const message = errorMessage(error);
  if (!mutationOutcomeRequiresReload(error) || (error instanceof ApiError && error.status === 409)) {
    return message;
  }
  return `操作结果未知；请求可能已在服务端完成。请载入最新版本确认后再继续。${message}`;
}

function mutationResponseCannotBeAppliedMessage(operation: 'save' | 'refresh' | 'repair' | 'export'): string {
  const label = { save: '终审保存', refresh: '终审刷新', repair: '终审修复', export: '终审导出' }[operation];
  return `${label}已在服务端完成，但当前批次状态已变化，无法安全应用响应。请载入最新版本确认后再继续。`;
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function canonicalAbsolutePosixPath(value: unknown): string | null {
  if (typeof value !== 'string' || !value.startsWith('/') || value.includes('\0')) return null;
  const segments: string[] = [];
  for (const segment of value.split('/')) {
    if (!segment || segment === '.') continue;
    if (segment === '..') segments.pop();
    else segments.push(segment);
  }
  return `/${segments.join('/')}`;
}

function exportManifestPath(outputPath: string): string {
  return outputPath === '/' ? '/manifest.json' : `${outputPath}/manifest.json`;
}

function canonicalIssueCodes(codes: FinalReviewIssueCode[]): FinalReviewIssueCode[] {
  return [...new Set(codes)].sort();
}

function normalizedSaveDraft(draft: FinalReviewDraft): FinalReviewDraft {
  return {
    verdict: draft.verdict,
    issueCodes: draft.verdict === 'issues' ? canonicalIssueCodes(draft.issueCodes) : [],
    feedback: draft.verdict === 'pending' ? '' : draft.feedback.trim(),
  };
}

function sameIssueCodes(left: FinalReviewIssueCode[], right: FinalReviewIssueCode[]): boolean {
  return left.length === right.length && left.every((code, index) => code === right[index]);
}

function stableSerialize(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableSerialize).join(',')}]`;
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => (
      `${JSON.stringify(key)}:${stableSerialize(record[key])}`
    )).join(',')}}`;
  }
  return JSON.stringify(value) ?? 'undefined';
}

const SHA256_CONSTANTS = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function pythonJsonString(value: string): string {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (character) => (
    `\\u${character.charCodeAt(0).toString(16).padStart(4, '0')}`
  ));
}

function pythonCanonicalJson(value: unknown, ancestors = new Set<object>()): string | null {
  if (value === null) return 'null';
  if (typeof value === 'string') return pythonJsonString(value);
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return Number.isSafeInteger(value) ? String(value) : null;
  if (typeof value !== 'object') return null;
  if (ancestors.has(value)) return null;
  ancestors.add(value);
  let serialized: string | null;
  if (Array.isArray(value)) {
    const entries = value.map((entry) => pythonCanonicalJson(entry, ancestors));
    serialized = entries.some((entry) => entry === null)
      ? null
      : `[${entries.join(',')}]`;
  } else {
    const record = value as Record<string, unknown>;
    const entries = Object.keys(record).sort().map((key) => {
      const encoded = pythonCanonicalJson(record[key], ancestors);
      return encoded === null ? null : `${pythonJsonString(key)}:${encoded}`;
    });
    serialized = entries.some((entry) => entry === null)
      ? null
      : `{${entries.join(',')}}`;
  }
  ancestors.delete(value);
  return serialized;
}

function rotateRight(value: number, shift: number): number {
  return (value >>> shift) | (value << (32 - shift));
}

function sha256Hex(value: string): string {
  const source = new TextEncoder().encode(value);
  const paddedLength = Math.ceil((source.length + 9) / 64) * 64;
  const padded = new Uint8Array(paddedLength);
  padded.set(source);
  padded[source.length] = 0x80;
  const bitLength = source.length * 8;
  const view = new DataView(padded.buffer);
  view.setUint32(paddedLength - 8, Math.floor(bitLength / 0x100000000), false);
  view.setUint32(paddedLength - 4, bitLength >>> 0, false);
  const hash = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const words = new Uint32Array(64);
  for (let offset = 0; offset < paddedLength; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      words[index] = view.getUint32(offset + index * 4, false);
    }
    for (let index = 16; index < 64; index += 1) {
      const previous15 = words[index - 15]!;
      const previous2 = words[index - 2]!;
      const sigma0 = rotateRight(previous15, 7) ^ rotateRight(previous15, 18) ^ (previous15 >>> 3);
      const sigma1 = rotateRight(previous2, 17) ^ rotateRight(previous2, 19) ^ (previous2 >>> 10);
      words[index] = (words[index - 16]! + sigma0 + words[index - 7]! + sigma1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = hash;
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotateRight(e!, 6) ^ rotateRight(e!, 11) ^ rotateRight(e!, 25);
      const choice = (e! & f!) ^ (~e! & g!);
      const temporary1 = (h! + sum1 + choice + SHA256_CONSTANTS[index]! + words[index]!) >>> 0;
      const sum0 = rotateRight(a!, 2) ^ rotateRight(a!, 13) ^ rotateRight(a!, 22);
      const majority = (a! & b!) ^ (a! & c!) ^ (b! & c!);
      const temporary2 = (sum0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d! + temporary1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temporary1 + temporary2) >>> 0;
    }
    hash[0] = (hash[0]! + a!) >>> 0;
    hash[1] = (hash[1]! + b!) >>> 0;
    hash[2] = (hash[2]! + c!) >>> 0;
    hash[3] = (hash[3]! + d!) >>> 0;
    hash[4] = (hash[4]! + e!) >>> 0;
    hash[5] = (hash[5]! + f!) >>> 0;
    hash[6] = (hash[6]! + g!) >>> 0;
    hash[7] = (hash[7]! + h!) >>> 0;
  }
  return Array.from(hash, (word) => word.toString(16).padStart(8, '0')).join('');
}

export function finalReviewCanonicalDigest(value: unknown): string | null {
  const canonical = pythonCanonicalJson(value);
  return canonical === null ? null : sha256Hex(canonical);
}

function saveResponseMatchesContract(
  item: FinalReviewItem,
  expectedItem: FinalReviewItem,
  submitted: FinalReviewDraft,
  historyCreated: boolean,
): boolean {
  const currentCodes = expectedItem.verdict === 'issues'
    ? canonicalIssueCodes(expectedItem.issueCodes)
    : [];
  const shouldCreateHistory = expectedItem.verdict !== submitted.verdict
    || !sameIssueCodes(currentCodes, submitted.issueCodes)
    || expectedItem.feedback !== submitted.feedback;
  const reviewedAtMatches = submitted.verdict === 'pending'
    ? item.reviewedAt == null
    : historyCreated
      ? nonEmptyString(item.reviewedAt)
      : item.reviewedAt === expectedItem.reviewedAt;
  return historyCreated === shouldCreateHistory
    && item.verdict === submitted.verdict
    && sameIssueCodes(item.issueCodes, submitted.issueCodes)
    && item.feedback === submitted.feedback
    && reviewedAtMatches
    && item.position === expectedItem.position
    && item.sourceProjectId === expectedItem.sourceProjectId
    && item.sourceProjectName === expectedItem.sourceProjectName
    && item.sourceImageId === expectedItem.sourceImageId
    && item.sourceRelativePath === expectedItem.sourceRelativePath
    && item.finalVariant === expectedItem.finalVariant
    && item.artifactChecksum === expectedItem.artifactChecksum
    && item.thumbnailChecksum === expectedItem.thumbnailChecksum
    && item.currentArtifactStale === expectedItem.currentArtifactStale
    && item.strictEvidence === expectedItem.strictEvidence
    && item.formatVersion === expectedItem.formatVersion
    && item.contentUrl === expectedItem.contentUrl
    && item.thumbnailUrl === expectedItem.thumbnailUrl
    && item.evidenceDigest === expectedItem.evidenceDigest
    && stableSerialize(item.evidence) === stableSerialize(expectedItem.evidence);
}

const EVIDENCE_KINDS = ['original', 'quality', 'mask', 'clean', 'final'] as const;
const PUBLIC_DESCRIPTOR_KEYS = [
  'artifactRevision', 'availability', 'checksum', 'generationId', 'grid', 'kind',
  'producerId', 'producerRevisionId', 'relativePath', 'resolutionDigest',
  'terminalChecksum', 'terminalId', 'terminalRevisionId', 'url',
].sort();
const ISSUE_CODES: ReadonlySet<string> = new Set([
  'typesetting', 'translation', 'mask', 'ai_inpaint', 'missing_text', 'preprocess', 'other',
]);

function validStrictEvidence(item: FinalReviewItem): boolean {
  if (!item.evidence || typeof item.evidence !== 'object') return false;
  const keys = Object.keys(item.evidence).sort();
  const expectedKeys = [...EVIDENCE_KINDS].sort();
  if (keys.length !== EVIDENCE_KINDS.length
    || !expectedKeys.every((kind, index) => kind === keys[index])) return false;
  const generationId = item.evidence.original.generationId;
  if (!nonEmptyString(generationId)) return false;
  const descriptorsValid = EVIDENCE_KINDS.every((kind) => {
    const descriptor = item.evidence[kind];
    const descriptorKeys = descriptor ? Object.keys(descriptor).sort() : [];
    if (!descriptor
      || descriptorKeys.length !== PUBLIC_DESCRIPTOR_KEYS.length
      || !PUBLIC_DESCRIPTOR_KEYS.every((key, index) => key === descriptorKeys[index])
      || descriptor.kind !== kind
      || !['available', 'not-applicable'].includes(descriptor.availability)
      || descriptor.artifactRevision !== item.artifactRevision
      || descriptor.generationId !== generationId
      || !nonEmptyString(descriptor.terminalId)
      || typeof descriptor.terminalChecksum !== 'string'
      || !/^[a-f0-9]{64}$/.test(descriptor.terminalChecksum)
      || !nonEmptyString(descriptor.terminalRevisionId)) return false;
    if (descriptor.availability === 'not-applicable') {
      return (kind === 'mask' || kind === 'clean')
        && descriptor.url === null
        && descriptor.checksum === null
        && descriptor.grid === null
        && descriptor.resolutionDigest === null
        && descriptor.relativePath === null
        && descriptor.producerId === null
        && descriptor.producerRevisionId === null;
    }
    const producerRevisionMayBeNull = kind === 'quality'
      || (kind === 'final' && item.finalVariant === 'preprocess');
    const terminalMustMatchArtifact = kind === 'original' || kind === 'quality'
      || (kind === 'final' && item.finalVariant === 'preprocess');
    return descriptor.url === `/api/final-review-items/${item.id}/artifacts/${kind}?artifactRevision=${item.artifactRevision}`
      && typeof descriptor.checksum === 'string' && /^[a-f0-9]{64}$/.test(descriptor.checksum)
      && typeof descriptor.resolutionDigest === 'string' && /^[a-f0-9]{64}$/.test(descriptor.resolutionDigest)
      && descriptor.resolutionDigest === finalReviewCanonicalDigest(descriptor.grid)
      && nonEmptyString(descriptor.relativePath)
      && nonEmptyString(descriptor.producerId)
      && (producerRevisionMayBeNull
        ? descriptor.producerRevisionId === null
        : nonEmptyString(descriptor.producerRevisionId))
      && (terminalMustMatchArtifact
        ? descriptor.terminalChecksum === descriptor.checksum
        : descriptor.terminalChecksum !== descriptor.checksum)
      && Boolean(descriptor.grid
        && Number.isSafeInteger(descriptor.grid.width) && descriptor.grid.width > 0
        && Number.isSafeInteger(descriptor.grid.height) && descriptor.grid.height > 0);
  });
  if (!descriptorsValid
    || item.evidence.original.availability !== 'available'
    || item.evidence.quality.availability !== 'available'
    || item.evidence.final.availability !== 'available'
    || item.evidence.mask.availability !== item.evidence.clean.availability
    || item.evidenceDigest !== finalReviewEvidenceDigest(item.evidence)) return false;
  if (item.finalVariant === 'preprocess') {
    return item.evidence.mask.availability === 'not-applicable'
      && item.evidence.final.checksum === item.evidence.quality.checksum
      && item.evidence.final.producerId === item.evidence.quality.producerId
      && item.evidence.final.producerRevisionId === null
      && item.evidence.quality.producerRevisionId === null;
  }
  return item.finalVariant === 'typeset'
    && nonEmptyString(item.evidence.final.producerRevisionId)
    && item.evidence.original.availability === 'available'
    && item.evidence.quality.availability === 'available'
    && item.evidence.final.availability === 'available';
}

export function finalReviewEvidenceDigest(evidence: FinalReviewItem['evidence']): string | null {
  const payload: Record<string, Record<string, unknown>> = {};
  for (const kind of EVIDENCE_KINDS) {
    const descriptor = evidence[kind] as unknown as Record<string, unknown>;
    const stored: Record<string, unknown> = {};
    for (const [key, value] of Object.entries(descriptor)) {
      if (key !== 'url') stored[key] = value;
    }
    payload[kind] = stored;
  }
  return finalReviewCanonicalDigest(payload);
}

function validLegacyEvidence(item: FinalReviewItem): boolean {
  if (!item.evidence || typeof item.evidence !== 'object') return false;
  const keys = Object.keys(item.evidence).sort();
  const expectedKeys = [...EVIDENCE_KINDS].sort();
  if (keys.length !== expectedKeys.length
    || !expectedKeys.every((kind, index) => kind === keys[index])) return false;
  return EVIDENCE_KINDS.every((kind) => {
    const descriptor = item.evidence[kind];
    const descriptorKeys = descriptor ? Object.keys(descriptor).sort() : [];
    if (!descriptor
      || descriptorKeys.length !== PUBLIC_DESCRIPTOR_KEYS.length
      || !PUBLIC_DESCRIPTOR_KEYS.every((key, index) => key === descriptorKeys[index])
      || descriptor.kind !== kind
      || descriptor.artifactRevision !== item.artifactRevision
      || descriptor.generationId !== null
      || descriptor.producerId !== null
      || descriptor.producerRevisionId !== null
      || descriptor.terminalId !== null
      || descriptor.terminalChecksum !== null
      || descriptor.terminalRevisionId !== null) return false;
    if (kind === 'final') {
      return descriptor.availability === 'available'
        && descriptor.url === `/api/final-review-items/${item.id}/artifacts/final?artifactRevision=${item.artifactRevision}`
        && descriptor.checksum === item.artifactChecksum
        && descriptor.resolutionDigest === null
        && nonEmptyString(descriptor.relativePath)
        && descriptor.grid === null;
    }
    return descriptor.availability === 'unavailable'
      && descriptor.url === null
      && descriptor.checksum === null
      && descriptor.grid === null
      && descriptor.resolutionDigest === null
      && descriptor.relativePath === null;
  });
}

function validFinalReviewItemResponse(item: FinalReviewItem, batchId: string): boolean {
  if (!item || !nonEmptyString(item.id) || item.batchId !== batchId
    || !Number.isSafeInteger(item.position) || item.position < 1
    || !nonEmptyString(item.sourceProjectId) || !nonEmptyString(item.sourceProjectName)
    || !nonEmptyString(item.sourceImageId) || !nonEmptyString(item.sourceRelativePath)
    || !['typeset', 'preprocess'].includes(item.finalVariant)
    || typeof item.currentArtifactStale !== 'boolean'
    || !['pending', 'approved', 'issues'].includes(item.verdict)
    || !Array.isArray(item.issueCodes) || item.issueCodes.some((code) => !ISSUE_CODES.has(code))
    || !sameIssueCodes(item.issueCodes, canonicalIssueCodes(item.issueCodes))
    || typeof item.feedback !== 'string' || item.feedback !== item.feedback.trim()
    || !Number.isSafeInteger(item.revision) || item.revision < 1
    || !Number.isSafeInteger(item.artifactRevision) || item.artifactRevision < 1
    || item.artifactRevision > item.revision
    || !/^[a-f0-9]{64}$/.test(item.artifactChecksum)
    || !/^[a-f0-9]{64}$/.test(item.thumbnailChecksum)
    || item.contentUrl !== `/api/final-review-items/${item.id}/content?artifactRevision=${item.artifactRevision}`
    || item.thumbnailUrl !== `/api/final-review-items/${item.id}/thumbnail?artifactRevision=${item.artifactRevision}`
  ) return false;
  if (item.verdict === 'pending') {
    if (item.issueCodes.length !== 0 || item.feedback !== '' || item.reviewedAt != null) return false;
  } else {
    if (!nonEmptyString(item.reviewedAt)) return false;
    if (item.verdict === 'approved' && item.issueCodes.length !== 0) return false;
    if (item.verdict === 'issues' && (item.issueCodes.length === 0
      || (item.issueCodes.includes('other') && item.feedback.length === 0))) return false;
  }
  if (item.strictEvidence) {
    return item.formatVersion === 2
      && typeof item.evidenceDigest === 'string'
      && /^[a-f0-9]{64}$/.test(item.evidenceDigest)
      && validStrictEvidence(item)
      && item.artifactChecksum === item.evidence.final.checksum;
  }
  return item.strictEvidence === false
    && item.formatVersion === 1
    && item.evidenceDigest === null
    && validLegacyEvidence(item);
}

function unchangedRevisionMatches(current: FinalReviewItem, loaded: FinalReviewItem): boolean {
  return stableSerialize({
    verdict: loaded.verdict,
    issueCodes: loaded.issueCodes,
    feedback: loaded.feedback,
    reviewedAt: loaded.reviewedAt ?? null,
    artifactRevision: loaded.artifactRevision,
    finalVariant: loaded.finalVariant,
    artifactChecksum: loaded.artifactChecksum,
    thumbnailChecksum: loaded.thumbnailChecksum,
    strictEvidence: loaded.strictEvidence,
    formatVersion: loaded.formatVersion,
    evidenceDigest: loaded.evidenceDigest ?? null,
    evidence: loaded.evidence,
    contentUrl: loaded.contentUrl,
    thumbnailUrl: loaded.thumbnailUrl,
  }) === stableSerialize({
    verdict: current.verdict,
    issueCodes: current.issueCodes,
    feedback: current.feedback,
    reviewedAt: current.reviewedAt ?? null,
    artifactRevision: current.artifactRevision,
    finalVariant: current.finalVariant,
    artifactChecksum: current.artifactChecksum,
    thumbnailChecksum: current.thumbnailChecksum,
    strictEvidence: current.strictEvidence,
    formatVersion: current.formatVersion,
    evidenceDigest: current.evidenceDigest ?? null,
    evidence: current.evidence,
    contentUrl: current.contentUrl,
    thumbnailUrl: current.thumbnailUrl,
  });
}

function unchangedFrozenEvidenceMatches(current: FinalReviewItem, loaded: FinalReviewItem): boolean {
  return stableSerialize({
    finalVariant: loaded.finalVariant,
    artifactChecksum: loaded.artifactChecksum,
    thumbnailChecksum: loaded.thumbnailChecksum,
    strictEvidence: loaded.strictEvidence,
    formatVersion: loaded.formatVersion,
    evidenceDigest: loaded.evidenceDigest ?? null,
    evidence: loaded.evidence,
    contentUrl: loaded.contentUrl,
    thumbnailUrl: loaded.thumbnailUrl,
  }) === stableSerialize({
    finalVariant: current.finalVariant,
    artifactChecksum: current.artifactChecksum,
    thumbnailChecksum: current.thumbnailChecksum,
    strictEvidence: current.strictEvidence,
    formatVersion: current.formatVersion,
    evidenceDigest: current.evidenceDigest ?? null,
    evidence: current.evidence,
    contentUrl: current.contentUrl,
    thumbnailUrl: current.thumbnailUrl,
  });
}

function assertAuthoritativeReloadBatch(
  loaded: FinalReviewBatch,
  expectedBatch: FinalReviewBatch,
  expectedItems: FinalReviewItem[],
  activeItemId: string,
): FinalReviewItem[] {
  const items = loaded?.items;
  const counts = loaded?.counts;
  const itemsAreObjects = Array.isArray(items)
    && items.every((item) => Boolean(item && typeof item === 'object'));
  const calculated = itemsAreObjects ? statsFor(items) : null;
  const ids = Array.isArray(items) ? items.map((item) => item?.id) : [];
  const expectedById = new Map(expectedItems.map((item) => [item.id, item]));
  const identityMatches = Array.isArray(items) && items.every((item, index) => {
    const expected = expectedById.get(item?.id);
    return Boolean(expected
      && item
      && item.position === index + 1
      && item.position === expected.position
      && item.sourceProjectId === expected.sourceProjectId
      && item.sourceProjectName === expected.sourceProjectName
      && item.sourceImageId === expected.sourceImageId
      && item.sourceRelativePath === expected.sourceRelativePath);
  });
  const revisionsDoNotRegress = Array.isArray(items) && items.every((item) => {
    const expected = expectedById.get(item?.id);
    const itemRevisionDelta = expected && item ? item.revision - expected.revision : -1;
    const artifactRevisionDelta = expected && item
      ? item.artifactRevision - expected.artifactRevision
      : -1;
    return Boolean(expected
      && item
      && item.revision >= expected.revision
      && item.artifactRevision >= expected.artifactRevision
      && artifactRevisionDelta <= itemRevisionDelta
      && (!expected.strictEvidence || item.strictEvidence)
      && (item.formatVersion ?? 1) >= (expected.formatVersion ?? 1)
      && (artifactRevisionDelta !== 0 || unchangedFrozenEvidenceMatches(expected, item))
      && (item.revision !== expected.revision || unchangedRevisionMatches(expected, item)));
  });
  const anyItemAdvanced = Array.isArray(items) && items.some((item) => {
    const expected = expectedById.get(item?.id);
    return Boolean(expected && item.revision > expected.revision);
  });
  const totalItemRevisionDelta = Array.isArray(items) ? items.reduce((sum, item) => {
    const expected = expectedById.get(item?.id);
    return sum + (expected && item ? item.revision - expected.revision : 0);
  }, 0) : -1;
  if (
    !loaded || loaded.id !== expectedBatch.id
    || loaded.name !== expectedBatch.name
    || loaded.rootPath !== expectedBatch.rootPath
    || loaded.manifestPath !== expectedBatch.manifestPath
    || loaded.createdAt !== expectedBatch.createdAt
    || loaded.formatVersion !== expectedBatch.formatVersion
    || !Number.isSafeInteger(loaded.revision) || loaded.revision < expectedBatch.revision
    || !Number.isSafeInteger(loaded.itemCount) || loaded.itemCount <= 0
    || loaded.itemCount !== expectedBatch.itemCount
    || !itemsAreObjects || !Array.isArray(items) || items.length !== loaded.itemCount
    || items.length !== expectedItems.length
    || new Set(ids).size !== ids.length
    || !items.some((item) => item?.id === activeItemId)
    || !identityMatches || !revisionsDoNotRegress
    || !items.every((item) => validFinalReviewItemResponse(item, loaded.id))
    || !counts || !calculated
    || !Number.isSafeInteger(counts.pending) || counts.pending < 0
    || !Number.isSafeInteger(counts.approved) || counts.approved < 0
    || !Number.isSafeInteger(counts.issues) || counts.issues < 0
    || counts.pending + counts.approved + counts.issues !== loaded.itemCount
    || counts.pending !== calculated.pending
    || counts.approved !== calculated.approved
    || counts.issues !== calculated.issues
    || (loaded.revision === expectedBatch.revision && anyItemAdvanced)
    || (loaded.revision > expectedBatch.revision && !anyItemAdvanced)
    || loaded.revision - expectedBatch.revision !== totalItemRevisionDelta
  ) {
    throw new ApiError('载入的终审批次无法证明是当前冲突后的权威新版本', 502, {
      code: 'INVALID_FINAL_REVIEW_RELOAD_RESPONSE',
    });
  }
  return items;
}

function refreshResponseMatchesContract(item: FinalReviewItem, expectedItem: FinalReviewItem): boolean {
  return item.verdict === 'pending'
    && item.issueCodes.length === 0
    && item.feedback === ''
    && item.reviewedAt == null
    && item.strictEvidence === true
    && item.formatVersion === 2
    && item.currentArtifactStale === false
    && item.position === expectedItem.position
    && item.sourceProjectId === expectedItem.sourceProjectId
    && item.sourceProjectName === expectedItem.sourceProjectName
    && item.sourceImageId === expectedItem.sourceImageId
    && item.sourceRelativePath === expectedItem.sourceRelativePath
    && ['typeset', 'preprocess'].includes(item.finalVariant)
    && nonEmptyString(item.artifactChecksum)
    && /^[a-f0-9]{64}$/.test(item.thumbnailChecksum)
    && typeof item.evidenceDigest === 'string' && /^[a-f0-9]{64}$/.test(item.evidenceDigest)
    && validStrictEvidence(item)
    && item.artifactChecksum === item.evidence.final.checksum
    && item.contentUrl === `/api/final-review-items/${item.id}/content?artifactRevision=${item.artifactRevision}`
    && item.thumbnailUrl === `/api/final-review-items/${item.id}/thumbnail?artifactRevision=${item.artifactRevision}`;
}

type MutationExpectation =
  | { operation: 'save'; submitted: FinalReviewDraft }
  | { operation: 'refresh' };

function assertAuthoritativeMutationResult(
  result: { item: FinalReviewItem; batchRevision: number; historyCreated: boolean },
  expectedItem: FinalReviewItem,
  expectedBatchRevision: number,
  expectation: MutationExpectation,
): void {
  const item = result?.item;
  const operation = expectation.operation;
  const validVerdict = item && ['pending', 'approved', 'issues'].includes(item.verdict);
  const expectedIncrement = result?.historyCreated === true ? 1 : 0;
  const contractMatches = item && validVerdict
    && Array.isArray(item.issueCodes) && typeof item.feedback === 'string'
    && typeof result.historyCreated === 'boolean' && (
    operation === 'save'
      ? saveResponseMatchesContract(item, expectedItem, expectation.submitted, result.historyCreated)
      : refreshResponseMatchesContract(item, expectedItem)
  );
  if (
    !item || item.id !== expectedItem.id || item.batchId !== expectedItem.batchId
    || !Number.isSafeInteger(item.revision) || item.revision < 1
    || !Number.isSafeInteger(item.artifactRevision) || item.artifactRevision < 1
    || !validVerdict || !Array.isArray(item.issueCodes) || typeof item.feedback !== 'string'
    || !Number.isSafeInteger(result.batchRevision) || result.batchRevision < 1
    || typeof result.historyCreated !== 'boolean'
    || (operation === 'refresh' && result.historyCreated !== true)
    || item.revision !== expectedItem.revision + expectedIncrement
    || result.batchRevision !== expectedBatchRevision + expectedIncrement
    || item.artifactRevision !== expectedItem.artifactRevision + (operation === 'refresh' ? 1 : 0)
    || !contractMatches
  ) {
    throw new ApiError(`${operation === 'save' ? '终审保存' : '终审刷新'}响应不符合当前操作的权威状态`, 502, {
      code: 'INVALID_FINAL_REVIEW_RESPONSE',
    });
  }
}

function assertAuthoritativeRepairResult(
  result: FinalReviewRepairResult,
  expectedItem: FinalReviewItem,
  expectedBatchRevision: number,
): void {
  const expectedRunId = `final-review-${expectedItem.id.slice(0, 8)}-r${expectedItem.revision}`;
  const sequenceMatches = result && Number.isSafeInteger(result.nextSequence) && (
    result.idempotent === false
      ? result.nextSequence === 2
      : result.idempotent === true && result.nextSequence >= 2
  );
  if (
    !result || result.itemId !== expectedItem.id
    || result.sourceProjectId !== expectedItem.sourceProjectId
    || result.sourceImageId !== expectedItem.sourceImageId
    || result.finalReviewItemRevision !== expectedItem.revision
    || result.artifactRevision !== expectedItem.artifactRevision
    || result.batchRevision !== expectedBatchRevision
    || result.repairProjectId !== expectedItem.sourceProjectId
    || !nonEmptyString(result.repairImageId) || result.repairImageId === expectedItem.sourceImageId
    || !nonEmptyString(result.pageGenerationId) || result.runId !== expectedRunId
    || !sequenceMatches
    || result.parameterSetId !== REPAIR_PARAMETER_SET_ID
    || result.parameterSetHash !== REPAIR_PARAMETER_SET_HASH
  ) {
    throw new ApiError('终审修复响应缺少匹配当前终审项的权威 handoff', 502, {
      code: 'INVALID_FINAL_REVIEW_REPAIR_RESPONSE',
    });
  }
}

function assertAuthoritativeExportResult(
  result: FinalReviewExportResult,
  expectedBatchId: string,
  expectedItemCount: number,
  requestedOutputPath: string,
): void {
  const expectedOutputPath = canonicalAbsolutePosixPath(requestedOutputPath);
  const responseOutputPath = canonicalAbsolutePosixPath(result?.outputPath);
  const responseManifestPath = canonicalAbsolutePosixPath(result?.manifestPath);
  const counts = result && [
    result.exportedCount,
    result.skippedPendingCount,
    result.skippedIssuesCount,
    result.skippedCollisionCount,
  ];
  if (
    !result || result.batchId !== expectedBatchId
    || expectedOutputPath === null
    || responseOutputPath !== expectedOutputPath || result.outputPath !== responseOutputPath
    || responseManifestPath !== exportManifestPath(expectedOutputPath)
    || result.manifestPath !== responseManifestPath
    || !counts || counts.some((count) => !Number.isSafeInteger(count) || count < 0)
    || result.skippedPendingCount !== 0 || result.skippedIssuesCount !== 0
    || result.skippedCollisionCount !== 0 || result.exportedCount !== expectedItemCount
  ) {
    throw new ApiError('终审导出响应缺少匹配当前批次的权威结果', 502, {
      code: 'INVALID_FINAL_REVIEW_EXPORT_RESPONSE',
    });
  }
}

function statsFor(items: FinalReviewItem[]): FinalReviewStats {
  return {
    pending: items.filter((item) => item.verdict === 'pending').length,
    approved: items.filter((item) => item.verdict === 'approved').length,
    issues: items.filter((item) => item.verdict === 'issues').length,
  };
}

function itemDraft(item: FinalReviewItem): FinalReviewDraft {
  return {
    verdict: item.verdict,
    issueCodes: [...item.issueCodes],
    feedback: item.feedback ?? '',
  };
}

function storedRepairContext(): FinalReviewRepairContext | null {
  try {
    const value = window.sessionStorage.getItem(REPAIR_STORAGE_KEY);
    return value ? JSON.parse(value) as FinalReviewRepairContext : null;
  } catch {
    return null;
  }
}

function setStoredRepairContext(value: FinalReviewRepairContext | null): void {
  try {
    if (value) window.sessionStorage.setItem(REPAIR_STORAGE_KEY, JSON.stringify(value));
    else window.sessionStorage.removeItem(REPAIR_STORAGE_KEY);
  } catch {
    // Session persistence is optional; the in-memory context remains authoritative.
  }
}

export function finalReviewDraftDirty(state: Pick<FinalReviewState, 'draft' | 'items' | 'activeItemId'>): boolean {
  const item = state.items.find((entry) => entry.id === state.activeItemId);
  if (!item || !state.draft) return false;
  return item.verdict !== state.draft.verdict
    || item.feedback !== state.draft.feedback
    || JSON.stringify([...item.issueCodes].sort()) !== JSON.stringify([...state.draft.issueCodes].sort());
}

export function finalReviewExportReady(state: Pick<
  FinalReviewState,
  'batch' | 'items' | 'activeItemId' | 'draft' | 'operation' | 'conflict'
>): boolean {
  const { batch, items } = state;
  if (!batch || state.operation || state.conflict || finalReviewDraftDirty(state)
    || !Number.isSafeInteger(batch.revision) || batch.revision < 1
    || !Number.isSafeInteger(batch.itemCount) || batch.itemCount <= 0
    || batch.counts.approved !== batch.itemCount
    || batch.counts.pending !== 0 || batch.counts.issues !== 0
    || items.length !== batch.itemCount || batch.items.length !== batch.itemCount
    || stableSerialize(batch.items) !== stableSerialize(items)) return false;
  const ids = items.map((item) => item.id);
  return new Set(ids).size === ids.length
    && items.every((item, index) => item.position === index + 1
      && item.verdict === 'approved'
      && item.currentArtifactStale === false
      && validFinalReviewItemResponse(item, batch.id));
}

export function finalReviewValidationError(draft: FinalReviewDraft | null): string {
  if (!draft) return '没有选中的终审项目';
  if (draft.verdict === 'issues' && draft.issueCodes.length === 0) return '有问题时至少选择一个问题类别';
  if (draft.verdict === 'issues' && draft.issueCodes.includes('other') && !draft.feedback.trim()) {
    return '选择“其他”时必须填写具体反馈';
  }
  return '';
}

export function filteredFinalReviewItems(state: Pick<
  FinalReviewState,
  'items' | 'statusFilter' | 'issueFilter' | 'search'
>): FinalReviewItem[] {
  const query = state.search.trim().toLocaleLowerCase();
  return state.items.filter((item) => {
    if (state.statusFilter !== 'all' && item.verdict !== state.statusFilter) return false;
    if (state.issueFilter !== 'all' && !item.issueCodes.includes(state.issueFilter)) return false;
    if (!query) return true;
    return [item.sourceRelativePath, item.sourceProjectName, String(item.position)]
      .some((value) => value.toLocaleLowerCase().includes(query));
  });
}

export const useFinalReviewStore = create<FinalReviewState>((set, get) => ({
  batches: [],
  batch: null,
  items: [],
  activeItemId: null,
  draft: null,
  statusFilter: 'all',
  issueFilter: 'all',
  search: '',
  loading: false,
  saving: false,
  refreshing: false,
  error: '',
  conflict: false,
  lastSavedAt: null,
  exportResult: null,
  exporting: false,
  repairContext: storedRepairContext(),
  operation: null,
  conflictDraft: null,

  loadBatches: async () => {
    if (get().operation) return;
    set({ loading: true, operation: 'load', error: '' });
    try {
      const batches = await api.listFinalReviewBatches();
      set({ batches, loading: false, operation: null });
      if (!get().batch && batches[0]) await get().loadBatch(batches[0].id);
    } catch (error) {
      set({ loading: false, operation: null, error: errorMessage(error) });
    }
  },

  loadBatch: async (batchId, preferredItemId) => {
    if (get().operation || get().conflict) return false;
    if (finalReviewDraftDirty(get()) && !window.confirm('当前终审标注尚未保存，确定放弃并切换批次吗？')) return false;
    set({ loading: true, operation: 'load', error: '', conflict: false, conflictDraft: null });
    try {
      const loaded = await api.getFinalReviewBatch(batchId);
      const items = Array.isArray(loaded.items) && loaded.items.length
        ? loaded.items
        : await api.listFinalReviewItems(batchId);
      const batch = { ...loaded, items, counts: loaded.counts ?? statsFor(items) };
      const active = items.find((item) => item.id === preferredItemId) ?? items[0] ?? null;
      set({
        batch,
        items,
        activeItemId: active?.id ?? null,
        draft: active ? itemDraft(active) : null,
        loading: false,
        operation: null,
        lastSavedAt: null,
        exportResult: null,
      });
      return true;
    } catch (error) {
      set({ loading: false, operation: null, error: errorMessage(error) });
      return false;
    }
  },

  setStatusFilter: (statusFilter) => set({ statusFilter }),
  setIssueFilter: (issueFilter) => set({ issueFilter }),
  setSearch: (search) => set({ search }),

  selectItem: (itemId, confirmDiscard = true) => {
    const state = get();
    if (itemId === state.activeItemId) return true;
    if (state.operation || state.conflict) return false;
    if (finalReviewDraftDirty(state) && confirmDiscard
      && !window.confirm('当前终审标注尚未保存，确定放弃并切换页面吗？')) return false;
    const item = state.items.find((entry) => entry.id === itemId);
    if (!item) return false;
    set({ activeItemId: item.id, draft: itemDraft(item), error: '', conflict: false, conflictDraft: null });
    return true;
  },

  navigate: (direction) => {
    const state = get();
    if (state.operation || state.conflict) return false;
    const filtered = filteredFinalReviewItems(state);
    const index = filtered.findIndex((item) => item.id === state.activeItemId);
    const next = filtered[index + direction];
    return next ? state.selectItem(next.id) : false;
  },

  updateDraft: (patch) => set((state) => state.operation || state.conflict || finalReviewLegacyReviewed(
    state.items.find((entry) => entry.id === state.activeItemId),
  ) ? state : ({
    draft: state.draft ? { ...state.draft, ...patch } : null,
    error: '',
    conflict: false,
  })),

  toggleIssue: (code) => set((state) => {
    if (state.operation || state.conflict || !state.draft || finalReviewLegacyReviewed(
      state.items.find((entry) => entry.id === state.activeItemId),
    )) return state;
    const issueCodes = state.draft.issueCodes.includes(code)
      ? state.draft.issueCodes.filter((entry) => entry !== code)
      : [...state.draft.issueCodes, code];
    return { draft: { ...state.draft, issueCodes }, error: '', conflict: false };
  }),

  save: async (moveNext = false) => {
    const state = get();
    const item = state.items.find((entry) => entry.id === state.activeItemId);
    const validation = finalReviewValidationError(state.draft);
    const filtered = moveNext ? filteredFinalReviewItems(state) : [];
    const activeIndex = moveNext ? filtered.findIndex((entry) => entry.id === item?.id) : -1;
    const nextItemId = moveNext && activeIndex >= 0 ? filtered[activeIndex + 1]?.id : undefined;
    if (state.operation || state.conflict || finalReviewLegacyReviewed(item)) return false;
    if (!item || !state.batch || !state.draft || validation || (moveNext && !nextItemId)) {
      set({ error: validation || '没有选中的终审项目' });
      if (moveNext && item && state.draft && !validation && !nextItemId) {
        set({ error: '当前筛选结果中已经没有下一张成品' });
      }
      return false;
    }

    if (moveNext && !finalReviewDraftDirty(state)) {
      // Keep the local advance atomic from the UI's perspective so a rapid repeat
      // cannot skip an additional item. No server write is needed for a clean draft.
      set({ saving: true, operation: 'save', error: '', conflict: false });
      await Promise.resolve();
      const current = get();
      const next = current.items.find((entry) => entry.id === nextItemId);
      if (!next || current.activeItemId !== item.id) {
        set({ saving: false, operation: null });
        return false;
      }
      set({
        activeItemId: next.id,
        draft: itemDraft(next),
        saving: false,
        operation: null,
        error: '',
        conflict: false,
      });
      return true;
    }

    set({ saving: true, operation: 'save', error: '', conflict: false, conflictDraft: null });
    const submitted = normalizedSaveDraft(state.draft);
    try {
      const saveResult = await api.updateFinalReviewItem(item.id, {
        ...submitted,
        expectedRevision: item.revision,
        expectedBatchRevision: state.batch.revision,
        actor: finalReviewActor(),
      });
      assertAuthoritativeMutationResult(saveResult, item, state.batch.revision, {
        operation: 'save', submitted,
      });
      const saved = saveResult.item;
      let applied = false;
      set((current) => {
        const currentSavedItem = current.items.find((entry) => entry.id === saved.id);
        const canSafelyApply = Boolean(current.batch && state.batch
          && current.batch.id === state.batch.id
          && current.batch.revision === state.batch.revision
          && currentSavedItem?.batchId === item.batchId
          && currentSavedItem?.revision === item.revision
          && currentSavedItem.artifactRevision === item.artifactRevision);
        if (!canSafelyApply) {
          return {
            saving: false,
            operation: null,
            error: mutationResponseCannotBeAppliedMessage('save'),
            conflict: true,
            conflictDraft: current.draft,
          };
        }
        applied = true;
        const items = current.items.map((entry) => entry.id === saved.id ? saved : entry);
        const isStillActive = current.activeItemId === item.id;
        return {
          items,
          batch: current.batch ? {
            ...current.batch, items, counts: statsFor(items),
            revision: saveResult.batchRevision,
          } : null,
          draft: isStillActive ? itemDraft(saved) : current.draft,
          saving: false,
          operation: null,
          lastSavedAt: isStillActive ? saved.reviewedAt ?? new Date().toISOString() : current.lastSavedAt,
        };
      });
      if (!applied) return false;
      if (moveNext && get().activeItemId === item.id) {
        const next = get().items.find((entry) => entry.id === nextItemId);
        if (next) {
          set({ activeItemId: next.id, draft: itemDraft(next), error: '', conflict: false });
        }
      }
      return true;
    } catch (error) {
      const requiresReload = mutationOutcomeRequiresReload(error);
      set((current) => {
        if (requiresReload) {
          return {
            saving: false,
            operation: null,
            error: mutationFailureMessage(error),
            conflict: true,
            conflictDraft: current.draft,
          };
        }
        return current.activeItemId === item.id ? {
          saving: false,
          operation: null,
          error: mutationFailureMessage(error),
          conflict: false,
          conflictDraft: null,
        } : { saving: false, operation: null };
      });
      return false;
    }
  },

  refreshActive: async () => {
    const state = get();
    const item = state.items.find((entry) => entry.id === state.activeItemId);
    if (!item || !state.batch || state.operation || state.conflict || finalReviewLegacyApproved(item)) return false;
    const refreshMessage = finalReviewDraftDirty(state)
      ? '当前草稿尚未保存。同步会保留旧快照历史、放弃草稿，并将本项重置为未审核。确定继续吗？'
      : '同步修复后的成品会保留旧快照历史，并将本项重置为未审核。确定继续吗？';
    if (!window.confirm(refreshMessage)) return false;
    const token = ++requestSequence;
    const requestedItemId = item.id;
    set({ refreshing: true, operation: 'refresh', error: '', conflict: false, conflictDraft: null });
    try {
      const result = await api.refreshFinalReviewItem(item.id, {
        expectedRevision: item.revision,
        expectedBatchRevision: state.batch.revision,
        actor: finalReviewActor(),
      });
      assertAuthoritativeMutationResult(result, item, state.batch.revision, { operation: 'refresh' });
      const refreshed = result.item;
      let applied = false;
      set((current) => {
        const currentRequestedItem = current.items.find((entry) => entry.id === refreshed.id);
        const canSafelyApply = Boolean(current.batch && state.batch
          && current.batch.id === state.batch.id
          && current.batch.revision === state.batch.revision
          && currentRequestedItem?.batchId === item.batchId
          && currentRequestedItem?.revision === item.revision
          && currentRequestedItem.artifactRevision === item.artifactRevision);
        if (!canSafelyApply) {
          return {
            refreshing: false,
            operation: null,
            error: mutationResponseCannotBeAppliedMessage('refresh'),
            conflict: true,
            conflictDraft: current.draft,
          };
        }
        applied = true;
        const items = current.items.map((entry) => entry.id === refreshed.id ? refreshed : entry);
        const requestStillOwnsActiveItem = token === requestSequence
          && current.activeItemId === requestedItemId;
        return {
          items,
          batch: current.batch ? {
            ...current.batch, items, counts: statsFor(items),
            revision: result.batchRevision,
          } : null,
          draft: requestStillOwnsActiveItem ? itemDraft(refreshed) : current.draft,
          refreshing: false,
          operation: null,
          lastSavedAt: requestStillOwnsActiveItem ? null : current.lastSavedAt,
        };
      });
      if (!applied) return false;
      get().clearRepairContext();
      return true;
    } catch (error) {
      const requiresReload = mutationOutcomeRequiresReload(error);
      set((current) => {
        if (requiresReload) {
          return {
            refreshing: false,
            operation: null,
            error: mutationFailureMessage(error),
            conflict: true,
            conflictDraft: current.draft,
          };
        }
        return current.activeItemId === requestedItemId ? {
          refreshing: false,
          operation: null,
          error: mutationFailureMessage(error),
          conflict: false,
          conflictDraft: null,
        } : { refreshing: false, operation: null };
      });
      return false;
    }
  },

  exportApproved: async (input) => {
    const state = get();
    if (!state.batch || input.conflict !== 'rename' || !canonicalAbsolutePosixPath(input.outputPath)
      || !finalReviewExportReady(state)) return false;
    set({ exporting: true, operation: 'export', error: '', exportResult: null });
    try {
      const exportResult = await api.exportApprovedFinalReviewItems(state.batch.id, {
        ...input,
        expectedBatchRevision: state.batch.revision,
        actor: finalReviewActor(),
      });
      assertAuthoritativeExportResult(
        exportResult,
        state.batch.id,
        state.batch.itemCount,
        input.outputPath,
      );
      let applied = false;
      set((current) => {
        if (current.batch?.id !== state.batch?.id || current.batch?.revision !== state.batch?.revision
          || !finalReviewExportReady({ ...current, operation: null })) {
          return {
            exporting: false,
            operation: null,
            error: mutationResponseCannotBeAppliedMessage('export'),
            conflict: true,
            conflictDraft: current.draft,
          };
        }
        applied = true;
        return { exporting: false, operation: null, exportResult };
      });
      return applied;
    } catch (error) {
      const requiresReload = mutationOutcomeRequiresReload(error);
      set((current) => ({
        exporting: false,
        operation: null,
        error: mutationFailureMessage(error),
        conflict: requiresReload,
        conflictDraft: requiresReload ? current.draft : null,
      }));
      return false;
    }
  },

  discardDraft: () => {
    const state = get();
    if (state.operation || state.conflict) return;
    const item = state.items.find((entry) => entry.id === state.activeItemId);
    set({ draft: item ? itemDraft(item) : null, error: '', conflict: false });
  },

  beginRepair: async () => {
    const state = get();
    const item = state.items.find((entry) => entry.id === state.activeItemId);
    if (!item || !state.batch || item.verdict !== 'issues' || state.operation || state.conflict || finalReviewDraftDirty(state)) return null;
    set({ operation: 'repair', error: '', conflict: false });
    let result: FinalReviewRepairResult;
    try {
      result = await api.beginFinalReviewRepair(item.id, {
        expectedRevision: item.revision,
        expectedBatchRevision: state.batch.revision,
        actor: finalReviewActor(),
      });
      assertAuthoritativeRepairResult(result, item, state.batch.revision);
    } catch (error) {
      const requiresReload = mutationOutcomeRequiresReload(error);
      set((current) => ({
        operation: null,
        error: mutationFailureMessage(error),
        conflict: requiresReload,
        conflictDraft: requiresReload ? current.draft : null,
      }));
      return null;
    }
    const current = get();
    const currentItem = current.items.find((entry) => entry.id === item.id);
    if (
      current.operation !== 'repair'
      || current.batch?.id !== state.batch.id || current.batch.revision !== state.batch.revision
      || current.activeItemId !== item.id
      || currentItem?.revision !== item.revision
      || currentItem.artifactRevision !== item.artifactRevision
    ) {
      set((latest) => ({
        operation: null,
        error: mutationResponseCannotBeAppliedMessage('repair'),
        conflict: true,
        conflictDraft: latest.draft,
      }));
      return null;
    }
    const repairContext = {
      batchId: item.batchId,
      itemId: item.id,
      issueCodes: [...item.issueCodes],
      feedback: item.feedback,
      sourceProjectId: result.sourceProjectId,
      sourceImageId: result.sourceImageId,
      repairProjectId: result.repairProjectId,
      repairImageId: result.repairImageId,
      pageGenerationId: result.pageGenerationId,
      runId: result.runId,
      itemRevision: result.finalReviewItemRevision,
      batchRevision: result.batchRevision,
      artifactRevision: result.artifactRevision,
      nextSequence: result.nextSequence,
      parameterSetId: result.parameterSetId,
      parameterSetHash: result.parameterSetHash,
    };
    setStoredRepairContext(repairContext);
    set({ repairContext });
    return repairContext;
  },

  finishRepairNavigation: (clearContext = false) => {
    const state = get();
    if (state.operation !== 'repair') return;
    if (clearContext) setStoredRepairContext(null);
    set({ operation: null, ...(clearContext ? { repairContext: null } : {}) });
  },

  reloadConflict: async () => {
    const state = get();
    if (!state.batch || !state.activeItemId || state.operation || !state.conflict) return false;
    const activeItemId = state.activeItemId;
    set({ operation: 'load', loading: true, error: '' });
    try {
      const loaded = await api.getFinalReviewBatch(state.batch.id);
      const items = assertAuthoritativeReloadBatch(loaded, state.batch, state.items, activeItemId);
      const current = get();
      if (current.operation !== 'load' || !current.conflict
        || current.batch?.id !== state.batch.id || current.batch.revision !== state.batch.revision
        || current.activeItemId !== activeItemId
        || stableSerialize(current.items) !== stableSerialize(state.items)) {
        throw new ApiError('载入响应到达时当前终审身份已变化，必须继续保持全局锁', 502, {
          code: 'FINAL_REVIEW_RELOAD_IDENTITY_CHANGED',
        });
      }
      const draft = current.conflictDraft ?? current.draft;
      set({
        batch: loaded, items, activeItemId, draft, conflict: false, conflictDraft: null,
        operation: null, loading: false, error: '',
      });
      return true;
    } catch (error) {
      set({ operation: null, loading: false, error: errorMessage(error), conflict: true });
      return false;
    }
  },

  clearRepairContext: () => {
    setStoredRepairContext(null);
    set({ repairContext: null });
  },
}));

export function resetFinalReviewStore(): void {
  setStoredRepairContext(null);
  useFinalReviewStore.setState({
    batches: [], batch: null, items: [], activeItemId: null, draft: null,
    statusFilter: 'all', issueFilter: 'all', search: '', loading: false,
    saving: false, refreshing: false, error: '', conflict: false,
    lastSavedAt: null, exportResult: null, exporting: false, repairContext: null,
    operation: null, conflictDraft: null,
  });
}
