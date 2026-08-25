import { expect, test } from '@playwright/test';

test.describe('Account access boundary', () => {
  test('bootstraps the first account and exposes the in-app switcher', async ({ page }) => {
    let initialized = false;
    const account = {
      id: 'account-owner',
      username: 'owner',
      displayName: 'Owner',
      isAdmin: true,
    };

    await page.route('**/api/v1/accounts/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          needsBootstrap: !initialized,
          authenticationRequired: true,
          supportsRemoteBackend: true,
        }),
      });
    });
    await page.route('**/api/v1/accounts/bootstrap', async (route) => {
      const submitted = route.request().postDataJSON() as Record<string, string>;
      expect(submitted.bootstrapToken).toBe('setup-token');
      initialized = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ token: 'owner-session-token', account }),
      });
    });
    await page.route('**/api/v1/accounts/me', async (route) => {
      expect(route.request().headers().authorization).toBe('Bearer owner-session-token');
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(account) });
    });
    await page.route('**/api/v1/accounts', async (route) => {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([account]) });
    });

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Create the first account' })).toBeVisible();
    await page.getByLabel('Display name').fill('Owner');
    await page.getByLabel('Username').fill('owner');
    await page.getByLabel('Password').fill('owner-password-123');
    await page.getByLabel('Remote setup token').fill('setup-token');
    await page.getByRole('button', { name: 'Create account' }).click();

    const switchButton = page.getByRole('button', {
      name: 'Switch account. Signed in as Owner',
    });
    await expect(switchButton).toBeVisible();
    await switchButton.click();
    await expect(page.getByRole('dialog', { name: 'Switch Drasil account' })).toBeVisible();
  });

  test('remembers an explicit remote backend without storing credentials in its URL', async ({ page }) => {
    await page.route('**/api/v1/accounts/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          needsBootstrap: false,
          authenticationRequired: true,
          supportsRemoteBackend: true,
        }),
      });
    });
    await page.route('https://api.example.test/api/v1/accounts/status', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          needsBootstrap: false,
          authenticationRequired: true,
          supportsRemoteBackend: true,
        }),
      });
    });

    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await page.getByLabel('Backend server').fill('https://api.example.test');
    await page.getByRole('button', { name: 'Use this server' }).click();
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await expect.poll(() => page.evaluate(() => localStorage.getItem('drasil.backend-url.v1')))
      .toBe('https://api.example.test');
  });
});
