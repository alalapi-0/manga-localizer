import assert from 'node:assert/strict';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { APP_BUNDLE_NAME } from './app-platform.mjs';
import { parsePackageArgs, writeAppSkeleton } from './package-app.mjs';

test('package args default to dist/macos and do not download during tests', () => {
  const parsed = parsePackageArgs(['--dest', '/tmp/app-out', '--skip-download', '--no-install-user'], {
    home: '/tmp/home',
    platform: 'linux',
  });
  assert.equal(parsed.dest, path.resolve('/tmp/app-out'));
  assert.equal(parsed.skipDownload, true);
  assert.equal(parsed.installUser, false);
});

test('app skeleton writes a double-clickable wrapper without model URLs', () => {
  const dest = path.join(os.tmpdir(), `manga-app-skeleton-${process.pid}`);
  const layout = writeAppSkeleton(dest, { version: '0.2.0' });
  assert.equal(existsSync(layout.infoPlist), true);
  assert.equal(existsSync(layout.executable), true);
  const wrapper = readFileSync(layout.executable, 'utf8');
  assert.match(wrapper, /^#!/);
  assert.match(wrapper, /MODEL_BUNDLE/);
  assert.doesNotMatch(wrapper, /huggingface|argos-net/);
  assert.equal(path.basename(layout.app), APP_BUNDLE_NAME);
  mkdirSync(layout.frontend, { recursive: true });
  writeFileSync(path.join(layout.frontend, 'index.html'), '<!doctype html><title>Manga Localizer</title>');
  assert.equal(existsSync(path.join(layout.frontend, 'index.html')), true);
});
