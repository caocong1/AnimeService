"""Stop only the locally installed AnimeService danmu adapter."""
from pathlib import Path
import psutil
root=Path(__file__).resolve().parents[1]
try:
    p=psutil.Process(int((root/'data/danmu.pid').read_text()))
    if Path(p.cwd()).resolve()==(root/'vendor/danmuapi').resolve() and 'danmu_api/server.js' in p.cmdline():p.terminate()
except (OSError,ValueError,psutil.Error):pass
