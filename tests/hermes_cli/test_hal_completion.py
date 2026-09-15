import json
from hermes_cli import hal_kanban_enforce as hal


def test_completion_closes_only_current_session_and_checks_readback(tmp_path,monkeypatch):
    script=tmp_path/'hal.py';script.touch();monkeypatch.setenv('HAL_RECORD_SCRIPT',str(script))
    calls=[]
    row={'id':'current','status':'open','session_id':'s2','agent_name':'worker','kanban_task_id':'task','cost_usd':None,'cost_status':'unknown','pricing_source':'unpriced'}
    def run(argv,timeout=45,env=None):
        command=argv[2];calls.append(command)
        if command=='open':return 0,json.dumps({'row':row}),''
        if command=='close':row['status']='closed';return 0,'{}',''
        if command=='show':return 0,json.dumps({'rows':[row]}),''
        assert command=='assert-visible';return 0,json.dumps({'visible':True,'closed':1}),''
    monkeypatch.setattr(hal,'_run',run)
    assert hal.assert_visible_or_autoclose(kanban_task_id='task',profile='worker',session='s2')[0]
    assert calls==['open','close','show','assert-visible']
    row['session_id']='previous';calls.clear()
    assert not hal.assert_visible_or_autoclose(kanban_task_id='task',profile='worker',session='s2')[0]
    assert 'assert-visible' not in calls


def test_each_spawn_has_own_identity_without_gateway_session(tmp_path,monkeypatch):
    script=tmp_path/'hal.py';script.touch();monkeypatch.setenv('HAL_RECORD_SCRIPT',str(script))
    monkeypatch.setenv('HERMES_SESSION_ID','gateway-session');monkeypatch.setenv('HERMES_HAL_LEDGER_ID','gateway-ledger')
    sessions=[]
    def run(argv,timeout=45,env=None):
        assert 'HERMES_SESSION_ID' not in env and 'HERMES_HAL_LEDGER_ID' not in env
        sessions.append(argv[argv.index('--session')+1]);return 0,json.dumps({'row':{'id':str(len(sessions))}}),''
    monkeypatch.setattr(hal,'_run',run)
    assert hal.open_on_spawn(agent='worker',kanban_task_id='task')=='1'
    assert hal.open_on_spawn(agent='worker',kanban_task_id='task')=='2'
    assert sessions[0]!=sessions[1]
