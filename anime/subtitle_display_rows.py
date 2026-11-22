"""Check final ASS line breaks before a new viewing release is delivered.

This is a structural house-style check, not picture-occlusion or font QA.
Never rewrite a source or an immutable release to make the check pass.
"""
import re
from pathlib import Path


def _language(font, style=''):
    for value in (font.lower(), style.lower()):
        if re.search(r'\b(ja|jp|japanese)\b', value):
            return 'ja'
        if re.search(r'\b(zh|sc|tc|chinese)\b', value):
            return 'zh'
    return 'unspecified'


def scan(path):
    """Read raw ASS escapes, rather than parser-normalized source text.

    Shadow copies are one finding. A deliberate paired ZH/JA event is allowed
    only when its visible rows switch language fonts in the correct order.
    Lowercase \\n is also rejected conservatively: q2 makes it a line break.
    """
    fonts, issues, seen = {}, [], set()
    section = ''
    style_format = event_format = None
    for number, line in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if line.startswith('['):
            section = line.lower()
        if section in ('[v4+ styles]', '[v4 styles]'):
            if line.startswith('Format:'):
                style_format = [x.strip().lower() for x in line.split(':', 1)[1].split(',')]
            elif line.startswith('Style:') and style_format:
                values = line.split(':', 1)[1].strip().split(',', len(style_format) - 1)
                row = dict(zip(style_format, values))
                fonts[row['name']] = row.get('fontname', '')
        if section != '[events]':
            continue
        if line.startswith('Format:'):
            event_format = [x.strip().lower() for x in line.split(':', 1)[1].split(',')]
        elif line.startswith('Dialogue:'):
            if not event_format or event_format[-1] != 'text':
                raise ValueError('ASS display-row check requires Events Format ending in Text')
            values = line.split(':', 1)[1].strip().split(',', len(event_format) - 1)
            if len(values) != len(event_format):
                raise ValueError('Malformed ASS dialogue')
            row = dict(zip(event_format, values))
            style, text = row['style'], row['text']
            language = _language(fonts.get(style, ''), style)
            languages, visible_languages = [], set()
            def finish_row():
                languages.append(next(iter(visible_languages)) if len(visible_languages) == 1
                                 else 'mixed' if visible_languages else 'empty')
                visible_languages.clear()
            # Keep tags for font switching, but don't count their contents as text.
            for token in re.split(r'(\{[^}]*\}|\\[Nn])', text):
                if token.startswith('{'):
                    # Overrides only affect subsequent text. Preserve their order:
                    # a reset followed by a font override differs from the reverse.
                    for tag, value in re.findall(r'\\(fn|r)([^\\}]*)', token):
                        if tag == 'fn':
                            language = _language(value or fonts.get(style, ''), style)
                        else:
                            language = _language(fonts.get(value or style, ''), value or style)
                elif token in (r'\N', r'\n'):
                    finish_row()
                elif token.strip():
                    visible_languages.add(language)
            finish_row()
            if len(languages) == 1 or languages == ['zh', 'ja']:
                continue
            plain = re.sub(r'\{[^}]*\}', '', text)
            key = (row['start'], row['end'], style, row.get('name'), plain)
            if key in seen:
                continue
            seen.add(key)
            issues.append({'line': number, 'start': row['start'], 'end': row['end'],
                           'style': style, 'actor': row.get('name', ''),
                           'text': plain, 'row_languages': languages})
    return {'ass': str(Path(path).resolve()), 'issues': issues,
            'scope': 'explicit ASS line breaks; no visual occlusion acceptance'}


def validate(path):
    report = scan(path)
    if report['issues']:
        first = report['issues'][0]
        raise ValueError(f"ASS display rows violate one row per language: {len(report['issues'])} events; "
                         f"first {first['actor']} {first['start']} ({path}). "
                         "Review source escapes, semantic segmentation and actual rendered frames; "
                         "do not shrink text or remove translation.")
    return report
