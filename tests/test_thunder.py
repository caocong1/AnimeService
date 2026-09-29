import sqlite3
import pytest
from anime.thunder import ThunderState,relative_file,clean_text

def test_paths_reject_escape_and_strip_legacy_terminators():
    assert relative_file('Season 1\0','E01.mkv\0')=='Season 1/E01.mkv'
    for folder,name in [('../','a.mkv'),('C:/other','a.mkv'),('','D:/a.mkv'),('','a\0b.mkv'),('','\\server\\a')]:
        with pytest.raises(ValueError):relative_file(folder,name)

def test_thunder_requires_hash_path_selection_and_real_completion(tmp_path):
    db=tmp_path/'client.db';root=tmp_path/'media';root.mkdir();h='a'*40
    with sqlite3.connect(db) as c:
        c.executescript('''CREATE TABLE TaskBase(TaskId,Name,Type,Status,SavePath,ResourceSize,
        TotalReceiveValidSize,CreationTime,CompletionTime,FailureErrorCode);
        CREATE TABLE BtTask(TaskId,InfoId);
        CREATE TABLE BtFile(BtTaskId,FileIndex,FilePath,FileName,FileSize,ReceivedSize,Download,Status);''')
        c.execute('INSERT INTO TaskBase VALUES(1,?,1,8,?,4,4,100,200,0)',('Example',str(root)))
        c.execute('INSERT INTO BtTask VALUES(1,?)',(bytes.fromhex(h),))
        c.execute('INSERT INTO BtFile VALUES(1,0,?,?,4,4,1,3)',('\0','E01.mkv\0'))
    t=ThunderState(db);files=[{'index':0,'name':'E01.mkv','size':4}]
    assert t.by_hash(h)[0]['TaskId']==1
    assert not t.verify(1,h,root,files,[0])['verified_complete']
    (root/'E01.mkv').write_bytes(b'test')
    assert t.verify(1,h,root,files,[0])['verified_complete']
    for args in [(1,'b'*40,root,files,[0]),(1,h,tmp_path,files,[0]),(1,h,root,files,[])]:
        with pytest.raises(ValueError):t.verify(*args)
    (root/'E01.mkv').write_bytes(b'bad')
    assert not t.verify(1,h,root,files,[0])['verified_complete']
    with t.connection() as c:
        with pytest.raises(sqlite3.OperationalError):c.execute('DELETE FROM TaskBase')


def test_multifile_root_and_unselected_preallocated_file(tmp_path):
    db=tmp_path/'bt.db';root=tmp_path/'media';(root/'Pack').mkdir(parents=True);h='b'*40
    with sqlite3.connect(db) as c:
        c.executescript('''CREATE TABLE TaskBase(TaskId,Name,Type,Status,SavePath,ResourceSize,
        TotalReceiveValidSize,CreationTime,CompletionTime,FailureErrorCode);
        CREATE TABLE BtTask(TaskId,InfoId);
        CREATE TABLE BtFile(BtTaskId,FileIndex,FilePath,FileName,FileSize,ReceivedSize,Download,Status);''')
        c.execute('INSERT INTO TaskBase VALUES(2,?,2,8,?,4,4,100,200,0)',('Pack',str(root)))
        c.execute('INSERT INTO BtTask VALUES(2,?)',(bytes.fromhex(h),))
        c.execute('INSERT INTO BtFile VALUES(2,0,?,?,4,0,0,0)',('','E01.mkv'))
        c.execute('INSERT INTO BtFile VALUES(2,1,?,?,4,4,1,3)',('','E02.mkv'))
    files=[{'index':i,'name':f'Pack/E0{i+1}.mkv','size':4} for i in range(2)]
    t=ThunderState(db);(root/'Pack/E02.mkv').write_bytes(b'test')
    assert t.verify(2,h,root,files,[1])['verified_complete']
    assert t.verify(2,h,root,files,[1])['files'][1]['name']=='Pack/E02.mkv'
    with sqlite3.connect(db) as c:c.execute('UPDATE TaskBase SET Status=7,CompletionTime=0')
    assert not t.verify(2,h,root,files,[1])['verified_complete']
    with sqlite3.connect(db) as c:c.execute("UPDATE TaskBase SET Name='Other'")
    with pytest.raises(ValueError):t.verify(2,h,root,files,[1])
