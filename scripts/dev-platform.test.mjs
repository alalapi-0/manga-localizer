import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';

import { frontendLaunch, isLoopbackHost } from './dev-platform.mjs';

test('frontend launcher invokes Vite through Node without a platform shell', () => {
  const launch = frontendLaunch('C:\\work\\manga-localizer', '127.0.0.1', 5173, 'node.exe');

  assert.equal(launch.command, 'node.exe');
  assert.equal(path.basename(launch.args[0]), 'vite.js');
  assert.equal(launch.args[1], path.join('C:\\work\\manga-localizer', 'frontend'));
  assert.deepEqual(launch.args.slice(2), ['--host', '127.0.0.1', '--port', '5173']);
  assert.ok(!launch.args.includes('npm.cmd'));
});

test('loopback validation rejects externally reachable hosts', () => {
  assert.equal(isLoopbackHost('localhost'), true);
  assert.equal(isLoopbackHost('127.0.0.1'), true);
  assert.equal(isLoopbackHost('::1'), true);
  assert.equal(isLoopbackHost('0.0.0.0'), false);
  assert.equal(isLoopbackHost('192.168.1.20'), false);
});
