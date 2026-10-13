from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from app.services.agent_kernel import agent_observability
from app.services.agent_kernel.agent_observability import (
    agent_observability_status,
    observe_agent_tool,
    observe_agent_turn,
    observe_generation,
    record_agent_event,
)


class _FakeObservation:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        return False

    def update(self, **values) -> None:
        self.updates.append(values)


class _FakeClient:
    def __init__(self) -> None:
        self.starts: list[dict] = []
        self.observations: list[_FakeObservation] = []
        self.events: list[dict] = []

    def create_trace_id(self, *, seed: str) -> str:
        self.trace_seed = seed
        return "a" * 32

    def get_current_trace_id(self) -> str:
        return "a" * 32

    def start_as_current_observation(self, **values) -> _FakeObservation:
        self.starts.append(values)
        observation = _FakeObservation()
        self.observations.append(observation)
        return observation

    def create_event(self, **values) -> None:
        self.events.append(values)


class _FailingClient(_FakeClient):
    def start_as_current_observation(self, **values) -> _FakeObservation:
        raise RuntimeError("exporter is unavailable")


class _NoActiveTraceClient(_FakeClient):
    def get_current_trace_id(self):
        return None


class AgentObservabilityTests(unittest.TestCase):
    def test_hosted_turn_flushes_after_span_closes_even_on_error(self) -> None:
        for fails in (False, True):
            with self.subTest(fails=fails):
                client = _FakeClient()
                client.flush = Mock()
                @observe_agent_turn
                def run():
                    if fails:
                        raise ValueError('turn failed')
                    return {'response': {'summary': 'done'}}
                with patch.object(agent_observability, '_get_client', return_value=client), \
                        patch.dict(agent_observability.os.environ, {'VERCEL':'1'}):
                    if fails:
                        with self.assertRaisesRegex(ValueError, 'turn failed'):
                            run()
                    else:
                        self.assertEqual(run()['response']['summary'], 'done')
                client.flush.assert_called_once()

    def test_hosted_flush_failure_does_not_fail_turn(self) -> None:
        client = _FakeClient()
        client.flush = Mock(side_effect=RuntimeError('export unavailable'))
        @observe_agent_turn
        def run():
            return {'response': {'summary':'done'}}
        with patch.object(agent_observability, '_get_client', return_value=client), \
                patch.dict(agent_observability.os.environ, {'VERCEL':'1'}):
            self.assertEqual(run()['response']['summary'], 'done')

    def test_status_is_fail_open_and_never_exposes_credentials(self) -> None:
        missing = agent_observability_status({})
        self.assertFalse(missing["enabled"])
        self.assertEqual(missing["reason"], "missing_public_key")

        configured = agent_observability_status(
            {
                "LANGFUSE_PUBLIC_KEY": "pk-private",
                "LANGFUSE_SECRET_KEY": "sk-private",
            }
        )
        self.assertTrue(configured["configured"])
        self.assertFalse(configured["captures_content"])
        self.assertEqual(configured["content_mode"], "aggregate")
        self.assertNotIn("pk-private", repr(configured))
        self.assertNotIn("sk-private", repr(configured))

        full = agent_observability_status(
            {
                "LANGFUSE_PUBLIC_KEY": "pk-private",
                "LANGFUSE_SECRET_KEY": "sk-private",
                "DRASIL_AGENT_OBSERVABILITY_CONTENT": "full",
            }
        )
        self.assertTrue(full["captures_content"])
        self.assertEqual(full["privacy_mode"], "full_content")

    def test_agent_turn_exports_aggregate_facts_without_private_io_or_ids(self) -> None:
        client = _FakeClient()
        private_prompt = "Move Project Nightfall under my confidential medical plan"
        private_session_id = "session-private-123"
        private_turn_id = "turn-private-456"

        @observe_agent_turn
        def run_turn(**_kwargs):
            return {
                "provider": "openai-api",
                "model_name": "gpt-5-mini",
                "response": {
                    "mode": "plan",
                    "policy": "proposal",
                    "summary": "Private completion text",
                    "action_proposal": {"task_title": "Project Nightfall"},
                },
            }

        with (
            patch.dict(
                agent_observability.os.environ,
                {"DRASIL_AGENT_OBSERVABILITY_CONTENT": "aggregate"},
            ),
            patch.object(agent_observability, "_get_client", return_value=client),
            patch.object(
                agent_observability,
                "_propagate_attributes_context",
                return_value=nullcontext(),
            ),
        ):
            result = run_turn(
                prompt=private_prompt,
                strategist_session_id=private_session_id,
                strategist_turn_id=private_turn_id,
                staged_node_ids=[11, 12],
            )

        self.assertEqual(result["provider"], "openai-api")
        self.assertEqual(client.starts[0]["as_type"], "agent")
        self.assertEqual(client.starts[0]["trace_context"]["trace_id"], "a" * 32)
        self.assertEqual(client.starts[0]["metadata"]["stagedNodeCount"], 2)
        self.assertTrue(client.observations[0].updates[-1]["metadata"]["hasProposal"])
        exported = repr(client.starts) + repr(client.observations[0].updates)
        for private_value in (
            private_prompt,
            private_session_id,
            private_turn_id,
            "Private completion text",
            "Project Nightfall",
        ):
            self.assertNotIn(private_value, exported)

    def test_full_content_captures_turn_io_and_tracks_pseudonymous_user(self) -> None:
        client = _FakeClient()
        prompt = "Move Project Nightfall under my medical plan"

        @observe_agent_turn
        def run_turn(**_kwargs):
            return {
                "provider": "codex-cli",
                "model_name": "gpt-5.6-sol",
                "response": {
                    "mode": "plan",
                    "summary": "Project Nightfall was added to the proposed plan.",
                },
            }

        with (
            patch.dict(
                agent_observability.os.environ,
                {"DRASIL_AGENT_OBSERVABILITY_CONTENT": "full"},
            ),
            patch.object(agent_observability, "_get_client", return_value=client),
            patch.object(
                agent_observability,
                "_current_user_id",
                return_value="user-pseudonym",
            ),
            patch.object(
                agent_observability,
                "_propagate_attributes_context",
                return_value=nullcontext(),
            ) as propagate,
        ):
            run_turn(
                prompt=prompt,
                strategist_session_id="private-session-id",
                strategist_turn_id="private-turn-id",
                conversation_history=[{"role": "user", "content": "Earlier context"}],
                staged_node_ids=[17],
            )

        self.assertEqual(client.starts[0]["input"]["prompt"], prompt)
        self.assertEqual(
            client.observations[0].updates[-1]["output"]["summary"],
            "Project Nightfall was added to the proposed plan.",
        )
        self.assertEqual(client.starts[0]["metadata"]["captureMode"], "full")
        self.assertEqual(propagate.call_args.kwargs["user_id"], "user-pseudonym")
        self.assertNotEqual(
            propagate.call_args.kwargs["session_id"],
            "private-session-id",
        )

    def test_generation_exports_usage_and_size_but_not_content(self) -> None:
        client = _FakeClient()

        @observe_generation(
            name="openai.responses",
            provider="openai-api",
            request_body_argument="request_body",
        )
        def provider_call(*, request_body: dict) -> dict:
            return {
                "usage": {"input_tokens": 31, "output_tokens": 7},
                "output": "Confidential model response",
            }

        with (
            patch.dict(
                agent_observability.os.environ,
                {"DRASIL_AGENT_OBSERVABILITY_CONTENT": "aggregate"},
            ),
            patch.object(agent_observability, "_get_client", return_value=client),
            patch.object(agent_observability, "_has_active_trace", return_value=True),
        ):
            provider_call(
                request_body={
                    "model": "gpt-5-mini",
                    "input": "Confidential model prompt",
                }
            )

        self.assertEqual(client.starts[0]["as_type"], "generation")
        self.assertEqual(client.starts[0]["model"], "gpt-5-mini")
        update = client.observations[0].updates[-1]
        self.assertEqual(
            update["usage_details"],
            {"input_tokens": 31, "output_tokens": 7},
        )
        exported = repr(client.starts) + repr(client.observations[0].updates)
        self.assertNotIn("Confidential model prompt", exported)
        self.assertNotIn("Confidential model response", exported)

    def test_full_content_captures_generation_and_tool_io(self) -> None:
        client = _FakeClient()

        @observe_generation(
            name="openai.responses",
            provider="openai-api",
            request_body_argument="request_body",
        )
        def provider_call(*, request_body: dict) -> dict:
            return {
                "usage": {"input_tokens": 10, "output_tokens": 4},
                "output": "Full model response",
            }

        @observe_agent_tool
        def tool(name: str, arguments: dict) -> dict:
            return {"task": arguments["task"], "status": "found"}

        with (
            patch.dict(
                agent_observability.os.environ,
                {"DRASIL_AGENT_OBSERVABILITY_CONTENT": "full"},
            ),
            patch.object(agent_observability, "_get_client", return_value=client),
            patch.object(agent_observability, "_has_active_trace", return_value=True),
        ):
            provider_call(
                request_body={
                    "model": "gpt-5-mini",
                    "input": "Full model prompt",
                }
            )
            tool("find_task", {"task": "Project Nightfall"})

        self.assertEqual(client.starts[0]["input"]["input"], "Full model prompt")
        self.assertEqual(
            client.observations[0].updates[-1]["output"]["output"],
            "Full model response",
        )
        self.assertEqual(client.starts[1]["input"]["task"], "Project Nightfall")
        self.assertEqual(
            client.observations[1].updates[-1]["output"],
            {"task": "Project Nightfall", "status": "found"},
        )

    def test_tool_and_error_events_are_bounded_to_categorical_metadata(self) -> None:
        client = _FakeClient()

        @observe_agent_tool
        def tool(name: str, arguments: dict) -> dict:
            return {
                "error": "invalid_scope",
                "message": "Private task title must not leave this process",
                "read_only": True,
            }

        with (
            patch.dict(
                agent_observability.os.environ,
                {"DRASIL_AGENT_OBSERVABILITY_CONTENT": "aggregate"},
            ),
            patch.object(agent_observability, "_get_client", return_value=client),
            patch.object(agent_observability, "_has_active_trace", return_value=True),
        ):
            result = tool(
                "list_tasks",
                {"query": "Project Nightfall", "task_id": "raw-private-id"},
            )
            record_agent_event(
                "rollout.provider_call",
                {
                    "outcome": "failed",
                    "latency_ms": 42,
                    "unsafeProse": "Private task title must not leave this process",
                },
            )

        self.assertEqual(result["error"], "invalid_scope")
        tool_metadata = client.observations[0].updates[-1]["metadata"]
        self.assertEqual(tool_metadata["outcome"], "tool-error")
        self.assertEqual(len(tool_metadata["errorFingerprint"]), 16)
        self.assertEqual(tool_metadata["argumentKeyCount"], 2)
        self.assertNotIn("unsafeProse", client.events[0]["metadata"])
        exported = repr(client.starts) + repr(client.observations[0].updates) + repr(client.events)
        self.assertNotIn("Project Nightfall", exported)
        self.assertNotIn("raw-private-id", exported)
        self.assertNotIn("Private task title", exported)

    def test_exception_detail_is_not_exported_and_original_error_is_raised(self) -> None:
        client = _FakeClient()

        @observe_generation(name="provider", provider="codex-cli")
        def provider_call() -> None:
            raise ValueError("secret provider payload")

        with (
            patch.object(agent_observability, "_get_client", return_value=client),
            patch.object(agent_observability, "_has_active_trace", return_value=True),
            self.assertRaisesRegex(ValueError, "secret provider payload"),
        ):
            provider_call()

        exported = repr(client.starts) + repr(client.observations[0].updates)
        self.assertIn("ValueError", exported)
        self.assertNotIn("secret provider payload", exported)

    def test_sdk_failure_does_not_block_the_agent_operation(self) -> None:
        @observe_agent_tool
        def tool(name: str, arguments: dict) -> dict:
            return {"ok": True, "name": name, "argument_count": len(arguments)}

        with (
            patch.object(
                agent_observability,
                "_get_client",
                return_value=_FailingClient(),
            ),
            patch.object(agent_observability, "_has_active_trace", return_value=True),
        ):
            result = tool("list_tasks", {"category": "private"})

        self.assertEqual(result, {"ok": True, "name": "list_tasks", "argument_count": 1})

    def test_root_trace_activates_nested_generations_tools_and_events(self) -> None:
        client = _FakeClient()

        @observe_generation(name="provider", provider="codex-cli", prompt_argument="prompt")
        def provider_call(*, prompt: str) -> str:
            return f"response to {prompt}"

        @observe_agent_tool
        def tool(name: str, arguments: dict) -> dict:
            return {"task": arguments["task"]}

        @observe_agent_turn
        def run_turn(**_kwargs):
            provider_call(prompt="nested prompt")
            tool("find_task", {"task": "nested task"})
            record_agent_event("rollout.provider_call", {"outcome": "success"})
            return {"response": {"mode": "plan", "summary": "nested response"}}

        with (
            patch.dict(
                agent_observability.os.environ,
                {"DRASIL_AGENT_OBSERVABILITY_CONTENT": "full"},
            ),
            patch.object(agent_observability, "_get_client", return_value=client),
            patch.object(
                agent_observability,
                "_propagate_attributes_context",
                return_value=nullcontext(),
            ),
        ):
            run_turn(
                prompt="root prompt",
                strategist_session_id="session-id",
                strategist_turn_id="turn-id",
            )

        self.assertEqual(
            [start["as_type"] for start in client.starts],
            ["agent", "generation", "tool"],
        )
        self.assertEqual(client.starts[1]["input"], "nested prompt")
        self.assertEqual(client.starts[2]["input"], {"task": "nested task"})
        self.assertEqual(client.events[0]["name"], "rollout.provider_call")

    def test_child_observations_and_events_do_not_create_orphan_traces(self) -> None:
        client = _NoActiveTraceClient()

        @observe_agent_tool
        def tool(name: str, arguments: dict) -> dict:
            return {"ok": True}

        with patch.object(agent_observability, "_get_client", return_value=client):
            self.assertEqual(tool("list_tasks", {}), {"ok": True})
            record_agent_event("rollout.provider_call", {"outcome": "success"})

        self.assertEqual(client.starts, [])
        self.assertEqual(client.events, [])


if __name__ == "__main__":
    unittest.main()
