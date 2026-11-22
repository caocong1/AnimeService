"""Finalize an already stopped successful render without another encode."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.finish_subtitle_episode import digest, check_unwatched, finalize_rendered


def finalize(config, review, owner, expected_input):
    from scripts.subtitle_delivery_queue import ReadStore
    from scripts.subtitle_output_lock import output_lock
    from anime.subtitle_coverage import validate as coverage
    from anime.subtitle_display_rows import validate as rows
    from anime.subtitle_refinement import validate_review
    import psutil
    c = json.loads(config.read_text('utf-8-sig'))
    root = Path(c['output']).resolve()
    assert not (root / 'release.json').exists(), 'Release already finalized'
    db = ReadStore(ROOT / 'data/anime.db')
    try:
        j = db.one('SELECT * FROM subtitle_delivery_jobs WHERE id=?', (c['delivery_job_id'],))
        assert j['thread_id'] == owner == c['delivery_thread_id'] and j['input_key'] == expected_input
        assert j['dispatch_state'] in ('blocked-stopped', 'acknowledged')
        show = db.one('SELECT * FROM shows WHERE id=?', (c['show_id'],))
        assert show['selected'] and show['state'] == 'watching' and show['authorized'] and not show['cleanup_hold']
    finally:
        db.con.close()
    check_unwatched(c)
    assert digest(config) == digest(root / 'input-config.json')
    assert not (root / 'worker.lock.json').exists()
    proc = json.loads((root / 'process.json').read_text('utf-8'))
    assert all(not psutil.pid_exists(proc[k]) for k in ('controller_pid', 'worker_pid')), 'Original render process still exists'
    outcome = json.loads((root / 'render-outcome.json').read_text('utf-8'))
    assert outcome['stopped'] and outcome['exit_code'] == 0 and not outcome.get('timeout')
    source = Path(c['media'])
    assert digest(source) == c['media_sha256'] and digest(c['ass']) == c['ass_sha256']
    original = Path(c['ass']).read_text('utf-8-sig').splitlines()
    removed, final = [], []
    for line in original:
        if line.startswith('Title:'):
            line = 'Title: ' + c['title']
        if c.get('omit_label_style') and line.startswith('Dialogue:') and line.split(',',4)[3] == c['omit_label_style']:
            removed.append(line)
        else:
            final.append(line)
    assert (root / 'bilingual.ass').read_text('utf-8-sig') == '\n'.join(final) + '\n'
    assert digest(root / 'static.ass') == c.get('static_ass_sha256', digest(root / 'bilingual.ass'))
    for font in c['fonts']:
        assert digest(font['path']) == font['sha256'] == digest(root / 'fonts' / Path(font['path']).name)
    for name in ('bilingual.ass', 'static.ass'):
        rows(root / name)
    coverage(ROOT, root / 'chinese-coverage-review.json', root / 'bilingual.ass', c['show_id'], c['episode'], c['media_sha256'], c['ass_sha256'])
    if c.get('edition') == 'v1':
        assert digest(c['refinement_review']) == digest(root / 'refinement-review.json')
        validate_review(ROOT, c['refinement_review'], c['show_id'], c['episode'], c['media_sha256'], c['baseline_release_id'])
    from scripts.subtitle_render_fonts import required_for_render, validate
    selected = [x for x in (root / 'render.log').read_text('utf-8', errors='replace').splitlines() if 'fontselect:' in x]
    validate(selected, required_for_render(c,root), root, digest, review)
    with output_lock(root / '.finalization.process.lock', 'subtitle_finalization', timeout=30):
        if (root / 'release.json').exists():
            return
        if (root / 'worker.lock.json').exists():
            raise ValueError('Output worker still present')
        check_unwatched(c)
        finalize_rendered(config, c, root, lambda name: None, source,
                          (source.stat().st_size, source.stat().st_mtime_ns), int(c.get('width',1920)),
                          root / c['filename'], removed, outcome, review)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('config', type=Path)
    p.add_argument('--symbol-review', type=Path)
    p.add_argument('--owner', required=True)
    p.add_argument('--expected-input', required=True)
    a = p.parse_args()
    from scripts.subtitle_output_lock import OutputBusyError
    try:
        finalize(a.config, a.symbol_review, a.owner, a.expected_input)
    except OutputBusyError as exc:
        print(json.dumps({'status':'waiting_for_output_slot','finalized':False,'reason':str(exc)}))
