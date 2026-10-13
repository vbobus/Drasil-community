import { test, expect } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
  await page.setViewportSize({ width: 1440, height: 900 });
});

test('two-line nested cards keep branch lines in gutters and metadata separated', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  const items = [
    { id: 401, parentId: null, title: 'Reply to the sample contact', sortOrder: 0 },
    { id: 402, parentId: 401, title: 'New Task', sortOrder: 0 },
    { id: 403, parentId: 402, title: 'test2', sortOrder: 0 },
    { id: 404, parentId: 401, title: 'Send the final reply', sortOrder: 1 },
    { id: 405, parentId: null, title: 'Prepare documents for the sample appointment', sortOrder: 1 },
  ].map(item => nodeFactory.build({ ...item, categoryId: 1, emoji: '✉️', deadlineAt: '2026-10-02', timeEstimateMinutes: 10, plannedDate: '2026-10-02' }));
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: items } });
  await mockStrategistApi(); await mockCalendarBlocks(page);
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const tree = page.locator('.planning-tree-surface');
  await expect(tree.locator('[data-node-id="403"]')).toBeVisible();
  const assertGeometry = async () => {
    const rows = await tree.locator('.node-card-row').evaluateAll(elements => elements.map(element => {
      const rect = element.getBoundingClientRect();
      const metadata = [...element.querySelectorAll('.node-row-meta > .node-meta-control')].map(control => control.getBoundingClientRect()).filter(bounds => bounds.width > 0);
      return { height: rect.height, overflow: element.scrollWidth > element.clientWidth + 2,
        overlaps: metadata.some((bounds, index) => metadata.slice(0, index).some(previous =>
          Math.max(bounds.left, previous.left) < Math.min(bounds.right, previous.right)
          && Math.max(bounds.top, previous.top) < Math.min(bounds.bottom, previous.bottom))),
        titleBottom: element.querySelector('.node-title')!.getBoundingClientRect().bottom, metaY: metadata[0].y };
    }));
    expect(rows.length).toBe(5);
    for (const row of rows) {
      expect(row.height).toBeGreaterThanOrEqual(64);
      expect(row.overflow).toBe(false);
      expect(row.overlaps).toBe(false);
      expect(row.metaY).toBeGreaterThanOrEqual(row.titleBottom);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  };
  await assertGeometry();
  await page.screenshot({ path: '../.impeccable/review/tree-two-line-narrow-dark.png' });
  const separator = page.getByRole('separator').first();
  await separator.focus();
  for (let index = 0; index < 12; index++) await page.keyboard.press('ArrowRight');
  await assertGeometry();
  await expect(tree.locator('.node-priority-trigger').first()).toBeVisible();
  await page.screenshot({ path: '../.impeccable/review/tree-two-line-wide-dark.png' });
  await page.evaluate(() => document.documentElement.dataset.theme = 'light');
  await page.waitForTimeout(200);
  await page.screenshot({ path: '../.impeccable/review/tree-two-line-wide-light.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
  await page.getByRole('button', { name: 'Tree', exact: true }).click();
  await assertGeometry();
  await page.screenshot({ path: '../.impeccable/review/tree-two-line-mobile.png' });
});

test('real pointer moves nest, unnest, cancel, and persist across workspace reloads', async ({ page, mockStrategistApi }) => {
  await mockStrategistApi();
  const createCategory = async (name: string) => {
    const response = await page.request.post('/api/v1/categories', { data: { name, accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 99 } });
    expect(response.ok()).toBeTruthy(); return response.json();
  };
  const work = await createCategory('Drag work');
  const home = await createCategory('Drag home');
  const createTask = async (title: string, categoryId: number) => {
    const response = await page.request.post('/api/v1/nodes', { data: { title, categoryId } });
    expect(response.ok()).toBeTruthy(); return response.json();
  };
  const parent = await createTask('Organize trip', work.id);
  const child = await createTask('Book the tickets', work.id);
  const other = await createTask('Home arrangements', home.id);
  await page.goto('/');
  await page.getByRole('button', { name: 'All workspace', exact: true }).click();
  const card = (id: number) => page.locator(`[data-node-id="${id}"] .node-card`).first();
  const drag = async (sourceId: number, targetId: number) => {
    await card(sourceId).scrollIntoViewIfNeeded();
    const a = (await card(sourceId).boundingBox())!;
    await page.mouse.move(a.x + a.width / 2, a.y + 25); await page.mouse.down();
    const b = (await card(targetId).boundingBox())!;
    await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2, { steps: 12 });
  };
  await expect(card(child.id)).toBeVisible();
  await drag(child.id, parent.id);
  await expect(card(parent.id)).toHaveClass(/drop-inside/);
  const nested = page.waitForResponse(r => r.url().endsWith(`/nodes/${child.id}/move`) && r.ok());
  await page.mouse.up(); await nested;
  await expect(card(child.id)).toHaveAttribute('aria-level', '2');
  await expect(page.getByRole('dialog', { name: 'Task page' })).toHaveCount(0);
  await drag(child.id, parent.id);
  const rootDrop = page.locator('[data-tree-root-drop]');
  const r = (await rootDrop.boundingBox())!;
  await page.mouse.move(r.x + r.width / 2, r.y + r.height / 2, { steps: 10 });
  const unnested = page.waitForResponse(response => response.url().endsWith(`/nodes/${child.id}/move`) && response.ok());
  await page.mouse.up(); await unnested;
  await expect(card(child.id)).toHaveAttribute('aria-level', '1');
  let writes = 0;
  page.on('request', request => { if (request.url().endsWith('/move')) writes++; });
  await drag(child.id, parent.id);
  await page.mouse.move(5, 5, { steps: 8 }); await page.mouse.up();
  await expect(card(child.id)).toHaveAttribute('aria-level', '1');
  expect(writes).toBe(0);
  await drag(child.id, other.id);
  await page.mouse.up();
  await expect(card(child.id)).toHaveAttribute('aria-level', '1');
  expect(writes).toBe(0);
  await page.getByRole('button', { name: 'Drag work workspace', exact: true }).click();
  await drag(child.id, parent.id);
  const moved = page.waitForResponse(response => response.url().endsWith(`/nodes/${child.id}/move`) && response.ok());
  await page.mouse.up(); await moved;
  await expect(card(child.id)).toHaveAttribute('aria-level', '2');
  await expect(page.locator(`[data-node-id="${parent.id}"]`)).toHaveCount(1);
  await page.reload();
  await page.getByRole('button', { name: 'Drag work workspace', exact: true }).click();
  await expect(card(child.id)).toHaveAttribute('aria-level', '2');
  const saved = await page.request.get(`/api/v1/nodes?categoryId=${work.id}`);
  expect((await saved.json()).find((node: { id: number }) => node.id === child.id).parentId).toBe(parent.id);
});

test('task page and floating chat retain work in both themes and on mobile', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory, openStrategist }) => {
  await page.clock.setFixedTime(new Date('2026-10-02T12:00:00'));
  const tasks = ['Prepare for the sample appointment', 'Reply to the sample contact', 'Plan the weekend'].map((title, index) => nodeFactory.build({ id: index + 101, categoryId: 1, title, plannedDate: '2026-10-02', timeEstimateMinutes: 60 }));
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: tasks } });
  await mockStrategistApi(); await mockCalendarBlocks(page);
  await page.route('**/api/v1/nodes/101', route => {
    Object.assign(tasks[0], route.request().postDataJSON()); return route.fulfill({ json: tasks[0] });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  await expect(page.getByRole('button', { name: 'Open chat', exact: true })).toBeVisible();
  await expect(page.getByText('No set time', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Drop a task here', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: '../.impeccable/review/workspace-cards-dark.png' });
  const title = page.getByLabel('Task title: Prepare for the sample appointment', { exact: true });
  await title.click();
  const dialog = page.getByRole('dialog', { name: 'Task page', exact: true });
  await expect(dialog).toBeVisible();
  expect((await dialog.boundingBox())!.width).toBeGreaterThan(1000);
  await page.getByRole('textbox', { name: 'Task title', exact: true }).fill('Bring the checklist to the sample appointment');
  await page.getByRole('button', { name: 'Close task page' }).focus();
  await page.screenshot({ path: '../.impeccable/review/workspace-task-page-dark.png' });
  await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);
  await expect(page.getByLabel('Task title: Bring the checklist to the sample appointment', { exact: true })).toBeVisible();
  await openStrategist();
  await page.getByLabel('AI workspace prompt').fill('Help me plan tomorrow');
  await page.screenshot({ path: '../.impeccable/review/workspace-floating-chat-dark.png' });
  await page.locator('.floating-chat-header').getByRole('button', { name: 'Minimize chat' }).click();
  await openStrategist();
  await expect(page.getByLabel('AI workspace prompt')).toHaveValue('Help me plan tomorrow');
  await expect(page.getByText('Review changes before applying.', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: 'Expand chat', exact: true }).click();
  expect((await page.locator('.floating-chat').boundingBox())!.width).toBe(960);
  await page.locator('.floating-chat-header').getByRole('button', { name: 'Minimize chat' }).click();
  await page.evaluate(() => document.documentElement.dataset.theme = 'light');
  await page.waitForTimeout(300);
  await page.screenshot({ path: '../.impeccable/review/workspace-cards-light.png' });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
  await page.waitForTimeout(300);
  await page.screenshot({ path: '../.impeccable/review/workspace-cards-mobile.png' });
  await openStrategist();
  await page.screenshot({ path: '../.impeccable/review/workspace-chat-mobile.png' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.keyboard.press('Escape');
  await page.getByRole('button', { name: 'Tree', exact: true }).click();
  await page.getByLabel('Task title: Bring the checklist to the sample appointment', { exact: true }).click();
  await expect(dialog).toBeVisible();
  await page.screenshot({ path: '../.impeccable/review/workspace-task-mobile.png' });
});
