from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.services.agent_kernel.contracts import (
    DraftChangeSetCommand,
    EntityExistsAssertion,
    EntityFieldEqualsAssertion,
    EntityRef,
    Preconditions,
    TaskCreateOperation,
    strict_provider_schema,
)


def create_operation() -> TaskCreateOperation:
    return TaskCreateOperation(
        operation_id="op:create",
        title="Prepare launch brief",
        workspace_ref=EntityRef(kind="workspace", entity_id="workspace:1"),
        temp_ref="temp:task",
    )


class AgentChangeSetAuthoringContractTests(unittest.TestCase):
    def test_draft_command_is_provider_strict_and_excludes_server_identity(self) -> None:
        schema = strict_provider_schema(DraftChangeSetCommand)
        self.assertIs(schema["additionalProperties"], False)

        command = DraftChangeSetCommand(
            disposition="create",
            expected_goal_revision=1,
            operations=[create_operation()],
        )
        self.assertEqual(command.disposition, "create")
        with self.assertRaises(ValidationError):
            DraftChangeSetCommand.model_validate(
                {
                    **command.model_dump(mode="json", by_alias=True),
                    "changeSetId": "changeset:model-chosen",
                }
            )

    def test_create_and_revise_require_exact_cas_shapes(self) -> None:
        with self.assertRaises(ValidationError):
            DraftChangeSetCommand(
                disposition="create",
                expected_goal_revision=1,
                expected_change_set_revision=1,
                operations=[create_operation()],
            )
        with self.assertRaises(ValidationError):
            DraftChangeSetCommand(
                disposition="revise",
                expected_goal_revision=2,
                operations=[create_operation()],
            )

    def test_preconditions_are_closed_typed_and_revision_refs_are_persisted(self) -> None:
        preconditions = Preconditions(
            expected_entity_revisions=[
                EntityRef(kind="task", entity_id="task:1", revision=2)
            ],
            assertions=[
                EntityExistsAssertion(
                    assertion_id="assert:exists",
                    entity_ref=EntityRef(kind="task", entity_id="task:1"),
                ),
                EntityFieldEqualsAssertion(
                    assertion_id="assert:title",
                    entity_ref=EntityRef(kind="task", entity_id="task:1"),
                    field="title",
                    expected_value="Launch",
                ),
            ],
        )
        self.assertEqual(len(preconditions.assertions), 2)
        strict_provider_schema(Preconditions)

        with self.assertRaises(ValidationError):
            Preconditions(
                expected_entity_revisions=[
                    EntityRef(kind="task", entity_id="task:1")
                ]
            )
        with self.assertRaises(ValidationError):
            Preconditions.model_validate(
                {
                    "assertions": [
                        {
                            "kind": "entity.exists",
                            "assertionId": "assert:bad",
                            "entityRef": {"kind": "task", "entityId": "task:1"},
                            "unknown": True,
                        }
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
