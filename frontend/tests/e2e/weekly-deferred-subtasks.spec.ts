import { test, expect } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test('deferred subtasks share identity but do not block today completion or its estimate', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.clock.setFixedTime(new Date('2026-10-02T12:00:00'));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: { authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false } }));
  const tasks = [
    nodeFactory.build({ id: 810, categoryId: 1, title: 'Prepare application', plannedDate: '2026-10-02', timeEstimateMinutes: 0 }),
    nodeFactory.build({ id: 811, categoryId: 1, parentId: 810, title: 'Review the draft today', timeEstimateMinutes: 30 }),
    nodeFactory.build({ id: 812, categoryId: 1, parentId: 810, title: 'Send application tomorrow', plannedDate: '2026-10-03', timeEstimateMinutes: 45 }),
    nodeFactory.build({ id: 813, categoryId: 1, parentId: 812, title: 'Attach the supporting document', timeEstimateMinutes: 15 }),
  ];
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: tasks } });
  await mockStrategistApi(); await mockCalendarBlocks(page);
  const patches: { id: number; status?: string }[] = [];
  await page.route('**/api/v1/nodes/*', route => {
    if (route.request().method() !== 'PATCH') return route.fallback();
    const id = Number(route.request().url().split('/').at(-1));
    const payload = route.request().postDataJSON();
    patches.push({ id, ...payload });
    const task = tasks.find(task => task.id === id)!;
    Object.assign(task, payload);
    return route.fulfill({ json: task });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  await board.getByRole('checkbox', { name: 'Show completed', exact: true }).check();
  const today = board.locator('[data-week-day="2026-10-02"]');
  const tomorrow = board.locator('[data-week-day="2026-10-03"]');
  const parent = today.locator('[data-week-task="810"]');
  const future = tomorrow.locator('[data-week-task="812"]');
  const deferred = parent.locator('[data-week-subtask="812"]');
  await expect(deferred).toHaveClass(/is-deferred/);
  await expect(deferred).toContainText('Planned Oct 3');
  await expect(parent.locator('[data-week-subtask="813"]')).toHaveClass(/is-deferred/);
  await expect(parent.locator('.week-task-estimate')).toHaveText('30m');
  await expect(today.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '0 of 1 tasks completed');
  await page.screenshot({ path: '../.impeccable/review/week-deferred-dark.png', animations: 'disabled' });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  await page.screenshot({ path: '../.impeccable/review/week-deferred-light.png', animations: 'disabled' });
  await page.getByRole('button', { name: 'Hide task repository', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: '../.impeccable/review/week-deferred-mobile.png', animations: 'disabled' });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: 'Tree', exact: true }).click();
  await parent.getByRole('checkbox', { name: 'Complete Prepare application', exact: true }).check();
  await expect(today.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '1 of 1 tasks completed');
  await expect(deferred.getByRole('checkbox')).not.toBeChecked();
  expect(patches.some(patch => [812, 813].includes(patch.id))).toBe(false);
  expect(tasks[0].status).toBe('todo');
  // Finishing the reference early completes tomorrow's canonical card and its descendants.
  await deferred.getByRole('checkbox').check();
  await expect(future.getByRole('checkbox', { name: 'Complete Send application tomorrow', exact: true })).toBeChecked();
  await expect(tomorrow.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '1 of 1 tasks completed');
  await expect(parent.locator('[data-week-subtask="813"] input')).toBeChecked();
  await board.locator('.week-board-scroll').evaluate(element => {
    const column = element.querySelector<HTMLElement>('[data-week-day="2026-10-03"]')!;
    const tree = document.querySelector('.floating-task-repository')!.getBoundingClientRect();
    const calendar = document.querySelector('.panel-calendar')!.getBoundingClientRect();
    element.scrollLeft += column.getBoundingClientRect().left + column.clientWidth / 2 - (tree.right + calendar.left) / 2;
  });
  await future.getByRole('checkbox', { name: 'Complete Send application tomorrow', exact: true }).uncheck();
  await expect(deferred.getByRole('checkbox')).not.toBeChecked();
  await expect(today.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '1 of 1 tasks completed');
  await expect(tomorrow.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '0 of 1 tasks completed');
});

test('containers with entirely deferred work are complete for today without completing future tasks', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  await page.clock.setFixedTime(new Date('2026-10-02T12:00:00'));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: { authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false } }));
  const tasks = [
    nodeFactory.build({ id: 820, categoryId: 1, title: 'Entirely deferred parent', plannedDate: '2026-10-02' }),
    nodeFactory.build({ id: 821, categoryId: 1, parentId: 820, title: 'Future work', plannedDate: '2026-10-03' }),
    nodeFactory.build({ id: 830, categoryId: 1, title: 'Parent of deferred container', plannedDate: '2026-10-02' }),
    nodeFactory.build({ id: 831, categoryId: 1, parentId: 830, title: 'Nested container' }),
    nodeFactory.build({ id: 832, categoryId: 1, parentId: 831, title: 'Nested future work', plannedDate: '2026-10-03' }),
  ];
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: tasks } });
  await mockStrategistApi(); await mockCalendarBlocks(page);
  const writes: string[] = [];
  page.on('request', request => { if (request.method() === 'PATCH' && request.url().includes('/nodes/')) writes.push(request.url()); });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  await board.getByRole('checkbox', { name: 'Show completed', exact: true }).check();
  const today = board.locator('[data-week-day="2026-10-02"]');
  for (const title of ['Entirely deferred parent', 'Parent of deferred container', 'Nested container']) {
    const checkbox = today.getByRole('checkbox', { name: `Complete ${title}`, exact: true });
    await expect(checkbox).toBeChecked();
    await expect(checkbox).toBeDisabled();
  }
  await expect(today.getByRole('progressbar')).toHaveAttribute('aria-valuetext', '2 of 2 tasks completed');
  for (const title of ['Future work', 'Nested future work']) {
    await expect(today.getByRole('checkbox', { name: `Complete ${title}`, exact: true })).not.toBeChecked();
    await expect(board.locator('[data-week-day="2026-10-03"]').getByRole('checkbox', { name: `Complete ${title}`, exact: true })).not.toBeChecked();
  }
  expect(tasks.every(task => task.status === 'todo')).toBe(true);
  expect(writes).toEqual([]);
});
