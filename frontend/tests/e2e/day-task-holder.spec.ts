import { test, expect } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-10-02T12:00:00'));
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
});

test('a task beyond the collapsed preview can be dragged onto time, retried after failure, and cancelled', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const tasks = Array.from({ length: 5 }, (_, index) => nodeFactory.build({ id: 950 + index, categoryId: 1, title: `Flexible item ${index + 1}`, plannedDate: '2026-10-02', timeEstimateMinutes: 45 }));
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: tasks } });
  await mockStrategistApi(); const calendar = await mockCalendarBlocks(page);
  let attempts = 0;
  await page.route('**/api/v1/calendar-blocks', route => {
    if (route.request().method() !== 'POST') return route.fallback();
    attempts++;
    if (attempts === 1) return route.fulfill({ status: 503, json: { detail: 'Booking failed; try again' } });
    return route.fallback();
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const holder = page.locator('[data-day-holder="2026-10-02"]');
  const drag = async (cancel = false) => {
    await holder.hover();
    const title = holder.getByRole('button', { name: 'Flexible item 5', exact: true });
    await expect(title).toBeVisible();
    const source = (await title.boundingBox())!;
    const surface = (await page.getByTestId('calendar-day-drop-zone').boundingBox())!;
    const agenda = (await page.locator('.calendar-agenda').boundingBox())!;
    await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2);
    await page.mouse.down();
    await page.mouse.move(source.x + source.width / 2 + 12, source.y + source.height / 2, { steps: 3 });
    await page.mouse.move(surface.x + surface.width / 2, agenda.y + agenda.height * .75, { steps: 15 });
    await page.mouse.move(surface.x + surface.width / 2 + 1, agenda.y + agenda.height * .75);
    await expect(holder).toHaveClass(/is-dragging-task/);
    if (cancel) await page.keyboard.press('Escape');
    await page.mouse.up();
    await expect(holder).not.toHaveClass(/is-dragging-task/);
  };
  await drag(true);
  expect(attempts).toBe(0);
  await drag();
  await expect(page.getByRole('alert')).toContainText('Booking failed');
  expect(calendar.createdPayloads).toHaveLength(0);
  await drag();
  await expect.poll(() => calendar.createdPayloads.length).toBe(1);
  expect(calendar.createdPayloads[0]).toMatchObject({ taskId: 954, categoryId: 1 });
  expect(Date.parse(String(calendar.createdPayloads[0].endAt)) - Date.parse(String(calendar.createdPayloads[0].startAt))).toBe(45 * 60 * 1000);
  await holder.hover();
  await expect(holder.getByRole('button', { name: 'Flexible item 5', exact: true })).toHaveCount(0);
});

test('real pointer drag assigns an empty untimed day without creating a timed block', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const task = nodeFactory.build({ id: 901, categoryId: 1, title: 'Drag me to a day', plannedDate: null, timeEstimateMinutes: 30 });
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: [task] } });
  await mockStrategistApi(); const calendar = await mockCalendarBlocks(page);
  const updates: Record<string, unknown>[] = [];
  await page.route('**/api/v1/nodes/901', route => {
    const payload = route.request().postDataJSON(); updates.push(payload); Object.assign(task, payload);
    return route.fulfill({ json: task });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const dragToDay = async (day: string) => {
    const source = (await page.locator('[data-node-id="901"] .node-card').boundingBox())!;
    const holder = page.locator(`[data-day-holder="${day}"]`);
    const target = (await holder.boundingBox())!;
    expect(target.height).toBeGreaterThanOrEqual(64);
    await page.mouse.move(source.x + source.width / 2, source.y + 24);
    await page.mouse.down();
    await page.mouse.move(target.x + target.width / 2, target.y + target.height - 14, { steps: 10 });
    await expect(holder).toHaveClass(/is-drop-target/);
    await page.screenshot({ path: '../.impeccable/review/day-holder-drop-target.png' });
    await page.mouse.up();
    await expect(holder.getByText(task.title, { exact: true })).toBeVisible();
    await expect(holder).not.toHaveClass(/is-drop-target/);
  };
  await dragToDay('2026-10-02');
  expect(updates).toEqual([{ plannedDate: '2026-10-02' }]);
  expect(calendar.createdPayloads).toHaveLength(0);
  await page.getByRole('button', { name: 'Week', exact: true }).click();
  await dragToDay('2026-10-03');
  expect(updates.at(-1)).toEqual({ plannedDate: '2026-10-03' });
  expect(calendar.createdPayloads).toHaveLength(0);
  await page.clock.setFixedTime(new Date('2026-10-02T12:00:01'));
  const check = page.locator('[data-node-id="901"] .node-checkbox');
  await check.check();
  await expect(check).toBeChecked();
  await expect(check).toHaveCSS('background-color', 'rgb(35, 134, 90)');
  await expect(check).toHaveCSS('border-radius', '50%');
  await page.waitForTimeout(400);
  await page.screenshot({ path: '../.impeccable/review/task-completed-green.png' });
  await check.uncheck();
  await expect(check).not.toBeChecked();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await check.check();
  expect(await check.evaluate(element => getComputedStyle(element, '::before').animationName)).toBe('none');
  expect(updates.at(-1)).toMatchObject({ status: 'done' });
});

test('day holder, composer, themes and mobile keep planned tasks untimed', async ({ page, mockWorkspace, mockStrategistApi, categoryFactory, nodeFactory }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const categories = [categoryFactory.build({ id: 1, name: 'Work', accentColor: '#6366f1' }), categoryFactory.build({ id: 2, name: 'Health', accentColor: '#10b981' })];
  const nodes = ['Send the project update', 'Review portfolio', 'Book a sample appointment', 'Pick up groceries', 'Read for twenty minutes', 'A long task description that wraps when the day holder is expanded'].map((title, index) => nodeFactory.build({ id: index + 10, categoryId: 1, title, plannedDate: '2026-10-02', timeEstimateMinutes: 30 }));
  nodes.push(nodeFactory.build({ id: 20, categoryId: 1, title: 'Timed focus session', plannedDate: '2026-10-02' }));
  const workspace = await mockWorkspace({ categories, nodesByCategoryId: { 1: nodes, 2: [] } });
  await mockStrategistApi();
  const calendar = await mockCalendarBlocks(page, { blocks: [{ id: 1, taskId: 20, categoryId: 1, title: 'Timed focus session', categoryName: 'Work', categoryColor: '#6366f1', startAt: '2026-10-02T13:00:00', endAt: '2026-10-02T14:00:00', variant: 'scheduled', completionStatus: 'pending', isPinned: false, isProtected: false, isRecurring: false }] });
  const writes: Record<string, unknown>[] = [];
  await page.route('**/api/v1/nodes', route => {
    const payload = route.request().postDataJSON(); writes.push(payload);
    const created = nodeFactory.build({ ...payload, id: 99 }); nodes.push(created);
    return route.fulfill({ status: 201, json: created });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const holder = page.locator('[data-day-holder="2026-10-02"]');
  await expect(holder.getByLabel('6 tasks', { exact: true })).toBeVisible();
  await expect(holder.locator('.day-task-chip')).toHaveCount(3);
  await expect(holder.getByText('Timed focus session')).toHaveCount(0);
  await expect(holder.locator('.day-task-title').first()).toHaveCSS('color', 'rgb(37, 36, 31)');
  await page.screenshot({ path: '../.impeccable/review/day-tasks-desktop-dark.png', animations: 'disabled' });
  await holder.hover(); await expect(holder.locator('.day-task-chip')).toHaveCount(6);
  await page.screenshot({ path: '../.impeccable/review/day-tasks-expanded.png', animations: 'disabled' });
  await page.mouse.move(0, 0); await expect(holder.locator('.day-task-chip')).toHaveCount(3);
  await page.getByRole('button', { name: 'Add task', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a task' });
  await expect(page.getByLabel('Task description', { exact: true })).toBeFocused();
  await page.getByLabel('Task description', { exact: true }).fill('Prepare for the sample appointment');
  await page.getByLabel('Task notes').fill('Bring my licence and booking confirmation.');
  await page.getByLabel('Estimate', { exact: true }).fill('60');
  await expect(dialog).toHaveCSS('width', '740px');
  await page.screenshot({ path: '../.impeccable/review/task-composer-dark.png', animations: 'disabled' });
  await page.evaluate(() => document.documentElement.dataset.theme = 'light');
  await page.screenshot({ path: '../.impeccable/review/task-composer-light.png', animations: 'disabled' });
  await dialog.getByRole('button', { name: 'Add task', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(writes[0]).toMatchObject({ title: 'Prepare for the sample appointment', plannedDate: '2026-10-02', deadlineAt: null, timeEstimateMinutes: 60 });
  expect(calendar.createdPayloads).toHaveLength(0);
  await expect(holder.getByLabel('7 tasks', { exact: true })).toBeVisible();
  await page.screenshot({ path: '../.impeccable/review/day-tasks-desktop-light.png', animations: 'disabled' });
  await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('navigation', { name: 'Workspace views' }).getByRole('button', { name: 'Calendar', exact: true }).click();
  await expect(holder).toBeVisible();
  const axisBounds = (await page.locator('.calendar-date-axis').boundingBox())!;
  const clockBounds = (await page.locator('.calendar-day-track').boundingBox())!;
  expect(clockBounds.y).toBeGreaterThanOrEqual(axisBounds.y + axisBounds.height - 1);
  await holder.getByRole('button', { name: 'Show 4 more' }).focus();
  await page.keyboard.press('Enter');
  await expect(holder.locator('.day-task-chip')).toHaveCount(7);
  await page.screenshot({ path: '../.impeccable/review/day-tasks-mobile.png', animations: 'disabled' });
  await holder.getByRole('button', { name: 'Add task for 2026-10-02' }).click();
  await page.getByLabel('Task description', { exact: true }).fill('An easy morning walk');
  await page.screenshot({ path: '../.impeccable/review/task-composer-mobile.png', animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);
});

test('tasks can move between day holders, appear in month view, and be completed', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const task = nodeFactory.build({ categoryId: 1, title: 'Flexible task', plannedDate: '2026-10-02' });
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#6366f1', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: [task] } });
  await mockStrategistApi(); const calendar = await mockCalendarBlocks(page);
  const updates: Record<string, unknown>[] = [];
  await page.route(`**/api/v1/nodes/${task.id}`, route => {
    const payload = route.request().postDataJSON(); updates.push(payload); Object.assign(task, payload);
    return route.fulfill({ json: task });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  await page.getByRole('button', { name: 'Week', exact: true }).click();
  const target = page.locator('[data-day-holder="2026-10-03"]');
  const dataTransfer = await page.evaluateHandle(id => {
    const data = new DataTransfer(); data.setData('application/x-drasil-node', JSON.stringify({ id: String(id), categoryId: 1, parentId: null, sortOrder: 0 })); return data;
  }, task.id);
  await target.dispatchEvent('drop', { dataTransfer });
  await expect(target.getByText('Flexible task')).toBeVisible();
  await expect(page.locator('[data-day-holder="2026-10-02"]').getByText('Flexible task')).toHaveCount(0);
  expect(updates[0]).toMatchObject({ plannedDate: '2026-10-03' });
  expect(calendar.createdPayloads).toHaveLength(0);
  await page.getByRole('button', { name: 'Month', exact: true }).click();
  await expect(target.getByText('Flexible task')).toBeVisible();
  await target.getByRole('button', { name: 'Complete Flexible task' }).click();
  await expect(target.getByText('Flexible task')).toHaveCount(0);
  expect(updates.at(-1)).toMatchObject({ status: 'done' });
});

test('composer preserves input after a failed save and cancellation creates nothing', async ({ page, mockWorkspace, mockStrategistApi }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const workspace = await mockWorkspace(); await mockStrategistApi(); await mockCalendarBlocks(page);
  let writes = 0;
  await page.route('**/api/v1/nodes', route => { writes++; return route.fulfill({ status: 503, json: { detail: 'Unavailable' } }); });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  await page.getByRole('button', { name: 'Add task', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Add a task' });
  await page.getByLabel('Task description', { exact: true }).fill('Keep this draft');
  await dialog.getByRole('button', { name: 'Add task', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Your details are still here');
  await expect(page.getByLabel('Task description', { exact: true })).toHaveValue('Keep this draft');
  await page.keyboard.press('Escape'); await expect(dialog).toHaveCount(0);
  expect(writes).toBe(1);
});
