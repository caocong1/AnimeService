"""OS-independent checks of the Windows Proactor client-reset handler and the send-after-lost log filter."""
import asyncio,logging,socket
from asyncio import proactor_events
from unittest.mock import Mock

import pytest

from anime import loop_hygiene


class FakeSock:
    """A socket whose peer already reset: shutdown() raises WinError 10054 as on Windows."""
    def __init__(self):self.closed=False
    def fileno(self):return -1 if self.closed else 7
    def shutdown(self,how):raise ConnectionResetError(10054,'An existing connection was forcibly closed by the remote host')
    def close(self):self.closed=True


@pytest.fixture
def loop():
    loop=asyncio.new_event_loop();loop.default_exception_handler=Mock()
    yield loop
    loop.close()


class Transport(proactor_events._ProactorSocketTransport):
    """Built without __init__; the real _call_connection_lost is inherited unchanged."""
    def __del__(self):pass


def transport(sock=None,called=False):
    t=object.__new__(Transport)
    t._sock,t._server,t._called_connection_lost,t._protocol=sock or FakeSock(),Mock(),called,Mock()
    return t


def run_callback(loop,t):
    """What the loop does: run _call_connection_lost and pass its exception to the handler."""
    handle=asyncio.Handle(t._call_connection_lost,(None,),loop)
    try:t._call_connection_lost(None)
    except Exception as error:
        loop_hygiene.connection_reset_handler(loop,{'message':'Exception in callback','exception':error,'handle':handle})
        return error


def test_reset_finishes_the_skipped_cleanup(loop):
    t=transport();sock,server=t._sock,t._server
    assert isinstance(run_callback(loop,t),ConnectionResetError)
    assert t._protocol.connection_lost.called
    assert sock.closed and t._sock is None
    server._detach.assert_called_once_with(t);assert t._server is None
    assert t._called_connection_lost and not loop.default_exception_handler.called


def test_real_socket_is_closed(loop):
    a,b=socket.socketpair()
    try:
        t=transport(a);handle=asyncio.Handle(t._call_connection_lost,(None,),loop)
        loop_hygiene.connection_reset_handler(loop,{'exception':ConnectionResetError(10054,'reset'),'handle':handle})
        assert a.fileno()==-1 and not loop.default_exception_handler.called
    finally:b.close()


def test_everything_else_reaches_the_default_handler(loop):
    t=transport();lost=asyncio.Handle(t._call_connection_lost,(None,),loop)
    contexts=[
        {'exception':ConnectionResetError(10054,'reset'),'handle':asyncio.Handle(print,('x',),loop)},  # another callback
        {'exception':RuntimeError('real bug'),'handle':lost},                                            # another error
        {'exception':ConnectionResetError(10054,'reset'),'handle':asyncio.Handle(transport(called=True)._call_connection_lost,(None,),loop)},
        {'message':'Task exception was never retrieved'},                                               # no handle at all
    ]
    odd=transport();del odd._server
    contexts.append({'exception':ConnectionResetError(10054,'reset'),'handle':asyncio.Handle(odd._call_connection_lost,(None,),loop)})
    bare=transport();bare._server=object()  # a server without _detach is not touched
    contexts.append({'exception':ConnectionResetError(10054,'reset'),'handle':asyncio.Handle(bare._call_connection_lost,(None,),loop)})
    for context in contexts:loop_hygiene.connection_reset_handler(loop,context)
    assert loop.default_exception_handler.call_count==len(contexts)
    assert not t._sock.closed and not bare._sock.closed


def test_install_only_on_windows(loop):
    loop_hygiene.install(loop,platform='darwin');assert loop.get_exception_handler() is None
    loop_hygiene.install(loop,platform='win32');assert loop.get_exception_handler() is loop_hygiene.connection_reset_handler


def test_filter_drops_only_the_send_after_lost_warning():
    records=[]
    class Keep(logging.Handler):
        def emit(self,record):records.append((record.levelname,record.getMessage()))
    logger=logging.getLogger('asyncio-hygiene-test');logger.propagate=False
    keep=Keep();noise=loop_hygiene.SendAfterLostFilter();logger.addHandler(keep);logger.addFilter(noise)
    before=loop_hygiene.SendAfterLostFilter.dropped
    try:
        logger.warning('socket.send() raised exception.')
        logger.error('socket.send() raised exception.')
        logger.warning('socket.send() raised exception. extra')
        try:raise ConnectionResetError(10054,'reset')
        except ConnectionResetError:logger.exception('Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)')
    finally:logger.removeHandler(keep);logger.removeFilter(noise)
    assert loop_hygiene.SendAfterLostFilter.dropped==before+1
    assert [m for _,m in records]==['socket.send() raised exception.','socket.send() raised exception. extra',
                                    'Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)']
