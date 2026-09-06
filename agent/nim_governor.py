"""NVIDIA NIM cross-process governor: exclusive key lease + 40 RPM + 60s 429 freeze.

Per Dan (2026-08-30): NVIDIA NIM free tier is capped at **40 requests / minute
per API key** (visible in the NVIDIA developer UI when a key is minted). A
burst of 6 kanban BUILD workers colliding on one shared key trips 429s, which
this repo's default retry path (jittered exponential backoff, then fallback to
a paid provider) escalates into a retry storm and a cost jump. Dan explicitly
overrides HEL-6108's "exp-backoff → paid fallback" for the NIM path:

    * after the FIRST HTTP 429, wait 60s and retry ONCE — do not retry-storm
    * do NOT fall through to paid providers (nous grok / anthropic / openai)
    * BUILD (kanban worker) seats must stay on NIM

This module provides three cross-process primitives, all keyed off the
credential's stable id (``PooledCredential.id``) so state persists across
worker subprocess restarts:

    * **Exclusive key lease** (``acquire_kanban_worker_lease``): when the
      dispatcher spawns a kanban worker whose provider is ``nvidia``, the
      child boot binds itself to a specific pool entry so N workers spread
      across N keys instead of piling onto the same one. The claim is
      exclusive up to ``CredentialPool.DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL``
      (1); only once every key is at that cap do extra workers layer onto
      the least-leased key (the spawn does not block). Leases are reclaimed
      after a bounded timeout when the holder dies — ``_LEASE_DEAD_PID_GRACE_SECONDS``
      once the holder PID is gone, ``_LEASE_STALE_SECONDS`` for a hung or
      off-host holder — so a killed worker's key returns to the pool with no
      operator action. Live workers keep their claim by heartbeating from
      the pre-request RPM gate.

      The lease capacity is exactly ``len(pool.entries())``: adding or
      retiring a sponsor key with ``hermes auth add/remove nvidia`` changes
      how many workers get an exclusive key, with no code change here. See
      ``docs/nvidia-sponsor-key-pool.md``.
    * **40 RPM token bucket** (``wait_for_rpm_slot`` / ``record_nim_request``):
      a rolling-60s request-timestamp log per leased key. If the log already
      has 40 entries in the last 60s, the caller sleeps until the oldest
      slot ages out, so requests never fire and 429 for being over the
      documented free-tier cap.
    * **60s freeze on 429** (``record_nim_429`` / ``nim_key_freeze_remaining``):
      a per-key freeze file. When a request 429s, the key is frozen for 60s.
      Subsequent calls that would fire against that key wait for the freeze
      to lift. One wait-and-retry per key per turn is enforced by the retry
      path in ``agent.agent_runtime_helpers.recover_with_credential_pool`` —
      this module only supplies the primitive.

All state lives under ``<HERMES_HOME>/nim_governor/`` and every mutation is
serialized with a POSIX ``fcntl.flock`` (Windows falls back to msvcrt) so
concurrent worker processes read consistent snapshots.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# fcntl on POSIX, msvcrt on Windows. Matches tools/skill_usage.py's pattern.
msvcrt = None
try:  # pragma: no cover - platform-specific import
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None
    try:
        import msvcrt as _msvcrt
        msvcrt = _msvcrt
    except ImportError:
        pass


# NVIDIA NIM free tier: 40 requests / minute / key (per NVIDIA developer UI,
# confirmed by Dan on 2026-08-30 for new keys minted the same day). Stated
# as documented capacity, not something inferred from a probe — do not raise
# without matching the on-key ceiling.
NIM_RPM_CAP = 40
NIM_RPM_WINDOW_SECONDS = 60.0

# On the first HTTP 429 for a given key, freeze the key for this many seconds
# before we allow another request against it. Dan's override of HEL-6108
# (2026-08-30): "wait 60s after first 429; bounded at ONE wait-and-retry;
# never paid fallback on NIM 429."
NIM_FREEZE_ON_429_SECONDS = 60.0

# Worker leases are refreshed by the pre-request RPM gate (see
# ``agent.chat_completion_helpers._apply_nim_rpm_gate``, which calls
# ``refresh_lease_heartbeat`` at most every
# ``LEASE_HEARTBEAT_INTERVAL_SECONDS``). Treat a lease whose heartbeat is
# older than ``_LEASE_STALE_SECONDS`` as abandoned and reclaim it. Set
# generously above the refresh interval so a worker sitting in one long
# model call isn't kicked mid-turn.
LEASE_HEARTBEAT_INTERVAL_SECONDS = 10.0
_LEASE_STALE_SECONDS = 180.0

# Bounded reclaim window for the "worker died" case (HEL-6226). When the
# holder PID is gone on this host we still wait this long after its last
# heartbeat before handing the key to someone else. Two reasons for a grace
# rather than an instant steal: (a) PIDs are recycled, so a just-observed
# "dead" PID can be a read race against a worker that is mid-exec, and
# (b) it gives the contract a single number to point at — "a killed worker's
# key returns to the pool within 15s" — instead of depending on scheduler
# timing. ``_LEASE_STALE_SECONDS`` remains the backstop for a hung worker
# whose PID is still alive, and for leases planted by another host.
_LEASE_DEAD_PID_GRACE_SECONDS = 15.0

# Absolute ceiling on how long the RPM gate will wait for a slot. The bucket
# never legitimately needs more than 60s + a hair, but this bounds pathological
# clock skew so a worker cannot wedge forever inside the pre-request gate.
_MAX_RPM_WAIT_SECONDS = 120.0

# One-shot per-key credit for post-freeze retry. Consumed by the recovery
# path in ``agent.agent_runtime_helpers.recover_with_credential_pool`` so a
# second consecutive 429 for the same key does not retry-storm.
_RETRY_CREDIT_TTL_SECONDS = 300.0

_NIM_HOST_SUBSTR = "integrate.api.nvidia.com"

# Providers whose runtime traffic hits NIM. ``nvidia-nim`` is the alias the
# provider profile also registers under; keep both in sync with
# ``plugins/model-providers/nvidia/__init__.py``.
_NIM_PROVIDER_NAMES = frozenset({"nvidia", "nvidia-nim"})


def is_nim_endpoint(base_url: Optional[str]) -> bool:
    """Whether *base_url* routes to NVIDIA NIM's shared inference host."""
    if not base_url:
        return False
    return _NIM_HOST_SUBSTR in str(base_url).lower()


def is_nim_provider(provider: Optional[str]) -> bool:
    """Whether *provider* names the NVIDIA NIM backend."""
    if not provider:
        return False
    return str(provider).strip().lower() in _NIM_PROVIDER_NAMES


def is_kanban_worker_process() -> bool:
    """Whether this process was spawned by the kanban dispatcher.

    ``HERMES_KANBAN_TASK`` is set by ``hermes_cli.kanban_db._default_spawn``
    on every worker Popen; the parent gateway/dispatcher never has it. This
    is the same signal the delegation isolation and heartbeat bridge use.
    """
    return bool(os.environ.get("HERMES_KANBAN_TASK", "").strip())


# ---------------------------------------------------------------------------
# On-disk state
# ---------------------------------------------------------------------------

def _root() -> Path:
    """State root under the active profile's HERMES_HOME."""
    return get_hermes_home() / "nim_governor"


def _leases_dir() -> Path:
    return _root() / "leases"


def _buckets_dir() -> Path:
    return _root() / "buckets"


def _freezes_dir() -> Path:
    return _root() / "freezes"


def _retry_credit_dir() -> Path:
    return _root() / "retry_credits"


def _lock_path() -> Path:
    return _root() / ".lock"


def _ensure_dirs() -> None:
    for d in (_leases_dir(), _buckets_dir(), _freezes_dir(), _retry_credit_dir()):
        d.mkdir(parents=True, exist_ok=True)


@contextmanager
def _governor_lock():
    """Serialize all governor state mutations across processes.

    One coarse lock (rather than per-key) keeps the failure modes obvious:
    the read-modify-write patterns here take microseconds and NIM workers
    are a tiny fleet, so contention on a single lock is negligible next to
    the 40 RPM ceiling this module exists to enforce.
    """
    _ensure_dirs()
    lock_file = _lock_path()
    if fcntl is None and msvcrt is None:  # pragma: no cover - unsupported
        yield
        return
    if msvcrt is not None and (not lock_file.exists() or lock_file.stat().st_size == 0):
        lock_file.write_text(" ", encoding="utf-8")
    fd = open(lock_file, "r+" if msvcrt else "a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows fallback
            fd.seek(0)
            msvcrt.locking(fd.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            fd.close()


def _safe_id(credential_id: str) -> str:
    """Sanitize credential ids into a filename-safe token."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(credential_id))[:96]


def _read_json(path: Path) -> Optional[dict]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("nim_governor: read %s failed (%s)", path, exc)
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # A malformed state file will be overwritten on next write. Treat as
        # absent rather than crashing a worker mid-turn.
        return None


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug("nim_governor: write %s failed (%s)", path, exc)
        try:
            tmp.unlink()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """Cross-platform "is this PID still running" check.

    Delegates to :func:`gateway.status._pid_exists`, this repo's single
    cross-platform implementation (psutil first; ctypes ``OpenProcess`` on
    Windows; ``/proc`` + ``ps`` zombie detection on POSIX).

    Never use ``os.kill(pid, 0)`` here. On Windows CPython routes ``sig=0``
    through ``GenerateConsoleCtrlEvent`` and hard-kills the whole console
    process group (bpo-14484) — i.e. the "just check if it's alive" call
    would kill the very kanban workers this module only means to observe.
    ``scripts/check-windows-footguns.py`` blocks that pattern in CI.

    Fails **closed** (returns True) when no backend can answer, so an
    unreadable process table can never cause a live worker's lease to be
    stolen. Heartbeat staleness is the bounded backstop for that case.
    """
    if pid <= 0:
        return False
    try:
        from gateway.status import _pid_exists

        return bool(_pid_exists(int(pid)))
    except Exception:
        pass
    try:
        import psutil  # type: ignore

        return bool(psutil.pid_exists(int(pid)))
    except Exception:
        # Unknown — treat as alive so we never steal a live worker's key.
        return True


# ---------------------------------------------------------------------------
# Lease selection
# ---------------------------------------------------------------------------

def pool_is_nim(pool) -> bool:
    """Whether *pool* is the NVIDIA NIM credential pool.

    True when the pool's provider is ``nvidia`` / ``nvidia-nim``, or when it
    is a named-custom-endpoint pool whose entries resolve to NIM's inference
    host (``hermes auth`` lets the sponsor keys be configured either way).

    Pools with no ``provider`` attribute are accepted — lightweight test and
    plugin adapters expose only ``entries()``, and
    ``credential_pool_matches_provider`` already treats an unscoped pool as
    compatible.
    """
    if pool is None:
        return False
    provider = getattr(pool, "provider", None)
    if provider is None:
        return True
    if is_nim_provider(provider):
        return True
    # Named custom endpoint pointed at NIM: judge by where the entries route.
    try:
        entries = pool.entries()
    except Exception:
        return False
    urls = []
    for entry in entries or []:
        for attr in ("runtime_base_url", "base_url", "inference_base_url"):
            value = getattr(entry, attr, None)
            if value:
                urls.append(value)
    return bool(urls) and all(is_nim_endpoint(url) for url in urls)


def max_concurrent_per_credential() -> int:
    """Per-credential concurrency cap for the exclusive claim.

    Sourced from ``agent.credential_pool.DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL``
    (currently 1) rather than re-declared here, so the cross-process kanban
    lease and the in-process ``CredentialPool.acquire_lease()`` used by
    ``tools/delegate_tool.py`` can never drift apart.
    """
    try:
        from agent.credential_pool import DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL

        return max(1, int(DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL))
    except Exception:
        return 1


def _iter_lease_files() -> Iterable[Path]:
    try:
        return list(_leases_dir().iterdir())
    except FileNotFoundError:
        return []


def _lease_matches_provider(record: dict, provider: Optional[str]) -> bool:
    """Whether *record* belongs to *provider*'s pool.

    Lease records written before provider scoping (and records with an empty
    provider) match every provider — they can only have come from the NIM
    path, which was the sole writer. ``provider=None`` matches everything.
    """
    if not provider:
        return True
    recorded = str(record.get("provider") or "").strip().lower()
    if not recorded:
        return True
    return recorded == provider


def _reclaim_stale_leases_locked(
    *,
    provider: Optional[str] = None,
    now_fn=time.time,
    pid_alive_fn=None,
) -> List[str]:
    """Drop lease files whose holder died or whose heartbeat went stale.

    Two independent reclaim rules, both bounded (HEL-6226):

    * **Heartbeat stale** — no ``refresh_lease_heartbeat`` in
      ``_LEASE_STALE_SECONDS``. Covers a hung worker, a SIGSTOPped worker,
      and any lease planted by a different host (where the PID check below
      cannot be trusted).
    * **Holder died** — the holder PID is gone on this host AND its last
      heartbeat is at least ``_LEASE_DEAD_PID_GRACE_SECONDS`` old. This is
      the fast path for the common case: a worker is killed, and its key is
      back in the pool within the grace window instead of being pinned for
      the full stale timeout.

    Returns the credential ids whose leases were reclaimed (for logging and
    for tests). ``now_fn`` / ``pid_alive_fn`` are injectable so the reclaim
    contract can be tested against a fake clock and a fake process table
    rather than by killing real processes and sleeping.
    """
    alive = pid_alive_fn if pid_alive_fn is not None else _pid_alive
    now = now_fn()
    reclaimed: List[str] = []
    for path in _iter_lease_files():
        if path.suffix != ".lease":
            continue
        record = _read_json(path)
        if not isinstance(record, dict):
            # Unparseable state file — nothing can renew it, so it would pin
            # a key forever. Drop it.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if not _lease_matches_provider(record, provider):
            continue
        pid = int(record.get("pid") or 0)
        heartbeat = float(record.get("heartbeat_at") or 0.0)
        age = now - heartbeat
        holder_gone = pid <= 0 or not alive(pid)
        if age > _LEASE_STALE_SECONDS or (
            holder_gone and age >= _LEASE_DEAD_PID_GRACE_SECONDS
        ):
            cid = record.get("credential_id")
            try:
                path.unlink()
            except OSError:
                continue
            if isinstance(cid, str) and cid:
                reclaimed.append(cid)
            logger.info(
                "nim_governor: reclaimed lease on credential %s from "
                "pid=%s task=%s (holder_gone=%s, heartbeat_age=%.1fs)",
                cid,
                pid,
                record.get("kanban_task") or "-",
                holder_gone,
                age,
            )
    return reclaimed


def _current_lease_counts_locked(
    *,
    provider: Optional[str] = None,
    now_fn=time.time,
    pid_alive_fn=None,
) -> dict:
    """Return {credential_id: active_lease_count} after reclaiming stale leases."""
    _reclaim_stale_leases_locked(
        provider=provider, now_fn=now_fn, pid_alive_fn=pid_alive_fn,
    )
    counts: dict = {}
    for path in _iter_lease_files():
        if path.suffix != ".lease":
            continue
        record = _read_json(path)
        if not isinstance(record, dict):
            continue
        if not _lease_matches_provider(record, provider):
            continue
        cid = record.get("credential_id")
        if not isinstance(cid, str) or not cid:
            continue
        counts[cid] = counts.get(cid, 0) + 1
    return counts


def lease_counts(
    *,
    provider: Optional[str] = None,
    now_fn=time.time,
    pid_alive_fn=None,
) -> dict:
    """Public read of live lease counts: ``{credential_id: holders}``.

    Reclaims stale leases as a side effect (the counts would otherwise be a
    lie). Used by the HEL-6227 tests and by anyone debugging "why did two
    workers land on the same key" from a shell.
    """
    with _governor_lock():
        return _current_lease_counts_locked(
            provider=provider, now_fn=now_fn, pid_alive_fn=pid_alive_fn,
        )


def _lease_path(credential_id: str, holder_token: str) -> Path:
    return _leases_dir() / f"{_safe_id(credential_id)}__{_safe_id(holder_token)}.lease"


def _select_lease_target(
    entry_ids: List[str], counts: dict, *, max_concurrent: int,
) -> Optional[str]:
    """Pick the credential this worker should claim.

    Exclusive first, layered only as a last resort (HEL-6226):

    1. Prefer any entry **below** ``max_concurrent`` holders — with the pool
       default of 1 that means an unleased key, so N workers over N keys get
       N distinct keys and never double-lease.
    2. Only when *every* entry is at the cap does the worker layer onto the
       least-leased entry. Spawning must not block just because more workers
       than keys are alive.

    Ties break on the pool's own entry order (priority-sorted by
    ``CredentialPool``), so selection is deterministic.
    """
    if not entry_ids:
        return None
    order = {cid: idx for idx, cid in enumerate(entry_ids)}
    below_cap = [cid for cid in entry_ids if counts.get(cid, 0) < max_concurrent]
    candidates = below_cap or entry_ids
    return min(candidates, key=lambda cid: (counts.get(cid, 0), order[cid]))


def acquire_kanban_worker_lease(
    pool,
    *,
    holder_token: Optional[str] = None,
    now_fn=time.time,
    pid_alive_fn=None,
) -> Optional[str]:
    """Bind this kanban worker to an NVIDIA NIM credential id from *pool*.

    Selection order (cross-process, keyed by ``PooledCredential.id`` so it
    survives worker subprocess restarts):

    1. Reclaim stale leases — holder PID gone for at least
       ``_LEASE_DEAD_PID_GRACE_SECONDS``, or heartbeat older than
       ``_LEASE_STALE_SECONDS``. A killed worker's key is back in the pool
       without operator action.
    2. Claim an entry below :func:`max_concurrent_per_credential` (the pool's
       own ``DEFAULT_MAX_CONCURRENT_PER_CREDENTIAL``, currently 1) — so six
       workers over six keys take six distinct keys.
    3. Only when every entry is at the cap, layer onto the least-leased
       entry. Spawn must never block just because more workers than keys are
       alive.

    **Provider-scoped.** Returns ``None`` for any pool that is not the NVIDIA
    NIM pool (see :func:`pool_is_nim`): this policy exists for one provider's
    per-key 40 RPM ceiling and must not pin credentials for anyone else.
    Lease bookkeeping is scoped to the pool's provider too, so a NIM lease
    can never be counted against — or reclaimed by — another provider's key
    that happens to share a short ``PooledCredential.id``.

    Also returns ``None`` when the pool has no entries. The caller must pair
    each successful acquire with :func:`release_kanban_worker_lease` (an
    ``atexit`` hook covers the crash-exit case).
    """
    if pool is None:
        return None
    if not pool_is_nim(pool):
        return None
    provider_key = str(getattr(pool, "provider", "") or "").strip().lower() or None
    try:
        entries = pool.entries()
    except Exception:
        return None
    entry_ids = [
        e.id for e in entries
        if isinstance(getattr(e, "id", None), str) and e.id
    ]
    if not entry_ids:
        return None
    token = holder_token or f"pid{os.getpid()}-{time.time_ns()}"
    with _governor_lock():
        counts = _current_lease_counts_locked(
            provider=provider_key, now_fn=now_fn, pid_alive_fn=pid_alive_fn,
        )
        chosen = _select_lease_target(
            entry_ids, counts, max_concurrent=max_concurrent_per_credential(),
        )
        if chosen is None:
            return None
        now = now_fn()
        record = {
            "credential_id": chosen,
            "provider": provider_key or "",
            "holder_token": token,
            "pid": os.getpid(),
            "kanban_task": os.environ.get("HERMES_KANBAN_TASK") or "",
            "acquired_at": now,
            "heartbeat_at": now,
        }
        _write_json_atomic(_lease_path(chosen, token), record)
    logger.info(
        "nim_governor: worker pid=%s task=%s leased credential %s (peers: %s)",
        os.getpid(),
        os.environ.get("HERMES_KANBAN_TASK") or "-",
        chosen,
        counts,
    )
    return chosen


def refresh_lease_heartbeat(
    credential_id: str, holder_token: str, *, now_fn=time.time,
) -> bool:
    """Bump the heartbeat on this worker's lease so it isn't reclaimed as stale.

    Called from the pre-request RPM gate (at most every
    ``LEASE_HEARTBEAT_INTERVAL_SECONDS``) — without it, a worker that holds a
    key for longer than ``_LEASE_STALE_SECONDS`` would have its own key
    reclaimed out from under it by the next worker to boot.

    Returns True when a lease record was found and refreshed, False when the
    lease is gone (already reclaimed or released).
    """
    if not credential_id or not holder_token:
        return False
    path = _lease_path(credential_id, holder_token)
    with _governor_lock():
        record = _read_json(path)
        if not isinstance(record, dict):
            return False
        record["heartbeat_at"] = now_fn()
        _write_json_atomic(path, record)
        return True


def release_kanban_worker_lease(credential_id: str, holder_token: Optional[str] = None) -> None:
    """Drop this worker's lease on *credential_id*.

    Best-effort — failures are logged at DEBUG. If *holder_token* is omitted
    every lease this PID holds on that credential is released (covers the
    atexit case where the caller only remembers the credential id).
    """
    if not credential_id:
        return
    safe_cid = _safe_id(credential_id)
    pid = os.getpid()
    with _governor_lock():
        for path in _iter_lease_files():
            if not path.name.startswith(f"{safe_cid}__"):
                continue
            record = _read_json(path)
            if not isinstance(record, dict):
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if holder_token and record.get("holder_token") != holder_token:
                continue
            if not holder_token and int(record.get("pid") or 0) != pid:
                continue
            try:
                path.unlink()
            except OSError as exc:
                logger.debug("nim_governor: release %s failed (%s)", path, exc)


# ---------------------------------------------------------------------------
# 40 RPM token bucket
# ---------------------------------------------------------------------------

def _bucket_path(credential_id: str) -> Path:
    return _buckets_dir() / f"{_safe_id(credential_id)}.json"


def _prune_bucket(timestamps: List[float], now: float) -> List[float]:
    cutoff = now - NIM_RPM_WINDOW_SECONDS
    return [t for t in timestamps if t > cutoff]


def _read_bucket_locked(credential_id: str) -> List[float]:
    payload = _read_json(_bucket_path(credential_id))
    if not isinstance(payload, dict):
        return []
    raw = payload.get("timestamps")
    if not isinstance(raw, list):
        return []
    out: List[float] = []
    for item in raw:
        try:
            out.append(float(item))
        except (TypeError, ValueError):
            continue
    return out


def _write_bucket_locked(credential_id: str, timestamps: List[float]) -> None:
    _write_json_atomic(_bucket_path(credential_id), {"timestamps": timestamps})


def wait_for_rpm_slot(credential_id: str, *, sleep_fn=time.sleep, now_fn=time.time) -> float:
    """Block until *credential_id* has a free 40 RPM slot AND is not frozen.

    Behaviour, in order (Dan 2026-08-30):

    * If the credential is currently frozen (recent 429), sleep for the
      remaining freeze window plus a tiny nudge — do not fire against a
      frozen key.
    * If the rolling 60-second bucket already has ``NIM_RPM_CAP`` entries,
      sleep until the oldest entry ages out.
    * Returns the total seconds spent waiting so callers can log slow gates.

    ``sleep_fn`` / ``now_fn`` are injectable for tests.
    """
    if not credential_id:
        return 0.0
    total_waited = 0.0
    while True:
        with _governor_lock():
            now = now_fn()
            freeze_until = _freeze_until_locked(credential_id)
            bucket = _prune_bucket(_read_bucket_locked(credential_id), now)
            wait_needed = 0.0
            if freeze_until and freeze_until > now:
                wait_needed = max(wait_needed, freeze_until - now)
            if len(bucket) >= NIM_RPM_CAP:
                # Oldest slot must age out before we can add another.
                oldest = min(bucket)
                slot_wait = (oldest + NIM_RPM_WINDOW_SECONDS) - now
                if slot_wait > 0:
                    wait_needed = max(wait_needed, slot_wait)
            if wait_needed <= 0:
                # Reserve a slot inside the same critical section so a burst
                # of concurrent callers cannot each individually see "39 in
                # bucket, 1 free" and all fire (which is the exact race the
                # coarse lock exists to close).
                bucket.append(now)
                _write_bucket_locked(credential_id, _prune_bucket(bucket, now))
                return total_waited
        # Cap each sleep tick so a caller with a Ctrl-C responsive parent
        # doesn't sit inside a single long sleep(), and bound total wait
        # against pathological clock skew.
        tick = min(wait_needed + 0.05, 5.0)
        total_waited += tick
        if total_waited >= _MAX_RPM_WAIT_SECONDS:
            logger.warning(
                "nim_governor: RPM wait for credential %s exceeded %.0fs cap; "
                "proceeding anyway (bucket may be corrupt)",
                credential_id,
                _MAX_RPM_WAIT_SECONDS,
            )
            with _governor_lock():
                bucket = _prune_bucket(_read_bucket_locked(credential_id), now_fn())
                bucket.append(now_fn())
                _write_bucket_locked(credential_id, bucket)
            return total_waited
        sleep_fn(tick)


def record_nim_request(credential_id: str, *, now_fn=time.time) -> None:
    """Record that a request just fired for *credential_id*.

    ``wait_for_rpm_slot`` normally reserves the slot itself, so most callers
    do not need this. Exposed for tests and defensive re-recording after an
    aborted request that already left the socket open on the wire.
    """
    if not credential_id:
        return
    with _governor_lock():
        bucket = _prune_bucket(_read_bucket_locked(credential_id), now_fn())
        bucket.append(now_fn())
        _write_bucket_locked(credential_id, bucket)


# ---------------------------------------------------------------------------
# 60-second freeze on 429
# ---------------------------------------------------------------------------

def _freeze_path(credential_id: str) -> Path:
    return _freezes_dir() / f"{_safe_id(credential_id)}.json"


def _freeze_until_locked(credential_id: str) -> float:
    payload = _read_json(_freeze_path(credential_id))
    if not isinstance(payload, dict):
        return 0.0
    try:
        return float(payload.get("until_ts") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def record_nim_429(credential_id: str, *, now_fn=time.time) -> float:
    """Freeze *credential_id* for :data:`NIM_FREEZE_ON_429_SECONDS` and grant one retry credit.

    Returns the epoch second at which the freeze lifts. If a freeze is
    already active and longer than the default window (e.g. an upstream
    ``Retry-After`` set it), it is left alone.
    """
    if not credential_id:
        return 0.0
    now = now_fn()
    proposed = now + NIM_FREEZE_ON_429_SECONDS
    with _governor_lock():
        current = _freeze_until_locked(credential_id)
        until = max(current, proposed)
        _write_json_atomic(
            _freeze_path(credential_id),
            {"until_ts": until, "recorded_at": now},
        )
        # One-shot retry credit: consumed by the recovery path so we retry
        # exactly once per 429, not on every subsequent one for the same key.
        _write_json_atomic(
            _retry_credit_dir() / f"{_safe_id(credential_id)}.json",
            {"granted_at": now, "expires_at": now + _RETRY_CREDIT_TTL_SECONDS},
        )
    logger.warning(
        "nim_governor: credential %s hit HTTP 429 — freezing for %.0fs "
        "(NVIDIA NIM free tier is 40 RPM/key; Dan 2026-08-30 override: no "
        "paid fallback, single wait-and-retry)",
        credential_id,
        max(0.0, until - now),
    )
    return until


def nim_key_freeze_remaining(credential_id: str, *, now_fn=time.time) -> float:
    """Seconds until *credential_id*'s freeze lifts, or 0 when not frozen."""
    if not credential_id:
        return 0.0
    with _governor_lock():
        until = _freeze_until_locked(credential_id)
    remaining = until - now_fn()
    return remaining if remaining > 0 else 0.0


def consume_retry_credit(credential_id: str, *, now_fn=time.time) -> bool:
    """Consume the one-shot post-freeze retry credit for *credential_id*.

    Returns True the first time it is called after a matching 429, False on
    every subsequent call until another :func:`record_nim_429` re-grants
    the credit. The credit expires after :data:`_RETRY_CREDIT_TTL_SECONDS`
    to prevent a stale credit from a prior turn from firing an unrelated
    retry.
    """
    if not credential_id:
        return False
    path = _retry_credit_dir() / f"{_safe_id(credential_id)}.json"
    with _governor_lock():
        payload = _read_json(path)
        if not isinstance(payload, dict):
            return False
        try:
            expires_at = float(payload.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            return False
        try:
            path.unlink()
        except OSError:
            pass
        return expires_at > now_fn()


# ---------------------------------------------------------------------------
# Combined kanban+NIM policy helpers
# ---------------------------------------------------------------------------

def is_nim_kanban_worker(agent) -> bool:
    """True when *agent* is a kanban worker whose runtime hits NVIDIA NIM.

    The rate-limit / fallback policy below applies only to this combination;
    NIM traffic from the interactive CLI or gateway sessions keeps the
    default retry / fallback behaviour untouched. Dan's override is narrowly
    about "BUILD (kanban) seats must stay on NIM".
    """
    if agent is None:
        return False
    if not is_kanban_worker_process():
        return False
    provider = (getattr(agent, "provider", "") or "").strip().lower()
    base_url = getattr(agent, "base_url", "") or ""
    return is_nim_provider(provider) or is_nim_endpoint(base_url)


def leased_credential_id(agent) -> Optional[str]:
    """Return the NIM credential id this agent has leased for the session, if any."""
    cid = getattr(agent, "_nim_worker_credential_id", None)
    if isinstance(cid, str) and cid:
        return cid
    return None


def leased_holder_token(agent) -> Optional[str]:
    """Return the lease holder token this agent registered at init, if any."""
    token = getattr(agent, "_nim_worker_holder_token", None)
    if isinstance(token, str) and token:
        return token
    return None


def heartbeat_leased_credential(agent, *, now_fn=time.time) -> None:
    """Refresh this agent's lease heartbeat, throttled to one write per interval.

    Best-effort and non-raising: a missed heartbeat only risks the lease
    being reclaimed after ``_LEASE_STALE_SECONDS``, never a failed turn.
    """
    cid = leased_credential_id(agent)
    token = leased_holder_token(agent)
    if not cid or not token:
        return
    now = now_fn()
    last = getattr(agent, "_nim_lease_heartbeat_at", 0.0) or 0.0
    try:
        last = float(last)
    except (TypeError, ValueError):
        last = 0.0
    if (now - last) < LEASE_HEARTBEAT_INTERVAL_SECONDS:
        return
    try:
        agent._nim_lease_heartbeat_at = now
    except Exception:
        pass
    try:
        refresh_lease_heartbeat(cid, token, now_fn=now_fn)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("nim_governor: heartbeat refresh failed (%s)", exc)
