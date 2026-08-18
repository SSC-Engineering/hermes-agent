"""Tests for hermes_cli.action_ledger open/close client + cost gate."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import action_ledger as al


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET", "test-key")
    monkeypatch.setenv("SSC_SUPABASE_HELIOS_AGENTIC_OS_URL", "https://example.supabase.co")
    monkeypatch.setattr(al, "_load_keys_env", lambda: None)


def test_open_action_ledger_posts_and_returns_id(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, prefer=None, extra_headers=None):
        calls.append({"method": method, "path": path, "body": body, "prefer": prefer})
        if method == "GET":
            return []
        return [{"id": "uuid-open-1", "status": "open"}]

    monkeypatch.setattr(al, "_request", fake_request)
    lid = al.open_action_ledger(
        linear_issue_id="HEL-4009",
        agent_name="cole-espinoza",
        job_title="stamp",
        kanban_task_id="t_abc",
        session_id="s1",
    )
    assert lid == "uuid-open-1"
    assert calls[-1]["method"] == "POST"
    assert calls[-1]["body"]["linear_issue_id"] == "HEL-4009"
    assert calls[-1]["body"]["status"] == "open"


def test_open_reuses_existing_open_row(monkeypatch):
    def fake_request(method, path, body=None, prefer=None, extra_headers=None):
        if method == "GET":
            return [{"id": "existing-1"}]
        raise AssertionError("POST should not run when open row exists")

    monkeypatch.setattr(al, "_request", fake_request)
    lid = al.open_action_ledger(
        linear_issue_id="HEL-4009",
        agent_name="worker",
        kanban_task_id="t_abc",
    )
    assert lid == "existing-1"


def test_close_action_ledger_patches_same_id(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, prefer=None, extra_headers=None):
        calls.append({"method": method, "path": path, "body": body})
        return [{"id": "uuid-open-1", "status": "closed"}]

    monkeypatch.setattr(al, "_request", fake_request)
    out = al.close_action_ledger(
        "uuid-open-1",
        tools_used=["terminal"],
        skills_used=["helios-agent-cea"],
        llm_model="x-ai/grok-4.5",
        prompt_tokens=10,
        completion_tokens=20,
        cost_usd=0.01,
        cost_status="ok",
        pricing_source="gateway",
        outcome="completed",
        signature_md="sig",
    )
    assert out == "uuid-open-1"
    assert calls[0]["method"] == "PATCH"
    assert "id=eq.uuid-open-1" in calls[0]["path"]
    assert calls[0]["body"]["status"] == "closed"
    assert calls[0]["body"]["tools_used"] == ["terminal"]
    assert calls[0]["body"]["prompt_tokens"] == 10
    assert calls[0]["body"]["cost_usd"] == 0.01


def test_close_requires_ledger_id():
    with pytest.raises(al.ActionLedgerError):
        al.close_action_ledger("")


def test_close_completed_without_cost_refuses(monkeypatch):
    def fake_request(method, path, body=None, prefer=None, extra_headers=None):
        raise AssertionError("must not PATCH when cost missing")

    monkeypatch.setattr(al, "_request", fake_request)
    with pytest.raises(al.ActionLedgerIncompleteError):
        al.close_action_ledger("uuid-open-1", outcome="completed")


def test_close_completed_allow_zero_cost(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, prefer=None, extra_headers=None):
        calls.append(body)
        return [{"id": "uuid-open-1", "status": "closed"}]

    monkeypatch.setattr(al, "_request", fake_request)
    al.close_action_ledger("uuid-open-1", outcome="completed", allow_zero_cost=True)
    assert calls[0]["cost_usd"] == 0.0
    assert calls[0]["pricing_source"] == "true_zero_no_spend"


def test_close_blocked_outcome_allows_null_cost(monkeypatch):
    calls = []

    def fake_request(method, path, body=None, prefer=None, extra_headers=None):
        calls.append(body)
        return [{"id": "uuid-open-1", "status": "closed"}]

    monkeypatch.setattr(al, "_request", fake_request)
    al.close_action_ledger("uuid-open-1", outcome="blocked")
    assert calls[0]["cost_usd"] is None
    assert calls[0]["outcome"] == "blocked"


def test_close_fills_cost_from_session_db(tmp_path, monkeypatch):
    profile = "cole-espinoza"
    session_id = "20260811_session_test"
    db = tmp_path / "profiles" / profile / "state.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT
        )
        """
    )
    con.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (session_id, "x-ai/grok-4.5", 100, 50, 0, 0, 0, 0.1234, None, "estimated"),
    )
    con.commit()
    con.close()

    monkeypatch.delenv("HERMES_REAL_HOME", raising=False)
    monkeypatch.setattr(al, "_HERMES_ROOT", tmp_path)
    calls = []

    def fake_request(method, path, body=None, prefer=None, extra_headers=None):
        calls.append(body)
        return [{"id": "uuid-open-1", "status": "closed"}]

    monkeypatch.setattr(al, "_request", fake_request)
    al.close_action_ledger(
        "uuid-open-1",
        outcome="completed",
        profile=profile,
        session_id=session_id,
    )
    assert calls[0]["cost_usd"] == pytest.approx(0.1234)
    assert calls[0]["prompt_tokens"] == 100
    assert calls[0]["completion_tokens"] == 50
    assert calls[0]["session_id"] == session_id
    assert calls[0]["pricing_source"] == "session_db"


def test_require_cost_on_when_service_configured(monkeypatch):
    monkeypatch.delenv("HERMES_HAL_REQUIRE_COST", raising=False)
    assert al.require_cost_on_complete() is True
    monkeypatch.setenv("HERMES_HAL_REQUIRE_COST", "0")
    assert al.require_cost_on_complete() is False


def test_missing_service_key_raises(monkeypatch):
    monkeypatch.delenv("SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    with pytest.raises(al.ActionLedgerError):
        al._service_config()
