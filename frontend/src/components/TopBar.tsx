import { hasPendingChanges, useWorkbenchStore } from '../store/workbench';
import { IconButton } from './Primitives';

export function TopBar() {
  const project = useWorkbenchStore((state) => state.currentProject);
  const theme = useWorkbenchStore((state) => state.theme);
  const saving = useWorkbenchStore((state) => state.saving);
  const saveError = useWorkbenchStore((state) => state.saveError);
  const lastSavedAt = useWorkbenchStore((state) => state.lastSavedAt);
  const dirty = useWorkbenchStore(hasPendingChanges);
  const canUndo = useWorkbenchStore((state) => state.past.length > 0);
  const canRedo = useWorkbenchStore((state) => state.future.length > 0);
  const undo = useWorkbenchStore((state) => state.undo);
  const redo = useWorkbenchStore((state) => state.redo);
  const flushAutosave = useWorkbenchStore((state) => state.flushAutosave);
  const setTheme = useWorkbenchStore((state) => state.setTheme);
  const setDrawerOpen = useWorkbenchStore((state) => state.setDrawerOpen);
  const setShortcutsOpen = useWorkbenchStore((state) => state.setShortcutsOpen);

  const saveLabel = saving
    ? '正在保存…'
    : saveError
      ? '保存失败'
      : dirty
        ? '有未保存更改'
        : lastSavedAt
          ? `已保存 ${new Date(lastSavedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`
          : '已同步';

  return (
    <header className="topbar">
      <div className="topbar__brand" aria-label="Manga Localizer">
        <span className="brand-mark" aria-hidden="true">漫</span>
        <span className="brand-name">Manga Localizer</span>
      </div>
      <div className="topbar__project">
        <span className="topbar__project-name">{project?.name ?? '未打开项目'}</span>
        {project?.rootPath ? <span className="topbar__project-path">{project.rootPath}</span> : null}
      </div>
      <div className="topbar__history" aria-label="编辑历史">
        <IconButton aria-label="撤销" disabled={!canUndo} onClick={undo} title="撤销 ⌘Z">↶</IconButton>
        <IconButton aria-label="重做" disabled={!canRedo} onClick={redo} title="重做 ⇧⌘Z">↷</IconButton>
      </div>
      <button
        className={`save-status ${saveError ? 'save-status--error' : dirty ? 'save-status--dirty' : ''}`}
        disabled={!dirty || saving}
        onClick={() => void flushAutosave()}
        type="button"
      >
        <span aria-hidden="true">{saving ? '◌' : saveError ? '!' : dirty ? '●' : '✓'}</span>
        {saveLabel}
      </button>
      <div className="topbar__actions">
        <IconButton
          aria-label={theme === 'dark' ? '切换到浅色主题' : '切换到深色主题'}
          onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
          title="切换主题"
        >
          {theme === 'dark' ? '☀' : '☾'}
        </IconButton>
        <IconButton aria-label="快捷键" onClick={() => setShortcutsOpen(true)} title="快捷键">⌨</IconButton>
        <button
          className="button button--accent"
          disabled={!project}
          onClick={() => setDrawerOpen(true)}
          type="button"
        >
          批处理与导出
        </button>
      </div>
    </header>
  );
}
