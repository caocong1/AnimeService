"""Use one TimeRanges snapshot in the pinned Artplayer loadedTime getter."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = 'get:()=>e.buffered.length?e.buffered.end(e.buffered.length-1):0'
NEW = 'get:()=>{const t=e.buffered;return t.length?t.end(t.length-1):0}'


def patch(path=None):
    path = Path(path) if path is not None else ROOT / 'static/vendor/artplayer.js'
    source = path.read_text(encoding='utf-8')
    if source.count(NEW) == 1 and OLD not in source:
        return
    if source.count(OLD) != 1 or NEW in source:
        raise RuntimeError('Pinned Artplayer changed; review buffered snapshot patch')
    # HTMLMediaElement.buffered returns a new static snapshot on every access.
    # Seeking can change its range count between separate getter calls.
    path.write_text(source.replace(OLD, NEW, 1), encoding='utf-8')


if __name__ == '__main__':
    patch()
