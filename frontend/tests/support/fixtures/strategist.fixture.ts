import { expect } from '@playwright/test';
import { test as base } from './app.fixture';
import {
  mockStrategistApi as registerStrategistRoutes,
  type MockStrategistApiOptions,
  type MockStrategistApiResult,
} from '../helpers/strategist';

interface StrategistFixtures {
  mockStrategistApi: (options?: MockStrategistApiOptions) => Promise<MockStrategistApiResult>;
  openStrategist: () => Promise<void>;
}

export const test = base.extend<StrategistFixtures>({
  mockStrategistApi: async ({ page }, fixtureUse) => {
    await fixtureUse((options = {}) => registerStrategistRoutes(page, options));
  },
  openStrategist: async ({ page }, fixtureUse) => {
    await fixtureUse(async () => {
      const launcher = page.getByRole('button', { name: 'Open chat', exact: true });
      if (await launcher.isVisible()) await launcher.click();
      const chatTab = page.getByRole('tab', { name: /^Chat$/i });
      if (await chatTab.isVisible() && (await chatTab.getAttribute('aria-selected')) !== 'true') {
        await chatTab.click();
      }
      await expect(page.getByLabel('AI workspace prompt')).toBeVisible();
    });
  },
});

export { expect } from '@playwright/test';
