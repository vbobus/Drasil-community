import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks } from '../support/helpers/network';
import {
  mockStrategistApi,
  type MockStrategistRunResponse,
} from '../support/helpers/strategist';

function session(id: string) {
  return {
    id,
    title: 'Custom schedule',
    providerLabel: 'Planner',
    createdAt: '2026-07-12T15:00:00Z',
    updatedAt: '2026-07-12T15:00:00Z',
  };
}

function scheduleRun(
  taskIds: number[],
  categoryId: number,
  status: 'valid' | 'invalid' = 'valid',
): MockStrategistRunResponse {
  const taskId = taskIds[0];
  const invalid = status === 'invalid';
  const previewBlocks = invalid ? [] : [{
    id: `proposal-${taskId}`,
    taskId,
    categoryId,
    title: 'Draft launch plan',
    emoji: 'D',
    categoryName: 'Work',
    categoryColor: '#1f6feb',
    startAt: '2026-07-13T09:00:00',
    endAt: '2026-07-13T10:00:00',
    variant: 'proposed',
    completionStatus: 'not_started',
    taskStatus: 'todo',
    taskDeadlineAt: null,
    taskPriority: 'medium',
    timeEstimateMinutes: 60,
    isPinned: false,
    isProtected: false,
    isRecurring: false,
    hierarchyDepth: 0,
    renderedChildTaskIds: [],
  }];
  return {
    session: session(`shared-session-${status}`),
    turn: {
      id: `shared-turn-${status}`,
      prompt: 'schedule these tomorrow',
      createdAt: '2026-07-12T15:00:00Z',
      response: {
        providerLabel: 'Planner',
        mode: 'schedule_patch',
        summary: invalid ? 'This schedule needs another pass.' : 'Prepared a schedule for tomorrow.',
        contextCards: [],
        suggestedPrompts: [],
        proposal: {
          proposalId: `schedule-proposal-${status}`,
          proposalKind: 'schedule_patch',
          operation: 'schedule',
          legacyResponseKey: 'scheduleProposal',
          title: 'Review schedule proposal',
          body: 'Review the proposed calendar block.',
          targetNodeIds: taskIds,
          preview: [],
          validationWarnings: [],
          schedulePatch: {
            patchId: `schedule-patch-${status}`,
            workingSetTaskIds: taskIds,
            windowStart: '2026-07-13T00:00:00',
            windowEnd: '2026-07-14T00:00:00',
            operations: invalid ? [] : [{ op: 'create_block', proposalBlockId: `proposal-${taskId}`, taskId }],
          },
          schedulePatchValidation: {
            status,
            errors: invalid ? ['SchedulePatch has no operations.'] : [],
            warnings: [],
            operationCount: invalid ? 0 : 1,
          },
          schedulePatchSimulation: { previewBlocks, warnings: [], operationCount: invalid ? 0 : 1 },
          schedulePatchStatus: status,
          capabilityId: 'planner',
        },
      },
    },
  };
}

function cancellationRun(taskId: number, categoryId: number): MockStrategistRunResponse {
  return {
    session: session('shared-session-cancellation'),
    turn: {
      id: 'shared-turn-cancellation',
      prompt: 'cancel the team call on Wednesday',
      createdAt: '2026-07-20T20:25:08Z',
      response: {
        providerLabel: 'Planner',
        mode: 'schedule_patch',
        summary: 'Review the cancellation below.',
        contextCards: [],
        suggestedPrompts: [],
        proposal: {
          proposalId: 'schedule-proposal-cancellation',
          proposalKind: 'schedule_patch',
          operation: 'schedule',
          legacyResponseKey: 'scheduleProposal',
          title: 'Review schedule proposal',
          body: 'Review this calendar cancellation.',
          targetNodeId: taskId,
          targetNodeIds: [taskId],
          preview: [],
          validationWarnings: [],
          schedulePatch: {
            patchId: 'schedule-patch-cancellation',
            intentSummary: 'Cancel the Wednesday Team call.',
            targetScope: 'selected_working_set',
            workingSetTaskIds: [taskId],
            windowStart: '2026-07-22T18:30:00',
            windowEnd: '2026-07-22T19:30:00',
            operations: [{
              op: 'remove_block',
              acceptedBlockId: 98,
              taskId,
              categoryId,
              title: 'Team call',
              startAt: '2026-07-22T18:30:00',
              endAt: '2026-07-22T19:30:00',
              isRecurring: true,
            }],
          },
          schedulePatchValidation: {
            status: 'valid',
            errors: [],
            warnings: [],
            operationCount: 1,
          },
          schedulePatchSimulation: {
            operationCount: 1,
            affectedTaskIds: [taskId],
            affectedCalendarBlockIds: [98],
            previewBlocks: [],
            removeBlocks: 1,
            warnings: [],
          },
          schedulePatchStatus: 'valid',
          capabilityId: 'planner',
        },
      },
    },
  };
}

function taskRun(taskId: number): MockStrategistRunResponse {
  return {
    session: session('shared-session-valid'),
    turn: {
      id: 'shared-turn-task-patch',
      prompt: 'raise the priority on these',
      createdAt: '2026-07-12T15:01:00Z',
      response: {
        providerLabel: 'ChatGPT login',
        mode: 'metadata',
        summary: 'Prepared a priority update for review.',
        contextCards: [],
        suggestedPrompts: [],
        proposal: {
          proposalId: 'task-proposal-priority',
          proposalKind: 'metadata',
          operation: 'update',
          legacyResponseKey: 'metadataProposal',
          title: 'Review priority update',
          body: 'Raise the selected task priority.',
          targetNodeId: taskId,
          targetNodeIds: [taskId],
          preview: ['Set priority to high'],
          validationWarnings: [],
          taskPatch: { patch_id: 'task-patch-priority', operations: [] },
          taskPatchStatus: 'valid',
        },
      },
    },
  };
}

function scheduleActionRun(
  action: 'edit_requested' | 'rejected',
  sourceTurnId: string,
  proposalId: string,
): MockStrategistRunResponse {
  return {
    session: session('shared-session-valid'),
    turn: {
      id: `shared-turn-${action}`,
      prompt: action === 'rejected' ? 'Reject schedule proposal' : 'Edit schedule proposal',
      createdAt: '2026-07-12T15:01:00Z',
      response: {
        providerLabel: 'Planner',
        mode: 'schedule_patch',
        summary: action === 'rejected'
          ? 'The schedule proposal was rejected. No calendar changes were made.'
          : 'Describe the schedule change in the composer.',
        contextCards: [],
        suggestedPrompts: [],
        proposal: null,
        actionResult: {
          proposalId,
          sourceTurnId,
          status: action,
        },
      },
    },
  };
}

test.describe('Story 2.5 - custom schedule in the shared AI workspace', () => {
  test.beforeEach(async ({ page }) => {
    await page.clock.setFixedTime(new Date('2026-07-12T08:00:00-07:00'));
  });

  test.skip('uses one composer, sends canonical context, previews without writes, and isolates actions', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 250, name: 'Work', accentColor: '#1f6feb' });
    const parent = nodeFactory.build({ id: 2501, categoryId: work.id, title: 'Draft launch plan', timeEstimateMinutes: 60 });
    const child = nodeFactory.build({ id: 2502, categoryId: work.id, parentId: parent.id, title: 'Outline launch plan', timeEstimateMinutes: 30 });
    const health = categoryFactory.build({ id: 251, name: 'Health', accentColor: '#2da44e' });
    const workout = nodeFactory.build({ id: 2511, categoryId: health.id, title: 'Plan workout', timeEstimateMinutes: 45 });
    const calendar = await mockCalendarBlocks(page, { blocks: [] });
    let plannerRequests = 0;
    let nodeMutations = 0;
    page.on('request', (request) => {
      if (request.url().includes('/api/v1/ai/planner/propose')) plannerRequests += 1;
      if (request.method() !== 'GET' && request.url().includes('/api/v1/nodes')) nodeMutations += 1;
    });
    const strategist = await mockStrategistApi(page, {
      runResponses: [
        scheduleRun([parent.id, workout.id], work.id),
        scheduleActionRun('edit_requested', 'shared-turn-valid', 'schedule-proposal-valid'),
        scheduleActionRun('rejected', 'shared-turn-valid', 'schedule-proposal-valid'),
        taskRun(parent.id),
      ],
    });
    const workspace = await mockWorkspace({
      categories: [work, health],
      nodesByCategoryId: { [work.id]: [parent, child], [health.id]: [workout] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select outline launch plan for execution staging/i }).click();
    await page.getByRole('button', { name: /select draft launch plan for execution staging/i }).click();
    await page.getByRole('tab', { name: /Selected tasks/i }).click();
    const selectedPanel = page.getByRole('region', { name: /^Selected tasks$/i });
    await expect(selectedPanel.getByRole('heading', { name: 'Draft launch plan' })).toBeVisible();
    const healthBoot = page.waitForResponse((response) =>
      response.request().method() === 'GET'
      && response.url().includes(`/api/v1/nodes?categoryId=${health.id}`),
    );
    await page.getByRole('button', { name: /Health workspace/i }).click();
    await healthBoot;
    await expect(selectedPanel.getByRole('heading', { name: 'Draft launch plan' })).toBeVisible();
    await page.getByRole('button', { name: /select plan workout for execution staging/i }).click();
    await expect(page.getByRole('button', { name: 'Custom schedule' })).toHaveCount(0);
    await page.getByRole('tab', { name: /^Chat$/i }).click();

    const composer = page.getByRole('textbox', { name: 'AI workspace prompt' });
    await expect(composer).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'AI workspace prompt' })).toHaveCount(1);
    await expect(page.getByLabel('Attached selected tasks')).toContainText('Draft launch plan');
    await expect(page.getByLabel('Attached selected tasks')).toContainText('Plan workout');
    await expect(page.getByLabel('Attached selected tasks')).not.toContainText('Outline launch plan');
    expect(strategist.capturedRunRequests).toHaveLength(0);

    const scheduleResponse = page.waitForResponse((response) =>
      response.request().method() === 'POST'
      && response.url().endsWith('/api/v1/ai/strategist'),
    );
    await composer.fill('schedule these tomorrow');
    await composer.press('Enter');
    await scheduleResponse;
    const chatPanel = page.getByRole('tabpanel', { name: /^Chat$/i });
    const proposal = chatPanel.getByText('Review schedule proposal');
    await expect(proposal).toBeVisible();
    await page.getByRole('button', { name: 'Next calendar range' }).click();
    await expect(page.getByRole('region', { name: /shared calendar/i }).locator('.variant-proposed')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Approve schedule' })).toBeEnabled();
    expect(strategist.capturedRunRequests[0]).toMatchObject({
      stagedNodeIds: [parent.id, workout.id],
      localDateTime: '2026-07-12T08:00:00',
    });
    expect(strategist.capturedRunRequests[0].calendarViewStart).toBeTruthy();
    expect(strategist.capturedRunRequests[0].calendarViewEnd).toBeTruthy();
    expect(strategist.capturedRunRequests[0].timeZone).toBeTruthy();

    await chatPanel.getByRole('button', { name: 'Edit request' }).click();
    await expect(page.getByText(/describe the change in the composer/i)).toBeVisible();
    await chatPanel.getByRole('button', { name: 'Reject' }).click();
    await expect(page.getByRole('region', { name: /shared calendar/i }).locator('.variant-proposed')).toHaveCount(0);
    await composer.fill('raise the priority on these');
    await composer.press('Enter');
    await expect(chatPanel.getByText('Review priority update')).toBeVisible();
    expect(strategist.capturedRunRequests[3].stagedNodeIds).toEqual([parent.id, workout.id]);
    expect(calendar.createdPayloads).toEqual([]);
    expect(calendar.updatedPayloads).toEqual([]);
    expect(calendar.deletedIds).toEqual([]);
    expect(plannerRequests).toBe(0);
    expect(nodeMutations).toBe(0);
  });

  test('suppresses a delayed stale response after the working set changes', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 251, name: 'Work', accentColor: '#1f6feb' });
    const task = nodeFactory.build({ id: 2511, categoryId: work.id, title: 'Draft launch plan' });
    await mockCalendarBlocks(page, { blocks: [] });
    await mockStrategistApi(page, { runResponses: [scheduleRun([task.id], work.id)], runResponseDelayMs: 400 });
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { [work.id]: [task] } });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select draft launch plan for execution staging/i }).click();
    await page.getByLabel('AI workspace prompt').fill('schedule these tomorrow');
    const delayedResponse = page.waitForResponse((response) =>
      response.request().method() === 'POST'
      && response.url().endsWith('/api/v1/ai/strategist'),
    );
    await page.getByRole('button', { name: 'Send message' }).click();
    await page.getByRole('tab', { name: /Selected tasks/i }).click();
    await page
      .getByRole('region', { name: /^Selected tasks$/i })
      .getByRole('button', { name: /remove draft launch plan from selected tasks/i })
      .click();
    await delayedResponse;

    await expect(page.getByRole('status', { name: /AI workspace is thinking/i })).toHaveCount(0);
    await expect(page.getByRole('region', { name: /shared calendar/i }).locator('.variant-proposed')).toHaveCount(0);
  });

  test('does not render an empty invalid schedule as a proposal', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 252, name: 'Work', accentColor: '#1f6feb' });
    const task = nodeFactory.build({ id: 2521, categoryId: work.id, title: 'Draft launch plan' });
    await mockCalendarBlocks(page, { blocks: [] });
    await mockStrategistApi(page, { runResponses: [scheduleRun([task.id], work.id, 'invalid')] });
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { [work.id]: [task] } });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select draft launch plan for execution staging/i }).click();
    await page.getByLabel('AI workspace prompt').fill('schedule these tomorrow');
    const invalidResponse = page.waitForResponse((response) =>
      response.request().method() === 'POST'
      && response.url().endsWith('/api/v1/ai/strategist'),
    );
    await page.getByRole('button', { name: 'Send message' }).click();
    const invalidHttpResponse = await invalidResponse;
    const invalidPayload = await invalidHttpResponse.json();
    expect(invalidPayload.turn.response.proposal.schedulePatchStatus).toBe('invalid');
    const activeTree = page.getByRole('tabpanel', { name: /^Chat$/i });
    await expect(activeTree.getByText('SchedulePatch has no operations.')).toHaveCount(0);
    await expect(activeTree.locator('.strategist-standard-proposal-card')).toHaveCount(0);
    await expect(activeTree.getByRole('button', { name: /approve/i })).toHaveCount(0);
    await expect(activeTree.getByRole('button', { name: 'Edit' })).toHaveCount(0);
    await expect(activeTree.getByRole('button', { name: 'Reject' })).toHaveCount(0);
  });

  test.skip('renders the exact calendar occurrence in a cancellation review', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 254, name: 'Work', accentColor: '#1f6feb' });
    const teamCall = nodeFactory.build({ id: 2541, categoryId: work.id, title: 'Team call' });
    await mockCalendarBlocks(page, { blocks: [] });
    await mockStrategistApi(page, { runResponses: [cancellationRun(teamCall.id, work.id)] });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [teamCall] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    const composer = page.getByLabel('AI workspace prompt');
    await composer.fill('cancel the team call on Wednesday');
    await composer.press('Enter');

    const chat = page.getByRole('tabpanel', { name: /^Chat$/i });
    await expect(chat.getByText('Will be cancelled')).toBeVisible();
    const cancellation = chat.getByRole('list', { name: 'Calendar items to cancel' });
    await expect(cancellation).toContainText('Team call');
    await expect(cancellation).toContainText('Jul 22');
    await expect(cancellation).toContainText('6:30 PM');
    await expect(chat.getByRole('button', { name: 'Approve schedule' })).toBeEnabled();
  });

  test('does not open an empty scheduling workspace with no selection', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 253, name: 'Work', accentColor: '#1f6feb' });
    const task = nodeFactory.build({ id: 2531, categoryId: work.id, title: 'Unselected task' });
    await mockCalendarBlocks(page, { blocks: [] });
    await mockStrategistApi(page);
    const workspace = await mockWorkspace({ categories: [work], nodesByCategoryId: { [work.id]: [task] } });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await expect(page.getByRole('button', { name: 'Custom schedule' })).toHaveCount(0);
    await expect(page.getByRole('textbox', { name: 'AI workspace prompt' })).toBeVisible();
    await expect(page.getByLabel('Attached selected tasks')).toHaveCount(0);
  });
});
