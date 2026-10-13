import { test, expect } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test('companion settings, inbox, and reviewed planner handoff', async ({ page, mockWorkspace, mockStrategistApi, openStrategist }) => {
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: { authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false } }));
  const settings = { enabled: false, paired: true, time_zone: 'America/Vancouver', check_in_hours: [9,18], daily_run_limit: 6,
    personal_context: 'Finish my course without losing evenings with family.', worker_status: 'ready', memory_status: 'ready', last_seen_at: '2026-10-02T16:00:00' };
  await page.route('**/api/v1/companion/settings', async route => {
    if (route.request().method() === 'PUT') Object.assign(settings, route.request().postDataJSON());
    await route.fulfill({ json: settings });
  });
  await page.route('**/api/v1/companion/runs', route => route.fulfill({ json: [{ id: 'one', kind: 'check_in', prompt: '', status: 'completed', created_at: '2026-10-02T16:00:00', error: null, dismissed: false,
    result: { message: 'You wanted to finish your course this month. Would a **25-minute session** today feel manageable?', suggested_prompt: 'Find 25 minutes for my course today.', quiet: false } }] }));
  await mockWorkspace(); await mockStrategistApi(); await mockCalendarBlocks(page);
  await page.setViewportSize({ width: 1440, height: 900 }); await page.goto('/');
  await page.addStyleTag({ content: '*, *::before, *::after { transition: none !important; animation: none !important; }' });
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Companion', exact: true }).click();
  await expect(page.getByLabel('What matters to you')).toHaveValue(settings.personal_context);
  await page.getByLabel('Enable companion').check();
  await page.getByRole('button', { name: 'Save settings', exact: true }).click();
  await expect(page.getByText('Companion settings saved.')).toBeVisible();
  await page.locator('.settings-content').evaluate(element => { element.scrollTop = 0; });
  for (const theme of ['dark', 'light']) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
    await page.screenshot({ path: `../.impeccable/review/companion-settings-desktop-${theme}.png` });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.settings-content').evaluate(element => { element.scrollTop = 0; });
  await page.screenshot({ path: '../.impeccable/review/companion-settings-mobile.png' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole('button', { name: 'Close settings' }).click();
  await openStrategist(); await page.getByRole('tab', { name: /^Companion/ }).click();
  await expect(page.getByText('25-minute session', { exact: true })).toBeVisible();
  await page.screenshot({ path: '../.impeccable/review/companion-inbox-mobile.png' });
  await page.setViewportSize({ width: 1440, height: 900 });
  for (const theme of ['dark', 'light']) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
    await page.screenshot({ path: `../.impeccable/review/companion-inbox-desktop-${theme}.png` });
  }
  await page.getByRole('button', { name: 'Draft a plan', exact: true }).click();
  await expect(page.getByLabel('AI workspace prompt')).toHaveValue('Find 25 minutes for my course today.');
});
