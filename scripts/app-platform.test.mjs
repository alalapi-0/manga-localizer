import assert from 'node:assert/strict';
import test from 'node:test';

import { desktopWindowLaunch } from './app-platform.mjs';

test('macOS prefers an installed Chromium app window', () => {
  const launch = desktopWindowLaunch('http://127.0.0.1:8000', {
    platform: 'darwin',
    pathExists: (candidate) => candidate.includes('Google Chrome.app'),
  });

  assert.equal(launch.kind, 'app-window');
  assert.equal(launch.command, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
  assert.deepEqual(launch.args, ['--app=http://127.0.0.1:8000']);
});

test('macOS falls back to opening the loopback workbench URL', () => {
  const launch = desktopWindowLaunch('http://127.0.0.1:8000', {
    platform: 'darwin',
    pathExists: () => false,
  });

  assert.equal(launch.kind, 'browser-tab');
  assert.equal(launch.command, 'open');
  assert.deepEqual(launch.args, ['http://127.0.0.1:8000']);
});
