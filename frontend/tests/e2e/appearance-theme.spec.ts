import { expect, test } from '../support/fixtures/strategist.fixture';
import { mockCalendarBlocks } from '../support/helpers/network';

test.describe('Appearance theme', () => {
  test('defaults to dark and persists light or dark from Settings', async ({
    page,
    mockWorkspace,
  }) => {
    const workspace = await mockWorkspace();
    const boot = workspace.waitForBoot();

    await page.goto('/');
    await boot;

    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect(page.locator('.app-root')).toHaveCSS(
      'background-color',
      'rgba(0, 0, 0, 0)',
    );
    await expect(page.locator('.sidebar')).toHaveCSS(
      'border-right-color',
      'rgb(39, 54, 81)',
    );

    await page.screenshot({
      path: '../.tmp/theme-workspace-dark.png',
      fullPage: true,
    });

    await page.getByRole('button', { name: 'Settings' }).click();
    await page.getByRole('button', { name: 'Appearance', exact: true }).click();
    await expect(page.getByTestId('theme-dark-choice')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('theme-light-choice')).toHaveAttribute('aria-pressed', 'false');

    await page.screenshot({
      path: '../.tmp/theme-settings-dark.png',
      fullPage: true,
    });

    await page.getByTestId('theme-light-choice').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
    await expect(page.getByTestId('theme-light-choice')).toHaveAttribute('aria-pressed', 'true');
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem('drasil.appearance-theme.v1')))
      .toBe('light');

    await page.screenshot({
      path: '../.tmp/theme-settings-light.png',
      fullPage: true,
    });

    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');

    await page.getByRole('button', { name: 'Settings' }).click();
    await page.getByRole('button', { name: 'Appearance', exact: true }).click();
    await page.getByTestId('theme-dark-choice').click();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem('drasil.appearance-theme.v1')))
      .toBe('dark');

    await page.reload();
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  });

  test('keeps user chat messages legible in dark mode', async ({
    page,
    mockWorkspace,
    mockStrategistApi,
    openStrategist,
  }) => {
    await mockStrategistApi({
      runResponses: [
        {
          session: {
            id: 'dark-user-message-session',
            title: 'Dark user message',
            providerLabel: 'Test provider',
            modelName: 'test-model',
            createdAt: '2026-07-30T22:00:00.000Z',
            updatedAt: '2026-07-30T22:00:00.000Z',
          },
          turn: {
            id: 'dark-user-message-turn',
            prompt: 'Create cooking and cleaning tasks today.',
            createdAt: '2026-07-30T22:00:00.000Z',
            response: {
              providerLabel: 'Test provider',
              mode: 'advisory',
              summary: 'I can help with that.',
              contextCards: [],
              suggestedPrompts: [],
            },
          },
        },
      ],
    });
    const workspace = await mockWorkspace();
    const boot = workspace.waitForBoot();
    await page.goto('/');
    await boot;
    await openStrategist();
    await page
      .getByLabel('AI workspace prompt')
      .fill('Create cooking and cleaning tasks today.');
    await page.getByRole('button', { name: /send message/i }).click();

    const userText = page
      .locator('.strategist-turn-user p')
      .filter({ hasText: 'Create cooking and cleaning tasks today.' });
    await expect(userText).toBeVisible();
    await expect(userText).toHaveCSS('color', 'rgb(237, 243, 255)');
  });

  test('keeps populated remembered preferences readable in dark mode', async ({
    page,
    mockWorkspace,
  }) => {
    await page.route('**/api/v1/preferences/remembered?includeInactive=true', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          preferences: [
            {
              id: 'preference-inter-task-gap',
              domain: 'scheduling',
              preferenceType: 'inter_task_gap',
              scopeType: 'global',
              scopeId: null,
              strength: 'strong',
              source: 'approved_memory_update',
              confidence: 0.94,
              active: true,
              description: 'Leave 15 minutes between tasks.',
              value: {
                preferenceText: 'Inter Task Gap',
              },
              constraints: [],
              createdAt: '2026-07-30T17:04:00Z',
              updatedAt: '2026-07-30T17:04:00Z',
            },
            {
              id: 'preference-focus-window',
              domain: 'focus',
              preferenceType: 'focus_window',
              scopeType: 'global',
              scopeId: null,
              strength: 'medium',
              source: 'approved_memory_update',
              confidence: 0.86,
              active: false,
              description: 'Keep mornings clear for focused work.',
              value: {
                startTime: '9:00 AM',
                endTime: '11:00 AM',
              },
              constraints: [],
              createdAt: '2026-07-29T16:00:00Z',
              updatedAt: '2026-07-29T16:00:00Z',
            },
          ],
          summary: {
            activeCount: 1,
            inactiveCount: 1,
            hardConstraintCount: 0,
          },
        }),
      });
    });

    const workspace = await mockWorkspace();
    const boot = workspace.waitForBoot();

    await page.goto('/');
    await boot;
    await page.getByRole('button', { name: 'Settings' }).click();
    await page.getByRole('button', { name: 'Memory', exact: true }).click();

    const activePreference = page
      .locator('.settings-memory-item')
      .filter({ hasText: 'Leave 15 minutes between tasks.' });
    await expect(activePreference).toBeVisible();
    await expect(activePreference.locator('.settings-memory-heading-row strong')).toHaveCSS(
      'color',
      'rgb(237, 243, 255)',
    );
    await expect(activePreference.locator('.settings-memory-main p')).toHaveCSS(
      'color',
      'rgb(184, 197, 218)',
    );
    await expect(activePreference.locator('.settings-memory-status')).toHaveCSS(
      'color',
      'rgb(237, 243, 255)',
    );
    await expect(activePreference.locator('.settings-memory-meta span').first()).toHaveCSS(
      'color',
      'rgb(184, 197, 218)',
    );
    await expect(activePreference.getByRole('button', { name: 'Disable' })).toHaveCSS(
      'color',
      'rgb(237, 243, 255)',
    );
    await expect(activePreference.getByRole('button', { name: 'Delete' })).toHaveCSS(
      'color',
      'rgb(255, 154, 168)',
    );

    await page.screenshot({
      path: '../.tmp/theme-remembered-preferences-dark.png',
      fullPage: true,
    });
  });

  test('uses distinct category colors for calendar blocks in dark mode', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    await page.clock.setFixedTime(new Date('2026-07-30T08:00:00-07:00'));

    const work = categoryFactory.build({
      id: 811,
      name: 'Work',
      accentColor: '#6366f1',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const health = categoryFactory.build({
      id: 812,
      name: 'Health',
      accentColor: '#10b981',
      iconSlug: 'heart-pulse',
      sortOrder: 1,
    });
    const workTask = nodeFactory.build({
      id: 8811,
      categoryId: work.id,
      title: 'Prepare project brief',
      sortOrder: 0,
    });
    const healthTask = nodeFactory.build({
      id: 8812,
      categoryId: health.id,
      title: 'Workout',
      sortOrder: 0,
    });
    const workSubtask = nodeFactory.build({
      id: 8813,
      categoryId: work.id,
      parentId: workTask.id,
      title: 'Draft key findings',
      sortOrder: 0,
    });

    await mockCalendarBlocks(page, {
      blocks: [
        {
          id: 9811,
          taskId: workTask.id,
          categoryId: work.id,
          title: workTask.title,
          emoji: '📘',
          categoryName: work.name,
          categoryColor: work.accentColor,
          startAt: '2026-07-30T13:00:00-07:00',
          endAt: '2026-07-30T14:30:00-07:00',
          variant: 'scheduled',
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
        },
        {
          id: 9812,
          taskId: healthTask.id,
          categoryId: health.id,
          title: healthTask.title,
          emoji: '💪',
          categoryName: health.name,
          categoryColor: health.accentColor,
          startAt: '2026-07-30T16:15:00-07:00',
          endAt: '2026-07-30T18:15:00-07:00',
          variant: 'scheduled',
          completionStatus: 'not_started',
          isPinned: false,
          isProtected: false,
          isRecurring: false,
        },
      ],
    });
    const workspace = await mockWorkspace({
      categories: [work, health],
      nodesByCategoryId: {
        [work.id]: [workTask, workSubtask],
        [health.id]: [healthTask],
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

    const workBlock = page.locator('[data-calendar-block-id="9811"]');
    const healthBlock = page.locator('[data-calendar-block-id="9812"]');
    await expect(workBlock).toBeVisible();
    await expect(healthBlock).toBeVisible();

    const workTreeCard = page.locator('.node-card').filter({ hasText: workTask.title }).first();
    const workTreeTitleWrap = workTreeCard.locator('.node-title-wrap');
    const workTreeTitle = workTreeTitleWrap.locator('.node-title');
    await workTreeTitleWrap.hover();

    const treeHoverColors = await workTreeTitleWrap.evaluate((element) => {
      const style = element.ownerDocument.defaultView?.getComputedStyle(element);
      if (!style) {
        throw new Error('Tree task title is not attached to a browser window.');
      }
      return {
        backgroundColor: style.backgroundColor,
        textColor: element
          .querySelector('.node-title')
          ?.ownerDocument.defaultView?.getComputedStyle(
            element.querySelector('.node-title') as Element,
          ).color,
      };
    });

    expect(treeHoverColors.backgroundColor).not.toBe('rgb(255, 255, 255)');
    expect(treeHoverColors.textColor).toBe('rgb(237, 243, 255)');
    await expect(workTreeTitle).toHaveCSS('color', 'rgb(237, 243, 255)');

    await page.screenshot({
      path: '../.tmp/theme-tree-hover-dark.png',
      fullPage: true,
    });

    const workParentVirtualRow = page
      .locator('.planning-tree-virtual-row')
      .filter({ hasText: workTask.title })
      .first();
    const workChildVirtualRow = page
      .locator('.planning-tree-virtual-row')
      .filter({ hasText: workSubtask.title })
      .first();
    await expect(workChildVirtualRow).toBeVisible();
    await workChildVirtualRow.hover();
    await expect(workParentVirtualRow).toHaveClass(/hover-ancestor/);

    const parentRelationshipHover = await workParentVirtualRow
      .locator('.node-card-row')
      .evaluate((element) => {
        const style = element.ownerDocument.defaultView?.getComputedStyle(element);
        if (!style) {
          throw new Error('Parent tree task is not attached to a browser window.');
        }
        return {
          backgroundColor: style.backgroundColor,
          textColor: element
            .querySelector('.node-title')
            ?.ownerDocument.defaultView?.getComputedStyle(
              element.querySelector('.node-title') as Element,
            ).color,
        };
      });

    expect(parentRelationshipHover.backgroundColor).not.toBe('rgb(255, 255, 255)');
    expect(parentRelationshipHover.textColor).toBe('rgb(237, 243, 255)');

    await page.screenshot({
      path: '../.tmp/theme-tree-child-hover-dark.png',
      fullPage: true,
    });

    const renderedColors = await Promise.all(
      [workBlock, healthBlock].map((block) =>
        block.evaluate((element) => {
          const style = element.ownerDocument.defaultView?.getComputedStyle(element);
          if (!style) {
            throw new Error('Calendar block is not attached to a browser window.');
          }
          const canvas = element.ownerDocument.createElement('canvas');
          canvas.width = 1;
          canvas.height = 1;
          const context = canvas.getContext('2d');
          if (!context) {
            throw new Error('Browser canvas is unavailable for contrast verification.');
          }
          const resolveRgb = (color: string) => {
            context.clearRect(0, 0, 1, 1);
            context.fillStyle = color;
            context.fillRect(0, 0, 1, 1);
            return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)];
          };
          const relativeLuminance = (rgb: number[]) =>
            rgb
              .map((channel) => channel / 255)
              .map((channel) =>
                channel <= 0.04045
                  ? channel / 12.92
                  : ((channel + 0.055) / 1.055) ** 2.4,
              )
              .reduce(
                (total, channel, index) =>
                  total + channel * [0.2126, 0.7152, 0.0722][index],
                0,
              );
          const textLuminance = relativeLuminance(resolveRgb(style.color));
          const backgroundLuminance = relativeLuminance(
            resolveRgb(style.backgroundColor),
          );
          const contrastRatio =
            (Math.max(textLuminance, backgroundLuminance) + 0.05) /
            (Math.min(textLuminance, backgroundLuminance) + 0.05);
          return {
            categoryColor: style.getPropertyValue('--block-color').trim(),
            backgroundColor: style.backgroundColor,
            borderLeftColor: style.borderLeftColor,
            borderLeftWidth: style.borderLeftWidth,
            textColor: style.color,
            boxShadow: style.boxShadow,
            contrastRatio,
          };
        }),
      ),
    );

    expect(renderedColors[0].categoryColor).toBe('#c5a16a');
    expect(renderedColors[1].categoryColor).toBe('#a9bca0');
    expect(renderedColors[0].backgroundColor).not.toBe(renderedColors[1].backgroundColor);
    expect(renderedColors[0].borderLeftWidth).toBe('1px');
    expect(renderedColors[1].borderLeftWidth).toBe('1px');
    expect(renderedColors[0].textColor).toBe('rgb(37, 36, 31)');
    expect(renderedColors[1].textColor).toBe('rgb(37, 36, 31)');
    expect(renderedColors[0].boxShadow).toBe('none');
    expect(renderedColors[1].boxShadow).toBe('none');
    expect(renderedColors[0].contrastRatio).toBeGreaterThanOrEqual(4.5);
    expect(renderedColors[1].contrastRatio).toBeGreaterThanOrEqual(4.5);

    await healthBlock.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: '../.tmp/theme-calendar-categories-dark.png',
      fullPage: true,
    });

    await healthBlock.click();
    const details = page.getByRole('dialog', { name: 'Calendar block details' });
    await expect(details).toBeVisible();
    await expect(details.locator('.calendar-popover-body h3')).toHaveCSS(
      'color',
      'rgb(237, 243, 255)',
    );
    await expect(details.locator('.calendar-popover-body p').first()).toHaveCSS(
      'color',
      'rgb(184, 197, 218)',
    );
    await expect(details.locator('.calendar-thread-title')).toHaveCSS(
      'color',
      'rgb(237, 243, 255)',
    );
    await expect(details.locator('.calendar-popover-facts dd').first()).toHaveCSS(
      'color',
      'rgb(237, 243, 255)',
    );

    await page.screenshot({
      path: '../.tmp/theme-calendar-details-dark.png',
      fullPage: true,
    });
  });
});
