"""Strict render font gate with immutable, output-specific symbol review."""
import json
import re
from pathlib import Path


def required_for_render(config, root):
    """Only narrow the packaged SC/JP pair for a proven Chinese-only v0."""
    required = config['font_postscript_names']
    if config.get('edition') != 'v0' or set(required) != {'NotoSansCJKsc-Medium','NotoSansCJKjp-Medium'}:
        return required
    roles = json.loads((root/'chinese-coverage-review.json').read_text(encoding='utf-8-sig'))['styles']
    section = ''
    columns = []
    styles = {}
    used = set()
    for raw in (root/'bilingual.ass').read_text(encoding='utf-8-sig').splitlines():
        line = raw.strip()
        if line.startswith('['):
            section, columns = line.lower(), []
        if section not in ('[v4+ styles]', '[events]'):
            continue
        kind, _, data = line.partition(':')
        if kind.lower() == 'format':
            columns = [k.strip().lower() for k in data.split(',')]
        elif kind.lower() in ('style','dialogue'):
            row = dict(zip(columns, data.strip().split(',', len(columns)-1))) if columns else {}
            if kind.lower() == 'style':
                styles[row.get('name')] = row.get('fontname')
            else:
                if columns[-1:] != ['text'] or re.search(r'\\(?:fn|r)', row.get('text',''), re.I):
                    return required
                used.add(row.get('style'))
    if used and all(roles.get(k)=='zh' and styles.get(k)=='Noto Sans CJK SC Medium' for k in used):
        return ['NotoSansCJKsc-Medium']
    return required


def validate(selected, required, root, digest, review=None):
    assert selected, 'Missing font selection log'
    actual = {line.split(' -> ')[-1].split(',')[0].strip() for line in selected}
    expected = set(required)
    assert expected <= actual, 'Required dialogue font not selected'
    extra = actual - expected
    if not extra:
        return None
    assert review, 'Unexpected font fallback requires exact rendered-output review'
    r = json.loads(Path(review).read_text(encoding='utf-8'))
    assert r['role'] == 'rendered_symbol_fallback_review' and r['reviewer']
    assert r['extra_postscript_names'] == sorted(extra)
    # This exception is deliberately limited to a reviewed nonverbal symbol.
    assert extra == {'SegoeUIEmoji'} and r['codepoints'] == ['U+2764']
    for name in ('input-config.json', 'bilingual.ass', 'static.ass', 'render.log'):
        assert digest(root / name) == r['files'][name], 'Reviewed render inputs changed'
    movie = root / r['movie']['name']
    assert movie.parent == root and digest(movie) == r['movie']['sha256']
    events = set()
    for line in (root / 'bilingual.ass').read_text('utf-8-sig').splitlines():
        if '\u2764' not in line:
            continue
        fields = line.split(',', 9)
        assert line.startswith('Dialogue:') and len(fields) == 10
        text = re.sub(r'\{[^}]*\}', '', fields[9])
        assert text.count('\u2764') == 1
        events.add((fields[1], fields[2], fields[3], fields[4], text))
    assert len(events) == 1, 'Review covers one display event, including its shadow layers'
    for ref in r['evidence']:
        assert digest(ref['path']) == ref['sha256'], 'Symbol review evidence changed'
    assert r['evidence'] and r['full_size_and_small_player_verified'] is True
    return r
