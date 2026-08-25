import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks } from '../support/helpers/network';

test.describe('Workspace tree filters', () => {
  test.use({ timezoneId: 'America/Vancouver' });

  test.beforeEach(async ({ page }) => {
    await page.clock.setFixedTime(new Date('2026-08-14T09:00:00-07:00'));
  });

  test('keeps the shelf usable and persists independent category filter and sort choices', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 710,
      name: 'Work',
      accentColor: '#516ff2',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const health = categoryFactory.build({
      id: 711,
      name: 'Health',
      accentColor: '#16a085',
      iconSlug: 'heart',
      sortOrder: 1,
    });
    const scheduledTask = nodeFactory.build({
      id: 7101,
      categoryId: work.id,
      title: 'Scheduled client review',
      deadlineAt: '2026-08-15',
      priority: 'low',
      sortOrder: 0,
    });
    const completedTask = nodeFactory.build({
      id: 7102,
      categoryId: work.id,
      title: 'Completed project brief',
      status: 'done',
      deadlineAt: '2026-08-14',
      priority: 'high',
      sortOrder: 1,
    });
    const openTask = nodeFactory.build({
      id: 7103,
      categoryId: work.id,
      title: 'Open research task',
      sortOrder: 2,
    });
    const recurringTask = nodeFactory.build({
      id: 7111,
      categoryId: health.id,
      title: 'Morning mobility',
      isRecurring: true,
      recurrenceRule: {
        id: 'recurrence-health-mobility',
        revision: 1,
        frequency: 'weekly',
        interval: 1,
        daysOfWeek: ['monday'],
        startDate: '2026-08-14',
        endDate: null,
        preferredStartTime: '09:00',
        durationMinutes: 15,
        timezone: 'America/Vancouver',
      },
      priority: 'medium',
      sortOrder: 0,
    });
    const healthTask = nodeFactory.build({
      id: 7112,
      categoryId: health.id,
      title: 'Order vitamins',
      priority: 'high',
      sortOrder: 1,
    });

    await mockCalendarBlocks(page, {
      blocks: [
        {
          id: 9710,
          taskId: scheduledTask.id,
          categoryId: work.id,
          title: scheduledTask.title,
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: '2026-08-14T13:00:00-07:00',
          endAt: '2026-08-14T13:30:00-07:00',
          variant: 'scheduled',
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
        },
      ],
    });
    const workspace = await mockWorkspace({
      categories: [work, health],
      nodesByCategoryId: {
        [work.id]: [scheduledTask, completedTask, openTask],
        [health.id]: [recurringTask, healthTask],
      },
    });

    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    const filterButton = page.getByRole('button', { name: /Filter tasks/ });
    await filterButton.click();
    const filterShelf = page.getByRole('region', { name: 'Task filters' });
    const filterGeometry = await filterShelf.evaluate((shelf) => {
      const shelfBounds = shelf.getBoundingClientRect();
      const surfaceBounds = shelf.closest('.planning-tree-surface')?.getBoundingClientRect();
      const finalOption = shelf.querySelector(
        '[data-filter-facet="schedule"] label:last-child',
      );
      const optionBounds = finalOption?.getBoundingClientRect();
      const hitTarget = optionBounds
        ? shelf.ownerDocument.elementFromPoint(
            optionBounds.left + optionBounds.width / 2,
            optionBounds.top + optionBounds.height / 2,
          )
        : null;
      return {
        shelfRight: shelfBounds.right,
        surfaceRight: surfaceBounds?.right ?? 0,
        finalOptionReceivesPointer: finalOption?.contains(hitTarget) ?? false,
      };
    });
    expect(filterGeometry.shelfRight).toBeLessThanOrEqual(filterGeometry.surfaceRight);
    expect(filterGeometry.finalOptionReceivesPointer).toBe(true);

    const workTree = page.getByRole('tree', { name: 'Work task nodes' });
    const statusFilter = filterShelf.getByRole('group', { name: 'Status filter' });
    const scheduleFilter = filterShelf.getByRole('group', { name: 'Schedule filter' });

    await statusFilter.getByRole('checkbox', { name: 'Open', exact: true }).click();
    await expect(workTree.getByRole('treeitem', { name: /Completed project brief/ })).toBeVisible();
    await expect(workTree.getByRole('treeitem', { name: /Scheduled client review/ })).toHaveCount(0);
    await expect(workTree.getByRole('treeitem', { name: /Open research task/ })).toHaveCount(0);

    await statusFilter.getByRole('checkbox', { name: 'Open', exact: true }).click();
    await scheduleFilter.getByRole('checkbox', { name: 'Unscheduled', exact: true }).click();
    await expect(workTree.getByRole('treeitem', { name: /Scheduled client review/ })).toBeVisible();
    await expect(workTree.getByRole('treeitem', { name: /Completed project brief/ })).toHaveCount(0);
    await expect(workTree.locator('.node-state-icon.scheduled')).toHaveAttribute(
      'title',
      'Scheduled',
    );

    await scheduleFilter.getByRole('checkbox', { name: 'Scheduled', exact: true }).click();
    await scheduleFilter.getByRole('checkbox', { name: 'Unscheduled', exact: true }).click();
    await expect(workTree.getByRole('treeitem', { name: /Scheduled client review/ })).toHaveCount(0);
    await expect(workTree.getByRole('treeitem', { name: /Completed project brief/ })).toBeVisible();
    await expect(workTree.getByRole('treeitem', { name: /Open research task/ })).toBeVisible();

    await scheduleFilter.getByRole('checkbox', { name: 'Scheduled', exact: true }).click();
    await expect(workTree.getByRole('treeitem')).toHaveCount(3);

    await scheduleFilter.getByRole('checkbox', { name: 'Scheduled', exact: true }).click();
    await scheduleFilter.getByRole('checkbox', { name: 'Unscheduled', exact: true }).click();
    await expect(workTree.getByRole('treeitem')).toHaveCount(0);
    await expect(workTree.getByText('No matching tasks')).toBeVisible();

    await statusFilter.getByRole('checkbox', { name: 'Closed', exact: true }).click();
    await scheduleFilter.getByRole('checkbox', { name: 'Unscheduled', exact: true }).click();
    await expect(workTree.getByRole('treeitem', { name: /Open research task/ })).toBeVisible();
    await expect(workTree.getByRole('treeitem', { name: /Scheduled client review/ })).toHaveCount(0);
    await expect(workTree.getByRole('treeitem', { name: /Completed project brief/ })).toHaveCount(0);
    await filterButton.click();
    const appliedFilters = page.getByRole('group', { name: 'Applied task filters' });
    await expect(appliedFilters.getByRole('button', { name: 'Clear Status filter: Open' })).toBeVisible();
    await expect(
      appliedFilters.getByRole('button', { name: 'Clear Schedule filter: Unscheduled' }),
    ).toBeVisible();

    await page.getByRole('button', { name: /Sort tasks/ }).click();
    await page.getByRole('menuitemradio', { name: 'Deadline' }).click();

    const healthLoad = page.waitForResponse((response) =>
      response.request().method() === 'GET' &&
      response.url().includes(`/api/v1/nodes?categoryId=${health.id}`),
    );
    await page.getByRole('button', { name: 'Health workspace', exact: true }).click();
    await healthLoad;
    const healthTree = page.getByRole('tree', { name: 'Health task nodes' });
    await expect(healthTree.getByRole('treeitem')).toHaveCount(2);

    const healthFilterButton = page.getByRole('button', { name: /Filter tasks/ });
    await healthFilterButton.click();
    const healthFilterShelf = page.getByRole('region', { name: 'Task filters' });
    await healthFilterShelf
      .getByRole('group', { name: 'Recurrence filter' })
      .getByRole('checkbox', { name: 'One-off', exact: true })
      .click();
    await expect(healthTree.getByRole('treeitem', { name: /Morning mobility/ })).toBeVisible();
    await expect(healthTree.getByRole('treeitem', { name: /Order vitamins/ })).toHaveCount(0);
    await expect(healthTree.locator('.node-state-icon.recurring')).toHaveAttribute(
      'title',
      'Recurring: Every Mon at 09:00',
    );
    await healthFilterButton.click();

    await page.getByRole('button', { name: /Sort tasks/ }).click();
    await page.getByRole('menuitemradio', { name: 'Priority' }).click();

    const workReload = page.waitForResponse((response) =>
      response.request().method() === 'GET' &&
      response.url().includes(`/api/v1/nodes?categoryId=${work.id}`),
    );
    await page.getByRole('button', { name: 'Work workspace', exact: true }).click();
    await workReload;
    await expect(
      page.getByRole('button', {
        name: /Current filters: status Open; schedule Unscheduled; recurrence Any/,
      }),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: /Current sort: deadline/ })).toBeVisible();

    const reloadBoot = workspace.waitForBoot(work.id);
    await page.reload();
    await reloadBoot;
    await expect(
      page.getByRole('button', {
        name: /Current filters: status Open; schedule Unscheduled; recurrence Any/,
      }),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: /Current sort: deadline/ })).toBeVisible();
    await expect(
      page.getByRole('tree', { name: 'Work task nodes' }).getByRole('treeitem', {
        name: /Open research task/,
      }),
    ).toBeVisible();

    const healthReload = page.waitForResponse((response) =>
      response.request().method() === 'GET' &&
      response.url().includes(`/api/v1/nodes?categoryId=${health.id}`),
    );
    await page.getByRole('button', { name: 'Health workspace', exact: true }).click();
    await healthReload;
    await expect(
      page.getByRole('button', {
        name: /Current filters: status Any; schedule Any; recurrence Recurring/,
      }),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: /Current sort: priority/ })).toBeVisible();
  });

  test('migrates a saved single filter into the matching filter facet', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 712,
      name: 'Work',
      accentColor: '#516ff2',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const scheduledTask = nodeFactory.build({
      id: 7121,
      categoryId: work.id,
      title: 'Migrated scheduled task',
      sortOrder: 0,
    });
    const unscheduledTask = nodeFactory.build({
      id: 7122,
      categoryId: work.id,
      title: 'Unscheduled task',
      sortOrder: 1,
    });

    await page.addInitScript((categoryId) => {
      window.localStorage.setItem(
        'drasil.tree-view-settings.v1',
        JSON.stringify({
          [`category:${categoryId}`]: {
            filter: 'scheduled',
            sort: 'priority',
          },
        }),
      );
    }, work.id);
    await mockCalendarBlocks(page, {
      blocks: [
        {
          id: 9712,
          taskId: scheduledTask.id,
          categoryId: work.id,
          title: scheduledTask.title,
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: '2026-08-14T14:00:00-07:00',
          endAt: '2026-08-14T14:30:00-07:00',
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
      nodesByCategoryId: { [work.id]: [scheduledTask, unscheduledTask] },
    });

    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await expect(
      page.getByRole('button', {
        name: /Current filters: status Any; schedule Scheduled; recurrence Any/,
      }),
    ).toBeVisible();
    await expect(page.getByRole('button', { name: /Current sort: priority/ })).toBeVisible();
    const tree = page.getByRole('tree', { name: 'Work task nodes' });
    await expect(tree.getByRole('treeitem', { name: /Migrated scheduled task/ })).toBeVisible();
    await expect(tree.getByRole('treeitem', { name: /^Unscheduled task/ })).toHaveCount(0);

    await page.getByRole('button', { name: /Filter tasks/ }).click();
    const filterShelf = page.getByRole('region', { name: 'Task filters' });
    await expect(
      filterShelf
        .getByRole('group', { name: 'Schedule filter' })
        .getByRole('checkbox', { name: 'Scheduled', exact: true }),
    ).toBeChecked();
    await expect(
      filterShelf
        .getByRole('group', { name: 'Schedule filter' })
        .getByRole('checkbox', { name: 'Unscheduled', exact: true }),
    ).not.toBeChecked();
    await expect(
      filterShelf
        .getByRole('group', { name: 'Status filter' })
        .getByRole('checkbox', { name: 'Open', exact: true }),
    ).toBeChecked();
    await expect(
      filterShelf
        .getByRole('group', { name: 'Status filter' })
        .getByRole('checkbox', { name: 'Closed', exact: true }),
    ).toBeChecked();
  });
});
