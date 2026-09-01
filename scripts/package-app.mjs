import {
  accessSync,
  chmodSync,
  constants,
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  realpathSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
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
import { resolveCanonicalUv } from './external-uv.mjs';
import { resolveGuardedModelBundle } from './storage-model-route.mjs';
import { resolveGuardedRuntimeEnvironment } from './storage-runtime-route.mjs';

const root = path.resolve(import.meta.dirname, '..');

function strictChild(parent, candidate, label) {
  const relative = path.relative(parent, candidate);
  if (
    !relative
    || relative === '..'
    || relative.startsWith(`..${path.sep}`)
    || path.isAbsolute(relative)
  ) {
    throw new Error(`${label} must be a child of its verified external root`);
  }
  return relative;
}

function guardedPackagePath(key, { env, guard, runGuard }) {
  const result = runGuard(guard, ['--get-path', key], {
    encoding: 'utf8',
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error || result.status !== 0) {
    const detail = String(result.stderr ?? '').trim();
    throw new Error(
      `storage governance guard rejected package routing${detail ? `: ${detail}` : ''}`,
    );
  }
  const lines = String(result.stdout ?? '').trimEnd().split(/\r?\n/);
  if (lines.length !== 1 || !path.isAbsolute(lines[0])) {
    throw new Error(`storage mapping ${key} is missing, ambiguous, or not absolute`);
  }
  return path.resolve(lines[0]);
}

function requireRealDirectory(candidate, label, {
  lstatPath,
  realpathPath,
  expectedDevice = null,
}) {
  const info = lstatPath(candidate);
  if (info.isSymbolicLink() || !info.isDirectory()) {
    throw new Error(`${label} is not a real directory`);
  }
  if (realpathPath(candidate) !== candidate) {
    throw new Error(`${label} path is not canonical`);
  }
  if (expectedDevice !== null && info.dev !== expectedDevice) {
    throw new Error(`${label} escaped the verified external filesystem`);
  }
  return info;
}

function validateExistingDirectoryComponents(parent, candidate, label, {
  pathExists,
  lstatPath,
  expectedDevice,
}) {
  const relative = strictChild(parent, candidate, label);
  let cursor = parent;
  for (const part of relative.split(path.sep)) {
    cursor = path.join(cursor, part);
    if (!pathExists(cursor)) break;
    const info = lstatPath(cursor);
    if (info.isSymbolicLink()) {
      throw new Error(`${label} path must not contain symbolic links`);
    }
    if (!info.isDirectory()) {
      throw new Error(`${label} path contains a non-directory component`);
    }
    if (info.dev !== expectedDevice) {
      throw new Error(`${label} escaped the verified external filesystem`);
    }
  }
}

export function resolveGuardedPackageStorage({
  requestedDest,
  env = process.env,
  runGuard = spawnSync,
  pathExists = existsSync,
  lstatPath = lstatSync,
  realpathPath = realpathSync,
  accessPath = accessSync,
} = {}) {
  const home = env.HOME?.trim();
  const guard = env.STORAGE_GOVERNANCE_GUARD?.trim()
    || (home ? path.join(home, '.config', 'storage-governance', 'guard.sh') : '');
  if (!guard || !path.isAbsolute(guard)) {
    throw new Error('storage governance guard path is missing or not absolute');
  }
  const guardInfo = lstatPath(guard);
  if (guardInfo.isSymbolicLink() || !guardInfo.isFile() || realpathPath(guard) !== guard) {
    throw new Error('storage governance guard is not a canonical regular file');
  }
  accessPath(guard, constants.X_OK);

  const guardOptions = { env, guard, runGuard };
  const artifactsRoot = guardedPackagePath('roots.artifacts', guardOptions);
  const cachesRoot = guardedPackagePath('roots.caches', guardOptions);
  const packageDest = guardedPackagePath(
    'mappings.manga_localizer.package_dest',
    guardOptions,
  );
  const uvCache = guardedPackagePath('mappings.manga_localizer.uv_cache', guardOptions);

  const artifactsInfo = requireRealDirectory(artifactsRoot, 'external artifacts root', {
    lstatPath,
    realpathPath,
  });
  requireRealDirectory(cachesRoot, 'external caches root', {
    lstatPath,
    realpathPath,
    expectedDevice: artifactsInfo.dev,
  });
  if (packageDest !== path.join(artifactsRoot, 'manga-localizer', 'macos')) {
    throw new Error('manga-localizer package destination drifted from the registered topology');
  }
  if (uvCache !== path.join(cachesRoot, 'uv')) {
    throw new Error('manga-localizer uv cache drifted from the registered topology');
  }
  validateExistingDirectoryComponents(artifactsRoot, packageDest, 'package destination', {
    pathExists,
    lstatPath,
    expectedDevice: artifactsInfo.dev,
  });
  validateExistingDirectoryComponents(cachesRoot, uvCache, 'uv cache', {
    pathExists,
    lstatPath,
    expectedDevice: artifactsInfo.dev,
  });
  requireRealDirectory(uvCache, 'external uv cache', {
    lstatPath,
    realpathPath,
    expectedDevice: artifactsInfo.dev,
  });

  if (requestedDest && path.resolve(requestedDest) !== packageDest) {
    throw new Error('package destination must match the guarded external mapping');
  }
  return { artifactsRoot, cachesRoot, packageDest, uvCache };
}

export function parsePackageArgs(argv, {
  home = process.env.HOME,
} = {}) {
  const destIndex = argv.indexOf('--dest');
  if (destIndex >= 0 && (!argv[destIndex + 1] || argv[destIndex + 1].startsWith('--'))) {
    throw new Error('--dest requires an absolute or relative path');
  }
  if (argv.includes('--install-user') && argv.includes('--no-install-user')) {
    throw new Error('--install-user and --no-install-user are mutually exclusive');
  }
  return {
    dest: destIndex >= 0 ? path.resolve(argv[destIndex + 1]) : undefined,
    skipDownload: argv.includes('--skip-download'),
    installUser: argv.includes('--install-user'),
    home,
  };
}

export function resolveUserMaintenanceInstaller({
  home = process.env.HOME,
  pathExists = existsSync,
  lstatPath = lstatSync,
  realpathPath = realpathSync,
  accessPath = accessSync,
} = {}) {
  const normalizedHome = home?.trim();
  if (
    !normalizedHome
    || !path.isAbsolute(normalizedHome)
    || path.resolve(normalizedHome) !== normalizedHome
    || !pathExists(normalizedHome)
  ) {
    throw new Error('HOME must be an existing absolute canonical directory');
  }
  const homeInfo = lstatPath(normalizedHome);
  if (
    homeInfo.isSymbolicLink()
    || !homeInfo.isDirectory()
    || realpathPath(normalizedHome) !== normalizedHome
  ) {
    throw new Error('HOME must be an existing absolute canonical directory');
  }
  const installer = path.join(
    normalizedHome,
    '.local',
    'libexec',
    'storage-governance',
    'manga-localizer-install',
  );
  if (!pathExists(installer)) {
    throw new Error('governed Manga Localizer maintenance installer is missing');
  }
  const installerInfo = lstatPath(installer);
  if (
    installerInfo.isSymbolicLink()
    || !installerInfo.isFile()
    || realpathPath(installer) !== installer
  ) {
    throw new Error('governed Manga Localizer maintenance installer is not a canonical regular file');
  }
  accessPath(installer, constants.X_OK);
  return installer;
}

export function installUserThinApp({
  home = process.env.HOME,
  env = process.env,
  runInstaller = spawnSync,
  ...resolverOptions
} = {}) {
  const installer = resolveUserMaintenanceInstaller({ home, ...resolverOptions });
  const result = runInstaller(installer, ['--install'], {
    cwd: root,
    env: { ...env, HOME: home },
    stdio: 'inherit',
  });
  if (result.error) throw result.error;
  if ((result.status ?? 1) !== 0) {
    throw new Error(`governed Manga Localizer maintenance installer failed with status ${result.status ?? 1}`);
  }
  return installer;
}

export function bundleRuntimeEnvironment(layout, {
  packageDest,
  bundleRoot = packageDest,
  uvCache,
  env = process.env,
} = {}) {
  if (!path.isAbsolute(packageDest) || !path.isAbsolute(bundleRoot)) {
    throw new Error('guarded package and bundle roots must be absolute');
  }
  if (bundleRoot !== packageDest) {
    strictChild(packageDest, bundleRoot, 'package build root');
  }
  const expectedBackend = appBundleLayout(bundleRoot).backend;
  if (layout.backend !== expectedBackend) {
    throw new Error('bundle backend escaped the guarded package destination');
  }
  if (!path.isAbsolute(uvCache)) {
    throw new Error('guarded external uv cache is missing or not absolute');
  }
  return {
    ...env,
    UV_PROJECT_ENVIRONMENT: path.join(layout.backend, '.venv'),
    UV_CACHE_DIR: uvCache,
    PYTHONNOUSERSITE: '1',
  };
}

export function packageModelArguments({
  bundleDest,
  modelSource,
  skipDownload = false,
} = {}) {
  for (const [label, candidate] of Object.entries({ bundleDest, modelSource })) {
    if (!candidate || !path.isAbsolute(candidate)) {
      throw new Error(`${label} must be an absolute guarded external path`);
    }
  }
  const args = [
    'run',
    '--project',
    'backend',
    'python',
    path.join(root, 'scripts', 'setup_optional_models.py'),
    '--bundle-dest',
    bundleDest,
    '--copy-from-models-dir',
    modelSource,
  ];
  if (skipDownload) args.push('--no-download');
  args.push(...BUNDLE_MODEL_SELECTION);
  return args;
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

function run(command, args, { cwd = root, allowFail = false, env = process.env } = {}) {
  const result = spawnSync(command, args, { cwd, stdio: 'inherit', env });
  if (!allowFail && result.status !== 0) {
    throw new Error(`${command} ${args.join(' ')} failed with status ${result.status ?? 1}`);
  }
  return result;
}

function copyTree(source, destination) {
  mkdirSync(path.dirname(destination), { recursive: true });
  cpSync(source, destination, { recursive: true });
}

function optionalLstat(candidate, lstatPath = lstatSync) {
  try {
    return lstatPath(candidate);
  } catch (error) {
    if (error && typeof error === 'object' && error.code === 'ENOENT') return null;
    throw error;
  }
}

export function validatePackagedApp(layout, {
  expectedDevice,
  lstatPath = lstatSync,
  realpathPath = realpathSync,
  accessPath = accessSync,
} = {}) {
  const appInfo = requireRealDirectory(layout.app, 'packaged app', {
    lstatPath,
    realpathPath,
    expectedDevice,
  });
  const device = expectedDevice ?? appInfo.dev;
  for (const [label, filename] of [
    ['Info.plist', layout.infoPlist],
    ['app executable', layout.executable],
    ['frontend index', path.join(layout.frontend, 'index.html')],
    ['backend pyvenv.cfg', path.join(layout.backend, '.venv', 'pyvenv.cfg')],
    ['backend launcher', layout.launcher],
    ['model manifest', layout.manifest],
  ]) {
    const info = lstatPath(filename);
    if (info.isSymbolicLink() || !info.isFile() || info.dev !== device) {
      throw new Error(`packaged ${label} is missing, linked, or on the wrong filesystem`);
    }
  }
  accessPath(layout.executable, constants.X_OK);
  accessPath(path.join(layout.backend, '.venv', 'bin', 'python'), constants.X_OK);
  return layout;
}

export function publishPackagedApp({
  stagedLayout,
  finalLayout,
  stagingRoot,
  previousApp,
  lstatPath = lstatSync,
  renamePath = renameSync,
  removePath = rmSync,
  validateApp = validatePackagedApp,
} = {}) {
  const destinationRoot = path.dirname(finalLayout.app);
  if (
    stagingRoot !== path.join(destinationRoot, '.manga-localizer-package-stage')
    || stagedLayout.app !== appBundleLayout(stagingRoot).app
    || previousApp !== path.join(destinationRoot, `.${APP_BUNDLE_NAME}.package-previous`)
  ) {
    throw new Error('package publication paths do not match the fixed guarded topology');
  }
  const destinationInfo = lstatPath(destinationRoot);
  if (destinationInfo.isSymbolicLink() || !destinationInfo.isDirectory()) {
    throw new Error('guarded package destination is not a real directory');
  }
  validateApp(stagedLayout, { expectedDevice: destinationInfo.dev });
  if (optionalLstat(previousApp, lstatPath)) {
    throw new Error('package rollback path is occupied');
  }
  const existing = optionalLstat(finalLayout.app, lstatPath);
  if (existing && (existing.isSymbolicLink() || !existing.isDirectory())) {
    throw new Error('existing packaged app is not a real directory');
  }

  let priorMoved = false;
  let candidateMoved = false;
  try {
    if (existing) {
      renamePath(finalLayout.app, previousApp);
      priorMoved = true;
    }
    renamePath(stagedLayout.app, finalLayout.app);
    candidateMoved = true;
    validateApp(finalLayout, { expectedDevice: destinationInfo.dev });
  } catch (error) {
    try {
      if (
        candidateMoved
        && optionalLstat(finalLayout.app, lstatPath)
        && !optionalLstat(stagedLayout.app, lstatPath)
      ) {
        renamePath(finalLayout.app, stagedLayout.app);
      }
      if (
        priorMoved
        && optionalLstat(previousApp, lstatPath)
        && !optionalLstat(finalLayout.app, lstatPath)
      ) {
        renamePath(previousApp, finalLayout.app);
      }
    } catch (rollbackError) {
      throw new AggregateError(
        [error, rollbackError],
        'packaged app publication failed and rollback did not complete',
      );
    }
    throw error;
  }

  if (priorMoved) {
    removePath(previousApp, { recursive: true, force: false });
  }
  if (optionalLstat(stagingRoot, lstatPath)) {
    removePath(stagingRoot, { recursive: true, force: false });
  }
  return finalLayout;
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
  dest: requestedDest,
  skipDownload = false,
  installUser = false,
  home = process.env.HOME,
  env = process.env,
} = {}) {
  let storage = resolveGuardedPackageStorage({ requestedDest, env });
  const canonicalUv = resolveCanonicalUv({ env });
  const modelSource = resolveGuardedModelBundle({ env });
  const frontendDist = path.join(root, 'frontend', 'dist');
  if (!existsSync(path.join(frontendDist, 'index.html'))) {
    run('npm', ['--prefix', 'frontend', 'run', 'build']);
  }
  storage = resolveGuardedPackageStorage({ requestedDest: storage.packageDest, env });
  const dest = storage.packageDest;
  const stagingRoot = path.join(dest, '.manga-localizer-package-stage');
  const previousApp = path.join(dest, `.${APP_BUNDLE_NAME}.package-previous`);
  if (optionalLstat(stagingRoot) || optionalLstat(previousApp)) {
    throw new Error('package staging or rollback path is occupied');
  }
  let layout;
  let helperReady = false;
  try {
    const stagedLayout = writeAppSkeleton(stagingRoot);
    copyTree(frontendDist, stagedLayout.frontend);
    mkdirSync(stagedLayout.backend, { recursive: true });
    copyTree(path.join(root, 'backend', 'src'), path.join(stagedLayout.backend, 'src'));
    cpSync(path.join(root, 'backend', 'pyproject.toml'), path.join(stagedLayout.backend, 'pyproject.toml'));
    cpSync(path.join(root, 'backend', 'uv.lock'), path.join(stagedLayout.backend, 'uv.lock'));
    storage = resolveGuardedPackageStorage({ requestedDest: dest, env });
    run(
      canonicalUv,
      ['sync', '--project', stagedLayout.backend, '--frozen', '--extra', 'ai', '--extra', 'mt', '--no-dev'],
      { env: bundleRuntimeEnvironment(stagedLayout, {
        packageDest: storage.packageDest,
        bundleRoot: stagingRoot,
        uvCache: storage.uvCache,
        env,
      }) },
    );
    cpSync(path.join(root, 'scripts', 'macos_app_launcher.py'), stagedLayout.launcher);
    const modelArgs = packageModelArguments({
      bundleDest: stagedLayout.models,
      modelSource,
      skipDownload,
    });
    run(canonicalUv, modelArgs, { env: resolveGuardedRuntimeEnvironment({ env }).environment });
    helperReady = compileWindowHelper(stagedLayout);
    layout = publishPackagedApp({
      stagedLayout,
      finalLayout: appBundleLayout(dest),
      stagingRoot,
      previousApp,
    });
  } catch (error) {
    if (optionalLstat(stagingRoot)) {
      rmSync(stagingRoot, { recursive: true, force: false });
    }
    throw error;
  }
  if (installUser) {
    installUserThinApp({ home, env });
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
