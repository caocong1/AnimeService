"""Expose a completed local MP4 in the existing authenticated static viewer.

This registers a viewing deliverable, not a fabricated production acceptance.
No service restart, database, source media or existing default is modified.
"""
import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def publish(root, series_title, library):
    root = root.resolve()
    assert root.is_relative_to(PROJECT / 'data/subtitle-studio/releases')
    r = json.loads((root / 'release.json').read_text(encoding='utf-8'))
    config = json.loads((root / 'input-config.json').read_text(encoding='utf-8-sig'))
    if config.get('delivery_job_id'):
        import sys
        sys.path.insert(0, str(PROJECT))
        from scripts.subtitle_delivery_queue import ReadStore
        db = ReadStore(PROJECT / 'data/anime.db')
        try:
            if db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1', (r['show_id'],r['episode'])):
                raise ValueError('Episode already watched; do not publish subtitle changes')
        finally:
            db.con.rollback(); db.con.close()
    if r.get('edition') == 'v1':
        import sys
        sys.path.insert(0, str(PROJECT))
        from anime.subtitle_refinement import validate_review
        validate_review(PROJECT, root / 'refinement-review.json', r['show_id'], r['episode'],
                        r['source_media_sha256'], r.get('baseline_release_id'))
    assert r['status'] == 'finished' and not (root / 'worker.lock.json').exists()
    import re
    assert re.fullmatch('[a-z0-9][a-z0-9-]{0,79}', r['id'])
    assert 'accepted' not in r or r['accepted'] is False
    for name, record in r['files'].items():
        p = (root / name).resolve()
        assert p.is_relative_to(root) and p.is_file()
        assert p.stat().st_size == record['bytes'] and digest(p) == record['sha256']
    movie = next(root / name for name in r['files'] if name.endswith('.mp4'))
    static = PROJECT / 'static/finished-media'
    static.mkdir(exist_ok=True)
    lock = static / '.publish.lock'
    with lock.open('x', encoding='utf-8') as f:
        json.dump({'pid': os.getpid(), 'release': r['id']}, f)
    try:
        dest = static / r['id']
        dest.mkdir(exist_ok=True)
        links = []
        def link(src, target):
            if target.exists():
                assert digest(target) == digest(src), 'Existing output differs; create a new version'
            else:
                os.link(src, target)
            links.append({'source': str(src), 'path': str(target), 'same_file': os.path.samefile(src, target)})
        link(movie, dest / 'episode.mp4')
        link(root / 'subtitles.zip', dest / 'subtitles.zip')
        link(root / '版本说明.txt', dest / 'notes.txt')
        if library:
            library = library.resolve()
            assert library.is_relative_to(Path('D:/MediaLibrary/Anime').resolve())
            library.mkdir(parents=True, exist_ok=True)
            link(movie, library / movie.name)
            link(root / 'subtitles.zip', library / (movie.stem + '.字幕.zip'))
            link(root / '版本说明.txt', library / (movie.stem + '.说明.txt'))
        index_path = static / 'index.json'
        previous = index_path.read_bytes() if index_path.exists() else None
        index = json.loads(previous.decode('utf-8-sig')) if previous else {'schema': 1, 'releases': []}
        entry = {k: r[k] for k in ('id', 'show_id', 'season', 'episode', 'version', 'status', 'duration_seconds', 'width', 'finished_at')}
        if r.get('edition'):
            entry['edition'] = r['edition']
        qa=json.loads((root / 'technical-qa.json').read_text(encoding='utf-8'))
        height=next(s['height'] for s in qa['probe']['streams'] if s['codec_type']=='video')
        entry.update(title=series_title, height=height, video_sha256=r['files'][movie.name]['sha256'])
        existing = next((x for x in index['releases'] if x['id'] == r['id']), None)
        if existing:
            comparable = dict(existing)
            if not r.get('edition'):
                # User-facing legacy v0 labels do not rewrite immutable files.
                comparable.pop('edition', None)
                comparable.pop('legacy_artifact_version', None)
            assert comparable == entry, 'Release identifier already registered differently'
        else:
            index['releases'].append(entry)
        if previous and not (root / 'index-before.json').exists():
            (root / 'index-before.json').write_bytes(previous)
        tmp = static / f'index-{os.getpid()}.tmp'
        with tmp.open('x', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        assert (index_path.read_bytes() if index_path.exists() else None) == previous
        os.replace(tmp, index_path)
        receipt = {'at': datetime.now().astimezone().isoformat(), 'status': 'registered_for_viewing',
                   'links': links, 'library_url': 'http://127.0.0.1:4871/subtitle-library?show=' + str(r['show_id']),
                   'player_url': 'http://127.0.0.1:4871/static/finished-player.html?release=' + r['id'],
                   'formal_review_complete': False, 'production_database_changed': False, 'default_changed': False}
        receipt_path=root / 'publish-receipt.json'
        if not receipt_path.exists():
            with receipt_path.open('x', encoding='utf-8') as f:
                json.dump(receipt, f, ensure_ascii=False, indent=2)
        print(json.dumps(receipt, ensure_ascii=False))
    finally:
        if lock.exists() and json.loads(lock.read_text())['pid'] == os.getpid():
            lock.unlink()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('release', type=Path)
    p.add_argument('--series-title', required=True)
    p.add_argument('--library-dir', type=Path)
    a = p.parse_args()
    publish(a.release, a.series_title, a.library_dir)
