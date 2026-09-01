import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from './client';
import { regionFixture } from '../test/fixtures';
import { CLEAN_PLATE_CHECKS, MASK_COLLATERAL_CHECKS, MASK_COVERAGE_CHECKS, TRANSLATION_QC_CHECKS, TYPESET_CHECKS } from '../types';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('api client contract', () => {
  afterEach(() => vi.restoreAllMocks());

  it('uses the final-review snapshot endpoints and preserves explicit review revisions', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse([{ id: 'batch-1', name: '终审', itemCount: 199 }]))
      .mockResolvedValueOnce(jsonResponse({
        item: {
          id: 'item-1', batchId: 'batch-1', position: 1, verdict: 'issues',
          issueCodes: ['translation'], feedback: '译意不准', revision: 4,
          thumbnailChecksum: 'thumb-checksum-4',
        },
        batchRevision: 8,
        historyCreated: true,
      }))
      .mockResolvedValueOnce(jsonResponse({
        item: {
          id: 'item-1', batchId: 'batch-1', position: 1, verdict: 'pending',
          issueCodes: [], feedback: '', revision: 5,
          thumbnailChecksum: 'thumb-checksum-5',
        },
        batchRevision: 9,
        historyCreated: true,
      }));

    expect(await api.listFinalReviewBatches()).toHaveLength(1);
    const saveActor = { actorKind: 'human' as const, sessionId: 'ui-save', operationSource: 'ui' as const };
    const updated = await api.updateFinalReviewItem('item-1', {
      verdict: 'issues', issueCodes: ['translation'], feedback: '译意不准', expectedRevision: 3,
      expectedBatchRevision: 7, actor: saveActor,
    });
    const actor = { actorKind: 'human' as const, sessionId: 'ui-1', operationSource: 'ui' as const };
    const refreshed = await api.refreshFinalReviewItem('item-1', {
      expectedRevision: 4, expectedBatchRevision: 8, actor,
    });

    expect(fetchMock.mock.calls[1]?.[0]).toBe('/api/final-review-items/item-1');
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({ method: 'PATCH' });
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      verdict: 'issues', issueCodes: ['translation'], feedback: '译意不准', expectedRevision: 3,
      expectedBatchRevision: 7, actor: saveActor,
    });
    expect(fetchMock.mock.calls[2]?.[0]).toBe('/api/final-review-items/item-1/refresh');
    expect(updated.item.thumbnailChecksum).toBe('thumb-checksum-4');
    expect(updated).toMatchObject({ batchRevision: 8, historyCreated: true });
    expect(refreshed.item.thumbnailChecksum).toBe('thumb-checksum-5');
    expect(refreshed).toMatchObject({ batchRevision: 9, historyCreated: true });
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({
      expectedRevision: 4, expectedBatchRevision: 8, actor,
    });
    expect(api.finalReviewContentUrl('item 1', 5)).toBe('/api/final-review-items/item%201/artifacts/final?artifactRevision=5');
  });

  it('sends terminal all-approved export policy options without an item list', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      batchId: 'batch-1', outputPath: '/safe/final', exportedCount: 199,
      skippedPendingCount: 0, skippedIssuesCount: 0, skippedCollisionCount: 0,
      manifestPath: '/safe/final/export.json',
    }));

    await api.exportApprovedFinalReviewItems('batch-1', {
      outputPath: '/safe/final', conflict: 'rename', preserveTree: true,
      expectedBatchRevision: 9,
      actor: { actorKind: 'human', sessionId: 'ui-export', operationSource: 'ui' },
    });

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body).toEqual({
      outputPath: '/safe/final', conflict: 'rename', preserveTree: true,
      expectedBatchRevision: 9,
      actor: { actorKind: 'human', sessionId: 'ui-export', operationSource: 'ui' },
    });
    expect(body).not.toHaveProperty('itemIds');
  });

  it('fails closed when save or refresh omits authoritative batch revision history metadata', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ item: { id: 'item-1' }, historyCreated: true }))
      .mockResolvedValueOnce(jsonResponse({ item: { id: 'item-1' }, batchRevision: 9 }));
    const actor = { actorKind: 'human' as const, sessionId: 'ui-strict', operationSource: 'ui' as const };

    await expect(api.updateFinalReviewItem('item-1', {
      verdict: 'approved', issueCodes: [], feedback: '', expectedRevision: 4,
      expectedBatchRevision: 8, actor,
    })).rejects.toMatchObject({ status: 502, code: 'INVALID_FINAL_REVIEW_RESPONSE' });
    await expect(api.refreshFinalReviewItem('item-1', {
      expectedRevision: 4, expectedBatchRevision: 8, actor,
    })).rejects.toMatchObject({ status: 502, code: 'INVALID_FINAL_REVIEW_RESPONSE' });
  });

  it('reports final-review revision conflicts as ApiError 409', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      detail: {
        message: '终审标注版本已变化', expectedRevision: 2, actualRevision: 3,
        resource: 'final-review-item:item-1', currentItem: { id: 'item-1', revision: 3 },
      },
    }, 409));

    await expect(api.updateFinalReviewItem('item-1', {
      verdict: 'approved', issueCodes: [], feedback: '', expectedRevision: 2,
    })).rejects.toMatchObject({ status: 409, message: '终审标注版本已变化' });
  });

  it('starts repair through the explicit fresh-G0 handoff and versions every artifact URL', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      itemId: 'item-1', sourceProjectId: 'project-1', sourceImageId: 'image-1',
      repairProjectId: 'project-1', repairImageId: 'image-1',
      pageGenerationId: 'generation-2', runId: 'run-2', finalReviewItemRevision: 4,
      batchRevision: 9, artifactRevision: 3, nextSequence: 1,
      parameterSetId: 'final-review-repair-v1', parameterSetHash: 'a'.repeat(64), idempotent: false,
    }));
    const actor = { actorKind: 'human' as const, sessionId: 'ui-repair', operationSource: 'ui' as const };
    await api.beginFinalReviewRepair('item 1', {
      expectedRevision: 4, expectedBatchRevision: 9, actor,
    });

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/final-review-items/item%201/repair');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      expectedRevision: 4, expectedBatchRevision: 9, actor,
    });
    expect(api.finalReviewArtifactUrl('item 1', 'clean', 12)).toBe(
      '/api/final-review-items/item%201/artifacts/clean?artifactRevision=12',
    );
    expect(api.finalReviewThumbnailUrl('item 1', 12)).toBe(
      '/api/final-review-items/item%201/thumbnail?artifactRevision=12',
    );
  });

  it('flattens backend provider capabilities without claiming unavailable providers', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      providers: {
        preprocessing: {
          'opencv-pillow': { provider: 'opencv-pillow', available: true },
          'realesrgan-onnx': { provider: 'realesrgan-onnx', available: false, error: 'model missing' },
        },
        ocr: {
          tesseract: {
            provider: 'tesseract',
            available: true,
            detectTextRegions: true,
          },
        },
        inpainting: {
          opencv: {
            provider: 'opencv',
            available: true,
            textPolarities: ['auto', 'dark', 'light', 'unsupported'],
          },
        },
        translation: {
          manual: { provider: 'manual', available: true, remote: false },
          mock: { provider: 'mock', available: true, deterministic: true, remote: false },
          'argos-ja-zh': {
            provider: 'argos-ja-zh',
            available: false,
            remote: false,
            error: 'ctranslate2 and sentencepiece are not installed',
          },
          'openai-compatible': {
            provider: 'openai-compatible',
            available: false,
            configurable: true,
            remote: true,
          },
        },
      },
      capabilities: {
        fonts: { available: false, error: 'No CJK font' },
        safeExport: true,
        lanAccess: true,
        companionUrl: 'http://192.168.1.20:8000',
      },
    }));

    const result = await api.getCapabilities();

    expect(result.providers).toEqual(expect.arrayContaining([
      expect.objectContaining({
        id: 'realesrgan-onnx',
        kind: 'preprocessor',
        available: false,
        label: 'Real-ESRGAN ONNX 动漫超分',
      }),
      expect.objectContaining({ id: 'tesseract', kind: 'detector', available: true }),
      expect.objectContaining({ id: 'tesseract', kind: 'ocr', available: true }),
      expect.objectContaining({ id: 'mock', kind: 'translator', isMock: true }),
      expect.objectContaining({
        id: 'opencv',
        kind: 'inpainter',
        textPolarities: ['auto', 'dark', 'light'],
      }),
      expect.objectContaining({
        id: 'argos-ja-zh',
        kind: 'translator',
        available: false,
        local: true,
        label: 'Argos 本地日→中',
        reason: 'ctranslate2 and sentencepiece are not installed',
      }),
      expect.objectContaining({
        id: 'openai-compatible',
        available: false,
        configurable: true,
        local: false,
      }),
      expect.objectContaining({ id: 'pillow', kind: 'typesetter', available: false, reason: 'No CJK font' }),
    ]));
    expect(result.system?.safeExport).toBe(true);
    expect(result.system?.lanAccess).toBe(true);
    expect(result.system?.companionUrl).toBe('http://192.168.1.20:8000');
  });

  it('sends only RegionCreate fields for a local draft', async () => {
    const draft = regionFixture('local-draft', {
      createdAt: '2026-08-06T00:00:00Z',
      updatedAt: '2026-08-06T00:00:00Z',
      revision: 0,
      paragraphGroupId: 'paragraph-1',
      contentDisposition: 'translate',
      style: {
        ...regionFixture().style,
        color: '#123456',
        strokeColor: '#abcdef',
        lineHeight: 1.25,
      },
      repair: {
        ...regionFixture().repair,
        method: 'navier_stokes',
        maskPadding: 6,
      },
    });
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      ...draft,
      id: 'region-server',
      revision: 1,
    }, 201));

    await api.createRegion('image-1', draft);

    const request = fetchMock.mock.calls[0]?.[1];
    const body = JSON.parse(String(request?.body)) as Record<string, unknown>;
    expect(body).toMatchObject({
      x: draft.x,
      sourceText: draft.sourceText,
      paragraphGroupId: 'paragraph-1',
      rubyParentId: null,
      contentDisposition: 'translate',
    });
    expect(body.style).toMatchObject({
      color: '#123456',
      strokeColor: '#abcdef',
      lineHeight: 1.25,
    });
    expect(body.repair).toMatchObject({
      method: 'navier_stokes',
      maskPadding: 6,
      textPolarity: draft.repair.textPolarity,
      dilation: draft.repair.dilation,
      radius: draft.repair.radius,
    });
    expect(body.repair).not.toHaveProperty('padding');
    expect(body).not.toHaveProperty('id');
    expect(body).not.toHaveProperty('imageId');
    expect(body).not.toHaveProperty('revision');
    expect(body).not.toHaveProperty('createdAt');
    expect(body).not.toHaveProperty('updatedAt');
    expect(body).not.toHaveProperty('trustDisposition');
    expect(body).not.toHaveProperty('trustReason');
    expect(body).not.toHaveProperty('trustPolicyVersion');
    expect(body).not.toHaveProperty('detectorConfidence');
    expect(body).not.toHaveProperty('ocrConfidence');
    expect(body).not.toHaveProperty('recognition');
    expect(body).not.toHaveProperty('detectorJobItemId');
    expect(body).not.toHaveProperty('detectorCandidateIndex');
  });

  it('preserves backend-supported repair settings on region patches too', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(
      regionFixture('region-1'),
    ));

    await api.updateRegion('region-1', {
      repair: {
        ...regionFixture().repair,
        method: 'navier_stokes',
        maskPadding: 9,
        inpainterProvider: 'lama-onnx',
        maskEdits: {
          version: 1,
          strokes: [{ mode: 'erase', radius: 8, points: [[101.5, 202.25], [110, 220]] }],
        },
      },
      expectedRevision: 4,
    });

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)) as {
      repair: Record<string, unknown>;
    };
    expect(body.repair).toMatchObject({
      method: 'navier_stokes',
      maskPadding: 9,
      inpainterProvider: 'lama-onnx',
      maskEdits: {
        version: 1,
        strokes: [{ mode: 'erase', radius: 8, points: [[101.5, 202.25], [110, 220]] }],
      },
    });
    expect(body.repair).not.toHaveProperty('padding');
  });

  it('omits server-owned trust evidence from region patches', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(
      regionFixture('region-1'),
    ));

    await api.updateRegion('region-1', {
      ...regionFixture('region-1', {
        trustDisposition: 'trusted',
        trustReason: 'human-confirmed',
        detectorConfidence: 0.42,
        ocrConfidence: 0.99,
        detectorJobItemId: 'server-item',
        detectorCandidateIndex: 3,
        recognition: { provider: 'test-provider' },
      }),
      expectedRevision: 4,
    });

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body)) as Record<string, unknown>;
    expect(body).not.toHaveProperty('trustDisposition');
    expect(body).not.toHaveProperty('trustReason');
    expect(body).not.toHaveProperty('trustPolicyVersion');
    expect(body).not.toHaveProperty('detectorConfidence');
    expect(body).not.toHaveProperty('ocrConfidence');
    expect(body).not.toHaveProperty('recognition');
    expect(body).not.toHaveProperty('detectorJobItemId');
    expect(body).not.toHaveProperty('detectorCandidateIndex');
  });

  it('preserves explicit nulls when clearing editable G4 region fields', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(
      regionFixture('region-1'),
    ));

    await api.updateRegion('region-1', {
      paragraphGroupId: null,
      rubyParentId: null,
      contentDisposition: null,
      expectedRevision: 4,
    });

    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body).toMatchObject({
      paragraphGroupId: null,
      rubyParentId: null,
      contentDisposition: null,
      expectedRevision: 4,
    });
  });

  it('sends the explicit page review state with its required image revision', async () => {
    const reviewed = {
      ...regionFixture(),
      id: 'image-1',
      status: { reviewState: 'no-text-reviewed', reviewedAt: '2026-08-10T10:00:00Z' },
      revision: 8,
    };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(reviewed));

    await api.reviewImage('image-1', 'no-text-reviewed', 7);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/images/image-1/review');
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PATCH');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      reviewState: 'no-text-reviewed',
      expectedRevision: 7,
    });
  });

  it('sends a durable visual-stage review with its image revision', async () => {
    const reviewed = {
      ...regionFixture(),
      id: 'image-1',
      stageReviews: {
        inpaint: { state: 'accepted', reviewedAt: '2026-08-13T10:00:00Z', resultRevision: 7, artifactChecksum: 'a'.repeat(64) },
      },
      revision: 8,
    };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(reviewed));

    await api.reviewImageStage('image-1', 'inpaint', 'accepted', 7, {
      artifactChecksum: 'a'.repeat(64),
      maskChecksum: 'b'.repeat(64),
    });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/images/image-1/stage-reviews/inpaint');
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PATCH');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      state: 'accepted',
      expectedRevision: 7,
      observedArtifactChecksum: 'a'.repeat(64),
      observedMaskChecksum: 'b'.repeat(64),
    });
  });

  it('selects an inpaint candidate with the image revision guard', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      id: 'image-1',
      inpaintCandidate: 'lineart-guided',
      revision: 8,
    }));

    await api.selectInpaintCandidate('image-1', 'lineart-guided', 7);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/images/image-1/inpaint-candidate');
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PATCH');
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('If-Match')).toBe('7');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      candidateId: 'lineart-guided',
      expectedRevision: 7,
    });
  });

  it('approves a page-scoped classical fallback with the image revision guard', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      id: 'image-1',
      inpaintFallback: { state: 'approved' },
      revision: 8,
    }));

    await api.setInpaintClassicalFallback('image-1', 'approved', 7, {
      reason: 'ai-visible-artifacts',
    });

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      '/api/images/image-1/inpaint-classical-fallback',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PATCH');
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('If-Match')).toBe('7');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      state: 'approved',
      reason: 'ai-visible-artifacts',
      expectedRevision: 7,
    });
  });

  it('reviews only the server-selected AI candidate without sending a candidate id', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      id: 'image-1',
      inpaintAiRejectedCandidateIds: ['ai-a'],
      revision: 8,
    }));

    await api.reviewSelectedInpaintAiCandidate('image-1', 'rejected', 7);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe(
      '/api/images/image-1/inpaint-ai-candidate-review',
    );
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PATCH');
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('If-Match')).toBe('7');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      state: 'rejected',
      expectedRevision: 7,
    });
  });

  it('revokes a page-scoped classical fallback without stale approval evidence', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      id: 'image-1',
      inpaintFallback: { state: 'pending' },
      revision: 9,
    }));

    await api.setInpaintClassicalFallback('image-1', 'pending', 8);

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      state: 'pending',
      expectedRevision: 8,
    });
  });

  it('keeps the delete revision guard in the expectedRevision query', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

    await api.deleteRegion('region-1', 7);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/regions/region-1?expectedRevision=7');
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('If-Match')).toBe('7');
  });

  it('uses a strict G4 create whitelist with image and lineage CAS', async () => {
    const region = regionFixture('region-local', {
      paragraphGroupId: 'paragraph-1',
      contentDisposition: 'translate',
      sourceText: '不得发送',
      translationText: '不得发送',
      confidence: 0.9,
      ignored: true,
      confirmed: true,
    });
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(region));
    const lineage = {
      runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 8,
      actor: { actorKind: 'human' as const, sessionId: 'session-1', operationSource: 'ui' as const },
    };

    await api.createG4Region('image-1', region, 12, lineage);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/images/image-1/regions');
    const body = JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body));
    expect(body).toEqual({
      x: region.x,
      y: region.y,
      width: region.width,
      height: region.height,
      rotation: region.rotation,
      type: region.type,
      direction: region.direction,
      order: region.order,
      paragraphGroupId: 'paragraph-1',
      rubyParentId: null,
      contentDisposition: 'translate',
      expectedImageRevision: 12,
      lineage,
    });
    expect(body).not.toHaveProperty('sourceText');
    expect(body).not.toHaveProperty('style');
    expect(body).not.toHaveProperty('repair');
    expect(body).not.toHaveProperty('detectorJobItemId');
  });

  it('preserves explicit G4 nulls and sends delete CAS in a JSON body', async () => {
    const lineage = {
      runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 9,
      actor: { actorKind: 'human' as const, actorId: 'reviewer-1', operationSource: 'ui' as const },
    };
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(regionFixture('region-1')))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await api.updateG4Region('region-1', {
      paragraphGroupId: null,
      rubyParentId: null,
      contentDisposition: 'false-positive',
    }, 7, 13, lineage);
    await api.deleteG4Region('region-1', 8, 14, { ...lineage, expectedSequence: 10 });

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      paragraphGroupId: null,
      rubyParentId: null,
      contentDisposition: 'false-positive',
      expectedRevision: 7,
      expectedImageRevision: 13,
      lineage,
    });
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe('/api/regions/region-1');
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      expectedRevision: 8,
      expectedImageRevision: 14,
      lineage: { ...lineage, expectedSequence: 10 },
    });
  });

  it('sends the complete G4 order and acceptance evidence', async () => {
    const lineage = {
      runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 11,
      actor: { actorKind: 'human' as const, sessionId: 'session-1', operationSource: 'ui' as const },
    };
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse([regionFixture('region-2'), regionFixture('region-1')]))
      .mockResolvedValueOnce(jsonResponse({
        imageId: 'image-1', imageRevision: 16, generationId: 'generation-1', nextSequence: 12,
        event: { id: 'event-11' },
      }));

    await api.reorderG4Regions('image-1', ['region-2', 'region-1'], 15, lineage);
    await api.acceptRegionsGate('image-1', 'a'.repeat(64), 16, {
      ...lineage, expectedSequence: 12,
    });

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      regionIds: ['region-2', 'region-1'],
      expectedImageRevision: 15,
      lineage,
    });
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      decision: 'accept',
      reason: 'all-region-decisions-reviewed',
      observedRegionChecksum: 'a'.repeat(64),
      expectedRevision: 16,
      lineage: { ...lineage, expectedSequence: 12 },
    });
  });

  it('uses strict G5 endpoints and never accepts a client-supplied reviewer', async () => {
    const lineage = {
      runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 8,
      actor: { actorKind: 'human' as const, sessionId: 'session-1', operationSource: 'ui' as const },
    };
    const saved = regionFixture('region-1', {
      backgroundCategory: 'white-solid',
      backgroundConfidence: 0,
      backgroundRationaleCodes: ['uniform-near-white'],
      backgroundReviewer: lineage.actor,
      backgroundGenerationId: 'generation-1',
    });
    const backgroundContext = {
      imageId: 'image-1', imageRevision: 17, generationId: 'generation-1', nextSequence: 9,
      g4Checksum: 'a'.repeat(64), backgroundChecksum: 'b'.repeat(64), state: 'pending',
      eligibleRegionIds: ['region-1'], classifiedRegionIds: ['region-1'],
    } as const;
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(backgroundContext))
      .mockResolvedValueOnce(jsonResponse(saved))
      .mockResolvedValueOnce(jsonResponse({
        imageId: 'image-1', imageRevision: 18, generationId: 'generation-1', nextSequence: 10,
        event: { id: 'event-9' },
      }));

    await api.getBackgroundGateContext('image-1');
    await api.updateBackgroundClassification('region-1', {
      category: 'white-solid',
      confidence: 0,
      rationaleCodes: ['uniform-near-white'],
      expectedRevision: 4,
      expectedImageRevision: 17,
      lineage,
    });
    await api.acceptBackgroundGate(
      'image-1',
      'all-eligible-backgrounds-reviewed',
      'b'.repeat(64),
      17,
      { ...lineage, expectedSequence: 9 },
    );

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/images/image-1/page-gates/background');
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe('/api/regions/region-1/background-classification');
    const classificationBody = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body));
    expect(classificationBody).toEqual({
      category: 'white-solid',
      confidence: 0,
      rationaleCodes: ['uniform-near-white'],
      expectedRevision: 4,
      expectedImageRevision: 17,
      lineage,
    });
    expect(classificationBody).not.toHaveProperty('reviewer');
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({
      decision: 'accept',
      reason: 'all-eligible-backgrounds-reviewed',
      observedBackgroundChecksum: 'b'.repeat(64),
      expectedRevision: 17,
      lineage: { ...lineage, expectedSequence: 9 },
    });
  });

  it('uses strict G6 endpoints and keeps reviewer, generation, and attempts server-owned', async () => {
    const lineage = {
      runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 12,
      actor: { actorKind: 'human' as const, sessionId: 'session-1', operationSource: 'ui' as const },
    };
    const checks = [
      'original-and-quality-compared',
      'source-text-characters-checked',
      'punctuation-checked',
      'direction-checked',
      'reading-order-checked',
      'empty-or-garbled-checked',
      'duplicate-fragment-checked',
      'template-contamination-checked',
      'page-text-consistency-checked',
    ] as const;
    const saved = regionFixture('region-1', {
      sourceText: '正しい原文',
      ocrReview: {
        sourceMode: 'quality-attempt',
        selectedAttemptId: 'attempt-quality',
        sourceTextChecksum: 'c'.repeat(64),
        qcChecks: [...checks],
        qcFlags: ['none'],
      },
      ocrReviewer: lineage.actor,
      ocrGenerationId: 'generation-1',
    });
    const ocrContext = {
      imageId: 'image-1', imageRevision: 21, generationId: 'generation-1', nextSequence: 12,
      g5Checksum: 'a'.repeat(64), ocrChecksum: 'b'.repeat(64), state: 'pending',
      eligibleRegionIds: ['region-1'], attemptedRegionIds: ['region-1'],
      reviewedRegionIds: [], attempts: [],
    } as const;
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(ocrContext))
      .mockResolvedValueOnce(jsonResponse(saved))
      .mockResolvedValueOnce(jsonResponse({
        imageId: 'image-1', imageRevision: 22, generationId: 'generation-1', nextSequence: 14,
        event: { id: 'event-13' },
      }));

    await api.getOCRGateContext('image-1');
    await api.updateOCRSourceReview('region-1', {
      sourceText: '正しい原文',
      sourceMode: 'quality-attempt',
      selectedAttemptId: 'attempt-quality',
      qcChecks: [...checks],
      expectedRevision: 4,
      expectedImageRevision: 21,
      lineage,
    });
    await api.acceptOCRGate(
      'image-1',
      'all-translatable-source-text-reviewed',
      'b'.repeat(64),
      21,
      { ...lineage, expectedSequence: 13 },
    );

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/images/image-1/page-gates/ocr');
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe('/api/regions/region-1/ocr-source-review');
    const reviewBody = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body));
    expect(reviewBody).toEqual({
      sourceText: '正しい原文',
      sourceMode: 'quality-attempt',
      selectedAttemptId: 'attempt-quality',
      qcChecks: [...checks],
      expectedRevision: 4,
      expectedImageRevision: 21,
      lineage,
    });
    expect(reviewBody).not.toHaveProperty('reviewer');
    expect(reviewBody).not.toHaveProperty('generationId');
    expect(reviewBody).not.toHaveProperty('attempts');
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({
      decision: 'accept',
      reason: 'all-translatable-source-text-reviewed',
      observedOCRChecksum: 'b'.repeat(64),
      expectedRevision: 21,
      lineage: { ...lineage, expectedSequence: 13 },
    });
  });

  it('uses strict G7 draft, artifact, and structured review contracts', async () => {
    const lineage = {
      runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 14,
      actor: { actorKind: 'human' as const, sessionId: 'session-1', operationSource: 'ui' as const },
    };
    const recipe = { regionId: 'region-1', maskMode: 'manual' as const,
      polygon: [[1, 2], [10, 2], [10, 12]] as Array<[number, number]>, padding: 4, dilation: 2,
      feather: 1, polarity: 'auto' as const,
      maskEdits: { version: 1 as const, strokes: [{ mode: 'add' as const, radius: 8, points: [[3, 4]] as Array<[number, number]> }] } };
    const context = { imageId: 'image-1' };
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(context))
      .mockResolvedValueOnce(jsonResponse(context))
      .mockResolvedValueOnce(jsonResponse(context));
    await api.getMaskGateContext('image-1');
    await api.updateMaskDraft('image-1', { regions: [recipe], expectedRevision: 20, lineage });
    const coverageChecks = MASK_COVERAGE_CHECKS.map((check) => ({ check, passed: true }));
    const collateralChecks = MASK_COLLATERAL_CHECKS.map((check) => ({ check, passed: true }));
    await api.reviewMaskGate('image-1', { decision: 'accept', reason: 'complete-and-no-collateral',
      selectedArtifactId: 'artifact-1', observedMaskChecksum: 'a'.repeat(64), coverageChecks,
      collateralChecks, expectedRevision: 21, lineage: { ...lineage, expectedSequence: 18 } });
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/images/image-1/page-gates/mask');
    expect(String(fetchMock.mock.calls[1]?.[0])).toBe('/api/images/image-1/page-gates/mask/draft');
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({ regions: [recipe], expectedRevision: 20, lineage });
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({ decision: 'accept',
      reason: 'complete-and-no-collateral', selectedArtifactId: 'artifact-1',
      observedMaskChecksum: 'a'.repeat(64), coverageChecks, collateralChecks, expectedRevision: 21,
      lineage: { ...lineage, expectedSequence: 18 } });
    expect(api.maskArtifactUrl('image 1', 'artifact/1')).toBe('/api/images/image%201/page-gates/mask/artifacts/artifact%2F1');
  });

  it('uses strict G8 candidate, review, and page-scoped fallback contracts', async () => {
    const lineage = {
      runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 22,
      actor: { actorKind: 'human' as const, sessionId: 'session-1', operationSource: 'ui' as const },
    };
    const context = { imageId: 'image-1' };
    const fetchMock = vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(context))
      .mockResolvedValueOnce(jsonResponse(context))
      .mockResolvedValueOnce(jsonResponse(context));
    const checks = CLEAN_PLATE_CHECKS.map((check) => ({ check, passed: true }));
    await api.getCleanPlateGateContext('image-1');
    await api.reviewCleanPlateGate('image-1', {
      decision: 'accept', reason: 'clean-plate-complete', candidateId: 'candidate-1',
      observedCandidateChecksum: 'c'.repeat(64), observedWidth: 2400, observedHeight: 3600,
      checks, expectedRevision: 24, lineage,
    });
    await api.setCleanPlateFallback('image-1', {
      enabled: true, reason: 'all-ai-candidates-rejected', expectedRevision: 25,
      lineage: { ...lineage, expectedSequence: 23 },
    });
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/images/image-1/page-gates/clean-plate');
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      decision: 'accept', reason: 'clean-plate-complete', candidateId: 'candidate-1',
      observedCandidateChecksum: 'c'.repeat(64), observedWidth: 2400, observedHeight: 3600,
      checks, expectedRevision: 24, lineage,
    });
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toEqual({
      enabled: true, reason: 'all-ai-candidates-rejected', expectedRevision: 25,
      lineage: { ...lineage, expectedSequence: 23 },
    });
    expect(api.cleanPlateCandidateUrl('image 1', 'candidate/1')).toBe(
      '/api/images/image%201/page-gates/clean-plate/candidates/candidate%2F1',
    );
  });

  it('uses dedicated G9 candidate/review APIs without client-claimed provenance', async () => {
    const lineage = { runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 31,
      actor: { actorKind: 'human' as const, sessionId: 'session-1', operationSource: 'ui' as const } };
    const checks = TRANSLATION_QC_CHECKS.map((check) => ({ check, passed: true }));
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse({}));
    await api.getTranslationGateContext('image-1');
    await api.createTranslationCandidate('image-1', {
      regionId: 'region-1', translationText: '别开玩笑了！', originKind: 'manual',
      observedG8Checksum: '8'.repeat(64), observedSourceTextChecksum: '6'.repeat(64),
      observedContextChecksum: '7'.repeat(64), observedTranslationStateChecksum: '9'.repeat(64),
      expectedRevision: 20, lineage,
    });
    await api.reviewTranslationCandidate('image-1', 'candidate-1', {
      decision: 'accept', reason: 'translation-reviewed', observedCandidateChecksum: 'a'.repeat(64),
      observedSourceTextChecksum: '6'.repeat(64), observedContextChecksum: '7'.repeat(64), observedG8Checksum: '8'.repeat(64),
      checks, qcFlags: ['none'], expectedRevision: 21, lineage: { ...lineage, expectedSequence: 32 },
    });
    await api.reviewTranslationGate('image-1', {
      decision: 'accept', observedTranslationStateChecksum: 'b'.repeat(64),
      expectedRevision: 22, lineage: { ...lineage, expectedSequence: 33 },
    });
    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      '/api/images/image-1/page-gates/translation',
      '/api/images/image-1/page-gates/translation/candidates',
      '/api/images/image-1/page-gates/translation/candidates/candidate-1',
      '/api/images/image-1/page-gates/translation',
    ]);
    const revisionBody = JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body));
    expect(revisionBody).not.toHaveProperty('provider');
    expect(revisionBody).not.toHaveProperty('modelVersion');
    expect(revisionBody).not.toHaveProperty('parameterHash');
    expect(revisionBody).not.toHaveProperty('confirmed');
    expect(revisionBody.translationText).toBe('别开玩笑了！');
    expect(JSON.parse(String(fetchMock.mock.calls[2]?.[1]?.body))).toMatchObject({
      checks, qcFlags: ['none'], observedCandidateChecksum: 'a'.repeat(64),
    });
  });

  it('uses the strict G10 context, immutable candidate URL, and exact review contract', async () => {
    const lineage = { runId: 'run-1', pageGenerationId: 'generation-1', expectedSequence: 41,
      actor: { actorKind: 'human' as const, sessionId: 'session-1', operationSource: 'ui' as const } };
    const checks = TYPESET_CHECKS.map((check) => ({ check, passed: true }));
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => jsonResponse({}));

    await api.getTypesetGateContext('image-1');
    await api.reviewTypesetCandidate('image-1', 'candidate/1', {
      decision: 'accept', reason: 'typeset-reviewed',
      observedCandidateChecksum: '1'.repeat(64), observedRouteChecksum: '2'.repeat(64),
      observedStyleChecksum: '3'.repeat(64), observedLayoutChecksum: '4'.repeat(64),
      observedTranslationTerminalChecksum: '5'.repeat(64),
      observedCleanPlateChecksum: '6'.repeat(64), observedWidth: 2400,
      observedHeight: 3600, observedRenderScale: 2, checks, expectedRevision: 27, lineage,
    });

    expect(fetchMock.mock.calls.map((call) => call[0])).toEqual([
      '/api/images/image-1/page-gates/typeset',
      '/api/images/image-1/page-gates/typeset/candidates/candidate%2F1',
    ]);
    expect(JSON.parse(String(fetchMock.mock.calls[1]?.[1]?.body))).toEqual({
      decision: 'accept', reason: 'typeset-reviewed',
      observedCandidateChecksum: '1'.repeat(64), observedRouteChecksum: '2'.repeat(64),
      observedStyleChecksum: '3'.repeat(64), observedLayoutChecksum: '4'.repeat(64),
      observedTranslationTerminalChecksum: '5'.repeat(64),
      observedCleanPlateChecksum: '6'.repeat(64), observedWidth: 2400,
      observedHeight: 3600, observedRenderScale: 2, checks, expectedRevision: 27, lineage,
    });
    expect(api.typesetCandidateUrl('image 1', 'candidate/1')).toBe(
      '/api/images/image%201/page-gates/typeset/candidates/candidate%2F1',
    );
  });

  it('carries the active page generation on a detect job request', async () => {
    const lineage = {
      runId: 'run-1',
      actor: { actorKind: 'human' as const, sessionId: 'session-1', operationSource: 'ui' as const },
      pages: [{ imageId: 'image-1', pageGenerationId: 'generation-1', expectedSequence: 8 }],
    };
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      id: 'job-1', projectId: 'project-1', kind: 'detect', status: 'queued', items: [],
    }));

    await api.startJob('project-1', 'detect', {
      imageIds: ['image-1'], options: { provider: 'ppocr-v3' }, lineage,
    });

    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      imageIds: ['image-1'], options: { provider: 'ppocr-v3' }, lineage,
    });
  });

  it('uses projectId when listing jobs', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse([]));
    await api.listJobs('project 中文');
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/jobs?projectId=project+%E4%B8%AD%E6%96%87');
  });

  it('marks browser directory uploads so the selected root is not persisted', async () => {
    const file = new File(['image'], '001.png', { type: 'image/png' });
    Object.defineProperty(file, 'webkitRelativePath', {
      value: 'input 漫画/第一章/001.png',
    });
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse([]));

    await api.uploadImages('project-1', [file]);

    const body = fetchMock.mock.calls[0]?.[1]?.body as FormData;
    expect(body.get('strip_common_root')).toBe('true');
    expect(body.get('relative_paths')).toBe('["input 漫画/第一章/001.png"]');
  });

  it('configures an OpenAI-compatible key in the session-only endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      providers: {
        translation: {
          'openai-compatible': { available: true, configurable: true, remote: true },
        },
      },
      capabilities: {},
    }));

    const result = await api.setSessionCredential(
      'openai-compatible',
      'session-secret',
      'https://example.test/v1',
      'local-model',
    );

    expect(result.configured).toBe(true);
    expect(result.capabilities.providers).toContainEqual(expect.objectContaining({
      id: 'openai-compatible',
      available: true,
      configurable: true,
    }));
    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/config/translation/openai-session');
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('PUT');
    expect(JSON.parse(String(fetchMock.mock.calls[0]?.[1]?.body))).toEqual({
      apiKey: 'session-secret',
      baseUrl: 'https://example.test/v1',
      model: 'local-model',
    });
  });

  it('exposes revision conflicts instead of treating them as a successful save', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({
      message: 'revision mismatch',
      actualRevision: 8,
      expectedRevision: 7,
    }, 409));

    await expect(api.updateRegion('region-1', {
      translationText: '冲突',
      expectedRevision: 7,
    })).rejects.toMatchObject({
      status: 409,
      message: 'revision mismatch',
      conflict: { actualRevision: 8, expectedRevision: 7 },
    });
  });

  it('routes erased and typeset previews to generated artifacts', () => {
    expect(api.contentUrl('image-1', 'original')).toBe('/api/images/image-1/content');
    expect(api.contentUrl('image-1', 'erased')).toBe('/api/images/image-1/generated/inpainted');
    expect(api.contentUrl('image-1', 'typeset')).toBe('/api/images/image-1/generated/typeset');
    expect(api.contentUrl('image-1', 'typeset', 7)).toBe(
      '/api/images/image-1/generated/typeset?v=7',
    );
  });
});
