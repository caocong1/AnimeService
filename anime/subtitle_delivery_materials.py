"""Read-only readiness adapter. Evidence availability is not episode acceptance."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .fidelity import fidelity_blocker
from .subtitle_targets import head, proof


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def sampled_frames_valid(root, refs):
    if not isinstance(refs, list) or len(refs) < 3:
        return False
    paths=set()
    for ref in refs:
        try:
            path=Path(ref['path']).resolve()
            if not path.is_relative_to(root.resolve()) or not path.is_file():
                return False
            raw=path.read_bytes()
            if (sha(raw)!=ref['sha256'] or len(raw)<33 or raw[:8]!=b'\x89PNG\r\n\x1a\n'
                    or int.from_bytes(raw[8:12],'big')!=13 or raw[12:16]!=b'IHDR'):
                return False
            width,height=int.from_bytes(raw[16:20],'big'),int.from_bytes(raw[20:24],'big')
            if not 0<width<=16384 or not 0<height<=16384:
                return False
            paths.add(path)
        except (OSError,TypeError,KeyError):
            return False
    # Distinct source frame files may legitimately have identical pixels during
    # a hold. This checks inputs, not their PTS or whole-episode cleanliness.
    return len(paths)>=3


def readiness(db, sid, ep, payload):
    from .subtitle_studio import lab_core, authored
    root = db.path.parent / 'subtitle-studio'
    payload = payload or {}
    core = lab_core()
    record = db.get(f'subtitle_delivery_readiness_{sid}_{ep}', {}) or {}
    first_release = (db.get('subtitle_delivery_policy', {}).get('first_release_mode') == 'finished'
                     and record.get('dispatch_standard') == 'finished_v1')
    frozen = head(db, sid, ep)
    # A reviewed completed research candidate may be prepared before freeze.
    # This is only a provisional input; it never replaces an ordinary episode.
    target = (frozen['payload']['target'] if frozen else record.get('target') or payload.get('target')) or {}
    reasons, refs, drafts, review_notes = [], [], [], []
    valid_media = False
    if target:
        try:
            path = Path(target['path']).resolve()
            if not path.is_file() or path.stat().st_size != target['size'] or core.fingerprint(path) != target['fingerprint']:
                raise ValueError('target_changed_or_missing')
            tasks = db.rows("SELECT * FROM tasks WHERE show_id=? AND status IN ('complete','linked')", (sid,))
            for task in tasks:
                selection = json.loads(task['selection'])
                selected = selection.get('selected', {}).get(str(ep))
                if not selected or selected.get('size') != target['size']:
                    continue
                expected = (Path(task['save_path']) / selected['name']).resolve()
                if expected != path or not expected.is_relative_to(Path(task['save_path']).resolve()):
                    continue
                if fidelity_blocker(db, task['title'], task['selection']):
                    continue
                # Only a previously reconciled project task can supply completion.
                if task.get('purpose') not in ('episode', 'source_sample', 'subtitle_sample'):
                    continue
                queue = db.one('SELECT lease,committing,state FROM thunder_queue WHERE hash=?', (task['hash'],))
                if queue and (queue['lease'] or queue['committing'] or queue['state'] in ('claimed', 'review')):
                    continue
                if db.get('thunder_dialog_hold_' + task['hash'], {}).get('unconfirmed'):
                    continue
                valid_media = True
                refs.append({'kind': 'completed_media', 'hash': task['hash'], 'target': target})
                break
            if not valid_media:
                reasons.append('needs_completed_manifest_and_ownership')
        except (ValueError, KeyError, TypeError, OSError):
            reasons.append('target_changed_or_missing')
    else:
        completed = db.rows("SELECT selection FROM tasks WHERE show_id=? AND status IN ('complete','linked')", (sid,))
        retained = False
        for task in completed:
            try:
                retained |= str(ep) in json.loads(task['selection']).get('selected', {})
            except (TypeError, ValueError):
                pass
        reasons.append('completed_media_needs_inventory_or_reconcile' if retained else 'needs_verified_completed_media')
    archive_db = root / 'archive.sqlite3'
    if archive_db.is_file():
        with sqlite3.connect(archive_db.resolve().as_uri() + '?mode=ro', uri=True) as con:
            con.row_factory = sqlite3.Row
            for row in con.execute('SELECT id,raw_hash,text_hash,language,metadata FROM drafts WHERE episode_key=? ORDER BY id',
                                   (core.episode_key(sid, db.mapping(sid).get('season', 1), ep),)):
                try:
                    meta = json.loads(row['metadata'])
                    raw_file = Path(meta['raw_file']).resolve()
                    if not raw_file.is_relative_to(root.resolve()) or sha(raw_file.read_bytes()) != row['raw_hash']:
                        raise ValueError('archive_hash_changed')
                    if fidelity_blocker(db, meta.get('release', '')):
                        continue
                    d = {k: row[k] for k in ('id', 'raw_hash', 'text_hash', 'language')}
                    d.update(estimated=bool(meta.get('timing_is_estimated')),
                             mixed=bool(meta.get('mixed_language_suspected')),
                             origin=meta.get('origin'), target_fingerprint=meta.get('media_fingerprint'))
                    drafts.append(d)
                except (ValueError, KeyError, TypeError, OSError):
                    reasons.append('archived_input_changed_or_invalid')
    zh = [d for d in drafts if d['language'] in core.ZH and not d['estimated'] and not d['mixed']]
    # Manual sign readings and song-only tracks are not a Japanese dialogue base.
    ja_candidates = [d for d in drafts if d['language'] in core.JA and not d['estimated']
          and d['origin'] not in ('manual-screen', 'manual-screen-text', 'screen_text', 'local-asr')]
    ja = []
    try:
        dialogue = proof(root, record.get('japanese_dialogue_report'))
        if (dialogue.get('status') == 'verified' and dialogue.get('role') == 'dialogue'
                and dialogue.get('coverage') == 'whole_episode_dialogue'
                and dialogue.get('show_id') == sid and dialogue.get('episode') == ep
                and dialogue.get('reviewer') and dialogue.get('evidence')):
            reviewed_raw = set(dialogue.get('source_raw_hashes') or [])
            ja = [d for d in ja_candidates if d['raw_hash'] in reviewed_raw]
            if ja:
                refs.append({'kind': 'japanese_dialogue_qualification', **record['japanese_dialogue_report']})
    except (ValueError, TypeError, OSError, KeyError):
        pass
    if not zh:
        reasons.append('needs_reliable_chinese_dialogue_base')
    if not ja:
        (review_notes if first_release else reasons).append('needs_reliable_japanese_dialogue_or_real_transcription')
    refs.extend({'kind': 'archived_draft', **d} for d in drafts)
    clean_ready, capability_ready, capability_refs = False, False, []
    if valid_media:
        try:
            ref = record.get('base_review') or (frozen['payload'].get('base_review') if frozen else None)
            review = proof(root, ref)
            clean = review.get('clean', {})
            identity_ready = bool(review.get('reviewer') and review.get('target_fingerprint') == target['fingerprint']
                               and review.get('identity', {}).get('status') == 'verified'
                               and review.get('fidelity', {}).get('status') == 'verified'
                               and all(review.get(k, {}).get('evidence') for k in ('identity', 'fidelity', 'clean')))
            whole_review = (clean.get('status') == 'verified'
                            and clean.get('method') in ('raw_continuous', 'soft_disabled_continuous'))
            candidate_review = (review.get('role') == 'production_candidate'
                                and review.get('scope') == 'sample_only'
                                and clean.get('status') == 'sampled_clear'
                                and clean.get('method') in ('raw_sampled', 'soft_disabled_sampled')
                                and sampled_frames_valid(root,clean.get('inspected_source_frames')))
            # Dispatch starts the whole-episode review; it must not require that
            # review to be completed already. Freeze/accept retain their full
            # continuous cleanliness/audio/semantic/player evidence gates.
            clean_ready = bool(identity_ready and (whole_review or candidate_review))
            if clean_ready:
                refs.append({'kind': 'base_review', **ref})
        except (ValueError, TypeError, OSError, KeyError):
            pass
        try:
            ref = record.get('capability_report')
            report = proof(root, ref)
            capability_ready = bool(report.get('reviewer') and report.get('target_fingerprint') == target['fingerprint']
                                    and report.get('actual_audio_received_and_listened') is True
                                    and report.get('continuous_picture_observed') is True
                                    and report.get('non_default_ass_player_verified') is True
                                    and type(report.get('expires_at')) in (int, float)
                                    and report['expires_at'] > time.time()
                                    and report.get('evidence'))
            if capability_ready:
                capability_refs.append(ref)
        except (ValueError, TypeError, OSError, KeyError):
            pass
        if first_release:
            # A local finished v1 needs usable output tools, not a fabricated
            # listening certificate. Formal accepted checks remain unchanged.
            capability_ready, capability_refs = False, []
            try:
                ref = record.get('delivery_capability_report')
                report = proof(root, ref)
                capability_ready = bool(report.get('role') == 'finished_v1_technical_preparation'
                                        and report.get('show_id') == sid and report.get('episode') == ep
                                        and report.get('reviewer')
                                        and report.get('target_fingerprint') == target['fingerprint']
                                        and report.get('technical_decode_passed') is True
                                        and report.get('output_tools_verified') is True
                                        and report.get('local_viewer_available') is True
                                        and type(report.get('expires_at')) in (int, float)
                                        and report['expires_at'] > time.time() and report.get('evidence'))
                if capability_ready:
                    capability_refs.append(ref)
                review_notes.append('whole_episode_original_audio_review_not_completed')
            except (ValueError, TypeError, OSError, KeyError):
                pass
    if not clean_ready:
        reasons.append('needs_actual_clean_mother_review')
    if not capability_ready:
        reasons.append('needs_verified_local_finished_output_tools' if first_release
                       else 'needs_verified_original_audio_and_continuous_player_capability')
    accepted = None
    if valid_media:
        try:
            accepted = authored(db, {'show_id': sid, 'episode': ep, **target})
        except (ValueError, TypeError, KeyError, OSError):
            pass
    book_sha = None
    if payload.get('workbook'):
        try:
            book = Path(payload['workbook']).resolve()
            if book.is_relative_to(root.resolve()) and book.is_file():
                book_sha = sha(book.read_bytes())
        except OSError:
            pass
    signature = sha(json.dumps({'target': target.get('fingerprint'), 'drafts': drafts},
                               sort_keys=True, ensure_ascii=False).encode())
    return {'material_ready': bool(valid_media and zh and (ja or first_release) and 'archived_input_changed_or_invalid' not in reasons),
            'dispatch_standard': 'finished_v1' if first_release else 'reviewed_bilingual',
            'review_notes': sorted(set(review_notes)),
            'clean_review_ready': clean_ready, 'capability_ready': capability_ready,
            'reasons': sorted(set(reasons)), 'target_fingerprint': target.get('fingerprint'),
            'input_signature': signature, 'material_refs': refs, 'capability_refs': capability_refs,
            'text_hashes': sorted({d['text_hash'] for d in zh}),
            'workbook': payload.get('workbook'), 'book_sha256': book_sha,
            'accepted_verified': bool(accepted), 'accepted_build_id': accepted['id'] if accepted else None}
