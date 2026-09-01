import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { resetWorkbenchStore, useWorkbenchStore } from '../store/workbench';
import { imageFixture, projectFixture, regionFixture, seedWorkbench } from '../test/fixtures';
import type {
  CleanPlateCandidate,
  CleanPlateGateContext,
  CleanPlateCandidateReview,
  MaskGateContext,
  OCRGateContext,
  PageGeneration,
  TranslationGateContext,
  TypesetCandidate,
  TypesetCandidateReview,
  TypesetGateContext,
  TypesetRegionStyle,
} from '../types';
import { CLEAN_PLATE_CHECKS, TRANSLATION_QC_CHECKS, TYPESET_CHECKS } from '../types';
import { Inspector } from './Inspector';

const checksum = (character: string) => character.repeat(64);

const cleanPlateCheckLabels = {
  'outside-mask-unchanged': 'mask 外像素完全未变化',
  'source-text-unreadable': '原日文已不可读',
  'no-white-or-gray-hole': '无白洞、灰块或硬色块',
  'no-blur-band': '无模糊带',
  'no-repeated-texture': '无重复纹理',
  'background-continuous': '背景、渐变与网点连续',
  'structure-preserved': '线条、人物与结构未受损',
} as const;

function generation(): PageGeneration {
  return {
    id: 'generation-1',
    runId: 'run-1',
    projectId: 'project-1',
    imageId: 'image-1',
    restartFromSource: true,
    parameterSetId: 'params-1',
    parameterSetHash: checksum('1'),
    sourceProjectId: 'project-1',
    sourceImageId: 'image-1',
    sourceChecksum: checksum('2'),
    state: 'active',
    nextSequence: 22,
    actor: { actorKind: 'codex', taskId: 'task-1', operationSource: 'api' },
    createdAt: '2026-08-25T00:00:00Z',
    closedAt: null,
  };
}

function candidate(review: CleanPlateCandidateReview | null = null): CleanPlateCandidate {
  return {
    candidateId: 'candidate-1',
    sequence: 1,
    jobId: 'job-inpaint',
    jobItemId: 'item-inpaint',
    parentChecksum: checksum('3'),
    qualityChecksum: checksum('4'),
    backgroundChecksum: checksum('5'),
    maskArtifactId: 'mask-accepted',
    maskChecksum: checksum('6'),
    routeManifest: [{
      regionId: 'region-1',
      backgroundCategory: 'complex-lineart',
      route: 'ai-inpaint-redraw',
      originKind: 'ai',
      provider: 'lama',
      modelVersion: 'lama-onnx-local-v1',
      parameterHash: checksum('7'),
    }],
    routeChecksum: checksum('8'),
    originKind: 'ai',
    providerIds: ['lama'],
    modelVersions: ['lama-onnx-local-v1'],
    parameterHash: checksum('7'),
    candidateChecksum: checksum('9'),
    width: 1200,
    height: 1800,
    renderScale: 1,
    outsideMaskChangeCount: 0,
    anomalies: [],
    completed: true,
    review,
    createdAt: '2026-08-25T00:00:00Z',
  };
}

function context(
  currentCandidate = candidate(),
  overrides: Partial<CleanPlateGateContext> = {},
): CleanPlateGateContext {
  return {
    imageId: 'image-1',
    imageRevision: 11,
    generationId: 'generation-1',
    nextSequence: 22,
    g7Checksum: checksum('3'),
    qualityChecksum: checksum('4'),
    backgroundChecksum: checksum('5'),
    maskArtifactId: 'mask-accepted',
    maskChecksum: checksum('6'),
    cleanPlateStateChecksum: checksum('a'),
    state: 'pending',
    routes: [{
      regionId: 'region-1',
      backgroundCategory: 'complex-lineart',
      defaultRoute: 'ai-inpaint-redraw',
    }],
    candidates: [currentCandidate],
    acceptedCandidateId: null,
    fallbackEnabled: false,
    fallbackAllowed: false,
    ...overrides,
  };
}

function seedG8Inspector(cleanPlate = context()) {
  const image = imageFixture('image-1', { revision: 11, width: 1200, height: 1800 });
  const region = regionFixture('region-1', {
    contentDisposition: 'translate',
    backgroundCategory: 'complex-lineart',
    backgroundGenerationId: 'generation-1',
  });
  seedWorkbench({ images: [image], regions: [region] });
  const ocrContext: OCRGateContext = {
    imageId: 'image-1',
    imageRevision: 11,
    generationId: 'generation-1',
    nextSequence: 22,
    g5Checksum: checksum('b'),
    ocrChecksum: checksum('c'),
    state: 'accepted',
    eligibleRegionIds: ['region-1'],
    attemptedRegionIds: ['region-1'],
    reviewedRegionIds: ['region-1'],
    attempts: [],
  };
  const maskContext: MaskGateContext = {
    imageId: 'image-1',
    imageRevision: 11,
    generationId: 'generation-1',
    nextSequence: 22,
    g6Checksum: checksum('c'),
    qualityChecksum: checksum('4'),
    maskStateChecksum: checksum('3'),
    state: 'accepted',
    eligibleRegionIds: ['region-1'],
    rubyRegionIdsByPrimary: { 'region-1': [] },
    draft: { revision: 1, stateChecksum: checksum('d'), regions: [] },
    artifacts: [{
      artifactId: 'mask-accepted',
      sequence: 1,
      jobId: 'job-mask',
      jobItemId: 'item-mask',
      parentChecksum: checksum('c'),
      maskChecksum: checksum('6'),
      recipeChecksum: checksum('d'),
      qualityChecksum: checksum('4'),
      renderScale: 1,
      provider: 'deterministic-mask',
      modelVersion: 'create-mask-v1',
      parameterHash: checksum('d'),
      width: 1200,
      height: 1800,
      nonzeroPixelCount: 42,
      bbox: { x: 1, y: 2, width: 3, height: 4 },
      createdAt: '2026-08-25T00:00:00Z',
    }],
    selectedArtifactId: 'mask-accepted',
    review: {
      id: 'mask-review',
      state: 'accepted',
      reason: 'complete-and-no-collateral',
      artifactId: 'mask-accepted',
      maskChecksum: checksum('6'),
      coverageChecks: [],
      collateralChecks: [],
      reviewer: { actorKind: 'human', sessionId: 'reviewer', operationSource: 'ui' },
      createdAt: '2026-08-25T00:00:00Z',
    },
  };
  useWorkbenchStore.setState({
    g4Contexts: {
      'image-1': {
        status: 'active',
        generation: generation(),
        events: [],
        phase: 'G8',
        error: '',
        conflict: false,
      },
    },
    ocrContexts: { 'image-1': ocrContext },
    maskContexts: { 'image-1': maskContext },
    cleanPlateContexts: { 'image-1': cleanPlate },
    selectedCleanPlateCandidateIds: { 'image-1': 'candidate-1' },
  });
}

function setCleanPlate(nextContext: CleanPlateGateContext) {
  act(() => useWorkbenchStore.setState((state) => ({
    cleanPlateContexts: { ...state.cleanPlateContexts, 'image-1': nextContext },
  })));
}

describe('G8 clean plate Inspector', () => {
  afterEach(() => {
    cleanup();
    resetWorkbenchStore();
    vi.restoreAllMocks();
  });

  it('requires exact four-view evidence and keeps fallback and terminal controls fail-closed', () => {
    const initialContext = context();
    seedG8Inspector(initialContext);
    render(<Inspector />);

    expect(screen.getByText('Provenance：ai')).toBeInTheDocument();
    expect(screen.getByText('ai-inpaint-redraw')).toBeInTheDocument();
    expect(screen.getByText(/lama · lama-onnx-local-v1/)).toBeInTheDocument();
    expect(screen.getByText(/parameter 777777777777/)).toBeInTheDocument();
    expect(screen.getByText(/accepted mask mask-accept/)).toBeInTheDocument();

    const accept = screen.getByRole('button', { name: '接受当前 clean plate' });
    const reject = screen.getByRole('button', { name: '拒绝当前候选' });
    expect(accept).toBeDisabled();
    expect(reject).toBeDisabled();
    CLEAN_PLATE_CHECKS.forEach((check) => {
      expect(screen.getByRole('checkbox', { name: cleanPlateCheckLabels[check] })).toBeDisabled();
    });

    act(() => useWorkbenchStore.getState().observeG8CleanPlateBitmap({
      imageId: 'image-1',
      generationId: 'generation-1',
      nextSequence: 22,
      cleanPlateStateChecksum: checksum('a'),
      candidateId: 'candidate-1',
      imageRevision: 11,
      sourceChecksum: checksum('2'),
      qualityChecksum: checksum('4'),
      maskArtifactId: 'mask-accepted',
      maskChecksum: checksum('6'),
      maskWidth: 1200,
      maskHeight: 1800,
      checksum: checksum('9'),
      width: 1200,
      height: 1800,
      state: 'ready',
    }));

    expect(reject).toBeEnabled();
    expect(accept).toBeDisabled();
    CLEAN_PLATE_CHECKS.forEach((check) => {
      fireEvent.click(screen.getByRole('checkbox', { name: cleanPlateCheckLabels[check] }));
    });
    expect(accept).toBeEnabled();
    expect(reject).toBeDisabled();

    const rejectedReview: CleanPlateCandidateReview = {
      id: 'review-rejected',
      state: 'rejected',
      reason: 'structure-damaged',
      checks: CLEAN_PLATE_CHECKS.map((check) => ({
        check,
        passed: check !== 'structure-preserved',
      })),
      reviewer: { actorKind: 'human', sessionId: 'reviewer', operationSource: 'ui' },
      createdAt: '2026-08-25T00:01:00Z',
    };
    const fallbackReady = context(candidate(rejectedReview), { fallbackAllowed: true });
    setCleanPlate(fallbackReady);
    expect(screen.getByRole('button', { name: '开启本页 classical fallback' })).toBeEnabled();

    setCleanPlate({ ...fallbackReady, fallbackEnabled: true });
    expect(screen.getByRole('button', { name: '传统回退已开启，请使用下方专用操作' }))
      .toBeDisabled();
    expect(screen.getByRole('button', { name: '生成 classical 候选' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '关闭回退并恢复 AI' })).toBeEnabled();

    const acceptedReview: CleanPlateCandidateReview = {
      ...rejectedReview,
      id: 'review-accepted',
      state: 'accepted',
      reason: 'clean-plate-complete',
      checks: CLEAN_PLATE_CHECKS.map((check) => ({ check, passed: true })),
    };
    const acceptedCandidate = candidate(acceptedReview);
    setCleanPlate(context(acceptedCandidate, {
      state: 'accepted',
      acceptedCandidateId: acceptedCandidate.candidateId,
    }));

    expect(screen.getByText('clean plate 已接受')).toBeInTheDocument();
    expect(screen.getByText('不可变结论：accepted')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'G8 不可变 clean plate 候选' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '开启本页 classical fallback' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '接受当前 clean plate' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '拒绝当前候选' })).not.toBeInTheDocument();
  });
});

describe('G9 translation Inspector', () => {
  afterEach(() => {
    cleanup();
    resetWorkbenchStore();
    vi.restoreAllMocks();
  });

  it('shows accepted clean-plate provenance, ruby exclusion, immutable candidate QC, and blocks hard-failed acceptance', () => {
    const image = imageFixture('image-1', { revision: 11 });
    const region = regionFixture('region-1', {
      order: 1, sourceText: 'ふざけるな！', type: 'dialogue', direction: 'vertical',
      contentDisposition: 'translate',
    });
    seedWorkbench({ images: [image], regions: [region] });
    const translation: TranslationGateContext = {
      imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 22,
      g8Checksum: checksum('3'), cleanPlateCandidateId: 'clean-1',
      cleanPlateChecksum: checksum('4'), targetLanguage: 'zh-CN',
      translationStateChecksum: checksum('5'), terminalChecksum: null, state: 'pending',
      eligibleRegions: [{
        regionId: 'region-1', readingOrder: 1, regionType: 'dialogue', direction: 'vertical',
        paragraphGroupId: null, sourceText: 'ふざけるな！', sourceTextChecksum: checksum('6'),
        contextRegionIds: [], contextChecksum: checksum('7'), rubyExcluded: true,
      }],
      candidates: [{
        candidateId: 'translation-1', sequence: 21, regionId: 'region-1', revisionNumber: 1,
        supersedesCandidateId: null, originKind: 'model', provider: 'argos-ja-zh',
        modelVersion: 'argos-local-v1', parameterHash: checksum('8'), targetLanguage: 'zh-CN',
        g8Checksum: checksum('3'), cleanPlateChecksum: checksum('4'),
        sourceTextChecksum: checksum('6'), sourceRegionRevision: region.revision,
        contextChecksum: checksum('7'), translationText: '联系我们', candidateChecksum: checksum('9'),
        computedQcFlags: ['forbidden-template'], jobId: 'job-1', jobItemId: 'item-1',
        revisionId: 'revision-1', review: null, createdAt: '2026-08-25T00:00:00Z',
      }],
      acceptedCandidateIdsByRegion: {}, reviewedRegionCount: 0,
    };
    useWorkbenchStore.setState({
      g4Contexts: { 'image-1': { status: 'active', generation: generation(), events: [],
        phase: 'G9', error: '', conflict: false } },
      translationContexts: { 'image-1': translation },
      selectedTranslationCandidateIds: { 'image-1': 'translation-1' },
    });
    render(<Inspector />);
    expect(screen.getByText(/Accepted clean plate 是唯一图像父项/)).toBeInTheDocument();
    expect(screen.getByText(/ruby 不在 eligible 集合/)).toBeInTheDocument();
    expect(screen.getByText(/server QC: forbidden-template/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '接受当前译文' })).toBeDisabled();
    expect(screen.getAllByRole('checkbox')).toHaveLength(19);
    expect(screen.queryByRole('button', { name: '生成整页翻译候选' })).not.toBeInTheDocument();
    expect(TRANSLATION_QC_CHECKS).toHaveLength(10);
  });

  it('lets a manual project create a nonempty parent-null first revision per eligible non-ruby region', () => {
    const image = imageFixture('image-1', { revision: 11 });
    const primary = regionFixture('region-1', { order: 1, sourceText: '待って！', type: 'dialogue',
      direction: 'vertical', contentDisposition: 'translate' });
    const ruby = regionFixture('ruby-1', { order: 2, sourceText: 'ま', type: 'ruby',
      rubyParentId: 'region-1', contentDisposition: 'ignore' });
    seedWorkbench({ images: [image], regions: [primary, ruby] });
    const revise = vi.fn(async () => true);
    useWorkbenchStore.setState({
      g4Contexts: { 'image-1': { status: 'active', generation: generation(), events: [],
        phase: 'G9', error: '', conflict: false } },
      translationContexts: { 'image-1': {
        imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 22,
        g8Checksum: checksum('3'), cleanPlateCandidateId: 'clean-1', cleanPlateChecksum: checksum('4'),
        targetLanguage: 'zh-CN', translationStateChecksum: checksum('3'), terminalChecksum: null,
        state: 'pending', eligibleRegions: [{ regionId: 'region-1', readingOrder: 1,
          regionType: 'dialogue', direction: 'vertical', paragraphGroupId: null,
          sourceText: '待って！', sourceTextChecksum: checksum('6'), contextRegionIds: [],
          contextChecksum: checksum('7'), rubyExcluded: true }], candidates: [],
        acceptedCandidateIdsByRegion: {}, reviewedRegionCount: 0,
      } },
      selectedTranslationCandidateIds: {},
      reviseG9Translation: revise,
    });
    render(<Inspector />);
    expect(screen.getByText('当前 provider 仅支持 revision')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '生成整页翻译候选' })).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'G9 首候选 region' })).toHaveTextContent('待って！');
    expect(screen.getByRole('combobox', { name: 'G9 首候选 region' })).not.toHaveTextContent('ま');
    const create = screen.getByRole('button', { name: '创建首个 revision' });
    expect(create).toBeDisabled();
    fireEvent.change(screen.getByRole('textbox', { name: 'G9 首候选译文' }), {
      target: { value: '等等！' },
    });
    expect(create).toBeEnabled();
    fireEvent.click(create);
    expect(revise).toHaveBeenCalledWith('region-1', '等等！', 'manual');
  });

  it('keeps dictionary projects revision-only while preserving candidate revision controls', () => {
    const image = imageFixture('image-1', { revision: 11 });
    const region = regionFixture('region-1', {
      order: 1,
      sourceText: '待って！',
      type: 'dialogue',
      direction: 'vertical',
      contentDisposition: 'translate',
    });
    const project = projectFixture({
      settings: { ...projectFixture().settings, translatorProvider: 'dictionary' },
    });
    seedWorkbench({ project, images: [image], regions: [region] });
    const revise = vi.fn(async () => true);
    useWorkbenchStore.setState({
      g4Contexts: { 'image-1': { status: 'active', generation: generation(), events: [],
        phase: 'G9', error: '', conflict: false } },
      translationContexts: { 'image-1': {
        imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 22,
        g8Checksum: checksum('3'), cleanPlateCandidateId: 'clean-1', cleanPlateChecksum: checksum('4'),
        targetLanguage: 'zh-CN', translationStateChecksum: checksum('3'), terminalChecksum: null,
        state: 'pending', eligibleRegions: [{ regionId: 'region-1', readingOrder: 1,
          regionType: 'dialogue', direction: 'vertical', paragraphGroupId: null,
          sourceText: '待って！', sourceTextChecksum: checksum('6'), contextRegionIds: [],
          contextChecksum: checksum('7'), rubyExcluded: true }], candidates: [{
          candidateId: 'translation-1', sequence: 21, regionId: 'region-1', revisionNumber: 1,
          supersedesCandidateId: null, originKind: 'dictionary', provider: 'dictionary',
          modelVersion: 'dictionary-revision-v1', parameterHash: checksum('8'),
          targetLanguage: 'zh-CN', g8Checksum: checksum('3'), cleanPlateChecksum: checksum('4'),
          sourceTextChecksum: checksum('6'), sourceRegionRevision: region.revision,
          contextChecksum: checksum('7'), translationText: '等等！', candidateChecksum: checksum('9'),
          computedQcFlags: ['none'], jobId: null, jobItemId: null,
          revisionId: 'revision-1', review: null, createdAt: '2026-08-25T00:00:00Z',
        }], acceptedCandidateIdsByRegion: {}, reviewedRegionCount: 0,
      } },
      selectedTranslationCandidateIds: { 'image-1': 'translation-1' },
      reviseG9Translation: revise,
    });

    render(<Inspector />);

    expect(screen.getByText('当前 provider 仅支持 revision')).toBeInTheDocument();
    expect(screen.getByText(/manual \/ dictionary 不创建自动整页任务/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '生成整页翻译候选' })).not.toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'G9 不可变译文候选' })).toBeEnabled();
    const revision = screen.getByRole('textbox', { name: 'G9 译文修订' });
    expect(revision).toHaveValue('等等！');

    fireEvent.change(revision, { target: { value: '等一下！' } });
    fireEvent.change(screen.getByDisplayValue('human manual'), { target: { value: 'dictionary' } });
    fireEvent.click(screen.getByRole('button', { name: '创建新 revision（当前候选须先拒绝）' }));
    expect(revise).toHaveBeenCalledWith('region-1', '等一下！', 'dictionary');
  });
});

const regularTypesetFont = {
  token: 'installed-font-aaaaaaaaaaaaaaaaaaaaaaaa', label: 'Regular CJK',
  fontChecksum: checksum('a'),
  capabilityChecksum: 'eaf85c5596a96fe0d5acfe6fbc04511ac0df7f691b5630d4396bd7e93f42c350',
  role: 'regular' as const,
};
const displayTypesetFont = {
  token: 'installed-font-cccccccccccccccccccccccc', label: 'Display CJK',
  fontChecksum: checksum('c'),
  capabilityChecksum: '130d17c1105d15a54da81201476b94e81ff14f9e004981f1c3107b863c74d8dd',
  role: 'display' as const,
};

function typesetStyle(display = false): TypesetRegionStyle {
  return {
    fontToken: display ? displayTypesetFont.token : regularTypesetFont.token,
    fontChecksum: display ? displayTypesetFont.fontChecksum : regularTypesetFont.fontChecksum,
    fontSize: display ? 48 : 32, minFontSize: 12, padding: 4, fill: '#111111',
    strokeColor: '#FFFFFF', strokeWidth: display ? 2 : 1, rotation: 0,
    scaleX: 1, scaleY: 1, shearX: 0, shearY: 0, opacity: 1,
    visualCenterX: 0.5, visualCenterY: 0.5, align: 'center', lineSpacing: 0.15,
    letterSpacing: 0, autoFit: true,
    fontSource: display ? 'server-display-default' : 'server-regular-default',
  };
}

function typesetReview(state: 'accepted' | 'rejected' = 'accepted'): TypesetCandidateReview {
  const rejected = state === 'rejected';
  return {
    id: `typeset-review-${state}`, sequence: 24, candidateId: 'typeset-candidate-1', state,
    reason: rejected ? 'overflow-free' : 'typeset-reviewed', parentChecksum: checksum('3'),
    candidateChecksum: checksum('6'), routeChecksum: checksum('7'), styleChecksum: checksum('8'),
    layoutChecksum: checksum('9'), g9TerminalChecksum: checksum('3'),
    cleanPlateChecksum: checksum('5'), observedWidth: 1200, observedHeight: 1800,
    observedRenderScale: 1, checks: TYPESET_CHECKS.map((check) => ({
      check, passed: !rejected || check !== 'overflow-free',
    })), reviewer: { actorKind: 'human', sessionId: 'reviewer', operationSource: 'ui' },
    terminalChecksum: checksum('f'), revisionId: `revision-review-${state}`,
    createdAt: '2026-08-25T00:03:00Z',
  };
}

function typesetCandidate(
  overrides: Partial<TypesetCandidate> = {},
): TypesetCandidate {
  const routes: TypesetGateContext['routeManifest'] = [
    { regionId: 'sfx-redraw', readingOrder: 1, route: 'art-lettering', renderRequired: true,
      translationCandidateId: 'translation-art', translationCandidateChecksum: checksum('1') },
    { regionId: 'dialogue', readingOrder: 2, route: 'bubble', renderRequired: true,
      translationCandidateId: 'translation-dialogue', translationCandidateChecksum: checksum('2') },
    { regionId: 'sfx-keep', readingOrder: 3, route: 'keep', renderRequired: false,
      translationCandidateId: null, translationCandidateChecksum: null },
    { regionId: 'ignored', readingOrder: 4, route: 'ignore', renderRequired: false,
      translationCandidateId: null, translationCandidateChecksum: null },
  ];
  const styles = { art: typesetStyle(true), regular: typesetStyle() };
  return {
    candidateId: 'typeset-candidate-1', sequence: 22, jobId: 'job-typeset',
    jobItemId: 'item-typeset', parentChecksum: checksum('3'),
    g9TerminalChecksum: checksum('3'), translationStateChecksum: checksum('4'),
    cleanPlateCandidateId: 'clean-1', cleanPlateChecksum: checksum('5'),
    regionManifest: routes.map((route) => ({
      regionId: route.regionId, regionRevision: 4,
      geometry: { x: 10, y: 20, width: 200, height: 100, rotation: 0 },
      readingOrder: route.readingOrder,
      regionType: route.route === 'bubble' ? 'dialogue' : 'sound_effect',
      direction: 'vertical', paragraphGroupId: null,
      contentDisposition: route.route === 'art-lettering' ? 'redraw-art'
        : route.route === 'keep' ? 'keep-art' : route.route === 'ignore' ? 'ignore' : 'translate',
      acceptedTranslationCandidateId: route.translationCandidateId,
      acceptedTranslationCandidateChecksum: route.translationCandidateChecksum,
    })),
    routeManifest: routes, routeChecksum: checksum('7'),
    styleManifest: routes.map((route) => ({ regionId: route.regionId, route: route.route,
      style: route.route === 'art-lettering' ? styles.art
        : route.renderRequired ? styles.regular : null })),
    styleChecksum: checksum('8'),
    layoutManifest: routes.filter((route) => route.renderRequired).map((route) => ({
      regionId: route.regionId, route: route.route as 'bubble' | 'art-lettering',
      bounds: { x: 10, y: 20, width: 200, height: 100 },
      fontSize: route.route === 'art-lettering' ? 48 : 32, overflow: false,
      direction: 'vertical', rotation: 0, scaleX: 1, scaleY: 1, shearX: 0, shearY: 0,
      opacity: 1, visualCenterX: 0.5, visualCenterY: 0.5, align: 'center',
    })),
    layoutChecksum: checksum('9'), provider: 'pillow-g10', modelVersion: 'g10-typeset-v1',
    parameterHash: checksum('0'), candidateChecksum: checksum('6'), width: 1200, height: 1800,
    renderScale: 1, overflowRegionIds: [], anomalies: [], revisionId: 'revision-typeset-1',
    completed: true, artifactUrl: '/images/image-1/page-gates/typeset/candidates/typeset-candidate-1',
    review: null, createdAt: '2026-08-25T00:01:00Z', ...overrides,
  };
}

function typesetContext(
  candidate = typesetCandidate(),
  overrides: Partial<TypesetGateContext> = {},
): TypesetGateContext {
  return {
    imageId: 'image-1', imageRevision: 11, generationId: 'generation-1', nextSequence: 22,
    g9TerminalChecksum: checksum('3'), translationStateChecksum: checksum('4'),
    cleanPlateCandidateId: 'clean-1', cleanPlateChecksum: checksum('5'), state: 'pending',
    terminalChecksum: null, candidates: [candidate], reviews: [],
    routeManifest: candidate.routeManifest, routeChecksum: candidate.routeChecksum,
    styleDefaults: { bubble: typesetStyle(), ordinary: typesetStyle(),
      artLettering: typesetStyle(true) },
    availableFonts: [regularTypesetFont, displayTypesetFont],
    availableDisplayFonts: [displayTypesetFont],
    artLetteringCapability: { available: true, contractVersion: 'g10-art-lettering-v1',
      features: ['explicit-installed-chinese-display-font', 'fill-stroke', 'rotation',
        'nonuniform-scale', 'shear-affine', 'opacity', 'visual-center',
        'alignment', 'line-spacing'], reason: null },
    retryRegionStyles: {}, ...overrides,
  };
}

function seedG10Inspector(context = typesetContext()) {
  const image = imageFixture('image-1', { revision: 11, width: 1200, height: 1800 });
  const regions = [
    regionFixture('sfx-redraw', { order: 1, type: 'sound_effect', contentDisposition: 'redraw-art' }),
    regionFixture('dialogue', { order: 2, type: 'dialogue', contentDisposition: 'translate' }),
    regionFixture('sfx-keep', { order: 3, type: 'sound_effect', contentDisposition: 'keep-art' }),
    regionFixture('ignored', { order: 4, type: 'other', contentDisposition: 'ignore' }),
    regionFixture('ruby-1', { order: 5, type: 'ruby', rubyParentId: 'dialogue',
      contentDisposition: 'ignore' }),
  ];
  seedWorkbench({ images: [image], regions });
  const toInput = (style: TypesetRegionStyle) => {
    const { fontChecksum: _fontChecksum, fontSource: _fontSource, ...input } = style;
    void _fontChecksum;
    void _fontSource;
    return input;
  };
  useWorkbenchStore.setState({
    g4Contexts: { 'image-1': { status: 'active', generation: generation(), events: [],
      phase: 'G10', error: '', conflict: false } },
    typesetContexts: { 'image-1': context },
    selectedTypesetCandidateIds: context.candidates.length
      ? { 'image-1': context.candidates[0]!.candidateId } : {},
    typesetStyleDrafts: { 'image-1': Object.fromEntries(context.routeManifest
      .filter((route) => route.renderRequired)
      .map((route) => [route.regionId, toInput(
        route.route === 'art-lettering' ? typesetStyle(true) : typesetStyle(),
      )])) },
  });
}

function observeTypeset(candidate: TypesetCandidate) {
  act(() => useWorkbenchStore.setState({
    typesetBitmapObservations: { 'image-1': {
      imageId: 'image-1', generationId: 'generation-1', nextSequence: 22,
      candidateId: candidate.candidateId, imageRevision: 11, sourceChecksum: checksum('2'),
      cleanPlateChecksum: candidate.cleanPlateChecksum, candidateChecksum: candidate.candidateChecksum,
      routeChecksum: candidate.routeChecksum, styleChecksum: candidate.styleChecksum,
      layoutChecksum: candidate.layoutChecksum, width: candidate.width, height: candidate.height,
      renderScale: candidate.renderScale, state: 'ready',
    } },
  }));
}

describe('G10 typeset Inspector', () => {
  afterEach(() => {
    cleanup();
    resetWorkbenchStore();
    vi.restoreAllMocks();
  });

  it('shows explicit mixed routes, excludes ruby, and exposes only route-supported style controls', () => {
    seedG10Inspector();
    render(<Inspector />);

    expect(screen.getAllByText('艺术字 / SFX 绘图式路线').length).toBeGreaterThan(0);
    expect(screen.getAllByText('气泡文字').length).toBeGreaterThan(0);
    expect(screen.getAllByText('保留原艺术字').length).toBeGreaterThan(0);
    expect(screen.getAllByText('明确忽略').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: '选择 G10 route ruby-1' })).not.toBeInTheDocument();
    expect(screen.getByText('曲线 / 局部 AI lettering 未宣告')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'G10 font token' })).toHaveTextContent('Display CJK');
    expect(screen.getByRole('combobox', { name: 'G10 font token' })).not.toHaveTextContent('Regular CJK');
    expect(screen.getByRole('spinbutton', { name: 'G10 scale X' })).toBeInTheDocument();
    expect(screen.queryByRole('spinbutton', { name: 'G10 letter spacing' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '选择 G10 route dialogue' }));
    expect(screen.queryByRole('spinbutton', { name: 'G10 scale X' })).not.toBeInTheDocument();
    expect(screen.queryByRole('spinbutton', { name: 'G10 visual center X' })).not.toBeInTheDocument();
    expect(screen.getByRole('spinbutton', { name: 'G10 letter spacing' })).toBeInTheDocument();
  });

  it('requires completion and exact observation, enforces check polarity, and blocks server failures', () => {
    const pending = typesetCandidate({ completed: false });
    seedG10Inspector(typesetContext(pending));
    const review = vi.fn(async () => true);
    useWorkbenchStore.setState({ reviewG10TypesetCandidate: review });
    render(<Inspector />);
    observeTypeset(pending);

    expect(screen.getByRole('button', { name: '整页 G10 候选生成中…' })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'G10 不可变候选' })).toHaveTextContent('生成中');
    const group = screen.getByRole('group', { name: 'G10 视觉检查（8 / 8）' });
    expect(group.querySelectorAll('input')).toHaveLength(8);
    expect(screen.getByRole('checkbox', {
      name: '字体、字号、粗细、填色、描边与原文视觉重量匹配',
    })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', {
      name: '气泡文字行数、对齐、方向与留白均正确，且完整位于气泡内',
    })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', {
      name: '艺术字描边、倾斜、对齐、重心与构图关系匹配',
    })).toBeInTheDocument();
    group.querySelectorAll('input').forEach((input) => expect(input).toBeDisabled());

    const completed = { ...pending, completed: true };
    act(() => useWorkbenchStore.setState((state) => ({
      typesetContexts: { ...state.typesetContexts,
        'image-1': typesetContext(completed) },
    })));
    observeTypeset(completed);
    group.querySelectorAll('input').forEach((input) => expect(input).toBeEnabled());
    const accept = screen.getByRole('button', { name: '接受最终候选' });
    const reject = screen.getByRole('button', { name: '拒绝并按样式重试' });
    expect(accept).toBeDisabled();
    expect(reject).toBeDisabled();
    const inputs = [...group.querySelectorAll('input')];
    fireEvent.click(inputs[0]!);
    fireEvent.click(inputs[0]!);
    expect(accept).toBeDisabled();
    expect(reject).toBeDisabled();
    inputs.slice(1).forEach((input) => fireEvent.click(input));
    expect(accept).toBeDisabled();
    expect(reject).toBeEnabled();
    fireEvent.click(inputs[0]!);
    expect(accept).toBeEnabled();
    expect(reject).toBeDisabled();
    fireEvent.click(inputs[0]!);
    expect(accept).toBeDisabled();
    expect(reject).toBeEnabled();

    const overflow = typesetCandidate({ candidateChecksum: checksum('b'),
      overflowRegionIds: ['sfx-redraw'],
      layoutManifest: completed.layoutManifest.map((entry) => entry.regionId === 'sfx-redraw'
        ? { ...entry, overflow: true } : entry) });
    act(() => useWorkbenchStore.setState((state) => ({
      typesetContexts: { ...state.typesetContexts, 'image-1': typesetContext(overflow) },
    })));
    observeTypeset(overflow);
    expect(screen.getByText('服务端 raster 硬失败')).toBeInTheDocument();
    const overflowAcknowledgement = screen.getByRole('checkbox', {
      name: /确认服务端硬失败：存在溢出或布局异常/,
    });
    expect(overflowAcknowledgement).toBeEnabled();
    expect(overflowAcknowledgement).not.toBeChecked();
    expect(screen.getByRole('button', { name: '接受最终候选' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '拒绝并按样式重试' })).toBeDisabled();
    [...group.querySelectorAll('input')]
      .filter((input) => input !== overflowAcknowledgement)
      .forEach((input) => fireEvent.click(input));
    expect(screen.getByRole('button', { name: '拒绝并按样式重试' })).toBeDisabled();
    fireEvent.click(overflowAcknowledgement);
    expect(overflowAcknowledgement).toBeChecked();
    expect(overflowAcknowledgement).toBeDisabled();
    expect(screen.getByRole('button', { name: '接受最终候选' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '拒绝并按样式重试' })).toBeEnabled();
  });

  it('keeps unavailable art capability hard-blocked and accepted terminal evidence read-only', () => {
    const noCapability = typesetContext(typesetCandidate(), {
      candidates: [], availableFonts: [], availableDisplayFonts: [],
      styleDefaults: { bubble: null, ordinary: null, artLettering: null },
      artLetteringCapability: { available: false, contractVersion: 'g10-art-lettering-v1',
        features: ['explicit-installed-chinese-display-font', 'fill-stroke', 'rotation',
          'nonuniform-scale', 'shear-affine', 'opacity', 'visual-center',
          'alignment', 'line-spacing'],
        reason: 'g10-art-lettering-capability-required' },
    });
    seedG10Inspector(noCapability);
    render(<Inspector />);
    expect(screen.getByText('艺术字能力硬阻断')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '生成整页 G10 候选' })).toBeDisabled();
    cleanup();

    const acceptedReview = typesetReview('accepted');
    const acceptedCandidate = typesetCandidate({ review: acceptedReview });
    seedG10Inspector(typesetContext(acceptedCandidate, {
      state: 'accepted', terminalChecksum: acceptedReview.terminalChecksum,
      reviews: [acceptedReview],
    }));
    render(<Inspector />);
    expect(screen.getByText('G10 已终结')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'G10 不可变候选' })).toBeDisabled();
    expect(screen.getByRole('combobox', { name: 'G10 font token' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: '接受最终候选' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '拒绝并按样式重试' })).not.toBeInTheDocument();
  });
});
