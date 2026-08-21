---
name: ssc-dan-final-approver
description: "HAA-authorized, CTO-controlled procedure for casting the FINAL GitHub approval on SSC-Engineering pull requests using the SSC-DAN identity. Separation of duties: SSC-ENG does all work, SSC-DAN only approves, the same SSC-ENG engineer merges. Gated to the ellis-turing profile only; enforces exact-head gates, mandatory AGA+STMA+TRC verdicts, no-ACEA-block, Linear linkage, and a signed CTO attribution block on every approval."
version: 1.5.0
author: Cortex Limited LLC / Arturo Gallo, AGA / HAA directive 2026-07-31 / D-1 marker-author enforcement 2026-08-02 / OQ-FND-08 2026-08-04 / HEL-6044 user-owner protection-API 403 2026-08-18
license: local-private
platforms: [linux, macos]
metadata:
  hermes:
    tags: [github, approval, identity, credential-boundary, separation-of-duties, audit, release, cto]
    related_skills: [ssc-github-approval-identity, engineering-definition-of-done, ticket-signature, helios-agent-cto]
---

# SSC-DAN Final Approver

## Binding outcome

This skill implements the HAA's "Final Approver" authority for Ellis Turing
(CTO). It is a SEPARATE identity and a SEPARATE skill from
`ssc-github-approval-identity` (the `obviouslogic-ai` / Rhea-Ramos+Simone-Park
lane). Do not conflate the two — they exist for different reasons:

| | `ssc-github-approval-identity` | `ssc-dan-final-approver` (this skill) |
|---|---|---|
| GitHub identity | `obviouslogic-ai` (HAA-owned, cross-org) | `SSC-DAN` (SSC-org native, near-full-admin classic PAT) |
| Holders | `rhea-ramos` (primary), `simone-park` (backup) | `ellis-turing` ONLY |
| Role | RRA merge-lane owner casts approval, then merges | CTO technical reviewer casts approval, engineer merges |
| Purpose | no-bleed exception so an outside identity can approve SSC work | in-org separation of duties: the approver is never the merger |

Authority model (HAA, binding, 2026-07-31):

- **SSC-ENG** = the engineers. All WORK (branches, commits, PRs, CI) runs
  through this identity. SSC-ENG never holds or invokes the SSC-DAN
  credential and never approves its own PR.
- **SSC-DAN** = approver only, never a worker. Its PAT casts the FINAL GitHub
  approval and nothing else — no push, no merge, no admin action, no
  authoring.
- **Flow:** (1) engineer (SSC-ENG) does the work, opens the PR, CI goes
  green, Council/Tribunal sign-offs (AGA/STMA/TRC) post to the PR; (2)
  **ellis-turing** (CTO, high-level TECHNICAL reviewer, NOT a PM) reads the
  WHOLE PR — charter compliance, no bugs, no conflicts, Linear linked,
  Council+Tribunal passed, no ACEA block — then casts the GitHub approval AS
  SSC-DAN through this gateway; (3) the **same SSC-ENG engineer** who opened
  the PR picks it back up and performs the merge. This gateway never merges.

This reconciles with the existing canon in `OBV-HELIos/L0-ontology/Approval
and Agent-Creation Authority.md` (Rule 1): CI green + AGA PASS + STMA PASS +
TRC PASS + no ACEA terminal BLOCK, cast only by an authorized role — that
canon names `rhea-ramos` and `ellis-turing` as the two roles authorized to
cast a delegated API approval. This skill is `ellis-turing`'s enactment of
that authority using a distinct in-org identity (SSC-DAN) instead of the
cross-org `obviouslogic-ai` identity Rhea/Simone use. The canon is not
weakened: this is a second lane for the same rule, not an exception to it.

## Authorized holder (exactly one)

**Ellis Turing, CTO (`ellis-turing`)** is the sole authorized holder. No
other agent, manager, or exec — especially no PM — may load or invoke this
skill or reach the SSC-DAN credential. The gateway (`scripts/approve.py`)
hard-checks `HERMES_PROFILE == "ellis-turing"` before it will even look at
the credential path; every other profile gets `GateError` immediately, no
credential file is ever read on their behalf.

`SSC_DAN_GITHUB_PAT` lives in `~/.config/helios/ssc/keys.env` (the shared
SSC secrets store loaded by `load-ssc-env.sh`) — that loader is a broad,
shell-wide convenience export used by everyone with SSC access. This skill
does NOT use that loader. It reads a narrow, single-purpose, single-holder
copy instead: see Provisioning below. This means even if `ellis-turing`'s
shell sources `load-ssc-env.sh`, that shell-wide `SSC_DAN_GITHUB_PAT` /
`SSC_GITHUB_TOKEN` export is irrelevant to this gateway and is never read by
`approve.py` — the gateway drops it during `clean_reexec()` along with every
other inherited credential before it reads its own isolated file.

## CTO review checklist (binding, run before every invocation)

Ellis (or any future `ellis-turing`-profile session) must verify ALL of the
following by reading the actual PR, not by trusting a claim, before invoking
the gateway:

1. **CI is green** on GitHub at the exact current head SHA (not self-reported
   by the engineer).
2. **AGA (Architecture Guardian)** posted a PASS/APPROVED verdict at that
   exact head — charter compliance, no architecture violations.
3. **STMA (Security & Threat Modeling)** posted a PASS/APPROVED verdict at
   that exact head — no unmitigated vulnerabilities.
4. **TRC (Technical Review Council / tribunal)** posted a PASS/APPROVED
   verdict at that exact head — the independent technical verdict.
5. **No ACEA terminal BLOCK** at that exact head. No block is present is
   NOT clearance by itself — items 1-4 are the clearance; this is only a
   negative check.
6. **No bugs** — read the diff. A green CI and passing gates are necessary,
   not sufficient; this is a human-grade technical read, not a rubber stamp.
7. **No merge conflicts** — the PR is cleanly mergeable and not behind base
   (the gateway also verifies this mechanically and fails closed if not).
8. **Linear is linked** — the PR title or body references the Linear issue
   id, and that Linear issue is in the correct pre-merge review state (the
   gateway mechanically verifies the PR references the id you pass it; you
   verify Linear's state and content by reading Linear directly).
9. **This is genuinely SSC-ENG's PR** — author is the `SSC-ENG` identity, not
   `SSC-DAN` itself and not some other identity (the gateway enforces this).

If any item fails, do NOT invoke the gateway. Post the signed HOLD (with the
signature block) naming the exact gap, then route by gap TYPE — do NOT dead-end
and wait for the HAA:

- **Missing Council/Tribunal sign-offs (items 2-4: AGA / STMA / TRC absent at
  head)** — this is NOT a rejection, it is an unfinished gate. AUTO-SUMMON the
  tribunal: create a kanban task assigned to `ines-navarro` (TRC coordinates) with
  the PR number + exact head SHA, instructing AGA + STMA + TRC to independently
  review the exact head and post their signed GATEWAY-VERDICT markers, and to
  confirm no ACEA block. State in the HOLD comment that the tribunal has been
  summoned and the PR will return to the CTO for final approval once all markers
  are PASS at head. The CTO does not idle — a missing-tribunal HOLD triggers the
  council automatically and the approval flow resumes when they clear.
  Dispatch shape:
  `hermes kanban create "TRIBUNAL SIGN-OFF (auto-summoned by CTO HOLD): PR #<n> head <sha> — AGA+STMA+TRC post signed verdicts at exact head; all-PASS returns to CTO" --assignee ines-navarro --created-by <approver> --workspace dir:$HOME/CoWork`
- **HEAD DRIFT (council PASSed, but new commits landed since — the PASS markers
  are at an OLD head, not the current one)** — this is NOT the CTO's to re-summon,
  and NOT a straight re-review. The head belongs to the ENGINEER. Hand the PR back
  to the **engineer (SSC-ENG)**: they must bring the branch back into alignment
  (rebase/resolve/confirm the new head is the intended final state), THEN the
  engineer **summons the council for re-approval at the new head** (the engineer,
  not the CTO, owns re-triggering the gate on their own head). Council re-approves
  at the new head → engineer/TRC re-requests `SSC-DAN` → CTO re-approves. State in
  the HOLD comment: "head drifted from <old> to <new>; returned to engineer to
  realign and re-summon council." Dispatch shape:
  `hermes kanban create "HEAD-DRIFT REALIGN: PR #<n> drifted <old>→<new> after council PASS — engineer realign the branch, confirm final head, then SUMMON council (AGA+STMA+TRC) to re-approve at the new head; all-PASS → request SSC-DAN → CTO" --assignee <engineer> --created-by <approver> --workspace dir:$HOME/CoWork`
- **A real defect (item 6 bug, item 7 conflict, item 1 genuine CI failure)** —
  hand back to the engineer (SSC-ENG) with the specifics to fix.
- **A governance conflict** (charter/ACEA-level, not a fixable defect) — escalate
  to the HAA.

The rule: a HOLD caused by *absent* gates self-heals by summoning those gates; a
HOLD caused by *head drift* returns to the ENGINEER to realign then summon the
council; a HOLD caused by a *defect* returns to the builder; only a true
governance conflict reaches the HAA. Never leave a summonable HOLD sitting idle.

## The approval LOOP always returns to the CTO (binding — no dead ends)

Every path through review MUST loop back to the CTO. There is no terminal state
except "CTO approved" or "HAA governance escalation." Trace all branches:

1. **CTO HOLD: council ABSENT** → CTO auto-summons the tribunal (above) → go to (2)/(3).
2. **Council/Tribunal reviews and FAILS (any of AGA/STMA/TRC posts HOLD/REJECT, or
   ACEA BLOCK)** → the failing reviewer posts the signed defect + hands the PR back
   to the **engineer (SSC-ENG)**. The engineer does the fix, pushes a new head, and
   **re-opens the PR for approval — which means re-requesting `SSC-DAN` as the
   reviewer on the PR** (this is what re-triggers the CTO). New head = full
   re-review; prior PASS markers at an old head do NOT carry forward.
3. **Council/Tribunal ALL PASS at exact head (+ no ACEA block)** → the gate is
   green. Whoever posts the final passing marker (TRC coordinator) **re-requests
   `SSC-DAN` as the reviewer on the PR**, which re-triggers the CTO to come do the
   final approval read and cast.
4. **CTO re-triggered (from 2's resubmit or 3's pass)** → runs the full checklist
   again at the current head → approves (casts SSC-DAN) if all green, or loops
   back to (2)/(3) if a new gap appears.

**TWO PARTIES MUST REMEMBER TO REQUEST SSC-DAN AS THE APPROVER — this is the
trigger that wakes the CTO. If neither does, the loop stalls:**
- **The ENGINEER**, every time they resubmit a fixed PR (path 2), must add
  `SSC-DAN` as a requested reviewer on the PR.
- **The COUNCIL** (TRC coordinator), when all verdicts PASS (path 3), must add
  `SSC-DAN` as a requested reviewer on the PR.

Mechanically: `gh pr edit <n> --add-reviewer SSC-DAN` (or the equivalent
`requested_reviewers` API call). The CTO watches for `SSC-DAN` review-requests on
gate-relevant PRs; a review-request is the CTO's wake signal.

## Signature protocol (binding, on every approval comment)

Every SSC-DAN approval review body ends with a CTO attribution block so the
approval is auditable and attributable to a specific model/session/human
role. Render it with the canonical script BEFORE invoking the gateway, save
it to a file, and pass that file via `--signature`:

```bash
python3 "$HERMES_REAL_HOME/.hermes/scripts/ticket_signature.py" \
  ellis-turing "$HERMES_SESSION_ID" > /tmp/ellis-signature.txt
```

That produces (from `ellis-turing/SOUL.md` + real `state.db` telemetry):

```
---
— Ellis Turing · credentials: helios-agent-cto (CTO) · agent: ellis-turing

**🪙 Token usage (from Hermes state.db — real per-session data)**

| session | model | in | out | reasoning | est cost |
|---|---|---|---|---|---|
| `<session>` | <model> | <in> | <out> | <reasoning> | $<cost> (<act|est>) |
| **TOTAL** | — | **<in>** | **<out>** | | **$<cost>** |

_profile: ellis-turing · cost estimated unless marked (act)._

_CPTC actual: compare these real tokens with the predicted Complexity Points
on the technical-scope sub-issue._
```

This already carries: model used (the `model` column), token count for the
review (`in`/`out`/`reasoning` + TOTAL), his name (Ellis Turing), his
position via the credential (CTO, from `helios-agent-cto`), and his
credentials list (backticked `CERTIFICATION (binding)` entries in
`ellis-turing/SOUL.md` — currently `helios-agent-cto`). If a future SOUL.md
adds more binding credentials, they render automatically; do not hand-edit
this block.

The gateway (`approve.py`) hard-requires `--signature <path>` and refuses to
approve without it (`GateError` if missing), and rejects a signature file
that itself contains a `GATEWAY-VERDICT:` marker line (that marker syntax is
reserved for verdict authorities, not the CTO signature).

## Exact gate enforced by the gateway (fail-closed)

`scripts/approve.py approve` refuses unless ALL of:

- Invoking profile is exactly `ellis-turing`.
- Target owner is exactly `SSC-Engineering` or `SSC-ENG`.
- Target PR is open, non-draft, and authored by `SSC-ENG` (not by `SSC-DAN`,
  not by anyone else).
- PR is at the exact head you pass (`--expected-head`, when supplied) and is
  not behind base / not conflicted (`mergeable == true`).
- Base branch protection (classic or ruleset, either source — same
  read-both-sources logic as the RRA gateway) requires >=1 approving review
  with stale-review dismissal, and strict required-status-checks.
  **v1.5 exception (HEL-6044):** if GET
  `/repos/{owner}/{repo}/branches/{base}/protection` or
  `/repos/{owner}/{repo}/rules/branches/{base}` returns HTTP 403 whose
  message contains `Upgrade to GitHub Pro or make this repository public`,
  AND the owner is in `ALLOWED_REPO_OWNERS` AND the owner is not
  `ALLOWED_OWNER` (i.e. the allowed user owner `SSC-ENG`), treat that as no
  readable protection surface — the same empty/`None` shape as today's 404
  path. Record source `none-readable:protection-api-403-user-owner`. Do
  **not** raise "protected branch does not require an approving review".
  Any other 403 stays `GateError`. Org-owned `SSC-Engineering` repos stay
  on the existing path (404/empty protection still fail-closed).
- Every required status-check context is green on the exact head (`success`,
  `neutral`, or `skipped` conclusion; commit statuses require `success`).
  Any non-`success` conclusion among required checks is recorded as
  `degraded_required_checks` and surfaced in the review body — never silent.
  **When required contexts are empty because protection was unreadable
  (v1.5 user-owner Pro-upgrade 403):** do **not** call
  `check_required_statuses` with `[]`. Require observed exact-head
  check-runs instead: at least one `success`; every completed run
  `success`/`neutral`/`skipped`; pending/`failure`/`timed_out`/`cancelled`
  is fatal.
  **One narrow, hardcoded exception (v1.1, DEV-001 circular-dep fix):** the
  single named context `AI-authored PR human review` (produced by
  `pr-gate.yml` on `pull_request_review: submitted/dismissed`, counting
  approving reviews from non-bot/non-author users) is excused from the fatal
  set ONLY when its check run is `completed` with conclusion exactly
  `failure` — the documented self-satisfying shape where the SSC-DAN approval
  this gateway is about to cast is exactly what flips that context green
  (submitting the approval fires the review event, the job re-runs, the
  context passes). A missing/never-run check, or any other conclusion
  (`cancelled`, `timed_out`, `action_required`, `stale`) for that same
  context, is NOT excused and remains fully fatal. Every OTHER required
  context has zero exception. The excusable set (`SELF_SATISFYING_CONTEXTS`
  in `approve.py`) is a hardcoded, single-entry Python frozenset with no CLI
  flag, environment variable, or argument that can add to it — extending it
  requires a code change and a fresh TRC review. Every exclusion is recorded
  as `excused_self_satisfying_checks` in the audit record and printed in the
  approval review body, identically to a degraded check — never silent.
- `--verdict` includes AGA, STMA, AND TRC, each `PASS` or `APPROVED`, each
  evidenced by a `GATEWAY-VERDICT: <AUTHORITY>=<STATUS> head=<sha>` marker
  line on a PR comment/review at the exact head. No legacy prose fallback —
  this is a newer gateway than `ssc-github-approval-identity` and starts
  marker-mandatory from day one.
- No `GATEWAY-VERDICT: ACEA=<unfavorable> head=<exact-head>` marker exists on
  any PR comment (a positive ACEA marker or ACEA silence both pass; an
  unfavorable ACEA marker at the exact head is fatal).
- `--linear <ISSUE-ID>` is well-formed (`TEAM-123`) and that exact id string
  appears in the PR title or body.
- `--deployment-impact VERIFIED_NO_UNAUTHORIZED_APPLY` is passed literally.
- `--signature <path-to-rendered-block>` is passed and non-empty.

Any failure raises `GateError`, prints `APPROVAL_GATE_REJECTED: <reason>` to
stderr, exits 1, and writes an `approval_rejected` audit record — no partial
credit, no silent downgrade.

## Invocation

```bash
python3 "$HERMES_REAL_HOME/.hermes/skills/software-development/ssc-dan-final-approver/scripts/approve.py" \
  --repo SSC-ENG/<repository> \
  --pr <number> \
  --linear <ISSUE-ID> \
  --verdict AGA:PASS=https://github.com/SSC-ENG/<repository>/pull/<number>#issuecomment-<id> \
  --verdict STMA:PASS=https://github.com/SSC-ENG/<repository>/pull/<number>#issuecomment-<id> \
  --verdict TRC:PASS=https://github.com/SSC-ENG/<repository>/pull/<number>#issuecomment-<id> \
  --deployment-impact VERIFIED_NO_UNAUTHORIZED_APPLY \
  --signature /tmp/ellis-signature.txt \
  --reason "Full PR read: charter-compliant, no bugs, no conflicts, Linear linked, Council+Tribunal PASS, no ACEA block"
```

Verdict evidence marker format (each verdict author posts this exact line,
unformatted, on its own line, on the target PR):

```
GATEWAY-VERDICT: AGA=PASS head=<full-sha-or-8+-hex-prefix>
GATEWAY-VERDICT: STMA=PASS head=<full-sha-or-8+-hex-prefix>
GATEWAY-VERDICT: TRC=PASS head=<full-sha-or-8+-hex-prefix>
```

Markdown bold/backtick wrapping or trailing punctuation on that line makes it
parse as zero markers — same pitfall as `ssc-github-approval-identity`. If a
verdict author's marker doesn't parse, bounce it back for republication at
the same exact head; never approve on prose alone with this gateway.

### Marker author is mechanically enforced (gateway v1.3 / OQ-FND-08)

`check_verdict_evidence()` does **not** accept marker *text* alone. For every
mandatory-authority marker the gateway relies on, it also requires the
resolved comment's `user.login` to satisfy the self-review test reoriented by
**HAA ruling OQ-FND-08, 2026-08-04**:

1. **Council markers authored by the PR author (`SSC-ENG`) are VALID.**
   Council-layer independence does **not** rest on GitHub login inequality.
2. **NOT** the approver identity (`SSC-DAN` / `APPROVAL_LOGIN`) — fail closed
   only if the approver authored the markers. Posting a verdict marker is a
   forbidden op for the approver credential.
3. **No login allow-list.** `AUTHORIZED_VERDICT_POSTERS` remains an empty
   legacy constant and is intentionally unused. HAA clause 6 closed
   per-seat GitHub identities / relay logins on cost.

Council independence is asserted **structurally** via
`check_council_seat_signatures()`: the three mandatory authorities (AGA /
STMA / TRC) must each present a DISTINCT in-body per-seat signature block
naming that seat (Arturo Gallo / `arturo-gallo` / `helios-agent-aga`,
Simone Park / `simone-park` / `helios-agent-stma`, Ines Navarro /
`ines-navarro` / `helios-agent-trc`). Missing or collapsed seats are a
HOLD, not a login failure. Name AND agent slug AND credential are all
required (AND-all-three); name+slug without credential is HOLD.

Each accepted verdict records `marker_author_login` (and
`council_seat_agent` when applicable) on the audit / basis record and prints
it in the approval review body. A missing `user.login` on the evidence
comment is fatal (approver-identity self-review cannot be enforced).

### THERE IS NO DRY-RUN / PROBE MODE (binding pitfall — 2026-08-01 incident)

`approve.py approve` ALWAYS casts a real GitHub approval when its gates pass.
There is no `--dry-run`, no `--check`, no probe flag. The ONLY non-casting
call is `--verify-identity` (below). Never invoke `approve` "just to see if
the gate passes" — if the gate passes, you have approved the PR for real.

CATASTROPHIC PITFALL, actually hit: passing a `--verdict <AUTH>:PASS=<url>`
whose URL resolves to a comment that merely *contains an instructional
GATEWAY-VERDICT example line* (e.g. the CTO's own HOLD comment, which quotes
the required marker format). The gateway trusts the comment you point it at
and parses the marker on it — so it accepted a HOLD-comment's example line as
a genuine STMA verdict and cast an approval STMA had never actually given.
Result: an invalid approval on a PR whose STMA marker did not yet exist.

RULES to never repeat it:
- Every `--verdict` URL MUST point at the GENUINE verdict-authority comment
  (the one the authority itself posted as its verdict), never at a HOLD,
  summary, instructional, or example comment — especially never at your own
  CTO comments.
- Before invoking `approve`, list the real marker-bearing comments and
  confirm each authority's marker lives on THAT AUTHORITY's own comment:
  `gh api repos/<owner>/<repo>/issues/<pr>/comments --paginate -q '.[] | . as $c | ($c.body|split("\n")[]) as $l | select($l|test("^GATEWAY-VERDICT: (AGA|STMA|TRC)=PASS head=<sha>$")) | "\($c.id)\t\($c.user.login)\t\($l)"'`
  A marker that only appears inside your own HOLD/summary comment is NOT a
  verdict — it is text you wrote. Exclude those comment ids from `--verdict`.
- If a required authority has NO genuine marker comment yet, DO NOT invoke
  `approve` at all. Block and wait for the real marker.

If you cast in error anyway: containment is immediate and mandatory —
`gh api -X PUT repos/<owner>/<repo>/pulls/<pr>/reviews/<review_id>/dismissals
-f event=DISMISS -f message="RETRACTED …"` (use your ordinary/SSC-ENG auth,
NOT the SSC-DAN gateway token — dismissal moves fail-closed and is not an
approval action), verify `reviewDecision` reverts and `mergedAt` is null,
post a signed correction on the PR, preserve the audit record (never delete
it), and file a security-incident task to STMA+HAA (STMA owns impact
assessment; the CTO cannot self-close a self-caused incident).

### Identity probe (no GitHub write)

```bash
python3 "$HERMES_REAL_HOME/.hermes/skills/software-development/ssc-dan-final-approver/scripts/approve.py" --verify-identity
```

Confirms the credential resolves to `SSC-DAN` with active `SSC-Engineering`
membership, under the isolated gateway environment, without approving
anything.

## After approval: the engineer merges

The gateway's job ends at `APPROVED`. The next actor is the ORIGINAL
`SSC-ENG` engineer who opened the PR — they pick the PR back up and merge it
through their normal `engineering-definition-of-done` / `github-pr-workflow`
flow. This gateway never merges, never pushes, never touches branch
protection or repo administration, even though the underlying SSC-DAN PAT is
a near-full-admin classic PAT with far broader platform capability. That
excess capability is treated as hazardous per the same principle as
`ssc-github-approval-identity`: approval capability is not bypass capability.

## Audit contract

Every invocation records to
`$HERMES_REAL_HOME/.hermes/profiles/ellis-turing/home/.local/state/helios/ssc-dan-approval-audit.jsonl`
(mode 0600, parent dir 0700): invoking agent, approval identity, repo/PR,
exact head/base SHA, protection source, required checks (+ any degraded, +
any excused self-satisfying context per the v1.1 DEV-001 exception),
ACEA check result, every verdict authority/status/URL/evidence-mode/
marker_author_login, Linear issue, deployment-impact attestation, UTC
timestamp, and outcome
(`validated` intent record, then `approved` or `unexpected_review_response`
outcome record) — or `approval_rejected` with the exact gate reason on
failure. The approving GitHub review body carries the same non-secret
evidence plus the CTO signature block.

## Forbidden operations and incident response

Forbidden with the SSC-DAN identity through this gateway or any other path:

- merging (any form, including a nominally normal merge);
- direct push to any branch;
- branch-protection or ruleset changes;
- repository/org/user/team/integration administration (even though the PAT
  technically can — this skill never exercises that scope);
- authoring code, commits, branches, PRs, issues, labels, or ordinary
  comments outside the one approving review;
- approving a PR authored by anyone other than `SSC-ENG`;
- token export, shell login persistence, `gh auth login` with this token, or
  copying/printing the token outside this gateway's isolated read.

If an agent attempts or performs a forbidden operation with this identity:

1. STOP the approval lane immediately. Do not retry or conceal the attempt.
2. Preserve the local audit record and relevant GitHub audit URLs without
   copying the secret.
3. Block the task as a security incident; notify the HAA and STMA with
   actor, attempted action, repository, PR/resource, timestamp, outcome.
4. Treat the credential as potentially compromised. The HAA owns
   revocation/rotation authorization; STMA owns impact assessment.
5. Review every action by `SSC-DAN` since the last known-good audit record
   before resuming.
6. Re-provision only after rotation/containment and a fresh audit.

## Provisioning (credential-gating design)

Approved secret path — exactly one holder, exactly one file:

`$HERMES_REAL_HOME/.hermes/profiles/ellis-turing/home/.config/helios/ssc-dan-approval.env`

Required controls (already provisioned as part of this skill's rollout):

- `home/.config/helios` directory mode `0700`;
- secret file mode `0600`;
- exactly one variable in the file: `APPROVAL_GITHUB_TOKEN` (holds the
  SSC-DAN PAT value copied from `SSC_DAN_GITHUB_PAT` in
  `~/.config/helios/ssc/keys.env` at provisioning time — the gateway never
  reads `keys.env` or `load-ssc-env.sh` directly, only this isolated file);
- provisioned by direct local write, never via command-line argument, shell
  expansion, terminal echo, chat, git, or logs;
- no copy exists for any other profile — `rhea-ramos`, `simone-park`,
  retired TRC slugs (`tessa-cole`, `theo-lang`), any PM profile, or any other
  agent MUST NOT resolve this skill or hold this file.
  `scripts/propagate.py --check` asserts that.
- same skill + holder set on every node this profile runs on (Dan-Mac,
  `hermes-review`/M4 if `ellis-turing` is dispatched there).

The Hermes profiles on a node share an OS account, so file modes are
defense-in-depth (they stop other processes reading the file, not a
malicious process running as the same OS user). The actual security boundary
is: one copy, one holder profile, no automatic shell-wide sourcing of this
specific file, an isolated subprocess environment (`clean_reexec` drops
every inherited credential before the token is read), a durable append-only
audit, and rapid rotation on any violation. Strong per-agent OS isolation
would need separate OS identities or an external secrets broker — a future
hardening item, not falsely claimed here.

## Verification

Run after any protocol, holder, or node change:

```bash
python3 "$HERMES_REAL_HOME/.hermes/skills/software-development/ssc-dan-final-approver/scripts/propagate.py"
```

Then verify, without printing values:

- exactly `ellis-turing` resolves this skill (via canonical symlink) and has
  the pointer line in its `SOUL.md`;
- exactly `ellis-turing` has the secret file, at `0700`/`0600`;
- no other profile (`rhea-ramos`, `simone-park`, any PM/manager profile, any
  engineer profile) resolves this skill or holds the file;
- `python3 scripts/approve.py --verify-identity` (run as `ellis-turing`)
  returns identity `SSC-DAN` with active `SSC-Engineering` membership;
- no approval is emitted during verification.

Structural presence is not behavioral proof. Provisioning is proven only
when the isolated identity probe succeeds and no unauthorized profile holds
the credential.
