import json
from types import SimpleNamespace
from unittest.mock import Mock

from anime.engine import Engine, observed_release_episodes
from anime.releases import validate_title


def test_title_reuse_retains_new_candidates_mapping_and_live_airing_file_boundaries(monkeypatch):
    observed_release_episodes.cache_clear()
    checked = Mock(wraps=validate_title)
    monkeypatch.setattr('anime.engine.validate_title', checked)
    mapping = {'aliases': ['Example'], 'season': 1, 'start': 1, 'end': 12, 'confirmed': True}
    snapshot = SimpleNamespace(shows={42: {'mapping': json.dumps(mapping), 'total': 12}},
        settings={}, airings={42: []}, candidates={42: [{'title': '[G] Example S01E01 [1080p]'}]},
        episodes={42: []})
    engine = object.__new__(Engine)
    engine.db = SimpleNamespace(show_eligible=lambda show: True)
    try:
        assert engine.gaps(42, snapshot=snapshot)['missing'] == [1]
        assert engine.gaps(42, snapshot=snapshot)['missing'] == [1]
        assert checked.call_count == 1
        snapshot.candidates[42].append({'title': '[G] Example S01E02 [1080p]'})
        assert engine.gaps(42, snapshot=snapshot)['missing'] == [1, 2]
        assert checked.call_count == 2
        snapshot.episodes[42].append({'episode': 1, 'status': 'complete'})
        assert engine.gaps(42, snapshot=snapshot)['missing'] == [2]
        snapshot.airings[42].append({'episode': 2, 'airdate': '2999-01-01'})
        assert engine.gaps(42, snapshot=snapshot)['missing'] == []
        assert checked.call_count == 2
        mapping['offset'] = 1
        snapshot.shows[42]['mapping'] = json.dumps(mapping)
        assert engine.gaps(42, snapshot=snapshot)['missing'] == []
        assert checked.call_count == 4
        mapping.update(require_season=True, season=2)
        snapshot.shows[42]['mapping'] = json.dumps(mapping)
        assert engine.gaps(42, snapshot=snapshot)['missing'] == []
        assert checked.call_count == 6
        assert engine.gaps(42, snapshot=snapshot)['missing'] == []
        assert checked.call_count == 6  # Rejections are deterministic too.
    finally:
        observed_release_episodes.cache_clear()
