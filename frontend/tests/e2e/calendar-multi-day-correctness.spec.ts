import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks, mockScheduleDryRun } from '../support/helpers/network';
import type { MockCalendarBlock } from '../support/helpers/network';
import { mockStrategistApi, type MockStrategistRunResponse } from '../support/helpers/strategist';

function block(overrides: Partial<MockCalendarBlock> & Pick<MockCalendarBlock, 'id' | 'taskId' | 'title' | 'startAt' | 'endAt'>): MockCalendarBlock {
  return {
    categoryId: 531,
    emoji: 'C',
    categoryName: 'Work',
    categoryColor: '#1f6feb',
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

function proposedSchedule(taskId: number, categoryId: number) {
  const previewBlock = {
    id: `proposal-${taskId}-overnight`,
    taskId,
    categoryId,
    title: 'Proposed overnight task',
    categoryName: 'Work',
    categoryColor: '#1f6feb',
    startAt: '2026-07-13T23:30:00',
    endAt: '2026-07-15T01:00:00',
    variant: 'proposed' as const,
    completionStatus: 'not_started',
    isPinned: false,
    isProtected: false,
    isRecurring: false,
    hierarchyDepth: 0,
    renderedChildTaskIds: [],
  };
  return {
    proposalId: 'story-3-1-proposal',
    proposalKind: 'schedule_patch',
    operation: 'schedule',
    legacyResponseKey: 'scheduleProposal',
    title: 'Cross-date schedule proposal',
    body: 'Review this proposed cross-date placement.',
    targetNodeIds: [taskId],
    preview: [],
    validationWarnings: [],
    schedulePatch: {
      patchId: 'story-3-1-patch',
      workingSetTaskIds: [taskId],
      windowStart: '2026-07-13T00:00:00',
      windowEnd: '2026-07-16T00:00:00',
      operations: [{
        op: 'create_block',
        proposalBlockId: previewBlock.id,
        taskId,
        categoryId,
        title: previewBlock.title,
        categoryName: previewBlock.categoryName,
        categoryColor: previewBlock.categoryColor,
        startAt: previewBlock.startAt,
        endAt: previewBlock.endAt,
        variant: 'proposed',
        completionStatus: 'not_started',
      }],
    },
    schedulePatchValidation: { status: 'valid', errors: [], warnings: [], operationCount: 1 },
    schedulePatchSimulation: {
      previewBlocks: [previewBlock],
      conflicts: [],
      warnings: [],
      operationCount: 1,
      affectedTaskIds: [taskId],
      affectedCalendarBlockIds: [],
    },
    schedulePatchStatus: 'valid',
    capabilityId: 'planner',
    payload: { conflicts: [], schedulePatchLifecycle: { status: 'pending' } },
  };
}

function proposedRun(proposal: ReturnType<typeof proposedSchedule>): MockStrategistRunResponse {
  return {
    session: {
      id: 'story-3-1-session',
      title: 'Cross-date schedule',
      providerLabel: 'Planner',
      createdAt: '2026-07-13T19:00:00Z',
      updatedAt: '2026-07-13T19:00:00Z',
    },
    turn: {
      id: 'story-3-1-turn',
      prompt: 'schedule this overnight',
      createdAt: '2026-07-13T19:00:00Z',
      response: {
        providerLabel: 'Planner',
        mode: 'schedule_patch',
        summary: 'Prepared one cross-date placement.',
        contextCards: [],
        suggestedPrompts: [],
        proposal,
      },
    },
  };
}

test.describe('Story 3.1 - multi-day calendar correctness', () => {
  test.use({ timezoneId: 'America/Vancouver' });
  test.beforeEach(async ({ page }) => {
    await page.clock.setFixedTime(new Date('2026-07-13T12:00:00-07:00'));
  });

  test('projects one canonical overnight block into each overlapping local day', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 531,
      name: 'Work',
      accentColor: '#1f6feb',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const task = nodeFactory.build({
      id: 5311,
      categoryId: work.id,
      title: 'Overnight migration',
      timeEstimateMinutes: 90,
      sortOrder: 0,
    });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [task] },
    });
    await mockCalendarBlocks(page, {
      blocks: [
        {
          id: 9311,
          taskId: task.id,
          categoryId: work.id,
          title: task.title,
          emoji: 'O',
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: '2026-07-13T23:30:00',
          endAt: '2026-07-14T01:00:00',
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
    const firstSegment = calendar.locator(
      '[data-calendar-block-id="9311"][data-calendar-segment-date="2026-07-13"]',
    );
    await expect(firstSegment).toHaveCount(1);

    await calendar.getByRole('button', { name: 'Next calendar range' }).click();
    const finalSegment = calendar.locator(
      '[data-calendar-block-id="9311"][data-calendar-segment-date="2026-07-14"]',
    );
    await expect(finalSegment).toHaveCount(1);
    await expect(finalSegment).toHaveAccessibleName(
      /Overnight migration.*July 14.*continues from previous day.*July 13.*July 14/i,
    );
    await expect(calendar.getByText('No scheduled blocks yet')).toHaveCount(0);
  });

  test('renders full intermediate days, exact-midnight boundaries, and the complete month grid range', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 531, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const tasks = [
      nodeFactory.build({ id: 5321, categoryId: work.id, title: 'Long release watch', sortOrder: 0 }),
      nodeFactory.build({ id: 5322, categoryId: work.id, title: 'Ends at midnight', sortOrder: 1 }),
      nodeFactory.build({ id: 5323, categoryId: work.id, title: 'Starts at midnight', sortOrder: 2 }),
    ];
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { [work.id]: tasks } });
    await mockCalendarBlocks(page, {
      blocks: [
        block({ id: 9321, taskId: 5321, title: 'Long release watch', startAt: '2026-07-12T23:30:00', endAt: '2026-07-15T01:00:00', variant: 'protected', isProtected: true }),
        block({ id: 9322, taskId: 5322, title: 'Ends at midnight', startAt: '2026-07-13T22:00:00', endAt: '2026-07-14T00:00:00' }),
        block({ id: 9323, taskId: 5323, title: 'Starts at midnight', startAt: '2026-07-14T00:00:00', endAt: '2026-07-14T00:30:00', variant: 'draft' }),
        block({ id: 9324, taskId: 5394, title: 'Month overflow one', startAt: '2026-07-14T08:00:00', endAt: '2026-07-14T08:30:00' }),
        block({ id: 9325, taskId: 5395, title: 'Month overflow two', startAt: '2026-07-14T09:00:00', endAt: '2026-07-14T09:30:00' }),
        block({ id: 9326, taskId: 5396, title: 'Month overflow three', startAt: '2026-07-14T10:00:00', endAt: '2026-07-14T10:30:00', variant: 'proposed' }),
      ],
    });

    const calendarRequests: URL[] = [];
    page.on('request', (request) => {
      const requestUrl = new URL(request.url());
      if (
        request.method() === 'GET'
        && requestUrl.pathname === '/api/v1/calendar-blocks'
      ) {
        calendarRequests.push(requestUrl);
      }
    });
    const boot = Promise.all([workspace.waitForBoot(work.id), page.waitForResponse((response) => response.request().method() === 'GET' && response.url().includes('/api/v1/calendar-blocks'))]);
    await page.goto('/');
    await boot;

    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await calendar.getByRole('button', { name: 'Week' }).click();
    const longSegments = calendar.locator('[data-calendar-block-id="9321"]');
    await expect(longSegments).toHaveCount(3);
    for (const date of ['2026-07-13', '2026-07-14']) {
      const fullDay = calendar.locator(`[data-calendar-block-id="9321"][data-calendar-segment-date="${date}"]`);
      await expect(fullDay).toHaveCSS('top', '0px');
      expect(await fullDay.evaluate((element) => element.getAttribute('style'))).toContain('--block-height: 100%');
    }
    await expect(calendar.locator('[data-calendar-block-id="9322"][data-calendar-segment-date="2026-07-14"]')).toHaveCount(0);
    await expect(calendar.locator('[data-calendar-block-id="9323"][data-calendar-segment-date="2026-07-14"]')).toHaveCount(1);
    await expect(calendar.locator('[data-calendar-block-id="9321"][data-calendar-segment-date="2026-07-14"]')).toContainText('Continues through this day');

    await calendar.getByRole('button', { name: 'Month', exact: true }).click();
    await expect(calendar.locator('[data-calendar-block-id="9321"]')).toHaveCount(4);
    await expect(calendar.locator('[data-calendar-block-id="9321"][data-calendar-segment-date="2026-07-14"]')).toContainText('Continues');
    await expect(calendar.locator('[data-calendar-date="2026-07-14"] .calendar-month-overflow')).toHaveText('+2 more');
    await expect.poll(() => calendarRequests.at(-1)?.searchParams.get('start')).toBe('2026-06-29T00:00:00');
    expect(calendarRequests.at(-1)?.searchParams.get('end')).toBe('2026-08-10T00:00:00');
    await expect(calendar.getByRole('grid', { name: /shared calendar month view/i })).toBeVisible();
  });

  test('opens canonical details from a continuation and edits a cross-date interval', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 531, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 5331, categoryId: work.id, title: 'Overnight audit', sortOrder: 0 });
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { [work.id]: [task] } });
    let nodePatchCount = 0;
    await page.route(`**/api/v1/nodes/${task.id}`, async (route) => {
      if (route.request().method() !== 'PATCH') {
        await route.fallback();
        return;
      }
      nodePatchCount += 1;
      const payload = route.request().postDataJSON() as { status?: string };
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...task, status: payload.status ?? task.status }) });
    });
    const calendarMock = await mockCalendarBlocks(page, {
      blocks: [block({ id: 9331, taskId: task.id, title: task.title, startAt: '2026-07-13T23:30:00', endAt: '2026-07-14T01:00:00' })],
    });
    const boot = Promise.all([workspace.waitForBoot(work.id), page.waitForResponse((response) => response.request().method() === 'GET' && response.url().includes('/api/v1/calendar-blocks'))]);
    await page.goto('/');
    await boot;

    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await calendar.getByRole('button', { name: 'Next calendar range' }).click();
    const continuation = calendar.locator('[data-calendar-block-id="9331"][data-calendar-segment-date="2026-07-14"]');
    await continuation.press('Enter');
    const dialog = page.getByRole('dialog', { name: 'Calendar block details' });
    await expect(dialog).toContainText('Monday, July 13, 11:30 PM - Tuesday, July 14, 1:00 AM');
    await dialog.getByRole('checkbox', { name: /mark overnight audit done/i }).check();
    await expect.poll(() => nodePatchCount).toBe(1);

    await dialog.getByRole('button', { name: 'Edit calendar block' }).click();
    await dialog.getByLabel('Calendar block start date and time').fill('2026-07-14T22:45');
    await dialog.getByLabel('Calendar block end date and time').fill('2026-07-16T00:15');
    await dialog.getByRole('button', { name: 'Save calendar block time' }).click();
    await expect.poll(() => calendarMock.updatedPayloads.length).toBe(1);
    expect(calendarMock.updatedPayloads[0]).toEqual({
      id: 9331,
      payload: { startAt: '2026-07-14T22:45:00', endAt: '2026-07-16T00:15:00' },
    });
  });

  test('moves the canonical interval from a continuation segment and exposes only canonical resize edges', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 531, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 5341, categoryId: work.id, title: 'Multi-day incident watch', sortOrder: 0 });
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { [work.id]: [task] } });
    const calendarMock = await mockCalendarBlocks(page, {
      blocks: [block({ id: 9341, taskId: task.id, title: task.title, startAt: '2026-07-13T23:30:00', endAt: '2026-07-15T01:00:00' })],
    });
    const boot = Promise.all([workspace.waitForBoot(work.id), page.waitForResponse((response) => response.request().method() === 'GET' && response.url().includes('/api/v1/calendar-blocks'))]);
    await page.goto('/');
    await boot;

    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await calendar.getByRole('button', { name: 'Week' }).click();
    const first = calendar.locator('[data-calendar-block-id="9341"][data-calendar-segment-date="2026-07-13"]');
    const middle = calendar.locator('[data-calendar-block-id="9341"][data-calendar-segment-date="2026-07-14"]');
    const last = calendar.locator('[data-calendar-block-id="9341"][data-calendar-segment-date="2026-07-15"]');
    await expect(first.getByTestId('calendar-resize-start-9341')).toHaveCount(1);
    await expect(first.getByTestId('calendar-resize-end-9341')).toHaveCount(0);
    await expect(middle.locator('[data-testid^="calendar-resize-"]')).toHaveCount(0);
    await expect(last.getByTestId('calendar-resize-end-9341')).toHaveCount(1);
    await expect(last.getByTestId('calendar-resize-start-9341')).toHaveCount(0);

    const sourceBox = await middle.boundingBox();
    const target = calendar.locator('[data-calendar-date="2026-07-13"][data-calendar-time-surface="true"]');
    const targetBox = await target.boundingBox();
    expect(sourceBox).not.toBeNull();
    expect(targetBox).not.toBeNull();
    const pointerY = Math.max(
      targetBox!.y + 20,
      Math.min(targetBox!.y + targetBox!.height - 20, 400),
    );
    await page.mouse.move(sourceBox!.x + sourceBox!.width / 2, pointerY);
    await page.mouse.down();
    await page.mouse.move(targetBox!.x + targetBox!.width / 2, pointerY, { steps: 10 });
    await expect(calendar.locator('[data-calendar-block-id="9341"][data-calendar-segment-date="2026-07-15"]')).toHaveCount(0);
    await page.mouse.up();

    await expect.poll(() => calendarMock.updatedPayloads.length).toBe(1);
    const moved = calendarMock.updatedPayloads[0];
    const originalStart = Date.parse('2026-07-13T23:30:00');
    const originalEnd = Date.parse('2026-07-15T01:00:00');
    const movedStart = Date.parse(String(moved.payload.startAt));
    const movedEnd = Date.parse(String(moved.payload.endAt));
    expect(moved.id).toBe(9341);
    expect(movedEnd - movedStart).toBe(originalEnd - originalStart);
    expect(movedStart - originalStart).toBe(movedEnd - originalEnd);
    expect(String(moved.payload.startAt)).not.toContain('Z');

    const movedFinal = calendar.locator('[data-calendar-block-id="9341"][data-calendar-segment-date="2026-07-14"]');
    const endHandle = movedFinal.getByTestId('calendar-resize-end-9341');
    const endHandleBox = await endHandle.boundingBox();
    const resizeTarget = await target.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const row = element.querySelector('.calendar-hour-row');
      if (!row) throw new Error('Calendar hour row was not rendered.');
      return {
        x: rect.left + rect.width / 2,
        y: rect.top + row.getBoundingClientRect().height * 2,
      };
    });
    expect(endHandleBox).not.toBeNull();
    await page.mouse.move(endHandleBox!.x + endHandleBox!.width / 2, endHandleBox!.y + endHandleBox!.height / 2);
    await page.mouse.down();
    await page.mouse.move(resizeTarget.x, resizeTarget.y, { steps: 8 });
    await page.mouse.up();
    await expect.poll(() => calendarMock.updatedPayloads.length).toBe(2);
    expect(calendarMock.updatedPayloads[1].payload.startAt).toBe(moved.payload.startAt);
    expect(String(calendarMock.updatedPayloads[1].payload.endAt)).toMatch(/^2026-07-13T02:00:00$/);
  });

  test('keeps a retired Planner proposal move local and blocks approval', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 531, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 5351, categoryId: work.id, title: 'Proposed overnight task', timeEstimateMinutes: 1530, sortOrder: 0 });
    const proposal = proposedSchedule(task.id, work.id);
    await mockStrategistApi(page, { runResponses: [proposedRun(proposal)] });
    const calendarMock = await mockCalendarBlocks(page, { blocks: [] });
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { [work.id]: [task] } });
    await page.goto('/');
    await workspace.waitForBoot(work.id);
    await page.getByRole('button', { name: /select proposed overnight task for execution staging/i }).click();
    await page.getByRole('tab', { name: /^Chat$/i }).click();
    const composer = page.getByRole('textbox', { name: 'AI workspace prompt' });
    await composer.fill('schedule this overnight');
    await composer.press('Enter');

    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await expect(calendar.locator(`[data-calendar-block-id="${proposal.schedulePatchSimulation.previewBlocks[0].id}"]`)).toHaveCount(1);
    await calendar.getByRole('button', { name: 'Week' }).click();
    const blockId = proposal.schedulePatchSimulation.previewBlocks[0].id;
    const middle = calendar.locator(`[data-calendar-block-id="${blockId}"][data-calendar-segment-date="2026-07-14"]`);
    const target = calendar.locator('[data-calendar-date="2026-07-13"][data-calendar-time-surface="true"]');
    const sourceBox = await middle.boundingBox();
    const targetBox = await target.boundingBox();
    expect(sourceBox).not.toBeNull();
    expect(targetBox).not.toBeNull();
    const pointerY = Math.max(targetBox!.y + 20, Math.min(targetBox!.y + targetBox!.height - 20, 400));
    await page.mouse.move(sourceBox!.x + sourceBox!.width / 2, pointerY);
    await page.mouse.down();
    await page.mouse.move(targetBox!.x + targetBox!.width / 2, pointerY, { steps: 8 });
    await page.mouse.up();

    expect(calendarMock.createdPayloads).toEqual([]);
    expect(calendarMock.updatedPayloads).toEqual([]);
    expect(calendarMock.deletedIds).toEqual([]);
    const activeTree = page.getByRole('tabpanel', { name: /^Chat$/i });
    await expect(activeTree.getByRole('button', { name: 'Approve schedule' })).toHaveCount(0);
    await expect(activeTree.getByText(/proposal has expired/i)).toBeVisible();
  });

  test('keeps a solver-draft cross-date resize local', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 531, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 5361, categoryId: work.id, title: 'Draft overnight task', timeEstimateMinutes: 1530, sortOrder: 0 });
    const calendarMock = await mockCalendarBlocks(page, { blocks: [] });
    await mockScheduleDryRun(page, {
      response: {
        draftBlocks: [{
          id: 'draft-5361-overnight',
          taskId: task.id,
          categoryId: work.id,
          title: task.title,
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: '2026-07-13T23:30:00',
          endAt: '2026-07-15T01:00:00',
          variant: 'draft',
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
          hierarchyDepth: 0,
          renderedChildTaskIds: [],
        }],
        conflicts: [],
        diagnostics: {
          solverStatus: 'feasible', runtimeMs: 5, scheduledTaskCount: 1, conflictCount: 0,
          occupiedBlockCount: 0, fallbackDurationTaskIds: [], alreadyScheduledTaskIds: [],
        },
      },
    });
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { [work.id]: [task] } });
    await page.goto('/');
    await workspace.waitForBoot(work.id);
    await page.getByRole('button', { name: /select draft overnight task for execution staging/i }).click();
    await page.getByRole('tab', { name: /^Selected tasks/i }).click();
    await page.getByRole('region', { name: /^Selected tasks$/i }).getByRole('button', { name: /auto-schedule selected/i }).click();
    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await calendar.getByRole('button', { name: 'Week' }).click();
    await expect(calendar.locator('[data-calendar-block-id="draft-5361-overnight"]')).toHaveCount(3);

    const final = calendar.locator('[data-calendar-block-id="draft-5361-overnight"][data-calendar-segment-date="2026-07-15"]');
    await final.scrollIntoViewIfNeeded();
    const handleBox = await final.getByTestId('calendar-resize-end-draft-5361-overnight').boundingBox();
    const target = calendar.locator('[data-calendar-date="2026-07-14"][data-calendar-time-surface="true"]');
    const targetPoint = await target.evaluate((element) => {
      const rect = element.getBoundingClientRect();
      const row = element.querySelector('.calendar-hour-row');
      if (!row) throw new Error('Calendar hour row was not rendered.');
      return { x: rect.left + rect.width / 2, y: rect.top + row.getBoundingClientRect().height * 2 };
    });
    expect(handleBox).not.toBeNull();
    await page.mouse.move(handleBox!.x + handleBox!.width / 2, handleBox!.y + handleBox!.height / 2);
    await page.mouse.down();
    await page.mouse.move(targetPoint.x, targetPoint.y, { steps: 8 });
    await page.mouse.up();
    await expect(calendar.locator('[data-calendar-block-id="draft-5361-overnight"][data-calendar-segment-date="2026-07-15"]')).toHaveAccessibleName(/2:00 AM/i);
    await expect(page.getByRole('region', { name: /^Selected tasks$/i }).getByText('26h 30m').first()).toBeVisible();
    expect(calendarMock.createdPayloads).toEqual([]);
    expect(calendarMock.updatedPayloads).toEqual([]);
  });

  test('rolls a day-view move backward across midnight and keeps day/month canvases white', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    await page.addInitScript(() => {
      localStorage.setItem('drasil.appearance-theme.v1', 'light');
    });
    const work = categoryFactory.build({ id: 531, name: 'Work', accentColor: '#6d5dfc', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 5371, categoryId: work.id, title: 'Move to yesterday', timeEstimateMinutes: 30, sortOrder: 0 });
    const calendarMock = await mockCalendarBlocks(page, {
      blocks: [block({
        id: 9371,
        taskId: task.id,
        title: task.title,
        categoryColor: work.accentColor,
        startAt: '2026-07-13T01:00:00',
        endAt: '2026-07-13T01:30:00',
      })],
    });
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { [work.id]: [task] } });
    const boot = Promise.all([
      workspace.waitForBoot(work.id),
      page.waitForResponse((response) => response.request().method() === 'GET' && response.url().includes('/api/v1/calendar-blocks')),
    ]);
    await page.goto('/');
    await boot;

    const calendar = page.getByRole('region', { name: /shared calendar/i });
    const dayTrack = calendar.getByTestId('calendar-day-drop-zone');
    const movable = calendar.locator('[data-calendar-block-id="9371"]');
    const blockBox = await movable.boundingBox();
    const trackBox = await dayTrack.boundingBox();
    expect(blockBox).not.toBeNull();
    expect(trackBox).not.toBeNull();
    await page.mouse.move(blockBox!.x + 12, blockBox!.y + blockBox!.height / 2);
    await page.mouse.down();
    await page.mouse.move(trackBox!.x + trackBox!.width / 2, trackBox!.y - 24, { steps: 8 });
    await page.mouse.up();

    await expect.poll(() => calendarMock.updatedPayloads.length).toBe(1);
    expect(String(calendarMock.updatedPayloads[0].payload.startAt)).toMatch(/^2026-07-12T/);
    expect(
      Date.parse(String(calendarMock.updatedPayloads[0].payload.endAt)) -
      Date.parse(String(calendarMock.updatedPayloads[0].payload.startAt)),
    ).toBe(30 * 60 * 1000);
    await expect(dayTrack).toHaveCSS('background-color', 'rgb(255, 255, 255)');

    await calendar.getByRole('button', { name: 'Month', exact: true }).click();
    await expect(calendar.locator('.calendar-month-grid')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
    await expect(calendar.locator('.calendar-month-cell').first()).toHaveCSS('background-color', 'rgb(255, 255, 255)');
    await expect(calendar.locator('.calendar-month-cell.is-current')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
  });
});
