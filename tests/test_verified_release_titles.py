import pytest

from anime.releases import Rejected, select_files, validate_title


TITLE = '[NEST] Steel Ball Run JoJos Bizarre Adventure - 03 [1080p][简日内封]'
MAPPING = {'aliases': ['JoJos Bizarre Adventure'], 'season': 6, 'offset': 1,
           'required_title': '第二&第三赛段', 'verified_release_titles': [TITLE]}


def test_exact_verified_title_retains_numbering_and_file_checks():
    assert validate_title(TITLE, MAPPING, {}, False) == [2]
    files = [{'index': 0, 'name': 'JoJos Bizarre Adventure S06E03.mkv', 'size': 100}]
    assert set(select_files(files, MAPPING, [2], [2])[0]) == {2}
    with pytest.raises(Rejected):
        select_files([dict(files[0], name='JoJos Bizarre Adventure S05E03.mkv')],
                     MAPPING, [2], [2])


@pytest.mark.parametrize('title', [TITLE.replace('03', '01'), TITLE.replace('NEST', 'OTHER'),
                                  TITLE.replace('03', '04')])
def test_unverified_release_still_requires_catalog_suffix(title):
    with pytest.raises(Rejected, match='赛段'):
        validate_title(title, MAPPING, {}, False)


def test_original_suffix_rule_remains_available():
    assert validate_title('JoJos Bizarre Adventure 第二&第三赛段 - 04',
                          MAPPING, {}, False) == [3]
