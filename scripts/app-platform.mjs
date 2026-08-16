import { existsSync } from 'node:fs';
import os from 'node:os';
import process from 'node:process';

import { isLoopbackHost } from './dev-platform.mjs';

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

export function desktopWindowLaunch(
  url,
  {
    platform = process.platform,
    pathExists = existsSync,
  } = {},
) {
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
