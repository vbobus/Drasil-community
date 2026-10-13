import { test, expect } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(new Date('2026-10-02T12:00:00'));
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
});

test('expanded calendar preserves its mode, fits seven days and anchors month weekdays', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }],
    nodesByCategoryId: { 1: [nodeFactory.build({ id: 710, categoryId: 1, title: 'Plan the release', plannedDate: '2026-10-02' })] } });
  await mockStrategistApi(); await mockCalendarBlocks(page);
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  const calendar = page.locator('.panel-calendar');
  const compactWidth = (await calendar.boundingBox())!.width;
  await calendar.getByRole('button', { name: 'Week', exact: true }).click();
  expect((await calendar.boundingBox())!.width).toBeGreaterThan(compactWidth + 300);
  const fits = async () => {
    const agenda = calendar.locator('.calendar-agenda');
    expect(await agenda.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    const cells = calendar.locator('.calendar-week-time-column, .calendar-month-weekdays > span');
    await expect(cells).toHaveCount(7);
    const bounds = (await agenda.boundingBox())!;
    for (const cell of await cells.all()) {
      const box = (await cell.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(bounds.x - 1);
      expect(box.x + box.width).toBeLessThanOrEqual(bounds.x + bounds.width + 1);
    }
  };
  await fits();
  const shell = (await page.locator('.app-shell-floating').boundingBox())!;
  expect((await calendar.boundingBox())!.y).toBe(shell.y);
  expect((await calendar.boundingBox())!.height).toBe(shell.height);
  await page.screenshot({ path: '../.impeccable/review/calendar-expanded-week.png', animations: 'disabled' });
  await page.getByRole('button', { name: 'Hide task repository', exact: true }).click();
  for (const control of [board.getByRole('button', { name: 'Next planning week', exact: true }), board.getByRole('button', { name: 'Today', exact: true }), page.getByRole('button', { name: 'Tree', exact: true })]) {
    const box = (await control.boundingBox())!;
    expect(box.x + box.width).toBeLessThanOrEqual((await calendar.boundingBox())!.x);
    expect(await control.evaluate(element => {
      const box = element.getBoundingClientRect();
      return element.contains(document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2));
    })).toBe(true);
  }
  const focus = async (day: string) => {
    await board.locator('.week-board-scroll').evaluate((element, day) => {
      const column = element.querySelector<HTMLElement>(`[data-week-day="${day}"]`)!;
      const calendar = document.querySelector('.panel-calendar')!.getBoundingClientRect();
      element.scrollLeft += column.getBoundingClientRect().left + column.clientWidth / 2 - (element.getBoundingClientRect().left + calendar.left) / 2;
    }, day);
    await expect(board.locator(`[data-week-day="${day}"]`)).toHaveClass(/is-focused/);
  };
  await focus('2026-10-05');
  await expect(calendar.getByRole('button', { name: 'Week', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(calendar.locator('.calendar-week-time-column').first()).toHaveAttribute('data-calendar-date', '2026-10-05');
  await calendar.locator('.calendar-date-axis-cell[data-calendar-date="2026-10-08"] .calendar-date-select').click();
  await expect(board.locator('[data-week-day="2026-10-08"]')).toHaveClass(/is-focused/);
  await expect(calendar.getByRole('button', { name: 'Week', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.screenshot({ path: '../.impeccable/review/calendar-full-height-week-closed.png', animations: 'disabled' });
  await calendar.locator('.calendar-week-time-column[data-calendar-date="2026-10-09"] .calendar-hour-cell').first().click();
  await expect(board.locator('[data-week-day="2026-10-09"]')).toHaveClass(/is-focused/);
  await calendar.getByRole('button', { name: 'Month', exact: true }).click();
  await fits();
  await calendar.locator('.calendar-month-cell[data-calendar-date="2026-10-26"] .calendar-date-select').click();
  await expect(board.locator('[data-week-day="2026-10-26"]')).toHaveClass(/is-focused/);
  await expect(calendar.locator('[data-focused-week="true"]').first()).toHaveAttribute('data-calendar-date', '2026-10-26');
  await calendar.locator('.calendar-month-cell[data-calendar-date="2026-10-14"] .calendar-date-select').focus();
  await page.keyboard.press('Enter');
  await expect(board.locator('[data-week-day="2026-10-14"]')).toHaveClass(/is-focused/);
  await expect(calendar.getByRole('button', { name: 'Month', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(calendar.locator('[data-focused-week="true"]')).toHaveCount(7);
  await expect(calendar.locator('[data-focused-week="true"]').first()).toHaveAttribute('data-calendar-date', '2026-10-12');
  const weekdays = calendar.getByLabel('Days of the week', { exact: true });
  const top = (await weekdays.boundingBox())!.y;
  await calendar.locator('.calendar-agenda').evaluate(element => { element.scrollTop = 220; });
  expect((await weekdays.boundingBox())!.y).toBeCloseTo(top, 0);
  await page.screenshot({ path: '../.impeccable/review/calendar-expanded-month-dark.png', animations: 'disabled' });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
  await page.screenshot({ path: '../.impeccable/review/calendar-expanded-month-light.png', animations: 'disabled' });
  await page.setViewportSize({ width: 1024, height: 900 });
  await fits();
  expect((await calendar.boundingBox())!.height).toBe((await page.locator('.app-shell-floating').boundingBox())!.height);
  expect((await board.getByRole('button', { name: 'Today', exact: true }).boundingBox())!.x + (await board.getByRole('button', { name: 'Today', exact: true }).boundingBox())!.width).toBeLessThanOrEqual((await calendar.boundingBox())!.x);
  await page.screenshot({ path: '../.impeccable/review/calendar-expanded-small-desktop.png', animations: 'disabled' });
  await calendar.getByRole('button', { name: 'Week', exact: true }).click();
  await fits();
  await page.screenshot({ path: '../.impeccable/review/calendar-full-height-week-small-desktop.png', animations: 'disabled' });
  await calendar.getByRole('button', { name: 'Month', exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('navigation', { name: 'Workspace views' }).getByRole('button', { name: 'Calendar', exact: true }).click();
  await fits();
  await page.screenshot({ path: '../.impeccable/review/calendar-expanded-mobile.png', animations: 'disabled' });
  await page.setViewportSize({ width: 1440, height: 900 });
  await calendar.getByRole('button', { name: 'Day', exact: true }).click();
  expect((await calendar.boundingBox())!.width).toBe(compactWidth);
});

test('daily progress includes hidden completed cards and removal preserves tree tasks with retry', async ({ page, mockWorkspace, mockStrategistApi, nodeFactory }) => {
  const tasks = [
    nodeFactory.build({ id: 720, categoryId: 1, title: 'Already finished', plannedDate: '2026-10-02', status: 'done' }),
    nodeFactory.build({ id: 721, categoryId: 1, title: 'Remove this task', plannedDate: '2026-10-02' }),
    nodeFactory.build({ id: 722, categoryId: 1, parentId: 721, title: 'Remove its subtask' }),
    nodeFactory.build({ id: 723, categoryId: 1, title: 'Keep this task', plannedDate: '2026-10-02' }),
  ];
  const workspace = await mockWorkspace({ categories: [{ id: 1, name: 'Work', accentColor: '#b59358', iconSlug: 'briefcase', sortOrder: 0 }], nodesByCategoryId: { 1: tasks } });
  await mockStrategistApi();
  const calendar = await mockCalendarBlocks(page, { blocks: [{ id: 972, taskId: 721, categoryId: 1, title: 'Remove this task', categoryName: 'Work', categoryColor: '#b59358',
    startAt: '2026-10-02T14:00:00', endAt: '2026-10-02T15:00:00', variant: 'scheduled', completionStatus: 'not_started', isPinned: false, isProtected: false, isRecurring: false }] });
  let removals = 0;
  const hardDeletes: string[] = [];
  page.on('request', request => { if (request.method() === 'DELETE' && request.url().includes('/nodes/')) hardDeletes.push(request.url()); });
  await page.route('**/api/v1/calendar-blocks/remove-task-day', route => {
    removals += 1;
    if (removals === 1) return route.fulfill({ status: 503, json: { detail: 'Try again' } });
    tasks.find(task => task.id === 721)!.plannedDate = null;
    calendar.blocks.splice(0, 1);
    return route.fulfill({ json: { plannedDates: { '721': null }, removedBlockIds: [972], updatedBlocks: [], scheduledTaskIds: [] } });
  });
  const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
  const board = page.getByRole('region', { name: 'Weekly planning', exact: true });
  const day = board.locator('[data-week-day="2026-10-02"]');
  const progress = day.getByRole('progressbar');
  await expect(progress).toHaveAttribute('aria-valuetext', '1 of 3 tasks completed');
  await expect(day.locator('[data-week-task="720"]')).toHaveCount(0);
  await board.getByRole('checkbox', { name: 'Show completed', exact: true }).check();
  await expect(day.locator('[data-week-task="720"]')).toHaveCount(1);
  await expect(progress).toHaveAttribute('aria-valuetext', '1 of 3 tasks completed');
  await page.screenshot({ path: '../.impeccable/review/week-day-progress.png', animations: 'disabled' });
  const card = day.locator('[data-week-task="721"]');
  await card.click({ button: 'right', position: { x: 8, y: 8 } });
  await page.getByRole('menuitem', { name: 'Remove from this day', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Remove “Remove this task” from this day?', exact: true });
  await expect(dialog).toContainText('its subtasks');
  await dialog.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect(removals).toBe(0);
  await card.focus(); await page.keyboard.press('Shift+F10');
  await page.getByRole('menuitem', { name: 'Remove from this day', exact: true }).click();
  await dialog.getByRole('button', { name: 'Remove from this day', exact: true }).click();
  await expect(dialog.getByRole('alert')).toContainText('Could not remove');
  await expect(card).toHaveCount(1);
  await page.screenshot({ path: '../.impeccable/review/week-remove-retry.png', animations: 'disabled' });
  await dialog.getByRole('button', { name: 'Remove from this day', exact: true }).click();
  await expect(card).toHaveCount(0);
  await expect(page.locator('[data-node-id="721"]')).toHaveCount(1);
  await expect(page.locator('[data-node-id="722"]')).toHaveCount(1);
  await expect(page.locator('[data-calendar-block-id="972"]')).toHaveCount(0);
  await expect(progress).toHaveAttribute('aria-valuetext', '1 of 2 tasks completed');
  expect(removals).toBe(2);
  expect(hardDeletes).toEqual([]);
  await page.reload();
  await expect(page.locator('[data-week-day="2026-10-02"] [data-week-task="721"]')).toHaveCount(0);
  await expect(page.locator('[data-node-id="721"]')).toHaveCount(1);
});
