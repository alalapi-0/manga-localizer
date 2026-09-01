import { createHash } from 'node:crypto';
import {
  accessSync,
  constants,
  existsSync,
  lstatSync,
  readFileSync,
  realpathSync,
  writeFileSync,
} from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';

const defaultProjectRoot = path.resolve(import.meta.dirname, '..');
const markerName = '.manga-localizer-runtime';
const rootMarkerName = '.storage-governance';
const rootMarkerContent = 'storage-governance:manga-localizer:runtime:v1\n';

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

function validateChildComponents(root, candidate, label, {
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

function runtimeMarker(projectRoot, readFile = readFileSync) {
  const lock = path.join(projectRoot, 'backend', 'uv.lock');
  const lockSha256 = createHash('sha256').update(readFile(lock)).digest('hex');
  return {
    lockSha256,
    content: `manga-localizer-backend-runtime:v1:${lockSha256}\n`,
  };
}

function guardedPath(key, {
  env,
  guard,
  runGuard,
}) {
  const result = runGuard(guard, ['--get-path', key], {
    encoding: 'utf8',
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error || result.status !== 0) {
    const detail = String(result.stderr ?? '').trim();
    throw new Error(
      `storage governance guard rejected runtime routing${detail ? `: ${detail}` : ''}`,
    );
  }
  const lines = String(result.stdout ?? '').trimEnd().split(/\r?\n/);
  if (lines.length !== 1 || !path.isAbsolute(lines[0])) {
    throw new Error(`storage mapping ${key} is missing, ambiguous, or not absolute`);
  }
  return path.resolve(lines[0]);
}

function validateRuntimePayload(runtimeVenv, projectRoot, device, {
  pathExists,
  lstatPath,
  readFile,
  accessPath,
  requireMarker,
}) {
  if (!pathExists(runtimeVenv)) {
    throw new Error('guarded external runtime is missing');
  }
  const runtimeInfo = lstatPath(runtimeVenv);
  if (runtimeInfo.isSymbolicLink() || !runtimeInfo.isDirectory() || runtimeInfo.dev !== device) {
    throw new Error('guarded external runtime is not a real directory on the verified filesystem');
  }

  const pyvenv = path.join(runtimeVenv, 'pyvenv.cfg');
  const pyvenvInfo = lstatPath(pyvenv);
  if (pyvenvInfo.isSymbolicLink() || !pyvenvInfo.isFile() || pyvenvInfo.dev !== device) {
    throw new Error('guarded external runtime has no valid pyvenv.cfg');
  }
  accessPath(path.join(runtimeVenv, 'bin', 'python'), constants.X_OK);

  if (requireMarker) {
    const expected = runtimeMarker(projectRoot, readFile).content;
    const marker = path.join(runtimeVenv, markerName);
    const markerInfo = lstatPath(marker);
    if (markerInfo.isSymbolicLink() || !markerInfo.isFile() || markerInfo.dev !== device) {
      throw new Error('guarded external runtime ownership marker is invalid');
    }
    if (readFile(marker, 'utf8') !== expected) {
      throw new Error('guarded external runtime does not match the current uv.lock');
    }
  }
}

export function resolveGuardedRuntimeEnvironment({
  env = process.env,
  runGuard = spawnSync,
  pathExists = existsSync,
  lstatPath = lstatSync,
  realpathPath = realpathSync,
  readFile = readFileSync,
  accessPath = accessSync,
  projectRoot = defaultProjectRoot,
  requireReady = true,
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
  const runtimesRoot = guardedPath('roots.runtimes', guardOptions);
  const cachesRoot = guardedPath('roots.caches', guardOptions);
  const runtimeRoot = guardedPath('mappings.manga_localizer.runtime_root', guardOptions);
  const runtimeVenv = guardedPath('mappings.manga_localizer.runtime_venv', guardOptions);
  const uvCache = guardedPath('mappings.manga_localizer.uv_cache', guardOptions);

  const runtimesInfo = requireRealDirectory(runtimesRoot, 'external runtimes root', {
    lstatPath,
    realpathPath,
  });
  const cachesInfo = requireRealDirectory(cachesRoot, 'external caches root', {
    lstatPath,
    realpathPath,
    expectedDevice: runtimesInfo.dev,
  });
  if (runtimeRoot !== path.join(runtimesRoot, 'manga-localizer')) {
    throw new Error('manga-localizer runtime root drifted from the registered topology');
  }
  if (runtimeVenv !== path.join(runtimeRoot, 'backend-venv')) {
    throw new Error('manga-localizer runtime venv drifted from the registered topology');
  }
  if (uvCache !== path.join(cachesRoot, 'uv')) {
    throw new Error('manga-localizer uv cache drifted from the registered topology');
  }

  validateChildComponents(runtimesRoot, runtimeRoot, 'runtime root', {
    pathExists,
    lstatPath,
    expectedDevice: runtimesInfo.dev,
  });
  const runtimeRootInfo = requireRealDirectory(runtimeRoot, 'manga-localizer runtime root', {
    lstatPath,
    realpathPath,
    expectedDevice: runtimesInfo.dev,
  });
  const rootMarker = path.join(runtimeRoot, rootMarkerName);
  const rootMarkerInfo = lstatPath(rootMarker);
  if (
    rootMarkerInfo.isSymbolicLink()
    || !rootMarkerInfo.isFile()
    || rootMarkerInfo.dev !== runtimeRootInfo.dev
    || readFile(rootMarker, 'utf8') !== rootMarkerContent
  ) {
    throw new Error('manga-localizer runtime root ownership marker is invalid');
  }
  validateChildComponents(runtimesRoot, runtimeVenv, 'runtime venv', {
    pathExists,
    lstatPath,
    expectedDevice: runtimesInfo.dev,
  });
  validateChildComponents(cachesRoot, uvCache, 'uv cache', {
    pathExists,
    lstatPath,
    expectedDevice: cachesInfo.dev,
  });
  requireRealDirectory(uvCache, 'external uv cache', {
    lstatPath,
    realpathPath,
    expectedDevice: runtimesInfo.dev,
  });

  if (requireReady) {
    validateRuntimePayload(runtimeVenv, projectRoot, runtimesInfo.dev, {
      pathExists,
      lstatPath,
      readFile,
      accessPath,
      requireMarker: true,
    });
  } else if (pathExists(runtimeVenv)) {
    validateRuntimePayload(runtimeVenv, projectRoot, runtimesInfo.dev, {
      pathExists,
      lstatPath,
      readFile,
      accessPath,
      requireMarker: false,
    });
  }

  return {
    runtimeRoot,
    runtimeVenv,
    uvCache,
    environment: {
      ...env,
      UV_PROJECT_ENVIRONMENT: runtimeVenv,
      UV_CACHE_DIR: uvCache,
      PYTHONNOUSERSITE: '1',
    },
  };
}

export function writeGuardedRuntimeMarker({
  env = process.env,
  runGuard = spawnSync,
  projectRoot = defaultProjectRoot,
  writeFile = writeFileSync,
} = {}) {
  const route = resolveGuardedRuntimeEnvironment({
    env,
    runGuard,
    projectRoot,
    requireReady: false,
  });
  const marker = runtimeMarker(projectRoot);
  writeFile(path.join(route.runtimeVenv, markerName), marker.content, { mode: 0o644 });
  return resolveGuardedRuntimeEnvironment({ env, runGuard, projectRoot, requireReady: true });
}

export function expectedRuntimeMarker(projectRoot = defaultProjectRoot) {
  return runtimeMarker(projectRoot).content;
}
