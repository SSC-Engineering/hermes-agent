"""Tests for the kanban worker turn-end stop guard."""

from __future__ import annotations

import pytest

from agent.kanban_stop import (
    build_kanban_stop_nudge,
    kanban_stop_nudge_enabled,
    session_called_kanban_terminal,
    successful_terminal_in_round,
)


@pytest.fixture
def clear_kanban_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_STOP_NUDGE"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch






def test_env_can_disable(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    clear_kanban_env.setenv("HERMES_KANBAN_STOP_NUDGE", "0")
    assert kanban_stop_nudge_enabled() is False
    assert build_kanban_stop_nudge(messages=[]) is None


def test_nudge_when_no_terminal_tool(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_46be8aa5")
    messages = [
        {"role": "user", "content": "work kanban task"},
        {
            "role": "assistant",
            "content": "Let me write the comprehensive recipe.",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_heartbeat", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_heartbeat", "tool_call_id": "1", "content": "ok"},
    ]
    nudge = build_kanban_stop_nudge(messages=messages, attempts=0)
    assert nudge is not None
    assert "kanban_complete" in nudge
    assert "kanban_block" in nudge
    assert "t_46be8aa5" in nudge
    assert "protocol violation" in nudge.lower() or "protocol" in nudge.lower()


def test_no_nudge_after_kanban_complete(clear_kanban_env):
    clear_kanban_env.setenv("HERMES_KANBAN_TASK", "t_abc")
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "kanban_complete", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "name": "kanban_complete", "tool_call_id": "1", "content": "done"},
    ]
    assert session_called_kanban_terminal(messages) is True
    assert build_kanban_stop_nudge(messages=messages) is None






# ── Integration: agent nudge + dispatcher bounded retry ──────────────
# These tests verify the two layers compose correctly: the agent-side
# nudge fires first (up to 2 attempts), and if the worker still exits
# without a terminal call, the dispatcher's bounded retry (streak of 3)
# handles it.  See also tests/hermes_cli/test_kanban_core_functionality.py
# for the dispatcher-side streak tests.


# ── H1 / MCP-INC-036: successful_terminal_in_round matcher ───────────
#
# The matcher decides whether the current tool round produced a persisted,
# correlated success for THIS worker's task+run. When it fires, the
# conversation loop breaks before the next provider call.

import json as _json
from types import SimpleNamespace as _NS

_TERMINALS = ("kanban_complete", "kanban_block")


def _call(name: str = "kanban_complete", call_id: str | None = "call-1"):
    """Chat-Completions-style tool call object (attribute access)."""
    return _NS(id=call_id, function=_NS(name=name, arguments="{}"))


def _result(
    content=None,
    name: str = "kanban_complete",
    call_id: str = "call-1",
) -> dict:
    if content is None:
        content = {"ok": True, "task_id": "t_current", "run_id": 42}
    return {
        "role": "tool",
        "name": name,
        "tool_call_id": call_id,
        "content": content,
    }


@pytest.mark.parametrize("name", _TERMINALS)
@pytest.mark.parametrize("serialized", [False, True])
def test_matcher_accepts_dict_and_json_for_each_terminal(name, serialized):
    payload = {"ok": True, "task_id": "t_current", "run_id": "42"}
    if serialized:
        payload = _json.dumps(payload)
    assert successful_terminal_in_round(
        [_call(name)], [_result(payload, name)], "t_current", "42"
    ) == name


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        [],
        {"ok": 1, "task_id": "t_current", "run_id": 42},
        {"ok": False, "task_id": "t_current", "run_id": 42},
        {"ok": True, "task_id": "other", "run_id": 42},
        {"ok": True, "task_id": "t_current", "run_id": 43},
        {"ok": True, "task_id": "t_current", "run_id": True},
        {"ok": True, "task_id": "t_current"},
    ],
)
def test_matcher_rejects_malformed_or_mismatched_result(payload):
    assert successful_terminal_in_round(
        [_call()], [_result(payload)], "t_current", "42"
    ) is None


@pytest.mark.parametrize(
    "task,run",
    [
        (None, "42"),
        ("", "42"),
        ("t_current", None),
        ("t_current", ""),
        ("t_current", True),
    ],
)
def test_matcher_requires_valid_worker_binding(task, run):
    assert successful_terminal_in_round([_call()], [_result()], task, run) is None


def test_matcher_uniqueness_and_correlation_rules():
    tc_dict = {"id": "call-1", "function": {"name": "kanban_complete"}}
    # Dict-style call still correlates.
    assert successful_terminal_in_round(
        [tc_dict], [_result()], "t_current", "42"
    ) == "kanban_complete"
    # Unpaired tool_call_id must not match.
    assert successful_terminal_in_round(
        [tc_dict], [_result(call_id="other")], "t_current", "42"
    ) is None
    # Mismatched tool `name` on the result rejects (defensive against
    # providers that stamp the wrong name).
    assert successful_terminal_in_round(
        [tc_dict], [_result(name="kanban_block")], "t_current", "42"
    ) is None
    # Missing call id on the assistant side cannot pair.
    assert successful_terminal_in_round(
        [_call(call_id=None)], [_result()], "t_current", "42"
    ) is None
    # Duplicate call ids in the same assistant batch: ambiguous → reject.
    assert successful_terminal_in_round(
        [tc_dict, tc_dict], [_result()], "t_current", "42"
    ) is None
    # A plain assistant message is not a tool result.
    assert successful_terminal_in_round(
        [tc_dict],
        [{"role": "assistant", "content": "completed"}],
        "t_current",
        "42",
    ) is None


def test_matcher_ignores_historical_results_outside_the_round():
    # The loop only passes messages[result_start:] — historical results
    # (from earlier rounds) are simply never in the window. Simulate that
    # constraint by passing an empty result window.
    assert successful_terminal_in_round([_call()], [], "t_current", "42") is None


# ── Integration: the conversation-loop guard breaks before next call ─
#
# We can't run the full AIAgent loop hermetically, but we CAN exercise the
# same helper the loop uses on the exact message shape the loop passes in.
# That covers the correctness contract; the guard's placement in
# ``conversation_loop.py`` is a one-line integration point (see the comment
# tagged "H1 / MCP-INC-036" there).


def test_matcher_end_to_end_with_realistic_result_payload():
    """The kanban_complete tool returns a JSON string with these fields;
    the loop passes the persisted message unchanged."""
    payload_json = _json.dumps({
        "ok": True,
        "task_id": "t_3c29fed7",
        "run_id": 86,
        "artifacts": ["PR#123"],
        "summary": "done",
    })
    messages = [_result(payload_json, "kanban_complete", "call-abc")]
    tool_calls = [_call("kanban_complete", "call-abc")]
    # Numeric run id from env is stringified by the dispatcher; the
    # matcher must accept both "86" and 86.
    assert successful_terminal_in_round(
        tool_calls, messages, "t_3c29fed7", "86"
    ) == "kanban_complete"
    assert successful_terminal_in_round(
        tool_calls, messages, "t_3c29fed7", 86
    ) == "kanban_complete"




