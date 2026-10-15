"""Verify finished viewing delivery without manufacturing formal acceptance."""
import hashlib
import json
import re
from pathlib import Path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def verify_release(project, release, player_report, sid, ep, target):
    project = Path(project).resolve()
    root = Path(release).resolve()
    if not root.is_relative_to(project / 'data/subtitle-studio/releases'):
        raise ValueError('Release must be in the private releases directory')
    manifest = root / 'release.json'
    r = json.loads(manifest.read_text(encoding='utf-8-sig'))
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', r.get('id', '')):
        raise ValueError('Invalid local release identifier')
    if (r.get('show_id') != sid or r.get('episode') != ep or r.get('status') != 'finished'
            or r.get('accepted') is True or (root / 'worker.lock.json').exists()):
        raise ValueError('Wrong/incomplete release or active writer')
    edition = r.get('edition')
    if edition is not None and edition not in ('v0', 'v1'):
        raise ValueError('Unknown viewing edition')
    config = root / 'input-config.json'
    c = json.loads(config.read_text(encoding='utf-8-sig'))
    source = Path(c['media']).resolve()
    from .subtitle_studio import lab_core
    if (digest(config) != r.get('input_config_sha256')
            or source != Path(target['path']).resolve() or source.stat().st_size != target['size']
            or lab_core().fingerprint(source) != target['fingerprint']
            or not r.get('source_media_sha256') or digest(source) != r['source_media_sha256']
            or c.get('media_sha256') != r['source_media_sha256']):
        raise ValueError('Finished output does not bind the exact dispatched source')
    files = r.get('files') or {}
    if not {'bilingual.ass', 'static.ass', 'subtitles.zip', 'technical-qa.json', '版本说明.txt'} <= files.keys():
        raise ValueError('Missing finished release files')
    for name, record in files.items():
        path = (root / name).resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or path.stat().st_size != record['bytes'] or digest(path) != record['sha256']):
            raise ValueError('Release file hash/ownership changed')
    movies = [name for name in files if name.endswith('.mp4')]
    if len(movies) != 1:
        raise ValueError('One complete movie required')
    qa = json.loads((root / 'technical-qa.json').read_text(encoding='utf-8-sig'))
    if (qa.get('decode_passed') is not True or qa.get('source_stat_unchanged') is not True
            or not qa.get('source_audio_packet_sha256')
            or qa['source_audio_packet_sha256'] != qa.get('output_audio_packet_sha256')
            or abs(float(qa['duration_seconds']) - float(r['duration_seconds'])) > .5):
        raise ValueError('Complete decode/duration/original audio proof missing')
    proof = Path(player_report).resolve()
    if not proof.is_relative_to(project / 'data/subtitle-studio'):
        raise ValueError('Player proof must remain private')
    p = json.loads(proof.read_text(encoding='utf-8-sig'))
    expected_video = files[movies[0]]['sha256']
    if (p.get('role') != 'finished_episode_player_check' or p.get('release_id') != r['id']
            or p.get('video_sha256') != expected_video or p.get('reviewer') is None
            or not p.get('evidence') or p.get('errors') != []
            or not all(p.get(k) is True for k in ('play_pause_passed', 'seek_passed', 'download_passed',
                                                 'complete_chinese_coverage_checked'))):
        raise ValueError('Exact actual viewing/coverage proof missing')
    index = json.loads((project / 'static/finished-media/index.json').read_text(encoding='utf-8-sig'))
    matches = [x for x in index['releases'] if x['id'] == r['id'] and x['show_id'] == sid
               and x['episode'] == ep and x['status'] == 'finished' and x['video_sha256'] == expected_video]
    linked = project / 'static/finished-media' / r['id'] / 'episode.mp4'
    if len(matches) != 1 or not linked.is_file() or digest(linked) != expected_video:
        raise ValueError('Actual local viewer file/index binding missing')
    extra = {}
    if edition:
        extra['edition'] = edition
    if edition == 'v1':
        from .subtitle_refinement import validate_review
        if 'refinement-review.json' not in files:
            raise ValueError('V1 refinement report missing from immutable release')
        extra['baseline_release_id'] = r.get('baseline_release_id')
        extra['refinement_review'] = validate_review(project, root / 'refinement-review.json', sid, ep,
                                                     r['source_media_sha256'], r.get('baseline_release_id'))
    return {**extra, 'release_id': r['id'], 'delivery_status': 'finished', 'accepted': False,
            'formal_review_complete': r.get('formal_review_complete') is True,
            'manifest_sha256': digest(manifest), 'release': str(root),
            'player_report': {'path': str(proof), 'sha256': digest(proof)},
            'video_sha256': expected_video,
            'player_url': 'http://127.0.0.1:4871/static/finished-player.html?release=' + r['id']}
