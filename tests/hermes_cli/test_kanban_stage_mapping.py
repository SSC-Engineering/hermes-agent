"""HEL-3123: canonical stage mapping + single current_step_key writer.

Covers:
  * All 8 Overwatch WiM stages have an explicit mapping rule.
  * create → claim → complete write current_step_key in the same txn.
  * block routes to gate_selection (or planning for dependency waits).
  * Replaying the same mutation does not double-write step_transitioned.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_stage_mapping import (
    CANONICAL_STAGES,
    STAGE_LABELS,
    is_canonical_stage,
    map_task_state_to_stage,
)


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-writer")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Pure mapping table
# ---------------------------------------------------------------------------


def test_eight_canonical_stages_match_overwatch_labels():
    assert CANONICAL_STAGES == (
        "intake",
        "classification",
        "gate_selection",
        "planning",
        "execution",
        "verification",
        "release",
        "archive",
    )
    assert set(STAGE_LABELS) == set(CANONICAL_STAGES)
    assert STAGE_LABELS["gate_selection"] == "Gate selection"
    assert STAGE_LABELS["intake"] == "Intake"
    assert STAGE_LABELS["archive"] == "Archive"


@pytest.mark.parametrize(
    "status,kwargs,expected",
    [
        ("ready", {"is_create": True}, "intake"),
        ("triage", {}, "classification"),
        ("blocked", {}, "gate_selection"),
        ("blocked", {"block_kind": "needs_input"}, "gate_selection"),
        ("todo", {}, "planning"),
        ("todo", {"block_kind": "dependency"}, "planning"),
        ("ready", {}, "planning"),
        ("scheduled", {}, "planning"),
        ("running", {}, "execution"),
        ("review", {}, "verification"),
        ("done", {}, "release"),
        ("done", {"run_outcome": "completed"}, "release"),
        ("archived", {}, "archive"),
        (None, {}, None),
        ("", {}, None),
        ("not-a-status", {}, None),
    ],
)
def test_map_task_state_to_stage_rules(status, kwargs, expected):
    assert map_task_state_to_stage(status, **kwargs) == expected
    if expected is not None:
        assert is_canonical_stage(expected)


def test_every_valid_status_has_a_mapping_rule():
    """Every VALID_STATUSES member must resolve to a canonical stage."""
    for status in sorted(kb.VALID_STATUSES):
        stage = map_task_state_to_stage(status, is_create=(status == "ready"))
        # ready without is_create is planning; with is_create is intake.
        # Either way it must not be None.
        if status == "ready":
            assert map_task_state_to_stage("ready") == "planning"
            assert map_task_state_to_stage("ready", is_create=True) == "intake"
        else:
            assert stage is not None, f"no mapping for status={status!r}"
            assert stage in CANONICAL_STAGES


# ---------------------------------------------------------------------------
# Writer at mutation boundaries
# ---------------------------------------------------------------------------


def _step_events(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT kind, payload, idempotency_key FROM task_events "
        "WHERE task_id = ? AND kind = 'step_transitioned' ORDER BY id",
        (task_id,),
    ).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload"]) if r["payload"] else {}
        out.append(
            {
                "kind": r["kind"],
                "payload": payload,
                "idempotency_key": r["idempotency_key"],
            }
        )
    return out


def test_create_claim_complete_writes_current_step_key(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stage-path", assignee="worker-a")
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "ready"
        assert task.current_step_key == "intake"

        claimed = kb.claim_task(conn, tid, claimer="worker-a@host")
        assert claimed is not None
        assert claimed.status == "running"
        assert claimed.current_step_key == "execution"

        assert kb.complete_task(conn, tid, summary="done") is True
        done = kb.get_task(conn, tid)
        assert done is not None
        assert done.status == "done"
        assert done.current_step_key == "release"

        steps = _step_events(conn, tid)
        keys = [s["payload"]["to_step"] for s in steps]
        assert keys == ["intake", "execution", "release"]
        for s in steps:
            assert s["payload"]["owner_credential"]
            assert s["payload"]["trigger"]
            assert s["payload"]["timestamp"]
            assert s["idempotency_key"]
            assert s["idempotency_key"].startswith("step:")


def test_block_writes_gate_selection(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="needs-human", assignee="worker-a")
        kb.claim_task(conn, tid, claimer="worker-a@host")
        assert kb.block_task(
            conn, tid, reason="need product decision", kind="needs_input"
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"
        assert task.current_step_key == "gate_selection"

        steps = _step_events(conn, tid)
        assert steps[-1]["payload"]["to_step"] == "gate_selection"
        assert steps[-1]["payload"]["block_kind"] == "needs_input"


def test_dependency_block_stays_planning(kanban_home):
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker-a")
        child = kb.create_task(
            conn, title="child", assignee="worker-a", parents=[parent]
        )
        # Child starts todo (parent not done) → planning
        child_task = kb.get_task(conn, child)
        assert child_task is not None
        assert child_task.status == "todo"
        assert child_task.current_step_key == "planning"

        # Promote parent path: complete parent so child becomes ready, claim, then dependency-block
        kb.complete_task(conn, parent, summary="parent done")
        child_ready = kb.get_task(conn, child)
        assert child_ready is not None
        assert child_ready.status == "ready"
        # After promote from todo→ready, stage stays planning (not intake)
        assert child_ready.current_step_key == "planning"

        kb.claim_task(conn, child, claimer="worker-a@host")
        assert kb.block_task(
            conn, child, reason="waiting on sibling", kind="dependency"
        )
        blocked = kb.get_task(conn, child)
        assert blocked is not None
        assert blocked.status == "todo"
        assert blocked.current_step_key == "planning"


def test_step_transition_idempotent_on_same_stage(kanban_home):
    """Re-writing the same stage must not emit a second step_transitioned."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="idem", assignee="worker-a")
        before = _step_events(conn, tid)
        assert len(before) == 1
        assert before[0]["payload"]["to_step"] == "intake"

        # Force-call writer again with same status/stage — no new event.
        kb._write_current_step_key(
            conn,
            tid,
            status="ready",
            trigger="created",
            is_create=True,
            mutation_idempotency_key=f"created:{tid}",
        )
        after = _step_events(conn, tid)
        assert len(after) == 1

        # Unique-index safety: even if stage somehow differed in payload
        # but same idempotency key, INSERT OR IGNORE keeps one row.
        kb._write_current_step_key(
            conn,
            tid,
            status="running",
            trigger="claimed",
            mutation_idempotency_key="forced-same-key",
        )
        mid = _step_events(conn, tid)
        assert len(mid) == 2
        kb._write_current_step_key(
            conn,
            tid,
            status="done",
            trigger="completed",
            mutation_idempotency_key="forced-same-key",  # same key, different stage attempt
        )
        # Stage column may move, but event row count for that key stays 1.
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE idempotency_key = ?",
            ("step:forced-same-key:release",),
        ).fetchone()
        # second call used stage=release with that key — at most one row
        assert rows["n"] <= 1
        # original forced-same-key:execution still one
        rows2 = conn.execute(
            "SELECT COUNT(*) AS n FROM task_events "
            "WHERE idempotency_key LIKE 'step:forced-same-key:%'",
        ).fetchone()
        assert rows2["n"] == 2  # execution + release are different keys (key includes stage)


def test_archive_writes_archive_stage(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="to-archive", assignee="worker-a")
        assert kb.archive_task(conn, tid)
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "archived"
        assert task.current_step_key == "archive"


def test_triage_create_maps_to_classification(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="needs-spec", assignee="specifier", triage=True
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "triage"
        assert task.current_step_key == "classification"
