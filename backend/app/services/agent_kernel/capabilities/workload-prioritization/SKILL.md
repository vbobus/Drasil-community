# Workload Prioritization

Use this skill when the user asks what to focus on, what is easy, what has leverage, what is urgent, or how to choose among tasks.

## Establish Scope and Evidence

- Resolve whether the request concerns a named task, branch, category, or workspace. Conversation and UI context help resolve references but do not replace fresh task evidence.
- Treat entity search as a named-task resolver only. Do not search the user's whole conceptual question.
- For category or workspace prioritization, retrieve a current bounded scope using `query_task_graph` or `get_workload_overview`. Candidate search results are not a workload snapshot.
- Use structured deadline/status/category filters for time horizons such as tomorrow, this week, overdue, or active work.
- Honor the requested time horizon exactly; do not append later tasks as fallback recommendations unless the user asks for broader alternatives.
- Fetch branches only for finalists where children, blockers, or dependencies affect the recommendation.
- If evidence is incomplete or truncated, retrieve more, narrow the claim, or state the limitation. Do not claim full-scope coverage from candidate-only evidence.

## Judge Priority

- Rank overdue and imminent deadlines before longer-horizon work unless the user states a different objective.
- Consider downstream unlocks, blockers, dependencies, application/career/school impact, and compounding value.
- Infer effort from title, notes, child count, ambiguity, blockers, dependencies, and whether the item is a project branch or executable leaf.
- Low-hanging fruit means clear, small, safe to start, and useful. High leverage means meaningful downstream value; it is not simply the shortest estimate.
- Do not rely only on manually entered priority, deadline, or estimate, but do not omit concrete due dates when they exist.

## Answer

- Synthesize a ranked recommendation instead of dumping retrieved tasks.
- Name each recommended task, explain the decisive evidence, and provide the next concrete action.
- State the inspected scope and any material coverage limitation.
- A read-only recommendation needs no mutation target or approval proposal.
- If the user also asks to change tasks, identify exact targets and use the appropriate typed proposal tool; never apply changes directly.

## Clarify Only When Blocked

- Do not clarify merely because several tasks exist; comparison requires multiple tasks.
- Clarify when the requested scope remains genuinely ambiguous, a relational reference has no reliable anchor, or an exact mutation target cannot be established safely.
