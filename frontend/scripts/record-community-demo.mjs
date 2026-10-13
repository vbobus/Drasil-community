// Record the real frontend with fictional, in-memory responses and no backend.
import { chromium, expect } from '@playwright/test';
import { createServer } from 'vite';
import react from '@vitejs/plugin-react';
import { mkdtemp, mkdir, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { categories, createDemoState, demoDay, reviewResult } from './community-demo-data.mjs';

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const root = path.dirname(frontend);
const output = path.resolve(root, '.tmp/community-demo');
const assets = path.resolve(root, 'docs/assets');
const emptyEnv = await mkdtemp(path.join(tmpdir(), 'drasil-demo-env-'));
// Ignore ambient frontend variables as well as repository environment files.
for (const name of Object.keys(process.env)) if (name.startsWith('VITE_')) delete process.env[name];
await mkdir(output, { recursive: true });
await mkdir(assets, { recursive: true });
// configFile:false and an empty envDir prevent ambient backend URLs and .env loads.
const server = await createServer({ root: frontend, configFile: false, envDir: emptyEnv,
  plugins: [react()], server: { host: '127.0.0.1', port: 4186, strictPort: true, proxy: {},
    fs: { allow: [root] } }, logLevel: 'warn' });
await server.listen();
const origin = 'http://127.0.0.1:4186';
const browser = await chromium.launch();
const state = createDemoState();
const unexpected = [];
const requests = [];
const context = await browser.newContext({ viewport: { width: 1440, height: 900 },
  timezoneId: 'UTC', locale: 'en-US', serviceWorkers: 'block',
  recordVideo: { dir: output, size: { width: 1440, height: 900 } } });

async function routeDemo(route) {
  const request = route.request();
  const url = new URL(request.url());
  // Fonts/integrations/analytics cannot reach another origin, even by accident.
  if (url.origin !== origin) return route.abort('blockedbyclient');
  if (!url.pathname.startsWith('/api/')) return route.continue();
  const endpoint = url.pathname;
  const method = request.method();
  requests.push({ method, endpoint });
  const json = async (data, status = 200) => route.fulfill({ status, json: data });
  if (endpoint === '/api/v1/accounts/status') return json({ authenticationRequired: false,
    needsBootstrap: false, supportsRemoteBackend: false });
  if (endpoint === '/api/v1/accounts/me') return json({ id: 'demo-account', username: 'demo', displayName: 'Demo account', isAdmin: false });
  if (endpoint === '/api/v1/workspace') return json({ categories, nodes: state.nodes, blocks: state.blocks,
    scheduledTaskIds: state.blocks.map(item => item.taskId), startAt: '2026-09-01T00:00:00', endAt: '2026-11-30T00:00:00' });
  if (endpoint === '/api/v1/categories') return json(categories);
  if (endpoint === '/api/v1/nodes' && method === 'GET') {
    const category = Number(url.searchParams.get('categoryId'));
    return json(state.nodes.filter(item => !category || item.categoryId === category));
  }
  const nodeId = endpoint.match(/^\/api\/v1\/nodes\/(\d+)$/)?.[1];
  if (nodeId && method === 'PATCH') {
    const node = state.nodes.find(item => item.id === Number(nodeId));
    Object.assign(node, request.postDataJSON());
    return json(node);
  }
  if (endpoint === '/api/v1/calendar-blocks/scheduled-task-ids') return json(state.blocks.map(item => item.taskId));
  if (endpoint === '/api/v1/calendar-blocks' && method === 'GET') return json(state.blocks);
  if (endpoint === '/api/v1/habits' && method === 'GET') return json(state.habits);
  const check = endpoint.match(/^\/api\/v1\/habits\/(demo-habit-\d)\/check-ins\/(\d{4}-\d{2}-\d{2})$/);
  if (check && method === 'PUT') {
    const habit = state.habits.habits.find(item => item.id === check[1]);
    const day = habit.week.find(item => item.date === check[2]);
    const completed = request.postDataJSON().completed;
    const change = completed === day.completed ? 0 : completed ? 1 : -1;
    day.completed = completed;
    habit.totalCheckIns += change;
    state.habits.totalCheckIns += change;
    state.habits.totalXp += change * 10;
    state.habits.levelXp = state.habits.totalXp % 100;
    return json(state.habits);
  }
  const habitId = endpoint.match(/^\/api\/v1\/habits\/(demo-habit-\d)$/)?.[1];
  if (habitId && method === 'PUT') {
    Object.assign(state.habits.habits.find(item => item.id === habitId), request.postDataJSON());
    return json(state.habits);
  }
  if (endpoint === '/api/v1/ai/strategist/sessions') return json([]);
  if (endpoint === '/api/v1/ai/strategist' && method === 'POST') return json(reviewResult());
  if (endpoint === '/api/v1/ai/strategist/sessions/demo-chat/context') return json({ ok: true });
  if (endpoint === '/api/v1/ai/strategist/sessions/demo-chat/review-actions' && method === 'POST') {
    const action = request.postDataJSON();
    if (action.action === 'approve' && !state.approved) {
      state.approved = true;
      state.blocks.push({ ...state.blocks[0], id: 904, taskId: 104, title: 'Prepare release notes',
        startAt: `${demoDay}T10:30:00`, endAt: `${demoDay}T11:00:00` });
    }
    return json({ status: 'ok', result: { status: 'execution_complete', reviewId: 'review:demo',
      reviewRevision: 1, sessionId: 'demo-chat', goalId: 'goal:demo', changeSetId: 'changeset:demo',
      summary: 'Release notes scheduled for 10:30–11:00 AM.', goalRevision: 1, changeSetRevision: 1,
      execution: { status: 'ok', result: { executionId: 'execution:demo', commandId: 'command:demo',
        idempotencyKey: 'idem:demo:approve', goalRevision: 1, changeSetRevision: 1, status: 'applied',
        verification: { status: 'verified' }, syncItems: [] } } } });
  }
  if (endpoint === '/api/v1/auth/chatgpt') return json({ provider: 'codex-cli', isAuthenticated: false });
  if (endpoint.startsWith('/api/v1/auth/strategist-provider')) return json({ activeProvider: 'codex-cli',
    activeProviderLabel: 'ChatGPT', codexModelOptions: [], isOpenAiKeyConfigured: false, openAiModel: '' });
  if (endpoint === '/api/v1/calendar-integrations/google/status') return json({ provider: 'google', isConfigured: false, isConnected: false, scopes: [] });
  if (endpoint.startsWith('/api/v1/companion')) return json({ enabled: false, paired: false, unreadCount: 0, proposals: [] });
  unexpected.push({ method, endpoint });
  return json({ detail: 'Unmocked demo endpoint; no request reached a backend.' }, 501);
}
await context.route('**/*', routeDemo);
await context.addInitScript(() => {
  localStorage.setItem('drasil.appearance-theme.v1', 'dark');
  localStorage.setItem('drasil.backend-url.v1', '');
  sessionStorage.setItem('drasil.account.v1', JSON.stringify({ id: 'demo-account', username: 'demo', displayName: 'Demo account', isAdmin: false }));
});
const page = await context.newPage();
await page.clock.setFixedTime(new Date(`${demoDay}T09:15:00Z`));
const video = page.video();
const pause = ms => page.waitForTimeout(ms);
const capture = name => page.screenshot({ path: path.join(assets, name), animations: 'disabled' });
async function click(control) {
  await control.scrollIntoViewIfNeeded();
  const box = await control.boundingBox();
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 12 });
  await control.click();
}
try {
  await page.goto(origin);
  await expect(page.getByRole('region', { name: 'Weekly planning' })).toBeVisible();
  await pause(2200);
  await capture('community-workspace.png');
  await click(page.getByRole('button', { name: 'Hide task repository', exact: true }));
  await click(page.getByRole('button', { name: 'Hide calendar panel', exact: true }));
  await pause(2800);
  await click(page.getByRole('button', { name: 'Show calendar panel', exact: true }));
  await click(page.locator('.calendar-view-switch').getByRole('button', { name: 'Week', exact: true }));
  await pause(2800);
  await capture('community-calendar.png');
  await click(page.locator('.calendar-view-switch').getByRole('button', { name: 'Day', exact: true }));
  await click(page.getByRole('button', { name: 'Open chat', exact: true }));
  await page.getByLabel('AI workspace prompt').fill('Schedule release notes after the introduction.');
  await click(page.getByRole('button', { name: /send message/i }));
  await expect(page.getByRole('button', { name: 'Apply changes', exact: true })).toBeVisible();
  await pause(3200);
  await capture('community-review.png');
  await click(page.getByRole('button', { name: 'Apply changes', exact: true }));
  await expect.poll(() => state.approved).toBe(true);
  await pause(2200);
  await click(page.getByRole('dialog', { name: 'Drasil chat', exact: true }).getByRole('button', { name: 'Minimize chat', exact: true }));
  await pause(1600);
  await click(page.getByRole('button', { name: 'Habits', exact: true }));
  await expect(page.getByRole('heading', { name: 'Habits', exact: true })).toBeVisible();
  await pause(2400);
  await click(page.getByRole('button', { name: 'Check in Read 10 pages', exact: true }));
  await expect(page.getByRole('button', { name: 'Undo Read 10 pages today', exact: true })).toBeVisible();
  await pause(2400);
  await capture('community-habits.png');
  await click(page.getByRole('button', { name: 'Edit Read 10 pages', exact: true }));
  await expect(page.getByLabel('Habit name', { exact: true })).toBeVisible();
  await pause(2300);
  await click(page.getByRole('button', { name: 'Cancel', exact: true }));
  await click(page.getByRole('button', { name: 'My Day', exact: true }));
  await expect(page.locator('.my-day-agenda')).toBeVisible();
  await pause(2500);
  if (unexpected.length) throw new Error(`Unmocked demo endpoints: ${JSON.stringify(unexpected)}`);
  await page.close();
  await video.saveAs(path.join(output, 'desktop.webm'));

  // Separate fresh context for genuine mobile captures, never the user's browser.
  const mobile = await browser.newContext({ viewport: { width: 390, height: 844 },
    isMobile: true, hasTouch: true, deviceScaleFactor: 2, timezoneId: 'UTC', serviceWorkers: 'block' });
  await mobile.route('**/*', routeDemo);
  await mobile.addInitScript(() => { localStorage.setItem('drasil.appearance-theme.v1', 'light'); localStorage.setItem('drasil.backend-url.v1', ''); });
  const phone = await mobile.newPage();
  await phone.clock.setFixedTime(new Date(`${demoDay}T09:15:00Z`));
  await phone.goto(origin);
  await expect(phone.getByRole('button', { name: 'Open navigation', exact: true })).toBeVisible();
  await phone.getByRole('button', { name: 'Open navigation', exact: true }).tap();
  await phone.getByRole('button', { name: 'Habits', exact: true }).tap();
  await expect(phone.getByRole('heading', { name: 'Habits', exact: true })).toBeVisible();
  await expect(phone.getByRole('article', { name: 'Read 10 pages', exact: true })).toBeVisible();
  await phone.screenshot({ path: path.join(assets, 'community-mobile.png'), animations: 'disabled' });
  await mobile.close();
  if (unexpected.length) throw new Error(`Unmocked demo endpoints: ${JSON.stringify(unexpected)}`);

  // Skip the initial blank browser frames; the first frame shows the loaded app.
  const conversion = spawnSync('ffmpeg', ['-y', '-ss', '2', '-i', path.join(output, 'desktop.webm'), '-vf',
    'fps=8,scale=1120:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=192:stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3',
    '-loop', '0', '-map_metadata', '-1', path.join(assets, 'community-demo.gif')], { encoding: 'utf8' });
  if (conversion.status !== 0) throw new Error(conversion.stderr);
  const gif = await readFile(path.join(assets, 'community-demo.gif'));
  if (gif.length > 10 * 1024 * 1024) throw new Error('GIF exceeds public asset budget.');
  await writeFile(path.join(output, 'audit.json'), JSON.stringify({ fictional: true,
    api: 'mock-only', externalRequestsAllowed: false, unexpected, requests,
    approvedBlocks: state.blocks.length, habitXp: state.habits.totalXp, gifBytes: gif.length }, null, 2));
  console.log(`Recorded fictional demo: ${gif.length} bytes; no backend or external account access.`);
} finally {
  await context.close();
  await browser.close();
  await server.close();
}
