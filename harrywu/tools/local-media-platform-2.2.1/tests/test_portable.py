"""Exercise the installed wheel from unrelated Unicode project directories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class PortableTests(unittest.TestCase):
    def test_cross_project_ingest_verify_and_read_only_resume(self):
        with tempfile.TemporaryDirectory(prefix='media_portable_') as temp:
            root = Path(temp)
            first, second = root / '项目甲', root / 'project B'
            first.mkdir()
            second.mkdir()
            source, output = first / '样本 video.mkv', first / 'evidence'
            subprocess.run(['ffmpeg', '-nostdin', '-y', '-loglevel', 'error', '-f', 'lavfi',
                '-i', 'testsrc2=size=320x180:rate=10:duration=3', '-c:v', 'libx264', str(source)],
                check=True, capture_output=True, timeout=30)
            env = {k:v for k,v in os.environ.items() if k not in ('PYTHONPATH', 'MEDIA_PLATFORM_HOME', 'MEDIA_PLATFORM_PROFILE')}
            def run(cwd, *args):
                r = subprocess.run([sys.executable, '-I', '-m', 'media_platform', *args], cwd=cwd,
                    env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                return r.stdout
            a, b = json.loads(run(first, 'doctor')), json.loads(run(second, 'doctor'))
            self.assertEqual(a['package'], b['package'])
            self.assertNotIn('推し活', a['package'])
            self.assertNotIn('SharedTools', a['package'])
            source_before = source.read_bytes()
            args = ('ingest', str(source), '--output', str(output), '--no-audio', '--no-ocr', '--device', 'cpu')
            run(first, *args)
            self.assertTrue(json.loads(run(second, 'verify', str(output)))['valid'])
            before = {p.relative_to(output).as_posix():p.read_bytes() for p in output.rglob('*') if p.is_file()}
            run(second, *args, '--resume')
            self.assertEqual(before, {p.relative_to(output).as_posix():p.read_bytes() for p in output.rglob('*') if p.is_file()})
            self.assertEqual(source_before, source.read_bytes())

    def test_shared_config_override_from_any_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            env = dict(os.environ, MEDIA_PLATFORM_HOME=temp)
            env.pop('MEDIA_PLATFORM_PROFILE', None)
            result = subprocess.run([sys.executable, '-I', '-m', 'media_platform', 'doctor'], cwd=temp,
                env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(Path(data['home']), Path(temp).resolve())
            self.assertEqual(Path(data['profile']), Path(temp).resolve() / 'device_profile.json')
            self.assertFalse(data['profile_exists'])
            self.assertEqual(list(Path(temp).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
