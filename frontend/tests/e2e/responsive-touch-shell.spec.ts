import { expect } from '@playwright/test';
import { test } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test.describe('responsive touch workspace', () => {
  test.use({ hasTouch: true, isMobile: true, deviceScaleFactor: 1 });

  test.beforeEach(async ({ page, mockStrategistApi }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
      authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
    } }));
    await mockStrategistApi();
  });

  test('switches full-height workspaces on phone and tablet widths', async ({
    page,
    mockWorkspace,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    const workspace = await mockWorkspace();
    await mockCalendarBlocks(page);

    const boot = workspace.waitForBoot();
    await page.goto('/');
    await boot;

    const workspaceNavigation = page.getByRole('navigation', { name: 'Workspace views' });
    await expect(workspaceNavigation).toBeVisible();

    const planButton = workspaceNavigation.getByRole('button', { name: 'Plan', exact: true });
    const aiButton = page.getByRole('button', { name: 'Open chat', exact: true });
    const calendarButton = workspaceNavigation.getByRole('button', {
      name: 'Calendar',
      exact: true,
    });

    await expect(planButton).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.panel-planning')).toBeVisible();
    await expect(page.locator('.floating-chat')).toBeHidden();
    await expect(page.locator('.panel-calendar')).toBeHidden();

    await aiButton.tap();
    await expect(page.locator('.chat-launcher')).toBeHidden();
    await expect(page.locator('.floating-chat')).toBeVisible();
    await page.locator('.floating-chat-header').getByRole('button', { name: 'Minimize chat' }).tap();
    await expect(page.getByRole('button', { name: 'Open chat', exact: true })).toBeFocused();

    await calendarButton.tap();
    await expect(calendarButton).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.panel-calendar')).toBeVisible();
    await expect(page.getByRole('grid', { name: /Shared calendar day agenda/i })).toBeVisible();
    await expect(page.locator('.calendar-shell-mobile-placeholder')).toHaveCount(0);

    const navigationButtonHeight = await calendarButton.evaluate(
      (element) => element.getBoundingClientRect().height,
    );
    expect(navigationButtonHeight).toBeGreaterThanOrEqual(44);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      ),
    ).toBe(true);

    await page.setViewportSize({ width: 820, height: 1180 });
    await expect(workspaceNavigation).toBeVisible();
    await planButton.tap();
    await expect(page.locator('.panel-planning')).toBeVisible();
    await expect(page.locator('.floating-chat')).toBeHidden();
  });

  test('publishes installable app metadata', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('link[rel="manifest"]')).toHaveAttribute(
      'href',
      '/manifest.webmanifest',
    );

    const manifestResponse = await page.request.get('/manifest.webmanifest');
    expect(manifestResponse.ok()).toBe(true);
    const manifest = (await manifestResponse.json()) as Record<string, unknown>;
    expect(manifest.name).toBe('Drasil');
    expect(manifest.display).toBe('standalone');
  });
});
