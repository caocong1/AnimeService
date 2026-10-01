import json

from fastapi.testclient import TestClient
from unittest.mock import Mock

from anime.app import create_app
from anime.db import Store


def client(tmp_path):
    app = create_app(Store(tmp_path / 'test.db'), Mock(), start_worker=False)
    return TestClient(app, base_url='http://127.0.0.1:4871')


def publish(tmp_path, size=None, **info):
    folder = tmp_path / 'tv'
    folder.mkdir(exist_ok=True)
    (folder / 'fanyu-tv.apk').write_bytes(b'apk-bytes')
    (folder / 'release.json').write_text(json.dumps({'version_code': 3, 'version_name': '1.2.0', 'notes': ['修复'],
                                                     'size': 9 if size is None else size, 'sha256': 'ab', **info}))


def test_no_release_is_empty_and_apk_missing(tmp_path):
    c = client(tmp_path)
    assert c.get('/api/tv/release').json() == {}
    assert c.get('/api/tv/release.apk').status_code == 404


def test_published_release_and_apk(tmp_path):
    publish(tmp_path)
    c = client(tmp_path)
    info = c.get('/api/tv/release').json()
    assert info == {'version_code': 3, 'version_name': '1.2.0', 'notes': ['修复'], 'size': 9, 'sha256': 'ab', 'url': '/api/tv/release.apk'}
    r = c.get('/api/tv/release.apk')
    assert r.status_code == 200 and r.content == b'apk-bytes'
    assert r.headers['content-type'] == 'application/vnd.android.package-archive'


def test_release_with_wrong_size_is_not_offered(tmp_path):
    publish(tmp_path, size=5)
    c = client(tmp_path)
    assert c.get('/api/tv/release').json() == {}
    assert c.get('/api/tv/release.apk').status_code == 404
