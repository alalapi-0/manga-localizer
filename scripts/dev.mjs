import { existsSync } from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { spawn } from 'node:child_process';

import { backendUvicornArgs, frontendLaunch, isLoopbackHost } from './dev-platform.mjs';
import { resolveCanonicalUv } from './external-uv.mjs';
import { resolveGuardedProjectData } from './storage-data-route.mjs';
import { resolveGuardedModelBundle } from './storage-model-route.mjs';
import { resolveGuardedRuntimeEnvironment } from './storage-runtime-route.mjs';

const root = path.resolve(import.meta.dirname, '..');
const envFile = path.join(root, '.env');
if (existsSync(envFile)) process.loadEnvFile(envFile);
const dataEnvironment = resolveGuardedProjectData().environment;
dataEnvironment.MANGA_LOCALIZER_MODEL_BUNDLE = resolveGuardedModelBundle({
  env: dataEnvironment,
});
const runtimeEnvironment = resolveGuardedRuntimeEnvironment({ env: dataEnvironment }).environment;
const canonicalUv = resolveCanonicalUv();

const apiHost = (process.env.MANGA_LOCALIZER_HOST || '127.0.0.1').replace(/^\[|\]$/g, '');
const apiPort = process.env.MANGA_LOCALIZER_PORT || '8000';
const webHost = (process.env.MANGA_LOCALIZER_WEB_HOST || '127.0.0.1').replace(/^\[|\]$/g, '');
const webPort = process.env.MANGA_LOCALIZER_WEB_PORT || '5173';

if (!isLoopbackHost(apiHost) || !isLoopbackHost(webHost)) {
  throw new Error('Manga Localizer development services must bind to a loopback host');
}

const proxyHost = apiHost.includes(':') ? `[${apiHost}]` : apiHost;
const environment = {
  ...runtimeEnvironment,
  VITE_DEV_API_TARGET:
    process.env.VITE_DEV_API_TARGET || `http://${proxyHost}:${apiPort}`,
};
const frontend = frontendLaunch(root, webHost, webPort);

const processes = [
  spawn(
    canonicalUv,
    backendUvicornArgs({ host: apiHost, port: apiPort, reload: true }),
    { cwd: root, env: environment, stdio: 'inherit' },
  ),
  spawn(
    frontend.command,
    frontend.args,
    { cwd: root, env: environment, stdio: 'inherit' },
  ),
];

let shuttingDown = false;
function stop(exitCode = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of processes) {
    if (!child.killed) child.kill('SIGTERM');
  }
  setTimeout(() => process.exit(exitCode), 250).unref();
}

for (const child of processes) {
  child.on('error', (error) => {
    console.error(error.message);
    stop(1);
  });
  child.on('exit', (code) => {
    if (!shuttingDown) stop(code ?? 1);
  });
}

process.on('SIGINT', () => stop(0));
process.on('SIGTERM', () => stop(0));
