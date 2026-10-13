import type {
  StrategistReviewAction,
  StrategistReviewActionOutcome,
  StrategistReviewDocument,
  StrategistStandardProposal,
} from '../../src/api/strategist';
import type { Page } from '@playwright/test';
import { test, expect } from '../support/fixtures/strategist.fixture';

const version = { contractVersion: '2.0', revision: 1 };
const binding = {
  ...version,
  sessionId: 'session-review',
  goalId: 'goal:review',
  goalRevision: 4,
  changeSetId: 'changeset:review',
  changeSetRevision: 3,
  simulationId: 'simulation:review',
  simulationInputFingerprint: 'a'.repeat(64),
  simulationOutputFingerprint: 'b'.repeat(64),
  correlationId: 'correlation:review',
  causationId: 'causation:review',
};
const entity = {
  ...version,
  ref: {
    contractVersion: '2.0',
    kind: 'task',
    entityId: 'task:7401',
    revision: 2,
    title: 'Prepare interview',
  },
  label: 'Prepare interview',
  context: 'Work',
};
const change = {
  ...version,
  changeId: 'operation:update-interview',
  summary: 'Update interview preparation',
  entities: [entity],
  before: 'Scheduled for Monday afternoon',
  after: 'Scheduled for Tuesday morning',
};

function action(
  kind: StrategistReviewAction['action'],
  suffix: string = kind,
): StrategistReviewAction {
  const base = {
    ...version,
    action: kind,
    eventId: `event:${suffix}`,
    idempotencyKey: `idem:${suffix}`,
    reviewId: `review:${suffix}`,
    reviewRevision: 1,
    binding,
  };
  if (kind === 'approve') return { ...base, action: kind, commandId: `command:${suffix}` };
  if (kind === 'decision') {
    return { ...base, action: kind, decisionId: 'decision:time', optionId: 'option:morning' };
  }
  if (kind === 'retry') {
    return {
      ...base,
      action: kind,
      executionId: 'execution:original',
      originalCommandId: 'command:original',
      originalIdempotencyKey: 'idem:original',
    };
  }
  return { ...base, action: kind } as StrategistReviewAction;
}

function documentWith(
  controls: StrategistReviewAction[],
  options: { allBlocks?: boolean; decision?: boolean } = {},
): StrategistReviewDocument {
  const reviewId = controls[0]?.reviewId ?? 'review:empty';
  const blocks: StrategistReviewDocument['blocks'] = options.allBlocks ? [
    { ...version, kind: 'entity_references', blockId: 'block:entities', title: 'Affected items', entities: [entity] },
    {
      ...version,
      kind: 'grouped_changes',
      blockId: 'block:groups',
      title: 'What will change',
      groups: [{ ...version, groupId: 'group:schedule', summary: 'Related changes', changes: [change] }],
    },
    { ...version, kind: 'semantic_diff', blockId: 'block:diff', title: 'Before and after', changes: [change] },
    {
      ...version,
      kind: 'schedule_summary',
      blockId: 'block:schedule',
      title: 'Schedule',
      cadence: 'Tuesday morning',
      rangeLabel: 'July 21, 2026 at 9:00 AM through 10:00 AM',
      timeZone: 'America/Vancouver',
      details: ['One focused hour'],
    },
    {
      ...version,
      kind: 'recurrence_summary',
      blockId: 'block:recurrence',
      title: 'Repeating schedule',
      cadence: 'Every Tuesday at 9:00 AM',
      rangeLabel: 'July 21 through August 25, 2026',
      scopeLabel: 'This series',
      timeZone: 'America/Vancouver',
      exceptions: ['Skip August 4'],
      additionalExceptions: 'Two later exceptions follow the same pattern.',
    },
    {
      ...version,
      kind: 'conflict_resolution',
      blockId: 'block:conflicts',
      title: 'Conflict resolution',
      conflicts: [{
        ...version,
        conflictId: 'conflict:one',
        summary: 'A scheduling conflict was found Tuesday morning.',
        resolution: 'Move the flexible interview preparation.',
        affectedEntities: [entity],
        scopeLabel: 'This occurrence',
        intervals: ['July 21, 2026 at 9:00 AM through 10:00 AM PDT'],
        alternatives: ['Keep the existing commitment unchanged.'],
      }],
    },
    { ...version, kind: 'assumptions', blockId: 'block:assumptions', title: 'Assumptions', statements: ['Tuesday morning is flexible.'] },
    { ...version, kind: 'warning', blockId: 'block:warning', title: 'What to know', severity: 'warning', message: 'The first occurrence needs confirmation.' },
    { ...version, kind: 'semantic_fallback', blockId: 'block:fallback', title: 'Plan changes', summary: 'These related planning changes stay together.', effects: [change] },
    {
      ...version,
      kind: 'execution_status',
      blockId: 'block:execution',
      title: 'Current status',
      status: 'verification_failed',
      summary: 'Local changes committed but need inspection.',
      verificationStatus: 'verification_failed',
      verificationSummary: 'A fresh read found a mismatch.',
      recovery: 'manual_inspection',
      executionId: 'execution:one',
      commandId: 'command:one',
      idempotencyKey: 'idem:one',
      replayed: true,
      bindings: [],
      facts: [],
      syncItems: [{
        ...version,
        syncId: 'sync:one',
        providerLabel: 'Google Calendar',
        status: 'failed',
        summary: 'Delivery needs inspection.',
        attempts: 3,
        retryAt: '2026-07-19T18:00:00.000Z',
        recovery: 'inspect',
      }],
    },
  ] : [{ ...version, kind: 'semantic_diff', blockId: `block:${reviewId}`, title: 'Before and after', changes: [change] }];
  return {
    ...version,
    reviewId,
    sessionId: binding.sessionId,
    goalId: binding.goalId,
    goalRevision: binding.goalRevision,
    changeSetId: binding.changeSetId,
    changeSetRevision: binding.changeSetRevision,
    simulationId: binding.simulationId,
    simulationInputFingerprint: binding.simulationInputFingerprint,
    simulationOutputFingerprint: binding.simulationOutputFingerprint,
    correlationId: binding.correlationId,
    causationId: binding.causationId,
    summary: 'Review the interview preparation changes before applying them.',
    blocks,
    decisions: options.decision ? [{
      decisionId: 'decision:time',
      question: 'Which time should be used?',
      reason: 'Two safe times are available.',
      options: [{ optionId: 'option:morning', label: 'Tuesday morning' }],
    }] : [],
    controls,
  };
}

function runResponse(reviewDocument: StrategistReviewDocument, turn: number) {
  return {
    session: {
      id: 'session-review',
      title: 'Interview plan review',
      providerLabel: 'Planner',
      modelName: 'gpt-5.4',
      createdAt: '2026-07-19T17:00:00.000Z',
      updatedAt: `2026-07-19T17:00:0${turn}.000Z`,
    },
    turn: {
      id: `turn-review-${turn}`,
      prompt: `Review request ${turn}`,
      createdAt: `2026-07-19T17:00:0${turn}.000Z`,
      response: {
        providerLabel: 'Planner',
        mode: 'review' as const,
        summary: 'A review is ready.',
        contextCards: [],
        suggestedPrompts: [],
        reviewDocument,
      },
    },
  };
}

function legacyRunResponse(proposal: StrategistStandardProposal, turn: number) {
  const response = runResponse(documentWith([]), turn);
  return {
    ...response,
    turn: {
      ...response.turn,
      response: {
        providerLabel: 'Planner',
        mode: 'branch' as const,
        summary: 'A recurring schedule is ready for review.',
        contextCards: [],
        suggestedPrompts: [],
        proposal,
      },
    },
  };
}

async function registerReviewActions(
  page: Page,
  outcomes: StrategistReviewActionOutcome[],
  delayMs = 0,
) {
  const captured: StrategistReviewAction[] = [];
  await page.route('**/api/v1/ai/strategist/sessions/session-review/review-actions', async (route) => {
    captured.push(await route.request().postDataJSON());
    const outcome = outcomes.shift();
    if (!outcome) throw new Error('No mocked review-action outcome remains.');
    if (delayMs > 0) {
      await new Promise((resolve) => { setTimeout(resolve, delayMs); });
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(outcome),
    });
  });
  return captured;
}

test.describe('Story 8.7 - generic ReviewDocument and typed interactions', () => {
  test('renders the complete task-family semantic surface without legacy operation vocabulary', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({ id: 73, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 7301, categoryId: category.id, title: 'Launch plan', sortOrder: 0 });
    const approve = action('approve', 'task-family');
    const review = documentWith([approve]);
    const taskChanges = [
      { changeId: 'operation:create', summary: 'Create launch brief', before: null, after: 'Launch brief' },
      { changeId: 'operation:update', summary: 'Rename launch plan', before: 'Launch plan', after: 'Launch execution plan' },
      { changeId: 'operation:move', summary: 'Move research under launch plan', before: 'Work root', after: 'Launch plan' },
      { changeId: 'operation:archive', summary: 'Archive the superseded outline', before: 'Active', after: 'Archived' },
      { changeId: 'operation:delete', summary: 'Delete the obsolete scratch task', before: 'Obsolete scratch task', after: null },
    ].map((item) => ({ ...change, ...item }));
    review.summary = 'Review every task change before applying it.';
    review.blocks = [{
      ...version,
      kind: 'semantic_diff',
      blockId: 'block:task-family',
      title: 'Task changes',
      changes: taskChanges,
    }];
    await mockStrategistApi({ runResponses: [runResponse(review, 1)] });
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    await page.getByLabel('AI workspace prompt').fill('Review the complete task change set');
    await page.getByRole('button', { name: /send message/i }).click();

    const renderedChanges = page.getByRole('region', { name: 'Task changes' }).getByRole('listitem');
    await expect(renderedChanges).toHaveCount(taskChanges.length);
    const renderedSummaries = [
      /Create launch brief/,
      /Rename .*Launch plan/,
      /Move research under .*Launch plan/,
      /Archive the superseded outline/,
      /Delete the obsolete scratch task/,
    ];
    for (const [index, summary] of renderedSummaries.entries()) {
      await expect(renderedChanges.nth(index)).toContainText(summary);
    }
    await expect(page.getByText(/task\.create|task\.update|task\.move|task\.archive|task\.delete|TaskPatch/)).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Apply changes' })).toBeVisible();
    await page.screenshot({ path: '../.impeccable/review/workspace-approval-dark.png', animations: 'disabled' });
  });

  test('renders local schedule time separately from its labeled time zone', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({ id: 79, name: 'Home', accentColor: '#1f6feb', iconSlug: 'home', sortOrder: 0 });
    const task = nodeFactory.build({ id: 7901, categoryId: category.id, title: 'Cooking', sortOrder: 0 });
    const review = documentWith([action('approve', 'time-zone')]);
    review.blocks = [{
      ...version,
      kind: 'schedule_summary',
      blockId: 'block:time-zone',
      title: 'Schedule',
      cadence: 'New scheduled time',
      rangeLabel: 'July 25, 2026, 5:00 PM–7:30 PM',
      timeZone: 'America/Vancouver (UTC−07:00)',
      details: ['Add time to the calendar: July 25, 2026, 5:00 PM–7:30 PM'],
    }];
    await mockStrategistApi({ runResponses: [runResponse(review, 1)] });
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    await page.getByLabel('AI workspace prompt').fill('Review the cooking schedule');
    await page.getByRole('button', { name: /send message/i }).click();

    const schedule = page.getByRole('region', { name: 'Schedule' });
    await expect(schedule.getByText('July 25, 2026, 5:00 PM–7:30 PM', { exact: true })).toBeVisible();
    await expect(schedule.getByText('Time zone: America/Vancouver (UTC−07:00)', { exact: true })).toBeVisible();
    await expect(schedule.getByText(/5:00 PM -07:00|7:30 PM -07:00/)).toHaveCount(0);
  });

  test('renders a consolidated preference update without repeated proposal copy', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({ id: 81, name: 'Home', accentColor: '#1f6feb', iconSlug: 'home', sortOrder: 0 });
    const task = nodeFactory.build({ id: 8101, categoryId: category.id, title: 'Cooking', sortOrder: 0 });
    const review = documentWith([action('approve', 'gap-preference')]);
    const globalPreference = 'Schedule tasks shorter than 45 minutes back-to-back; prefer a 15-minute buffer between tasks of 45 minutes or longer.';
    const scopedPreference = 'Prefer a 15-minute gap around tasks lasting at least 45 minutes.';
    const newPreference = 'Prefer a 20-minute gap between tasks lasting at least 45 minutes; shorter tasks can remain back-to-back. Applies to all planning.';
    review.summary = 'Review your updated gap-between-tasks preference before applying it.';
    review.blocks = [{
      ...version,
      kind: 'semantic_diff',
      blockId: 'block:gap-preference',
      title: 'Before and after',
      changes: [{
        ...change,
        changeId: 'operation:update-gap',
        supportingOperationIds: ['operation:retire-gap-copy'],
        summary: 'Replace overlapping gap preferences',
        entities: [],
        before: `Replace these overlapping saved preferences:\n- ${globalPreference}\n- ${scopedPreference}`,
        after: newPreference,
      }],
    }];
    await mockStrategistApi({ runResponses: [runResponse(review, 1)] });
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    await page.getByLabel('AI workspace prompt').fill('Add a 20-minute gap between tasks');
    await page.getByRole('button', { name: /send message/i }).click();

    const card = page.locator('.review-document-card');
    await expect(card.getByRole('heading', { name: review.summary, exact: true })).toBeVisible();
    await expect(page.getByText('A review is ready.', { exact: true })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Proposed changes', exact: true })).toHaveCount(0);
    await expect(page.getByRole('heading', { name: 'Affected items', exact: true })).toHaveCount(0);
    await expect(
      card.getByRole('region', { name: 'Before and after' }).locator('.review-document-changes > li'),
    ).toHaveCount(1);
    await expect(card.getByText(globalPreference, { exact: true })).toHaveCount(1);
    await expect(card.getByText(scopedPreference, { exact: true })).toHaveCount(1);
    await expect(card.getByText(newPreference, { exact: true })).toHaveCount(1);
    await expect(card.getByText(/older cop(?:y|ies)/i)).toHaveCount(0);
  });

  test('renders proposed tasks as a workspace tree with estimates and schedule ownership', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({ id: 80, name: 'Home', accentColor: '#1f6feb', iconSlug: 'home', sortOrder: 0 });
    const task = nodeFactory.build({ id: 8001, categoryId: category.id, title: 'Existing home task', sortOrder: 0 });
    const review = documentWith([action('approve', 'task-tree')]);
    const proposedEntity = (entityId: string, label: string) => ({
      ...version,
      ref: {
        contractVersion: '2.0',
        kind: 'temporary',
        entityId,
        revision: null,
        title: label,
        targetKind: 'task',
      },
      label,
      context: 'New',
    });
    review.summary = 'Review the proposed task structure and schedule in Home before applying it.';
    review.blocks = [{
      ...version,
      kind: 'task_structure',
      blockId: 'block:task-structure',
      title: 'Proposed task structure',
      workspaces: [{
        ...version,
        workspace: {
          ...version,
          ref: {
            contractVersion: '2.0',
            kind: 'workspace',
            entityId: 'workspace:home',
            revision: null,
            title: 'Home',
            targetKind: null,
          },
          label: 'Home',
          context: null,
        },
        roots: [{
          ...version,
          task: proposedEntity('temp:workflow', 'Set up hard drive casing and repartition computer'),
          state: 'new',
          estimateMinutes: 180,
          scheduledBlocks: [],
          children: [
            {
              ...version,
              task: proposedEntity('temp:pickup', 'Pick up hard drive casing from concierge'),
              state: 'new',
              estimateMinutes: 30,
              scheduledBlocks: [{
                ...version,
                rangeLabel: 'July 24, 2026, 9:10 AM–9:40 AM',
                durationMinutes: 30,
              }],
              children: [],
            },
            {
              ...version,
              task: proposedEntity('temp:install', 'Install hard drive casing'),
              state: 'new',
              estimateMinutes: 60,
              scheduledBlocks: [],
              children: [],
            },
            {
              ...version,
              task: proposedEntity('temp:repartition', 'Repartition computer'),
              state: 'new',
              estimateMinutes: 90,
              scheduledBlocks: [],
              children: [],
            },
          ],
        }],
      }],
    }];
    await mockStrategistApi({ runResponses: [runResponse(review, 1)] });
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    await page.getByLabel('AI workspace prompt').fill('Review the hard drive workflow');
    await page.getByRole('button', { name: /send message/i }).click();

    const structure = page.getByRole('region', { name: 'Proposed task structure' });
    await expect(structure.getByText('Home', { exact: true })).toBeVisible();
    await expect(structure.getByText('3h estimate', { exact: true })).toBeVisible();
    await expect(structure.getByText('30m scheduled', { exact: true })).toBeVisible();
    await expect(structure.getByText('July 24, 2026, 9:10 AM–9:40 AM', { exact: true })).toBeVisible();
    await expect(structure.locator('.review-document-task-tree > li > ul > li')).toHaveCount(3);
    await expect(page.getByRole('heading', { name: 'Affected items' })).toHaveCount(0);
  });

  test('renders one decomposable Belcarra tree, displacement, and partial schedule review', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({
      id: 81,
      name: 'Leisure',
      accentColor: '#00a47a',
      iconSlug: 'coffee',
      sortOrder: 0,
    });
    const task = nodeFactory.build({
      id: 8101,
      categoryId: category.id,
      title: 'Existing leisure task',
      sortOrder: 0,
    });
    const review = documentWith([action('approve', 'belcarra')]);
    const proposedEntity = (entityId: string, label: string) => ({
      ...version,
      ref: {
        contractVersion: '2.0',
        kind: 'temporary' as const,
        entityId,
        revision: null,
        title: label,
        targetKind: 'task',
      },
      label,
      context: 'New',
    });
    review.summary = 'Review the Belcarra hike plan and calendar adjustment before applying them.';
    review.blocks = [
      {
        ...version,
        kind: 'task_structure',
        blockId: 'block:belcarra-tree',
        title: 'Proposed task structure',
        workspaces: [{
          ...version,
          workspace: {
            ...version,
            ref: {
              contractVersion: '2.0',
              kind: 'workspace',
              entityId: 'workspace:leisure',
              revision: null,
              title: 'Leisure',
              targetKind: null,
            },
            label: 'Leisure',
            context: null,
          },
          roots: [{
            ...version,
            task: proposedEntity('temp:belcarra', 'Go hiking at Belcarra trail'),
            state: 'new',
            scheduledBlocks: [],
            children: [
              {
                ...version,
                task: proposedEntity('temp:parking', 'Check Belcarra parking availability'),
                state: 'new',
                estimateMinutes: 10,
                scheduledBlocks: [{
                  ...version,
                  rangeLabel: 'August 15, 2026, 7:00 AM–7:10 AM',
                  durationMinutes: 10,
                }],
                children: [],
              },
              {
                ...version,
                task: proposedEntity('temp:hike', 'Hike Belcarra trail'),
                state: 'new',
                deadlineAt: '2026-08-16T23:59:59-07:00',
                scheduledBlocks: [],
                children: [],
              },
            ],
          }],
        }],
      },
      {
        ...version,
        kind: 'grouped_changes',
        blockId: 'block:belcarra-actions',
        title: 'What will change',
        groups: [{
          ...version,
          groupId: 'group:belcarra',
          summary: 'Task tree and calendar changes',
          changes: [
            { ...change, changeId: 'create-parent', summary: 'Create hiking plan', before: 'Not present', after: 'Go hiking at Belcarra trail' },
            { ...change, changeId: 'create-parking', summary: 'Create parking check', before: 'Not present', after: '10-minute child task' },
            { ...change, changeId: 'create-hike', summary: 'Create hike task', before: 'Not present', after: 'Due August 16; not scheduled' },
            { ...change, changeId: 'move-existing', summary: 'Move scheduled time', before: 'August 15, 7:00 AM–8:00 AM', after: 'August 15, 7:10 AM–8:10 AM' },
            { ...change, changeId: 'schedule-parking', summary: 'Add time to the calendar', before: 'Not present', after: 'August 15, 7:00 AM–7:10 AM' },
          ],
        }],
      },
    ];
    await mockStrategistApi({ runResponses: [runResponse(review, 1)] });
    const workspace = await mockWorkspace({
      categories: [category],
      nodesByCategoryId: { [category.id]: [task] },
    });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    await page.getByLabel('AI workspace prompt').fill('Review the Belcarra hiking plan');
    await page.getByRole('button', { name: /send message/i }).click();

    const structure = page.getByRole('region', { name: 'Proposed task structure' });
    await expect(structure.getByText('Leisure', { exact: true })).toBeVisible();
    await expect(structure.getByText('10m estimate', { exact: true })).toBeVisible();
    await expect(structure.getByText('10m scheduled', { exact: true })).toBeVisible();
    await expect(structure.getByText('August 15, 2026, 7:00 AM–7:10 AM', { exact: true })).toBeVisible();
    await expect(structure.getByText('Due Aug 16, 2026', { exact: true })).toBeVisible();
    await expect(structure.getByText('Not scheduled', { exact: true })).toBeVisible();
    await expect(structure.locator('.review-document-task-tree > li > ul > li')).toHaveCount(2);
    await expect(
      page.getByRole('region', { name: 'What will change' }).locator('.review-document-changes > li'),
    ).toHaveCount(5);
    await expect(page.getByText('Move scheduled time', { exact: true })).toBeVisible();
    await expect(page.getByText('Add time to the calendar', { exact: true })).toBeVisible();
  });

  test('renders every block and keeps verified local state separate from failed sync', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.setViewportSize({ width: 640, height: 900 });
    const category = categoryFactory.build({ id: 74, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 7401, categoryId: category.id, title: 'Prepare interview', sortOrder: 0 });
    const approve = action('approve');
    const strategist = await mockStrategistApi({ runResponses: [runResponse(documentWith([approve], { allBlocks: true }), 1)] });
    const outcomes: StrategistReviewActionOutcome[] = [{
      status: 'ok',
      result: {
        status: 'execution_complete',
        reviewId: approve.reviewId,
        reviewRevision: 1,
        sessionId: binding.sessionId,
        goalId: binding.goalId,
        changeSetId: binding.changeSetId,
        summary: 'The reviewed changes reached the verified execution boundary.',
        goalRevision: 5,
        changeSetRevision: 3,
        execution: {
          status: 'ok',
          result: {
            executionId: 'execution:one', commandId: 'command:approve', idempotencyKey: 'idem:approve',
            goalRevision: 5, changeSetRevision: 3, status: 'applied',
            verification: { status: 'verified' },
            syncItems: [{ outboxId: 'outbox:one', provider: 'google', status: 'failed', attempts: 3, errorSummary: 'Calendar delivery needs another attempt.', recovery: 'retry' }],
          },
        },
      },
    }];
    const captured = await registerReviewActions(page, outcomes, 100);
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    await page.getByLabel('AI workspace prompt').fill('Review my interview plan');
    await page.getByRole('button', { name: /send message/i }).click();

    await page.getByText('Included tasks', { exact: true }).click();
    for (const heading of ['Affected items', 'What will change', 'Before and after', 'Schedule', 'Repeating schedule', 'Conflict resolution', 'Assumptions', 'What to know', 'Plan changes', 'Current status']) {
      await expect(page.getByRole('heading', { name: heading, exact: true })).toBeVisible();
    }
    await expect(page.locator('.floating-chat').getByText('Prepare interview', { exact: true }).first()).toBeVisible();
    await expect(page.locator('.floating-chat [data-task-id="7401"]').first()).toBeVisible();
    await expect(page.getByText('This is the durable result of the original request.')).toBeVisible();
    await expect(page.getByText('Inspect the local changes before continuing.')).toBeVisible();
    await expect(page.getByText('Time zone: America/Vancouver')).toHaveCount(2);
    await expect(page.getByText('Delivery attempts: 3').first()).toBeVisible();
    await expect(page.getByText(/TaskPatch|operation:update-interview|task:7401/)).toHaveCount(0);
    expect(strategist.capturedRunRequests).toHaveLength(1);

    const reviewCard = page.locator('.review-document-card').first();
    const typeScale = await reviewCard.evaluate((node) => {
      const view = node.ownerDocument.defaultView!;
      const size = (selector?: string) => Number.parseFloat(
        view.getComputedStyle(selector ? node.querySelector(selector)! : node).fontSize,
      );
      return {
        body: size(),
        title: size('h4'),
        section: size('h5'),
        schedule: size('.review-document-summary-grid > span'),
        taskReference: size('.ai-task-reference'),
        action: size('.review-document-primary-action'),
      };
    });
    expect(typeScale.body).toBeLessThanOrEqual(13);
    expect(typeScale.title - typeScale.body).toBeLessThanOrEqual(1.5);
    for (const size of [typeScale.section, typeScale.schedule, typeScale.taskReference, typeScale.action]) {
      expect(Math.abs(size - typeScale.body)).toBeLessThanOrEqual(0.5);
    }

    await page.route(/\/api\/v1\/nodes\?.*categoryId=74/, async (route) => {
      await route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'internal refresh detail' }),
      });
    });

    const approveButton = page.getByRole('button', { name: 'Apply changes' });
    await approveButton.focus();
    await expect(approveButton).toBeFocused();
    expect((await approveButton.boundingBox())?.height).toBeGreaterThanOrEqual(40);
    const reducedTransition = await approveButton.evaluate(
      (node) => Number.parseFloat(
        node.ownerDocument.defaultView?.getComputedStyle(node).transitionDuration ?? '0',
      ),
    );
    expect(reducedTransition).toBeLessThanOrEqual(0.00001);
    await page.keyboard.press('Enter');
    await expect(approveButton).toBeDisabled();

    const executionStatus = page.getByRole('status').filter({ hasText: 'Local changes verified.' });
    await expect(executionStatus).toContainText('Google Calendar');
    await expect(executionStatus).toContainText('Calendar delivery needs another attempt.');
    await expect(executionStatus).toBeFocused();
    await expect(page.getByText(/Local changes are confirmed, but the latest workspace view/)).toBeVisible();
    await expect(page.getByText('internal refresh detail')).toHaveCount(0);
    expect(await reviewCard.evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true);
    expect(captured).toEqual([approve]);
  });

  test('submits edit, reject, decision, and replay controls as exact typed bodies', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({ id: 75, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 7501, categoryId: category.id, title: 'Prepare interview', sortOrder: 0 });
    const actions = [
      action('edit'),
      action('reject'),
      action('decision'),
      action('retry'),
      action('approve', 'stale'),
    ];
    await mockStrategistApi({
      runResponses: actions.map((item, index) => runResponse(documentWith([item], { decision: item.action === 'decision' }), index + 1)),
    });
    const labels = ['Revise', 'Reject', 'Which time should be used?: Tuesday morning', 'Retry', 'Apply changes'];
    const statuses: Array<NonNullable<StrategistReviewActionOutcome['result']>['status']> = ['edit_requested', 'rejected', 'decision_recorded', 'retry_replayed'];
    const outcomes: StrategistReviewActionOutcome[] = statuses.map((status, index) => ({
      status: 'ok',
      result: {
        status,
        reviewId: actions[index].reviewId,
        reviewRevision: 1,
        sessionId: binding.sessionId,
        goalId: binding.goalId,
        changeSetId: binding.changeSetId,
        summary: `${labels[index]} was recorded safely.`,
        goalRevision: 5,
        changeSetRevision: 3,
        correction: status === 'edit_requested' ? 'Move it to Wednesday morning.' : null,
        execution: status === 'retry_replayed' ? {
          status: 'ok',
          result: {
            executionId: 'execution:original', commandId: 'command:original', idempotencyKey: 'idem:original',
            goalRevision: 5, changeSetRevision: 3, status: 'applied', verification: { status: 'verified' }, syncItems: [],
          },
        } : null,
      },
    }));
    outcomes.push({
      status: 'error',
      error: {
        code: 'stale_review',
        summary: 'This review is out of date. Refresh it before continuing.',
        recovery: 'refresh',
      },
    });
    const captured = await registerReviewActions(page, outcomes);
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();

    for (let index = 0; index < actions.length; index += 1) {
      await page.getByLabel('AI workspace prompt').fill(`Review action ${index + 1}`);
      await page.getByRole('button', { name: /send message/i }).click();
      await page.getByRole('button', { name: labels[index], exact: true }).last().click();
      if (actions[index].action === 'edit') {
        await page.getByLabel('What should change?').fill('Move it to Wednesday morning.');
        await page.getByRole('button', { name: 'Submit edit' }).click();
      }
      await expect.poll(() => captured.length).toBe(index + 1);
    }
    expect(captured).toEqual(actions.map((item) => item.action === 'edit'
      ? { ...item, correction: 'Move it to Wednesday morning.' }
      : item));
    await expect(page.getByRole('alert')).toContainText('This review is out of date.');
    await expect(page.getByRole('button', { name: 'Refresh review' })).toBeVisible();
    expect(captured[2]).toMatchObject({ action: 'decision', decisionId: 'decision:time', optionId: 'option:morning', eventId: 'event:decision', binding: { goalRevision: 4 } });
    expect(captured[3]).toMatchObject({ action: 'retry', originalCommandId: 'command:original', originalIdempotencyKey: 'idem:original' });
  });

  test('degrades malformed review payloads without crashing the workspace', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({ id: 76, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 7601, categoryId: category.id, title: 'Prepare interview', sortOrder: 0 });
    const malformed = { ...documentWith([action('approve')]) } as unknown as Record<string, unknown>;
    delete malformed.controls;
    await mockStrategistApi({
      runResponses: [runResponse(malformed as unknown as StrategistReviewDocument, 1)],
    });
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    await page.getByLabel('AI workspace prompt').fill('Show the malformed review safely');
    await page.getByRole('button', { name: /send message/i }).click();

    await expect(page.getByText('A review is ready.')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Proposed changes' })).toHaveCount(0);
    await expect(page.getByLabel('AI workspace prompt')).toBeVisible();
  });

  test('keeps nested execution failures recoverable and hides raw HTTP detail', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({ id: 77, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 7701, categoryId: category.id, title: 'Prepare interview', sortOrder: 0 });
    const approve = action('approve');
    await mockStrategistApi({ runResponses: [runResponse(documentWith([approve]), 1)] });
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    await page.getByLabel('AI workspace prompt').fill('Review execution failure');
    await page.getByRole('button', { name: /send message/i }).click();
    await page.route('**/api/v1/ai/strategist/sessions/session-review/review-actions', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          result: {
            status: 'execution_complete', reviewId: approve.reviewId, reviewRevision: 1,
            sessionId: binding.sessionId, goalId: binding.goalId, changeSetId: binding.changeSetId,
            summary: 'Execution was checked.', goalRevision: 4, changeSetRevision: 3,
            execution: {
              status: 'error',
              error: { code: 'stale_world', userSummary: 'The local plan changed before it could be applied.', recovery: 'retry' },
            },
          },
        }),
      });
    });
    await page.getByRole('button', { name: 'Apply changes' }).click();
    await expect(page.getByRole('alert')).toContainText('The local plan changed before it could be applied.');
    await expect(page.getByRole('button', { name: 'Apply changes' })).toBeEnabled();

    await page.unroute('**/api/v1/ai/strategist/sessions/session-review/review-actions');
    await page.route('**/api/v1/ai/strategist/sessions/session-review/review-actions', async (route) => {
      await route.fulfill({
        status: 422,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'internal_validator_field_name' }),
      });
    });
    await page.getByRole('button', { name: 'Apply changes' }).click();
    await expect(page.getByRole('alert')).toContainText('Drasil could not complete this review action safely.');
    await expect(page.getByText('internal_validator_field_name')).toHaveCount(0);
  });

  test('serializes controls across turns and keeps decision continuation keyboard reachable', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({ id: 78, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 7801, categoryId: category.id, title: 'Prepare interview', sortOrder: 0 });
    const firstDecision = action('decision', 'first');
    const secondDecision: StrategistReviewAction = {
      ...firstDecision,
      action: 'decision',
      eventId: 'event:second',
      idempotencyKey: 'idem:second',
      decisionId: 'decision:day',
      optionId: 'option:wednesday',
    };
    const decisionDocument = documentWith([firstDecision, secondDecision], { decision: true });
    decisionDocument.decisions.push({
      decisionId: 'decision:day',
      question: 'Which day should be used?',
      reason: 'Two safe days are available.',
      options: [{ optionId: 'option:wednesday', label: 'Wednesday' }],
    });
    const rejectOne = action('reject', 'lock-one');
    const rejectTwo = { ...rejectOne, eventId: 'event:lock-two', idempotencyKey: 'idem:lock-two' };
    await mockStrategistApi({
      runResponses: [
        runResponse(documentWith([rejectOne]), 1),
        runResponse(documentWith([rejectTwo]), 2),
        runResponse(decisionDocument, 3),
      ],
    });
    const outcomes: StrategistReviewActionOutcome[] = [{
      status: 'ok',
      result: {
        status: 'rejected', reviewId: rejectTwo.reviewId, reviewRevision: 1,
        sessionId: binding.sessionId, goalId: binding.goalId, changeSetId: binding.changeSetId,
        summary: 'Rejected safely.', goalRevision: 5, changeSetRevision: 3,
      },
    }, {
      status: 'ok',
      result: {
        status: 'decision_recorded', reviewId: firstDecision.reviewId, reviewRevision: 1,
        sessionId: binding.sessionId, goalId: binding.goalId, changeSetId: binding.changeSetId,
        summary: 'The first choice was saved.', goalRevision: 5, changeSetRevision: 3,
      },
    }];
    const captured = await registerReviewActions(page, outcomes, 200);
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    for (const prompt of ['First review', 'Second review']) {
      await page.getByLabel('AI workspace prompt').fill(prompt);
      await page.getByRole('button', { name: /send message/i }).click();
    }
    const rejectButtons = page.getByRole('button', { name: 'Reject' });
    await rejectButtons.last().click();
    await expect(rejectButtons.first()).toBeDisabled();
    expect(captured).toHaveLength(1);
    await expect(page.getByText('Rejected safely.')).toBeVisible();

    await page.getByLabel('AI workspace prompt').fill('Decision review');
    await page.getByRole('button', { name: /send message/i }).click();
    const firstOption = page.getByRole('button', { name: 'Which time should be used?: Tuesday morning' });
    await firstOption.focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('button', { name: 'Continue review in the prompt' })).toBeVisible();
    await page.getByRole('button', { name: 'Continue review in the prompt' }).click();
    await expect(page.getByLabel('AI workspace prompt')).toBeFocused();

    const labelledIds = await page.locator('.review-document-card').evaluateAll((cards) =>
      cards.map((card) => card.getAttribute('aria-labelledby')),
    );
    expect(new Set(labelledIds).size).toBe(labelledIds.length);
  });

  test('fails closed for a retired legacy recurrence proposal', async ({
    page, categoryFactory, nodeFactory, mockWorkspace, mockStrategistApi, openStrategist,
  }) => {
    const category = categoryFactory.build({ id: 79, name: 'Work', accentColor: '#1f6feb', iconSlug: 'briefcase', sortOrder: 0 });
    const task = nodeFactory.build({ id: 7901, categoryId: category.id, title: 'Monthly review', sortOrder: 0 });
    const proposal = {
      proposalId: 'proposal:monthly-review',
      proposalKind: 'branch',
      operation: 'create_and_schedule',
      legacyResponseKey: 'branchProposal',
      title: 'Schedule Monthly review',
      body: 'Schedule the review while keeping existing work visible.',
      targetNodeIds: [task.id],
      preview: [],
      validationWarnings: [],
      taskPatchStatus: 'valid',
      taskPatch: { operations: [] },
      taskPatchValidation: { status: 'valid', errors: [], warnings: [], operation_count: 1 },
      taskPatchSimulation: { status: 'valid', errors: [], warnings: [], operation_count: 1 },
      payload: {
        recurring_schedule_plan: {
          frequency: 'monthly',
          category_name: 'Work',
          duration_minutes: 60,
          start_date: '2026-07-20',
          end_date: '2026-09-20',
          occurrences: [{
            occurrence_id: 'occurrence:first', start_at: '2026-07-20T09:00:00', end_at: '2026-07-20T10:00:00', shifted: false,
          }, {
            occurrence_id: 'occurrence:shifted', start_at: '2026-08-20T10:00:00', end_at: '2026-08-20T11:00:00', shifted: true,
          }],
          conflict_resolutions: [
            { date: '2026-07-20', taskId: 'blocker:one' },
            { date: '2026-09-20', taskId: 'blocker:one' },
            { date: '2026-07-20', taskId: 'blocker:two' },
          ].map(({ date, taskId }) => ({
            date,
            blockers: [{
              taskId,
              taskTitle: 'Planning block',
              startAt: `${date}T09:00:00`,
              endAt: `${date}T10:00:00`,
              movedStartAt: `${date}T11:00:00`,
              movedEndAt: `${date}T12:00:00`,
            }],
          })),
        },
      },
    } as unknown as StrategistStandardProposal;
    await mockStrategistApi({ runResponses: [legacyRunResponse(proposal, 1)] });
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [task] } });
    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    await openStrategist();
    await page.getByLabel('AI workspace prompt').fill('Review the recurring schedule');
    await page.getByRole('button', { name: /send message/i }).click();

    await expect(page.getByText('This proposal has expired')).toBeVisible();
    await expect(
      page.getByText(
        'Ask the AI workspace to regenerate this request as a current canonical review before making any changes.',
      ),
    ).toBeVisible();
    await expect(page.getByLabel('Proposed recurring schedule')).toHaveCount(0);
    await expect(page.getByRole('button', { name: /approve/i })).toHaveCount(0);
  });
});
