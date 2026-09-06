"""Session accountability ledger (HEL-6658, proof for HEL-6662).

Every session opens exactly one ``action_ledger`` row at creation and closes it
at end / expiry / failure / abandonment.  Deterministic by construction: the
Supabase transport is a fake in-memory table (no network), time is injected, and
nothing sleeps.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from pathlib import Path

import pytest

from hermes_cli import action_ledger as al
from hermes_state import SessionDB

# A credential *value* that must never reach a ledger row. The row may carry the
# credential's code (an identity), never its secret.
FIXTURE_SECRET = "sk-fixture-DO-NOT-LOG-8f3a9c21b7"
FIXTURE_CREDENTIAL_CODE = "cred-nim-007"

FIXED_NOW = 1_760_000_000.0  # injected clock; no test reads the wall clock


class FakeLedgerTransport:
    """In-memory stand-in for the PostgREST ``action_ledger`` endpoint."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self._seq = 0
        self.reject_session_kind = False

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _filters(path: str) -> dict[str, str]:
        query = path.split("?", 1)[1] if "?" in path else ""
        out: dict[str, str] = {}
        for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True):
            if key in {"select", "limit", "order"}:
                continue
            out[key] = value
        return out

    def _matches(self, row: dict, filters: dict[str, str]) -> bool:
        for column, expr in filters.items():
            if expr == "not.is.null":
                if row.get(column) is None:
                    return False
            elif expr.startswith("eq."):
                if str(row.get(column)) != urllib.parse.unquote(expr[3:]):
                    return False
        return True

    # -- transport -------------------------------------------------------
    def __call__(self, method, path, body=None, prefer=None, extra_headers=None):
        self.requests.append((method, path, body))
        if method == "GET":
            filters = self._filters(path)
            return [
                dict(row)
                for row in self.rows.values()
                if self._matches(row, filters)
            ]
        if method == "POST":
            if self.reject_session_kind and "session_kind" in (body or {}):
                raise al.ActionLedgerError(
                    "action_ledger HTTP 400: {\"code\":\"PGRST204\",\"message\":"
                    "\"Could not find the 'session_kind' column of 'action_ledger' "
                    "in the schema cache\"}"
                )
            self._seq += 1
            row_id = f"row-{self._seq}"
            row = dict(body or {})
            row["id"] = row_id
            row.setdefault("start_ts", "2025-10-09T00:00:00Z")
            self.rows[row_id] = row
            return [row]
        if method == "PATCH":
            filters = self._filters(path)
            updated = []
            for row in self.rows.values():
                if self._matches(row, filters):
                    row.update(body or {})
                    updated.append(dict(row))
            return updated
        raise AssertionError(f"unexpected method {method}")

    # -- assertions ------------------------------------------------------
    def open_rows(self) -> list[dict]:
        return [r for r in self.rows.values() if r.get("status") == "open"]

    def closed_rows(self) -> list[dict]:
        return [r for r in self.rows.values() if r.get("status") == "closed"]

    def only_row(self) -> dict:
        assert len(self.rows) == 1, f"expected exactly one row, got {len(self.rows)}"
        return next(iter(self.rows.values()))


@pytest.fixture
def ledger(monkeypatch):
    """Fake transport + a service role, with no network and no keys.env read."""
    transport = FakeLedgerTransport()
    monkeypatch.setenv("SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET", FIXTURE_SECRET)
    monkeypatch.setenv("SSC_SUPABASE_HELIOS_AGENTIC_OS_URL", "https://example.supabase.co")
    monkeypatch.setenv("HERMES_SESSION_LEDGER", "1")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_CREDENTIAL_ID", raising=False)
    monkeypatch.setattr(al, "_load_keys_env", lambda: None)
    monkeypatch.setattr(al, "_request", transport)
    monkeypatch.setattr(al, "_active_profile_name", lambda: "cole-espinoza")
    monkeypatch.setattr(
        al, "resolve_credential_id", lambda profile: FIXTURE_CREDENTIAL_CODE
    )
    al._TRACKED_SESSIONS.clear()
    yield transport
    al._TRACKED_SESSIONS.clear()


@pytest.fixture
def db(tmp_path, ledger):
    """A real state.db so create_session/end_session run their real paths."""
    database = SessionDB(db_path=tmp_path / "state.db")
    yield database
    try:
        database.close()
    except Exception:
        pass


def _seed_worker_cost(tmp_path, monkeypatch, profile: str, session_id: str) -> None:
    """Write a worker profile state.db row so close reads honest tokens/cost."""
    root = tmp_path / "hermes-root"
    worker_db = root / "profiles" / profile / "state.db"
    worker_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(worker_db))
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, input_tokens INT, "
        "output_tokens TEXT, actual_cost_usd REAL, estimated_cost_usd REAL, "
        "cost_status TEXT)"
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
        (session_id, "qwen3.8-engineer", 1200, 340, 0.0125, None, "actual"),
    )
    con.commit()
    con.close()
    monkeypatch.setattr(al, "_HERMES_ROOT", root)


# ── AC1: starting a session opens its record automatically ───────────────


def test_gateway_session_create_opens_exactly_one_row(db, ledger):
    db.create_session("sess-gw-1", "telegram", profile_name="cole-espinoza")

    row = ledger.only_row()
    assert row["status"] == "open"
    assert row["session_id"] == "sess-gw-1"
    assert row["agent_name"] == "cole-espinoza"
    assert row["session_kind"] == al.SESSION_KIND_OPERATOR
    assert row["linear_issue_id"] is None
    assert row["job_title"] == al.OPERATOR_JOB_TITLE
    assert row["credential_id"] == FIXTURE_CREDENTIAL_CODE


def test_cli_session_create_opens_exactly_one_row(db, ledger):
    db.create_session("sess-cli-1", "cli", profile_name="cole-espinoza")

    row = ledger.only_row()
    assert row["status"] == "open"
    assert row["session_kind"] == al.SESSION_KIND_OPERATOR
    assert row["job_title"] == al.OPERATOR_JOB_TITLE


def test_repeat_create_session_never_opens_a_second_row(db, ledger):
    db.create_session("sess-dup", "cli", profile_name="cole-espinoza")
    db.create_session("sess-dup", "cli", profile_name="cole-espinoza")
    db.create_session("sess-dup", "cli", profile_name="cole-espinoza")

    assert len(ledger.rows) == 1


def test_kanban_worker_session_reuses_the_claim_row(db, ledger, monkeypatch):
    """A dispatched worker already opened on claim — never open a second row."""
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    claim_id = al.open_action_ledger(
        linear_issue_id="HEL-4009",
        agent_name="cole-espinoza",
        job_title="Ship the thing",
        kanban_task_id="t_abc",
        session_id="sess-worker-1",
    )

    db.create_session("sess-worker-1", "cli", profile_name="cole-espinoza")

    assert len(ledger.rows) == 1
    row = ledger.only_row()
    assert row["id"] == claim_id
    # The claim row's work reference survives — the session hook adopts, it
    # does not overwrite identity with an ad hoc "operator session".
    assert row["linear_issue_id"] == "HEL-4009"
    assert row["job_title"] == "Ship the thing"


def test_dispatched_session_without_claim_row_is_marked_dispatched(db, ledger, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_zzz")
    db.create_session("sess-worker-2", "cli", profile_name="cole-espinoza")

    row = ledger.only_row()
    assert row["session_kind"] == al.SESSION_KIND_DISPATCHED
    assert row["kanban_task_id"] == "t_zzz"


def test_open_tolerates_session_kind_column_absent_on_older_schema(db, ledger):
    """The column arrives in a HELIOS migration; an old schema must not lose rows."""
    ledger.reject_session_kind = True
    db.create_session("sess-old-schema", "cli", profile_name="cole-espinoza")

    row = ledger.only_row()
    assert row["status"] == "open"
    assert "session_kind" not in row


# ── AC2: a session that ends leaves a closed record ──────────────────────


def test_clean_end_closes_row_with_completed(db, ledger, tmp_path, monkeypatch):
    _seed_worker_cost(tmp_path, monkeypatch, "cole-espinoza", "sess-end-1")
    db.create_session("sess-end-1", "cli", profile_name="cole-espinoza")

    db.end_session("sess-end-1", "cli_close")

    row = ledger.only_row()
    assert row["status"] == "closed"
    assert row["outcome"] == al.OUTCOME_COMPLETED
    assert row["end_ts"]
    # Tokens / model / cost come from state.db, not from a guess.
    assert row["prompt_tokens"] == 1200
    assert row["completion_tokens"] == 340
    assert row["cost_usd"] == pytest.approx(0.0125)
    assert row["llm_model"] == "qwen3.8-engineer"


def test_expiry_finalization_closes_row_with_expired(db, ledger):
    db.create_session("sess-exp-1", "telegram", profile_name="cole-espinoza")

    # What gateway/session.py set_expiry_finalized calls.
    db.close_session_ledger_row("sess-exp-1", outcome=al.OUTCOME_EXPIRED)

    row = ledger.only_row()
    assert row["status"] == "closed"
    assert row["outcome"] == al.OUTCOME_EXPIRED


def test_end_reason_session_expired_maps_to_expired(db, ledger):
    db.create_session("sess-exp-2", "telegram", profile_name="cole-espinoza")
    db.end_session("sess-exp-2", "session_expired")

    assert ledger.only_row()["outcome"] == al.OUTCOME_EXPIRED


def test_failed_session_closes_row_with_failed(db, ledger):
    db.create_session("sess-fail-1", "cli", profile_name="cole-espinoza")

    # What cli.py's close path calls when it is unwinding an exception.
    db.close_session_ledger_row("sess-fail-1", outcome=al.OUTCOME_FAILED)

    row = ledger.only_row()
    assert row["status"] == "closed"
    assert row["outcome"] == al.OUTCOME_FAILED


def test_process_exit_closes_open_rows_with_abandoned(db, ledger):
    db.create_session("sess-kill-1", "cli", profile_name="cole-espinoza")
    assert al._TRACKED_SESSIONS.get("sess-kill-1")

    closed = al.close_tracked_sessions_at_exit()

    assert closed == 1
    row = ledger.only_row()
    assert row["status"] == "closed"
    assert row["outcome"] == al.OUTCOME_ABANDONED
    assert not al._TRACKED_SESSIONS


def test_close_is_idempotent_first_outcome_wins(db, ledger):
    db.create_session("sess-once", "cli", profile_name="cole-espinoza")
    db.close_session_ledger_row("sess-once", outcome=al.OUTCOME_EXPIRED)

    assert db.close_session_ledger_row("sess-once", outcome=al.OUTCOME_COMPLETED) is None
    assert ledger.only_row()["outcome"] == al.OUTCOME_EXPIRED


# ── AC2 (abandonment): the stale sweep closes orphans ────────────────────


def test_sweep_closes_orphaned_row_as_abandoned(db, ledger):
    """A row whose session is gone from state.db closes as abandoned."""
    al.open_session_ledger(
        "sess-orphan", "cli", "cole-espinoza", db=db, track_for_exit=False
    )
    assert ledger.open_rows()

    closed = al.sweep_stale_session_ledgers(
        db=db, profile="cole-espinoza", now=FIXED_NOW
    )

    assert closed == 1
    row = ledger.only_row()
    assert row["status"] == "closed"
    assert row["outcome"] == al.OUTCOME_ABANDONED


def test_sweep_closes_row_whose_session_ended_without_closing(db, ledger):
    db.create_session("sess-half", "cli", profile_name="cole-espinoza")
    # Session ends without the ledger close ever running (killed mid-shutdown).
    db._execute_write(
        lambda conn: conn.execute(
            "UPDATE sessions SET ended_at = ?, end_reason = 'crash' WHERE id = ?",
            (FIXED_NOW, "sess-half"),
        )
    )
    al._TRACKED_SESSIONS.clear()

    assert al.sweep_stale_session_ledgers(db=db, profile="cole-espinoza", now=FIXED_NOW) == 1
    assert ledger.only_row()["outcome"] == al.OUTCOME_ABANDONED


def test_sweep_leaves_live_sessions_open(db, ledger):
    db.create_session("sess-live", "cli", profile_name="cole-espinoza")

    assert al.sweep_stale_session_ledgers(db=db, profile="cole-espinoza", now=FIXED_NOW) == 0
    assert ledger.only_row()["status"] == "open"


def test_sweep_respects_the_grace_period_for_young_rows(db, ledger):
    """A row younger than the grace window may just be racing its own insert."""
    al.open_session_ledger(
        "sess-young", "cli", "cole-espinoza", db=db, track_for_exit=False
    )
    from datetime import datetime, timezone

    ledger.only_row()["start_ts"] = "2025-10-09T00:00:00Z"
    # Pin "now" to one minute after the row's start_ts.
    young_now = datetime(2025, 10, 9, 0, 1, tzinfo=timezone.utc).timestamp()

    assert al.sweep_stale_session_ledgers(
        db=db, profile="cole-espinoza", now=young_now, min_age_s=900.0
    ) == 0
    assert ledger.only_row()["status"] == "open"

    old_now = datetime(2025, 10, 9, 1, 0, tzinfo=timezone.utc).timestamp()
    assert al.sweep_stale_session_ledgers(
        db=db, profile="cole-espinoza", now=old_now, min_age_s=900.0
    ) == 1


def test_sweep_never_touches_another_seats_rows(db, ledger):
    al.open_session_ledger(
        "sess-other", "cli", "other-seat", db=None, track_for_exit=False
    )

    assert al.sweep_stale_session_ledgers(db=db, profile="cole-espinoza", now=FIXED_NOW) == 0
    assert ledger.only_row()["status"] == "open"


# ── Fail-open for the chat, never fail-silent ────────────────────────────


def test_open_failure_bumps_defect_counter_and_does_not_raise(db, ledger, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise al.ActionLedgerError("supabase unreachable")

    monkeypatch.setattr(al, "_request", boom)

    with caplog.at_level("WARNING"):
        db.create_session("sess-defect", "cli", profile_name="cole-espinoza")

    assert al.session_ledger_defect_count(db) == 1
    assert al.session_ledger_defect_count(db, "open_failed") == 1
    assert any("session ledger open failed" in r.message for r in caplog.records)
    # The session itself still exists — the chat is never blocked.
    assert db.get_session("sess-defect") is not None


def test_close_failure_bumps_defect_counter_and_does_not_raise(db, ledger, monkeypatch):
    db.create_session("sess-defect-2", "cli", profile_name="cole-espinoza")

    def boom(*args, **kwargs):
        raise al.ActionLedgerError("supabase unreachable")

    monkeypatch.setattr(al, "close_action_ledger", boom)
    assert db.close_session_ledger_row("sess-defect-2", outcome="completed") is None
    assert al.session_ledger_defect_count(db, "close_failed") == 1


def test_ledger_is_inert_without_a_service_role(tmp_path, monkeypatch):
    """No key configured → no network call, no row, no defect."""
    calls = []
    monkeypatch.delenv("SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("HERMES_SESSION_LEDGER", raising=False)
    monkeypatch.setattr(al, "_load_keys_env", lambda: None)
    monkeypatch.setattr(al, "_request", lambda *a, **k: calls.append(a))
    database = SessionDB(db_path=tmp_path / "state.db")
    try:
        database.create_session("sess-nokey", "cli")
        assert calls == []
    finally:
        database.close()


# ── No secret material on any row ────────────────────────────────────────


def test_no_secret_material_on_any_row(db, ledger, tmp_path, monkeypatch):
    """Rows carry identity — the credential code, never the credential value."""
    _seed_worker_cost(tmp_path, monkeypatch, "cole-espinoza", "sess-secret")
    monkeypatch.setenv("HERMES_API_KEY", FIXTURE_SECRET)

    db.create_session("sess-secret", "telegram", profile_name="cole-espinoza")
    db.end_session("sess-secret", "cli_close")

    assert ledger.rows
    for row in ledger.rows.values():
        blob = json.dumps(row)
        assert FIXTURE_SECRET not in blob
    for _method, path, body in ledger.requests:
        assert FIXTURE_SECRET not in path
        assert FIXTURE_SECRET not in json.dumps(body or {})
    # The identity that *should* be there still is.
    assert ledger.only_row()["credential_id"] == FIXTURE_CREDENTIAL_CODE


def test_credential_code_comes_from_profile_meta_not_a_secret(tmp_path, monkeypatch):
    """resolve_credential_id reads a code, and returns None rather than guessing."""
    monkeypatch.delenv("HERMES_CREDENTIAL_ID", raising=False)
    profile_dir = tmp_path / "profiles" / "cole-espinoza"
    profile_dir.mkdir(parents=True)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_profile_dir", lambda name: profile_dir
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.read_profile_meta",
        lambda d: {"credential_id": FIXTURE_CREDENTIAL_CODE, "api_key": FIXTURE_SECRET},
    )
    assert al.resolve_credential_id("cole-espinoza") == FIXTURE_CREDENTIAL_CODE

    monkeypatch.setattr("hermes_cli.profiles.read_profile_meta", lambda d: {})
    assert al.resolve_credential_id("cole-espinoza") is None


def test_outcome_for_end_reason_mapping():
    assert al.outcome_for_end_reason("cli_close") == al.OUTCOME_COMPLETED
    assert al.outcome_for_end_reason("session_expired") == al.OUTCOME_EXPIRED
    assert al.outcome_for_end_reason("crash") == al.OUTCOME_FAILED
    assert al.outcome_for_end_reason("ws_orphan_reap") == al.OUTCOME_ABANDONED
    assert al.outcome_for_end_reason(None) == al.OUTCOME_COMPLETED


def test_fake_transport_never_reaches_the_network(ledger):
    """Guard: the suite must not hold a real transport by accident."""
    assert isinstance(al._request, FakeLedgerTransport)
    assert Path(al.__file__).name == "action_ledger.py"
