import { test, expect } from '../support/fixtures/strategist.fixture';

for (const view of ['Day', 'Week', 'Month']) {
  test(`${view} calendar move carries the planned day without leaving an untimed copy`, async ({ page, mockStrategistApi }) => {
    await page.setViewportSize({ width: 1600, height: 1000 });
    await page.clock.setFixedTime(new Date('2026-10-02T12:00:00'));
    await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
      authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
    } }));
    await mockStrategistApi();
    const category = await (await page.request.post('/api/v1/categories', { data: {
      name: `Move ${view}`, accentColor: '#b59358', iconSlug: 'briefcase',
    } })).json();
    const task = await (await page.request.post('/api/v1/nodes', { data: {
      categoryId: category.id, title: `Appointment ${view}`, plannedDate: '2026-10-02',
      deadlineAt: '2026-10-07', timeEstimateMinutes: 60,
    } })).json();
    const block = await (await page.request.post('/api/v1/calendar-blocks', { data: {
      taskId: task.id, categoryId: category.id,
      startAt: view === 'Day' ? '2026-10-02T23:00:00' : '2026-10-02T12:00:00',
      endAt: view === 'Day' ? '2026-10-02T23:30:00' : '2026-10-02T13:00:00',
    } })).json();
    await page.goto('/');
    await page.getByRole('button', { name: `Move ${view} workspace`, exact: true }).click();
    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await calendar.getByRole('button', { name: view, exact: true }).click();
    const source = calendar.locator(`[data-calendar-block-id="${block.id}"][data-calendar-segment-date="2026-10-02"]`);
    const destination = calendar.locator(`[data-calendar-block-id="${block.id}"][data-calendar-segment-date="2026-10-03"]`);
    const oldHolder = calendar.locator('[data-day-holder="2026-10-02"]');
    const oldCard = page.locator(`[data-week-day="2026-10-02"] [data-week-task="${task.id}"]`);
    const newCard = page.locator(`[data-week-day="2026-10-03"] [data-week-task="${task.id}"]`);
    await expect(source).toHaveCount(1);
    await expect(oldCard).toHaveCount(1);

    const drag = async () => {
      await source.scrollIntoViewIfNeeded();
      if (view === 'Day') await calendar.locator('.calendar-agenda').evaluate(element => { element.scrollTop = element.scrollHeight; });
      const from = (await source.boundingBox())!;
      const to = (await calendar.locator(view === 'Day' ? '.calendar-hour-grid' : view === 'Week'
        ? '[data-calendar-time-surface="true"][data-calendar-date="2026-10-03"]'
        : '.calendar-month-cell[data-calendar-date="2026-10-03"]').boundingBox())!;
      const y = from.y + Math.min(18, from.height / 2);
      await page.mouse.move(from.x + from.width * .35, y);
      await page.mouse.down();
      await page.mouse.move(to.x + to.width / 2, view === 'Day' ? to.y + to.height + 16
        : view === 'Week' ? y : to.y + to.height - 14, { steps: 15 });
      await page.mouse.up();
    };
    if (view === 'Week') {
      await page.route(`**/api/v1/calendar-blocks/${block.id}`, route => route.fulfill({
        status: 503, json: { detail: 'Temporary move failure' },
      }));
      await drag();
      await expect(page.getByText('Temporary move failure', { exact: false })).toBeVisible();
      await expect(source).toHaveCount(1);
      await expect(oldCard).toHaveCount(1);
      await expect(newCard).toHaveCount(0);
      await expect(oldHolder.getByText(task.title, { exact: true })).toHaveCount(0);
      await page.unroute(`**/api/v1/calendar-blocks/${block.id}`);
    }

    let release = () => {};
    const gate = new Promise<void>(resolve => { release = resolve; });
    await page.route(`**/api/v1/calendar-blocks/${block.id}`, async route => {
      await gate;
      await route.continue();
    });
    try {
      const saved = page.waitForResponse(response => response.request().method() === 'PATCH'
        && response.url().endsWith(`/api/v1/calendar-blocks/${block.id}`));
      await drag();
      if (view !== 'Day') await expect(destination).toHaveCount(1);
      await expect(oldHolder.getByText(task.title, { exact: true })).toHaveCount(0);
      await expect(oldCard).toHaveCount(0);
      await expect(newCard).toHaveCount(1);
      release();
      expect((await saved).ok()).toBe(true);
    } finally { release(); }
    const nodes = await (await page.request.get(`/api/v1/nodes?categoryId=${category.id}`)).json();
    expect(nodes.filter((node: { id: number }) => node.id === task.id)).toHaveLength(1);
    expect(nodes.find((node: { id: number }) => node.id === task.id)).toMatchObject({
      plannedDate: '2026-10-03', deadlineAt: '2026-10-07',
    });
    await page.reload();
    await page.getByRole('button', { name: `Move ${view} workspace`, exact: true }).click();
    await calendar.getByRole('button', { name: view, exact: true }).click();
    if (view === 'Day') await calendar.getByRole('button', { name: 'Next calendar range' }).click();
    await expect(destination).toHaveCount(1);
    await expect(source).toHaveCount(0);
    await expect(oldHolder.getByText(task.title, { exact: true })).toHaveCount(0);
    await expect(calendar.locator('[data-day-holder="2026-10-03"]').getByText(task.title, { exact: true })).toHaveCount(0);
    await expect(oldCard).toHaveCount(0);
    await expect(newCard).toHaveCount(1);
  });
}
