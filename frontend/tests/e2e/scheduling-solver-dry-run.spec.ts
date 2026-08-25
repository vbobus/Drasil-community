import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks, mockScheduleDryRun } from '../support/helpers/network';

test.describe('Story 2.3 - Deterministic scheduling solver dry run', () => {
  test.beforeEach(async ({ page }) => {
    await page.clock.setFixedTime(new Date('2026-06-18T08:00:00-07:00'));
  });

  test('auto-schedules selected tasks into draft calendar preview without writes or AI calls', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 230,
      name: 'Work',
      accentColor: '#1f6feb',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });

    const parentTask = nodeFactory.build({
      id: 2301,
      categoryId: work.id,
      title: 'Launch case study',
      emoji: 'L',
      timeEstimateMinutes: null,
      sortOrder: 0,
    });
    const outlineTask = nodeFactory.build({
      id: 2302,
      categoryId: work.id,
      parentId: parentTask.id,
      title: 'Draft outline',
      timeEstimateMinutes: 45,
      sortOrder: 0,
    });
    const visualTask = nodeFactory.build({
      id: 2303,
      categoryId: work.id,
      parentId: parentTask.id,
      title: 'Review visuals',
      timeEstimateMinutes: 30,
      sortOrder: 1,
    });
    const alreadyScheduledTask = nodeFactory.build({
      id: 2304,
      categoryId: work.id,
      title: 'Write launch email',
      emoji: 'E',
      timeEstimateMinutes: 60,
      sortOrder: 1,
    });

    let nodeMutationRequests = 0;
    let aiRequests = 0;
    page.on('request', (request) => {
      const url = request.url();
      const method = request.method();
      if (method !== 'GET' && url.includes('/api/v1/nodes')) {
        nodeMutationRequests += 1;
      }
      if (method !== 'GET' && url.includes('/api/v1/ai/')) {
        aiRequests += 1;
      }
    });

    const calendarRoutes = await mockCalendarBlocks(page, {
      blocks: [
        {
          id: 8300,
          taskId: alreadyScheduledTask.id,
          categoryId: work.id,
          title: alreadyScheduledTask.title,
          emoji: alreadyScheduledTask.emoji,
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: '2026-06-18T14:00:00',
          endAt: '2026-06-18T15:00:00',
          variant: 'scheduled',
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
          hierarchyDepth: 0,
          renderedChildTaskIds: [],
        },
      ],
    });

    const scheduleRoutes = await mockScheduleDryRun(page, {
      response: {
        draftBlocks: [
          {
            id: 'draft-2301-20260618T090000',
            taskId: parentTask.id,
            categoryId: work.id,
            title: parentTask.title,
            emoji: parentTask.emoji,
            categoryName: work.name,
            categoryColor: work.accentColor,
            startAt: '2026-06-18T09:00:00',
            endAt: '2026-06-18T10:15:00',
            variant: 'draft',
            completionStatus: 'not_started',
            isPinned: false,
            isProtected: false,
            isRecurring: false,
            hierarchyDepth: 0,
            renderedChildTaskIds: [outlineTask.id, visualTask.id],
          },
        ],
        conflicts: [],
        diagnostics: {
          solverStatus: 'feasible',
          runtimeMs: 18,
          scheduledTaskCount: 1,
          conflictCount: 0,
          occupiedBlockCount: 1,
          fallbackDurationTaskIds: [],
          alreadyScheduledTaskIds: [alreadyScheduledTask.id],
        },
      },
    });

    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: {
        [work.id]: [parentTask, outlineTask, visualTask, alreadyScheduledTask],
      },
    });

    const boot = Promise.all([
      workspace.waitForBoot(work.id),
      page.waitForResponse(
        (response) =>
          response.request().method() === 'GET' &&
          response.url().includes('/api/v1/calendar-blocks'),
      ),
    ]);
    await page.goto('/');
    await boot;

    await page
      .getByRole('button', { name: /select launch case study for execution staging/i })
      .click();
    await page
      .getByRole('button', { name: /select write launch email for execution staging/i })
      .click();

    await page.getByRole('tab', { name: /^Selected tasks/i }).click();
    const selectedPanel = page.getByRole('region', { name: /^Selected tasks$/i });
    await selectedPanel.getByRole('button', { name: /auto-schedule selected/i }).click();

    await expect.poll(() => scheduleRoutes.requests.length).toBe(1);
    expect(scheduleRoutes.requests[0]).toMatchObject({
      taskIds: [parentTask.id, alreadyScheduledTask.id],
      windowStart: '2026-06-18T08:00:00',
      windowEnd: '2026-06-19T00:00:00',
    });

    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await expect(
      calendar.getByRole('button', {
        name: /Launch case study.*9:00 AM.*10:15 AM.*Work.*2 subtasks.*draft/i,
      }),
    ).toBeVisible();
    await expect(calendar.getByText('Draft outline')).toBeVisible();
    await expect(calendar.getByText('Review visuals')).toBeVisible();

    await calendar.getByRole('button', { name: /next calendar range/i }).click();
    await expect(
      calendar.getByRole('button', {
        name: /Launch case study.*9:00 AM.*10:15 AM.*Work.*2 subtasks.*draft/i,
      }),
    ).toHaveCount(0);
    await expect(selectedPanel.getByText('1 drafted')).toBeVisible();

    await calendar.getByRole('button', { name: /previous calendar range/i }).click();
    await expect(
      calendar.getByRole('button', {
        name: /Launch case study.*9:00 AM.*10:15 AM.*Work.*2 subtasks.*draft/i,
      }),
    ).toBeVisible();

    await calendar.getByRole('button', { name: /^Week$/i }).click();
    await expect(
      calendar.getByRole('button', {
        name: /Launch case study.*9:00 AM.*10:15 AM.*Work.*2 subtasks.*draft/i,
      }),
    ).toBeVisible();
    await expect(selectedPanel.getByText('1 drafted')).toBeVisible();

    await calendar.getByRole('button', { name: /^Month$/i }).click();
    const monthDraftCard = calendar.locator('.calendar-grid-block.variant-draft').filter({
      hasText: 'Launch case study',
    });
    await expect(
      calendar.getByRole('button', {
        name: /Launch case study.*9:00 AM.*10:15 AM.*Work.*2 subtasks.*draft/i,
      }),
    ).toBeVisible();
    await expect(monthDraftCard).toHaveCSS('border-top-style', 'dashed');
    await expect(selectedPanel.getByText('1 drafted')).toBeVisible();

    await calendar.getByRole('button', { name: /^Day$/i }).click();
    await expect(
      calendar.getByRole('button', {
        name: /Launch case study.*9:00 AM.*10:15 AM.*Work.*2 subtasks.*draft/i,
      }),
    ).toBeVisible();
    expect(scheduleRoutes.requests).toHaveLength(1);

    await expect(selectedPanel.getByText('1 drafted')).toBeVisible();
    await expect(selectedPanel.getByText('1 already scheduled')).toBeVisible();
    await expect(selectedPanel.getByText('1h 15m')).toBeVisible();
    await expect(selectedPanel.getByText('18ms')).toBeVisible();
    const solverResult = selectedPanel.locator('.selected-solver-result');
    await expect(solverResult.locator('.solver-list').first().getByText('Launch case study')).toBeVisible();
    await expect(solverResult.getByText('9:00 AM - 10:15 AM')).toBeVisible();
    await expect(solverResult.getByText('Already on the calendar: Write launch email')).toBeVisible();
    await expect(
      calendar.getByRole('button', {
        name: /Write launch email.*2:00 PM.*3:00 PM.*Work.*no subtasks.*scheduled/i,
      }),
    ).toHaveCount(1);

    await calendar
      .getByRole('button', {
        name: /Launch case study.*9:00 AM.*10:15 AM.*Work.*2 subtasks.*draft/i,
      })
      .click();
    const draftDialog = page.getByRole('dialog', { name: /calendar block details/i });
    await expect(
      draftDialog.getByRole('checkbox', { name: /mark launch case study done/i }),
    ).toHaveCount(0);
    await draftDialog.getByRole('button', { name: /edit calendar block/i }).click();
    await draftDialog.getByLabel('Calendar block start date and time').fill('2026-06-18T12:30');
    await draftDialog.getByLabel('Calendar block end date and time').fill('2026-06-18T13:45');
    await draftDialog.getByRole('button', { name: /save calendar block time/i }).click();
    await expect(
      calendar.getByRole('button', {
        name: /Launch case study.*12:30 PM.*1:45 PM.*Work.*2 subtasks.*draft/i,
      }),
    ).toBeVisible();

    expect(calendarRoutes.createdPayloads).toEqual([]);
    expect(calendarRoutes.updatedPayloads).toEqual([]);
    expect(calendarRoutes.deletedIds).toEqual([]);
    expect(nodeMutationRequests).toBe(0);
    expect(aiRequests).toBe(0);
  });
});
