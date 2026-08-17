import { chmodSync, cpSync, existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';

import {
  APP_BUNDLE_NAME,
  BUNDLE_MODEL_SELECTION,
  appBundleLayout,
  infoPlistXml,
  macosWrapperScript,
} from './app-platform.mjs';

const root = path.resolve(import.meta.dirname, '..');

export function parsePackageArgs(argv, {
  home = process.env.HOME,
  platform = process.platform,
} = {}) {
  const destIndex = argv.indexOf('--dest');
  const dest = destIndex >= 0 ? argv[destIndex + 1] : path.join(root, 'dist', 'macos');
  return {
    dest: path.resolve(dest),
    skipDownload: argv.includes('--skip-download'),
    installUser: argv.includes('--install-user')
      || (platform === 'darwin' && !argv.includes('--no-install-user')),
    home,
  };
}

export function writeAppSkeleton(destRoot, { version = '0.2.0' } = {}) {
  const layout = appBundleLayout(destRoot);
  mkdirSync(layout.macos, { recursive: true });
  mkdirSync(layout.resources, { recursive: true });
  writeFileSync(layout.infoPlist, infoPlistXml({ version }));
  writeFileSync(layout.executable, macosWrapperScript(), { mode: 0o755 });
  chmodSync(layout.executable, 0o755);
  return layout;
}

function run(command, args, { cwd = root, allowFail = false } = {}) {
  const result = spawnSync(command, args, { cwd, stdio: 'inherit', env: process.env });
  if (!allowFail && result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed with status ${result.status ?? 1}`);
  }
  return result;
}

function copyTree(source, destination) {
  mkdirSync(path.dirname(destination), { recursive: true });
  cpSync(source, destination, { recursive: true });
}

function compileWindowHelper(layout) {
  const source = path.join(root, 'scripts', 'macos_webview.swift');
  if (!existsSync(source)) return false;
  const compiled = run(
    'swiftc',
    ['-O', '-framework', 'Cocoa', '-framework', 'WebKit', '-o', layout.windowHelper, source],
    { allowFail: true },
  );
  return compiled.status === 0 && existsSync(layout.windowHelper);
}

export function packageMacApp({
  dest = path.join(root, 'dist', 'macos'),
  skipDownload = false,
  installUser = false,
  home = process.env.HOME,
} = {}) {
  const frontendDist = path.join(root, 'frontend', 'dist');
  if (!existsSync(path.join(frontendDist, 'index.html'))) {
    run('npm', ['--prefix', 'frontend', 'run', 'build']);
  }
  if (existsSync(path.join(dest, APP_BUNDLE_NAME))) {
    rmSync(path.join(dest, APP_BUNDLE_NAME), { recursive: true, force: true });
  }
  const layout = writeAppSkeleton(dest);
  copyTree(frontendDist, layout.frontend);
  mkdirSync(layout.backend, { recursive: true });
  copyTree(path.join(root, 'backend', 'src'), path.join(layout.backend, 'src'));
  cpSync(path.join(root, 'backend', 'pyproject.toml'), path.join(layout.backend, 'pyproject.toml'));
  cpSync(path.join(root, 'backend', 'uv.lock'), path.join(layout.backend, 'uv.lock'));
  run('uv', ['sync', '--project', layout.backend, '--extra', 'ai', '--extra', 'mt', '--no-dev']);
  cpSync(path.join(root, 'scripts', 'macos_app_launcher.py'), layout.launcher);
  const staging = path.join(dest, '.model-staging');
  mkdirSync(staging, { recursive: true });
  const modelArgs = [
    'run',
    '--project',
    'backend',
    'python',
    path.join(root, 'scripts', 'setup_optional_models.py'),
    '--data-dir',
    staging,
    '--bundle-dest',
    layout.models,
    '--copy-from',
    path.join(root, '.manga-localizer'),
  ];
  if (home) {
    modelArgs.push('--copy-from', path.join(home, '.manga-localizer'));
  }
  if (skipDownload) {
    modelArgs.push('--no-download');
  }
  modelArgs.push(...BUNDLE_MODEL_SELECTION);
  run('uv', modelArgs);
  const helperReady = compileWindowHelper(layout);
  if (installUser && home) {
    const applications = path.join(home, 'Applications');
    mkdirSync(applications, { recursive: true });
    const installed = path.join(applications, APP_BUNDLE_NAME);
    rmSync(installed, { recursive: true, force: true });
    copyTree(layout.app, installed);
  }
  return { layout, windowHelper: helperReady };
}

if (import.meta.filename === path.resolve(process.argv[1] ?? '')) {
  try {
    const options = parsePackageArgs(process.argv.slice(2));
    const result = packageMacApp(options);
    console.log(`Packaged ${result.layout.app}`);
    if (result.windowHelper) {
      console.log('Compiled the native workbench window helper.');
    } else {
      console.log('Native window helper was not compiled; the app will use a Chromium app window if available.');
    }
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exit(1);
  }
}
