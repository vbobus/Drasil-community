import { expect, type Locator, type Page } from '@playwright/test';
import type { HabitInput, HabitSnapshot } from '../../src/api/habits';
import { test } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks, type MockCalendarBlock } from '../support/helpers/network';

const work = { id: 1, name: 'Work', accentColor: '#6366f1', iconSlug: 'briefcase', sortOrder: 0 };
const health = { id: 2, name: 'Health', accentColor: '#10b981', iconSlug: 'heart-pulse', sortOrder: 1 };
const focusTitle = 'Prepare the project handoff and next steps for the design team';
const focusBlock: MockCalendarBlock = {
  id: 9601, taskId: 8601, categoryId: work.id, title: focusTitle, emoji: '📝',
  categoryName: work.name, categoryColor: work.accentColor,
  startAt: '2026-10-09T08:00:00', endAt: '2026-10-09T12:00:00',
  variant: 'scheduled', completionStatus: 'not_started', isPinned: false,
  isProtected: false, isRecurring: false,
};

async function openNavigation(page: Page) {
  await page.getByRole('button', { name: 'Open navigation', exact: true }).tap();
  const dialog = page.getByRole('dialog', { name: 'Navigation', exact: true });
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate(element => element.matches(':modal'))).toBe(true);
  return dialog;
}

async function selectDestination(page: Page, name: RegExp) {
  const dialog = await openNavigation(page);
  await dialog.getByRole('button', { name }).tap();
  await expect(dialog).toBeHidden();
}

async function assertNoPageOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth))
    .toBeLessThanOrEqual(page.viewportSize()!.width);
}

async function assertUtilityClearOfLauncher(page: Page, utility: Locator) {
  const launcher = (await page.locator('.chat-launcher').boundingBox())!;
  const viewport = (await utility.boundingBox())!;
  // Controls scroll inside this boundary, above the fixed launcher.
  expect(viewport.y + viewport.height).toBeLessThanOrEqual(launcher.y + 1);
}

async function assertReachable(control: Locator, page: Page) {
  await control.scrollIntoViewIfNeeded();
  await expect(control).toBeInViewport({ ratio: 1 });
  const bounds = await control.boundingBox();
  expect(bounds!.height).toBeGreaterThanOrEqual(44);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(page.viewportSize()!.height + 1);
}

async function screenshot(page: Page, name: string) {
  const engine = page.context().browser()!.browserType().name();
  await page.screenshot({ path: `../.impeccable/review/mobile-adaptation-${engine}-${name}.png`, animations: 'disabled' });
}

function habitSnapshot(): HabitSnapshot {
  return {
    today: '2026-10-09', totalXp: 170, level: 2, levelXp: 70, totalCheckIns: 17, bestStreak: 6,
    badges: [
      { id: 'first', title: 'First step', description: 'Complete your first check-in.', earned: true },
      { id: 'ten', title: 'Finding rhythm', description: 'Make 10 check-ins.', earned: true },
      { id: 'streak', title: 'Steady seven', description: 'Build a 7-check-in streak.', earned: false },
      { id: 'fifty', title: 'Showing up', description: 'Make 50 check-ins.', earned: false },
    ],
    habits: ['Read 10 pages', 'Take a 15-minute walk', 'Drink a glass of water'].map((title, index) => ({
      id: `mobile-habit-${index}`, title, cue: ['After my morning coffee', 'After lunch, before I sit down again', 'When I start my day'][index],
      icon: ['book', 'activity', 'droplet'][index], weekdays: [0, 1, 2, 3, 4, 5, 6], createdOn: '2026-09-01', archived: false,
      currentStreak: [4, 2, 6][index], bestStreak: [4, 3, 6][index], totalCheckIns: [5, 4, 8][index],
      week: Array.from({ length: 7 }, (_, day) => ({ date: `2026-10-0${day + 3}`, scheduled: true, completed: day < index + 2 })),
    })),
  };
}

async function mockHabits(page: Page) {
  const snapshot = habitSnapshot();
  const writes: Array<{ path: string; body: unknown }> = [];
  await page.route('**/api/v1/habits**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET') {
      const body = request.postDataJSON();
      writes.push({ path, body });
      const checkIn = path.match(/\/habits\/(mobile-habit-\d)\/check-ins\/(\d{4}-\d{2}-\d{2})$/);
      if (checkIn) {
        const habit = snapshot.habits.find(item => item.id === checkIn[1])!;
        const day = habit.week.find(item => item.date === checkIn[2])!;
        const change = body.completed === day.completed ? 0 : body.completed ? 1 : -1;
        day.completed = body.completed;
        habit.totalCheckIns += change;
        snapshot.totalCheckIns += change;
        snapshot.totalXp += change * 10;
        snapshot.levelXp = snapshot.totalXp % 100;
      } else if (request.method() === 'PUT') {
        const habit = snapshot.habits.find(item => path.endsWith(`/${item.id}`))!;
        Object.assign(habit, body as HabitInput);
      }
    }
    await route.fulfill({ json: snapshot });
  });
  return { snapshot, writes };
}

async function mockSettings(page: Page) {
  await page.route('**/api/v1/auth/chatgpt', route => route.fulfill({ json: {
    provider: 'codex-cli', isAuthenticated: false,
  } }));
  await page.route('**/api/v1/auth/strategist-provider**', route => route.fulfill({ json: {
    activeProvider: 'codex-cli', activeProviderLabel: 'Codex CLI', codexModelOptions: [],
    isOpenAiKeyConfigured: false, openAiModel: 'gpt-5',
  } }));
  await page.route('**/api/v1/calendar-integrations/google/status', route => route.fulfill({ json: {
    provider: 'google', isConfigured: false, isConnected: false, scopes: [],
    syncPolicy: { sourceOfTruth: 'Drasil', googleTimeEdits: 'Review in Drasil', googleTitleAndDescriptionEdits: 'Review in Drasil', completedTitlePrefix: '', calendarMode: 'dedicated' },
  } }));
}

async function touchDrag(page: Page, from: { x: number; y: number }, to: { x: number; y: number }) {
  const session = await page.context().newCDPSession(page);
  const point = (x: number, y: number) => [{ x, y, radiusX: 2, radiusY: 2, force: 1, id: 1 }];
  try {
    await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: point(from.x, from.y) });
    for (let step = 1; step <= 10; step++) {
      await session.send('Input.dispatchTouchEvent', {
        type: 'touchMove', touchPoints: point(from.x + (to.x - from.x) * step / 10, from.y + (to.y - from.y) * step / 10),
      });
      // Give native scrolling and pointer previews a frame to update.
      await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
    }
    await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  } finally { await session.detach(); }
}

async function waitForNativeScrollToSettle(scroller: Locator) {
  await scroller.evaluate(async element => {
    let previous = element.scrollTop;
    let stableFrames = 0;
    while (stableFrames < 8) {
      await new Promise<void>(resolve => requestAnimationFrame(() => resolve()));
      const current = element.scrollTop;
      stableFrames = Math.abs(current - previous) < .1 ? stableFrames + 1 : 0;
      previous = current;
    }
  });
}

test.describe('mobile adaptation', () => {
  test.use({ hasTouch: true, isMobile: true, deviceScaleFactor: 1, timezoneId: 'America/Vancouver', viewport: { width: 390, height: 844 } });

  test.beforeEach(async ({ page, mockStrategistApi }) => {
    await page.clock.setFixedTime(new Date('2026-10-09T08:00:00-07:00'));
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
      authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
    } }));
    await mockStrategistApi();
    await mockSettings(page);
  });

  for (const viewport of [{ width: 320, height: 740 }, { width: 390, height: 844 }, { width: 768, height: 1024 }, { width: 844, height: 390 }]) {
    test(`keeps labelled destinations and dialogs usable at ${viewport.width}x${viewport.height}`, async ({ page, mockWorkspace, nodeFactory, browserName }) => {
      await page.setViewportSize(viewport);
      const workspace = await mockWorkspace({ categories: [work, health], nodesByCategoryId: {
        1: [nodeFactory.build({ id: focusBlock.taskId, categoryId: 1, title: focusTitle, emoji: '📝' })],
        2: [nodeFactory.build({ id: 8602, categoryId: 2, title: 'Plan a gentle evening walk', emoji: '🌿' })],
      } });
      await mockCalendarBlocks(page, { blocks: [focusBlock] });
      await mockHabits(page);
      const boot = workspace.waitForBoot();
      await page.goto('/'); await boot;
      // WebKit emulates tap input but does not report a hardware touch count.
      if (browserName === 'chromium') expect(await page.evaluate(() => navigator.maxTouchPoints)).toBeGreaterThan(0);

      const trigger = page.getByRole('button', { name: 'Open navigation' });
      const navigation = await openNavigation(page);
      for (const label of [/^All workspace/, /^Work workspace/, /^Health workspace/, /^My Day/, /^Habits/, /^Settings/]) {
        const destination = navigation.getByRole('button', { name: label });
        await expect(destination).toBeVisible();
        expect((await destination.boundingBox())!.height).toBeGreaterThanOrEqual(44);
      }
      await screenshot(page, `navigation-${viewport.width}x${viewport.height}-dark`);
      await page.keyboard.press('Escape');
      await expect(navigation).toBeHidden();
      await expect(trigger).toBeFocused();
      await openNavigation(page);
      await navigation.getByRole('button', { name: 'Close navigation' }).tap();
      await expect(navigation).toBeHidden();
      await expect(trigger).toBeFocused();
      await openNavigation(page);
      await page.touchscreen.tap(viewport.width - 2, viewport.height / 2);
      await expect(navigation).toBeHidden();
      await expect(trigger).toBeFocused();

      await selectDestination(page, /^Health workspace/);
      await expect(page.getByRole('heading', { name: 'Health week', exact: true })).toBeVisible();
      await assertNoPageOverflow(page);

      for (const theme of ['dark', 'light'] as const) {
        await selectDestination(page, /^Settings/);
        const settings = page.getByRole('dialog', { name: 'Settings', exact: true });
        await expect(settings).toBeVisible();
        await settings.getByRole('button', { name: 'Appearance', exact: true }).tap();
        await settings.getByTestId(`theme-${theme}-choice`).tap();
        await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
        await screenshot(page, `settings-${viewport.width}x${viewport.height}-${theme}`);
        await settings.getByRole('button', { name: 'Close settings' }).tap();
        await expect(settings).toHaveCount(0);

        await selectDestination(page, /^Habits/);
        await expect(page.getByRole('heading', { name: 'Habits', exact: true })).toBeVisible();
        await expect(page.getByRole('article', { name: 'Read 10 pages', exact: true })).toBeVisible();
        await assertNoPageOverflow(page);
        await assertUtilityClearOfLauncher(page, page.locator('.habits-page'));
        await screenshot(page, `habits-${viewport.width}x${viewport.height}-${theme}`);

        await selectDestination(page, /^My Day/);
        await expect(page.locator('.my-day-agenda-card').filter({ hasText: focusTitle })).toBeVisible();
        await assertNoPageOverflow(page);
        await assertUtilityClearOfLauncher(page, page.locator('.my-day-agenda'));
        await screenshot(page, `my-day-${viewport.width}x${viewport.height}-${theme}`);

        await page.getByRole('button', { name: 'Open calendar', exact: true }).first().tap();
        const calendar = page.locator('.calendar-shell');
        await expect(calendar).toBeVisible();
        await calendar.getByRole('button', { name: 'Week', exact: true }).tap();
        const agenda = calendar.getByRole('grid', { name: 'Shared calendar week view' });
        await expect(calendar.getByTestId('calendar-week-day-cell')).toHaveCount(7);
        const alignment = () => calendar.evaluate(element => {
          const headers = [...element.querySelectorAll('.calendar-date-axis-cell')].map(item => item.getBoundingClientRect());
          const columns = [...element.querySelectorAll('.calendar-week-time-column')].map(item => item.getBoundingClientRect());
          return headers.map((header, index) => ({
            headerWidth: header.width, columnWidth: columns[index].width,
            offset: Math.abs(header.left - columns[index].left),
          }));
        });
        for (const column of await alignment()) {
          // Allow one CSS pixel for the shared grid's border at a 160px minimum.
          expect(column.headerWidth).toBeGreaterThanOrEqual(159);
          expect(column.columnWidth).toBeGreaterThanOrEqual(159);
          expect(column.offset).toBeLessThanOrEqual(2);
        }
        if (viewport.width <= 390) {
          expect(await agenda.evaluate(element => element.scrollWidth)).toBeGreaterThan(await agenda.evaluate(element => element.clientWidth));
          await agenda.evaluate(element => { element.scrollLeft = element.scrollWidth - element.clientWidth; });
          expect(await agenda.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
          for (const column of await alignment()) expect(column.offset).toBeLessThanOrEqual(2);
        }
        const sunday = calendar.locator('.calendar-date-axis-cell[data-calendar-date="2026-10-11"] > .calendar-date-select');
        await sunday.tap();
        await expect(sunday).toHaveAttribute('aria-pressed', 'true');
        await expect(calendar.getByTestId('calendar-day-drop-zone')).toHaveAttribute('data-calendar-date', '2026-10-11');
        if (viewport.width === 390) await screenshot(page, `calendar-week-${theme}`);
        await calendar.getByRole('button', { name: 'Day', exact: true }).tap();
        await expect(calendar.getByRole('grid', { name: 'Shared calendar day agenda' })).toBeVisible();
        await expect(calendar.getByTestId('calendar-day-drop-zone')).toHaveAttribute('data-calendar-date', '2026-10-11');

        await calendar.getByRole('button', { name: 'Month', exact: true }).tap();
        await expect(calendar.getByTestId('calendar-month-day-cell')).toHaveCount(42);
        await expect(calendar.locator('.calendar-month-weekdays > span')).toHaveCount(7);
        const monthColumnWidths = await calendar.locator('.calendar-month-cell').evaluateAll(elements => elements.slice(0, 7).map(element => element.getBoundingClientRect().width));
        expect(Math.min(...monthColumnWidths)).toBeGreaterThanOrEqual(43);
        expect(Math.max(...monthColumnWidths) - Math.min(...monthColumnWidths)).toBeLessThanOrEqual(2);
        const friday = calendar.locator('.calendar-month-cell[data-calendar-date="2026-10-09"] > header > .calendar-date-select');
        await friday.tap();
        await expect(friday).toHaveAttribute('aria-pressed', 'true');
        if (viewport.width === 390) await screenshot(page, `calendar-month-${theme}`);
        await calendar.getByRole('button', { name: 'Day', exact: true }).tap();
        await expect(calendar.getByTestId('calendar-day-drop-zone')).toHaveAttribute('data-calendar-date', '2026-10-09');
        await calendar.locator('[data-calendar-block-id="9601"]').scrollIntoViewIfNeeded();
        if (viewport.width === 390) await screenshot(page, `calendar-day-${theme}`);
        await assertNoPageOverflow(page);
      }
    });
  }

  test('compact chat keeps a draft through minimize, resize, and simulated visual viewport geometry', async ({ page, mockWorkspace }) => {
    const workspace = await mockWorkspace(); await mockCalendarBlocks(page);
    const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
    await page.getByRole('button', { name: 'Open chat' }).tap();
    const dialog = page.getByRole('dialog', { name: 'Drasil chat' });
    const close = dialog.getByRole('button', { name: 'Minimize chat' });
    await expect(close).toBeFocused();
    expect(await dialog.evaluate(element => element.matches(':modal'))).toBe(true);
    await expect(dialog.getByRole('button', { name: 'Expand chat' })).toHaveCount(0);
    await expect(page.locator('.chat-launcher')).toBeHidden();
    const input = dialog.getByLabel('AI workspace prompt');
    await input.fill('Help me make time for a walk after I finish the project handoff.');

    // This simulates geometry events; it is not a hardware keyboard test.
    await page.evaluate(() => {
      const viewport = window.visualViewport!;
      for (const [key, value] of Object.entries({ height: 430, width: 390, offsetTop: 72, offsetLeft: 0, scale: 1 })) {
        Object.defineProperty(viewport, key, { configurable: true, get: () => value });
      }
      viewport.dispatchEvent(new Event('resize'));
      viewport.dispatchEvent(new Event('scroll'));
    });
    await expect.poll(() => page.evaluate(() => document.documentElement.style.getPropertyValue('--keyboard-inset'))).toBe('342px');
    const bounds = (await dialog.boundingBox())!;
    expect(bounds.x).toBeCloseTo(0, 0); expect(bounds.y).toBeCloseTo(72, 0);
    expect(bounds.width).toBeCloseTo(390, 0); expect(bounds.height).toBeCloseTo(430, 0);
    const inputBounds = (await input.boundingBox())!;
    expect(inputBounds.y + inputBounds.height).toBeLessThanOrEqual(503);
    await screenshot(page, 'chat-simulated-geometry-dark');
    await close.tap();
    await expect(page.getByRole('button', { name: 'Open chat' })).toBeFocused();
    await page.evaluate(() => {
      const viewport = window.visualViewport!;
      for (const key of ['height', 'width', 'offsetTop', 'offsetLeft', 'scale']) Reflect.deleteProperty(viewport, key);
      viewport.dispatchEvent(new Event('resize'));
      viewport.dispatchEvent(new Event('scroll'));
    });
    await page.setViewportSize({ width: 844, height: 390 });
    await page.getByRole('button', { name: 'Open chat' }).tap();
    await expect(input).toHaveValue('Help me make time for a walk after I finish the project handoff.');
    await expect(close).toBeFocused();
    await expect.poll(async () => (await dialog.boundingBox())!.height).toBe(390);
    await screenshot(page, 'chat-landscape-dark');
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(page.getByRole('button', { name: 'Open chat' })).toBeFocused();
  });

  test('habit check-ins keep order and survive a compact editor and orientation change', async ({ page, mockWorkspace }) => {
    const workspace = await mockWorkspace(); await mockCalendarBlocks(page);
    const habits = await mockHabits(page);
    const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
    await selectDestination(page, /^Habits/);
    const row = page.getByRole('article', { name: 'Read 10 pages', exact: true });
    await row.getByRole('button', { name: 'Check in Read 10 pages', exact: true }).tap();
    await expect(row.getByRole('button', { name: 'Undo Read 10 pages today', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('.habit-list article')).toHaveText([
      /Read 10 pages/, /Take a 15-minute walk/, /Drink a glass of water/,
    ]);
    expect(habits.writes).toHaveLength(1);
    await row.getByRole('button', { name: 'Edit Read 10 pages', exact: true }).tap();
    const editor = page.getByRole('dialog', { name: 'Edit habit', exact: true });
    await expect(editor).toBeVisible();
    expect(await editor.evaluate(element => element.matches(':modal'))).toBe(true);
    await editor.getByLabel('Habit name', { exact: true }).fill('Read 12 pages');
    await page.setViewportSize({ width: 844, height: 390 });
    await expect(editor.getByLabel('Habit name', { exact: true })).toHaveValue('Read 12 pages');
    const save = editor.getByRole('button', { name: 'Save habit', exact: true });
    await assertReachable(save, page);
    await screenshot(page, 'habit-editor-landscape-dark');
    await save.tap();
    await expect(editor).toHaveCount(0);
    const edited = page.getByRole('article', { name: 'Read 12 pages', exact: true });
    await expect(edited.getByRole('button', { name: 'Undo Read 12 pages today', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(edited.getByRole('button', { name: 'Edit Read 12 pages', exact: true })).toBeFocused();
    expect(habits.writes).toHaveLength(2);
    await page.setViewportSize({ width: 320, height: 740 });
    const history = edited.locator('.habit-week');
    await history.getByRole('button', { name: 'Undo Read 12 pages on 2026-10-09', exact: true }).scrollIntoViewIfNeeded();
    await expect(history.getByRole('button', { name: 'Undo Read 12 pages on 2026-10-09', exact: true })).toBeInViewport();
    await assertNoPageOverflow(page);
    const milestones = page.locator('.habit-milestones');
    await expect(milestones).not.toHaveAttribute('open', '');
    await milestones.locator('summary').tap();
    await expect(milestones).toHaveAttribute('open', '');
  });

  test('short-height task composer and calendar details can save without losing fields', async ({ page, mockWorkspace, nodeFactory }) => {
    await page.setViewportSize({ width: 390, height: 380 });
    const nodes = [nodeFactory.build({ id: focusBlock.taskId, categoryId: 1, title: focusTitle })];
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { 1: nodes } });
    const calendar = await mockCalendarBlocks(page, { blocks: [focusBlock] });
    const writes: Record<string, unknown>[] = [];
    await page.route('**/api/v1/nodes', route => {
      const payload = route.request().postDataJSON(); writes.push(payload);
      const created = nodeFactory.build({ ...payload, id: 8603 }); nodes.push(created);
      return route.fulfill({ status: 201, json: created });
    });
    const mutations: string[] = [];
    page.on('request', request => {
      if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method()) && /\/api\/v1\/(nodes|calendar-blocks)(?:\/|\?|$)/.test(request.url())) {
        mutations.push(`${request.method()} ${new URL(request.url()).pathname}`);
      }
    });
    const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
    const bookedTask = page.locator('[data-week-day="2026-10-09"] [data-week-task="8601"]');
    const options = bookedTask.getByRole('button', { name: `Options for ${focusTitle}`, exact: true });
    await assertReachable(options, page);
    await options.tap();
    await page.getByRole('menu', { name: `Actions for ${focusTitle}`, exact: true })
      .getByRole('menuitem', { name: 'Remove from this day', exact: true }).tap();
    const confirmation = bookedTask.getByRole('dialog', { name: /^Remove .* from this day\?$/ });
    await expect(confirmation).toBeVisible();
    expect(await confirmation.evaluate(element => element.matches(':modal'))).toBe(true);
    await confirmation.getByRole('button', { name: 'Cancel', exact: true }).tap();
    await expect(confirmation).toBeHidden();
    await expect(bookedTask).toBeFocused();
    expect(mutations).toEqual([]);
    expect(calendar.updatedPayloads).toHaveLength(0);
    expect(calendar.deletedIds).toHaveLength(0);
    await page.locator('[data-week-day="2026-10-09"] .week-add-task').tap();
    const composer = page.getByRole('dialog', { name: 'Add a task', exact: true });
    await composer.getByLabel('Task description', { exact: true }).fill('Book the community centre swimming class');
    await composer.getByLabel('Task notes', { exact: true }).fill('Choose a weekday evening and confirm the start date.');
    await composer.getByLabel('Estimate', { exact: true }).fill('20');
    const add = composer.getByRole('button', { name: 'Add task', exact: true });
    await assertReachable(add, page);
    await screenshot(page, 'composer-short-height-dark');
    await add.tap();
    await expect(composer).toHaveCount(0);
    expect(writes[0]).toMatchObject({ title: 'Book the community centre swimming class', notes: 'Choose a weekday evening and confirm the start date.', timeEstimateMinutes: 20 });
    expect(calendar.createdPayloads).toHaveLength(0);

    await page.getByRole('navigation', { name: 'Workspace views' }).getByRole('button', { name: 'Calendar', exact: true }).tap();
    const block = page.locator('[data-calendar-block-id="9601"]');
    await block.scrollIntoViewIfNeeded();
    await block.tap();
    const details = page.getByRole('dialog', { name: 'Calendar block details' });
    await details.getByRole('button', { name: 'Edit calendar block' }).tap();
    await details.getByLabel('Calendar block start date and time').fill('2026-10-09T08:30');
    await details.getByLabel('Calendar block end date and time').fill('2026-10-09T12:30');
    const save = details.getByRole('button', { name: 'Save calendar block time' });
    await assertReachable(save, page);
    await screenshot(page, 'calendar-details-short-height-dark');
    await save.tap();
    await expect.poll(() => calendar.updatedPayloads.length).toBe(1);
    expect(calendar.updatedPayloads[0].payload).toMatchObject({ startAt: '2026-10-09T08:30:00', endAt: '2026-10-09T12:30:00' });
    await assertNoPageOverflow(page);
  });

  test('a native touch swipe scrolls a booking without rescheduling', async ({ page, mockWorkspace, nodeFactory, browserName }) => {
    test.skip(browserName !== 'chromium', 'CDP touch input is Chromium-only; both engines cover compact layouts and dialogs.');
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: {
      1: [nodeFactory.build({ id: focusBlock.taskId, categoryId: 1, title: focusTitle })],
    } });
    const calendar = await mockCalendarBlocks(page, { blocks: [focusBlock] });
    const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
    await page.getByRole('navigation', { name: 'Workspace views' }).getByRole('button', { name: 'Calendar', exact: true }).tap();
    const block = page.locator('[data-calendar-block-id="9601"]');
    await expect(block).toBeVisible();
    const agenda = page.locator('.calendar-agenda');
    await block.evaluate(element => {
      const scroll = element.closest<HTMLElement>('.calendar-agenda')!;
      scroll.scrollTop += element.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 80;
    });
    const bounds = (await block.boundingBox())!;
    const agendaBounds = (await agenda.boundingBox())!;
    const x = bounds.x + bounds.width * .65;
    const y = Math.min(bounds.y + bounds.height - 25, agendaBounds.y + agendaBounds.height - 35);
    expect(y - 130).toBeGreaterThan(Math.max(bounds.y, agendaBounds.y));
    const before = await agenda.evaluate(element => element.scrollTop);
    await touchDrag(page, { x, y }, { x, y: y - 130 });
    await expect.poll(() => agenda.evaluate(element => element.scrollTop)).toBeGreaterThan(before + 30);
    await waitForNativeScrollToSettle(agenda);
    expect(calendar.updatedPayloads).toHaveLength(0);
    await expect(page.getByRole('dialog', { name: 'Calendar block details' })).toHaveCount(0);
    expect(calendar.blocks[0]).toMatchObject({ startAt: focusBlock.startAt, endAt: focusBlock.endAt });
    await screenshot(page, 'calendar-after-native-touch-scroll-dark');
  });

  test('an explicit touch Move control updates one booking and preserves duration', async ({ page, mockWorkspace, nodeFactory, browserName }) => {
    test.skip(browserName !== 'chromium', 'CDP touch input is Chromium-only; both engines cover compact layouts and dialogs.');
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: {
      1: [nodeFactory.build({ id: focusBlock.taskId, categoryId: 1, title: focusTitle })],
    } });
    const calendar = await mockCalendarBlocks(page, { blocks: [focusBlock] });
    const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
    await page.getByRole('navigation', { name: 'Workspace views' }).getByRole('button', { name: 'Calendar', exact: true }).tap();
    const block = page.locator('[data-calendar-block-id="9601"]');
    const agenda = page.locator('.calendar-agenda');
    // Each native gesture has a fresh page. Chromium CDP sometimes suppresses a
    // subsequent tap's click after panning, despite delivered pointer/touch events.
    const bookingTitle = block.locator('.calendar-block-title');
    await bookingTitle.scrollIntoViewIfNeeded();
    await waitForNativeScrollToSettle(agenda);
    const target = (await block.boundingBox())!;
    const visibleAgenda = (await agenda.boundingBox())!;
    const destination = { x: target.x + target.width / 2, y: Math.min(target.y + target.height - 30, visibleAgenda.y + visibleAgenda.height - 30) };
    await bookingTitle.tap();
    const details = page.getByRole('dialog', { name: 'Calendar block details' });
    await expect(details).toBeVisible();
    for (const name of ['Move calendar block', 'Resize calendar block start', 'Resize calendar block end']) {
      const control = details.getByRole('button', { name, exact: true });
      await expect(control).toBeVisible();
      expect((await control.boundingBox())!.height).toBeGreaterThanOrEqual(44);
    }
    const move = (await details.getByRole('button', { name: 'Move calendar block', exact: true }).boundingBox())!;
    await touchDrag(page, { x: move.x + move.width / 2, y: move.y + move.height / 2 }, destination);
    await expect.poll(() => calendar.updatedPayloads.length).toBe(1);
    const saved = calendar.updatedPayloads[0].payload;
    expect(saved.startAt).not.toBe(focusBlock.startAt);
    expect(Date.parse(String(saved.endAt)) - Date.parse(String(saved.startAt))).toBe(4 * 60 * 60 * 1000);
    await screenshot(page, 'calendar-after-explicit-touch-move-dark');
  });
});

test.describe('desktop preservation', () => {
  test.use({ hasTouch: false, isMobile: false, viewport: { width: 1440, height: 1000 } });

  test('retains desktop sidebar, adjacent planning/calendar, and chat expansion', async ({ page, mockWorkspace, mockStrategistApi }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
      authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
    } }));
    const workspace = await mockWorkspace(); await mockStrategistApi(); await mockCalendarBlocks(page);
    const boot = workspace.waitForBoot(); await page.goto('/'); await boot;
    await expect(page.getByRole('button', { name: 'Open navigation' })).toHaveCount(0);
    await expect(page.getByRole('complementary', { name: 'Primary workspace navigation' })).toBeVisible();
    await expect(page.locator('.panel-planning')).toBeVisible();
    await expect(page.locator('.panel-calendar')).toBeVisible();
    await expect(page.getByRole('navigation', { name: 'Workspace views' })).toBeHidden();
    for (const theme of ['dark', 'light']) {
      await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
      await screenshot(page, `desktop-${theme}`);
    }
    await page.getByRole('button', { name: 'Open chat' }).click();
    const chat = page.getByRole('dialog', { name: 'Drasil chat' });
    expect(await chat.evaluate(element => element.matches(':modal'))).toBe(false);
    await expect(chat.getByRole('button', { name: 'Expand chat' })).toBeVisible();
    await chat.getByRole('button', { name: 'Expand chat' }).click();
    await expect(chat.getByRole('button', { name: 'Restore chat size' })).toBeVisible();
    expect((await chat.boundingBox())!.width).toBeLessThan(1440);
    await chat.getByRole('button', { name: 'Minimize chat' }).click();
    await expect(page.getByRole('button', { name: 'Open chat' })).toBeFocused();
  });
});
