"""Tests for hermes_cli.action_ledger open/close client."""

from __future__ import annotations

import json
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


def test_close_requires_ledger_id():
    with pytest.raises(al.ActionLedgerError):
        al.close_action_ledger("")


def test_missing_service_key_raises(monkeypatch):
    monkeypatch.delenv("SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    with pytest.raises(al.ActionLedgerError):
        al._service_config()
