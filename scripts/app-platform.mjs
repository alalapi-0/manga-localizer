import { existsSync } from 'node:fs';
import process from 'node:process';

const DARWIN_APP_BROWSERS = [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
];

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
