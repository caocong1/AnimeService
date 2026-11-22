"""Register exact publisher soft-track declarations as acquisition hints only.

No Engine, downloader, target, production owner or clean-picture review is
created. The explicit apply path changes one candidate's metadata and appends
its immutable declaration receipt in the same guarded transaction.
"""
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import time
from urllib.parse import urljoin, urlparse

from .fidelity import fidelity_blocker
from .native_indexes import candidate_mapping, media_mode
from .releases import bdecode, select_files, validate_title
from .subtitle_chinese import digest
from .subtitle_chinese_intake import _TransactionStore, _guard, _ref, _stat
from .subtitle_targets import exists

SCHEMA = 'subtitle-acquisition-declaration-request/v1'
TABLE = 'subtitle_acquisition_declarations'


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.pre, self._anchor, self._inside = [], [], None, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a':
            self._anchor = [attrs.get('href', ''), []]
        if tag == 'pre' and 'data-media-info-text' in attrs:
            self._inside = True

    def handle_data(self, data):
        if self._anchor is not None:
            self._anchor[1].append(data)
        if self._inside:
            self.pre.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self._anchor is not None:
            self.links.append((self._anchor[0], ''.join(self._anchor[1]).strip()))
            self._anchor = None
        if tag == 'pre':
            self._inside = False


def _fields(text):
    return {k.strip(): v.strip() for k, v in re.findall(r'^([^:\r\n]+?)\s+:\s*(.*?)\s*$', text, re.M)}


def _file_records(files):
    return [{k: row[k] for k in ('index', 'name', 'size')} | {'padding': bool(row.get('padding'))} for row in files]


def _load(root, request_path):
    path = Path(request_path).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('Declaration request must be a bounded private JSON file')
    refs = {}
    _, raw = _ref(root, {'path': str(path), 'sha256': digest(path.read_bytes())}, refs)
    request = json.loads(raw.decode('utf-8-sig'))
    if (not isinstance(request, dict) or request.get('schema') != SCHEMA
            or any(type(request.get(k)) is not int or request[k] < 1
                   for k in ('candidate_id', 'show_id', 'season', 'episode'))
            or not isinstance(request.get('expected_input'), str) or not request['expected_input'].strip()
            or not re.fullmatch(r'[a-f0-9]{64}', str(request.get('expected_metadata_sha256', '')))):
        raise ValueError('Exact candidate, seasonal episode, input and old metadata SHA are required')
    _, report_raw = _ref(root, request['report'], refs)
    report = json.loads(report_raw.decode('utf-8-sig'))
    if (not isinstance(report, dict) or report.get('schema') != 'subtitle-clean-acquisition-evidence/v1'
            or (report.get('recommended_existing_candidate_id'), report.get('show_id'), report.get('season'), report.get('episode'))
               != tuple(request[k] for k in ('candidate_id', 'show_id', 'season', 'episode'))
            or report.get('declaration_only') is not True or report.get('clean_frames_verified') is not False):
        raise ValueError('Only exact declaration-only evidence, without a clean-frame claim, is supported')
    declaration = report['recommended_source_metadata_for_guarded_registration']
    torrent = report['actual_torrent_manifest']
    if (declaration.get('media_mode') != 'soft' or declaration.get('declaration_only') is not True
            or not declaration.get('declaration_basis')):
        raise ValueError('Explicit publisher soft-track declaration required')
    _, torrent_raw = _ref(root, torrent, refs)
    infohash, files = bdecode(torrent_raw, include_padding=True)
    if (any(torrent.get(k) != infohash for k in ('infohash', 'expected_hash'))
            or declaration.get('expected_hash') != infohash
            or report.get('existing_candidate_expected_hash') != infohash
            or _file_records(torrent['files']) != _file_records(files)):
        raise ValueError('Real torrent infohash or complete physical file manifest differs')
    page_ref, nfo_ref, extracted_ref = (declaration[k] for k in
        ('declaration_page', 'declaration_mediainfo', 'extracted_mediainfo'))
    _, page_raw = _ref(root, page_ref, refs)
    _, nfo_raw = _ref(root, nfo_ref, refs)
    _, extracted_raw = _ref(root, extracted_ref, refs)
    page, nfo = _Page(), _Page()
    page.feed(page_raw.decode('utf-8-sig'))
    nfo.feed(nfo_raw.decode('utf-8-sig'))
    nfo_url = request['mediainfo_url']
    parsed = urlparse(nfo_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443)
            or not any(label.casefold() == 'mediainfo' and urljoin(page_ref['url'], href) == nfo_url
                       for href, label in page.links)):
        raise ValueError('MediaInfo must be the exact publisher-linked HTTPS declaration')
    text = extracted_raw.decode('utf-8-sig').replace('\r\n', '\n').strip()
    if ''.join(nfo.pre).replace('\r\n', '\n').strip() != text:
        raise ValueError('Extracted MediaInfo differs from the actual publisher-linked HTML')
    general = _fields(re.split(r'^Video(?: #\d+)?\s*$', text, maxsplit=1, flags=re.M)[0])
    tracks = []
    for block in re.split(r'^Text(?: #\d+)?\s*$', text, flags=re.M)[1:]:
        fields = _fields(re.split(r'^(?:Menu|Video|Audio|Image)(?: #\d+)?\s*$', block, maxsplit=1, flags=re.M)[0])
        if fields.get('Format') != 'ASS' or fields.get('Codec ID') != 'S_TEXT/ASS' or fields.get('Forced') != 'No':
            raise ValueError('Every declared text track must be a separate, non-forced ASS track')
        tracks.append({k: fields.get(k) for k in ('ID', 'Format', 'Codec ID', 'Language', 'Forced')})
    if general.get('Format') != 'Matroska' or not general.get('Complete name') or not tracks:
        raise ValueError('Actual Matroska complete filename and independent ASS tracks required')
    if len({t['ID'] for t in tracks}) != len(tracks) or any(not t['ID'] for t in tracks):
        raise ValueError('Declared subtitle tracks need distinct real stream identifiers')
    data = {'request': request, 'request_raw': raw, 'report': report, 'report_raw': report_raw,
            'infohash': infohash, 'files': files, 'tracks': tracks, 'complete_name': general['Complete name'],
            'page_url': page_ref['url'], 'torrent_url': torrent['url'],
            'evidence': [request['report'], page_ref, nfo_ref, extracted_ref, torrent], 'refs': refs}
    data['id'] = 'acquisition-declaration-' + digest(raw)
    return data


def _existing(db, identity):
    return db.one('SELECT * FROM ' + TABLE + ' WHERE id=?', (identity,)) if exists(db, TABLE) else None


def _checked(db, data):
    request = data['request']
    sid, season, ep = (request[k] for k in ('show_id', 'season', 'episode'))
    _guard(db, sid, season, ep, request['expected_input'])
    candidate = db.one('SELECT * FROM candidates WHERE id=?', (request['candidate_id'],))
    saved = request['candidate']
    if (not candidate or candidate['show_id'] != sid
            or any(candidate.get(k) != saved.get(k) for k in ('title', 'url', 'detail', 'source_id', 'hash'))
            or candidate['status'] not in ('discovered', 'source_wait', 'review', 'rejected', 'retry')):
        raise ValueError('Original candidate identity, status or ownership changed')
    source = db.one('SELECT * FROM sources WHERE id=?', (candidate['source_id'],))
    if (not source or source['show_id'] != sid or source['enabled'] != 1
            or any(source.get(k) != request['source'].get(k) for k in ('id', 'kind', 'url'))):
        raise ValueError('Original source is disabled, changed or belongs to another show')
    if (candidate['url'] != data['torrent_url'] or candidate['detail'] != data['page_url']
            or not re.search(r'(?i)/' + data['infohash'] + r'\.torrent(?:$|\?)', candidate['url'])
            or not re.search(r'(?i)/' + data['infohash'] + r'(?:$|\?)', candidate['detail'])):
        raise ValueError('Publisher page and torrent must identify the exact existing candidate hash')
    if candidate['hash'] not in (None, '', data['infohash']):
        raise ValueError('Candidate already has a different torrent identity')
    metadata = json.loads(candidate['source_metadata'] or '{}')
    if (not isinstance(metadata, dict) or metadata.get('acquisition_declaration_conflict')
            or media_mode(candidate) not in ('unknown', 'soft')):
        raise ValueError('Conflicting existing media-mode evidence is protected')
    if metadata.get('expected_hash') not in (None, '', data['infohash']):
        raise ValueError('Existing declared source hash conflicts with the real torrent')
    prior = _existing(db, data['id'])
    recorded = metadata.get('acquisition_declaration') or {}
    replay = bool(prior and recorded.get('id') == data['id']
                  and metadata.get('media_mode') == 'soft' and metadata.get('declaration_only') is True
                  and metadata.get('expected_hash') == data['infohash'])
    if digest(candidate['source_metadata'].encode('utf-8')) != request['expected_metadata_sha256'] and not replay:
        raise ValueError('Candidate metadata changed; preserve and inspect the new evidence first')
    eps = validate_title(candidate['title'], candidate_mapping(db.mapping(sid), candidate), {}, False)
    if eps != [ep]:
        raise ValueError('Candidate title does not uniquely match this authorized episode')
    selected, ids = select_files(data['files'], candidate_mapping(db.mapping(sid), candidate), wanted=[ep], title_eps=eps)
    if set(selected) != {ep} or len(ids) != 1:
        raise ValueError('Actual torrent must select exactly this episode and one physical video')
    filename = PurePosixPath(selected[ep]['name'].replace('\\', '/')).name
    if filename != data['complete_name']:
        raise ValueError('Publisher MediaInfo describes a different physical torrent member')
    selection = {'files': data['files'], 'ids': ids, 'selected': {str(ep): selected[ep]}}
    blocked = fidelity_blocker(db, candidate['title'], selection)
    if blocked:
        raise ValueError(blocked)
    for table in ('tasks', 'thunder_queue'):
        if db.one('SELECT 1 FROM ' + table + ' WHERE hash=?', (data['infohash'],)):
            raise ValueError('Existing task or downloader receipt must use its original controlled flow')
    if db.get('thunder_dialog_hold_' + data['infohash']):
        raise ValueError('Unknown original downloader dialog receipt remains protected')
    for table in ('subtitle_samples', 'source_samples'):
        if exists(db, table) and db.one('SELECT 1 FROM ' + table + ' WHERE candidate_id=? OR hash=?',
                                      (candidate['id'], data['infohash'])):
            raise ValueError('Existing sample ownership or receipt remains protected')
    return candidate, metadata, selection, replay


def register_acquisition_declaration(db, request_path, *, apply=False):
    root = (db.path.parent / 'subtitle-studio').resolve()
    data = _load(root, request_path)
    request = data['request']
    with db.gate, db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        candidate, metadata, selection, replay = _checked(_TransactionStore(db, con), data)
        original = dict(candidate)
        con.rollback()
    pointer = {'id': data['id'], 'schema': 'subtitle-acquisition-declaration/v1',
               'request_sha256': digest(data['request_raw']), 'report': request['report'],
               'infohash': data['infohash'], 'candidate_id': candidate['id'], 'declaration_only': True}
    projected = {**metadata, 'media_mode': 'soft', 'declaration_only': True,
                 'expected_hash': data['infohash'], 'acquisition_declaration': pointer}
    result = {'schema': 'subtitle-acquisition-declaration-receipt/v1',
              **{k: request[k] for k in ('candidate_id', 'show_id', 'season', 'episode', 'expected_input')},
              'id': data['id'], 'infohash': data['infohash'], 'selection': selection,
              'media_mode': 'soft', 'declaration_only': True, 'declared_tracks': data['tracks'],
              'applied': False, 'idempotent': replay, 'metadata_changed': False,
              'projected_metadata': projected, 'clean_frames_verified': False,
              'ready': False, 'accepted': False, 'finished': False,
              'download_submitted': False, 'target_changed': False, 'owner_changed': False}
    with db.gate, db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        current, _, current_selection, current_replay = _checked(_TransactionStore(db, con), data)
        if current != original or current_selection != selection or current_replay != replay:
            raise ValueError('Source candidate changed during declaration verification')
        if any(_stat(Path(p)) != ref['stat'] for p, ref in data['refs'].items()):
            raise ValueError('Publisher declaration evidence changed before registration')
        if apply and not replay:
            con.execute('''CREATE TABLE IF NOT EXISTS subtitle_acquisition_declarations(
                id TEXT PRIMARY KEY,candidate_id INTEGER NOT NULL,show_id INTEGER NOT NULL,
                season INTEGER NOT NULL,episode INTEGER NOT NULL,source_id INTEGER NOT NULL,
                infohash TEXT NOT NULL,request_sha256 TEXT NOT NULL,metadata_before TEXT NOT NULL,
                metadata_after TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL)''')
            payload = {'schema': 'subtitle-acquisition-declaration/v1', 'request': request,
                       'report': data['report'], 'selection': selection, 'declared_tracks': data['tracks'],
                       'declaration_only': True, 'clean_frames_verified': False,
                       'evidence_sha256': {p: r['sha256'] for p, r in data['refs'].items()}}
            con.execute('INSERT INTO ' + TABLE + ' VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                        (data['id'], candidate['id'], request['show_id'], request['season'], request['episode'],
                         candidate['source_id'], data['infohash'], digest(data['request_raw']),
                         candidate['source_metadata'], _dump(projected), _dump(payload), time.time()))
            changed = con.execute('UPDATE candidates SET source_metadata=? WHERE id=? AND source_metadata=?',
                                  (_dump(projected), candidate['id'], candidate['source_metadata'])).rowcount
            if changed != 1:
                raise ValueError('Declaration metadata compare-and-swap failed')
            result.update(applied=True, metadata_changed=True)
        elif apply:
            result['applied'] = True
        else:
            con.rollback()
    return result


def preserve_acquisition_declaration(con, source_id, url, incoming):
    """Keep only registered evidence when a normal RSS poll has no declaration.

    Explicit conflicting feed mode/hash wins conservatively; the historical
    receipt remains visible and can never turn a hard declaration into soft.
    """
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return incoming
    row = con.execute('SELECT * FROM candidates WHERE source_id=? AND url=?', (source_id, url)).fetchone()
    if not row:
        return incoming
    candidate = dict(row)
    try:
        old = json.loads(candidate['source_metadata'] or '{}')
        pointer = old.get('acquisition_declaration') or {}
        receipt = con.execute('SELECT * FROM ' + TABLE + ' WHERE id=? AND candidate_id=? AND source_id=?',
                              (pointer.get('id'), candidate['id'], source_id)).fetchone()
        if not receipt:
            return incoming
        payload = json.loads(receipt['payload'])
        binding = payload['request']['candidate']
        if any(candidate.get(k) != binding.get(k) for k in ('title', 'url', 'detail', 'source_id')):
            return incoming
        result = {**old, **incoming}
        result['acquisition_declaration'] = pointer
        if old.get('acquisition_declaration_conflict'):
            # An empty later RSS poll cannot erase evidence of a conflicting
            # declaration. Keep it blocked for an explicit evidence review.
            result['media_mode'] = (incoming.get('media_mode') if incoming.get('media_mode') in ('hard', 'raw')
                                    else old.get('media_mode') if old.get('media_mode') in ('hard', 'raw')
                                    else 'unknown')
            result['acquisition_declaration_conflict'] = True
            return result
        if (incoming.get('media_mode') in ('hard', 'raw')
                or incoming.get('expected_hash') not in (None, '', receipt['infohash'])):
            result['media_mode'] = incoming.get('media_mode', 'unknown')
            result['acquisition_declaration_conflict'] = True
            return result
        result.update(media_mode='soft', declaration_only=True, expected_hash=receipt['infohash'])
        result.pop('acquisition_declaration_conflict', None)
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        return incoming
