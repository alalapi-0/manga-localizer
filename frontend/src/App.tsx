import { lazy, Suspense, useEffect } from 'react';

import { BatchDrawer } from './components/BatchDrawer';
import { Inspector } from './components/Inspector';
import { EmptyState, IconButton, LoadingState } from './components/Primitives';
import { ShortcutsDialog } from './components/ShortcutsDialog';
import { Sidebar } from './components/Sidebar';
import { TopBar } from './components/TopBar';
import { hasPendingChanges, overflowingRegionIds, useWorkbenchStore } from './store/workbench';

const CanvasWorkspace = lazy(async () => {
  const module = await import('./components/CanvasWorkspace');
  return { default: module.CanvasWorkspace };
});

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(
    target.closest('input, textarea, select, [contenteditable="true"], [role="textbox"]'),
  );
}

function useGlobalShortcuts() {
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const state = useWorkbenchStore.getState();
      const modifier = event.metaKey || event.ctrlKey;
      const editable = isEditableTarget(event.target);

      if (modifier && event.key.toLowerCase() === 's') {
        event.preventDefault();
        void state.flushAutosave();
        return;
      }
      if (modifier && event.key.toLowerCase() === 'o') {
        event.preventDefault();
        window.dispatchEvent(new Event('manga-localizer:import'));
        return;
      }
      if (editable) return;

      if (modifier && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        if (event.shiftKey) state.redo();
        else state.undo();
        return;
      }
      if (modifier && event.key.toLowerCase() === 'y') {
        event.preventDefault();
        state.redo();
        return;
      }
      if (modifier && event.key === '0') {
        event.preventDefault();
        state.requestFit();
        return;
      }
      if (event.key === 'Delete' || event.key === 'Backspace') {
        event.preventDefault();
        state.deleteSelectedRegions();
        return;
      }
      if (event.key === 'Enter') {
        const regionId = state.selectedRegionIds[0];
        if (regionId) {
          event.preventDefault();
          void state.setRegionConfirmed(regionId, true);
        }
        return;
      }
      if (event.altKey && event.shiftKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        event.preventDefault();
        void state.navigateImage(event.key === 'ArrowLeft' ? -1 : 1, 'overflow');
        return;
      }
      if (event.altKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        event.preventDefault();
        void state.navigateImage(event.key === 'ArrowLeft' ? -1 : 1, 'failed');
        return;
      }
      if (event.altKey && (event.key === 'ArrowDown' || event.key === 'ArrowUp')) {
        const regions = state.activeImageId
          ? [...(state.regionsByImage[state.activeImageId] ?? [])].sort((a, b) => a.order - b.order)
          : [];
        if (regions.length) {
          event.preventDefault();
          const currentIndex = regions.findIndex((region) => region.id === state.selectedRegionIds[0]);
          const offset = event.key === 'ArrowUp' ? -1 : 1;
          const nextIndex = currentIndex < 0
            ? event.key === 'ArrowUp' ? regions.length - 1 : 0
            : (currentIndex + offset + regions.length) % regions.length;
          const nextRegion = regions[nextIndex];
          if (nextRegion) {
            state.selectRegion(nextRegion.id);
            state.focusRegions([nextRegion.id]);
          }
        }
        return;
      }
      if (!event.altKey && (event.key === 'ArrowLeft' || (!event.shiftKey && event.key === '['))) {
        event.preventDefault();
        void state.navigateImage(-1, event.shiftKey ? 'unreviewed' : 'adjacent');
        return;
      }
      if (!event.altKey && (event.key === 'ArrowRight' || (!event.shiftKey && event.key === ']'))) {
        event.preventDefault();
        void state.navigateImage(1, event.shiftKey ? 'unreviewed' : 'adjacent');
        return;
      }
      if (event.key === 'Escape') {
        state.clearRegionSelection();
        state.setDrawerOpen(false);
        state.setShortcutsOpen(false);
        return;
      }
      if (event.code === 'Space') {
        event.preventDefault();
        state.setSpacePressed(true);
        return;
      }
      if (!modifier && event.key.toLowerCase() === 't') {
        const image = state.images.find((entry) => entry.id === state.activeImageId);
        if (!image) return;
        const exportOptions = {
          format: 'both' as const,
          imageVariant: 'typeset' as const,
          conflict: 'rename' as const,
          preserveTree: true,
        };
        if (event.shiftKey) {
          const overflowIds = overflowingRegionIds(
            image,
            state.regionsByImage[image.id] ?? [],
          );
          if (!overflowIds.length) return;
          event.preventDefault();
          state.setDrawerOpen(true);
          void state.startBatch(['typeset'], [image.id], exportOptions, 1, overflowIds);
          return;
        }
        if (state.selectedRegionIds.length !== 1) return;
        const regionId = state.selectedRegionIds[0];
        if (!regionId) return;
        event.preventDefault();
        state.setDrawerOpen(true);
        void state.startBatch(['typeset'], [image.id], exportOptions, 1, [regionId]);
        return;
      }
      switch (event.key.toLowerCase()) {
        case 'v': state.setCanvasTool('select'); break;
        case 'n':
        case 'r': state.setCanvasTool('region'); break;
        case 'h': state.setCanvasTool('hand'); break;
        case 'm': state.setCanvasTool('mask-brush'); break;
        case 'e': state.setCanvasTool('mask-eraser'); break;
        case 'f': state.requestFit(); break;
        case 'g': state.focusSelectedRegions(); break;
        case 'b': state.toggleCompareMode(); break;
        case '1': state.setCanvasMode('original'); break;
        case '2': state.setCanvasMode('preprocessed'); break;
        case '3': state.setCanvasMode('erased'); break;
        case '4': state.setCanvasMode('typeset'); break;
      }
    }

    function onKeyUp(event: KeyboardEvent) {
      if (event.code === 'Space') useWorkbenchStore.getState().setSpacePressed(false);
    }

    function clearTemporaryTools() {
      useWorkbenchStore.getState().setSpacePressed(false);
    }

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('blur', clearTemporaryTools);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      window.removeEventListener('blur', clearTemporaryTools);
    };
  }, []);
}

function StartupState() {
  const loadState = useWorkbenchStore((state) => state.loadState);
  const message = useWorkbenchStore((state) => state.loadMessage);
  const error = useWorkbenchStore((state) => state.globalError);
  const retry = useWorkbenchStore((state) => state.retryInitialize);
  if (loadState === 'loading' || loadState === 'idle') {
    return <div className="startup-screen"><LoadingState label={message} /><p>所有项目数据均由本机服务读取。</p></div>;
  }
  return (
    <div className="startup-screen startup-screen--error">
      <EmptyState
        icon="!"
        title={message || '本地服务不可用'}
        description={error || '请启动 FastAPI 服务后重试。'}
        action={<button className="button button--accent" onClick={() => void retry()} type="button">重新连接</button>}
      />
    </div>
  );
}

function ErrorBanner() {
  const error = useWorkbenchStore((state) => state.globalError);
  const saveError = useWorkbenchStore((state) => state.saveError);
  const conflict = useWorkbenchStore((state) => state.revisionConflict);
  const dismiss = useWorkbenchStore((state) => state.dismissError);
  const reload = useWorkbenchStore((state) => state.reloadActiveImage);
  if (!error && !saveError) return null;
  return (
    <div className="error-banner" role="alert">
      <span aria-hidden="true">!</span>
      <div><strong>{conflict ? '保存时发现版本冲突' : saveError ? '更改尚未保存' : '操作失败'}</strong><small>{saveError || error}</small></div>
      {conflict ? <button className="button button--compact" onClick={() => void reload()} type="button">放弃本页本地更改并重载</button> : null}
      {error ? <IconButton aria-label="关闭错误提示" onClick={dismiss}>×</IconButton> : null}
    </div>
  );
}

export default function App() {
  const loadState = useWorkbenchStore((state) => state.loadState);
  const project = useWorkbenchStore((state) => state.currentProject);
  const theme = useWorkbenchStore((state) => state.theme);
  const initialize = useWorkbenchStore((state) => state.initialize);
  const jobs = useWorkbenchStore((state) => state.jobs);
  const refreshJobs = useWorkbenchStore((state) => state.refreshJobs);
  const dirty = useWorkbenchStore(hasPendingChanges);
  const flushAutosave = useWorkbenchStore((state) => state.flushAutosave);
  useGlobalShortcuts();

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  }, [theme]);

  useEffect(() => {
    if (loadState === 'idle') void initialize();
  }, [initialize, loadState]);

  useEffect(() => {
    const active = jobs.some((job) => ['queued', 'running'].includes(job.status));
    if (!active) return;
    const timer = window.setInterval(() => void refreshJobs(), 1500);
    return () => window.clearInterval(timer);
  }, [jobs, refreshJobs]);

  useEffect(() => {
    function beforeUnload(event: BeforeUnloadEvent) {
      if (!hasPendingChanges(useWorkbenchStore.getState())) return;
      event.preventDefault();
    }
    function visibilityChange() {
      if (document.visibilityState === 'hidden') void flushAutosave();
    }
    window.addEventListener('beforeunload', beforeUnload);
    document.addEventListener('visibilitychange', visibilityChange);
    return () => {
      window.removeEventListener('beforeunload', beforeUnload);
      document.removeEventListener('visibilitychange', visibilityChange);
    };
  }, [dirty, flushAutosave]);

  if ((loadState === 'loading' || loadState === 'idle' || loadState === 'error') && !project) {
    return <StartupState />;
  }

  return (
    <div className="app-shell">
      <TopBar />
      <ErrorBanner />
      <div className="workbench-grid">
        <Sidebar />
        <Suspense fallback={<div className="canvas-workspace"><LoadingState label="正在加载画布…" /></div>}>
          <CanvasWorkspace />
        </Suspense>
        <Inspector />
      </div>
      <BatchDrawer />
      <ShortcutsDialog />
    </div>
  );
}
