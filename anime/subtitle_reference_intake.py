"""Exact, same-owner reference acquisition evidence; never submit a download.

An immutable receipt permits one candidate, one physical file and one external
episode number. It does not edit the saved show mapping or qualify a base.
"""
import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import time
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup
import feedparser

from .fidelity import fidelity_blocker
from .releases import Rejected, bdecode, season_of, select_files, validate_title
from .subtitle_chinese import digest
from .subtitle_chinese_intake import _TransactionStore, _ref, _stat
from .subtitle_targets import exists, head
from .viewing_workflow import (enabled, production_wanted, require_reference_slot,
                               scope_blocker, unreleased_date)

SCHEMA = 'subtitle-reference-candidate-request/v1'
TABLE = 'subtitle_reference_intakes'
KEY = 'subtitle_reference_binding'
MAX_AGE = 6 * 3600


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _metadata(candidate):
    value = candidate.get('source_metadata') or {}
    try:
        value = json.loads(value) if isinstance(value, str) else value
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def reference_only(candidate, db=None):
    meta = _metadata(candidate)
    if meta.get('reference_only') or KEY in meta or meta.get('reference_binding_conflict'):
        return True
    # Durable registrations keep their restriction even if a stale caller or a
    # malformed metadata refresh omits the marker.
    return bool(db is not None and exists(db, TABLE) and db.one(
        'SELECT 1 FROM ' + TABLE + ' WHERE candidate_id=? OR infohash=?',
        (candidate.get('id'), candidate.get('hash'))))


def reference_hash(db, infohash):
    return bool(exists(db, TABLE) and db.one('SELECT 1 FROM ' + TABLE + ' WHERE infohash=?', (infohash,)))


def reference_mapping(mapping, candidate):
    """Fail closed on a binding marker; ordinary mappings remain unchanged."""
    meta = _metadata(candidate)
    binding = meta.get(KEY) or {}
    identity = binding.get('candidate') or {}
    file = binding.get('file') or {}
    if (meta.get('reference_only') is not True or meta.get('reference_binding_conflict')
            or binding.get('schema') != 'subtitle-reference-binding/v1'
            or binding.get('permitted_role') != 'subtitle_reference'
            or not binding.get('id') or not re.fullmatch('[a-f0-9]{64}', str(binding.get('request_sha256', '')))
            or any(candidate.get(k) != identity.get(k) for k in
                   ('id', 'show_id', 'source_id', 'title', 'url', 'detail', 'hash'))
            or not re.fullmatch('[a-f0-9]{40}', str(binding.get('infohash', '')))
            or candidate.get('hash') != binding.get('infohash')
            or meta.get('expected_hash') != binding.get('infohash')
            or digest(canonical(mapping).encode()) != binding.get('saved_mapping_sha256')
            or mapping.get('require_season') is not True
            or binding.get('season') != mapping.get('season', 1)
            or any(type(binding.get(k)) is not int or binding[k] < 1 for k in ('season', 'episode', 'external_episode'))
            or not mapping.get('start', 1) <= binding['episode'] <= mapping.get('end', 999)
            or season_of(candidate.get('title', '')) != binding['season']
            or season_of(file.get('name', '')) != binding['season']
            or file.get('index') != 0 or type(file.get('size')) is not int or file['size'] <= 0
            or PurePosixPath(file.get('name', '')).suffix.lower() != '.mkv'):
        raise Rejected('Exact subtitle-reference binding changed or conflicts with the original mapping')
    # Only this complete physical filename is an additional identity token.
    return {**mapping, 'aliases': list(mapping.get('aliases', [])) + [file['name']],
            'numbering': [{'min': binding['external_episode'], 'max': binding['external_episode'],
                           'offset': binding['external_episode'] - binding['episode']}]}


def _https(url, host=None):
    p = urlparse(str(url))
    if (p.scheme != 'https' or not p.hostname or p.username or p.password
            or p.port not in (None, 443) or (host and p.hostname != host)):
        raise ValueError('Exact public HTTPS provenance is required')
    return p


def _same_url(left, right):
    a, b = urlparse(left), urlparse(right)
    return (a.scheme, a.netloc, unquote(a.path), parse_qs(a.query)) == (b.scheme, b.netloc, unquote(b.path), parse_qs(b.query))


def _load(root, request_path, now):
    path = Path(request_path).resolve()
    refs = {}
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError('Reference request must be a bounded private JSON file')
    _, raw = _ref(root, {'path': str(path), 'sha256': digest(path.read_bytes())}, refs)
    request = json.loads(raw.decode('utf-8-sig'))
    if (not isinstance(request, dict) or request.get('schema') != SCHEMA
            or any(type(request.get(k)) is not int or request[k] < 1 for k in
                   ('show_id', 'season', 'episode', 'external_episode', 'mikan_id', 'bangumi_episode_id'))
            or not request.get('expected_owner')
            or not re.fullmatch('[a-f0-9]{64}', str(request.get('expected_input', '')))
            or not re.fullmatch('[a-f0-9]{64}', str(request.get('saved_mapping_sha256', '')))):
        raise ValueError('Exact owner, input, season, local/external episode and source identities required')
    evidence, content = request['evidence'], {}
    for name, ref in evidence.items():
        _, content[name] = _ref(root, ref, refs)
    required = {'mikan_work', 'publisher', 'rss', 'torrent', 'nfo_html', 'nfo_text',
                'abema', 'bangumi', 'jimaku_page', 'japanese_srt', 'japanese_fetch', 'fetches'}
    if set(content) != required:
        raise ValueError('Complete independently archived source, episode, track and fetch evidence required')
    fetches = json.loads(content['fetches'].decode('utf-8-sig'))
    for name in required - {'fetches', 'japanese_srt', 'japanese_fetch'}:
        ref = evidence[name]
        _https(ref.get('url'))
        matches = [f for f in fetches if f.get('status') == 200 and f.get('url') == ref['url']
                   and Path(f.get('path', '')).resolve() == Path(ref['path']).resolve()
                   and f.get('sha256') == ref['sha256'] and f.get('bytes') == len(content[name])]
        if len(matches) != 1:
            raise ValueError('Every current web artifact needs its exact successful fetch receipt')
        stamp = datetime.datetime.fromisoformat(matches[0]['checked_at'])
        if stamp.tzinfo is None or not -60 <= now - stamp.timestamp() <= MAX_AGE:
            raise ValueError('Source identity evidence expired; collect a fresh bounded proof')
    h, files = bdecode(content['torrent'])
    if (request.get('expected_hash') != h or len(files) != 1 or files[0]['index'] != 0
            or files != request.get('files') or PurePosixPath(files[0]['name']).suffix.lower() != '.mkv'
            or files[0]['size'] <= 0 or season_of(files[0]['name']) != request['season']):
        raise ValueError('Real torrent must identify exactly the declared seasonal single MKV')
    title, detail, url = (request['candidate'][k] for k in ('title', 'detail', 'url'))
    if (season_of(title) != request['season'] or 'ABEMA' not in title or 'ABEMA' not in files[0]['name']
            or detail != evidence['publisher']['url'] or url != evidence['torrent']['url']
            or detail != 'https://mikanani.me/Home/Episode/' + h
            or not re.fullmatch(r'https://mikanani\.me/Download/\d{8}/' + h + r'\.torrent', url)):
        raise ValueError('Exact publisher title, explicit season, ABEMA declaration and torrent URLs required')
    work_url = 'https://mikanani.me/Home/Bangumi/' + str(request['mikan_id'])
    if evidence['mikan_work']['url'] != work_url:
        raise ValueError('Mikan work page differs from its declared subject')
    work = BeautifulSoup(content['mikan_work'], 'html.parser')
    publisher = BeautifulSoup(content['publisher'], 'html.parser')
    subject = 'https://bgm.tv/subject/' + str(request['show_id'])
    if (not work.find('a', href=subject) or not publisher.find('a', href=subject)
            or not any(_same_url(urljoin(detail, a.get('href', '')), work_url) for a in publisher.find_all('a', href=True))
            or not any(_same_url(urljoin(detail, a.get('href', '')), url) for a in publisher.find_all('a', href=True))
            or not publisher.find('p', string=title)):
        raise ValueError('Actual Mikan work/release pages do not prove the same subject and exact release')
    rss_url = request['source']['url']
    parsed = _https(rss_url, 'mikanani.me')
    if (request['source'].get('kind') != 'mikan' or request['source'].get('archive_url') != work_url
            or parsed.path != '/RSS/Bangumi' or parse_qs(parsed.query).get('bangumiId') != [str(request['mikan_id'])]
            or evidence['rss']['url'] != rss_url
            or not any(_same_url(urljoin(detail, a.get('href', '')), rss_url) for a in publisher.find_all('a', href=True))):
        raise ValueError('Only the exact same-subject publisher-linked Mikan RSS source is supported')
    entries = feedparser.parse(content['rss']).entries
    entries = [e for e in entries if e.get('link') == detail]
    if (len(entries) != 1 or entries[0].get('title') != title
            or not any(x.get('href') == url for x in entries[0].get('enclosures', []))):
        raise ValueError('Actual source RSS does not contain this exact title/page/torrent')
    episode = json.loads(content['bangumi'].decode('utf-8-sig'))
    if (evidence['bangumi']['url'] != 'https://api.bgm.tv/v0/episodes/' + str(request['bangumi_episode_id'])
            or any(episode.get(k) != request[v] for k, v in
                   (('id', 'bangumi_episode_id'), ('subject_id', 'show_id'), ('ep', 'episode'), ('sort', 'external_episode')))
            or not episode.get('name') or not episode.get('airdate')):
        raise ValueError('Independent catalogue local/external episode and title evidence differs')
    abema = BeautifulSoup(content['abema'], 'html.parser').get_text(' ', strip=True)
    if (not re.fullmatch(r'https://abema\.tv/video/episode/\d+-\d+_s' + str(request['season'])
                         + '_p' + str(request['external_episode']), evidence['abema']['url'])
            or episode['name'] not in abema or '第' + str(request['external_episode']) + '話' not in abema
            or '第' + str(request['season']) + '期' not in abema):
        raise ValueError('Official ABEMA season, numbered episode and exact catalogue title are required')
    nfo_url = evidence['nfo_html']['url']
    if not any(a.get_text(' ', strip=True).casefold() == 'mediainfo'
               and urljoin(detail, a.get('href', '')) == nfo_url for a in publisher.find_all('a', href=True)):
        raise ValueError('MediaInfo must be linked from the actual release page')
    nfo = BeautifulSoup(content['nfo_html'], 'html.parser')
    pre = nfo.find('pre', attrs={'data-media-info-text': True})
    # The public HTML renderer obfuscates strings containing @ (including the
    # AVC profile Main@L3.1). Restore only its explicit reversible text markers.
    if pre:
        for node in pre.select('[data-cfemail]'):
            encoded = bytes.fromhex(node['data-cfemail'])
            if not 1 < len(encoded) < 512:
                raise ValueError('Invalid publisher text marker')
            node.replace_with(bytes(x ^ encoded[0] for x in encoded[1:]).decode('utf-8'))
    text = content['nfo_text'].decode('utf-8-sig').replace('\r\n', '\n').strip()
    if (not pre or pre.get_text().replace('\r\n', '\n').strip() != text
            or not any(_same_url(urljoin(nfo_url, a.get('href', '')), evidence['nfo_text']['url'])
                       and a.get_text(' ', strip=True) == 'Raw' for a in nfo.find_all('a', href=True))):
        raise ValueError('Actual linked NFO HTML and raw declaration differ')
    fields = lambda s: {k.strip(): v.strip() for k, v in re.findall(r'^([^:\r\n]+?)\s+:\s*(.*?)\s*$', s, re.M)}
    general = fields(re.split(r'^Video\s*$', text, maxsplit=1, flags=re.M)[0])
    tracks = [fields(s) for s in re.split(r'^Text(?: #\d+)?\s*$', text, flags=re.M)[1:]]
    if (general.get('Complete name') != files[0]['name'] or general.get('Format') != 'Matroska'
            or len(tracks) != 1 or tracks[0].get('Format') != 'ASS'
            or tracks[0].get('Codec ID') != 'S_TEXT/ASS' or tracks[0].get('Language') != 'Japanese'
            or tracks[0].get('Forced') != 'No'):
        raise ValueError('Exact physical member and a separately declared Japanese ASS stream required')
    srt_url = request['japanese_source_url']
    _https(srt_url, 'jimaku.cc')
    jp = BeautifulSoup(content['jimaku_page'], 'html.parser')
    if (not re.fullmatch(r'https://jimaku\.cc/entry/\d+', evidence['jimaku_page']['url'])
            or not srt_url.startswith(evidence['jimaku_page']['url'] + '/download/')
            or not jp.find('a', href=urlparse(srt_url).path)
            or not re.search(r'\.S' + f"{request['season']:02}" + 'E' + str(request['external_episode']) + r'\.', unquote(srt_url))
            or episode['name'] not in unquote(srt_url) or '.ABEMA.ja[cc].srt' not in unquote(srt_url)):
        raise ValueError('Current Jimaku listing must identify the same ABEMA numbered episode and exact title')
    original_fetch = json.loads(content['japanese_fetch'].decode('utf-8-sig'))
    jp_ref = evidence['japanese_srt']
    if not any(f.get('url') == srt_url and f.get('sha256') == jp_ref['sha256']
               and Path(f.get('path', '')).resolve() == Path(jp_ref['path']).resolve()
               and f.get('bytes') == len(content['japanese_srt']) for f in original_fetch):
        raise ValueError('Original Japanese source bytes must retain their actual acquisition provenance')
    count = len(re.findall(r'(?m)^\d+\r?\n\d\d:\d\d:\d\d,\d{3} --> ', content['japanese_srt'].decode('utf-8-sig')))
    if count < 1 or tracks[0].get('Count of elements') != str(count):
        raise ValueError('Declared Japanese track and actual archived source cue counts differ')
    return {'request': request, 'raw': raw, 'refs': refs, 'content': content,
            'id': 'reference-intake-' + digest(raw), 'infohash': h, 'files': files,
            'cue_count': count, 'episode': episode}


def _guard(db, data, now):
    r = data['request']; sid, ep = r['show_id'], r['episode']
    show, mapping = db.show(sid), db.mapping(sid)
    if (not show or not enabled(db) or not production_wanted(db, sid)
            or not db.get('subtitle_studio_enabled', False) or scope_blocker(db, sid, ep)
            or mapping.get('season', 1) != r['season'] or mapping.get('require_season') is not True
            or digest(canonical(mapping).encode()) != r['saved_mapping_sha256']
            or db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1', (sid, ep))):
        raise ValueError('Original independently authorized, unwatched seasonal scope changed')
    local = [a for a in db.local_airings(sid) if a['episode'] == ep]
    if (len(local) != 1 or local[0].get('airdate') != data['episode']['airdate']
            or unreleased_date(db, sid, ep, now)
            or datetime.date.fromisoformat(local[0]['airdate']) > datetime.datetime.fromtimestamp(now).date()):
        raise ValueError('Exact episode must have independently confirmed, already aired local metadata')
    jobs = db.rows('SELECT * FROM subtitle_delivery_jobs WHERE show_id=? AND episode=?', (sid, ep))
    if len(jobs) != 1:
        raise ValueError('Unique original episode owner required')
    job = jobs[0]; payload = json.loads(job['payload'])
    if (job['id'] != f'episode:{sid}:{ep}' or job['kind'] != 'episode' or job['state'] != 'blocked'
            or job['dispatch_state'] != 'blocked-stopped' or job['thread_id'] != r['expected_owner']
            or job['input_key'] != r['expected_input'] or job.get('lease_expires', 0) > now
            or job.get('accepted_build_id') or payload.get('owner_stopped_writing') is not True
            or payload.get('user_stopped') or payload.get('finished') or payload.get('recovery_class') != 'repair_blocked'
            or db.get(f'subtitle_delivery_finished_{sid}_{ep}')):
        raise ValueError('Original stopped owner, input, lease, unknown receipt or delivery is protected')
    if exists(db, 'subtitle_viewing_leases') and db.one(
            'SELECT 1 FROM subtitle_viewing_leases WHERE show_id=? AND episode=? AND expires>?', (sid, ep, now)):
        raise ValueError('Active viewing lease is protected')
    if exists(db, 'subtitle_studio_jobs') and db.one(
            "SELECT 1 FROM subtitle_studio_jobs WHERE show_id=? AND episode=? AND state IN ('production','finished','accepted')", (sid, ep)):
        raise ValueError('Existing studio writer or completion is protected')
    target = r['target']
    assessment = payload.get('assessment') or {}
    fixed = [x.get('target') for x in assessment.get('material_refs', []) if x.get('kind') == 'completed_media']
    saved = db.get(f'subtitle_delivery_readiness_{sid}_{ep}', {}).get('target')
    current_head = head(db, sid, ep)
    compare = lambda x: isinstance(x, dict) and all(x.get(k) == target.get(k) for k in ('path', 'size', 'fingerprint', 'task_hash'))
    if (len(fixed) != 1 or not compare(fixed[0]) or not compare(saved)
            or assessment.get('target_fingerprint') != target.get('fingerprint')
            or assessment.get('input_signature') != r['expected_input']
            or current_head and not compare(current_head['payload'].get('target'))):
        raise ValueError('Fixed production mother or assessment differs')
    path = Path(target['path']).resolve()
    task = db.one('SELECT * FROM tasks WHERE hash=? AND show_id=?', (target.get('task_hash'), sid))
    if not task or task['status'] not in ('complete', 'linked') or not task.get('acknowledged'):
        raise ValueError('Fixed mother must retain its completed acknowledged original task')
    from .download_receipts import pending_receipt
    queue = db.one('SELECT * FROM thunder_queue WHERE hash=?', (task['hash'],))
    if pending_receipt(task, queue) or db.get('thunder_dialog_hold_' + task['hash'], {}).get('unconfirmed'):
        raise ValueError('Original mother downloader receipt remains unknown or leased')
    for other in db.rows("SELECT * FROM tasks WHERE show_id=? AND status IN ('intent','uncertain')", (sid,)):
        if str(ep) in json.loads(other['selection'] or '{}').get('selected', {}):
            raise ValueError('Another original episode download intent/unknown receipt is protected')
    selected = json.loads(task['selection']).get('selected', {}).get(str(ep), {})
    from .subtitle_lab import lab_core
    if (not path.is_file() or path.stat().st_size != target['size']
            or (Path(task['save_path']) / selected.get('name', '')).resolve() != path
            or selected.get('size') != target['size']
            or not re.search(r'(?i)(?:\b|[._ -])CR(?:\b|[._ -])', task.get('title', ''))
            or lab_core().fingerprint(str(path), target['size']) != target['fingerprint']):
        raise ValueError('Actual fixed CR mother identity changed')
    require_reference_slot(db, sid, ep)
    used = db.one('SELECT coalesce(sum(size),0) n FROM subtitle_samples WHERE show_id=? AND episode=?', (sid, ep))['n']
    # Same existing backend staging volume; no directory is created here.
    from .download_storage import download_roots
    stage = download_roots()[1] if Path('D:/').exists() else db.path.parent
    if (used + data['files'][0]['size'] > 20 * 1024**3
            or shutil.disk_usage(stage.anchor or stage).free < data['files'][0]['size'] + db.get('minimum_free_gb', 10) * 1024**3):
        raise ValueError('Reference budget or original staging-volume free space is insufficient')
    from .download_policy import require_submission_ready
    require_submission_ready(db)
    source = db.one('SELECT * FROM sources WHERE url=?', (r['source']['url'],))
    if source and (source['show_id'] != sid or source['kind'] != 'mikan' or source['enabled'] != 1
                   or source['archive_url'] != r['source']['archive_url']):
        raise ValueError('Original source is disabled, conflicting or owned by another subject')
    selection = {'files': data['files'], 'ids': [0], 'selected': {str(ep): data['files'][0]}}
    blocked = fidelity_blocker(db, r['candidate']['title'], selection)
    if blocked:
        raise ValueError(blocked)
    proposed = {'id': 0, 'source_id': source['id'] if source else 0, 'show_id': sid,
                **r['candidate'], 'hash': data['infohash']}
    proposed['source_metadata'] = {'reference_only': True, 'expected_hash': data['infohash'], KEY: _binding(data, proposed)}
    scoped = reference_mapping(mapping, proposed)
    eps = validate_title(proposed['title'], scoped, {}, False)
    selected_file, ids = select_files(data['files'], scoped, [ep], eps)
    if eps != [ep] or ids != [0] or selected_file != {ep: data['files'][0]}:
        raise ValueError('Exact candidate title and real physical file do not select only this episode')
    return source, job, selection


def _binding(data, candidate):
    r = data['request']
    return {'schema': 'subtitle-reference-binding/v1', 'id': data['id'], 'request_sha256': digest(data['raw']),
            'permitted_role': 'subtitle_reference', 'candidate': {k: candidate[k] for k in
                ('id', 'show_id', 'source_id', 'title', 'url', 'detail', 'hash')},
            'infohash': data['infohash'], 'file': data['files'][0], 'season': r['season'],
            'episode': r['episode'], 'external_episode': r['external_episode'],
            'saved_mapping_sha256': r['saved_mapping_sha256']}


def _registered(db, identity):
    return db.one('SELECT * FROM ' + TABLE + ' WHERE id=?', (identity,)) if exists(db, TABLE) else None


def _check_receipts(db, data, receipt=None):
    h = data['infohash']
    candidates = db.rows('SELECT * FROM candidates WHERE hash=? OR url=?', (h, data['request']['candidate']['url']))
    if candidates and (not receipt or len(candidates) != 1 or candidates[0]['id'] != receipt['candidate_id']):
        raise ValueError('Existing candidate/hash ownership must retain its original controlled flow')
    if receipt:
        candidate = db.one('SELECT * FROM candidates WHERE id=?', (receipt['candidate_id'],))
        if not candidate:
            raise ValueError('Registered reference candidate disappeared')
        binding = _binding(data, candidate)
        if (_metadata(candidate).get(KEY) != binding or _metadata(candidate).get('reference_binding_conflict')
                or not reference_only(candidate)):
            raise ValueError('Registered candidate evidence was changed')
        reference_mapping(db.mapping(candidate['show_id']), candidate)
        root = (db.path.parent / 'subtitle-studio').resolve()
        for proof in json.loads(receipt['payload'])['archived_proofs']:
            path = Path(proof['path']).resolve()
            if not path.is_relative_to(root) or not path.is_file() or digest(path.read_bytes()) != proof['sha256']:
                raise ValueError('Immutable reference proof changed')
    for table in ('tasks', 'thunder_queue'):
        rows = db.rows('SELECT * FROM ' + table + ' WHERE hash=?', (h,))
        if rows:
            if table != 'tasks' or not receipt or reference_task_blocker(db, rows[0]) or rows[0]['status'] == 'uncertain':
                raise ValueError('Existing or unknown downloader receipt is protected')
    if db.get('thunder_dialog_hold_' + h):
        raise ValueError('Unknown downloader dialog receipt remains protected')
    for table in ('source_samples', 'subtitle_samples'):
        if exists(db, table):
            rows = db.rows('SELECT * FROM ' + table + ' WHERE hash=?', (h,))
            if rows and (not receipt or table != 'subtitle_samples' or len(rows) != 1
                         or rows[0]['candidate_id'] != receipt['candidate_id']
                         or json.loads(rows[0]['evidence']).get('workflow_role') != 'subtitle_reference'):
                raise ValueError('Existing sample ownership remains protected')


def register_reference_candidate(db, request_path, *, apply=False, now=None):
    now = time.time() if now is None else now
    root = (db.path.parent / 'subtitle-studio').resolve()
    data = _load(root, request_path, now)
    r = data['request']
    result = {'schema': 'subtitle-reference-candidate-receipt/v1', 'id': data['id'],
              **{k: r[k] for k in ('show_id', 'season', 'episode', 'external_episode', 'expected_owner', 'expected_input')},
              'infohash': data['infohash'], 'files': data['files'], 'permitted_role': 'subtitle_reference',
              'applied': False, 'idempotent': False, 'candidate_id': None, 'source_id': None,
              'download_submitted': False, 'owner_changed': False, 'target_changed': False,
              'saved_mapping_changed': False, 'ready': False, 'accepted': False,
              'same_japanese_text_verified': False, 'same_clock_verified': False}
    with db.gate, db.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        current = _TransactionStore(db, con)
        source, _, selection = _guard(current, data, now)
        prior = _registered(current, data['id'])
        _check_receipts(current, data, prior)
        for path, evidence in data['refs'].items():
            if _stat(Path(path)) != evidence['stat'] or digest(Path(path).read_bytes()) != evidence['sha256']:
                raise ValueError('Source proof changed before atomic registration')
        result['selection'] = selection
        if prior:
            result.update(candidate_id=prior['candidate_id'], source_id=prior['source_id'], idempotent=True, applied=apply)
        elif apply:
            proof_dir = root / 'reference-intake-proofs' / data['id']
            if not proof_dir.resolve().is_relative_to(root):
                raise ValueError('Immutable proof directory must remain inside the private archive')
            proof_dir.mkdir(parents=True, exist_ok=True)
            archived = []
            for original, evidence in data['refs'].items():
                dest = proof_dir / (evidence['sha256'] + '.bin')
                raw = Path(original).read_bytes()
                if digest(raw) != evidence['sha256']:
                    raise ValueError('Source proof changed while archiving')
                if dest.exists():
                    if digest(dest.read_bytes()) != evidence['sha256']:
                        raise ValueError('Immutable source proof conflict')
                else:
                    with dest.open('xb') as stream:
                        stream.write(raw)
                archived.append({'original_path': original, 'path': str(dest), 'sha256': evidence['sha256']})
            con.execute('''CREATE TABLE IF NOT EXISTS subtitle_reference_intakes(
                id TEXT PRIMARY KEY,candidate_id INTEGER NOT NULL UNIQUE,source_id INTEGER NOT NULL,
                show_id INTEGER NOT NULL,season INTEGER NOT NULL,episode INTEGER NOT NULL,
                infohash TEXT NOT NULL UNIQUE,source_exclusive INTEGER NOT NULL,
                request_sha256 TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL)''')
            source_exclusive = int(source is None)
            source_id = source['id'] if source else con.execute(
                'INSERT INTO sources(show_id,kind,url,archive_url,enabled) VALUES(?,?,?,?,1)',
                (r['show_id'], 'mikan', r['source']['url'], r['source']['archive_url'])).lastrowid
            candidate_id = con.execute('''INSERT INTO candidates(show_id,source_id,title,url,detail,hash,status,reason,discovered)
                VALUES(?,?,?,?,?,?,'discovered',?,?)''', (r['show_id'], source_id, r['candidate']['title'],
                r['candidate']['url'], r['candidate']['detail'], data['infohash'], '仅本集原负责人对轴参考，不作为母片或普通选源', now)).lastrowid
            candidate = dict(con.execute('SELECT * FROM candidates WHERE id=?', (candidate_id,)).fetchone())
            binding = _binding(data, candidate)
            metadata = {'reference_only': True, 'expected_hash': data['infohash'], 'media_mode': 'soft',
                        'declaration_only': True, KEY: binding}
            reference_mapping(current.mapping(r['show_id']), candidate | {'source_metadata': metadata})
            con.execute('UPDATE candidates SET source_metadata=? WHERE id=?', (canonical(metadata), candidate_id))
            payload = {'request': r, 'binding': binding, 'archived_proofs': archived,
                       'cue_count_corroboration_only': data['cue_count'], 'selection': selection}
            con.execute('INSERT INTO ' + TABLE + ' VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (data['id'], candidate_id, source_id, r['show_id'], r['season'], r['episode'], data['infohash'],
                 source_exclusive, digest(data['raw']), canonical(payload), now))
            result.update(applied=True, candidate_id=candidate_id, source_id=source_id,
                          immutable_proof_directory=str(proof_dir))
        else:
            result['source_id'] = source['id'] if source else None
        if not apply:
            con.rollback()
    return result


def require_reference_candidate(db, candidate, role, files=None, *, now=None):
    if not reference_only(candidate, db):
        return
    if role != 'subtitle_reference':
        raise Rejected('This candidate is restricted to the original episode subtitle-reference queue')
    current = db.one('SELECT * FROM candidates WHERE id=?', (candidate.get('id'),))
    if not current or any(current.get(k) != candidate.get(k) for k in
            ('id', 'show_id', 'source_id', 'title', 'url', 'detail', 'hash', 'source_metadata')):
        raise Rejected('Original reference candidate identity changed')
    mapping = reference_mapping(db.mapping(current['show_id']), current)
    binding = _metadata(current)[KEY]
    receipt = _registered(db, binding['id'])
    if not receipt or receipt['candidate_id'] != current['id']:
        raise Rejected('Reference binding has no controlled immutable registration')
    payload = json.loads(receipt['payload'])
    if payload['binding'] != binding:
        raise Rejected('Registered reference proof changed')
    now = time.time() if now is None else now
    # Re-read originals for freshness and hash validation before a new queue action.
    proof = next(x for x in payload['archived_proofs'] if x['sha256'] == receipt['request_sha256'])
    data = _load((db.path.parent / 'subtitle-studio').resolve(), proof['original_path'], now)
    if data['id'] != binding['id']:
        raise Rejected('Original reference request changed')
    source, _, _ = _guard(db, data, now)
    if not source or source['id'] != current['source_id']:
        raise Rejected('Original same-subject source is disabled or changed')
    _check_receipts(db, data, receipt)
    if files is not None and files != data['files']:
        raise Rejected('Actual reference torrent manifest changed')
    if validate_title(current['title'], mapping, {}, False) != [binding['episode']]:
        raise Rejected('Reference title is outside its exact local episode')


def preserve_reference_binding(con, source_id, item, incoming):
    """An exclusive reference feed cannot create unrelated ordinary candidates."""
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return incoming
    receipts = [dict(x) for x in con.execute('SELECT * FROM ' + TABLE + ' WHERE source_id=?', (source_id,))]
    match = next((x for x in receipts if json.loads(x['payload'])['binding']['candidate']['url'] == item['url']), None)
    if not match:
        return None if any(x['source_exclusive'] for x in receipts) else incoming
    saved = json.loads(match['payload'])['binding']
    row = con.execute('SELECT * FROM candidates WHERE id=?', (match['candidate_id'],)).fetchone()
    if not row:
        raise ValueError('Registered reference candidate disappeared')
    old = _metadata(dict(row)); merged = {**old, **incoming}
    conflict = (old.get('reference_binding_conflict') or any(item.get(k, '') != saved['candidate'][k] for k in ('title', 'url', 'detail'))
                or incoming.get('expected_hash') not in (None, '', saved['infohash'])
                or incoming.get('media_mode') in ('hard', 'raw') or incoming.get(KEY) not in (None, saved))
    merged.update(reference_only=True, expected_hash=saved['infohash'], **{KEY: saved})
    if conflict:
        merged['reference_binding_conflict'] = True
    else:
        merged.update(media_mode='soft', declaration_only=True)
    return merged


def reference_task_blocker(db, task):
    """A durable reference selection can never be replayed as ordinary media."""
    if not exists(db, TABLE):
        return ''
    receipt = db.one('SELECT * FROM ' + TABLE + ' WHERE infohash=?', (task.get('hash'),))
    if not receipt:
        return ''
    payload = json.loads(receipt['payload'])
    sample = db.one('SELECT * FROM subtitle_samples WHERE hash=? AND candidate_id=?',
                    (task.get('hash'), receipt['candidate_id']))
    try:
        if (task.get('purpose') != 'subtitle_sample' or task.get('show_id') != receipt['show_id']
                or task.get('title') != payload['binding']['candidate']['title'] or not sample
                or sample['show_id'] != receipt['show_id'] or sample['episode'] != receipt['episode']
                or json.loads(sample['evidence']).get('workflow_role') != 'subtitle_reference'
                or json.loads(task['selection']) != payload['selection']):
            return 'Registered reference hash is restricted to its exact subtitle-reference task and selection'
    except (KeyError, ValueError, TypeError):
        return 'Registered reference task identity cannot be verified'
    return ''
