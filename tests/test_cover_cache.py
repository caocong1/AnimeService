import concurrent.futures
import io
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from anime.app import create_app
from anime.cover_cache import CACHE_CONTROL, CoverCache, CoverUnavailable, checked_url, local_url
from anime.db import Store

URL = 'https://lain.bgm.tv/r/800/pic/cover/l/17/c7/554779_oPitO.jpg'


def jpeg(size=(800, 1200)):
    buffer = io.BytesIO()
    Image.new('RGB', size, (32, 112, 160)).save(buffer, 'JPEG')
    return buffer.getvalue()


def test_covers_are_resized_once_and_work_after_restart_without_the_network(tmp_path):
    fetch = Mock(return_value=jpeg())
    cache = CoverCache(tmp_path, fetch=fetch)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        files = list(pool.map(lambda width: cache.get(URL, width), [160, 480] * 4))
    fetch.assert_called_once_with(URL)
    for path in set(files):
        with Image.open(path) as image:
            width = int(path.stem.rsplit('-', 1)[1])
            assert image.format == 'WEBP' and image.size == (width, width * 3 // 2)
    cold = CoverCache(tmp_path, fetch=Mock(side_effect=RuntimeError('offline')))
    assert cold.get(URL).read_bytes() == files[1].read_bytes()
    assert cold.warm([URL, URL]) == {'cached': 1, 'downloaded': 0, 'failed': 0, 'stopped': 0}
    cold.fetch.assert_not_called()
    assert not list(tmp_path.glob('*.tmp'))


def test_missing_thumbnail_is_repaired_and_source_changes_have_separate_keys(tmp_path):
    fetch = Mock(return_value=jpeg((80, 120)))
    cache = CoverCache(tmp_path, fetch=fetch)
    path = cache.get(URL, 160)
    with Image.open(path) as image:
        assert image.size == (80, 120)  # Never inflate a small source.
    path.unlink()
    assert cache.warm([URL])['downloaded'] == 1 and path.is_file()
    new = URL.replace('oPitO', 'Changed')
    assert cache.get(new) != cache.get(URL)
    assert len(list(tmp_path.glob('*.webp'))) == 4


@pytest.mark.parametrize('url', [
    'http://lain.bgm.tv/pic/cover/l/17/c7/a.jpg', 'https://127.0.0.1/pic/cover/l/17/c7/a.jpg',
    'https://lain.bgm.tv.evil.test/pic/cover/l/17/c7/a.jpg',
    URL.replace('lain.bgm.tv', 'user:secret@lain.bgm.tv'), URL + '?token=secret', URL + '#fragment',
    'https://lain.bgm.tv//pic/cover/l/17/c7/a.jpg',
    'https://lain.bgm.tv/pic/cover/l/17/c7/../../config.json',
    'https://lain.bgm.tv/pic/cover/l/17/c7/a.svg',
])
def test_only_fixed_public_image_paths_can_reach_the_network(tmp_path, url):
    fetch = Mock()
    with pytest.raises(ValueError):
        CoverCache(tmp_path, fetch=fetch).get(url)
    assert local_url(url) == ''
    fetch.assert_not_called()


def test_invalid_or_oversized_images_are_not_published_and_failures_cool_down(tmp_path, monkeypatch):
    for name, data in [('html', b'<html>failed</html>'), ('large', b'x' * (4 * 1024 * 1024 + 1))]:
        fetch = Mock(return_value=data)
        cache = CoverCache(tmp_path / name, fetch=fetch)
        for _ in range(2):
            with pytest.raises(CoverUnavailable):
                cache.get(URL)
        assert fetch.call_count == 1 and not list((tmp_path / name).glob('*'))
    monkeypatch.setattr('anime.cover_cache.MAX_PIXELS', 100)
    with pytest.raises(CoverUnavailable):
        CoverCache(tmp_path / 'pixels', fetch=lambda url: jpeg()).get(URL)


def test_redirect_cannot_leave_the_allowlisted_image_host(tmp_path):
    cache = CoverCache(tmp_path)
    response = Mock(is_redirect=True, headers={'Location': 'http://127.0.0.1:4870/private'})
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    cache.http.session = Mock()
    cache.http.session.get.return_value = response
    with pytest.raises(CoverUnavailable):
        cache.get(URL)
    assert cache.http.session.get.call_count == 1


def test_cached_http_image_has_conditional_private_cache_and_existing_access_controls(tmp_path, monkeypatch):
    monkeypatch.setattr('anime.app.load_deployment', lambda: {'public_authorities': ['anime.example.test']})
    db = Store(tmp_path / 'test.db')
    app = create_app(db, Mock(), start_worker=False)
    cache = app.state.cover_cache
    cache.fetch = Mock(return_value=jpeg())
    client = TestClient(app, base_url='http://localhost:4871')
    path = local_url(URL)
    response = client.get(path)
    assert response.status_code == 200 and response.headers['content-type'] == 'image/webp'
    assert response.headers['cache-control'] == CACHE_CONTROL
    cache.fetch.assert_called_once_with(URL)
    response2 = client.head(path)
    assert response2.status_code == 200 and not response2.content
    conditional = client.get(path, headers={'If-None-Match': 'W/' + response.headers['etag']})
    assert conditional.status_code == 304 and not conditional.content
    assert conditional.headers['cache-control'] == CACHE_CONTROL
    assert client.get(path.replace('/480/', '/999/')).status_code == 404
    remote = TestClient(app, base_url='https://anime.example.test')
    assert remote.get(path, follow_redirects=False).status_code == 303
    assert not db.rows('SELECT * FROM watches')
    assert cache.fetch.call_count == 1


def test_bootstrap_advertises_local_covers_and_detail_html_preloads_its_own_image(tmp_path):
    db = Store(tmp_path / 'test.db')
    db.upsert_subject({'id': 42, 'name': 'Example', 'images': {'medium': URL}})
    app = create_app(db, Mock(), start_worker=False)
    client = TestClient(app, base_url='http://localhost:4871')
    assert client.get('/api/bootstrap').json()['cover_cache_version'] == 1
    page = client.get('/show/42')
    assert page.headers['link'] == f'<{local_url(URL)}>; rel=preload; as=image'
    assert page.headers['cache-control'] == 'no-cache'
    db.execute('UPDATE shows SET image=? WHERE id=42', (URL.replace('oPitO', 'Changed'),))
    assert 'Changed' in client.get('/show/42').headers['link']
    assert 'link' not in client.get('/show/999').headers
