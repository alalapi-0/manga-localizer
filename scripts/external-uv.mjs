#!/usr/bin/env node

import { accessSync, constants, lstatSync, realpathSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';

import { resolveGuardedModelBundle } from './storage-model-route.mjs';
import {
  resolveGuardedRuntimeEnvironment,
  writeGuardedRuntimeMarker,
} from './storage-runtime-route.mjs';

const projectRoot = path.resolve(import.meta.dirname, '..');
const backendRoot = path.join(projectRoot, 'backend');

function projectArgumentMatches(value) {
  return path.resolve(projectRoot, value) === backendRoot;
}

export function normalizeUvArguments(argv) {
  if (argv.length === 1 && argv[0] === '--check') {
    return { action: 'check', uvArgs: [] };
  }
  const [action, ...input] = argv;
  if (action !== 'sync' && action !== 'run') {
    throw new Error('external-uv accepts only --check, sync, or run');
  }

  const forwarded = [];
  let sawProject = false;
  let withGuardedModels = false;
  for (let index = 0; index < input.length; index += 1) {
    const argument = input[index];
    if (argument === '--with-guarded-models') {
      if (action !== 'run') {
        throw new Error('--with-guarded-models is available only for run');
      }
      withGuardedModels = true;
      continue;
    }
    if (argument === '--directory' || argument === '--config-file' || argument === '--no-config') {
      throw new Error(`${argument} may not override the registered project route`);
    }
    if (argument.startsWith('--directory=') || argument.startsWith('--config-file=')) {
      throw new Error(`${argument.split('=')[0]} may not override the registered project route`);
    }
    if (argument === '--project') {
      const value = input[index + 1];
      if (!value || !projectArgumentMatches(value)) {
        throw new Error('uv project must be the registered manga-localizer backend');
      }
      sawProject = true;
      index += 1;
      continue;
    }
    if (argument.startsWith('--project=')) {
      const value = argument.slice('--project='.length);
      if (!projectArgumentMatches(value)) {
        throw new Error('uv project must be the registered manga-localizer backend');
      }
      sawProject = true;
      continue;
    }
    forwarded.push(argument);
  }

  return {
    action,
    withGuardedModels,
    uvArgs: [action, '--project', sawProject ? backendRoot : 'backend', ...forwarded],
  };
}

export function resolveCanonicalUv({
  env = process.env,
  lstatPath = lstatSync,
  realpathPath = realpathSync,
  accessPath = accessSync,
} = {}) {
  const home = env.HOME?.trim();
  if (!home || !path.isAbsolute(home) || path.resolve(home) !== home) {
    throw new Error('HOME must be an absolute canonical path');
  }
  if (realpathPath(home) !== home) {
    throw new Error('HOME must resolve to itself');
  }
  const canonicalUv = path.join(home, '.local', 'bin', 'uv');
  const info = lstatPath(canonicalUv);
  if (info.isSymbolicLink() || !info.isFile() || realpathPath(canonicalUv) !== canonicalUv) {
    throw new Error('registered uv binary is missing, non-canonical, or a symbolic link');
  }
  accessPath(canonicalUv, constants.X_OK);
  return canonicalUv;
}

export function modelRoutedEnvironment({
  env = process.env,
  resolveModelBundle = resolveGuardedModelBundle,
} = {}) {
  return {
    ...env,
    MANGA_LOCALIZER_MODEL_BUNDLE: resolveModelBundle({ env }),
  };
}

export function runExternalUv(argv, {
  env = process.env,
  runUv = spawnSync,
  resolveUv = resolveCanonicalUv,
  stdout = process.stdout,
} = {}) {
  const parsed = normalizeUvArguments(argv);
  const canonicalUv = resolveUv({ env });

  if (parsed.action === 'check') {
    const routedEnv = modelRoutedEnvironment({ env });
    const route = resolveGuardedRuntimeEnvironment({ env: routedEnv, requireReady: true });
    stdout.write(`manga-localizer-runtime: OK ${route.runtimeVenv}\n`);
    stdout.write(`manga-localizer-models: OK ${routedEnv.MANGA_LOCALIZER_MODEL_BUNDLE}\n`);
    return 0;
  }

  const syncDryRun = parsed.action === 'sync' && parsed.uvArgs.includes('--dry-run');
  const routedEnv = parsed.withGuardedModels ? modelRoutedEnvironment({ env }) : env;
  const route = resolveGuardedRuntimeEnvironment({
    env: routedEnv,
    requireReady: parsed.action !== 'sync',
  });
  const result = runUv(canonicalUv, parsed.uvArgs, {
    cwd: projectRoot,
    env: route.environment,
    stdio: 'inherit',
  });
  if (result.error) throw result.error;
  const status = result.status ?? 1;
  if (status !== 0) return status;

  if (parsed.action === 'sync' && !syncDryRun) {
    writeGuardedRuntimeMarker({ env });
  } else {
    resolveGuardedRuntimeEnvironment({
      env: routedEnv,
      requireReady: parsed.action !== 'sync',
    });
  }
  return 0;
}

if (import.meta.filename === path.resolve(process.argv[1] ?? '')) {
  try {
    process.exitCode = runExternalUv(process.argv.slice(2));
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 78;
  }
}
