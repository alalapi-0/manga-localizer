import path from 'node:path';
import process from 'node:process';

import { resolveActiveFrontendRuntime } from './storage-frontend-route.mjs';

export function isLoopbackHost(host) {
  const normalized = host.trim().replace(/^\[|\]$/g, '').replace(/\.$/, '').toLowerCase();
  return normalized === 'localhost'
    || normalized.endsWith('.localhost')
    || normalized === '::1'
    || normalized === '0:0:0:0:0:0:0:1'
    || /^127(?:\.[0-9]{1,3}){3}$/.test(normalized);
}

export function frontendLaunch(
  root,
  host,
  port,
  nodeExecutable = process.execPath,
  resolveFrontend = resolveActiveFrontendRuntime,
) {
  const route = resolveFrontend({ projectRoot: root });
  return {
    command: nodeExecutable,
    args: [
      path.join(route.nodeModules, 'vite', 'bin', 'vite.js'),
      path.join(root, 'frontend'),
      '--host',
      host,
      '--port',
      String(port),
    ],
  };
}
