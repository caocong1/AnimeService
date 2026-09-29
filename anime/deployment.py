"""Machine-specific access settings stay outside the public repository."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load():
    path = Path(os.environ.get('ANIMESERVICE_DEPLOYMENT', ROOT / 'data/deployment.json'))
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
