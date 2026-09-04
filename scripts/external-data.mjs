#!/usr/bin/env node

import process from 'node:process';

import { resolveGuardedProjectData } from './storage-data-route.mjs';

export function runExternalData(argv, { stdout = process.stdout } = {}) {
  if (argv.length !== 1) {
    throw new Error('external-data accepts exactly one action');
  }
  const route = resolveGuardedProjectData();
  if (argv[0] === '--check') {
    stdout.write(`manga-localizer-project-data: OK ${route.projectRoot}\n`);
    return 0;
  }
  if (argv[0] === '--print-real-data') {
    stdout.write(`${route.realDataRoot}\n`);
    return 0;
  }
  if (argv[0] === '--print-app-data') {
    stdout.write(`${route.appDataRoot}\n`);
    return 0;
  }
  throw new Error(
    'external-data accepts only --check, --print-real-data, or --print-app-data',
  );
}

if (import.meta.filename === process.argv[1]) {
  try {
    process.exitCode = runExternalData(process.argv.slice(2));
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 78;
  }
}
