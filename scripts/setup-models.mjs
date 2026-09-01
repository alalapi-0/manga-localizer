import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import process from 'node:process';

import { resolveCanonicalUv } from './external-uv.mjs';
import { resolveGuardedModelBundle } from './storage-model-route.mjs';
import { resolveGuardedRuntimeEnvironment } from './storage-runtime-route.mjs';

const root = path.resolve(import.meta.dirname, '..');
const envFile = path.join(root, '.env');
if (existsSync(envFile)) process.loadEnvFile(envFile);

const forwarded = process.argv.slice(2);
if (forwarded.some((argument) => (
  argument === '--bundle-dest'
  || argument.startsWith('--bundle-dest=')
  || argument === '--data-dir'
  || argument.startsWith('--data-dir=')
))) {
  throw new Error('setup model destination is fixed by the verified external models root');
}

const bundle = resolveGuardedModelBundle({ requireReady: false });
const runtimeEnvironment = resolveGuardedRuntimeEnvironment().environment;
const canonicalUv = resolveCanonicalUv();
const result = spawnSync(
  canonicalUv,
  [
    'run',
    '--project',
    'backend',
    'python',
    'scripts/setup_optional_models.py',
    '--bundle-dest',
    bundle,
    ...forwarded,
  ],
  { cwd: root, env: runtimeEnvironment, stdio: 'inherit' },
);
if (result.error) throw result.error;
process.exit(result.status ?? 1);
