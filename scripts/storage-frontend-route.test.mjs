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
  allowsCiLocalFrontendRuntime,
  expectedFrontendMarker,
  installActiveFrontendRuntime,
  resolveActiveFrontendRuntime,
  resolveGuardedFrontendRuntime,
} from './storage-frontend-route.mjs';

function fixture(t) {
  const root = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'manga-frontend-route-')));
  const home = path.join(root, 'home');
  const guard = path.join(home, '.config', 'storage-governance', 'guard.sh');
  const projectRoot = path.join(root, 'project');
  const frontend = path.join(projectRoot, 'frontend');
  const runtimesRoot = path.join(root, 'Runtimes');
  const runtimeRoot = path.join(runtimesRoot, 'manga-localizer');
  const frontendRuntime = path.join(runtimeRoot, 'frontend-runtime');
  const nodeModules = path.join(frontendRuntime, 'node_modules');

  mkdirSync(path.dirname(guard), { recursive: true });
  writeFileSync(guard, '#!/bin/sh\nexit 1\n');
  chmodSync(guard, 0o755);
  mkdirSync(frontend, { recursive: true });
  writeFileSync(path.join(frontend, 'package.json'), '{"name":"fixture"}\n');
  writeFileSync(path.join(frontend, 'package-lock.json'), '{"lockfileVersion":3}\n');
  mkdirSync(path.join(nodeModules, '.bin'), { recursive: true });
  writeFileSync(path.join(nodeModules, '.bin', 'vite'), '#!/bin/sh\nexit 0\n');
  chmodSync(path.join(nodeModules, '.bin', 'vite'), 0o755);
  writeFileSync(
    path.join(runtimeRoot, '.storage-governance'),
    'storage-governance:manga-localizer:runtime:v1\n',
  );
  writeFileSync(
    path.join(frontendRuntime, '.manga-localizer-frontend-runtime'),
    expectedFrontendMarker(projectRoot),
  );
  symlinkSync(nodeModules, path.join(frontend, 'node_modules'), 'dir');

  const mappings = new Map([
    ['roots.runtimes', runtimesRoot],
    ['mappings.manga_localizer.runtime_root', runtimeRoot],
    ['mappings.manga_localizer.frontend_runtime', frontendRuntime],
    ['mappings.manga_localizer.frontend_node_modules', nodeModules],
  ]);
  const runGuard = (_command, args) => ({
    status: 0,
    stdout: `${mappings.get(args[1]) ?? ''}\n`,
    stderr: '',
  });
  t.after(() => rmSync(root, { recursive: true, force: true }));
  return {
    root,
    home,
    projectRoot,
    frontend,
    frontendRuntime,
    nodeModules,
    mappings,
    runGuard,
  };
}

test('frontend route derives the dependency tree from guarded external topology', (t) => {
  const data = fixture(t);
  const route = resolveGuardedFrontendRuntime({
    env: { HOME: data.home },
    runGuard: data.runGuard,
    projectRoot: data.projectRoot,
  });
  assert.equal(route.nodeModules, data.nodeModules);
  assert.equal(route.frontendRuntime, data.frontendRuntime);
  assert.equal(realpathSync(route.localNodeModules), data.nodeModules);
});

test('guard rejection fails closed before dependency routing', (t) => {
  const data = fixture(t);
  assert.throws(
    () => resolveGuardedFrontendRuntime({
      env: { HOME: data.home },
      runGuard: () => ({ status: 78, stdout: '', stderr: 'wrong volume UUID' }),
      projectRoot: data.projectRoot,
    }),
    /guard rejected frontend routing: wrong volume UUID/,
  );
});

test('mapping escape, stale marker, and local-link bypass are rejected', (t) => {
  const data = fixture(t);
  data.mappings.set(
    'mappings.manga_localizer.frontend_node_modules',
    path.join(data.root, 'internal-node-modules'),
  );
  assert.throws(
    () => resolveGuardedFrontendRuntime({
      env: { HOME: data.home },
      runGuard: data.runGuard,
      projectRoot: data.projectRoot,
    }),
    /frontend node_modules drifted/,
  );

  data.mappings.set('mappings.manga_localizer.frontend_node_modules', data.nodeModules);
  writeFileSync(
    path.join(data.frontendRuntime, '.manga-localizer-frontend-runtime'),
    'stale\n',
  );
  assert.throws(
    () => resolveGuardedFrontendRuntime({
      env: { HOME: data.home },
      runGuard: data.runGuard,
      projectRoot: data.projectRoot,
    }),
    /does not match the current package lock/,
  );

  writeFileSync(
    path.join(data.frontendRuntime, '.manga-localizer-frontend-runtime'),
    expectedFrontendMarker(data.projectRoot),
  );
  unlinkSync(path.join(data.frontend, 'node_modules'));
  mkdirSync(path.join(data.frontend, 'node_modules'));
  assert.throws(
    () => resolveGuardedFrontendRuntime({
      env: { HOME: data.home },
      runGuard: data.runGuard,
      projectRoot: data.projectRoot,
    }),
    /not the registered external link/,
  );
});

test('only an exact CI opt-in may use a real local dependency tree', (t) => {
  const data = fixture(t);
  unlinkSync(path.join(data.frontend, 'node_modules'));
  const localNodeModules = path.join(data.frontend, 'node_modules');
  mkdirSync(path.join(localNodeModules, '.bin'), { recursive: true });
  writeFileSync(path.join(localNodeModules, '.bin', 'vite'), '#!/bin/sh\nexit 0\n');
  chmodSync(path.join(localNodeModules, '.bin', 'vite'), 0o755);
  const route = resolveActiveFrontendRuntime({
    env: {
      HOME: path.join(data.root, 'other-home'),
      CI: 'true',
      MANGA_LOCALIZER_CI_LOCAL_RUNTIME: '1',
    },
    projectRoot: data.projectRoot,
  });
  assert.equal(route.local, true);
  assert.equal(route.nodeModules, localNodeModules);
});

test('missing guard fails closed unless both CI values match exactly', (t) => {
  const data = fixture(t);
  const missingHome = path.join(data.root, 'other-home');
  for (const env of [
    { HOME: missingHome },
    { HOME: missingHome, CI: 'true' },
    { HOME: missingHome, MANGA_LOCALIZER_CI_LOCAL_RUNTIME: '1' },
    { HOME: missingHome, CI: '1', MANGA_LOCALIZER_CI_LOCAL_RUNTIME: '1' },
    { HOME: missingHome, CI: 'true', MANGA_LOCALIZER_CI_LOCAL_RUNTIME: 'true' },
  ]) {
    assert.equal(allowsCiLocalFrontendRuntime(env), false);
    assert.throws(
      () => resolveActiveFrontendRuntime({ env, projectRoot: data.projectRoot }),
      /guard is missing/,
    );
  }
});

test('CI local installation is explicit, deterministic, and never an implicit fallback', (t) => {
  const data = fixture(t);
  const missingHome = path.join(data.root, 'other-home');
  const calls = [];
  const runNpm = (...args) => {
    calls.push(args);
    return { status: 0 };
  };
  assert.throws(
    () => installActiveFrontendRuntime({
      env: { HOME: missingHome },
      projectRoot: data.projectRoot,
      runNpm,
    }),
    /guard is missing/,
  );
  assert.equal(calls.length, 0);

  const env = {
    HOME: missingHome,
    CI: 'true',
    MANGA_LOCALIZER_CI_LOCAL_RUNTIME: '1',
  };
  assert.equal(installActiveFrontendRuntime({ env, projectRoot: data.projectRoot, runNpm }), 0);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], process.platform === 'win32' ? 'npm.cmd' : 'npm');
  assert.deepEqual(calls[0][1], ['ci']);
  assert.equal(calls[0][2].cwd, data.frontend);
  assert.equal(calls[0][2].env, env);
});
