# NVIDIA Sponsor Key Pool — Runbook

> **Audience:** Fleet operators
> **Source files:** `agent/nim_governor.py`, `agent/credential_pool.py`, `agent/agent_init.py`
> **Tests:** `tests/agent/test_nim_key_pool_capacity.py`, `tests/agent/test_nim_governor.py`

## What the pool does

NVIDIA NIM rate-limits the free tier **per API key** (40 requests/minute). With every
agent pointed at one key, a handful of concurrent workers turned into a retry storm that
starved the fleet. The pool fixes that in two halves:

* **Load spreading** — the shared credential pool holds the sponsor keys and
  `credential_pool_strategies.nvidia: least_used` spreads calls across them.
* **Exclusive claim** — a dispatcher-spawned kanban worker *leases* one key for the life
  of its session, so N workers over N keys get N distinct keys with no collisions. Only
  when every key is already leased does a new worker layer onto the least-leased key.

Capacity is therefore **however many keys are in the pool**. Six keys means six workers at
full speed before any sharing begins. Nothing in the code is keyed to a fixed six.

The claim is **NVIDIA-only**. Other providers keep their normal rotation; a non-NIM pool is
never pinned (`nim_governor.pool_is_nim`).

## Adding a sponsor key

Run this in the profile that owns the pool — normally the **global root**, so every profile
inherits the keys (see "Where the keys live" below).

```bash
hermes auth add nvidia --type api-key --label sponsor-<name> --api-key nvapi-...
hermes auth list nvidia          # confirm the new row and the new count
```

Capacity grows on the **next worker spawn** — no restart of the dispatcher, no code change,
no config edit. Existing workers keep the key they already leased.

### Label convention

`sponsor-<owner>` — the senior agent the NVIDIA developer account was minted under, e.g.
`sponsor-atlas`, `sponsor-vega`. Two rules:

* The label is **provenance, not ownership**. Any agent may lease any key; the label only
  tells you whose developer account to visit when a key needs rotating or its quota
  inspected.
* Keep labels unique — `hermes auth remove nvidia <label>` resolves by index, entry id, **or
  exact label**, and a duplicate label makes the removal ambiguous.

## Retiring a sponsor key

```bash
hermes auth list nvidia          # note the index / label to retire
hermes auth remove nvidia sponsor-<name>
```

Capacity drops to the remaining key count on the next spawn. A worker **already holding**
the retired key finishes its session on it (the key is removed from the pool, not revoked
mid-flight); the lease disappears with the worker.

Retire the key on the NVIDIA side only after confirming no worker still holds it:

```bash
ls "$HERMES_HOME/nim_governor/leases"    # one *.lease file per active claim
```

## Where the keys live

Keys are rows under `credential_pool.nvidia` in `auth.json`.

Kanban workers are spawned with `HERMES_HOME=<root>/profiles/<name>`. A profile that has no
`nvidia` entries of its own falls back **read-only** to the global root's pool, so adding
the sponsor keys once at the root makes them visible to every profile. As soon as a profile
gets its own `nvidia` entries, they fully shadow the global ones for that profile.

Practical consequence: **add sponsor keys at the global root**, not inside a worker profile,
unless you deliberately want that profile on a separate set of keys.

## Verifying capacity

```bash
hermes auth list nvidia                   # N credentials => N exclusive lease slots
ls "$HERMES_HOME/nim_governor/leases"     # one file per live claim
```

Each `.lease` file names the credential id, the holder PID, the kanban task, and the last
heartbeat. Two files for the same credential id mean layering is active — expected only
once every key is claimed.

## When a worker dies

A killed worker does not permanently burn a key. Two bounded reclaim rules, both in
`agent/nim_governor.py`:

| Situation | Reclaimed after | Constant |
|---|---|---|
| Holder PID is gone on this host | 15 s since its last heartbeat | `_LEASE_DEAD_PID_GRACE_SECONDS` |
| Heartbeat stopped (hung worker, SIGSTOP, lease from another host) | 180 s | `_LEASE_STALE_SECONDS` |

A live worker refreshes its heartbeat from the pre-request RPM gate, so a long model call
never loses its own key. Clean exits release immediately: the dispatcher's SIGTERM path and
the `atexit` hook both call back into the governor.

No operator action is needed for a crashed worker. If a key looks stuck for longer than the
stale timeout, check the `.lease` file's `heartbeat_at` and `pid` before deleting it by
hand — deleting a lease whose worker is still alive puts two workers on one key.

## Rate-limit behaviour (context)

Per key: 40 RPM, enforced by a cross-process token bucket before each request. On an HTTP
429 the key is frozen for 60 s and the turn is retried **once** on the same key. NIM kanban
workers do not fall back to a paid provider on a 429 — a second consecutive 429 fails the
turn and the dispatcher respawns the task. See `agent/nim_governor.py` for the constants.
