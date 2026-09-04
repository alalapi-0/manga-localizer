import assert from 'node:assert/strict';
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { APP_BUNDLE_NAME, appBundleLayout } from './app-platform.mjs';
import {
  bundleRuntimeEnvironment,
  installUserThinApp,
  packageModelArguments,
  parsePackageArgs,
  publishPackagedApp,
  resolveGuardedPackageStorage,
  resolveUserMaintenanceInstaller,
  writeAppSkeleton,
} from './package-app.mjs';

function storageFixture(t) {
  const fixtureRoot = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'manga-package-route-')));
  const home = path.join(fixtureRoot, 'home');
  const guard = path.join(home, '.config', 'storage-governance', 'guard.sh');
  const artifactsRoot = path.join(fixtureRoot, 'Artifacts');
  const cachesRoot = path.join(fixtureRoot, 'Caches');
  const packageDest = path.join(artifactsRoot, 'manga-localizer', 'macos');
  const uvCache = path.join(cachesRoot, 'uv');
  mkdirSync(path.dirname(guard), { recursive: true });
  writeFileSync(guard, '#!/bin/sh\nexit 1\n');
  chmodSync(guard, 0o755);
  mkdirSync(packageDest, { recursive: true });
  mkdirSync(uvCache, { recursive: true });
  const mappings = new Map([
    ['roots.artifacts', artifactsRoot],
    ['roots.caches', cachesRoot],
    ['mappings.manga_localizer.package_dest', packageDest],
    ['mappings.manga_localizer.uv_cache', uvCache],
  ]);
  const runGuard = (command, args, options) => {
    assert.equal(command, guard);
    assert.equal(options.env.HOME, home);
    return { status: 0, stdout: `${mappings.get(args[1]) ?? ''}\n`, stderr: '' };
  };
  t.after(() => rmSync(fixtureRoot, { recursive: true, force: true }));
  return { home, artifactsRoot, cachesRoot, packageDest, uvCache, mappings, runGuard };
}

test('package args have no internal default and user installation is explicit', () => {
  const defaults = parsePackageArgs([], { home: '/tmp/home' });
  assert.equal(defaults.dest, undefined);
  assert.equal(defaults.installUser, false);

  const parsed = parsePackageArgs(['--dest', '/tmp/app-out', '--skip-download', '--no-install-user'], {
    home: '/tmp/home',
  });
  assert.equal(parsed.dest, path.resolve('/tmp/app-out'));
  assert.equal(parsed.skipDownload, true);
  assert.equal(parsed.installUser, false);
  assert.equal(
    parsePackageArgs(['--install-user'], { home: '/tmp/home' }).installUser,
    true,
  );
  assert.throws(() => parsePackageArgs(['--dest', '--skip-download']), /requires/);
  assert.throws(
    () => parsePackageArgs(['--install-user', '--no-install-user']),
    /mutually exclusive/,
  );
});

test('user installation delegates only to the canonical governed thin-app installer', (t) => {
  const data = storageFixture(t);
  const installer = path.join(
    data.home,
    '.local',
    'libexec',
    'storage-governance',
    'manga-localizer-install',
  );
  const calls = [];
  assert.throws(
    () => installUserThinApp({
      home: data.home,
      env: { HOME: data.home, ROUTE_TEST: '1' },
      runInstaller: (...args) => {
        calls.push(args);
        return { status: 0 };
      },
    }),
    /installer is missing/,
  );
  assert.equal(calls.length, 0);

  mkdirSync(path.dirname(installer), { recursive: true });
  writeFileSync(installer, '#!/bin/sh\nexit 0\n');
  chmodSync(installer, 0o755);
  assert.equal(resolveUserMaintenanceInstaller({ home: data.home }), installer);
  assert.equal(installUserThinApp({
    home: data.home,
    env: { HOME: data.home, ROUTE_TEST: '1' },
    runInstaller: (...args) => {
      calls.push(args);
      return { status: 0 };
    },
  }), installer);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], installer);
  assert.deepEqual(calls[0][1], ['--install']);
  assert.equal(calls[0][2].cwd, path.resolve(import.meta.dirname, '..'));
  assert.equal(calls[0][2].env.HOME, data.home);
  assert.equal(calls[0][2].env.ROUTE_TEST, '1');
  assert.equal(existsSync(path.join(data.home, 'Applications', APP_BUNDLE_NAME)), false);

  rmSync(installer);
  const outside = path.join(data.home, 'outside-installer');
  writeFileSync(outside, '#!/bin/sh\nexit 0\n');
  chmodSync(outside, 0o755);
  symlinkSync(outside, installer);
  assert.throws(
    () => resolveUserMaintenanceInstaller({ home: data.home }),
    /not a canonical regular file/,
  );
});

test('a failed governed thin-app install is never reported as success', (t) => {
  const data = storageFixture(t);
  const installer = path.join(
    data.home,
    '.local',
    'libexec',
    'storage-governance',
    'manga-localizer-install',
  );
  mkdirSync(path.dirname(installer), { recursive: true });
  writeFileSync(installer, '#!/bin/sh\nexit 0\n');
  chmodSync(installer, 0o755);
  assert.throws(
    () => installUserThinApp({
      home: data.home,
      runInstaller: () => ({ status: 78 }),
    }),
    /failed with status 78/,
  );
});

test('packaged app publication atomically replaces a validated external artifact', (t) => {
  const destinationRoot = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'manga-package-publish-')));
  t.after(() => rmSync(destinationRoot, { recursive: true, force: true }));
  const stagingRoot = path.join(destinationRoot, '.manga-localizer-package-stage');
  const previousApp = path.join(destinationRoot, `.${APP_BUNDLE_NAME}.package-previous`);
  const stagedLayout = appBundleLayout(stagingRoot);
  const finalLayout = appBundleLayout(destinationRoot);
  mkdirSync(stagedLayout.app, { recursive: true });
  mkdirSync(finalLayout.app, { recursive: true });
  writeFileSync(path.join(stagedLayout.app, 'candidate-marker'), 'new\n');
  writeFileSync(path.join(finalLayout.app, 'candidate-marker'), 'old\n');
  const validateApp = (layout) => {
    assert.match(readFileSync(path.join(layout.app, 'candidate-marker'), 'utf8'), /^(?:new|old)\n$/);
    return layout;
  };

  assert.equal(publishPackagedApp({
    stagedLayout,
    finalLayout,
    stagingRoot,
    previousApp,
    validateApp,
  }), finalLayout);
  assert.equal(readFileSync(path.join(finalLayout.app, 'candidate-marker'), 'utf8'), 'new\n');
  assert.equal(existsSync(stagingRoot), false);
  assert.equal(existsSync(previousApp), false);
});

test('packaged app publication restores the exact prior artifact on validation failure', (t) => {
  const destinationRoot = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'manga-package-rollback-')));
  t.after(() => rmSync(destinationRoot, { recursive: true, force: true }));
  const stagingRoot = path.join(destinationRoot, '.manga-localizer-package-stage');
  const previousApp = path.join(destinationRoot, `.${APP_BUNDLE_NAME}.package-previous`);
  const stagedLayout = appBundleLayout(stagingRoot);
  const finalLayout = appBundleLayout(destinationRoot);
  mkdirSync(stagedLayout.app, { recursive: true });
  mkdirSync(finalLayout.app, { recursive: true });
  writeFileSync(path.join(stagedLayout.app, 'candidate-marker'), 'new\n');
  writeFileSync(path.join(finalLayout.app, 'candidate-marker'), 'old\n');
  const validateApp = (layout) => {
    const marker = readFileSync(path.join(layout.app, 'candidate-marker'), 'utf8');
    if (layout.app === finalLayout.app && marker === 'new\n') {
      throw new Error('injected final validation failure');
    }
    return layout;
  };

  assert.throws(
    () => publishPackagedApp({
      stagedLayout,
      finalLayout,
      stagingRoot,
      previousApp,
      validateApp,
    }),
    /injected final validation failure/,
  );
  assert.equal(readFileSync(path.join(finalLayout.app, 'candidate-marker'), 'utf8'), 'old\n');
  assert.equal(readFileSync(path.join(stagedLayout.app, 'candidate-marker'), 'utf8'), 'new\n');
  assert.equal(existsSync(previousApp), false);
});

test('package destination and uv cache come from one guarded external topology', (t) => {
  const data = storageFixture(t);
  const route = resolveGuardedPackageStorage({
    env: { HOME: data.home },
    runGuard: data.runGuard,
  });
  assert.equal(route.packageDest, data.packageDest);
  assert.equal(route.uvCache, data.uvCache);
  assert.equal(
    resolveGuardedPackageStorage({
      requestedDest: data.packageDest,
      env: { HOME: data.home },
      runGuard: data.runGuard,
    }).packageDest,
    data.packageDest,
  );
  assert.throws(
    () => resolveGuardedPackageStorage({
      requestedDest: '/tmp/internal-package',
      env: { HOME: data.home },
      runGuard: data.runGuard,
    }),
    /must match the guarded external mapping/,
  );
});

test('package routing fails closed on guard rejection or mapping drift', (t) => {
  const data = storageFixture(t);
  assert.throws(
    () => resolveGuardedPackageStorage({
      env: { HOME: data.home },
      runGuard: () => ({ status: 78, stdout: '', stderr: 'wrong volume UUID' }),
    }),
    /guard rejected package routing: wrong volume UUID/,
  );
  data.mappings.set(
    'mappings.manga_localizer.package_dest',
    path.join(data.artifactsRoot, 'other-project'),
  );
  assert.throws(
    () => resolveGuardedPackageStorage({
      env: { HOME: data.home },
      runGuard: data.runGuard,
    }),
    /destination drifted/,
  );
});

test('bundle-local uv environment stays inside the guarded external app', (t) => {
  const data = storageFixture(t);
  const layout = appBundleLayout(data.packageDest);
  const environment = bundleRuntimeEnvironment(layout, {
    packageDest: data.packageDest,
    uvCache: data.uvCache,
    env: { HOME: data.home, UV_PROJECT_ENVIRONMENT: '/tmp/internal-fallback' },
  });
  assert.equal(environment.UV_PROJECT_ENVIRONMENT, path.join(layout.backend, '.venv'));
  assert.equal(environment.UV_CACHE_DIR, data.uvCache);
  assert.equal(environment.PYTHONNOUSERSITE, '1');
  const stagingRoot = path.join(data.packageDest, '.manga-localizer-package-stage');
  const stagedLayout = appBundleLayout(stagingRoot);
  assert.equal(
    bundleRuntimeEnvironment(stagedLayout, {
      packageDest: data.packageDest,
      bundleRoot: stagingRoot,
      uvCache: data.uvCache,
    }).UV_PROJECT_ENVIRONMENT,
    path.join(stagedLayout.backend, '.venv'),
  );
  assert.throws(
    () => bundleRuntimeEnvironment(
      { ...layout, backend: '/tmp/internal-backend' },
      { packageDest: data.packageDest, uvCache: data.uvCache },
    ),
    /escaped the guarded package destination/,
  );
});

test('package models copy from the guarded external bundle without an internal fallback', () => {
  const args = packageModelArguments({
    bundleDest: '/test-external/Artifacts/manga-localizer/app/Models',
    modelSource: '/test-external/Models/manga-localizer/model-bundle',
    skipDownload: true,
  });
  assert.deepEqual(
    args.slice(args.indexOf('--copy-from-models-dir'), args.indexOf('--copy-from-models-dir') + 2),
    [
      '--copy-from-models-dir',
      '/test-external/Models/manga-localizer/model-bundle',
    ],
  );
  assert.equal(args.includes('--copy-from'), false);
  assert.equal(args.includes('--no-download'), true);
  assert.equal(args.some((argument) => argument.includes('.manga-localizer')), false);
});

test('app skeleton writes a double-clickable wrapper without model URLs', (t) => {
  const dest = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'manga-app-skeleton-')));
  t.after(() => rmSync(dest, { recursive: true, force: true }));
  const layout = writeAppSkeleton(dest, { version: '0.2.0' });
  assert.equal(existsSync(layout.infoPlist), true);
  assert.equal(existsSync(layout.executable), true);
  const wrapper = readFileSync(layout.executable, 'utf8');
  assert.match(wrapper, /^#!/);
  assert.match(wrapper, /MODEL_BUNDLE/);
  assert.match(wrapper, /mappings\.manga_localizer\.app_data_root/);
  assert.doesNotMatch(wrapper, /\.manga-localizer|export MANGA_LOCALIZER_DATA_DIR="\$\{/);
  assert.doesNotMatch(wrapper, /huggingface|argos-net/);
  assert.equal(path.basename(layout.app), APP_BUNDLE_NAME);
  mkdirSync(layout.frontend, { recursive: true });
  writeFileSync(path.join(layout.frontend, 'index.html'), '<!doctype html><title>Manga Localizer</title>');
  assert.equal(existsSync(path.join(layout.frontend, 'index.html')), true);
});

test('clean source includes every fail-closed storage route and the backend sentinel', () => {
  const sourceRoot = path.resolve(import.meta.dirname, '..');
  for (const relative of [
    'scripts/external-frontend.mjs',
    'scripts/external-uv.mjs',
    'scripts/setup-models.mjs',
    'scripts/storage-data-route.mjs',
    'scripts/storage-data-route.test.mjs',
    'scripts/storage-frontend-route.mjs',
    'scripts/storage-frontend-route.test.mjs',
    'scripts/storage-model-route.mjs',
    'scripts/storage-model-route.test.mjs',
    'scripts/storage-runtime-route.mjs',
    'scripts/storage-runtime-route.test.mjs',
  ]) {
    assert.equal(existsSync(path.join(sourceRoot, relative)), true, `${relative} is missing`);
  }
  const sentinel = path.join(sourceRoot, 'backend', '.venv');
  const sentinelInfo = lstatSync(sentinel);
  assert.equal(sentinelInfo.isSymbolicLink(), false);
  assert.equal(sentinelInfo.isFile(), true);
  assert.equal(
    readFileSync(sentinel, 'utf8'),
    [
      'MANGA_LOCALIZER_EXTERNAL_RUNTIME_REQUIRED',
      'Use `node scripts/external-uv.mjs --check` and the guarded package scripts.',
      'This regular file intentionally prevents uv from creating an internal project environment.',
      '',
    ].join('\n'),
  );
  assert.doesNotMatch(
    readFileSync(path.join(sourceRoot, 'scripts', 'external-uv.mjs'), 'utf8'),
    /\/Users\/[^/]+\/\.local\/bin\/uv/,
  );
  assert.doesNotMatch(
    readFileSync(path.join(sourceRoot, 'scripts', 'package-app.mjs'), 'utf8'),
    /copyTree\(layout\.app,\s*installed\)/,
  );
  assert.doesNotMatch(
    readFileSync(path.join(sourceRoot, 'scripts', 'macos_app_launcher.py'), 'utf8'),
    /Path\.home\(\)\s*\/\s*["']\.manga-localizer["']/,
  );
});
