import { useWorkbenchStore } from '../store/workbench';
import { Modal } from './Primitives';

const shortcuts = [
  ['⌘ / Ctrl + S', '立即保存'],
  ['⌘ / Ctrl + O', '导入图像'],
  ['⌘ / Ctrl + Z', '撤销'],
  ['⇧⌘ / Ctrl + Y', '重做'],
  ['V', '选择工具'],
  ['N / R', '绘制文本框'],
  ['H', '平移工具'],
  ['M / E', '蒙版画笔 / 蒙版橡皮擦（需选中一个文本框）'],
  ['Space + 拖动', '临时平移'],
  ['F 或 ⌘0', '适合窗口'],
  ['1 / 2 / 3 / 4', '原图 / 增强 / 擦除 / 成品'],
  ['B', '开关原图 / 结果对比'],
  ['⌘ / Ctrl + 滚轮', '以指针为中心缩放'],
  ['Enter', '确认当前文本框'],
  ['Alt + ↓ / ↑', '选择下 / 上一个文本框'],
  ['← / → 或 [ / ]', '上一张 / 下一张（切换前保存）'],
  ['⇧ ← / ⇧ →', '上一张 / 下一张未检查页'],
  ['Delete / Backspace', '删除所选文本框'],
  ['Esc', '取消选择或关闭浮层'],
];

export function ShortcutsDialog() {
  const open = useWorkbenchStore((state) => state.shortcutsOpen);
  const setOpen = useWorkbenchStore((state) => state.setShortcutsOpen);
  if (!open) return null;
  return (
    <Modal title="键盘快捷键" description="焦点在输入框、文本框、下拉框或可编辑内容中时，编辑类全局快捷键会自动停用。" onClose={() => setOpen(false)}>
      <dl className="shortcut-list">
        {shortcuts.map(([keys, action]) => <div key={keys}><dt><kbd>{keys}</kbd></dt><dd>{action}</dd></div>)}
      </dl>
    </Modal>
  );
}
