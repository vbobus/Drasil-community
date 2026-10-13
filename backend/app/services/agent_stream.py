"""Request-scoped public progress. Never forward raw reasoning or tool arguments."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import threading
from typing import Callable

from fastapi import HTTPException

_sink: ContextVar[Callable[[dict], None] | None] = ContextVar("agent_stream_sink", default=None)
_cancel: ContextVar[threading.Event | None] = ContextVar("agent_stream_cancel", default=None)
_summaries: ContextVar[list[str] | None] = ContextVar("agent_reasoning_summaries", default=None)


@contextmanager
def stream_progress(sink, cancelled):
    sink_token = _sink.set(sink)
    cancel_token = _cancel.set(cancelled)
    summaries_token = _summaries.set([])
    try:
        yield
    finally:
        _sink.reset(sink_token)
        _cancel.reset(cancel_token)
        _summaries.reset(summaries_token)


def public_reasoning_summary() -> str:
    return "".join(_summaries.get() or [])[:48_000].strip()


def check_cancelled():
    flag = _cancel.get()
    if flag is not None and flag.is_set():
        raise HTTPException(499, "Response stopped.")


def emit_progress(kind: str, text: str):
    check_cancelled()
    summaries = _summaries.get()
    if kind == "reasoning" and text and summaries is not None:
        remaining = 48_000 - sum(map(len, summaries))
        if remaining > 0:
            summaries.append(text[:remaining])
    sink = _sink.get()
    if sink is not None and text:
        sink({"kind": kind, "text": text})


class PublicAnswerStream:
    """Extract only the top-level summary from the model's structured JSON."""

    def __init__(self):
        self.buffer = ""
        self.last = ""

    def append(self, delta: str):
        check_cancelled()
        if _sink.get() is None:
            return
        self.buffer = (self.buffer + delta)[:2_000_000]
        decoder = json.JSONDecoder()
        text = self.buffer.lstrip()
        if not text.startswith("{"):
            return
        index = 1
        try:
            while index < len(text):
                while index < len(text) and text[index] in " \r\n\t,":
                    index += 1
                key, index = decoder.raw_decode(text, index)
                while index < len(text) and text[index].isspace():
                    index += 1
                if index >= len(text) or text[index] != ":":
                    return
                index += 1
                while index < len(text) and text[index].isspace():
                    index += 1
                if key == "summary" and index < len(text) and text[index] == '"':
                    tail = text[index:]
                    try:
                        value, _ = decoder.raw_decode(tail)
                    except ValueError:
                        # Trim a partial escape (including a split Unicode escape).
                        value = ""
                        for trim in range(min(7, len(tail))):
                            try:
                                value = json.loads((tail[:-trim] if trim else tail) + '"')
                                break
                            except ValueError:
                                continue
                    if isinstance(value, str) and value != self.last:
                        self.last = value
                        emit_progress("answer", value)
                    return
                _, index = decoder.raw_decode(text, index)
        except (ValueError, IndexError):
            return


def read_responses_stream(response) -> dict:
    """Read through the terminal event; early deltas never imply success."""
    answer = PublicAnswerStream()
    data: list[str] = []
    for raw_line in response:
        check_cancelled()
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            raw = "\n".join(data)
            data.clear()
            if raw == "[DONE]":
                break
            event = json.loads(raw)
            kind = event.get("type")
            if kind == "response.output_text.delta":
                answer.append(event.get("delta", ""))
            elif kind == "response.reasoning_summary_text.delta":
                emit_progress("reasoning", event.get("delta", ""))
            elif kind == "response.completed":
                return event["response"]
            elif kind in {"response.failed", "response.incomplete", "error"}:
                raise HTTPException(502, "The AI response did not complete. Please try again.")
    raise HTTPException(502, "The AI stream disconnected before completing. Please try again.")
