"""Persistent publisher exclusions, independent of scores and download authorization."""
import json,re,unicodedata
from pathlib import Path

KEY='source_publisher_blacklist'


def publisher(text):
    text=re.sub(r'^\s*(?:\[(?:搬运|搬運|转载|轉載)\]\s*)+(?=\[[^\]]+\])','',str(text))
    match=re.match(r'^\s*[\[【]([^\]】]+)',text)
    return unicodedata.normalize('NFKC',match[1]).strip().casefold() if match else ''


def blacklist_blocker(entries,title,selection=None):
    titles=[title]
    if selection:
        selection=json.loads(selection) if isinstance(selection,str) else selection
        titles.extend(Path(f['name'].replace('\\','/')).name for f in selection.get('files',[])
                      if f['index'] in selection.get('ids',[]))
    names={publisher(t) for t in titles}-{''}
    for entry in entries:
        aliases={unicodedata.normalize('NFKC',a).strip().casefold() for a in entry.get('aliases',[])}
        if names & aliases:
            return '片源黑名单：'+entry['publisher']+'（'+entry['reason']+'）'
    return ''


def source_blocker(db,title,selection=None):
    return blacklist_blocker(db.get(KEY,[]),title,selection)


def media_filter(db):
    entries=db.get(KEY,[])
    tasks={t['hash']:t for t in db.rows('SELECT hash,title,selection FROM tasks')} if entries else {}
    def blocked(item):
        task=tasks.get(item.get('hash'))
        return (blacklist_blocker(entries,task['title'],task['selection']) if task else '') or blacklist_blocker(entries,Path(item['path']).name)
    return blocked


def media_blocker(db,item):
    """Keep completed media and its ledger, but never offer a blocked copy for playback."""
    task=db.one('SELECT title,selection FROM tasks WHERE hash=?',(item.get('hash'),)) if item.get('hash') else None
    return (source_blocker(db,task['title'],task['selection']) if task else '') or source_blocker(db,Path(item['path']).name)
