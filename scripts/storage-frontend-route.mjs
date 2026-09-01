import { createHash } from 'node:crypto';
import {
  accessSync,
  constants,
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';

const defaultProjectRoot = path.resolve(import.meta.dirname, '..');
const markerName = '.manga-localizer-frontend-runtime';
const rootMarkerName = '.storage-governance';
const rootMarkerContent = 'storage-governance:manga-localizer:runtime:v1\n';

function sha256File(filename, readFile = readFileSync) {
  return createHash('sha256').update(readFile(filename)).digest('hex');
}

export function expectedFrontendMarker(
  projectRoot = defaultProjectRoot,
  readFile = readFileSync,
) {
  const packageSha256 = sha256File(path.join(projectRoot, 'frontend', 'package.json'), readFile);
  const lockSha256 = sha256File(path.join(projectRoot, 'frontend', 'package-lock.json'), readFile);
  return `manga-localizer-frontend-runtime:v1:${packageSha256}:${lockSha256}\n`;
}

function strictChild(root, candidate, label) {
  const relative = path.relative(root, candidate);
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

function validateExistingDirectoryComponents(root, candidate, label, {
  pathExists,
  lstatPath,
  expectedDevice,
}) {
  const relative = strictChild(root, candidate, label);
  let cursor = root;
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

function guardedPath(key, { env, guard, runGuard }) {
  const result = runGuard(guard, ['--get-path', key], {
    encoding: 'utf8',
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error || result.status !== 0) {
    const detail = String(result.stderr ?? '').trim();
    throw new Error(
      `storage governance guard rejected frontend routing${detail ? `: ${detail}` : ''}`,
    );
  }
  const lines = String(result.stdout ?? '').trimEnd().split(/\r?\n/);
  if (lines.length !== 1 || !path.isAbsolute(lines[0])) {
    throw new Error(`storage mapping ${key} is missing, ambiguous, or not absolute`);
  }
  return path.resolve(lines[0]);
}

export function storageGovernanceGuardPath(env = process.env) {
  const home = env.HOME?.trim();
  return env.STORAGE_GOVERNANCE_GUARD?.trim()
    || (home ? path.join(home, '.config', 'storage-governance', 'guard.sh') : '');
}

function resolveTopology({
  env,
  runGuard,
  pathExists,
  lstatPath,
  realpathPath,
  readFile,
  accessPath,
  projectRoot,
  allowMissingPayload,
}) {
  const guard = storageGovernanceGuardPath(env);
  if (!guard || !path.isAbsolute(guard)) {
    throw new Error('storage governance guard path is missing or not absolute');
  }
  if (!pathExists(guard)) {
    throw new Error('storage governance guard is missing');
  }
  const guardInfo = lstatPath(guard);
  if (guardInfo.isSymbolicLink() || !guardInfo.isFile() || realpathPath(guard) !== guard) {
    throw new Error('storage governance guard is not a canonical regular file');
  }
  accessPath(guard, constants.X_OK);

  const guardOptions = { env, guard, runGuard };
  const runtimesRoot = guardedPath('roots.runtimes', guardOptions);
  const runtimeRoot = guardedPath('mappings.manga_localizer.runtime_root', guardOptions);
  const frontendRuntime = guardedPath(
    'mappings.manga_localizer.frontend_runtime',
    guardOptions,
  );
  const nodeModules = guardedPath(
    'mappings.manga_localizer.frontend_node_modules',
    guardOptions,
  );

  const runtimesInfo = requireRealDirectory(runtimesRoot, 'external runtimes root', {
    lstatPath,
    realpathPath,
  });
  if (runtimeRoot !== path.join(runtimesRoot, 'manga-localizer')) {
    throw new Error('manga-localizer runtime root drifted from the registered topology');
  }
  if (frontendRuntime !== path.join(runtimeRoot, 'frontend-runtime')) {
    throw new Error('frontend runtime root drifted from the registered topology');
  }
  if (nodeModules !== path.join(frontendRuntime, 'node_modules')) {
    throw new Error('frontend node_modules drifted from the registered topology');
  }

  validateExistingDirectoryComponents(runtimesRoot, runtimeRoot, 'runtime root', {
    pathExists,
    lstatPath,
    expectedDevice: runtimesInfo.dev,
  });
  const runtimeInfo = requireRealDirectory(runtimeRoot, 'manga-localizer runtime root', {
    lstatPath,
    realpathPath,
    expectedDevice: runtimesInfo.dev,
  });
  const rootMarker = path.join(runtimeRoot, rootMarkerName);
  const rootMarkerInfo = lstatPath(rootMarker);
  if (
    rootMarkerInfo.isSymbolicLink()
    || !rootMarkerInfo.isFile()
    || rootMarkerInfo.dev !== runtimeInfo.dev
    || readFile(rootMarker, 'utf8') !== rootMarkerContent
  ) {
    throw new Error('manga-localizer runtime root ownership marker is invalid');
  }

  validateExistingDirectoryComponents(runtimeRoot, frontendRuntime, 'frontend runtime', {
    pathExists,
    lstatPath,
    expectedDevice: runtimesInfo.dev,
  });
  validateExistingDirectoryComponents(runtimeRoot, nodeModules, 'frontend node_modules', {
    pathExists,
    lstatPath,
    expectedDevice: runtimesInfo.dev,
  });

  if (!allowMissingPayload || pathExists(frontendRuntime)) {
    requireRealDirectory(frontendRuntime, 'frontend runtime', {
      lstatPath,
      realpathPath,
      expectedDevice: runtimesInfo.dev,
    });
  }
  if (!allowMissingPayload || pathExists(nodeModules)) {
    requireRealDirectory(nodeModules, 'frontend node_modules', {
      lstatPath,
      realpathPath,
      expectedDevice: runtimesInfo.dev,
    });
  }

  return {
    guard,
    device: runtimesInfo.dev,
    runtimesRoot,
    runtimeRoot,
    frontendRuntime,
    nodeModules,
    localNodeModules: path.join(projectRoot, 'frontend', 'node_modules'),
  };
}

function validateReady(route, {
  lstatPath,
  realpathPath,
  readFile,
  accessPath,
  projectRoot,
}) {
  const marker = path.join(route.frontendRuntime, markerName);
  const markerInfo = lstatPath(marker);
  if (
    markerInfo.isSymbolicLink()
    || !markerInfo.isFile()
    || markerInfo.dev !== route.device
    || readFile(marker, 'utf8') !== expectedFrontendMarker(projectRoot, readFile)
  ) {
    throw new Error('guarded frontend runtime does not match the current package lock');
  }

  const linkInfo = lstatPath(route.localNodeModules);
  if (!linkInfo.isSymbolicLink() || realpathPath(route.localNodeModules) !== route.nodeModules) {
    throw new Error('local frontend node_modules is not the registered external link');
  }
  accessPath(path.join(route.nodeModules, '.bin', 'vite'), constants.X_OK);
  return route;
}

export function resolveGuardedFrontendRuntime({
  env = process.env,
  runGuard = spawnSync,
  pathExists = existsSync,
  lstatPath = lstatSync,
  realpathPath = realpathSync,
  readFile = readFileSync,
  accessPath = accessSync,
  projectRoot = defaultProjectRoot,
  requireReady = true,
  allowMissingPayload = false,
} = {}) {
  const route = resolveTopology({
    env,
    runGuard,
    pathExists,
    lstatPath,
    realpathPath,
    readFile,
    accessPath,
    projectRoot,
    allowMissingPayload,
  });
  if (requireReady) {
    return validateReady(route, {
      lstatPath,
      realpathPath,
      readFile,
      accessPath,
      projectRoot,
    });
  }
  return route;
}

export function writeGuardedFrontendMarker({
  env = process.env,
  runGuard = spawnSync,
  projectRoot = defaultProjectRoot,
} = {}) {
  const route = resolveGuardedFrontendRuntime({
    env,
    runGuard,
    projectRoot,
    requireReady: false,
  });
  copyFileSync(
    path.join(projectRoot, 'frontend', 'package.json'),
    path.join(route.frontendRuntime, 'package.json'),
  );
  copyFileSync(
    path.join(projectRoot, 'frontend', 'package-lock.json'),
    path.join(route.frontendRuntime, 'package-lock.json'),
  );
  const marker = path.join(route.frontendRuntime, markerName);
  const pending = `${marker}.next`;
  writeFileSync(pending, expectedFrontendMarker(projectRoot), { mode: 0o644 });
  renameSync(pending, marker);
  return route;
}

function localFrontendRoute(projectRoot, {
  lstatPath = lstatSync,
  realpathPath = realpathSync,
  accessPath = accessSync,
} = {}) {
  const nodeModules = path.join(projectRoot, 'frontend', 'node_modules');
  const info = lstatPath(nodeModules);
  if (info.isSymbolicLink() || !info.isDirectory() || realpathPath(nodeModules) !== nodeModules) {
    throw new Error('local frontend node_modules is not a canonical directory');
  }
  accessPath(path.join(nodeModules, '.bin', 'vite'), constants.X_OK);
  return { nodeModules, local: true };
}

export function allowsCiLocalFrontendRuntime(env = process.env) {
  return env.CI === 'true' && env.MANGA_LOCALIZER_CI_LOCAL_RUNTIME === '1';
}

export function resolveActiveFrontendRuntime({
  env = process.env,
  projectRoot = defaultProjectRoot,
  pathExists = existsSync,
  ...options
} = {}) {
  if (allowsCiLocalFrontendRuntime(env)) {
    return localFrontendRoute(projectRoot, options);
  }
  return resolveGuardedFrontendRuntime({ env, projectRoot, pathExists, ...options });
}

export function installActiveFrontendRuntime({
  env = process.env,
  projectRoot = defaultProjectRoot,
  runNpm = spawnSync,
  pathExists = existsSync,
  lstatPath = lstatSync,
  realpathPath = realpathSync,
} = {}) {
  if (allowsCiLocalFrontendRuntime(env)) {
    const result = runNpm(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['ci'], {
      cwd: path.join(projectRoot, 'frontend'),
      env,
      stdio: 'inherit',
    });
    if (result.error) throw result.error;
    return result.status ?? 1;
  }

  const route = resolveGuardedFrontendRuntime({
    env,
    projectRoot,
    pathExists,
    lstatPath,
    realpathPath,
    requireReady: false,
    allowMissingPayload: true,
  });
  if (pathExists(route.localNodeModules)) {
    const localInfo = lstatPath(route.localNodeModules);
    if (!localInfo.isSymbolicLink() || realpathPath(route.localNodeModules) !== route.nodeModules) {
      throw new Error('refusing to overwrite an unregistered local frontend dependency tree');
    }
  }
  mkdirSync(route.frontendRuntime, { recursive: true });
  copyFileSync(
    path.join(projectRoot, 'frontend', 'package.json'),
    path.join(route.frontendRuntime, 'package.json'),
  );
  copyFileSync(
    path.join(projectRoot, 'frontend', 'package-lock.json'),
    path.join(route.frontendRuntime, 'package-lock.json'),
  );
  rmSync(path.join(route.frontendRuntime, markerName), { force: true });
  const result = runNpm(process.platform === 'win32' ? 'npm.cmd' : 'npm', ['ci'], {
    cwd: route.frontendRuntime,
    env,
    stdio: 'inherit',
  });
  if (result.error) throw result.error;
  if ((result.status ?? 1) !== 0) return result.status ?? 1;
  writeGuardedFrontendMarker({ env, projectRoot });
  if (!pathExists(route.localNodeModules)) {
    symlinkSync(route.nodeModules, route.localNodeModules, 'dir');
  }
  resolveGuardedFrontendRuntime({ env, projectRoot });
  return 0;
}
