"""Bounded assignment guidance; never a permission or approval mechanism.

A seat can hold review credentials while doing artifact or dispatcher work.
Explicit task modes distinguish that work without editing the seat's identity.
"""
import json
from pathlib import Path

_MODE_PREFIX = 'HELIOS_TASK_MODE:'
_CONTRACT_PREFIX = 'HELIOS_COMPLETION_CONTRACT:'
_MODES = {
    'artifact': 'Produce and verify the requested artifacts. Do not invent a review verdict or commit SHA for an assignment that does not request formal review.',
    'dispatch': 'Create and verify the requested specialist handoffs. Do not invent a review verdict or commit SHA. Dispatch completion and downstream product completion are separate; apply the declared completion contract.',
    'review': 'Required formal review evidence remains mandatory, including exact revisions and authorized signatures where the governing review requires them.',
}


def render_assignment_scope(body, workspace_path):
    declarations = [line[len(_MODE_PREFIX):].strip() for line in (body or '').splitlines()
                    if line.startswith(_MODE_PREFIX)]
    if not declarations:
        return []
    if len(declarations) != 1 or declarations[0] not in _MODES:
        return ['Invalid assignment mode declaration; have the dispatcher correct it. No work scope selected.', '']
    mode = declarations[0]
    lines = ['## Assignment scope', f'Assignment mode: {mode}', _MODES[mode],
             'No approval or tool permission is granted by this mode. Existing authority, HAL, safety and completion gates still apply.']
    controls = [line[len(_CONTRACT_PREFIX):].strip() for line in (body or '').splitlines()
                if line.startswith(_CONTRACT_PREFIX)]
    if len(controls) == 1 and controls[0] and workspace_path:
        root = Path(workspace_path).resolve()
        control = (root / controls[0]).resolve()
        if control.is_relative_to(root):
            lines.append('Do not overwrite the completion contract. It is control input, not the report destination: ' + json.dumps(str(control)))
            try:
                with control.open('rb') as handle:
                    raw = handle.read(65537)
                if len(raw) > 65536:
                    raise ValueError('oversized contract')
                contract = json.loads(raw)
                if not isinstance(contract, dict) or contract.get('version') != 1:
                    raise ValueError('invalid contract')
                artifacts = contract.get('artifacts', [])
                if not isinstance(artifacts, list):
                    raise ValueError('invalid artifacts')
                for artifact in artifacts[:20]:
                    raw_path = artifact.get('path') if isinstance(artifact, dict) else None
                    if isinstance(raw_path, str) and raw_path and len(raw_path) <= 4096:
                        path = (root / raw_path).resolve()
                        if path.is_relative_to(root):
                            lines.append('Artifact destination: ' + json.dumps(str(path)))
                if len(artifacts) > 20:
                    lines.append('More artifact destinations are listed in the contract.')
            except (OSError, ValueError):
                lines.append('Contract cannot be rendered; ask the dispatcher to repair the control input. Do not replace it with report content.')
        else:
            lines.append('Contract path is outside the workspace; have the dispatcher repair it.')
    lines.append('')
    return lines
