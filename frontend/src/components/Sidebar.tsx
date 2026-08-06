import { useEffect, useMemo, useRef, useState } from 'react';

import { api } from '../api/client';
import { useWorkbenchStore } from '../store/workbench';
import type { ImageAsset, ProviderCapability, StageState } from '../types';
import { EmptyState, IconButton, ProviderBadge, StatusPill } from './Primitives';
import { ProjectDialog } from './ProjectDialog';

function imageReviewState(image: ImageAsset): StageState | 'no_text' | 'needs_review' {
  const stages = Object.values(image.status);
  if (image.error || stages.includes('failed')) return 'failed';
  if (stages.includes('running')) return 'running';
  if (stages.includes('queued')) return 'queued';
  if (image.status.ocr === 'unavailable' || image.status.detection === 'unavailable') return 'unavailable';
  if (image.status.ocr === 'done' && image.regionCount === 0) return 'no_text';
  if (image.regionCount > image.confirmedCount + image.ignoredCount) return 'needs_review';
  if (image.regionCount > 0 && image.regionCount === image.confirmedCount + image.ignoredCount) return 'done';
  return 'not_started';
}

function matchesFilter(image: ImageAsset, filter: string): boolean {
  const state = imageReviewState(image);
  if (filter === 'all') return true;
  if (filter === 'failed') return state === 'failed' || state === 'unavailable';
  if (filter === 'complete') return state === 'done';
  if (filter === 'no_text') return state === 'no_text';
  return state === 'needs_review' || state === 'not_started' || state === 'running' || state === 'queued';
}

function folderName(path: string): string {
  const parts = path.split('/');
  return parts.length > 1 ? parts.slice(0, -1).join('/') : '项目根目录';
}

function findProvider(
  providers: ProviderCapability[],
  id: string | undefined,
  kind: ProviderCapability['kind'],
): ProviderCapability | undefined {
  if (!id) return undefined;
  return providers.find((provider) => provider.kind === kind && provider.id === id) ?? {
    id,
    label: id,
    kind,
    available: false,
    local: true,
    isMock: id.toLowerCase().includes('mock'),
    reason: '当前页记录了此 provider，但服务未报告其能力。',
  };
}

function ImageRow({ image }: { image: ImageAsset }) {
  const activeImageId = useWorkbenchStore((state) => state.activeImageId);
  const selectedImageIds = useWorkbenchStore((state) => state.selectedImageIds);
  const providers = useWorkbenchStore((state) => state.capabilities.providers);
  const selectImage = useWorkbenchStore((state) => state.selectImage);
  const toggleImageSelection = useWorkbenchStore((state) => state.toggleImageSelection);
  const reviewState = imageReviewState(image);
  const detector = findProvider(providers, image.detectorProvider, 'detector');
  const ocr = findProvider(providers, image.ocrProvider, 'ocr');
  const translator = findProvider(providers, image.translatorProvider, 'translator');

  return (
    <article className={`image-row ${activeImageId === image.id ? 'image-row--active' : ''}`}>
      <label className="image-row__check" title="加入批处理选择">
        <input
          aria-label={`批选 ${image.name}`}
          checked={selectedImageIds.includes(image.id)}
          onChange={() => toggleImageSelection(image.id)}
          type="checkbox"
        />
      </label>
      <button className="image-row__main" onClick={() => void selectImage(image.id)} type="button">
        <span className="thumbnail-wrap">
          <img alt="" loading="lazy" src={api.thumbnailUrl(image.id)} />
          <span>{image.regionCount}</span>
        </span>
        <span className="image-row__content">
          <span className="image-row__name" title={image.relativePath}>{image.name}</span>
          <span className="image-row__meta">{image.width} × {image.height}px</span>
          {reviewState === 'no_text' ? (
            <span className="status-pill status-pill--no-text"><span />无文本（正常）</span>
          ) : reviewState === 'needs_review' ? (
            <span className="status-pill status-pill--needs-review"><span />待复核</span>
          ) : (
            <StatusPill state={reviewState} label={reviewState === 'done' ? '已复核' : undefined} />
          )}
          <span className="image-row__stages" aria-label="页面处理阶段">
            <span className={`stage-mini stage-mini--${image.status.detection}`}>检测</span>
            <span className={`stage-mini stage-mini--${image.status.ocr}`}>OCR</span>
            <span className={`stage-mini stage-mini--${image.status.translation}`}>翻译</span>
          </span>
        </span>
      </button>
      <div className="image-row__providers" aria-label="本页实际 provider">
        <span>D</span><ProviderBadge provider={detector} />
        <span>O</span><ProviderBadge provider={ocr} />
        <span>T</span><ProviderBadge provider={translator} />
      </div>
    </article>
  );
}

export function Sidebar() {
  const projects = useWorkbenchStore((state) => state.projects);
  const project = useWorkbenchStore((state) => state.currentProject);
  const images = useWorkbenchStore((state) => state.images);
  const imageSearch = useWorkbenchStore((state) => state.imageSearch);
  const imageFilter = useWorkbenchStore((state) => state.imageFilter);
  const selectedImageIds = useWorkbenchStore((state) => state.selectedImageIds);
  const setImageSearch = useWorkbenchStore((state) => state.setImageSearch);
  const setImageFilter = useWorkbenchStore((state) => state.setImageFilter);
  const selectProject = useWorkbenchStore((state) => state.selectProject);
  const importFiles = useWorkbenchStore((state) => state.importFiles);
  const selectAllVisibleImages = useWorkbenchStore((state) => state.selectAllVisibleImages);
  const clearImageSelection = useWorkbenchStore((state) => state.clearImageSelection);
  const navigateImage = useWorkbenchStore((state) => state.navigateImage);
  const [dialog, setDialog] = useState<'create' | 'open' | null>(null);
  const singleRef = useRef<HTMLInputElement>(null);
  const multipleRef = useRef<HTMLInputElement>(null);
  const folderRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const openImporter = () => multipleRef.current?.click();
    window.addEventListener('manga-localizer:import', openImporter);
    return () => window.removeEventListener('manga-localizer:import', openImporter);
  }, []);

  const visibleImages = useMemo(() => {
    const query = imageSearch.trim().toLocaleLowerCase();
    return images.filter(
      (image) =>
        (!query || image.relativePath.toLocaleLowerCase().includes(query)) &&
        matchesFilter(image, imageFilter),
    );
  }, [imageFilter, imageSearch, images]);

  const groups = useMemo(() => {
    const map = new Map<string, ImageAsset[]>();
    visibleImages.forEach((image) => {
      const folder = folderName(image.relativePath);
      map.set(folder, [...(map.get(folder) ?? []), image]);
    });
    return [...map.entries()];
  }, [visibleImages]);

  async function handleFiles(input: HTMLInputElement) {
    const files = [...(input.files ?? [])].filter((file) => file.type.startsWith('image/'));
    await importFiles(files);
    input.value = '';
  }

  return (
    <aside className="sidebar panel" aria-label="项目与图像">
      <section className="sidebar__project">
        <span className="section-kicker">当前项目</span>
        <div className="project-select-row">
          <select
            aria-label="切换项目"
            disabled={!projects.length}
            onChange={(event) => void selectProject(event.target.value)}
            value={project?.id ?? ''}
          >
            {!projects.length ? <option value="">暂无项目</option> : null}
            {projects.map((entry) => <option key={entry.id} value={entry.id}>{entry.name}</option>)}
          </select>
          <IconButton aria-label="新建项目" onClick={() => setDialog('create')} title="新建项目">＋</IconButton>
          <IconButton aria-label="打开本地项目" onClick={() => setDialog('open')} title="打开 project.json">⌁</IconButton>
        </div>
      </section>

      <section className="sidebar__imports" aria-label="导入图像">
        <button className="button button--compact" disabled={!project} onClick={() => singleRef.current?.click()} type="button">单图</button>
        <button className="button button--compact" disabled={!project} onClick={() => multipleRef.current?.click()} type="button">多图</button>
        <button className="button button--compact" disabled={!project} onClick={() => folderRef.current?.click()} type="button">文件夹</button>
        <input
          accept="image/*"
          className="sr-only"
          onChange={(event) => void handleFiles(event.currentTarget)}
          ref={singleRef}
          type="file"
        />
        <input
          accept="image/*"
          className="sr-only"
          multiple
          onChange={(event) => void handleFiles(event.currentTarget)}
          ref={multipleRef}
          type="file"
        />
        <input
          accept="image/*"
          className="sr-only"
          multiple
          onChange={(event) => void handleFiles(event.currentTarget)}
          ref={(element) => {
            folderRef.current = element;
            element?.setAttribute('webkitdirectory', '');
            element?.setAttribute('directory', '');
          }}
          type="file"
        />
      </section>

      <section className="sidebar__search">
        <label className="search-box">
          <span aria-hidden="true">⌕</span>
          <input
            aria-label="搜索图像路径"
            onChange={(event) => setImageSearch(event.target.value)}
            placeholder="搜索文件名或路径"
            type="search"
            value={imageSearch}
          />
        </label>
        <select
          aria-label="按状态筛选"
          onChange={(event) => setImageFilter(event.target.value as typeof imageFilter)}
          value={imageFilter}
        >
          <option value="all">全部状态</option>
          <option value="needs_review">待处理 / 待复核</option>
          <option value="failed">失败 / 不可用</option>
          <option value="complete">已复核</option>
          <option value="no_text">无文本</option>
        </select>
      </section>

      <div className="sidebar__batchbar">
        <button
          className="text-button"
          disabled={!visibleImages.length}
          onClick={() => selectAllVisibleImages(visibleImages.map((image) => image.id))}
          type="button"
        >
          全选当前 {visibleImages.length}
        </button>
        <button className="text-button" disabled={!selectedImageIds.length} onClick={clearImageSelection} type="button">
          清除（{selectedImageIds.length}）
        </button>
      </div>

      <div className="image-tree" aria-label="图像列表">
        {!project ? (
          <EmptyState icon="▧" title="先创建或打开项目" description="项目数据只保存在你选择的本机目录。" />
        ) : images.length === 0 ? (
          <EmptyState icon="▧" title="尚未导入图像" description="支持单图、多图和保留目录结构的文件夹导入。" />
        ) : visibleImages.length === 0 ? (
          <EmptyState icon="⌕" title="没有匹配的图像" description="调整搜索词或状态筛选。" />
        ) : groups.map(([folder, entries]) => (
          <section className="image-group" key={folder}>
            <h3 title={folder}><span aria-hidden="true">⌄</span>{folder}<b>{entries.length}</b></h3>
            {entries.map((image) => <ImageRow image={image} key={image.id} />)}
          </section>
        ))}
      </div>

      <footer className="sidebar__footer">
        <IconButton aria-label="上一张图" disabled={!images.length} onClick={() => void navigateImage(-1)}>←</IconButton>
        <span>{images.length ? `${images.findIndex((image) => image.id === useWorkbenchStore.getState().activeImageId) + 1} / ${images.length}` : '0 / 0'}</span>
        <IconButton aria-label="下一张图" disabled={!images.length} onClick={() => void navigateImage(1)}>→</IconButton>
      </footer>

      {dialog ? <ProjectDialog mode={dialog} onClose={() => setDialog(null)} /> : null}
    </aside>
  );
}
