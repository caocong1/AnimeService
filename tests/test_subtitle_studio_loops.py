"""Deterministic archive rejections are recorded once per input; derived tracks stay immutable.

The private subtitle core is replaced by a synthetic core and archive, so these run without the lab.
"""
import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_reliability import setup
import anime.subtitle_studio as studio_module
from anime.subtitle_chinese import archive_text_source


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def fake_core(time_format='{:d}'):
    cues = [{'id': 'c1', 'style': 'Text CN', 'text': '明天再见。', 'start_ms': 1000, 'end_ms': 2000},
            {'id': 'c2', 'style': 'Text JP', 'text': 'また明日。', 'start_ms': 1000, 'end_ms': 2000}]
    return SimpleNamespace(
        ZH={'chi', 'zho'}, JA={'jpn'}, digest=digest,
        canonical=lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False),
        fingerprint=lambda path, size=None: 'fp-' + Path(path).name,
        parse_subtitles=lambda raw, ext: [dict(c) for c in cues],
        vtt_time=lambda ms: time_format.format(ms))


class FakeArchive:
    """add_base keeps the core's identity rule: same identity, language and bytes is the same draft."""
    def __init__(self, root):
        self.root = Path(root);self.drafts = {};self.calls = []

    def register(self, *args, **kwargs):
        return 'key'

    def add_base(self, sid, season, ep, path, metadata):
        data = Path(path).read_bytes();self.calls.append(Path(path).name)
        if b'BROKEN' in data:
            raise ValueError('empty subtitles or invalid timing')
        identity = [ep] + [metadata.get(k) for k in ('source_key', 'track', 'media_fingerprint', 'language')] + [digest(data)]
        draft_id = digest(json.dumps(identity).encode())[:12]
        self.drafts.setdefault(draft_id, {'id': draft_id, 'language': metadata.get('language'),
                                          'metadata': {**metadata, 'raw_hash': digest(data)}})
        return draft_id

    def bases(self, sid, season, ep):
        return list(self.drafts.values())


ASS = ('[Script Info]\n[Events]\nDialogue: 0,0:00:01.00,0:00:02.00,Text CN,,明天再见。\n'
       'Dialogue: 0,0:00:01.00,0:00:02.00,Text JP,,また明日。\n')
META = {'source_key': 'h', 'track': 100000, 'media_fingerprint': 'fp-video', 'language': 'chi'}


def test_changed_parser_keeps_the_registered_derivation(tmp_path):
    source = tmp_path / 'source.ass';source.write_text(ASS, encoding='utf-8')
    archive = FakeArchive(tmp_path / 'archive')
    first = archive_text_source(archive, fake_core(), 42, 1, 1, source, META)
    assert len(first) == 3 and len(archive.drafts) == 3
    staged = archive.root / 'derived-language-tracks' / digest(source.read_bytes())
    before = {p.name: p.read_bytes() for p in staged.iterdir()}
    assert set(before) == {'chi.vtt', 'jpn.vtt'}
    again = archive_text_source(archive, fake_core('{:08d}'), 42, 1, 1, source, META)
    assert again == first and len(archive.drafts) == 3
    assert {p.name: p.read_bytes() for p in staged.iterdir()} == before


def test_unregistered_staging_file_is_kept_and_the_new_body_is_staged_beside_it(tmp_path):
    source = tmp_path / 'source.ass';source.write_text(ASS, encoding='utf-8')
    archive = FakeArchive(tmp_path / 'archive')
    staged = archive.root / 'derived-language-tracks' / digest(source.read_bytes())
    staged.mkdir(parents=True);(staged / 'chi.vtt').write_bytes(b'WEBVTT\n\n100')  # torn, never registered
    ids = archive_text_source(archive, fake_core(), 42, 1, 1, source, META)
    assert len(ids) == 3 and (staged / 'chi.vtt').read_bytes() == b'WEBVTT\n\n100'
    sibling = [p for p in staged.iterdir() if p.name.startswith('chi-')]
    assert len(sibling) == 1 and b'1000 --> 2000' in sibling[0].read_bytes()
    assert archive_text_source(archive, fake_core(), 42, 1, 1, source, META) == ids and len(archive.drafts) == 3
    assert not [p for p in staged.iterdir() if p.name.endswith('.tmp')]
