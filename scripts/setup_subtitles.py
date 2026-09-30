"""Install pinned local ASS renderer and its redistributable CJK fallback font."""
import hashlib
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT_SHA256 = '2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b'
BASE = 'https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/Sans/'


def install():
    target = ROOT / 'static/vendor/libass'
    target.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / 'node_modules/libass-wasm/dist/js').iterdir():
        if source.is_file():
            shutil.copyfile(source, target / source.name)
    font = target / 'NotoSansCJKsc-Regular.otf'
    if not font.exists() or hashlib.sha256(font.read_bytes()).hexdigest() != FONT_SHA256:
        with urllib.request.urlopen(BASE + 'OTF/SimplifiedChinese/' + font.name, timeout=120) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != FONT_SHA256:
            raise RuntimeError('Unexpected CJK font checksum')
        font.write_bytes(data)
    with urllib.request.urlopen(BASE + '../LICENSE', timeout=30) as r:
        (target / 'NOTO-LICENSE.txt').write_bytes(r.read())


if __name__ == '__main__':
    install()
