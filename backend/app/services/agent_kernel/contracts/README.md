# Agent Kernel v2 Contracts

This package is the canonical Pydantic v2 source for Agent Kernel semantic contracts.
Provider JSON Schema, runtime validation, API projections, persistence adapters, and tests
must derive from these models rather than maintain independent field definitions.

## Server-owned execution boundary

`ApproveAndApplyChangeSetCommand` is a typed UI/server command, not a model tool. It
binds approval to the exact GoalState, ChangeSet, and simulation revisions and
fingerprints. `LocalExecutionResult` stores immutable bindings, affected facts,
post-commit verification, and external-sync items. Local application status and
provider delivery status are intentionally independent; an exact retry replays the
stored result and never re-executes domain mutations.

### Story 8.7 typed-action handoff

Story 8.7 exposes `ReviewDocument` through the optional `reviewDocument` Strategist
response field and submits its closed action union to
`POST /api/v1/ai/strategist/sessions/{session_id}/review-actions`. The sole projection
entry point is `ReviewDocumentProjector.project()` in `presentation.py`; it consumes one
current GoalState, ChangeSet, and immutable simulation result and fails closed on stale,
non-reviewable, or non-conserved evidence. `ReviewActionCoordinator` reloads that evidence,
constructs `ApproveAndApplyChangeSetCommand` on the server, and invokes
`AgentChangeSetTransactionRuntime.execute(command)`. An action carries the exact
`commandId`, `idempotencyKey`, authenticated `sessionId`, `goalId` plus
`expectedGoalRevision`, `changeSetId` plus `expectedChangeSetRevision`, `simulationId`,
both simulation fingerprints, `correlationId`, and `causationId`. Display labels,
ReviewDocument prose, operation summaries, and model output are never approval identity.

The ReviewDocument action state must retain those revisions and identities and render the
returned `LocalExecutionResult.status`, immutable entity/temp bindings, verification status
and recovery guidance, and each external-sync item's status, attempts, retry time, safe error
summary/recovery action, and remote identity/version when present. `applied` means local
fresh-read verification passed; it does not imply Google delivery succeeded. A
`verification_failed` result must keep the committed local effects visible and offer its
bounded recovery path. An exact action retry uses the original command and idempotency key
and renders the replayed result; it never constructs a replacement command merely because a
response was lost.

The generic browser renderer handles blocks in server order and submits approve, edit,
reject, stored decision, and replay controls without translating their labels into prompts.
The focused browser gate is `frontend/tests/e2e/generic-review-document.spec.ts`; backend
contract, projection, and dispatch gates are `test_agent_review_contracts.py`,
`test_agent_review_projection.py`, and `test_agent_review_actions.py`. Story 8.8 must keep
those gates, the Agent Kernel preservation runner, active legacy Strategist/scheduling flows,
frontend build, lint, and full backend suites green for each cutover family.

### Story 8.8 cutover matrix and gates

Story 8.8 owns declaration and production wiring of four default-off, server-side write
flags: `agent_kernel_v2_task_writes`, `agent_kernel_v2_calendar_writes`,
`agent_kernel_v2_recurrence_writes`, and `agent_kernel_v2_preference_writes`. A mixed
ChangeSet may use the native executor only when every represented operation family is
enabled; otherwise the typed action is unavailable before acceptance. It must not split the
ChangeSet or route only its enabled subset.

Every family requires the contract baseline, all isolated capability-preservation evidence,
native authoring/conservation/simulation gates, atomic transaction and fresh-verification
gates, Story 8.7 typed-action/browser coverage, and representative shadow-divergence results.
Task cutover additionally requires tree relationship and dependent-cleanup coverage;
calendar requires interval/split plus transactional-outbox and Google replay coverage;
recurrence requires composite occurrence identity and rolling-horizon coverage; preference
requires active/supersession/constraint/link coverage. Provider/model matrix and latency/error
telemetry gates remain Story 8.8 responsibilities.

Once a v2 approval command is accepted, its only outcomes are native result replay, typed
conflict/stale failure, local verification recovery, or independently visible sync recovery.
It must never fall back to TaskPatch, SchedulePatch, preference-family apply, or any other
legacy mutation path. Legacy routing remains authoritative only for requests that never enter
the accepted v2 typed-action boundary.

The executable rollout policy is `agent_kernel/rollout.py`. Deployment environment names
are the uppercase forms of the logical flags above. A true flag is insufficient by itself:
`AGENT_KERNEL_V2_GATE_MANIFEST_JSON` must contain passing deterministic, trajectory,
live-model, and browser evidence for that family, with zero unsafe decisions and zero actual
partial commits. Missing, malformed, or failed evidence keeps the family disabled.
Every evidence item also declares its closed `operationKinds` coverage. The union for each
gate must cover every canonical operation in that family, and each required live provider/model
entry must independently cover the entire family. A representative create or cleanup trajectory
therefore cannot unlock unrelated update, delete, split, occurrence, or supersession operations.
`AGENT_KERNEL_V2_BUILD_ID` must exactly match every accepted evidence record's `buildId`;
missing build identity or evidence from another build fails closed.
For local/deployment configuration, `AGENT_KERNEL_V2_GATE_MANIFEST_PATH` may reference the
same bounded JSON array from a server-owned file instead of embedding it in an environment
variable. Inline JSON takes precedence; a missing, unreadable, oversized, or malformed file
produces no evidence and therefore cannot enable writes.
`NativeAuthorityController` derives represented families only from canonical operation
discriminators, emits redacted semantic shadow evidence, and projects exactly one native
`ReviewDocument` only when the complete ChangeSet is eligible. The action coordinator
rechecks the same policy immediately before accepting approval; exact retry replays an
already accepted native command even if later rollout configuration changes.

`provider_matrix.py` is the checked-in/configurable model-matrix boundary. It declares both
OpenAI API and Codex/app-server transports, validates configured model/reasoning selections,
and keeps model identifiers replaceable as provider catalogs evolve. The cached-ChatGPT-OAuth
Codex/app-server entry is the required production/live gate; OpenAI API BYOK remains supported
but optional. Required live entries must pass; an unavailable required entry is not evidence.
Provider readiness reports typed matrix error codes and never logs credentials or raw provider
content.

The live Strategist receives a fail-closed toolset projected from canonical
`KernelToolSpec` contracts through the authoritative in-process `drasil-core`
MCP boundary. `set_goal_transition` is the high-level semantic goal capability.
Low-level `draft_change_set`, `simulate_change_set`, and `update_goal_state`
contracts are internal and never provider or MCP tools. Historical native
authoring/read-shadow code is offline characterization evidence only; environment
flags cannot make it routable from a live turn.

`retirement.py` is the machine-readable adapter inventory and architecture guard.
It verifies that the live MCP surface contains only high-level semantic-plan tools,
the review action boundary cannot import older apply paths, and no shadow or
proposal-family authority can be selected. Existing compatibility cards return an
HTTP 410 recovery response and cannot mutate product state.

The checked real execution gate is `npm run test:e2e:agent-kernel-gate`. It runs an isolated
browser through the real API, GoalState, ChangeSet, simulation, native approval/runtime,
verification, database, and rendered-result path, then directly audits every task, calendar,
recurrence, and preference outcome. The build-bound evidence is recorded in
`_bmad-output/implementation-artifacts/8-8-agent-kernel-gate-manifest.json`. Loading that
manifest remains historical release evidence. Rollback now deploys the previous known-good
build; a request never switches to a second semantic authority. Renderer-only mocked API
coverage remains preservation evidence, not a production routing control.

## Versioning

- Every persisted or externally consumed contract carries `contractVersion`.
- Version `2.x` may add optional or safely defaulted fields without changing existing meaning.
- A field is never renamed in place, an enum value never changes meaning, and a single value
  never becomes plural inside the same major version.
- Breaking semantic changes require a new major version and a named, directional adapter.
- Every revisioned object starts at revision 1. Editing an approved object creates a new
  revision and invalidates approval for the older revision.
- Correlation IDs join one user goal across runs and tools. Causation IDs identify the
  event or command that directly produced the new object.
- Idempotency keys identify retry-safe mutation intent; they are never regenerated merely
  because a provider or process retries.

## Provider Projection

`strict_provider_schema` derives strict-provider schemas from canonical models. It closes
every object and requires every declared property as demanded by strict function calling.
Canonical optional fields remain nullable in the provider projection.

Free-form maps cannot be projected into the strict provider subset without semantic loss.
The compiler fails closed in that case. A model-facing tool must replace such a map with a
typed object or key/value union before it can use the strict v2 path.

Provider acceptance never replaces canonical validation. Arguments are validated again
immediately before execution and tool results are validated before model context ingestion.

## Legacy Compatibility Map

| Legacy surface | v2 canonical owner | Current migration state |
| --- | --- | --- |
| Session active request | `GoalState` | Durable snapshot/reducer authority |
| Historical proposal-family payloads | `ChangeSet` and `Operation` | Offline fixture input only; not routable or approvable |
| Request constraint dictionaries | `Constraint` and `ConstraintScope` | Canonical schema available; cross-domain use is Story 8.4 |
| Conflict prose and payloads | `ConflictGraph` and `ChangeSetSimulationResult` | Canonical typed simulation authority |
| Clarification labels/prompts | `DecisionRequest` and `DecisionDelta` | Stored deltas and generic review choices apply by exact IDs |
| Historical proposal cards | `ReviewDocument` | Retired cards fail closed; one current review is canonical |
| Prose `error` and `repair_hint` | `ToolError` | Stable v2 codes at the MCP boundary |
| Tool result dictionaries | `ToolResult` and declared output models | v2 validation boundary established |
| SQLModel `ChangeSetRecord` JSON columns | Persistence adapter | Not the canonical semantic model; Story 8.3+ owns storage |

Compatibility adapters may be used by offline fixtures to prove semantic
conservation. They are not agent-facing, approvable, executable, or a production
semantic controller.

## Durable GoalState Runtime

- `AgentGoalStateRecord` stores the canonical JSON snapshot with session, status, revision,
  active-session, current-run, correlation, and causation indexes.
- `AgentGoalTransitionRecord` is the ordered command audit. `(session_id, command_id)` is
  unique, so transport retries cannot append the same accepted command twice.
- `GoalStateStore` is the persistence boundary. It validates every snapshot, enforces one
  active unresolved goal per session, rejects cross-session access, and compares the
  expected revision in the database update.
- `goal_state_reducer.py` owns sparse correction, decision, and lifecycle reduction. Callers
  never edit persisted JSON directly.
- Explicit `user`/`ui` provenance outranks tool/retrieval/policy values, which outrank
  inference, which outranks defaults. Equal-precedence current input supersedes the earlier
  value only on the addressed path; explicit clear/null is never inferred from falsiness.
- Lifecycle edges are declared in `ALLOWED_GOAL_TRANSITIONS`. Revisions invalidate older
  approval, and terminal goals (`applied`, `rejected`, `superseded`, `cancelled`,
  `system_failed`) are excluded from active-goal selection.
- A transport/system retry reuses its StrategistRun correlation and command identity. A new
  user turn may create a linked run under the same unresolved goal. Only the typed
  `new_goal` disposition supersedes an active unrelated goal.
- The provider-visible `set_goal_transition` tool accepts only `new`,
  `continue`, or `correct` plus sparse semantic changes. The Agent Host binds
  goal, revision, command, trace, and provenance authority. The older
  `update_goal_state` command remains an internal compatibility input only.

## Internal Native ChangeSet Foundation

- `DraftChangeSetCommand` is an internal compatibility/fixture input. The
  `ServerChangeCompilationCoordinator` owns operation construction; agents do not
  author this contract.
- Strategist semantic clauses and an optional completed owned Planner artifact enter
  one deterministic compilation. The compiler binds exact task/run/goal/revision
  identity plus the immutable task-content fingerprint and source goal revision,
  conserves metadata, hierarchy, independent roots, constraints, and
  unscheduled work, then persists one ChangeSet. High-level tool inputs are validated
  and normalized before this single server-owned compilation; no agent constructs
  operations, review identity, approval identity, or execution commands.
- Compilation failure is a typed repair or stale-evidence result before simulation.
  Successful composition may be simulated once and projected into one exact
  ReviewDocument; it does not add a model authoring pass.
- The operation catalog is the closed 17-kind union in `operations.py`. Temporary refs use
  `kind=temporary` plus their eventual `targetKind`; consumers depend on their one compatible
  producer. Operation IDs, list multiplicity, groups, and stable topological order are preserved.
- Constraints are kind-owned closed values scoped to the goal, operation sets, entity sets,
  series, or occurrence sets. Series and occurrence identity never substitute for each other.
- Baseline bounds are 128 operations, 128 constraints, 512 references, and 256 KiB canonical
  JSON. Validation fails closed and never truncates a plan.
- `operations.requested` is the typed GoalState intent ledger. Requested clauses retain their
  exact typed operations; submitted IDs, kinds, targets, values, times, durations, dependencies,
  groups, and clause links must match. Satisfied clauses cannot absorb new operations.
- Exact calendar placement must match a server-attested operation digest produced from the
  independently validated Planner result (or structured evidence retained with GoalState).
  The compiler verifies that digest rather than minting evidence from submitted effects.
  Model-authored provenance labels and source text are never schedule authority.
- `NativeChangeSetStore` persists canonical snapshots and command audit independently of the
  legacy proposal-compiled store. Create/revise use GoalState and ChangeSet CAS revisions, exact
  command fingerprints, session ownership, target/precondition authorization, immutable command
  result snapshots, and retry-safe replay of the originally accepted revisions.
- Internal drafting may bind one exact feasible alternative from an immutable
  simulation; it cannot approve, execute, or mutate domain entities.
  Historical proposal adapters are offline fixture inputs only.

## Cross-Domain Simulation and ConflictGraph

- All canonical models and union members validate and round-trip.
- Strategist and its owned Planner receive invocation-specific projections from
  canonical v2 `KernelToolSpec` contracts; per-agent MCP policy filters and
  independently enforces each live surface.
- The Agent Host owns Planner task/run/goal identity, budgets, cancellation,
  status mapping, and one-decision callback guards. Planner cannot address the
  user, transition the goal, approve, commit, or create another agent.
- Strict provider schemas and runtime input validation are green.
- Typed ToolError codes are available without removing legacy error compatibility.
- Agent documents meet section, line-length, context-budget, and behavior-preservation gates.
- The Story 8.1 capability-preservation runner remains green in isolated subprocesses.
- GoalState persistence, typed sparse correction, typed UI decisions, lifecycle, retry
  identity, and legacy shadow projection are operational.
- Internal ChangeSet simulation accepts expected GoalState and ChangeSet revisions. The runtime binds
  session, command, trace, and current entity authority; re-authorizes the plan; snapshots detached
  task, workspace, calendar, recurrence, preference, and constraint facts; and rejects stale input.
- Projection applies all 17 native operation kinds in stable dependency order. Temporary references
  resolve only in the detached overlay. Final-state validation rejects orphaned task, calendar, or
  recurrence references, task-parent cycles, and cross-workspace drift. Recurrence rules expand only
  through a 56-day/128-occurrence horizon with composite series-plus-occurrence identities; recurring
  occurrences cannot be split through ordinary calendar-block semantics.
- `ConflictGraph` uses closed conflict, interval, blocker, evidence, recommendation, feasibility,
  and resolution contracts. Conflict and alternative IDs are deterministic for one ChangeSet
  revision and snapshot. Blockers carry explicit freshness evidence, and every feasible alternative
  carries a snapshot/delta proof produced by projecting the complete candidate revision. Collections
  and serialized payloads are bounded and never truncate.
- Every typed hard constraint is evaluated before stored hard scheduling rules and preferences;
  unknown persisted hard policy fails closed. Soft scheduling windows rank solver placement only
  after hard facts pass. Pinned,
  protected, and typed fixed facts are invariants; absent movability evidence remains `unknown`
  rather than being guessed from a title or prompt. Legacy regex policy remains legacy-only.
- Feasible alternatives carry exact revision deltas. CP-SAT supplies bounded placement arithmetic;
  explicit split operations conserve duration; recurring repairs target composite occurrences unless
  the authored operation explicitly targets the series. Simulation never silently edits the plan.
- Clear results bind the exact revision to `ready_for_review`. Repairable results return alternatives
  for model revision and same-run re-simulation. Genuine ambiguity stores one DecisionRequest whose
  options apply exact semantic deltas by ID. Any revision clears prior simulation/readiness linkage.
- Simulation persists `simulating` before its final lifecycle disposition. Expected stale,
  authorization, conservation, projection, recurrence-bound, and invariant outcomes use canonical
  typed issues. Results retain conservation, exact operation/clause effects, bounded final-state
  fingerprints, and immutable input-snapshot evidence.
- `AgentSimulationRecord` stores immutable result and ChangeSet snapshots with command replay
  identity. Simulation events record started/final lifecycle facts. Session cleanup removes this
  audit before its parent ChangeSet and GoalState records.
- Story 8.5 remains shadow-only. It cannot approve, execute, synchronize externally, or mutate
  product-domain state; atomic commit and verification remain Story 8.6.
