import { test, expect } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test('workspace load errors remain visible and Retry restores existing tasks', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
  const task = nodeFactory.build({ categoryId: 1, title: 'My existing task' });
  await mockWorkspace({
    categories: [{ id: 1, name: 'Work', accentColor: '#b69862', iconSlug: 'briefcase', sortOrder: 0 }],
    nodesByCategoryId: { 1: [task] },
  });
  await mockStrategistApi();
  await mockCalendarBlocks(page);
  let unavailable = true;
  await page.route(/\/api\/v1\/nodes\?.*categoryId=\d+/, route => unavailable
    ? route.fulfill({ status: 503, json: { detail: 'Temporarily unavailable' } })
    : route.fallback());
  await page.clock.install();
  await page.goto('/');
  const alert = page.getByRole('alert').filter({ hasText: 'Failed to load' });
  await expect(alert).toBeVisible();
  await page.clock.fastForward(10_000);
  await expect(alert).toBeVisible();
  await alert.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(alert).toBeVisible();
  unavailable = false;
  await alert.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(alert).toHaveCount(0);
  await expect(page.getByText('My existing task', { exact: true }).first()).toBeVisible();
});
