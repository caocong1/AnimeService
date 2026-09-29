"""Stop only this project's HTTPS master and its children."""
from pathlib import Path
import psutil
ROOT=Path(__file__).resolve().parents[1]
try:
    process=psutil.Process(int((ROOT/'data/edge.pid').read_text()))
    if Path(process.exe()).resolve()!=ROOT/'data/tools/nginx.exe' or Path(process.cwd()).resolve()!=ROOT/'data/edge':
        raise RuntimeError('HTTPS process ownership mismatch')
    for child in process.children(recursive=True):child.terminate()
    process.terminate()
except (FileNotFoundError,psutil.NoSuchProcess):pass
