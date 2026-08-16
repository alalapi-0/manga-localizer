import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applicationBindHost,
  desktopWindowLaunch,
  firstPrivateLanIPv4,
  isPrivateLanIPv4,
} from './app-platform.mjs';

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

test('LAN companion binds a private IPv4 only when explicitly enabled', () => {
  assert.equal(isPrivateLanIPv4('192.168.1.20'), true);
  assert.equal(isPrivateLanIPv4('10.0.0.4'), true);
  assert.equal(isPrivateLanIPv4('172.16.0.8'), true);
  assert.equal(isPrivateLanIPv4('127.0.0.1'), false);
  assert.equal(isPrivateLanIPv4('0.0.0.0'), false);
  assert.equal(isPrivateLanIPv4('8.8.8.8'), false);

  assert.equal(applicationBindHost({ lanAccess: false, requestedHost: '127.0.0.1' }), '127.0.0.1');
  assert.equal(
    applicationBindHost({
      lanAccess: true,
      requestedHost: '127.0.0.1',
      lanAddress: '192.168.1.20',
    }),
    '192.168.1.20',
  );
  assert.equal(
    applicationBindHost({
      lanAccess: true,
      requestedHost: '10.0.0.4',
      lanAddress: '192.168.1.20',
    }),
    '10.0.0.4',
  );
  assert.throws(
    () => applicationBindHost({ lanAccess: false, requestedHost: '192.168.1.20' }),
    /loopback host/,
  );
  assert.throws(
    () => applicationBindHost({ lanAccess: true, requestedHost: '127.0.0.1', lanAddress: null }),
    /No private LAN IPv4/,
  );
  assert.throws(
    () => applicationBindHost({ lanAccess: true, requestedHost: '0.0.0.0', lanAddress: null }),
    /No private LAN IPv4/,
  );

  assert.equal(
    firstPrivateLanIPv4({
      en0: [
        { family: 'IPv6', address: 'fe80::1', internal: false },
        { family: 'IPv4', address: '127.0.0.1', internal: true },
        { family: 'IPv4', address: '192.168.1.20', internal: false },
      ],
    }),
    '192.168.1.20',
  );
  assert.equal(
    firstPrivateLanIPv4({
      lo0: [{ family: 'IPv4', address: '127.0.0.1', internal: true }],
    }),
    null,
  );
});
