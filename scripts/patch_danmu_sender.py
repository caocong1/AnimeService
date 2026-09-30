"""Keep the anonymous sender hash on each comment so the player can block by sender."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKER = '// AnimeService: sender hash.'


def replace_once(path, replacements):
    source = path.read_text(encoding='utf-8')
    if MARKER in source:
        return
    for old, new in replacements:
        if source.count(old) != 1:
            raise RuntimeError(f'Pinned adapter changed ({path.name}); review patch before applying')
        source = source.replace(old, new, 1)
    path.write_text(MARKER + '\n' + source, encoding='utf-8')


def patch():
    base = ROOT / 'vendor/danmuapi/danmu_api'
    # Bilibili protobuf carries midHash; Bahamut carries userid. Others send none.
    replace_once(base / 'utils/danmu-util.js', [
        ('const danmu = { p: attributes, m, cid: cidCounter++, like: item?.like };',
         'const danmu = { p: attributes, m, cid: cidCounter++, like: item?.like };\n'
         '    const sender = item.midHash || item.sender;\n'
         '    if (sender) danmu.sender = String(sender);'),
        # A merged "text x N" row keeps a sender only when one sender wrote all of it.
        ('          sources: new Set() // 收集当前具体弹幕内容的真实独立来源\n',
         '          sources: new Set(), // 收集当前具体弹幕内容的真实独立来源\n'
         '          senders: new Set()\n'),
        ('      acc[message].count += 1;\n',
         '      acc[message].count += 1;\n'
         '      acc[message].senders.add(danmu.sender);\n'),
        ('        ...(data.color_v2 !== undefined ? { color_v2: data.color_v2 } : {})\n',
         '        ...(data.color_v2 !== undefined ? { color_v2: data.color_v2 } : {}),\n'
         '        ...(data.senders.size === 1 && [...data.senders][0] ? { sender: [...data.senders][0] } : {})\n'),
    ])
    replace_once(base / 'sources/bahamut.js', [
        ('      m: c.text,\n      t: c.time / 10\n',
         '      m: c.text,\n      t: c.time / 10,\n      sender: c.userid\n'),
    ])


if __name__ == '__main__':
    patch()
