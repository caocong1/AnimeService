import hashlib
import json

from scripts.check_automation_contract import check, preserved_sha, prompt_sha


def fixture(tmp_path):
    root = tmp_path / 'automations'
    path = root / 'automation-2' / 'automation.toml'
    path.parent.mkdir(parents=True)
    config = {'id': 'automation-2', 'name': 'subtitle', 'kind': 'heartbeat', 'status': 'ACTIVE',
              'rrule': 'FREQ=MINUTELY;INTERVAL=15', 'target_thread_id': 'original-chat',
              'prompt': 'current rules\n', 'updated_at': 123}
    raw = '\n'.join(f'{k} = {json.dumps(v)}' for k, v in config.items()).encode()
    path.write_bytes(raw)
    (tmp_path / 'prompt.txt').write_text('current rules\n', encoding='utf-8')
    task = {k: config[k] for k in ('id', 'name', 'kind', 'status', 'rrule')}
    task.update(managed_here=True, template='prompt.txt', prompt_sha256_lf=prompt_sha(config['prompt']),
                applied_config_sha256=hashlib.sha256(raw).hexdigest(), preserved_fields_sha256=preserved_sha(config))
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'tasks': [task, {'id': 'family', 'managed_here': False}]}))
    return manifest, root, path


def test_checks_applied_prompt_without_reading_or_changing_another_project(tmp_path):
    manifest, root, path = fixture(tmp_path)
    before = path.read_bytes()
    result = check(manifest, root)
    assert result['ok'] and result['tasks'][1]['status'] == 'external_project'
    assert before == path.read_bytes()
    assert 'original-chat' not in json.dumps(result)
    assert prompt_sha('current rules\r\n\r\n') == prompt_sha('current rules\n')


def test_detects_live_target_prompt_template_and_missing_configuration_drift(tmp_path):
    manifest, root, path = fixture(tmp_path)
    original = path.read_bytes()
    for old, new, expected in ((b'original-chat', b'other-chat', 'preserved target'),
                               (b'current rules', b'stale rules', 'active prompt')):
        path.write_bytes(original.replace(old, new))
        result = check(manifest, root)
        assert not result['ok'] and any(expected in e for e in result['tasks'][0]['errors'])
    path.write_bytes(original)
    (tmp_path / 'prompt.txt').write_text('new draft only')
    assert not check(manifest, root)['ok']
    path.unlink()
    assert not check(manifest, root)['ok']
