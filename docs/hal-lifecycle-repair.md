# HAL lifecycle and handoff completion

The runtime owns session identity and accounting closure. Unknown price is stored as `cost_usd=null`, `cost_status=unknown` and an explicit source; it does not turn completed work into blocked work. A zero still requires pricing evidence.

Spawn rows have a unique provisional identity. Workers adopt only their handed-off provisional row. Subsequent attempts get separate rows. Completion resolves the current task/profile/session, closes its row, and verifies the exact row. A historical or merely open row cannot satisfy completion.

Tasks can opt into observable completion requirements with one body line:

    HELIOS_COMPLETION_CONTRACT: review/handoff-contract.json

Paths are relative to and confined to the task workspace. Version 1 contracts support `tasks` selected by ID or idempotency key, expected assignee/model/provider/runtime settings and `allowed_statuses`; `artifacts` require a nonempty file and optionally validate a workspace JSON Schema. The shared database completion function checks this contract, covering CLI and tool completion. This validates dispatch state and artifact shape, not semantic review or role authorization.

Example:

```json
{"version":1,"tasks":[{"idempotency_key":"release-api-lane","assignee":"api-specialist","allowed_statuses":["ready","running","done"]}],"artifacts":[{"path":"review/result.json","json_schema":"review/result.schema.json"}]}
```

Deployment must include both the shared HAL helper and the runtime completion adapter. Existing processes may retain imported modules; new workers load the deployed version. Never overwrite another session's ledger to reconcile a retry. Reconcile historical attempts separately from current work and retain unknown costs when no supported price exists.
