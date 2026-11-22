"""Original synthetic dialogue only; no production database or releases."""
import json

import pytest

from anime.subtitle_coverage import scan, sha, validate, validate_release


def fixture(tmp_path):
    root = tmp_path / 'data/subtitle-studio'
    root.mkdir(parents=True)
    ass = root / 'test.ass'
    ass.write_text('[Events]\nFormat: Layer, Start, End, Style, Name, Text\n'
                   'Dialogue: 0,0:00:01.00,0:00:02.00,ZH,z1,{\\blur2}等一下\n'
                   'Dialogue: 1,0:00:01.00,0:00:02.00,ZH,z1,等一下\n'
                   'Dialogue: 1,0:00:01.00,0:00:02.00,JA,j1,待って\n', encoding='utf-8')
    source = root / 'ja.vtt'
    source.write_text('WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n待って\n', encoding='utf-8')
    sources = [{'path': str(source), 'sha256': sha(source), 'language': 'ja'}]
    audit = scan(ass, {'ZH': 'zh', 'JA': 'source'}, sources)
    report = {'schema': 1, 'role': 'episode_chinese_coverage_review', 'show_id': 42, 'episode': 1,
              'media_sha256': 'fixture-media', 'ass_sha256': sha(ass), 'events_sha256': audit['events_sha256'],
              'styles': {'ZH': 'zh', 'JA': 'source'}, 'sources': sources, 'reviewer': 'synthetic reviewer',
              'source_inventory_evidence': 'Synthetic source inventory only',
              'decisions': [{'id': i['id'], 'status': 'translated', 'reviewer': 'synthetic reviewer',
                             'reason': 'Same request in synthetic fixture',
                             'zh_ids': [audit['chinese_events'][0]['id']]} for i in audit['items']]}
    path = root / 'coverage.json'
    return ass, source, path, report


def check(tmp_path, ass, path, report):
    path.write_text(json.dumps(report), encoding='utf-8')
    return validate(tmp_path, path, ass, 42, 1, 'fixture-media')


@pytest.mark.requires_lab
def test_shadow_is_one_translation_and_title_change_preserves_coverage(tmp_path):
    ass, source, path, report = fixture(tmp_path)
    result = check(tmp_path, ass, path, report)
    assert result['source_cues_and_display_events'] == 2
    assert len(scan(ass, report['styles'], report['sources'])['chinese_events']) == 1
    ass.write_text('[Script Info]\nTitle: revised\n' + ass.read_text(encoding='utf-8'), encoding='utf-8')
    assert validate(tmp_path, path, ass, 42, 1, 'fixture-media', report['ass_sha256'])


@pytest.mark.requires_lab
@pytest.mark.parametrize('failure', ['pending', 'missing_source_cue', 'changed_ass', 'changed_source',
                                     'no_source_inventory', 'wrong_chinese', 'boolean_only',
                                     'blanket_exception', 'dialogue_as_screen_text', 'unknown_style'])
def test_rejects_previous_coverage_shortcuts(tmp_path, failure):
    ass, source, path, report = fixture(tmp_path)
    if failure == 'pending': report['decisions'][0]['status'] = 'pending'
    if failure == 'missing_source_cue': report['decisions'].pop()
    if failure == 'changed_ass': ass.write_text(ass.read_text(encoding='utf-8').replace('等一下', '走开'), encoding='utf-8')
    if failure == 'changed_source': source.write_text('changed', encoding='utf-8')
    if failure == 'no_source_inventory': report['sources'] = []
    if failure == 'wrong_chinese': report['decisions'][0]['zh_ids'] = ['absent']
    if failure == 'boolean_only': report['decisions'] = []; report['complete_chinese_coverage_checked'] = True
    if failure == 'blanket_exception': report['decisions'][0]['status'] = 'nonverbal'
    if failure == 'dialogue_as_screen_text':
        report['decisions'][0].update(status='nonessential_screen_text', kind='dialogue')
    if failure == 'unknown_style': del report['styles']['JA']
    with pytest.raises(ValueError): check(tmp_path, ass, path, report)


@pytest.mark.requires_lab
def test_chinese_parent_preservation_does_not_cover_japanese_only_sentence(tmp_path):
    ass, source, path, report = fixture(tmp_path)
    # The Chinese base is unchanged, yet a new meaningful original sentence exists.
    source.write_text(source.read_text(encoding='utf-8') + '\n00:00:03.000 --> 00:00:05.000\nここにいて\n', encoding='utf-8')
    report['sources'][0]['sha256'] = sha(source)
    with pytest.raises(ValueError, match='Every original cue'): check(tmp_path, ass, path, report)


@pytest.mark.requires_lab
def test_gap_recomputed_against_corresponding_chinese_not_unrelated_overlap(tmp_path):
    ass, source, path, report = fixture(tmp_path)
    # Some other speaker has Chinese after the mapped line disappears.
    text = ass.read_text(encoding='utf-8').replace('0:00:02.00,JA', '0:00:03.00,JA')
    ass.write_text(text + 'Dialogue: 1,0:00:02.00,0:00:03.00,ZH,z2,天气不错\n', encoding='utf-8')
    audit = scan(ass, report['styles'], report['sources'])
    report.update(ass_sha256=sha(ass), events_sha256=audit['events_sha256'])
    mapped = next(e['id'] for e in audit['chinese_events'] if e['actor'] == 'z1')
    report['decisions'] = [{'id': i['id'], 'status': 'translated', 'reviewer': 'fixture',
                            'reason': 'Synthetic mapped request', 'zh_ids': [mapped]} for i in audit['items']]
    assert not audit['items'][0]['uncovered_ms']  # Global overlap alone would miss it.
    with pytest.raises(ValueError, match='unreviewed source-only'): check(tmp_path, ass, path, report)
    evidence = ass.parent / 'timing.txt'; evidence.write_text('Synthetic timing observation', encoding='utf-8')
    report['decisions'][0]['timing_review'] = {'windows_ms': [[2000, 3000]],
        'status': 'verified_correspondence', 'reason': 'Synthetic reading hold checked',
        'evidence_refs': [{'path': str(evidence), 'sha256': sha(evidence)}]}
    assert check(tmp_path, ass, path, report)
    evidence.write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='evidence changed'): check(tmp_path, ass, path, report)


def test_new_release_cannot_bypass_with_player_boolean(tmp_path):
    with pytest.raises(ValueError, match='coverage review missing'):
        validate_release(tmp_path, tmp_path, {'files': {}, 'complete_chinese_coverage_checked': True})


def test_encoding_rejects_missing_report_before_creating_output(tmp_path):
    from scripts.finish_subtitle_episode import build
    output = tmp_path / 'new-release'
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'output': str(output), 'ass': str(tmp_path / 'not-needed.ass'),
                                 'show_id': 42, 'episode': 1, 'media_sha256': 'fixture'}))
    with pytest.raises(ValueError, match='coverage review required'): build(config)
    assert not output.exists()


def test_publishing_rejects_missing_report_before_static_writes(tmp_path, monkeypatch):
    from scripts import publish_finished_episode as publisher
    monkeypatch.setattr(publisher, 'PROJECT', tmp_path)
    root = tmp_path / 'data/subtitle-studio/releases/42/S01E001/v0'
    root.mkdir(parents=True)
    (root / 'release.json').write_text(json.dumps({'id': 'fixture-v0', 'status': 'finished', 'files': {}}))
    (root / 'input-config.json').write_text('{}')
    with pytest.raises(ValueError, match='coverage review missing'): publisher.publish(root, 'fixture', None)
    assert not (tmp_path / 'static').exists()


def test_existing_identical_release_can_be_reused_without_rewriting(tmp_path, capsys, monkeypatch):
    from scripts import finish_subtitle_episode
    monkeypatch.setattr(finish_subtitle_episode, 'PROJECT', tmp_path)
    build = finish_subtitle_episode.build
    root = tmp_path / 'existing'; root.mkdir()
    config = tmp_path / 'config.json'; config.write_text(json.dumps({'output': str(root)}))
    manifest = root / 'release.json'
    manifest.write_text(json.dumps({'input_config_sha256': sha(config), 'files': {}}))
    before = manifest.read_bytes()
    build(config)
    assert manifest.read_bytes() == before
    assert 'existing_completed_release_reused' in capsys.readouterr().out
