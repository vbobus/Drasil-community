import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks } from '../support/helpers/network';

test.describe('Global scheduled task marker', () => {
  test.beforeEach(async ({ page }) => {
    await page.clock.setFixedTime(new Date('2026-06-04T12:00:00-07:00'));
  });

  test('stays visible while the calendar is showing a different day', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 510,
      name: 'Work',
      accentColor: '#2563eb',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const scheduledTask = nodeFactory.build({
      id: 5101,
      categoryId: work.id,
      title: 'Scheduled next week',
      timeEstimateMinutes: 60,
      sortOrder: 0,
    });

    await mockCalendarBlocks(page, {
      blocks: [
        {
          id: 9510,
          taskId: scheduledTask.id,
          categoryId: work.id,
          title: scheduledTask.title,
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: '2026-06-10T09:00:00-07:00',
          endAt: '2026-06-10T10:00:00-07:00',
          variant: 'scheduled',
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
        },
      ],
    });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [scheduledTask] },
    });

    await page.goto('/');
    await workspace.waitForBoot(work.id);

    const tree = page.getByRole('tree', { name: /work task nodes/i });
    const taskRow = tree.locator('.node-card-wrapper').filter({
      hasText: scheduledTask.title,
    });
    const scheduledMarker = taskRow.locator('.node-state-icon.scheduled');
    const calendar = page.getByRole('region', { name: /shared calendar/i });

    await expect(calendar.locator('.variant-scheduled')).toHaveCount(0);
    await expect(scheduledMarker).toHaveAttribute('title', 'Scheduled');

    await calendar.getByRole('button', { name: 'Next calendar range' }).click();

    await expect(calendar.locator('.variant-scheduled')).toHaveCount(0);
    await expect(scheduledMarker).toHaveAttribute('title', 'Scheduled');
  });
});
