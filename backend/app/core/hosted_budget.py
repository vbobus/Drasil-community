"""Keep hosted AI work inside Vercel's request lifetime."""
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

_deadline: ContextVar[float | None] = ContextVar("hosted_deadline", default=None)


@contextmanager
def request_budget():
    token = _deadline.set(monotonic() + 240)
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_timeout(requested: float) -> float:
    deadline = _deadline.get()
    if deadline is None:
        return requested
    remaining = deadline - monotonic()
    if remaining < 5:
        raise TimeoutError("This request exceeded its time budget. Please try a smaller request.")
    return min(requested, remaining)
