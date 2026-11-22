"""Real OS-lock/process tests; all media, indexes and locks are synthetic/temp."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from scripts.subtitle_output_lock import OutputBusyError, output_lock


REPO = Path(__file__).resolve().parents[1]


def spawn(code, *args):
    return subprocess.Popen([sys.executable, '-c', code, *map(str, args)], cwd=REPO,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding='utf-8', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def wait_file(path):
    until = time.monotonic() + 10
    while time.monotonic() < until:
        if path.exists():
            return
        time.sleep(.02)
    pytest.fail(f'Test subprocess did not create {path.name}')


def success(process):
    out, err = process.communicate(timeout=15)
    assert process.returncode == 0, out + err
    return out


def test_two_processes_wait_then_acquire_same_slot(tmp_path):
    path = tmp_path / 'slot.lock'
    code = '''
import sys
from pathlib import Path
from scripts.subtitle_output_lock import output_lock
with output_lock(sys.argv[1], 'fixture', timeout=5, poll_seconds=.02, report_seconds=.05):
    Path(sys.argv[2]).write_text('acquired')
'''
    ready = tmp_path / 'acquired'
    with output_lock(path, 'parent'):
        process = spawn(code, path, ready)
        first = process.stdout.readline()
        assert 'waiting_for_output_slot' in first
        assert not ready.exists()
    assert 'output_slot_acquired' in success(process)
    assert ready.read_text() == 'acquired'
    assert path.exists()  # Stable inode/name is intentional, never a stale lock.


def test_busy_timeout_is_visible_and_leaves_existing_owner(tmp_path, capsys):
    path = tmp_path / 'slot.lock'
    with output_lock(path, 'first'):
        with pytest.raises(OutputBusyError, match='inputs preserved, retry later'):
            with output_lock(path, 'second', timeout=.08, poll_seconds=.01, report_seconds=.03):
                pytest.fail('Contending lock was acquired')
    output = capsys.readouterr().out
    assert output.count('waiting_for_output_slot') >= 2
    assert 'blocked' not in output and 'stopped' not in output
    with output_lock(path, 'third', timeout=0):
        pass


@pytest.mark.parametrize('marker', [b'{"pid":123,"release":"old-publisher"}', b'unknown receipt'])
def test_legacy_or_unknown_publish_marker_is_never_deleted(tmp_path, marker, monkeypatch):
    from scripts import subtitle_output_lock as locks
    path = tmp_path / '.publish.lock'
    path.write_bytes(marker)
    # Even a momentary deny-sharing open could break an old owner's cleanup.
    monkeypatch.setattr(locks, '_open_exclusive', lambda path:
                        pytest.fail('Legacy marker must not be opened exclusively'))
    with pytest.raises(OutputBusyError, match='legacy_or_unknown_marker_preserved'):
        with output_lock(path, 'fixture', timeout=.03, poll_seconds=.01):
            pytest.fail('Legacy publisher was bypassed')
    assert path.read_bytes() == marker


def test_abrupt_controller_exit_releases_process_lock(tmp_path):
    path = tmp_path / 'slot.lock'
    code = '''
import os, sys
from scripts.subtitle_output_lock import output_lock
with output_lock(sys.argv[1], 'fixture'):
    os._exit(0)
'''
    success(spawn(code, path))
    with output_lock(path, 'after-crash', timeout=0):
        pass


def test_inherited_lock_survives_controller_until_child_stops(tmp_path):
    path = tmp_path / 'slot.lock'
    started, stop = tmp_path / 'child-started', tmp_path / 'child-stop'
    child = '''
import sys, time
from pathlib import Path
Path(sys.argv[1]).write_text('started')
deadline = time.monotonic() + 12
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(.02)
'''
    controller = '''
import os, subprocess, sys
from scripts.subtitle_output_lock import output_lock
with output_lock(sys.argv[1], 'fixture') as lease:
    with lease.child_options() as options:
        subprocess.Popen([sys.executable, '-c', sys.argv[4], sys.argv[2], sys.argv[3]],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), **options)
    os._exit(0)
'''
    process = spawn(controller, path, started, stop, child)
    try:
        success(process)
        wait_file(started)
        with pytest.raises(OutputBusyError):
            with output_lock(path, 'must-wait-for-child', timeout=.05, poll_seconds=.01):
                pytest.fail('Live encoding child lost its inherited lock')
    finally:
        stop.write_text('stop')
    with output_lock(path, 'after-child', timeout=5, poll_seconds=.02):
        pass


def make_release(project, episode):
    from anime.subtitle_coverage import scan, sha
    root = project / f'data/subtitle-studio/releases/42/S01E{episode:03d}/v0'
    root.mkdir(parents=True)
    (project / 'static/finished-media').mkdir(parents=True, exist_ok=True)
    ass = root / 'bilingual.ass'
    ass.write_text('[Events]\nFormat: Layer, Start, End, Style, Name, Text\n'
                   'Dialogue: 1,0:00:00.00,0:00:03.00,ZH,fixture,测试对白\n', encoding='utf-8')
    (root / 'static.ass').write_bytes(ass.read_bytes())
    audit = scan(ass, {'ZH': 'zh'}, [])
    coverage = {'schema': 1, 'role': 'episode_chinese_coverage_review', 'show_id': 42,
                'episode': episode, 'media_sha256': 'synthetic-media', 'ass_sha256': sha(ass),
                'events_sha256': audit['events_sha256'], 'styles': {'ZH': 'zh'}, 'sources': [],
                'reviewer': 'synthetic fixture', 'source_inventory_evidence': 'synthetic Chinese-only text',
                'decisions': []}
    (root / 'chinese-coverage-review.json').write_text(json.dumps(coverage), encoding='utf-8')
    for name in ('episode.mp4', 'subtitles.zip', '版本说明.txt'):
        (root / name).write_bytes(f'synthetic episode {episode}: {name}'.encode())
    (root / 'technical-qa.json').write_text(json.dumps({'probe': {'streams': [
        {'codec_type': 'video', 'height': 1080}]}}), encoding='utf-8')
    files = {p.name: {'bytes': p.stat().st_size, 'sha256': sha(p)} for p in root.iterdir()}
    manifest = {'id': f'fixture-e{episode}-v0', 'show_id': 42, 'season': 1, 'episode': episode,
                'version': 'v0', 'edition': 'v0', 'status': 'finished', 'duration_seconds': 3,
                'width': 1920, 'finished_at': 'synthetic fixture', 'files': files,
                'source_media_sha256': 'synthetic-media', 'source_ass_sha256': sha(ass)}
    (root / 'release.json').write_text(json.dumps(manifest), encoding='utf-8')
    (root / 'input-config.json').write_text('{}', encoding='utf-8')
    return root


@pytest.mark.requires_lab
def test_three_concurrent_publishers_merge_without_losing_release(tmp_path):
    roots = [make_release(tmp_path, ep) for ep in (1, 2, 3)]
    static = tmp_path / 'static/finished-media'
    stale = static / 'index-123.tmp'
    stale.write_bytes(b'preserved earlier crash evidence')
    (static / 'index.json').write_text(json.dumps({'schema': 1, 'releases': [
        {'id': 'existing-release', 'status': 'finished'}]}), encoding='utf-8')
    code = '''
import sys
from pathlib import Path
from scripts import publish_finished_episode as publisher
publisher.PROJECT = Path(sys.argv[1])
publisher.publish(Path(sys.argv[2]), 'Synthetic test series', None)
'''
    # Queue all three behind one actual lock, forcing the contested path.
    with output_lock(static / '.publish.lock', 'test-barrier'):
        workers = [spawn(code, tmp_path, root) for root in roots]
        for worker in workers:
            assert 'waiting_for_output_slot' in worker.stdout.readline()
    for worker in workers:
        assert 'registered_for_viewing' in success(worker)
    rows = json.loads((static / 'index.json').read_text(encoding='utf-8'))['releases']
    assert {row['id'] for row in rows} == {'existing-release', *(f'fixture-e{i}-v0' for i in (1, 2, 3))}
    assert len(rows) == 4 and stale.read_bytes() == b'preserved earlier crash evidence'
    assert list(static.glob('*.tmp')) == [stale]
    for root in roots:
        assert (root / 'publish-receipt.json').exists()
        assert os.path.samefile(root / 'episode.mp4', static / json.loads(
            (root / 'release.json').read_text())['id'] / 'episode.mp4')
    assert not (tmp_path / 'data/anime.db').exists()


@pytest.mark.requires_lab
def test_publication_busy_marker_creates_no_release_links_or_receipt(tmp_path, monkeypatch):
    from scripts import publish_finished_episode as publisher
    root = make_release(tmp_path, 1)
    monkeypatch.setattr(publisher, 'PROJECT', tmp_path)
    static = tmp_path / 'static/finished-media'
    marker = static / '.publish.lock'
    marker.write_bytes(b'{"pid":123,"release":"legacy"}')
    with pytest.raises(OutputBusyError):
        publisher.publish(root, 'Fixture', None, lock_timeout=0)
    assert not (static / 'fixture-e1-v0').exists()
    assert not (static / 'index.json').exists()
    assert not (root / 'publish-receipt.json').exists()


@pytest.mark.requires_lab
def test_publish_retry_keeps_one_immutable_entry(tmp_path, monkeypatch):
    from scripts import publish_finished_episode as publisher
    root = make_release(tmp_path, 1)
    monkeypatch.setattr(publisher, 'PROJECT', tmp_path)
    publisher.publish(root, 'Fixture', None)
    receipt = (root / 'publish-receipt.json').read_bytes()
    publisher.publish(root, 'Fixture', None)
    rows = json.loads((tmp_path / 'static/finished-media/index.json').read_text())['releases']
    assert len(rows) == 1 and (root / 'publish-receipt.json').read_bytes() == receipt
    with pytest.raises(AssertionError, match='registered differently'):
        publisher.publish(root, 'Changed immutable title', None)
    assert len(json.loads((tmp_path / 'static/finished-media/index.json').read_text())['releases']) == 1


def build_config(project, root):
    config = project / 'render-config.json'
    output = project / 'data/subtitle-studio/releases/new-release'
    config.write_text(json.dumps({'output': str(output), 'ass': str(root / 'bilingual.ass'),
        'show_id': 42, 'episode': 1, 'media_sha256': 'synthetic-media',
        'chinese_coverage_review': str(root / 'chinese-coverage-review.json')}), encoding='utf-8')
    return config, output


@pytest.mark.requires_lab
def test_busy_encoding_slot_leaves_output_uncreated(tmp_path, monkeypatch):
    from scripts import finish_subtitle_episode as finisher
    from scripts import subtitle_output_lock as locks
    root = make_release(tmp_path, 1)
    config, output = build_config(tmp_path, root)
    monkeypatch.setattr(finisher, 'PROJECT', tmp_path)
    # Existing validation also resolves project from __file__.
    monkeypatch.setattr(finisher, '__file__', str(tmp_path / 'scripts/finish_subtitle_episode.py'))
    real_lock = locks.output_lock
    monkeypatch.setattr(locks, 'output_lock', lambda path, purpose, **kw:
                        real_lock(path, purpose, timeout=.03, poll_seconds=.01))
    with real_lock(tmp_path / 'data/subtitle-studio/.render.process.lock', 'existing-render'):
        with pytest.raises(OutputBusyError):
            finisher.build(config)
    assert not output.exists()
    assert not list(tmp_path.rglob('worker.lock.json'))
    assert not (tmp_path / 'data/anime.db').exists()


@pytest.mark.requires_lab
def test_encoding_rechecks_watched_gate_after_wait_before_output(tmp_path, monkeypatch):
    from scripts import finish_subtitle_episode as finisher
    root = make_release(tmp_path, 1)
    config, output = build_config(tmp_path, root)
    monkeypatch.setattr(finisher, 'PROJECT', tmp_path)
    monkeypatch.setattr(finisher, '__file__', str(tmp_path / 'scripts/finish_subtitle_episode.py'))
    checks = []
    def watched_gate(c):
        checks.append(c['episode'])
        if len(checks) == 2:
            raise ValueError('Episode already watched; preserve existing subtitles')
    monkeypatch.setattr(finisher, 'check_unwatched', watched_gate)
    with pytest.raises(ValueError, match='already watched'):
        finisher.build(config)
    assert checks == [1, 1] and not output.exists()
    assert not (tmp_path / 'data/anime.db').exists()
