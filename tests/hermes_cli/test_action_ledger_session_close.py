"""Session-bound HAL close through real HTTP transport against a local fixture."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import pytest
from hermes_cli import action_ledger as al

@pytest.fixture
def ledger(monkeypatch):
    row = {'id': 'ledger', 'session_id': 'current', 'status': 'open'}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def respond(self, patch=False):
            filters = parse_qs(urlparse(self.path).query)
            matches = all(filters[k] == ['eq.'+str(row[k])] for k in ('id','session_id','status') if k in filters)
            if patch:
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if matches: row.update(body)
            payload=json.dumps([dict(row)] if matches else []).encode()
            self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers(); self.wfile.write(payload)
        def do_PATCH(self): self.respond(True)
        def do_GET(self): self.respond()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(al,'_service_config',lambda:(f'http://127.0.0.1:{server.server_port}','fixture-key'))
    try: yield row
    finally: server.shutdown();server.server_close();thread.join()

@pytest.mark.parametrize('session,cost,status,source,allow_zero,accepted',[
    ('current',None,'unknown','unpriced fixture',False,True),
    ('old',0.01,'actual','fixture receipt',False,False),
    (None,None,'unknown','unpriced fixture',False,False),
    ('current',None,'unknown',' ',False,False),
    ('current',None,None,None,True,False),
    ('current',0,'actual',None,True,False),
    ('current',0,'actual','fixture no-spend receipt',True,True),
    ('current',0,'actual','claimed zero',False,False),
    ('current',float('nan'),'actual','invalid',False,False),
    ('current',0,'unknown','fixture',True,False),
    ('current','0.05','actual','fixture receipt',False,True),
    ('current','bad','actual','fixture receipt',False,False),
])
def test_close_preserves_session_and_honest_cost(ledger,session,cost,status,source,allow_zero,accepted):
    kwargs=dict(session_id=session,cost_usd=cost,cost_status=status,pricing_source=source,allow_zero_cost=allow_zero)
    if accepted:
        assert al.close_action_ledger('ledger',**kwargs)=='ledger'
        receipt=dict(ledger)
        assert al.close_action_ledger('ledger',**kwargs)=='ledger'
        assert ledger==receipt  # repeat does not rewrite the original receipt
        assert ledger['session_id']=='current' and ledger['cost_usd']==(float(cost) if cost is not None else None)
    else:
        with pytest.raises(al.ActionLedgerError):al.close_action_ledger('ledger',**kwargs)
        assert ledger=={'id':'ledger','session_id':'current','status':'open'}

@pytest.mark.real_hal_gate
def test_task_session_cannot_be_overridden_by_completion_metadata(tmp_path):
    import sqlite3
    from types import SimpleNamespace
    from hermes_cli import kanban_db as kb
    con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
    con.execute('CREATE TABLE task_runs (id INTEGER, task_id TEXT, metadata TEXT)')
    con.execute('INSERT INTO task_runs VALUES (1,?,?)',('task',json.dumps({'worker_session_id':'current'})))
    task=SimpleNamespace(session_id='old-task-session',assignee='worker')
    try:
        with pytest.raises(kb.ActionLedgerCloseError):
            kb._worker_session_for_hal(con,'task',task,{'session_id':'old-attempt'})
        assert kb._worker_session_for_hal(con,'task',task,{})==('worker','current')
    finally:con.close()

@pytest.mark.real_hal_gate
@pytest.mark.parametrize('supplied_session,accepted',[('current',True),('old',False)])
def test_kanban_complete_uses_session_bound_http_close(ledger,tmp_path,monkeypatch,supplied_session,accepted):
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    home=tmp_path/'home';home.mkdir()
    monkeypatch.setenv('HERMES_HOME',str(home))
    monkeypatch.setattr(Path,'home',lambda:tmp_path)
    monkeypatch.setattr('hermes_cli.profiles.profile_exists',lambda _:True)
    monkeypatch.setattr(kb,'_best_effort_action_ledger_open',lambda *a,**kw:None)
    monkeypatch.setattr(al,'require_cost_on_complete',lambda:True)
    monkeypatch.setattr(al,'estimate_session_cost',lambda *a: {})
    kb.init_db()
    with kb.connect() as conn:
        tid=kb.create_task(conn,title='fixture',assignee='fixture-worker')
        assert kb.claim_task(conn,tid,claimer='dispatcher')
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET action_ledger_id=?,session_id=? WHERE id=?',('ledger','current',tid))
            conn.execute('UPDATE task_runs SET metadata=? WHERE task_id=?',
                         (json.dumps({'worker_session_id':'current'}),tid))
        metadata={'session_id':supplied_session,'cost_status':'unknown','pricing_source':'fixture unpriced'}
        if accepted:
            assert kb.complete_task(conn,tid,summary='fixture completed',metadata=metadata)
            assert kb.get_task(conn,tid).status=='done'
            assert ledger['status']=='closed' and ledger['cost_usd'] is None
        else:
            with pytest.raises(kb.ActionLedgerCloseError):kb.complete_task(conn,tid,summary='wrong attempt',metadata=metadata)
            assert kb.get_task(conn,tid).status!='done'
            assert ledger['status']=='open'

@pytest.mark.real_hal_gate
@pytest.mark.parametrize('run_metadata',[None,'{}','{"worker_session_id":"   "}'])
def test_existing_attempt_without_session_stamp_never_uses_old_task_session(run_metadata):
    import sqlite3
    from types import SimpleNamespace
    from hermes_cli import kanban_db as kb
    con=sqlite3.connect(':memory:');con.row_factory=sqlite3.Row
    con.execute('CREATE TABLE task_runs (id INTEGER, task_id TEXT, metadata TEXT)')
    con.execute('INSERT INTO task_runs VALUES (7,?,?)',('task',run_metadata))
    task=SimpleNamespace(session_id='old',assignee='worker')
    try:
        with pytest.raises(kb.ActionLedgerCloseError,match='attempt 7.*session'):
            kb._worker_session_for_hal(con,'task',task,{'session_id':'old'})
    finally:con.close()

@pytest.mark.real_hal_gate
@pytest.mark.parametrize('fault', [None, 'run', 'claim', 'profile', 'task'])
def test_worker_tool_binds_only_current_claim_before_http_close(ledger, tmp_path, monkeypatch, fault):
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda _: True)
    monkeypatch.setattr(kb, '_best_effort_action_ledger_open', lambda *a, **kw: None)
    monkeypatch.setattr(al, 'require_cost_on_complete', lambda: True)
    monkeypatch.setattr(al, 'estimate_session_cost', lambda *a: {})
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title='worker binding fixture', assignee='fixture-worker')
        task = kb.claim_task(conn, tid, claimer='dispatcher')
        assert task is not None
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET action_ledger_id=?,session_id=? WHERE id=?', ('ledger', 'old-origin', tid))
        run_id, claim = task.current_run_id, task.claim_lock
    env = {'HERMES_KANBAN_TASK': tid, 'HERMES_KANBAN_RUN_ID': str(run_id),
           'HERMES_KANBAN_CLAIM_LOCK': claim, 'HERMES_PROFILE': 'fixture-worker',
           'HERMES_SESSION_ID': 'current'}
    key = {'run': 'HERMES_KANBAN_RUN_ID', 'claim': 'HERMES_KANBAN_CLAIM_LOCK',
           'profile': 'HERMES_PROFILE', 'task': 'HERMES_KANBAN_TASK'}.get(fault)
    if key:
        env[key] = 'stale'
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    response = json.loads(kt._handle_complete({'task_id': tid, 'summary': 'fixture result',
                        'metadata': {'cost_status': 'unknown', 'pricing_source': 'unpriced fixture'}}))
    with kb.connect() as conn:
        fresh = kb.get_task(conn, tid)
        stamp = json.loads(conn.execute('SELECT metadata FROM task_runs WHERE id=?', (run_id,)).fetchone()[0] or '{}')
    if fault is None:
        assert fresh.status == 'done', response
        assert stamp['worker_session_id'] == 'current'
        assert ledger['status'] == 'closed' and ledger['session_id'] == 'current'
        assert fresh.session_id == 'old-origin'  # task-origin identity not retagged
    else:
        assert fresh.status != 'done', response
        assert stamp.get('worker_session_id') is None
        assert ledger['status'] == 'open'


@pytest.fixture
def ledger_store(monkeypatch):
    """Exercise open and close over HTTP, including session-scoped lookup."""
    rows = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, method):
            filters = parse_qs(urlparse(self.path).query)
            matches = [row for row in rows if all(
                value == ['eq.' + str(row.get(key))]
                or (value == ['is.null'] and row.get(key) is None)
                for key, value in filters.items() if key not in {'select', 'limit'}
            )]
            if method in {'POST', 'PATCH'}:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if method == 'POST':
                    row = {'id': f'ledger-{len(rows) + 1}', **body}
                    rows.append(row)
                    matches = [row]
                else:
                    for row in matches:
                        row.update(body)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(matches).encode())

        def do_GET(self):
            self.respond('GET')

        def do_POST(self):
            self.respond('POST')

        def do_PATCH(self):
            self.respond('PATCH')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(al, '_service_config', lambda: (
        f'http://127.0.0.1:{server.server_port}', 'fixture-key'))
    try:
        yield rows
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.real_hal_gate
@pytest.mark.parametrize('origin', [None, 'old-origin'])
def test_fresh_claim_opens_only_after_worker_identity_exists(ledger_store, tmp_path, monkeypatch, origin):
    from pathlib import Path
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr('hermes_cli.profiles.profile_exists', lambda _: True)
    monkeypatch.setattr(al, 'require_cost_on_complete', lambda: True)
    monkeypatch.setattr(al, 'estimate_session_cost', lambda *a: {})
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(conn, title='fresh lifecycle', assignee='fixture-worker', session_id=origin)
        task = kb.claim_task(conn, tid, claimer='dispatcher')
        assert task is not None
        assert ledger_store == []  # claim cannot attribute a not-yet-created worker
        assert kb.get_task(conn, tid).action_ledger_id is None

    for key, value in {
        'HERMES_KANBAN_TASK': tid, 'HERMES_KANBAN_RUN_ID': str(task.current_run_id),
        'HERMES_KANBAN_CLAIM_LOCK': task.claim_lock, 'HERMES_PROFILE': 'fixture-worker',
        'HERMES_SESSION_ID': 'fresh-worker',
    }.items():
        monkeypatch.setenv(key, value)
    response = json.loads(kt._handle_complete({
        'task_id': tid, 'summary': 'fresh worker finished',
        'metadata': {'cost_status': 'unknown', 'pricing_source': 'fixture unpriced'},
    }))
    with kb.connect() as conn:
        fresh = kb.get_task(conn, tid)
        assert fresh.status == 'done', response
        assert fresh.session_id == origin
        assert len(ledger_store) == 1
        assert fresh.action_ledger_id == ledger_store[0]['id']
    assert ledger_store[0]['session_id'] == 'fresh-worker'
    assert ledger_store[0]['status'] == 'closed'
    assert ledger_store[0]['cost_usd'] is None


@pytest.mark.parametrize('prior_session', [None, 'earlier-worker'])
def test_open_does_not_reuse_another_attempts_row(ledger_store, prior_session):
    prior = {'id': 'prior', 'kanban_task_id': 'task', 'session_id': prior_session, 'status': 'open'}
    ledger_store.append(dict(prior))
    args = {'linear_issue_id': None, 'agent_name': 'worker',
            'kanban_task_id': 'task', 'session_id': 'current-worker'}
    current = al.open_action_ledger(**args)
    assert current != 'prior'
    assert al.open_action_ledger(**args) == current
    assert ledger_store[0] == prior
    assert len(ledger_store) == 2
