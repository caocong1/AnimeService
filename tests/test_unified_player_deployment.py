import json
import sqlite3
import time
from types import SimpleNamespace
from scripts import deploy_unified_player as deployment


def connection():
    c = sqlite3.connect(':memory:')
    c.executescript('''CREATE TABLE subtitle_viewing_leases(expires REAL);
        CREATE TABLE web_progress(last_played REAL);
        CREATE TABLE thunder_queue(lease TEXT,expires REAL);
        CREATE TABLE kv(key TEXT,value TEXT);''')
    c.execute('INSERT INTO kv VALUES(?,?)', ('heartbeat',json.dumps({'phase':'idle'})))
    return c


def test_idle_reload_requires_no_playback_or_background_work(monkeypatch):
    monkeypatch.setattr(deployment.psutil, 'process_iter', lambda fields: [])
    c = connection()
    assert deployment.idle(c)
    c.execute('INSERT INTO subtitle_viewing_leases VALUES(?)', (time.time()+60,))
    assert not deployment.idle(c)
    c.execute('DELETE FROM subtitle_viewing_leases')
    c.execute('INSERT INTO web_progress VALUES(?)', (time.time(),))
    assert not deployment.idle(c)
    c.execute('DELETE FROM web_progress')
    c.execute('INSERT INTO thunder_queue VALUES(?,?)', ('active',time.time()+60))
    assert not deployment.idle(c)
    c.execute('DELETE FROM thunder_queue')
    c.execute('UPDATE kv SET value=?', (json.dumps({'phase':'source'}),))
    assert not deployment.idle(c)


def test_desktop_playback_also_prevents_reload(monkeypatch):
    monkeypatch.setattr(deployment.psutil, 'process_iter',
                        lambda fields: [SimpleNamespace(info={'name':'vlc.exe'})])
    assert not deployment.idle(connection())
