import { expect, test } from '../support/fixtures/strategist.fixture';

test.describe('UI/UX polish shell', () => {
  test('uses the compact tree, floating chat, and keyboard-resizable task repository', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
    mockStrategistApi,
  }) => {
    const work = categoryFactory.build({
      id: 701,
      name: 'Work',
      accentColor: '#516ff2',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const inbox = nodeFactory.build({
      id: 7100,
      categoryId: work.id,
      title: 'Unsorted requests',
      emoji: 'ðŸ“¥',
      timeEstimateMinutes: 20,
      sortOrder: -1,
    });
    const project = nodeFactory.build({
      id: 7101,
      categoryId: work.id,
      title: 'Launch portfolio refresh',
      emoji: '🚀',
      timeEstimateMinutes: 90,
      sortOrder: 0,
    });
    const draft = nodeFactory.build({
      id: 7102,
      categoryId: work.id,
      parentId: project.id,
      title: 'Draft case study',
      priority: 'high',
      emoji: '📝',
      timeEstimateMinutes: 45,
      sortOrder: 0,
    });
    const review = nodeFactory.build({
      id: 7103,
      categoryId: work.id,
      parentId: draft.id,
      title: 'Review launch copy',
      emoji: '🧠',
      timeEstimateMinutes: 30,
      sortOrder: 0,
    });
    const publish = nodeFactory.build({
      id: 7104,
      categoryId: work.id,
      parentId: project.id,
      title: 'Publish portfolio refresh',
      emoji: 'ðŸš€',
      timeEstimateMinutes: 30,
      sortOrder: 1,
    });

    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [inbox, project, draft, review, publish] },
    });
    await page.route(/\/api\/v1\/nodes\/\d+$/, async (route) => {
      if (route.request().method() !== 'PATCH') {
        await route.continue();
        return;
      }
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ...draft, ...payload }),
      });
    });
    await mockStrategistApi({
      runResponseDelayMs: 250,
      runResponses: [
        {
          session: {
            id: 'ui-polish-session',
            title: 'UI polish test',
            providerLabel: 'Strategist',
            createdAt: '2026-07-14T12:00:00',
            updatedAt: '2026-07-14T12:00:00',
          },
          turn: {
            id: 'ui-polish-turn',
            prompt: 'Summarize the plan with math',
            createdAt: '2026-07-14T12:00:00',
            response: {
              providerLabel: 'Strategist',
              mode: 'review',
              summary: 'Prioritize Draft case study. Capacity is $x^2 + y^2$.',
              contextCards: [],
              suggestedPrompts: [],
              taskReferences: [
                {
                  taskId: draft.id,
                  title: draft.title,
                  emoji: draft.emoji,
                  nodeType: draft.nodeType,
                  categoryId: work.id,
                  categoryName: work.name,
                  categoryAccentColor: work.accentColor,
                },
              ],
              proposal: null,
              clarification: null,
              trace: {
                summary: 'Internal diagnostic trace',
                steps: [
                  {
                    kind: 'decision',
                    title: 'Rank tasks',
                    detail: 'Kept for logs, not ordinary chat.',
                  },
                ],
              },
            },
          },
        },
      ],
    });

    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await expect(page.getByRole('heading', { name: 'Work tree' })).toBeVisible();
    const activeWorkspaceRow = page
      .getByRole('button', { name: 'Work workspace (active)' })
      .locator('..');
    await expect
      .poll(() =>
        activeWorkspaceRow.evaluate(
          (element) => element.ownerDocument.defaultView?.getComputedStyle(element).boxShadow,
        ),
      )
      .toBe('none');
    await expect(page.getByLabel('Search tasks in this workspace')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Filter tasks' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Add task', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /Sort tasks/i })).toBeVisible();
    const planningPanel = page.getByRole('article', { name: 'Task repository' });
    const planningResizer = page.getByRole('separator', {
      name: 'Resize task repository',
    });
    const widthBefore = await planningPanel.evaluate((element) => element.getBoundingClientRect().width);
    await planningResizer.focus();
    for (let index = 0; index < 5; index += 1) {
      await planningResizer.press('ArrowRight');
    }
    const widthAfter = await planningPanel.evaluate((element) => element.getBoundingClientRect().width);
    expect(widthAfter).toBeGreaterThan(widthBefore);
    const childWrapper = page.locator(`[data-node-id="${draft.id}"]`);
    await expect(childWrapper.locator('.node-title')).toContainText(draft.title);
    await expect(childWrapper.getByRole('button', { name: 'Time estimate: 45m' })).toBeVisible();
    await expect(childWrapper.locator('.node-priority-dot')).toBeVisible();
    await page.getByRole('button', { name: 'Open chat', exact: true }).click();
    await expect(page.getByRole('tab', { name: /^Chat$/i })).toHaveAttribute('aria-selected', 'true');
    await expect(page.getByRole('tab', { name: /Selected tasks/i })).toBeVisible();
    await page.getByRole('dialog', { name: 'Drasil chat' }).getByRole('button', { name: 'Minimize chat', exact: true }).click();

    const branchConnector = childWrapper.locator('.node-tree-connector.branch-end');
    const connectorBorder = await branchConnector.evaluate((element) =>
      element.ownerDocument.defaultView?.getComputedStyle(element, '::after').borderTopStyle,
    );
    expect(connectorBorder).toBe('solid');
    const connectorGeometry = await branchConnector.evaluate(element => {
      const style = getComputedStyle(element, '::before');
      const connectorBounds = element.getBoundingClientRect();
      const card = element.closest('.node-card-wrapper')?.querySelector('.node-card-row');
      return { top: parseFloat(style.top), lineX: connectorBounds.left + parseFloat(style.left), cardLeft: card?.getBoundingClientRect().left };
    });
    expect(connectorGeometry.top).toBeLessThan(0);
    expect(connectorGeometry.cardLeft).toBeDefined();
    expect(connectorGeometry.lineX).toBeLessThanOrEqual(connectorGeometry.cardLeft!);
    const nestedWrapper = page.locator(`[data-node-id="${review.id}"]`);
    const ancestorContinuation = nestedWrapper.locator('.node-tree-connector.continues').first();
    await expect(ancestorContinuation).toBeAttached();
    const continuationGeometry = await ancestorContinuation.evaluate(element => {
      const style = getComputedStyle(element, '::before');
      return { top: parseFloat(style.top), bottom: parseFloat(style.bottom), border: style.borderLeftStyle };
    });
    expect(continuationGeometry.top).toBeLessThan(0);
    expect(continuationGeometry.bottom).toBeLessThanOrEqual(0);
    expect(continuationGeometry.border).toBe('solid');
    await expect(page.locator(`[data-node-id="${inbox.id}"] .node-tree-connectors`)).toHaveCount(0);

    await childWrapper.getByRole('button', { name: 'Time estimate: 45m' }).click();
    const estimatePopover = page.locator('body > .node-meta-popover.estimate-popover');
    await expect(estimatePopover).toBeVisible();
    const estimateBounds = await estimatePopover.boundingBox();
    const viewport = page.viewportSize();
    expect(estimateBounds).not.toBeNull();
    expect(viewport).not.toBeNull();
    expect(estimateBounds!.x).toBeGreaterThanOrEqual(0);
    expect(estimateBounds!.y).toBeGreaterThanOrEqual(0);
    expect(estimateBounds!.x + estimateBounds!.width).toBeLessThanOrEqual(viewport!.width);
    expect(estimateBounds!.y + estimateBounds!.height).toBeLessThanOrEqual(viewport!.height);
    await page.keyboard.press('Escape');
    await page.mouse.click(2, 2);

    await childWrapper.getByRole('button', { name: 'No deadline set. Click to add one.' }).click();
    const deadlinePopover = page.locator('body > .node-meta-popover').filter({
      has: page.getByText('Deadline', { exact: true }),
    });
    const deadlineInput = deadlinePopover.locator('input[type="date"]');
    await deadlineInput.fill('2026-07-20');
    await expect(deadlinePopover).toBeVisible();
    await expect(deadlineInput).toHaveValue('2026-07-20');
    await page.mouse.click(2, 2);

    const rootRow = page.getByRole('treeitem', { name: new RegExp(project.title) });
    await rootRow.hover();
    const childVirtualRow = page
      .locator('.planning-tree-virtual-row')
      .filter({ has: childWrapper });
    await expect(childVirtualRow).toHaveClass(/hover-descendant/);

    await page.getByRole('button', { name: `Select ${project.title} for execution staging` }).click();
    await page.getByRole('button', { name: 'Open chat', exact: true }).click();
    const selectedTab = page.getByRole('tab', { name: /Selected tasks/i });
    await expect(selectedTab).toContainText('1');
    await selectedTab.click();

    const selectedRegion = page.getByRole('region', { name: /^Selected tasks$/i });
    await expect(selectedRegion).toBeVisible();
    await expect(selectedRegion.getByRole('button', { name: /Auto-schedule selected/i })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Custom schedule' })).toHaveCount(0);
    await expect(page.getByLabel('AI workspace prompt')).toBeHidden();

    await page.getByRole('tab', { name: /^Chat$/i }).click();
    const composer = page.getByLabel('AI workspace prompt');
    await composer.fill('Summarize the plan with math');
    await composer.press('Enter');
    await expect(page.locator('.chat-progress-status')).toBeVisible();
    await expect(page.locator('.ai-task-reference[data-task-id="7102"]')).toContainText(
      'Draft case study',
    );
    await expect(page.locator('.ai-math .katex')).toBeVisible();
    await expect(page.getByText('Internal diagnostic trace')).toHaveCount(0);

    await page.getByRole('dialog', { name: 'Drasil chat' }).getByRole('button', { name: 'Minimize chat', exact: true }).click();
    const widthBeforeFinalResize = await planningPanel.evaluate(
      (element) => element.getBoundingClientRect().width,
    );
    await planningResizer.press('ArrowRight');
    const widthAfterFinalResize = await planningPanel.evaluate(
      (element) => element.getBoundingClientRect().width,
    );
    expect(widthAfterFinalResize).toBeGreaterThan(widthBeforeFinalResize);
  });
});
