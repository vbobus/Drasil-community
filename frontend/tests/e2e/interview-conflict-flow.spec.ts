import { test, expect } from '../support/fixtures/strategist.fixture';

const session = {
  id: 'session-interview-conflict',
  title: 'Schedule an interview',
  providerLabel: 'Planner',
  modelName: 'gpt-5.4',
  createdAt: '2026-07-18T17:00:00.000Z',
  updatedAt: '2026-07-18T17:00:00.000Z',
};

test.skip('names an exact-time conflict and reviews moving only the affected occurrence', async ({
  page,
  categoryFactory,
  nodeFactory,
  mockWorkspace,
  mockStrategistApi,
  openStrategist,
}) => {
  const work = categoryFactory.build({
    id: 81,
    name: 'Work',
    accentColor: '#1f6feb',
    iconSlug: 'briefcase',
    sortOrder: 0,
  });
  const health = categoryFactory.build({
    id: 82,
    name: 'Health',
    accentColor: '#16a34a',
    iconSlug: 'heart',
    sortOrder: 1,
  });
  const teamCall = nodeFactory.build({
    id: 8101,
    categoryId: work.id,
    title: 'Team call',
    emoji: '📞',
    timeEstimateMinutes: 60,
    sortOrder: 0,
  });
  const taskReference = {
    taskId: teamCall.id,
    title: teamCall.title,
    emoji: teamCall.emoji,
    nodeType: 'task',
    categoryId: work.id,
    categoryName: work.name,
    categoryAccentColor: work.accentColor,
  };
  const workout = nodeFactory.build({
    id: 8201,
    categoryId: health.id,
    title: 'Workout',
    emoji: '💪',
    timeEstimateMinutes: 120,
    sortOrder: 0,
  });
  const workoutReference = {
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
          id: 'turn-interview-conflict',
          prompt: 'i have an interview on next friday from 6- 7 pm',
          createdAt: '2026-07-18T17:01:00.000Z',
          response: {
            providerLabel: 'Planner',
            mode: 'clarification',
            summary: 'Interview overlaps your Workout and Team call.',
            contextCards: [],
            suggestedPrompts: [],
            taskReferences: [taskReference, workoutReference],
            clarification: {
              message: 'Interview at 6:00 PM conflicts with Workout (4:15 PM–6:15 PM) and Team call (6:30 PM–7:30 PM). Choose which schedule should keep this time.',
              entityChoices: [],
              fieldChoices: [
                {
                  kind: 'field',
                  value: 'preserve_existing',
                  label: 'Keep Workout and Team call here',
                  description: 'Move the interview to another time.',
                  followUpPrompt: 'Keep Team call here and move the Interview.',
                },
                {
                  kind: 'field',
                  value: 'move_existing',
                  label: 'Use this time for Interview',
                  description: 'Move only these two Friday occurrences.',
                  followUpPrompt: 'Keep the Interview here and move these existing tasks.',
                },
              ],
              scopedFollowUpPrompts: [],
              conflicts: [
                {
                  date: '2026-07-24',
                  incomingTaskTitle: 'Interview',
                  incomingPriority: 'medium',
                  requestedStartAt: '2026-07-24T18:00:00',
                  requestedEndAt: '2026-07-24T19:00:00',
                  blockerTaskTitle: 'Workout',
                  blockerPriority: 'medium',
                  blockerStartAt: '2026-07-24T16:15:00',
                  blockerEndAt: '2026-07-24T18:15:00',
                  decisionReason: 'The workout is flexible, but the Team call is another exact-time commitment.',
                },
                {
                  date: '2026-07-24',
                  incomingTaskTitle: 'Interview',
                  incomingPriority: 'medium',
                  requestedStartAt: '2026-07-24T18:00:00',
                  requestedEndAt: '2026-07-24T19:00:00',
                  blockerTaskTitle: 'Team call',
                  blockerPriority: 'medium',
                  blockerStartAt: '2026-07-24T18:30:00',
                  blockerEndAt: '2026-07-24T19:30:00',
                  decisionReason: 'The workout is flexible, but the Team call is another exact-time commitment.',
                },
              ],
            },
          },
        },
      },
      {
        session,
        turn: {
          id: 'turn-interview-proposal',
          prompt: 'Use this time for Interview',
          createdAt: '2026-07-18T17:02:00.000Z',
          response: {
            providerLabel: 'Planner',
            mode: 'branch',
            summary: 'Keep the Interview at 6:00 PM and move only this Team call.',
            contextCards: [],
            suggestedPrompts: [],
            taskReferences: [taskReference, workoutReference],
            proposal: {
              proposalId: 'proposal-interview',
              proposalKind: 'branch',
              operation: 'create_and_schedule',
              legacyResponseKey: 'branchProposal',
              title: 'Create and schedule Interview',
              body: 'You chose the Interview for this time, so only the two overlapping Friday occurrences move.',
              targetNodeIds: [teamCall.id, workout.id],
              preview: [],
              validationWarnings: [],
              taskPatchStatus: 'valid',
              taskPatch: {
                operations: [{
                  op: 'create',
                  tempId: 'interview',
                  categoryId: work.id,
                  title: 'Interview',
                }],
              },
              taskPatchValidation: {
                status: 'valid',
                errors: [],
                warnings: [],
                operation_count: 2,
              },
              taskPatchSimulation: {
                status: 'valid',
                errors: [],
                warnings: [],
                operation_count: 2,
              },
              payload: {
                created_task_schedule_plan: {
                  status: 'valid',
                  blocks: [{
                    target_temp_id: 'interview',
                    task_title: 'Interview',
                    start_at: '2026-07-24T18:00:00',
                    end_at: '2026-07-24T19:00:00',
                  }],
                  conflict_resolutions: [{
                    date: '2026-07-24',
                    action: 'moved_existing',
                    incomingTaskTitle: 'Interview',
                    blockers: [
                      {
                        taskId: workout.id,
                        taskTitle: 'Workout',
                        startAt: '2026-07-24T16:15:00',
                        endAt: '2026-07-24T18:15:00',
                        movedStartAt: '2026-07-24T19:00:00',
                        movedEndAt: '2026-07-24T21:00:00',
                        isRecurring: true,
                      },
                      {
                        taskId: teamCall.id,
                        taskTitle: 'Team call',
                        startAt: '2026-07-24T18:30:00',
                        endAt: '2026-07-24T19:30:00',
                        movedStartAt: '2026-07-24T21:00:00',
                        movedEndAt: '2026-07-24T22:00:00',
                        isRecurring: true,
                      },
                    ],
                  }],
                },
              },
            },
          },
        },
      },
    ],
  });
  const workspace = await mockWorkspace({
    categories: [work, health],
    nodesByCategoryId: { [work.id]: [teamCall], [health.id]: [workout] },
  });

  const boot = workspace.waitForBoot(work.id);
  await page.goto('/');
  await boot;
  await openStrategist();

  const composer = page.getByLabel('AI workspace prompt');
  await composer.fill('i have an interview on next friday from 6- 7 pm');
  await page.getByRole('button', { name: /send message/i }).click();

  const clarification = page.locator('.strategist-clarification-card').last();
  await expect(clarification).toContainText('Interview at 6:00 PM conflicts with');
  await expect(clarification).toContainText('Team call');
  await expect(clarification).toContainText('Workout');
  await expect(clarification).toContainText('6:30 PM–7:30 PM');
  await expect(clarification).toContainText('Team call is another exact-time commitment');
  await clarification.getByRole('button', { name: /Use this time for Interview/ }).click();

  const proposal = page.locator('.strategist-standard-proposal-card').last();
  await expect(proposal).toContainText('Create and schedule Interview');
  await expect(proposal.getByLabel('Proposed new task schedule')).toContainText('6:00 PM');
  await expect(proposal).toContainText('Making room');
  await expect(proposal).toContainText('Team call');
  await expect(proposal).toContainText('Workout');
  await expect(proposal).toContainText(/6:30 PM.*7:30 PM/);
  await expect(proposal).toContainText(/9:00 PM.*10:00 PM/);
  await expect(proposal.getByRole('button', { name: 'Approve' })).toBeVisible();

  expect(strategist.capturedRunRequests).toHaveLength(2);
  expect(strategist.capturedRunRequests[1]).toMatchObject({
    sessionId: session.id,
    uiEvent: {
      kind: 'clarification_choice',
      sourceTurnId: 'turn-interview-conflict',
      payload: { choiceId: 'move_existing' },
    },
  });
});
