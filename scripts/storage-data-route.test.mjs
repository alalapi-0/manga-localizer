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

import { resolveGuardedProjectData } from './storage-data-route.mjs';

function fixture() {
  const root = realpathSync(mkdtempSync(path.join(os.tmpdir(), 'manga-data-route-')));
  const home = path.join(root, 'home');
  const guard = path.join(home, '.config', 'storage-governance', 'guard.sh');
  const projectDataRoot = path.join(root, 'ProjectData');
  const projectRoot = path.join(projectDataRoot, 'manga-localizer');
  const realDataRoot = path.join(projectRoot, 'real-data');
  const appDataRoot = path.join(projectRoot, 'app-data');
  mkdirSync(path.dirname(guard), { recursive: true });
  writeFileSync(guard, '#!/bin/sh\nexit 0\n');
  chmodSync(guard, 0o755);
  mkdirSync(realDataRoot, { recursive: true });
  mkdirSync(appDataRoot, { recursive: true });
  const mappings = {
    'roots.project_data': projectDataRoot,
    'mappings.manga_localizer.project_data_root': projectRoot,
    'mappings.manga_localizer.real_data_root': realDataRoot,
    'mappings.manga_localizer.app_data_root': appDataRoot,
  };
  const runGuard = (_command, args) => ({
    status: Object.hasOwn(mappings, args[1]) ? 0 : 78,
    stdout: Object.hasOwn(mappings, args[1]) ? `${mappings[args[1]]}\n` : '',
    stderr: '',
  });
  return {
    root,
    home,
    guard,
    projectDataRoot,
    projectRoot,
    realDataRoot,
    appDataRoot,
    mappings,
    runGuard,
  };
}

test('resolves the exact guarded project data topology', () => {
  const data = fixture();
  try {
    const route = resolveGuardedProjectData({
      env: { HOME: data.home },
      runGuard: data.runGuard,
    });
    assert.equal(route.projectRoot, data.projectRoot);
    assert.equal(route.realDataRoot, data.realDataRoot);
    assert.equal(route.appDataRoot, data.appDataRoot);
    assert.equal(route.environment.MANGA_LOCALIZER_DATA_DIR, data.appDataRoot);
    assert.equal(route.environment.MANGA_LOCALIZER_REAL_DATA_ROOT, data.realDataRoot);
  } finally {
    rmSync(data.root, { recursive: true, force: true });
  }
});

test('rejects an internal or conflicting app-data override', () => {
  const data = fixture();
  try {
    assert.throws(
      () => resolveGuardedProjectData({
        env: { HOME: data.home, MANGA_LOCALIZER_DATA_DIR: path.join(data.home, 'local') },
        runGuard: data.runGuard,
      }),
      /conflicts with the registered project data route/,
    );
  } finally {
    rmSync(data.root, { recursive: true, force: true });
  }
});

test('rejects mapping drift outside the registered topology', () => {
  const data = fixture();
  try {
    data.mappings['mappings.manga_localizer.real_data_root'] = path.join(
      data.projectDataRoot,
      'other-real-data',
    );
    assert.throws(
      () => resolveGuardedProjectData({
        env: { HOME: data.home },
        runGuard: data.runGuard,
      }),
      /real-data root drifted/,
    );
  } finally {
    rmSync(data.root, { recursive: true, force: true });
  }
});

test('rejects a symlinked project data component', () => {
  const data = fixture();
  try {
    rmSync(data.realDataRoot, { recursive: true });
    const elsewhere = path.join(data.root, 'elsewhere');
    mkdirSync(elsewhere);
    symlinkSync(elsewhere, data.realDataRoot);
    assert.throws(
      () => resolveGuardedProjectData({
        env: { HOME: data.home },
        runGuard: data.runGuard,
      }),
      /real-data root is not a real directory/,
    );
  } finally {
    rmSync(data.root, { recursive: true, force: true });
  }
});
