import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from scripts.finish_subtitle_episode import finalize_rendered, digest


class FinalizationTests(unittest.TestCase):
    def test_stale_receipt_and_offline_mac_stop_finalization(self):
        from scripts.finish_subtitle_episode import verified_movie_decode
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'data').mkdir()
            (root/'data/subtitle-render-worker.json').write_text('{"host":"test-mac"}')
            movie = root/'movie.mp4'
            movie.write_bytes(b'new')
            (root/'remote-render.json').write_text(json.dumps(dict(
                host='test-mac', stage='returned', decode=True, bytes=3, sha256='0'*64)))
            with patch('scripts.finish_subtitle_episode.PROJECT', root), patch(
                    'scripts.subtitle_remote_media.preparation', side_effect=OSError('offline')) as remote:
                with self.assertRaisesRegex(OSError, 'offline'):
                    verified_movie_decode({}, root, movie, 3, {})
                self.assertEqual(remote.call_args.args[:3], ('test-mac', movie, digest(movie)))
            self.assertEqual(movie.read_bytes(), b'new')
            self.assertFalse((root/'mac-final-decode.json').exists())

    def test_packages_completed_video_and_license_without_encoder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'fonts').mkdir();(root/'font-licenses').mkdir()
            for name in ('movie.mp4','source.mkv','bilingual.ass','static.ass','chinese-coverage-review.json','fonts/font.otf','font-licenses/OFL.txt','OFL.txt','config.json'):
                (root/name).write_text('fixture',encoding='utf-8')
            (root/'render.log').write_text('fontselect: x -> Dialogue, 0, Dialogue')
            c={'ffmpeg':'ffmpeg','ffprobe':'ffprobe','font_postscript_names':['Dialogue'],'sample_seconds':[],
               'font_licenses':[{'path':str(root/'OFL.txt'),'sha256':digest(root/'OFL.txt')}],
               'title':'title','notes':'notes','id':'test','show_id':1,'season':1,'episode':1,'version':0,
               'media_sha256':digest(root/'source.mkv'),'ass_sha256':digest(root/'bilingual.ass')}
            probe={'format':{'duration':'10'},'streams':[{'codec_type':'video','width':1920,'height':1080}]}
            commands=[]
            def run(args,cwd,timeout=120):
                commands.append(args)
                return SimpleNamespace(stdout=json.dumps(probe).encode() if args[0]=='ffprobe' else b'audio-hash',stderr=b'')
            source=root/'source.mkv';stat=source.stat()
            (root/'data').mkdir()
            (root/'data/subtitle-render-worker.json').write_text(json.dumps({'host':'test-mac'}))
            (root/'remote-render.json').write_text(json.dumps(dict(host='test-mac',stage='returned',
                decode=True,sha256=digest(root/'movie.mp4'),bytes=(root/'movie.mp4').stat().st_size)))
            with patch('scripts.finish_subtitle_episode.PROJECT',root), patch('scripts.finish_subtitle_episode.run',side_effect=run):
                finalize_rendered(root/'config.json',c,root,lambda stage:None,source,(stat.st_size,stat.st_mtime_ns),1920,root/'movie.mp4',[],{'seconds':1})
            result=json.loads((root/'release.json').read_text())
            self.assertIn(str(Path('font-licenses')/'OFL.txt'),result['files'])
            self.assertIn('movie.mp4',result['files'])
            self.assertFalse(any('libx264' in cmd for cmd in commands))
