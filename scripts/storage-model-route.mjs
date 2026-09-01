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

function strictChild(root, candidate, label) {
  const relative = path.relative(root, candidate);
  if (
    !relative
    || relative === '..'
    || relative.startsWith(`..${path.sep}`)
    || path.isAbsolute(relative)
  ) {
    throw new Error(`${label} must be a child of the verified external models root`);
  }
  return relative;
}

function rejectSymlinkOrDeviceEscape(root, candidate, { pathExists, lstatPath }) {
  const rootInfo = lstatPath(root);
  if (rootInfo.isSymbolicLink() || !rootInfo.isDirectory()) {
    throw new Error('storage governance models root is not a real directory');
  }

  const relative = strictChild(root, candidate, 'model bundle');
  let cursor = root;
  for (const part of relative.split(path.sep)) {
    cursor = path.join(cursor, part);
    if (!pathExists(cursor)) break;
    const info = lstatPath(cursor);
    if (info.isSymbolicLink()) {
      throw new Error('model bundle path must not contain symbolic links');
    }
    if (info.dev !== rootInfo.dev) {
      throw new Error('model bundle path escaped the verified external filesystem');
    }
    if (!info.isDirectory()) {
      throw new Error('model bundle path contains a non-directory component');
    }
  }
  return rootInfo;
}

export function resolveGuardedModelBundle({
  env = process.env,
  runGuard = spawnSync,
  requireReady = true,
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
  if (!pathExists(guard)) {
    throw new Error('storage governance guard is missing');
  }
  const guardInfo = lstatPath(guard);
  if (guardInfo.isSymbolicLink() || !guardInfo.isFile() || realpathPath(guard) !== guard) {
    throw new Error('storage governance guard is not a canonical regular file');
  }
  accessPath(guard, constants.X_OK);

  const result = runGuard(guard, ['--get-path', 'roots.models'], {
    encoding: 'utf8',
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  if (result.error || result.status !== 0) {
    const detail = String(result.stderr ?? '').trim();
    throw new Error(
      `storage governance guard rejected model bundle${detail ? `: ${detail}` : ''}`,
    );
  }

  const rawModelsRoot = String(result.stdout ?? '').trim();
  if (!path.isAbsolute(rawModelsRoot)) {
    throw new Error('storage governance models root is missing or not absolute');
  }
  const modelsRoot = path.resolve(rawModelsRoot);
  if (!pathExists(modelsRoot)) {
    throw new Error('verified external models root is missing');
  }

  const configured = env.MANGA_LOCALIZER_MODEL_BUNDLE?.trim();
  if (configured && !path.isAbsolute(configured)) {
    throw new Error('configured model bundle must be absolute');
  }
  const bundle = path.resolve(
    configured || path.join(modelsRoot, 'manga-localizer', 'model-bundle'),
  );
  const rootInfo = rejectSymlinkOrDeviceEscape(modelsRoot, bundle, {
    pathExists,
    lstatPath,
  });

  if (requireReady) {
    if (!pathExists(bundle)) {
      throw new Error('guarded external model bundle is missing');
    }
    const manifest = path.join(bundle, 'manifest.json');
    if (!pathExists(manifest)) {
      throw new Error('guarded external model bundle manifest is missing');
    }
    const manifestInfo = lstatPath(manifest);
    if (manifestInfo.isSymbolicLink() || !manifestInfo.isFile()) {
      throw new Error('guarded external model bundle manifest is not a real file');
    }
    if (manifestInfo.dev !== rootInfo.dev) {
      throw new Error('model bundle manifest escaped the verified external filesystem');
    }
  }

  return bundle;
}
