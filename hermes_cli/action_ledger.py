"""Best-effort Supabase action_ledger open/close client (PRD-HEL-27 Lane A).

Opens a row on kanban claim and closes the SAME PK on complete. Failures are
surfaced as ``ActionLedgerError`` so callers can log and continue — never crash
the kanban state machine.

Env (loaded from ``~/.config/helios/ssc/keys.env`` when present):
  SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET
  SSC_SUPABASE_HELIOS_AGENTIC_OS_URL (optional; default project URL)
"""

from __future__ import annotations

import json
import os
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


class ActionLedgerError(RuntimeError):
    """Raised when the ledger cannot be opened or closed."""


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
) -> str:
    """UPDATE the SAME open row to closed. Returns the row UUID."""
    if not ledger_id or not str(ledger_id).strip():
        raise ActionLedgerError("ledger_id is required for close")
    lid = str(ledger_id).strip()

    body: dict[str, Any] = {
        "status": "closed",
        "end_ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "outcome": outcome or "completed",
        "tools_used": _as_list(tools_used),
        "skills_used": _as_list(skills_used),
        "llm_model": llm_model,
        "prompt_tokens": _as_int(prompt_tokens),
        "completion_tokens": _as_int(completion_tokens),
        "cost_usd": _as_float(cost_usd),
        "cost_status": cost_status,
        "pricing_source": pricing_source,
        "signature_md": signature_md,
    }

    rows = _request(
        "PATCH",
        f"/rest/v1/action_ledger?id=eq.{urllib.parse.quote(lid, safe='')}",
        body=body,
        prefer="return=representation",
    )
    if not isinstance(rows, list) or not rows:
        raise ActionLedgerError(f"action_ledger close returned no row for {lid}")
    return str(rows[0].get("id") or lid)
