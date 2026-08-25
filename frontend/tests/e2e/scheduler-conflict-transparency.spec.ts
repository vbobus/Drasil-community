import { test, expect } from '../support/fixtures/strategist.fixture';

test('names the blocking rule and removes duplicate resolution choices', async ({
  page,
  categoryFactory,
  nodeFactory,
  mockWorkspace,
  mockStrategistApi,
  openStrategist,
}) => {
  const category = categoryFactory.build({
    id: 91,
    name: 'Work',
    accentColor: '#1f6feb',
    iconSlug: 'briefcase',
    sortOrder: 0,
  });
  const task = nodeFactory.build({
    id: 9101,
    categoryId: category.id,
    title: 'Send Kunji the updated resume',
    sortOrder: 0,
  });
  const api = await mockStrategistApi({
    runResponses: [{
      session: {
        id: 'session-wake-conflict',
        title: 'Move resume task',
        providerLabel: 'Planner',
        modelName: 'gpt-5.4',
        createdAt: '2026-08-12T17:00:00.000Z',
        updatedAt: '2026-08-12T17:00:00.000Z',
      },
      turn: {
        id: 'turn-wake-conflict',
        prompt: 'I mean AM. Move the task.',
        createdAt: '2026-08-12T17:00:00.000Z',
        response: {
          providerLabel: 'Planner',
          mode: 'clarification',
          summary: 'The requested time reaches your saved wake-up boundary.',
          contextCards: [],
          suggestedPrompts: [],
          clarification: {
            message: 'How should I resolve the conflict with “Unavailable sleep time” during August 13, 2026, 6:30 AM–7:00 AM?',
            entityChoices: [],
            fieldChoices: [
              {
                kind: 'field',
                value: 'move-incoming',
                label: 'Move “Send Kunji the updated resume” to August 13, 2026, 7:00 AM–7:30 AM.',
                goalId: 'goal-wake-conflict',
                goalRevision: 2,
                decisionId: 'decision-wake-conflict',
                optionId: 'option-move-incoming',
              },
              {
                kind: 'field',
                value: 'legacy-duplicate',
                label: 'Move “Send Kunji the updated resume” to August 13, 2026, 7:00 AM–7:30 AM.',
                goalId: 'goal-wake-conflict',
                goalRevision: 2,
                decisionId: 'decision-wake-conflict',
                optionId: 'option-legacy-duplicate',
              },
            ],
            scopedFollowUpPrompts: [],
            conflicts: [{
              date: '2026-08-13',
              incomingTaskTitle: 'Send Kunji the updated resume',
              incomingPriority: 'medium',
              requestedStartAt: '2026-08-13T06:30:00-07:00',
              requestedEndAt: '2026-08-13T07:00:00-07:00',
              blockerTaskTitle: 'Unavailable sleep time',
              blockerPriority: 'high',
              blockerStartAt: '2026-08-13T06:30:00-07:00',
              blockerEndAt: '2026-08-13T07:00:00-07:00',
              fixedReason: 'Your saved preference keeps 11:00 PM–7:00 AM unavailable for sleep.',
              decisionReason: 'The requested task overlaps the final 30 minutes of that protected window.',
              recommendedAction: 'preserve_existing',
            }],
          },
        },
      },
    }],
  });
  const workspace = await mockWorkspace({
    categories: [category],
    nodesByCategoryId: { [category.id]: [task] },
  });

  const boot = workspace.waitForBoot(category.id);
  await page.goto('/');
  await boot;
  await openStrategist();
  await page.getByLabel('AI workspace prompt').fill('I mean AM. Move the task.');
  await page.getByRole('button', { name: /send message/i }).click();

  const clarification = page.locator('.strategist-clarification-card').last();
  await expect(clarification).toContainText('Unavailable sleep time');
  await expect(clarification).toContainText('6:30 AM–7:00 AM');
  await expect(clarification).toContainText('saved preference keeps 11:00 PM–7:00 AM unavailable for sleep');
  await expect(clarification).toContainText('final 30 minutes of that protected window');
  await expect(clarification).toContainText('Resolution choices');
  await expect(
    clarification.getByRole('button', {
      name: /Move “Send Kunji the updated resume” to August 13, 2026, 7:00 AM–7:30 AM/,
    }),
  ).toHaveCount(1);
  expect(api.capturedRunRequests).toHaveLength(1);
});

test('shows a direct exact-time reschedule for review without clarification', async ({
  page,
  categoryFactory,
  nodeFactory,
  mockWorkspace,
  mockStrategistApi,
  openStrategist,
}) => {
  const category = categoryFactory.build({
    id: 92,
    name: 'Work',
    accentColor: '#1f6feb',
    iconSlug: 'briefcase',
    sortOrder: 0,
  });
  const task = nodeFactory.build({
    id: 9201,
    categoryId: category.id,
    title: 'Send Kunji the updated resume',
    timeEstimateMinutes: 30,
    sortOrder: 0,
  });
  const version = { contractVersion: '2.0', revision: 1 };
  const entity = {
    ...version,
    ref: {
      ...version,
      kind: 'task',
      entityId: `task:${task.id}`,
      title: task.title,
    },
    label: task.title,
    context: category.name,
  };
  await mockStrategistApi({
    runResponses: [{
      session: {
        id: 'session-direct-reschedule',
        title: 'Move resume task',
        providerLabel: 'Planner',
        modelName: 'gpt-5.4',
        createdAt: '2026-08-12T17:00:00.000Z',
        updatedAt: '2026-08-12T17:00:00.000Z',
      },
      turn: {
        id: 'turn-direct-reschedule',
        prompt: 'move teh send resuem to kunji task to6:30 am tmrw',
        createdAt: '2026-08-12T17:00:00.000Z',
        response: {
          providerLabel: 'Planner',
          mode: 'review',
          summary: 'Review the exact task move before applying it.',
          contextCards: [],
          suggestedPrompts: [],
          reviewDocument: {
            ...version,
            reviewId: 'review:direct-reschedule',
            sessionId: 'session-direct-reschedule',
            goalId: 'goal:direct-reschedule',
            goalRevision: 2,
            changeSetId: 'changeset:direct-reschedule',
            changeSetRevision: 1,
            simulationId: 'simulation:direct-reschedule',
            simulationInputFingerprint: 'a'.repeat(64),
            simulationOutputFingerprint: 'b'.repeat(64),
            correlationId: 'correlation:direct-reschedule',
            causationId: 'causation:direct-reschedule',
            summary: 'Move Send Kunji the updated resume to 6:30 AM tomorrow.',
            blocks: [
              {
                ...version,
                kind: 'entity_references',
                blockId: 'block:direct-entity',
                title: 'Affected items',
                entities: [entity],
              },
              {
                ...version,
                kind: 'schedule_summary',
                blockId: 'block:direct-schedule',
                title: 'Schedule',
                cadence: 'One exact task move',
                rangeLabel: 'August 13, 2026 at 6:30 AM through 7:00 AM',
                timeZone: 'America/Vancouver',
                details: ['Your exact 6:30 AM instruction is used as a one-off exception.'],
              },
            ],
            decisions: [],
            controls: [],
          },
        },
      },
    }],
  });
  const workspace = await mockWorkspace({
    categories: [category],
    nodesByCategoryId: { [category.id]: [task] },
  });

  const boot = workspace.waitForBoot(category.id);
  await page.goto('/');
  await boot;
  await openStrategist();
  await page.getByLabel('AI workspace prompt').fill(
    'move teh send resuem to kunji task to6:30 am tmrw',
  );
  await page.getByRole('button', { name: /send message/i }).click();

  await expect(page.locator('.strategist-clarification-card')).toHaveCount(0);
  await expect(page.getByText(/6:30 AM through 7:00 AM/)).toBeVisible();
  await expect(page.getByText(/one-off exception/)).toBeVisible();
});
