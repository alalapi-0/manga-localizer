import { useMemo, useState } from 'react';

import { api } from '../api/client';
import {
  activeImage,
  useWorkbenchStore,
} from '../store/workbench';
import type {
  ExportOptions,
  ProjectSettings,
  ProviderCapability,
  Region,
  RegionType,
  TextDirection,
} from '../types';
import { EmptyState, Field, ProviderBadge, Toggle } from './Primitives';

const regionTypeLabels: Record<RegionType, string> = {
  dialogue: '对白',
  narration: '旁白',
  sound_effect: '拟声词',
  title: '标题',
  ruby: '注音',
  background: '背景文字',
  unknown: '未知',
  thought: '内心',
  sign: '标牌',
  other: '其他',
};

const directionLabels: Record<TextDirection, string> = {
  auto: '自动',
  vertical: '竖排',
  horizontal: '横排',
};

const defaultExportOptions: ExportOptions = {
  format: 'both',
  conflict: 'rename',
  preserveTree: true,
};

const EMPTY_REGIONS: Region[] = [];

function TextInspector({ regions, selected }: { regions: Region[]; selected: Region[] }) {
  const image = useWorkbenchStore(activeImage);
  const selectRegion = useWorkbenchStore((state) => state.selectRegion);
  const updateRegion = useWorkbenchStore((state) => state.updateRegion);
  const deleteSelectedRegions = useWorkbenchStore((state) => state.deleteSelectedRegions);
  const mergeSelectedRegions = useWorkbenchStore((state) => state.mergeSelectedRegions);
  const splitSelectedRegion = useWorkbenchStore((state) => state.splitSelectedRegion);

  if (!selected.length) {
    if (!regions.length) {
      const ocrDone = image?.status.ocr === 'done';
      return (
        <EmptyState
          icon={ocrDone ? '✓' : '文'}
          title={ocrDone ? '本页未检测到文本' : '未选择文本框'}
          description={
            ocrDone
              ? '零文本是正常结果。你仍可在画布上手动框选。'
              : regions.length ? '在画布或下方列表中选择文本框。' : '运行 OCR 或在画布上绘制文本框。'
          }
        />
      );
    }
    return (
      <div className="region-index">
        <p className="panel-hint">选择一个文本框编辑内容；按住 Shift 可多选。</p>
        {regions.map((region) => (
          <button key={region.id} onClick={() => selectRegion(region.id)} type="button">
            <b>#{region.order}</b>
            <span>{region.sourceText || '（空文本）'}</span>
            <em>{region.confirmed ? '已确认' : region.ignored ? '已忽略' : '待复核'}</em>
          </button>
        ))}
      </div>
    );
  }

  if (selected.length > 1) {
    const allConfirmed = selected.every((region) => region.confirmed);
    const allIgnored = selected.every((region) => region.ignored);
    return (
      <div className="form-stack">
        <div className="multi-selection-summary">
          <strong>已选择 {selected.length} 个文本框</strong>
          <span>批量修改只影响当前选择。</span>
        </div>
        <Field label="文本类型">
          <select
            defaultValue=""
            onChange={(event) => selected.forEach((region) => updateRegion(region.id, { type: event.target.value as RegionType }))}
          >
            <option disabled value="">批量设置…</option>
            {Object.entries(regionTypeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Field>
        <Field label="排版方向">
          <select
            defaultValue=""
            onChange={(event) => selected.forEach((region) => updateRegion(region.id, { direction: event.target.value as TextDirection }))}
          >
            <option disabled value="">批量设置…</option>
            {Object.entries(directionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Field>
        <Toggle
          checked={allConfirmed}
          label="全部确认"
          onChange={(event) => selected.forEach((region) => updateRegion(region.id, { confirmed: event.target.checked }))}
        />
        <Toggle
          checked={allIgnored}
          label="全部忽略"
          onChange={(event) => selected.forEach((region) => updateRegion(region.id, { ignored: event.target.checked }))}
        />
        <button className="button" onClick={mergeSelectedRegions} type="button">合并为一个文本框</button>
        <button className="button button--danger" onClick={deleteSelectedRegions} type="button">删除所选文本框</button>
      </div>
    );
  }

  const region = selected[0];
  if (!region) return null;
  const confidencePercent = region.confidence === null
    ? ''
    : Math.round((region.confidence <= 1 ? region.confidence * 100 : region.confidence) * 10) / 10;

  return (
    <div className="form-stack text-inspector">
      <div className="region-heading">
        <div><span>文本框</span><strong>#{region.order}</strong></div>
        <span className={`review-state review-state--${region.confirmed ? 'confirmed' : region.ignored ? 'ignored' : 'pending'}`}>
          {region.confirmed ? '已确认' : region.ignored ? '已忽略' : '待复核'}
        </span>
      </div>
      <Field label="日文原文">
        <textarea
          aria-label="日文原文"
          onChange={(event) => updateRegion(region.id, { sourceText: event.target.value })}
          rows={5}
          spellCheck={false}
          value={region.sourceText}
        />
      </Field>
      <div className="text-meta"><span>{region.sourceText.length} 字符</span><span>OCR {confidencePercent === '' ? '未评分' : `${confidencePercent}%`}</span></div>
      <Field label="中文译文">
        <textarea
          aria-label="中文译文"
          lang="zh-CN"
          onChange={(event) => updateRegion(region.id, { translationText: event.target.value })}
          rows={6}
          value={region.translationText}
        />
      </Field>
      <div className="text-meta"><span>{region.translationText.length} 字符</span><span>{region.style.autoFit ? '自动适配字号' : '固定字号'}</span></div>
      <div className="field-grid">
        <Field label="类型">
          <select aria-label="文本类型" onChange={(event) => updateRegion(region.id, { type: event.target.value as RegionType })} value={region.type}>
            {Object.entries(regionTypeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Field>
        <Field label="方向">
          <select aria-label="文本方向" onChange={(event) => updateRegion(region.id, { direction: event.target.value as TextDirection })} value={region.direction}>
            {Object.entries(directionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </Field>
        <Field label="阅读顺序">
          <input
            aria-label="阅读顺序"
            min={0}
            onChange={(event) => updateRegion(region.id, { order: event.target.value === '' ? 0 : Number(event.target.value) })}
            type="number"
            value={region.order || ''}
          />
        </Field>
        <Field label="置信度 %" hint="可手动校正 OCR 评分">
          <input
            aria-label="置信度"
            max={100}
            min={0}
            onChange={(event) => updateRegion(region.id, { confidence: event.target.value === '' ? null : Number(event.target.value) / 100 })}
            step="0.1"
            type="number"
            value={confidencePercent}
          />
        </Field>
      </div>
      <Toggle checked={region.confirmed} description="计入页面复核进度" label="确认此文本框" onChange={(event) => updateRegion(region.id, { confirmed: event.target.checked })} />
      <Toggle checked={region.ignored} description="图像处理会跳过；导出 JSON 仍保留此记录" label="忽略此文本框" onChange={(event) => updateRegion(region.id, { ignored: event.target.checked })} />
      <div className="split-actions" aria-label="拆分文本框">
        <button className="button" onClick={() => splitSelectedRegion('horizontal')} type="button">水平中线拆分</button>
        <button className="button" onClick={() => splitSelectedRegion('vertical')} type="button">垂直中线拆分</button>
      </div>
      <button className="button button--danger" onClick={deleteSelectedRegions} type="button">删除文本框</button>
    </div>
  );
}

function TypesettingInspector({ region }: { region: Region | undefined }) {
  const updateRegion = useWorkbenchStore((state) => state.updateRegion);
  if (!region) return <EmptyState icon="字" title="选择一个文本框" description="排版参数会按文本框单独保存。" />;
  const style = region.style;
  const updateStyle = (patch: Partial<Region['style']>) => updateRegion(region.id, { style: { ...style, ...patch } });
  return (
    <div className="form-stack">
      <Toggle checked={style.autoFit} description="缩小字号直到译文放入区域" label="自动适配字号" onChange={(event) => updateStyle({ autoFit: event.target.checked })} />
      <Field label="字体族" hint="使用本机已安装字体；字体文件不会被上传。">
        <input onChange={(event) => updateStyle({ fontFamily: event.target.value })} value={style.fontFamily} />
      </Field>
      <div className="field-grid">
        <Field label="字号 px"><input disabled={style.autoFit} min={6} onChange={(event) => updateStyle({ fontSize: Number(event.target.value) })} type="number" value={style.fontSize} /></Field>
        <Field label="行高"><input min={0.7} onChange={(event) => updateStyle({ lineHeight: Number(event.target.value) })} step="0.05" type="number" value={style.lineHeight} /></Field>
        <Field label="字间距 px"><input onChange={(event) => updateStyle({ letterSpacing: Number(event.target.value) })} step="0.5" type="number" value={style.letterSpacing} /></Field>
        <Field label="内边距 px"><input min={0} onChange={(event) => updateStyle({ padding: Number(event.target.value) })} type="number" value={style.padding} /></Field>
      </div>
      <div className="field-grid">
        <Field label="文字颜色"><input aria-label="文字颜色" onChange={(event) => updateStyle({ color: event.target.value })} type="color" value={style.color} /></Field>
        <Field label="描边颜色"><input aria-label="描边颜色" onChange={(event) => updateStyle({ strokeColor: event.target.value })} type="color" value={style.strokeColor} /></Field>
        <Field label="描边 px"><input min={0} onChange={(event) => updateStyle({ strokeWidth: Number(event.target.value) })} step="0.5" type="number" value={style.strokeWidth} /></Field>
        <Field label="对齐">
          <select onChange={(event) => updateStyle({ align: event.target.value as Region['style']['align'] })} value={style.align}>
            <option value="start">起始</option><option value="center">居中</option><option value="end">末端</option>
          </select>
        </Field>
      </div>
      <div className="typeset-preview" style={{ color: style.color, fontFamily: style.fontFamily, fontSize: `${Math.min(style.fontSize, 32)}px`, lineHeight: style.lineHeight, WebkitTextStroke: `${style.strokeWidth}px ${style.strokeColor}` }}>
        {region.translationText || '中文排版预览'}
      </div>
    </div>
  );
}

function RepairInspector({ region }: { region: Region | undefined }) {
  const image = useWorkbenchStore(activeImage);
  const updateRegion = useWorkbenchStore((state) => state.updateRegion);
  const startBatch = useWorkbenchStore((state) => state.startBatch);
  if (!region) return <EmptyState icon="◌" title="选择一个文本框" description="蒙版与修复参数会按区域保存。" />;
  const repair = region.repair;
  const updateRepair = (patch: Partial<Region['repair']>) => updateRegion(region.id, { repair: { ...repair, ...patch } });
  return (
    <div className="form-stack">
      <div className="notice notice--local"><b>本地处理</b><span>擦字只把区域坐标交给本机 OpenCV provider，不会发送图像。</span></div>
      <Field label="修复方法">
        <select onChange={(event) => updateRepair({ method: event.target.value as Region['repair']['method'] })} value={repair.method}>
          <option value="telea">OpenCV Telea</option>
          <option value="navier_stokes">OpenCV Navier–Stokes</option>
          <option value="solid">纯色填充</option>
        </select>
      </Field>
      <div className="field-grid">
        <Field label="蒙版外扩 px"><input min={0} onChange={(event) => updateRepair({ maskPadding: Number(event.target.value) })} type="number" value={repair.maskPadding} /></Field>
        <Field label="膨胀 px"><input min={0} onChange={(event) => updateRepair({ dilation: Number(event.target.value) })} type="number" value={repair.dilation} /></Field>
        <Field label="修复半径"><input min={1} onChange={(event) => updateRepair({ radius: Number(event.target.value) })} type="number" value={repair.radius} /></Field>
        <Field label="填充色"><input aria-label="修复填充色" onChange={(event) => updateRepair({ fillColor: event.target.value })} type="color" value={repair.fillColor} /></Field>
      </div>
      <div className="mask-preview">
        <span style={{ inset: `${Math.max(8, 22 - repair.maskPadding)}px` }}>MASK</span>
      </div>
      <button
        className="button button--accent"
        disabled={!image}
        onClick={() => image && void startBatch(['inpaint'], [image.id], defaultExportOptions)}
        type="button"
      >
        修复当前页
      </button>
    </div>
  );
}

function ProviderSelect({
  label,
  kind,
  value,
  providers,
  onChange,
}: {
  label: string;
  kind: ProviderCapability['kind'];
  value: string;
  providers: ProviderCapability[];
  onChange: (value: string) => void;
}) {
  const choices = providers.filter((provider) => provider.kind === kind);
  const selected = choices.find((provider) => provider.id === value);
  return (
    <div className="provider-setting">
      <Field label={label}>
        <select onChange={(event) => onChange(event.target.value)} value={value}>
          {!selected && value ? <option value={value}>{value}（能力未报告）</option> : null}
          {!choices.length && !value ? <option value="">无可用 provider</option> : null}
          {choices.map((provider) => (
            <option disabled={!provider.available && !provider.configurable} key={provider.id} value={provider.id}>
              {provider.label}{provider.isMock ? ' [演示 MOCK]' : ''}{provider.available ? '' : provider.configurable ? ' [未配置]' : ' [不可用]'}
            </option>
          ))}
        </select>
      </Field>
      <ProviderBadge provider={selected} />
    </div>
  );
}

function ProjectInspector() {
  const project = useWorkbenchStore((state) => state.currentProject);
  const providers = useWorkbenchStore((state) => state.capabilities.providers);
  const updateProjectSettings = useWorkbenchStore((state) => state.updateProjectSettings);
  const [sessionKey, setSessionKey] = useState('');
  const [sessionConfigured, setSessionConfigured] = useState(project?.settings.apiKeyConfigured ?? false);
  const [credentialState, setCredentialState] = useState('');
  if (!project) return <EmptyState icon="⚙" title="未打开项目" />;
  const settings = project.settings;
  const update = (patch: Partial<ProjectSettings>) => updateProjectSettings(patch);
  const translator = providers.find((provider) => provider.kind === 'translator' && provider.id === settings.translatorProvider);
  const isRemote = translator ? !translator.local : Boolean(settings.remoteEndpoint);

  async function saveSessionKey() {
    setCredentialState('正在写入当前服务会话…');
    try {
      const result = await api.setSessionCredential(
        settings.translatorProvider,
        sessionKey,
        settings.remoteEndpoint,
        settings.remoteModel,
      );
      setSessionConfigured(result.configured);
      useWorkbenchStore.setState({ capabilities: result.capabilities });
      setSessionKey('');
      setCredentialState(result.configured ? '已配置，仅当前后端会话有效。' : '凭据未配置。');
    } catch (error) {
      setCredentialState(error instanceof Error ? error.message : '凭据配置失败');
    }
  }

  return (
    <div className="form-stack project-inspector">
      <section className="inspector-section">
        <h3>语言与上下文</h3>
        <div className="field-grid">
          <Field label="源语言"><select onChange={(event) => update({ sourceLanguage: event.target.value })} value={settings.sourceLanguage}><option value="ja">日语</option><option value="zh-CN">简体中文</option><option value="en">英语</option></select></Field>
          <Field label="目标语言"><select onChange={(event) => update({ targetLanguage: event.target.value })} value={settings.targetLanguage}><option value="zh-CN">简体中文</option><option value="zh-TW">繁体中文</option><option value="en">英语</option></select></Field>
          <Field label="相邻文本层级" hint="仅发送当前页前后相邻文本块；0 表示不带上下文。"><input max={5} min={0} onChange={(event) => update({ contextPages: Number(event.target.value) })} type="number" value={settings.contextPages} /></Field>
        </div>
      </section>
      <section className="inspector-section">
        <h3>处理 Provider</h3>
        <ProviderSelect kind="detector" label="文本检测" onChange={(detectorProvider) => update({ detectorProvider })} providers={providers} value={settings.detectorProvider} />
        <ProviderSelect kind="ocr" label="日文 OCR" onChange={(ocrProvider) => update({ ocrProvider })} providers={providers} value={settings.ocrProvider} />
        <ProviderSelect kind="translator" label="翻译" onChange={(translatorProvider) => update({ translatorProvider })} providers={providers} value={settings.translatorProvider} />
        <ProviderSelect kind="inpainter" label="图像修复" onChange={(inpainterProvider) => update({ inpainterProvider })} providers={providers} value={settings.inpainterProvider} />
      </section>
      {translator?.isMock ? (
        <div className="notice notice--mock"><b>演示 MOCK 翻译</b><span>输出是确定性演示文本，不代表真实翻译质量，导出前必须复核。</span></div>
      ) : null}
      <div className={`notice ${isRemote ? 'notice--remote' : 'notice--local'}`}>
        <b>{isRemote ? '远程文本翻译' : '本地 / 手动模式'}</b>
        <span>
          {isRemote
            ? '只会发送当前文本、当前页前后相邻文本块、术语表和角色名；原图、擦除图和项目路径绝不发送。请确认你有权向所选服务提交文本。'
            : '图像和文本留在本机；手动模式不会发起任何外部请求。'}
        </span>
      </div>
      {isRemote ? (
        <>
          <Field label="兼容 API 地址" hint="地址会写入项目配置；不要在 URL 中放凭据。"><input onChange={(event) => update({ remoteEndpoint: event.target.value })} placeholder="https://example.com/v1" type="url" value={settings.remoteEndpoint} /></Field>
          <Field label="模型名称"><input onChange={(event) => update({ remoteModel: event.target.value })} value={settings.remoteModel} /></Field>
          <Field label="API Key（仅当前会话）" hint="密钥不会写入 SQLite、project.json、浏览器存储或日志。">
            <div className="inline-input-button"><input autoComplete="off" onChange={(event) => setSessionKey(event.target.value)} placeholder={sessionConfigured ? '已在当前会话配置' : 'sk-…'} type="password" value={sessionKey} /><button className="button" disabled={!sessionKey} onClick={() => void saveSessionKey()} type="button">应用</button></div>
          </Field>
          {credentialState ? <p className="credential-state" role="status">{credentialState}</p> : null}
        </>
      ) : null}
      <Field label="术语表" hint="每行“日文 = 中文”。远程模式下此文本会随翻译请求发送。"><textarea onChange={(event) => update({ glossary: event.target.value })} rows={5} value={settings.glossary} /></Field>
      <Field label="角色名" hint="每行一个名字或“日文 = 中文”。"><textarea onChange={(event) => update({ characterNames: event.target.value })} rows={4} value={settings.characterNames} /></Field>
      <Toggle checked={settings.preserveTree} description="导出时重建导入时的相对目录" label="保留目录结构" onChange={(event) => update({ preserveTree: event.target.checked })} />
    </div>
  );
}

export function Inspector() {
  const tab = useWorkbenchStore((state) => state.rightTab);
  const setRightTab = useWorkbenchStore((state) => state.setRightTab);
  const activeImageId = useWorkbenchStore((state) => state.activeImageId);
  const regionsByImage = useWorkbenchStore((state) => state.regionsByImage);
  const selectedRegionIds = useWorkbenchStore((state) => state.selectedRegionIds);
  const regions = activeImageId ? regionsByImage[activeImageId] ?? EMPTY_REGIONS : EMPTY_REGIONS;
  const selected = useMemo(() => {
    const ids = new Set(selectedRegionIds);
    return regions.filter((region) => ids.has(region.id));
  }, [regions, selectedRegionIds]);

  return (
    <aside className="inspector panel" aria-label="属性检查器">
      <nav className="inspector-tabs" aria-label="属性标签">
        {([
          ['text', '文本'],
          ['typesetting', '排版'],
          ['repair', '修复'],
          ['project', '项目'],
        ] as const).map(([value, label]) => (
          <button aria-selected={tab === value} key={value} onClick={() => setRightTab(value)} role="tab" type="button">{label}</button>
        ))}
      </nav>
      <div className="inspector__content" role="tabpanel">
        {tab === 'text' ? <TextInspector regions={regions} selected={selected} /> : null}
        {tab === 'typesetting' ? <TypesettingInspector region={selected.length === 1 ? selected[0] : undefined} /> : null}
        {tab === 'repair' ? <RepairInspector region={selected.length === 1 ? selected[0] : undefined} /> : null}
        {tab === 'project' ? <ProjectInspector /> : null}
      </div>
    </aside>
  );
}
