import sys
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect_posts import download_image, normalize_post
from collect_posts_via_macmini import CollectionError, verify_manifest


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

    def test_image_download_rejects_non_youtube_host_before_network(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "image_url_host_not_allowed"):
                download_image("https://example.com/image.jpg", Path(temp) / "image")

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


class SyncManifestTests(unittest.TestCase):
    def test_valid_manifest_passes_and_modified_file_fails(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            content = b'{"posts":[]}'
            (folder / "posts.json").write_bytes(content)
            manifest = {"schema_version": 1, "files": [{"path": "posts.json", "bytes": len(content),
                                                        "sha256": hashlib.sha256(content).hexdigest()}]}
            (folder / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(verify_manifest(folder), manifest)
            (folder / "posts.json").write_bytes(content + b" ")
            with self.assertRaises(CollectionError):
                verify_manifest(folder)

if __name__ == "__main__":
    unittest.main()
