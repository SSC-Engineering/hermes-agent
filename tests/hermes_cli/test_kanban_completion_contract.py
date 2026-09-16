import json
from pathlib import Path
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

@pytest.fixture
def board(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'.hermes'))
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    kb.init_db()
    with kbc.connect_closing() as conn:
        yield conn,tmp_path


def test_shared_complete_rejects_card_presence_until_actual_release(board):
    conn,root=board
    child=kb.create_task(conn,title='worker',assignee='specialist',workspace_kind='dir',workspace_path=str(root),initial_status='blocked',model_override='test-model',provider_override='test')
    (root/'contract.json').write_text(json.dumps({'version':1,'tasks':[{'id':child,'allowed_statuses':['ready','running','done'],'assignee':'specialist','model_override':'test-model'}]}))
    parent=kb.create_task(conn,title='dispatch',workspace_kind='dir',workspace_path=str(root),body='HELIOS_COMPLETION_CONTRACT: contract.json')
    with pytest.raises(ValueError,match='has status blocked'):
        kb.complete_task(conn,parent,summary='Dispatched successfully',fire_lifecycle_hook=False)
    assert kb.get_task(conn,parent).status!='done'
    conn.execute("UPDATE tasks SET status='ready' WHERE id=?",(child,));conn.commit()
    assert kb.complete_task(conn,parent,summary='Release verified',fire_lifecycle_hook=False)
    assert kb.get_task(conn,parent).status=='done'


def test_artifact_schema_prevents_empty_handoff(board):
    conn,root=board
    (root/'schema.json').write_text(json.dumps({'type':'object','required':['scenarios'],'properties':{'scenarios':{'type':'array','minItems':1}}}))
    (root/'contract.json').write_text(json.dumps({'version':1,'artifacts':[{'path':'result.json','json_schema':'schema.json'}]}))
    task=kb.create_task(conn,title='QA',workspace_kind='dir',workspace_path=str(root),body='HELIOS_COMPLETION_CONTRACT: contract.json')
    for data in ({},{'scenarios':[]}):
        (root/'result.json').write_text(json.dumps(data))
        with pytest.raises(ValueError,match='artifact schema'):
            kb.complete_task(conn,task,summary='Complete',fire_lifecycle_hook=False)
    (root/'result.json').write_text(json.dumps({'scenarios':[{'action':'defined'}]}))
    assert kb.complete_task(conn,task,summary='Produced scenario',fire_lifecycle_hook=False)


@pytest.mark.parametrize("schema", [
    {"type": "object", "required": ["scenarios"], "properties": {"scenarios": {"type": "array", "minItems": 1}}},
    False,
    [],
    42,
])
def test_inline_schema_enforced_at_real_completion_boundary(board, schema):
    conn, root = board
    (root / "result.json").write_text(json.dumps({"scenarios": []}))
    (root / "contract.json").write_text(json.dumps({"version": 1, "artifacts": [{"path": "result.json", "json_schema": schema}]}))
    task = kb.create_task(conn, title="inline schema", workspace_kind="dir", workspace_path=str(root), body="HELIOS_COMPLETION_CONTRACT: contract.json")
    with pytest.raises(ValueError, match="artifact schema"):
        kb.complete_task(conn, task, summary="invalid result", fire_lifecycle_hook=False)
    assert kb.get_task(conn, task).status != "done"
    if isinstance(schema, dict):
        (root / "result.json").write_text(json.dumps({"scenarios": [{"action": "defined"}]}))
        assert kb.complete_task(conn, task, summary="valid result", fire_lifecycle_hook=False)


def test_non_path_artifact_has_clear_contract_error(board):
    conn, root = board
    (root / "contract.json").write_text(json.dumps({"version": 1, "artifacts": [{"path": {"bad": "path"}}]}))
    task = kb.create_task(conn, title="invalid path", workspace_kind="dir", workspace_path=str(root), body="HELIOS_COMPLETION_CONTRACT: contract.json")
    with pytest.raises(ValueError, match="nonempty string"):
        kb.complete_task(conn, task, summary="invalid path", fire_lifecycle_hook=False)
    assert kb.get_task(conn, task).status != "done"
