import { test, expect } from '../support/fixtures/strategist.fixture';

const snapshot = {
  generatedAt: '2026-07-21T22:00:00Z',
  definition: { toolContractBoundary: 'Typed host handoff.', authorityRule: 'Host preserves scope.' },
  pipeline: [],
  agents: [{ id: 'strategist', version: '1', title: 'Strategist', summary: 'Owns intent.', duties: ['Select tools'], boundaries: ['Cannot bypass approval'], available_capabilities: ['scheduling'] }],
  skills: [{ id: 'scheduling', version: '1', title: 'Scheduling', summary: 'Scheduling guidance.', available_to: ['strategist'], use_for: ['schedule work'], compatible_output_modes: ['proposal'], notFor: [], recommendedTools: ['handoff_to_planner'], dependencies: [], evidenceRequirements: {} }],
  tools: [{
    name: 'handoff_to_planner', description: 'Send a typed scheduling request to Planner.', contractVersion: '2.0', readOnly: true, sideEffect: 'none', approvalRequirement: 'none', idempotencyBehavior: 'read_only', resultKind: 'planner_handoff',
    externalSurfaces: [],
    inputFields: [{ name: 'target_scope', required: false, description: 'Series or occurrence.', schema: { enum: ['series', 'occurrence'] } }],
    outputSchema: { type: 'object' }, errorCodes: ['TOOL_INVARIANT_FAILED'], semanticValidator: '_validate_planner_handoff_semantics',
  }],
  connections: [{ id: 'codex', label: 'Codex app-server', protocol: 'JSON-RPC over stdio', kind: 'provider_transport', enabled: true, description: 'Cached OAuth transport.' }],
  mcpServers: [], mcpBoundaryNote: 'No Drasil-owned MCP server.',
  executionMigration: {
    mcpReadAuthority: 'native',
    mcpReadShadowEnabled: true,
    mcpReadPromotionEligible: false,
    writeAuthority: 'current_agent_kernel_and_compatibility_proposals',
    retirementAllowed: false,
  },
  mcpReadPromotion: {
    requested: true,
    eligible: false,
    reasonCode: 'shadow_evidence_insufficient',
    requiredBuildId: 'build-admin-test',
    requiredTools: ['search_entities'],
    minMatchesPerTool: 25,
    maxEvidenceAgeDays: 14,
    evidence: [{
      toolName: 'search_entities',
      sampleCount: 12,
      matchCount: 12,
      mismatchCount: 0,
      candidateFailureCount: 0,
      candidateErrorCount: 0,
      invalidEventCount: 0,
      blockingCount: 0,
      latestAt: '2026-07-21T22:00:03Z',
    }],
    blockers: ['search_entities:insufficient_matches:12/25'],
    changesAuthority: false,
  },
  provider: { activeProvider: 'codex-cli', model: 'gpt-5.6-terra', reasoningEffort: 'high', transport: 'codex_app_server' },
  rollout: { nativeReviewRequired: false, shadowSampling: false, shadowModelTools: false, families: [{ family: 'recurrence', flag: 'AGENT_KERNEL_V2_RECURRENCE_WRITES', enabled: true, operationKinds: ['recurrence.update_series'], evidenceCount: 4 }] },
  apiRoutes: [{ path: '/api/v1/ai/strategist', methods: ['POST'], name: 'run', tags: ['AI'] }],
  database: { engine: 'SQLite', tables: [{ name: 'strategist_agent_events', rowCount: 12, columns: [{ name: 'event_type', type: 'VARCHAR', nullable: false, primaryKey: false }], foreignKeys: [{ columns: ['session_id'], targetTable: 'strategist_chat_sessions', targetColumns: ['id'] }] }] },
};

const trace = {
  session: { id: 'session-live', title: 'Move Team Call', provider: 'ChatGPT login', model: 'gpt-5.6-terra', createdAt: '2026-07-21T22:00:00Z', updatedAt: '2026-07-21T22:00:03Z' },
  turns: [{ id: 'turn-live', prompt: 'move every occurrence to 7 pm', createdAt: '2026-07-21T22:00:00Z', error: null, response: { mode: 'proposal', summary: 'Review the recurring change.', proposalKind: 'review_document', hasClarification: false, handoffOutcome: null } }],
  runs: [{ id: 'run-live', turnId: 'turn-live', status: 'completed', controlPlane: 'agent-kernel', goalId: 'goal-1', goalRevision: 3, correlationId: 'correlation-1', prompt: 'move every occurrence to 7 pm', error: null, request: { timeZone: 'America/Vancouver' }, createdAt: '2026-07-21T22:00:00Z', startedAt: '2026-07-21T22:00:00Z', completedAt: '2026-07-21T22:00:03Z' }],
  events: [
    { id: 'event-1', sequence: 1, eventType: 'user_turn', source: 'agent_kernel', turnId: 'turn-live', runId: 'run-live', goalId: 'goal-1', goalRevision: 1, correlationId: 'correlation-1', createdAt: '2026-07-21T22:00:00Z', details: { captured: { prompt: 'move every occurrence to 7 pm' } } },
    { id: 'event-2', sequence: 2, eventType: 'context_packet', source: 'agent_kernel', turnId: 'turn-live', runId: 'run-live', goalId: 'goal-1', goalRevision: 1, correlationId: 'correlation-1', createdAt: '2026-07-21T22:00:01Z', details: { captured: { active_request_continuation: { request_prompts: ['move every occurrence to 7 pm'] }, goal_state: { objective: 'Move the Team Call series' } } } },
    { id: 'event-3', sequence: 3, eventType: 'tool_observation', source: 'agent_kernel', turnId: 'turn-live', runId: 'run-live', goalId: 'goal-1', goalRevision: 2, correlationId: 'correlation-1', createdAt: '2026-07-21T22:00:02Z', details: { tool: 'handoff_to_planner', arguments: { target_scope: 'series', preferred_start_time: '19:00' }, result: { planner_mode: 'native_recurrence' }, captured: { name: 'handoff_to_planner', arguments: { target_scope: 'series', preferred_start_time: '19:00' }, result: { planner_mode: 'native_recurrence', proposal_ready: true } } } },
    { id: 'event-4', sequence: 4, eventType: 'final_answer', source: 'agent_kernel', turnId: 'turn-live', runId: 'run-live', goalId: 'goal-1', goalRevision: 3, correlationId: 'correlation-1', createdAt: '2026-07-21T22:00:03Z', details: { captured: { mode: 'proposal', summary: 'Review the recurring change.' } } },
  ],
};

test.describe('Agent architecture admin', () => {
  test('opens separately and replays a prompt through the live Strategist contract', async ({ page, categoryFactory, mockWorkspace }) => {
    const category = categoryFactory.build({ id: 91, name: 'Work' });
    const workspace = await mockWorkspace({ categories: [category], nodesByCategoryId: { [category.id]: [] } });
    const context = page.context();
    let promptCount = 0;

    await context.route('**/api/v1/admin/agent-architecture', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(snapshot) }));
    await context.route('**/api/v1/admin/agent-architecture/sessions?limit=30', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ sessions: [{ id: 'session-live', title: 'Move Team Call', provider: 'ChatGPT login', model: 'gpt-5.6-terra', updatedAt: '2026-07-21T22:00:03Z', turnCount: 1, eventCount: 4, latestPrompt: 'move every occurrence to 7 pm' }] }) }));
    await context.route('**/api/v1/admin/agent-architecture/sessions/session-live', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(trace) }));
    await context.route('**/api/v1/ai/strategist/sessions', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
    await context.route('**/api/v1/calendar-blocks?**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }));
    await context.route('**/api/v1/ai/strategist', async (route) => {
      promptCount += 1;
      expect(route.request().method()).toBe('POST');
      const body = route.request().postDataJSON();
      expect(body.prompt).toBe(promptCount === 1 ? 'move every occurrence to 7 pm' : 'and keep the same duration');
      expect(body.timeZone).toBeTruthy();
      expect(body.sessionId).toBe(promptCount === 1 ? null : 'session-live');
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
        session: { id: 'session-live', title: 'Move Team Call', providerLabel: 'ChatGPT login', modelName: 'gpt-5.6-terra', createdAt: '2026-07-21T22:00:00Z', updatedAt: '2026-07-21T22:00:03Z' },
        turn: { id: promptCount === 1 ? 'turn-live' : 'turn-live-2', prompt: body.prompt, createdAt: '2026-07-21T22:00:00Z', response: { providerLabel: 'ChatGPT login', mode: 'proposal', summary: promptCount === 1 ? 'Review the recurring change.' : 'I kept the one-hour duration.', contextCards: [], suggestedPrompts: [], diagnostics: {} } },
        run: { id: promptCount === 1 ? 'run-live' : 'run-live-2', sessionId: 'session-live', turnId: promptCount === 1 ? 'turn-live' : 'turn-live-2', status: 'completed', prompt: body.prompt, createdAt: '2026-07-21T22:00:00Z', updatedAt: '2026-07-21T22:00:03Z' },
      }) });
    });

    const boot = workspace.waitForBoot(category.id);
    await page.goto('/');
    await boot;
    const adminLink = page.getByRole('link', { name: /open agent architecture/i });
    await expect(adminLink).toHaveAttribute('target', '_blank');
    await expect(adminLink).toHaveAttribute('href', '/agent-architecture');
    await expect(page.getByTestId('sidebar-utilities').getByRole('link', {
      name: /open agent architecture/i,
    })).toBeVisible();

    await page.goto('/agent-architecture');
    await expect(page.getByRole('heading', { name: 'Agent architecture' })).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Conversation and live execution' })).toBeVisible();
    await expect(page.getByText('Agent Kernel runtime', { exact: true })).toBeVisible();
    await expect(page.getByText('Context system', { exact: true })).toBeVisible();
    await expect(page.getByText('Tool and Planner subsystem', { exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: /Context reconstruction/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Strategist model loop/ })).toBeVisible();
    await expect(page.getByRole('button', { name: /Context compaction/ })).toBeVisible();
    const contextEdge = page.locator('[data-edge-id="context-router"]');
    await expect(contextEdge.locator('.loop-edge-label')).toHaveText('context packet');
    await expect(contextEdge.locator('.loop-edge')).toHaveAttribute('marker-end', 'url(#arrow-normal)');
    await expect(contextEdge).toHaveAttribute('data-direction', 'context-to-router');
    await expect(page.locator('[data-edge-id="router-response"]')).toHaveCount(0);
    await expect(page.getByText('✓ live runtime only', { exact: true })).toBeVisible();
    const graphViewport = page.locator('.agent-loop-viewport');
    expect(await graphViewport.evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    const plannerHandoffBox = await page.getByRole('button', { name: /Planner handoff/ }).boundingBox();
    const readToolsBox = await page.getByRole('button', { name: /Read \/ proposal tools/ }).boundingBox();
    expect(plannerHandoffBox).not.toBeNull();
    expect(readToolsBox).not.toBeNull();
    expect((plannerHandoffBox?.x ?? 0) + (plannerHandoffBox?.width ?? 0)).toBeLessThanOrEqual(readToolsBox?.x ?? 0);

    await page.getByLabel('AI workspace prompt').fill('move every occurrence to 7 pm');
    await page.getByRole('button', { name: 'Send message' }).click();
    await expect(page.getByText('completed', { exact: true })).toBeVisible();
    await expect(page.getByText('4', { exact: true }).first()).toBeVisible();
    await expect(page.getByText('handoff_to_planner', { exact: true }).first()).toBeVisible();

    await page.getByRole('button', { name: 'Previous', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'tool observation' })).toBeVisible();
    await expect(page.getByText(/"target_scope": "series"/)).toBeVisible();
    await expect(page.getByText(/"preferred_start_time": "19:00"/)).toBeVisible();
    await expect(page.getByRole('heading', { name: 'When each flow should be selected' })).toBeVisible();
    await expect(page.getByText('Planner sub-loop', { exact: true })).toBeVisible();

    await page.getByLabel('AI workspace prompt').fill('and keep the same duration');
    await page.getByRole('button', { name: 'Send message' }).click();
    await expect(page.getByText('I kept the one-hour duration.', { exact: true })).toBeVisible();

    await page.getByRole('button', { name: /tools & contracts/i }).click();
    await expect(page.getByRole('heading', { name: 'handoff_to_planner' })).toBeVisible();
    await expect(page.getByText('target_scope')).toBeVisible();

    await page.getByRole('button', { name: /data & endpoints/i }).click();
    await expect(page.getByRole('heading', { name: 'MCP promotion readiness' })).toBeVisible();
    await expect(page.getByText('build-admin-test', { exact: true })).toBeVisible();
    await expect(page.getByText('authority unchanged', { exact: false })).toBeVisible();
    await expect(page.getByText('search_entities:insufficient_matches:12/25')).toBeVisible();
    await expect(page.getByRole('heading', { name: 'strategist_agent_events' })).toBeVisible();
    await expect(page.getByText('/api/v1/ai/strategist')).toBeVisible();
  });
});
