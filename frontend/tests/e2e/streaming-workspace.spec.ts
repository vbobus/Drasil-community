import { test, expect } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
});

test('chat starters, reuse, copy, history search and themes work without connection chrome', async ({ page, context, mockWorkspace, mockStrategistApi, openStrategist }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.setViewportSize({ width: 1440, height: 900 });
  const workspace = await mockWorkspace();
  await mockCalendarBlocks(page);
  const now = new Date().toISOString();
  const session = { id: 'modern-chat', title: 'A calmer morning', providerLabel: 'ChatGPT', createdAt: now, updatedAt: now };
  const response = { providerLabel: 'ChatGPT', mode: 'review', summary: '## Make room for focused work\n\nStart with **one priority**, then leave space to adjust.\n\n1. Review the tasks that matter today.\n2. Protect a focused block before your meetings.\n3. Take a short break and reassess.\n\n> A realistic plan leaves room for the unexpected.\n\n```text\n09:00  Review priorities\n09:30  Focus time\n```', reasoningSummary: 'I considered your available time and kept the plan flexible.', contextCards: [], suggestedPrompts: [] };
  const api = await mockStrategistApi({ sessions: [session], sessionDetails: { [session.id]: { session, turns: [] } }, runResponses: [{ session, turn: { id: 'modern-turn', prompt: 'Help me make a realistic plan for today.', response, createdAt: now } }] });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot; await openStrategist();
  await page.getByRole('button', { name: 'New chat', exact: true }).click();
  await expect(page.locator('.chat-connection')).toHaveCount(0);
  await page.screenshot({ animations: 'disabled', path: '../.impeccable/review/chat-empty-dark.png' });
  await page.getByRole('button', { name: 'Plan my day', exact: true }).click();
  const input = page.getByLabel('AI workspace prompt');
  await expect(input).toBeFocused();
  await expect(input).toHaveValue('Help me make a realistic plan for today.');
  expect(api.capturedRunRequests).toHaveLength(0);
  await input.press('Shift+Enter');
  await input.press('Backspace');
  await input.press('Enter');
  await expect(page.getByRole('heading', { name: 'Make room for focused work' })).toBeVisible();
  await page.getByRole('button', { name: 'Copy response', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Copy response', exact: true })).toHaveAttribute('data-copied', 'true');
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('Make room for focused work');
  await page.getByRole('button', { name: 'Copy code', exact: true }).click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toContain('09:30  Focus time');
  await page.getByRole('button', { name: 'Reuse message' }).click();
  await expect(input).toBeFocused();
  await expect(input).toHaveValue('Help me make a realistic plan for today.');
  expect(api.capturedRunRequests).toHaveLength(1);
  await input.fill('');
  await page.screenshot({ animations: 'disabled', path: '../.impeccable/review/chat-rich-dark.png' });
  await page.evaluate(() => document.documentElement.dataset.theme = 'light');
  await page.mouse.move(0, 0);
  await expect(page.getByRole('button', { name: 'Chat history', exact: true })).toHaveCSS('color', 'rgb(71, 85, 105)');
  await page.screenshot({ animations: 'disabled', path: '../.impeccable/review/chat-rich-light.png' });
  await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
  await page.setViewportSize({ width: 390, height: 844 }); await openStrategist();
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.locator('.strategist-scroll').evaluate(el => el.scrollTop = 0);
  await expect(page.getByRole('button', { name: 'Scroll to latest message' })).toHaveCSS('height', '40px');
  await page.screenshot({ animations: 'disabled', path: '../.impeccable/review/chat-rich-mobile.png' });
  await page.getByRole('button', { name: 'Chat history', exact: true }).click();
  await page.getByRole('searchbox', { name: 'Search chats' }).fill('no match');
  await expect(page.getByText('No conversations match your search.')).toBeVisible();
  await page.getByRole('searchbox', { name: 'Search chats' }).fill('calmer');
  await expect(page.locator('.strategist-history-item')).toHaveCount(1);
  await expect(page.getByRole('searchbox', { name: 'Search chats' })).toBeInViewport();
  await page.screenshot({ animations: 'disabled', path: '../.impeccable/review/chat-history-mobile.png' });
  await page.getByRole('button', { name: 'Close chat history' }).click();
  await expect(page.getByRole('searchbox', { name: 'Search chats' })).toHaveCount(0);
});

test('laptop keeps planning and calendar visible and loads floating chat on demand', async ({ page, mockWorkspace, mockStrategistApi }) => {
  await page.setViewportSize({ width: 1100, height: 800 });
  const workspace = await mockWorkspace();
  await mockStrategistApi();
  await mockCalendarBlocks(page);
  const boot = workspace.waitForBoot();
  await page.goto('/');
  await boot;
  const panels = ['planning', 'calendar'].map((name) => page.locator('.panel-' + name));
  const bounds = [];
  for (const panel of panels) {
    await expect(panel).toBeVisible();
    bounds.push((await panel.boundingBox())!);
  }
  expect(bounds[1].x + bounds[1].width).toBeLessThanOrEqual(1100);
  await expect(page.getByRole('region', { name: 'Drasil chat', exact: true })).toBeHidden();
  await page.getByRole('button', { name: 'Open chat', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Drasil chat', exact: true })).toBeVisible();
  await expect(page.getByPlaceholder('Ask Drasil to plan, review, or schedule…')).toBeVisible();
  await page.getByRole('button', { name: 'Minimize chat', exact: true }).first().click();
  await expect(page.getByPlaceholder('Ask Drasil to plan, review, or schedule…')).toHaveCount(1);
  await expect(page.getByRole('navigation', { name: 'Workspace views' })).toBeHidden();
  await page.screenshot({ animations: 'disabled', path: '../.tmp/assistant-workspace-desktop.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('navigation', { name: 'Workspace views' })).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: '../.tmp/assistant-workspace-mobile.png' });
});

test('a connection failure stays an error and opens settings without losing the conversation', async ({ page, mockWorkspace, mockStrategistApi, openStrategist }) => {
  const workspace = await mockWorkspace();
  await mockCalendarBlocks(page);
  const now = new Date().toISOString();
  await mockStrategistApi({ runResponses: [{
    session: { id: 'connection-session', title: 'Connection test', providerLabel: 'ChatGPT', createdAt: now, updatedAt: now },
    turn: { id: 'connection-turn', prompt: 'hey', response: null, error: 'ChatGPT is not connected. Open Connections in Settings, finish sign-in, then retry this message.', createdAt: now },
  }] });
  await page.route('**/api/v1/auth/chatgpt', route => route.fulfill({ json: { provider: 'codex-cli', isAuthenticated: false } }));
  await page.route('**/api/v1/auth/strategist-provider*', route => route.fulfill({ json: { activeProvider: 'codex-cli', activeProviderLabel: 'ChatGPT', supportsChatGptLogin: true, chatGptHosting: 'sandbox', codexModelOptions: [], isOpenAiKeyConfigured: false, openAiModel: 'gpt-5-mini' } }));
  // Optional calendar outage must leave subscription setup usable.
  await page.route('**/api/v1/calendar-integrations/google/status', route => route.fulfill({ status: 503, json: { detail: 'Calendar unavailable' } }));
  const boot = workspace.waitForBoot();
  await page.goto('/'); await boot; await openStrategist();
  await page.getByLabel('AI workspace prompt').fill('hey');
  await page.getByLabel('AI workspace prompt').press('Enter');
  await expect(page.getByRole('alert')).toContainText('ChatGPT is not connected');
  await expect(page.getByText('Thought', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Copy response' })).toHaveCount(0);
  await page.screenshot({ animations: 'disabled', path: '../.impeccable/review/chat-error.png', fullPage: true });
  await page.getByRole('button', { name: 'Open connections', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Continue with ChatGPT', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Close settings', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('ChatGPT is not connected');
  await expect(page.getByRole('button', { name: 'Retry message' })).toBeVisible();
});

test('streams thinking and answer before completion, then supports another turn', async ({ page, mockWorkspace, mockStrategistApi, openStrategist }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const workspace = await mockWorkspace();
  await mockStrategistApi();
  await mockCalendarBlocks(page);
  await page.addInitScript(() => {
    const original = window.fetch.bind(window);
    let turn = 0;
    window.fetch = async (input, init) => {
      if (typeof input !== 'string' || !input.endsWith('/api/v1/ai/strategist') || init?.method !== 'POST') {
        return original(input, init);
      }
      turn++;
      const encoder = new TextEncoder();
      const payload = {
        session: { id: 'stream-session', title: 'Planning chat', providerLabel: 'ChatGPT login', createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() },
        turn: { id: 'stream-turn-' + turn, prompt: JSON.parse(String(init.body)).prompt, response: null as unknown, createdAt: new Date().toISOString() },
        run: { id: 'run-' + turn, status: 'running' },
      };
      return new Response(new ReadableStream({
        start(controller) {
          const send = (name: string, value: unknown) => controller.enqueue(encoder.encode('event: ' + name + '\ndata: ' + JSON.stringify(value) + '\n\n'));
          send('update', payload);
          send('progress', { kind: 'reasoning', text: 'Checking tasks against your available time.' });
          setTimeout(() => send('progress', { kind: 'answer', text: 'Your morning has room for focused work.' }), 150);
          setTimeout(() => {
            payload.turn.response = { providerLabel: 'ChatGPT login', mode: 'review', summary: 'Your morning has room for focused work.', contextCards: [], suggestedPrompts: [] };
            payload.run.status = 'completed';
            send('done', payload);
            controller.close();
          }, 2500);
        },
      }), { headers: { 'content-type': 'text/event-stream' } });
    };
  });
  const boot = workspace.waitForBoot();
  await page.goto('/');
  await boot;
  await openStrategist();
  const input = page.getByLabel('AI workspace prompt');
  await input.fill('Help me plan my morning');
  await input.press('Enter');
  await expect(page.getByText('Checking tasks against your available time.')).toBeVisible();
  await expect(page.getByText('Your morning has room for focused work.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Stop response' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Copy response' })).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: '../.impeccable/review/chat-desktop.png', fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await openStrategist();
  await page.screenshot({ animations: 'disabled', path: '../.impeccable/review/chat-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await input.fill('And tomorrow?');
  await input.press('Enter');
  await expect(page.getByText('And tomorrow?', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Copy response' })).toHaveCount(2);
  await page.evaluate(() => {
    const original = window.fetch.bind(window);
    let blockNext = true;
    window.fetch = (input, init) => {
      if (blockNext && typeof input === 'string' && input.endsWith('/api/v1/ai/strategist')) {
        blockNext = false;
        return new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
        });
      }
      return original(input, init);
    };
  });
  await input.fill('Check my afternoon');
  await input.press('Enter');
  await page.getByRole('button', { name: 'Stop response' }).click();
  await expect(page.getByRole('alert')).toContainText('Response stopped.');
  await page.getByRole('button', { name: 'Retry message' }).click();
  await expect(page.getByRole('button', { name: 'Copy response' })).toHaveCount(3);
  await expect(page.getByRole('button', { name: 'Send message', exact: true })).toBeVisible();
});

test('renders Markdown safely and restores the saved reasoning summary', async ({ page, mockWorkspace, mockStrategistApi, openStrategist }) => {
  const workspace = await mockWorkspace();
  await mockCalendarBlocks(page);
  const now = new Date().toISOString();
  await mockStrategistApi({ runResponses: [{
    session: { id: 'markdown-session', title: 'Markdown test', providerLabel: 'ChatGPT', createdAt: now, updatedAt: now },
    turn: { id: 'markdown-turn', prompt: 'Plan my day', createdAt: now, response: {
      providerLabel: 'ChatGPT', mode: 'review', summary: '## Your plan\n\n- **First:** review priorities.\n- Then take a break.\n\n<script>window.badMarkup = true</script>\n\n[Unsafe](javascript:alert(1))',
      reasoningSummary: 'Checking your available time.', contextCards: [], suggestedPrompts: [],
    } },
  }] });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot; await openStrategist();
  await page.getByLabel('AI workspace prompt').fill('Plan my day'); await page.getByLabel('AI workspace prompt').press('Enter');
  await expect(page.getByRole('heading', { name: 'Your plan', exact: true })).toBeVisible();
  await expect(page.locator('.chat-markdown li')).toHaveCount(2);
  await expect(page.locator('.chat-markdown strong')).toContainText('First:');
  await expect(page.locator('.chat-markdown script')).toHaveCount(0);
  await expect(page.getByRole('link', { name: 'Unsafe' })).not.toHaveAttribute('href', /javascript:/);
  const details = page.locator('.chat-reasoning');
  if (!await details.evaluate(el => (el as HTMLDetailsElement).open)) await details.locator('summary').click();
  await expect(page.getByText('Checking your available time.', { exact: true })).toBeVisible();
});
