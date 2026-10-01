import pytest
from anime.danmu_identity import Work, episode_number


@pytest.mark.parametrize('title', [
    '无职转生 第三季', '無職轉生 第3季', '无职转生 3期',
    'Mushoku Tensei Season 3',
])
def test_work_season_variations_are_equivalent(title):
    work=Work(['无职转生 第三季 ～到了异世界就拿出真本事～', 'Mushoku Tensei S3'],3)
    assert work.matches(title)
    assert work.matches('【'+title+'】全14话 超清中字',True)


@pytest.mark.parametrize('title', [
    '无职转生 第二季 第1话', '无职转生 第一季 第13话',
    '无职转生 第1-3季 全集', '无职转生 S1+S2+S3 全集',
    '无职转生 第三季 第2部分 第1集',
    '无职转生 第三季 第13集 reaction', '无职转生 第三季 第13集 解說',
    '无职转生 第三季 第13集 预告', '无职转生 第三季 第13集 番外',
    '无职转生 第13话', '无职 第三季 第13话',
])
def test_wrong_or_unproven_versions_are_rejected(title):
    work=Work(['无职转生 第三季 ～到了异世界就拿出真本事～', '无职转生 3期'],3)
    assert not work.matches(title,True)


@pytest.mark.parametrize(('label','number'),[
    ('【bilibili1】 01 · 32:38',1), ('【bilibili1】 第十三話 · 32:38',13),
    ('【bilibili1】 无职转生 第三季 第14话 · 34:50',14),
    ('【bilibili1】 小书痴的下克上 领主的养女--23 · 31:10',23),
    ('【bilibili1】 S03E07',7), ('Episode 7',7),
    ('【bilibili1】 P13 · 32:38',None), ('【bilibili1】 周更 · 33:38',None),
    ('【bilibili1】 第13集预告 · 1:38',None), ('【bilibili1】 第13集上半 · 12:00',None),
    ('【bilibili1】 13-14 · 44:00',None), ('【bilibili1】 第0话',None),
    ('【bilibili1】 第12.5话',None), ('第1话 / 第2话',None),
    ('【bilibili1】 第13话 OP',None), ('【bilibili1】 1/2',None),
])
def test_source_written_episode_labels_not_p_order(label,number):
    assert episode_number(label)==number


def test_generic_alias_does_not_identify_a_sequel_without_season():
    work=Work(['某个测试动画 第二季','Some Test Anime Season 2','Some Test Anime'],2)
    assert not work.matches('Some Test Anime')
    assert not work.matches('Some Test Anime 第1话',True)
    assert work.matches('Some Test Anime Season 2 第1话',True)


def test_verified_number_suffix_and_bilingual_catalogue_title():
    assert Work(['异世界悠闲农家 第二季','異世界のんびり農家２'],2).matches('異世界のんびり農家２')
    assert Work(['摩绪','MAO'],1).matches('MAO 摩緒')
    assert not Work(['摩绪','MAO'],1).matches('MAO 某个不同作品')


def test_season_before_or_after_subtitle_is_same_work():
    work=Work(['无职转生 第三季 ～到了异世界就拿出真本事～'],3)
    assert work.matches('無職轉生～到了異世界就拿出真本事～第三季')
    assert not work.matches('無職轉生～到了異世界就拿出真本事～第二季')


def test_generic_season_alias_cannot_erase_verified_cour():
    work=Work(['测试作品 第3部分','Test Anime Season 4'],4)
    assert not work.matches('Test Anime Season 4')
    assert not work.matches('【Test Anime Season 4】第13集',True)
    assert not work.matches('测试作品 第2部分')
    assert work.matches('测试作品 第3部分')
