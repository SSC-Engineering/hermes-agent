"""Fleet model policy — allowlist enforcement at spawn and at session start.

Two callers, one allowlist:

``enforce()``
    Spawn-time enforcement (kanban dispatch, cron, anything built through
    ``HermesCLI``).  Honours the policy file's ``mode`` (``downgrade`` by
    default, ``block`` to raise).

``enforce_session_model()``
    Ad hoc session screening (gateway session model resolution, CLI session
    start — HEL-6661).  **Fail-closed**: a forbidden rail refuses the session
    with the violated rule id instead of silently downgrading.  The single
    exception is a restricted-tier violation for which the policy names a
    ``restricted_downgrade`` that is itself on the allowlist — the session
    then proceeds on that safe rail.

Policy lives in ``~/.hermes/model_policy.yaml`` so it is auditable and
editable without a code change, and mirrors ``HELIos/config/model-policy.yaml``.
A missing or disabled policy file fails OPEN with no violation (never brick the
fleet on a missing config — the same contract the spawn path has always had);
an actual violation of a present policy fails CLOSED for ad hoc sessions.

This module deliberately ships **no model names** other than the local monitor
ceiling.  Every allowlist entry comes from the policy file, so no rail can be
introduced by editing code (HAA 2026-09-03: NVIDIA NIM direct plus local Qwen
only for HELIos seats).

Rule ids are reported on refusal so an operator can look the rule up.  The
defaults below are overridable from the policy file's ``rule_ids`` mapping, so
the manifest stays the source of truth:

  * ``MODEL-006`` — monitor ceiling (nothing that monitors runs above it).
  * ``MODEL-007`` — the rail must be defined in the allowlist.
  * ``MODEL-008`` — restricted rails are limited to named holders.
"""

from __future__ import annotations

import logging
import os
from typing import Any, NamedTuple, Optional

try:
    import yaml
except Exception:  # pragma: no cover - yaml is a hard dep in practice
    yaml = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

POLICY_PATH = os.path.expanduser("~/.hermes/model_policy.yaml")

# Fallback ceiling used when a profile matches a monitor pattern but the policy
# file does not name one.  Local Qwen rail (Constitution 15.3 monitoring floor).
MONITOR_CEILING = "qwen3.8-engineer"

RULE_MONITOR_CEILING = "MODEL-006"
RULE_ALLOWLIST = "MODEL-007"
RULE_RESTRICTED = "MODEL-008"

_RULE_ID_KEYS = {
    "monitor_ceiling": RULE_MONITOR_CEILING,
    "allowlist": RULE_ALLOWLIST,
    "restricted": RULE_RESTRICTED,
}


class ModelPolicyViolation(RuntimeError):
    """Raised when a spawn requests a model outside the allowlist."""


class ModelPolicySessionRefused(ModelPolicyViolation):
    """Raised when an ad hoc session is refused before its first request.

    Carries the violated ``rule_id`` so the refusal message, the log line and
    the ``policy_refused`` ledger row all name the same rule.
    """

    def __init__(
        self,
        rule_id: str,
        reason: str,
        *,
        model: Optional[str] = None,
        profile: Optional[str] = None,
    ):
        self.rule_id = rule_id
        self.reason = reason
        self.model = model
        self.profile = profile
        super().__init__(f"[model-policy {rule_id}] {reason}")


class PolicyDecision(NamedTuple):
    """Outcome of a policy screen. ``rule_id`` is set only when denied."""

    allowed: bool
    reason: str
    suggested: Optional[str]
    rule_id: Optional[str]


_cache: dict[str, Any] = {}
_cache_mtime: float = -1.0


def _load() -> dict[str, Any]:
    """Read the policy file, memoised on mtime so edits apply live."""
    global _cache, _cache_mtime
    path = os.environ.get("HERMES_MODEL_POLICY_PATH") or POLICY_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if mtime != _cache_mtime or _cache.get("__path__") != path:
        if yaml is None:
            return {}
        try:
            with open(path, "r", encoding="utf-8") as fh:
                loaded = yaml.safe_load(fh) or {}
        except Exception:
            return {}
        if not isinstance(loaded, dict):
            return {}
        loaded["__path__"] = path
        _cache = loaded
        _cache_mtime = mtime
    return _cache


def _rule_id(policy: dict[str, Any], key: str) -> str:
    """Rule id for *key*, overridable from the policy's ``rule_ids`` map."""
    overrides = policy.get("rule_ids")
    if isinstance(overrides, dict):
        value = overrides.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return _RULE_ID_KEYS[key]


def _is_monitor(profile: str, policy: dict[str, Any]) -> bool:
    pats = policy.get("monitor_patterns") or []
    return any(p in profile for p in pats)


def check(profile: Optional[str], model: Optional[str]) -> PolicyDecision:
    """Screen *model* for *profile*. Pure and side-effect free.

    Returns a :class:`PolicyDecision`.  Backwards compatible with the
    three-tuple ``(allowed, reason, suggested)`` shape via ``NamedTuple``
    unpacking of the first three fields is *not* implied — callers that only
    want the legacy shape should use :func:`check_legacy`.
    """
    if not model:
        return PolicyDecision(True, "no model resolved; upstream default applies", None, None)
    policy = _load()
    if not policy:
        return PolicyDecision(True, "no policy file; failing open", None, None)
    if not policy.get("enabled", True):
        return PolicyDecision(True, "policy disabled", None, None)

    profile = profile or "(unknown)"
    allowed = set(policy.get("allowed_models") or [])
    if not allowed:
        return PolicyDecision(True, "empty allowlist; failing open", None, None)

    # Rule 1 — monitors are capped regardless of what they ask for.
    if _is_monitor(profile, policy):
        ceiling = policy.get("monitor_ceiling") or MONITOR_CEILING
        if model != ceiling:
            return PolicyDecision(
                False,
                f"monitor profile '{profile}' may only run {ceiling} "
                f"(requested {model})",
                ceiling,
                _rule_id(policy, "monitor_ceiling"),
            )
        return PolicyDecision(True, "monitor at ceiling", None, None)

    # Rule 2 — the model must be defined in the allowlist.
    if model not in allowed:
        return PolicyDecision(
            False,
            f"model '{model}' is not in the fleet allowlist; "
            f"requires HAA approval (add it to {policy.get('__path__') or POLICY_PATH})",
            policy.get("default_model"),
            _rule_id(policy, "allowlist"),
        )

    # Rule 3 — restricted tiers are limited to named profiles.
    for tier_model, holders in (policy.get("restricted") or {}).items():
        if model == tier_model and profile not in set(holders or []):
            return PolicyDecision(
                False,
                f"model '{model}' is restricted to {sorted(holders or [])}; "
                f"'{profile}' is not authorised",
                policy.get("restricted_downgrade") or policy.get("default_model"),
                _rule_id(policy, "restricted"),
            )

    return PolicyDecision(True, "allowed", None, None)


def check_legacy(
    profile: Optional[str], model: Optional[str]
) -> tuple[bool, str, Optional[str]]:
    """``check`` in the historical ``(allowed, reason, suggested)`` shape."""
    decision = check(profile, model)
    return decision.allowed, decision.reason, decision.suggested


def enforce(profile: Optional[str], model: Optional[str]) -> str:
    """Enforce policy at spawn.

    Returns the model to actually use.  In ``downgrade`` mode a violation is
    corrected to the suggested model; in ``block`` mode it raises
    :class:`ModelPolicyViolation`.
    """
    decision = check(profile, model)
    if decision.allowed:
        return model or ""

    policy = _load()
    mode = (policy.get("mode") or "downgrade").lower()
    msg = f"[model-policy {decision.rule_id}] {decision.reason}"

    if mode == "block":
        raise ModelPolicyViolation(msg)

    target = decision.suggested or policy.get("default_model") or MONITOR_CEILING
    logger.warning("%s -> downgraded to %s", msg, target)
    return target


def enforce_session_model(
    profile: Optional[str],
    model: Optional[str],
    *,
    session_kind: str = "operator",
) -> str:
    """Screen an ad hoc session's model. Fail-closed (HEL-6661).

    Returns the model the session may run.  Raises
    :class:`ModelPolicySessionRefused` when the requested rail is forbidden and
    the policy names no safe rail to fall to.

    A restricted-tier violation (``MODEL-008``) falls to the policy's
    ``restricted_downgrade`` when that target is itself on the allowlist; every
    other violation refuses, so a rail that policy has not defined can never be
    reached by starting a session instead of dispatching a job.
    """
    decision = check(profile, model)
    if decision.allowed:
        return model or ""

    policy = _load()
    rule_id = decision.rule_id or _rule_id(policy, "allowlist")
    allowed = set(policy.get("allowed_models") or [])

    if decision.rule_id == _rule_id(policy, "restricted"):
        downgrade = policy.get("restricted_downgrade")
        if downgrade and (not allowed or downgrade in allowed):
            logger.warning(
                "[model-policy %s] %s -> %s session downgraded to %s",
                rule_id, decision.reason, session_kind, downgrade,
            )
            return str(downgrade)

    logger.warning(
        "[model-policy %s] %s session refused: %s",
        rule_id, session_kind, decision.reason,
    )
    raise ModelPolicySessionRefused(
        rule_id, decision.reason, model=model, profile=profile
    )
