export type FinalReviewVerdict = 'pending' | 'approved' | 'issues';

import type { LineageActor } from '../types';

export type FinalReviewEvidenceKind = 'original' | 'quality' | 'mask' | 'clean' | 'final';
export type FinalReviewEvidenceAvailability = 'available' | 'not-applicable' | 'unavailable';

export interface FinalReviewEvidenceDescriptor {
  kind: FinalReviewEvidenceKind;
  availability: FinalReviewEvidenceAvailability;
  artifactRevision: number;
  checksum?: string | null;
  url?: string | null;
  resolutionDigest?: string | null;
  grid?: { width: number; height: number } | null;
  generationId?: string | null;
  producerId?: string | null;
  terminalId?: string | null;
  terminalRevisionId?: string | null;
  terminalChecksum?: string | null;
  producerRevisionId?: string | null;
  relativePath?: string | null;
  reason?: string | null;
}

export type FinalReviewEvidence = Record<FinalReviewEvidenceKind, FinalReviewEvidenceDescriptor>;

export type FinalReviewIssueCode =
  | 'typesetting'
  | 'translation'
  | 'mask'
  | 'ai_inpaint'
  | 'missing_text'
  | 'preprocess'
  | 'other';

export interface FinalReviewBatchSummary {
  id: string;
  name: string;
  itemCount: number;
  counts: FinalReviewStats;
  rootPath?: string;
  manifestPath?: string;
  revision: number;
  formatVersion?: number;
  createdAt?: string;
  updatedAt?: string;
}

export interface FinalReviewStats {
  pending: number;
  approved: number;
  issues: number;
}

export interface FinalReviewItem {
  id: string;
  batchId: string;
  position: number;
  sourceProjectId: string;
  sourceProjectName: string;
  sourceImageId: string;
  sourceRelativePath: string;
  finalVariant: 'typeset' | 'preprocess';
  artifactChecksum: string;
  thumbnailChecksum: string;
  currentArtifactStale: boolean;
  verdict: FinalReviewVerdict;
  issueCodes: FinalReviewIssueCode[];
  feedback: string;
  revision: number;
  artifactRevision: number;
  formatVersion?: number;
  strictEvidence: boolean;
  evidence: FinalReviewEvidence;
  evidenceDigest?: string | null;
  reviewedAt?: string | null;
  refreshedAt?: string | null;
  contentUrl: string;
  thumbnailUrl: string;
}

export interface FinalReviewBatch extends FinalReviewBatchSummary {
  items: FinalReviewItem[];
}

export interface FinalReviewItemPatch {
  verdict: FinalReviewVerdict;
  issueCodes: FinalReviewIssueCode[];
  feedback: string;
  expectedRevision: number;
  expectedBatchRevision?: number;
  actor?: LineageActor;
}

export interface FinalReviewExportRequest {
  outputPath: string;
  conflict: 'rename' | 'skip';
  preserveTree: boolean;
  expectedBatchRevision: number;
  actor: LineageActor;
}

export interface FinalReviewExportResult {
  batchId: string;
  outputPath: string;
  exportedCount: number;
  skippedPendingCount: number;
  skippedIssuesCount: number;
  skippedCollisionCount: number;
  manifestPath: string;
}

export interface FinalReviewRefreshResult {
  item: FinalReviewItem;
  batchRevision: number;
  historyCreated: boolean;
}

export interface FinalReviewSaveResult {
  item: FinalReviewItem;
  batchRevision: number;
  historyCreated: boolean;
}

export interface FinalReviewRefreshRequest {
  expectedRevision: number;
  expectedBatchRevision: number;
  actor: LineageActor;
}

export interface FinalReviewRepairRequest extends FinalReviewRefreshRequest {
  parameterSetId?: string;
  parameterSetHash?: string;
}

export interface FinalReviewRepairResult {
  itemId: string;
  sourceProjectId: string;
  sourceImageId: string;
  repairProjectId: string;
  repairImageId: string;
  pageGenerationId: string;
  runId: string;
  finalReviewItemRevision: number;
  batchRevision: number;
  artifactRevision: number;
  nextSequence: number;
  parameterSetId: string;
  parameterSetHash: string;
  idempotent: boolean;
}

export const FINAL_REVIEW_EVIDENCE_KINDS: ReadonlyArray<FinalReviewEvidenceKind> = [
  'original', 'quality', 'mask', 'clean', 'final',
];

export const FINAL_REVIEW_ISSUES: ReadonlyArray<{
  code: FinalReviewIssueCode;
  label: string;
}> = [
  { code: 'typesetting', label: '嵌字排版' },
  { code: 'translation', label: '翻译' },
  { code: 'mask', label: '抠图蒙版' },
  { code: 'ai_inpaint', label: 'AI 补图' },
  { code: 'missing_text', label: '漏字或 OCR' },
  { code: 'preprocess', label: '预处理' },
  { code: 'other', label: '其他' },
];

export function finalReviewIssueLabel(code: FinalReviewIssueCode): string {
  return FINAL_REVIEW_ISSUES.find((entry) => entry.code === code)?.label ?? code;
}
