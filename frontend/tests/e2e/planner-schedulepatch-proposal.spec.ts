import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks, mockPlannerProposal } from '../support/helpers/network';

// Retired direct-Planner UI harness. Stories 2.5/2.6 cover SchedulePatch generation,
// Panel 3 projection, and approval through the shared workspace E2E specs.
test.describe.skip('Story 2.4 - retired direct Planner SchedulePatch proposal', () => {
  test.beforeEach(async ({ page }) => {
    await page.clock.setFixedTime(new Date('2026-06-18T08:00:00-07:00'));
  });

  test('renders a validated SchedulePatch proposal without accepted writes', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 240,
      name: 'Work',
      accentColor: '#1f6feb',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });

    const parentTask = nodeFactory.build({
      id: 2401,
      categoryId: work.id,
      title: 'Launch case study',
      emoji: 'L',
      timeEstimateMinutes: null,
      sortOrder: 0,
    });
    const outlineTask = nodeFactory.build({
      id: 2402,
      categoryId: work.id,
      parentId: parentTask.id,
      title: 'Draft outline',
      timeEstimateMinutes: 45,
      sortOrder: 0,
    });
    const visualTask = nodeFactory.build({
      id: 2403,
      categoryId: work.id,
      parentId: parentTask.id,
      title: 'Review visuals',
      timeEstimateMinutes: 30,
      sortOrder: 1,
    });

    let nodeMutationRequests = 0;
    let strategistRequests = 0;
    page.on('request', (request) => {
      const url = request.url();
      const method = request.method();
      if (method !== 'GET' && url.includes('/api/v1/nodes')) {
        nodeMutationRequests += 1;
      }
      if (url.includes('/api/v1/ai/strategist')) {
        strategistRequests += 1;
      }
    });

    const calendarRoutes = await mockCalendarBlocks(page, {
      blocks: [],
    });

    const plannerRoutes = await mockPlannerProposal(page, {
      response: {
        providerLabel: 'Planner',
        mode: 'schedule_patch',
        summary: 'Prepared a reviewable schedule proposal for 1 selected task.',
        contextCards: [
          {
            id: 'working-set',
            label: 'Selected',
            value: '1 tasks',
            detail: '75 minutes planned',
          },
        ],
        suggestedPrompts: [],
        proposal: {
          proposalId: 'schedule-proposal-20260618',
          proposalKind: 'schedule_patch',
          title: 'Schedule proposal',
          body: 'Review these proposed calendar placements.',
          summary: 'Drafted 1 proposed calendar block.',
          preview: ['Launch case study, 2026-06-18T09:00:00 - 2026-06-18T10:15:00'],
          validationWarnings: [],
          schedulePatch: {
            patchId: 'schedule-patch-20260618',
            intentSummary: 'Schedule these selected tasks.',
            targetScope: 'selected_working_set',
            workingSetTaskIds: [parentTask.id],
            windowStart: '2026-06-18T00:00:00',
            windowEnd: '2026-06-19T00:00:00',
            operations: [
              {
                op: 'create_block',
                proposalBlockId: 'proposal-2401-20260618T090000',
                taskId: parentTask.id,
                categoryId: work.id,
                title: parentTask.title,
                emoji: parentTask.emoji,
                categoryName: work.name,
                categoryColor: work.accentColor,
                startAt: '2026-06-18T09:00:00',
                endAt: '2026-06-18T10:15:00',
                variant: 'proposed',
                completionStatus: 'not_started',
                taskStatus: 'todo',
                taskDeadlineAt: null,
                taskPriority: 'medium',
                timeEstimateMinutes: null,
                isPinned: false,
                isProtected: false,
                isRecurring: false,
                hierarchyDepth: 0,
                renderedChildTaskIds: [outlineTask.id, visualTask.id],
                source: 'solver',
              },
            ],
          },
          schedulePatchValidation: {
            status: 'valid',
            errors: [],
            warnings: [],
            operationCount: 1,
          },
          schedulePatchSimulation: {
            operationCount: 1,
            affectedTaskIds: [parentTask.id],
            affectedCalendarBlockIds: [],
            previewBlocks: [
              {
                id: 'proposal-2401-20260618T090000',
                taskId: parentTask.id,
                categoryId: work.id,
                title: parentTask.title,
                emoji: parentTask.emoji,
                categoryName: work.name,
                categoryColor: work.accentColor,
                startAt: '2026-06-18T09:00:00',
                endAt: '2026-06-18T10:15:00',
                variant: 'proposed',
                completionStatus: 'not_started',
                taskStatus: 'todo',
                taskDeadlineAt: null,
                taskPriority: 'medium',
                timeEstimateMinutes: null,
                isPinned: false,
                isProtected: false,
                isRecurring: false,
                hierarchyDepth: 0,
                renderedChildTaskIds: [outlineTask.id, visualTask.id],
              },
            ],
            skippedTaskIds: [],
            createBlocks: 1,
            updateBlockTimes: 0,
            removeBlocks: 0,
            skipTasks: 0,
          },
          schedulePatchStatus: 'valid',
          payload: {
            solverStatus: 'feasible',
            conflicts: [],
          },
        },
        diagnostics: {
          controlPlane: 'planner-capability',
          providerReadiness: {
            checked: false,
            passed: true,
            source: 'skipped_for_fixture',
          },
          solverStatus: 'feasible',
          runtimeMs: 24,
          fallbackDurationTaskIds: [],
          conflictCount: 0,
          acceptedBlockCount: 0,
        },
      },
    });

    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: {
        [work.id]: [parentTask, outlineTask, visualTask],
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

    await page
      .getByRole('button', { name: /select launch case study for execution staging/i })
      .click();

    const selectedPanel = page.getByRole('region', { name: /^Selected tasks$/i });
    await selectedPanel.getByRole('button', { name: /custom schedule/i }).click();

    await expect.poll(() => plannerRoutes.requests.length).toBe(1);
    expect(plannerRoutes.requests[0]).toMatchObject({
      prompt: 'Schedule these selected tasks.',
      workingSetTaskIds: [parentTask.id],
      windowStart: '2026-06-18T08:00:00',
      windowEnd: '2026-06-19T00:00:00',
    });

    await expect(selectedPanel.getByText('Schedule proposal')).toBeVisible();
    const proposalCard = selectedPanel.locator('.selected-schedulepatch-card');
    await expect(proposalCard.getByText('valid')).toBeVisible();
    await expect(proposalCard.getByText('1 ops')).toBeVisible();
    await expect(proposalCard.getByText('1h 15m')).toBeVisible();
    await expect(selectedPanel.getByRole('button', { name: /^Approve$/i })).toBeVisible();
    await expect(selectedPanel.getByRole('button', { name: /^Edit$/i })).toBeVisible();
    await expect(selectedPanel.getByRole('button', { name: /^Reject$/i })).toBeVisible();

    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await expect(
      calendar.getByRole('button', {
        name: /Launch case study.*9:00 AM.*10:15 AM.*Work.*2 subtasks.*proposed/i,
      }),
    ).toBeVisible();

    await calendar
      .getByRole('button', {
        name: /Launch case study.*9:00 AM.*10:15 AM.*Work.*2 subtasks.*proposed/i,
      })
      .click();
    const proposalDialog = page.getByRole('dialog', { name: /calendar block details/i });
    await expect(
      proposalDialog.getByRole('checkbox', { name: /mark launch case study done/i }),
    ).toHaveCount(0);
    await proposalDialog.getByRole('button', { name: /edit calendar block/i }).click();
    await proposalDialog.getByLabel('Calendar block start date and time').fill('2026-06-18T12:30');
    await proposalDialog.getByLabel('Calendar block end date and time').fill('2026-06-18T13:45');
    await proposalDialog.getByRole('button', { name: /save calendar block time/i }).click();

    await expect(
      calendar.getByRole('button', {
        name: /Launch case study.*12:30 PM.*1:45 PM.*Work.*2 subtasks.*proposed/i,
      }),
    ).toBeVisible();
    await expect(selectedPanel.getByText('12:30 PM - 1:45 PM')).toBeVisible();
    await expect(proposalCard.getByText('dirty')).toBeVisible();
    await expect(proposalCard.getByRole('button', { name: /^Approve$/i })).toHaveCount(0);

    await proposalDialog.getByRole('button', { name: /remove proposed calendar block/i }).click();
    await expect(proposalCard.getByText('No schedule proposed')).toBeVisible();
    await expect(proposalCard.getByText('SchedulePatch has no operations.')).toHaveCount(0);

    expect(calendarRoutes.createdPayloads).toEqual([]);
    expect(calendarRoutes.updatedPayloads).toEqual([]);
    expect(calendarRoutes.deletedIds).toEqual([]);
    expect(nodeMutationRequests).toBe(0);
    expect(strategistRequests).toBe(0);
  });

  test('shows validation errors without approvable controls for invalid SchedulePatch', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 241,
      name: 'Work',
      accentColor: '#1f6feb',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const task = nodeFactory.build({
      id: 2411,
      categoryId: work.id,
      title: 'Write launch email',
      emoji: 'E',
      timeEstimateMinutes: 60,
      sortOrder: 0,
    });

    await mockCalendarBlocks(page, { blocks: [] });
    await mockPlannerProposal(page, {
      response: {
        providerLabel: 'Planner',
        mode: 'schedule_patch',
        summary: 'Prepared a reviewable schedule proposal for 1 selected task.',
        contextCards: [],
        suggestedPrompts: [],
        proposal: {
          proposalId: 'schedule-proposal-invalid',
          proposalKind: 'schedule_patch',
          title: 'Schedule proposal',
          body: 'Review these proposed calendar placements.',
          summary: 'This proposal needs a cleaner time range.',
          preview: [],
          validationWarnings: [],
          schedulePatch: {
            patchId: 'schedule-patch-invalid',
            intentSummary: 'Schedule these selected tasks.',
            targetScope: 'selected_working_set',
            workingSetTaskIds: [task.id],
            windowStart: '2026-06-18T00:00:00',
            windowEnd: '2026-06-19T00:00:00',
            operations: [],
          },
          schedulePatchValidation: {
            status: 'invalid',
            errors: ['SchedulePatch has no operations.'],
            warnings: [],
            operationCount: 0,
          },
          schedulePatchSimulation: {
            operationCount: 0,
            affectedTaskIds: [],
            affectedCalendarBlockIds: [],
            previewBlocks: [],
            skippedTaskIds: [],
          },
          schedulePatchStatus: 'invalid',
          payload: {
            solverStatus: 'conflict',
            conflicts: [],
          },
        },
        diagnostics: {
          controlPlane: 'planner-capability',
          providerReadiness: { checked: false, passed: true },
          solverStatus: 'conflict',
          runtimeMs: 12,
        },
      },
    });

    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: {
        [work.id]: [task],
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

    await page
      .getByRole('button', { name: /select write launch email for execution staging/i })
      .click();

    const selectedPanel = page.getByRole('region', { name: /^Selected tasks$/i });
    await selectedPanel.getByRole('button', { name: /custom schedule/i }).click();

    const proposalCard = selectedPanel.locator('.selected-schedulepatch-card');
    await expect(proposalCard.getByText('No schedule proposed')).toBeVisible();
    await expect(proposalCard.getByText('SchedulePatch has no operations.')).toHaveCount(0);
    await expect(proposalCard.getByRole('button', { name: /^Approve$/i })).toHaveCount(0);
    await expect(proposalCard.getByRole('button', { name: /^Edit$/i })).toHaveCount(0);
    await expect(proposalCard.getByRole('button', { name: /^Reject$/i })).toHaveCount(0);
  });

  test('ignores a stale Planner response after the selected working set is cleared', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({
      id: 242,
      name: 'Work',
      accentColor: '#1f6feb',
      iconSlug: 'briefcase',
      sortOrder: 0,
    });
    const task = nodeFactory.build({
      id: 2421,
      categoryId: work.id,
      title: 'Clear stale proposal',
      timeEstimateMinutes: 30,
      sortOrder: 0,
    });

    await mockCalendarBlocks(page, { blocks: [] });
    const plannerRoutes = await mockPlannerProposal(page, {
      delayMs: 250,
      response: {
        providerLabel: 'Planner',
        mode: 'needs_configuration',
        summary: 'This stale response must not repopulate the cleared Planner state.',
        contextCards: [],
        suggestedPrompts: [],
        proposal: null,
        diagnostics: {
          controlPlane: 'planner-capability',
          providerReadiness: { checked: true, passed: false },
        },
      },
    });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [task] },
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

    await page
      .getByRole('button', { name: /select clear stale proposal for execution staging/i })
      .click();
    const selectedPanel = page.getByRole('region', { name: /^Selected tasks$/i });
    const responsePromise = page.waitForResponse('**/api/v1/ai/planner/propose');
    await selectedPanel.getByRole('button', { name: /custom schedule/i }).click();
    await expect.poll(() => plannerRoutes.requests.length).toBe(1);
    await selectedPanel
      .getByRole('button', { name: /remove clear stale proposal from selected tasks/i })
      .click();
    await responsePromise;

    await expect(selectedPanel.locator('.selected-schedulepatch-card')).toHaveCount(0);
    await expect(selectedPanel.getByText(/stale response must not repopulate/i)).toHaveCount(0);
  });
});
