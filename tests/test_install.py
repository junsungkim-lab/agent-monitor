"""install.py 가 기존 설정을 망가뜨리지 않는지 확인한다. (가짜 HOME 사용)"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, 'install.py')


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if k != 'CODEX_HOME'}
        env['HOME'] = self.home
        return subprocess.run([sys.executable, INSTALL, *args], text=True, capture_output=True, env=env, timeout=20)

    def path(self, *parts):
        return os.path.join(self.home, *parts)

    def write(self, rel, obj):
        p = self.path(*rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))
        return p

    def read(self, *rel):
        with open(self.path(*rel), encoding='utf-8') as fh:
            return json.load(fh)

    def ours(self, cfg):
        return [g for groups in cfg.get('hooks', {}).values() for g in groups
                if any('agent_hook.py' in h.get('command', '') for h in g.get('hooks', []))]

    def test_install_keeps_existing_settings_and_is_idempotent(self):
        mine = {'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'echo mine'}]}
        self.write(('.claude', 'settings.json'),
                   {'model': 'opus', 'permissions': {'allow': ['Bash(ls)']},
                    'hooks': {'PostToolUse': [mine], 'PreToolUse': [mine]}})
        r = self.run_cli('install', 'claude')
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = self.read('.claude', 'settings.json')
        self.assertEqual(cfg['model'], 'opus')
        self.assertEqual(cfg['permissions'], {'allow': ['Bash(ls)']})
        self.assertIn(mine, cfg['hooks']['PostToolUse'])
        self.assertEqual(cfg['hooks']['PreToolUse'], [mine])
        first = len(self.ours(cfg))
        self.assertEqual(first, 11)
        for ev in ('SessionStart', 'UserPromptSubmit', 'Stop', 'SubagentStart', 'SubagentStop', 'SessionEnd',
                   'Notification', 'PermissionRequest'):
            self.assertIn(ev, cfg['hooks'])
        self.assertNotIn('async', cfg['hooks']['SessionEnd'][0]['hooks'][0])
        self.assertTrue(cfg['hooks']['Stop'][0]['hooks'][0]['async'])
        backups = [f for f in os.listdir(self.path('.claude')) if '.bak-agent-monitor-' in f]
        self.assertEqual(len(backups), 1)

        self.assertEqual(self.run_cli('install', 'claude').returncode, 0)
        self.assertEqual(len(self.ours(self.read('.claude', 'settings.json'))), first)   # 두 번 깔아도 그대로

    def test_uninstall_restores_original_shape(self):
        original = {'model': 'opus', 'hooks': {'PreToolUse': [{'hooks': [{'type': 'command', 'command': 'echo mine'}]}]}}
        self.write(('.claude', 'settings.json'), original)
        self.run_cli('install', 'claude')
        self.assertEqual(self.run_cli('uninstall', 'claude').returncode, 0)
        self.assertEqual(self.read('.claude', 'settings.json'), original)

    def test_uninstall_drops_empty_hooks_key(self):
        self.write(('.gemini', 'settings.json'), {'theme': 'dark'})
        self.run_cli('install', 'gemini')
        cfg = self.read('.gemini', 'settings.json')
        self.assertEqual(len(self.ours(cfg)), 6)
        self.assertEqual(cfg['hooks']['AfterAgent'][0]['matcher'], '*')
        self.run_cli('uninstall', 'gemini')
        self.assertEqual(self.read('.gemini', 'settings.json'), {'theme': 'dark'})

    def test_all_skips_missing_clis_but_explicit_creates(self):
        os.makedirs(self.path('.codex'))
        r = self.run_cli('install')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(self.path('.codex', 'hooks.json')))
        self.assertFalse(os.path.exists(self.path('.claude')))
        self.assertFalse(os.path.exists(self.path('.gemini')))
        codex = self.read('.codex', 'hooks.json')
        self.assertEqual(len(self.ours(codex)), 9)
        self.assertEqual(codex['hooks']['SessionEnd'][0]['hooks'][0]['timeout'], 2)

        self.assertEqual(self.run_cli('install', 'gemini').returncode, 0)
        self.assertTrue(os.path.exists(self.path('.gemini', 'settings.json')))

    def test_dry_run_changes_nothing(self):
        p = self.write(('.claude', 'settings.json'), {'model': 'opus'})
        with open(p, encoding='utf-8') as fh:
            before = fh.read()
        r = self.run_cli('install', 'claude', '--dry-run')
        self.assertEqual(r.returncode, 0)
        with open(p, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual([f for f in os.listdir(self.path('.claude')) if 'bak' in f], [])

    def test_unparseable_config_is_left_alone(self):
        text = '{\n  // 주석이 있는 설정\n  "model": "opus"\n}\n'
        p = self.write(('.claude', 'settings.json'), text)
        r = self.run_cli('install', 'claude')
        self.assertNotEqual(r.returncode, 0)
        with open(p, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), text)

    def test_custom_url_is_baked_into_command(self):
        os.makedirs(self.path('.gemini'))
        self.run_cli('install', 'gemini', '--url', 'http://127.0.0.1:9999')
        cmd = self.read('.gemini', 'settings.json')['hooks']['AfterAgent'][0]['hooks'][0]['command']
        self.assertIn('--url http://127.0.0.1:9999', cmd)
        self.assertIn(sys.executable, cmd)

    def test_print_outputs_valid_json(self):
        r = self.run_cli('print', 'codex')
        self.assertEqual(r.returncode, 0)
        body = r.stdout.split('\n', 1)[1]
        hooks = json.loads(body)['hooks']
        self.assertIsInstance(hooks['Stop'], list)          # 그대로 붙여넣을 수 있는 모양
        self.assertEqual(hooks['Stop'][0]['hooks'][0]['type'], 'command')



class StartTest(unittest.TestCase):
    """start.py: 훅 연결과 서버 실행을 한 번에."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, '.claude'))
        with open(os.path.join(self.home, '.claude', 'settings.json'), 'w', encoding='utf-8') as fh:
            json.dump({'model': 'opus'}, fh)

    def tearDown(self):
        self.tmp.cleanup()

    def free_port(self):
        import socket
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def start(self, port, *args):
        env = {k: v for k, v in os.environ.items() if k != 'CODEX_HOME'}
        env.update(HOME=self.home, PYTHONUNBUFFERED='1')
        return subprocess.Popen([sys.executable, os.path.join(ROOT, 'start.py'), '--port', str(port), '--quiet', *args],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)

    def wait_up(self, port):
        import time
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(50):
            try:
                return json.loads(opener.open('http://127.0.0.1:%d/api/state' % port, timeout=1).read())
            except OSError:
                time.sleep(0.1)
        self.fail('server did not start')

    def settings(self):
        with open(os.path.join(self.home, '.claude', 'settings.json'), encoding='utf-8') as fh:
            return json.load(fh)

    def backups(self):
        return [f for f in os.listdir(os.path.join(self.home, '.claude')) if '.bak-agent-monitor-' in f]

    def stop(self, proc):
        proc.terminate()
        out = proc.communicate(timeout=5)[0]
        return out

    def test_start_installs_hooks_once_and_serves(self):
        port = self.free_port()
        proc = self.start(port)
        try:
            self.assertTrue(self.wait_up(port)['ok'])
        finally:
            out = self.stop(proc)
        cfg = self.settings()
        self.assertEqual(cfg['model'], 'opus')
        self.assertIn('Stop', cfg['hooks'])
        self.assertIn('훅 11개 등록', out)
        self.assertEqual(len(self.backups()), 1)

        proc = self.start(port)                      # 두 번째 실행: 설정을 다시 쓰지 않는다
        try:
            self.wait_up(port)
        finally:
            out = self.stop(proc)
        self.assertIn('이미 연결되어 있습니다', out)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.settings(), cfg)

    def test_custom_port_is_baked_into_hooks(self):
        port = self.free_port()
        proc = self.start(port)
        try:
            self.wait_up(port)
        finally:
            self.stop(proc)
        cmd = self.settings()['hooks']['Stop'][0]['hooks'][0]['command']
        self.assertIn('--url http://127.0.0.1:%d' % port, cmd)

    def test_no_hooks_leaves_config_alone(self):
        port = self.free_port()
        proc = self.start(port, '--no-hooks')
        try:
            self.wait_up(port)
        finally:
            self.stop(proc)
        self.assertEqual(self.settings(), {'model': 'opus'})

    def test_port_in_use_gives_a_plain_message(self):
        port = self.free_port()
        first = self.start(port, '--no-hooks')
        try:
            self.wait_up(port)
            second = self.start(port, '--no-hooks')
            out = second.communicate(timeout=10)[0]
            self.assertEqual(second.returncode, 1)
            self.assertIn('열 수 없습니다', out)
            self.assertNotIn('Traceback', out)
        finally:
            self.stop(first)


if __name__ == '__main__':
    unittest.main()
