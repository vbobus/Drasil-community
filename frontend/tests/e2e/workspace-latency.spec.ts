import { test, expect } from '../support/fixtures/strategist.fixture';
import type { Page } from '@playwright/test';
import type { ApiNode } from '../../src/api/nodes';

const account = { id: 'latency-account', username: 'owner', displayName: 'Owner', isAdmin: true };
const categories = [
  { id: 1, name: 'Work', accentColor: '#b69862', iconSlug: 'briefcase', sortOrder: 0 },
  { id: 2, name: 'Life', accentColor: '#669988', iconSlug: 'heart', sortOrder: 1 },
];

async function setup(page: Page, nodes: ApiNode[]) {
  await page.clock.setFixedTime(new Date('2026-10-07T12:00:00'));
  await page.addInitScript(value => sessionStorage.setItem('drasil.account.v1', JSON.stringify(value)), account);
  const state = { bootstrapReads: 0, statusReads: 0, taskReads: 0, calendarReads: 0, categoryReads: 0,
    calendarRanges: [] as { start: string; end: string }[],
    gate: null as Promise<void> | null, failRefresh: false };
  await page.route('**/api/v1/accounts/me', route => route.fulfill({ json: account }));
  await page.route('**/api/v1/accounts/status', route => {
    state.statusReads += 1;
    return route.fulfill({ json: { authenticationRequired: true, needsBootstrap: false, supportsRemoteBackend: true } });
  });
  await page.route('**/api/v1/workspace?*', async route => {
    state.bootstrapReads += 1;
    const params = new URL(route.request().url()).searchParams;
    const snapshot = structuredClone({ categories, nodes, blocks: [], scheduledTaskIds: [], startAt: params.get('start'), endAt: params.get('end') });
    if (state.gate) await state.gate;
    if (state.failRefresh) return route.fulfill({ status: 503, json: { detail: 'Temporarily unavailable' } });
    return route.fulfill({ json: snapshot });
  });
  await page.route('**/api/v1/categories', route => { state.categoryReads += 1; return route.fulfill({ json: categories }); });
  await page.route('**/api/v1/nodes**', route => {
    if (route.request().method() === 'GET') { state.taskReads += 1; return route.fulfill({ json: nodes }); }
    const id = Number(new URL(route.request().url()).pathname.split('/').at(-1));
    const task = nodes.find(node => node.id === id);
    if (task) Object.assign(task, route.request().postDataJSON());
    return route.fulfill({ json: task });
  });
  await page.route('**/api/v1/calendar-blocks**', route => {
    state.calendarReads += 1;
    const params = new URL(route.request().url()).searchParams;
    if (params.has('start')) state.calendarRanges.push({ start: params.get('start')!, end: params.get('end')! });
    return route.fulfill({ json: [] });
  });
  await page.route('**/api/v1/companion/runs', route => route.fulfill({ json: [] }));
  return state;
}

test('one workspace read feeds all categories and focused days reuse covered bookings', async ({ page, nodeFactory }) => {
  const nodes = [nodeFactory.build({ id: 901, categoryId: 1, title: 'Cached work', plannedDate: '2026-10-07' }),
    nodeFactory.build({ id: 902, categoryId: 2, title: 'Cached life', plannedDate: '2026-10-07' })];
  const state = await setup(page, nodes);
  await page.goto('/');
  await expect(page.getByText('Cached work', { exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Life workspace', exact: true }).click();
  await expect(page.getByText('Cached life', { exact: true }).first()).toBeVisible({ timeout: 500 });
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  await board.getByRole('button', { name: 'Next planning week', exact: true }).click();
  await expect(page.locator('.panel-calendar').getByRole('heading').first()).toContainText('October');
  expect(state).toMatchObject({ bootstrapReads: 1, statusReads: 0, taskReads: 0, calendarReads: 0, categoryReads: 0 });
  // Chat initialization is deferred until the user opens it.
  await expect(page.locator('.strategist-card')).toHaveCount(0);
});

test('verified reload shows saved cards while refresh is pending and stale reads cannot undo completion', async ({ page, nodeFactory }) => {
  const nodes = [nodeFactory.build({ id: 903, categoryId: 1, title: 'Cached work', plannedDate: '2026-10-07' })];
  const state = await setup(page, nodes);
  await page.goto('/');
  await expect(page.locator('[data-week-task="903"]')).toBeVisible();
  let release = () => {};
  state.gate = new Promise<void>(resolve => { release = resolve; });
  try {
    await page.reload();
    await expect.poll(() => state.bootstrapReads).toBe(2);
    await expect(page.locator('[data-week-task="903"]')).toBeVisible();
    await page.getByLabel('Show completed', { exact: true }).check();
    await page.getByRole('checkbox', { name: 'Complete Cached work', exact: true }).check();
    await expect.poll(() => nodes[0].status).toBe('done');
    const refreshed = page.waitForResponse('**/api/v1/workspace?*');
    release(); await refreshed;
    await expect(page.getByRole('checkbox', { name: 'Complete Cached work', exact: true })).toBeChecked();
  } finally { release(); }
});

test('scrolling beyond bootstrap coverage fetches only the missing dates', async ({ page, nodeFactory }) => {
  const state = await setup(page, [nodeFactory.build({ id: 906, categoryId: 1, title: 'Cached work', plannedDate: '2026-10-07' })]);
  await page.goto('/');
  await expect(page.locator('[data-week-task="906"]')).toBeVisible();
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  for (let week = 0; week < 5; week += 1) {
    const previousRange = await board.locator('.week-navigation > span').innerText();
    await board.getByRole('button', { name: 'Next planning week', exact: true }).click();
    await expect(board.locator('.week-navigation > span')).not.toHaveText(previousRange);
  }
  await expect.poll(() => state.calendarRanges.length).toBeGreaterThan(0);
  expect(state.calendarRanges.every(range => range.start >= '2026-11-09T00:00:00')).toBe(true);
  expect(state.taskReads).toBe(0);
});

test('refresh failure retains cards and Retry restores a current workspace', async ({ page, nodeFactory }) => {
  const nodes = [nodeFactory.build({ id: 904, categoryId: 1, title: 'Cached work', plannedDate: '2026-10-07' })];
  const state = await setup(page, nodes);
  await page.goto('/');
  await expect(page.locator('[data-week-task="904"]')).toBeVisible();
  state.failRefresh = true;
  await page.reload();
  const alert = page.getByRole('alert').filter({ hasText: 'Could not refresh workspace' });
  await expect(alert).toBeVisible();
  await expect(page.locator('[data-week-task="904"]')).toBeVisible();
  state.failRefresh = false;
  nodes[0].title = 'Updated work';
  await alert.getByRole('button', { name: 'Retry', exact: true }).click();
  await expect(alert).toHaveCount(0);
  await expect(page.locator('[data-week-task="904"]')).toContainText('Updated work');
});

test('expired verification clears snapshots before any cached task can appear', async ({ page, nodeFactory }) => {
  await setup(page, [nodeFactory.build({ id: 905, categoryId: 1, title: 'Private cached work', plannedDate: '2026-10-07' })]);
  await page.goto('/');
  await expect(page.locator('[data-week-task="905"]')).toBeVisible();
  await expect.poll(() => page.evaluate(() => Object.keys(sessionStorage).filter(key => key.startsWith('drasil.workspace.v1:')).length)).toBe(1);
  await page.route('**/api/v1/accounts/me', route => route.fulfill({ status: 401, json: { detail: 'Expired session' } }));
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
  await expect(page.getByText('Private cached work', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => Object.keys(sessionStorage).filter(key => key.startsWith('drasil.workspace.v1:')).length)).toBe(0);
});
