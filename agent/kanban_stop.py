"""Turn-end guard for kanban workers.

Kanban workers must end with ``kanban_complete`` or ``kanban_block``. Models
(especially GLM / Qwen families) sometimes narrate the next step
("Let me write the report now") and stop with ``finish_reason=stop`` and no
tool calls. Hermes treats that as a clean exit → ``rc=0`` → dispatcher
``protocol_violation``.

This module is policy-only: when a kanban worker tries to finish without a
terminal board tool, return a bounded synthetic nudge so the conversation
loop continues instead of exiting.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Optional

from agent.message_sanitization import coalesce_tool_call_id


_TERMINAL_KANBAN_TOOLS = frozenset({"kanban_complete", "kanban_block"})

_DEFAULT_MAX_ATTEMPTS = 2


def kanban_stop_nudge_enabled() -> bool:
    """Return whether the kanban stop-guard is active for this process.

    On when ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), unless
    ``HERMES_KANBAN_STOP_NUDGE`` explicitly disables it.
    """
    env = os.environ.get("HERMES_KANBAN_STOP_NUDGE")
    if env is not None and env.strip().lower() in {"0", "false", "no", "off"}:
        return False
    task = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return bool(task)


def _tool_call_name(tc: Any) -> str:
    if isinstance(tc, dict):
        fn = tc.get("function")
        if isinstance(fn, dict):
            return str(fn.get("name") or "")
        return str(tc.get("name") or "")
    fn = getattr(tc, "function", None)
    if fn is not None:
        return str(getattr(fn, "name", "") or "")
    return str(getattr(tc, "name", "") or "")


def session_called_kanban_terminal(messages: Iterable[dict] | None) -> bool:
    """True if this conversation already invoked a terminal kanban tool."""
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in msg.get("tool_calls") or []:
                if _tool_call_name(tc) in _TERMINAL_KANBAN_TOOLS:
                    return True
        elif role == "tool":
            name = str(msg.get("name") or "")
            if name in _TERMINAL_KANBAN_TOOLS:
                return True
    return False


def build_kanban_stop_nudge(
    *,
    messages: Iterable[dict] | None = None,
    attempts: int = 0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
    task_id: Optional[str] = None,
) -> Optional[str]:
    """Return a synthetic follow-up when a kanban worker exits without a terminal tool.

    Returns ``None`` when the guard should not fire (not a kanban worker,
    already completed/blocked, or nudge budget exhausted).
    """
    if not kanban_stop_nudge_enabled():
        return None
    if attempts >= max_attempts:
        return None
    if session_called_kanban_terminal(messages):
        return None

    tid = (task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip() or "this task"
    return (
        "[System: You are a Hermes kanban worker. A plain-text reply is NOT a "
        "terminal state for the board.\n\n"
        f"Task `{tid}` is still `running`. Ending now without a board tool "
        "causes a protocol violation (clean exit with no "
        "`kanban_complete` / `kanban_block`).\n\n"
        "Do this immediately in your next response — do not narrate intent:\n"
        "1. Finish any remaining deliverable (write the required file(s) now).\n"
        "2. Call `kanban_complete(summary=..., artifacts=[...])` if the work "
        "is done, OR `kanban_block(reason=...)` if you are blocked.\n\n"
        "Never end a turn with only a promise of future action. Repeated "
        "protocol violations will block this task and require manual intervention.]"
    )


def successful_terminal_in_round(
    tool_calls: Iterable[Any] | None,
    result_messages: Iterable[dict] | None,
    task_id: Optional[str],
    run_id: Optional[Any],
) -> Optional[str]:
    """Match a fresh, correlated successful terminal result for THIS worker+run.

    Used by the tool-round loop to end the worker's turn deterministically
    right after ``kanban_complete`` / ``kanban_block`` returns success on the
    task id and run id the dispatcher pinned in env (``HERMES_KANBAN_TASK`` /
    ``HERMES_KANBAN_RUN_ID``). Returning ``None`` means "keep looping":

    - The current batch has no matching successful terminal tool result.
    - The result belongs to a different task / run (i.e. historical or
      inherited context) and must not end this round.
    - The result is malformed, rejected (``ok`` != True), or otherwise
      ambiguous — safer to keep the model in the loop.

    Only ``result_messages`` (the tool results appended by the current
    ``_execute_tool_calls`` invocation) are inspected. Callers must not pass
    a wider window: historical successful terminals should not fire the
    guard again.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    if isinstance(run_id, bool) or not isinstance(run_id, (int, str)):
        return None
    task_id = task_id.strip()
    run_id_str = str(run_id).strip()
    if not run_id_str:
        return None

    calls: dict[str, str] = {}
    duplicate_ids: set[str] = set()
    for call in tool_calls or ():
        call_id = coalesce_tool_call_id(call)
        if not call_id:
            continue
        if call_id in calls:
            duplicate_ids.add(call_id)
        calls[call_id] = _tool_call_name(call)

    for message in result_messages or ():
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = message.get("tool_call_id")
        if not isinstance(call_id, str) or call_id in duplicate_ids:
            continue
        name = calls.get(call_id)
        if name not in _TERMINAL_KANBAN_TOOLS:
            continue
        if message.get("name") is not None and message["name"] != name:
            continue
        result = message.get("content")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                continue
        if not isinstance(result, dict) or result.get("ok") is not True:
            continue
        actual_run = result.get("run_id")
        if isinstance(actual_run, bool) or not isinstance(actual_run, (int, str)):
            continue
        if result.get("task_id") == task_id and str(actual_run) == run_id_str:
            return name
    return None


__all__ = [
    "build_kanban_stop_nudge",
    "kanban_stop_nudge_enabled",
    "session_called_kanban_terminal",
    "successful_terminal_in_round",
]
