import { test, expect } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-10-02T12:00:00'));
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
});

test('weekly cards contain subtasks and scroll focus follows the calendar without task writes', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const tasks = [
    nodeFactory.build({ id: 110, categoryId: 1, title: 'Prepare launch', plannedDate: '2026-10-02', priority: 'high', timeEstimateMinutes: 30, emoji: '🚀' }),
    nodeFactory.build({ id: 111, categoryId: 1, parentId: 110, title: 'Review release notes', timeEstimateMinutes: 15 }),
    nodeFactory.build({ id: 112, categoryId: 1, parentId: 110, title: 'Check deployment', timeEstimateMinutes: 20 }),
    nodeFactory.build({ id: 113, categoryId: 1, parentId: 112, title: 'Verify sign in', timeEstimateMinutes: 10 }),
    nodeFactory.build({ id: 114, categoryId: 1, parentId: 110, title: 'Send update', plannedDate: '2026-10-03', timeEstimateMinutes: 10 }),
    nodeFactory.build({ id: 120, categoryId: 1, title: 'Unassigned repository task' }),
    nodeFactory.build({ id: 121, categoryId: 1, title: 'Saturday review', plannedDate: '2026-10-03', timeEstimateMinutes: 45 }),
    nodeFactory.build({ id: 122, categoryId: 1, title: 'Scheduled appointment', timeEstimateMinutes: 30 }),
  ];
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: tasks } });
  await mockStrategistApi();
  await mockCalendarBlocks(page, { blocks: [{ id: 801, taskId: 122, categoryId: 1, title: 'Scheduled appointment', categoryName: 'Work', categoryColor: '#b59358', startAt: '2026-10-02T14:00:00', endAt: '2026-10-02T14:30:00', variant: 'scheduled', completionStatus: 'not_started', isPinned: false, isProtected: false, isRecurring: false }] });
  const updates: Record<string, unknown>[] = [];
  await page.route('**/api/v1/nodes/*', route => {
    if (route.request().method() === 'GET') return route.fallback();
    const id = Number(route.request().url().split('/').at(-1));
    const task = tasks.find(node => node.id === id);
    if (!task) return route.fallback();
    const payload = route.request().postDataJSON();
    updates.push({ id, ...payload }); Object.assign(task, payload);
    return route.fulfill({ json: task });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  const friday = board.locator('[data-week-day="2026-10-02"]');
  await expect(board.getByRole('searchbox')).toHaveCount(0);
  await expect(board.locator('.week-board-footer')).toHaveCount(0);
  await expect(page.locator('.calendar-shell > .calendar-detail-control')).toHaveCount(0);
  const launchCard = friday.locator('[data-week-task="110"]');
  await expect(launchCard).toBeVisible();
  await expect(launchCard.getByText('Verify sign in', { exact: true })).toBeVisible();
  await expect(launchCard.getByText('Part of Check deployment', { exact: true })).toBeVisible();
  await expect(friday.locator('[data-week-task="111"]')).toHaveCount(0);
  await expect(board.locator('[data-week-task="120"]')).toHaveCount(0);
  await expect(friday.locator('[data-week-task="122"]')).toBeVisible();
  await expect(friday.locator('[data-week-task="122"] .week-task-times')).toContainText('2:00');
  await expect(page.locator('[data-day-holder="2026-10-02"]')).toBeVisible();
  const treeBox = (await page.locator('.floating-task-repository').boundingBox())!;
  const calendarBox = (await page.locator('.panel-calendar').boundingBox())!;
  const boardBox = (await board.boundingBox())!;
  expect(treeBox.x).toBeLessThan(boardBox.x + boardBox.width / 2);
  expect(calendarBox.x).toBeGreaterThan(boardBox.x + boardBox.width / 2);
  expect(boardBox.width).toBeGreaterThan(treeBox.width + calendarBox.width);
  const shellBox = (await page.getByTestId('app-shell').boundingBox())!;
  expect(treeBox.x).toBeCloseTo(shellBox.x, 0);
  expect(calendarBox.x + calendarBox.width).toBeCloseTo(shellBox.x + shellBox.width, 0);
  for (const box of [treeBox, calendarBox]) {
    expect(box.y).toBeCloseTo(shellBox.y, 0);
    expect(box.height).toBeCloseTo(shellBox.height, 0);
  }
  for (const selector of ['.floating-task-repository', '.calendar-shell', '.planning-panel']) {
    await expect(page.locator(selector)).toHaveCSS('border-top-width', '0px');
    await expect(page.locator(selector)).toHaveCSS('border-radius', '0px');
  }
  for (const selector of ['.floating-task-repository', '.panel-calendar']) {
    expect(await page.locator(selector).evaluate(element => getComputedStyle(element).boxShadow)).not.toBe('none');
  }
  expect((await board.locator('.week-board-scroll').boundingBox())!.y - shellBox.y).toBeLessThan(115);
  const previousScroll = await board.locator('.week-board-scroll').evaluate(element => element.scrollLeft);
  await page.locator('.floating-task-repository').dispatchEvent('wheel', { deltaX: 280, deltaY: 0 });
  await expect.poll(() => board.locator('.week-board-scroll').evaluate(element => element.scrollLeft)).toBeGreaterThan(previousScroll);
  expect((await page.locator('.floating-task-repository').boundingBox())!.x).toBe(treeBox.x);
  expect((await page.locator('.panel-calendar').boundingBox())!.x).toBe(calendarBox.x);

  await board.locator('.week-board-scroll').evaluate(element => {
    const target = element.querySelector<HTMLElement>('[data-week-day="2026-10-03"]')!;
    element.scrollLeft += target.getBoundingClientRect().left + target.clientWidth / 2 - element.getBoundingClientRect().left - element.clientWidth / 2;
  });
  await expect(board.locator('[data-week-day="2026-10-03"]')).toHaveClass(/is-focused/);
  await expect(page.locator('[data-day-holder="2026-10-03"]')).toBeVisible();
  expect(updates).toEqual([]);
  await board.getByRole('button', { name: 'Today', exact: true }).click();
  await launchCard.getByRole('checkbox', { name: 'Complete Review release notes', exact: true }).check();
  await expect(launchCard.getByRole('checkbox', { name: 'Complete Review release notes', exact: true })).toBeChecked();
  expect(updates).toContainEqual({ id: 111, status: 'done' });
  await launchCard.getByRole('button', { name: 'Verify sign in', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await page.screenshot({ path: '../.impeccable/review/week-board-desktop-dark.png', animations: 'disabled' });
  await page.evaluate(() => { document.documentElement.setAttribute('data-theme', 'light'); });
  await page.screenshot({ path: '../.impeccable/review/week-board-desktop-light.png', animations: 'disabled' });
  // Saved large widths and smaller desktop windows must still leave room to plan.
  await page.getByTestId('app-shell').evaluate(element => {
    (element as HTMLElement).style.setProperty('--preferred-tree-panel-width', '620px');
    (element as HTMLElement).style.setProperty('--preferred-calendar-panel-width', '720px');
  });
  const expectUsableCenter = async () => {
    const tree = (await page.locator('.floating-task-repository').boundingBox())!;
    const calendar = (await page.locator('.panel-calendar').boundingBox())!;
    expect(calendar.x - tree.x - tree.width).toBeGreaterThanOrEqual(359);
    const toggle = (await page.getByRole('button', { name: 'Hide calendar panel', exact: true }).boundingBox())!;
    expect(toggle.x).toBeGreaterThanOrEqual(tree.x + tree.width);
    expect(toggle.x + toggle.width).toBeLessThanOrEqual(calendar.x);
    await expect.poll(async () => {
      const focused = (await board.locator('.week-day-column.is-focused').boundingBox())!;
      return Math.abs(focused.x + focused.width / 2 - (tree.x + tree.width + calendar.x) / 2);
    }).toBeLessThan(2);
  };
  await expectUsableCenter();
  await page.screenshot({ path: '../.impeccable/review/week-board-wide-panels.png', animations: 'disabled' });
  await page.setViewportSize({ width: 1024, height: 900 });
  await expectUsableCenter();
  await page.screenshot({ path: '../.impeccable/review/week-board-small-desktop.png', animations: 'disabled' });
  await page.getByRole('button', { name: 'Hide task repository', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.impeccable/review/week-board-mobile.png', animations: 'disabled' });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('tree drop assigns a day, supports retry, and leaves timed blocks intact', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.setViewportSize({ width: 1600, height: 900 });
  const task = nodeFactory.build({ id: 210, categoryId: 1, title: 'Choose this for Friday', plannedDate: null, timeEstimateMinutes: 30 });
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: [task] } });
  await mockStrategistApi(); const calendar = await mockCalendarBlocks(page);
  let fail = true;
  const updates: Record<string, unknown>[] = [];
  await page.route('**/api/v1/nodes/210', route => {
    const payload = route.request().postDataJSON(); updates.push(payload);
    if (fail) { fail = false; return route.fulfill({ status: 503, json: { detail: 'Try again' } }); }
    Object.assign(task, payload); return route.fulfill({ json: task });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  const friday = board.locator('[data-week-day="2026-10-02"]');
  const drag = async () => {
    const source = (await page.locator('[data-node-id="210"] .node-card').boundingBox())!;
    const target = (await friday.locator('.week-add-task').boundingBox())!;
    await page.mouse.move(source.x + source.width / 2, source.y + 24);
    await page.mouse.down();
    await page.mouse.move(target.x + target.width / 2, target.y + target.height / 2, { steps: 12 });
    await page.mouse.up();
  };
  await drag();
  await expect(page.getByRole('alert').first()).toBeVisible();
  await expect(board.locator('.is-drop-target')).toHaveCount(0);
  await expect(friday.locator('[data-week-task="210"]')).toHaveCount(0);
  await drag();
  await expect(friday.locator('[data-week-task="210"]')).toBeVisible();
  await expect(board.locator('.is-drop-target')).toHaveCount(0);
  await expect(page.locator('body')).not.toHaveClass(/app-pointer-dragging/);
  await expect(page.locator('[data-day-holder="2026-10-02"]').getByText(task.title, { exact: true })).toBeVisible();
  expect(updates).toEqual([{ plannedDate: '2026-10-02' }, { plannedDate: '2026-10-02' }]);
  expect(calendar.createdPayloads).toHaveLength(0);
  await friday.getByRole('button', { name: task.title, exact: true }).click();
  await page.getByRole('dialog').getByLabel('Task planned day', { exact: true }).fill('2026-10-03');
  await page.keyboard.press('Escape');
  await expect(board.locator('[data-week-day="2026-10-03"] [data-week-task="210"]')).toHaveCount(1);
  expect(updates.at(-1)).toEqual({ plannedDate: '2026-10-03' });
  expect(calendar.createdPayloads).toHaveLength(0);
  await page.screenshot({ path: '../.impeccable/review/week-board-repository.png', animations: 'disabled' });
  await page.getByRole('button', { name: 'Next planning week', exact: true }).click();
  await expect(board.locator('[data-week-day="2026-10-05"]')).toHaveClass(/is-focused/);
  await expect(page.locator('[data-day-holder="2026-10-05"]')).toBeVisible();
  await page.getByRole('button', { name: 'Tree', exact: true }).click();
  await expect(page.getByRole('tree')).not.toBeVisible();
  await page.getByRole('button', { name: 'Tree', exact: true }).click();
  await expect(page.getByRole('tree')).toBeVisible();
  expect(calendar.createdPayloads).toHaveLength(0);
});

test('cards follow time and task order while scrolling extends through both week boundaries', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const tasks = [
    nodeFactory.build({ id: 301, categoryId: 1, title: 'Afternoon booking', plannedDate: '2026-10-02', sortOrder: 0 }),
    nodeFactory.build({ id: 302, categoryId: 1, title: 'Morning booking', plannedDate: '2026-10-02', sortOrder: 9 }),
    nodeFactory.build({ id: 303, categoryId: 1, title: 'Second untimed task', plannedDate: '2026-10-02', sortOrder: 5 }),
    nodeFactory.build({ id: 304, categoryId: 1, title: 'First untimed task', plannedDate: '2026-10-02', sortOrder: 1 }),
    nodeFactory.build({ id: 305, categoryId: 1, title: 'Booking beyond the first window' }),
  ];
  const workspace = await mockWorkspace({ categories: [
    { id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 },
    { id: 2, name: 'Personal', accentColor: '#739b9b', iconSlug: 'circle', sortOrder: 1 },
  ], nodesByCategoryId: { 1: tasks, 2: [nodeFactory.build({ id: 306, categoryId: 2, title: 'Personal task' })] } });
  await mockStrategistApi();
  await mockCalendarBlocks(page, { blocks: [
    { id: 901, taskId: 301, startAt: '2026-10-02T16:00:00', endAt: '2026-10-02T16:30:00' },
    { id: 902, taskId: 302, startAt: '2026-10-02T09:00:00', endAt: '2026-10-02T09:30:00' },
    { id: 903, taskId: 305, startAt: '2026-10-12T10:00:00', endAt: '2026-10-12T10:30:00' },
  ].map(block => ({ ...block, categoryId: 1, title: tasks.find(task => task.id === block.taskId)!.title, categoryName: 'Work', categoryColor: '#b59358', variant: 'scheduled' as const, completionStatus: 'not_started' as const, isPinned: false, isProtected: false, isRecurring: false })) });
  const writes: string[] = [];
  page.on('request', request => { if (['PATCH', 'POST', 'DELETE'].includes(request.method()) && /api\/v1\/(nodes|calendar-blocks)/.test(request.url())) writes.push(request.url()); });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  const scroll = board.locator('.week-board-scroll');
  await expect.poll(() => board.locator('[data-week-day="2026-10-02"] .week-task-card').evaluateAll(cards => cards.map(card => card.getAttribute('data-week-task')))).toEqual(['302', '301', '304', '303']);
  await expect(board.locator('.week-task-card').first()).toHaveCSS('border-radius', '6px');
  await expect(page.locator('.node-card-row').first()).toHaveCSS('border-radius', '6px');
  const focus = async (day: string) => {
    await scroll.evaluate((element, day) => {
      const column = element.querySelector<HTMLElement>(`[data-week-day="${day}"]`)!;
      const tree = document.querySelector('.floating-task-repository')!.getBoundingClientRect();
      const calendar = document.querySelector('.panel-calendar')!.getBoundingClientRect();
      element.scrollLeft += column.getBoundingClientRect().left + column.clientWidth / 2 - (tree.right + calendar.left) / 2;
    }, day);
    await expect(board.locator(`[data-week-day="${day}"]`)).toHaveClass(/is-focused/);
    await expect(page.locator(`[data-day-holder="${day}"]`)).toBeVisible();
  };
  await focus('2026-10-04');
  const sundayOffset = await scroll.evaluate(element => element.querySelector('[data-week-day="2026-10-04"]')!.getBoundingClientRect().left + element.scrollLeft);
  await focus('2026-10-05');
  await expect(board.locator('.week-navigation')).toContainText('Oct 5');
  expect(await scroll.evaluate(element => element.querySelector('[data-week-day="2026-10-04"]')!.getBoundingClientRect().left + element.scrollLeft)).toBeCloseTo(sundayOffset, 0);
  await focus('2026-10-10');
  await expect(board.locator('[data-week-day="2026-10-18"]')).toHaveCount(1);
  await focus('2026-10-12');
  await expect(board.locator('[data-week-day="2026-10-12"] [data-week-task="305"] .week-task-times')).toContainText('10:00');
  await page.screenshot({ path: '../.impeccable/review/week-board-next-week.png', animations: 'disabled' });
  await focus('2026-09-21');
  await expect(board.locator('[data-week-day="2026-09-14"]')).toHaveCount(1);
  const position = await board.locator('[data-week-day="2026-09-21"]').boundingBox();
  const tree = (await page.locator('.floating-task-repository').boundingBox())!;
  const calendar = (await page.locator('.panel-calendar').boundingBox())!;
  expect(Math.abs(position!.x + position!.width / 2 - (tree.x + tree.width + calendar.x) / 2)).toBeLessThan(2);
  await focus('2026-09-14');
  await expect(board.locator('[data-week-day="2026-09-07"]')).toHaveCount(1);
  await focus('2026-09-07');
  await expect(board.locator('[data-week-day="2026-08-31"]')).toHaveCount(1);
  expect(await board.locator('[data-week-day]').count()).toBeLessThanOrEqual(35);
  expect(writes).toEqual([]);
  await board.getByRole('button', { name: 'Today', exact: true }).click();
  await page.getByRole('button', { name: 'All workspace', exact: true }).click();
  const divider = page.locator('.planning-tree-workspace-divider').last();
  await expect(divider).toBeVisible();
  await expect(divider).toHaveCSS('border-top-style', 'solid');
  expect(await divider.evaluate(element => getComputedStyle(element, '::after').backgroundImage)).toBe('none');
  await page.screenshot({ path: '../.impeccable/review/week-board-all-tree.png', animations: 'disabled' });
  await page.getByLabel('Calendar detail', { exact: true }).click();
  await page.getByLabel('Zoom / Detail', { exact: true }).fill('0.5');
  await page.screenshot({ path: '../.impeccable/review/week-board-calendar-detail.png', animations: 'disabled' });
  await page.getByLabel('Calendar detail', { exact: true }).click();
  await expect(page.getByLabel('Zoom / Detail', { exact: true })).not.toBeVisible();
});

test('whole-card drag scrolls across weeks and moves the existing booking without duplication', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  const task = nodeFactory.build({ id: 410, categoryId: 1, title: 'Move this appointment', plannedDate: '2026-10-02', priority: 'high', timeEstimateMinutes: 60 });
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: [task] } });
  await mockStrategistApi();
  const calendar = await mockCalendarBlocks(page, { blocks: [{ id: 941, taskId: 410, categoryId: 1, title: task.title,
    categoryName: 'Work', categoryColor: '#b59358', startAt: '2026-10-02T14:00:00', endAt: '2026-10-02T15:00:00',
    variant: 'scheduled', completionStatus: 'not_started', isPinned: false, isProtected: false, isRecurring: false }] });
  const moves: Record<string, unknown>[] = [];
  await page.route('**/api/v1/calendar-blocks/move-task-day', route => {
    const payload = route.request().postDataJSON();
    moves.push(payload);
    task.plannedDate = payload.targetDate;
    const block = calendar.blocks[0];
    block.startAt = `${payload.targetDate}T14:00:00`;
    block.endAt = `${payload.targetDate}T15:00:00`;
    return route.fulfill({ json: [block] });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  const card = board.locator('[data-week-task="410"]');
  await expect(card).toHaveCount(1);
  await expect(card.locator('.week-task-grip')).toHaveCount(0);
  const priority = (await card.locator('.week-task-priority').boundingBox())!;
  const category = (await card.locator('.week-task-category').boundingBox())!;
  expect(category.x).toBeGreaterThan(priority.x + priority.width);
  const tree = page.locator('.floating-task-repository');
  expect((await tree.boundingBox())!.width).toBe(300);
  const treeCard = tree.locator('.node-card').first();
  await page.mouse.move(800, 80);
  await expect(treeCard.locator('.node-add-child-btn')).toHaveCSS('opacity', '0');
  await treeCard.hover();
  await expect(treeCard.locator('.node-add-child-btn')).toHaveCSS('opacity', '1');
  await expect(treeCard.locator('.node-stage-trigger')).toHaveCSS('opacity', '1');
  await treeCard.focus();
  await page.mouse.move(800, 80);
  await expect(treeCard.locator('.node-add-child-btn')).toHaveCSS('opacity', '1');
  await card.getByRole('button', { name: task.title, exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  const box = (await card.boundingBox())!;
  const calendarBox = (await page.locator('.panel-calendar').boundingBox())!;
  const startOffset = await board.locator('.week-board-scroll').evaluate(element => element.scrollLeft);
  await page.mouse.move(box.x + 8, box.y + box.height - 5);
  await page.mouse.down();
  await page.mouse.move(calendarBox.x - 8, box.y + box.height - 5, { steps: 16 });
  await expect(card).toHaveClass(/is-dragging/);
  await expect.poll(() => board.locator('.week-day-column.is-focused').getAttribute('data-week-day')).toMatch(/^2026-10-0[5-9]$/);
  expect(await board.locator('.week-board-scroll').evaluate(element => element.scrollLeft)).toBeGreaterThan(startOffset + 900);
  await page.screenshot({ path: '../.impeccable/review/week-board-dragging.png', animations: 'disabled' });
  const center = ((await tree.boundingBox())!.x + (await tree.boundingBox())!.width + calendarBox.x) / 2;
  await page.mouse.move(center, box.y + box.height - 5);
  // Chromium dispatches dragover after entering the newly revealed column.
  await page.mouse.move(center, box.y + box.height - 5);
  const targetDate = await page.evaluate(({ x, y }) => document.elementFromPoint(x, y)?.closest<HTMLElement>('[data-week-day]')?.dataset.weekDay, { x: center, y: box.y + box.height - 5 });
  await page.mouse.up();
  await expect.poll(() => moves).toEqual([{ taskId: 410, sourceDate: '2026-10-02', targetDate }]);
  await expect(board.locator(`[data-week-day="${targetDate}"] [data-week-task="410"]`)).toHaveCount(1);
  await expect(board.locator('[data-week-task="410"]')).toHaveCount(1);
  await expect(board.locator('.is-drop-target')).toHaveCount(0);
  await expect(page.locator('.week-drag-preview')).toHaveCount(0);
  await expect(page.locator('.week-task-card.is-dragging')).toHaveCount(0);
  expect(calendar.createdPayloads).toHaveLength(0);
  expect(calendar.blocks[0].id).toBe(941);
  expect(calendar.blocks[0].startAt).toBe(`${targetDate}T14:00:00`);
  await expect(page.locator(`[data-day-holder="${targetDate}"]`)).toBeVisible();
  await expect(page.locator('.calendar-block-title').filter({ hasText: task.title })).toHaveCount(1);
});

test('a move during a pending destination fetch retains unrelated bookings and ignores stale results', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  const task = nodeFactory.build({ id: 510, categoryId: 1, title: 'Rescheduled appointment', plannedDate: '2026-10-02' });
  const other = nodeFactory.build({ id: 511, categoryId: 1, title: 'Existing destination appointment' });
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: [task, other] } });
  await mockStrategistApi();
  const calendar = await mockCalendarBlocks(page, { blocks: [
    { id: 951, taskId: 510, title: task.title, startAt: '2026-10-02T14:00:00', endAt: '2026-10-02T15:00:00' },
  ].map(block => ({ ...block, categoryId: 1, categoryName: 'Work', categoryColor: '#b59358', variant: 'scheduled' as const,
    completionStatus: 'not_started' as const, isPinned: false, isProtected: false, isRecurring: false })) });
  await page.route('**/api/v1/calendar-blocks/move-task-day', route => {
    task.plannedDate = '2026-10-05';
    Object.assign(calendar.blocks[0], { startAt: '2026-10-05T14:00:00', endAt: '2026-10-05T15:00:00' });
    return route.fulfill({ json: [calendar.blocks[0]] });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  await expect(board.locator('[data-week-task="510"] .week-task-times')).toBeVisible();
  // A booking created elsewhere after our cache loaded is only discoverable by
  // the interrupted read/replacement read, not the move response itself.
  calendar.blocks.push({ id: 952, taskId: 511, title: other.title, startAt: '2026-10-05T10:00:00', endAt: '2026-10-05T11:00:00',
    categoryId: 1, categoryName: 'Work', categoryColor: '#b59358', variant: 'scheduled',
    completionStatus: 'not_started', isPinned: false, isProtected: false, isRecurring: false });
  let destinationReads = 0;
  let releaseStale: () => void = () => {};
  const staleResponse = new Promise<void>(resolve => { releaseStale = resolve; });
  await page.route('**/api/v1/calendar-blocks?*', async route => {
    const start = new URL(route.request().url()).searchParams.get('start');
    if (start !== '2026-10-05T00:00:00') return route.fallback();
    destinationReads += 1;
    if (destinationReads !== 1) return route.fallback();
    const snapshot = structuredClone([calendar.blocks[1]]);
    await staleResponse;
    return route.fulfill({ json: snapshot });
  });
  try {
    // Focus within fresh coverage now reuses data. Expire it to exercise a
    // genuinely pending read and the move's stale-response protection.
    await page.clock.setFixedTime(new Date('2026-10-02T12:01:01'));
    await board.getByRole('button', { name: 'Next planning week', exact: true }).click();
    await expect.poll(() => destinationReads).toBe(1);
    const dataTransfer = await page.evaluateHandle(() => {
      const data = new DataTransfer();
      data.setData('application/x-drasil-node', JSON.stringify({ id: '510', sourceDay: '2026-10-02' }));
      return data;
    });
    await board.locator('[data-week-day="2026-10-05"]').dispatchEvent('drop', { dataTransfer });
    await expect.poll(() => destinationReads).toBe(2);
    await expect(page.locator('[data-calendar-block-id="951"]')).toHaveCount(1);
    await expect(page.locator('[data-calendar-block-id="952"]')).toHaveCount(1);
    const staleDone = page.waitForResponse(response => {
      const url = new URL(response.url());
      return url.pathname === '/api/v1/calendar-blocks' && url.searchParams.get('start') === '2026-10-05T00:00:00';
    });
    releaseStale(); await staleDone;
    await expect(page.locator('[data-calendar-block-id="951"]')).toHaveCount(1);
    await expect(page.locator('[data-calendar-block-id="952"]')).toHaveCount(1);
    await expect(board.locator('[data-week-task="510"]')).toHaveCount(1);
    await expect(board.locator('[data-week-day="2026-10-02"] [data-week-task="510"]')).toHaveCount(0);
  } finally { releaseStale(); }
});
