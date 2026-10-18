"""Explicitly injected, default-off adapter. Normal production app never registers it."""
from fastapi.responses import JSONResponse


def register_playback_writer_api(app, db, writers):
    if writers.db is not db or getattr(db,'playback_writer_enabled',None) is not None:
        raise ValueError('writer adapter requires the same Store and a single trusted callback')
    # Dynamic indirection preserves feature-off if the trusted callback changes.
    db.playback_writer_enabled=lambda:writers.enabled() is True

    def ready():
        if writers.enabled() is not True:
            return JSONResponse({'error':'playback writer disabled','reason':'disabled'},status_code=503)

    def reply(result):
        if result.get('ok') is False or result.get('ignored'):
            return JSONResponse({'error':'playback writer conflict',**result,'ok':False},status_code=503 if result.get('reason')=='disabled' else 409)
        return result

    def invalid():
        return JSONResponse({'error':'invalid playback writer request','reason':'invalid_request'},status_code=400)

    @app.get('/api/playback-sessions/state')
    def state(media_key:str):
        disabled=ready()
        if disabled is not None:return disabled
        try:return {**writers.state(media_key), 'requested_media_key':media_key}
        except ValueError:return invalid()

    @app.post('/api/playback-sessions/claim')
    def claim(p:dict):
        disabled=ready()
        if disabled is not None:return disabled
        try:return reply({**writers.claim(p), 'requested_media_key':p['media_key']})
        except ValueError:return invalid()

    def update(p,action):
        disabled=ready()
        if disabled is not None:return disabled
        try:return reply(writers.update(p.get('media_key'),p,action=action))
        except ValueError:return invalid()

    @app.post('/api/playback-sessions/progress')
    def progress(p:dict):return update(p,'progress')

    @app.post('/api/playback-sessions/heartbeat')
    def heartbeat(p:dict):return update(p,'heartbeat')

    @app.post('/api/playback-sessions/release')
    def release(p:dict):return update(p,'release')
