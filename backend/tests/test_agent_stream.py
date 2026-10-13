import asyncio
import io
import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from app.services.agent_stream import PublicAnswerStream, read_responses_stream, stream_progress, emit_progress


class AgentStreamTests(unittest.TestCase):
    def test_login_failure_recovery_does_not_invent_a_security_setting_cause(self):
        from app.services.sandbox_guest import login_failure_message
        self.assertIn("timed out", login_failure_message(TimeoutError()))
        self.assertNotIn("device-code", login_failure_message(RuntimeError("network failed")))
        self.assertNotIn("network failed", login_failure_message(RuntimeError("network failed")))

    def test_saved_summary_contains_only_public_reasoning_and_is_request_scoped(self):
        from app.services.agent_stream import public_reasoning_summary
        with stream_progress(lambda event: None, threading.Event()):
            emit_progress("activity", "Opening a tool")
            emit_progress("reasoning", "Checking constraints. ")
            emit_progress("answer", "Final answer")
            emit_progress("reasoning", "Comparing times.")
            self.assertEqual(public_reasoning_summary(), "Checking constraints. Comparing times.")
        self.assertEqual(public_reasoning_summary(), "")

    def test_structured_answer_stream_never_exposes_nested_fields(self):
        events = []
        source = json.dumps({"private": {"summary": "not public"}, "summary": 'Hello "world" 🙂', "proposal": {"secret": "hidden"}})
        with stream_progress(events.append, threading.Event()):
            reader = PublicAnswerStream()
            for char in source:
                reader.append(char)
        self.assertEqual(events[-1], {"kind": "answer", "text": 'Hello "world" 🙂'})
        self.assertNotIn("not public", str(events))
        self.assertNotIn("hidden", str(events))

    def test_completed_response_and_public_reasoning(self):
        events = []
        final = {"id": "r1", "output": []}
        wire = [
            {"type": "response.reasoning_text.delta", "delta": "private"},
            {"type": "response.reasoning_summary_text.delta", "delta": "Checking your calendar."},
            {"type": "response.completed", "response": final},
        ]
        source = io.BytesIO(''.join('data: ' + json.dumps(item) + '\n\n' for item in wire).encode())
        with stream_progress(events.append, threading.Event()):
            self.assertEqual(read_responses_stream(source), final)
        self.assertEqual(events, [{"kind": "reasoning", "text": "Checking your calendar."}])

    def test_interrupted_or_failed_stream_is_not_success(self):
        for wire in [b'', b'data: {"type":"response.failed"}\n\n', b'data: {"type":"response.incomplete"}\n\n']:
            with self.subTest(wire=wire), self.assertRaises(HTTPException):
                read_responses_stream(io.BytesIO(wire))

    def test_cancellation_and_context_isolation(self):
        events = []
        stop = threading.Event()
        with stream_progress(events.append, stop):
            emit_progress("activity", "Checking tasks")
            stop.set()
            with self.assertRaises(HTTPException) as error:
                emit_progress("activity", "Should not appear")
            self.assertEqual(error.exception.status_code, 499)
        emit_progress("activity", "Another account's request")
        self.assertEqual(len(events), 1)

    def test_http_stream_yields_progress_before_worker_finishes(self):
        from app.api.ai import stream_strategist_run
        complete = threading.Event()
        def worker(*_):
            emit_progress("reasoning", "Checking constraints.")
            time.sleep(0.1)
            complete.set()
        def payload(*_, **kwargs):
            self.assertFalse(kwargs["ensure_active"])
            return {"run": {"status": "completed" if complete.is_set() else "queued"}}
        async def read():
            chunks = []
            response = stream_strategist_run("run1", None)
            async for chunk in response.body_iterator:
                chunks.append(chunk)
                if "event: progress" in chunk:
                    self.assertFalse(complete.is_set())
            return chunks
        with patch('app.api.ai.Session', MagicMock()), patch('app.api.ai.get_active_engine'), patch('app.api.ai.execute_strategist_run', worker), patch('app.api.ai.build_strategist_run_response_payload', payload):
            chunks = asyncio.run(read())
        self.assertIn('event: update', chunks[0])
        self.assertIn('event: progress', chunks[1])
        self.assertIn('event: done', chunks[-1])


if __name__ == '__main__':
    unittest.main()
