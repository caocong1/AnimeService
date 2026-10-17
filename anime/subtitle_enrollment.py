"""Idempotent watching enrollment, independent of the premiere/quarter cohort.

Only policy and requested workflow metadata are written. This cannot authorize a
show, acquire material, claim a writer or manufacture readiness evidence.
"""
import json
import time

from .viewing_workflow import RESEARCH_MODES, enabled, queue_snapshot

AUTHORITY = 'User 2026-10-16: repair eligible watching omissions; v0 then same-owner v1'


def enroll(db, sid, episodes):
    episodes = sorted(set(episodes))
    show = db.show(sid)
    if (not show or not show['selected'] or show['state'] != 'watching'
            or not db.allowed(sid, 1)
            or any(type(ep) is not int or not db.allowed(sid, ep) for ep in episodes)):
        raise ValueError('Explicit watching selection and independent episode authorization required')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        current = c.execute('SELECT * FROM shows WHERE id=?', (sid,)).fetchone()
        mapping = json.loads(current['mapping']) if current else {}
        if (not current or not current['selected'] or current['state'] != 'watching'
                or current['cleanup_hold'] or not current['authorized']
                or not mapping.get('confirmed') or not mapping.get('inventory_checked')
                or any(not mapping.get('start', 1) <= ep <= mapping.get('end', current['total'] or 999)
                       for ep in [1, *episodes])):
            raise ValueError('Watching scope changed')
        old = c.execute('SELECT config FROM source_policies WHERE show_id=?', (sid,)).fetchone()
        if old and json.loads(old['config']).get('mode') not in RESEARCH_MODES:
            raise ValueError('Existing research hold preserved')
        inserted = not old
        if inserted:
            config = {'mode': 'subtitle_only', 'enrollment': 'explicit_user_watching_subtitles',
                      'primary': '', 'backups': [], 'wait_hours': 24,
                      'runbook': 'docs/SUBTITLE-TWO-PASS.md'}
            c.execute('INSERT INTO source_policies VALUES(?,?,?)',
                      (sid, json.dumps(config), time.time()))
        requests = 0
        for ep in episodes:
            watched = c.execute('SELECT finished FROM watches WHERE show_id=? AND episode=?', (sid, ep)).fetchone()
            if watched and watched['finished']:
                continue
            key = f'subtitle_delivery_readiness_{sid}_{ep}'
            row = c.execute('SELECT value FROM kv WHERE key=?', (key,)).fetchone()
            value = json.loads(row['value']) if row else {}
            before = dict(value)
            # Request metadata is not material/QA proof. Preserve existing facts.
            value.setdefault('dispatch_standard', 'finished_v1')
            value.setdefault('requested_editions', ['v0', 'v1'])
            value.setdefault('request_authority', AUTHORITY)
            if row and value == before:
                continue
            c.execute('INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (key, json.dumps(value, ensure_ascii=False)))
            requests += 1
    return {'show_id': sid, 'policy_added': inserted, 'requests_added': requests}


def reconcile(db, sid=None):
    """Event + periodic catch-up; explicit holds and independent gates win."""
    result = {'policies_added': [], 'requests_added': 0, 'skipped': []}
    if not enabled(db):
        return result
    for show in queue_snapshot(db)['queues']['watching']:
        identity = show['id']
        if sid is not None and sid != identity:
            continue
        if not db.allowed(identity, 1) or (show['source_mode'] is not None
                                         and show['source_mode'] not in RESEARCH_MODES):
            continue
        watched = {r['episode'] for r in db.rows(
            'SELECT episode FROM watches WHERE show_id=? AND finished=1', (identity,))}
        episodes = {job['episode'] for job in show['jobs']}
        if show['next_unwatched']:
            episodes.add(show['next_unwatched'])
        episodes = sorted(ep for ep in episodes - watched if ep > 0 and db.allowed(identity, ep))
        if not episodes and show['source_mode'] is not None:
            continue
        try:
            change = enroll(db, identity, episodes)
        except ValueError as exc:
            # Permission may change after the read-only snapshot. Never override it.
            result['skipped'].append({'show_id': identity, 'reason': str(exc)})
            continue
        if change['policy_added']:
            result['policies_added'].append(identity)
        result['requests_added'] += change['requests_added']
    return result


def coverage(db):
    """Expose drift separately from legitimate authorization/material blockers."""
    missing, protected = [], []
    for show in queue_snapshot(db)['queues']['watching']:
        if show['source_mode'] not in RESEARCH_MODES:
            target = missing if show['source_mode'] is None and db.allowed(show['id'], 1) else protected
            target.append({'show_id': show['id'], 'title': show['title'],
                           'reason': show['scope_blocker']})
    return {'missing_eligible_policies': missing, 'protected_or_unauthorized': protected}
