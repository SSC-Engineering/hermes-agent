"""Ad hoc session model policy (HEL-6661, proof for HEL-6662).

A gateway or CLI session's model is screened by the same allowlist a dispatched
kanban job is.  Fail-closed: a forbidden rail refuses the session with the rule
id and leaves a ``policy_refused`` ledger row.  Deterministic: the policy is a
tmp file, the ledger transport is the in-memory fake, nothing sleeps.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli import action_ledger as al
from hermes_cli import model_policy as mp
from tests.hermes_cli.test_session_ledger import (
    FIXTURE_CREDENTIAL_CODE,
    FIXTURE_SECRET,
    FakeLedgerTransport,
)

ALLOWED_RAIL = "qwen3.8-engineer"
RESTRICTED_RAIL = "nvidia-nim-large"
FORBIDDEN_RAIL = "some-undefined-frontier-rail"


def _write_policy(tmp_path, monkeypatch, name="model_policy.yaml", **overrides):
    policy = {
        "enabled": True,
        "mode": "downgrade",
        "allowed_models": [ALLOWED_RAIL, RESTRICTED_RAIL],
        "default_model": ALLOWED_RAIL,
        "monitor_patterns": ["monitor"],
        "monitor_ceiling": ALLOWED_RAIL,
        "restricted": {RESTRICTED_RAIL: ["capacity-holder"]},
        "restricted_downgrade": ALLOWED_RAIL,
    }
    policy.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(policy), encoding="utf-8")  # YAML is a JSON superset
    monkeypatch.setenv("HERMES_MODEL_POLICY_PATH", str(path))
    mp._cache = {}
    mp._cache_mtime = -1.0
    return path


@pytest.fixture(autouse=True)
def _reset_policy_cache(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL_POLICY_PATH", raising=False)
    mp._cache = {}
    mp._cache_mtime = -1.0
    yield
    mp._cache = {}
    mp._cache_mtime = -1.0


# ── Allowed rails proceed ────────────────────────────────────────────────


def test_allowed_rail_proceeds(tmp_path, monkeypatch):
    _write_policy(tmp_path, monkeypatch)
    assert mp.enforce_session_model("cole-espinoza", ALLOWED_RAIL) == ALLOWED_RAIL


def test_restricted_rail_proceeds_for_a_named_holder(tmp_path, monkeypatch):
    _write_policy(tmp_path, monkeypatch)
    assert (
        mp.enforce_session_model("capacity-holder", RESTRICTED_RAIL) == RESTRICTED_RAIL
    )


def test_missing_policy_file_fails_open(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_MODEL_POLICY_PATH", str(tmp_path / "absent.yaml"))
    assert mp.enforce_session_model("cole-espinoza", FORBIDDEN_RAIL) == FORBIDDEN_RAIL


def test_disabled_policy_fails_open(tmp_path, monkeypatch):
    _write_policy(tmp_path, monkeypatch, name="disabled.yaml", enabled=False)
    assert mp.enforce_session_model("cole-espinoza", FORBIDDEN_RAIL) == FORBIDDEN_RAIL


# ── Forbidden rails are refused with the rule id ─────────────────────────


def test_forbidden_rail_refused_with_rule_id(tmp_path, monkeypatch):
    _write_policy(tmp_path, monkeypatch)

    with pytest.raises(mp.ModelPolicySessionRefused) as excinfo:
        mp.enforce_session_model("cole-espinoza", FORBIDDEN_RAIL)

    assert excinfo.value.rule_id == mp.RULE_ALLOWLIST
    assert mp.RULE_ALLOWLIST in str(excinfo.value)
    assert FORBIDDEN_RAIL in str(excinfo.value)


def test_forbidden_rail_refuses_rather_than_downgrading(tmp_path, monkeypatch):
    """An undefined rail never falls back — fail-closed, not fail-quiet."""
    _write_policy(tmp_path, monkeypatch, name="nofall.yaml")
    with pytest.raises(mp.ModelPolicySessionRefused):
        mp.enforce_session_model("cole-espinoza", FORBIDDEN_RAIL)


def test_restricted_rail_falls_to_restricted_downgrade(tmp_path, monkeypatch):
    _write_policy(tmp_path, monkeypatch)

    assert mp.enforce_session_model("cole-espinoza", RESTRICTED_RAIL) == ALLOWED_RAIL


def test_restricted_downgrade_off_the_allowlist_is_refused(tmp_path, monkeypatch):
    _write_policy(
        tmp_path,
        monkeypatch,
        name="bad_downgrade.yaml",
        restricted_downgrade=FORBIDDEN_RAIL,
    )

    with pytest.raises(mp.ModelPolicySessionRefused) as excinfo:
        mp.enforce_session_model("cole-espinoza", RESTRICTED_RAIL)

    assert excinfo.value.rule_id == mp.RULE_RESTRICTED


def test_monitor_above_ceiling_is_refused_with_rule_id(tmp_path, monkeypatch):
    _write_policy(tmp_path, monkeypatch)

    with pytest.raises(mp.ModelPolicySessionRefused) as excinfo:
        mp.enforce_session_model("fleet-monitor", RESTRICTED_RAIL)

    assert excinfo.value.rule_id == mp.RULE_MONITOR_CEILING


def test_rules_sharing_one_citation_still_behave_differently(tmp_path, monkeypatch):
    """Two rules may cite the same manifest id; control flow must not conflate them.

    MODEL-003 covers both the monitor ceiling and restricted tiers. A monitor
    above its ceiling must still be refused, never moved onto
    restricted_downgrade — that target need not be at or below the ceiling.
    """
    _write_policy(
        tmp_path,
        monkeypatch,
        name="shared_ids.yaml",
        rule_ids={"monitor_ceiling": "MODEL-003", "restricted": "MODEL-003"},
    )

    with pytest.raises(mp.ModelPolicySessionRefused) as excinfo:
        mp.enforce_session_model("fleet-monitor", RESTRICTED_RAIL)
    assert excinfo.value.rule_id == "MODEL-003"

    # Same citation, different rule: this one does fall to the safe rail.
    assert mp.enforce_session_model("cole-espinoza", RESTRICTED_RAIL) == ALLOWED_RAIL


def test_rule_ids_are_overridable_from_the_policy_file(tmp_path, monkeypatch):
    _write_policy(
        tmp_path,
        monkeypatch,
        name="rule_ids.yaml",
        rule_ids={"allowlist": "MODEL-999"},
    )

    with pytest.raises(mp.ModelPolicySessionRefused) as excinfo:
        mp.enforce_session_model("cole-espinoza", FORBIDDEN_RAIL)

    assert excinfo.value.rule_id == "MODEL-999"


def test_spawn_enforce_keeps_its_downgrade_contract(tmp_path, monkeypatch):
    """Preservation: spawn enforcement still downgrades, it does not refuse."""
    _write_policy(tmp_path, monkeypatch)
    assert mp.enforce("cole-espinoza", FORBIDDEN_RAIL) == ALLOWED_RAIL
    assert mp.enforce("cole-espinoza", ALLOWED_RAIL) == ALLOWED_RAIL


def test_spawn_enforce_block_mode_raises(tmp_path, monkeypatch):
    _write_policy(tmp_path, monkeypatch, name="block.yaml", mode="block")
    with pytest.raises(mp.ModelPolicyViolation):
        mp.enforce("cole-espinoza", FORBIDDEN_RAIL)


def test_module_names_no_rails_of_its_own():
    """Every allowlist entry comes from the policy file, never from code."""
    source = open(mp.__file__, encoding="utf-8").read()
    for banned in ("claude-", "gpt-", "gemini-", "grok-", "o3", "sonnet", "opus"):
        assert banned not in source.lower(), f"code names a rail: {banned}"


# ── A refusal leaves a policy_refused ledger row ─────────────────────────


@pytest.fixture
def ledger(monkeypatch):
    transport = FakeLedgerTransport()
    monkeypatch.setenv("SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET", FIXTURE_SECRET)
    monkeypatch.setenv("HERMES_SESSION_LEDGER", "1")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(al, "_load_keys_env", lambda: None)
    monkeypatch.setattr(al, "_request", transport)
    monkeypatch.setattr(al, "_active_profile_name", lambda: "cole-espinoza")
    monkeypatch.setattr(al, "resolve_credential_id", lambda p: FIXTURE_CREDENTIAL_CODE)
    al._TRACKED_SESSIONS.clear()
    yield transport
    al._TRACKED_SESSIONS.clear()


def test_refusal_records_a_closed_policy_refused_row(ledger):
    al.record_policy_refusal(
        "sess-refused",
        profile="cole-espinoza",
        rule_id=mp.RULE_ALLOWLIST,
        model=FORBIDDEN_RAIL,
        db=None,
    )

    row = ledger.only_row()
    assert row["status"] == "closed"
    assert row["outcome"] == al.OUTCOME_POLICY_REFUSED
    assert row["session_id"] == "sess-refused"
    assert row["agent_name"] == "cole-espinoza"
    assert row["job_title"] == al.OPERATOR_JOB_TITLE
    assert mp.RULE_ALLOWLIST in json.dumps(row["tools_used"])
    # A refusal is not tracked for the exit hook — it is already closed.
    assert "sess-refused" not in al._TRACKED_SESSIONS


def test_refusal_closes_the_sessions_existing_row_not_a_second_one(ledger):
    """A session refused after its row was opened still has exactly one row."""
    opened = al.open_session_ledger(
        "sess-refused-3", "telegram", "cole-espinoza", db=None
    )
    assert opened

    closed = al.record_policy_refusal(
        "sess-refused-3",
        profile="cole-espinoza",
        rule_id=mp.RULE_ALLOWLIST,
        model=FORBIDDEN_RAIL,
        db=None,
    )

    assert closed == opened
    row = ledger.only_row()
    assert row["outcome"] == al.OUTCOME_POLICY_REFUSED
    assert "sess-refused-3" not in al._TRACKED_SESSIONS


def test_no_secret_material_on_a_policy_refused_row(ledger):
    al.record_policy_refusal(
        "sess-refused-2",
        profile="cole-espinoza",
        rule_id=mp.RULE_ALLOWLIST,
        model=FORBIDDEN_RAIL,
        db=None,
    )
    for row in ledger.rows.values():
        assert FIXTURE_SECRET not in json.dumps(row)
    for _method, path, body in ledger.requests:
        assert FIXTURE_SECRET not in path
        assert FIXTURE_SECRET not in json.dumps(body or {})


# ── CLI session start is screened ────────────────────────────────────────


class _StubCLI:
    """Minimal stand-in carrying only what the screen helper reads."""

    profile = "cole-espinoza"
    session_id = "sess-cli-policy"
    _session_db = None

    def __init__(self):
        from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

        self._screen = CLIAgentSetupMixin._screen_adhoc_session_model.__get__(self)


def test_cli_session_start_allows_an_allowlisted_rail(tmp_path, monkeypatch, ledger):
    _write_policy(tmp_path, monkeypatch)
    assert _StubCLI()._screen(ALLOWED_RAIL) == ALLOWED_RAIL
    assert not ledger.rows


def test_cli_session_start_refuses_a_forbidden_rail_and_records_the_row(
    tmp_path, monkeypatch, ledger
):
    _write_policy(tmp_path, monkeypatch)

    with pytest.raises(mp.ModelPolicySessionRefused) as excinfo:
        _StubCLI()._screen(FORBIDDEN_RAIL)

    assert excinfo.value.rule_id == mp.RULE_ALLOWLIST
    row = ledger.only_row()
    assert row["outcome"] == al.OUTCOME_POLICY_REFUSED
    assert row["session_id"] == "sess-cli-policy"


def test_dispatched_worker_spawn_is_not_screened_by_the_session_path(
    tmp_path, monkeypatch, ledger
):
    """Preservation: kanban spawn enforcement is untouched by HEL-6661."""
    _write_policy(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")

    assert _StubCLI()._screen(FORBIDDEN_RAIL) == FORBIDDEN_RAIL
    assert not ledger.rows


# ── Gateway session model resolution is screened ─────────────────────────


class _StubEntry:
    session_id = "sess-gw-policy"


class _StubStore:
    _entries = {"agent:main:telegram:dm:1": _StubEntry()}


class _StubGateway:
    _session_db = None
    session_store = _StubStore()

    def __init__(self):
        from gateway.run import GatewayRunner

        self._screen = GatewayRunner._screen_session_model.__get__(self)
        self._record_session_policy_refusal = (
            GatewayRunner._record_session_policy_refusal.__get__(self)
        )


def test_gateway_session_allows_an_allowlisted_rail(tmp_path, monkeypatch, ledger):
    _write_policy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "cole-espinoza"
    )
    assert _StubGateway()._screen(ALLOWED_RAIL, "agent:main:telegram:dm:1") == ALLOWED_RAIL
    assert not ledger.rows


def test_gateway_session_refuses_a_forbidden_rail_and_records_the_row(
    tmp_path, monkeypatch, ledger
):
    _write_policy(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name", lambda: "cole-espinoza"
    )

    with pytest.raises(mp.ModelPolicySessionRefused) as excinfo:
        _StubGateway()._screen(FORBIDDEN_RAIL, "agent:main:telegram:dm:1")

    assert excinfo.value.rule_id == mp.RULE_ALLOWLIST
    row = ledger.only_row()
    assert row["outcome"] == al.OUTCOME_POLICY_REFUSED
    assert row["session_id"] == "sess-gw-policy"
