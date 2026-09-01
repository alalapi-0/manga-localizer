import { existsSync } from 'node:fs';
import path from 'node:path';

import { defineConfig, devices } from '@playwright/test';

const workspaceRoot = process.cwd();
const backendPort = 18100;
const frontendPort = 15173;
const backendUrl = `http://127.0.0.1:${backendPort}`;
const frontendUrl = `http://127.0.0.1:${frontendPort}`;
const ciLocalRuntime = process.env.CI === 'true'
  && process.env.MANGA_LOCALIZER_CI_LOCAL_RUNTIME === '1';
const backendCommand = ciLocalRuntime
  ? `uv run --project backend --frozen --offline --no-sync uvicorn manga_localizer.main:app --host 127.0.0.1 --port ${backendPort}`
  : `node scripts/external-uv.mjs run --frozen --offline --no-sync uvicorn manga_localizer.main:app --host 127.0.0.1 --port ${backendPort}`;
const browserExecutable = process.env.MANGA_LOCALIZER_E2E_BROWSER_EXECUTABLE?.trim() || null;
if (browserExecutable && (!path.isAbsolute(browserExecutable) || !existsSync(browserExecutable))) {
  throw new Error('MANGA_LOCALIZER_E2E_BROWSER_EXECUTABLE must be an existing absolute path');
}
const backendDataDir = path.resolve(
  process.env.MANGA_LOCALIZER_E2E_DATA_DIR
    || path.join(workspaceRoot, 'tests/e2e/.generated/runtime'),
);
const inheritedEnvironment = Object.fromEntries(
  Object.entries(process.env).filter((entry): entry is [string, string] => entry[1] !== undefined),
);

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [
    ['line'],
    ['html', { open: 'never', outputFolder: 'playwright-report' }],
  ],
  use: {
    ...devices['Desktop Chrome'],
    baseURL: frontendUrl,
    locale: 'zh-CN',
    viewport: { width: 1440, height: 900 },
    launchOptions: browserExecutable ? { executablePath: browserExecutable } : undefined,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
  },
  webServer: [
    {
      command: backendCommand,
      cwd: workspaceRoot,
      env: {
        ...inheritedEnvironment,
        MANGA_LOCALIZER_DATA_DIR: backendDataDir,
      },
      reuseExistingServer: false,
      timeout: 120_000,
      url: `${backendUrl}/api/health`,
    },
    {
      command: `npm --prefix frontend run dev -- --host 127.0.0.1 --port ${frontendPort}`,
      cwd: workspaceRoot,
      env: {
        ...inheritedEnvironment,
        VITE_DEV_API_TARGET: backendUrl,
      },
      reuseExistingServer: false,
      timeout: 120_000,
      url: frontendUrl,
    },
  ],
});
