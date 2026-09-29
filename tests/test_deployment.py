import json

from fastapi.testclient import TestClient
from unittest.mock import Mock

from anime.app import create_app
from anime.db import Store
from anime.deployment import load


def test_fresh_checkout_rejects_remote_access(tmp_path, monkeypatch):
    monkeypatch.setenv('ANIMESERVICE_DEPLOYMENT', str(tmp_path / 'missing.json'))
    app = create_app(Store(tmp_path / 'test.db'), Mock(), start_worker=False)
    assert load() == {}
    assert TestClient(app, base_url='http://127.0.0.1:4871').get('/').status_code == 200
    for url in ('https://anime.example.test', 'http://192.168.1.100:4871'):
        client = TestClient(app, base_url=url, client=('192.168.1.20', 50000))
        assert client.get('/api/bootstrap').status_code == 403


def test_explicit_database_and_deployment_override(tmp_path, monkeypatch):
    monkeypatch.setenv('ANIMESERVICE_DB', str(tmp_path / 'override.db'))
    assert Store().path == tmp_path / 'override.db'
    config = tmp_path / 'deployment.json'
    config.write_text(json.dumps({'public_authorities': ['anime.example.test']}))
    monkeypatch.setenv('ANIMESERVICE_DEPLOYMENT', str(config))
    app = create_app(Store(), Mock(), start_worker=False)
    client = TestClient(app, base_url='https://anime.example.test')
    assert client.get('/api/bootstrap').status_code == 401
