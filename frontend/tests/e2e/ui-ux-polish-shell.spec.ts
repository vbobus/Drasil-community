import { expect, test } from '../support/fixtures/strategist.fixture';

test.describe('UI/UX polish shell', () => {
  test('uses the compact tree, tabbed AI workspace, and resizable panel structure', async ({
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
    await expect(page.getByRole('button', { name: 'Add task' })).toBeVisible();
    await expect(page.getByRole('button', { name: /Sort tasks/i })).toBeVisible();
    const planningPanel = page.locator('.panel-planning');
    const planningResizer = page.getByRole('separator', {
      name: 'Resize planning and AI workspaces',
    });
    const widthBefore = await planningPanel.evaluate((element) => element.getBoundingClientRect().width);
    await planningResizer.focus();
    for (let index = 0; index < 5; index += 1) {
      await planningResizer.press('ArrowRight');
    }
    const widthAfter = await planningPanel.evaluate((element) => element.getBoundingClientRect().width);
    expect(widthAfter).toBeGreaterThan(widthBefore);
    await expect(page.locator('.planning-tree-column-headings')).toContainText('Task');
    await expect(page.locator('.planning-tree-column-headings')).toContainText('Due');
    await expect(page.locator('.planning-tree-column-headings')).toContainText('Priority');
    await expect(page.locator('.planning-tree-column-headings')).toContainText('Est.');
    await expect(page.getByRole('tab', { name: /^Chat$/i })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    await expect(page.getByRole('tab', { name: /Selected tasks/i })).toBeVisible();

    const childWrapper = page.locator(`[data-node-id="${draft.id}"]`);
    const priorityDot = childWrapper.locator('.node-priority-dot');
    const priorityAlignment = await priorityDot.evaluate((element) => {
      const heading = element.ownerDocument.querySelector(
        '.planning-tree-column-headings span:nth-child(3)',
      );
      if (!heading) return null;
      const headingBounds = heading.getBoundingClientRect();
      const dotBounds = element.getBoundingClientRect();
      return {
        headingCenter: headingBounds.left + headingBounds.width / 2,
        dotCenter: dotBounds.left + dotBounds.width / 2,
        dotColor: element.ownerDocument.defaultView?.getComputedStyle(element).backgroundColor,
      };
    });
    expect(priorityAlignment).not.toBeNull();
    expect(Math.abs(priorityAlignment!.headingCenter - priorityAlignment!.dotCenter)).toBeLessThan(
      0.5,
    );
    expect(priorityAlignment!.dotColor).toBe('rgb(220, 38, 38)');
    const metadataAlignment = await childWrapper.evaluate((element) => {
      const headings = element.ownerDocument.querySelectorAll(
        '.planning-tree-column-headings span',
      );
      const dueHeading = headings.item(1);
      const estimateHeading = headings.item(3);
      const dueControl = element.querySelector('.node-meta-deadline');
      const estimateControl = element.querySelector('.node-meta-time');
      if (!dueHeading || !estimateHeading || !dueControl || !estimateControl) return null;

      return {
        dueHeadingRight: dueHeading.getBoundingClientRect().right,
        dueControlRight: dueControl.getBoundingClientRect().right,
        dueTextAlign:
          element.ownerDocument.defaultView?.getComputedStyle(dueHeading).textAlign,
        estimateHeadingRight: estimateHeading.getBoundingClientRect().right,
        estimateControlRight: estimateControl.getBoundingClientRect().right,
        estimateTextAlign:
          element.ownerDocument.defaultView?.getComputedStyle(estimateHeading).textAlign,
      };
    });
    expect(metadataAlignment).not.toBeNull();
    expect(metadataAlignment!.dueTextAlign).toBe('right');
    expect(metadataAlignment!.estimateTextAlign).toBe('right');
    expect(
      Math.abs(metadataAlignment!.dueHeadingRight - metadataAlignment!.dueControlRight),
    ).toBeLessThan(0.5);
    expect(
      Math.abs(metadataAlignment!.estimateHeadingRight - metadataAlignment!.estimateControlRight),
    ).toBeLessThan(0.5);
    const branchConnector = childWrapper.locator('.node-tree-connector.branch-end');
    const connectorBorder = await branchConnector.evaluate((element) =>
      element.ownerDocument.defaultView?.getComputedStyle(element, '::before').borderBottomStyle,
    );
    expect(connectorBorder).toBe('solid');
    const connectorGeometry = await branchConnector.evaluate((element) => {
      const style = element.ownerDocument.defaultView?.getComputedStyle(element, '::before');
      return {
        radius: style?.borderBottomLeftRadius,
        top: style?.top,
        width: style?.width,
      };
    });
    expect(connectorGeometry).toEqual({ radius: '3px', top: '-18px', width: '17px' });

    const connectorAlignment = await branchConnector.evaluate(
      (element, parentId) => {
        const parent = element.ownerDocument.querySelector(`[data-node-id="${parentId}"]`);
        const parentArrow = parent?.querySelector('.node-chevron-icon');
        if (!parentArrow) return null;

        const arrowBounds = parentArrow.getBoundingClientRect();
        const connectorBounds = element.getBoundingClientRect();
        const connectorStyle = element.ownerDocument.defaultView?.getComputedStyle(
          element,
          '::before',
        );
        return {
          arrowAxis: arrowBounds.left + arrowBounds.width / 2,
          connectorLane: connectorBounds.left + Number.parseFloat(connectorStyle?.left ?? '0'),
        };
      },
      String(project.id),
    );
    expect(connectorAlignment).not.toBeNull();
    expect(Math.abs(connectorAlignment!.arrowAxis - connectorAlignment!.connectorLane)).toBeLessThan(
      0.5,
    );

    const nestedWrapper = page.locator(`[data-node-id="${review.id}"]`);
    const ancestorContinuation = nestedWrapper.locator('.node-tree-connector.continues').first();
    await expect(ancestorContinuation).toBeAttached();
    const continuationGeometry = await ancestorContinuation.evaluate((element) => {
      const style = element.ownerDocument.defaultView?.getComputedStyle(element, '::before');
      return { top: style?.top, bottom: style?.bottom };
    });
    expect(continuationGeometry).toEqual({ top: '-18px', bottom: '0px' });

    const continuationAlignment = await ancestorContinuation.evaluate(
      (element, rootId) => {
        const rootArrow = element.ownerDocument.querySelector(
          `[data-node-id="${rootId}"] .node-chevron-icon`,
        );
        if (!rootArrow) return null;
        const rootBounds = rootArrow.getBoundingClientRect();
        const connectorBounds = element.getBoundingClientRect();
        const style = element.ownerDocument.defaultView?.getComputedStyle(element, '::before');
        return {
          rootCenter: rootBounds.left + rootBounds.width / 2,
          lane: connectorBounds.left + Number.parseFloat(style?.left ?? '0'),
        };
      },
      String(project.id),
    );
    expect(continuationAlignment).not.toBeNull();
    expect(Math.abs(continuationAlignment!.rootCenter - continuationAlignment!.lane)).toBeLessThan(
      0.5,
    );
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
    await expect(page.getByRole('status', { name: /AI workspace is thinking/i })).toContainText(
      /Understanding your request|Finalizing the response/,
    );
    await expect(page.locator('.ai-task-reference[data-task-id="7102"]')).toContainText(
      'Draft case study',
    );
    await expect(page.locator('.ai-math .katex')).toBeVisible();
    await expect(page.getByText('Internal diagnostic trace')).toHaveCount(0);

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
