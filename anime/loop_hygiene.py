"""Windows Proactor client-reset hygiene for the serving event loop.

When a peer has already reset the connection, CPython's
_ProactorBasePipeTransport._call_connection_lost runs protocol.connection_lost() and then raises
WinError 10054 from sock.shutdown() in its finally block. That skips sock.close(),
Server._detach() and the "called" flag, and the default handler logs a full ERROR traceback.
The handler below finishes exactly those skipped steps for that one case and hands every other
exception to the default handler. It relies on private asyncio attributes; when they do not match,
it falls through to the default handler, so a future Python change at worst brings the traceback back.
"""
import logging,sys
from asyncio import proactor_events

SEND_AFTER_LOST='socket.send() raised exception.'


def _reset_transport(context):
    """The transport whose _call_connection_lost was cut short by a peer reset, or None."""
    if not isinstance(context.get('exception'),ConnectionResetError):return None
    callback=getattr(context.get('handle'),'_callback',None)
    if getattr(callback,'__func__',None) is not getattr(proactor_events._ProactorBasePipeTransport,'_call_connection_lost',False):return None
    transport=getattr(callback,'__self__',None)
    if not all(hasattr(transport,name) for name in ('_sock','_server','_called_connection_lost')):return None
    return None if transport._called_connection_lost else transport


def connection_reset_handler(loop,context):
    transport=_reset_transport(context)
    if transport is None or (transport._server is not None and not hasattr(transport._server,'_detach')):
        loop.default_exception_handler(context);return
    # connection_lost() already ran; only shutdown() failed. Finish the rest of the finally block.
    sock,transport._sock=transport._sock,None
    if sock is not None:sock.close()
    server,transport._server=transport._server,None
    if server is not None:server._detach(transport)
    transport._called_connection_lost=True


class SendAfterLostFilter(logging.Filter):
    """Drop and count asyncio's repeated warning about writes to an already-lost connection; its data is discarded anyway."""
    dropped=0

    def filter(self,record):
        if record.levelno==logging.WARNING and record.getMessage()==SEND_AFTER_LOST:
            type(self).dropped+=1;return False
        return True


def install(loop,platform=sys.platform):
    """Only the Windows Proactor loop needs the handler; other loops keep their defaults."""
    if platform=='win32':loop.set_exception_handler(connection_reset_handler)
