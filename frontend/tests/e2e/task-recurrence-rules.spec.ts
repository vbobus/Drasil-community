import { expect, test } from '../support/fixtures';

test.describe('Story 4.1 - task recurrence rules', () => {
  test('configures, renders, edits, and stops recurrence without changing task type', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const category = categoryFactory.build({
      name: 'Personal Growth',
      accentColor: '#8b5cf6',
      iconSlug: 'school',
      sortOrder: 0,
    });
    const task = nodeFactory.build({
      id: 7401,
      categoryId: category.id,
      title: 'Review the week',
      nodeType: 'task',
      timeEstimateMinutes: 45,
      sortOrder: 0,
    });
    const nodesByCategoryId = { [category.id]: [task] };
    const putPayloads: Record<string, unknown>[] = [];
    let stoppedRevision: string | null = null;

    await page.route(`**/api/v1/recurrence-rules/${task.id}**`, async (route) => {
      const method = route.request().method();
      const requestUrl = new URL(route.request().url());
      if (method === 'PUT') {
        const putPayload = route.request().postDataJSON() as Record<string, unknown>;
        putPayloads.push(putPayload);
        const rule = {
          id: 'series-review-week',
          revision: 1,
          taskId: task.id,
          categoryId: category.id,
          frequency: String(putPayload.frequency),
          interval: 1,
          daysOfWeek: putPayload.daysOfWeek,
          startDate: putPayload.startDate,
          endDate: putPayload.endDate,
          preferredStartTime: putPayload.preferredStartTime,
          durationMinutes: putPayload.durationMinutes,
          timezone: putPayload.timezone,
          status: 'active',
        };
        Object.assign(task, {
          isRecurring: true,
          recurrenceRule: rule,
        });
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify(rule),
        });
        return;
      }
      if (method === 'DELETE') {
        stoppedRevision = requestUrl.searchParams.get('expectedRevision');
        Object.assign(task, {
          isRecurring: false,
          recurrenceRule: null,
        });
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            id: 'series-review-week',
            revision: 2,
            taskId: task.id,
            categoryId: category.id,
            frequency: 'weekly',
            interval: 1,
            daysOfWeek: ['monday', 'friday'],
            startDate: '2026-07-24',
            endDate: null,
            preferredStartTime: '09:30',
            durationMinutes: 45,
            timezone: 'America/Vancouver',
            status: 'inactive',
          }),
        });
        return;
      }
      await route.fulfill({ status: 405 });
    });

    const workspace = await mockWorkspace({
      categories: [category],
      nodesByCategoryId,
    });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;

    const tree = page.getByRole('tree', { name: /personal growth task nodes/i });
    const row = tree.locator('.node-card-wrapper').filter({
      has: page.getByText(task.title, { exact: true }),
    });
    await row.locator('.node-card-row').click({ button: 'right' });
    const recurringMenuItem = page.getByRole('menuitem', { name: 'Mark as recurring' });
    await expect(recurringMenuItem.locator('.lucide-refresh-cw')).toBeVisible();
    await recurringMenuItem.click();

    const dialog = page.getByRole('dialog', { name: `Make ${task.title} recurring` });
    await expect(dialog).toContainText(
      'The task stays in its current workspace and keeps the same task type.',
    );
    const ensureWeekdaySelected = async (weekday: string) => {
      const button = dialog.getByRole('button', { name: `Repeat on ${weekday}` });
      if (await button.getAttribute('aria-pressed') !== 'true') {
        await button.click();
      }
      await expect(button).toHaveAttribute('aria-pressed', 'true');
    };
    await ensureWeekdaySelected('monday');
    await ensureWeekdaySelected('friday');
    await dialog.getByLabel('Starts').fill('2026-07-24');
    await dialog.getByLabel('Preferred time').fill('09:30');
    await dialog.getByLabel('Duration').fill('45');
    await dialog.getByLabel('Timezone').fill('America/Vancouver');

    const putRequest = page.waitForRequest((request) => (
      request.method() === 'PUT'
      && request.url().includes(`/api/v1/recurrence-rules/${task.id}`)
    ));
    await dialog.getByRole('button', { name: 'Make recurring' }).click();
    await putRequest;

    const putPayload = putPayloads[0];
    expect(putPayload).toMatchObject({
      frequency: 'weekly',
      startDate: '2026-07-24',
      endDate: null,
      preferredStartTime: '09:30',
      durationMinutes: 45,
      timezone: 'America/Vancouver',
    });
    expect((putPayload.daysOfWeek as string[])).toEqual(
      expect.arrayContaining(['monday', 'friday']),
    );
    expect(task.nodeType).toBe('task');
    await expect(row.locator('.node-state-icon.recurring')).toBeVisible();
    await expect(row.getByLabel('Edit recurrence rule').locator('.lucide-refresh-cw')).toHaveCount(1);

    await row.locator('.node-card').focus();
    await page.keyboard.press('Shift+F10');
    await page.getByRole('menuitem', { name: 'Edit recurrence' }).click();
    const editDialog = page.getByRole('dialog', { name: `Edit recurrence for ${task.title}` });
    await expect(editDialog.getByLabel('Preferred time')).toHaveValue('09:30');
    await expect(editDialog.getByText(
      'Calendar blocks already accepted or completed stay in your history.',
      { exact: false },
    )).toBeVisible();
    const deleteRequest = page.waitForRequest((request) => (
      request.method() === 'DELETE'
      && request.url().includes(`/api/v1/recurrence-rules/${task.id}`)
    ));
    await editDialog.getByRole('button', { name: 'Stop recurring' }).click();
    await deleteRequest;

    expect(stoppedRevision).toBe('1');
    expect(task.nodeType).toBe('task');
    await expect(row.locator('.node-state-icon.recurring')).toHaveCount(0);
    await expect(row.getByLabel('Set recurrence rule').locator('.lucide-refresh-cw')).toHaveCount(1);
  });
});
