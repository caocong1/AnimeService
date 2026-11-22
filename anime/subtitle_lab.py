"""Load the independent subtitle core without importing the web application.

Read-only schedulers and recovery guards need parsing, fingerprinting and archive
keys, but must not require FastAPI or the rest of the HTTP service. Keep this
module dependency-light; production web modules may re-export ``lab_core``.
"""
import importlib.util
from pathlib import Path
import sys


def lab_core():
    name = '_anime_subtitle_lab_core'
    if name not in sys.modules:
        path = Path('D:/SubtitleSkillLab/subtitle_lab/core.py')
        if not path.is_file():
            raise ValueError('字幕研究项目未安装')
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]
