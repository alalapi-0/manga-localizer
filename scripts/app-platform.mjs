import { existsSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import process from 'node:process';

import { isLoopbackHost } from './dev-platform.mjs';

export const APP_BUNDLE_NAME = 'Manga Localizer.app';
export const APP_BUNDLE_ID = 'local.manga-localizer';
export const BUNDLE_MODEL_SELECTION = ['ppocr', 'lama', 'realesrgan', 'argos-ja-zh'];

const DARWIN_APP_BROWSERS = [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
];

export function isPrivateLanIPv4(host) {
  const parts = String(host).trim().split('.').map((part) => Number(part));
  const invalid = parts.some((part) => !Number.isInteger(part) || part < 0 || part > 255);
  if (parts.length !== 4 || invalid) {
    return false;
  }
  const [first, second] = parts;
  if (first === 10) return true;
  if (first === 192 && second === 168) return true;
  if (first === 172 && second >= 16 && second <= 31) return true;
  return false;
}

export function firstPrivateLanIPv4(interfaces = os.networkInterfaces()) {
  for (const addresses of Object.values(interfaces)) {
    for (const entry of addresses ?? []) {
      const ipv4 = entry.family === 'IPv4' || entry.family === 4;
      if (ipv4 && !entry.internal && isPrivateLanIPv4(entry.address)) {
        return entry.address;
      }
    }
  }
  return null;
}

export function applicationBindHost({
  lanAccess = false,
  requestedHost = '127.0.0.1',
  lanAddress = null,
} = {}) {
  const host = String(requestedHost || '127.0.0.1').replace(/^\[|\]$/g, '');
  if (!lanAccess) {
    if (!isLoopbackHost(host)) {
      throw new Error('Manga Localizer application services must bind to a loopback host');
    }
    return host;
  }
  if (isPrivateLanIPv4(host)) return host;
  if (lanAddress && isPrivateLanIPv4(lanAddress)) return lanAddress;
  throw new Error('No private LAN IPv4 address is available for phone companion access');
}

export function appBundleLayout(root) {
  const app = path.join(root, APP_BUNDLE_NAME);
  const contents = path.join(app, 'Contents');
  const macos = path.join(contents, 'MacOS');
  const resources = path.join(contents, 'Resources');
  return {
    app,
    contents,
    macos,
    resources,
    frontend: path.join(resources, 'frontend'),
    models: path.join(resources, 'models'),
    manifest: path.join(resources, 'models', 'manifest.json'),
    backend: path.join(resources, 'backend'),
    launcher: path.join(resources, 'macos_app_launcher.py'),
    windowHelper: path.join(macos, 'WorkbenchWindow'),
    executable: path.join(macos, 'Manga Localizer'),
    infoPlist: path.join(contents, 'Info.plist'),
  };
}

export function infoPlistXml({
  version = '0.2.0',
  executable = 'Manga Localizer',
  bundleId = APP_BUNDLE_ID,
} = {}) {
  return `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>en</string>
  <key>CFBundleDisplayName</key>
  <string>Manga Localizer</string>
  <key>CFBundleExecutable</key>
  <string>${executable}</string>
  <key>CFBundleIdentifier</key>
  <string>${bundleId}</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>Manga Localizer</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>${version}</string>
  <key>CFBundleVersion</key>
  <string>${version}</string>
  <key>LSMinimumSystemVersion</key>
  <string>13.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsLocalNetworking</key>
    <true/>
  </dict>
</dict>
</plist>
`;
}

export function macosWrapperScript() {
  return `#!/bin/bash
set -euo pipefail
CONTENTS="$(cd "$(dirname "$0")/.." && pwd)"
RESOURCES="$CONTENTS/Resources"
export MANGA_LOCALIZER_RESOURCES="$RESOURCES"
export MANGA_LOCALIZER_FRONTEND_DIST="\${MANGA_LOCALIZER_FRONTEND_DIST:-$RESOURCES/frontend}"
export MANGA_LOCALIZER_MODEL_BUNDLE="\${MANGA_LOCALIZER_MODEL_BUNDLE:-$RESOURCES/models}"
export MANGA_LOCALIZER_DATA_DIR="\${MANGA_LOCALIZER_DATA_DIR:-$HOME/.manga-localizer}"
export MANGA_LOCALIZER_WINDOW_HELPER="\${MANGA_LOCALIZER_WINDOW_HELPER:-$CONTENTS/MacOS/WorkbenchWindow}"
PYTHON="$RESOURCES/backend/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "Manga Localizer is missing its bundled Python runtime." >&2
  exit 1
fi
exec "$PYTHON" "$RESOURCES/macos_app_launcher.py" "$@"
`;
}

export function bundledRuntimeEnvironment(resources, {
  lanAccess = false,
  host = '127.0.0.1',
  port = '8000',
} = {}) {
  return {
    MANGA_LOCALIZER_FRONTEND_DIST: path.join(resources, 'frontend'),
    MANGA_LOCALIZER_MODEL_BUNDLE: path.join(resources, 'models'),
    MANGA_LOCALIZER_HOST: host,
    MANGA_LOCALIZER_PORT: String(port),
    MANGA_LOCALIZER_LAN_ACCESS: lanAccess ? '1' : '0',
  };
}

export function desktopWindowLaunch(
  url,
  {
    platform = process.platform,
    pathExists = existsSync,
    windowHelper = null,
  } = {},
) {
  if (windowHelper && pathExists(windowHelper)) {
    return { command: windowHelper, args: [url], kind: 'app-window' };
  }
  if (platform === 'darwin') {
    const command = DARWIN_APP_BROWSERS.find((candidate) => pathExists(candidate));
    if (command) {
      return { command, args: [`--app=${url}`], kind: 'app-window' };
    }
    return { command: 'open', args: [url], kind: 'browser-tab' };
  }
  if (platform === 'win32') {
    return { command: 'cmd', args: ['/c', 'start', '', url], kind: 'browser-tab' };
  }
  return { command: 'xdg-open', args: [url], kind: 'browser-tab' };
}
