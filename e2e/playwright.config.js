// @ts-check
const path = require('node:path');
const { defineConfig, devices } = require('@playwright/test');

const PORT = process.env.E2E_PORT || '8765';
const BASE_URL = `http://127.0.0.1:${PORT}`;
const ROOT = path.resolve(__dirname, '..');
// Screenshots are opt-in: set E2E_SCREENSHOT_DIR=<dir> to also write full-page PNGs at both sizes.
const SHOTS = !!process.env.E2E_SCREENSHOT_DIR;
// A project-level testIgnore replaces the top-level one, so the shared list is spelled out for each project.
// evidence.spec.js captures the before/after shots of a pull request and runs only when E2E_EVIDENCE_DIR is set.
const IGNORE = [/auth\.setup\.js$/, ...(SHOTS ? [] : [/screenshots\.spec\.js$/]),
  ...(process.env.E2E_EVIDENCE_DIR ? [] : [/evidence\.spec\.js$/])];
const AUTH = path.join(__dirname, '.auth', 'admin.json');

module.exports = defineConfig({
  testDir: './tests',
  globalSetup: require.resolve('./global-setup.js'),
  // The node, its database and the seed data are shared by every test, so tests run in one worker.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: { baseURL: BASE_URL, trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  webServer: {
    command: process.env.E2E_SERVER_COMMAND || 'uv run --frozen python e2e/server.py',
    cwd: ROOT,
    url: `${BASE_URL}/api/v2/health`,
    reuseExistingServer: false,
    timeout: 120_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
  projects: [
    { name: 'setup', testMatch: /auth\.setup\.js$/ },
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, storageState: AUTH },
      dependencies: ['setup'],
      testIgnore: IGNORE,
    },
    {
      name: 'mobile',
      // Chromium with a phone-sized viewport and touch; no WebKit/Firefox so one pinned browser is enough.
      use: {
        ...devices['Pixel 5'], viewport: { width: 390, height: 844 }, deviceScaleFactor: 1,
        defaultBrowserType: 'chromium', storageState: AUTH,
      },
      dependencies: ['setup'],
      testIgnore: IGNORE,
    },
  ],
  outputDir: './test-results',
});
