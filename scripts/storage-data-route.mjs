import {
  accessSync,
  constants,
  existsSync,
  lstatSync,
  realpathSync,
} from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';

function guardedPath(key, { env, guard, runGuard }) {
  const result = runGuard(guard, ['--get-path', key], {
    encoding: 'utf8',
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error || result.status !== 0) {
    const detail = String(result.stderr ?? '').trim();
    throw new Error(
      `storage governance guard rejected project data routing${detail ? `: ${detail}` : ''}`,
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

function rejectConflictingOverride(env, name, expected) {
  const configured = env[name]?.trim();
  if (!configured) return;
  if (!path.isAbsolute(configured) || path.resolve(configured) !== expected) {
    throw new Error(`${name} conflicts with the registered project data route`);
  }
}

export function resolveGuardedProjectData({
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
  if (!guard || !path.isAbsolute(guard) || !pathExists(guard)) {
    throw new Error('storage governance guard is missing or not absolute');
  }
  const guardInfo = lstatPath(guard);
  if (guardInfo.isSymbolicLink() || !guardInfo.isFile() || realpathPath(guard) !== guard) {
    throw new Error('storage governance guard is not a canonical regular file');
  }
  accessPath(guard, constants.X_OK);

  const options = { env, guard, runGuard };
  const projectDataRoot = guardedPath('roots.project_data', options);
  const projectRoot = guardedPath('mappings.manga_localizer.project_data_root', options);
  const realDataRoot = guardedPath('mappings.manga_localizer.real_data_root', options);
  const appDataRoot = guardedPath('mappings.manga_localizer.app_data_root', options);

  const rootInfo = requireRealDirectory(projectDataRoot, 'external project data root', {
    lstatPath,
    realpathPath,
  });
  if (projectRoot !== path.join(projectDataRoot, 'manga-localizer')) {
    throw new Error('manga-localizer project data root drifted from the registered topology');
  }
  if (realDataRoot !== path.join(projectRoot, 'real-data')) {
    throw new Error('manga-localizer real-data root drifted from the registered topology');
  }
  if (appDataRoot !== path.join(projectRoot, 'app-data')) {
    throw new Error('manga-localizer app-data root drifted from the registered topology');
  }
  for (const [candidate, label] of [
    [projectRoot, 'manga-localizer project data root'],
    [realDataRoot, 'manga-localizer real-data root'],
    [appDataRoot, 'manga-localizer app-data root'],
  ]) {
    requireRealDirectory(candidate, label, {
      lstatPath,
      realpathPath,
      expectedDevice: rootInfo.dev,
    });
  }

  rejectConflictingOverride(env, 'MANGA_LOCALIZER_DATA_DIR', appDataRoot);
  rejectConflictingOverride(env, 'MANGA_LOCALIZER_REAL_DATA_ROOT', realDataRoot);

  return {
    projectDataRoot,
    projectRoot,
    realDataRoot,
    appDataRoot,
    environment: {
      ...env,
      MANGA_LOCALIZER_DATA_DIR: appDataRoot,
      MANGA_LOCALIZER_REAL_DATA_ROOT: realDataRoot,
    },
  };
}
