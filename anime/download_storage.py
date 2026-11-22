"""Media roots for new writes and exact, ledger-bound legacy reads."""
from pathlib import Path
from .deployment import load

OLD_LIBRARY = Path('D:/MediaLibrary/Anime')
OLD_STAGE = Path('D:/MediaDownloads/Incomplete/AnimeService')


def download_roots():
    drive = load().get('download_drive', 'D:')
    if drive not in ('C:', 'D:'):
        raise ValueError('Invalid AnimeService download drive')
    return Path(drive + '/MediaLibrary/Anime'), Path(drive + '/MediaDownloads/Incomplete/AnimeService')


def library_for(active, path):
    """Only the original and newly configured library may contain managed files."""
    active, path = Path(active).resolve(), Path(path).resolve()
    if active == Path('C:/MediaLibrary/Anime').resolve() and path.is_relative_to(OLD_LIBRARY.resolve()):
        return OLD_LIBRARY.resolve()
    return active


def stage_for(active, path):
    active, path = Path(active).resolve(), Path(path).resolve()
    if active == Path('C:/MediaDownloads/Incomplete/AnimeService').resolve() and path.is_relative_to(OLD_STAGE.parent.resolve()):
        return OLD_STAGE.resolve()
    return active


def torrent_staging(save):
    """An explicit per-torrent path keeps even incomplete new bytes on C."""
    if Path(save).resolve().is_relative_to(Path('C:/MediaLibrary/Anime').resolve()):
        return Path('C:/MediaDownloads/Incomplete')
    return None
