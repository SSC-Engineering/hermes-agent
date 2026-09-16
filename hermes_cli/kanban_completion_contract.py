"""Verify operator-declared handoff results at the shared completion boundary.

A task may carry a single HELIOS_COMPLETION_CONTRACT: relative/file.json line.
This checks observable dispatch/artifact facts, not semantic product acceptance.
"""
import json
from pathlib import Path

PREFIX = 'HELIOS_COMPLETION_CONTRACT:'


def verify_completion_contract(conn, task_id):
    task = conn.execute('SELECT body, workspace_path FROM tasks WHERE id=?', (task_id,)).fetchone()
    if task is None:
        return
    lines = [line[len(PREFIX):].strip() for line in (task['body'] or '').splitlines() if line.startswith(PREFIX)]
    if not lines:
        return
    def fail(reason):
        raise ValueError('Completion contract not satisfied: ' + reason)
    if len(lines) != 1 or not task['workspace_path']:
        fail('one contract and a workspace are required')
    root = Path(task['workspace_path']).resolve()
    def scoped(raw):
        if not isinstance(raw, str) or not raw.strip():
            fail("path must be a nonempty string")
        path = (root / raw).resolve()
        if not path.is_relative_to(root):
            fail('contract paths must stay inside the workspace')
        return path
    try:
        contract = json.loads(scoped(lines[0]).read_text())
    except (OSError, ValueError) as exc:
        fail(str(exc))
    if not isinstance(contract, dict) or contract.get('version') != 1:
        fail('unsupported contract version')
    if set(contract) - {'version','tasks','artifacts'}:
        fail('unknown contract fields')
    for expected in contract.get('tasks', []):
        selector = 'id' if expected.get('id') else 'idempotency_key'
        if not expected.get(selector):
            fail('task selector missing')
        rows = conn.execute(f'SELECT * FROM tasks WHERE {selector}=?', (expected[selector],)).fetchall()
        if len(rows) != 1:
            fail('expected one task for ' + str(expected[selector]))
        row = rows[0]
        for field, value in expected.items():
            if field in {'id','idempotency_key'}:
                continue
            if field == 'allowed_statuses':
                if row['status'] not in value:
                    fail(row['id'] + ' has status ' + row['status'])
            elif field in {'assignee','model_override','provider_override','workspace_path','max_runtime_seconds','max_retries'}:
                if row[field] != value:
                    fail(row['id'] + ' mismatches ' + field)
            else:
                fail('unsupported task field ' + field)
    for artifact in contract.get('artifacts', []):
        path = scoped(artifact['path'])
        if not path.is_file() or path.stat().st_size == 0:
            fail('missing or empty artifact ' + artifact['path'])
        if 'json_schema' in artifact:
            from jsonschema import validate, ValidationError, SchemaError
            try:
                schema = artifact['json_schema']
                if isinstance(schema, str):
                    schema = json.loads(scoped(schema).read_text())
                elif not isinstance(schema, (dict, bool)):
                    fail('artifact schema must be an object, boolean, or workspace file path')
                validate(json.loads(path.read_text()), schema)
            except (OSError, ValueError, ValidationError, SchemaError) as exc:
                fail('artifact schema: ' + str(exc)[:400])
