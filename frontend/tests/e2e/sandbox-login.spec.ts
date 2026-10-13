import { expect, test } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test('hosted ChatGPT connection shows device code, detects completion, and disconnects', async ({ page, context, mockWorkspace, mockStrategistApi }) => {
  const workspace = await mockWorkspace();
  await mockStrategistApi();
  await mockCalendarBlocks(page);
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
  let phase: 'disconnected' | 'pending' | 'connected' | 'failed' = 'disconnected';
  const status = () => ({
    provider: 'codex-cli', isAuthenticated: phase === 'connected',
    email: phase === 'connected' ? 'person@example.test' : null,
    planType: phase === 'connected' ? 'plus' : null,
    pendingSession: phase === 'pending' ? {
      sessionId: 'test-login', status: 'pending', userCode: 'TEST-1234',
      verificationUri: 'https://auth.openai.com/codex/device',
    } : phase === 'failed' ? { sessionId: 'expired-login', status: 'failed', error: 'ChatGPT sign-in timed out. Start a fresh connection to get a new code.' } : null,
  });
  await context.route('https://auth.openai.com/**', route => route.fulfill({ body: '<p>OpenAI sign-in test</p>', contentType: 'text/html' }));
  await page.route('**/api/v1/auth/strategist-provider*', route => route.fulfill({ json: {
    activeProvider: 'codex-cli', activeProviderLabel: 'ChatGPT login', supportsChatGptLogin: true,
    chatGptHosting: 'sandbox', codexModelOptions: [], isOpenAiKeyConfigured: false, openAiModel: 'gpt-5-mini',
  } }));
  await page.route('**/api/v1/auth/chatgpt**', async route => {
    if (route.request().url().endsWith('/start')) phase = 'pending';
    if (route.request().url().endsWith('/logout')) phase = 'disconnected';
    await route.fulfill({ json: status() });
  });
  await page.route('**/api/v1/calendar-integrations/google/**', route => route.fulfill({ json: { isConfigured: false } }));
  const boot = workspace.waitForBoot();
  await page.goto('/');
  await boot;
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('button', { name: 'Connections', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Settings', exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: /Open ChatGPT security settings/ })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeInViewport();
  await page.screenshot({ path: '../.impeccable/review/desktop.png', fullPage: true });
  await expect(page.getByText(/sleeps after five idle minutes/)).toBeVisible();
  await page.getByRole('button', { name: 'Continue with ChatGPT', exact: true }).click();
  await expect(page.getByText('TEST-1234', { exact: true })).toBeVisible();
  await expect(page.getByText('TEST-1234', { exact: true })).toBeInViewport();
  await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toBeVisible();
  await page.screenshot({ path: '../.tmp/sandbox-device-login.png' });
  phase = 'connected';
  await expect(page.locator('.connection-identity')).toContainText('person@example.test');
  await expect(page.getByText('TEST-1234', { exact: true })).not.toBeVisible();
  await page.getByRole('button', { name: 'Disconnect', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeEnabled();
  await expect(page.getByText('person@example.test', { exact: true })).not.toBeVisible();
  await page.getByRole('button', { name: 'Appearance', exact: true }).click();
  await page.getByTestId('theme-light-choice').click();
  await page.getByRole('button', { name: 'Connections', exact: true }).click();
  await page.screenshot({ path: '../.impeccable/review/desktop-light.png', fullPage: true });
  await page.getByRole('button', { name: 'Appearance', exact: true }).click();
  await page.getByTestId('theme-dark-choice').click();
  await page.getByRole('button', { name: 'Connections', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeInViewport();
  await page.screenshot({ path: '../.impeccable/review/mobile.png', fullPage: true });
  expect(await page.getByRole('dialog').evaluate(el => el.scrollWidth <= el.clientWidth)).toBe(true);
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  phase = 'failed';
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('sign-in timed out');
  await expect(page.getByRole('alert')).not.toContainText('Enable device-code');
  await page.screenshot({ path: '../.impeccable/review/login-timeout.png', fullPage: true });
});
