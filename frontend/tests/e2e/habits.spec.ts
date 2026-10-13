import { test, expect } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';
import type { HabitSnapshot } from '../../src/api/habits';
import type { Page } from '@playwright/test';

async function openHabits(page: Page) {
  const navigation = page.getByRole('button', { name: 'Open navigation', exact: true });
  const habits = page.getByRole('button', { name: 'Habits', exact: true });
  await expect.poll(async () => await navigation.isVisible() || await habits.isVisible()).toBe(true);
  if (await navigation.isVisible()) await navigation.click();
  await habits.click();
}

test.beforeEach(async ({ page, mockWorkspace, mockStrategistApi }) => {
  await page.route('**/api/v1/accounts/status', route => route.fulfill({ json: {
    authenticationRequired: false, needsBootstrap: false, supportsRemoteBackend: false,
  } }));
  await mockWorkspace();
  await mockStrategistApi();
  await mockCalendarBlocks(page);
});

test('weekday habit check-ins persist, undo, recover from failure, edit, archive, and restore', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const timeZone = await page.evaluate(() => Intl.DateTimeFormat().resolvedOptions().timeZone);
  const habitsPath = `/api/v1/habits?timeZone=${encodeURIComponent(timeZone)}`;
  const baseline: HabitSnapshot = await (await page.request.get(habitsPath)).json();
  const name = `Walk habit ${Date.now()}`;
  await page.goto('/');
  await openHabits(page);
  await expect(page.getByRole('heading', { name: 'Habits', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'New habit', exact: true }).click();
  await page.getByLabel('Habit name', { exact: true }).fill(name);
  await page.getByLabel(/When will you do it/).fill('After lunch');
  const today = (new Date(`${baseline.today}T12:00:00`).getDay() + 6) % 7;
  const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  for (let index = 0; index < days.length; index++) {
    if (index !== today) await page.getByRole('button', { name: days[index], exact: true }).click();
  }
  await page.getByRole('button', { name: 'Movement', exact: true }).click();
  await page.getByRole('button', { name: 'Save habit', exact: true }).click();
  const row = page.getByRole('article', { name, exact: true });
  await expect(row).toBeVisible();
  await row.getByRole('button', { name: `Check in ${name}`, exact: true }).click();
  await expect(row.getByRole('button', { name: `Undo ${name} today`, exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.habit-level').getByText(`${baseline.totalXp + 10} XP`, { exact: true })).toBeVisible();
  const saved: HabitSnapshot = await (await page.request.get(habitsPath)).json();
  const habit = saved.habits.find(item => item.title === name)!;
  expect(habit.weekdays).toEqual([today]);
  expect(habit.totalCheckIns).toBe(1);
  await page.reload();
  await openHabits(page);
  await expect(row.getByRole('button', { name: `Undo ${name} today`, exact: true })).toBeVisible();
  await page.route('**/api/v1/habits/*/check-ins/*', route => route.fulfill({ status: 503, json: { detail: 'Check-in could not be saved. Please try again.' } }));
  await row.getByRole('button', { name: `Undo ${name} today`, exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Check-in could not be saved');
  await expect(row.getByRole('button', { name: `Undo ${name} today`, exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.unroute('**/api/v1/habits/*/check-ins/*');
  await row.getByRole('button', { name: `Undo ${name} today`, exact: true }).click();
  await expect(page.locator('.habit-level').getByText(`${baseline.totalXp} XP`, { exact: true })).toBeVisible();
  await row.getByRole('button', { name: `Edit ${name}`, exact: true }).click();
  await page.getByLabel('Habit name', { exact: true }).fill(`${name} edited`);
  await page.getByRole('button', { name: 'Save habit', exact: true }).click();
  const edited = page.getByRole('article', { name: `${name} edited`, exact: true });
  await edited.getByRole('button', { name: `Check in ${name} edited`, exact: true }).click();
  await expect(edited.getByRole('button', { name: `Undo ${name} edited today`, exact: true })).toBeVisible();
  await edited.getByRole('button', { name: `Archive ${name} edited`, exact: true }).click();
  await expect(edited).toHaveCount(0);
  await expect(page.locator('.habit-level').getByText(`${baseline.totalXp + 10} XP`, { exact: true })).toBeVisible();
  await page.getByRole('button', { name: /Archived habits/ }).click();
  await page.locator('.habit-archived-row').filter({ hasText: `${name} edited` }).getByRole('button', { name: 'Restore', exact: true }).click();
  await expect(edited).toBeVisible();
  await expect(edited.getByRole('button', { name: `Undo ${name} edited today`, exact: true })).toBeVisible();
});

test('habit page visual states, history interaction, keyboard, and responsive themes', async ({ page }) => {
  const snapshot: HabitSnapshot = {
    today: '2026-10-09', totalXp: 270, level: 3, levelXp: 70, totalCheckIns: 27, bestStreak: 8,
    badges: [
      { id: 'first', title: 'First step', description: 'Complete your first check-in.', earned: true },
      { id: 'ten', title: 'Finding rhythm', description: 'Make 10 check-ins.', earned: true },
      { id: 'streak', title: 'Steady seven', description: 'Build a 7-check-in streak on one habit.', earned: true },
      { id: 'fifty', title: 'Showing up', description: 'Make 50 check-ins.', earned: false },
    ], habits: [],
  };
  const titles = ['Read 10 pages', 'Take a 15-minute walk', 'Drink a glass of water'];
  snapshot.habits = titles.map((title, index) => ({
    id: `visual-${index}`, title, cue: ['After my morning coffee', 'After lunch, before I sit down again', 'When I start my day'][index],
    icon: ['book', 'activity', 'droplet'][index], weekdays: [0, 1, 2, 3, 4, 5, 6], createdOn: '2026-09-01', archived: false,
    currentStreak: [6, 2, 8][index], bestStreak: [6, 4, 8][index], totalCheckIns: [9, 7, 11][index],
    week: Array.from({ length: 7 }, (_, offset) => ({ date: `2026-10-0${offset + 3}`, scheduled: true,
      completed: index === 2 || (index === 0 ? offset < 6 : offset === 4 || offset === 5) })),
  }));
  await page.route('**/api/v1/habits**', async route => {
    const match = route.request().url().match(/habits\/(visual-\d)\/check-ins\/(\d{4}-\d{2}-\d{2})/);
    if (match && route.request().method() === 'PUT') {
      const completed = route.request().postDataJSON().completed;
      const day = snapshot.habits.find(habit => habit.id === match[1])!.week.find(day => day.date === match[2])!;
      snapshot.totalXp += completed === day.completed ? 0 : completed ? 10 : -10;
      snapshot.levelXp = snapshot.totalXp % 100;
      day.completed = completed;
    }
    await route.fulfill({ json: snapshot });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/');
  await openHabits(page);
  await expect(page.getByRole('heading', { name: 'Read 10 pages', exact: true })).toBeVisible();
  const originalOrder = await page.getByRole('article').evaluateAll(rows => rows.map(row => row.getAttribute('aria-label')));
  await page.getByRole('button', { name: 'Check in Read 10 pages on 2026-10-09', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Undo Read 10 pages today', exact: true })).toBeVisible();
  expect(await page.getByRole('article').evaluateAll(rows => rows.map(row => row.getAttribute('aria-label')))).toEqual(originalOrder);
  for (const theme of ['dark', 'light']) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
    await page.locator('.habits-page').evaluate(element => { element.scrollTop = 0; });
    await page.screenshot({ path: `../.impeccable/review/habits-distilled-desktop-${theme}.png` });
  }
  await page.setViewportSize({ width: 390, height: 844 });
  for (const theme of ['dark', 'light']) {
    await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
    await page.locator('.habits-page').evaluate(element => { element.scrollTop = 0; });
    await page.screenshot({ path: `../.impeccable/review/habits-distilled-mobile-${theme}.png` });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator('.habits-page').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    const tools = (await page.locator('.habit-row-tools').first().boundingBox())!;
    const trail = (await page.locator('.habit-week').first().boundingBox())!;
    expect(tools.y + tools.height).toBeLessThanOrEqual(trail.y);
    for (const target of await page.locator('.habits-page .habit-button, .habit-row-tools .habit-icon-button, .habit-week button').all()) {
      const bounds = (await target.boundingBox())!;
      expect(bounds.width).toBeGreaterThanOrEqual(44);
      expect(bounds.height).toBeGreaterThanOrEqual(44);
    }
    const milestones = page.locator('.habit-milestones');
    if (!(await milestones.evaluate(element => (element as HTMLDetailsElement).open))) await milestones.locator('summary').click();
    await page.locator('.habits-page').evaluate(element => { element.scrollTop = element.scrollHeight; });
    await page.screenshot({ path: `../.impeccable/review/habits-distilled-mobile-rewards-${theme}.png` });
  }
  await page.getByRole('button', { name: 'New habit', exact: true }).click();
  const editorDialog = page.getByRole('dialog', { name: 'New habit', exact: true });
  await expect(editorDialog).toBeVisible();
  await expect(page.getByLabel('Habit name', { exact: true })).toBeFocused();
  await page.getByLabel('Habit name', { exact: true }).fill('A habit with a longer title that still fits on a small screen');
  await page.screenshot({ path: '../.impeccable/review/habits-distilled-editor-mobile-top.png' });
  const save = page.getByRole('button', { name: 'Save habit', exact: true });
  await save.scrollIntoViewIfNeeded();
  const saveRect = (await save.boundingBox())!;
  const dialogRect = (await editorDialog.boundingBox())!;
  expect(saveRect.y + saveRect.height).toBeLessThanOrEqual(dialogRect.y + dialogRect.height);
  expect(saveRect.y + saveRect.height).toBeLessThanOrEqual(844);
  await page.screenshot({ path: '../.impeccable/review/habits-distilled-editor-mobile.png' });
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect(page.getByRole('button', { name: 'New habit', exact: true })).toBeFocused();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.getByRole('button', { name: 'Check in Take a 15-minute walk', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Today complete', exact: true })).toBeVisible();
  expect(await page.locator('.habit-reward-sparks i').first().evaluate(element => getComputedStyle(element).animationName)).toBe('none');
  snapshot.habits = [];
  await page.reload();
  await openHabits(page);
  await expect(page.getByRole('heading', { name: 'No habits yet', exact: true })).toBeVisible();
  await expect(page.getByRole('progressbar', { name: "Today's habit progress", exact: true })).toHaveCount(0);
  await page.screenshot({ path: '../.impeccable/review/habits-distilled-empty-mobile.png' });
  await page.getByRole('button', { name: 'Read 10 pages', exact: true }).click();
  await expect(page.getByLabel('Habit name', { exact: true })).toHaveValue('Read 10 pages');
  for (const day of ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']) {
    await page.getByRole('button', { name: day, exact: true }).click();
  }
  await expect(page.getByText('Choose at least one day.', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save habit', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Friday', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Save habit', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  snapshot.habits = [{
    id: 'visual-0', title: 'Read 10 pages', cue: '', icon: 'book', weekdays: [0], createdOn: '2026-09-01', archived: false,
    currentStreak: 1, bestStreak: 1, totalCheckIns: 1,
    week: Array.from({ length: 7 }, (_, offset) => ({ date: `2026-10-0${offset + 3}`, scheduled: offset === 2, completed: offset === 2 })),
  }];
  snapshot.habits[0].archived = true;
  await page.reload();
  await openHabits(page);
  await expect(page.getByRole('heading', { name: 'No active habits', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Archived habits (1)', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Restore', exact: true })).toBeVisible();
  await page.screenshot({ path: '../.impeccable/review/habits-distilled-archived-mobile.png' });
  snapshot.habits[0].archived = false;
  await page.reload();
  await openHabits(page);
  await expect(page.getByText('No habits scheduled today.', { exact: true })).toBeVisible();
  await expect(page.getByRole('progressbar', { name: "Today's habit progress", exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Check in Read 10 pages', exact: true })).toBeDisabled();
  await page.getByRole('button', { name: 'Edit Read 10 pages', exact: true }).click();
  await expect(page.getByText('Schedule changes start today.', { exact: true })).toBeVisible();
});

test.describe('compact habit touch interactions', () => {
  test.use({ hasTouch: true });

  test('narrow history scrolls without check-ins, saves stay local, and the editor fits a short viewport', async ({ page }) => {
    const snapshot: HabitSnapshot = {
      today: '2026-10-09', totalXp: 0, level: 1, levelXp: 0, totalCheckIns: 0, bestStreak: 0,
      badges: [{ id: 'first', title: 'First step', description: 'Complete your first check-in.', earned: false }],
      habits: ['Read 10 pages', 'Take a 15-minute walk', 'Drink a glass of water'].map((title, index) => ({
        id: `touch-${index}`, title, cue: '', icon: 'sprout', weekdays: [0, 1, 2, 3, 4, 5, 6],
        createdOn: '2026-10-01', archived: false, currentStreak: 0, bestStreak: 0, totalCheckIns: 0,
        week: Array.from({ length: 7 }, (_, offset) => ({ date: `2026-10-0${offset + 3}`, scheduled: true, completed: false })),
      })),
    };
    let checkInRequests = 0;
    let releaseSave: (() => void) | undefined;
    const saveResponse = new Promise<void>(resolve => { releaseSave = resolve; });
    await page.route('**/api/v1/habits**', async route => {
      const request = route.request();
      const match = request.url().match(/habits\/(touch-\d)\/check-ins\/(\d{4}-\d{2}-\d{2})/);
      if (match && request.method() === 'PUT') {
        checkInRequests += 1;
        await saveResponse;
        const completed = request.postDataJSON().completed;
        snapshot.habits.find(habit => habit.id === match[1])!.week.find(day => day.date === match[2])!.completed = completed;
        snapshot.totalXp += completed ? 10 : -10;
        snapshot.levelXp = snapshot.totalXp;
      } else if (request.method() === 'PUT') {
        await route.fulfill({ status: 503, json: { detail: 'Habit could not be saved. Try again.' } });
        return;
      }
      await route.fulfill({ json: snapshot });
    });
    await page.setViewportSize({ width: 320, height: 720 });
    await page.goto('/');
    await openHabits(page);
    const rows = page.locator('.habit-row');
    const row = page.getByRole('article', { name: 'Read 10 pages', exact: true });
    await expect(row).toBeVisible();
    await expect(rows).toHaveCount(3);
    const originalOrder = await rows.evaluateAll(elements => elements.map(element => element.getAttribute('aria-label')));
    await expect(page.locator('.habit-milestones')).toHaveJSProperty('open', false);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator('.habits-page').evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
    const history = row.locator('.habit-week');
    await history.scrollIntoViewIfNeeded();
    expect(await history.evaluate(element => element.scrollWidth > element.clientWidth)).toBe(true);
    for (const target of await history.getByRole('button').all()) {
      const bounds = (await target.boundingBox())!;
      expect(bounds.width).toBeGreaterThanOrEqual(44);
      expect(bounds.height).toBeGreaterThanOrEqual(44);
    }

    const touch = await page.context().newCDPSession(page);
    const swipe = async (x: number, y: number, dx: number, dy: number) => {
      await touch.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y }] });
      for (let step = 1; step <= 8; step++) {
        await touch.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + dx * step / 8, y: y + dy * step / 8 }] });
        await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => resolve())));
      }
      await touch.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    };
    const waitForScrollSettled = async () => page.evaluate(async () => {
      const scrollers = Array.from(document.querySelectorAll<HTMLElement>('.habits-page, .habit-week'));
      const positions = () => scrollers.map(element => `${element.scrollTop},${element.scrollLeft}`).join(';');
      let previous = positions();
      let unchangedSince = performance.now();
      await new Promise<void>(resolve => {
        const frame = (now: number) => {
          const current = positions();
          if (current !== previous) { previous = current; unchangedSince = now; }
          if (now - unchangedSince >= 250) resolve();
          else requestAnimationFrame(frame);
        };
        requestAnimationFrame(frame);
      });
    });
    const beforeScroll = await page.locator('.habits-page').evaluate(element => element.scrollTop);
    let trail = (await history.boundingBox())!;
    await swipe(trail.x + trail.width / 2, trail.y + 28, 0, -90);
    await expect.poll(() => page.locator('.habits-page').evaluate(element => element.scrollTop)).toBeGreaterThan(beforeScroll);
    await waitForScrollSettled();
    expect(checkInRequests).toBe(0);
    await history.scrollIntoViewIfNeeded();
    trail = (await history.boundingBox())!;
    await swipe(trail.x + trail.width - 30, trail.y + 28, -90, 0);
    await expect.poll(() => history.evaluate(element => element.scrollLeft)).toBeGreaterThan(0);
    await waitForScrollSettled();
    expect(checkInRequests).toBe(0);

    // Chromium CDP can suppress compatibility clicks after synthetic scrolling.
    // Validate saving in a fresh document, independently of the no-write swipe checks.
    await touch.detach();
    await page.reload();
    await openHabits(page);
    await expect(row).toBeVisible();
    await expect(rows).toHaveCount(3);
    try {
      const checkIn = row.getByRole('button', { name: 'Check in Read 10 pages', exact: true });
      await checkIn.scrollIntoViewIfNeeded();
      await waitForScrollSettled();
      const saveRequest = page.waitForRequest(request => request.method() === 'PUT'
        && request.url().includes('/api/v1/habits/touch-0/check-ins/2026-10-09'), { timeout: 10_000 });
      await checkIn.tap();
      await saveRequest;
      const saving = row.getByRole('button', { name: 'Saving Read 10 pages', exact: true });
      await expect(saving).toHaveAttribute('aria-busy', 'true');
      await expect(saving).toHaveText('Saving…');
      expect(await rows.evaluateAll(elements => elements.map(element => element.getAttribute('aria-label')))).toEqual(originalOrder);
    } finally { releaseSave?.(); }
    await expect(row.getByRole('button', { name: 'Undo Read 10 pages today', exact: true })).toBeVisible();
    expect(await rows.evaluateAll(elements => elements.map(element => element.getAttribute('aria-label')))).toEqual(originalOrder);
    await row.getByRole('button', { name: 'Undo Read 10 pages today', exact: true }).tap();
    await expect(row.getByRole('button', { name: 'Check in Read 10 pages', exact: true })).toBeVisible();
    expect(await rows.evaluateAll(elements => elements.map(element => element.getAttribute('aria-label')))).toEqual(originalOrder);

    const edit = row.getByRole('button', { name: 'Edit Read 10 pages', exact: true });
    await edit.scrollIntoViewIfNeeded();
    const beforeEditor = await page.locator('.habits-page').evaluate(element => element.scrollTop);
    await edit.tap();
    const dialog = page.getByRole('dialog', { name: 'Edit habit', exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByLabel('Habit name', { exact: true })).toBeFocused();
    await page.setViewportSize({ width: 320, height: 420 });
    const save = dialog.getByRole('button', { name: 'Save habit', exact: true });
    await expect.poll(async () => {
      const bounds = (await save.boundingBox())!;
      return bounds.y + bounds.height;
    }).toBeLessThanOrEqual(420);
    expect(await dialog.locator('.habit-editor-body').evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
    for (const target of await dialog.getByRole('button').all()) {
      const bounds = (await target.boundingBox())!;
      expect(bounds.width).toBeGreaterThanOrEqual(44);
      expect(bounds.height).toBeGreaterThanOrEqual(44);
    }
    await page.setViewportSize({ width: 320, height: 720 });
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(edit).toBeFocused();
    await expect.poll(() => page.locator('.habits-page').evaluate(element => element.scrollTop)).toBe(beforeEditor);
    await edit.tap();
    await expect(dialog).toBeVisible();
    await save.tap();
    await expect(dialog.getByRole('alert')).toContainText('Habit could not be saved');
    await expect(dialog.getByLabel('Habit name', { exact: true })).toHaveValue('Read 10 pages');
    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(edit).toBeFocused();
  });
});
