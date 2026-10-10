"""Real-decoder checks for frame timing and authenticated resume."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from media_platform.evidence import sha256_file
from media_platform.frame_review import sample_frame


class FrameReviewTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="frame_review_test_")
        self.root = Path(self.scratch.name)
        self.source = self.root / "test.mkv"
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi",
            "-i", "testsrc2=size=320x180:rate=10:duration=6", "-c:v", "libx264",
            str(self.source)], check=True, capture_output=True, timeout=30)

    def tearDown(self):
        self.scratch.cleanup()

    def call(self, times="0.5,3.3,5.8", resume=False):
        command = [sys.executable, "-m", "media_platform.frame_review", str(self.source),
                   "--output", str(self.root / "review"), "--times", times]
        if resume:
            command.append("--resume")
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=45)

    def test_measured_timestamps_and_source_immutability(self):
        before = sha256_file(self.source)
        for second in (.5, 3.3, 5.8):
            row = sample_frame(self.source, second, self.root / f"{second}.jpg")
            self.assertAlmostEqual(row["time"], second, delta=.11)
            self.assertEqual(row["sha256"], sha256_file(self.root / row["frame"]))
        self.assertEqual(before, sha256_file(self.source))

    def test_resume_preserves_frames_and_rejects_changed_parameters(self):
        result = self.call()
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.root / "review"
        frames = {p.name: (p.stat().st_mtime_ns, sha256_file(p)) for p in target.glob("frame_*.jpg")}
        result = self.call(resume=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(frames, {p.name: (p.stat().st_mtime_ns, sha256_file(p)) for p in target.glob("frame_*.jpg")})
        before = sha256_file(target / "frame_manifest.json")
        self.assertNotEqual(self.call(times="1,2", resume=True).returncode, 0)
        self.assertEqual(before, sha256_file(target / "frame_manifest.json"))

    def test_tampered_frame_is_rejected_before_resume_write(self):
        self.assertEqual(self.call().returncode, 0)
        target = self.root / "review"
        frame = next(target.glob("frame_*.jpg"))
        frame.write_bytes(b"changed")
        before = sha256_file(target / "frame_manifest.json")
        result = self.call(resume=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Recorded frame changed", result.stderr)
        self.assertEqual(before, sha256_file(target / "frame_manifest.json"))


if __name__ == "__main__":
    unittest.main()
