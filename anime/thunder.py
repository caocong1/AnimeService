"""Read-only desktop Thunder state; never writes the client's database or reads credentials."""
import contextlib,sqlite3
from pathlib import Path,PureWindowsPath
from .releases import Rejected

class ThunderReviewRequired(Rejected):
    """A trusted local validation failure; never retry or adopt automatically."""
    pass

TASK_DB=Path('C:/Program Files (x86)/Thunder Network/Thunder/Profiles/TaskDb.dat')

def clean_text(value):
    value=(value or '').rstrip('\0')
    if '\0' in value:raise ThunderReviewRequired('迅雷路径含内嵌空字符')
    return value

def relative_file(directory,name):
    parts=[clean_text(directory),clean_text(name)]
    for part in parts:
        p=PureWindowsPath(part)
        if p.is_absolute() or p.drive or p.root or '..' in p.parts:raise ThunderReviewRequired('迅雷文件路径越界')
    p=PureWindowsPath(*parts)
    if not p.name:raise ThunderReviewRequired('迅雷文件名为空')
    return p.as_posix()

class ThunderState:
    def __init__(self,path=TASK_DB):self.path=Path(path)
    @contextlib.contextmanager
    def connection(self):
        c=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
        c.row_factory=sqlite3.Row;c.execute('PRAGMA query_only=ON')
        try:yield c
        finally:c.close()
    def by_hash(self,infohash):
        if len(infohash)!=40 or any(c not in '0123456789abcdef' for c in infohash):raise ValueError('无效infohash')
        with self.connection() as c:
            ids=[r[0] for r in c.execute('SELECT TaskId FROM BtTask WHERE InfoId=?',(bytes.fromhex(infohash),))]
        return [self.task(i) for i in ids]
    def task(self,task_id):
        with self.connection() as c:
            row=c.execute('''SELECT TaskId,Name,Type,Status,SavePath,ResourceSize,TotalReceiveValidSize,
                CreationTime,CompletionTime,FailureErrorCode FROM TaskBase WHERE TaskId=?''',(int(task_id),)).fetchone()
            if row is None:return None
            result=dict(row)
            for key in ('Name','SavePath'):result[key]=clean_text(result[key])
            bt=c.execute('SELECT InfoId FROM BtTask WHERE TaskId=?',(int(task_id),)).fetchone()
            result['hash']=bytes(bt[0]).hex() if bt else None
            files=c.execute('''SELECT FileIndex,FilePath,FileName,FileSize,ReceivedSize,Download,Status
                FROM BtFile WHERE BtTaskId=? ORDER BY FileIndex''',(int(task_id),)).fetchall() if bt else []
            result['files']=[{'index':r['FileIndex'],'name':relative_file(r['FilePath'],r['FileName']),
                 'size':r['FileSize'],'received':r['ReceivedSize'],'selected':bool(r['Download']),
                 'status':r['Status']} for r in files]
            if not bt:
                result['files']=[{'index':0,'name':relative_file('',result['Name']),'size':result['ResourceSize'],
                    'received':result['TotalReceiveValidSize'],'selected':True,'status':3 if result['Status']==8 else 0}]
            return result
    def verify(self,task_id,infohash,save_path,expected_files,selected_ids):
        task=self.task(task_id)
        if not task or task['hash']!=infohash:raise ThunderReviewRequired('迅雷任务ID或hash不符')
        root=Path(save_path).resolve()
        if Path(task['SavePath']).resolve()!=root:raise ThunderReviewRequired('迅雷未采用项目指定目录')
        expected={r['index']:(r['name'].replace('\\','/'),r['size']) for r in expected_files}
        actual={r['index']:(r['name'],r['size']) for r in task['files']}
        if actual!=expected:
            # Desktop Thunder stores multi-file paths relative to TaskBase.Name,
            # whereas torrent paths include that root directory. Match the entire
            # manifest before accepting this representation; never guess per file.
            prefix=relative_file('',task['Name'])
            rooted={i:(prefix+'/'+name,size) for i,(name,size) in actual.items()}
            if rooted!=expected:raise ThunderReviewRequired('迅雷实际文件清单与已核验种子不符')
            task['files']=[{**f,'name':prefix+'/'+f['name']} for f in task['files']]
        if {r['index'] for r in task['files'] if r['selected']}!=set(selected_ids):raise ThunderReviewRequired('迅雷选集不符')
        done=task['Status']==8 and task['CompletionTime']>0
        for f in task['files']:
            if not f['selected']:continue
            p=(root/f['name']).resolve()
            if not p.is_relative_to(root) or p==root:raise ThunderReviewRequired('迅雷完成文件越界')
            done=done and f['status']==3 and f['received']>=f['size'] and p.is_file() and p.stat().st_size==f['size']
        return {**task,'verified_complete':bool(done)}
