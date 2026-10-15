"""Even the module-level ASGI app must never open the production database."""
import os
import tempfile
from pathlib import Path

_sandbox = tempfile.TemporaryDirectory(prefix='animeservice-tests-')
os.environ['ANIMESERVICE_DB'] = str(Path(_sandbox.name) / 'import.db')
os.environ['ANIMESERVICE_START_WORKER'] = '0'
os.environ['ANIMESERVICE_DEPLOYMENT'] = str(Path(_sandbox.name) / 'deployment.json')

import pytest

@pytest.fixture(autouse=True)
def isolate_default_thunder_reader(monkeypatch):
    # Downloader routing tests must not read the user's live Thunder ledger.
    from anime.thunder import ThunderState,TASK_DB
    original=ThunderState.by_hash
    def by_hash(reader,h):
        return [] if reader.path==TASK_DB else original(reader,h)
    monkeypatch.setattr(ThunderState,'by_hash',by_hash)
