import { expect, test } from '../support/fixtures';
import { mockCalendarBlocks } from '../support/helpers/network';
import { mockStrategistApi, type MockStrategistRunResponse } from '../support/helpers/strategist';

const session = {
  id: 'schedule-conflict-session',
  title: 'Resolve schedule conflict',
  providerLabel: 'Planner',
  createdAt: '2026-07-12T15:00:00Z',
  updatedAt: '2026-07-12T15:00:00Z',
};

function scheduleProposal(
  id: string,
  taskIds: number[],
  categoryId: number,
  includeConflict: boolean,
) {
  const operations = taskIds.slice(0, includeConflict ? 1 : 2).map((taskId, index) => ({
    op: 'create_block',
    proposalBlockId: `proposal-${taskId}`,
    taskId,
    categoryId,
    title: index === 0 ? 'Draft launch plan' : 'Review launch plan',
    categoryName: 'Work',
    categoryColor: '#1f6feb',
    startAt: `2026-07-12T${index === 0 ? '09' : '10'}:00:00`,
    endAt: `2026-07-12T${index === 0 ? '10' : '11'}:00:00`,
    variant: 'proposed',
    completionStatus: 'not_started',
  }));
  const previewBlocks = operations.map((operation) => ({
    id: operation.proposalBlockId,
    taskId: operation.taskId,
    categoryId,
    title: operation.title,
    categoryName: 'Work',
    categoryColor: '#1f6feb',
    startAt: operation.startAt,
    endAt: operation.endAt,
    variant: 'proposed',
    completionStatus: 'not_started',
    isPinned: false,
    isProtected: false,
    isRecurring: false,
    hierarchyDepth: 0,
    renderedChildTaskIds: [],
  }));
  const conflicts = includeConflict ? [{
    taskId: taskIds[1],
    taskTitle: 'Review launch plan',
    type: 'occupied_slot',
    detail: 'Review launch plan could not fit because that time is already occupied. Choose another time, edit the request, or reject this proposal.',
    conflictingBlockIds: [77],
    conflictingBlocks: [{
      blockId: 77,
      taskId: 999,
      startAt: '2026-07-12T10:00:00',
      endAt: '2026-07-12T11:00:00',
      state: 'protected',
      label: 'Protected launch review',
    }],
    alternatives: [{
      startAt: '2026-07-12T10:00:00',
      endAt: '2026-07-12T11:00:00',
      reason: 'First available time after the protected commitment.',
    }],
  }] : [];
  return {
    proposalId: id,
    proposalKind: 'schedule_patch',
    operation: 'schedule',
    legacyResponseKey: 'scheduleProposal',
    title: includeConflict ? 'Schedule proposal with a conflict' : 'Revised schedule proposal',
    body: 'Review these proposed calendar placements.',
    targetNodeIds: taskIds,
    preview: [],
    validationWarnings: includeConflict ? ['One selected task needs another time.'] : [],
    schedulePatch: {
      patchId: id.replace('proposal', 'patch'),
      workingSetTaskIds: taskIds,
      windowStart: '2026-07-12T00:00:00',
      windowEnd: '2026-07-13T00:00:00',
      operations,
    },
    schedulePatchValidation: {
      status: includeConflict ? 'warning' : 'valid',
      errors: [],
      warnings: includeConflict ? ['One selected task needs another time.'] : [],
      operationCount: operations.length,
    },
    schedulePatchSimulation: {
      previewBlocks,
      conflicts,
      warnings: [],
      operationCount: operations.length,
      affectedTaskIds: operations.map((operation) => operation.taskId),
      affectedCalendarBlockIds: [],
    },
    schedulePatchStatus: includeConflict ? 'warning' : 'valid',
    capabilityId: 'planner',
    payload: { conflicts, schedulePatchLifecycle: { status: 'pending' } },
  };
}

function proposalRun(
  turnId: string,
  proposal: ReturnType<typeof scheduleProposal>,
): MockStrategistRunResponse {
  return {
    session,
    turn: {
      id: turnId,
      prompt: 'schedule these today',
      createdAt: '2026-07-12T15:00:00Z',
      response: {
        providerLabel: 'Planner',
        mode: 'schedule_patch',
        summary: proposal.title,
        contextCards: [],
        suggestedPrompts: [],
        proposal,
      },
    },
  };
}

test.describe('Story 2.6 - retired compatibility proposal conflict UI', () => {
  test.skip(
    true,
    'Native conflict decisions are covered through ChangeSet simulation, ReviewDocument, and typed review actions.',
  );
  test('explains conflict, revises by exact alternative, and applies without browser calendar writes', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    await page.clock.setFixedTime(new Date('2026-07-12T08:00:00-07:00'));
    const work = categoryFactory.build({ id: 260, name: 'Work', accentColor: '#1f6feb' });
    const draft = nodeFactory.build({ id: 2601, categoryId: work.id, title: 'Draft launch plan', timeEstimateMinutes: 60 });
    const review = nodeFactory.build({ id: 2602, categoryId: work.id, title: 'Review launch plan', timeEstimateMinutes: 60 });
    const taskIds = [draft.id, review.id];
    const initial = scheduleProposal('schedule-proposal-conflict', taskIds, work.id, true);
    const revised = scheduleProposal('schedule-proposal-revised', taskIds, work.id, false);
    const revisedRun = proposalRun('turn-revised', revised);
    if (revisedRun.turn.response) {
      revisedRun.turn.response.actionResult = {
        proposalId: revised.proposalId,
        sourceProposalId: initial.proposalId,
        sourceTurnId: 'turn-conflict',
        status: 'revised',
      };
    }
    const applied: MockStrategistRunResponse = {
      session,
      turn: {
        id: 'turn-approved',
        prompt: 'Approve revised schedule proposal',
        createdAt: '2026-07-12T15:02:00Z',
        response: {
          providerLabel: 'Planner',
          mode: 'schedule_patch',
          summary: 'The approved schedule is now on your calendar.',
          contextCards: [],
          suggestedPrompts: [],
          proposal: null,
          actionResult: {
            proposalId: revised.proposalId,
            sourceTurnId: 'turn-revised',
            status: 'applied',
            createdBlockIds: [901, 902],
            operationCount: 2,
            syncWarnings: ['Schedule applied, but Google sync is incomplete for block 902.'],
          },
        },
      },
    };
    const strategist = await mockStrategistApi(page, {
      runResponses: [
        proposalRun('turn-conflict', initial),
        revisedRun,
        applied,
      ],
    });
    const calendar = await mockCalendarBlocks(page, { blocks: [] });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [draft, review] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select draft launch plan for execution staging/i }).click();
    await page.getByRole('button', { name: /select review launch plan for execution staging/i }).click();
    await page.getByRole('tab', { name: /^Chat$/i }).click();
    const composer = page.getByRole('textbox', { name: 'AI workspace prompt' });
    await composer.fill('schedule these today');
    await composer.press('Enter');

    const activeTree = page.getByRole('tabpanel', { name: /^Chat$/i });
    await expect(activeTree.locator('.strategist-schedule-conflicts')).toHaveAttribute('aria-live', 'polite');
    await expect(activeTree.getByText(/couldn't fit this task in the requested time/i)).toBeVisible();
    await expect(activeTree.getByText(/protected launch review/i)).toBeVisible();
    const sourceCard = activeTree.locator('.strategist-standard-proposal-card', {
      hasText: 'Schedule proposal with a conflict',
    });
    const alternative = sourceCard.getByRole('button', { name: /use .* for review launch plan/i });
    await alternative.focus();
    await expect(alternative).toBeFocused();
    await alternative.press('Enter');
    const revisedStatus = sourceCard.getByText(/revised schedule replaced this proposal/i);
    await expect(revisedStatus).toBeVisible();
    await expect(revisedStatus).toBeFocused();
    await expect(alternative).toBeDisabled();
    await expect(activeTree.locator('.strategist-action-title', { hasText: 'Revised schedule proposal' })).toBeVisible();
    expect(strategist.capturedRunRequests[1].uiEvent).toMatchObject({
      kind: 'proposal_action',
      sourceTurnId: 'turn-conflict',
      proposalId: initial.proposalId,
      proposalKind: 'schedule_patch',
      action: 'edit',
      payload: { conflictIndex: 0, alternativeIndex: 0 },
    });

    calendar.blocks.push(
      ...revised.schedulePatchSimulation.previewBlocks.map((block, index) => ({
        ...block,
        id: 901 + index,
        variant: 'scheduled' as const,
      })),
    );
    await page.getByRole('tab', { name: /^Selected tasks/i }).click();
    const selectedPanel = page.getByRole('region', { name: /^Selected tasks$/i });
    await expect(selectedPanel.getByRole('button', { name: 'Approve' })).toBeVisible();
    await expect(selectedPanel.getByRole('button', { name: 'Edit' })).toBeVisible();
    await expect(selectedPanel.getByRole('button', { name: 'Reject' })).toBeVisible();
    await selectedPanel.getByRole('button', { name: 'Approve' }).click();
    const appliedStatus = activeTree.getByText('Schedule applied.');
    await expect(appliedStatus).toBeVisible();
    await expect(appliedStatus).toBeFocused();
    await expect(activeTree.getByText(/Google sync is incomplete for block 902/i)).toBeVisible();
    await page.getByRole('tab', { name: /^Selected tasks/i }).click();
    await expect(selectedPanel.getByText('Schedule applied', { exact: true })).toBeVisible();
    await expect(selectedPanel.getByRole('button', { name: 'Approve' })).toHaveCount(0);
    await expect(page.getByRole('region', { name: /shared calendar/i }).locator('.variant-scheduled')).toHaveCount(2);
    await expect(page.getByRole('region', { name: /shared calendar/i }).locator('.variant-proposed')).toHaveCount(0);
    await expect(page.getByRole('tree', { name: /work task nodes/i }).locator('.node-state-icon.scheduled')).toHaveCount(2);
    await expect(
      selectedPanel.locator('.selected-state-pill.scheduled'),
    ).toHaveCount(2);
    expect(strategist.capturedRunRequests[2].uiEvent).toMatchObject({
      kind: 'proposal_action',
      sourceTurnId: 'turn-revised',
      proposalId: revised.proposalId,
      proposalKind: 'schedule_patch',
      action: 'approve',
    });
    expect(strategist.capturedRunRequests).toHaveLength(3);
    expect(calendar.createdPayloads).toEqual([]);
    expect(calendar.updatedPayloads).toEqual([]);
    expect(calendar.deletedIds).toEqual([]);
  });

  test('announces a newly stale alternative and disables obsolete proposal controls', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    await page.clock.setFixedTime(new Date('2026-07-12T08:00:00-07:00'));
    const work = categoryFactory.build({ id: 261, name: 'Work', accentColor: '#1f6feb' });
    const draft = nodeFactory.build({ id: 2611, categoryId: work.id, title: 'Draft launch plan', timeEstimateMinutes: 60 });
    const review = nodeFactory.build({ id: 2612, categoryId: work.id, title: 'Review launch plan', timeEstimateMinutes: 60 });
    const taskIds = [draft.id, review.id];
    const initial = scheduleProposal('schedule-proposal-stale-source', taskIds, work.id, true);
    const refreshed = scheduleProposal(initial.proposalId, taskIds, work.id, true);
    refreshed.title = 'Refreshed schedule proposal';
    const staleRun = proposalRun('turn-stale-refresh', refreshed);
    if (staleRun.turn.response) {
      staleRun.turn.response.actionResult = {
        proposalId: initial.proposalId,
        sourceProposalId: initial.proposalId,
        sourceTurnId: 'turn-stale-source',
        status: 'stale',
      };
    }
    const strategist = await mockStrategistApi(page, {
      runResponses: [proposalRun('turn-stale-source', initial), staleRun],
    });
    await mockCalendarBlocks(page, { blocks: [] });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [draft, review] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select draft launch plan for execution staging/i }).click();
    await page.getByRole('button', { name: /select review launch plan for execution staging/i }).click();
    await page.getByRole('tab', { name: /^Chat$/i }).click();
    const composer = page.getByRole('textbox', { name: 'AI workspace prompt' });
    await composer.fill('schedule these today');
    await composer.press('Enter');

    const activeTree = page.getByRole('tabpanel', { name: /^Chat$/i });
    const sourceCard = activeTree.locator('.strategist-standard-proposal-card', {
      hasText: 'Schedule proposal with a conflict',
    }).first();
    await sourceCard.getByRole('button', { name: /use .* for review launch plan/i }).click();

    const staleStatus = sourceCard.getByText(/that time changed/i);
    await expect(staleStatus).toBeVisible();
    await expect(staleStatus).toBeFocused();
    await expect(sourceCard.getByRole('button', { name: /use .* for review launch plan/i })).toBeDisabled();
    const refreshedCard = activeTree.locator('.strategist-standard-proposal-card', {
      hasText: 'Refreshed schedule proposal',
    });
    await expect(refreshedCard.getByRole('button', { name: /use .* for review launch plan/i })).toBeEnabled();
    expect(strategist.capturedRunRequests[1].uiEvent).toMatchObject({
      sourceTurnId: 'turn-stale-source',
      proposalId: initial.proposalId,
      proposalKind: 'schedule_patch',
      payload: { conflictIndex: 0, alternativeIndex: 0 },
    });
  });

  test('clears schedule projection when history changes transcript or starts a new chat', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    await page.clock.setFixedTime(new Date('2026-07-12T08:00:00-07:00'));
    const work = categoryFactory.build({ id: 262, name: 'Work', accentColor: '#1f6feb' });
    const task = nodeFactory.build({ id: 2621, categoryId: work.id, title: 'History task', timeEstimateMinutes: 30 });
    const historicalProposal = scheduleProposal('schedule-proposal-history', [task.id], work.id, false);
    const emptySession = {
      ...session,
      id: 'schedule-empty-session',
      title: 'No schedule here',
      updatedAt: '2026-07-12T16:00:00Z',
    };
    const malformedSession = {
      ...session,
      id: 'schedule-malformed-session',
      title: 'Malformed saved schedule',
      updatedAt: '2026-07-12T17:00:00Z',
    };
    const malformedProposal = {
      ...historicalProposal,
      schedulePatch: null,
    } as unknown as typeof historicalProposal;
    await mockStrategistApi(page, {
      sessions: [session, emptySession, malformedSession],
      sessionDetails: {
        [session.id]: {
          session,
          turns: [proposalRun('turn-history-proposal', historicalProposal).turn],
        },
        [emptySession.id]: {
          session: emptySession,
          turns: [{
            id: 'turn-history-empty',
            prompt: 'Review my plan',
            createdAt: '2026-07-12T16:00:00Z',
            response: {
              providerLabel: 'Strategist',
              mode: 'review',
              summary: 'No schedule proposal in this chat.',
              contextCards: [],
              suggestedPrompts: [],
              proposal: null,
            },
          }],
        },
        [malformedSession.id]: {
          session: malformedSession,
          turns: [proposalRun('turn-history-malformed', malformedProposal).turn],
        },
      },
      sessionDetailDelayMs: { [session.id]: 500 },
    });
    await mockCalendarBlocks(page, { blocks: [] });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [task] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('tab', { name: /^Chat$/i }).click();
    await page.getByRole('button', { name: 'Chat history' }).click();
    await page.getByRole('button', { name: /resolve schedule conflict/i }).click();
    await expect(page.getByRole('region', { name: /shared calendar/i }).locator('.variant-proposed')).toHaveCount(1);

    await page.getByRole('button', { name: 'Chat history' }).click();
    await page.getByRole('button', { name: /no schedule here/i }).click();
    await expect(page.getByRole('region', { name: /shared calendar/i }).locator('.variant-proposed')).toHaveCount(0);
    await expect(page.locator('.selected-schedulepatch-card')).toHaveCount(0);

    await page.getByRole('button', { name: 'Chat history' }).click();
    await page.getByRole('button', { name: /malformed saved schedule/i }).click();
    await expect(page.getByText(/not approvable/i)).toBeVisible();
    await expect(page.locator('.selected-schedulepatch-card')).toHaveCount(0);

    await page.getByRole('button', { name: 'Chat history' }).click();
    await page.getByRole('button', { name: /resolve schedule conflict/i }).click();
    await page.getByRole('button', { name: 'New chat' }).click();
    await page.waitForTimeout(600);
    await expect(page.getByRole('region', { name: /shared calendar/i }).locator('.variant-proposed')).toHaveCount(0);
  });

  test('keeps a proposal reviewable and reports a failed action', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 263, name: 'Work', accentColor: '#1f6feb' });
    const task = nodeFactory.build({ id: 2631, categoryId: work.id, title: 'Failure task', timeEstimateMinutes: 30 });
    const proposal = scheduleProposal('schedule-proposal-action-failure', [task.id], work.id, false);
    await mockStrategistApi(page, {
      runResponses: [proposalRun('turn-action-failure', proposal)],
    });
    await mockCalendarBlocks(page, { blocks: [] });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [task] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select failure task for execution staging/i }).click();
    await page.getByRole('tab', { name: /^Chat$/i }).click();
    const composer = page.getByRole('textbox', { name: 'AI workspace prompt' });
    await composer.fill('schedule this task');
    await composer.press('Enter');
    const activeTree = page.getByRole('tabpanel', { name: /^Chat$/i });
    await activeTree.getByRole('button', { name: 'Approve schedule' }).click();
    await expect(
      activeTree.getByText(/The AI workspace could not complete that proposal action/i),
    ).toBeVisible();
    await expect(activeTree.getByRole('button', { name: 'Approve schedule' })).toBeEnabled();
  });

  test('routes Panel 3 edit and reject through the canonical workspace action handler', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 264, name: 'Work', accentColor: '#1f6feb' });
    const task = nodeFactory.build({ id: 2641, categoryId: work.id, title: 'Panel action task', timeEstimateMinutes: 30 });
    const proposal = scheduleProposal('schedule-proposal-panel-actions', [task.id], work.id, false);
    const editRequested: MockStrategistRunResponse = {
      session,
      turn: {
        id: 'turn-panel-edit',
        prompt: 'Edit Revised schedule proposal',
        createdAt: '2026-07-12T15:03:00Z',
        response: {
          providerLabel: 'Planner',
          mode: 'schedule_patch',
          summary: 'Describe the requested change.',
          contextCards: [],
          suggestedPrompts: [],
          proposal: null,
          actionResult: {
            proposalId: proposal.proposalId,
            sourceTurnId: 'turn-panel-source',
            status: 'edit_requested',
          },
        },
      },
    };
    const rejected: MockStrategistRunResponse = {
      session,
      turn: {
        id: 'turn-panel-reject',
        prompt: 'Reject Revised schedule proposal',
        createdAt: '2026-07-12T15:04:00Z',
        response: {
          providerLabel: 'Planner',
          mode: 'schedule_patch',
          summary: 'Schedule proposal rejected.',
          contextCards: [],
          suggestedPrompts: [],
          proposal: null,
          actionResult: {
            proposalId: proposal.proposalId,
            sourceTurnId: 'turn-panel-source',
            status: 'rejected',
          },
        },
      },
    };
    const strategist = await mockStrategistApi(page, {
      runResponses: [proposalRun('turn-panel-source', proposal), editRequested, rejected],
      runResponseDelayMs: 1000,
    });
    await mockCalendarBlocks(page, { blocks: [] });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [task] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select panel action task for execution staging/i }).click();
    await page.getByRole('tab', { name: /^Chat$/i }).click();
    const composer = page.getByRole('textbox', { name: 'AI workspace prompt' });
    await composer.fill('schedule this task');
    await composer.press('Enter');
    await page.getByRole('tab', { name: /^Selected tasks/i }).click();
    const selectedPanel = page.getByRole('region', { name: /^Selected tasks$/i });
    await selectedPanel.getByRole('button', { name: 'Edit' }).click();
    await expect(composer).toBeFocused();
    expect(strategist.capturedRunRequests[1].uiEvent).toMatchObject({
      proposalId: proposal.proposalId,
      proposalKind: 'schedule_patch',
      action: 'edit',
    });

    await page.getByRole('tab', { name: /^Selected tasks/i }).click();
    await selectedPanel.getByRole('button', { name: 'Reject' }).click();
    await expect(selectedPanel.locator('.selected-schedulepatch-card')).toHaveCount(0);
    expect(strategist.capturedRunRequests[2].uiEvent).toMatchObject({
      proposalId: proposal.proposalId,
      proposalKind: 'schedule_patch',
      action: 'reject',
    });
  });

  test('suppresses approval after a local preview edit makes the proposal dirty', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    await page.clock.setFixedTime(new Date('2026-07-12T08:00:00-07:00'));
    const work = categoryFactory.build({ id: 265, name: 'Work', accentColor: '#1f6feb' });
    const task = nodeFactory.build({ id: 2651, categoryId: work.id, title: 'Dirty preview task', timeEstimateMinutes: 30 });
    const proposal = scheduleProposal('schedule-proposal-dirty-preview', [task.id], work.id, false);
    await mockStrategistApi(page, {
      runResponses: [proposalRun('turn-dirty-preview', proposal)],
    });
    await mockCalendarBlocks(page, { blocks: [] });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [task] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select dirty preview task for execution staging/i }).click();
    await page.getByRole('tab', { name: /^Chat$/i }).click();
    const composer = page.getByRole('textbox', { name: 'AI workspace prompt' });
    await composer.fill('schedule this task');
    await composer.press('Enter');
    const calendar = page.getByRole('region', { name: /shared calendar/i });
    await calendar.locator('.variant-proposed').click();
    const dialog = page.getByRole('dialog', { name: /calendar block details/i });
    await dialog.getByRole('button', { name: /edit calendar block/i }).click();
    await dialog.getByLabel('Calendar block start date and time').fill('2026-07-12T11:30');
    await dialog.getByLabel('Calendar block end date and time').fill('2026-07-12T12:30');
    await dialog.getByRole('button', { name: /save calendar block time/i }).click();

    const activeTree = page.getByRole('tabpanel', { name: /^Chat$/i });
    await expect(activeTree.getByRole('button', { name: 'Approve schedule' })).toHaveCount(0);
    await expect(activeTree.getByText(/preview changed locally/i)).toBeVisible();
  });

  test('does not restore a revised proposal when its action response arrives after new chat', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    await page.clock.setFixedTime(new Date('2026-07-12T08:00:00-07:00'));
    const work = categoryFactory.build({ id: 266, name: 'Work', accentColor: '#1f6feb' });
    const draft = nodeFactory.build({ id: 2661, categoryId: work.id, title: 'Late draft', timeEstimateMinutes: 30 });
    const review = nodeFactory.build({ id: 2662, categoryId: work.id, title: 'Late review', timeEstimateMinutes: 30 });
    const taskIds = [draft.id, review.id];
    const initial = scheduleProposal('schedule-proposal-late-source', taskIds, work.id, true);
    const revised = scheduleProposal('schedule-proposal-late-revised', taskIds, work.id, false);
    const revisedRun = proposalRun('turn-late-revised', revised);
    if (revisedRun.turn.response) {
      revisedRun.turn.response.actionResult = {
        proposalId: revised.proposalId,
        sourceProposalId: initial.proposalId,
        sourceTurnId: 'turn-late-source',
        status: 'revised',
      };
    }
    await mockStrategistApi(page, {
      runResponses: [proposalRun('turn-late-source', initial), revisedRun],
      runResponseDelayMs: 500,
    });
    await mockCalendarBlocks(page, { blocks: [] });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [draft, review] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select late draft for execution staging/i }).click();
    await page.getByRole('button', { name: /select late review for execution staging/i }).click();
    await page.getByRole('tab', { name: /^Chat$/i }).click();
    const composer = page.getByRole('textbox', { name: 'AI workspace prompt' });
    await composer.fill('schedule these tasks');
    await composer.press('Enter');
    const activeTree = page.getByRole('tabpanel', { name: /^Chat$/i });
    await activeTree.getByRole('button', { name: /use .* for review launch plan/i }).click();
    await page.getByRole('button', { name: 'New chat' }).click();
    await page.waitForTimeout(650);

    await expect(page.getByRole('region', { name: /shared calendar/i }).locator('.variant-proposed')).toHaveCount(0);
    await expect(activeTree.getByText('Revised schedule proposal')).toHaveCount(0);
  });

  test('does not apply an older approval result to a newer history proposal', async ({
    page,
    categoryFactory,
    nodeFactory,
    mockWorkspace,
  }) => {
    const work = categoryFactory.build({ id: 267, name: 'Work', accentColor: '#1f6feb' });
    const task = nodeFactory.build({ id: 2671, categoryId: work.id, title: 'Proposal identity task', timeEstimateMinutes: 30 });
    const source = scheduleProposal('schedule-proposal-old-approval', [task.id], work.id, false);
    const newer = scheduleProposal('schedule-proposal-newer-history', [task.id], work.id, false);
    newer.title = 'Newer history schedule';
    newer.body = 'This newer proposal must remain pending.';
    const newerSession = {
      ...session,
      id: 'newer-proposal-session',
      title: 'Newer schedule proposal',
      updatedAt: '2026-07-12T18:00:00Z',
    };
    const appliedRun: MockStrategistRunResponse = {
      session,
      turn: {
        id: 'turn-old-approval-result',
        prompt: 'Approve Review schedule proposal',
        createdAt: '2026-07-12T18:01:00Z',
        response: {
          providerLabel: 'Planner',
          mode: 'schedule_patch',
          summary: 'The approved schedule is now on your calendar.',
          contextCards: [],
          suggestedPrompts: [],
          proposal: null,
          actionResult: {
            proposalId: source.proposalId,
            sourceTurnId: 'turn-old-approval-source',
            status: 'applied',
          },
        },
      },
    };
    await mockStrategistApi(page, {
      runResponses: [proposalRun('turn-old-approval-source', source), appliedRun],
      runResponseDelayMs: 500,
      sessions: [newerSession],
      sessionDetails: {
        [newerSession.id]: {
          session: newerSession,
          turns: [proposalRun('turn-newer-history', newer).turn],
        },
      },
    });
    await mockCalendarBlocks(page, { blocks: [] });
    const workspace = await mockWorkspace({
      categories: [work],
      nodesByCategoryId: { [work.id]: [task] },
    });
    const boot = workspace.waitForBoot(work.id);
    await page.goto('/');
    await boot;

    await page.getByRole('button', { name: /select proposal identity task for execution staging/i }).click();
    await page.getByRole('tab', { name: /^Chat$/i }).click();
    const composer = page.getByRole('textbox', { name: 'AI workspace prompt' });
    await composer.fill('schedule this task');
    await composer.press('Enter');
    const activeTree = page.getByRole('tabpanel', { name: /^Chat$/i });
    await activeTree.getByRole('button', { name: 'Approve schedule' }).click();
    await page.getByRole('button', { name: 'Chat history' }).click();
    await page.getByRole('button', { name: /newer schedule proposal/i }).click();
    await page.waitForTimeout(650);

    await page.getByRole('tab', { name: /^Selected tasks/i }).click();
    const selectedPanel = page.getByRole('region', { name: /^Selected tasks$/i });
    await expect(selectedPanel.locator('.selected-schedulepatch-card')).toContainText(
      'Newer history schedule',
    );
    await expect(selectedPanel.getByRole('button', { name: 'Approve' })).toBeEnabled();
    await expect(selectedPanel.getByText('Schedule applied')).toHaveCount(0);
  });
});
