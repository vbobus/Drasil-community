import { expect, test } from '../support/fixtures/strategist.fixture';

test('centers the expanded wordmark inside the dark-mode brand control', async ({
  page,
  mockWorkspace,
}) => {
  const workspace = await mockWorkspace();
  const boot = workspace.waitForBoot();

  await page.goto('/');
  await boot;

  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const alignment = await page
    .locator('.sidebar-logo-btn.expanded')
    .evaluate((button) => {
      const wordmark = button.querySelector<HTMLElement>('.sidebar-wordmark');
      if (!wordmark) {
        throw new Error('Sidebar wordmark is missing.');
      }
      const buttonRect = button.getBoundingClientRect();
      const wordmarkRect = wordmark.getBoundingClientRect();
      return {
        horizontalOffset:
          wordmarkRect.left +
          wordmarkRect.width / 2 -
          (buttonRect.left + buttonRect.width / 2),
        verticalOffset:
          wordmarkRect.top +
          wordmarkRect.height / 2 -
          (buttonRect.top + buttonRect.height / 2),
      };
    });

  expect(Math.abs(alignment.horizontalOffset)).toBeLessThanOrEqual(1);
  expect(Math.abs(alignment.verticalOffset)).toBeLessThanOrEqual(1);
});
