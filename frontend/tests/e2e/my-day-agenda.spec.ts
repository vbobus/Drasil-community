import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks } from '../support/helpers/network';

function startOfToday() {
  const date = new Date();
  date.setHours(0, 0, 0, 0);
  return date;
}

function atTime(date: Date, hours: number, minutes = 0) {
  const nextDate = new Date(date);
  nextDate.setHours(hours, minutes, 0, 0);
  return nextDate;
}

function addDays(date: Date, days: number) {
  const nextDate = new Date(date);
  nextDate.setDate(nextDate.getDate() + days);
  return nextDate;
}

function toLocalIso(date: Date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, '0');
  const day = String(date.getDate()).padStart(2, '0');
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  return `${year}-${month}-${day}T${hours}:${minutes}:00`;
}

test.describe('My Day agenda', () => {
  test('opens from My Day and presents the accepted daily schedule in order', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 801,
      name: 'Work',
      accentColor: '#315fd5',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const morningTask = nodeFactory.build({
      id: 8101,
      categoryId: work.id,
      title: 'Reply to project lead',
      emoji: '✉️',
      timeEstimateMinutes: 30,
      sortOrder: 0,
    });
    const deepWorkTask = nodeFactory.build({
      id: 8102,
      categoryId: work.id,
      title: 'Finish case study',
      emoji: '🧠',
      timeEstimateMinutes: 60,
      status: 'done',
      sortOrder: 1,
    });
    const overnightTask = nodeFactory.build({
      id: 8103,
      categoryId: work.id,
      title: 'Overnight sync',
      emoji: '🌙',
      timeEstimateMinutes: 60,
      sortOrder: 2,
    });
    const tomorrowTask = nodeFactory.build({
      id: 8104,
      categoryId: work.id,
      title: 'Tomorrow only',
      timeEstimateMinutes: 30,
      sortOrder: 3,
    });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: {
        [work.id]: [morningTask, deepWorkTask, overnightTask, tomorrowTask],
      },
    });

    const today = startOfToday();
    const yesterday = addDays(today, -1);
    const tomorrow = addDays(today, 1);
    await mockCalendarBlocks(page, {
      blocks: [
        {
          id: 9103,
          taskId: overnightTask.id,
          categoryId: work.id,
          title: overnightTask.title,
          emoji: overnightTask.emoji,
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: toLocalIso(atTime(yesterday, 23, 30)),
          endAt: toLocalIso(atTime(today, 0, 30)),
          variant: 'protected',
          completionStatus: 'not_started',
          isPinned: true,
          isProtected: true,
          isRecurring: false,
        },
        {
          id: 9102,
          taskId: deepWorkTask.id,
          taskStatus: 'done',
          categoryId: work.id,
          title: deepWorkTask.title,
          emoji: deepWorkTask.emoji,
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: toLocalIso(atTime(today, 13)),
          endAt: toLocalIso(atTime(today, 14)),
          variant: 'scheduled',
          completionStatus: 'completed',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
        },
        {
          id: 9101,
          taskId: morningTask.id,
          categoryId: work.id,
          title: morningTask.title,
          emoji: morningTask.emoji,
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: toLocalIso(atTime(today, 9)),
          endAt: toLocalIso(atTime(today, 9, 30)),
          variant: 'scheduled',
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: true,
        },
        {
          id: 9104,
          taskId: tomorrowTask.id,
          categoryId: work.id,
          title: tomorrowTask.title,
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: toLocalIso(atTime(tomorrow, 10)),
          endAt: toLocalIso(atTime(tomorrow, 10, 30)),
          variant: 'scheduled',
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
        },
      ],
    });

    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;
    await page.getByRole('button', { name: 'My Day' }).click();

    const agenda = page.locator('.my-day-agenda');
    await expect(agenda).toBeVisible();
    await expect(agenda.locator('.my-day-header h1')).toHaveCSS(
      'color',
      'rgb(237, 243, 255)',
    );
    await expect(agenda.locator('.my-day-subtitle')).toHaveCSS(
      'color',
      'rgb(184, 197, 218)',
    );
    await expect(agenda.locator('.my-day-overview-copy strong')).toHaveCSS(
      'color',
      'rgb(237, 243, 255)',
    );
    await expect(agenda.locator('.my-day-section-heading h2').first()).toHaveCSS(
      'color',
      'rgb(237, 243, 255)',
    );
    await expect(page.getByRole('heading', { level: 1 })).toContainText(
      new Intl.DateTimeFormat('en-US', { weekday: 'long' }).format(today),
    );
    await expect(page.getByText('1 of 3 complete')).toBeVisible();
    await expect(page.getByText('2h scheduled')).toBeVisible();
    await expect(page.getByText('Tomorrow only')).toHaveCount(0);

    const cards = page.locator('.my-day-agenda-card');
    await expect(cards).toHaveCount(3);
    await expect(cards.nth(0)).toContainText('Overnight sync');
    await expect(cards.nth(1)).toContainText('Reply to project lead');
    await expect(cards.nth(2)).toContainText('Finish case study');
    await expect(page.getByText(/Until 12:30 AM/i)).toBeVisible();
    await expect(page.getByText('Repeats', { exact: true })).toBeVisible();
    await page.screenshot({
      path: '../.tmp/theme-my-day-dark.png',
      fullPage: true,
    });
    await page.getByRole('button', { name: 'Open calendar' }).first().click();
    await expect(page.getByTestId('app-shell')).toBeVisible();
    await expect(page.getByRole('button', { name: 'My Day' })).not.toHaveAttribute('aria-current', 'page');
  });

  test('allows completing a later task before earlier tasks across workspaces', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 821,
      name: 'Work',
      accentColor: '#315fd5',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const personal = categoryFactory.build({
      id: 822,
      name: 'Personal',
      accentColor: '#8b5cf6',
      iconSlug: 'heart',
      sortOrder: 1,
    });
    const firstTask = nodeFactory.build({
      id: 8201,
      categoryId: work.id,
      title: 'First scheduled task',
      sortOrder: 0,
    });
    const secondTask = nodeFactory.build({
      id: 8202,
      categoryId: work.id,
      title: 'Second scheduled task',
      sortOrder: 1,
    });
    const laterTask = nodeFactory.build({
      id: 8203,
      categoryId: personal.id,
      title: 'Later personal task',
      sortOrder: 0,
    });
    const workspace = await mockWorkspace({
      categories: [work, personal],
      nodesByCategoryId: {
        [work.id]: [firstTask, secondTask],
        [personal.id]: [laterTask],
      },
    });
    const updatedTaskIds: number[] = [];
    await page.route('**/api/v1/nodes/*', async (route) => {
      if (route.request().method() !== 'PATCH') {
        await route.continue();
        return;
      }
      const taskId = Number(new URL(route.request().url()).pathname.split('/').at(-1));
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      updatedTaskIds.push(taskId);
      const source = taskId === laterTask.id ? laterTask : taskId === secondTask.id ? secondTask : firstTask;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...source, ...payload }),
      });
    });

    const today = startOfToday();
    await mockCalendarBlocks(page, {
      blocks: [
        [firstTask, work, 9],
        [secondTask, work, 11],
        [laterTask, personal, 15],
      ].map(([task, category, hour], index) => {
        const typedTask = task as typeof firstTask;
        const typedCategory = category as typeof work;
        const typedHour = hour as number;
        return {
          id: 9200 + index,
          taskId: typedTask.id,
          categoryId: typedCategory.id,
          title: typedTask.title,
          emoji: typedTask.emoji,
          categoryName: typedCategory.name,
          categoryColor: typedCategory.accentColor,
          startAt: toLocalIso(atTime(today, typedHour)),
          endAt: toLocalIso(atTime(today, typedHour, 30)),
          variant: 'scheduled' as const,
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
        };
      }),
    });

    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;
    await page.getByRole('button', { name: 'My Day' }).click();

    const firstCheckbox = page.getByRole('button', { name: 'Mark complete: First scheduled task' });
    const laterCheckbox = page.getByRole('button', { name: 'Mark complete: Later personal task' });
    await laterCheckbox.click();

    await expect(page.getByRole('button', { name: 'Mark incomplete: Later personal task' }))
      .toHaveAttribute('aria-pressed', 'true');
    await expect(firstCheckbox).toHaveAttribute('aria-pressed', 'false');
    await expect(page.getByText('1 of 3 complete')).toBeVisible();
    await expect.poll(() => updatedTaskIds).toContain(laterTask.id);
    expect(updatedTaskIds).not.toContain(firstTask.id);
    expect(updatedTaskIds).not.toContain(secondTask.id);
  });
});
