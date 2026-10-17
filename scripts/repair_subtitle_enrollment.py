"""Audit and reconcile independently authorized, selected watching shows.

No downloader, acceptance, playback or media changes. A continuing show receives
subtitle_only policy rather than reopening premiere research or choosing a source.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.subtitle_delivery_queue import ControlStore, ReadStore
from anime.viewing_workflow import queue_snapshot
from anime.subtitle_delivery import SubtitleDelivery
from anime.subtitle_enrollment import enroll, reconcile
from anime.subtitle_delivery_view import export_view


def audit(db):
    result = []
    for show in queue_snapshot(db)['queues']['watching']:
        sid = show['id']
        existing = {r['episode'] for r in db.rows(
            "SELECT episode FROM subtitle_delivery_jobs WHERE show_id=? AND kind='episode'", (sid,))}
        watched = {r['episode'] for r in db.rows('SELECT episode FROM watches WHERE show_id=? AND finished=1', (sid,))}
        complete = [r['episode'] for r in db.rows("SELECT episode FROM episodes WHERE show_id=? AND status='complete' ORDER BY episode", (sid,))
                    if r['episode'] not in watched]
        pending = [j['episode'] for j in show['jobs'] if j['episode'] not in existing]
        result.append({'show_id': sid, 'title': show['title'], 'source_mode': show['source_mode'],
                       'scope_blocker': show['scope_blocker'], 'next_unwatched': show['next_unwatched'],
                       'available_without_delivery_job': pending,
                       'unwatched_completed_episodes': complete,
                       'independently_allowed': bool(db.allowed(sid, 1)),
                       'missing_research_policy': show['source_mode'] is None})
    return result



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default=str(ROOT/'data/anime.db'))
    parser.add_argument('--show', type=int)
    parser.add_argument('--episodes', type=int, nargs='+')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--all-watching', action='store_true', help='Reconcile independently authorized watching scope')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    db = ReadStore(args.db)
    before = audit(db)
    db.con.rollback(); db.con.close()
    report = {'time': time.time(), 'before': before, 'applied': False}
    if args.apply:
        if args.all_watching and (args.show or args.episodes):
            parser.error('--all-watching cannot be combined with --show/--episodes')
        if not args.all_watching and (not args.show or not args.episodes):
            parser.error('--apply requires --all-watching or --show and --episodes')
        backup = args.output/'before.sqlite3'
        if backup.exists():
            raise ValueError('Preserve the original backup; use a fresh output directory')
        with sqlite3.connect(args.db) as src, sqlite3.connect(backup) as dst:
            src.backup(dst)
        db = ControlStore(args.db)
        enrollment = reconcile(db) if args.all_watching else enroll(db, args.show, args.episodes)
        scheduler = SubtitleDelivery(db)
        refreshed = scheduler.refresh()
        export_view(db, ROOT if Path(args.db).resolve() == (ROOT/'data/anime.db').resolve() else None)
        report.update(applied=True, enrollment=enrollment, show_id=args.show, episodes=args.episodes,
                      backup=str(backup), refreshed_jobs=refreshed['changed'], after=audit(db))
    (args.output/'enrollment-audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'applied': report['applied'], 'audit': str(args.output/'enrollment-audit.json'),
                      'missing_policies': [r['show_id'] for r in before if r['missing_research_policy']],
                      'uncovered_material': {r['show_id']: r['available_without_delivery_job'] for r in before
                                             if r['available_without_delivery_job']}}, ensure_ascii=False))


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'): sys.stdout.reconfigure(encoding='utf-8')
    main()
