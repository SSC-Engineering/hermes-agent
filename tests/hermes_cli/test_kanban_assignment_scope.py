"""Assignment context must separate requested work from a seat's review duties."""
import json
from pathlib import Path

import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.mark.parametrize('mode', ['artifact', 'dispatch', 'review'])
def test_explicit_scope_reaches_worker_without_changing_task_or_contract(tmp_path, monkeypatch, mode):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    root = tmp_path / 'work'
    root.mkdir()
    control = root / 'control.json'
    payload = {'version': 1, 'artifacts': [{'path': 'report.json'}]}
    control.write_text(json.dumps(payload))
    before = control.read_bytes()
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title='Bounded assignment', workspace_kind='dir',
                                workspace_path=str(root), body=f'HELIOS_TASK_MODE: {mode}\nHELIOS_COMPLETION_CONTRACT: control.json')
        task_before = kb.get_task(conn, task_id)
        rendered = kb.build_worker_context(conn, task_id)
        assert f'Assignment mode: {mode}' in rendered
        assert str(control) in rendered
        assert str(root / 'report.json') in rendered
        assert 'Do not overwrite the completion contract' in rendered
        assert 'No approval or tool permission is granted by this mode' in rendered
        if mode == 'review':
            assert 'Required formal review evidence remains mandatory' in rendered
        else:
            assert 'Do not invent a review verdict or commit SHA' in rendered
        assert kb.get_task(conn, task_id) == task_before
        assert control.read_bytes() == before


@pytest.mark.parametrize('markers', ['', 'HELIOS_TASK_MODE: unknown',
                                      'HELIOS_TASK_MODE: artifact\nHELIOS_TASK_MODE: review'])
def test_missing_or_ambiguous_mode_does_not_select_work_scope(tmp_path, monkeypatch, markers):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'home'))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title='No valid declaration', body=markers)
        rendered = kb.build_worker_context(conn, task_id)
        assert 'Assignment mode:' not in rendered
        if markers:
            assert 'Invalid assignment mode declaration' in rendered
