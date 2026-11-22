import json
from pathlib import Path
from unittest.mock import Mock
from anime.download_storage import download_roots, library_for, stage_for, torrent_staging
from anime.qbit import Qbit


def test_new_torrents_override_both_paths_without_relocating_old_tasks(tmp_path, monkeypatch):
    config = tmp_path/'deployment.json'
    config.write_text(json.dumps({'download_drive':'C:'}), encoding='utf-8')
    monkeypatch.setenv('ANIMESERVICE_DEPLOYMENT', str(config))
    library, stage = download_roots()
    assert library == Path('C:/MediaLibrary/Anime') and stage.drive == library.drive
    old = Path('D:/MediaLibrary/Anime/show/Season 01')
    assert library_for(library, old) == Path('D:/MediaLibrary/Anime').resolve()
    assert library_for(library, Path('D:/unrelated')) == library.resolve()
    assert stage_for(stage, Path('D:/MediaDownloads/Incomplete/file')).drive == old.resolve().drive
    assert torrent_staging(old) is None
    qb = Qbit()
    qb.check_storage = Mock()
    qb.call = Mock(side_effect=[{'AnimeService':{}},'Ok.',{'AnimeService':{}},'Ok.'])
    monkeypatch.setattr(Path,'mkdir',Mock())
    for target in (library/'show',old):
        qb.add('a'*40,b'fixture',str(target))
    new, legacy = [call.kwargs['data'] for call in qb.call.call_args_list if call.args[0]=='torrents/add']
    assert Path(new['downloadPath']) == stage.parent
    assert new['savepath'] == str(library/'show') and new['useDownloadPath'] == 'true'
    assert new['stopped'] == 'true' and new['autoTMM'] == 'false'
    assert 'downloadPath' not in legacy and legacy['savepath'] == str(old)
