"""Authoritative task/run/event-state → 8-canonical-stage mapping (HEL-3123).

This is the single lookup the Kanban kernel writer and (separately) the
Work-in-Motion dashboard both treat as source of truth. Dashboard-side
inference from raw events is explicitly rejected — the kernel writer
persists ``tasks.current_step_key`` at mutation boundaries; readers only
read the column.

Canonical stage keys (snake_case, stored in ``tasks.current_step_key``)
match the Overwatch WiM ``STAGES`` labels 1:1:

    Intake, Classification, Gate selection, Planning,
    Execution, Verification, Release, Archive

Mapping rules (status-primary; optional run/block signals refine):

| Kanban status | Stage key        | Rationale                                      |
|---------------|------------------|------------------------------------------------|
| (new create)  | intake           | Just landed; not yet classified or planned     |
| triage        | classification   | Specifier/triager is classifying the work      |
| blocked       | gate_selection   | Human/capability/needs_input gate              |
| scheduled     | planning         | Time-parked; waiting to become dispatchable    |
| todo          | planning         | Waiting on parents / not yet ready             |
| ready         | planning         | Dispatchable; waiting for claim                |
| running       | execution        | Worker claimed and is executing                |
| review        | verification     | Awaiting review agent / human verify           |
| done          | release          | Completed; deliverable released to dependents  |
| archived      | archive          | Terminal archive                               |

Refinements (optional kwargs, never invent a 9th stage):

* ``block_kind == "dependency"`` with status ``todo`` stays ``planning``
  (dependency wait is not a human gate).
* ``run_outcome`` of ``completed`` with status ``done`` → ``release``.
* Unknown / NULL status → ``None`` (HEL-3124 owns UNKNOWN/stale handling;
  this module does not invent a sentinel stage).

The labels below are display strings only; storage and comparisons use the
snake_case keys in ``CANONICAL_STAGES``.
"""

from __future__ import annotations

from typing import Optional

# Ordered 8-stage pipeline (Overwatch WiM STAGES). Do not reorder without
# AGA review — dashboard column order and throughput math depend on it.
CANONICAL_STAGES: tuple[str, ...] = (
    "intake",
    "classification",
    "gate_selection",
    "planning",
    "execution",
    "verification",
    "release",
    "archive",
)

STAGE_LABELS: dict[str, str] = {
    "intake": "Intake",
    "classification": "Classification",
    "gate_selection": "Gate selection",
    "planning": "Planning",
    "execution": "Execution",
    "verification": "Verification",
    "release": "Release",
    "archive": "Archive",
}

# Primary status → stage. Every VALID_STATUSES member must appear here so
# the writer never leaves current_step_key NULL after a known transition.
_STATUS_TO_STAGE: dict[str, str] = {
    "triage": "classification",
    "todo": "planning",
    "ready": "planning",
    "scheduled": "planning",
    "running": "execution",
    "review": "verification",
    "blocked": "gate_selection",
    "done": "release",
    "archived": "archive",
}


def map_task_state_to_stage(
    status: Optional[str],
    *,
    block_kind: Optional[str] = None,
    run_outcome: Optional[str] = None,
    is_create: bool = False,
    previous_stage: Optional[str] = None,
) -> Optional[str]:
    """Return the canonical stage key for a task/run state, or ``None``.

    Parameters
    ----------
    status:
        Kanban ``tasks.status`` value after the mutation (or the target
        status the mutation is writing).
    block_kind:
        Optional ``tasks.block_kind`` / block_task kind. Dependency waits
        that land in ``todo`` stay in ``planning`` (not gate_selection).
    run_outcome:
        Optional closing ``task_runs.outcome``. Reserved for future
        refinements; ``completed`` + ``done`` already maps via status.
    is_create:
        When True and the new task is immediately ``ready`` (no parents,
        not triage), map to ``intake`` rather than ``planning`` so a
        freshly created card is distinguishable from one promoted to ready
        after parent completion.
    previous_stage:
        Ignored for mapping (the writer uses it only for event payloads).
        Accepted so call sites can pass a uniform kwargs shape.

    Returns
    -------
    One of ``CANONICAL_STAGES``, or ``None`` when status is unknown/empty
    (HEL-3124 backfill owns NULL/UNKNOWN rows; we do not invent one).
    """
    del run_outcome, previous_stage  # reserved; status-primary today

    if not status:
        return None
    status_key = str(status).strip().lower()
    if not status_key:
        return None

    # Fresh create into ready = Intake (just arrived on the board).
    if is_create and status_key == "ready":
        return "intake"

    # Dependency-wait blocks are routed to todo by block_task; they are
    # planning waits, not human gates. (block_kind on a true blocked row
    # still maps to gate_selection via status.)
    if status_key == "todo" and block_kind == "dependency":
        return "planning"

    stage = _STATUS_TO_STAGE.get(status_key)
    if stage is None:
        return None
    # Defensive: only ever return a canonical key.
    if stage not in CANONICAL_STAGES:
        return None
    return stage


def stage_label(stage_key: Optional[str]) -> Optional[str]:
    """Display label for a stage key, or None if unknown."""
    if not stage_key:
        return None
    return STAGE_LABELS.get(stage_key)


def is_canonical_stage(stage_key: Optional[str]) -> bool:
    """True iff ``stage_key`` is one of the 8 canonical keys."""
    return bool(stage_key) and stage_key in CANONICAL_STAGES
