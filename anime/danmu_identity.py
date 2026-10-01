"""Provider-independent work/season/episode evidence, without P-index inference."""
import re
import unicodedata
from opencc import OpenCC

_cc = OpenCC('t2s')
_number = r'[零〇一二两三四五六七八九十百\d]+'
_season = re.compile(rf'(?:第\s*)?({_number})\s*[季期]|\b(?:season\s*|s)(\d+)\b', re.I)
_bad = re.compile(r'解说|小说|漫画|预告|次回予告|一口气|剪辑|混剪|广播剧|音乐剧|小剧场|剧透|外传|番外|总集篇|\b(?:reaction|review|trailer|PV|OP|ED|OVA|OAD|SP)\b', re.I)
_chapter = re.compile(rf'第\s*{_number}\s*(?:部分|部(?!分)|篇)|\b(?:part|cour)\s*\d+\b|上半|下半|前半|后半',re.I)


def text(value):
    value = re.sub(r'\((?:\d{4}|N/A)\)【[^】]+】from \w+$', '', str(value)).strip()
    return _cc.convert(unicodedata.normalize('NFKC', value)).casefold()


def key(value):
    return ''.join(c for c in text(value) if c.isalnum())


def integer(value):
    if value.isdigit():return int(value)
    digits = {'零':0,'〇':0,'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9}
    if value in digits:return digits[value]
    if '十' in value and value.count('十') == 1:
        a,b=value.split('十')
        if (not a or a in digits) and (not b or b in digits):return digits.get(a,1)*10+digits.get(b,0)
    return None


def seasons(value):
    return [integer(m[1] or m[2]) for m in _season.finditer(text(value))] + [int(n) for n in re.findall(r'\bs(\d+)e\d+\b',text(value))]


def unsafe(value, collections=False):
    value = text(value)
    if _bad.search(value):return True
    if re.search(rf'(?:第\s*)?{_number}\s*[-+~至&]\s*{_number}\s*[季期]|\bs\d+\s*[+&-]\s*s?\d+', value):return True
    if len(set(seasons(value))) > 1:return True
    return not collections and bool(re.search(r'合集|全集|全\s*\d+\s*[集话]|\d\s*[-~至]\s*\d',value))


class Work:
    def __init__(self, names, season):
        self.names = list(dict.fromkeys(n.strip() for n in names if isinstance(n,str) and n.strip()))
        self.season = season
        self.keys = {key(n) for n in self.names}
        self.cores = {key(_season.sub('',text(n))) for n in self.names}
        self.combined_keys = {a+b for a in self.keys for b in self.keys if a!=b}
        self.chapters = {key(m[0]) for n in self.names for m in _chapter.finditer(text(n))}
        self.stems = set()
        self.queries = []
        for name in self.names:
            normalized = text(name)
            marker = _season.search(normalized)
            base = normalized[:marker.start()] if marker else normalized
            base = re.split(r'[~〜:：]',base, maxsplit=1)[0].strip(' -「」『』《》【】')
            k = key(base)
            # Full structural work name, never an arbitrary short query prefix.
            if len(k) >= (4 if re.search(r'[\u3400-\u9fff]',k) else 8):
                self.stems.add(k)
                self.queries.append(base)

    def matches(self, title, ugc=False):
        marks = seasons(title)
        if any(n != self.season for n in marks):return False
        chapters = {key(m[0]) for m in _chapter.finditer(text(title))}
        if chapters and not chapters.issubset(self.chapters):return False
        # A generic season alias cannot erase the subject's specific cour/part.
        # Source numbering for a whole season and one cour are independent.
        if self.chapters and not chapters:return False
        k = key(title)
        numbered_alias = k in self.keys and bool(re.search(rf'(?<!\d){self.season}$',text(title)))
        distinct = self.season == 1 or bool(marks) or bool(chapters) or numbered_alias or any(k.startswith(stem) and len(k)>=len(stem)+4 for stem in self.stems)
        if not ugc:
            if k in self.keys or k in self.combined_keys:return distinct
            core = key(_season.sub('',text(title)))
            return bool(marks) and core in (self.stems | self.cores)
        if unsafe(title, collections=True):return False
        full = any(len(n)>=4 and n in k for n in self.keys)
        # Existing substantial quoted aliases remain usable; names are verified locally.
        quoted = re.findall(r'[《「『【](.*?)[》」』】]',text(title))
        full = full or any(len(key(q))>=8 and any(key(q) in n for n in self.keys) for q in quoted)
        if full:return distinct
        return bool(marks) and any(n in k for n in self.stems)


def episode_number(title):
    """Parse source-written episode labels; never use upstream episodeNumber/P order."""
    value = re.sub(r'^【(?:bilibili\d*|bahamut)】\s*','',text(title))
    value = re.sub(r'\s*·\s*\d+:\d{2}(?::\d{2})?\s*$','',value).strip()
    if unsafe(value) or re.search(r'\d+\.\d+\s*[集话]|上半|下半|前半|后半',value):return None
    labels = [integer(n) for n in re.findall(rf'(?:第\s*)?({_number})\s*[集话]',value)]
    labels += [int(n) for n in re.findall(r'\b(?:episode\s*|ep\s*)(\d+)\b',value)]
    encoded = re.findall(r'\bs\d+e(\d+)\b',value)
    labels += [int(n) for n in encoded]
    if not labels:
        # A numbered part title is evidence. A generated P1 or 1/2 split is not.
        m = re.fullmatch(r'(?:第\s*)?(\d{1,3})',value)
        if not m:m = re.search(r'(?:--| - |\s)(\d{1,3})$',value)
        if m:labels=[int(m[1])]
    return labels[0] if len(labels)==1 and labels[0] and labels[0]>0 else None


def subject_names(show, mapping):
    names = [show['title'],show.get('original',''),*mapping.get('aliases',[])]
    import json
    metadata = json.loads(show.get('metadata') or '{}')
    for field in metadata.get('infobox',[]):
        if field.get('key') == '别名':
            values = field.get('value',[])
            if isinstance(values,str):names.append(values)
            elif isinstance(values,list):names.extend(x.get('v') for x in values if isinstance(x,dict))
    return names
