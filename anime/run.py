"""Single local uvicorn process; startup supervisor handles process restarts."""
import logging,logging.handlers,os
from .db import ROOT
def main():
    import ctypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,ctypes.c_bool,ctypes.c_wchar_p];kernel.CreateMutexW.restype=ctypes.c_void_p
    mutex=kernel.CreateMutexW(None,False,'Local\\AnimeService-Worker-v1')
    if ctypes.get_last_error()==183:return
    (ROOT/'data/service.pid').write_text(str(os.getpid()))
    (ROOT/'logs').mkdir(exist_ok=True)
    handler=logging.handlers.RotatingFileHandler(ROOT/'logs/service.log',maxBytes=2_000_000,backupCount=4,encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logging.basicConfig(handlers=[handler],level=logging.INFO)
    import uvicorn
    from .deployment import load as load_deployment
    uvicorn.run('anime.app:app',host=load_deployment().get('bind_host','127.0.0.1'),port=4871,access_log=False,log_config=None,
                proxy_headers=True,forwarded_allow_ips='127.0.0.1')
if __name__=='__main__':main()
