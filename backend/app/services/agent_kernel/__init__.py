"""Strategist Agent Kernel runtime package."""

def __getattr__(name):
    # Importing contracts must not initialize the execution runtime.
    if name not in __all__:
        raise AttributeError(name)
    from importlib import import_module
    module = "session_memory" if name == "build_session_memory" else "runtime"
    value = getattr(import_module(f"{__name__}.{module}"), name)
    globals()[name] = value
    return value

__all__ = [
    "append_kernel_event",
    "build_session_memory",
    "run_agent_kernel_turn",
    "strategist_agent_kernel_enabled",
]
