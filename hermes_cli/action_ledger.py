"""Supabase action_ledger open/close client (PRD-HEL-27 Lane A + HAL cost gate).

Opens a row on kanban claim and closes the SAME PK on complete.

Cost discipline (HEL-4156 follow-up):
  * Close prefers session-backed tokens/$ from the worker profile ``state.db``.
  * Completed work may retain unknown cost with an explicit pricing source.
  * Zero cost requires an explicit zero and documented true-zero assertion.
  * Closure matches the existing session; it never retags another attempt.

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
import math
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
) -> str:
    """Insert an open action_ledger row. Returns the row UUID."""
    payload: dict[str, Any] = {
        "status": "open",
        "agent_name": (agent_name or "unknown").strip() or "unknown",
        "linear_issue_id": (linear_issue_id or None),
        "credential_id": credential_id,
        "job_title": job_title,
        "kanban_task_id": kanban_task_id,
        "session_id": session_id,
    }
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
    tokens/$ from the worker session DB. Explicit unknown cost needs a pricing
    source. Closure requires the existing session and never overwrites a closed row.
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
    if cost is None and cost_status != "unknown" and est.get("cost_usd") is not None:
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

    p_source = str(p_source or "").strip() or None
    if cost_usd is not None and cost is None:
        raise ActionLedgerIncompleteError("invalid cost evidence")
    if cost is not None and (not math.isfinite(cost) or cost < 0):
        raise ActionLedgerIncompleteError("cost must be finite and nonnegative")
    if cost == 0 and not (allow_zero_cost and p_source):
        raise ActionLedgerIncompleteError("zero cost requires documented true-zero evidence")
    if cost is not None and c_status == "unknown":
        raise ActionLedgerIncompleteError("unknown cost must remain null")
    if cost is None and oc == "completed" and not (c_status == "unknown" and p_source):
        raise ActionLedgerIncompleteError(
            "completed work requires cost evidence or explicit unknown cost with pricing source"
        )
    sid = str(session_id or "").strip()
    if not sid:
        raise ActionLedgerIncompleteError("session_id is required for close")

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
    # Compare-and-set on the original session and open state; never retag lineage.
    identity = (f"id=eq.{urllib.parse.quote(lid, safe='')}"
                f"&session_id=eq.{urllib.parse.quote(sid, safe='')}")
    rows = _request(
        "PATCH", f"/rest/v1/action_ledger?{identity}&status=eq.open",
        body=body, prefer="return=representation",
    )
    if isinstance(rows, list) and rows:
        return str(rows[0].get("id") or lid)
    # Repeated completion may observe the same already-closed receipt. Preserve it.
    rows = _request("GET", f"/rest/v1/action_ledger?{identity}&status=eq.closed&select=id,outcome,cost_usd,cost_status,pricing_source")
    if isinstance(rows, list) and len(rows) == 1:
        row = rows[0]
        recorded = _as_float(row.get("cost_usd"))
        documented_unknown = (row.get("cost_usd") is None and
                              row.get("cost_status") == "unknown" and
                              str(row.get("pricing_source") or "").strip())
        documented_cost = (recorded is not None and math.isfinite(recorded) and recorded > 0)
        documented_zero = (recorded == 0 and allow_zero_cost and
                           str(row.get("pricing_source") or "").strip())
        if row.get("outcome") == oc and (documented_cost or documented_zero or documented_unknown):
            return str(row.get("id") or lid)
    raise ActionLedgerError("ledger missing, session mismatch, or incompatible closed receipt")
