import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from scripts.subtitle_render_fonts import validate


class RenderFontTests(unittest.TestCase):
    def test_strict_without_review(self):
        validate(['fontselect: x -> Dialogue, 0, Dialogue'], ['Dialogue'], None, None)
        with self.assertRaises(AssertionError):
            validate(['fontselect: x -> Other, 0, Other'], ['Dialogue'], None, None)

    def test_output_bound_symbol_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def digest(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
            for name in ('input-config.json', 'bilingual.ass', 'static.ass', 'render.log', 'movie.mp4', 'frame.png'):
                (root/name).write_text('❤', encoding='utf-8')
            (root/'bilingual.ass').write_text('Dialogue: 0,0:00:01.00,0:00:02.00,JA,J1,0,0,0,,❤\nDialogue: 1,0:00:01.00,0:00:02.00,JA,J1,0,0,0,,{\\blur1}❤\n',encoding='utf-8')
            r = {'role':'rendered_symbol_fallback_review','reviewer':'test','extra_postscript_names':['SegoeUIEmoji'],
                 'codepoints':['U+2764'],'files':{n:digest(root/n) for n in ('input-config.json','bilingual.ass','static.ass','render.log')},
                 'movie':{'name':'movie.mp4','sha256':digest(root/'movie.mp4')},
                 'evidence':[{'path':str(root/'frame.png'),'sha256':digest(root/'frame.png')}],
                 'full_size_and_small_player_verified':True}
            p=root/'review.json'; p.write_text(json.dumps(r))
            logs=['fontselect: x -> Dialogue, 0, Dialogue','fontselect: x -> SegoeUIEmoji, 0, SegoeUIEmoji']
            validate(logs,['Dialogue'],root,digest,p)
            with self.assertRaises(AssertionError): validate(logs,['Dialogue'],root,digest)
            (root/'movie.mp4').write_text('changed')
            with self.assertRaises(AssertionError): validate(logs,['Dialogue'],root,digest,p)
            (root/'movie.mp4').write_text('❤',encoding='utf-8')
            (root/'frame.png').write_text('changed')
            with self.assertRaises(AssertionError): validate(logs,['Dialogue'],root,digest,p)

def test_chinese_v0_requires_only_the_font_used_by_reviewed_visible_styles(tmp_path):
    from scripts.subtitle_render_fonts import required_for_render
    ass = tmp_path/'bilingual.ass'
    header = ('[V4+ Styles]\nFormat: Name, Fontname\nStyle: ZH,Noto Sans CJK SC Medium\n'
              'Style: JA,Noto Sans CJK JP Medium\n[Events]\nFormat: Style, Text\n')
    (tmp_path/'chinese-coverage-review.json').write_text(json.dumps({'styles':{'ZH':'zh','JA':'source'}}))
    fonts = ['NotoSansCJKsc-Medium','NotoSansCJKjp-Medium']
    config = {'edition':'v0','font_postscript_names':fonts}
    for text in ('Dialogue: ZH,你好','Dialogue: JA,こんにちは','Dialogue: ZH,{\\rJA}你好',
                 'Dialogue: ZH,{\\fnOther}你好','Dialogue: Unreviewed,你好'):
        ass.write_text(header+text+'\n',encoding='utf-8')
        assert required_for_render(config,tmp_path) == (fonts[:1] if text=='Dialogue: ZH,你好' else fonts)
        assert required_for_render({**config,'edition':'v1'},tmp_path)==fonts
