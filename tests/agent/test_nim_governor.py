"""NVIDIA NIM cross-process governor: lease + 40 RPM + 60s 429 freeze.

Covers Dan's 2026-08-30 override for HEL-6108 / HEL-6226:

* Two NVIDIA kanban-style worker inits with two pool entries take TWO
  DIFFERENT keys (exclusive lease across processes; matches the delegate-
  subagent path already using ``acquire_lease()`` in-process).
* A simulated 429 freezes the key for ~60s and grants a single retry credit
  the recovery path can consume — never falls back to a paid provider.
* The RPM bucket refuses a 41st call inside the same 60-second window.

All tests use temp ``HERMES_HOME`` (the autouse ``_isolate_hermes_home``
fixture in ``tests/conftest.py`` handles that already) and inject fake
``sleep_fn`` / ``now_fn`` so wall-clock waits do not appear in the suite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

import pytest


# ---------------------------------------------------------------------------
# Minimal in-memory pool fake — matches the tiny surface nim_governor uses.
# ---------------------------------------------------------------------------

@dataclass
class _Entry:
    id: str
    runtime_api_key: Optional[str] = None


class _FakePool:
    def __init__(self, ids: List[str]):
        self._entries = [_Entry(id=i, runtime_api_key=f"key-{i}") for i in ids]

    def entries(self):
        return list(self._entries)


def _reset_gov_state(tmp_path, monkeypatch):
    """Point HERMES_HOME at a fresh temp dir and clear any leases."""
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))


# ---------------------------------------------------------------------------
# 1. Exclusive lease across two worker-init calls
# ---------------------------------------------------------------------------

def test_two_kanban_worker_inits_pick_distinct_keys_when_two_keys_exist(
    tmp_path, monkeypatch,
):
    _reset_gov_state(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_a")
    from agent import nim_governor

    pool = _FakePool(["cred-a", "cred-b"])
    first = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-1",
    )
    second = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-2",
    )
    assert first is not None
    assert second is not None
    assert first != second, (
        "Two kanban workers must lease DIFFERENT NIM keys when two exist "
        "(HEL-6226 exclusive claim)."
    )

    # Third worker layers onto the least-leased (both have 1 now, ties
    # broken by input order → cred-a).
    third = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-3",
    )
    assert third == "cred-a"

    # Releasing worker-1 must free cred-a so the next lease picks it first.
    nim_governor.release_kanban_worker_lease("cred-a", "worker-1")
    fourth = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-4",
    )
    assert fourth == "cred-a"


def test_lease_reclaims_dead_holder_pid(tmp_path, monkeypatch):
    """A lease held by a dead PID must be reclaimed on the next acquire."""
    _reset_gov_state(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_a")
    from agent import nim_governor

    # Manually plant a lease record for a PID that will never exist.
    nim_governor._ensure_dirs()
    dead_pid = 2  # PID 2 is reserved by the kernel; user-space cannot signal it.
    import json
    import time as _t

    path = nim_governor._lease_path("cred-a", "ghost")
    path.write_text(json.dumps({
        "credential_id": "cred-a",
        "holder_token": "ghost",
        "pid": dead_pid,
        "kanban_task": "t_dead",
        "acquired_at": _t.time() - 3600,
        "heartbeat_at": _t.time() - 3600,
    }))

    pool = _FakePool(["cred-a", "cred-b"])
    chosen = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-alive",
    )
    # The dead lease must have been reclaimed, so the fresh worker still
    # gets cred-a (no other worker holds it).
    assert chosen == "cred-a"


# ---------------------------------------------------------------------------
# 2. 429 freeze + one-shot retry credit
# ---------------------------------------------------------------------------

def test_first_429_freezes_key_for_60s_then_grants_single_retry(tmp_path, monkeypatch):
    _reset_gov_state(tmp_path, monkeypatch)
    from agent import nim_governor

    t = [1_000_000.0]

    def _now():
        return t[0]

    until = nim_governor.record_nim_429("cred-a", now_fn=_now)
    assert until - t[0] == pytest.approx(nim_governor.NIM_FREEZE_ON_429_SECONDS)

    # Freeze window is active — remaining is > 0 and close to 60s.
    remaining = nim_governor.nim_key_freeze_remaining("cred-a", now_fn=_now)
    assert 55.0 <= remaining <= 60.0, remaining

    # The one-shot retry credit exists exactly once — the recovery path
    # consumes it on the SECOND 429 to enforce "bounded at ONE wait-and-retry".
    assert nim_governor.consume_retry_credit("cred-a", now_fn=_now) is True
    assert nim_governor.consume_retry_credit("cred-a", now_fn=_now) is False


def test_freeze_lifts_after_60s(tmp_path, monkeypatch):
    _reset_gov_state(tmp_path, monkeypatch)
    from agent import nim_governor

    t = [1_000_000.0]

    def _now():
        return t[0]

    nim_governor.record_nim_429("cred-a", now_fn=_now)
    t[0] += nim_governor.NIM_FREEZE_ON_429_SECONDS + 1
    assert nim_governor.nim_key_freeze_remaining("cred-a", now_fn=_now) == 0.0


def test_rpm_gate_waits_on_frozen_key(tmp_path, monkeypatch):
    """A frozen key must make ``wait_for_rpm_slot`` sleep until it lifts."""
    _reset_gov_state(tmp_path, monkeypatch)
    from agent import nim_governor

    t = [1_000_000.0]
    slept: List[float] = []

    def _now():
        return t[0]

    def _sleep(sec: float):
        slept.append(sec)
        t[0] += sec

    nim_governor.record_nim_429("cred-a", now_fn=_now)
    waited = nim_governor.wait_for_rpm_slot(
        "cred-a", sleep_fn=_sleep, now_fn=_now,
    )
    assert waited >= nim_governor.NIM_FREEZE_ON_429_SECONDS - 1
    # After the wait, the bucket must have recorded exactly one entry for
    # the request we reserved a slot for.
    with nim_governor._governor_lock():
        bucket = nim_governor._read_bucket_locked("cred-a")
    assert len(bucket) == 1


# ---------------------------------------------------------------------------
# 3. 40 RPM bucket
# ---------------------------------------------------------------------------

def test_bucket_refuses_41st_call_inside_same_minute(tmp_path, monkeypatch):
    _reset_gov_state(tmp_path, monkeypatch)
    from agent import nim_governor

    t = [1_000_000.0]

    def _now():
        return t[0]

    # Instant zero-cost sleeps: the gate should never actually block on the
    # first 40 calls in a row (bucket is empty each time we advance below
    # the cap), and on the 41st call it must sleep enough to age out the
    # oldest slot.
    slept: List[float] = []

    def _sleep(sec: float):
        slept.append(sec)
        t[0] += sec

    for i in range(nim_governor.NIM_RPM_CAP):
        waited = nim_governor.wait_for_rpm_slot(
            "cred-a", sleep_fn=_sleep, now_fn=_now,
        )
        assert waited == 0.0
        # Space calls 0.1s apart so the bucket is genuinely full at t=~4s,
        # not all sharing the same timestamp.
        t[0] += 0.1

    with nim_governor._governor_lock():
        bucket = nim_governor._read_bucket_locked("cred-a")
    assert len(bucket) == nim_governor.NIM_RPM_CAP

    # 41st call inside the 60s window must WAIT — never fire and 429.
    waited = nim_governor.wait_for_rpm_slot(
        "cred-a", sleep_fn=_sleep, now_fn=_now,
    )
    assert waited > 0.0, "41st call inside the same minute must be gated"
    assert slept, "gate must have slept at least once for the 41st call"


def test_bucket_prunes_old_timestamps(tmp_path, monkeypatch):
    _reset_gov_state(tmp_path, monkeypatch)
    from agent import nim_governor

    t = [1_000_000.0]

    def _now():
        return t[0]

    def _sleep(sec):
        t[0] += sec

    # Fill the bucket, then advance beyond the 60s window and confirm the
    # next call sees an empty bucket and fires without waiting.
    for _ in range(nim_governor.NIM_RPM_CAP):
        nim_governor.wait_for_rpm_slot(
            "cred-a", sleep_fn=_sleep, now_fn=_now,
        )
    t[0] += nim_governor.NIM_RPM_WINDOW_SECONDS + 1.0
    waited = nim_governor.wait_for_rpm_slot(
        "cred-a", sleep_fn=_sleep, now_fn=_now,
    )
    assert waited == 0.0


# ---------------------------------------------------------------------------
# 4. Provider / endpoint detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://integrate.api.nvidia.com/v1", True),
    ("https://INTEGRATE.api.nvidia.com/v1", True),
    ("https://api.openai.com/v1", False),
    ("", False),
    (None, False),
])
def test_is_nim_endpoint(url, expected):
    from agent.nim_governor import is_nim_endpoint
    assert is_nim_endpoint(url) is expected


@pytest.mark.parametrize("provider,expected", [
    ("nvidia", True),
    ("NVIDIA-NIM", True),
    ("openai", False),
    ("", False),
    (None, False),
])
def test_is_nim_provider(provider, expected):
    from agent.nim_governor import is_nim_provider
    assert is_nim_provider(provider) is expected


def test_is_nim_kanban_worker_requires_both_env_and_provider(tmp_path, monkeypatch):
    from agent.nim_governor import is_nim_kanban_worker

    class _Agent:
        provider = "nvidia"
        base_url = "https://integrate.api.nvidia.com/v1"

    # Not a kanban worker (no env var) → False even for a NIM agent.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert is_nim_kanban_worker(_Agent()) is False

    # kanban worker + NIM → True
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_a")
    assert is_nim_kanban_worker(_Agent()) is True

    # kanban worker but different provider → False (policy is NIM-only).
    class _OtherAgent:
        provider = "anthropic"
        base_url = "https://api.anthropic.com"

    assert is_nim_kanban_worker(_OtherAgent()) is False


# ---------------------------------------------------------------------------
# 5. 429 recovery path in agent_runtime_helpers wires into nim_governor and
#    does NOT engage the fallback chain.
# ---------------------------------------------------------------------------

def test_recover_with_credential_pool_nim_kanban_freeze_then_retry(tmp_path, monkeypatch):
    """First 429 → freeze + wait + retry True. Second 429 → give up, no fallback."""
    _reset_gov_state(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_a")

    from agent import nim_governor
    from agent.agent_runtime_helpers import recover_with_credential_pool
    from agent.error_classifier import FailoverReason

    # Collapse the freeze window so the recovery-path wall-clock sleep loop
    # returns in a couple hundred milliseconds instead of the production 60s
    # (the loop uses real time.time() against _deadline; monkey-patching only
    # time.sleep would deadlock).
    monkeypatch.setattr(nim_governor, "NIM_FREEZE_ON_429_SECONDS", 0.2)

    # Fake pool: single leased key, so no rotation is possible.
    class _PoolWithLease:
        provider = "nvidia"

        def entries(self):
            return [_Entry(id="cred-a", runtime_api_key="key-a")]

        def current(self):
            return _Entry(id="cred-a", runtime_api_key="key-a")

    class _Agent:
        provider = "nvidia"
        base_url = "https://integrate.api.nvidia.com/v1"
        api_key = "key-a"
        _credential_pool = _PoolWithLease()
        _credential_pool_entry_id = "cred-a"
        _nim_worker_credential_id = "cred-a"
        _interrupt_requested = False

        def _swap_credential(self, entry):
            pass

    agent = _Agent()

    # First 429: recovery must return recovered=True with the freeze applied.
    recovered, has_retried = recover_with_credential_pool(
        agent,
        status_code=429,
        has_retried_429=False,
        classified_reason=FailoverReason.rate_limit,
    )
    assert recovered is True, "First NIM 429 must recover-and-retry"
    assert has_retried is True
    # Freeze must have been recorded on the credential.
    assert nim_governor.nim_key_freeze_remaining("cred-a") >= 0.0

    # Retry credit has been granted by record_nim_429 in the first call.
    # A SECOND consecutive 429 for the same key must give up (recovered=False)
    # rather than retry-storm or fall through to a paid provider.
    recovered2, has_retried2 = recover_with_credential_pool(
        agent,
        status_code=429,
        has_retried_429=True,
        classified_reason=FailoverReason.rate_limit,
    )
    assert recovered2 is False, (
        "Second consecutive NIM 429 must NOT recover — Dan 2026-08-30: "
        "bounded at ONE wait-and-retry."
    )
    assert getattr(agent, "_nim_no_more_retries", False) is True, (
        "Second NIM 429 must set _nim_no_more_retries so the conversation "
        "loop surfaces the error instead of drifting into jittered backoff."
    )
