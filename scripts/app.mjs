import { existsSync } from 'node:fs';
import { spawn, spawnSync } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';

import { desktopWindowLaunch } from './app-platform.mjs';
import { isLoopbackHost } from './dev-platform.mjs';

const root = path.resolve(import.meta.dirname, '..');
const envFile = path.join(root, '.env');
if (existsSync(envFile)) process.loadEnvFile(envFile);

const apiHost = (process.env.MANGA_LOCALIZER_HOST || '127.0.0.1').replace(/^\[|\]$/g, '');
const apiPort = process.env.MANGA_LOCALIZER_PORT || '8000';
const frontendDist = path.join(root, 'frontend', 'dist');

if (!isLoopbackHost(apiHost)) {
  throw new Error('Manga Localizer application services must bind to a loopback host');
}

const proxyHost = apiHost.includes(':') ? `[${apiHost}]` : apiHost;
const appUrl = `http://${proxyHost}:${apiPort}`;
const environment = {
  ...process.env,
  MANGA_LOCALIZER_FRONTEND_DIST: process.env.MANGA_LOCALIZER_FRONTEND_DIST || frontendDist,
};

if (!existsSync(path.join(environment.MANGA_LOCALIZER_FRONTEND_DIST, 'index.html'))) {
  const build = spawnSync('npm', ['--prefix', 'frontend', 'run', 'build'], {
    cwd: root,
    env: environment,
    stdio: 'inherit',
  });
  if (build.status !== 0) {
    process.exit(build.status ?? 1);
  }
}

const api = spawn(
  'uv',
  [
    'run',
    '--project',
    'backend',
    'uvicorn',
    'manga_localizer.main:app',
    '--host',
    apiHost,
    '--port',
    apiPort,
  ],
  { cwd: root, env: environment, stdio: 'inherit' },
);

let windowProcess;
let shuttingDown = false;

function stop(exitCode = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of [windowProcess, api]) {
    if (child && !child.killed) child.kill('SIGTERM');
  }
  setTimeout(() => process.exit(exitCode), 250).unref();
}

async function waitForHealth() {
  const deadline = Date.now() + 30_000;
  let lastError = '';
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${appUrl}/api/health`);
      if (response.ok) return;
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Manga Localizer API did not become ready: ${lastError}`);
}

api.on('error', (error) => {
  console.error(error.message);
  stop(1);
});
api.on('exit', (code) => {
  if (!shuttingDown) stop(code ?? 1);
});

process.on('SIGINT', () => stop(0));
process.on('SIGTERM', () => stop(0));

try {
  await waitForHealth();
} catch (error) {
  console.error(error instanceof Error ? error.message : error);
  stop(1);
}

const windowLaunch = desktopWindowLaunch(appUrl);
windowProcess = spawn(windowLaunch.command, windowLaunch.args, {
  cwd: root,
  env: environment,
  stdio: 'ignore',
});
windowProcess.on('error', (error) => {
  console.error(error.message);
  stop(1);
});
windowProcess.on('exit', (code) => {
  if (!shuttingDown) stop(code ?? 0);
});

console.log(
  windowLaunch.kind === 'app-window'
    ? `Opened Manga Localizer as a Mac application window at ${appUrl}`
    : `Opened Manga Localizer at ${appUrl}. Install Chrome, Edge, or Chromium for a dedicated app window.`,
);
