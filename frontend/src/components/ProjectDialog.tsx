import { useState } from 'react';

import { useWorkbenchStore } from '../store/workbench';
import { Field, Modal } from './Primitives';

export function ProjectDialog({ mode, onClose }: { mode: 'create' | 'open'; onClose: () => void }) {
  const [name, setName] = useState('我的漫画项目');
  const [path, setPath] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const createProject = useWorkbenchStore((state) => state.createProject);
  const openProjectPath = useWorkbenchStore((state) => state.openProjectPath);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    const success = mode === 'create'
      ? await createProject(name.trim(), path.trim() || undefined)
      : await openProjectPath(path.trim());
    setSubmitting(false);
    if (success) onClose();
  }

  return (
    <Modal
      title={mode === 'create' ? '新建本地项目' : '打开已有项目'}
      description={
        mode === 'create'
          ? '源图会复制到项目目录中，原始文件始终保持只读。'
          : '输入本机 project.json 的完整路径。浏览器不会把路径发送到远程服务。'
      }
      onClose={onClose}
    >
      <form className="form-stack" id="project-form" onSubmit={(event) => void submit(event)}>
        {mode === 'create' ? (
          <Field label="项目名称">
            <input
              autoFocus
              value={name}
              onChange={(event) => setName(event.target.value)}
              required
            />
          </Field>
        ) : null}
        <Field
          label={mode === 'create' ? '输出目录（可选）' : '项目清单路径'}
          hint={mode === 'create' ? '留空则由本地服务选择默认目录。' : '请选择 output/project/project.json 的完整路径。'}
        >
          <input
            autoFocus={mode === 'open'}
            value={path}
            onChange={(event) => setPath(event.target.value)}
            placeholder={mode === 'create' ? '/本机/输出目录' : '/本机/project/project.json'}
            required={mode === 'open'}
          />
        </Field>
        <div className="form-actions">
          <button className="button" type="button" onClick={onClose}>取消</button>
          <button
            className="button button--accent"
            disabled={submitting || (mode === 'create' ? !name.trim() : !path.trim())}
            type="submit"
          >
            {submitting ? '处理中…' : mode === 'create' ? '创建项目' : '打开项目'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
