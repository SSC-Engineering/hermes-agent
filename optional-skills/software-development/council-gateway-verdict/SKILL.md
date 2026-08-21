---
name: council-gateway-verdict
description: "use this when posting AGA/STMA/TRC GATEWAY-VERDICT comments so CAST accepts on first try. Required on every TRC convener card. Names-only posts have already cost merge hours; this skill makes that regression structurally impossible."
version: 1.0.0
author: Cortex Limited LLC / HAA ruling OQ-FND-08 (2026-08-04) — GATEWAY-VERDICT durable canon
license: local-private
platforms: [linux, macos]
metadata:
  hermes:
    tags: [github, council, tribunal, gateway-verdict, cast, separation-of-duties, seat-signature]
    related_skills: [ssc-dan-final-approver, ticket-signature]
    auto_activate:
      - GATEWAY-VERDICT
      - council verdict
      - TRC convener
      - CAST HOLD
      - check_council_seat_signatures
---

# Council Gateway Verdict — seat-signature template

**AUTO-ACTIVATE** on: `GATEWAY-VERDICT`, `council verdict`, `TRC convener`, `CAST HOLD`,
`check_council_seat_signatures`. This skill MUST be pinned on every TRC convener card.

## Binding outcome

CAST (`check_council_seat_signatures` in
`optional-skills/software-development/ssc-dan-final-approver/scripts/approve.py`) HOLDs any
council verdict comment that carries a bare name without the seat's structural anchors.
A convener who posts names-only has already burned a merge hour to a HOLD. This skill
makes that regression impossible on the drafting side by forcing every convener to pin
the three-token template before posting.

The rule this skill encodes, and the rule CAST enforces, are the same rule:

> **A GATEWAY-VERDICT comment body MUST contain all three tokens: name AND agent slug
> AND credential.**

Names-only is a hard fail. Name+slug without credential is a hard fail. Name+credential
without slug is a hard fail. There is no exception, no bypass, no override. This skill
never instructs anyone to bypass CAST, use `--admin`, or merge.

## How to post

Post **three DISTINCT GitHub PR comments, one per seat**, then request review from
`SSC-DAN`. Never combine seats in one comment — CAST reads each seat's body
independently, and a combined comment cannot satisfy three distinct in-body signatures.

Order does not matter. AGA, STMA, and TRC each get their own comment.

## Exact template

Use these tokens verbatim. They are the CAST contract; do not paraphrase them, do not
merge them into prose, do not swap `head=<sha>` for another form on new posts.

```
GATEWAY-VERDICT: <SEAT>=GO head=<fullsha>

<Name>
<agent-slug>
<credential>
```

- `<SEAT>` is one of `AGA`, `STMA`, `TRC`.
- `<fullsha>` is the exact PR head SHA (full 40-char SHA; abbreviated 8+ also accepted
  by the marker regex, but prefer full).
- The three signature lines (`<Name>` / `<agent-slug>` / `<credential>`) MUST all appear
  in the same comment body. Empty lines between them are fine; extra prose above or
  below is fine; the tokens themselves are non-negotiable.

## Seat table (this skill is the source of truth)

| Seat | Name            | Agent slug       | Credential              |
|------|-----------------|------------------|-------------------------|
| AGA  | `Arturo Gallo`  | `arturo-gallo`   | `helios-agent-aga`      |
| STMA | `Simone Park`   | `simone-park`    | `helios-agent-stma`     |
| TRC  | `Ines Navarro`  | `ines-navarro`   | `helios-agent-trc`      |

These must match `COUNCIL_SEATS` in `approve.py` exactly. If either side changes, update
both in the same PR.

**The prior TRC identity is retired (HEL-6066, parent HEL-3387) and MUST NOT be
resurrected.** Any earlier TRC name / agent slug / credential is a retired token: it
may appear only as a negative HOLD fixture in tests, never as the live TRC seat here,
in `COUNCIL_SEATS["TRC"]`, or in a real council comment body. Do not restore retired
Hermes profiles as part of a TRC post.

## Worked AGA example

```
GATEWAY-VERDICT: AGA=GO head=abc123def

Arturo Gallo
arturo-gallo
helios-agent-aga
```

## Worked TRC example

```
GATEWAY-VERDICT: TRC=GO head=<sha>

Ines Navarro
ines-navarro
helios-agent-trc
```

Either of the above is the minimum-viable body CAST accepts for its seat. Prose framing
(reviewer notes, verdict detail, links) is welcome above or below the tokens; the tokens
themselves must be in the body.

## Also accepted: ticket_signature footer form

If a comment already renders the ticket_signature footer, it is accepted **only when
that footer still carries all three tokens together**:

```
— <Name> · credentials: <credential> · agent: <agent-slug>
```

Reading this as CAST does:

- name lives in `— <Name>`
- agent slug lives in `agent: <agent-slug>`
- credential lives in `credentials: <credential>`

An `_profile: <agent-slug>` token on its own carries the agent slug but **not** the
credential. `_profile: <slug>` without the credential also present in the body is a
hard fail under AND-all-three. If you use the `_profile:` shape, include the credential
elsewhere in the same body (bare token or `credentials: <credential>` line).

## Hard-fail block — do NOT post if any of these are true

Before you post, read your draft comment body and confirm all three of the following.
If any answer is "no", **rewrite the comment**. Do not post. Do not "just try it and
see what CAST says" — that is exactly the merge-hour-losing loop this skill exists to
prevent.

1. Does the body contain the seat's **name** (e.g. `Ines Navarro` for TRC,
   `Arturo Gallo` for AGA, `Simone Park` for STMA)?
2. Does the body contain the seat's **agent slug** (e.g. `ines-navarro` for TRC),
   either bare, as `agent: ines-navarro`, as `_profile: ines-navarro`, or inside the
   ticket_signature footer?
3. Does the body contain the seat's **credential** (e.g. `helios-agent-trc` for TRC),
   either bare or as `credentials: helios-agent-trc` inside the ticket_signature
   footer?

All three answers must be "yes". Names-only fails. Two-of-three fails.

## What CAST does

`check_council_seat_signatures` in
`optional-skills/software-development/ssc-dan-final-approver/scripts/approve.py`:

- Reads the verdict comment body for each of AGA, STMA, TRC at the exact head.
- Calls `_body_has_seat_signature(body, seat)` which returns True only when the body
  contains name AND agent-slug AND credential.
- HOLDs with a message of the form
  `council independence HOLD: missing distinct in-body per-seat signature blocks for:
  <seat> (need name='<Name>' + agent='<slug>' + credential='<credential>')`
  when any of the three tokens is missing.
- HOLDs additionally when the three resolved seats are not three distinct agents.

That HOLD is by design. It is the last line of defence against a council signature
collapse (one identity signing all three seats) and against copy-paste posts that
silently drop the seat anchors.

## Non-goals

- This skill does not authorize `--admin`, does not authorize bypassing CAST, and
  does not authorize merges. The Charter §14.4 stop conditions and the SSC-DAN
  final-approver contract are unchanged.
- This skill does not add a fourth Charter stop and does not add a new human gate.
- This skill does not change `COUNCIL_SEATS` values, `MANDATORY_AUTHORITIES`, or the
  three-distinct-seats requirement.

## Success criterion

The next council post using this skill's template CAST-accepts on the first try, with
no HOLD, no rewrite loop, and no merge-hour cost.
