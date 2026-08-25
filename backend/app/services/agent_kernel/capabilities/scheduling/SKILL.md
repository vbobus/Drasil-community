# Scheduling and Timeboxing

Use this skill to create a feasible time-based plan from task evidence and known availability.

- Retrieve the current tasks in scope, including deadlines, estimates, status, and dependencies.
- Distinguish task deadlines from calendar availability. If calendar data is unavailable, say so and plan only from the availability the user supplied.
- Schedule prerequisites first, include transition and review buffers, and avoid filling every available minute.
- If an existing task has no estimate and the user did not provide a duration or exact time range, ask how much time to reserve before scheduling it. Do not silently turn the generic fallback duration into the user's estimate.
- A valid user-supplied start-to-end range is the duration and exact placement. Preserve it, hand off once, and do not ask for duration or availability again; deterministic Planner validation owns feasibility.
- Treat corrections to an unapproved schedule proposal as revisions of that proposal. Use rescheduling only for accepted calendar blocks; if no accepted block exists, create the corrected proposed block.
- Preserve resolved context across a clarification answer. Once the user supplies a missing duration, do not ask again; compile it into the schedule and, when the task had no estimate, include a companion task update in the same approval.
- Prefer one composed proposal for task-detail and calendar changes that serve the same user goal. Use `task_updates` on the Planner handoff instead of making the user approve disconnected metadata and schedule proposals.
- When the user asks to create and schedule new tasks, preserve both parts through clarification and produce one composed proposal. Use temporary task identities so reviewed calendar blocks resolve to the newly created tasks on approval.
- In a compound create flow, decide tree parentage and calendar placement independently for every new node. Calendar-schedule only nodes whose own placement and duration are resolved, and use separate scheduling groups when their windows differ.
- Treat a clock-time meal or break as a temporary protected interval, not as an inter-task gap attached to an unrelated earlier task. Split long work around that interval when the total work fits in the surrounding availability.
- Treat protected, pinned, externally synced, and clearly fixed calendar items such as appointments, meetings, or classes as anchors. For long work, prefer useful blocks before and after an anchor when both sides are available, and tell the user which item caused the split.
- Compile every explicit request-level restriction into typed `request_constraints`, even when it is only communicated verbally and has no corresponding task or calendar block. Use daily recurrence and weekday scope for repeating availability or unavailability. Hard request constraints outrank remembered preferences and heuristics, and the completed proposal must be checked against them before it is shown.
- Split long work when the user asks, when an explicit break falls inside it, or when fragmented availability can fit the work but no sensible contiguous opening can. Keep all blocks linked to the same task and preserve the full estimate.
- Apply conditional gap preferences per adjacent task. If the saved rule says tasks below a duration threshold may be back-to-back, do not apply the longer-task buffer to those shorter tasks.
- Preserve structured placement reasons with every proposal so later questions about timing can be answered from evidence rather than reconstructed or guessed.
- A proposed schedule is read-only unless the user asks to change task metadata or remember a durable preference.
- Use `submit_preference_plan` for durable scheduling preferences and the high-level task-plan tools for requested task changes. The server owns ChangeSet compilation, validation, review identity, and execution.
- For saved-preference administration, call `list_remembered_preferences` first, then use `submit_preference_management_plan` with the exact IDs and revisions. Disable is reversible, restore reactivates linked rules, and delete is permanent. These actions remain approval-gated.
- For saved-preference cleanup, use the narrowest `cleanup_scope`: `contradictions` for conflicting/superseded values, `overlaps` for duplicate/redundant copies, and `all` only when both are requested. Pass the same scope and analyzer fingerprint to `submit_preference_cleanup_plan`. The server owns the complete scoped batch; do not hand-select a subset. Ambiguous tensions remain separate.

## Constraint Translation

- Translate an unavailable break, appointment, travel interval, or personal commitment to `unavailable_interval`.
- Translate a bounded feasible range to `allowed_window` and a required start to `exact_start`.
- Translate before/after relationships to `task_order`, transition time to `minimum_gap`, and no-split instructions to `contiguous`.
- Use daily recurrence plus a weekday set when an availability rule repeats across the requested range.
- Leave `task_ids` absent when a rule governs the whole request rather than one task.
- Mark explicit must, cannot, exact, and fixed instructions hard and preserve their source text.
- For ongoing recurrence, preserve `end_mode: never`; when time is immutable, also preserve `strict: true`.

## Conflict and Recurrence Policy

- Evaluate incoming and existing commitments symmetrically using typed priority, deadline, movability, anchoring, and protection evidence.
- Treat exact-time meetings, interviews, calls, classes, and appointments as anchored unless current evidence says otherwise.
- Treat ordinary task blocks, workouts, and focus sessions as flexible unless the user or stored state says otherwise.
- Preserve protected, pinned, externally fixed, and hard-constrained commitments.
- Move a flexible blocker for a higher-priority anchored incoming commitment only after the solver finds a valid nearby opening.
- For a 5–15 minute exact-time prerequisite check, reservation, or booking blocked by
  an ordinary flexible task of no higher priority, prefer the smallest solver-proven
  pushback of that blocker. Preserve its full duration and keep the task-tree creation,
  blocker move, and new scheduled block inside the same approval.
- Move a flexible incoming task around an anchored blocker. Ask one precise question when both sides are anchored or precedence is unclear.
- For recurring conflicts, move or revise only the colliding occurrence unless the user explicitly targets the full series.
- A missing time of day is not automatically ambiguous; let Planner choose a feasible opening from current evidence and policy.
- A user-supplied clock time is the placement anchor but does not authorize overriding another anchored or protected commitment.

## Composition and Review

- Keep create-and-schedule, duration propagation, and related task/calendar effects inside one goal and one approval boundary.
- Preserve temporary identity across create-then-schedule flows so approved calendar blocks bind to the created task.
- Conserve every requested operation before review; repair a partial plan instead of presenting it.
- Keep user-facing review language semantic and concise. Do not expose patch families, field keys, operation counts, or database ids.
