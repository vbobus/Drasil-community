import { test, expect } from '../support/fixtures/strategist.fixture';

for (const view of ['Day', 'Week', 'Month']) {
  test(`scheduled block moves to an untimed day and persists in ${view} view`, async ({ page, mockStrategistApi }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.clock.setFixedTime(new Date('2026-10-02T12:00:00'));
    await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: { authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false } }));
    await mockStrategistApi();
    const category = await (await page.request.post('/api/v1/categories', { data: { name: `Untimed ${view}`, accentColor: '#b59358', iconSlug: 'briefcase' } })).json();
    const task = await (await page.request.post('/api/v1/nodes', { data: { categoryId: category.id, title: `Move appointment ${view}`, deadlineAt: '2026-10-07', timeEstimateMinutes: 60 } })).json();
    const blockResponse = await page.request.post('/api/v1/calendar-blocks', { data: { taskId: task.id, categoryId: category.id, startAt: '2026-10-02T12:00:00', endAt: '2026-10-02T13:00:00' } });
    expect(blockResponse.ok()).toBeTruthy();
    const block = await blockResponse.json();
    await page.goto('/');
    await page.getByRole('button', { name: `Untimed ${view} workspace`, exact: true }).click();
    await page.getByRole('button', { name: view, exact: true }).click();
    const targetDay = view === 'Day' ? '2026-10-02' : '2026-10-03';
    const holder = page.locator(`[data-day-holder="${targetDay}"]`);
    const scheduled = page.locator(`[data-calendar-block-id="${block.id}"]`).first();
    let requests = 0;
    await page.route(`**/api/v1/calendar-blocks/${block.id}/unschedule`, route => {
      requests++;
      if (view === 'Day' && requests === 1) return route.fulfill({ status: 503, json: { detail: 'Temporary save failure' } });
      return route.continue();
    });
    const drag = async () => {
      await scheduled.scrollIntoViewIfNeeded();
      const source = (await scheduled.boundingBox())!;
      const target = (await holder.boundingBox())!;
      await page.mouse.move(source.x + source.width / 2, source.y + Math.min(18, source.height / 2));
      await page.mouse.down();
      await page.mouse.move(target.x + target.width / 2, target.y + target.height - 12, { steps: 15 });
      await expect(holder).toHaveClass(/is-drop-target/);
      await page.mouse.up();
    };
    if (view === 'Day') {
      await drag();
      await expect(page.getByText('Temporary save failure', { exact: false })).toBeVisible();
      await expect(scheduled).toBeVisible();
      const stillSaved = await (await page.request.get(`/api/v1/nodes?categoryId=${category.id}`)).json();
      expect(stillSaved.find((item: { id: number }) => item.id === task.id).plannedDate).toBeNull();
    }
    await drag();
    await expect(scheduled).toHaveCount(0);
    await expect(holder.getByText(task.title, { exact: true })).toBeVisible();
    await page.reload();
    await page.getByRole('button', { name: `Untimed ${view} workspace`, exact: true }).click();
    await page.getByRole('button', { name: view, exact: true }).click();
    await expect(holder.getByText(task.title, { exact: true })).toBeVisible();
    await expect(scheduled).toHaveCount(0);
    const tasks = await (await page.request.get(`/api/v1/nodes?categoryId=${category.id}`)).json();
    expect(tasks.find((item: { id: number }) => item.id === task.id)).toMatchObject({ plannedDate: targetDay, deadlineAt: '2026-10-07', timeEstimateMinutes: 60 });
    if (view !== 'Month') {
      const chip = holder.locator('.day-task-title').filter({ hasText: task.title });
      const source = (await chip.boundingBox())!;
      const agenda = (await page.locator('.calendar-agenda').boundingBox())!;
      const timeSurface = page.locator(`[data-calendar-time-surface="true"][data-calendar-date="${targetDay}"]`);
      const target = (await timeSurface.boundingBox())!;
      await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2);
      await page.mouse.down();
      await page.mouse.move(source.x + source.width / 2 + 10, source.y + source.height / 2, { steps: 3 });
      await page.mouse.move(target.x + target.width / 2, agenda.y + agenda.height / 2, { steps: 15 });
      await page.mouse.move(target.x + target.width / 2 + 1, agenda.y + agenda.height / 2);
      await page.mouse.up();
      await expect(holder.getByText(task.title, { exact: true })).toHaveCount(0);
      await expect(page.locator('[data-calendar-block-id]').filter({ hasText: task.title })).toHaveCount(1);
      await page.reload();
      await page.getByRole('button', { name: `Untimed ${view} workspace`, exact: true }).click();
      await page.getByRole('button', { name: view, exact: true }).click();
      await expect(holder.getByText(task.title, { exact: true })).toHaveCount(0);
      await expect(page.locator('[data-calendar-block-id]').filter({ hasText: task.title })).toHaveCount(1);
    }
  });
}
