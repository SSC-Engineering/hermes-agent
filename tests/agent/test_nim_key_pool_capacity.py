"""HEL-6227 — NVIDIA sponsor-key pool: claim, capacity, layering, reclaim.

Proves the operator-facing contract of the NIM sponsor-key pool (HEL-6225)
and the exclusive clean-key claim that backs it (HEL-6226):

* Six concurrent NVIDIA workers take six DISTINCT keys, and no key is
  double-leased while any key is still free.
* A seventh worker layers onto the least-leased key — no error, no block.
* A worker killed without releasing has its key reclaimed after a bounded
  timeout, and the key returns to the pool.
* ``least_used`` spreads calls across every key over N selections.
* A profile with no per-provider NVIDIA entries resolves the sponsor keys
  from the global-root pool.
* Adding or removing a key changes lease capacity with no code change.
* The claim is provider-scoped: a non-NIM pool is never pinned.

Determinism: every test injects ``now_fn`` (a fake clock) and, where the
"worker died" path is exercised, ``pid_alive_fn`` (a fake process table).
Nothing here sleeps, spawns a process, or touches the network — the reclaim
rules are asserted against injected time rather than against the scheduler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import List, Optional

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"


class _Clock:
    """Injectable monotonic-enough clock. ``clock()`` is the ``now_fn``."""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += seconds
        return self.now


@dataclass
class _Entry:
    id: str
    access_token: str = ""
    base_url: Optional[str] = NIM_BASE_URL

    @property
    def runtime_api_key(self) -> str:
        return self.access_token

    @property
    def runtime_base_url(self) -> Optional[str]:
        return self.base_url


class _FakePool:
    """The tiny surface ``nim_governor`` reads off a credential pool."""

    def __init__(self, ids: List[str], provider: str = "nvidia"):
        self.provider = provider
        self._entries = [
            _Entry(id=cid, access_token=f"nvapi-{cid}") for cid in ids
        ]

    def entries(self):
        return list(self._entries)


def _use_temp_home(tmp_path, monkeypatch, subdir: str = "hermes"):
    home = tmp_path / subdir
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_hel6227")
    return home


def _sponsor_entries(count: int, start: int = 1) -> List[dict]:
    """``count`` NVIDIA sponsor-key rows as they live in auth.json."""
    return [
        {
            "id": f"nim-{i}",
            "label": f"sponsor-{i}",
            "auth_type": "api_key",
            "priority": i - start,
            "source": "manual",
            "access_token": f"nvapi-sponsor-{i}",
            "base_url": NIM_BASE_URL,
        }
        for i in range(start, start + count)
    ]


def _write_auth_store(home, entries: List[dict]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {"nvidia": entries}}, indent=2)
    )


def _plant_lease(nim_governor, credential_id, holder_token, *, pid, heartbeat_at):
    """Write a lease record directly — stands in for a worker process.

    Used where the holder must be a PID this test controls (the "worker was
    killed" cases); the normal path goes through
    ``acquire_kanban_worker_lease`` like production does.
    """
    nim_governor._ensure_dirs()
    path = nim_governor._lease_path(credential_id, holder_token)
    path.write_text(json.dumps({
        "credential_id": credential_id,
        "provider": "nvidia",
        "holder_token": holder_token,
        "pid": pid,
        "kanban_task": f"t_{holder_token}",
        "acquired_at": heartbeat_at,
        "heartbeat_at": heartbeat_at,
    }))
    return path


# ---------------------------------------------------------------------------
# 1. Six workers => six distinct keys (no double-lease below the threshold)
# ---------------------------------------------------------------------------

def test_six_concurrent_workers_lease_six_distinct_keys(tmp_path, monkeypatch):
    _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor

    clock = _Clock()
    pool = _FakePool([f"nim-{i}" for i in range(1, 7)])

    leased = [
        nim_governor.acquire_kanban_worker_lease(
            pool, holder_token=f"worker-{i}", now_fn=clock,
            pid_alive_fn=lambda _pid: True,
        )
        for i in range(1, 7)
    ]

    assert None not in leased, "every one of six workers must get a key"
    assert len(set(leased)) == 6, (
        f"six workers over six keys must take six DISTINCT keys, got {leased}"
    )
    assert set(leased) == {f"nim-{i}" for i in range(1, 7)}

    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=lambda _pid: True,
    )
    assert counts == {f"nim-{i}": 1 for i in range(1, 7)}, (
        "no key may be double-leased while any key is still free"
    )


def test_no_double_lease_at_any_point_below_the_layering_threshold(
    tmp_path, monkeypatch,
):
    """Assert the invariant after EVERY acquire, not just at the end."""
    _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor

    clock = _Clock()
    pool = _FakePool([f"nim-{i}" for i in range(1, 7)])

    for i in range(1, 7):
        nim_governor.acquire_kanban_worker_lease(
            pool, holder_token=f"worker-{i}", now_fn=clock,
            pid_alive_fn=lambda _pid: True,
        )
        counts = nim_governor.lease_counts(
            provider="nvidia", now_fn=clock, pid_alive_fn=lambda _pid: True,
        )
        assert sum(counts.values()) == i
        assert max(counts.values()) == 1, (
            f"after {i} workers a key was already shared: {counts}"
        )


# ---------------------------------------------------------------------------
# 2. Seventh worker layers onto the least-leased key
# ---------------------------------------------------------------------------

def test_seventh_worker_layers_onto_least_leased_key(tmp_path, monkeypatch):
    _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor

    clock = _Clock()
    pool = _FakePool([f"nim-{i}" for i in range(1, 7)])
    for i in range(1, 7):
        nim_governor.acquire_kanban_worker_lease(
            pool, holder_token=f"worker-{i}", now_fn=clock,
            pid_alive_fn=lambda _pid: True,
        )

    seventh = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-7", now_fn=clock,
        pid_alive_fn=lambda _pid: True,
    )

    assert seventh is not None, "the seventh worker must run, not error or block"
    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=lambda _pid: True,
    )
    assert sum(counts.values()) == 7
    assert counts[seventh] == 2
    assert sorted(counts.values()) == [1, 1, 1, 1, 1, 2], (
        "layering must land on ONE key, not redistribute the others"
    )

    # The eighth layers onto a still-single key, never doubling up on the
    # key that is already carrying two.
    eighth = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-8", now_fn=clock,
        pid_alive_fn=lambda _pid: True,
    )
    assert eighth != seventh
    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=lambda _pid: True,
    )
    assert sorted(counts.values()) == [1, 1, 1, 1, 2, 2]


# ---------------------------------------------------------------------------
# 3. Stale-lease reclaim when a worker dies without releasing
# ---------------------------------------------------------------------------

def test_killed_worker_lease_reclaimed_after_stale_timeout(tmp_path, monkeypatch):
    """A holder whose heartbeat stops is reclaimed at the bounded timeout.

    The holder PID is reported alive throughout (a hung/SIGSTOPped worker,
    or a lease planted by another host) so this exercises the heartbeat
    rule specifically, and shows it is bounded on BOTH sides: not reclaimed
    a second early, reclaimed once the window passes.
    """
    _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor

    clock = _Clock()
    always_alive = lambda _pid: True  # noqa: E731
    pool = _FakePool(["nim-1", "nim-2"])

    dead_worker = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-doomed", now_fn=clock,
        pid_alive_fn=always_alive,
    )
    survivor = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-alive", now_fn=clock,
        pid_alive_fn=always_alive,
    )
    assert dead_worker != survivor

    # Just inside the window: the doomed worker's key is still pinned. The
    # survivor keeps heartbeating, exactly as the pre-request RPM gate does
    # on every turn — that is what separates "alive" from "abandoned".
    clock.advance(nim_governor._LEASE_STALE_SECONDS - 1)
    nim_governor.refresh_lease_heartbeat(survivor, "worker-alive", now_fn=clock)
    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=always_alive,
    )
    assert counts.get(dead_worker) == 1, (
        "a lease must NOT be reclaimed before the stale timeout elapses"
    )

    # Past the window: reclaimed, and the key is free again.
    clock.advance(2)
    nim_governor.refresh_lease_heartbeat(survivor, "worker-alive", now_fn=clock)
    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=always_alive,
    )
    assert dead_worker not in counts, "stale lease must be reclaimed"
    assert counts.get(survivor) == 1, "a live worker's lease must survive"

    # The key returns to the pool: the next worker to boot takes it.
    replacement = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-replacement", now_fn=clock,
        pid_alive_fn=always_alive,
    )
    assert replacement == dead_worker, (
        "the reclaimed key must be handed to the next worker, not layered"
    )


def test_killed_worker_key_returns_within_dead_pid_grace(tmp_path, monkeypatch):
    """A worker whose PID is gone frees its key on the shorter grace window."""
    _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor

    clock = _Clock()
    ghost_pid = 424242
    dead_pids = {ghost_pid}
    alive = lambda pid: pid not in dead_pids  # noqa: E731

    _plant_lease(
        nim_governor, "nim-1", "worker-killed",
        pid=ghost_pid, heartbeat_at=clock.now,
    )
    pool = _FakePool(["nim-1", "nim-2"])

    # Before the grace elapses the key stays pinned — a "dead" PID read can
    # race a worker that is mid-exec, so the steal is deliberately delayed.
    clock.advance(nim_governor._LEASE_DEAD_PID_GRACE_SECONDS - 1)
    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=alive,
    )
    assert counts.get("nim-1") == 1

    # Past the grace the key is back in the pool, well inside the much
    # longer heartbeat-stale timeout.
    clock.advance(2)
    assert clock.now < nim_governor._LEASE_STALE_SECONDS + 1_700_000_000.0
    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=alive,
    )
    assert "nim-1" not in counts

    chosen = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-next", now_fn=clock, pid_alive_fn=alive,
    )
    assert chosen == "nim-1"


def test_heartbeat_keeps_a_long_running_worker_from_losing_its_key(
    tmp_path, monkeypatch,
):
    """A worker that keeps heartbeating past the stale window keeps its key."""
    _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor

    clock = _Clock()
    always_alive = lambda _pid: True  # noqa: E731
    pool = _FakePool(["nim-1", "nim-2"])
    cid = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-slow", now_fn=clock,
        pid_alive_fn=always_alive,
    )

    for _ in range(4):
        clock.advance(nim_governor._LEASE_STALE_SECONDS - 1)
        assert nim_governor.refresh_lease_heartbeat(
            cid, "worker-slow", now_fn=clock,
        ) is True

    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=always_alive,
    )
    assert counts.get(cid) == 1, (
        "a heartbeating worker must not have its own key reclaimed under it"
    )


def test_release_returns_the_key_to_the_pool_immediately(tmp_path, monkeypatch):
    """The clean-exit path frees the key without waiting for any timeout."""
    _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor

    clock = _Clock()
    always_alive = lambda _pid: True  # noqa: E731
    pool = _FakePool(["nim-1", "nim-2"])
    cid = nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-1", now_fn=clock, pid_alive_fn=always_alive,
    )

    class _FakeAgent:
        _nim_worker_credential_id = cid
        _nim_worker_holder_token = "worker-1"
        _credential_pool = pool

    agent = _FakeAgent()
    nim_governor.release_agent_lease(agent)

    assert nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=always_alive,
    ) == {}
    assert agent._nim_worker_credential_id is None
    # Idempotent: the forced-exit path and atexit can both fire.
    nim_governor.release_agent_lease(agent)


# ---------------------------------------------------------------------------
# 4. least_used spread across keys
# ---------------------------------------------------------------------------

def test_least_used_strategy_spreads_calls_across_every_key(tmp_path, monkeypatch):
    """Over N selections no key is starved and none is hammered."""
    home = _use_temp_home(tmp_path, monkeypatch)
    _write_auth_store(home, _sponsor_entries(6))
    (home / "config.yaml").write_text(
        "credential_pool_strategies:\n  nvidia: least_used\n"
    )

    from agent.credential_pool import STRATEGY_LEAST_USED, get_pool_strategy, load_pool

    assert get_pool_strategy("nvidia") == STRATEGY_LEAST_USED, (
        "spread is config-driven: credential_pool_strategies.nvidia"
    )

    pool = load_pool("nvidia")
    assert len(pool.entries()) == 6

    picks: dict = {}
    for _ in range(60):
        entry = pool.select()
        assert entry is not None
        picks[entry.id] = picks.get(entry.id, 0) + 1

    assert len(picks) == 6, f"a key was starved: {picks}"
    assert set(picks.values()) == {10}, (
        f"least_used must spread 60 calls evenly over 6 keys, got {picks}"
    )


# ---------------------------------------------------------------------------
# 5. Global-pool fallback for a profile with no per-provider entry
# ---------------------------------------------------------------------------

def test_profile_without_nvidia_entries_resolves_global_sponsor_keys(
    tmp_path, monkeypatch,
):
    """A profile that never ran ``hermes auth add nvidia`` still sees the keys.

    Kanban workers are spawned with ``HERMES_HOME=<root>/profiles/<name>``
    (``hermes_cli.kanban_db._default_spawn``). The sponsor keys are added
    once at the global root; the profile inherits them read-only.
    """
    root = tmp_path / "hermes"
    profile_home = root / "profiles" / "build"
    profile_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_hel6227")

    # Profile store exists but has NO nvidia entries.
    (profile_home / "auth.json").write_text(
        json.dumps({"version": 1, "credential_pool": {}}, indent=2)
    )
    # Global root holds the six sponsor keys.
    _write_auth_store(root, _sponsor_entries(6))

    from hermes_cli.auth import _global_auth_file_path, read_credential_pool

    assert _global_auth_file_path() == root / "auth.json", (
        "profile mode must resolve a global-root fallback path"
    )
    resolved = read_credential_pool("nvidia")
    assert [e["id"] for e in resolved] == [f"nim-{i}" for i in range(1, 7)], (
        "a profile with no per-provider entry must resolve the global keys"
    )

    from agent.credential_pool import load_pool

    pool = load_pool("nvidia")
    assert len(pool.entries()) == 6

    # And the fallback keys are leasable: six workers, six distinct keys.
    from agent import nim_governor

    clock = _Clock()
    leased = [
        nim_governor.acquire_kanban_worker_lease(
            pool, holder_token=f"worker-{i}", now_fn=clock,
            pid_alive_fn=lambda _pid: True,
        )
        for i in range(1, 7)
    ]
    assert len(set(leased)) == 6


def test_profile_entries_shadow_the_global_pool(tmp_path, monkeypatch):
    """Once the profile has its own nvidia keys, the global ones are ignored."""
    root = tmp_path / "hermes"
    profile_home = root / "profiles" / "build"
    profile_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(profile_home))

    _write_auth_store(profile_home, _sponsor_entries(2, start=90))
    _write_auth_store(root, _sponsor_entries(6))

    from hermes_cli.auth import read_credential_pool

    assert [e["id"] for e in read_credential_pool("nvidia")] == ["nim-90", "nim-91"]


# ---------------------------------------------------------------------------
# 6. Capacity follows the key count — no code change
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key_count", [3, 6, 8])
def test_lease_capacity_equals_key_count_with_no_code_change(
    tmp_path, monkeypatch, key_count,
):
    """N keys in auth.json => N exclusive leases before layering starts."""
    home = _use_temp_home(tmp_path, monkeypatch)
    _write_auth_store(home, _sponsor_entries(key_count))

    from agent import nim_governor
    from agent.credential_pool import load_pool

    clock = _Clock()
    pool = load_pool("nvidia")
    leased = [
        nim_governor.acquire_kanban_worker_lease(
            pool, holder_token=f"worker-{i}", now_fn=clock,
            pid_alive_fn=lambda _pid: True,
        )
        for i in range(key_count)
    ]
    assert len(set(leased)) == key_count

    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=lambda _pid: True,
    )
    assert max(counts.values()) == 1
    # One more worker than keys: layering, never an error.
    assert nim_governor.acquire_kanban_worker_lease(
        pool, holder_token="worker-extra", now_fn=clock,
        pid_alive_fn=lambda _pid: True,
    ) is not None


def test_adding_and_retiring_a_key_changes_capacity_without_code_change(
    tmp_path, monkeypatch,
):
    """Add a key => capacity grows. Retire a key => capacity shrinks.

    The whole operation is an edit to the credential pool (what
    ``hermes auth pool add/remove nvidia`` writes); nothing in the lease
    path is keyed to a fixed six.
    """
    home = _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor
    from agent.credential_pool import load_pool

    def _distinct_lease_capacity(entry_ids, clock):
        """How many workers get their own key before layering starts."""
        pool = _FakePool(entry_ids)
        seen = []
        for i in range(len(entry_ids) + 2):
            cid = nim_governor.acquire_kanban_worker_lease(
                pool, holder_token=f"probe-{clock.now}-{i}", now_fn=clock,
                pid_alive_fn=lambda _pid: True,
            )
            if cid in seen:
                break
            seen.append(cid)
        return len(seen)

    # Baseline: five sponsor keys on disk.
    _write_auth_store(home, _sponsor_entries(5))
    ids = [e.id for e in load_pool("nvidia").entries()]
    assert len(ids) == 5

    clock = _Clock()
    assert _distinct_lease_capacity(ids, clock) == 5

    # Add a sixth key — pool edit only, no code change.
    _write_auth_store(home, _sponsor_entries(6))
    ids = [e.id for e in load_pool("nvidia").entries()]
    assert len(ids) == 6
    clock = _Clock(start=clock.now + 10_000)  # fresh window: old leases stale
    assert _distinct_lease_capacity(ids, clock) == 6

    # Retire one — capacity drops cleanly to five.
    _write_auth_store(home, _sponsor_entries(6)[:-1])
    ids = [e.id for e in load_pool("nvidia").entries()]
    assert len(ids) == 5
    clock = _Clock(start=clock.now + 10_000)
    assert _distinct_lease_capacity(ids, clock) == 5


# ---------------------------------------------------------------------------
# 7. Provider scoping — the claim is NVIDIA-only
# ---------------------------------------------------------------------------

def test_non_nim_pool_is_never_leased(tmp_path, monkeypatch):
    """Other providers keep their normal rotation; nothing is pinned."""
    _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor

    clock = _Clock()
    anthropic_pool = _FakePool(["ant-1", "ant-2"], provider="anthropic")
    for entry in anthropic_pool.entries():
        entry.base_url = "https://api.anthropic.com"

    assert nim_governor.pool_is_nim(anthropic_pool) is False
    assert nim_governor.acquire_kanban_worker_lease(
        anthropic_pool, holder_token="worker-1", now_fn=clock,
    ) is None
    assert nim_governor.lease_counts(now_fn=clock) == {}


def test_nim_lease_counts_ignore_another_providers_leases(tmp_path, monkeypatch):
    """Lease bookkeeping is provider-scoped even on a shared credential id."""
    _use_temp_home(tmp_path, monkeypatch)
    from agent import nim_governor

    clock = _Clock()
    nim_pool = _FakePool(["shared-id", "nim-2"])
    nim_governor.acquire_kanban_worker_lease(
        nim_pool, holder_token="nim-worker", now_fn=clock,
        pid_alive_fn=lambda _pid: True,
    )

    # A lease record from a different provider on the same credential id.
    nim_governor._ensure_dirs()
    path = nim_governor._lease_path("shared-id", "other-provider-worker")
    path.write_text(json.dumps({
        "credential_id": "shared-id",
        "provider": "openrouter",
        "holder_token": "other-provider-worker",
        "pid": 1,
        "acquired_at": clock.now,
        "heartbeat_at": clock.now,
    }))

    counts = nim_governor.lease_counts(
        provider="nvidia", now_fn=clock, pid_alive_fn=lambda _pid: True,
    )
    assert counts == {"shared-id": 1}, (
        "another provider's lease must not count against a NIM key"
    )
