import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from scripts.finish_subtitle_episode import mapped_duration_check

class MappedDurationTests(unittest.TestCase):
    def check(self, original, encoded):
        values=[SimpleNamespace(stdout=json.dumps({'packets':p}).encode()) for p in (original,encoded)]
        with patch('scripts.finish_subtitle_episode.run',side_effect=values):
            return mapped_duration_check('probe','source','movie','cwd',{'format':{'duration':'1420.25'}},{'format':{'duration':'1420.002'}})
    def test_unselected_mkv_tail_does_not_reject_complete_video(self):
        packets=[{'pts_time':'0','duration_time':'.041'},{'pts_time':'1419.96','duration_time':'.041'}]
        result=self.check(packets,packets)
        self.assertTrue(result['mapped_video_checked'])
    def test_missing_frame_is_rejected(self):
        with self.assertRaisesRegex(AssertionError,'frame count'):
            self.check([{'pts_time':'0'},{'pts_time':'1'}],[{'pts_time':'0'}])
    def test_truncated_equal_count_video_is_rejected(self):
        with self.assertRaisesRegex(AssertionError,'duration'):
            self.check([{'pts_time':'0'},{'pts_time':'10'}],[{'pts_time':'0'},{'pts_time':'9'}])
    def test_unchanged_container_duration_keeps_existing_fast_path(self):
        with patch('scripts.finish_subtitle_episode.run') as process:
            result=mapped_duration_check('p','s','m','c',{'format':{'duration':'10'}},{'format':{'duration':'10.02'}})
            self.assertFalse(result['mapped_video_checked']);process.assert_not_called()
