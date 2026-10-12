import sys
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from collect_posts import download_image, normalize_post
from collect_posts_via_macmini import CollectionError, verify_manifest
from archive_posts_by_month import (
    _month, _month_assignment, _relative_time_estimate, archive_snapshot, refresh_manifests,
)


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

    def test_only_highest_resolution_variant_is_selected(self):
        thread = {"post": {"backstagePostRenderer": {
            "backstageAttachment": {"imageRenderer": {"image": {"thumbnails": [
                {"url": "https://yt3.ggpht.com/photo_small.jpg", "width": 120, "height": 90},
                {"url": "https://yt3.ggpht.com/photo_large.jpg", "width": 1200, "height": 900},
            ]}}}
        }}}
        post = normalize_post(thread, "https://www.youtube.com/@yuncong")
        self.assertEqual(post["image_urls"], ["https://yt3.ggpht.com/photo_large.jpg"])

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


class MonthlyArchiveTests(unittest.TestCase):
    def test_month_uses_youtube_timestamp_in_beijing(self):
        # 2026-09-30 16:30 UTC is already October 1 in Beijing.
        self.assertEqual(_month({"published_timestamp": "1790785800000000"}), "2026-10")

    def test_relative_month_is_marked_as_estimated(self):
        month, precision = _month_assignment(
            {"published_timestamp": None, "published_time_raw": "1 month ago (edited)"},
            "2026-10-12T00:00:00+00:00",
        )
        self.assertEqual(month, "2026-09")
        self.assertEqual(precision, "estimated_from_relative_display")

    def test_relative_year_is_not_falsely_assigned_to_a_month(self):
        month, precision = _month_assignment(
            {"published_timestamp": None, "published_time_raw": "1 year ago"},
            "2026-10-12T00:00:00+00:00",
        )
        self.assertEqual(month, "未识别日期")
        self.assertEqual(precision, "unknown")

    def test_relative_hour_produces_an_estimated_range_not_an_exact_timestamp(self):
        estimate = _relative_time_estimate(
            {"published_timestamp": None, "published_time_raw": "12 hours ago"},
            "2026-10-12T15:44:00.097072+00:00",
        )
        self.assertEqual(estimate["estimated_published_at_start"], "2026-10-12T10:44+08:00")
        self.assertEqual(estimate["estimated_published_at_end"], "2026-10-12T11:44+08:00")
        self.assertEqual(estimate["estimated_published_at_precision"], "relative_hour")

    def test_relative_day_and_week_produce_calendar_date_ranges(self):
        reference = "2026-10-12T15:44:00+00:00"
        day = _relative_time_estimate({"published_time_raw": "5 days ago"}, reference)
        week = _relative_time_estimate({"published_time_raw": "3 weeks ago"}, reference)
        self.assertEqual(day["estimated_published_at_start"], "2026-10-06T23:44+08:00")
        self.assertEqual(day["estimated_published_at_end"], "2026-10-07T23:44+08:00")
        self.assertEqual(week["estimated_published_at_start"], "2026-09-14T23:44+08:00")
        self.assertEqual(week["estimated_published_at_end"], "2026-09-21T23:44+08:00")

    def test_month_relative_label_does_not_claim_a_specific_date(self):
        self.assertEqual(_relative_time_estimate({"published_time_raw": "1 month ago"},
                                                  "2026-10-12T15:44:00+00:00"), {})

    def test_snapshot_is_partitioned_into_month_folders(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            posts = [
                {"post_id": "one", "published_timestamp": "1790785800000000", "text": "10月"},
                {"post_id": "two", "published_timestamp": "1789441800000000", "text": "9月"},
            ]
            (snapshot / "posts.json").write_text(json.dumps({"channel_url": "https://www.youtube.com/channel/UCg9zI64pEB_3xwJPur6isiQ", "collected_at": "2026-10-12T00:00:00+00:00", "posts": posts}), encoding="utf-8")
            result = archive_snapshot(snapshot, root / "云聪帖子全集")
            self.assertEqual([item["month"] for item in result["months"]], ["2026-09", "2026-10"])
            october = json.loads((root / "云聪帖子全集/2026-10/posts.json").read_text(encoding="utf-8"))
            self.assertEqual(october["posts"][0]["post_id"], "one")

    def test_ppt_manifest_is_at_archive_root_and_month_manifests_stay_monthly(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "云聪帖子全集"
            month = root / "2026-10"
            month.mkdir(parents=True)
            (root / "云聪帖子_2026-10.pptx").write_bytes(b"ppt")
            (root / ".DS_Store").write_bytes(b"finder metadata")
            (month / "posts.json").write_text(json.dumps({"posts": []}), encoding="utf-8")
            (month / "image.jpg").write_bytes(b"image")

            refresh_manifests(root)

            root_manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
            month_manifest = json.loads((month / "MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual([item["path"] for item in root_manifest["files"]], ["云聪帖子_2026-10.pptx"])
            self.assertEqual({item["path"] for item in month_manifest["files"]}, {"image.jpg", "posts.json"})

if __name__ == "__main__":
    unittest.main()
