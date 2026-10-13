import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks } from '../support/helpers/network';
import type { MockCalendarBlock } from '../support/helpers/network';

function calendarBlock(
  overrides: Pick<MockCalendarBlock, 'id' | 'taskId' | 'title' | 'startAt' | 'endAt'>,
): MockCalendarBlock {
  return {
    categoryId: 610,
    emoji: 'C',
    categoryName: 'Work',
    categoryColor: '#6366f1',
    variant: 'scheduled',
    completionStatus: 'not_started',
    isPinned: false,
    isProtected: false,
    isRecurring: false,
    hierarchyDepth: 0,
    renderedChildTaskIds: [],
    ...overrides,
  };
}

test.describe('Short calendar event rendering', () => {
  test.use({ timezoneId: 'America/Vancouver' });

  test.beforeEach(async ({ page }) => {
    await page.clock.setFixedTime(new Date('2026-08-13T06:00:00-07:00'));
  });

  test('keeps 5- and 15-minute events readable without enlarging 30-minute events', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 610,
      name: 'Work',
      accentColor: '#6366f1',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const tasks = [
      nodeFactory.build({ id: 6101, categoryId: work.id, title: 'Five minute standup' }),
      nodeFactory.build({ id: 6102, categoryId: work.id, title: 'Fifteen minute review' }),
      nodeFactory.build({ id: 6103, categoryId: work.id, title: 'Thirty minute planning' }),
    ];

    await mockCalendarBlocks(page, {
      blocks: [
        calendarBlock({
          id: 9601,
          taskId: tasks[0].id,
          title: tasks[0].title,
          startAt: '2026-08-13T07:00:00-07:00',
          endAt: '2026-08-13T07:05:00-07:00',
        }),
        calendarBlock({
          id: 9602,
          taskId: tasks[1].id,
          title: tasks[1].title,
          startAt: '2026-08-13T07:15:00-07:00',
          endAt: '2026-08-13T07:30:00-07:00',
        }),
        calendarBlock({
          id: 9603,
          taskId: tasks[2].id,
          title: tasks[2].title,
          startAt: '2026-08-13T08:00:00-07:00',
          endAt: '2026-08-13T08:30:00-07:00',
        }),
      ],
    });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: tasks },
    });

    const boot = Promise.all([
      workspace.waitForBoot(work.id),
      page.waitForResponse((response) =>
        response.request().method() === 'GET' &&
        response.url().includes('/api/v1/calendar-blocks'),
      ),
    ]);
    await page.goto('/');
    await boot;

    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await calendar.getByLabel('Calendar detail', { exact: true }).click();
    await calendar.getByLabel('Zoom / Detail').fill('1');

    const fiveMinute = calendar.locator('[data-calendar-block-id="9601"]');
    const fifteenMinute = calendar.locator('[data-calendar-block-id="9602"]');
    const thirtyMinute = calendar.locator('[data-calendar-block-id="9603"]');

    await expect(fiveMinute).toHaveClass(/is-short-block/);
    await expect(fifteenMinute).toHaveClass(/is-short-block/);
    await expect(thirtyMinute).not.toHaveClass(/is-short-block/);
    await expect(fiveMinute).toHaveAttribute('data-duration-minutes', '5');
    await expect(fifteenMinute).toHaveAttribute('data-duration-minutes', '15');
    await expect(thirtyMinute).toHaveAttribute('data-duration-minutes', '30');

    await expect(fiveMinute).toContainText('Five minute standup, 7:00 AM');
    await expect(fiveMinute).not.toContainText('7:05 AM');
    await expect(fifteenMinute).toContainText('Fifteen minute review, 7:15 AM');
    await expect(thirtyMinute).toContainText('8:00 AM - 8:30 AM');

    const [fiveMetrics, fifteenMetrics, thirtyMetrics] = await Promise.all(
      [fiveMinute, fifteenMinute, thirtyMinute].map((block) =>
        block.evaluate((element) => {
          const blockRect = element.getBoundingClientRect();
          const titleRect = element.querySelector('.calendar-block-title')?.getBoundingClientRect();
          return {
            x: blockRect.x,
            height: blockRect.height,
            titleHeight: titleRect?.height ?? 0,
            titleTop: titleRect?.top ?? 0,
            titleBottom: titleRect?.bottom ?? 0,
            blockTop: blockRect.top,
            blockBottom: blockRect.bottom,
          };
        }),
      ),
    );

    for (const metrics of [fiveMetrics, fifteenMetrics]) {
      expect(metrics.height).toBeGreaterThanOrEqual(13.5);
      expect(metrics.height).toBeLessThan(15);
      expect(metrics.titleHeight).toBeGreaterThan(0);
      expect(metrics.titleTop).toBeGreaterThanOrEqual(metrics.blockTop);
      expect(metrics.titleBottom).toBeLessThanOrEqual(metrics.blockBottom);
    }
    expect(thirtyMetrics.height).toBeGreaterThan(fifteenMetrics.height);
    expect(fiveMetrics.x).not.toBe(fifteenMetrics.x);
  });
});

// Calendar fixtures run without an authenticated live backend.
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
});
