import errno,sqlite3
import pytest
import requests
from anime.bangumi import BangumiError
from anime.net import NetworkError,RequestDeferred,safe_error
from anime.releases import Rejected

NETWORK='：请求失败，请检查网络或本机接口'


@pytest.mark.parametrize('error',[requests.exceptions.ProxyError('https://x.invalid/?token=secret'),
                                  requests.exceptions.SSLError('token=secret'),ConnectionError('secret'),TimeoutError('secret')])
def test_transport_errors_keep_the_network_wording(error):
    assert safe_error(error)==type(error).__name__+NETWORK


def test_project_authored_messages_pass_through():
    assert safe_error(RequestDeferred(0,'https://bt4gprx.com/search?q=secret'))=='站点请求等待下一时隙'
    assert safe_error(Rejected('种子infohash与来源标识不符'))=='种子infohash与来源标识不符'
    assert safe_error(BangumiError('Bangumi HTTP 502，稍后回读重试'))=='Bangumi HTTP 502，稍后回读重试'
    assert safe_error(NetworkError('x HTTP 500'))=='x HTTP 500'


def test_local_failures_are_not_reported_as_network_trouble():
    assert safe_error(ValueError('D:/x token=secret'))=='ValueError：内部处理失败'
    disk=safe_error(OSError(errno.ENOSPC,'No space','D:/x/secret.mkv'))
    assert disk=='OSError：本机文件或系统操作失败（ENOSPC）' and 'secret' not in disk
    assert safe_error(PermissionError('D:/x'))=='PermissionError：本机文件或系统操作失败'
    con=sqlite3.connect(':memory:');con.close()
    try:con.execute('SELECT secret')
    except sqlite3.ProgrammingError as e:closed=safe_error(e)
    assert closed=='ProgrammingError：本地数据库操作失败'
    con=sqlite3.connect(':memory:');con.execute('CREATE TABLE t(x UNIQUE)');con.execute("INSERT INTO t VALUES('secret')")
    try:con.execute("INSERT INTO t VALUES('secret')")
    except sqlite3.IntegrityError as e:duplicate=safe_error(e)
    assert duplicate.startswith('IntegrityError：本地数据库操作失败（SQLITE_CONSTRAINT') and 'secret' not in duplicate


def test_busy_and_io_wording_unchanged():
    busy=sqlite3.OperationalError('database is locked');busy.sqlite_errorname='SQLITE_BUSY'
    assert safe_error(busy)=='本地数据库正忙，请稍后重试'
    io=sqlite3.OperationalError('disk I/O error');io.sqlite_errorname='SQLITE_IOERR_WRITE'
    assert safe_error(io).startswith('OperationalError：本地数据库读写失败')
