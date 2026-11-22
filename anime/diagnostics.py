"""Best-effort diagnostic writes, never a boundary for business operations."""
import logging
import sqlite3

from .net import safe_error


_LOG = logging.getLogger(__name__)


def _error_details(error):
    if error is None:
        return None
    details = {'type': type(error).__name__, 'message': safe_error(error)}
    if isinstance(error, sqlite3.Error):
        for name in ('sqlite_errorcode', 'sqlite_errorname'):
            value = getattr(error, name, None)
            if value is not None:
                details[name] = value
    return details


def _recording_failed(destination, message, error, recording_error):
    try:
        # Do not use exc_info: raw exception strings can contain credentials.
        # This fallback never writes to the database or retries its own failure.
        _LOG.error(
            'diagnostic_recording_failed destination=%s message=%s '
            'operation_error=%s recording_error=%s',
            destination, message, _error_details(error), _error_details(recording_error),
        )
    except Exception:
        pass


def record_event(db, scope, message, level='info', *, error=None):
    """Attempt one diagnostic event; the result says nothing about business success."""
    try:
        db.event(scope, message, level)
    except Exception as recording_error:
        _recording_failed('event:' + scope, message, error, recording_error)
        return False
    return True


def record_kv(db, key, value, *, error=None):
    """Attempt one diagnostic kv memo; a failed write must not stop the caller's loop."""
    try:
        db.set(key, value)
    except Exception as recording_error:
        _recording_failed('kv:' + key, str(value.get('error', '')), error, recording_error)
        return False
    return True


def record_subtitle_health(db, health, *, error=None):
    """Only the archive's diagnostic health key may fail independently."""
    try:
        db.set('subtitle_studio_health', health)
    except Exception as recording_error:
        _recording_failed('kv:subtitle_studio_health', '字幕归档健康记录写入失败', error, recording_error)
        return False
    return True
