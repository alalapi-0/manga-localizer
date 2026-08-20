import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from './client';
import { regionFixture } from '../test/fixtures';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('api client contract', () => {
  afterEach(() => vi.restoreAllMocks());

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
    expect(body).toMatchObject({ x: draft.x, sourceText: draft.sourceText });
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

  it('keeps the delete revision guard in the expectedRevision query', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(null, { status: 204 }));

    await api.deleteRegion('region-1', 7);

    expect(String(fetchMock.mock.calls[0]?.[0])).toBe('/api/regions/region-1?expectedRevision=7');
    expect(new Headers(fetchMock.mock.calls[0]?.[1]?.headers).get('If-Match')).toBe('7');
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
