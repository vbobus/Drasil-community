import { test, expect } from '../support/fixtures/strategist.fixture';

const session = {
  id: 'session-recurring-proposal-revision',
  title: 'Schedule weekday team call',
  providerLabel: 'Planner',
  modelName: 'gpt-5.4',
  createdAt: '2026-07-18T17:00:00.000Z',
  updatedAt: '2026-07-18T17:00:00.000Z',
};

function recurringProposal(options: {
  proposalId: string;
  sourceProposalId?: string;
  workoutTaskId: number;
  workoutCategoryId: number;
  moveWorkoutBeforeCall: boolean;
}) {
  const workoutDates = ['2026-07-20', '2026-07-22', '2026-07-27', '2026-07-29'];
  const movedStart = options.moveWorkoutBeforeCall ? '16:15' : '19:45';
  const movedEnd = options.moveWorkoutBeforeCall ? '18:15' : '21:45';
  const occurrences = [
    '2026-07-20',
    '2026-07-21',
    '2026-07-22',
    '2026-07-23',
    '2026-07-24',
  ].map((dateValue, index) => ({
    occurrence_id: `team-call-${options.proposalId}-${index}`,
    date: dateValue,
    start_at: `${dateValue}T18:30:00`,
    end_at: `${dateValue}T19:30:00`,
    shifted: false,
  }));

  return {
    proposalId: options.proposalId,
    proposalKind: 'branch',
    operation: 'create_and_schedule',
    legacyResponseKey: 'branchProposal',
    title: 'Create and schedule Team call',
    body: options.moveWorkoutBeforeCall
      ? 'Team call stays at 6:30 PM. Workout moves earlier only on conflicting Mondays and Wednesdays.'
      : 'Team call stays at 6:30 PM. Workout moves later on conflicting Mondays and Wednesdays.',
    targetNodeIds: [options.workoutTaskId],
    preview: [],
    validationWarnings: [],
    taskPatchStatus: 'valid',
    taskPatch: {
      operations: [{
        op: 'create',
        tempId: 'team-call',
        categoryId: options.workoutCategoryId,
        title: 'Team call',
      }],
    },
    taskPatchValidation: {
      status: 'valid',
      errors: [],
      warnings: [],
      operation_count: 27,
    },
    taskPatchSimulation: {
      status: 'valid',
      errors: [],
      warnings: [],
      operation_count: 27,
    },
    payload: {
      ...(options.sourceProposalId
        ? { revision_of_proposal_id: options.sourceProposalId }
        : {}),
      recurring_schedule_plan: {
        frequency: 'weekly',
        days_of_week: ['monday', 'tuesday', 'wednesday', 'thursday', 'friday'],
        category_name: 'Work',
        duration_minutes: 60,
        start_date: '2026-07-20',
        end_mode: 'never',
        occurrences,
        conflict_resolutions: workoutDates.map((dateValue) => ({
          date: dateValue,
          action: 'moved_existing',
          incomingTaskTitle: 'Team call',
          blockers: [{
            taskId: options.workoutTaskId,
            taskTitle: 'Workout',
            startAt: `${dateValue}T17:00:00`,
            endAt: `${dateValue}T19:00:00`,
            movedStartAt: `${dateValue}T${movedStart}:00`,
            movedEndAt: `${dateValue}T${movedEnd}:00`,
            isRecurring: true,
          }],
        })),
      },
    },
  };
}

test.skip('keeps a recurring proposal edit scoped to existing conflict days in the approval UI', async ({
  page,
  categoryFactory,
  nodeFactory,
  mockWorkspace,
  mockStrategistApi,
  openStrategist,
}) => {
  const work = categoryFactory.build({
    id: 71,
    name: 'Work',
    accentColor: '#1f6feb',
    iconSlug: 'briefcase',
    sortOrder: 0,
  });
  const health = categoryFactory.build({
    id: 72,
    name: 'Health',
    accentColor: '#16a34a',
    iconSlug: 'heart',
    sortOrder: 1,
  });
  const workout = nodeFactory.build({
    id: 7201,
    categoryId: health.id,
    title: 'Workout',
    emoji: '💪',
    timeEstimateMinutes: 120,
    sortOrder: 0,
  });
  const initialProposal = recurringProposal({
    proposalId: 'proposal-team-call-initial',
    workoutTaskId: workout.id,
    workoutCategoryId: work.id,
    moveWorkoutBeforeCall: false,
  });
  const revisedProposal = recurringProposal({
    proposalId: 'proposal-team-call-before-workout',
    sourceProposalId: initialProposal.proposalId,
    workoutTaskId: workout.id,
    workoutCategoryId: work.id,
    moveWorkoutBeforeCall: true,
  });
  const taskReference = {
    taskId: workout.id,
    title: workout.title,
    emoji: workout.emoji,
    nodeType: 'task',
    categoryId: health.id,
    categoryName: health.name,
    categoryAccentColor: health.accentColor,
  };

  const strategist = await mockStrategistApi({
    runResponses: [
      {
        session,
        turn: {
          id: 'turn-team-call-duration',
          prompt: 'Schedule a team call at 6:30 PM every weekday.',
          createdAt: '2026-07-18T17:00:00.000Z',
          response: {
            providerLabel: 'Planner',
            mode: 'clarification',
            summary: 'How long should each team call last?',
            contextCards: [],
            suggestedPrompts: [],
            clarification: {
              message: 'How many minutes should each weekday Team call last?',
              entityChoices: [],
              fieldChoices: [],
              scopedFollowUpPrompts: [],
              conflicts: [],
            },
          },
        },
      },
      {
        session,
        turn: {
          id: 'turn-team-call-initial',
          prompt: '1 hr',
          createdAt: '2026-07-18T17:01:00.000Z',
          response: {
            providerLabel: 'Planner',
            mode: 'branch',
            summary: 'Create a recurring weekday Team call.',
            contextCards: [],
            suggestedPrompts: [],
            taskReferences: [taskReference],
            proposal: initialProposal,
          },
        },
      },
      {
        session,
        turn: {
          id: 'turn-team-call-edit',
          prompt: 'Edit Create and schedule Team call',
          createdAt: '2026-07-18T17:02:00.000Z',
          response: {
            providerLabel: 'Planner',
            mode: 'clarification',
            summary: 'What would you like to change?',
            contextCards: [],
            suggestedPrompts: [],
            clarification: {
              message: 'What should change about this schedule?',
              entityChoices: [],
              fieldChoices: [],
              scopedFollowUpPrompts: [],
              conflicts: [],
            },
            actionResult: {
              proposalId: initialProposal.proposalId,
              sourceTurnId: 'turn-team-call-initial',
              status: 'edit_requested',
            },
          },
        },
      },
      {
        session,
        turn: {
          id: 'turn-team-call-revised',
          prompt: 'Keep Workout before the Team call and move it only when there is a conflict.',
          createdAt: '2026-07-18T17:03:00.000Z',
          response: {
            providerLabel: 'Planner',
            mode: 'branch',
            summary: 'Workout now moves earlier only on conflicting Mondays and Wednesdays.',
            contextCards: [],
            suggestedPrompts: [],
            taskReferences: [taskReference],
            proposal: revisedProposal,
          },
        },
      },
    ],
  });
  const workspace = await mockWorkspace({
    categories: [work, health],
    nodesByCategoryId: {
      [work.id]: [],
      [health.id]: [workout],
    },
  });

  const boot = workspace.waitForBoot(work.id);
  await page.goto('/');
  await boot;
  await openStrategist();

  const composer = page.getByLabel('AI workspace prompt');
  await composer.fill('Schedule a team call at 6:30 PM every weekday.');
  await page.getByRole('button', { name: /send message/i }).click();
  await expect(page.getByText('How many minutes should each weekday Team call last?')).toBeVisible();

  await composer.fill('1 hr');
  await page.getByRole('button', { name: /send message/i }).click();
  const initialCard = page.locator('.strategist-standard-proposal-card').first();
  await expect(initialCard).toContainText('Repeats: Weekdays');
  await expect(initialCard.getByLabel('Proposed recurring schedule')).toContainText('Weekdays');
  await initialCard.getByRole('button', { name: 'Edit' }).click();

  await composer.fill('Keep Workout before the Team call and move it only when there is a conflict.');
  await page.getByRole('button', { name: /send message/i }).click();

  const revisedCard = page.locator('.strategist-standard-proposal-card').last();
  await expect(revisedCard).toBeVisible();
  await expect(revisedCard).toContainText('Repeats: Weekdays');
  await expect(revisedCard.getByLabel('Proposed recurring schedule')).toContainText('Weekdays');
  await expect(revisedCard.getByText(/Monday and Wednesday.*Jul 20.*Jul 29, 2026/)).toBeVisible();
  await expect(revisedCard.getByText(/5:00 PM.*4:15 PM/)).toBeVisible();
  await expect(revisedCard.getByText(/Tuesday.*Workout/i)).toHaveCount(0);
  const movedWorkoutChip = revisedCard.locator(
    '.strategist-compact-recurrence-list .ai-task-reference',
  );
  const movedWorkoutBounds = await movedWorkoutChip.boundingBox();
  expect(movedWorkoutBounds).not.toBeNull();
  expect(movedWorkoutBounds!.width).toBeGreaterThan(movedWorkoutBounds!.height * 2);
  await expect(revisedCard.getByRole('button', { name: 'Approve' })).toBeVisible();

  await expect(initialCard.getByText('A revised proposal replaced this one.')).toBeVisible();
  await expect(initialCard.getByRole('button', { name: 'Approve' })).toHaveCount(0);
  expect(strategist.capturedRunRequests).toHaveLength(4);
  expect(strategist.capturedRunRequests[2]).toMatchObject({
    sessionId: session.id,
    uiEvent: {
      kind: 'proposal_action',
      proposalId: initialProposal.proposalId,
      proposalKind: 'branch',
      action: 'edit',
    },
  });
  expect(strategist.capturedRunRequests[3]).toMatchObject({
    sessionId: session.id,
    prompt: 'Keep Workout before the Team call and move it only when there is a conflict.',
  });
});
