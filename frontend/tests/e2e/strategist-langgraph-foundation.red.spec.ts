import { test, expect } from '../support/fixtures/strategist.fixture';

test.describe('Story 5.2 - Strategist LangGraph foundation and hard-ID proposal state', () => {
  test(
    'sends clarification choices as structured uiEvent payloads with hard node ids',
    async ({
      page,
      categoryFactory,
      nodeFactory,
      mockWorkspace,
      mockStrategistApi,
      openStrategist,
    }) => {
      const category = categoryFactory.build({
        id: 12,
        name: 'Resume polish',
        accentColor: '#1f6feb',
        iconSlug: 'briefcase',
        sortOrder: 0,
      });
      const rootTask = nodeFactory.build({
        id: 401,
        categoryId: category.id,
        title: 'Resume polish',
        sortOrder: 0,
      });
      const clarificationTarget = nodeFactory.build({
        id: 402,
        categoryId: category.id,
        parentId: rootTask.id,
        title: 'Revise the highest-impact parts of Resume',
        sortOrder: 0,
      });

      const strategistHarness = await mockStrategistApi({
        runResponses: [
          {
            session: {
              id: 'session-clarify-1',
              title: 'Resume polish strategist chat',
              providerLabel: 'ChatGPT login',
              modelName: 'gpt-5.4',
              createdAt: '2026-05-09T07:00:00.000Z',
              updatedAt: '2026-05-09T07:00:00.000Z',
            },
            turn: {
              id: 'turn-clarify-1',
              prompt: 'mark the highest impact part of the resume polish task as done',
              createdAt: '2026-05-09T07:00:00.000Z',
              response: {
                providerLabel: 'ChatGPT login',
                mode: 'review',
                summary: 'I want to make sure I use the right task context before I continue.',
                contextCards: [],
                suggestedPrompts: [],
                clarification: {
                  message: 'I found more than one likely task. Choose the exact subtask.',
                  entityChoices: [
                    {
                      kind: 'entity',
                      label: clarificationTarget.title,
                      value: 'clarification-node-402',
                      description: 'Resume polish | medium priority | no deadline',
                      targetNodeId: clarificationTarget.id,
                      followUpPrompt: `Use ${clarificationTarget.title}`,
                    },
                  ],
                  fieldChoices: [],
                  scopedFollowUpPrompts: [],
                },
              },
            },
          },
          {
            session: {
              id: 'session-clarify-1',
              title: 'Resume polish strategist chat',
              providerLabel: 'ChatGPT login',
              modelName: 'gpt-5.4',
              createdAt: '2026-05-09T07:00:00.000Z',
              updatedAt: '2026-05-09T07:00:05.000Z',
            },
            turn: {
              id: 'turn-clarify-2',
              prompt: clarificationTarget.title,
              createdAt: '2026-05-09T07:00:05.000Z',
              response: {
                providerLabel: 'ChatGPT login',
                mode: 'metadata',
                summary: 'I prepared a metadata update for the requested subtask.',
                contextCards: [],
                suggestedPrompts: [],
              },
            },
          },
        ],
      });
      const workspace = await mockWorkspace({
        categories: [category],
        nodesByCategoryId: {
          [category.id]: [rootTask, clarificationTarget],
        },
      });

      const boot = workspace.waitForBoot(category.id);
      await page.goto('/');
      await boot;
      await openStrategist();

      await page.getByLabel('AI workspace prompt').fill(
        'mark the highest impact part of the resume polish task as done',
      );
      await page.getByRole('button', { name: /send message/i }).click();
      const clarificationButton = page
        .locator('.strategist-clarification-choice')
        .filter({ hasText: clarificationTarget.title });
      await expect(clarificationButton).toHaveCount(1);
      await expect(clarificationButton).toBeVisible();

      await clarificationButton.click();

      expect(strategistHarness.capturedRunRequests).toHaveLength(2);
      expect(strategistHarness.capturedRunRequests[1]).toMatchObject({
        sessionId: 'session-clarify-1',
        uiEvent: {
          kind: 'clarification_choice',
          sourceTurnId: 'turn-clarify-1',
          targetNodeId: clarificationTarget.id,
          payload: {
            choiceId: 'clarification-node-402',
          },
        },
      });
      expect(strategistHarness.capturedRunRequests[1].prompt).not.toBe(
        `Use ${clarificationTarget.title}`,
      );
    },
  );

  test.skip(
    'resumes proposal approvals by proposalId instead of mutating tasks directly from the card',
    async ({
      page,
      categoryFactory,
      nodeFactory,
      mockWorkspace,
      mockStrategistApi,
      openStrategist,
    }) => {
      const category = categoryFactory.build({
        id: 21,
        name: 'Resume polish',
        accentColor: '#1f6feb',
        iconSlug: 'briefcase',
        sortOrder: 0,
      });
      const rootTask = nodeFactory.build({
        id: 801,
        categoryId: category.id,
        title: 'Resume polish',
        sortOrder: 0,
      });

      const strategistHarness = await mockStrategistApi({
        runResponses: [
          {
            session: {
              id: 'session-branch-1',
              title: 'Resume polish strategist chat',
              providerLabel: 'ChatGPT login',
              modelName: 'gpt-5.4',
              createdAt: '2026-05-09T08:00:00.000Z',
              updatedAt: '2026-05-09T08:00:00.000Z',
            },
            turn: {
              id: 'turn-branch-1',
              prompt: 'Break down Resume polish into concrete subtasks',
              createdAt: '2026-05-09T08:00:00.000Z',
              response: {
                providerLabel: 'ChatGPT login',
                mode: 'branch',
                summary: 'I prepared a branch proposal for Resume polish.',
                contextCards: [],
                suggestedPrompts: [],
                proposal: {
                  proposalId: 'proposal-branch-1',
                  proposalKind: 'branch',
                  operation: 'branch',
                  legacyResponseKey: 'branchProposal',
                  title: 'Review task breakdown for Resume polish',
                  body: 'Review the proposed subtasks below before applying.',
                  summary: 'I prepared a branch proposal for Resume polish.',
                  rationale: 'Review the proposed subtasks below before applying.',
                  targetNodeId: rootTask.id,
                  targetNodeIds: [rootTask.id],
                  targetNodeTitle: rootTask.title,
                  preview: [
                    'Revise the highest-impact parts of Resume',
                    'Update job history bullets',
                  ],
                  validationWarnings: [],
                  taskPatchStatus: 'valid',
                  taskPatch: {
                    patch_id: 'proposal-branch-1',
                    operations: [
                      {
                        op: 'create',
                        parent_id: rootTask.id,
                        category_id: category.id,
                        title: 'Revise the highest-impact parts of Resume',
                      },
                      {
                        op: 'create',
                        parent_id: rootTask.id,
                        category_id: category.id,
                        title: 'Update job history bullets',
                      },
                    ],
                  },
                  payload: {
                    proposalId: 'proposal-branch-1',
                    proposalKind: 'branch',
                    targetNodeId: rootTask.id,
                    targetCategoryId: category.id,
                    targetNodeTitle: rootTask.title,
                    rationale: 'Review the proposed subtasks below before applying.',
                    proposedChildren: [
                      'Revise the highest-impact parts of Resume',
                      'Update job history bullets',
                    ],
                  },
                },
              },
            },
          },
          {
            session: {
              id: 'session-branch-1',
              title: 'Resume polish strategist chat',
              providerLabel: 'ChatGPT login',
              modelName: 'gpt-5.4',
              createdAt: '2026-05-09T08:00:00.000Z',
              updatedAt: '2026-05-09T08:00:05.000Z',
            },
            turn: {
              id: 'turn-branch-2',
              prompt: 'Approve branch proposal',
              createdAt: '2026-05-09T08:00:05.000Z',
              response: {
                providerLabel: 'ChatGPT login',
                mode: 'review',
                summary: 'Applied the branch proposal by proposal id.',
                contextCards: [],
                suggestedPrompts: [],
              },
            },
          },
        ],
      });
      const workspace = await mockWorkspace({
        categories: [category],
        nodesByCategoryId: {
          [category.id]: [rootTask],
        },
      });

      const boot = workspace.waitForBoot(category.id);
      await page.goto('/');
      await boot;
      await openStrategist();

      await page.getByLabel('AI workspace prompt').fill(
        'Break down Resume polish into concrete subtasks',
      );
      await page.getByRole('button', { name: /send message/i }).click();
      const proposalTitle = page.locator('.strategist-action-title');
      await expect(proposalTitle).toContainText('Review task breakdown for');
      await expect(proposalTitle.locator('[data-task-id="801"]')).toContainText('Resume polish');

      await page.getByRole('button', { name: /^Approve$/ }).click();

      expect(strategistHarness.capturedRunRequests).toHaveLength(2);
      expect(strategistHarness.capturedRunRequests[1]).toMatchObject({
        sessionId: 'session-branch-1',
        uiEvent: {
          kind: 'proposal_action',
          sourceTurnId: 'turn-branch-1',
          proposalId: 'proposal-branch-1',
          action: 'approve',
          targetNodeId: rootTask.id,
        },
      });
      expect(strategistHarness.capturedContextRequests).toHaveLength(0);
    },
  );
});
