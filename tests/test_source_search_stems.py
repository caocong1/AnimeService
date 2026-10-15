import json
from unittest.mock import Mock
from urllib.parse import parse_qs,urlparse
from anime.db import Store
from anime.sources import Catalog


def test_broader_sequel_search_still_requires_exact_subject(tmp_path):
    db=Store(tmp_path/'test.db');db.upsert_subject({'id':42,'name':'佐々木とピーちゃん 第2期','name_cn':'佐佐木与文鸟小哔 第二季'})
    def fetch(url):
        term=parse_qs(urlparse(url).query).get('searchstr',[''])[0]
        if '/Home/Search' in url:
            return Mock(text='<a href="/Home/Bangumi/4072">Season 2</a>' if term=='佐々木とピーちゃん' else '')
        return Mock(text='<a href="https://bgm.tv/subject/42">identity</a>')
    cat=Catalog(db,Mock(fetch=Mock(side_effect=fetch)))
    assert cat.mikan_matches(42)[0]['id']=='4072'
    assert cat.mikan_mapping('4072',42)['verified']
