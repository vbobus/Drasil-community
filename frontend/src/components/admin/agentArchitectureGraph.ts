export type ArchitectureLayer = 'entry' | 'context' | 'reasoning' | 'tools' | 'safety' | 'data';

export interface AgentLoopNode {
  id: string;
  label: string;
  plainTitle: string;
  layer: ArchitectureLayer;
  x: number;
  y: number;
  width?: number;
  summary: string;
  receives: string;
  produces: string;
  implementation: string[];
}

export interface AgentLoopGroup {
  id: string;
  label: string;
  plainTitle: string;
  x: number;
  y: number;
  width: number;
  height: number;
  kind: 'system' | 'subsystem' | 'external';
}

export interface FlowSelectionRule {
  id: string;
  title: string;
  routeLabel: string;
  stageId: string;
  selectedWhen: string;
  expectedBehavior: string;
  warningSign: string;
  eventTypes: string[];
  toolNames?: string[];
}

export interface AgentLoopEdge {
  id: string;
  from: string;
  to: string;
  label: string;
  kind: 'normal' | 'loop' | 'storage' | 'conditional' | 'human';
}

export const LOOP_CANVAS = { width: 1840, height: 1280 };

export const agentLoopGroups: AgentLoopGroup[] = [
  { id: 'interface', label: 'Browser / human boundary', plainTitle: 'Conversation enters and leaves here', x: 20, y: 340, width: 420, height: 250, kind: 'external' },
  { id: 'persistence', label: 'Persistence layer', plainTitle: 'Durable product truth and execution history', x: 20, y: 625, width: 420, height: 420, kind: 'external' },
  { id: 'runtime', label: 'Agent Kernel runtime', plainTitle: 'The host-owned orchestration envelope', x: 460, y: 75, width: 1330, height: 770, kind: 'system' },
  { id: 'context-system', label: 'Context system', plainTitle: 'Reconstruct, retrieve and compact only relevant state', x: 480, y: 115, width: 470, height: 690, kind: 'subsystem' },
  { id: 'strategist-system', label: 'Strategist reasoning system', plainTitle: 'Route and run the free-form reasoning loop', x: 970, y: 115, width: 790, height: 420, kind: 'subsystem' },
  { id: 'tool-system', label: 'Tool and Planner subsystem', plainTitle: 'Typed capabilities that return observations to Strategist', x: 970, y: 560, width: 790, height: 245, kind: 'subsystem' },
  { id: 'outcome-system', label: 'Proposal, safety and execution pipeline', plainTitle: 'Conserve intent, obtain approval and apply atomically', x: 460, y: 875, width: 1330, height: 365, kind: 'system' },
];

export const agentLoopNodes: AgentLoopNode[] = [
  { id: 'prompt', label: 'Prompt + UI context', plainTitle: 'What the user sends', layer: 'entry', x: 45, y: 410, width: 160, summary: 'The words you type plus the visible workspace state.', receives: 'Prompt, selected task, active workspace, calendar window, local time and timezone.', produces: 'One validated Strategist request.', implementation: ['StrategistRequest', 'Chat workspace'] },
  { id: 'api', label: 'AI API boundary', plainTitle: 'Accept and record the request', layer: 'entry', x: 235, y: 410, width: 170, summary: 'Checks the HTTP shape, ownership and session before starting work.', receives: 'The browser request and authenticated principal.', produces: 'A durable session, turn and queued run.', implementation: ['/api/v1/ai/strategist', 'StrategistRun'] },
  { id: 'ledger', label: 'Run + event ledger', plainTitle: 'Durable execution journal', layer: 'data', x: 235, y: 650, width: 170, summary: 'Records the run and each important event so execution can be inspected and recovered.', receives: 'Run status, correlation IDs, tool observations and decisions.', produces: 'A replayable chronological trace.', implementation: ['strategist_runs', 'strategist_agent_events'] },
  { id: 'context', label: 'Context reconstruction', plainTitle: 'Rebuild what matters now', layer: 'context', x: 500, y: 410, summary: 'Combines the new prompt with the bounded facts needed for this objective.', receives: 'Current prompt, prior turns, pending proposal, working set, recent tool evidence and UI context.', produces: 'A compact context packet for routing and reasoning.', implementation: ['build_session_memory', 'context_packet'] },
  { id: 'memory', label: 'Session working set', plainTitle: 'Remember the ongoing subject', layer: 'context', x: 500, y: 170, summary: 'Keeps referable task identities and active-objective facts across turns—even after a proposal is applied.', receives: 'Resolved entities, recurrence snapshots, corrections and proposal lineage.', produces: 'Bounded continuation memory.', implementation: ['session_memory.py', 'working set'] },
  { id: 'compaction', label: 'Context compaction', plainTitle: 'Keep meaning, drop bulk', layer: 'context', x: 750, y: 170, summary: 'Preserves semantic facts and handles while avoiding an ever-growing raw transcript.', receives: 'Recent events, tool summaries, objective corrections and large result handles.', produces: 'A small continuation envelope plus references to exact stored evidence.', implementation: ['compact continuation envelope', 'tool evidence handles'] },
  { id: 'retrieval', label: 'Authoritative retrieval', plainTitle: 'Read current product truth', layer: 'data', x: 500, y: 650, summary: 'Fetches exact task, tree, schedule, recurrence and preference facts instead of relying on remembered prose.', receives: 'Entity names, IDs, requested date/scope and active workspace.', produces: 'Revisioned entity evidence.', implementation: ['get_task_schedule', 'get_task_tree', 'entity memory'] },
  { id: 'database', label: 'SQLite product state', plainTitle: 'Source of local truth', layer: 'data', x: 200, y: 880, summary: 'Stores tasks, calendars, recurrence, preferences, goals, proposals, runs and events.', receives: 'Read queries and approved atomic writes.', produces: 'Versioned rows and relationships.', implementation: ['SQLModel', 'SQLite'] },
  { id: 'router', label: 'Tier + route decision', plainTitle: 'Choose the smallest safe path', layer: 'reasoning', x: 990, y: 400, summary: 'Decides whether a deterministic answer is enough or the free-form model/tool loop is needed.', receives: 'Prompt intent and reconstructed session state.', produces: 'Route, budgets and tool policy.', implementation: ['tier_router.py', 'tier_route_decision'] },
  { id: 'profile', label: 'Agent profile', plainTitle: 'Define the agent’s job', layer: 'reasoning', x: 990, y: 170, summary: 'Defines what the Strategist owns, what it must not do and which capabilities it can load.', receives: 'Registered AGENT.md profile.', produces: 'Role instructions and boundaries.', implementation: ['Strategist AGENT.md', 'Planner AGENT.md'] },
  { id: 'skills', label: 'Capability / skill loader', plainTitle: 'Load task-specific guidance', layer: 'reasoning', x: 1205, y: 170, summary: 'Adds focused instructions only when the objective needs them, such as scheduling.', receives: 'Requested capability IDs and route.', produces: 'Skill guidance and recommended tool names.', implementation: ['SKILL.md manifests', 'load_skill'] },
  { id: 'provider', label: 'Codex app-server', plainTitle: 'Call the configured model', layer: 'reasoning', x: 1210, y: 400, summary: 'Uses the cached ChatGPT/Codex OAuth transport and streams model/tool-loop progress.', receives: 'Agent instructions, compact context, schemas and available tool names.', produces: 'A model response or a typed tool request.', implementation: ['JSON-RPC over stdio', 'cached OAuth'] },
  { id: 'model', label: 'Strategist model loop', plainTitle: 'Reason, act, observe, repeat', layer: 'reasoning', x: 1450, y: 400, summary: 'The free-form loop chooses whether to answer, retrieve evidence, ask a precise question or call another capability.', receives: 'Context plus every prior tool observation in this run.', produces: 'Tool calls, clarification, or a response candidate.', implementation: ['model_step', 'dynamic tool loop'] },
  { id: 'registry', label: 'Tool registry + contracts', plainTitle: 'Expose safe actions', layer: 'tools', x: 1550, y: 650, summary: 'Lists the tools the agent may use and validates their typed arguments and semantic invariants.', receives: 'Tool name and model-authored arguments.', produces: 'Authorized dispatch or a repairable typed error.', implementation: ['tool_registry.py', 'Pydantic + semantic validators'] },
  { id: 'tool', label: 'Read / proposal tools', plainTitle: 'Perform one bounded capability', layer: 'tools', x: 1380, y: 650, width: 170, summary: 'Reads authoritative state or drafts a proposal. Tools cannot silently escape their declared side-effect policy.', receives: 'Validated arguments with exact targets and scope.', produces: 'A typed observation returned to the same model loop.', implementation: ['dynamicTools', 'tool_observation'] },
  { id: 'planner_bridge', label: 'Planner handoff', plainTitle: 'Send temporal intent without losing it', layer: 'tools', x: 1180, y: 650, width: 170, summary: 'Carries scheduling objective, constraints, targets and occurrence/series scope into Planner and returns its result.', receives: 'A validated Planner handoff contract.', produces: 'Placement, conflict, clarification or recurrence proposal evidence.', implementation: ['handoff_to_planner', 'target_scope'] },
  { id: 'planner', label: 'Planner loop', plainTitle: 'Work out time and conflicts', layer: 'reasoning', x: 990, y: 650, width: 170, summary: 'Reasons over windows, recurrence, preferences, conflicts and alternatives using authoritative calendar evidence.', receives: 'Conserved scheduling request and exact schedule facts.', produces: 'A typed plan, conflict decision or repairable failure.', implementation: ['Planner capability', 'native_recurrence'] },
  { id: 'goal', label: 'GoalState coordinator', plainTitle: 'Keep the objective stable', layer: 'safety', x: 810, y: 900, summary: 'Tracks the original objective, revisions, constraints, unresolved decisions and terminal status.', receives: 'User corrections, resolved decisions and planner/tool evidence.', produces: 'A revisioned objective used to judge whether the proposal is complete.', implementation: ['GoalState', 'goal revision'] },
  { id: 'changeset', label: 'ChangeSet authoring', plainTitle: 'Turn intent into exact proposed operations', layer: 'safety', x: 1030, y: 900, summary: 'Creates atomic operations with explicit targets, values, scope and dependencies.', receives: 'GoalState plus authoritative evidence.', produces: 'A draft ChangeSet; still no product write.', implementation: ['DraftChangeSetCommand', 'canonical operations'] },
  { id: 'guards', label: 'Validate + authorize + simulate', plainTitle: 'Prove the plan is safe and possible', layer: 'safety', x: 1280, y: 900, summary: 'Checks schemas, identity, revisions, scope, permissions, conflicts and combined effects before review.', receives: 'Draft ChangeSet and current database revisions.', produces: 'Simulation, decisions, typed failures or a reviewable plan.', implementation: ['semantic validation', 'entity authorizer', 'simulation'] },
  { id: 'decision', label: 'Conflict / missing decision', plainTitle: 'Ask only for a real choice', layer: 'entry', x: 1280, y: 1130, summary: 'Pauses when user judgment is genuinely required and carries exact affected items and alternatives.', receives: 'Typed conflict or unresolved decision.', produces: 'A user choice that revises GoalState and resumes the loop.', implementation: ['Decision contract', 'clarification'] },
  { id: 'review', label: 'Review projection', plainTitle: 'Explain the exact before and after', layer: 'entry', x: 1510, y: 900, summary: 'Transforms internal operations into a human-readable proposal with concrete entities, dates, times and effects.', receives: 'Simulated ChangeSet and authoritative titles.', produces: 'ReviewDocument and approve/edit/reject controls.', implementation: ['ReviewDocument', 'presentation projector'] },
  { id: 'approval', label: 'Human approval', plainTitle: 'The user controls the write', layer: 'entry', x: 1510, y: 1130, summary: 'Nothing mutates until the user approves the exact revision they reviewed.', receives: 'Approve, edit, reject or decision action.', produces: 'A signed action bound to the reviewed goal, ChangeSet and simulation.', implementation: ['review action binding', 'idempotency key'] },
  { id: 'transaction', label: 'Transaction + verification', plainTitle: 'Apply atomically and verify', layer: 'safety', x: 1030, y: 1130, summary: 'Rechecks revisions, commits all operations together, verifies the resulting facts and queues external sync.', receives: 'Approved immutable command.', produces: 'Verified local outcome, bindings and sync status.', implementation: ['transaction runtime', 'outbox sync'] },
  { id: 'response', label: 'Response + next turn', plainTitle: 'Show the result and preserve continuity', layer: 'entry', x: 560, y: 1130, summary: 'Returns the answer, clarification, review or execution result, then updates bounded session memory for follow-up requests.', receives: 'Final answer candidate or verified execution outcome.', produces: 'UI response and compact continuation evidence.', implementation: ['final_answer', 'SSE update/done'] },
  { id: 'recovery', label: 'Typed repair / retry', plainTitle: 'Recover without inventing missing facts', layer: 'safety', x: 1510, y: 170, summary: 'Schema, provider, tool and simulation failures are classified so the agent can repair internally or ask for the exact missing decision.', receives: 'Typed failure code, affected fields and retryability.', produces: 'A repaired call, safe retry or truthful user-facing failure.', implementation: ['contract retry', 'provider recovery'] },
];

export const agentLoopEdges: AgentLoopEdge[] = [
  { id: 'prompt-api', from: 'prompt', to: 'api', label: 'submit', kind: 'normal' },
  { id: 'api-ledger', from: 'api', to: 'ledger', label: 'persist', kind: 'storage' },
  { id: 'api-context', from: 'api', to: 'context', label: 'start run', kind: 'normal' },
  { id: 'memory-context', from: 'memory', to: 'context', label: 'continuation', kind: 'storage' },
  { id: 'compaction-memory', from: 'compaction', to: 'memory', label: 'bounded memory', kind: 'storage' },
  { id: 'retrieval-context', from: 'retrieval', to: 'context', label: 'exact facts', kind: 'storage' },
  { id: 'database-retrieval', from: 'database', to: 'retrieval', label: 'revisioned rows', kind: 'storage' },
  { id: 'context-router', from: 'context', to: 'router', label: 'context packet', kind: 'normal' },
  { id: 'router-provider', from: 'router', to: 'provider', label: 'model-owned route', kind: 'conditional' },
  { id: 'profile-provider', from: 'profile', to: 'provider', label: 'role rules', kind: 'normal' },
  { id: 'skills-provider', from: 'skills', to: 'provider', label: 'focused guidance', kind: 'normal' },
  { id: 'provider-model', from: 'provider', to: 'model', label: 'reasoning step', kind: 'normal' },
  { id: 'model-registry', from: 'model', to: 'registry', label: 'tool request', kind: 'normal' },
  { id: 'registry-tool', from: 'registry', to: 'tool', label: 'validated dispatch', kind: 'normal' },
  { id: 'tool-model', from: 'tool', to: 'model', label: 'observation', kind: 'loop' },
  { id: 'tool-retrieval', from: 'tool', to: 'retrieval', label: 'read current state', kind: 'storage' },
  { id: 'tool-planner-bridge', from: 'tool', to: 'planner_bridge', label: 'scheduling handoff', kind: 'conditional' },
  { id: 'planner-bridge-planner', from: 'planner_bridge', to: 'planner', label: 'conserved intent', kind: 'normal' },
  { id: 'planner-tool', from: 'planner', to: 'tool', label: 'typed result', kind: 'loop' },
  { id: 'model-goal', from: 'model', to: 'goal', label: 'objective update', kind: 'normal' },
  { id: 'planner-goal', from: 'planner', to: 'goal', label: 'placement evidence', kind: 'normal' },
  { id: 'goal-changeset', from: 'goal', to: 'changeset', label: 'author operations', kind: 'normal' },
  { id: 'changeset-guards', from: 'changeset', to: 'guards', label: 'check together', kind: 'normal' },
  { id: 'guards-decision', from: 'guards', to: 'decision', label: 'user choice required', kind: 'conditional' },
  { id: 'decision-goal', from: 'decision', to: 'goal', label: 'revise objective', kind: 'human' },
  { id: 'guards-review', from: 'guards', to: 'review', label: 'safe simulation', kind: 'normal' },
  { id: 'review-approval', from: 'review', to: 'approval', label: 'review', kind: 'human' },
  { id: 'approval-goal', from: 'approval', to: 'goal', label: 'edit / reject', kind: 'human' },
  { id: 'approval-transaction', from: 'approval', to: 'transaction', label: 'approve', kind: 'human' },
  { id: 'transaction-database', from: 'transaction', to: 'database', label: 'atomic write', kind: 'storage' },
  { id: 'transaction-response', from: 'transaction', to: 'response', label: 'verified outcome', kind: 'normal' },
  { id: 'model-response', from: 'model', to: 'response', label: 'answer / clarification', kind: 'normal' },
  { id: 'response-compaction', from: 'response', to: 'compaction', label: 'retain meaning', kind: 'loop' },
  { id: 'registry-recovery', from: 'registry', to: 'recovery', label: 'invalid contract', kind: 'conditional' },
  { id: 'provider-recovery', from: 'provider', to: 'recovery', label: 'transport failure', kind: 'conditional' },
  { id: 'recovery-model', from: 'recovery', to: 'model', label: 'repair / retry', kind: 'loop' },
  { id: 'context-ledger', from: 'context', to: 'ledger', label: 'trace', kind: 'storage' },
  { id: 'model-ledger', from: 'model', to: 'ledger', label: 'trace', kind: 'storage' },
];

export const flowSelectionRules: FlowSelectionRule[] = [
  { id: 'freeform', title: 'Free-form Strategist loop', routeLabel: 'Model-owned tool loop', stageId: 'model', selectedWhen: 'The request needs interpretation, conversation continuity, multiple steps, or flexible tool choice.', expectedBehavior: 'Reason, call bounded tools, observe their results and continue until it can answer or propose safely.', warningSign: 'The loop answers from memory when authoritative state should have been read.', eventTypes: ['model_action_loop_started', 'model_step'] },
  { id: 'retrieval', title: 'Authoritative retrieval branch', routeLabel: 'Read before deciding', stageId: 'retrieval', selectedWhen: 'The request names or implies existing tasks, calendar blocks, recurrence, preferences, or relationships.', expectedBehavior: 'Resolve exact entities and revisions before proposing a mutation.', warningSign: 'A proposal invents an ID, cadence, date, duration, or scope that was available in storage.', eventTypes: ['tool_observation'], toolNames: ['get_task_schedule', 'get_task_tree', 'search_tasks', 'resolve_entities'] },
  { id: 'planner', title: 'Planner sub-loop', routeLabel: 'Temporal reasoning', stageId: 'planner', selectedWhen: 'Placement, recurrence, duration, calendar conflict, availability, or schedule alternatives matter.', expectedBehavior: 'Carry the full target and occurrence/series scope into Planner, then return its typed result to Strategist.', warningSign: 'A whole-series request becomes one calendar occurrence or previously supplied duration disappears.', eventTypes: ['planner_handoff_started', 'planner_handoff_completed', 'tool_observation'], toolNames: ['handoff_to_planner'] },
  { id: 'clarification', title: 'User clarification branch', routeLabel: 'Missing meaning', stageId: 'decision', selectedWhen: 'A required target, time, duration, scope, or other user-owned fact is genuinely unknown or ambiguous.', expectedBehavior: 'Ask one precise question and preserve everything already known.', warningSign: 'The same answered question is asked again or internal failures are presented as missing user information.', eventTypes: ['deterministic_scope_resolution', 'final_answer'] },
  { id: 'conflict', title: 'Conflict decision branch', routeLabel: 'Real trade-off', stageId: 'decision', selectedWhen: 'The simulated plan conflicts with protected work or has multiple valid alternatives requiring user preference.', expectedBehavior: 'Show exact affected dates/items and concrete options, then resume from the selected option.', warningSign: 'A generic conflict is claimed without identifying the actual overlapping or displaced item.', eventTypes: ['validation_result', 'final_answer'] },
  { id: 'review', title: 'Human review branch', routeLabel: 'Any proposed write', stageId: 'review', selectedWhen: 'The request would create, update, move, schedule, remember, archive, or delete product state.', expectedBehavior: 'Simulate first and show concrete before/after effects with approve, edit and reject controls.', warningSign: 'A mutation happens directly from model text or the review hides the actual target/time.', eventTypes: ['proposal_tool_contract_compiled', 'native_authority_selection', 'final_answer'] },
  { id: 'execution', title: 'Approved execution branch', routeLabel: 'Write after approval', stageId: 'transaction', selectedWhen: 'The user approves the exact current review revision.', expectedBehavior: 'Revalidate, commit atomically, verify facts and report external-sync state.', warningSign: 'Approval applies an older revision, partially commits, or reports success before verification.', eventTypes: ['approval_decision', 'applied_patch'] },
  { id: 'repair', title: 'Typed repair and retry loop', routeLabel: 'Internal recovery', stageId: 'recovery', selectedWhen: 'A tool schema, semantic invariant, provider response, simulation, or projection fails but no user decision is missing.', expectedBehavior: 'Repair or retry internally with the precise error; ask the user only when their judgment is actually needed.', warningSign: 'The user receives “try again” with no reason or is asked to repeat information already present.', eventTypes: ['provider_failure', 'model_rescue_started', 'model_contract_retry_started', 'model_contract_retry_completed', 'planner_handoff_failed', 'proposal_contract_repair', 'native_review_requirement_blocked'] },
  { id: 'continuation', title: 'Conversation continuation loop', routeLabel: 'Next turn, same subject', stageId: 'compaction', selectedWhen: 'A follow-up refers to the active objective, recently applied task, pending proposal, or earlier tool evidence.', expectedBehavior: 'Compact completed bulk, retain semantic facts and refresh mutable state before another change.', warningSign: 'The agent forgets answered slots, keeps a stale proposal, or contaminates a new objective with the old one.', eventTypes: ['context_packet', 'user_turn'] },
];

const directEventStages: Record<string, string> = {
  user_turn: 'prompt',
  context_packet: 'context',
  tier_route_decision: 'router',
  agent_run_plan_created: 'router',
  model_action_loop_started: 'provider',
  model_step: 'model',
  planner_handoff_started: 'planner_bridge',
  planner_handoff_completed: 'planner',
  planner_handoff_failed: 'recovery',
  draft_patch: 'changeset',
  validation_result: 'guards',
  native_shadow_sampling: 'guards',
  native_changeset_shadow_comparison: 'guards',
  native_authority_selection: 'guards',
  native_review_requirement_blocked: 'recovery',
  proposal_tool_contract_compiled: 'review',
  proposal_contract_repair: 'recovery',
  approval_decision: 'approval',
  applied_patch: 'transaction',
  provider_failure: 'recovery',
  model_rescue_started: 'recovery',
  model_contract_retry_started: 'recovery',
  model_contract_retry_completed: 'recovery',
  final_answer: 'response',
};

export function stageForEvent(eventType: string, toolName?: unknown): string {
  if (eventType === 'tool_observation') {
    const name = typeof toolName === 'string' ? toolName : '';
    if (name === 'load_skill') return 'skills';
    if (name === 'handoff_to_planner') return 'planner_bridge';
    if (/^(get|list|search|resolve|inspect|query|load_.*context)/.test(name)) return 'retrieval';
    if (/change_set|patch|proposal|author/.test(name)) return 'changeset';
    return 'tool';
  }
  if (eventType.includes('clarification') || eventType.includes('decision')) return 'decision';
  if (eventType.includes('retry') || eventType.includes('failed') || eventType.includes('failure')) return 'recovery';
  if (eventType.includes('proposal') || eventType.includes('review')) return 'review';
  if (eventType.includes('scope') || eventType.includes('route') || eventType.includes('reconnaissance')) return 'router';
  return directEventStages[eventType] ?? 'ledger';
}
