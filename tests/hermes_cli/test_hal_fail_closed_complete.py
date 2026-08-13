"""HAL fail-closed complete gate (HEL-4156 follow-up).

``complete_task`` must refuse done when HAL cannot close with honest cost.
Marked ``real_hal_gate`` so root conftest does not soft-patch the require path.
"""

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
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _p: True)
    # Claim must not open a live Supabase row; seed ledger id in the tests.
    monkeypatch.setattr(kb, "_best_effort_action_ledger_open", lambda *a, **k: None)
    kb.init_db()
    return home


@pytest.mark.real_hal_gate
def test_complete_blocks_when_hal_close_refuses_cost(board, monkeypatch):
    closes = []

    def fake_close(ledger_id, **kwargs):
        closes.append({"id": ledger_id, **kwargs})
        from hermes_cli.action_ledger import ActionLedgerIncompleteError

        raise ActionLedgerIncompleteError("no cost evidence")

    import hermes_cli.action_ledger as al

    monkeypatch.setattr(al, "close_action_ledger", fake_close)
    monkeypatch.setattr(al, "require_cost_on_complete", lambda: True)
    monkeypatch.setattr(al, "service_role_configured", lambda: True)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Gate test HEL-4156",
            body="must not complete without cost",
            assignee="cole-espinoza",
        )
        claimed = kb.claim_task(conn, tid, claimer="dispatcher")
        assert claimed is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET action_ledger_id = ? WHERE id = ?",
                ("ledger-uuid-gate", tid),
            )
        with pytest.raises(kb.ActionLedgerCloseError) as ei:
            kb.complete_task(conn, tid, summary="done without cost")
        assert "cost" in ei.value.reason.lower() or "no cost" in str(ei.value).lower()
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status != "done"
        events = [
            e for e in kb.list_events(conn, tid) if e.kind == "completion_blocked_hal"
        ]
        assert len(events) == 1
        assert closes and closes[0]["id"] == "ledger-uuid-gate"


@pytest.mark.real_hal_gate
def test_complete_succeeds_when_hal_close_ok(board, monkeypatch):
    closed = []

    def fake_close(ledger_id, **kwargs):
        closed.append({"id": ledger_id, **kwargs})
        return ledger_id

    import hermes_cli.action_ledger as al

    monkeypatch.setattr(al, "close_action_ledger", fake_close)
    monkeypatch.setattr(al, "require_cost_on_complete", lambda: True)
    monkeypatch.setattr(al, "service_role_configured", lambda: True)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="HAL ok HEL-4156",
            assignee="cole-espinoza",
        )
        assert kb.claim_task(conn, tid, claimer="dispatcher") is not None
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET action_ledger_id = ? WHERE id = ?",
                ("ledger-ok", tid),
            )
        assert kb.complete_task(
            conn,
            tid,
            summary="done",
            metadata={"cost_usd": 0.05, "session_id": "s-ok"},
        )
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "done"
        assert closed and closed[0]["cost_usd"] == 0.05
        kinds = {e.kind for e in kb.list_events(conn, tid)}
        assert "action_ledger_closed" in kinds
