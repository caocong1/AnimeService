"""Observe one owned encoder; only increasing encoded counters renew its wait.

FFmpeg's -progress protocol ends each key=value record with progress=continue
or progress=end: https://ffmpeg.org/ffmpeg.html#Generic-options
No database, owner control, process lookup, lock or publication operations here.
"""
import math
from pathlib import Path
import re
import subprocess
import time


IDLE_SECONDS = 600
EXIT_SECONDS = 600
POLL_SECONDS = 5
TAIL_BYTES = 64 * 1024


class RenderStalledError(TimeoutError):
    """The encoder stopped advancing, or reported end without exiting."""


def _counter(value):
    if value and len(value) <= 19 and value.isascii() and value.isdecimal():
        number = int(value)
        if number <= 2**63 - 1:
            return number
    return None


def _timestamp(value):
    match = re.fullmatch(r'(\d{1,6}):([0-5]\d):([0-5]\d)(?:\.(\d{1,6}))?', value or '')
    if match:
        h, m, s, fraction = match.groups()
        return (int(h) * 3600 + int(m) * 60 + int(s)) * 1_000_000 + int((fraction or '').ljust(6, '0'))
    return None


def read_progress(path):
    """Read a bounded tail, ignore incomplete records and unrelated log growth."""
    try:
        with Path(path).open('rb') as stream:
            size = stream.seek(0, 2)
            offset = max(0, size - TAIL_BYTES)
            stream.seek(offset)
            data = stream.read(TAIL_BYTES)
    except FileNotFoundError:
        return {}
    if offset:
        # The tail can start inside a key/value or a record. Discard that record
        # rather than combine its remaining keys with the following sequence.
        boundary = re.search(rb'(?:^|\n)progress=(?:continue|end)\r?\n', data)
        if not boundary:
            return {}
        data = data[boundary.end():]
    record, latest = {}, {}
    for line in data.splitlines(keepends=True):
        if not line.endswith(b'\n'):
            break
        key, separator, value = line.strip().partition(b'=')
        if not separator:
            continue
        key, value = key.decode('ascii', errors='replace'), value.decode('ascii', errors='replace')
        record[key] = value
        if key == 'progress':
            if value in ('continue', 'end'):
                timestamp = _counter(record.get('out_time_us'))
                if timestamp is None:
                    timestamp = _timestamp(record.get('out_time'))
                latest = {'frame': _counter(record.get('frame')), 'out_time_us': timestamp,
                          'ended': value == 'end'}
            record = {}
    return latest


def wait_for_render(process, progress_path, outcome, *, idle_seconds=IDLE_SECONDS,
                    exit_seconds=EXIT_SECONDS, poll_seconds=POLL_SECONDS):
    """Wait past any total runtime while real frames/time continue to advance.

mtime, log bytes, speed/fps changes and repeated progress=continue do not renew
the deadline. The first progress=end starts a separate bounded exit wait; it
never proves a successful process exit or a valid finished release.
"""
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value <= 0
           for value in (idle_seconds, exit_seconds, poll_seconds)):
        raise ValueError('Invalid render watchdog bounds')
    started = last_advance = time.monotonic()
    ended_at = None
    frame = timestamp = 0
    monitor = outcome['watchdog'] = {'schema': 1, 'idle_seconds': idle_seconds,
                                     'exit_seconds': exit_seconds, 'poll_seconds': poll_seconds,
                                     'total_runtime_limit': None}
    while True:
        report = read_progress(progress_path)
        now = time.monotonic()
        new_frame = max(frame, report.get('frame') or 0)
        new_timestamp = max(timestamp, report.get('out_time_us') or 0)
        if new_frame > frame or new_timestamp > timestamp:
            last_advance = now
        frame, timestamp = new_frame, new_timestamp
        if report.get('ended') and ended_at is None:
            ended_at = now
        monitor.update(frame=frame, out_time_us=timestamp, progress_end_seen=ended_at is not None,
                       elapsed_seconds=now - started, idle_seconds_observed=now - last_advance,
                       exit_wait_seconds=now - ended_at if ended_at is not None else None)
        code = process.poll()
        if code is not None:
            return code
        remaining = (exit_seconds - (now - ended_at) if ended_at is not None
                     else idle_seconds - (now - last_advance))
        if remaining <= 0:
            reason = 'progress_end_without_exit' if ended_at is not None else 'no_encoded_progress'
            outcome.update(timeout=True, timeout_reason=reason)
            raise RenderStalledError(f'Encoder {reason}; preserve partial output and progress')
        try:
            process.wait(timeout=min(poll_seconds, remaining))
        except subprocess.TimeoutExpired:
            pass
