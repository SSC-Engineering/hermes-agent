"""HEL-4131: fail-closed action_ledger sign-off on kanban complete."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb


SIGNOFF_MD = {
    "prompt_tokens": 1200,
    "completion_tokens": 340,
    "cost_usd": 0.0123,
    "cost_status": "estimated",
    "pricing_source": "official_docs_snapshot",
    "llm_model": "x-ai/grok-4.5",
    "signature_md": "Agent: cole\nLinear: HEL-4131\nCost: 0.0123",
}


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in (
        "SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SSC_SUPABASE_HELIOS_AGENTIC_OS_URL",
        "SUPABASE_URL",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return home


def _running_task(conn, **kwargs) -> str:
    tid = kb.create_task(
        conn,
        title=kwargs.get("title", "signoff probe"),
        body=kwargs.get("body", "do the thing HEL-4131"),
        assignee=kwargs.get("assignee", "cole-espinoza"),
        created_by="test",
        initial_status="running",
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='ready', linear_issue_id=? WHERE id=?",
            (kwargs.get("linear_issue_id", "HEL-4131"), tid),
        )
    claimed = kb.claim_task(conn, tid, claimer="test-worker")
    assert claimed is not None
    assert claimed.status == "running"
    return tid


def test_bare_complete_blocked_without_tokens_cost(kanban_home):
    with kb.connect() as conn:
        tid = _running_task(conn)
        with patch.object(kb, "_load_session_usage_row", return_value=None):
            with pytest.raises(kb.MissingLedgerSignoffError) as ei:
                kb.complete_task(conn, tid, summary="done", result="done")
        assert ei.value.missing_fields
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "running"
        kinds = [
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (tid,),
            ).fetchall()
        ]
        assert "completion_blocked_ledger_signoff" in kinds
        assert "completed" not in kinds


def test_complete_succeeds_with_explicit_signoff_metadata(kanban_home):
    with kb.connect() as conn:
        tid = _running_task(conn)
        with patch.object(kb, "_load_session_usage_row", return_value=None):
            with patch.object(kb, "_action_ledger_configured", return_value=False):
                ok = kb.complete_task(
                    conn,
                    tid,
                    summary="finished the work",
                    result="finished the work",
                    metadata=dict(SIGNOFF_MD),
                )
        assert ok is True
        task = kb.get_task(conn, tid)
        assert task.status == "done"
        kinds = {
            r["kind"]
            for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=?",
                (tid,),
            ).fetchall()
        }
        assert "completed" in kinds
        assert "action_ledger_close_skipped_unconfigured" in kinds


def test_session_usage_auto_enriches_cost_and_tokens(kanban_home):
    with kb.connect() as conn:
        tid = _running_task(conn)
        session_row = {
            "id": "sess-1",
            "input_tokens": 500,
            "output_tokens": 100,
            "estimated_cost_usd": 0.0042,
            "actual_cost_usd": None,
            "model": "anthropic/claude-sonnet-4-5",
            "cost_status": "estimated",
            "cost_source": "session",
            "billing_provider": "openrouter",
        }
        with patch.object(kb, "_load_session_usage_row", return_value=session_row):
            with patch.object(kb, "_action_ledger_configured", return_value=False):
                ok = kb.complete_task(
                    conn, tid, summary="auto priced", result="auto priced"
                )
        assert ok is True
        assert kb.get_task(conn, tid).status == "done"


def test_configured_ledger_close_failure_blocks_done(kanban_home):
    with kb.connect() as conn:
        tid = _running_task(conn)
        with patch.object(kb, "_load_session_usage_row", return_value=None):
            with patch.object(kb, "_action_ledger_configured", return_value=True):
                with patch.object(kb, "_best_effort_action_ledger_open"):
                    with patch(
                        "hermes_cli.action_ledger.close_action_ledger",
                        side_effect=Exception("boom"),
                    ):
                        with kb.write_txn(conn):
                            conn.execute(
                                "UPDATE tasks SET action_ledger_id=? WHERE id=?",
                                ("11111111-1111-1111-1111-111111111111", tid),
                            )
                        with pytest.raises(kb.LedgerSignoffFailedError):
                            kb.complete_task(
                                conn,
                                tid,
                                summary="should fail close",
                                result="should fail close",
                                metadata=dict(SIGNOFF_MD),
                            )
        assert kb.get_task(conn, tid).status == "running"
