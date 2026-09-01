import assert from 'node:assert/strict';
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { resolveGuardedModelBundle } from './storage-model-route.mjs';

function fixture(t) {
  const root = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'manga-model-route-')));
  const home = path.join(root, 'home');
  const guard = path.join(home, '.config', 'storage-governance', 'guard.sh');
  const modelsRoot = path.join(root, 'Models');
  const bundle = path.join(modelsRoot, 'manga-localizer', 'model-bundle');
  mkdirSync(path.dirname(guard), { recursive: true });
  writeFileSync(guard, '#!/bin/sh\nexit 1\n');
  chmodSync(guard, 0o755);
  mkdirSync(bundle, { recursive: true });
  writeFileSync(path.join(bundle, 'manifest.json'), '{"schemaVersion":1,"models":[]}\n');
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return { root, home, guard, modelsRoot, bundle };
}

function passingGuard(modelsRoot, expectedHome = '/tmp/home') {
  return (command, args, options) => {
    assert.equal(command, path.join(expectedHome, '.config', 'storage-governance', 'guard.sh'));
    assert.deepEqual(args, ['--get-path', 'roots.models']);
    assert.equal(options.env.HOME, expectedHome);
    return { status: 0, stdout: `${modelsRoot}\n`, stderr: '' };
  };
}

test('runtime model bundle is derived from the guarded external models root', (t) => {
  const { home, modelsRoot, bundle } = fixture(t);
  assert.equal(
    resolveGuardedModelBundle({
      env: { HOME: home },
      runGuard: passingGuard(modelsRoot, home),
    }),
    bundle,
  );
});

test('setup may create only a descendant of the guarded models root', (t) => {
  const { home, modelsRoot } = fixture(t);
  const future = path.join(modelsRoot, 'custom', 'future-bundle');
  assert.equal(
    resolveGuardedModelBundle({
      env: { HOME: home, MANGA_LOCALIZER_MODEL_BUNDLE: future },
      runGuard: passingGuard(modelsRoot, home),
      requireReady: false,
    }),
    future,
  );
  assert.throws(
    () => resolveGuardedModelBundle({
      env: {
        HOME: home,
        MANGA_LOCALIZER_MODEL_BUNDLE: path.join(path.dirname(modelsRoot), 'internal-fallback'),
      },
      runGuard: passingGuard(modelsRoot, home),
      requireReady: false,
    }),
    /must be a child of the verified external models root/,
  );
});

test('model route fails closed when the identity guard rejects the volume', (t) => {
  const home = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'manga-model-guard-')));
  t.after(() => rmSync(home, { recursive: true, force: true }));
  const guard = path.join(home, '.config', 'storage-governance', 'guard.sh');
  mkdirSync(path.dirname(guard), { recursive: true });
  writeFileSync(guard, '#!/bin/sh\nexit 1\n');
  chmodSync(guard, 0o755);
  assert.throws(
    () => resolveGuardedModelBundle({
      env: { HOME: home },
      runGuard: () => ({ status: 78, stdout: '', stderr: 'wrong volume UUID' }),
    }),
    /guard rejected model bundle: wrong volume UUID/,
  );
});

test('runtime rejects missing manifests, guard symlinks, and model symlink components', (t) => {
  const { root, home, guard, modelsRoot, bundle } = fixture(t);
  rmSync(path.join(bundle, 'manifest.json'));
  assert.throws(
    () => resolveGuardedModelBundle({
      env: { HOME: home },
      runGuard: passingGuard(modelsRoot, home),
    }),
    /manifest is missing/,
  );

  const outsideGuard = path.join(root, 'outside-guard');
  writeFileSync(outsideGuard, '#!/bin/sh\nexit 0\n');
  chmodSync(outsideGuard, 0o755);
  rmSync(guard);
  symlinkSync(outsideGuard, guard);
  assert.throws(
    () => resolveGuardedModelBundle({
      env: { HOME: home },
      runGuard: passingGuard(modelsRoot, home),
    }),
    /not a canonical regular file/,
  );
  rmSync(guard);
  writeFileSync(guard, '#!/bin/sh\nexit 1\n');
  chmodSync(guard, 0o755);

  const outside = path.join(root, 'outside');
  mkdirSync(outside);
  writeFileSync(path.join(outside, 'manifest.json'), '{}\n');
  const symlinkBundle = path.join(modelsRoot, 'linked-bundle');
  symlinkSync(outside, symlinkBundle);
  assert.throws(
    () => resolveGuardedModelBundle({
      env: { HOME: home, MANGA_LOCALIZER_MODEL_BUNDLE: symlinkBundle },
      runGuard: passingGuard(modelsRoot, home),
    }),
    /must not contain symbolic links/,
  );
});
