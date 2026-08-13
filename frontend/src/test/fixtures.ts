import { resetWorkbenchStore, useWorkbenchStore } from '../store/workbench';
import type {
  AppCapabilities,
  ImageAsset,
  Job,
  Project,
  Region,
} from '../types';
import {
  DEFAULT_PROJECT_SETTINGS,
  DEFAULT_REGION_STYLE,
  DEFAULT_REPAIR_SETTINGS,
  EMPTY_PIPELINE_STATUS,
} from '../types';

export function projectFixture(overrides: Partial<Project> = {}): Project {
  return {
    id: 'project-1',
    name: '测试漫画',
    rootPath: '/tmp/测试漫画',
    imageCount: 2,
    revision: 3,
    settings: { ...DEFAULT_PROJECT_SETTINGS },
    ...overrides,
  };
}

export function imageFixture(id = 'image-1', overrides: Partial<ImageAsset> = {}): ImageAsset {
  return {
    id,
    projectId: 'project-1',
    name: `${id}.png`,
    relativePath: id === 'image-1' ? `第一话/${id}.png` : `第二话/${id}.png`,
    width: 1200,
    height: 1800,
    regionCount: id === 'image-1' ? 2 : 0,
    confirmedCount: 0,
    ignoredCount: 0,
    trustedCount: 0,
    trustReviewCount: id === 'image-1' ? 2 : 0,
    status: { ...EMPTY_PIPELINE_STATUS, ocr: id === 'image-1' ? 'done' : 'not_started' },
    stageReviews: {},
    detectorProvider: id === 'image-1' ? 'tesseract' : undefined,
    ocrProvider: id === 'image-1' ? 'tesseract' : undefined,
    revision: 1,
    ...overrides,
  };
}

export function regionFixture(id = 'region-1', overrides: Partial<Region> = {}): Region {
  return {
    id,
    imageId: 'image-1',
    x: id === 'region-1' ? 100 : 360,
    y: id === 'region-1' ? 120 : 400,
    width: 220,
    height: 120,
    rotation: 0,
    sourceText: id === 'region-1' ? 'こんにちは' : 'ありがとう',
    translationText: id === 'region-1' ? '你好' : '谢谢',
    type: 'dialogue',
    direction: 'vertical',
    order: id === 'region-1' ? 1 : 2,
    confidence: 0.91,
    detectorConfidence: 0.83,
    ocrConfidence: 0.91,
    trustDisposition: 'review',
    trustReason: 'automatic-ocr-complete',
    trustPolicyVersion: 1,
    recognition: { provider: 'tesseract' },
    ignored: false,
    confirmed: false,
    style: { ...DEFAULT_REGION_STYLE },
    repair: { ...DEFAULT_REPAIR_SETTINGS },
    revision: 4,
    ...overrides,
  };
}

export function capabilitiesFixture(): AppCapabilities {
  return {
    providers: [
      { id: 'opencv-pillow', label: 'OpenCV / Pillow 基础增强', kind: 'preprocessor', available: true, local: true, isMock: false },
      { id: 'tesseract', label: 'Tesseract 文本检测', kind: 'detector', available: true, local: true, isMock: false },
      { id: 'tesseract', label: 'Tesseract', kind: 'ocr', available: true, local: true, isMock: false },
      { id: 'manual', label: '手动翻译', kind: 'translator', available: true, local: true, isMock: false },
      { id: 'mock', label: '确定性演示翻译', kind: 'translator', available: true, local: true, isMock: true },
      { id: 'opencv', label: 'OpenCV', kind: 'inpainter', available: true, local: true, isMock: false },
      { id: 'lama-onnx', label: 'LaMa ONNX', kind: 'inpainter', available: true, local: true, isMock: false },
      { id: 'pillow', label: 'Pillow 本地排版', kind: 'typesetter', available: true, local: true, isMock: false },
    ],
  };
}

export function jobFixture(overrides: Partial<Job> = {}): Job {
  return {
    id: 'job-1',
    projectId: 'project-1',
    kind: 'export',
    status: 'queued',
    progress: 0,
    total: 2,
    completed: 0,
    items: [],
    ...overrides,
  };
}

export function seedWorkbench(options: {
  selectedRegionIds?: string[];
  regions?: Region[];
  images?: ImageAsset[];
  project?: Project;
} = {}) {
  resetWorkbenchStore();
  const project = options.project ?? projectFixture();
  const images = options.images ?? [imageFixture('image-1'), imageFixture('image-2')];
  const regions = options.regions ?? [regionFixture('region-1'), regionFixture('region-2')];
  useWorkbenchStore.setState({
    loadState: 'ready',
    capabilities: capabilitiesFixture(),
    projects: [project],
    currentProject: project,
    images,
    activeImageId: images[0]?.id ?? null,
    selectedImageIds: images[0] ? [images[0].id] : [],
    regionsByImage: {
      'image-1': regions,
      'image-2': [],
    },
    serverRegionRevisions: Object.fromEntries(
      regions
        .filter((region) => !region.id.startsWith('local-'))
        .map((region) => [region.id, region.revision]),
    ),
    selectedRegionIds: options.selectedRegionIds ?? [],
  });
  return { project, images, regions };
}
