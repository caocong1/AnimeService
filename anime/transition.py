import json
from pathlib import Path
from fastapi.responses import FileResponse
from .db import ROOT

def register_transition(app,db,engine):
    @app.get('/transition')
    def page():return FileResponse(ROOT/'static/transition.html')
    @app.get('/api/transition')
    def status():
        report=db.get('transition',{'shows':[]})
        inventory=Path(db.path.parent/'inventory.json')
        files=json.loads(inventory.read_text(encoding='utf-8')) if inventory.exists() else []
        rows=[]
        for x in report['shows']:
            sid=x.get('id');s=db.show(sid)
            if not s:continue
            eps=db.rows('SELECT episode,status FROM episodes WHERE show_id=? ORDER BY episode',(sid,))
            rows.append({**x,'state':s['state'],'enabled':bool(s['authorized']),
                'episodes':eps,'missing':engine.gaps(sid)['missing'],
                'unwatched':sum(e['status']=='complete' and not db.one('SELECT 1 FROM watches WHERE show_id=? AND episode=? AND finished=1',(sid,e['episode'])) for e in eps)})
        return {**report,'shows':rows,'files':len(files),'verified':sum(x.get('verified_complete',False) for x in files),
            'legacy_owner':db.get('legacy_rss_transition',{}),'desktop':db.get('desktop_health',{}),
            'note':'旧文件原位接入；不搬移、不重下、不参与自动清理。下载完成与看完分开记录。'}
