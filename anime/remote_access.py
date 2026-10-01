"""Private-device access. Pairing can only be started from the loopback UI."""
import hashlib,secrets,threading,time
from fastapi import HTTPException,Request
from fastapi.responses import JSONResponse,FileResponse
from .db import ROOT

COOKIE='anime_device'
# A TV box sits in someone else's living room; re-pairing it monthly is impractical.
SESSION_DAYS={'tv':365}
class RemoteAccess:
    def __init__(self,db):
        self.db=db;self.lock=threading.Lock();self.code=None;self.expires=0;self.attempts=[]
        db.execute('CREATE TABLE IF NOT EXISTS remote_sessions(digest TEXT PRIMARY KEY, expires REAL, created REAL)')
    @staticmethod
    def digest(value):return hashlib.sha256(value.encode()).hexdigest()
    def valid(self,request):
        value=request.cookies.get(COOKIE,'')
        return bool(value and self.db.one('SELECT 1 FROM remote_sessions WHERE digest=? AND expires>?',(self.digest(value),time.time())))
    def issue_pair(self):
        with self.lock:
            code=secrets.token_hex(6).upper();self.code=self.digest(code);self.expires=time.time()+600
        return {'code':code,'expires':self.expires}
    def exchange(self,value,days=30):
        now=time.time()
        with self.lock:
            self.attempts=[t for t in self.attempts if now-t<600]
            if len(self.attempts)>=12:raise HTTPException(429,'尝试过多，请十分钟后重试')
            self.attempts.append(now)
            value=str(value).replace(' ','').replace('-','').upper()
            if not self.code or now>=self.expires or not secrets.compare_digest(self.code,self.digest(value)):
                raise HTTPException(401,'配对码无效或已过期，请在家中电脑重新生成')
            self.code=None;self.expires=0
        session=secrets.token_urlsafe(48)
        self.db.execute('DELETE FROM remote_sessions WHERE expires<?',(now,))
        self.db.execute('INSERT INTO remote_sessions VALUES(?,?,?)',(self.digest(session),now+days*86400,now))
        return session

def register_access(app,access):
    @app.get('/login')
    def login_page():return FileResponse(ROOT/'static/login.html')
    @app.post('/api/access/pair')
    def pair(request:Request):
        if not request.state.local:raise HTTPException(403,'请在家中电脑生成配对码')
        return access.issue_pair()
    @app.post('/api/access/login')
    def login(payload:dict,request:Request):
        days=SESSION_DAYS.get(payload.get('device'),30)
        session=access.exchange(payload.get('code',''),days)
        response=JSONResponse({'ok':True})
        response.set_cookie(COOKIE,session,max_age=days*86400,secure=not request.state.lan,httponly=True,samesite='strict')
        return response
    @app.post('/api/access/logout')
    def logout(request:Request):
        access.db.execute('DELETE FROM remote_sessions WHERE digest=?',(access.digest(request.cookies.get(COOKIE,'')),))
        response=JSONResponse({'ok':True});response.delete_cookie(COOKIE,secure=not request.state.lan,httponly=True,samesite='strict');return response
    @app.post('/api/access/revoke')
    def revoke(request:Request):
        if not request.state.local:raise HTTPException(403,'请在家中电脑管理设备')
        access.db.execute('DELETE FROM remote_sessions');return {'ok':True}
