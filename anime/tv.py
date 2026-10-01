"""Android TV client updates: a published APK and its release.json live in data/tv/."""
import json
from fastapi import HTTPException
from fastapi.responses import FileResponse

APK='fanyu-tv.apk'


def release_info(folder):
    """The published release, or {} when none is published or it is incomplete."""
    try:info=json.loads((folder/'release.json').read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}
    apk=folder/APK
    if not isinstance(info,dict) or not isinstance(info.get('version_code'),int) or not apk.is_file():return {}
    if info.get('size')!=apk.stat().st_size:return {}
    notes=info.get('notes') if isinstance(info.get('notes'),list) else []
    return {'version_code':info['version_code'],'version_name':str(info.get('version_name',''))[:40],
            'notes':[str(n)[:200] for n in notes[:20]],'size':info['size'],'sha256':str(info.get('sha256',''))[:64],
            'url':'/api/tv/release.apk'}


def register_tv(app,db):
    folder=db.path.parent/'tv'
    @app.get('/api/tv/release')
    def release():return release_info(folder)
    @app.get('/api/tv/release.apk')
    def apk():
        if not release_info(folder):raise HTTPException(404,'没有已发布的 TV 版本')
        return FileResponse(folder/APK,media_type='application/vnd.android.package-archive',filename=APK)
