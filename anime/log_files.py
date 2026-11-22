"""Bounded append logs for long-lived helper processes (danmu adapter, HTTPS listener)."""
import os

LIMIT=8*1024*1024   # rotate a child log at spawn once it is larger than this
KEEP=1024*1024      # the previous generation keeps only this much of the old log's tail


def bound_log(path,limit=LIMIT,keep=KEEP):
    """Before a new child starts: move the tail of an oversized log to <name>.1 and start afresh.

    Windows will not rename or delete a file another process still holds open; a surviving
    holder leaves the log as it is (OSError), and the spawn goes ahead either way.
    """
    try:
        size=path.stat().st_size
        if size<=limit:return False
        with path.open('rb') as source:
            source.seek(max(0,size-keep));tail=source.read()
        previous=path.with_name(path.name+'.1');temporary=path.with_name(path.name+'.1.tmp')
        temporary.write_bytes(tail);os.replace(temporary,previous)
        path.unlink();return True
    except OSError:return False
