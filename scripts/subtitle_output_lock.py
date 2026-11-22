"""Bounded process-lifetime locks for local subtitle output only.

The file remains in place; an OS handle, never a stale PID, owns the lock.
An old publisher's marker is preserved until that publisher removes it.
"""
from contextlib import contextmanager
import errno
import json
import math
import os
from pathlib import Path
import subprocess
import time


_MARKER = b'{"schema":"subtitle-output-process-lock/v1"}\n'


class OutputBusyError(TimeoutError):
    """Output is still queued; this is not a subtitle production failure."""


def _open_exclusive(path):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # OPEN_ALWAYS, no sharing: an inherited child handle keeps exclusivity
        # even if the controller is terminated while ffmpeg is still running.
        handle = kernel.CreateFileW(str(path), 0xC0000000, 0, None, 4, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error in (32, 33):
                return None
            raise ctypes.WinError(error)
        try:
            fd = msvcrt.open_osfhandle(int(handle), os.O_BINARY | os.O_RDWR)
        except BaseException:
            kernel.CloseHandle(handle)
            raise
        return os.fdopen(fd, 'r+b')
    import fcntl
    stream = path.open('a+b')
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        stream.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise
    return stream


class _Lease:
    def __init__(self, stream):
        self.stream = stream

    @contextmanager
    def child_options(self):
        """Whitelist only this handle for the one owned encoding subprocess."""
        fd = self.stream.fileno()
        if os.name == 'nt':
            import msvcrt
            handle = msvcrt.get_osfhandle(fd)
            os.set_handle_inheritable(handle, True)
            try:
                info = subprocess.STARTUPINFO()
                info.lpAttributeList = {'handle_list': [handle]}
                yield {'startupinfo': info, 'close_fds': True}
            finally:
                os.set_handle_inheritable(handle, False)
        else:
            yield {'pass_fds': (fd,)}


@contextmanager
def output_lock(path, purpose, *, timeout=300, poll_seconds=.25, report_seconds=30):
    """Wait visibly, keep legacy/unknown markers, and release on process exit."""
    if not math.isfinite(timeout) or timeout < 0 or poll_seconds <= 0 or report_seconds <= 0:
        raise ValueError('Invalid output lock wait bounds')
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    next_report = started
    waiting = False
    while True:
        reason = 'another_output_process'
        # Old publishers close their JSON marker while still working, then
        # read/delete it to finish. Do not briefly deny sharing on that marker:
        # that could make the old owner's own cleanup fail on Windows.
        try:
            with path.open('rb') as marker_file:
                before_lock = marker_file.read(len(_MARKER) + 1)
        except FileNotFoundError:
            before_lock = b''
        except PermissionError:
            if os.name != 'nt':
                raise
            # Python's CRT may drop winerror and expose only EACCES for an
            # active share lock. CreateFile below distinguishes a real access
            # error from sharing conflict without guessing or deleting it.
            before_lock = b''
        if before_lock is not None and before_lock not in (b'', _MARKER):
            stream = None
            reason = 'legacy_or_unknown_marker_preserved'
        else:
            stream = _open_exclusive(path) if before_lock is not None else None
        if stream is not None:
            try:
                stream.seek(0)
                marker = stream.read(len(_MARKER) + 1)
                if marker in (b'', _MARKER):
                    if not marker:
                        stream.seek(0)
                        stream.write(_MARKER)
                        stream.flush()
                    break
                reason = 'legacy_or_unknown_marker_preserved'
            except BaseException:
                stream.close()
                raise
            stream.close()
        now = time.monotonic()
        if now >= next_report:
            print(json.dumps({'status': 'waiting_for_output_slot', 'purpose': purpose,
                              'reason': reason, 'elapsed_seconds': round(now - started, 1),
                              'timeout_seconds': timeout}), flush=True)
            next_report = now + report_seconds
        waiting = True
        if now - started >= timeout:
            raise OutputBusyError(f'{purpose} slot still busy ({reason}); inputs preserved, retry later')
        time.sleep(min(poll_seconds, timeout - (now - started)))
    try:
        if waiting:
            print(json.dumps({'status': 'output_slot_acquired', 'purpose': purpose,
                              'waited_seconds': round(time.monotonic() - started, 1)}), flush=True)
        yield _Lease(stream)
    finally:
        # Never unlink: waiters must all lock the same file, including after a
        # crash. Closing leaves an inherited ffmpeg handle live until it exits.
        stream.close()
