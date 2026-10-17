"""One-way personal collection export, durable revisions, read-before-write recovery."""
import json
import threading
import time
from .bangumi import BangumiClient, BangumiError, episode_map
from .bangumi_auth import TokenVault
from .net import safe_error

TYPES = {'wish': 1, 'completed': 2, 'trial': 3, 'watching': 3, 'paused': 4, 'dropped': 5}
OPTIONS = {'enabled': False, 'private_new': True, 'ratings': False, 'comments': False}


class Conflict(ValueError):
    pass


class BangumiSync:
    def __init__(self, db, vault=None, client_factory=None):
        self.db = db
        self.vault = vault or TokenVault(db.path.parent)
        self.client_factory = client_factory or BangumiClient
        self.lock = threading.RLock()
        self.wake = threading.Event()

    def options(self):
        return {**OPTIONS, **self.db.get('bangumi_sync_options', {})}

    def enqueue_all(self):
        self.db.execute('''INSERT INTO bangumi_sync_queue(show_id,revision,state,updated)
            SELECT id,1,'pending',? FROM shows WHERE selected=1
            ON CONFLICT(show_id) DO UPDATE SET revision=revision+1,state='pending',error='',next_attempt=0,updated=excluded.updated''', (time.time(),))
        self.wake.set()

    def connect(self, token):
        if not isinstance(token, str) or not 10 <= len(token.strip()) <= 4096 or any(c.isspace() for c in token.strip()):
            raise ValueError('请输入有效的 Bangumi Access Token')
        with self.lock:
            client = self.client_factory(token.strip())
            me = client.me()
            uid, username = me.get('id'), me.get('username')
            if type(uid) is not int or uid <= 0 or not isinstance(username, str) or not username:
                raise ValueError('Bangumi 账号核验失败')
            prior = self.db.get('bangumi_sync_account')
            if prior and prior['id'] != uid:
                raise ValueError('这是另一个 Bangumi 账号；请先断开原账号再连接，避免混合记录')
            self.vault.save({'token': token.strip(), 'id': uid, 'username': username})
            self.db.set('bangumi_sync_account', {'id': uid, 'username': username, 'nickname': me.get('nickname', username)})
            self.db.set('bangumi_sync_health', {'ok': True, 'connected': time.time()})
            self.enqueue_all()
            return self.status()

    def configure(self, payload):
        if set(payload) - set(OPTIONS) or any(type(v) is not bool for v in payload.values()):
            raise ValueError('无效 Bangumi 同步设置')
        with self.lock:
            opts = {**self.options(), **payload}
            if opts['enabled'] and (not self.db.get('bangumi_sync_account') or not self.vault.read()):
                raise ValueError('请先在本机连接 Bangumi 账号')
            self.db.set('bangumi_sync_options', opts)
            self.enqueue_all()
        return self.status()

    def disconnect(self):
        with self.lock:
            self.db.set('bangumi_sync_options', {**self.options(), 'enabled': False})
            self.vault.clear()
            self.db.set('bangumi_sync_account', None)
            self.db.set('bangumi_sync_health', {})
        return self.status()

    def status(self):
        counts = {r['state']: r['n'] for r in self.db.rows('SELECT q.state,count(*) n FROM bangumi_sync_queue q JOIN shows s ON s.id=q.show_id WHERE s.selected=1 GROUP BY q.state')}
        rows = self.db.rows('''SELECT q.show_id,s.title,s.state local_state,
            (SELECT count(*) FROM watches w WHERE w.show_id=s.id AND w.finished=1) watched,q.state,q.error,q.updated,q.last_success
            FROM bangumi_sync_queue q JOIN shows s ON s.id=q.show_id WHERE s.selected=1
            ORDER BY CASE q.state WHEN 'conflict' THEN 0 WHEN 'blocked' THEN 1 WHEN 'error' THEN 2 WHEN 'pending' THEN 3 ELSE 4 END,q.updated DESC LIMIT 100''')
        scope = self.db.one('SELECT count(*) n FROM shows WHERE selected=1')['n']
        return {'account': self.db.get('bangumi_sync_account'), 'options': self.options(),
                'health': self.db.get('bangumi_sync_health', {}), 'counts': counts, 'items': rows,
                'scope_count': scope, 'interval': 5, 'direction': 'local_to_bangumi'}

    def retry(self, sid=None, resolve=False):
        with self.lock:
            if resolve:
                if type(sid) is not int or not self.db.show(sid):
                    raise ValueError('请选择具体作品确认使用本地记录')
                account = self.db.get('bangumi_sync_account')
                if not account:
                    raise ValueError('请先连接 Bangumi')
            if sid is None:
                self.db.execute("UPDATE bangumi_sync_queue SET state='pending',next_attempt=0,error='' WHERE state IN ('error','blocked')")
            else:
                self.db.execute("UPDATE bangumi_sync_queue SET revision=revision+1,resolve_revision=CASE WHEN ? THEN revision+1 ELSE 0 END,state='pending',next_attempt=0,error='' WHERE show_id=?", (resolve,sid))
            self.wake.set()
        return self.status()

    def _snapshot(self, sid, opts):
        with self.db.connect() as c:
            show = c.execute('SELECT * FROM shows WHERE id=?', (sid,)).fetchone()
            watches = [dict(r) for r in c.execute('SELECT episode,finished,method FROM watches WHERE show_id=? ORDER BY episode', (sid,))]
        if not show or not show['selected']:
            return None
        body = {'type': TYPES[show['state']]}
        if opts['ratings']:
            if show['rating'] is not None:
                body['rate'] = show['rating']
        if opts['comments'] and show['feedback']:
            body['comment'] = show['feedback']
        return {'body': body, 'watches': watches}

    def _receipt(self, uid, sid, body, episodes, intents=None):
        stored = self.db.one('SELECT payload FROM bangumi_sync_receipts WHERE account_id=? AND show_id=?', (uid, sid))
        if stored:
            # Disabling a field does not erase its ownership or recovery history.
            body = {**json.loads(stored['payload'])['body'], **body}
        self.db.execute('''INSERT INTO bangumi_sync_receipts(account_id,show_id,payload) VALUES(?,?,?)
            ON CONFLICT(account_id,show_id) DO UPDATE SET payload=excluded.payload''',
            (uid, sid, json.dumps({'body': body, 'episodes': episodes, 'intents': intents or {}}, ensure_ascii=False)))

    def _sync(self, client, account, job, opts):
        sid = job['show_id']
        snapshot = self._snapshot(sid, opts)
        if snapshot is None:
            return 'skipped'
        body = snapshot['body']
        prior = self.db.one('SELECT payload FROM bangumi_sync_receipts WHERE account_id=? AND show_id=?', (account['id'], sid))
        receipt = json.loads(prior['payload']) if prior else {'body': {}, 'episodes': {}}
        intents = receipt.get('intents', {})
        if opts['ratings'] and ('rate' in receipt['body'] or 'rate' in intents.get('body', {})) and 'rate' not in body:
            body['rate'] = 0
        if opts['comments'] and ('comment' in receipt['body'] or 'comment' in intents.get('body', {})) and 'comment' not in body:
            body['comment'] = ''
        resolved = job['resolve_revision'] == job['revision']
        subject = client.request('GET', f'/v0/subjects/{sid}')
        if subject.get('id') != sid or subject.get('type') != 2:
            raise ValueError('Bangumi 作品身份未核验为相同动画条目')
        remote = client.collection(account['username'], sid)
        if remote:
            if remote.get('subject_id') != sid or remote.get('subject_type') != 2:
                raise ValueError('Bangumi 收藏作品身份不匹配')
            for key, old in receipt['body'].items():
                allowed = [old, body.get(key)] + intents.get('body', {}).get(key, [])
                if not resolved and key in body and remote.get(key) not in allowed:
                    raise Conflict('Bangumi 收藏已在外部修改；请核对后选择使用本地记录')
            for key, allowed in intents.get('body', {}).items():
                if not resolved and key in body and remote.get(key) not in allowed + [body[key]]:
                    raise Conflict('Bangumi 未确认提交后收藏发生外部修改，请核对')
        elif receipt['body'] and not resolved:
            raise Conflict('Bangumi 收藏已在外部移除；请核对后选择使用本地记录')
        changed = {k: v for k, v in body.items() if remote is None or remote.get(k) != v}
        if remote is None:
            changed['private'] = opts['private_new']
        # Check the revision after remote reads and immediately before mutation.
        if self.db.one('SELECT revision FROM bangumi_sync_queue WHERE show_id=?', (sid,))['revision'] != job['revision']:
            return 'pending'
        if changed:
            intents['body'] = {k: [remote.get(k) if remote else None, v] for k, v in changed.items() if k in body}
            self._receipt(account['id'], sid, receipt['body'], receipt['episodes'], intents)
            client.write_collection(sid, changed, exists=remote is not None)
            check = client.collection(account['username'], sid)
            if not check or any(check.get(k) != v for k, v in changed.items()):
                raise BangumiError('Bangumi 收藏写入后未能确认，稍后先回读再重试')
        intents.pop('body', None)
        self._receipt(account['id'], sid, body, receipt['episodes'], intents)
        if not snapshot['watches']:
            return 'synced'
        cache = self.db.get('bangumi_episodes_' + str(sid), {})
        if time.time() - cache.get('time', 0) < 21600:
            episodes = cache['items']
        else:
            episodes = client.episodes(sid)
            self.db.set('bangumi_episodes_' + str(sid), {'time': time.time(), 'items': episodes})
        mapping = episode_map(self.db, sid, episodes)
        if time.time() - cache.get('time', 0) < 21600 and any(w['finished'] and w['episode'] not in mapping for w in snapshot['watches']):
            # A newly watched episode must not wait six hours for an old catalog cache.
            episodes = client.episodes(sid)
            self.db.set('bangumi_episodes_' + str(sid), {'time': time.time(), 'items': episodes})
            mapping = episode_map(self.db, sid, episodes)
        wanted = {}
        for watch in snapshot['watches']:
            eid = mapping.get(watch['episode'])
            if not eid:
                if watch['finished']:
                    raise ValueError(f'本地第 {watch["episode"]} 集缺少唯一 Bangumi 正片章节，等待编号核验')
                continue
            if watch['finished']:
                wanted[str(eid)] = 2
            elif watch['method'] == 'manual' and (str(eid) in receipt['episodes'] or str(eid) in intents.get('episodes', {})):
                # Explicit local corrections can undo only progress previously managed here.
                wanted[str(eid)] = 0
        remote_eps = {str(r['episode']['id']): r['type'] for r in client.episode_collections(sid)}
        for eid, value in wanted.items():
            allowed = [receipt['episodes'].get(eid, remote_eps.get(eid, 0)), value] + intents.get('episodes', {}).get(eid, [])
            if not resolved and remote_eps.get(eid, 0) not in allowed:
                raise Conflict('Bangumi 分集记录已在外部修改；请核对后选择使用本地记录')
        wrote_episodes = False
        for value in (2, 0):
            ids = [int(eid) for eid, status in wanted.items() if status == value and remote_eps.get(eid, 0) != status]
            for start in range(0, len(ids), 100):
                if self.db.one('SELECT revision FROM bangumi_sync_queue WHERE show_id=?', (sid,))['revision'] != job['revision']:
                    return 'pending'
                intents.setdefault('episodes', {}).update({str(eid): [remote_eps.get(str(eid), 0), value] for eid in ids[start:start + 100]})
                self._receipt(account['id'], sid, body, receipt['episodes'], intents)
                client.write_episodes(sid, ids[start:start + 100], value)
                wrote_episodes = True
        check = {str(r['episode']['id']): r['type'] for r in client.episode_collections(sid)} if wanted else remote_eps
        if any(check.get(eid, 0) != value for eid, value in wanted.items()):
            raise BangumiError('Bangumi 分集写入后未能确认，稍后先回读再重试')
        # Episode endpoints may recalculate collection completion. Keep the explicit local state.
        collection_check = client.collection(account['username'], sid)
        if not collection_check:
            raise BangumiError('Bangumi 分集写入后收藏无法回读，保留队列')
        if collection_check.get('type') != body['type']:
            if self.db.one('SELECT revision FROM bangumi_sync_queue WHERE show_id=?', (sid,))['revision'] != job['revision']:
                return 'pending'
            if not wrote_episodes:
                raise Conflict('Bangumi 收藏状态发生外部修改，请核对')
            self._receipt(account['id'], sid, body, {**receipt['episodes'], **wanted},
                          {'body': {'type': [collection_check.get('type'), body['type']]}})
            client.write_collection(sid, {'type': body['type']}, exists=True)
            if client.collection(account['username'], sid).get('type') != body['type']:
                raise BangumiError('Bangumi 收藏状态回读未确认，保留队列')
        self._receipt(account['id'], sid, body, {**receipt['episodes'], **wanted})
        return 'synced'

    def tick(self):
        with self.lock:
            opts = self.options()
            account = self.db.get('bangumi_sync_account')
            if not opts['enabled'] or not account:
                return
            health = self.db.get('bangumi_sync_health', {})
            if health.get('auth_required') or time.time() < health.get('next_attempt', 0):
                return
            try:
                credentials = self.vault.read()
                if not credentials or credentials['id'] != account['id']:
                    raise ValueError('Bangumi 授权与账号不匹配，请重新连接')
            except Exception as error:  # only a credential problem asks for a new login
                self.db.set('bangumi_sync_health', {'ok': False, 'auth_required': True,
                    'error': str(error) if isinstance(error, ValueError) else 'Bangumi 本机授权不可用，请重新连接'})
                return
            try:
                client = self.client_factory(credentials['token'])
                for job in self.db.rows("SELECT * FROM bangumi_sync_queue WHERE state IN ('pending','error') AND next_attempt<=? ORDER BY updated DESC,show_id LIMIT 4", (time.time(),)):
                    try:
                        state = self._sync(client, account, job, opts)
                        self.db.execute('''UPDATE bangumi_sync_queue SET state=?,error='',failures=0,next_attempt=0,
                            last_success=CASE WHEN ?='synced' THEN ? ELSE last_success END WHERE show_id=? AND revision=?''',
                            (state, state, time.time(), job['show_id'], job['revision']))
                        self.db.set('bangumi_sync_health', {'ok': True, 'success': time.time()})
                    except Exception as e:
                        if isinstance(e, Conflict):
                            state, message = 'conflict', str(e)
                        elif isinstance(e, ValueError):
                            state, message = 'blocked', str(e)
                        else:
                            state = 'error'
                            message = str(e) if isinstance(e, BangumiError) else 'Bangumi 同步暂时失败，保留队列自动重试'
                        delay = max(min(3600, 30 * 2 ** min(job['failures'], 6)), getattr(e, 'retry_after', 0))
                        self.db.execute('''UPDATE bangumi_sync_queue SET state=?,error=?,failures=failures+1,next_attempt=?
                            WHERE show_id=? AND revision=?''', (state, message, time.time() + delay, job['show_id'], job['revision']))
                        if isinstance(e, BangumiError) and e.status in (401, 403, 429):
                            self.db.set('bangumi_sync_health', {'ok': False, 'error': message,
                                'auth_required': e.status in (401, 403), 'next_attempt': time.time() + delay})
                            break
            except Exception as error:
                # A busy database, a client that cannot be built or an unexpected shape is not a
                # revoked login: back off (a minute doubling to an hour) and keep the queue.
                failures = (health.get('failures', 0) if not health.get('ok') else 0) + 1
                self.db.set('bangumi_sync_health', {'ok': False, 'auth_required': False, 'error': safe_error(error),
                    'failures': failures, 'next_attempt': time.time() + min(3600, 60 * 2 ** min(failures - 1, 6))})

    def run(self, stop):
        while not stop.is_set():
            self.tick()
            self.wake.wait(5)
            self.wake.clear()


def register_bangumi(app, db):
    from fastapi import Request, HTTPException
    sync = BangumiSync(db)
    app.state.bangumi_sync = sync

    def local(request):
        if not request.state.local:
            raise HTTPException(403, 'Bangumi 账号和同步设置请在家中电脑管理')

    @app.get('/api/bangumi-sync')
    def status():
        return sync.status()

    @app.post('/api/bangumi-sync/connect')
    def connect(request: Request, payload: dict):
        local(request)
        try:
            return sync.connect(payload.get('access_token'))
        except BangumiError as e:
            raise ValueError(str(e)) from None

    @app.post('/api/bangumi-sync/settings')
    def settings(request: Request, payload: dict):
        local(request)
        return sync.configure(payload)

    @app.post('/api/bangumi-sync/disconnect')
    def disconnect(request: Request):
        local(request)
        return sync.disconnect()

    @app.post('/api/bangumi-sync/retry')
    def retry(request: Request, payload: dict):
        local(request)
        if payload.get('resolve') and payload.get('confirm') != '使用本地记录':
            raise ValueError('请明确确认使用本地记录')
        return sync.retry(payload.get('show_id'), payload.get('resolve') is True)
