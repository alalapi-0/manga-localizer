import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from 'react';

import type { ProviderCapability, StageState } from '../types';

export function IconButton({
  children,
  className = '',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { children: ReactNode }) {
  return (
    <button className={`icon-button ${className}`.trim()} type="button" {...props}>
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  children,
  className = '',
}: {
  label: string;
  hint?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <label className={`field ${className}`.trim()}>
      <span className="field__label">{label}</span>
      {children}
      {hint ? <span className="field__hint">{hint}</span> : null}
    </label>
  );
}

export function Toggle({
  label,
  description,
  ...props
}: Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> & {
  label: string;
  description?: string;
}) {
  return (
    <label className="toggle-row">
      <span>
        <span className="toggle-row__label">{label}</span>
        {description ? <span className="toggle-row__description">{description}</span> : null}
      </span>
      <span className="switch">
        <input type="checkbox" {...props} />
        <span className="switch__track" aria-hidden="true" />
      </span>
    </label>
  );
}

export function Modal({
  title,
  description,
  onClose,
  children,
  footer,
  labelledBy,
}: {
  title: string;
  description?: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  labelledBy?: string;
}) {
  const titleId = labelledBy ?? `modal-${title.replace(/\s+/g, '-')}`;
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        aria-labelledby={titleId}
        aria-modal="true"
        className="modal"
        role="dialog"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="modal__header">
          <div>
            <h2 id={titleId}>{title}</h2>
            {description ? <p>{description}</p> : null}
          </div>
          <IconButton aria-label="关闭" onClick={onClose}>×</IconButton>
        </header>
        <div className="modal__body">{children}</div>
        {footer ? <footer className="modal__footer">{footer}</footer> : null}
      </section>
    </div>
  );
}

export function LoadingState({ label = '正在加载…' }: { label?: string }) {
  return (
    <div className="loading-state" role="status">
      <span className="spinner" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function EmptyState({
  icon = '◇',
  title,
  description,
  action,
}: {
  icon?: string;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <span className="empty-state__icon" aria-hidden="true">{icon}</span>
      <strong>{title}</strong>
      {description ? <p>{description}</p> : null}
      {action}
    </div>
  );
}

export function CreateLocalProjectButton() {
  return (
    <button
      className="button button--accent"
      onClick={() => window.dispatchEvent(new Event('manga-localizer:create-project'))}
      type="button"
    >
      创建本机项目
    </button>
  );
}

export function ImportPhotosButton() {
  return (
    <button
      className="button button--accent"
      onClick={() => window.dispatchEvent(new Event('manga-localizer:import'))}
      type="button"
    >
      从相册导入
    </button>
  );
}

const stateLabels: Record<StageState, string> = {
  not_started: '未开始',
  queued: '排队中',
  running: '处理中',
  done: '已完成',
  failed: '失败',
  unavailable: '不可用',
};

export function StatusPill({ state, label }: { state: StageState; label?: string }) {
  return (
    <span className={`status-pill status-pill--${state}`}>
      <span aria-hidden="true" />
      {label ?? stateLabels[state]}
    </span>
  );
}

export function ProviderBadge({ provider }: { provider: ProviderCapability | undefined }) {
  if (!provider) return <span className="provider-badge provider-badge--missing">未配置</span>;
  return (
    <span
      className={`provider-badge ${provider.available ? '' : 'provider-badge--missing'} ${provider.isMock ? 'provider-badge--mock' : ''}`}
      title={provider.reason || provider.label}
    >
      {provider.label}
      {provider.isMock ? <b>演示 MOCK</b> : null}
      {!provider.available ? <b>{provider.configurable ? '待配置' : '不可用'}</b> : null}
    </span>
  );
}
