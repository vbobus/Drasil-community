import { expect, test } from '../support/fixtures/strategist.fixture';

test.describe('All workspace', () => {
  test('pins All first and aggregates tasks from every workspace', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
    mockStrategistApi,
  }) => {
    const work = categoryFactory.build({
      id: 801,
      name: 'Work',
      accentColor: '#516ff2',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const health = categoryFactory.build({
      id: 802,
      name: 'Health',
      accentColor: '#16a085',
      iconSlug: 'heart',
      sortOrder: 1,
    });
    const workTask = nodeFactory.build({
      id: 8101,
      categoryId: work.id,
      title: 'Ship landing page',
      sortOrder: 0,
    });
    const healthTask = nodeFactory.build({
      id: 8201,
      categoryId: health.id,
      title: 'Book annual checkup',
      sortOrder: 0,
    });
    const strategistApi = await mockStrategistApi({
      runResponses: [{
        session: {
          id: 'session-all-workspaces',
          title: 'Cross-workspace priorities',
          providerLabel: 'Strategist',
          modelName: 'gpt-5.4',
          createdAt: '2026-08-12T17:00:00.000Z',
          updatedAt: '2026-08-12T17:00:00.000Z',
        },
        turn: {
          id: 'turn-all-workspaces',
          prompt: 'What should I prioritize across all my workspaces?',
          createdAt: '2026-08-12T17:00:00.000Z',
          response: {
            providerLabel: 'Strategist',
            mode: 'review',
            summary: 'Start with the landing page, then book the checkup.',
            contextCards: [],
            suggestedPrompts: [],
            taskReferences: [workTask, healthTask].map((task) => ({
              taskId: task.id,
              title: task.title,
              emoji: task.emoji,
              nodeType: task.nodeType,
              categoryId: task.categoryId,
              categoryName: task.categoryId === work.id ? work.name : health.name,
              categoryAccentColor:
                task.categoryId === work.id ? work.accentColor : health.accentColor,
            })),
          },
        },
      }],
    });
    const workspace = await mockWorkspace({
      categories: [work, health],
      nodesByCategoryId: {
        [work.id]: [workTask],
        [health.id]: [healthTask],
      },
    });
    const statusUpdates: Array<{ id: string; status: string }> = [];
    await page.route('**/api/v1/nodes/*', async (route) => {
      if (route.request().method() !== 'PATCH') {
        await route.continue();
        return;
      }

      const payload = route.request().postDataJSON() as { status?: string };
      const id = route.request().url().split('/').at(-1) ?? '';
      statusUpdates.push({ id, status: payload.status ?? '' });
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ...(id === healthTask.id.toString() ? healthTask : workTask),
          status: payload.status ?? 'not-started',
        }),
      });
    });

    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    const workspaceLinks = page
      .locator('.sidebar-section')
      .first()
      .locator('.sidebar-item-main');
    await expect(workspaceLinks.first()).toHaveAccessibleName('All workspace');
    const allLabelStyles = await page.locator('.sidebar-all-label').evaluate((element) => {
      const styles = element.ownerDocument.defaultView?.getComputedStyle(element);
      return {
        animationName: styles?.animationName ?? '',
        backgroundImage: styles?.backgroundImage ?? '',
        textFillColor: styles?.webkitTextFillColor ?? '',
      };
    });
    expect(allLabelStyles.animationName).toBe('sidebar-all-shimmer');
    expect(allLabelStyles.backgroundImage).toContain('linear-gradient');
    expect(allLabelStyles.textFillColor).toBe('rgba(0, 0, 0, 0)');

    const allLoads = Promise.all(
      [work.id, health.id].map((categoryId) =>
        page.waitForResponse(
          (response) =>
            response.request().method() === 'GET' &&
            response.url().includes(`/api/v1/nodes?categoryId=${categoryId}`),
        ),
      ),
    );
    await page.getByRole('button', { name: 'All workspace' }).click();
    await allLoads;

    await expect(page.getByRole('button', { name: 'All workspace (active)' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'All tree' })).toBeVisible();
    await expect(page.getByRole('treeitem', { name: new RegExp(workTask.title) })).toBeVisible();
    await expect(page.getByRole('treeitem', { name: new RegExp(healthTask.title) })).toBeVisible();
    await expect(page.getByRole('separator', { name: 'Work workspace' })).toBeVisible();
    await expect(page.getByRole('separator', { name: 'Health workspace' })).toBeVisible();
    await expect(page.locator('.node-workspace-pill')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Add task' })).toBeDisabled();
    await expect(page.getByLabel('AI workspace prompt')).toBeVisible();

    await page
      .getByLabel('AI workspace prompt')
      .fill('What should I prioritize across all my workspaces?');
    await page.getByRole('button', { name: /send message/i }).click();

    await expect(page.getByText('Start with the landing page, then book the checkup.')).toBeVisible();
    await expect.poll(() => strategistApi.capturedRunRequests.length).toBe(1);
    expect(strategistApi.capturedRunRequests[0]).toMatchObject({
      prompt: 'What should I prioritize across all my workspaces?',
      activeCategoryId: null,
      focusNodeId: null,
    });

    await page.getByLabel(`Mark "${healthTask.title}" as complete`).check();
    await expect.poll(() => statusUpdates).toContainEqual({
      id: healthTask.id.toString(),
      status: 'done',
    });
    await expect(page.getByRole('treeitem', { name: new RegExp(workTask.title) })).toBeVisible();
  });
});
