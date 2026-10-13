import { defineConfig, devices } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({
  ...base,
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], hasTouch: true } },
    { name: 'webkit', use: { ...devices['Desktop Safari'], hasTouch: true } },
  ],
});
