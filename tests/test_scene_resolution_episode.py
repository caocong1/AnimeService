import pytest

from anime.releases import Rejected, episodes_of, select_files


@pytest.mark.parametrize('resolution', ['720p', '1080p', '2160p', '1080i'])
def test_scene_resolution_after_episode_is_a_technical_token(resolution):
    name = f'Example.S01E04.{resolution}.CR.WEB-DL.H.264.mkv'
    files = [{'index': 0, 'name': name, 'size': 100}]
    mapping = {'aliases': ['Example'], 'season': 1}
    assert episodes_of(name) == [4]
    assert select_files(files, mapping, [4], [4]) == ({4: files[0]}, [0])


@pytest.mark.parametrize('suffix', [
    '5.1080p', '021.1080p', '1080', '1080preview', '1080p50', '1080p5',
])
def test_numeric_or_partial_resolution_still_needs_separate_evidence(suffix):
    with pytest.raises(Rejected, match='小数集号'):
        episodes_of(f'Example.S01E04.{suffix}.CR.mkv')


@pytest.mark.parametrize('name', [
    'Other.S01E04.1080p.CR.mkv',
    'Example.S02E04.1080p.CR.mkv',
    'Example.S01E05.1080p.CR.mkv',
    'Example.S01E04.1080p.OVA.mkv',
])
def test_resolution_exception_keeps_identity_season_episode_and_special_guards(name):
    with pytest.raises(Rejected):
        select_files([{'index': 0, 'name': name, 'size': 100}],
                     {'aliases': ['Example'], 'season': 1}, [4], [4])
