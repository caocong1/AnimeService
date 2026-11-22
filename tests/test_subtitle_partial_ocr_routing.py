"""Partial recovered cues must retain source correction, independent of alignment."""
import pytest
import anime.subtitle_chinese as chinese

@pytest.mark.parametrize('stage,expected', [('recovered_cues_corrected','review_chinese_ocr'),
                                           ('complete','align_chinese_source')])
def test_source_completion_controls_next_route(monkeypatch, stage, expected):
    draft={'sha256':'fixture','estimated':True,'mixed':False,
           'candidate_not_reliable_base':True,'origin':'codex_partial',
           'correction_progress':{'source_hardtext_review':stage}}
    monkeypatch.setattr(chinese,'archived_chinese_sources',lambda *a:[draft])
    monkeypatch.setattr(chinese,'completed_sources',lambda *a:[])
    monkeypatch.setattr(chinese,'discovery_sources',lambda *a:[])
    plan=chinese.chinese_preparation(None,42,2)
    assert plan['action']==expected
    assert plan['production_ready_claimed'] is False
    if stage!='complete':
        assert 'review_chinese_ocr' in plan['actions']
        assert 'align_chinese_source' not in plan['actions']
