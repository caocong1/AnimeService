"""Synthetic configuration/child-process tests; never contact DNS, ACME or UPnP."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def renewal(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('test_https_renew_module', ROOT / 'scripts/https_renew.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / 'data').mkdir()
    config = tmp_path / 'data/https.json'
    config.write_text(json.dumps({'domain': 'home.example.test', 'email': 'admin@example.test',
                                  'dns_provider': 'tencentcloud', 'renew_days': 30}), encoding='utf-8')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'CONFIG', config)
    monkeypatch.setattr(module, 'credentials', lambda _: ('SYNTHETIC_ACCOUNT', 'SYNTHETIC_SECRET'))
    return module


@pytest.mark.parametrize('existing', [False, True])
def test_lego_v5_arguments_and_memory_only_credentials(renewal, monkeypatch, capsys, existing):
    m = renewal
    if existing:
        certs = m.ROOT / 'data/tls/certificates'
        certs.mkdir(parents=True)
        (certs / 'home.example.test.crt').write_text('synthetic')
    calls = []
    def run(args, **kw):
        calls.append((args, kw))
        return SimpleNamespace(returncode=0, stdout=b'SYNTHETIC_SECRET', stderr=b'')
    monkeypatch.setattr(m.subprocess, 'run', run)
    assert m.main() == 0
    args, kw = calls[0]
    assert args[1] == 'run'  # local supported binary is lego 5.5.2, not the v4 CLI
    assert ('--renew-days' in args) is existing
    assert 'SYNTHETIC_SECRET' not in ' '.join(args)
    assert kw['env']['TENCENTCLOUD_SECRET_KEY'] == 'SYNTHETIC_SECRET'
    assert 'TENCENTCLOUD_SECRET_KEY' not in os.environ or os.environ['TENCENTCLOUD_SECRET_KEY'] != 'SYNTHETIC_SECRET'
    assert kw['timeout'] == 2400
    assert capsys.readouterr().out == 'ok\n'
    assert not any(p.suffix == '.json' and p != m.CONFIG for p in m.ROOT.rglob('*'))


@pytest.mark.parametrize('output,expected', [(b'propagation SYNTHETIC_SECRET https://secret.invalid/?a=b', 'dns_wait_failed'),
                                            (b'private body SYNTHETIC_ACCOUNT', 'certificate_request_failed')])
def test_no_provider_output_even_on_failure(renewal, monkeypatch, capsys, output, expected):
    monkeypatch.setattr(renewal.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=2, stdout=output, stderr=output))
    assert renewal.main() == 1
    assert capsys.readouterr().out == expected + '\n'


def test_timeout_does_not_print_partial_output(renewal, monkeypatch, capsys):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('synthetic', 1, output=b'SYNTHETIC_SECRET')
    monkeypatch.setattr(renewal.subprocess, 'run', timeout)
    assert renewal.main() == 1
    assert capsys.readouterr().out == 'certificate_request_timeout\n'


@pytest.mark.parametrize('change', [{'domain': '../outside'}, {'domain': 'host.test\n'},
                                    {'renew_days': True}, {'renew_days': 0}, {'propagation_timeout': 3601},
                                    {'resolver': 'value\n'}, {'dns_provider': 'unsupported'}])
def test_invalid_configuration_fails_before_child_or_credentials(renewal, monkeypatch, change):
    config = json.loads(renewal.CONFIG.read_text())
    config.update(change)
    renewal.CONFIG.write_text(json.dumps(config))
    monkeypatch.setattr(renewal, 'credentials', lambda _: pytest.fail('credentials touched'))
    monkeypatch.setattr(renewal.subprocess, 'run', lambda *a, **k: pytest.fail('child started'))
    with pytest.raises(ValueError):
        renewal.main()


def test_powershell_mapping_ownership_synthetic_only(tmp_path):
    shell = shutil.which('powershell.exe') or shutil.which('pwsh')
    if not shell:
        pytest.skip('PowerShell unavailable')
    target = str(ROOT / 'scripts/https_upnp.ps1').replace("'", "''")
    probe = tmp_path / 'mapping-test.ps1'
    probe.write_text("""
$ErrorActionPreference='Stop'
. 'TARGET' -LibraryOnly
$c=Get-HttpsMappingConfig ([pscustomobject]@{internal_client='192.0.2.10';port=38443;description='test'})
if ((Get-HttpsMappingAction @() $c) -ne 'add') { throw 'new' }
$owned=[pscustomobject]@{ExternalPort=38443;Protocol='TCP';InternalPort=38443;InternalClient='192.0.2.10';Description='test';Enabled=$true}
if ((Get-HttpsMappingAction @($owned) $c) -ne 'ready') { throw 'owned' }
foreach($field in @('InternalClient','InternalPort','Description','Enabled')) {
  $other=$owned.PSObject.Copy()
  $other.$field = if($field -eq 'Enabled'){$false}elseif($field -eq 'InternalPort'){1}else{'other'}
  if ((Get-HttpsMappingAction @($other) $c) -ne 'conflict') { throw 'foreign' }
}
if ((Get-HttpsMappingAction @($owned,$owned) $c) -ne 'conflict') { throw 'ambiguous' }
foreach($port in @(0,65536,'38443',1.5)) {
  $rejected=$false
  try { $null=Get-HttpsMappingConfig ([pscustomobject]@{internal_client='192.0.2.10';port=$port}) } catch { $rejected=$true }
  if(-not $rejected){throw 'invalid_port'}
}
'synthetic_mapping_ok'
""".replace('TARGET', target), encoding='utf-8')
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(probe)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'synthetic_mapping_ok'


def test_powershell_entry_failure_is_fixed_category_without_com(tmp_path):
    shell = shutil.which('powershell.exe') or shutil.which('pwsh')
    if not shell:
        pytest.skip('PowerShell unavailable')
    config = tmp_path / 'invalid.json'
    config.write_text(json.dumps({'upnp': {'internal_client': 'PRIVATE_SYNTHETIC_VALUE'}}))
    result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-File', str(ROOT / 'scripts/https_upnp.ps1'),
                             '-ConfigPath', str(config)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert result.stdout.strip() == 'mapping_setup_failed'
    assert 'PRIVATE_SYNTHETIC_VALUE' not in result.stdout + result.stderr
