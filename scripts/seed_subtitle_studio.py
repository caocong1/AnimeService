"""Explicit local archival bootstrap, without submitting or controlling downloads."""
import argparse,json,sqlite3,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from anime.db import Store
from anime.engine import Engine
from anime.subtitle_studio import archive_for


def main():
    p=argparse.ArgumentParser();p.add_argument('--enable',action='store_true',required=True)
    p.add_argument('--limit',type=int,default=200);args=p.parse_args()
    root=Path(__file__).resolve().parents[1];path=root/'data/anime.db'
    backup=path.parent/('anime-before-subtitle-studio-'+time.strftime('%Y%m%d-%H%M%S')+'.sqlite3')
    with sqlite3.connect(path) as src,sqlite3.connect(backup) as dst:src.backup(dst)
    db=Store(path);db.set('subtitle_studio_enabled',True)
    engine=Engine(db);studio=engine.subtitle_studio;results=[]
    # Do NOT call cycle, discover, dispatch, maintenance, reconcile or downloader APIs here.
    for item in studio.media()[:args.limit]:
        prior=db.one('SELECT payload FROM subtitle_studio_media WHERE fingerprint=?',(item['fingerprint'],))
        if prior and json.loads(prior['payload']).get('inventory_revision')==item['inventory_revision']:continue
        try:
            result=studio.collect(item)
            record={'show_id':item['show_id'],'episode':item['episode'],'candidate_id':item.get('candidate_id'),
                    'bases':len(result['draft_ids']),'warnings':result['errors'],'ok':True}
        except Exception as ex:
            record={'show_id':item['show_id'],'episode':item['episode'],'ok':False,'error':str(ex)[:300]}
        results.append(record);print(json.dumps(record,ensure_ascii=False),flush=True)
    summary=archive_for(db).summary()
    report={'time':time.time(),'purpose':'archive-only; no downloader submissions','backup':str(backup),
            'results':results,'summary':summary}
    out=studio.root/'bootstrap-report.json';out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'summary':summary,'report':str(out)},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
