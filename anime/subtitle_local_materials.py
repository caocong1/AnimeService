"""Exact-E1 subtitle material receipts for already completed legacy torrents.

This is not a downloader provider. Only this module's immutable registry is
written; shows, tasks, ordinary episodes, owners and acceptance stay untouched.
The host must explicitly integrate this projection into subtitle-only consumers.
Never use ``allowed`` to grant acquisition/submission permission.
"""
import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .fidelity import fidelity_blocker
from .releases import bdecode, select_files, validate_title

TABLE = 'subtitle_local_materials'
SCHEMA = 'subtitle-local-material-request/v1'
AUTHORITY = 'subtitle-exact-local-material-authority/v1'
KIND = 'local_completed_torrent'


def _json(raw):
    return json.loads(raw.decode('utf-8-sig'), parse_constant=lambda _: _fail('Non-finite JSON'))


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _fail(message):
    raise ValueError(message)


def _path(db):
    path = Path(getattr(db, 'path', db)).resolve()
    if not path.is_file():
        _fail('Existing database required')
    return path


def _connect(path, write=False):
    c = sqlite3.connect(path.as_uri() + ('?mode=rw' if write else '?mode=ro'), uri=True, timeout=20)
    c.row_factory = sqlite3.Row
    if not write:
        c.execute('PRAGMA query_only=ON')
    return c


@contextmanager
def _connection(path, connection=None):
    """Borrow the host's transaction when supplied; otherwise hold a read snapshot."""
    if connection is not None:
        yield connection
        return
    c = _connect(path)
    try:
        c.execute('BEGIN')
        yield c
    finally:
        c.close()


def _exists(c, table):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _rows(c, table, where='', args=()):
    return [dict(r) for r in c.execute('SELECT * FROM ' + table + where, args)] if _exists(c, table) else []


class _View:
    def __init__(self, c):
        self.c = c

    def get(self, key, default=None):
        rows = _rows(self.c, 'kv', ' WHERE key=?', (key,))
        return json.loads(rows[0]['value']) if rows else default


def _stat(path):
    s = path.stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def _read(path, limit=16 * 1024 * 1024):
    if not path.is_file() or path.stat().st_size > limit:
        _fail('Missing or oversized evidence: ' + str(path))
    before = _stat(path)
    raw = path.read_bytes()
    if before != _stat(path):
        _fail('Evidence changed while reading: ' + str(path))
    return raw


def _ref(root, ref, refs):
    if not isinstance(ref, dict) or not re.fullmatch('[a-f0-9]{64}', str(ref.get('sha256', ''))):
        _fail('Hash-bound private evidence reference required')
    p = Path(ref.get('path', '')).resolve()
    if not p.is_relative_to(root):
        _fail('Evidence must remain in the private subtitle archive')
    raw = _read(p)
    if _sha(raw) != ref['sha256']:
        _fail('Evidence SHA mismatch: ' + str(p))
    refs[str(p)] = ref['sha256']
    return raw


def _decode(raw):
    """Bounded bencode reader, also used for the actual original resume receipt."""
    pos = 0

    def value(depth=0):
        nonlocal pos
        if depth > 40 or pos >= len(raw):
            _fail('Invalid bencode')
        ch = raw[pos:pos + 1]
        pos += 1
        if ch == b'i':
            end = raw.index(b'e', pos)
            n = int(raw[pos:end])
            pos = end + 1
            return n
        if ch in (b'd', b'l'):
            obj = {} if ch == b'd' else []
            while raw[pos:pos + 1] != b'e':
                if ch == b'd':
                    key = value(depth + 1)
                    if not isinstance(key, bytes) or key in obj:
                        _fail('Duplicate or invalid bencode key')
                    obj[key] = value(depth + 1)
                else:
                    obj.append(value(depth + 1))
            pos += 1
            return obj
        if ch.isdigit():
            end = raw.index(b':', pos - 1)
            length = int(raw[pos - 1:end])
            pos = end + 1 + length
            if length < 0 or pos > len(raw):
                _fail('Invalid bencode length')
            return raw[end + 1:pos]
        _fail('Invalid bencode token')

    try:
        obj = value()
        if pos != len(raw) or not isinstance(obj, dict):
            _fail('Invalid bencode root')
        return obj
    except (IndexError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError('Invalid bencode structure') from exc


def _safe_file(base, name):
    rel = PurePosixPath(name.replace('\\', '/'))
    if rel.is_absolute() or any(p in ('..', '.', '') for p in rel.parts) or ':' in name:
        _fail('Unsafe torrent file path')
    p = base.joinpath(*rel.parts).resolve()
    if not p.is_relative_to(base):
        _fail('Torrent file escaped original save path')
    return p


def _load(path, spec_path):
    root = (path.parent / 'subtitle-studio').resolve()
    spec_path = Path(spec_path).resolve()
    if not spec_path.is_relative_to(root):
        _fail('Request must be in the private subtitle archive')
    raw = _read(spec_path)
    s = _json(raw)
    refs = {str(spec_path): _sha(raw)}
    if not isinstance(s, dict) or s.get('schema') != SCHEMA or s.get('kind') != KIND:
        _fail('Unsupported local material schema/kind; non-torrent media needs its own contract')
    if any(type(s.get(k)) is not int or s[k] < 1 for k in ('show_id', 'season', 'episode')) or s['episode'] != 1:
        _fail('Only the exact authorized E1 is supported')
    sid, season, ep = s['show_id'], s['season'], s['episode']
    media, source, mapping = s['media'], s['source'], s['mapping']
    if (type(media.get('bytes')) is not int or media['bytes'] <= 0
            or not re.fullmatch('[a-f0-9]{64}', str(media.get('sha256', '')))
            or not re.fullmatch('[a-f0-9]{40}', str(source.get('infohash', '')))
            or type(source.get('file_index')) is not int or source['file_index'] < 0):
        _fail('Real file SHA/size, torrent infohash and file index required')
    if (mapping.get('season') != season or mapping.get('start') != 1 or mapping.get('end') != 1
            or mapping.get('confirmed') is not True or mapping.get('inventory_checked') is not True
            or mapping.get('require_season') is not True):
        _fail('Confirmed exact-E1 seasonal mapping required')
    auth = _json(_ref(root, s['authority'], refs))
    if (auth.get('schema') != AUTHORITY or auth.get('action') != 'register_existing_local_material'
            or auth.get('scope') != 'subtitle_only_no_download'
            or any(type(auth.get(k)) is not int or auth[k] != s[k] for k in ('show_id', 'season', 'episode'))):
        _fail('Exact-E1 subtitle-only authority required')
    request = _json(_ref(root, auth['request'], refs))
    if f'episode:{sid}:{ep}' not in request.get('preparation_requested', []):
        _fail('Current request does not contain this preparation episode')
    handoff = _json(_ref(root, auth['handoff'], refs))
    jobs = [j for j in handoff.get('jobs', []) if (j.get('show_id'), j.get('season'), j.get('episode')) == (sid, season, ep)]
    if handoff.get('schema') != 'subtitle-exact-e1-entry-engineering-handoff/v1' or len(jobs) != 1:
        _fail('Exact original engineering handoff required')
    original = jobs[0].get('existing_media', {})
    if (Path(original.get('path', '')).resolve() != Path(media['path']).resolve()
            or original.get('bytes') != media['bytes'] or original.get('sha256') != media['sha256']
            or original.get('real_torrent_infohash') != source['infohash']
            or original.get('torrent_file_index') != source['file_index']):
        _fail('Original handoff identity does not match the requested material')
    proposal_ref = s['mapping_proposal']
    proposal_path = Path(proposal_ref.get('path', '')).resolve()
    if (proposal_path != Path(jobs[0].get('local_scope_proposal', '')).resolve()
            or not any(Path(r.get('path', '')).resolve() == proposal_path
                       and r.get('sha256') == proposal_ref.get('sha256') for r in handoff.get('evidence', []))):
        _fail('Mapping proposal must be bound to the original handoff')
    proposal = _json(_ref(root, proposal_ref, refs))
    if (proposal.get('mapping') != mapping or proposal.get('authorization_bounds') != [1, 1]
            or proposal.get('requested_selected_local_episodes') != [1]
            or proposal.get('infohash') != source['infohash'] or proposal.get('source_title') != source.get('title')
            or proposal.get('source_url') != source.get('url') or proposal.get('ids') != [source['file_index']]):
        _fail('Original exact-E1 mapping/source proposal mismatch')
    torrent = _ref(root, s['torrent'], refs)
    infohash, files = bdecode(torrent, include_padding=True)
    if infohash != source['infohash']:
        _fail('Real torrent infohash mismatch')
    title_eps = validate_title(source['title'], mapping, {}, False)
    chosen, ids = select_files(files, mapping, wanted=[ep], title_eps=title_eps)
    selection = {'selected': {str(k): v for k, v in chosen.items()}, 'ids': ids, 'files': files}
    selected = selection.get('selected', {}).get(str(ep))
    if (selection.get('ids') != [source['file_index']] or set(selection.get('selected', {})) != {str(ep)}
            or not selected or selected.get('index') != source['file_index'] or selected.get('size') != media['bytes']):
        _fail('The real torrent must select exactly E1 and its original file index/size')
    declared = proposal.get('selected', {}).get(str(ep), {})
    if any(declared.get(k) != selected.get(k) for k in ('index', 'name', 'size')):
        _fail('Original mapping proposal selects a different physical file')
    if not isinstance(s.get('provenance'), list) or not s['provenance']:
        _fail('Archived identity/completion provenance required')
    for ref in s['provenance']:
        _ref(root, ref, refs)
    s['media']['path'] = str(Path(media['path']).resolve())
    return s, refs, torrent, files, selection


def _legacy(path, s, torrent, files):
    backup = Path(os.environ.get('LOCALAPPDATA', '')).resolve() / 'qBittorrent' / 'BT_backup'
    if not os.environ.get('LOCALAPPDATA'):
        _fail('Original local qB receipt directory is unavailable')
    h = s['source']['infohash']
    resume_path, torrent_path = backup / (h + '.fastresume'), backup / (h + '.torrent')
    raw, original = _read(resume_path), _read(torrent_path)
    resume = _decode(raw)
    original_hash, original_files = bdecode(original, include_padding=True)
    if original_hash != h or original_files != files or _decode(original)[b'info'] != _decode(torrent)[b'info']:
        _fail('Original torrent receipt differs from the real source torrent')
    priorities = resume.get(b'file_priority', [])
    index = s['source']['file_index']
    if (resume.get(b'file-format') != b'libtorrent resume file' or resume.get(b'info-hash') != bytes.fromhex(h)
            or type(resume.get(b'completed_time')) is not int or resume[b'completed_time'] <= 0
            or resume.get(b'paused') != 1 or resume.get(b'mapped_files')
            or not isinstance(priorities, list) or len(priorities) != len(files)
            or type(priorities[index]) is not int or priorities[index] <= 0):
        _fail('Original completed paused legacy receipt required; ownership is not adopted')
    try:
        base = Path(resume[b'save_path'].decode('utf-8')).resolve()
    except (KeyError, UnicodeDecodeError, AttributeError) as exc:
        raise ValueError('Original legacy save path required') from exc
    if _safe_file(base, files[index]['name']) != Path(s['media']['path']):
        _fail('Original completed save path/file identity mismatch')
    inventory_path = path.parent / 'inventory.json'
    inventory_raw = _read(inventory_path, 64 * 1024 * 1024)
    entries = [r for r in _json(inventory_raw) if Path(r.get('path', '')).resolve() == Path(s['media']['path'])]
    st = Path(s['media']['path']).stat()
    if (len(entries) != 1 or entries[0].get('verified_complete') is not True
            or entries[0].get('legacy_active') is not False or entries[0].get('completion_source') != 'legacy_qbit'
            or entries[0].get('size') != st.st_size or entries[0].get('mtime') != st.st_mtime):
        _fail('Fresh original completed/inactive inventory entry required')
    return base, {str(resume_path): _sha(raw), str(torrent_path): _sha(original), str(inventory_path): _sha(inventory_raw)}, {
        'resume': raw, 'torrent': original, 'inventory': inventory_raw, 'completed_time': resume[b'completed_time']}


def _verify_media(s, base, torrent, files):
    media = Path(s['media']['path'])
    before = _stat(media)
    if before[2] != s['media']['bytes']:
        _fail('Actual file size mismatch')
    h = hashlib.sha256()
    with media.open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(block)
    if h.hexdigest() != s['media']['sha256']:
        _fail('Actual full file SHA mismatch')
    info = _decode(torrent)[b'info']
    length, pieces = info.get(b'piece length'), info.get(b'pieces')
    total = sum(f['size'] for f in files)
    if (type(length) is not int or not 1 <= length <= 64 * 1024 * 1024 or not isinstance(pieces, bytes)
            or len(pieces) != 20 * ((total + length - 1) // length)):
        _fail('Invalid real torrent piece manifest')
    index = s['source']['file_index']
    start = sum(f['size'] for f in files[:index])
    end = start + files[index]['size']
    stamps = {str(media): before}
    tested = 0
    for piece in range(start // length, (end - 1) // length + 1):
        a, b = piece * length, min((piece + 1) * length, total)
        digest, offset = hashlib.sha1(), 0
        for file in files:
            lo, hi = max(a, offset), min(b, offset + file['size'])
            if hi > lo:
                if file.get('padding'):
                    digest.update(bytes(hi - lo))
                else:
                    p = _safe_file(base, file['name'])
                    stamp = _stat(p)
                    if stamp[2] != file['size']:
                        _fail('Torrent boundary file size mismatch')
                    if str(p) in stamps and stamps[str(p)] != stamp:
                        _fail('Media changed during verification')
                    stamps[str(p)] = stamp
                    with p.open('rb') as stream:
                        stream.seek(lo - offset)
                        data = stream.read(hi - lo)
                    if len(data) != hi - lo:
                        _fail('Incomplete torrent piece read')
                    digest.update(data)
            offset += file['size']
        if digest.digest() != pieces[piece * 20:(piece + 1) * 20]:
            _fail('Actual overlapping torrent piece mismatch')
        tested += 1
    if any(_stat(Path(p)) != stamp for p, stamp in stamps.items()):
        _fail('Media changed during verification')
    return {'full_sha256': h.hexdigest(), 'overlapping_pieces': tested, 'all_match': True}, stamps


def _guard(c, s, registration=True):
    sid, ep = s['show_id'], s['episode']
    shows = _rows(c, 'shows', ' WHERE id=?', (sid,))
    if (len(shows) != 1 or shows[0].get('selected') != 1 or shows[0].get('state') != 'watching'
            or shows[0].get('cleanup_hold')):
        _fail('Current selected/watching/cleanup scope is protected')
    mapping = json.loads(shows[0]['mapping'])
    if (mapping.get('season', 1) != s['season'] or not mapping.get('confirmed') or not mapping.get('inventory_checked')
            or str(mapping.get('mikan_id', '')) != str(s['mapping'].get('mikan_id', ''))):
        _fail('Current seasonal identity/mapping changed')
    policy = _rows(c, 'source_policies', ' WHERE show_id=?', (sid,))
    if policy and json.loads(policy[0]['config']).get('mode') not in ('sampling', 'selected', 'subtitle_only'):
        _fail('Explicit source policy hold is protected')
    if any(r.get('finished') for r in _rows(c, 'watches', ' WHERE show_id=? AND episode=?', (sid, ep))):
        _fail('Watched episode is protected')
    if fidelity_blocker(_View(c), s['source']['title'], s['selection']):
        _fail('Current source fidelity/blacklist protection')
    h = s['source']['infohash']
    if _rows(c, 'tasks', ' WHERE hash=?', (h,)) or _rows(c, 'thunder_queue', ' WHERE hash=?', (h,)):
        _fail('Existing downloader task/queue ownership or unknown receipt is protected')
    if _View(c).get('thunder_dialog_hold_' + h):
        _fail('Unknown downloader dialog receipt is protected')
    # No cross-show aliasing of this actual path, including unrelated task selections.
    for row in _rows(c, 'episodes', ' WHERE path=?', (s['media']['path'],)):
        if (row['show_id'], row['episode']) != (sid, ep):
            _fail('Cross-show/episode media binding conflict')
    for row in _rows(c, TABLE):
        prior = json.loads(row['payload'])['request']
        same_member = (prior['source']['infohash'], prior['source']['file_index']) == (h, s['source']['file_index'])
        if ((same_member or Path(prior['media']['path']).resolve() == Path(s['media']['path']))
                and (row['show_id'], row['episode']) != (sid, ep)):
            _fail('Cross-show local material receipt conflict')
    for table in ('source_samples', 'subtitle_samples'):
        for row in _rows(c, table):
            if row.get('show_id') != sid and (row.get('hash') == h or
                    (row.get('path') and Path(row['path']).resolve() == Path(s['media']['path']))):
                _fail('Cross-show sample source identity conflict')
    for task in _rows(c, 'tasks'):
        sel = json.loads(task.get('selection') or '{}')
        for item in sel.get('selected', {}).values():
            if _safe_file(Path(task.get('save_path') or '.').resolve(), item['name']) == Path(s['media']['path']):
                _fail('Actual file already belongs to another downloader task')
    if not registration:
        return
    if _rows(c, 'episodes', ' WHERE show_id=? AND episode=?', (sid, ep)):
        _fail('Existing ordinary episode input requires its original controlled path')
    jobs = _rows(c, 'subtitle_delivery_jobs', ' WHERE show_id=? AND episode=?', (sid, ep))
    if len(jobs) != 1 or jobs[0].get('kind') != 'episode' or jobs[0].get('input_key') != s.get('expected_input'):
        _fail('Original preparation input/job changed')
    job = jobs[0]
    payload = json.loads(job.get('payload') or '{}')
    if (job.get('state') not in ('queued', 'blocked', 'preparing', 'ready')
            or any(job.get(k) for k in ('thread_id', 'lease_owner', 'lease_token', 'dispatch_token', 'dispatch_state', 'accepted_build_id'))
            or job.get('lease_expires', 0) > time.time() or payload.get('user_stopped') or payload.get('owner_stopped_writing')):
        _fail('Owner/stopped/lease/unknown/committing/completed input is protected')
    if _rows(c, 'subtitle_target_heads', ' WHERE show_id=? AND episode=?', (sid, ep)):
        _fail('Frozen target is protected')
    if _rows(c, 'subtitle_targets', ' WHERE show_id=? AND episode=?', (sid, ep)):
        _fail('Existing target history requires its original controlled path')
    if _View(c).get(f'subtitle_delivery_finished_{sid}_{ep}'):
        _fail('Existing finished delivery is protected')
    if _rows(c, 'subtitle_viewing_leases', ' WHERE show_id=? AND episode=? AND expires>?', (sid, ep, time.time())):
        _fail('Existing viewing lease is protected')
    for row in _rows(c, 'subtitle_studio_jobs', ' WHERE show_id=? AND episode=?', (sid, ep)):
        if row.get('state') in ('production', 'accepted', 'finished'):
            _fail('Existing studio writer/completion is protected')


def _snapshot(c, s):
    sid, ep = s['show_id'], s['episode']
    facts = {t: _rows(c, t, ' WHERE show_id=? AND episode=?', (sid, ep)) for t in
             ('watches', 'subtitle_delivery_jobs', 'subtitle_studio_jobs', 'subtitle_target_heads',
              'subtitle_targets', 'subtitle_viewing_leases')}
    facts.update({t: _rows(c, t) for t in ('tasks', 'episodes', TABLE)})
    facts['shows'] = _rows(c, 'shows', ' WHERE id=?', (sid,))
    facts['source_policies'] = _rows(c, 'source_policies', ' WHERE show_id=?', (sid,))
    facts['thunder_queue'] = _rows(c, 'thunder_queue', ' WHERE hash=?', (s['source']['infohash'],))
    view = _View(c)
    facts['settings'] = {k: view.get(k) for k in ('source_fidelity', 'source_publisher_blacklist',
        'thunder_dialog_hold_' + s['source']['infohash'], f'subtitle_delivery_finished_{sid}_{ep}')}
    return _sha(_dump(facts).encode())


def _check_refs(refs):
    for p, digest in refs.items():
        if _sha(_read(Path(p), 64 * 1024 * 1024)) != digest:
            _fail('Evidence/original completion changed during transaction')


def _release_index(path, s):
    index = path.parent.parent / 'static/finished-media/index.json'
    if not index.exists():
        return {}
    raw = _read(index)
    for release in _json(raw).get('releases', []):
        if (release.get('show_id'), release.get('season'), release.get('episode')) == (s['show_id'], s['season'], s['episode']):
            _fail('Existing published/indexed release is protected')
    return {str(index): _sha(raw)}


def _project(payload):
    s = payload['request']
    return {'kind': KIND, 'material_id': payload['id'], 'receipt_id': payload['id'], 'revision': payload['revision'],
            'show_id': s['show_id'], 'season': s['season'], 'episode': s['episode'],
            'path': s['media']['path'], 'size': s['media']['bytes'], 'sha256': s['media']['sha256'],
            'source_infohash': s['source']['infohash'], 'source_title': s['source']['title'],
            'selection': s['selection'], 'file_index': s['source']['file_index'],
            'completion': payload['verification'], 'subtitle_only': True, 'download_authorized': False,
            'quality_ready': False, 'accepted': False}


def _register(db, spec_path, apply=False):
    """Plan read-only by default; apply rechecks all bytes and guards under SQLite CAS."""
    path = _path(db)
    s, refs, torrent, files, selection = _load(path, spec_path)
    s['selection'] = selection
    base, original_refs, completion = _legacy(path, s, torrent, files)
    refs.update(original_refs)
    publication_refs = _release_index(path, s)
    refs.update(publication_refs)
    with _connection(path) as c:
        _guard(c, s)
        before = _snapshot(c, s)
        existing = _rows(c, TABLE, ' WHERE show_id=? AND episode=?', (s['show_id'], s['episode']))
    verification, stamps = _verify_media(s, base, torrent, files)
    # Identity is based on the exact immutable request, not timestamps/job progress.
    identity = 'local-e1-' + _sha(_dump(s).encode())
    if existing:
        payload = json.loads(existing[0]['payload'])
        if existing[0]['id'] != identity:
            _fail('Immutable local material already registered with a different request')
        current = get_material_by_id(path, identity)
        return {'registered': True, 'replayed': True, 'applied': False, 'material': current}
    result = {'registered': False, 'would_register': True, 'applied': False, 'material_id': identity,
              'show_id': s['show_id'], 'season': s['season'], 'episode': s['episode'], 'verification': verification,
              'source_infohash': s['source']['infohash'], 'selection': selection, 'download_submitted': False,
              'production_ready': False}
    if not apply:
        _check_refs(refs)
        return result
    # Content-addressed private copies preserve real original receipts, with no fabricated ACK.
    archive = Path(spec_path).resolve().parent / 'original-receipts'
    archive.mkdir(exist_ok=True)
    durable_refs = {p: h for p, h in refs.items() if p not in original_refs and p not in publication_refs}
    for key, suffix in (('resume', '.fastresume'), ('torrent', '.torrent'), ('inventory', '.inventory.json')):
        raw = completion[key]
        dest = archive / (_sha(raw) + suffix)
        try:
            with dest.open('xb') as stream:
                stream.write(raw)
        except FileExistsError:
            if _read(dest) != raw:
                _fail('Immutable original receipt archive conflict')
        durable_refs[str(dest)] = _sha(raw)
    payload = {'schema': 'subtitle-local-completion-receipt/v1', 'id': identity, 'request': s,
               'evidence': durable_refs, 'verification': verification,
               'original_completion': {'provider': 'legacy_qbit', 'completed_time': completion['completed_time'],
                                       'receipt_sha256': _sha(completion['resume']), 'ownership_adopted': False}}
    payload['revision'] = _sha(_dump(payload).encode())
    c = _connect(path, write=True)
    try:
        c.execute('BEGIN IMMEDIATE')
        if before != _snapshot(c, s):
            _fail('Protected database facts changed before registration transaction')
        _guard(c, s)
        _release_index(path, s)
        _check_refs(refs)
        _check_refs(durable_refs)
        actual, current_stamps = _verify_media(s, base, torrent, files)
        if current_stamps != stamps or actual != verification:
            _fail('Media identity changed before registration transaction')
        _check_refs(refs)
        _check_refs(durable_refs)
        if any(_stat(Path(p)) != stamp for p, stamp in stamps.items()):
            _fail('Media changed before receipt insert')
        c.execute('''CREATE TABLE IF NOT EXISTS subtitle_local_materials(
            id TEXT PRIMARY KEY,show_id INTEGER NOT NULL,season INTEGER NOT NULL,episode INTEGER NOT NULL,
            payload TEXT NOT NULL,created REAL NOT NULL,UNIQUE(show_id,episode))''')
        c.execute('INSERT INTO subtitle_local_materials VALUES(?,?,?,?,?,?)',
                  (identity, s['show_id'], s['season'], s['episode'], _dump(payload), time.time()))
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return {'registered': True, 'replayed': False, 'applied': True, 'material': _project(payload)}


def register(db, spec_path, apply=False):
    """Register only structurally valid exact requests; malformed inputs fail closed."""
    try:
        return _register(db, spec_path, apply)
    except (OSError, KeyError, TypeError, AttributeError, IndexError, OverflowError) as exc:
        raise ValueError('Invalid/incomplete local material request or original evidence: ' + str(exc)) from exc


def get_material(db, sid, ep, verify_media=True, *, connection=None):
    """Validated subtitle-only material; routine production ownership does not revoke it."""
    path = _path(db)
    with _connection(path, connection) as c:
        rows = _rows(c, TABLE, ' WHERE show_id=? AND episode=?', (sid, ep))
        if not rows:
            return None
        p = json.loads(rows[0]['payload'])
        rev = p.pop('revision')
        if _sha(_dump(p).encode()) != rev or p['id'] != rows[0]['id']:
            _fail('Local material receipt integrity mismatch')
        p['revision'] = rev
        if (p['request']['show_id'], p['request']['season'], p['request']['episode']) != (rows[0]['show_id'], rows[0]['season'], rows[0]['episode']):
            _fail('Local material registry identity mismatch')
        _guard(c, p['request'], registration=False)
    _check_refs(p['evidence'])
    media = Path(p['request']['media']['path'])
    before = _stat(media)
    if before[2] != p['request']['media']['bytes']:
        _fail('Actual file size mismatch')
    if verify_media:
        h = hashlib.sha256()
        with media.open('rb') as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
                h.update(block)
        if h.hexdigest() != p['request']['media']['sha256'] or _stat(media) != before:
            _fail('Actual full file SHA/identity mismatch')
    result = _project(p)
    result['actual_media_verified'] = bool(verify_media)
    return result


def get_material_by_id(db, identity, verify_media=True, *, connection=None):
    with _connection(_path(db), connection) as c:
        rows = _rows(c, TABLE, ' WHERE id=?', (identity,))
        return get_material(db, rows[0]['show_id'], rows[0]['episode'], verify_media, connection=c) if rows else None


def allowed(db, sid, ep, *, connection=None):
    """Exact subtitle scope only. This NEVER grants downloader/Store.allowed scope."""
    try:
        return bool(type(ep) is int and ep == 1 and get_material(db, sid, ep, verify_media=False, connection=connection))
    except (ValueError, OSError, KeyError, sqlite3.Error):
        return False


def revision(db, sid, ep, *, connection=None):
    material = get_material(db, sid, ep, verify_media=False, connection=connection)
    return material['revision'] if material else None
