import path from 'node:path';

import { defineConfig, devices } from '@playwright/test';

const workspaceRoot = process.cwd();
const backendPort = 18100;
const frontendPort = 15173;
const backendUrl = `http://127.0.0.1:${backendPort}`;
const frontendUrl = `http://127.0.0.1:${frontendPort}`;
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
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
    video: 'retain-on-failure',
  },
  webServer: [
    {
      command: `uv run --project backend uvicorn manga_localizer.main:app --host 127.0.0.1 --port ${backendPort}`,
      cwd: workspaceRoot,
      env: {
        ...inheritedEnvironment,
        MANGA_LOCALIZER_DATA_DIR: path.join(
          workspaceRoot,
          'tests/e2e/.generated/runtime',
        ),
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
