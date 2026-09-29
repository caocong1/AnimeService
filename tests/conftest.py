"""Even the module-level ASGI app must never open the production database."""
import os
import tempfile
from pathlib import Path

_sandbox = tempfile.TemporaryDirectory(prefix='animeservice-tests-')
os.environ['ANIMESERVICE_DB'] = str(Path(_sandbox.name) / 'import.db')
os.environ['ANIMESERVICE_START_WORKER'] = '0'
os.environ['ANIMESERVICE_DEPLOYMENT'] = str(Path(_sandbox.name) / 'deployment.json')
