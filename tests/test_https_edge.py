"""Synthetic listener-maintenance tests: fake children only; never UPnP, ACME or a real nginx."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from anime import edge

PRIVATE_TOKEN = 'PRIVATE_SYNTHETIC_VALUE'
SCRIPTS = ('.ps1', '.py')


class Stop:
    """Runs the loop a fixed number of iterations; each wait() advances the fake clock."""
    def __init__(self, clock, iterations=1, advance=0):
        self.clock, self.left, self.advance, self.waits = clock, iterations, advance, []

    def is_set(self):
        return self.left <= 0

    def wait(self, seconds):
        self.waits.append(seconds)
        self.left -= 1
        self.clock['now'] += self.advance


def script_of(args):
    return next((Path(str(a)).name for a in args if str(a).endswith(SCRIPTS)), None)


def never_persisted(root):
    assert not any(PRIVATE_TOKEN in p.read_text(errors='replace') for p in root.rglob('*') if p.is_file())


@pytest.fixture
def listener(tmp_path, monkeypatch):
    for d in ('data/edge', 'data/tools', 'data/tls/certificates', 'logs'):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / 'data/edge/nginx.conf').write_text('synthetic')
    (tmp_path / 'data/tools/nginx.exe').write_text('synthetic')
    cert = tmp_path / 'data/tls/certificates/home.example.test.crt'
    cert.write_text('old')
    (tmp_path / 'data/https.json').write_text(json.dumps({'domain': 'home.example.test', 'upnp': {'internal_client': '192.0.2.10'}}))
    (tmp_path / 'data/edge.pid').write_text('4242')
    monkeypatch.setattr(edge, 'ROOT', tmp_path)
    monkeypatch.setattr(edge, 'load_deployment', lambda: {'certificate': 'data/tls/certificates/home.example.test.crt'})
    clock = {'now': 1_700_000_000.0}
    monkeypatch.setattr(edge, 'time', SimpleNamespace(time=lambda: clock['now']))
    # The listener process already runs and belongs to this project: nothing is spawned or quit.
    owned = SimpleNamespace(is_running=lambda: True, exe=lambda: str(tmp_path / 'data/tools/nginx.exe'),
                            cwd=lambda: str(tmp_path / 'data/edge'))
    monkeypatch.setattr(edge.psutil, 'Process', lambda pid: owned)
    monkeypatch.setattr(edge.subprocess, 'Popen', lambda *a, **k: pytest.fail('nginx spawned'))
    calls = []
    results = {'https_upnp.ps1': (0, 'mapping_ready'), 'https_renew.py': (0, 'ok')}

    def run(args, **kw):
        calls.append((list(args), kw))
        script = script_of(args)
        if script is None:
            return SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
        result = results[script]
        if result == 'timeout':
            raise subprocess.TimeoutExpired(args, kw.get('timeout'), output=PRIVATE_TOKEN.encode())
        code, text = result
        if callable(text):
            text = text()
        # Real scripts print one token; a private-looking line before it must never be kept.
        return SimpleNamespace(returncode=code, stdout=f'{PRIVATE_TOKEN}\n{text}'.encode(), stderr=PRIVATE_TOKEN.encode())
    monkeypatch.setattr(edge.subprocess, 'run', run)
    return SimpleNamespace(root=tmp_path, cert=cert, clock=clock, calls=calls, results=results,
                           health=lambda: json.loads((tmp_path / 'data/https-health.json').read_text()),
                           scripts=lambda: [c for c in calls if script_of(c[0])],
                           reloads=lambda: [c for c in calls if c[0][-2:] == ['-s', 'reload']])


def test_first_iteration_runs_both_generic_scripts_with_the_private_configuration(listener):
    edge.maintain_https(Stop(listener.clock))
    mapping, renewal = listener.scripts()
    assert Path(mapping[0][0]).as_posix().endswith('System32/WindowsPowerShell/v1.0/powershell.exe')
    assert mapping[0][1:] == ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                              str(listener.root / 'scripts/https_upnp.ps1'), '-ConfigPath', str(listener.root / 'data/https.json')]
    assert mapping[1]['timeout'] == 90 and mapping[1]['capture_output'] is True
    assert renewal[0] == [str(listener.root / '.venv/Scripts/python.exe'), str(listener.root / 'scripts/https_renew.py')]
    assert renewal[1]['timeout'] >= 3600 + 600 and renewal[1]['capture_output'] is True
    assert not any('Map-AnimeHttps' in str(a) or 'renew_https' in str(a) for c in listener.calls for a in c[0])
    now = listener.clock['now']
    assert listener.health() == {'mapped': now, 'mapping': 'mapping_ready', 'checked': now, 'renew_ok': True, 'renewal': 'ok'}
    assert listener.reloads() == []  # the certificate file did not change
    never_persisted(listener.root)


def test_cadences_and_loop_interval_are_kept(listener):
    stop = Stop(listener.clock, iterations=3, advance=1000)
    edge.maintain_https(stop)
    assert stop.waits == [20, 20, 20]
    # 0 s: both; 1000 s: neither; 2000 s: mapping again, renewal only after a day
    assert [script_of(c[0]) for c in listener.scripts()] == ['https_upnp.ps1', 'https_renew.py', 'https_upnp.ps1']
    assert listener.health()['mapped'] == 1_700_000_000.0 + 2000 and listener.health()['checked'] == 1_700_000_000.0


@pytest.mark.parametrize('result,expected', [
    ((3, 'mapping_conflict'), 'mapping_conflict'),
    ((1, 'mapping_setup_failed'), 'mapping_setup_failed'),
    ((0, 'mapping_conflict'), 'unexpected'),  # a conflict is never a success exit
    ((2, 'mapping_ready'), 'unexpected'),
    ((0, ''), 'unexpected'),  # exit 0 without the ready token is not ready
    ('timeout', 'timeout'),
])
def test_mapping_outcomes_are_fixed_classes(listener, result, expected):
    listener.results['https_upnp.ps1'] = result
    edge.maintain_https(Stop(listener.clock))
    assert listener.health()['mapping'] == expected
    assert listener.health()['renewal'] == 'ok'  # an unmapped port never blocks the certificate check
    never_persisted(listener.root)


def renewed(listener, code=0):
    def text():
        listener.cert.write_text('new')
        os.utime(listener.cert, (2_000_000_000, 2_000_000_000))
        return 'ok' if code == 0 else 'certificate_request_failed'
    return (code, text)


@pytest.mark.parametrize('result,expected,reload', [
    ('renewed', 'ok', True),
    ((0, 'ok'), 'ok', False),  # success without a new certificate file: nothing to reload
    ('renewed-failed', 'certificate_request_failed', False),  # a changed file after a failed exit is not trusted
    ((1, 'dns_wait_failed'), 'dns_wait_failed', False),
    ((1, 'certificate_request_timeout'), 'certificate_request_timeout', False),
    ((1, 'certificate_setup_failed'), 'certificate_setup_failed', False),
    ((0, 'certificate_request_failed'), 'unexpected', False),
    ((0, ''), 'unexpected', False),
    ('timeout', 'timeout', False),
])
def test_renewal_outcomes_and_reload_only_after_a_successful_certificate_change(listener, result, expected, reload):
    if result == 'renewed':
        result = renewed(listener)
    elif result == 'renewed-failed':
        result = renewed(listener, 1)
    listener.results['https_renew.py'] = result
    edge.maintain_https(Stop(listener.clock))
    health = listener.health()
    assert (health['renewal'], health['renew_ok']) == (expected, expected == 'ok')
    assert (len(listener.reloads()) == 1) is reload
    never_persisted(listener.root)


def test_missing_private_configuration_skips_maintenance_but_keeps_the_listener(listener, monkeypatch):
    (listener.root / 'data/https.json').unlink()
    (listener.root / 'data/edge.pid').unlink()  # no owned process on record: the listener is started
    spawned = []
    monkeypatch.setattr(edge.subprocess, 'Popen',
                        lambda args, **kw: spawned.append((list(args), kw)) or SimpleNamespace(pid=4343, poll=lambda: None))
    edge.maintain_https(Stop(listener.clock))
    assert len(spawned) == 1 and spawned[0][0][0] == str(listener.root / 'data/tools/nginx.exe')
    assert (listener.root / 'data/edge.pid').read_text() == '4343'
    assert listener.scripts() == []
    now = listener.clock['now']
    assert listener.health() == {'mapped': now, 'mapping': 'config_missing', 'checked': now, 'renew_ok': False, 'renewal': 'config_missing'}
    assert listener.calls[-1][0][-2:] == ['-s', 'quit']  # the process it started is quit on exit, as before


def test_nothing_runs_without_a_deployed_certificate(listener, monkeypatch):
    monkeypatch.setattr(edge, 'load_deployment', lambda: {})
    edge.maintain_https(Stop(listener.clock))
    assert listener.calls == [] and not (listener.root / 'data/https-health.json').exists()


@pytest.mark.parametrize('script', ['https_upnp.ps1', 'https_renew.py'])
def test_child_launch_failure_is_private_and_does_not_stop_the_listener(listener, monkeypatch, script):
    original = edge.subprocess.run
    def run(args, **kw):
        if script_of(args) == script:
            raise OSError(PRIVATE_TOKEN)
        return original(args, **kw)
    monkeypatch.setattr(edge.subprocess, 'run', run)
    stop = Stop(listener.clock, iterations=2)
    edge.maintain_https(stop)
    assert stop.waits == [20, 20]
    health = listener.health()
    assert health['mapping' if script.endswith('.ps1') else 'renewal'] == 'unexpected'
    if script.endswith('.ps1'):
        assert health['renewal'] == 'ok'
    else:
        assert health['mapping'] == 'mapping_ready' and health['renew_ok'] is False
    never_persisted(listener.root)
