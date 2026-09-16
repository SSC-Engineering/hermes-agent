# Explicit assignment scope

A reviewer seat may also receive ordinary artifact work. The worker context renderer now accepts one task-body line `HELIOS_TASK_MODE: artifact`, `dispatch`, or `review`. It renders the selected duty in each new worker's existing task context; it does not edit profiles or mutate a running conversation's cached system prefix.

Artifact and dispatch modes explain that unrelated formal verdicts and commit SHAs must not be invented. Review mode retains required review evidence. Every mode preserves existing authorization, HAL, safety, and completion gates. Unknown or repeated declarations produce a diagnostic and select no mode. Tasks without a declaration keep their existing context.

For an in-workspace completion contract, the renderer distinguishes the control file from up to twenty resolved artifact destinations. Reads are bounded to 64 KiB and paths cannot resolve outside the workspace. Invalid control inputs produce a diagnostic without printing file contents.

This is framework-owned context guidance. It does not enforce filesystem read-only access, prevent a worker from changing task metadata, replace formal approval checks, or prove that every model follows the guidance. Independent artifact verification remains required. The observed incidents were a worker writing its report over the contract and multiple seats applying review duties to non-review preparation tasks.
