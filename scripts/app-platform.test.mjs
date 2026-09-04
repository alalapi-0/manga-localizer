import assert from 'node:assert/strict';
import test from 'node:test';

import {
  APP_BUNDLE_NAME,
  applicationBindHost,
  appBundleLayout,
  bundledRuntimeEnvironment,
  desktopWindowLaunch,
  firstPrivateLanIPv4,
  infoPlistXml,
  isPrivateLanIPv4,
  macosWrapperScript,
} from './app-platform.mjs';

test('bundled window helper is preferred over Chromium', () => {
  const launch = desktopWindowLaunch('http://127.0.0.1:8000', {
    platform: 'darwin',
    windowHelper: '/tmp/WorkbenchWindow',
    pathExists: (candidate) => candidate === '/tmp/WorkbenchWindow' || candidate.includes('Google Chrome.app'),
  });

  assert.equal(launch.kind, 'app-window');
  assert.equal(launch.command, '/tmp/WorkbenchWindow');
  assert.deepEqual(launch.args, ['http://127.0.0.1:8000']);
  assert.equal(launch.tracksWindowLifetime, true);
});

test('macOS app bundle layout and wrapper stay loopback by default', () => {
  const layout = appBundleLayout('/tmp/macos-dist');
  assert.equal(layout.app.endsWith(APP_BUNDLE_NAME), true);
  assert.equal(layout.manifest.endsWith('models/manifest.json'), true);

  const plist = infoPlistXml({ version: '0.2.0' });
  assert.match(plist, /local.manga-localizer/);
  assert.match(plist, /CFBundleExecutable/);
  assert.match(plist, /Manga Localizer/);

  const wrapper = macosWrapperScript();
  assert.match(wrapper, /MANGA_LOCALIZER_MODEL_BUNDLE/);
  assert.match(wrapper, /MANGA_LOCALIZER_FRONTEND_DIST/);
  assert.match(wrapper, /STORAGE_GOVERNANCE_GUARD/);
  assert.match(wrapper, /mappings\.manga_localizer\.app_data_root/);
  assert.match(wrapper, /conflicts with the registered app-data root/);
  assert.doesNotMatch(wrapper, /\.manga-localizer|export MANGA_LOCALIZER_DATA_DIR="\$\{/);
  assert.match(wrapper, /macos_app_launcher.py/);
  assert.doesNotMatch(wrapper, /huggingface|argos-net|setup_optional_models/);

  const env = bundledRuntimeEnvironment(layout.resources);
  assert.equal(env.MANGA_LOCALIZER_LAN_ACCESS, '0');
  assert.equal(env.MANGA_LOCALIZER_HOST, '127.0.0.1');
  assert.equal(env.MANGA_LOCALIZER_MODEL_BUNDLE, layout.models);
  assert.equal(env.MANGA_LOCALIZER_FRONTEND_DIST, layout.frontend);
});

test('macOS prefers an installed Chromium app window', () => {
  const launch = desktopWindowLaunch('http://127.0.0.1:8000', {
    platform: 'darwin',
    pathExists: (candidate) => candidate.includes('Google Chrome.app'),
  });

  assert.equal(launch.kind, 'app-window');
  assert.equal(launch.command, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
  assert.deepEqual(launch.args, ['--app=http://127.0.0.1:8000']);
  assert.equal(launch.tracksWindowLifetime, false);
});

test('macOS falls back to opening the loopback workbench URL', () => {
  const launch = desktopWindowLaunch('http://127.0.0.1:8000', {
    platform: 'darwin',
    pathExists: () => false,
  });

  assert.equal(launch.kind, 'browser-tab');
  assert.equal(launch.command, 'open');
  assert.deepEqual(launch.args, ['http://127.0.0.1:8000']);
  assert.equal(launch.tracksWindowLifetime, false);
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
