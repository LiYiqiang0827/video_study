import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect_posts import normalize_post


class NormalizePostTests(unittest.TestCase):
    def test_text_id_video_and_image_are_preserved(self):
        thread = {"post": {"backstagePostRenderer": {
            "postId": "Ugk-test",
            "contentText": {"runs": [{"text": "抵扣价观察"}, {"text": "，关注均线。"}]},
            "publishedTimeText": {"simpleText": "2天前"},
            "backstageAttachment": {
                "imageRenderer": {"image": {"thumbnails": [{"url": "https://yt3.ggpht.com/photo.jpg"}]}},
                "videoRenderer": {"videoId": "abc123"},
            },
        }}}
        post = normalize_post(thread, "https://www.youtube.com/@yuncong")
        self.assertEqual(post["post_id"], "Ugk-test")
        self.assertEqual(post["text"], "抵扣价观察，关注均线。")
        self.assertEqual(post["published_time_raw"], "2天前")
        self.assertEqual(post["video_ids"], ["abc123"])
        self.assertEqual(post["image_urls"], ["https://yt3.ggpht.com/photo.jpg"])

    def test_missing_id_gets_stable_hash_id(self):
        thread = {"post": {"backstagePostRenderer": {"contentText": {"simpleText": "文字"}}}}
        first = normalize_post(thread, "https://www.youtube.com/@yuncong")
        second = normalize_post(thread, "https://www.youtube.com/@yuncong")
        self.assertTrue(first["post_id"].startswith("sha256-"))
        self.assertEqual(first["post_id"], second["post_id"])

    def test_video_thumbnail_is_not_treated_as_post_image(self):
        thread = {"post": {"backstagePostRenderer": {
            "contentText": {"simpleText": "视频动态"},
            "backstageAttachment": {"videoRenderer": {
                "videoId": "abc123", "thumbnail": {"thumbnails": [{"url": "https://i.ytimg.com/vi/abc123/hqdefault.jpg"}]}
            }},
        }}}
        post = normalize_post(thread, "https://www.youtube.com/@yuncong")
        self.assertEqual(post["image_urls"], [])
        self.assertEqual(post["video_ids"], ["abc123"])


if __name__ == "__main__":
    unittest.main()
