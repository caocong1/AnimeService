from unittest.mock import Mock

import pytest
import requests

from anime.qbit import Qbit, QbitUnavailable


def test_loopback_does_not_use_environment_proxy(tmp_path):
    assert Qbit(tmp_path/'unused').session.trust_env is False


@pytest.mark.parametrize('method', ['GET', 'POST'])
@pytest.mark.parametrize('error', [requests.Timeout, requests.ConnectionError])
def test_transport_failure_is_classified_and_never_replays_write(tmp_path, monkeypatch, method, error):
    q = Qbit(tmp_path/'unused')
    q.session.request = Mock(side_effect=error('secret must not leak'))
    monkeypatch.setattr('anime.qbit.socket.create_connection', Mock(side_effect=ConnectionRefusedError()))
    with pytest.raises(QbitUnavailable) as caught:
        q.call('torrents/add', method)
    assert caught.value.code == 'qbit_unavailable'
    assert 'secret' not in str(caught.value)
    assert ('超时' if error is requests.Timeout else '未监听') in str(caught.value)
    q.session.request.assert_called_once()
