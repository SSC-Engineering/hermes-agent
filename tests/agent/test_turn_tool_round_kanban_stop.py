"""Current-round terminal acceptance and normal continuation through real tool rounds."""
import copy
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from agent.kanban_stop import successful_terminal_in_round
from agent.turn_tool_round import run_tool_round
import agent.turn_tool_round as round_module

TERMINALS = ("kanban_complete", "kanban_block", "kanban_request_review", "kanban_request_changes")


def call(name="kanban_complete", call_id="call-1"):
    return NS(id=call_id, function=NS(name=name, arguments="{}"))


def result(content=None, name="kanban_complete", call_id="call-1"):
    if content is None:
        content = {"ok": True, "task_id": "t_current", "run_id": 42}
    return {"role": "tool", "name": name, "tool_call_id": call_id, "content": content}


@pytest.mark.parametrize("name", TERMINALS)
@pytest.mark.parametrize("serialized", [False, True])
def test_matching_result_accepts_dict_and_json(name, serialized):
    payload = {"ok": True, "task_id": "t_current", "run_id": "42"}
    if serialized:
        payload = json.dumps(payload)
    assert successful_terminal_in_round([call(name)], [result(payload, name)], "t_current", "42") == name


@pytest.mark.parametrize("payload", ["not-json", "[]", [], {"ok": 1, "task_id": "t_current", "run_id": 42},
    {"ok": False, "task_id": "t_current", "run_id": 42},
    {"ok": True, "task_id": "other", "run_id": 42},
    {"ok": True, "task_id": "t_current", "run_id": 43},
    {"ok": True, "task_id": "t_current", "run_id": True},
    {"ok": True, "task_id": "t_current"}])
def test_bad_result_rejected(payload):
    assert successful_terminal_in_round([call()], [result(payload)], "t_current", "42") is None


@pytest.mark.parametrize("task,run", [(None, "42"), ("", "42"), ("t_current", None), ("t_current", ""), ("t_current", True)])
def test_worker_binding_required(task, run):
    assert successful_terminal_in_round([call()], [result()], task, run) is None


def test_dict_call_and_correlation():
    tc = {"id": "call-1", "function": {"name": "kanban_complete"}}
    assert successful_terminal_in_round([tc], [result()], "t_current", "42") == "kanban_complete"
    assert successful_terminal_in_round([tc], [result(call_id="other")], "t_current", "42") is None
    assert successful_terminal_in_round([tc], [result(name="kanban_block")], "t_current", "42") is None
    assert successful_terminal_in_round([call(call_id=None)], [result()], "t_current", "42") is None
    assert successful_terminal_in_round([tc, tc], [result()], "t_current", "42") is None
    assert successful_terminal_in_round([tc], [{"role": "assistant", "content": "completed"}], "t_current", "42") is None


@pytest.fixture
def exercise(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_current")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setattr(round_module, "validate_tool_calls", lambda *a, **kw: NS(action="ok", result=None, mixed_invalid_batch=False))

    def stage(agent, *, assistant_message, finish_reason, messages):
        return ({"role": "assistant", "content": "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in assistant_message.tool_calls]}, False)
    monkeypatch.setattr(round_module, "stage_tool_call_message", stage)

    def run(results, calls=None, prefix=None, persistence_failure=False, before_failure=False, halt=False):
        calls = calls or [call()]
        messages = copy.deepcopy(prefix or [{"role": "system", "content": "byte-stable system"}, {"role": "user", "content": "work"}])
        original = copy.deepcopy(messages)
        agent = NS(quiet_mode=True, verbose_logging=False, session_id="fixture", log_prefix="", stream_delta_callback=None,
            _deduplicate_tool_calls=lambda x: x, _cap_delegate_task_calls=lambda x: x,
            _flush_messages_to_session_db=Mock(return_value=not before_failure), _emit_interim_assistant_message=Mock(),
            _incremental_persistence_failed=False, _tool_guardrail_halt_decision=None, _touch_activity=Mock(),
            iteration_budget=NS(refund=Mock()), _emit_status=Mock(), _safe_print=Mock(),
            _toolguard_controlled_halt_response=Mock(return_value="guardrail halt"))

        def execute(*args):
            messages.extend(copy.deepcopy(results))
            agent._incremental_persistence_failed = persistence_failure
            if halt:
                agent._tool_guardrail_halt_decision = NS(tool_name="kanban_complete", code="deny")
        agent._execute_tool_calls = Mock(side_effect=execute)

        def compress(*args, **kw):
            return NS(messages=kw["messages"], active_system_prompt=kw["active_system_prompt"],
                conversation_history=kw["conversation_history"], compression_attempts=kw["compression_attempts"],
                final_response=kw["final_response"], turn_exit_reason=kw["turn_exit_reason"], end_turn=False)
        compression = Mock(side_effect=compress)
        monkeypatch.setattr(round_module, "compress_after_tool_results", compression)
        verdict = run_tool_round(agent, assistant_message=NS(tool_calls=calls, content=""), finish_reason="tool_calls",
            messages=messages, conversation_history=[], api_call_count=1, effective_task_id="fixture", user_message="work",
            system_message="byte-stable system", active_system_prompt="byte-stable system", compression_attempts=0,
            max_compression_attempts=2, final_response=None, failed=False, _turn_exit_reason="unknown", truncated_tool_call_retries=0)
        assert messages[:len(original)] == original
        assert verdict.active_system_prompt == "byte-stable system"
        return verdict, agent, compression, messages
    return run


@pytest.mark.parametrize("name", TERMINALS)
def test_real_round_ends_after_success_without_compression(exercise, name):
    verdict, agent, compression, messages = exercise([result(name=name)], [call(name)])
    assert verdict.action == "break"  # caller reaches ordinary finalize_turn
    assert verdict.result is None
    assert verdict._turn_exit_reason == "kanban_worker_done"
    assert name in verdict.final_response and "t_current" in verdict.final_response
    assert verdict.failed is False
    assert agent._session_messages is messages
    compression.assert_not_called()
    agent._flush_messages_to_session_db.assert_called_once()


@pytest.mark.parametrize("payload", ["invalid", "[]", {"ok": False}, {"ok": True, "task_id": "elsewhere", "run_id": 42},
    {"ok": True, "task_id": "t_current", "run_id": 99}])
def test_real_round_rejected_result_continues(exercise, payload):
    verdict, _, compression, _ = exercise([result(payload)])
    assert verdict.action == "continue"
    compression.assert_called_once()


def test_nonworker_keeps_normal_continuation(exercise, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    verdict, _, compression, _ = exercise([result()])
    assert verdict.action == "continue"
    compression.assert_called_once()


def test_historical_success_does_not_end_new_round(exercise):
    prefix = [{"role": "system", "content": "byte-stable system"}, {"role": "user", "content": "work"}, result()]
    verdict, _, compression, _ = exercise([result({"ok": False})], prefix=prefix)
    assert verdict.action == "continue"
    compression.assert_called_once()


def test_mixed_batch_preserves_all_pairs(exercise):
    calls = [call(), call("terminal", "call-2")]
    outputs = [result(), result({"output": "safe"}, "terminal", "call-2")]
    verdict, _, compression, messages = exercise(outputs, calls)
    assert verdict.action == "break"
    assert messages[-2:] == outputs
    assert [tc["id"] for tc in messages[-3]["tool_calls"]] == [m["tool_call_id"] for m in outputs]
    compression.assert_not_called()


@pytest.mark.parametrize("before", [False, True])
def test_persistence_failure_precedes_completion(exercise, before):
    verdict, agent, compression, _ = exercise([result()], persistence_failure=not before, before_failure=before)
    assert verdict.action == "break" and verdict.failed
    assert verdict._turn_exit_reason == "session_persistence_failed"
    assert not verdict.final_response
    compression.assert_not_called()
    if before:
        agent._execute_tool_calls.assert_not_called()


def test_guardrail_precedes_completion(exercise):
    verdict, _, compression, _ = exercise([result()], halt=True)
    assert verdict.action == "break"
    assert verdict._turn_exit_reason == "guardrail_halt"
    assert verdict.final_response == "guardrail halt"
    compression.assert_not_called()
