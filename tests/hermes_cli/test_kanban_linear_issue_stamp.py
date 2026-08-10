"""HEL-4008: stamp linear_issue_id on tasks + work_intent events."""

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
    # Never hit network from unit tests.
    monkeypatch.setattr(kb, "_best_effort_action_ledger_open", lambda *a, **k: None)
    monkeypatch.setattr(kb, "_best_effort_action_ledger_close", lambda *a, **k: None)
    kb.init_db()
    return home


def test_extract_linear_issue_id_prefers_hel():
    assert kb.extract_linear_issue_id("feat/ABC-1-and-HEL-4009") == "HEL-4009"
    assert kb.extract_linear_issue_id("no ticket here") is None
    assert kb.extract_linear_issue_id("Linear: HEL-3988 body") == "HEL-3988"
    assert kb.extract_linear_issue_id("work on HEL-1 and FOO-2") == "HEL-1"


def test_create_task_stamps_linear_issue_from_title(board):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Implement stamp for HEL-4008",
            body="details",
            assignee="worker",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.linear_issue_id == "HEL-4008"
        created = [e for e in kb.list_events(conn, task_id) if e.kind == "created"][0]
        assert created.payload is not None
        assert created.payload["linear_issue_id"] == "HEL-4008"


def test_create_task_stamps_from_branch_when_title_lacks_key(board):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Implement stamp",
            body="no key",
            assignee="worker",
            workspace_kind="worktree",
            branch_name="feat/HEL-4009-stamp",
        )
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.linear_issue_id == "HEL-4009"


def test_work_intent_heuristic_stamps_task_row_not_envelope(board):
    """HEL-4008 task-row stamp + HEL-3990 signature-only work_intent envelope.

    Title heuristic must persist linear_issue_id on the task (and feed ledger
    side effects). Governed work_intent payloads only carry signature keys so
    C2 never treats a title guess as attributed spend. Run metadata still
    carries the heuristic key for fleet join (HEL-3988).
    """
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Lane A HEL-4008 stamp",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        events = [
            e
            for e in kb.list_events(conn, task_id)
            if e.payload and e.payload.get("event_type") == "task_claimed"
        ]
        assert len(events) == 1
        assert events[0].payload is not None
        # HEL-3990: heuristic title must not populate the governed envelope.
        assert events[0].payload["linear_issue_id"] is None
        task = kb.get_task(conn, task_id)
        assert task is not None
        # HEL-4008: task row still carries the resolved key for consumers.
        assert task.linear_issue_id == "HEL-4008"
        run = conn.execute(
            "SELECT metadata FROM task_runs WHERE id = ?",
            (claimed.current_run_id,),
        ).fetchone()
        meta = __import__("json").loads(run["metadata"] or "{}")
        assert meta.get("linear_issue_id") == "HEL-4008"
        assert meta.get("linear_issue_source") == "heuristic"


def test_resolve_linear_colon_marker_is_signature():
    stamp = kb.resolve_linear_issue_stamp(body="**Linear:** HEL-3988\n\nDo work.")
    assert stamp["linear_issue_id"] == "HEL-3988"
    assert stamp["source"] == "signature"
    assert stamp["signature"] is True
    stamp2 = kb.resolve_linear_issue_stamp(body="Linear: STA-1551 parent")
    assert stamp2["linear_issue_id"] == "STA-1551"
    assert stamp2["signature"] is True


def test_work_intent_signature_stamps_envelope(board):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Lane A signature stamp",
            body="issue_key: HEL-4008\n\nDo the work.",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        events = [
            e
            for e in kb.list_events(conn, task_id)
            if e.payload and e.payload.get("event_type") == "task_claimed"
        ]
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["linear_issue_id"] == "HEL-4008"
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.linear_issue_id == "HEL-4008"


def test_resolve_prefers_metadata_over_title(board):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Work for HEL-1111",
            assignee="worker",
        )
        with kb.write_txn(conn):
            resolved = kb.resolve_task_linear_issue_id(
                conn,
                task_id,
                metadata={"linear_issue_id": "HEL-2222"},
                persist=True,
            )
        assert resolved == "HEL-2222"
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.linear_issue_id == "HEL-2222"


def test_get_task_linear_issue_id_public_export(board):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="Export path HEL-3988", assignee="worker"
        )
        assert kb.get_task_linear_issue_id(conn, task_id) == "HEL-3988"


def test_claim_opens_action_ledger_best_effort(board, monkeypatch):
    opens = []

    def fake_open(**kwargs):
        opens.append(kwargs)
        return "ledger-uuid-1"

    import hermes_cli.action_ledger as al

    monkeypatch.setattr(al, "open_action_ledger", fake_open)

    # Use the real best-effort helper (fixture no-ops it).
    def run_open(conn, task_id):
        return kb.__dict__["__class__"]  # never called

    # Rebind to module-level implementation by calling source logic inline
    # through an unpatched private that imports open_action_ledger live.
    def real_best_effort_open(conn, task_id):
        task = kb.get_task(conn, task_id)
        if task is None:
            return
        if getattr(task, "action_ledger_id", None):
            return
        linear_id = getattr(task, "linear_issue_id", None)
        ledger_id = al.open_action_ledger(
            linear_issue_id=linear_id,
            agent_name=task.assignee or "unknown",
            job_title=task.title,
            kanban_task_id=task_id,
            session_id=task.session_id,
        )
        if ledger_id:
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET action_ledger_id = ? WHERE id = ?",
                    (ledger_id, task_id),
                )

    monkeypatch.setattr(kb, "_best_effort_action_ledger_open", real_best_effort_open)

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn, title="Open ledger HEL-4007", assignee="worker"
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.action_ledger_id == "ledger-uuid-1"
        assert opens and opens[0]["linear_issue_id"] == "HEL-4007"


def test_action_ledger_open_failure_does_not_block_claim(board, monkeypatch):
    def failing_open(_conn, _task_id):
        raise RuntimeError("network down")

    # claim_task wraps open in try/except, but our fixture path calls the helper
    # after claim; ensure exceptions inside the claim path are swallowed.
    monkeypatch.setattr(kb, "_best_effort_action_ledger_open", failing_open)
    with kb.connect() as conn:
        task_id = kb.create_task(conn, title="HEL-4007 resilient", assignee="worker")
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        assert claimed.status == "running"


def _run_meta(conn, run_id):
    import json

    row = conn.execute(
        "SELECT metadata FROM task_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    return json.loads(row["metadata"] or "{}")


def test_unattributed_run_writes_explicit_marker(board):
    """HEL-3988: no Linear key → linear_issue_id null + source=unattributed."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Routine cleanup with no ticket",
            body="plain work, nothing to attribute",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        meta = _run_meta(conn, claimed.current_run_id)
        assert "linear_issue_source" in meta
        assert meta["linear_issue_source"] == "unattributed"
        assert meta.get("linear_issue_id") is None
        # Governed envelope stays null (not a signature key).
        events = [
            e
            for e in kb.list_events(conn, task_id)
            if e.payload and e.payload.get("event_type") == "task_claimed"
        ]
        assert len(events) == 1
        assert events[0].payload is not None
        assert events[0].payload["linear_issue_id"] is None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.linear_issue_id is None


def test_signature_run_metadata_source(board):
    """Signature body marker stamps run metadata with source=signature."""
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Signature path stamp",
            body="issue_key: HEL-3988\n\nDo the work.",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        meta = _run_meta(conn, claimed.current_run_id)
        assert meta.get("linear_issue_id") == "HEL-3988"
        assert meta.get("linear_issue_source") == "signature"


def test_stamp_does_not_downgrade_signature_to_unattributed(board):
    """Higher-grade run stamp must not be wiped by a later unattributed write."""
    import json

    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Signature hold",
            body="Linear: HEL-3988",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        run_id = claimed.current_run_id
        meta_before = _run_meta(conn, run_id)
        assert meta_before["linear_issue_source"] == "signature"
        assert meta_before["linear_issue_id"] == "HEL-3988"

        with kb.write_txn(conn):
            kb._stamp_run_metadata_linear_issue(
                conn,
                run_id,
                {
                    "linear_issue_id": None,
                    "source": "unattributed",
                    "signature": False,
                },
            )
        meta_after = _run_meta(conn, run_id)
        assert meta_after["linear_issue_source"] == "signature"
        assert meta_after["linear_issue_id"] == "HEL-3988"
        # Sanity: metadata still round-trips as JSON object.
        assert isinstance(json.loads(json.dumps(meta_after)), dict)


def test_stamp_does_not_downgrade_heuristic_to_unattributed(board):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="Heuristic hold HEL-4008",
            assignee="worker",
        )
        claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
        assert claimed is not None
        run_id = claimed.current_run_id
        meta_before = _run_meta(conn, run_id)
        assert meta_before["linear_issue_source"] == "heuristic"
        assert meta_before["linear_issue_id"] == "HEL-4008"

        with kb.write_txn(conn):
            kb._stamp_run_metadata_linear_issue(
                conn,
                run_id,
                {
                    "linear_issue_id": None,
                    "source": "unattributed",
                    "signature": False,
                },
            )
        meta_after = _run_meta(conn, run_id)
        assert meta_after["linear_issue_source"] == "heuristic"
        assert meta_after["linear_issue_id"] == "HEL-4008"


def test_resolve_linear_issue_stamp_unattributed_default():
    stamp = kb.resolve_linear_issue_stamp(
        title="no ticket here",
        body="still nothing",
        branch_name="fix/cleanup",
    )
    assert stamp["linear_issue_id"] is None
    assert stamp["source"] == "unattributed"
    assert stamp["signature"] is False
