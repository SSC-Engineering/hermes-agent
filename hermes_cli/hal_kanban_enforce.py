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
import uuid
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

_HAL_SCRIPT = Path.home() / ".hermes" / "scripts" / "hal_record.py"


def _hal_script() -> Path:
    override = os.environ.get("HAL_RECORD_SCRIPT")
    return Path(override).expanduser() if override else _HAL_SCRIPT


def _run(argv: list[str], timeout: float = 45.0, env=None) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
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
    cmd.extend(["--session", session or "dispatch:" + uuid.uuid4().hex])
    env = None
    if not session:
        # A gateway may itself be running in another task/session. Spawn records
        # belong to this attempt, never to the gateway's inherited identity.
        env = os.environ.copy()
        for key in ("HERMES_SESSION_ID", "HAL_LEDGER_ID", "HERMES_HAL_LEDGER_ID"):
            env.pop(key, None)
    rc, out, err = _run(cmd, env=env)
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
    # Shared helper records unknown pricing independently of work outcome.
    rc, out, err = _run(cmd, timeout=60)
    if rc == 0:
        return True
    # An actual transport or identity failure still blocks completion.
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
    """Close and verify this session's row; another attempt cannot satisfy it."""
    script = _hal_script()
    if not script.is_file():
        return False, f"HAL helper missing at {script}"
    if not session or not profile:
        return False, "HAL completion requires the worker's actual profile and session"
    ledger = open_on_spawn(
        agent=agent or profile, kanban_task_id=kanban_task_id,
        job_title=job_title or "kanban completion", session=session,
    )
    if not ledger:
        return False, "HAL could not resolve this session's ledger"
    if not _best_effort_close(ledger, profile=profile, session=session):
        return False, f"HAL could not close session ledger {ledger}"
    rc, out, err = _run(
        [sys.executable, str(script), "show", "--ledger", ledger], timeout=30,
    )
    try:
        rows = json.loads(out).get("rows", [])
    except (ValueError, AttributeError):
        return False, "HAL returned an invalid ledger readback"
    if rc or len(rows) != 1:
        return False, "HAL ledger readback failed"
    row = rows[0]
    matches = (row.get("status") == "closed"
               and row.get("session_id") == session
               and row.get("agent_name") == profile
               and row.get("kanban_task_id") == kanban_task_id)
    if not matches:
        return False, "HAL completion row does not match the current task/profile/session"
    rc, out, err = _run(
        [sys.executable, str(script), "assert-visible", "--ledger", ledger], timeout=30,
    )
    return rc == 0, out.strip() or err.strip()
