import { expect, test } from '../support/fixtures';

test.describe('Story 2.1 - Selected task cards and estimate rollups', () => {
  test('preserves selected task references while reordering the selected set only', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const category = categoryFactory.build({
      name: 'Work',
      accentColor: '#1f6feb',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });

    const parentTask = nodeFactory.build({
      id: 2101,
      categoryId: category.id,
      title: 'Launch plan',
      priority: 'high',
      deadlineAt: '2026-08-01',
      timeEstimateMinutes: null,
      sortOrder: 0,
    });
    const firstChild = nodeFactory.build({
      id: 2102,
      categoryId: category.id,
      parentId: parentTask.id,
      title: 'Draft implementation outline',
      priority: 'high',
      deadlineAt: '2026-08-07',
      timeEstimateMinutes: 45,
      sortOrder: 0,
    });
    const secondChild = nodeFactory.build({
      id: 2103,
      categoryId: category.id,
      parentId: parentTask.id,
      title: 'Review launch risks',
      priority: 'medium',
      deadlineAt: '2026-08-09',
      timeEstimateMinutes: 30,
      sortOrder: 1,
    });
    const designTask = nodeFactory.build({
      id: 2104,
      categoryId: category.id,
      title: 'Design visuals',
      emoji: 'D',
      priority: 'low',
      deadlineAt: '2026-08-10',
      timeEstimateMinutes: 90,
      sortOrder: 1,
    });
    const backlogTask = nodeFactory.build({
      id: 2105,
      categoryId: category.id,
      title: 'Follow up with vendors',
      priority: 'medium',
      deadlineAt: '2026-08-11',
      timeEstimateMinutes: 30,
      sortOrder: 2,
      isBacklog: true,
    });

    let treeMoveRequests = 0;
    await page.route('**/api/v1/nodes/*/move', async (route) => {
      treeMoveRequests += 1;
      await route.abort();
    });

    const workspace = await mockWorkspace({
      categories: [category],
      nodesByCategoryId: {
        [category.id]: [parentTask, firstChild, secondChild, designTask, backlogTask],
      },
    });

    const workspaceBoot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await workspaceBoot;

    await page
      .getByRole('button', { name: /select draft implementation outline for execution staging/i })
      .click();
    await page
      .getByRole('button', { name: /select launch plan for execution staging/i })
      .click();
    await page
      .getByRole('button', { name: /select design visuals for execution staging/i })
      .click();
    await page.getByRole('button', { name: /^Backlog/i }).click();
    await page
      .getByRole('button', { name: /select follow up with vendors for execution staging/i })
      .click();

    await page.getByRole('tab', { name: /Selected tasks/i }).click();
    const selectedPanel = page.getByRole('region', { name: /^Selected tasks$/i });
    const selectedRootTitles = selectedPanel.locator('.selected-stage-root-title');
    const selectedTaskWrapper = (title: string) =>
      selectedPanel.locator('.selected-task-drag-wrapper').filter({ hasText: title });

    await expect(selectedRootTitles).toHaveText([
      'Launch plan',
      'Design visuals',
      'Follow up with vendors',
    ]);
    await expect(
      selectedPanel.locator('.selected-stage-node-title', {
        hasText: 'Draft implementation outline',
      }),
    ).toBeVisible();
    await expect(
      selectedPanel.locator('.selected-stage-node-title', { hasText: 'Review launch risks' }),
    ).toBeVisible();
    await expect(selectedPanel.getByText('Aug 7')).toBeVisible();
    const outlineRow = selectedPanel.locator('.selected-stage-node-row', {
      hasText: 'Draft implementation outline',
    });
    await expect(outlineRow.getByText('45m')).toBeVisible();
    await expect(selectedPanel.getByText('3h 15m')).toBeVisible();
    await expect(outlineRow.getByRole('img', { name: 'high priority' })).toBeVisible();
    await expect(
      selectedPanel
        .locator('.selected-stage-node-row', { hasText: 'Follow up with vendors' })
        .getByText('Backlog'),
    ).toBeVisible();
    await expect(
      selectedTaskWrapper('Design visuals').locator('.selected-row-icon'),
    ).toHaveText('D');

    await selectedTaskWrapper('Follow up with vendors')
      .locator('.selected-task-group')
      .dragTo(selectedTaskWrapper('Design visuals').locator('.selected-task-group'), {
        targetPosition: { x: 120, y: 4 },
      });
    await expect(selectedRootTitles).toHaveText([
      'Launch plan',
      'Follow up with vendors',
      'Design visuals',
    ]);

    await selectedTaskWrapper('Launch plan')
      .locator('.selected-task-group')
      .dragTo(selectedTaskWrapper('Follow up with vendors').locator('.selected-task-group'), {
        targetPosition: { x: 120, y: 28 },
      });
    await expect(selectedRootTitles).toHaveText([
      'Follow up with vendors',
      'Launch plan',
      'Design visuals',
    ]);

    await expect(
      page.locator(
        '.planning-tree-scroll .node-card-wrapper[data-lane="active"][data-depth="0"] > .node-card > .node-card-row .node-title',
      ),
    ).toHaveText(['Launch plan', 'Design visuals']);
    expect(treeMoveRequests).toBe(0);

    await selectedPanel
      .getByRole('button', { name: /remove design visuals from selected tasks/i })
      .click();

    await expect(selectedRootTitles).toHaveText(['Follow up with vendors', 'Launch plan']);
    await expect(selectedPanel.getByText('1h 45m')).toBeVisible();
    await expect(
      page.getByRole('button', { name: /select design visuals for execution staging/i }),
    ).toBeVisible();
  });
});
