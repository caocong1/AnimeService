"""User source-fidelity preference; declared processing is not native-detail proof."""
import json,re
from .source_blacklist import source_blocker


def processing_flags(text):
    flags=[]
    if re.search(r'(?i)(?<![a-z])AI[\s_-]*(?:\d{3,4}p|[248]k|超分|修复|修復|增强|增強|upscal|enhanc)|upscal(?:e|ed|ing)?|超分|超解析|超清修复|高清修复',text):
        flags.append('超分/画面修复')
    if re.search(r'(?i)补帧|補幀|補帧|插帧|插幀|frame[ _-]*interpolat|(?<![a-z])(?:RIFE|SVP|DAIN)(?![a-z])',text):
        flags.append('补帧')
    if re.search(r'(?i)AI[\s_-]*(?:去噪|降噪|锐化|銳化|denois|sharpen)|Topaz[ _-]*(?:Video|AI)|Video[ _-]*Enhance[ _-]*AI',text):
        flags.append('AI画面增强')
    return flags


def fidelity_blocker(db,title,selection=None):
    return source_blocker(db,title,selection) or preference_blocker(db.get('source_fidelity',''),title,selection)


def preference_blocker(preference,title,selection=None):
    if preference!='original':return ''
    flags=processing_flags(title)
    if selection:
        sel=json.loads(selection) if isinstance(selection,str) else selection
        for f in sel.get('files',[]):
            if f['index'] in sel.get('ids',[]):flags.extend(processing_flags(f['name']))
    if flags:return '原片保真偏好：排除'+ '、'.join(dict.fromkeys(flags)) +'版本'
    return ''
