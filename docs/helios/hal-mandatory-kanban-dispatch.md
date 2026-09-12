# HAL mandatory at kanban dispatch (2026-09-12)

Gospel for SSC fork: every kanban spawn must open a HELIos Activity Ledger row and
`kanban_complete` must fail closed without findable, honestly priced evidence.

## Store

- Supabase project `mqtvvacrzmutyrznfwri`
- Table `public.action_ledger`
- Not curator `.usage.json` / `.curator_ledger.jsonl` / `.curator_state`

## In-repo enforcement (this PR)

| Piece | Path |
|-------|------|
| Skill | `skills/autonomous-ai-agents/helios-activity-ledger/SKILL.md` |
| Enforce helpers | `hermes_cli/hal_kanban_enforce.py` |
| Prompt law | `agent/prompt_builder.py` `KANBAN_GUIDANCE` HAL section |
| Spawn inject + open | `hermes_cli/kanban_db.py` `_default_spawn` |
| Complete fail-closed | `tools/kanban_tools.py` + `kanban_db._require_action_ledger_close` |
| In-process client | `hermes_cli/action_ledger.py` |

## Operator helper

Prefer live helper:

```bash
python3 ~/.hermes/scripts/hal_record.py open|close|assert-visible|show …
```

Override path with `HAL_RECORD_SCRIPT`. Env aliases: `HERMES_KANBAN_TASK` and
`HERMES_KANBAN_TASK_ID` both accepted for `--kanban`. House copy also lives in
`SSC-Engineering/HELIOS-AGENTIC-OS` at `scripts/hal_record.py`.

## Note on `kanban_db_dispatch.py`

Live Mac installs that split dispatch into `hermes_cli/kanban_db_dispatch.py` carry
the same skill inject + spawn open there. This fork tip keeps dispatch inside
`kanban_db.py`; the gospel is adapted to that tree.
