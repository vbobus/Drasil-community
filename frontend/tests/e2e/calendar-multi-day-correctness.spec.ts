import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks } from '../support/helpers/network';
import type { MockCalendarBlock } from '../support/helpers/network';

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
    // Retire the old single-request assertion: gap fetching may use several reads.
    await expect.poll(() => {
      let coveredTo = '2026-06-29T00:00:00';
      const ranges = calendarRequests.map(request => ({
        start: request.searchParams.get('start')!, end: request.searchParams.get('end')!,
      })).sort((a, b) => a.start.localeCompare(b.start));
      for (const range of ranges) if (range.start <= coveredTo && range.end > coveredTo) coveredTo = range.end;
      return coveredTo >= '2026-08-10T00:00:00';
    }).toBe(true);
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

  test('keeps light canvases white while highlighting the focused month week', async ({
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
    await mockCalendarBlocks(page, {
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
    await expect(dayTrack).toHaveCSS('background-color', 'rgb(255, 255, 255)');

    await calendar.getByRole('button', { name: 'Month', exact: true }).click();
    await expect(calendar.locator('.calendar-month-grid')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
    await expect(calendar.locator('.calendar-month-cell').first()).toHaveCSS('background-color', 'rgb(255, 255, 255)');
    // Retire the old all-white assertion: the focused week intentionally has a tint.
    await expect(calendar.locator('.calendar-month-cell.is-focused-week')).toHaveCount(7);
    await expect(calendar.locator('.calendar-month-cell.is-current')).not.toHaveCSS('background-color', 'rgb(255, 255, 255)');
  });
});

// Calendar fixtures run without an authenticated live backend.
test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
});
