"""Behavioral contracts for correlated dispatcher lifecycle events."""
from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _profile: True)
    kb.init_db()
    return home


def _typed_events(conn, task_id):
    events = [
        event
        for event in kb.list_events(conn, task_id)
        if event.payload
        and event.payload.get("event_type") in {
            "task_claimed",
            "worker_spawn_requested",
            "worker_spawned",
            "worker_started",
            "heartbeat",
            "worker_exited",
        }
        and event.payload.get("schema_version") == 1
    ]
    assert all(event.payload is not None for event in events)
    return events


def test_successful_work_intent_is_reconstructable_from_typed_events(board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="success", assignee="worker")
        result = kb.dispatch_once(
            conn,
            spawn_fn=lambda _task, _workspace: 4321,
            max_spawn=1,
        )
        claimed = kb.get_task(conn, task_id)
        assert result.spawned and claimed is not None
        assert kb.heartbeat_worker(
            conn, task_id, expected_run_id=claimed.current_run_id,
        )
        assert kb.complete_task(
            conn, task_id, summary="finished", expected_run_id=claimed.current_run_id,
        )
        events = _typed_events(conn, task_id)
        final_task = kb.get_task(conn, task_id)

    assert [event.payload["event_type"] for event in events] == [
        "task_claimed",
        "worker_spawn_requested",
        "worker_spawned",
        "worker_started",
        "heartbeat",
        "worker_exited",
    ]
    intent_ids = {event.payload["work_intent_id"] for event in events}
    assert intent_ids == {task_id}
    assert events[-1].payload["to_state"] == "completed"
    assert events[-1].payload["reason_code"] == "completed"
    assert final_task is not None
    assert final_task.current_step_key is None

    required = {
        "event_id", "occurred_at", "recorded_at", "event_type",
        "source_system", "source_event_id", "work_intent_id", "task_id",
        "run_id", "session_id", "linear_issue_id", "repo", "pr_number",
        "head_sha", "deployment_id", "environment", "deployed_sha",
        "actor_type", "actor_id", "node_id", "from_state", "to_state",
        "reason_code", "causation_event_id", "idempotency_key",
        "evidence_ref", "policy_version", "schema_version",
    }
    assert all(required <= set(event.payload) for event in events)
    serialized = " ".join(str(event.payload) for event in events).lower()
    assert "secret-canary" not in serialized
    assert "raw haa" not in serialized
    assert str(board).lower() not in serialized


def test_failed_spawn_has_one_terminal_worker_exit(board):
    def fail_spawn(_task, _workspace):
        raise RuntimeError("controlled spawn failure")

    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="failure", assignee="worker")
        kb.dispatch_once(conn, spawn_fn=fail_spawn, max_spawn=1, failure_limit=2)
        events = _typed_events(conn, task_id)

    assert [event.payload["event_type"] for event in events] == [
        "task_claimed", "worker_spawn_requested", "worker_exited",
    ]
    terminal = [event for event in events if event.payload["event_type"] == "worker_exited"]
    assert len(terminal) == 1
    assert terminal[0].payload["to_state"] == "spawn_failed"
    assert terminal[0].payload["reason_code"] == "spawn_failed"


def test_duplicate_claim_replay_emits_one_logical_transition(board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="idempotent", assignee="worker")
        first = kb.claim_task(conn, task_id, claimer="dispatcher")
        second = kb.claim_task(conn, task_id, claimer="dispatcher")
        events = _typed_events(conn, task_id)

    assert first is not None
    assert second is None
    claimed = [event for event in events if event.payload["event_type"] == "task_claimed"]
    assert len(claimed) == 1
    assert len({event.payload["idempotency_key"] for event in claimed}) == 1


def test_replayed_source_event_is_ignored_by_unique_idempotency_key(board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="source replay", assignee="worker")
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        key = f"work-intent:{task_id}:run:{claimed.current_run_id}:task_claimed"
        with kb.write_txn(conn):
            kb._append_work_intent_event(
                conn,
                task_id,
                "task_claimed",
                run_id=claimed.current_run_id,
                from_state="ready",
                to_state="running",
                reason_code="dispatch_claim",
                source_event_id=f"run:{claimed.current_run_id}:claim",
                idempotency_key=key,
            )
        events = _typed_events(conn, task_id)

    assert [
        event.payload["event_type"] for event in events
    ] == ["task_claimed"]


def test_claim_without_spawn_is_explicitly_incomplete(board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="claim only", assignee="worker")
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        events = _typed_events(conn, task_id)

    assert claimed is not None
    assert [event.payload["event_type"] for event in events] == ["task_claimed"]


def test_work_intent_stamps_explicit_issue_key_from_body(board):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="implement stamp",
            body="issue_key: HEL-3990\n\nDo the work.",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        events = _typed_events(conn, task_id)
        run = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()

    assert claimed is not None
    assert events[0].payload["linear_issue_id"] == "HEL-3990"
    meta = __import__("json").loads(run["metadata"] or "{}")
    assert meta.get("linear_issue_id") == "HEL-3990"
    assert meta.get("linear_issue_source") == "signature"


def test_work_intent_title_heuristic_does_not_stamp_signature(board):
    """Title heuristic: envelope stays null; run metadata still carries the key."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="HEL-3990 title only",
            body="no explicit marker",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        events = _typed_events(conn, task_id)
        run = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()

    assert claimed is not None
    # HEL-3990: governed envelope stays signature-only.
    assert events[0].payload["linear_issue_id"] is None
    # HEL-3988: task_runs.metadata still gets the resolved key for fleet join.
    meta = __import__("json").loads(run["metadata"] or "{}")
    assert meta.get("linear_issue_id") == "HEL-3990"
    assert meta.get("linear_issue_source") == "heuristic"


def test_work_intent_linear_colon_marker_is_signature(board):
    """Brief-style ``**Linear:** HEL-####`` is signature-grade (HEL-3988)."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="fleet stamp pickup",
            body="**Linear:** HEL-3988\n\nProve live stamp.",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        events = _typed_events(conn, task_id)
        run = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()

    assert claimed is not None
    assert events[0].payload["linear_issue_id"] == "HEL-3988"
    meta = __import__("json").loads(run["metadata"] or "{}")
    assert meta.get("linear_issue_id") == "HEL-3988"
    assert meta.get("linear_issue_source") == "signature"


def test_work_intent_missing_key_is_null_not_sentinel(board):
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="no key", assignee="worker")
        kb.claim_task(conn, task_id, claimer="dispatcher")
        events = _typed_events(conn, task_id)

    assert events[0].payload["linear_issue_id"] is None
    assert "UNKNOWN" not in str(events[0].payload["linear_issue_id"])
    assert events[0].payload["linear_issue_id"] != "unattributed"


def test_work_intent_multi_team_keys_from_marker(board):
    with kb.connect() as conn:
        for key in ("HEL-3990", "STA-1553", "NEX-633", "SIG-12"):
            task_id = kb.create_task(
                conn,
                title=f"work {key}",
                body=f"linear_issue_id: {key}",
                assignee="worker",
            )
            claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
            events = _typed_events(conn, task_id)
            assert events[0].payload["linear_issue_id"] == key
            assert claimed is not None


def test_resolve_linear_issue_stamp_env_and_heuristic_split(board):
    sig = kb.resolve_linear_issue_stamp(
        env={"HERMES_LINEAR_ISSUE_ID": "hel-3991"},
        title="HEL-3990 should not win",
    )
    assert sig == {
        "linear_issue_id": "HEL-3991",
        "source": "signature",
        "signature": True,
    }
    heur = kb.resolve_linear_issue_stamp(title="feat/HEL-3990-issue-key-stamp")
    assert heur["linear_issue_id"] == "HEL-3990"
    assert heur["source"] == "heuristic"
    assert heur["signature"] is False
    bare = kb.resolve_linear_issue_stamp(title="nope", body="still nothing")
    assert bare["linear_issue_id"] is None
    assert bare["source"] == "unattributed"
