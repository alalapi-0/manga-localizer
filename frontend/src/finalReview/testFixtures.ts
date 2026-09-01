import { finalReviewCanonicalDigest, finalReviewEvidenceDigest } from './store';
import type { FinalReviewBatch, FinalReviewItem } from './types';

export function finalReviewItemFixture(
  id = 'final-item-1',
  patch: Partial<FinalReviewItem> = {},
): FinalReviewItem {
  const artifactRevision = patch.artifactRevision ?? 1;
  const artifactChecksums = {
    original: '1'.repeat(64),
    quality: '2'.repeat(64),
    mask: '3'.repeat(64),
    clean: '4'.repeat(64),
    final: '5'.repeat(64),
  } as const;
  const terminalChecksums = {
    original: artifactChecksums.original,
    quality: artifactChecksums.quality,
    mask: '6'.repeat(64),
    clean: '7'.repeat(64),
    final: '8'.repeat(64),
  } as const;
  const grid = { width: 1200, height: 1800 };
  const resolutionDigest = finalReviewCanonicalDigest(grid);
  if (!resolutionDigest) throw new TypeError('Fixture grid must have a canonical digest');
  const evidence = Object.fromEntries((['original', 'quality', 'mask', 'clean', 'final'] as const).map((kind) => [kind, {
    kind,
    availability: 'available' as const,
    artifactRevision,
    checksum: artifactChecksums[kind],
    url: `/api/final-review-items/${id}/artifacts/${kind}?artifactRevision=${artifactRevision}`,
    resolutionDigest,
    grid: { ...grid },
    generationId: `generation-${id}`,
    producerId: `candidate-${kind}-${id}`,
    producerRevisionId: kind === 'quality' ? null : `candidate-revision-${kind}-${id}`,
    terminalId: `accept-${kind}-${id}`,
    terminalChecksum: terminalChecksums[kind],
    terminalRevisionId: `accept-revision-${kind}-${id}`,
    relativePath: `images/${id}/r${String(artifactRevision).padStart(6, '0')}/${kind}.png`,
  }])) as FinalReviewItem['evidence'];
  return {
    id,
    batchId: 'final-batch-1',
    position: Number(id.split('-').at(-1)) || 1,
    sourceProjectId: 'project-1',
    sourceProjectName: '真实项目一',
    sourceImageId: `image-${id.split('-').at(-1) ?? '1'}`,
    sourceRelativePath: `第一话/${id}.png`,
    finalVariant: 'typeset',
    artifactChecksum: evidence.final.checksum ?? `checksum-${id}`,
    thumbnailChecksum: '9'.repeat(64),
    currentArtifactStale: false,
    verdict: 'pending',
    issueCodes: [],
    feedback: '',
    revision: 1,
    artifactRevision,
    formatVersion: 2,
    strictEvidence: true,
    evidence,
    evidenceDigest: patch.strictEvidence === false ? null : finalReviewEvidenceDigest(evidence),
    reviewedAt: null,
    contentUrl: `/api/final-review-items/${id}/content?artifactRevision=${artifactRevision}`,
    thumbnailUrl: `/api/final-review-items/${id}/thumbnail?artifactRevision=${artifactRevision}`,
    ...patch,
  };
}

export function finalReviewBatchFixture(items: FinalReviewItem[] = [finalReviewItemFixture()]): FinalReviewBatch {
  return {
    id: 'final-batch-1',
    name: '199 张最终成品',
    itemCount: items.length,
    counts: {
      pending: items.filter((item) => item.verdict === 'pending').length,
      approved: items.filter((item) => item.verdict === 'approved').length,
      issues: items.filter((item) => item.verdict === 'issues').length,
    },
    rootPath: '/local/final-review',
    manifestPath: '/local/final-review/final-review.json',
    revision: 1,
    formatVersion: 2,
    items,
  };
}
