import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: true,
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'mobile-chromium',
      use: { ...devices['Pixel 7'], defaultBrowserType: 'chromium', channel: process.env.PLAYWRIGHT_CHANNEL },
    },
    {
      name: 'desktop-chromium',
      use: {
        viewport: { width: 1280, height: 900 },
        browserName: 'chromium',
        channel: process.env.PLAYWRIGHT_CHANNEL,
      },
    },
    { name: 'mobile-webkit', use: { ...devices['iPhone 13'], browserName: 'webkit', channel: undefined } },
  ],
  webServer: [
    {
      command: 'npm run preview',
      env: { GYM_E2E_API: 'http://127.0.0.1:4174' },
      url: 'http://127.0.0.1:4173',
      reuseExistingServer: false,
    },
    {
      command: `${process.env.GYM_TEST_PYTHON ?? 'python'} tests/api-server.py`,
      url: 'http://127.0.0.1:4174/__test/health',
      reuseExistingServer: false,
    },
  ],
});
