"""HAL enforce helpers for kanban dispatch / complete (HELIos Activity Ledger).

Opens a ledger row at worker spawn (best-effort) and fail-closes kanban_complete
unless assert-visible passes. Never fabricates cost_usd.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

_HAL_SCRIPT = Path.home() / ".hermes" / "scripts" / "hal_record.py"


def _hal_script() -> Path:
    override = os.environ.get("HAL_RECORD_SCRIPT")
    return Path(override).expanduser() if override else _HAL_SCRIPT


def _run(argv: list[str], timeout: float = 45.0) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except Exception as exc:  # noqa: BLE001
        return 99, "", str(exc)


def open_on_spawn(
    *,
    agent: str,
    kanban_task_id: str,
    job_title: Optional[str] = None,
    credential: Optional[str] = None,
    linear: Optional[str] = None,
    session: Optional[str] = None,
) -> Optional[str]:
    """Best-effort HAL open at dispatch. Returns ledger id or None."""
    script = _hal_script()
    if not script.is_file():
        logger.warning("HAL open skipped: missing %s", script)
        return None
    cmd = [
        sys.executable,
        str(script),
        "open",
        "--agent",
        agent,
        "--kanban",
        kanban_task_id,
    ]
    if job_title:
        cmd.extend(["--job-title", job_title[:200]])
    if credential:
        cmd.extend(["--credential", credential])
    if linear:
        cmd.extend(["--linear", linear])
    if session:
        cmd.extend(["--session", session])
    rc, out, err = _run(cmd)
    if rc != 0:
        logger.warning("HAL open failed rc=%s err=%s out=%s", rc, err[:200], out[:200])
        return None
    try:
        data = json.loads(out.strip().splitlines()[-1])
        row = data.get("row") or {}
        return row.get("id")
    except Exception:  # noqa: BLE001
        logger.warning("HAL open parse failed: %s", out[:300])
        return None


def _find_open_ledger(kanban_task_id: str) -> Optional[str]:
    script = _hal_script()
    if not script.is_file():
        return None
    rc, out, _err = _run(
        [sys.executable, str(script), "show", "--kanban", kanban_task_id],
        timeout=30,
    )
    if rc not in (0, 2):
        return None
    try:
        data = json.loads(out)
    except Exception:  # noqa: BLE001
        return None
    for row in data.get("rows") or []:
        if row.get("status") == "open" and row.get("id"):
            return row["id"]
    return None


def _best_effort_close(
    ledger_id: str,
    *,
    profile: Optional[str],
    session: Optional[str],
) -> bool:
    script = _hal_script()
    cmd = [
        sys.executable,
        str(script),
        "close",
        "--ledger",
        ledger_id,
        "--outcome",
        "completed",
        "--skills",
        "helios-activity-ledger",
    ]
    if profile:
        cmd.extend(["--profile", profile])
    if session:
        cmd.extend(["--session", session])
    # If session estimate missing, close may refuse without cost — allow true-zero
    # only when no tokens known; otherwise leave open and let gate fail closed.
    rc, out, err = _run(cmd, timeout=60)
    if rc == 0:
        return True
    # Retry with explicit unknown-cost path: outcome blocked is wrong for complete.
    # Prefer leaving visible open failure for the worker to fix.
    logger.warning("HAL auto-close failed rc=%s err=%s out=%s", rc, err[:240], out[:240])
    return False


def assert_visible_or_autoclose(
    *,
    kanban_task_id: str,
    profile: Optional[str] = None,
    session: Optional[str] = None,
    agent: Optional[str] = None,
    job_title: Optional[str] = None,
) -> Tuple[bool, str]:
    """Return (ok, detail). Tries auto-close of an open row before failing.

    Never invents paid cost_usd. If close cannot price the session, returns False
    so kanban_complete stays blocked.
    """
    script = _hal_script()
    if not script.is_file():
        return False, f"HAL helper missing at {script}"

    def _assert() -> Tuple[int, Any]:
        rc, out, err = _run(
            [sys.executable, str(script), "assert-visible", "--kanban", kanban_task_id],
            timeout=30,
        )
        payload: Any
        try:
            payload = json.loads(out) if out.strip() else {"raw": out, "err": err}
        except Exception:  # noqa: BLE001
            payload = {"raw": out, "err": err}
        return rc, payload

    rc, payload = _assert()
    if rc == 0:
        return True, json.dumps(payload)

    # No row → open then close best-effort from session
    if rc == 2 or (isinstance(payload, dict) and payload.get("reason") == "no_hal_row"):
        lid = open_on_spawn(
            agent=agent or profile or "unknown",
            kanban_task_id=kanban_task_id,
            job_title=job_title or "kanban complete (auto-open)",
            session=session,
        )
        if lid and session and profile:
            _best_effort_close(lid, profile=profile, session=session)
        rc2, payload2 = _assert()
        if rc2 == 0:
            return True, json.dumps(payload2)
        return False, f"HAL assert failed after auto-open: {payload2}"

    # Open row present or closed-without-cost → try close open row
    lid = _find_open_ledger(kanban_task_id)
    if lid:
        if session and profile:
            _best_effort_close(lid, profile=profile, session=session)
        else:
            # No session evidence — close with honest unknown is not allowed as
            # completed+$0. Fail closed.
            return False, (
                f"HAL open row {lid} has no session to price; "
                "pass session metadata or close manually before kanban_complete"
            )
        rc3, payload3 = _assert()
        if rc3 == 0:
            return True, json.dumps(payload3)
        return False, f"HAL assert failed after auto-close: {payload3}"

    return False, f"HAL assert-visible failed: {payload}"
