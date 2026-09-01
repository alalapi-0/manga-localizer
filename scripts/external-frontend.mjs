#!/usr/bin/env node

import process from 'node:process';

import {
  installActiveFrontendRuntime,
  resolveActiveFrontendRuntime,
  writeGuardedFrontendMarker,
} from './storage-frontend-route.mjs';

export function runExternalFrontend(argv, { stdout = process.stdout } = {}) {
  if (argv.length !== 1) {
    throw new Error('external-frontend accepts exactly one action');
  }
  if (argv[0] === '--check') {
    const route = resolveActiveFrontendRuntime();
    stdout.write(`manga-localizer-frontend: OK ${route.nodeModules}\n`);
    return 0;
  }
  if (argv[0] === '--write-marker') {
    const route = writeGuardedFrontendMarker();
    stdout.write(`manga-localizer-frontend-marker: OK ${route.frontendRuntime}\n`);
    return 0;
  }
  if (argv[0] === 'install') {
    return installActiveFrontendRuntime();
  }
  throw new Error('external-frontend accepts only --check, --write-marker, or install');
}

if (import.meta.filename === process.argv[1]) {
  try {
    process.exitCode = runExternalFrontend(process.argv.slice(2));
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 78;
  }
}
