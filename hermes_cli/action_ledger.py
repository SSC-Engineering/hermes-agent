"""Supabase action_ledger open/close client (PRD-HEL-27 Lane A + HAL cost gate).

Opens a row on kanban claim and closes the SAME PK on complete.

Cost discipline (HEL-4156 follow-up):
  * Close prefers session-backed tokens/$ from the worker profile ``state.db``.
  * A ``completed`` close without cost evidence is refused unless
    ``allow_zero_cost=True`` (documented true zero only).
  * ``session_id`` is stamped on close when provided so story rollups keep lineage.

Callers that must not crash the kanban state machine catch ``ActionLedgerError``
and decide fail-open vs fail-closed. HELIos kanban complete is fail-closed when
the service role is configured (see ``kanban_db._require_action_ledger_close``).

Env (loaded from ``~/.config/helios/ssc/keys.env`` when present):
  SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET
  SSC_SUPABASE_HELIOS_AGENTIC_OS_URL (optional; default project URL)
  HERMES_HAL_REQUIRE_COST — \"0\" to disable fail-closed cost gate (default on
    when service key is present)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DEFAULT_SUPABASE_URL = "https://mqtvvacrzmutyrznfwri.supabase.co"
_KEYS_ENV_PATH = Path.home() / ".config" / "helios" / "ssc" / "keys.env"
_SERVICE_KEY_NAMES = (
    "SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET",
    "SUPABASE_SERVICE_ROLE_KEY",
)
_URL_KEY_NAMES = (
    "SSC_SUPABASE_HELIOS_AGENTIC_OS_URL",
    "SUPABASE_URL",
)
_HERMES_ROOT = Path.home() / ".hermes"


class ActionLedgerError(RuntimeError):
    """Raised when the ledger cannot be opened or closed."""


class ActionLedgerIncompleteError(ActionLedgerError):
    """Raised when close would leave a completed row without honest cost."""

def _load_keys_env() -> None:
    """Best-effort load of HELIOS SSC keys into os.environ (no overwrite)."""
    path = os.environ.get("HELIOS_SSC_KEYS_ENV") or str(_KEYS_ENV_PATH)
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ and value:
            os.environ[key] = value


def _service_config() -> tuple[str, str]:
    _load_keys_env()
    url = ""
    for name in _URL_KEY_NAMES:
        url = (os.environ.get(name) or "").strip().rstrip("/")
        if url:
            break
    if not url:
        url = DEFAULT_SUPABASE_URL
    key = ""
    for name in _SERVICE_KEY_NAMES:
        key = (os.environ.get(name) or "").strip()
        if key:
            break
    if not key:
        raise ActionLedgerError("action_ledger service role key not configured")
    return url, key


def _request(
    method: str,
    path: str,
    *,
    body: Optional[dict[str, Any]] = None,
    prefer: Optional[str] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> Any:
    base, key = _service_config()
    url = f"{base}{path}"
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8") or "null"
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise ActionLedgerError(f"action_ledger HTTP {exc.code}: {detail}") from exc
    except Exception as exc:  # network / json
        raise ActionLedgerError(f"action_ledger request failed: {exc}") from exc


def _column_missing(exc: Exception, column: str) -> bool:
    """True when a PostgREST error blames an unknown *column* (PGRST204)."""
    text = str(exc)
    return column in text and (
        "PGRST204" in text
        or "schema cache" in text
        or "does not exist" in text
        or "Unprocessable" in text
    )


def _as_list(value: Any) -> Optional[list]:
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if isinstance(value, (tuple, set)):
        return list(value)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def service_role_configured() -> bool:
    """True when HAL can reach Supabase (key present after keys.env load)."""
    try:
        _service_config()
        return True
    except ActionLedgerError:
        return False


def require_cost_on_complete() -> bool:
    """Fail-closed cost gate: on by default when the service role is configured."""
    raw = (os.environ.get("HERMES_HAL_REQUIRE_COST") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return service_role_configured()


def estimate_session_cost(profile: str, session_id: str) -> dict[str, Any]:
    """Best-effort tokens + $ from profile state.db. Never fabricates.

    Source of truth matches ``token_usage_for_session.py``:
    ``~/.hermes/profiles/<profile>/state.db`` table ``sessions``
    (input_tokens/output_tokens/actual_cost_usd/estimated_cost_usd).
    """
    out: dict[str, Any] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "cost_usd": None,
        "cost_status": None,
        "pricing_source": None,
        "llm_model": None,
    }
    if not profile or not session_id:
        return out

    real_home = os.environ.get("HERMES_REAL_HOME")
    roots: list[Path] = []
    # Always honor module root (tests patch this; live default is ~/.hermes).
    roots.append(_HERMES_ROOT)
    if real_home:
        roots.append(Path(real_home).expanduser() / ".hermes")
    # Dedup roots
    seen_roots: set[str] = set()
    uniq_roots: list[Path] = []
    for r in roots:
        k = str(r)
        if k in seen_roots:
            continue
        seen_roots.add(k)
        uniq_roots.append(r)

    candidates: list[Path] = []
    # Prefer the named profile path over ambient HERMES_HOME — a dispatcher
    # session's HERMES_HOME is often 01-max-headroom, while the worker cost
    # lives under profiles/<assignee>/state.db.
    for root in uniq_roots:
        if profile in ("default", None, ""):
            candidates.append(root / "state.db")
        candidates.extend(
            [
                root / "profiles" / profile / "state.db",
                root / "profiles" / profile / "profiles" / profile / "state.db",
            ]
        )
    candidates.extend(
        [
            Path.home() / ".hermes" / "profiles" / profile / "state.db",
            Path.home()
            / ".hermes"
            / "profiles"
            / profile
            / "profiles"
            / profile
            / "state.db",
        ]
    )
    # Active HERMES_HOME may itself be the worker profile home (last resort).
    hh = Path(os.environ.get("HERMES_HOME") or "").expanduser()
    if hh:
        if hh.name == profile and (hh / "state.db").exists():
            candidates.append(hh / "state.db")
        elif (hh / "profiles" / profile / "state.db").exists():
            candidates.append(hh / "profiles" / profile / "state.db")
        elif (hh / "state.db").exists():
            candidates.append(hh / "state.db")

    # Dedup while preserving order.
    seen: set[str] = set()
    ordered: list[Path] = []
    for p in candidates:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.exists():
            ordered.append(p)

    row = None
    db_used: Optional[Path] = None
    for db in ordered:
        try:
            con = sqlite3.connect(str(db))
            con.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue
        try:
            try:
                row = con.execute(
                    "SELECT id, model, input_tokens, output_tokens, "
                    "cache_read_tokens, cache_write_tokens, reasoning_tokens, "
                    "estimated_cost_usd, actual_cost_usd, cost_status "
                    "FROM sessions WHERE id = ? LIMIT 1",
                    (session_id,),
                ).fetchone()
            except sqlite3.Error:
                row = None
            if row is None:
                try:
                    row = con.execute(
                        "SELECT * FROM sessions WHERE id = ? LIMIT 1",
                        (session_id,),
                    ).fetchone()
                except sqlite3.Error:
                    row = None
            # Materialize before close — sqlite3.Row dies with the connection.
            if row is not None:
                row = {k: row[k] for k in row.keys()}
        finally:
            con.close()
        if row is not None:
            db_used = db
            break

    if row is None:
        return out
    _ = db_used  # retained for future debug logging

    keys = set(row.keys())

    def g(*names: str) -> Any:
        for n in names:
            if n in keys and row[n] is not None:
                return row[n]
        return None

    pt = g("input_tokens", "prompt_tokens", "total_input_tokens")
    ct = g("output_tokens", "completion_tokens", "total_output_tokens")
    model = g("model", "llm_model")
    actual = g("actual_cost_usd")
    estimated = g("estimated_cost_usd", "cost_usd", "total_cost_usd")
    status = g("cost_status")

    out["prompt_tokens"] = int(pt) if pt is not None else None
    out["completion_tokens"] = int(ct) if ct is not None else None
    out["llm_model"] = str(model) if model else None

    if actual is not None:
        try:
            out["cost_usd"] = float(actual)
            out["cost_status"] = "actual"
            out["pricing_source"] = "session_db_actual"
            return out
        except (TypeError, ValueError):
            pass

    if estimated is not None:
        try:
            out["cost_usd"] = float(estimated)
            out["cost_status"] = str(status or "estimated")
            out["pricing_source"] = "session_db"
            return out
        except (TypeError, ValueError):
            pass

    # Rate-card fallback only when tokens exist but no stored cost.
    if out["prompt_tokens"] is not None or out["completion_tokens"] is not None:
        try:
            agent_root = Path.home() / ".hermes" / "hermes-agent"
            if str(agent_root) not in sys.path:
                sys.path.insert(0, str(agent_root))
            from agent.usage_pricing import (  # type: ignore
                CanonicalUsage,
                estimate_usage_cost,
            )

            usage = CanonicalUsage(
                input_tokens=out["prompt_tokens"] or 0,
                output_tokens=out["completion_tokens"] or 0,
                cache_read_tokens=int(g("cache_read_tokens") or 0),
                cache_write_tokens=int(g("cache_write_tokens") or 0),
                reasoning_tokens=int(g("reasoning_tokens") or 0),
            )
            est = estimate_usage_cost(out["llm_model"] or "", usage)
            amount = getattr(est, "amount_usd", None)
            if amount is not None:
                out["cost_usd"] = float(amount)
                out["cost_status"] = str(getattr(est, "status", None) or "estimated")
                out["pricing_source"] = str(getattr(est, "source", None) or "rate_card")
        except Exception:
            pass

    return out


def open_action_ledger(
    *,
    linear_issue_id: Optional[str],
    agent_name: str,
    credential_id: Optional[str] = None,
    job_title: Optional[str] = None,
    kanban_task_id: Optional[str] = None,
    session_id: Optional[str] = None,
    session_kind: Optional[str] = None,
) -> str:
    """Insert an open action_ledger row. Returns the row UUID.

    ``session_kind`` ('operator' | 'dispatched') is added by a HELIOS-side
    migration.  Older schemas do not have the column, so the payload builder
    tolerates its absence: PostgREST rejects the unknown key and the insert is
    retried once without it rather than losing the row.
    """
    payload: dict[str, Any] = {
        "status": "open",
        "agent_name": (agent_name or "unknown").strip() or "unknown",
        "linear_issue_id": (linear_issue_id or None),
        "credential_id": credential_id,
        "job_title": job_title,
        "kanban_task_id": kanban_task_id,
        "session_id": session_id,
    }
    if session_kind:
        payload["session_kind"] = str(session_kind)
    # If an open row already exists for this kanban task, reuse it.
    if kanban_task_id:
        q = urllib.parse.quote(str(kanban_task_id), safe="")
        existing = _request(
            "GET",
            f"/rest/v1/action_ledger?kanban_task_id=eq.{q}&status=eq.open&select=id&limit=1",
        )
        if isinstance(existing, list) and existing:
            row_id = existing[0].get("id")
            if row_id:
                return str(row_id)
    try:
        rows = _request(
            "POST",
            "/rest/v1/action_ledger",
            body=payload,
            prefer="return=representation",
        )
    except ActionLedgerError as exc:
        if not (session_kind and _column_missing(exc, "session_kind")):
            raise
        # Older schema without the column — never lose the row over it.
        payload.pop("session_kind", None)
        rows = _request(
            "POST",
            "/rest/v1/action_ledger",
            body=payload,
            prefer="return=representation",
        )
    if not isinstance(rows, list) or not rows or not rows[0].get("id"):
        raise ActionLedgerError("action_ledger open returned no id")
    return str(rows[0]["id"])


def close_action_ledger(
    ledger_id: str,
    *,
    tools_used: Any = None,
    skills_used: Any = None,
    llm_model: Optional[str] = None,
    prompt_tokens: Any = None,
    completion_tokens: Any = None,
    cost_usd: Any = None,
    cost_status: Optional[str] = None,
    pricing_source: Optional[str] = None,
    outcome: str = "completed",
    signature_md: Optional[str] = None,
    session_id: Optional[str] = None,
    profile: Optional[str] = None,
    allow_zero_cost: bool = False,
) -> str:
    """UPDATE the SAME open row to closed. Returns the row UUID.

    When ``cost_usd`` is missing and ``profile``+``session_id`` are set, fills
    tokens/$ from the worker session DB. Refuses ``outcome=completed`` with no
    cost unless ``allow_zero_cost`` (documented true zero only).
    """
    if not ledger_id or not str(ledger_id).strip():
        raise ActionLedgerError("ledger_id is required for close")
    lid = str(ledger_id).strip()
    oc = (outcome or "completed").strip() or "completed"

    est: dict[str, Any] = {}
    if (cost_usd is None or prompt_tokens is None or completion_tokens is None) and (
        profile and session_id
    ):
        est = estimate_session_cost(profile, session_id)

    cost = _as_float(cost_usd)
    if cost is None and est.get("cost_usd") is not None:
        cost = _as_float(est.get("cost_usd"))
    pt = _as_int(prompt_tokens)
    if pt is None:
        pt = _as_int(est.get("prompt_tokens"))
    ct = _as_int(completion_tokens)
    if ct is None:
        ct = _as_int(est.get("completion_tokens"))
    model = llm_model or est.get("llm_model")
    c_status = cost_status or est.get("cost_status")
    p_source = pricing_source or est.get("pricing_source")

    if cost is None and oc == "completed":
        if allow_zero_cost:
            cost = 0.0
            c_status = c_status or "estimated"
            p_source = p_source or "true_zero_no_spend"
        else:
            raise ActionLedgerIncompleteError(
                "action_ledger close refused: no cost evidence for completed "
                "outcome. Pass cost_usd, profile+session_id with usage, or "
                "allow_zero_cost for documented true zero."
            )

    body: dict[str, Any] = {
        "status": "closed",
        "end_ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "outcome": oc,
        "tools_used": _as_list(tools_used),
        "skills_used": _as_list(skills_used),
        "llm_model": model,
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "cost_usd": cost,
        "cost_status": c_status,
        "pricing_source": p_source,
        "signature_md": signature_md,
    }
    # Lineage: stamp session used for cost so story rollups join cleanly.
    if session_id:
        body["session_id"] = str(session_id).strip()

    rows = _request(
        "PATCH",
        f"/rest/v1/action_ledger?id=eq.{urllib.parse.quote(lid, safe='')}",
        body=body,
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        raise ActionLedgerError(f"action_ledger close returned no row for {lid}")
    return str(rows[0].get("id") or lid)


# ─────────────────────────────────────────────────────────────────────────────
# Session ledger (HEL-6658) — one open row per session, closed on every exit
#
# Every Hermes session opens its own ``public.action_ledger`` row at creation
# and closes it at end / expiry / failure / abandonment, without any skill or
# agent having to remember.  ``hermes_state.SessionDB.create_session`` calls
# :func:`open_session_ledger`; the gateway expiry funnel, the CLI close path and
# the process-exit hook call :func:`close_session_ledger`; the gateway expiry
# watcher calls :func:`sweep_stale_session_ledgers` on its own cadence.
#
# Identity only.  No secret material ever reaches a row: the payload carries the
# profile slug, the profile's binding credential *code* (never its value), the
# work reference when one exists, and the session id.
#
# Fail-open for the chat: a ledger failure never blocks a session.  It bumps a
# visible defect counter in ``state.db`` (``state_meta`` key
# ``session_ledger_defects``) and logs at WARNING so unopened rows stay
# detectable instead of silently vanishing.
# ─────────────────────────────────────────────────────────────────────────────

import logging as _logging

_log = _logging.getLogger("hermes.action_ledger")

SESSION_LEDGER_META_PREFIX = "session_ledger:"
SESSION_LEDGER_DEFECT_KEY = "session_ledger_defects"
OPERATOR_JOB_TITLE = "operator session"
SESSION_KIND_OPERATOR = "operator"
SESSION_KIND_DISPATCHED = "dispatched"

#: Outcomes a session row may close with.
OUTCOME_COMPLETED = "completed"
OUTCOME_EXPIRED = "expired"
OUTCOME_FAILED = "failed"
OUTCOME_ABANDONED = "abandoned"
OUTCOME_POLICY_REFUSED = "policy_refused"

#: state.db ``end_reason`` → ledger outcome.  Anything unmapped is ``completed``
#: for a clean close and ``abandoned`` for a swept one.
_END_REASON_OUTCOMES = {
    "session_expired": OUTCOME_EXPIRED,
    "expiry_finalized": OUTCOME_EXPIRED,
    "session_reset": OUTCOME_EXPIRED,
    "resume_pending_expired": OUTCOME_EXPIRED,
    "error": OUTCOME_FAILED,
    "failed": OUTCOME_FAILED,
    "crash": OUTCOME_FAILED,
    "ws_orphan_reap": OUTCOME_ABANDONED,
    "orphaned_compression": OUTCOME_ABANDONED,
}

# session_id -> (profile, session_kind, db) for the process-exit close hook.
_TRACKED_SESSIONS: dict[str, tuple[Optional[str], str, Any]] = {}
_ATEXIT_REGISTERED = False


def outcome_for_end_reason(end_reason: Optional[str], default: str = OUTCOME_COMPLETED) -> str:
    """Map a state.db ``end_reason`` onto a ledger outcome."""
    if not end_reason:
        return default
    return _END_REASON_OUTCOMES.get(str(end_reason).strip(), default)


def session_ledger_enabled() -> bool:
    """True when session rows should be written.

    Off when ``HERMES_SESSION_LEDGER`` is falsy, and — importantly for tests and
    for any install without HELIOS credentials — off when no service role is
    configured, so the hook costs one env read and never touches the network.
    """
    raw = (os.environ.get("HERMES_SESSION_LEDGER") or "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return service_role_configured()


def _meta_key(session_id: str) -> str:
    return f"{SESSION_LEDGER_META_PREFIX}{session_id}"


def _meta_read(db: Any, key: str) -> Optional[str]:
    getter = getattr(db, "get_meta", None)
    if not callable(getter):
        return None
    try:
        return getter(key)
    except Exception:
        return None


def _meta_write(db: Any, key: str, value: str) -> None:
    setter = getattr(db, "set_meta", None)
    if not callable(setter):
        return
    try:
        setter(key, value)
    except Exception as exc:
        _log.debug("session ledger meta write failed for %s: %s", key, exc)


def record_ledger_defect(db: Any, kind: str) -> None:
    """Bump the visible session-ledger defect counter in ``state.db``.

    Fail-open never means fail-silent: every skipped open, skipped close and
    missing-cost close lands here so the gap is countable.
    """
    try:
        current = _meta_read(db, SESSION_LEDGER_DEFECT_KEY)
        total = int(current) if current and str(current).isdigit() else 0
    except Exception:
        total = 0
    _meta_write(db, SESSION_LEDGER_DEFECT_KEY, str(total + 1))
    try:
        per_kind_key = f"{SESSION_LEDGER_DEFECT_KEY}:{kind}"
        current = _meta_read(db, per_kind_key)
        sub = int(current) if current and str(current).isdigit() else 0
        _meta_write(db, per_kind_key, str(sub + 1))
    except Exception:
        pass


def session_ledger_defect_count(db: Any, kind: Optional[str] = None) -> int:
    """Read the defect counter (total, or for one *kind*)."""
    key = SESSION_LEDGER_DEFECT_KEY if not kind else f"{SESSION_LEDGER_DEFECT_KEY}:{kind}"
    raw = _meta_read(db, key)
    try:
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


def resolve_credential_id(profile: Optional[str]) -> Optional[str]:
    """The profile's binding credential *code*, or None. Never a secret.

    Reads ``credential_id`` / ``credential_code`` from the profile's
    ``profile.yaml`` meta, falling back to ``HERMES_CREDENTIAL_ID``.  Returns
    None rather than guessing when no code is recorded (HAL ``null_over_guess``).
    """
    if profile:
        try:
            from hermes_cli.profiles import get_profile_dir, read_profile_meta

            meta = read_profile_meta(get_profile_dir(profile)) or {}
            for key in ("credential_id", "credential_code"):
                value = meta.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        except Exception as exc:
            _log.debug("credential code lookup failed for %s: %s", profile, exc)
    env_value = (os.environ.get("HERMES_CREDENTIAL_ID") or "").strip()
    return env_value or None


def _active_profile_name() -> Optional[str]:
    try:
        from hermes_cli.profiles import get_active_profile_name

        name = get_active_profile_name()
        return name or None
    except Exception:
        return None


def find_open_ledger_row(
    *,
    session_id: Optional[str] = None,
    kanban_task_id: Optional[str] = None,
) -> Optional[str]:
    """Return the id of an existing open row for this session/task, if any.

    Kanban workers already open on claim
    (``kanban_db._best_effort_action_ledger_open``); this is how the session
    hook finds that row and reuses it instead of opening a second one.
    """
    for column, value in (("kanban_task_id", kanban_task_id), ("session_id", session_id)):
        if not value:
            continue
        quoted = urllib.parse.quote(str(value), safe="")
        rows = _request(
            "GET",
            f"/rest/v1/action_ledger?{column}=eq.{quoted}"
            "&status=eq.open&select=id&limit=1",
        )
        if isinstance(rows, list) and rows:
            row_id = rows[0].get("id")
            if row_id:
                return str(row_id)
    return None


def _register_exit_hook() -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    try:
        import atexit

        atexit.register(close_tracked_sessions_at_exit)
        _ATEXIT_REGISTERED = True
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("session ledger atexit registration failed: %s", exc)


def open_session_ledger(
    session_id: str,
    source: Optional[str] = None,
    profile: Optional[str] = None,
    *,
    db: Any = None,
    kanban_task_id: Optional[str] = None,
    linear_issue_id: Optional[str] = None,
    job_title: Optional[str] = None,
    credential_id: Optional[str] = None,
    session_kind: Optional[str] = None,
    track_for_exit: bool = True,
) -> Optional[str]:
    """Open (or adopt) the one ledger row for *session_id*. Fail-open.

    Identity only: ``agent_name`` is the profile slug, ``credential_id`` the
    profile's binding credential code or null, ``linear_issue_id`` null for ad
    hoc sessions, ``job_title`` ``"operator session"`` when there is no work
    reference, and ``session_kind`` ``'operator'`` for ad hoc sessions or
    ``'dispatched'`` for kanban workers.

    Never opens a second row: a row already recorded for this session in
    ``state.db``, or an open row found in Supabase by ``kanban_task_id`` or
    ``session_id``, is adopted and returned.

    Returns the ledger row id, or None when the ledger is disabled or the open
    failed (in which case the defect counter is bumped and a WARNING logged).
    """
    if not session_id:
        return None
    if not session_ledger_enabled():
        return None

    meta_key = _meta_key(session_id)
    existing = _meta_read(db, meta_key) if db is not None else None
    if existing:
        try:
            recorded = json.loads(existing)
        except (TypeError, ValueError):
            recorded = None
        if isinstance(recorded, dict) and recorded.get("ledger_id"):
            return str(recorded["ledger_id"])

    agent_name = (profile or _active_profile_name() or "unknown").strip() or "unknown"
    if not kanban_task_id:
        # A worker process carries its task id in the environment; without it a
        # dispatched session would look ad hoc and open a duplicate row.
        kanban_task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip() or None
    kind = session_kind or (
        SESSION_KIND_DISPATCHED if kanban_task_id else SESSION_KIND_OPERATOR
    )
    if credential_id is None:
        credential_id = resolve_credential_id(profile)
    title = job_title or (None if linear_issue_id or kanban_task_id else OPERATOR_JOB_TITLE)

    try:
        ledger_id = find_open_ledger_row(
            session_id=session_id, kanban_task_id=kanban_task_id
        )
        if not ledger_id:
            ledger_id = open_action_ledger(
                linear_issue_id=linear_issue_id or None,
                agent_name=agent_name,
                credential_id=credential_id,
                job_title=title,
                kanban_task_id=kanban_task_id,
                session_id=session_id,
                session_kind=kind,
            )
    except Exception as exc:
        # Fail-open for the chat — but never silently.
        _log.warning(
            "session ledger open failed for %s (source=%s profile=%s): %s",
            session_id, source, agent_name, exc,
        )
        if db is not None:
            record_ledger_defect(db, "open_failed")
        return None

    if db is not None:
        _meta_write(
            db,
            meta_key,
            json.dumps(
                {
                    "ledger_id": ledger_id,
                    "profile": agent_name,
                    "session_kind": kind,
                    "source": source or None,
                }
            ),
        )
    if track_for_exit:
        _TRACKED_SESSIONS[session_id] = (profile or agent_name, kind, db)
        _register_exit_hook()
    return str(ledger_id)


def _tracked_ledger_id(db: Any, session_id: str) -> tuple[Optional[str], Optional[str]]:
    """(ledger_id, profile) recorded for *session_id* in ``state.db``."""
    raw = _meta_read(db, _meta_key(session_id)) if db is not None else None
    if not raw:
        return None, None
    try:
        recorded = json.loads(raw)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(recorded, dict):
        return None, None
    ledger_id = recorded.get("ledger_id")
    return (str(ledger_id) if ledger_id else None), recorded.get("profile")


def close_session_ledger(
    session_id: str,
    *,
    outcome: str = OUTCOME_COMPLETED,
    profile: Optional[str] = None,
    db: Any = None,
    ledger_id: Optional[str] = None,
    llm_model: Optional[str] = None,
    tools_used: Any = None,
    skills_used: Any = None,
) -> Optional[str]:
    """Close the session's ledger row with tokens/model/cost from state.db.

    Fail-open: a failure bumps the defect counter and logs WARNING, it never
    raises into the session's shutdown path.  Idempotent — a session whose row
    is already closed (its ``state_meta`` marker cleared) is a no-op.
    """
    if not session_id:
        return None
    if not session_ledger_enabled():
        return None

    recorded_id, recorded_profile = _tracked_ledger_id(db, session_id)
    lid = ledger_id or recorded_id
    resolved_profile = profile or recorded_profile
    if not lid:
        try:
            lid = find_open_ledger_row(session_id=session_id)
        except Exception as exc:
            _log.warning("session ledger lookup failed for %s: %s", session_id, exc)
            if db is not None:
                record_ledger_defect(db, "close_lookup_failed")
            return None
    if not lid:
        _TRACKED_SESSIONS.pop(session_id, None)
        return None

    est: dict[str, Any] = {}
    if resolved_profile:
        try:
            est = estimate_session_cost(resolved_profile, session_id) or {}
        except Exception as exc:  # pragma: no cover - defensive
            _log.debug("session cost read failed for %s: %s", session_id, exc)
            est = {}
    has_tokens = est.get("prompt_tokens") is not None or est.get("completion_tokens") is not None
    if est.get("cost_usd") is None and has_tokens and db is not None:
        # Tokens without a price is a real accounting gap — count it, then close
        # honestly rather than leaving the row open forever.
        record_ledger_defect(db, "cost_missing")

    try:
        closed = close_action_ledger(
            lid,
            tools_used=tools_used,
            skills_used=skills_used,
            llm_model=llm_model or est.get("llm_model"),
            prompt_tokens=est.get("prompt_tokens"),
            completion_tokens=est.get("completion_tokens"),
            cost_usd=est.get("cost_usd"),
            cost_status=est.get("cost_status"),
            pricing_source=est.get("pricing_source"),
            outcome=outcome,
            session_id=session_id,
            profile=resolved_profile,
            # A session that never billed is a documented true zero, not a
            # missing figure; the defect counter above records the ambiguous case.
            allow_zero_cost=True,
        )
    except Exception as exc:
        _log.warning(
            "session ledger close failed for %s (outcome=%s): %s",
            session_id, outcome, exc,
        )
        if db is not None:
            record_ledger_defect(db, "close_failed")
        return None

    _TRACKED_SESSIONS.pop(session_id, None)
    if db is not None:
        _meta_write(db, _meta_key(session_id), "")
    return str(closed)


def close_tracked_sessions_at_exit() -> int:
    """Close rows this process opened and never closed. Outcome ``abandoned``.

    Registered with :mod:`atexit` by :func:`open_session_ledger`, so a session
    that dies with the process still leaves a closed row.
    """
    closed = 0
    for session_id, (profile, _kind, db) in list(_TRACKED_SESSIONS.items()):
        try:
            if close_session_ledger(
                session_id, outcome=OUTCOME_ABANDONED, profile=profile, db=db
            ):
                closed += 1
        except Exception as exc:  # pragma: no cover - exit path must not raise
            _log.debug("exit close failed for %s: %s", session_id, exc)
        finally:
            _TRACKED_SESSIONS.pop(session_id, None)
    return closed


def _row_age_seconds(row: dict[str, Any], now: float) -> Optional[float]:
    started = row.get("start_ts") or row.get("created_at")
    if not started:
        return None
    text = str(started).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return now - parsed.timestamp()


def sweep_stale_session_ledgers(
    *,
    db: Any,
    profile: Optional[str] = None,
    limit: int = 200,
    min_age_s: float = 900.0,
    now: Optional[float] = None,
) -> int:
    """Close rows whose session no longer exists, with ``outcome=abandoned``.

    Runs on the gateway expiry-watcher cadence.  Scoped to this seat's
    ``agent_name`` so one host never closes another host's rows, and a row whose
    session is simply missing must also be at least *min_age_s* old (a row
    opened moments ago may just be racing its own ``state.db`` insert).  A row
    whose session row exists but has already ended is swept immediately: the
    session is over and nothing closed its ledger.

    Returns the number of rows closed.  Fail-open, like every other session
    ledger call.
    """
    if db is None or not session_ledger_enabled():
        return 0
    agent_name = (profile or _active_profile_name() or "").strip()
    if not agent_name:
        return 0
    clock = float(now if now is not None else datetime.now(timezone.utc).timestamp())

    quoted = urllib.parse.quote(agent_name, safe="")
    try:
        rows = _request(
            "GET",
            f"/rest/v1/action_ledger?status=eq.open&agent_name=eq.{quoted}"
            "&session_id=not.is.null"
            f"&select=id,session_id,start_ts,kanban_task_id&limit={int(limit)}",
        )
    except Exception as exc:
        _log.warning("session ledger sweep listing failed for %s: %s", agent_name, exc)
        record_ledger_defect(db, "sweep_failed")
        return 0
    if not isinstance(rows, list):
        return 0

    getter = getattr(db, "get_session", None)
    if not callable(getter):
        return 0

    closed = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        session_id = row.get("session_id")
        ledger_id = row.get("id")
        if not session_id or not ledger_id:
            continue
        try:
            session_row = getter(str(session_id))
        except Exception as exc:  # pragma: no cover - defensive
            _log.debug("sweep session lookup failed for %s: %s", session_id, exc)
            continue
        if session_row is None:
            age = _row_age_seconds(row, clock)
            if age is not None and age < min_age_s:
                continue
        elif not session_row.get("ended_at"):
            continue
        if close_session_ledger(
            str(session_id),
            outcome=OUTCOME_ABANDONED,
            profile=agent_name,
            db=db,
            ledger_id=str(ledger_id),
        ):
            closed += 1
    if closed:
        _log.warning(
            "session ledger sweep closed %d abandoned row(s) for %s", closed, agent_name
        )
    return closed
