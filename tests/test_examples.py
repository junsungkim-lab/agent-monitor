"""examples/reel-team: make_reel.py 와 replay.py."""
import importlib.util
import os
import struct
import subprocess
import sys
import tempfile
import unittest

from test_server import ROOT, Base

EX = os.path.join(ROOT, 'examples', 'reel-team')


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(EX, name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def box(kind, payload):
    return struct.pack('>I', 8 + len(payload)) + kind + payload


class MakeReel(unittest.TestCase):
    def test_probe_reads_duration_and_size_from_the_mp4_header(self):
        mvhd = box(b'mvhd', bytes(12) + struct.pack('>II', 1000, 5200) + bytes(80))
        tkhd = lambda w, h: box(b'tkhd', bytes(76) + struct.pack('>II', w << 16, h << 16))
        moov = box(b'moov', mvhd + box(b'trak', tkhd(0, 0)) + box(b'trak', tkhd(1080, 1920)))
        with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False) as f:
            f.write(box(b'ftyp', b'isom' + bytes(4)) + moov + box(b'mdat', bytes(64)))
        try:
            self.assertEqual(load('make_reel').probe(f.name), (5.2, 1080, 1920))
        finally:
            os.unlink(f.name)

    def test_hello_prints_the_name_tag_for_the_monitor(self):
        r = subprocess.run([sys.executable, os.path.join(EX, 'make_reel.py'), '3', '--hello'],
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0)
        self.assertIn('[agent-monitor] name: 홈트 서윤', r.stdout)
        r = subprocess.run([sys.executable, os.path.join(EX, 'make_reel.py'), '99'], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 2)

    def test_without_video_files_it_still_reports_a_result_line(self):
        import shutil
        tmp = tempfile.mkdtemp(prefix='reel-')
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in ('make_reel.py', 'hosts.json'):                 # 저장소를 막 받은 상태: media/ 에 영상 없음
            shutil.copy(os.path.join(EX, name), tmp)
        r = subprocess.run([sys.executable, os.path.join(tmp, 'make_reel.py'), '1'], capture_output=True, text=True,
                           timeout=20, env=dict(os.environ, REEL_SPEED='1000'))
        self.assertEqual(r.returncode, 0)
        self.assertIn('[agent-monitor] text: 재테크 민지 릴스 · 영상 파일 없음', r.stdout)
        self.assertNotIn('완성:', r.stdout)


class Replay(Base):
    def test_replay_puts_a_lead_and_eight_hosts_with_results_on_the_monitor(self):
        import contextlib
        import io
        replay = load('replay')
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(replay.main(['--url', 'http://127.0.0.1:%d' % self.port, '--speed', '2000']), 0)
        ag = self.agents()
        self.assertEqual(ag['custom:reel-pd']['state'], 'waiting')
        hosts = [a for a in ag.values() if a['parent'] == 'custom:reel-pd']
        self.assertEqual(len(hosts), 8)
        with_video = {'custom:host-%d' % n for n in range(1, 9)
                      if any(f.startswith('%d_' % n) for f in os.listdir(os.path.join(EX, 'media')))}
        self.assertTrue(all(a['state'] == 'reported' and len(a['media']) == (a['id'] in with_video) for a in hosts))
        self.assertEqual(ag['custom:host-3']['name'], '홈트 서윤')


if __name__ == '__main__':
    unittest.main()
