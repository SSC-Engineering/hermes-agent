---
name: helios-activity-ledger
description: "Record Hermes/HELIos work in HAL with cost."
version: 1.0.0
author: Miles Turing (DISP)
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [hal, ledger, cost, visibility, kanban, spend, supabase, governance]
    related_skills:
      - ticket-signature
      - agent-spend-attribution-audit
      - agent-fleet-cost-governance
      - capability-gap-shared-skill-and-soul
---

# HAL — HELIos Activity Ledger

## Outcome

Every unit of Hermes/HELIos work is **findable in HAL** (`public.action_ledger`) with honest **cost**. No completed think is invisible.

- **Artifact name:** HAL = HELIos Activity Ledger
- **Store:** prod Supabase `mqtvvacrzmutyrznfwri` · table `public.action_ledger`
- **Hard rule (from 2026-08-11 16:00 America/Phoenix):** completed Hermes/HELIos work without HAL visibility is unacceptable
- **Cost rule:** closed rows MUST carry `cost_usd` (rate-card/session evidence). Explicit `$0` only when spend is truly none. Never fabricate dollars.

## When to use

Load this skill at the start of **any** task that spends tokens, touches kanban, runs a monitor/cron that does work, or produces a handoff the HAA should see.

Do **not** skip because:
- the task is "just a check"
- you are a monitor / facilities / overwatch profile
- kanban already opened a row (you still verify + close with cost)
- you have no Linear issue (still open HAL; leave `linear_issue_id` null)

## Credentials (names only — never print values)

```bash
# Presence check only
set -a; . ~/.config/helios/ssc/keys.env 2>/dev/null; set +a
[ -n "${SSC_SUPABASE_HELIOS_AGENTIC_OS_SERVICE_ROLE_SECRET:-}" ] && echo "HAL key: present" || echo "HAL key: MISSING"
```

Client defaults live in `hermes_cli/action_ledger.py` (loads the same keys.env).

## Canonical helper (prefer this)

```bash
# OPEN at work start (idempotent per kanban task)
python3 ~/.hermes/scripts/hal_record.py open \
  --agent "$HERMES_PROFILE" \
  --session "$HERMES_SESSION_ID" \
  --kanban "$HERMES_KANBAN_TASK" \
  --linear "$LINEAR_ISSUE_ID" \
  --credential "$HAL_CREDENTIAL_ID" \
  --job-title "$HAL_JOB_TITLE"

# CLOSE at handoff (stamps tokens + cost from session when possible)
python3 ~/.hermes/scripts/hal_record.py close \
  --ledger "$HAL_LEDGER_ID" \
  --session "$HERMES_SESSION_ID" \
  --profile "$HERMES_PROFILE" \
  --outcome completed \
  --skills helios-activity-ledger

# VERIFY a think is findable
python3 ~/.hermes/scripts/hal_record.py show --kanban "$HERMES_KANBAN_TASK"
python3 ~/.hermes/scripts/hal_record.py show --session "$HERMES_SESSION_ID"
python3 ~/.hermes/scripts/hal_record.py show --ledger "$HAL_LEDGER_ID"
```

Prints JSON. On `open`, capture `id` into env/`HAL_LEDGER_ID` for close. Kanban workers: if `action_ledger_id` is already on the task, reuse it — do not open a duplicate.

## Procedure

### 1) Open-on-claim / open-on-start

1. Resolve identity: profile slug, session id, kanban task id, Linear issue (if any), binding credential from SOUL.
2. If kanban already stamped `action_ledger_id` → use it.
3. Else run `hal_record.py open …` and keep the returned UUID.
4. Done when: you hold a ledger UUID **or** a verified existing open row for this task/session.

### 2) Work

Do the job. Prefer keeping the same session that opened the row so token/cost attribution stays clean.

### 3) Close-on-handoff (mandatory before claiming done)

1. Render signature when the work is Linear-bound (`ticket-signature` skill).
2. Close the **same PK**:
   ```bash
   python3 ~/.hermes/scripts/hal_record.py close \
     --ledger "$HAL_LEDGER_ID" \
     --session "$HERMES_SESSION_ID" \
     --profile "$HERMES_PROFILE" \
     --outcome completed \
     --signature-file /tmp/sig.md
   ```
3. Helper pulls tokens/cost from profile `state.db` + rate card when `--session` is set. Pass explicit `--cost` only when you have better evidence.
4. Done when: `show` returns `status=closed` and `cost_usd` is non-null **or** outcome is a true zero-spend case with `cost_usd=0` and `pricing_source` explaining why.

### 4) Before you say "done" to the HAA / kanban_complete

```bash
python3 ~/.hermes/scripts/hal_record.py assert-visible \
  --kanban "$HERMES_KANBAN_TASK" \
  --session "$HERMES_SESSION_ID"
```

Exit 0 = findable. Non-zero = **you are not done** — open/close first. Do not mark kanban complete on a failed assert after the hard-rule start.

## Cost honesty

| Situation | `cost_usd` | `cost_status` / `pricing_source` |
|---|---|---|
| Session has tokens; rate card hits | estimated $ | `estimated` / rate-card id |
| Provider receipt available | actual $ | `actual` / receipt id |
| Abandoned fixture, no model call | `0` | `estimated` / `true_zero_no_spend` |
| Tokens unknown | **do not close as completed without cost** | reopen path / leave open / outcome=`blocked` |

Never invent a dollar figure to clear the gate.

## Monitors, cron, facilities, overwatch

Script-only jobs (`no_agent` cron, vitals, janitors) still leave a HAL trail when they perform **fleet-mutating or HAA-visible work**:

```bash
python3 ~/.hermes/scripts/hal_record.py open --agent "cron:hal-eod" --job-title "monitor"
# ... do work ...
python3 ~/.hermes/scripts/hal_record.py close --ledger "$ID" --cost 0 --pricing-source true_zero_script_only --outcome completed
```

Token-burning agent monitors: open with real `--session` / `--profile` so cost is non-zero when models ran.

Daily EOD (4pm America/Phoenix): cron `b88093fd63b8` → `hal_eod_cron.sh` → `~/.hermes/tmp/hal/`.

## Kanban integration (mandatory path — 2026-09-12)

Dispatcher **opens HAL at spawn** and injects this skill + KANBAN_GUIDANCE HAL block into every worker.
`kanban_complete` is **fail-closed**: `hal_record.py assert-visible` must pass (auto-close of an open
row with session-backed cost is attempted first; paid dollars are never invented).

**You still:**

1. Confirm a ledger UUID after start (`action_ledger_id` on the task, `HAL_LEDGER_ID` env, or `hal_record.py show --kanban $HERMES_KANBAN_TASK`).
2. Close with session tokens/cost before complete; true `$0` only with honest `pricing_source`.
3. If platform auto-open/close failed, call `hal_record.py` yourself — platform best-effort is not a free pass to skip visibility.
4. Env note: spawn sets `HERMES_KANBAN_TASK` (not `_TASK_ID`); helper accepts both.

## Query / prove findability

```bash
python3 ~/.hermes/scripts/hal_record.py show --since-hours 24 --agent "$HERMES_PROFILE"
python3 ~/.hermes/scripts/helios_account_ledger_eod.py   # rollup → ~/.hermes/tmp/hal/
```

## Pitfalls

1. **Closing a different row** than you opened — always same UUID.
2. **Fabricated cost** — forbidden; use helper session estimate or true zero.
3. **"Monitor exemption"** — none. Every think findable.
4. **Printing service-role keys** — never; names only.
5. **Marking kanban done when assert-visible fails** — after hard-rule start, that is a process failure.
6. **Duplicate opens** — helper reuses open row for same `kanban_task_id`.

## Verification checklist

- [ ] Shared skill present: `~/.hermes/skills/autonomous-ai-agents/helios-activity-ledger/SKILL.md`
- [ ] Helper present: `~/.hermes/scripts/hal_record.py`
- [ ] This profile can load the skill (`skills_list` / path under profile `skills/`)
- [ ] SOUL contains `HAL (binding)` / `helios-activity-ledger`
- [ ] After your last completed task: `hal_record.py assert-visible` exits 0
- [ ] Closed row has non-null `cost_usd` (or documented true zero)
