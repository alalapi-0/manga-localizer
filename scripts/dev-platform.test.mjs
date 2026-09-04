import assert from 'node:assert/strict';
import path from 'node:path';
import test from 'node:test';

import { backendUvicornArgs, frontendLaunch, isLoopbackHost } from './dev-platform.mjs';

test('backend launcher uses python -m uvicorn so zsh wrapper shebangs cannot nest', () => {
  assert.deepEqual(
    backendUvicornArgs({ host: '127.0.0.1', port: 8000, reload: true }),
    [
      'run',
      '--project',
      'backend',
      '--frozen',
      '--offline',
      '--no-sync',
      'python',
      '-m',
      'uvicorn',
      'manga_localizer.main:app',
      '--reload',
      '--reload-dir',
      'backend',
      '--host',
      '127.0.0.1',
      '--port',
      '8000',
    ],
  );
  const packaged = backendUvicornArgs({ host: '127.0.0.1', port: '8000' });
  assert.deepEqual(packaged.slice(packaged.indexOf('python'), packaged.indexOf('python') + 3), [
    'python',
    '-m',
    'uvicorn',
  ]);
  assert.equal(packaged[packaged.indexOf('python') - 1], '--no-sync');
  assert.ok(!packaged.includes('--reload'));
  assert.ok(!packaged.includes('--reload-dir'));
  assert.throws(() => backendUvicornArgs({ host: '', port: 8000 }), /host and port/);
});

test('frontend launcher invokes Vite through Node without a platform shell', () => {
  const root = 'C:\\work\\manga-localizer';
  const nodeModules = path.join(root, 'external', 'node_modules');
  const launch = frontendLaunch(
    root,
    '127.0.0.1',
    5173,
    'node.exe',
    () => ({ nodeModules }),
  );

  assert.equal(launch.command, 'node.exe');
  assert.equal(path.basename(launch.args[0]), 'vite.js');
  assert.equal(launch.args[0], path.join(nodeModules, 'vite', 'bin', 'vite.js'));
  assert.equal(launch.args[1], path.join(root, 'frontend'));
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
