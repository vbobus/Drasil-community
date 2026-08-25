# Task Metadata Maintenance

Use this skill when the user wants task fields inspected or changed.

- Resolve every mutation target to an exact task id and read the current relevant values.
- Conversation context can resolve references such as “the previous task,” but validate that the task still exists before proposing a change.
- Preserve explicit values exactly. Infer values only when the user asked for a recommendation or practical estimate, and explain material assumptions.
- Use one `submit_task_metadata_plan` call. For different values across tasks, use per-target updates rather than applying one shared value.
- Do not create duplicate tasks to represent metadata changes.
- Read-only review or recommendation does not require a proposal. Requested changes remain approval-gated.
- Clarify only when exact targets or required values cannot be established safely.
