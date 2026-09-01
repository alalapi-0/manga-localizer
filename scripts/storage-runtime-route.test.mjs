import assert from 'node:assert/strict';
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  symlinkSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import {
  modelRoutedEnvironment,
  normalizeUvArguments,
  resolveCanonicalUv,
} from './external-uv.mjs';
import {
  expectedRuntimeMarker,
  resolveGuardedRuntimeEnvironment,
} from './storage-runtime-route.mjs';

function fixture(t) {
  const root = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'manga-runtime-route-')));
  const home = path.join(root, 'home');
  const guard = path.join(home, '.config', 'storage-governance', 'guard.sh');
  const projectRoot = path.join(root, 'project');
  const runtimesRoot = path.join(root, 'Runtimes');
  const cachesRoot = path.join(root, 'Caches');
  const runtimeRoot = path.join(runtimesRoot, 'manga-localizer');
  const runtimeVenv = path.join(runtimeRoot, 'backend-venv');
  const uvCache = path.join(cachesRoot, 'uv');

  mkdirSync(path.dirname(guard), { recursive: true });
  writeFileSync(guard, '#!/bin/sh\nexit 1\n');
  chmodSync(guard, 0o755);
  mkdirSync(path.join(projectRoot, 'backend'), { recursive: true });
  writeFileSync(path.join(projectRoot, 'backend', 'uv.lock'), 'fixture-lock\n');
  mkdirSync(path.join(runtimeVenv, 'bin'), { recursive: true });
  mkdirSync(uvCache, { recursive: true });
  writeFileSync(path.join(runtimeRoot, '.storage-governance'), 'storage-governance:manga-localizer:runtime:v1\n');
  writeFileSync(path.join(runtimeVenv, 'pyvenv.cfg'), 'version_info = 3.12.13\n');
  writeFileSync(path.join(runtimeVenv, 'bin', 'python'), '#!/bin/sh\nexit 0\n');
  chmodSync(path.join(runtimeVenv, 'bin', 'python'), 0o755);
  writeFileSync(path.join(runtimeVenv, '.manga-localizer-runtime'), expectedRuntimeMarker(projectRoot));

  const mappings = new Map([
    ['roots.runtimes', runtimesRoot],
    ['roots.caches', cachesRoot],
    ['mappings.manga_localizer.runtime_root', runtimeRoot],
    ['mappings.manga_localizer.runtime_venv', runtimeVenv],
    ['mappings.manga_localizer.uv_cache', uvCache],
  ]);
  const runGuard = (_command, args) => ({
    status: 0,
    stdout: `${mappings.get(args[1]) ?? ''}\n`,
    stderr: '',
  });
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return { root, home, guard, projectRoot, runtimeRoot, runtimeVenv, uvCache, mappings, runGuard };
}

test('runtime route is derived from guarded roots and overrides caller destinations', (t) => {
  const data = fixture(t);
  const result = resolveGuardedRuntimeEnvironment({
    env: {
      HOME: data.home,
      UV_PROJECT_ENVIRONMENT: '/tmp/internal-fallback',
      UV_CACHE_DIR: '/tmp/internal-cache',
    },
    runGuard: data.runGuard,
    projectRoot: data.projectRoot,
  });
  assert.equal(result.runtimeVenv, data.runtimeVenv);
  assert.equal(result.uvCache, data.uvCache);
  assert.equal(result.environment.UV_PROJECT_ENVIRONMENT, data.runtimeVenv);
  assert.equal(result.environment.UV_CACHE_DIR, data.uvCache);
});

test('guard rejection fails closed before returning an environment', (t) => {
  const data = fixture(t);
  assert.throws(
    () => resolveGuardedRuntimeEnvironment({
      env: { HOME: data.home },
      runGuard: () => ({ status: 78, stdout: '', stderr: 'wrong volume UUID' }),
      projectRoot: data.projectRoot,
    }),
    /guard rejected runtime routing: wrong volume UUID/,
  );
});

test('mapping escape, symlink topology, and stale lock marker are rejected', (t) => {
  const data = fixture(t);
  data.mappings.set('mappings.manga_localizer.runtime_venv', path.join(data.root, 'internal'));
  assert.throws(
    () => resolveGuardedRuntimeEnvironment({
      env: { HOME: data.home },
      runGuard: data.runGuard,
      projectRoot: data.projectRoot,
    }),
    /runtime venv drifted/,
  );

  data.mappings.set(
    'mappings.manga_localizer.runtime_venv',
    path.join(data.runtimeRoot, 'backend-venv'),
  );
  rmSync(data.runtimeVenv, { recursive: true });
  const outside = path.join(data.root, 'outside');
  mkdirSync(outside);
  symlinkSync(outside, data.runtimeVenv);
  assert.throws(
    () => resolveGuardedRuntimeEnvironment({
      env: { HOME: data.home },
      runGuard: data.runGuard,
      projectRoot: data.projectRoot,
    }),
    /must not contain symbolic links/,
  );

  rmSync(data.runtimeVenv);
  mkdirSync(path.join(data.runtimeVenv, 'bin'), { recursive: true });
  writeFileSync(path.join(data.runtimeVenv, 'pyvenv.cfg'), 'version_info = 3.12.13\n');
  writeFileSync(path.join(data.runtimeVenv, 'bin', 'python'), '#!/bin/sh\nexit 0\n');
  chmodSync(path.join(data.runtimeVenv, 'bin', 'python'), 0o755);
  writeFileSync(path.join(data.runtimeVenv, '.manga-localizer-runtime'), 'stale\n');
  assert.throws(
    () => resolveGuardedRuntimeEnvironment({
      env: { HOME: data.home },
      runGuard: data.runGuard,
      projectRoot: data.projectRoot,
    }),
    /does not match the current uv.lock/,
  );
});

test('external uv parser pins the backend and rejects routing overrides', () => {
  assert.deepEqual(normalizeUvArguments(['run', 'pytest', 'backend/tests']), {
    action: 'run',
    withGuardedModels: false,
    uvArgs: ['run', '--project', 'backend', 'pytest', 'backend/tests'],
  });
  assert.deepEqual(
    normalizeUvArguments(['run', '--with-guarded-models', 'python', 'scripts/compare_inpaint.py']),
    {
      action: 'run',
      withGuardedModels: true,
      uvArgs: ['run', '--project', 'backend', 'python', 'scripts/compare_inpaint.py'],
    },
  );
  assert.equal(
    normalizeUvArguments(['sync', '--project', 'backend', '--group', 'dev']).uvArgs[2],
    path.join(path.resolve(import.meta.dirname, '..'), 'backend'),
  );
  assert.throws(() => normalizeUvArguments(['run', '--project', '../other', 'pytest']), /registered/);
  assert.throws(() => normalizeUvArguments(['run', '--directory', '/tmp', 'pytest']), /may not override/);
  assert.throws(() => normalizeUvArguments(['pip', 'install', 'example']), /accepts only/);
  assert.throws(
    () => normalizeUvArguments(['sync', '--with-guarded-models']),
    /available only for run/,
  );
});

test('external uv binary is derived from a canonical HOME without a username literal', (t) => {
  const data = fixture(t);
  const uv = path.join(data.home, '.local', 'bin', 'uv');
  mkdirSync(path.dirname(uv), { recursive: true });
  writeFileSync(uv, '#!/bin/sh\nexit 0\n');
  chmodSync(uv, 0o755);
  assert.equal(resolveCanonicalUv({ env: { HOME: data.home } }), uv);
  assert.throws(
    () => resolveCanonicalUv({ env: { HOME: 'relative-home' } }),
    /absolute canonical path/,
  );

  const outside = path.join(data.root, 'outside-uv');
  writeFileSync(outside, '#!/bin/sh\nexit 0\n');
  chmodSync(outside, 0o755);
  unlinkSync(uv);
  symlinkSync(outside, uv);
  assert.throws(
    () => resolveCanonicalUv({ env: { HOME: data.home } }),
    /symbolic link/,
  );
});

test('external uv run environment injects only the guarded model bundle', () => {
  const environment = modelRoutedEnvironment({
    env: {
      HOME: '/tmp/home',
      MANGA_LOCALIZER_MODEL_BUNDLE: '/tmp/internal-override',
    },
    resolveModelBundle: ({ env }) => {
      assert.equal(env.HOME, '/tmp/home');
      return '/test-external/Models/manga-localizer/model-bundle';
    },
  });
  assert.equal(
    environment.MANGA_LOCALIZER_MODEL_BUNDLE,
    '/test-external/Models/manga-localizer/model-bundle',
  );
});
