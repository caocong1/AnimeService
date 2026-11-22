"""Regression for source ASS escapes surviving whitespace flattening."""
import pytest
import json

from anime.subtitle_display_rows import scan, validate


def subtitle(tmp_path, text, style='JA', shadow=False):
    header = '''[V4+ Styles]
Format: Name, Fontname
Style: JA,Noto Sans CJK JP Medium
Style: ZH,Noto Sans CJK SC Medium
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
'''
    event = f'0:04:11.28,0:04:14.78,{style},JA58P0,0,0,0,,{{\\an2\\pos(960,1036)\\q2}}{text}\n'
    path = tmp_path / 'final.ass'
    path.write_text(header + 'Dialogue: 1,' + event +
                    ('Dialogue: 0,' + event if shadow else ''), encoding='utf-8-sig')
    return path


@pytest.mark.parametrize('newline', [r'\N', r'\n'])
def test_raw_source_escape_rejected_and_shadow_deduplicated(tmp_path, newline):
    path = subtitle(tmp_path, '貴方にならば教えてしまっても' + newline + '構わないのですが……', shadow=True)
    before = path.read_bytes()
    assert len(scan(path)['issues']) == 1
    with pytest.raises(ValueError, match='JA58P0 0:04:11.28'):
        validate(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize('style,text', [('JA', '構わないのですが……'), ('ZH', '告诉您倒也没什么关系')])
def test_single_row_passes(tmp_path, style, text):
    assert validate(subtitle(tmp_path, text, style))['issues'] == []


def test_intentional_zh_then_ja_pair_passes(tmp_path):
    assert validate(subtitle(tmp_path, r'中文\N{\fnNoto Sans CJK JP Medium}日本語', 'ZH'))['issues'] == []


@pytest.mark.parametrize('text', [r'中文\N另一行', r'中文\N{\fnNoto Sans CJK JP Medium}日文\N第三行',
                                r'中文\N{\rZH}另一行'])
def test_repeated_language_or_third_row_rejected(tmp_path, text):
    with pytest.raises(ValueError, match='one row per language'):
        validate(subtitle(tmp_path, text, 'ZH'))


def test_font_override_takes_precedence_over_style(tmp_path):
    assert validate(subtitle(tmp_path, r'{\fnNoto Sans CJK SC Medium}中文\N{\fnNoto Sans CJK JP Medium}日本語'))['issues'] == []


@pytest.mark.parametrize('text', [
    r'一行目{\fnNoto Sans CJK SC Medium}\N{\fnNoto Sans CJK JP Medium}二行目',
    r'{\rZH\fnNoto Sans CJK JP Medium}一行目\N{\fnNoto Sans CJK JP Medium}二行目',
    r'{\fnNoto Sans CJK SC Medium}中文{\fnNoto Sans CJK JP Medium}一行目'
    r'{\fnNoto Sans CJK SC Medium}\N{\fnNoto Sans CJK JP Medium}二行目',
])
def test_visible_rows_cannot_be_relabelled_by_later_font_tags(tmp_path, text):
    path = subtitle(tmp_path, text, shadow=True)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='one row per language'):
        validate(path)
    assert len(scan(path)['issues']) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize('text', [
    r'中文{\fnNoto Sans CJK JP Medium}\N日本語',
    r'{\rJA\fnNoto Sans CJK SC Medium}中文\N{\fnNoto Sans CJK JP Medium}日本語',
    r'{\fnNoto Sans CJK JP Medium\rZH}中文\N{\rJA}日本語',
])
def test_valid_pairs_use_visible_text_fonts_in_tag_order(tmp_path, text):
    assert validate(subtitle(tmp_path, text, 'ZH'))['issues'] == []


def test_encoding_rejects_before_output_directory_or_worker(tmp_path, monkeypatch):
    from scripts import finish_subtitle_episode
    from anime import subtitle_coverage
    monkeypatch.setattr(finish_subtitle_episode, 'PROJECT', tmp_path)  # never the project's real DB
    build = finish_subtitle_episode.build
    monkeypatch.setattr(subtitle_coverage, 'validate', lambda *args: None)
    ass = subtitle(tmp_path, r'第一行\N第二行')
    output = tmp_path / 'new-release'
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'ass': str(ass), 'output': str(output),
                                 'show_id': 42, 'episode': 1, 'media_sha256': 'fixture'}))
    with pytest.raises(ValueError, match='one row per language'):
        build(config)
    assert not output.exists()


def test_publish_rejects_before_index_or_media_links(tmp_path, monkeypatch):
    from scripts import publish_finished_episode as publisher
    from anime import subtitle_coverage
    monkeypatch.setattr(publisher, 'PROJECT', tmp_path)
    monkeypatch.setattr(subtitle_coverage, 'validate_release', lambda *args: None)
    root = tmp_path / 'data/subtitle-studio/releases/42/S01E001/v0'
    root.mkdir(parents=True)
    ass = subtitle(root, r'第一行\N第二行')
    ass.rename(root / 'bilingual.ass')
    (root / 'input-config.json').write_text('{}')
    (root / 'release.json').write_text(json.dumps({'id': 'fixture-v0', 'status': 'finished', 'files': {}}))
    with pytest.raises(ValueError, match='one row per language'):
        publisher.publish(root, 'fixture', None)
    assert not (tmp_path / 'static').exists()
