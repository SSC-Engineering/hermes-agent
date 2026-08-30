"""Unit tests for fleet model_policy (HEL-6107 re-land)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

import hermes_cli.model_policy as mp


@pytest.fixture()
def policy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "model_policy.yaml"
    path.write_text(
        textwrap.dedent(
            """\
            enabled: true
            mode: downgrade
            default_model: nvidia/nemotron-3.5-lightning-30b-a3b
            restricted_downgrade: anthropic/claude-opus-4.8
            monitor_ceiling: qwen3.8-engineer
            monitor_patterns:
              - facilities-
              - monitor
            allowed_models:
              - nvidia/nemotron-3.5-lightning-30b-a3b
              - qwen3.8-engineer
              - anthropic/claude-opus-4.8
              - x-ai/grok-4.5
            restricted:
              anthropic/claude-opus-4.8:
                - fiona-sterling
                - cornelius-corey-vale
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(mp, "POLICY_PATH", str(path))
    # Reset mtime cache between tests
    mp._cache = {}
    mp._cache_mtime = -1.0
    return path


def test_monitor_over_ceiling_downgrades(policy_file: Path) -> None:
    ok, reason, sug = mp.check("facilities-x", "anthropic/claude-opus-4.8")
    assert ok is False
    assert sug == "qwen3.8-engineer"
    assert "monitor" in reason
    assert mp.enforce("facilities-x", "anthropic/claude-opus-4.8") == "qwen3.8-engineer"


def test_monitor_at_ceiling_allowed(policy_file: Path) -> None:
    ok, reason, sug = mp.check("facilities-x", "qwen3.8-engineer")
    assert ok is True
    assert sug is None
    assert mp.enforce("facilities-x", "qwen3.8-engineer") == "qwen3.8-engineer"


def test_undefined_model_uses_default(policy_file: Path) -> None:
    ok, reason, sug = mp.check("cole-espinoza", "some/undefined-paid-model")
    assert ok is False
    assert sug == "nvidia/nemotron-3.5-lightning-30b-a3b"
    assert mp.enforce("cole-espinoza", "some/undefined-paid-model") == (
        "nvidia/nemotron-3.5-lightning-30b-a3b"
    )


def test_allowlisted_pin_unchanged(policy_file: Path) -> None:
    model = "nvidia/nemotron-3.5-lightning-30b-a3b"
    ok, reason, sug = mp.check("cole-espinoza", model)
    assert ok is True
    assert reason == "allowed"
    assert mp.enforce("cole-espinoza", model) == model


def test_restricted_holder_miss_downgrades(policy_file: Path) -> None:
    ok, reason, sug = mp.check("cole-espinoza", "anthropic/claude-opus-4.8")
    assert ok is False
    assert sug == "anthropic/claude-opus-4.8"
    assert "not authorised" in reason
    assert mp.enforce("cole-espinoza", "anthropic/claude-opus-4.8") == (
        "anthropic/claude-opus-4.8"
    )


def test_restricted_holder_hit_allowed(policy_file: Path) -> None:
    ok, reason, sug = mp.check("fiona-sterling", "anthropic/claude-opus-4.8")
    assert ok is True
    assert mp.enforce("fiona-sterling", "anthropic/claude-opus-4.8") == (
        "anthropic/claude-opus-4.8"
    )


def test_missing_yaml_fails_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    monkeypatch.setattr(mp, "POLICY_PATH", str(missing))
    mp._cache = {}
    mp._cache_mtime = -1.0
    ok, reason, sug = mp.check("anyone", "anthropic/claude-opus-5")
    assert ok is True
    assert "failing open" in reason
    assert mp.enforce("anyone", "anthropic/claude-opus-5") == "anthropic/claude-opus-5"


def test_block_mode_raises(policy_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Rewrite mode to block
    text = policy_file.read_text(encoding="utf-8").replace(
        "mode: downgrade", "mode: block"
    )
    policy_file.write_text(text, encoding="utf-8")
    mp._cache = {}
    mp._cache_mtime = -1.0
    with pytest.raises(mp.ModelPolicyViolation):
        mp.enforce("facilities-x", "anthropic/claude-opus-4.8")


def test_monitor_ceiling_default_constant() -> None:
    """Hardcoded fallback must match live YAML after Super retirement."""
    assert mp.MONITOR_CEILING == "qwen3.8-engineer"


def test_no_model_passes() -> None:
    ok, reason, sug = mp.check("cole-espinoza", None)
    assert ok is True
    assert mp.enforce("cole-espinoza", None) == ""
    assert mp.enforce("cole-espinoza", "") == ""
