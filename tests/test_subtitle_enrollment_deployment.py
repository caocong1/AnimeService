import json
import sqlite3

import requests

from scripts.deploy_subtitle_enrollment import acquire, backend_ready, idle


def database():
    c=sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE subtitle_viewing_leases(expires REAL);
        CREATE TABLE web_progress(last_played REAL);
        CREATE TABLE thunder_queue(lease TEXT,expires REAL);
        CREATE TABLE kv(key TEXT,value TEXT);
        CREATE TABLE watches(show_id INTEGER,episode INTEGER,finished INTEGER);
        CREATE TABLE tasks(hash TEXT,status TEXT);
        INSERT INTO kv VALUES('heartbeat','{"phase":"idle"}');''')
    return c


def test_deployment_waits_for_leases_actual_progress_and_backend_cycle(monkeypatch):
    monkeypatch.setattr('scripts.deploy_subtitle_enrollment.psutil.process_iter',lambda fields:[])
    c=database()
    assert idle(c,1000)
    for table,values in [('subtitle_viewing_leases',(1100,)),('web_progress',(950,)),('thunder_queue',('live',1100))]:
        c.execute(f'INSERT INTO {table} VALUES({",".join("?" for _ in values)})',values)
        assert not idle(c,1000)
        c.execute(f'DELETE FROM {table}')
    c.execute('UPDATE kv SET value=?',(json.dumps({'phase':'source'}),))
    assert not idle(c,1000)


class Response:
    def __init__(self,value):self.value=value
    def raise_for_status(self):pass
    def json(self):return self.value


class Session:
    def __init__(self,fail=False):self.posts=[];self.fail=fail
    def get(self,*args,**kwargs):return Response({'token':'synthetic-test-token'})
    def post(self,url,**kwargs):
        self.posts.append(url)
        if self.fail:raise requests.Timeout('fixture timeout')
        return Response({'ok':True})


def test_acquisition_never_retries_unknown_outcome():
    c=database();s=Session(fail=True)
    candidates=[{'episode':ep,'hash':str(ep),'candidate_id':ep} for ep in (15,16)]
    result=acquire(s,c,candidates)
    assert len(s.posts)==1 and len(result)==1
    assert result[0]['state']=='acquisition_needs_reconcile'


def test_acquisition_preserves_watched_and_any_existing_task():
    c=database();s=Session()
    c.execute('INSERT INTO watches VALUES(511177,15,1)')
    c.execute("INSERT INTO tasks VALUES('16','uncertain')")
    result=acquire(s,c,[{'episode':ep,'hash':str(ep),'candidate_id':ep} for ep in (15,16)])
    assert not s.posts
    assert [r['state'] for r in result]==['watched_preserved','existing_task_preserved']
    assert result[1]['task_status']=='uncertain'


def test_new_backend_is_verified_before_acquisition():
    class FeatureSession:
        def __init__(self,features):self.features=features
        def get(self,*args,**kwargs):return Response({'preparation_features':self.features})
    assert not backend_ready(FeatureSession({}))
    assert backend_ready(FeatureSession({'all_available_unwatched':True,
        'watched_acquisition_guard':True,'watching_auto_enrollment':True,'research_modes':['subtitle_only']}))
