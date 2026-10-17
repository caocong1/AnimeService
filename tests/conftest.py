"""Even the module-level ASGI app must never open the production database."""
import os
import tempfile
from pathlib import Path

_sandbox = tempfile.TemporaryDirectory(prefix='animeservice-tests-')
os.environ['ANIMESERVICE_DB'] = str(Path(_sandbox.name) / 'import.db')
os.environ['ANIMESERVICE_START_WORKER'] = '0'
os.environ['ANIMESERVICE_DEPLOYMENT'] = str(Path(_sandbox.name) / 'deployment.json')

import shutil
import pytest

LAB_CORE = Path(os.environ.get('ANIMESERVICE_LAB_ROOT', 'D:/SubtitleSkillLab')) / 'subtitle_lab/core.py'


def pytest_collection_modifyitems(config, items):
    """Tests that need the private subtitle core or ffmpeg are skipped with a visible reason where they are absent.

    The skip is explicit so a public runner can be green for real regressions while the
    original machine, which has both, still runs everything (nothing is skipped there).
    """
    lab = pytest.mark.skip(reason=f'private subtitle core not installed at {LAB_CORE.parent.parent}')
    ffmpeg = pytest.mark.skip(reason='ffmpeg/ffprobe not on PATH')
    for item in items:
        if 'requires_lab' in item.keywords and not LAB_CORE.is_file():
            item.add_marker(lab)
        if 'requires_ffmpeg' in item.keywords and not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
            item.add_marker(ffmpeg)


@pytest.fixture(autouse=True)
def isolate_default_thunder_reader(monkeypatch):
    # Downloader routing tests must not read the user's live Thunder ledger.
    from anime.thunder import ThunderState,TASK_DB
    original=ThunderState.by_hash
    def by_hash(reader,h):
        return [] if reader.path==TASK_DB else original(reader,h)
    monkeypatch.setattr(ThunderState,'by_hash',by_hash)
